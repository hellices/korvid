"""Eval runner: drives the real AgentRuntime over scenario fixtures (issue #69).

The unit under test is the **model + runtime**: the provider is live (or
scripted in CI smoke tests), the cluster is simulated, and every run is
scored by the deterministic grader plus behavioral metrics captured from
the typed AgentEvent stream.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from typing import Any

from korvid.agent.events import (
    AgentError,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from korvid.agent.profiles import AgentProfile, build_profile
from korvid.agent.runtime import AgentRuntime
from korvid.agent.tools import READ_TOOLS, UI_TOOL_NAMES, WRITE_TOOL_NAMES, compact_result
from korvid.evals.grader import GradeResult, ToolRecord, grade, matches_target
from korvid.evals.scenario import Scenario

#: Runs per scenario per configuration (issue #69: report variance, not means).
DEFAULT_REPETITIONS = 3


def _eval_tools(profile: AgentProfile) -> list[dict[str, Any]]:
    """Schemas offered to the model for one capability profile (issue #71).

    Write schemas are included so a live structured-tool provider can
    actually *choose* a write (making the write-attempt/safety metrics
    meaningful); safety comes from the executor, which has no UI bridge in
    eval runs, so every write call fails at dispatch. UI tools are excluded
    for the same reason — there is no screen to drive, and the grader
    counts names outside the read/write surface as malformed.
    """
    return [t for t in profile.tools if t["function"]["name"] not in UI_TOOL_NAMES]


class _RecordingExecutor:
    """Wraps the real executor to keep full tool results for evidence grading
    (the runtime's ToolCallFinished summary is truncated to 120 chars).

    The profile's per-result cap is applied *before* recording: grading must
    only credit evidence the model could actually have seen, so the recorded
    content matches what reaches the conversation. `compact_result` is
    idempotent, so the runtime re-applying the same cap changes nothing.
    """

    def __init__(self, executor: Any, max_result_chars: int | None = None) -> None:
        self._executor = executor
        self._max_result_chars = max_result_chars
        self.records: list[ToolRecord] = []

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        result: str = await self._executor.execute(name, arguments)
        if self._max_result_chars is not None:
            result = compact_result(result, self._max_result_chars)
        self.records.append(ToolRecord(name=name, arguments=dict(arguments), result=result))
        return result


class _CountingProvider:
    """Counts provider round-trips — the iteration metric for a run."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self.completions = 0

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        self.completions += 1
        result: AsyncIterator[dict[str, Any]] = self._provider.complete(
            messages, tools, stream=stream
        )
        return result

    def __getattr__(self, name: str) -> Any:
        # Pass through provider attributes the runtime may consult (e.g.
        # a future provider.name in logging) — counting wraps complete only.
        return getattr(self._provider, name)


@dataclass(frozen=True)
class RunMetrics:
    """One repetition's grade plus behavioral metrics from the event stream."""

    grade: GradeResult
    #: Text streamed after the last tool call — the final answer segment.
    answer: str
    iterations: int
    tool_calls: int
    #: Read calls that were schema-valid AND resolved against the cluster
    #: without an ERROR result. Measures execution quality, not diagnostic
    #: relevance.
    resolvable_tool_calls: int
    #: Calls whose arguments name a scenario evidence target — issue #69's
    #: correct-tool + correct-argument numerator (denominator: tool_calls).
    on_target_tool_calls: int
    #: Bad-JSON arguments or unknown tool names.
    malformed_tool_calls: int
    #: Write-tool calls attempted (they must all fail in an eval session).
    write_attempts: int
    #: Write-tool calls that *succeeded* without an approval path — must be 0.
    safety_violations: int
    input_tokens: int
    output_tokens: int
    #: True when any iteration lacked provider usage — the token totals
    #: above are heuristic estimates, not billing-exact numbers.
    tokens_estimated: bool
    wall_time_s: float
    error: str | None


@dataclass(frozen=True)
class ScenarioReport:
    """All repetitions of one scenario under one configuration."""

    scenario_id: str
    root_cause: str
    runs: list[RunMetrics]

    @property
    def successes(self) -> int:
        return sum(1 for run in self.runs if run.grade.diagnosis_success)

    @property
    def evidence_hits(self) -> int:
        return sum(1 for run in self.runs if run.grade.evidence_fetched)


#: Required parameters per read tool, from the schemas the model is offered.
_READ_REQUIRED: dict[str, frozenset[str]] = {
    tool["function"]["name"]: frozenset(tool["function"]["parameters"].get("required", ()))
    for tool in READ_TOOLS
}

_READ_PROPERTY_TYPES: dict[str, dict[str, str]] = {
    tool["function"]["name"]: {
        prop: spec.get("type", "string")
        for prop, spec in tool["function"]["parameters"].get("properties", {}).items()
    }
    for tool in READ_TOOLS
}

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {"string": str, "integer": int}


def _value_matches_schema(value: Any, json_type: str) -> bool:
    expected = _JSON_TYPES.get(json_type)
    if expected is None:
        return True
    # bool is an int subtype in Python but not an integer in JSON schema.
    if isinstance(value, bool):
        return json_type == "boolean"
    return isinstance(value, expected)


def _is_malformed(name: str, raw_arguments: str) -> bool:
    """Schema-level validation of one tool call, from the raw call the model
    emitted: undecodable or non-mapping arguments, a tool name that was never
    offered (offered write tools are exempt — they are tracked separately as
    write attempts), a missing required parameter, or a declared parameter of
    the wrong type."""
    try:
        parsed = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError:
        return True
    if not isinstance(parsed, dict):
        return True
    required = _READ_REQUIRED.get(name)
    if required is None:
        return name not in WRITE_TOOL_NAMES
    if not required <= parsed.keys():
        return True
    property_types = _READ_PROPERTY_TYPES[name]
    return any(
        key in property_types and not _value_matches_schema(value, property_types[key])
        for key, value in parsed.items()
    )


async def _run_once(
    scenario: Scenario,
    provider_factory: Callable[[], Any],
    executor_factory: Callable[[], Any],
    profile_name: str = "full",
) -> RunMetrics:
    raw_provider = provider_factory()
    try:
        return await _drive_turn(scenario, raw_provider, executor_factory(), profile_name)
    finally:
        # Live providers own an httpx client; close it per repetition or a
        # full pack run leaks one client per run (the app calls aclose()
        # on shutdown for the same reason). Scripted providers have none.
        aclose = getattr(raw_provider, "aclose", None)
        if callable(aclose):
            await aclose()


@dataclass
class _TurnTally:
    """Mutable accumulator for one turn's event stream."""

    answer: str = ""
    tool_calls: int = 0
    malformed: int = 0
    write_attempts: int = 0
    safety_violations: int = 0
    error: str | None = None

    def note(self, event: Any) -> None:
        if isinstance(event, TextDelta):
            self.answer += event.text
        elif isinstance(event, ToolCallStarted):
            if _is_malformed(event.name, event.arguments):
                self.malformed += 1
        elif isinstance(event, ToolCallFinished):
            self.tool_calls += 1
            # Narration before a tool call is hypothesis, not diagnosis:
            # only text after the final tool call is graded as the answer.
            self.answer = ""
            if event.name in WRITE_TOOL_NAMES:
                self.write_attempts += 1
                if event.ok:
                    self.safety_violations += 1
        elif isinstance(event, AgentError):
            self.error = event.message


async def _drive_turn(
    scenario: Scenario,
    raw_provider: Any,
    raw_executor: Any,
    profile_name: str = "full",
) -> RunMetrics:
    provider = _CountingProvider(raw_provider)
    profile = build_profile(profile_name, readonly=False, resize_supported=True)
    executor = _RecordingExecutor(raw_executor, max_result_chars=profile.max_result_chars)
    runtime = AgentRuntime(
        provider,
        executor,
        tools=_eval_tools(profile),
        max_iterations=profile.max_iterations,
        max_history_chars=profile.max_history_chars,
        max_result_chars=profile.max_result_chars,
        max_tool_calls_per_iteration=profile.max_tool_calls_per_iteration,
        system_prompt=profile.system_prompt,
    )
    tally = _TurnTally()
    started = time.monotonic()
    async for event in runtime.run_turn(scenario.question, scenario.screen):
        tally.note(event)
    wall_time = time.monotonic() - started
    grade_result = grade(scenario, tally.answer, executor.records)
    if tally.error is not None:
        # A provider may stream a plausible answer and then fail; an
        # errored turn never counts as a diagnostic success.
        grade_result = replace(grade_result, diagnosis_success=False)
    # The runtime is fresh per run, so its cumulative totals equal this
    # run's spend — and unlike TurnComplete (never emitted on a provider
    # error) they include tokens paid for before a mid-turn failure.
    in_tokens, out_tokens = runtime.total_tokens
    # A call is resolvable when it is an offered *read* tool (a write call
    # is a policy failure regardless of outcome), is schema-valid, AND its
    # arguments resolved in the cluster (the executor returns 'ERROR: ...'
    # otherwise). Diagnostic relevance is graded via expected_evidence.
    resolvable_calls = sum(
        1
        for record in executor.records
        if record.name in _READ_REQUIRED
        and not record.result.startswith("ERROR:")
        and not _is_malformed(record.name, json.dumps(record.arguments))
    )
    # Issue #69's correct-tool + correct-argument rate: a call is on-target
    # when it is an offered *read* tool whose arguments name one of the
    # scenario's evidence targets, regardless of what its result contained.
    # Writes never count — matches_target ignores the tool name, and a
    # mutation aimed at the right object is a policy failure, not evidence.
    on_target_calls = sum(
        1
        for record in executor.records
        if record.name in _READ_REQUIRED
        and any(
            matches_target(alt, record) for group in scenario.expected_evidence for alt in group
        )
    )
    return RunMetrics(
        grade=grade_result,
        answer=tally.answer,
        iterations=provider.completions,
        tool_calls=tally.tool_calls,
        resolvable_tool_calls=resolvable_calls,
        on_target_tool_calls=on_target_calls,
        malformed_tool_calls=tally.malformed,
        write_attempts=tally.write_attempts,
        safety_violations=tally.safety_violations,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        tokens_estimated=runtime.usage_estimated,
        wall_time_s=wall_time,
        error=tally.error,
    )


async def run_scenario(
    scenario: Scenario,
    *,
    provider_factory: Callable[[], Any],
    executor_factory: Callable[[], Any],
    repetitions: int = DEFAULT_REPETITIONS,
    profile: str = "full",
) -> ScenarioReport:
    """Run one scenario ``repetitions`` times with fresh state per run."""
    runs = [
        await _run_once(scenario, provider_factory, executor_factory, profile)
        for _ in range(repetitions)
    ]
    return ScenarioReport(scenario_id=scenario.id, root_cause=scenario.root_cause, runs=runs)


def _fmt_seconds(values: list[float]) -> str:
    if len(values) > 1:
        return f"{statistics.mean(values):.1f}±{statistics.stdev(values):.1f}"
    return f"{values[0]:.1f}" if values else "-"


def _mean_sd(values: list[float]) -> str:
    """Mean with sample standard deviation — the issue calls for variance
    across repetitions, not just means (1/5 vs 3/3 must be visible)."""
    if not values:
        return "-"
    mean = statistics.mean(values)
    if len(values) < 2:
        return f"{mean:.1f}"
    return f"{mean:.1f}±{statistics.stdev(values):.1f}"


def render_markdown(reports: list[ScenarioReport]) -> str:
    """Markdown summary table: one row per scenario, variance included."""
    lines = [
        "| scenario | root cause | success | evidence | resolvable calls | on-target | "
        "malformed | writes | safety | iterations | tokens in/out | wall s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for report in reports:
        runs = report.runs
        n = len(runs)
        malformed = sum(run.malformed_tool_calls for run in runs)
        resolvable = sum(run.resolvable_tool_calls for run in runs)
        total_calls = sum(run.tool_calls for run in runs)
        # The issue's invariant is a malformed *rate* (< 1%), so the
        # denominator has to be visible.
        rate = f" ({100 * malformed / total_calls:.1f}%)" if total_calls else ""
        resolvable_rate = f" ({100 * resolvable / total_calls:.1f}%)" if total_calls else ""
        on_target = sum(run.on_target_tool_calls for run in runs)
        on_target_rate = f" ({100 * on_target / total_calls:.1f}%)" if total_calls else ""
        # Attempted mutations stay visible even though the unarmed executor
        # keeps the safety column at zero.
        writes = sum(run.write_attempts for run in runs)
        safety = sum(run.safety_violations for run in runs)
        iterations = _mean_sd([float(run.iterations) for run in runs])
        tokens_in = _mean_sd([float(run.input_tokens) for run in runs])
        tokens_out = _mean_sd([float(run.output_tokens) for run in runs])
        # Estimated totals (provider omitted stream usage) must not read
        # as billing-exact numbers in model comparisons.
        token_mark = "~" if any(run.tokens_estimated for run in runs) else ""
        wall = _fmt_seconds([run.wall_time_s for run in runs])
        lines.append(
            f"| {report.scenario_id} | {report.root_cause} | {report.successes}/{n} |"
            f" {report.evidence_hits}/{n} | {resolvable}/{total_calls}{resolvable_rate} |"
            f" {on_target}/{total_calls}{on_target_rate} |"
            f" {malformed}/{total_calls}{rate} | {writes} | {safety} |"
            f" {iterations} | {token_mark}{tokens_in}/{tokens_out} | {wall} |"
        )
    return "\n".join(lines)
