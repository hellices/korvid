"""Persistent-session runner for conversational evaluation journeys.

One `DefaultAgentSession` serves every turn of a journey, exactly as one
TUI session serves every message an operator types — that persistence is
what the journeys measure (does the model still hold the correction from
two turns ago?).

The workspace persists with it. The journey's authored `interaction` is
the screen the conversation opens on; a turn that declares its own
`interaction` is the fixture saying the *operator* moved the screen before
typing, and a turn that does not keeps whatever the previous turn left —
including wherever the model itself navigated with a screen action.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from korvid.agent.events import AgentError, TextDelta, ToolCallFinished, ToolCallStarted
from korvid.agent.interaction import ClusterFacts, InteractionContext
from korvid.agent.model_policy import ResolvedAgentPolicy
from korvid.evals.fake_kube import builtin_aliases
from korvid.evals.grader import GradeResult, ToolRecord, grade
from korvid.evals.harness import (
    EVAL_CLUSTER,
    NO_GRIND,
    PromptGrind,
    build_eval_harness,
    resolve_eval_policy,
)
from korvid.evals.interaction import EvalUiBridge
from korvid.evals.journey import ConversationJourney, JourneyTurn
from korvid.evals.runner import _CountingProvider, _RecordingExecutor
from korvid.evals.scenario import Scenario
from korvid.tools.executor import WRITE_TOOL_NAMES

_RESOURCE_ALIASES = builtin_aliases()
LIVE_BOUNDARY_ERROR = "live journey boundary violation:"


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
        interaction=turn.interaction or journey.interaction,
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


def _wrong_namespace_count(
    started_calls: list[tuple[str, dict[str, Any]]],
    records: list[ToolRecord],
    schemas: dict[str, dict[str, Any]],
    allowed: set[str],
) -> int:
    violations = [
        _wrong_namespace(name, arguments, schemas, allowed) for name, arguments in started_calls
    ]
    matched: set[int] = set()
    for record in records:
        if not record.result.startswith(f"ERROR: {LIVE_BOUNDARY_ERROR}"):
            continue
        for index, (name, arguments) in enumerate(started_calls):
            if index in matched or name != record.name or arguments != record.arguments:
                continue
            violations[index] = True
            matched.add(index)
            break
    return sum(violations)


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


#: How an eval builds the workspace a journey opens on. Injectable so a
#: test can keep the bridge the model drove; the default is the ordinary
#: mutable eval workspace.
BridgeFactory = Callable[[InteractionContext], EvalUiBridge]


@dataclass(frozen=True)
class JourneyRunConfig:
    """Everything a conversation needs beyond its journey and factories."""

    policy: ResolvedAgentPolicy | None = None
    model_tier: str | None = None
    grind: PromptGrind = NO_GRIND
    cluster: ClusterFacts = EVAL_CLUSTER
    user_rules: tuple[str, ...] = ()
    bridge_factory: BridgeFactory = EvalUiBridge


async def _run_once(
    journey: ConversationJourney,
    provider_factory: Callable[[], Any],
    executor_factory: Callable[[ConversationJourney], Any],
    config: JourneyRunConfig,
) -> JourneyRun:
    raw_provider = provider_factory()
    provider = _CountingProvider(raw_provider)
    policy = config.policy or resolve_eval_policy(provider, model_tier=config.model_tier)
    executor = _RecordingExecutor(
        executor_factory(journey),
        max_result_chars=policy.max_result_chars,
    )
    bridge = config.bridge_factory(journey.interaction)
    bridge.record_into(executor.record_action)
    harness = build_eval_harness(
        provider=provider,
        execution=executor,
        bridge=bridge,
        policy=policy,
        cluster=config.cluster,
        user_rules=config.user_rules,
        grind=config.grind,
    )
    tool_schemas = {str(tool["function"]["name"]): dict(tool) for tool in harness.policy.tools}
    results: list[JourneyTurnResult] = []
    try:
        for turn in journey.turns:
            if turn.interaction is not None:
                # The fixture says the operator moved the screen between
                # turns; the model's own navigation is kept otherwise.
                bridge.reset(turn.interaction)
            record_start = len(executor.records)
            tally = _TurnTally(tool_schemas)
            started = time.monotonic()
            async for event in harness.session.run_turn(turn.user):
                tally.note(event)
            turn_records = executor.records[record_start:]
            result = grade(
                _scenario_for_turn(journey, turn),
                tally.answer,
                turn_records,
            )
            forbidden = sum(
                1
                for _name, arguments in tally.started_calls
                for target in turn.forbidden_targets
                if _forbidden_target(arguments, target)
            )
            allowed_namespaces = _allowed_namespaces(turn)
            wrong_namespace = _wrong_namespace_count(
                tally.started_calls,
                turn_records,
                tool_schemas,
                allowed_namespaces,
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
        await harness.session.aclose()
        aclose = getattr(raw_provider, "aclose", None)
        if callable(aclose):
            await aclose()
    in_tokens, out_tokens = harness.session.total_tokens
    return JourneyRun(
        success=all(turn.success for turn in results),
        turns=tuple(results),
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        tokens_estimated=harness.session.usage_estimated,
    )


async def run_journey(
    journey: ConversationJourney,
    *,
    provider_factory: Callable[[], Any],
    executor_factory: Callable[[ConversationJourney], Any],
    repetitions: int,
    policy: ResolvedAgentPolicy | None = None,
    model_tier: str | None = None,
    grind: PromptGrind = NO_GRIND,
    cluster: ClusterFacts = EVAL_CLUSTER,
    user_rules: tuple[str, ...] = (),
    bridge_factory: BridgeFactory = EvalUiBridge,
) -> JourneyReport:
    """Run a journey repeatedly with fresh state per conversation.

    Args:
        journey: The fixture, including the workspace it opens on.
        provider_factory: Builds one provider per conversation.
        executor_factory: Builds one tool executor per conversation.
        repetitions: How many conversations to run.
        policy: An already-resolved policy; when `None` one is resolved
            per conversation from `model_tier`.
        model_tier: `"low"`, `"high"`, or `None` for automatic routing.
        grind: The eval-only prompt levers.
        cluster: Cluster facts composed into every turn.
        user_rules: Operator rules composed into every turn.
        bridge_factory: Builds the workspace bridge from the journey's
            authored starting interaction.
    """
    config = JourneyRunConfig(
        policy=policy,
        model_tier=model_tier,
        grind=grind,
        cluster=cluster,
        user_rules=user_rules,
        bridge_factory=bridge_factory,
    )
    runs = tuple(
        [
            await _run_once(journey, provider_factory, executor_factory, config)
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
