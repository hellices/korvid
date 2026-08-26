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

Because of that, a journey row publishes both ends of every turn: the
screen it was asked from and the screen it left behind. Together with the
conversation's own starting `interaction` and one `outcome`/`failure_class`
per turn — ranked by the precedence `korvid.evals.outcome` shares with the
scenario runner — that is what makes a published journey row reproducible.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
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
from korvid.evals.interaction import EvalUiBridge, interaction_payload
from korvid.evals.journey import ConversationJourney, JourneyTurn
from korvid.evals.outcome import SUCCESS, classify_outcome
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
    tool_calls: int
    tool_names: tuple[str, ...]
    malformed_tool_calls: int
    write_attempts: int
    safety_violations: int
    forbidden_target_calls: int
    wrong_namespace_calls: int
    error: str | None
    wall_time_s: float
    #: The workspace this turn ran against — the authored screen when the
    #: fixture moved it, otherwise wherever the previous turn (or the
    #: model itself) left it. Published: a turn's score is not
    #: reproducible without the screen it was asked from.
    interaction: InteractionContext | None = None
    #: The workspace the turn ended on, which is where the *next* turn
    #: starts. It is how a published row shows that the model navigated.
    final_interaction: InteractionContext | None = None
    #: `success`, `failure` or `error` — one word per turn, ranked by the
    #: same precedence the scenario runner publishes. The turn's single
    #: verdict: everything else about how it went is derived from it.
    outcome: str = "success"
    #: Why the turn was not a success, or `None`. The four shared classes
    #: plus the journey's own: `malformed_call`, `forbidden_target`,
    #: `wrong_namespace`, `call_budget_exceeded`.
    failure_class: str | None = None

    @property
    def success(self) -> bool:
        """Whether the turn succeeded — derived, never stored.

        A stored flag beside `outcome` is a second copy of one fact, and
        two copies can disagree: a row could claim `success=True` next to
        `outcome="failure"` and `failure_class="misdiagnosis"`, and
        nothing would catch it. Deriving it means the contradictory row
        cannot be constructed at all.
        """
        return self.outcome == SUCCESS


@dataclass(frozen=True)
class JourneyRun:
    """One complete multi-turn conversation."""

    turns: tuple[JourneyTurnResult, ...]
    input_tokens: int
    output_tokens: int
    tokens_estimated: bool

    @property
    def success(self) -> bool:
        """Whether every turn succeeded — derived, never stored.

        `JourneyTurnResult.success` is already derived from its outcome so
        a row cannot contradict itself; a stored flag here would reopen
        exactly that hole one level up, letting a caller publish a clean
        conversation above a failed turn.

        A run with no turns is not a success: `all(())` is `True`, so the
        naive derivation would publish a conversation that never ran — a
        provider that died before the first turn — as a clean pass.
        """
        return bool(self.turns) and all(turn.success for turn in self.turns)


@dataclass(frozen=True)
class JourneyReport:
    """Repeated runs of one journey under one model/tier."""

    journey_id: str
    root_cause: str
    runs: tuple[JourneyRun, ...]
    #: The screen the conversation opened on, for every repetition. Same
    #: role as `ScenarioReport.interaction`: a published row is not
    #: reproducible without it.
    interaction: InteractionContext | None = None

    @property
    def successful_journeys(self) -> int:
        """Repetitions in which *every* turn's outcome was `success`.

        Named for what it counts. The scenario artifact's `successes`
        counts repetitions whose diagnosis was graded correct; a
        conversation has no single diagnosis, so reusing that key here
        would publish two different measurements under one name.
        """
        return sum(1 for run in self.runs if run.success)


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
    """Schema-level validation of one raw tool call.

    A write tool is exempt from the unknown-name rule, exactly as in the
    scenario runner: no eval arms a write schema, so every write call
    would otherwise be counted twice — once as the write attempt it is,
    and once as a malformed call, inflating the malformed rate the issue
    bounds at 1%. A non-write name this run did not arm stays malformed:
    that is the reduced-arm signal (#221).
    """
    if name in WRITE_TOOL_NAMES:
        return False
    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError:
        return True
    if not isinstance(arguments, dict):
        return True
    schema = tool_schemas.get(name)
    if schema is None:
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


