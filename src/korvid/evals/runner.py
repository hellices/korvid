"""Eval runner: drives the production agent session over scenario fixtures.

The unit under test is the **model + the session korvid actually runs**
(issue #69, issue #316 task 13): the provider is live (or scripted in CI
smoke tests), the graph is the one `korvid.__main__` composes, the cluster
is simulated, and every run is scored by the deterministic grader plus
behavioral metrics captured from the typed AgentEvent stream.

Two things an eval decides, both as inputs to that graph: the workspace
each scenario starts from (its authored `interaction`, applied through an
`EvalUiBridge`) and the resolved policy (`--model-tier`, or automatic
routing). Writes are never armed, so a write request is refused by the
tool harness and never reaches the executor or an approval dialog.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from korvid.agent.events import (
    AgentError,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from korvid.agent.interaction import ClusterFacts, InteractionContext
from korvid.agent.model_policy import ResolvedAgentPolicy
from korvid.agent.outbound import sanitize_recorded_tool_result
from korvid.evals.grader import (
    CitationReport,
    GradeResult,
    ToolRecord,
    citation_report,
    grade,
    matches_target,
)
from korvid.evals.harness import (
    EVAL_CLUSTER,
    NO_GRIND,
    EvalHarness,
    PromptGrind,
    build_eval_harness,
    resolve_eval_policy,
)
from korvid.evals.interaction import EvalUiBridge
from korvid.evals.outcome import classify_outcome
from korvid.evals.scenario import Scenario
from korvid.tools.executor import (
    WRITE_TOOL_NAMES,
    RecordedExecution,
    ToolOutcome,
    as_recorded,
)
from korvid.tools.registry import tool_def

#: Runs per scenario per configuration (issue #69: report variance, not means).
DEFAULT_REPETITIONS = 3


class _RecordingExecutor(RecordedExecution):
    """Wraps the real executor to keep full tool results for evidence grading
    (the engine's ToolCallFinished summary is truncated to 120 chars).

    The tool harness's own sanitize-and-bound step is applied *before*
    recording, with the same registry result format and the same result
    budget the harness will use: grading must only credit evidence the
    model could actually have seen, so the recorded content matches what
    reaches the conversation (and carries the same redactions). That step
    is idempotent, so the harness re-applying it changes nothing; the
    engine's discard notice (excess parallel calls) is appended after its
    own bounding step, so it never re-truncates the recorded content.

    It sits between the real executor and the tool harness, so it is on
    the path that carries producer redaction records — and the path a
    blocked result has to travel to stop the turn. Both pass through: an
    eval run's boundary behaviour is the behaviour under test, down to the
    inventory the payload inspector would export.

    It does *not* hide tools. A tool dropped from a controlled arm (#221)
    is dropped from the resolved policy itself, so the production tool
    harness refuses the call and this executor is never reached.
    """

    def __init__(
        self,
        executor: object,
        max_result_chars: int | None = None,
    ) -> None:
        # Scenario and journey packs hand over whatever they built; this is
        # the composition point that turns it into the contract the tool
        # harness requires, so the harness itself never has to guess.
        self._executor = as_recorded(executor)
        self._max_result_chars = max_result_chars
        self.records: list[ToolRecord] = []

    def record_action(self, name: str, arguments: dict[str, Any], result: str) -> None:
        """File a screen action in the same ordered stream as the reads.

        The eval bridge reports here so a journey that grades "and it put
        that on screen" sees the action where the model made it, without
        the agent minting evidence for a screen move.

        The record is *marked* as a screen action, because the stream it
        joins is also what evidence is graded against: an action's message
        names the resource it moved to, so an unmarked one could satisfy a
        read that never happened (`grader._satisfies`).
        """
        self.records.append(
            ToolRecord(
                name=name,
                arguments=dict(arguments),
                result=result,
                screen_action=True,
            )
        )

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        return (await self.execute_recorded(name, arguments)).text

    async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        outcome = await self._executor.execute_recorded(name, arguments)
        # This pass's own redactions are kept, not dropped. It runs before
        # the harness's and is idempotent, so a redaction made here is one
        # the harness's re-run can no longer find — discarding the records
        # left an eval run's inventory thinner than production's for the
        # same content. Merged the way the harness merges them: the
        # producer's trail re-rooted onto this result, so a mask both
        # passes saw is reported once (PR #197 review).
        definition = tool_def(name)
        result, redactions = sanitize_recorded_tool_result(
            name,
            outcome.text,
            outcome.redactions,
            max_chars=self._max_result_chars,
            error=outcome.error,
            result_format=None if definition is None else definition.result_format,
        )
        self.records.append(ToolRecord(name=name, arguments=dict(arguments), result=result))
        return ToolOutcome(
            text=result,
            redactions=redactions,
            error=outcome.error,
            incarnation=outcome.incarnation,
            container=outcome.container,
        )


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
    #: How well the answer's claims cited the reads behind them (#192).
    citations: CitationReport
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
    #: `success`, `failure`, or `error` — one word per run, so a
    #: scoreboard row does not have to re-derive the verdict from five
    #: counters that each mean something slightly different.
    outcome: str = "success"
    #: Why a run was not a success: `provider_error`, `safety_violation`,
    #: `missing_evidence`, or `misdiagnosis`. `None` for a success.
    failure_class: str | None = None


@dataclass(frozen=True)
class ScenarioReport:
    """All repetitions of one scenario under one configuration."""

    scenario_id: str
    root_cause: str
    runs: list[RunMetrics]
    #: The workspace every repetition started from. Published with the
    #: results: a diagnostic score is not reproducible without the screen
    #: the question was asked from.
    interaction: InteractionContext | None = None

    @property
    def successes(self) -> int:
        """Repetitions whose **diagnosis** was graded correct.

        The historical scoreboard number, kept under its published name.
        It is narrower than `run.outcome`: a repetition can diagnose
        correctly and still be published as a failure for missing its
        evidence, erroring, or violating the write boundary. The journey
        artifact counts whole conversations and names that count
        `successful_journeys` rather than reusing this one.
        """
        return sum(1 for run in self.runs if run.grade.diagnosis_success)

    @property
    def evidence_hits(self) -> int:
        return sum(1 for run in self.runs if run.grade.evidence_fetched)

    @property
    def max_tool_calls(self) -> int:
        """The most tool calls any repetition of this scenario made."""
        return max((run.tool_calls for run in self.runs), default=0)


_JSON_TYPES: dict[str, type | tuple[type, ...]] = {"string": str, "integer": int}

#: One armed surface, indexed for schema validation.
_ArmedSchemas = dict[str, dict[str, Any]]


def _armed_schemas(policy: ResolvedAgentPolicy) -> _ArmedSchemas:
    """The schemas this run actually offered, keyed by tool name.

    Derived from the resolved policy rather than a second static list, so
    a controlled arm that unarms a tool (#221) and a tier that never
    offered it are the same fact to every metric below.
    """
    armed: _ArmedSchemas = {}
    for tool in policy.tools:
        function = tool.get("function")
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str):
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, Mapping):
            parameters = {}
        properties = parameters.get("properties")
        if not isinstance(properties, Mapping):
            properties = {}
        armed[name] = {
            "required": frozenset(parameters.get("required", ())),
            "types": {
                str(prop): spec.get("type", "string")
                for prop, spec in properties.items()
                if isinstance(spec, Mapping)
            },
        }
    return armed


def _read_names(armed: _ArmedSchemas) -> frozenset[str]:
    """Armed names whose results are evidence, not screen moves or writes."""
    return frozenset(
        name
        for name in armed
        if name not in WRITE_TOOL_NAMES
        and (definition := tool_def(name)) is not None
        and definition.effect in ("cluster_read", "external_read")
    )


def _value_matches_schema(value: Any, json_type: str) -> bool:
    expected = _JSON_TYPES.get(json_type)
    if expected is None:
        return True
    # bool is an int subtype in Python but not an integer in JSON schema.
    if isinstance(value, bool):
        return json_type == "boolean"
    return isinstance(value, expected)


def _is_malformed(name: str, raw_arguments: str, armed: _ArmedSchemas) -> bool:
    """Schema-level validation of one tool call, from the raw call the model
    emitted: undecodable or non-mapping arguments, a tool name that was never
    armed (write tools are exempt — they are tracked separately as write
    attempts), a missing required parameter, or a declared parameter of the
    wrong type.

    A name this run did not arm counts as never offered, which is the signal
    a reduced arm exists to capture: whether the model still reaches for a
    tool it was not given (#221)."""
    if name in WRITE_TOOL_NAMES:
        return False
    try:
        parsed = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError:
        return True
    if not isinstance(parsed, dict):
        return True
    schema = armed.get(name)
    if schema is None:
        return True
    if not schema["required"] <= parsed.keys():
        return True
    property_types = schema["types"]
    return any(
        key in property_types and not _value_matches_schema(value, property_types[key])
        for key, value in parsed.items()
    )


@dataclass(frozen=True)
class EvalRunConfig:
    """Everything a run needs beyond the scenario and its two factories.

    One value rather than six parameters threaded through three
    functions: the CLI resolves it once, and every repetition of every
    scenario is composed against exactly the same policy and prompt.
    """

    policy: ResolvedAgentPolicy | None = None
    model_tier: str | None = None
    omit_tools: frozenset[str] = frozenset()
    grind: PromptGrind = NO_GRIND
    cluster: ClusterFacts = EVAL_CLUSTER
    user_rules: tuple[str, ...] = ()


async def _run_once(
    scenario: Scenario,
    provider_factory: Callable[[], Any],
    executor_factory: Callable[[], Any],
    config: EvalRunConfig,
) -> RunMetrics:
    raw_provider = provider_factory()
    try:
        return await _drive_turn(scenario, raw_provider, executor_factory(), config)
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

    armed: _ArmedSchemas = field(default_factory=dict)
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
            if _is_malformed(event.name, event.arguments, self.armed):
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


def _classify(result: GradeResult, tally: _TurnTally) -> tuple[str, str | None]:
    """One word for what happened, plus why it was not a success.

    The precedence itself lives in `korvid.evals.outcome`, shared with the
    journey runner: a journey turn and a scenario repetition are published
    side by side, so the two artifacts must rank failures identically.
    """
    return classify_outcome(
        grade=result,
        safety_violations=tally.safety_violations,
        error=tally.error,
    )


def _build_harness(
    bridge: EvalUiBridge,
    provider: Any,
    executor: Any,
    config: EvalRunConfig,
) -> EvalHarness:
    """Compose the production session this repetition runs on."""
    return build_eval_harness(
        provider=provider,
        execution=executor,
        bridge=bridge,
        policy=config.policy,
        model_tier=config.model_tier,
        omit_tools=config.omit_tools,
        cluster=config.cluster,
        user_rules=config.user_rules,
        grind=config.grind,
    )


async def _drive_turn(
    scenario: Scenario,
    raw_provider: Any,
    raw_executor: Any,
    config: EvalRunConfig,
) -> RunMetrics:
    provider = _CountingProvider(raw_provider)
    policy = config.policy or resolve_eval_policy(
        provider, model_tier=config.model_tier, omit_tools=config.omit_tools
    )
    executor = _RecordingExecutor(raw_executor, max_result_chars=policy.max_result_chars)
    bridge = EvalUiBridge(scenario.interaction)
    bridge.bind_objects(scenario.objects)
    bridge.record_into(executor.record_action)
    harness = _build_harness(bridge, provider, executor, replace(config, policy=policy))
    armed = _armed_schemas(harness.policy)
    tally = _TurnTally(armed=armed)
    started = time.monotonic()
    try:
        async for event in harness.session.run_turn(scenario.question):
            tally.note(event)
    finally:
        await harness.session.aclose()
    wall_time = time.monotonic() - started
    grade_result = grade(scenario, tally.answer, executor.records)
    if tally.error is not None:
        # A provider may stream a plausible answer and then fail; an
        # errored turn never counts as a diagnostic success.
        grade_result = replace(grade_result, diagnosis_success=False)
    in_tokens, out_tokens = harness.session.total_tokens
    reads = _read_names(armed)
    # A call is resolvable when it is an armed *read* (a write call is a
    # policy failure regardless of outcome), is schema-valid, AND its
    # arguments resolved in the cluster (the executor returns 'ERROR: ...'
    # otherwise). Diagnostic relevance is graded via expected_evidence.
    resolvable_calls = sum(
        1
        for record in executor.records
        if record.name in reads
        and not record.result.startswith("ERROR:")
        and not _is_malformed(record.name, json.dumps(record.arguments), armed)
    )
    # Issue #69's correct-tool + correct-argument rate: a call is on-target
    # when it is an armed *read* whose arguments name one of the scenario's
    # evidence targets, regardless of what its result contained. Writes
    # never count — matches_target ignores the tool name, and a mutation
    # aimed at the right object is a policy failure, not evidence.
    on_target_calls = sum(
        1
        for record in executor.records
        if record.name in reads
        and any(
            matches_target(alt, record) for group in scenario.expected_evidence for alt in group
        )
    )
    outcome, failure_class = _classify(grade_result, tally)
    return RunMetrics(
        grade=grade_result,
        # Measured against what the session actually minted, so an
        # invented reference scores worse than citing nothing (#192).
        citations=citation_report(tally.answer, minted=harness.session.evidence.references()),
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
        tokens_estimated=harness.session.usage_estimated,
        wall_time_s=wall_time,
        error=tally.error,
        outcome=outcome,
        failure_class=failure_class,
    )


async def run_scenario(
    scenario: Scenario,
    *,
    provider_factory: Callable[[], Any],
    executor_factory: Callable[[], Any],
    repetitions: int = DEFAULT_REPETITIONS,
    policy: ResolvedAgentPolicy | None = None,
    model_tier: str | None = None,
    omit_tools: frozenset[str] = frozenset(),
    grind: PromptGrind = NO_GRIND,
    cluster: ClusterFacts = EVAL_CLUSTER,
    user_rules: tuple[str, ...] = (),
) -> ScenarioReport:
    """Run one scenario ``repetitions`` times with fresh state per run.

    Args:
        scenario: The fixture, including the workspace it starts from.
        provider_factory: Builds one provider per repetition.
        executor_factory: Builds one tool executor per repetition.
        repetitions: How many times to run it (variance, not means).
        policy: An already-resolved policy; when `None` one is resolved
            per repetition from `model_tier` and `omit_tools`.
        model_tier: `"low"`, `"high"`, or `None` for automatic routing.
        omit_tools: Names to drop from the armed surface (#221).
        grind: The eval-only prompt levers.
        cluster: Cluster facts composed into every turn.
        user_rules: Operator rules composed into every turn.
    """
    config = EvalRunConfig(
        policy=policy,
        model_tier=model_tier,
        omit_tools=omit_tools,
        grind=grind,
        cluster=cluster,
        user_rules=user_rules,
    )
    runs = [
        await _run_once(scenario, provider_factory, executor_factory, config)
        for _ in range(repetitions)
    ]
    return ScenarioReport(
        scenario_id=scenario.id,
        root_cause=scenario.root_cause,
        runs=runs,
        interaction=scenario.interaction,
    )


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


def _citation_cell(runs: Sequence[RunMetrics]) -> str:
    """Citation precision and coverage for one scenario's runs (#192).

    Precision is over runs that cited anything: averaging an undefined
    precision as zero would punish an uncited answer twice, once in each
    column, and the two failures are different.
    """
    scored = [run.citations.precision for run in runs if run.citations.precision is not None]
    coverage = sum(run.citations.coverage for run in runs) / len(runs) if runs else 0.0
    if not scored:
        return f"— / {100 * coverage:.1f}%"
    precision = sum(scored) / len(scored)
    return f"{100 * precision:.1f}% / {100 * coverage:.1f}%"


def render_markdown(reports: list[ScenarioReport]) -> str:
    """Markdown summary table: one row per scenario, variance included.

    The verdict column is headed `correct diagnosis`, not `success`:
    `ScenarioReport.successes` counts repetitions whose diagnosis was
    graded correct, which is narrower than a passing run and is a
    different measurement from the journey table's whole conversations.
    """
    lines = [
        "| scenario | root cause | correct diagnosis | evidence | resolvable calls | on-target | "
        "malformed | writes | safety | cite precision/coverage | iterations | "
        "tokens in/out | wall s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
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
        citations = _citation_cell(runs)
        lines.append(
            f"| {report.scenario_id} | {report.root_cause} | {report.successes}/{n} |"
            f" {report.evidence_hits}/{n} | {resolvable}/{total_calls}{resolvable_rate} |"
            f" {on_target}/{total_calls}{on_target_rate} |"
            f" {malformed}/{total_calls}{rate} | {writes} | {safety} | {citations} |"
            f" {iterations} | {token_mark}{tokens_in}/{tokens_out} | {wall} |"
        )
    return "\n".join(lines)
