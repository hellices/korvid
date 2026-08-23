"""Persistent multi-turn journey runner tests."""

from __future__ import annotations

import json
from typing import Any

from korvid.evals.harness import resolve_eval_policy
from korvid.evals.interaction import EvalUiBridge

from korvid.agent.interaction import InteractionContext, OpenDescribe
from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.journey import load_journeys
from korvid.evals.journey_runner import run_journey
from korvid.evals.live_journey import NamespaceBoundReadOps
from korvid.evals.scripted import ScriptedProvider
from korvid.tools.executor import ToolExecutor


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, Any]:
    return {
        "type": "tool_call",
        "id": call_id,
        "name": name,
        "arguments": json.dumps(args),
    }


def _text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text_delta", "text": text}, {"type": "done"}]


def _remember(bridges: list[EvalUiBridge], context: InteractionContext) -> EvalUiBridge:
    """Bridge factory that keeps every bridge a journey run built."""
    bridge = EvalUiBridge(context)
    bridges.append(bridge)
    return bridge


def _armed_schemas() -> dict[str, dict[str, Any]]:
    """The tool schemas the journey policy actually arms."""
    policy = resolve_eval_policy(ScriptedProvider([[{"type": "done"}]]))
    return {str(tool["function"]["name"]): dict(tool) for tool in policy.tools}


async def test_run_journey_persists_history_and_honors_user_correction() -> None:
    journey = next(
        item
        for item in load_journeys(
            __import__(
                "korvid.evals.journey", fromlist=["bundled_journeys_dir"]
            ).bundled_journeys_dir()
        )
        if item.id == "triage-and-correct"
    )
    script: list[list[dict[str, Any]]] = [
        [
            _call(
                "list_resources",
                {"kind": "pods", "namespace": "shop"},
                "c1",
            ),
            {"type": "done"},
        ],
        _text("checkout and payments need attention; inspect checkout first."),
        [
            _call(
                "diagnose_pod",
                {"pod": "payments-1", "namespace": "shop"},
                "c2",
            ),
            {"type": "done"},
        ],
        _text("payments has an unauthorized registry authentication failure."),
        [
            _call(
                "open_describe",
                {"kind": "pods", "name": "payments-1", "namespace": "shop"},
                "c3",
            ),
            {"type": "done"},
        ],
        _text("payments needs registry credentials or an image pull secret."),
    ]
    provider = ScriptedProvider(script)
    bridges: list[EvalUiBridge] = []

    report = await run_journey(
        journey,
        provider_factory=lambda: provider,
        executor_factory=lambda fixture: ToolExecutor(
            FakeKubeClient(fixture),
            builtin_aliases(),
        ),
        repetitions=1,
        bridge_factory=lambda context: _remember(bridges, context),
    )

    run = report.runs[0]
    assert run.success is True
    assert [turn.success for turn in run.turns] == [True, True, True]
    assert run.turns[1].forbidden_target_calls == 0
    assert run.turns[2].tool_names == ("open_describe",)
    assert bridges[-1].actions[-1] == OpenDescribe(kind="pods", name="payments-1", namespace="shop")
    selected = bridges[-1].snapshot().focused_pane.selected
    assert selected is not None
    assert selected.name == "payments-1"
    # One provider instance serves every user turn; six completions proves the
    # runtime was not recreated between turns.
    assert provider._cursor == 6


async def test_live_boundary_rejection_fails_an_otherwise_successful_turn() -> None:
    journey = next(
        item
        for item in load_journeys(
            __import__(
                "korvid.evals.journey", fromlist=["bundled_journeys_dir"]
            ).bundled_journeys_dir()
        )
        if item.id == "triage-and-correct"
    )
    script: list[list[dict[str, Any]]] = [
        [
            _call("list_resources", {"kind": "nodes"}, "c0"),
            {"type": "done"},
        ],
        [
            _call("list_resources", {"kind": "pods", "namespace": "shop"}, "c1"),
            {"type": "done"},
        ],
        _text("checkout and payments need attention; inspect checkout first."),
        [
            _call(
                "diagnose_pod",
                {"pod": "payments-1", "namespace": "shop"},
                "c2",
            ),
            {"type": "done"},
        ],
        _text("payments has an unauthorized registry authentication failure."),
        [
            _call(
                "open_describe",
                {"kind": "pods", "name": "payments-1", "namespace": "shop"},
                "c3",
            ),
            {"type": "done"},
        ],
        _text("payments needs registry credentials or an image pull secret."),
    ]

    report = await run_journey(
        journey,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda fixture: ToolExecutor(
            NamespaceBoundReadOps(FakeKubeClient(fixture), "shop"),
            builtin_aliases(),
        ),
        repetitions=1,
    )

    first = report.runs[0].turns[0]
    assert first.grade.evidence_fetched is True
    assert first.wrong_namespace_calls == 1
    assert first.success is False


async def test_run_journey_fails_redundant_turn_budget() -> None:
    journey = next(
        item
        for item in load_journeys(
            __import__(
                "korvid.evals.journey", fromlist=["bundled_journeys_dir"]
            ).bundled_journeys_dir()
        )
        if item.id == "healthy-stop"
    )
    script: list[list[dict[str, Any]]] = [
        [
            _call("list_resources", {"kind": "pods", "namespace": "catalog"}, "c1"),
            {"type": "done"},
        ],
        _text("The namespace is healthy."),
        [
            _call("list_resources", {"kind": "pods", "namespace": "catalog"}, "c2"),
            {"type": "done"},
        ],
        [
            _call("list_resources", {"kind": "pods", "namespace": "catalog"}, "c3"),
            {"type": "done"},
        ],
        _text("No further investigation is needed; stop here."),
    ]

    report = await run_journey(
        journey,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda fixture: ToolExecutor(FakeKubeClient(fixture), builtin_aliases()),
        repetitions=1,
    )

    assert report.runs[0].turns[1].tool_calls == 2
    assert report.runs[0].turns[1].success is False


