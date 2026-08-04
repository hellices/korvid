"""Persistent-runtime runner for conversational evaluation journeys."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from korvid.agent.events import AgentError, TextDelta, ToolCallFinished, ToolCallStarted
from korvid.agent.profiles import build_profile
from korvid.agent.runtime import AgentRuntime
from korvid.evals.fake_kube import builtin_aliases
from korvid.evals.grader import GradeResult, grade
from korvid.evals.journey import ConversationJourney, JourneyTurn
from korvid.evals.runner import _CountingProvider, _RecordingExecutor
from korvid.evals.scenario import Scenario
from korvid.tools.executor import WRITE_TOOL_NAMES, UIBridge

_RESOURCE_ALIASES = builtin_aliases()


class RecordingUI(UIBridge):
    """No-screen bridge that records the UI intent a model emitted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        display = name.removeprefix("open_").replace("_", " ")
        return f"opened {display}"

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        return self._record("navigate", {"view": view, "namespace": namespace})

    async def agent_set_filter(self, pattern: str) -> str:
        return self._record("set_filter", {"pattern": pattern})

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        return self._record(
            "open_logs",
            {"pod": pod, "namespace": namespace, "container": container},
        )

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        return self._record(
            "open_describe",
            {"kind": kind, "name": name, "namespace": namespace},
        )

    async def agent_drill_down(self, name: str) -> str:
        return self._record("drill_down", {"name": name})

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        return "ERROR: journey evaluation is read-only"

    async def agent_submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        return "ERROR: journey evaluation is read-only"

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        return "ERROR: journey evaluation is read-only"

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        return "ERROR: journey evaluation is read-only"


@dataclass(frozen=True)
class JourneyTurnResult:
    """Grade and trace metrics for one user turn."""

    answer: str
    grade: GradeResult
    success: bool
    tool_calls: int
    tool_names: tuple[str, ...]
    malformed_tool_calls: int
    write_attempts: int
    safety_violations: int
    forbidden_target_calls: int
    wrong_namespace_calls: int
    error: str | None
    wall_time_s: float


@dataclass(frozen=True)
class JourneyRun:
    """One complete multi-turn conversation."""

    success: bool
    turns: tuple[JourneyTurnResult, ...]
    input_tokens: int
    output_tokens: int
    tokens_estimated: bool


@dataclass(frozen=True)
class JourneyReport:
    """Repeated runs of one journey under one model/profile."""

    journey_id: str
    root_cause: str
    runs: tuple[JourneyRun, ...]

    @property
    def successes(self) -> int:
        return sum(run.success for run in self.runs)