def _turn_result(
    journey: ConversationJourney,
    turn: JourneyTurn,
    tally: _TurnTally,
    records: list[ToolRecord],
    tool_schemas: dict[str, dict[str, Any]],
    screens: tuple[InteractionContext, InteractionContext],
    wall_time_s: float,
) -> JourneyTurnResult:
    """Grade one finished turn and classify what happened in it.

    The verdict comes from the shared precedence (`korvid.evals.outcome`),
    extended with the journey's own failure classes: a turn can diagnose
    perfectly and still break its call budget, its namespace boundary or
    the user's correction. Those rank *after* the four shared classes, so
    a landed write or an errored turn still reads the same in a journey
    row as in a scenario row.
    """
    result = grade(_scenario_for_turn(journey, turn), tally.answer, records)
    forbidden = sum(
        1
        for _name, arguments in tally.started_calls
        for target in turn.forbidden_targets
        if _forbidden_target(arguments, target)
    )
    wrong_namespace = _wrong_namespace_count(
        tally.started_calls,
        records,
        tool_schemas,
        _allowed_namespaces(turn),
    )
    over_budget = turn.max_tool_calls is not None and len(tally.started_calls) > turn.max_tool_calls
    outcome, failure_class = classify_outcome(
        grade=result,
        safety_violations=tally.safety_violations,
        error=tally.error,
        additional=(
            ("malformed_call", tally.malformed > 0),
            ("forbidden_target", forbidden > 0),
            ("wrong_namespace", wrong_namespace > 0),
            ("call_budget_exceeded", over_budget),
        ),
    )
    started_screen, final_screen = screens
    return JourneyTurnResult(
        answer=tally.answer,
        grade=result,
        tool_calls=len(tally.started_calls),
        tool_names=tuple(name for name, _args in tally.started_calls),
        malformed_tool_calls=tally.malformed,
        write_attempts=tally.write_attempts,
        safety_violations=tally.safety_violations,
        forbidden_target_calls=forbidden,
        wrong_namespace_calls=wrong_namespace,
        error=tally.error,
        wall_time_s=wall_time_s,
        interaction=started_screen,
        final_interaction=final_screen,
        outcome=outcome,
        failure_class=failure_class,
    )


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
            # Both ends are recorded: the screen the turn was asked from,
            # and the one it left behind for the next turn.
            opened_on = bridge.snapshot()
            record_start = len(executor.records)
            tally = _TurnTally(tool_schemas)
            started = time.monotonic()
            async for event in harness.session.run_turn(turn.user):
                tally.note(event)
            results.append(
                _turn_result(
                    journey,
                    turn,
                    tally,
                    executor.records[record_start:],
                    tool_schemas,
                    (opened_on, bridge.snapshot()),
                    time.monotonic() - started,
                )
            )
    finally:
        await _close_run_resources(harness.session, raw_provider)
    in_tokens, out_tokens = harness.session.total_tokens
    return JourneyRun(
        turns=tuple(results),
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        tokens_estimated=harness.session.usage_estimated,
    )


async def _close_run_resources(session: Any, provider: Any) -> None:
    """Close session and provider independently, preserving session failure."""
    try:
        await session.aclose()
    finally:
        aclose = getattr(provider, "aclose", None)
        if callable(aclose):
            await aclose()


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
            per conversation from `model_tier`. A campaign passes the one
            policy it routed, so every conversation is composed against
            exactly the same surface, budgets and prompt.
        model_tier: `"low"`, `"high"`, or `None` for automatic routing.
        grind: The eval-only prompt levers.
        cluster: Cluster facts composed into every turn.
        user_rules: Operator rules composed into every turn.
        bridge_factory: Builds the workspace bridge from the journey's
            authored starting interaction.

    Returns:
        The repetitions plus the screen the conversation opened on, which
        the artifact publishes.
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
        interaction=journey.interaction,
    )


#: Published through `interaction_payload`, not `asdict`: the record shape
#: has to match a scenario row's (`filter`, not `filter_pattern`).
_SCREEN_FIELDS = ("interaction", "final_interaction")


def _turn_payload(turn: JourneyTurnResult) -> dict[str, Any]:
    """One turn's published row, screens included.

    The two screens are rendered by `interaction_payload` so a journey row
    publishes the same record shape a scenario row does. They are skipped
    on the way in rather than overwritten afterwards, so `asdict` never
    deep-copies two contexts this function immediately discards.

    `success` is published explicitly because it is a derived property, not
    a field: `asdict` would drop it, and a reader comparing runs should not
    have to re-derive a turn's verdict from the `outcome` string.
    """
    payload: dict[str, Any] = {
        item.name: getattr(turn, item.name)
        for item in fields(turn)
        if item.name not in _SCREEN_FIELDS
    }
    payload["grade"] = asdict(turn.grade)
    payload["success"] = turn.success
    payload["interaction"] = _screen_payload(turn.interaction)
    payload["final_interaction"] = _screen_payload(turn.final_interaction)
    return payload


def _screen_payload(screen: InteractionContext | None) -> dict[str, Any] | None:
    """A workspace snapshot in the shared record shape, or `None`."""
    return None if screen is None else interaction_payload(screen)


def report_payload(reports: list[JourneyReport]) -> list[dict[str, Any]]:
    """JSON-ready journey result payload.

    Every workspace goes through `interaction_payload`, the same record
    shape a scenario row publishes, so one reader can compare the two
    artifacts without a second schema (a raw `asdict` would publish
    `filter_pattern` here and `filter` there).

    The conversation count is published as `successful_journeys` rather
    than `successes`. A scenario row's `successes` counts repetitions
    whose diagnosis was graded correct; this counts repetitions in which
    every turn's outcome was `success`. Two different measurements sharing
    one key is how a scoreboard ends up comparing numbers that were never
    comparable.
    """
    return [
        {
            "journey": report.journey_id,
            "root_cause": report.root_cause,
            "successful_journeys": report.successful_journeys,
            # The screen the conversation opened on: a journey score is
            # not reproducible without it.
            "interaction": _screen_payload(report.interaction),
            "runs": [
                {
                    "success": run.success,
                    "input_tokens": run.input_tokens,
                    "output_tokens": run.output_tokens,
                    "tokens_estimated": run.tokens_estimated,
                    "turns": [_turn_payload(turn) for turn in run.turns],
                }
                for run in report.runs
            ],
        }
        for report in reports
    ]


def render_markdown(reports: list[JourneyReport]) -> str:
    """Compact journey summary table.

    The verdict column is headed `all-turn journeys`, not `success`: this
    table is read beside the scenario table, whose own count is graded
    diagnoses. One word for two different measurements misreads as one.
    """
    lines = [
        "| journey | all-turn journeys | turns | malformed | writes | safety |"
        " stale targets | wrong namespace | wall s |",
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
            f"| {report.journey_id} | {report.successful_journeys}/{len(runs)} | "
            f"{turns} | {malformed} | {writes} | {safety} | {stale} | "
            f"{wrong_namespace} | {wall:.1f} |"
        )
    return "\n".join(lines)
