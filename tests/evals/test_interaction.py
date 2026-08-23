"""Typed eval interaction fixtures (issue #316 task 13).

A scenario or journey no longer describes the operator's screen in prose:
it records the exact `InteractionContext` the turn starts from, and the
eval bridge applies the agent's own typed actions to it. These tests pin
the loader's refusals (a fixture that loads but describes no pane is a
fixture that silently measures the wrong screen) and the bridge's
transitions.
"""

from __future__ import annotations

from typing import Any

import pytest

from korvid.agent.interaction import (
    DrillDown,
    FocusPane,
    InteractionContext,
    Navigate,
    OpenDescribe,
    OpenEvidence,
    OpenLogs,
    PaneContext,
    ResourceIdentity,
    SelectResource,
    SetFilter,
)
from korvid.evals.interaction import (
    ALL_NAMESPACES_SCOPE,
    EvalUiBridge,
    interaction_payload,
    load_interaction,
)

_FULL: dict[str, Any] = {
    "kube_context": "eval-cluster",
    "context_epoch": 3,
    "focused_pane": {
        "kind": "pods",
        "scope": "jobs",
        "filter": "worker",
        "selected": {
            "kind": "Pod",
            "namespace": "jobs",
            "name": "worker-1",
            "uid": "pod-oom-1",
        },
    },
    "secondary_pane": {"kind": "events", "scope": "jobs"},
    "timeline_cursor": "2026-07-27T07:59:00Z",
}


def _minimal() -> dict[str, Any]:
    return {
        "kube_context": "eval-cluster",
        "context_epoch": 1,
        "focused_pane": {"kind": "pods", "scope": "jobs"},
    }


def test_load_interaction_reads_every_typed_field() -> None:
    context = load_interaction(_FULL, "fixture.yaml: interaction")
    assert context == InteractionContext(
        kube_context="eval-cluster",
        context_epoch=3,
        focused_pane=PaneContext(
            kind="pods",
            scope="jobs",
            filter_pattern="worker",
            selected=ResourceIdentity(
                kind="Pod", namespace="jobs", name="worker-1", uid="pod-oom-1"
            ),
        ),
        secondary_pane=PaneContext(kind="events", scope="jobs", filter_pattern=None, selected=None),
        timeline_cursor="2026-07-27T07:59:00Z",
    )


def test_load_interaction_accepts_a_pane_without_filter_or_selection() -> None:
    context = load_interaction(_minimal(), "fixture.yaml: interaction")
    assert context.focused_pane == PaneContext(
        kind="pods", scope="jobs", filter_pattern=None, selected=None
    )
    assert context.secondary_pane is None
    assert context.timeline_cursor is None


def test_load_interaction_requires_a_mapping() -> None:
    with pytest.raises(ValueError, match=r"interaction.*must be a mapping"):
        load_interaction(["pods"], "fixture.yaml: interaction")


def test_load_interaction_requires_a_focused_pane() -> None:
    raw = _minimal()
    del raw["focused_pane"]
    with pytest.raises(ValueError, match="focused_pane"):
        load_interaction(raw, "fixture.yaml: interaction")


def test_load_interaction_requires_a_pane_kind_and_scope() -> None:
    raw = _minimal()
    raw["focused_pane"] = {"kind": "pods"}
    with pytest.raises(ValueError, match="scope"):
        load_interaction(raw, "fixture.yaml: interaction")


def test_load_interaction_rejects_a_blank_pane_kind() -> None:
    raw = _minimal()
    raw["focused_pane"] = {"kind": "  ", "scope": "jobs"}
    with pytest.raises(ValueError, match="kind"):
        load_interaction(raw, "fixture.yaml: interaction")


def test_load_interaction_rejects_a_selected_resource_without_a_name() -> None:
    raw = _minimal()
    raw["focused_pane"]["selected"] = {"kind": "Pod", "namespace": "jobs"}
    with pytest.raises(ValueError, match="name"):
        load_interaction(raw, "fixture.yaml: interaction")


def test_load_interaction_rejects_a_non_mapping_selection() -> None:
    raw = _minimal()
    raw["focused_pane"]["selected"] = "jobs/worker-1"
    with pytest.raises(ValueError, match="selected"):
        load_interaction(raw, "fixture.yaml: interaction")


def test_load_interaction_rejects_unknown_keys() -> None:
    raw = _minimal()
    raw["screen"] = "resource view: pods"
    with pytest.raises(ValueError, match="unknown keys"):
        load_interaction(raw, "fixture.yaml: interaction")


def test_load_interaction_rejects_unknown_pane_keys() -> None:
    raw = _minimal()
    raw["focused_pane"]["selection"] = {"kind": "Pod", "name": "worker-1"}
    with pytest.raises(ValueError, match="unknown keys"):
        load_interaction(raw, "fixture.yaml: interaction")


def test_load_interaction_rejects_a_negative_context_epoch() -> None:
    raw = _minimal()
    raw["context_epoch"] = -1
    with pytest.raises(ValueError, match="context_epoch"):
        load_interaction(raw, "fixture.yaml: interaction")


def test_load_interaction_rejects_a_boolean_context_epoch() -> None:
    raw = _minimal()
    raw["context_epoch"] = True
    with pytest.raises(ValueError, match="context_epoch"):
        load_interaction(raw, "fixture.yaml: interaction")