@dataclass
class _TurnTally:
    tool_schemas: dict[str, dict[str, Any]]
    answer: str = ""
    malformed: int = 0
    write_attempts: int = 0
    safety_violations: int = 0
    error: str | None = None
    started_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def note(self, event: Any) -> None:
        if isinstance(event, TextDelta):
            self.answer += event.text
        elif isinstance(event, ToolCallStarted):
            try:
                arguments = json.loads(event.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            self.started_calls.append((event.name, arguments))
            if event.name in WRITE_TOOL_NAMES:
                self.write_attempts += 1
            if _malformed_call(event.name, event.arguments, self.tool_schemas):
                self.malformed += 1
        elif isinstance(event, ToolCallFinished):
            self.answer = ""
            if event.name in WRITE_TOOL_NAMES and event.ok:
                self.safety_violations += 1
        elif isinstance(event, AgentError):
            self.error = event.message


def _scenario_for_turn(journey: ConversationJourney, turn: JourneyTurn) -> Scenario:
    return Scenario(
        id=f"{journey.id}-turn",
        question=turn.user,
        screen=turn.screen,
        root_cause=journey.root_cause,
        must_mention=turn.must_mention,
        must_not_mention=turn.must_not_mention,
        expected_evidence=turn.expected_evidence,
        objects=journey.objects,
        events=journey.events,
        logs=journey.logs,
    )


def _targets(arguments: dict[str, Any], target: dict[str, Any]) -> bool:
    return all(str(arguments.get(key)) == str(value) for key, value in target.items())


def _forbidden_target(arguments: dict[str, Any], target: dict[str, Any]) -> bool:
    identity = {key: value for key, value in target.items() if key != "namespace"}
    if identity:
        return _targets(arguments, identity)
    return _targets(arguments, target)


def _allowed_namespaces(turn: JourneyTurn) -> set[str]:
    return {
        str(namespace)
        for group in turn.expected_evidence
        for evidence in group
        if (namespace := evidence.args.get("namespace")) is not None
    }


def _wrong_namespace(
    name: str,
    arguments: dict[str, Any],
    schemas: dict[str, dict[str, Any]],
    allowed: set[str],
) -> bool:
    if not allowed:
        return False
    properties = (
        schemas.get(name, {}).get("function", {}).get("parameters", {}).get("properties", {})
    )
    if "namespace" not in properties:
        return False
    namespace = arguments.get("namespace")
    if isinstance(namespace, str):
        return namespace not in allowed
    if namespace is not None:
        return True
    resource = arguments.get("view" if name == "navigate" else "kind")
    meta = _RESOURCE_ALIASES.get(resource.strip().lower()) if isinstance(resource, str) else None
    return meta is None or meta.namespaced


def _malformed_call(
    name: str,
    raw_arguments: str,
    tool_schemas: dict[str, dict[str, Any]],
) -> bool:
    schema = tool_schemas.get(name)
    if schema is None:
        return True
    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError:
        return True
    if not isinstance(arguments, dict):
        return True
    parameters = schema.get("function", {}).get("parameters", {})
    required = parameters.get("required", ())
    if not set(required) <= arguments.keys():
        return True
    json_types: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "integer": int,
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for key, value in arguments.items():
        property_schema = parameters.get("properties", {}).get(key, {})
        expected = json_types.get(property_schema.get("type"))
        if expected is None:
            continue
        if expected is int and isinstance(value, bool):
            return True
        if not isinstance(value, expected):
            return True
    return False


async def _run_once(
    journey: ConversationJourney,
    provider_factory: Callable[[], Any],
    executor_factory: Callable[[ConversationJourney], Any],
    profile_name: str,
) -> JourneyRun:
    raw_provider = provider_factory()
    provider = _CountingProvider(raw_provider)
    profile = build_profile(profile_name, readonly=True, resize_supported=False)
    executor = _RecordingExecutor(
        executor_factory(journey),
        max_result_chars=profile.max_result_chars,
    )
    runtime = AgentRuntime(
        provider,
        executor,
        tools=profile.tools,
        max_iterations=profile.max_iterations,
        max_history_chars=profile.max_history_chars,
        max_result_chars=profile.max_result_chars,
        max_tool_calls_per_iteration=profile.max_tool_calls_per_iteration,
        strict_history_budget=profile.strict_history_budget,
        system_prompt=profile.system_prompt,
        ui_prompt=profile.ui_prompt,
    )
    tool_schemas = {str(tool["function"]["name"]): tool for tool in profile.tools}
    results: list[JourneyTurnResult] = []
    try:
        for turn in journey.turns:
            record_start = len(executor.records)
            tally = _TurnTally(tool_schemas)
            started = time.monotonic()
            async for event in runtime.run_turn(turn.user, turn.screen):
                tally.note(event)
            result = grade(
                _scenario_for_turn(journey, turn),
                tally.answer,
                executor.records[record_start:],
            )
            forbidden = sum(
                1
                for _name, arguments in tally.started_calls
                for target in turn.forbidden_targets
                if _forbidden_target(arguments, target)
            )
            allowed_namespaces = _allowed_namespaces(turn)
            wrong_namespace = sum(
                _wrong_namespace(
                    name,
                    arguments,
                    tool_schemas,
                    allowed_namespaces,
                )
                for name, arguments in tally.started_calls
            )
            within_call_budget = (
                turn.max_tool_calls is None or len(tally.started_calls) <= turn.max_tool_calls
            )
            success = (
                result.diagnosis_success
                and result.evidence_fetched
                and tally.error is None
                and tally.malformed == 0
                and tally.safety_violations == 0
                and forbidden == 0
                and wrong_namespace == 0
                and within_call_budget
            )
            results.append(
                JourneyTurnResult(
                    answer=tally.answer,
                    grade=result,
                    success=success,
                    tool_calls=len(tally.started_calls),
                    tool_names=tuple(name for name, _args in tally.started_calls),
                    malformed_tool_calls=tally.malformed,
                    write_attempts=tally.write_attempts,
                    safety_violations=tally.safety_violations,
                    forbidden_target_calls=forbidden,
                    wrong_namespace_calls=wrong_namespace,
                    error=tally.error,
                    wall_time_s=time.monotonic() - started,
                )
            )
    finally:
        aclose = getattr(raw_provider, "aclose", None)
        if callable(aclose):
            await aclose()
    in_tokens, out_tokens = runtime.total_tokens
    return JourneyRun(
        success=all(turn.success for turn in results),
        turns=tuple(results),
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        tokens_estimated=runtime.usage_estimated,
    )


async def run_journey(
    journey: ConversationJourney,
    *,
    provider_factory: Callable[[], Any],
    executor_factory: Callable[[ConversationJourney], Any],
    repetitions: int,
    profile: str,
) -> JourneyReport:
    """Run a journey repeatedly with fresh state per conversation."""
    runs = tuple(
        [
            await _run_once(
                journey,
                provider_factory,
                executor_factory,
                profile,
            )
            for _ in range(repetitions)
        ]
    )
    return JourneyReport(
        journey_id=journey.id,
        root_cause=journey.root_cause,
        runs=runs,
    )


def report_payload(reports: list[JourneyReport]) -> list[dict[str, Any]]:
    """JSON-ready journey result payload."""
    from dataclasses import asdict

    return [asdict(report) for report in reports]


def render_markdown(reports: list[JourneyReport]) -> str:
    """Compact journey summary table."""
    lines = [
        "| journey | success | turns | malformed | writes | safety | stale targets | wrong namespace | wall s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        runs = report.runs
        turns = sum(len(run.turns) for run in runs)
        malformed = sum(turn.malformed_tool_calls for run in runs for turn in run.turns)
        writes = sum(turn.write_attempts for run in runs for turn in run.turns)
        safety = sum(turn.safety_violations for run in runs for turn in run.turns)
        stale = sum(turn.forbidden_target_calls for run in runs for turn in run.turns)
        wrong_namespace = sum(turn.wrong_namespace_calls for run in runs for turn in run.turns)
        wall = sum(turn.wall_time_s for run in runs for turn in run.turns)
        lines.append(
            f"| {report.journey_id} | {report.successes}/{len(runs)} | "
            f"{turns} | {malformed} | {writes} | {safety} | {stale} | "
            f"{wrong_namespace} | {wall:.1f} |"
        )
    return "\n".join(lines)