async def test_each_turn_must_fetch_its_own_required_evidence() -> None:
    journey = next(
        item
        for item in load_journeys(
            __import__(
                "korvid.evals.journey", fromlist=["bundled_journeys_dir"]
            ).bundled_journeys_dir()
        )
        if item.id == "healthy-stop"
    )
    script: list[list[dict[str, Any]]] = [
        [
            _call("list_resources", {"kind": "pods", "namespace": "catalog"}, "c1"),
            {"type": "done"},
        ],
        _text("The namespace is healthy."),
        _text("No further investigation is needed; stop here."),
    ]
    report = await run_journey(
        journey,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda fixture: ToolExecutor(FakeKubeClient(fixture), builtin_aliases()),
        repetitions=1,
    )
    second = report.runs[0].turns[1]
    assert second.grade.evidence_fetched is False
    assert second.success is False


def test_malformed_call_rejects_wrong_json_property_types() -> None:
    from korvid.evals.journey_runner import _malformed_call

    schemas = _armed_schemas()
    assert _malformed_call(
        "list_resources",
        '{"kind": 7, "namespace": ["catalog"]}',
        schemas,
    )


def test_wrong_namespace_handles_malformed_and_cluster_scoped_calls() -> None:
    from korvid.evals.journey_runner import _wrong_namespace

    schemas = _armed_schemas()
    assert _wrong_namespace(
        "list_resources",
        {"kind": "pods", "namespace": ["shop"]},
        schemas,
        {"shop"},
    )
    assert not _wrong_namespace(
        "get_resource",
        {"kind": "nodes", "name": "node-a"},
        schemas,
        {"shop"},
    )
    assert not _wrong_namespace(
        "get_resource",
        {"kind": " Node ", "name": "node-a"},
        schemas,
        {"shop"},
    )
    assert not _wrong_namespace(
        "navigate",
        {"view": " Nodes "},
        schemas,
        {"shop"},
    )


def test_turn_tally_tracks_write_attempts_and_safety_violations() -> None:
    from korvid.agent.events import ToolCallFinished, ToolCallStarted
    from korvid.evals.journey_runner import _TurnTally

    tally = _TurnTally({})
    tally.note(
        ToolCallStarted(
            call_id="w1",
            name="delete_resource",
            arguments='{"kind":"pods","name":"x"}',
        )
    )
    tally.note(
        ToolCallFinished(
            call_id="w1",
            name="delete_resource",
            ok=True,
            summary="deleted",
        )
    )
    assert tally.write_attempts == 1
    assert tally.safety_violations == 1


async def test_discarded_parallel_calls_count_toward_budget_and_stale_targets() -> None:
    journey = next(
        item
        for item in load_journeys(
            __import__(
                "korvid.evals.journey", fromlist=["bundled_journeys_dir"]
            ).bundled_journeys_dir()
        )
        if item.id == "triage-and-correct"
    )
    script: list[list[dict[str, Any]]] = [
        [
            _call("list_resources", {"kind": "pods", "namespace": "shop"}, "c1"),
            {"type": "done"},
        ],
        _text("checkout and payments need attention."),
        [
            _call(
                "diagnose_pod",
                {"pod": "payments-1", "namespace": "shop"},
                "c2",
            ),
            _call(
                "diagnose_pod",
                {"pod": "checkout-1", "namespace": "other"},
                "c3",
            ),
            {"type": "done"},
        ],
        _text("payments has unauthorized registry authentication."),
        [
            _call(
                "open_describe",
                {"kind": "pods", "name": "payments-1", "namespace": "shop"},
                "c4",
            ),
            {"type": "done"},
        ],
        _text("payments needs registry credentials."),
    ]
    report = await run_journey(
        journey,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda fixture: ToolExecutor(FakeKubeClient(fixture), builtin_aliases()),
        repetitions=1,
    )
    turn = report.runs[0].turns[1]
    assert turn.tool_calls == 2
    assert turn.forbidden_target_calls == 1
    assert turn.success is False


async def test_wrong_namespace_call_fails_even_if_later_call_is_on_target() -> None:
    journey = next(
        item
        for item in load_journeys(
            __import__(
                "korvid.evals.journey", fromlist=["bundled_journeys_dir"]
            ).bundled_journeys_dir()
        )
        if item.id == "triage-and-correct"
    )
    script: list[list[dict[str, Any]]] = [
        [
            _call("list_resources", {"kind": "pods", "namespace": "shop"}, "c1"),
            {"type": "done"},
        ],
        _text("Checkout and payments need attention; inspect checkout first."),
        [
            _call(
                "get_events",
                {"kind": "pods", "name": "payments-1", "namespace": "other"},
                "c2",
            ),
            _call(
                "diagnose_pod",
                {"pod": "payments-1", "namespace": "shop"},
                "c3",
            ),
            {"type": "done"},
        ],
        _text("Payments has an unauthorized registry authentication failure."),
        [
            _call(
                "open_describe",
                {"kind": "pods", "name": "payments-1", "namespace": "shop"},
                "c4",
            ),
            {"type": "done"},
        ],
        _text("Fix the invalid payments image or registry credentials."),
    ]
    report = await run_journey(
        journey,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda fixture: ToolExecutor(FakeKubeClient(fixture), builtin_aliases()),
        repetitions=1,
    )
    turn = report.runs[0].turns[1]
    assert turn.wrong_namespace_calls == 1
    assert turn.success is False