def test_interaction_payload_is_json_ready_and_complete() -> None:
    payload = interaction_payload(load_interaction(_FULL, "fixture.yaml: interaction"))
    assert payload == {
        "kube_context": "eval-cluster",
        "context_epoch": 3,
        "focused_pane": {
            "kind": "pods",
            "scope": "jobs",
            "filter": "worker",
            "selected": {
                "kind": "Pod",
                "namespace": "jobs",
                "name": "worker-1",
                "uid": "pod-oom-1",
            },
        },
        "secondary_pane": {"kind": "events", "scope": "jobs", "filter": None, "selected": None},
        "timeline_cursor": "2026-07-27T07:59:00Z",
    }


async def test_bridge_navigate_repoints_the_focused_pane() -> None:
    bridge = EvalUiBridge(load_interaction(_FULL, "fixture.yaml: interaction"))
    result = await bridge.apply(Navigate(view="deployments", namespace="shop"))
    assert result.ok
    assert result.context.focused_pane.kind == "deployments"
    assert result.context.focused_pane.scope == "shop"
    # A new view is a new list: the previous filter and selection belong to
    # the pane the operator just left.
    assert result.context.focused_pane.filter_pattern is None
    assert result.context.focused_pane.selected is None
    assert bridge.snapshot() == result.context


async def test_bridge_navigate_without_a_namespace_widens_the_scope() -> None:
    bridge = EvalUiBridge(load_interaction(_FULL, "fixture.yaml: interaction"))
    result = await bridge.apply(Navigate(view="nodes"))
    assert result.context.focused_pane.scope == ALL_NAMESPACES_SCOPE


async def test_bridge_set_filter_updates_and_clears_the_pattern() -> None:
    bridge = EvalUiBridge(load_interaction(_FULL, "fixture.yaml: interaction"))
    await bridge.apply(SetFilter(filter_pattern="crash"))
    assert bridge.snapshot().focused_pane.filter_pattern == "crash"
    await bridge.apply(SetFilter())
    assert bridge.snapshot().focused_pane.filter_pattern is None


async def test_bridge_select_resource_records_the_full_identity() -> None:
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))
    await bridge.apply(
        SelectResource(kind="Pod", name="worker-2", namespace="jobs", uid="pod-oom-2")
    )
    assert bridge.snapshot().focused_pane.selected == ResourceIdentity(
        kind="Pod", namespace="jobs", name="worker-2", uid="pod-oom-2"
    )


async def test_bridge_open_logs_selects_the_named_pod() -> None:
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))
    result = await bridge.apply(OpenLogs(pod="worker-9", namespace="jobs", container="app"))
    assert result.ok
    selected = bridge.snapshot().focused_pane.selected
    assert selected is not None
    assert (selected.kind, selected.namespace, selected.name) == ("Pod", "jobs", "worker-9")


async def test_bridge_open_describe_selects_the_named_resource() -> None:
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))
    await bridge.apply(OpenDescribe(kind="deployments", name="api", namespace="shop"))
    selected = bridge.snapshot().focused_pane.selected
    assert selected is not None
    assert (selected.kind, selected.namespace, selected.name) == ("deployments", "shop", "api")


async def test_bridge_drill_down_selects_within_the_focused_pane() -> None:
    bridge = EvalUiBridge(load_interaction(_FULL, "fixture.yaml: interaction"))
    await bridge.apply(DrillDown(name="worker-3"))
    selected = bridge.snapshot().focused_pane.selected
    assert selected is not None
    assert selected.name == "worker-3"
    assert selected.namespace == "jobs"


async def test_bridge_focus_pane_refuses_a_pane_that_does_not_exist() -> None:
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))
    result = await bridge.apply(FocusPane(index=1))
    assert not result.ok
    assert bridge.snapshot().focused_pane.kind == "pods"


async def test_bridge_focus_pane_swaps_the_two_panes() -> None:
    bridge = EvalUiBridge(load_interaction(_FULL, "fixture.yaml: interaction"))
    result = await bridge.apply(FocusPane(index=1))
    assert result.ok
    assert bridge.snapshot().focused_pane.kind == "events"
    assert bridge.snapshot().secondary_pane is not None
    assert bridge.snapshot().secondary_pane.kind == "pods"  # type: ignore[union-attr]


async def test_bridge_open_evidence_moves_the_timeline_cursor() -> None:
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))
    result = await bridge.apply(OpenEvidence(ref="E1"))
    assert result.ok
    assert bridge.snapshot().timeline_cursor == "E1"


async def test_bridge_records_every_applied_action_in_order() -> None:
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))
    await bridge.apply(Navigate(view="deployments", namespace="shop"))
    await bridge.apply(SetFilter(filter_pattern="api"))
    assert bridge.actions == (
        Navigate(view="deployments", namespace="shop"),
        SetFilter(filter_pattern="api"),
    )


async def test_bridge_reset_restores_an_authored_starting_interaction() -> None:
    start = load_interaction(_minimal(), "fixture.yaml: interaction")
    bridge = EvalUiBridge(start)
    await bridge.apply(Navigate(view="nodes"))
    bridge.reset(start)
    assert bridge.snapshot() == start
    assert bridge.actions == ()


def test_bridge_never_reaches_a_real_screen() -> None:
    """The eval bridge is the whole workspace: no Textual app is involved."""
    import korvid.evals.interaction as module

    source = module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "korvid.ui" not in text
    assert "textual" not in text
