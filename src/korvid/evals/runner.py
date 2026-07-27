"""Eval runner: drives the real AgentRuntime over scenario fixtures (issue #69).

The unit under test is the **model + runtime**: the provider is live (or
scripted in CI smoke tests), the cluster is simulated, and every run is
scored by the deterministic grader plus behavioral metrics captured from
the typed AgentEvent stream.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from korvid.agent.events import AgentError, TextDelta, ToolCallFinished, TurnComplete
from korvid.agent.runtime import AgentRuntime
from korvid.agent.tools import READ_TOOLS, WRITE_TOOL_NAMES
from korvid.evals.grader import GradeResult, ToolRecord, grade
from korvid.evals.scenario import Scenario

#: Runs per scenario per configuration (issue #69: report variance, not means).
DEFAULT_REPETITIONS = 3


class _RecordingExecutor:
    """Wraps the real executor to keep full tool results for evidence grading
    (the runtime's ToolCallFinished summary is truncated to 120 chars)."""

    def __init__(self, executor: Any) -> None:
        self._executor = executor
        self.records: list[ToolRecord] = []

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        result: str = await self._executor.execute(name, arguments)
        self.records.append(ToolRecord(name=name, arguments=str(arguments), result=result))
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


@dataclass(frozen=True)
class RunMetrics:
    """One repetition's grade plus behavioral metrics from the event stream."""

    grade: GradeResult
    #: Text streamed after the last tool call — the final answer segment.
    answer: str
    iterations: int
    tool_calls: int
    #: Bad-JSON arguments or unknown tool names.
    malformed_tool_calls: int
    #: Write-tool calls attempted (they must all fail in an eval session).
    write_attempts: int
    #: Write-tool calls that *succeeded* without an approval path — must be 0.
    safety_violations: int
    input_tokens: int
    output_tokens: int
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


def _is_malformed(summary: str) -> bool:
    """Bad-JSON arguments (rejected by the runtime before dispatch) or an
    unknown tool name (rejected by the executor)."""
    return summary.startswith(("ERROR: bad arguments", "ERROR: unknown tool"))


async def _run_once(
    scenario: Scenario,
    provider_factory: Callable[[], Any],
    executor_factory: Callable[[], Any],
) -> RunMetrics:
    provider = _CountingProvider(provider_factory())
    executor = _RecordingExecutor(executor_factory())
    runtime = AgentRuntime(provider, executor, tools=READ_TOOLS)
    answer = ""
    tool_calls = 0
    malformed = 0
    write_attempts = 0
    safety_violations = 0
    in_tokens = 0
    out_tokens = 0
    error: str | None = None
    started = time.monotonic()
    async for event in runtime.run_turn(scenario.question, scenario.screen):
        if isinstance(event, TextDelta):
            answer += event.text
        elif isinstance(event, ToolCallFinished):
            tool_calls += 1
            # Narration before a tool call is hypothesis, not diagnosis:
            # only text after the final tool call is graded as the answer.
            answer = ""
            if _is_malformed(event.summary):
                malformed += 1
            if event.name in WRITE_TOOL_NAMES:
                write_attempts += 1
                if event.ok:
                    safety_violations += 1
        elif isinstance(event, TurnComplete):
            in_tokens = event.input_tokens
            out_tokens = event.output_tokens
        elif isinstance(event, AgentError):
            error = event.message
    wall_time = time.monotonic() - started
    return RunMetrics(
        grade=grade(scenario, answer, executor.records),
        answer=answer,
        iterations=provider.completions,
        tool_calls=tool_calls,
        malformed_tool_calls=malformed,
        write_attempts=write_attempts,
        safety_violations=safety_violations,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        wall_time_s=wall_time,
        error=error,
    )


async def run_scenario(
    scenario: Scenario,
    *,
    provider_factory: Callable[[], Any],
    executor_factory: Callable[[], Any],
    repetitions: int = DEFAULT_REPETITIONS,
) -> ScenarioReport:
    """Run one scenario ``repetitions`` times with fresh state per run."""
    runs = [
        await _run_once(scenario, provider_factory, executor_factory) for _ in range(repetitions)
    ]
    return ScenarioReport(scenario_id=scenario.id, root_cause=scenario.root_cause, runs=runs)


def _fmt_seconds(values: list[float]) -> str:
    if len(values) > 1:
        return f"{statistics.mean(values):.1f}±{statistics.stdev(values):.1f}"
    return f"{values[0]:.1f}" if values else "-"


def render_markdown(reports: list[ScenarioReport]) -> str:
    """Markdown summary table: one row per scenario, variance included."""
    lines = [
        "| scenario | root cause | success | evidence | malformed | safety | "
        "iterations | tokens in/out | wall s |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for report in reports:
        runs = report.runs
        n = len(runs)
        malformed = sum(run.malformed_tool_calls for run in runs)
        safety = sum(run.safety_violations for run in runs)
        iterations = f"{statistics.mean([run.iterations for run in runs]):.1f}" if runs else "-"
        tokens_in = sum(run.input_tokens for run in runs) // max(1, n)
        tokens_out = sum(run.output_tokens for run in runs) // max(1, n)
        wall = _fmt_seconds([run.wall_time_s for run in runs])
        lines.append(
            f"| {report.scenario_id} | {report.root_cause} | {report.successes}/{n} |"
            f" {report.evidence_hits}/{n} | {malformed} | {safety} | {iterations} |"
            f" {tokens_in}/{tokens_out} | {wall} |"
        )
    return "\n".join(lines)
