"""Typed eval interaction fixtures (issue #316 task 13).

A scenario or journey no longer describes the operator's screen in prose:
it records the exact `InteractionContext` the turn starts from, and the
eval bridge applies the agent's own typed actions to it. These tests pin
the loader's refusals (a fixture that loads but describes no pane is a
fixture that silently measures the wrong screen) and the bridge's
transitions.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from korvid.agent.interaction import (
    DrillDown,
    InteractionContext,
    Navigate,
    OpenDescribe,
    OpenLogs,
    PaneContext,
    ResourceIdentity,
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
    # The filter survives the view change, exactly as it does in the TUI:
    # `WorkspaceController.navigate` never touches `pane.filter_pattern`,
    # and `AgentUiController.agent_navigate` reports the surviving filter
    # back to the model ("(filter 'worker' applied)"). An eval that
    # cleared it here would score a screen production never shows.
    assert result.context.focused_pane.filter_pattern == "worker"
    # The selection does not survive: it names a row of the list the
    # operator just left, and the new list has its own rows. Reporting the
    # old one would tell the next turn a resource is on screen that is not.
    assert result.context.focused_pane.selected is None
    assert bridge.snapshot() == result.context


async def test_bridge_navigate_without_a_namespace_preserves_the_scope() -> None:
    bridge = EvalUiBridge(load_interaction(_FULL, "fixture.yaml: interaction"))
    result = await bridge.apply(Navigate(view="nodes"))
    assert result.context.focused_pane.scope == "jobs"


async def test_bridge_set_filter_updates_and_clears_the_pattern() -> None:
    bridge = EvalUiBridge(load_interaction(_FULL, "fixture.yaml: interaction"))
    await bridge.apply(SetFilter(filter_pattern="crash"))
    assert bridge.snapshot().focused_pane.filter_pattern == "crash"
    await bridge.apply(SetFilter())
    assert bridge.snapshot().focused_pane.filter_pattern is None


async def test_a_filter_the_model_set_survives_its_next_navigate() -> None:
    """The two-action sequence the divergence showed up in.

    A model that filters and then switches view sees, in production, the
    filter still applied to the new list — that is what
    `agent_navigate`'s result text tells it. An eval bridge that dropped
    the filter would hand the next turn a different screen than the TUI
    would, and any journey scored on "did it look at the right rows"
    would be scoring the fake.
    """
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))

    await bridge.apply(SetFilter(filter_pattern="crash"))
    result = await bridge.apply(Navigate(view="deployments", namespace="shop"))

    assert result.ok
    assert result.context.focused_pane.filter_pattern == "crash"
    assert bridge.snapshot().focused_pane.filter_pattern == "crash"


async def test_clearing_the_filter_before_navigating_leaves_it_clear() -> None:
    """The other half of the same rule: the bridge carries the *current*
    filter across a navigate, whatever it is — it neither invents one nor
    resurrects the one the model just cleared."""
    bridge = EvalUiBridge(load_interaction(_FULL, "fixture.yaml: interaction"))

    await bridge.apply(SetFilter())
    result = await bridge.apply(Navigate(view="nodes"))

    assert result.context.focused_pane.filter_pattern is None


async def test_bridge_open_logs_selects_the_named_pod() -> None:
    source = _minimal()
    source["focused_pane"] = {
        "kind": "deployments",
        "scope": "legacy",
        "filter": "api",
    }
    bridge = EvalUiBridge(load_interaction(source, "fixture.yaml: interaction"))
    result = await bridge.apply(OpenLogs(pod="worker-9", namespace="jobs", container="app"))
    assert result.ok
    pane = bridge.snapshot().focused_pane
    assert (pane.kind, pane.scope, pane.filter_pattern) == ("pods", "jobs", None)
    selected = pane.selected
    assert selected is not None
    assert (selected.kind, selected.namespace, selected.name) == ("pods", "jobs", "worker-9")


async def test_bridge_open_describe_selects_the_named_resource() -> None:
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))
    await bridge.apply(OpenDescribe(kind="deployments", name="api", namespace="shop"))
    pane = bridge.snapshot().focused_pane
    assert (pane.kind, pane.scope, pane.filter_pattern) == ("deployments", "shop", None)
    selected = pane.selected
    assert selected is not None
    assert (selected.kind, selected.namespace, selected.name) == ("deployments", "shop", "api")


async def test_bridge_refuses_a_display_target_missing_from_the_fixture() -> None:
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))
    bridge.bind_objects(
        (
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "namespace": "jobs",
                    "name": "worker-1",
                    "uid": "pod-1",
                },
            },
        )
    )
    before = bridge.snapshot()

    result = await bridge.apply(OpenLogs(pod="ghost", namespace="jobs"))

    assert result.ok is False
    assert result.context == before


async def test_bridge_applies_namespaced_and_cluster_scoped_target_rules() -> None:
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))
    bridge.bind_objects(
        (
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"namespace": "jobs", "name": "worker-1", "uid": "pod-1"},
            },
            {
                "apiVersion": "v1",
                "kind": "Node",
                "metadata": {"name": "node-a", "uid": "node-1"},
            },
        )
    )

    missing_namespace = await bridge.apply(OpenDescribe(kind="pods", name="worker-1"))
    node = await bridge.apply(OpenDescribe(kind="nodes", name="node-a", namespace="jobs"))

    assert missing_namespace.ok is False
    assert node.ok is True
    assert node.context.focused_pane.scope == ALL_NAMESPACES_SCOPE
    assert node.context.focused_pane.selected is not None
    assert node.context.focused_pane.selected.namespace is None


async def test_bridge_refuses_drill_for_a_parent_missing_from_the_fixture() -> None:
    source = _FULL | {
        "focused_pane": _FULL["focused_pane"] | {"kind": "deployments"},
    }
    bridge = EvalUiBridge(load_interaction(source, "fixture.yaml: interaction"))
    bridge.bind_objects(())

    result = await bridge.apply(DrillDown(name="ghost"))

    assert result.ok is False
    assert result.context.focused_pane.kind == "deployments"


async def test_bridge_drill_resolves_one_parent_across_all_namespaces() -> None:
    source = _FULL | {
        "focused_pane": _FULL["focused_pane"]
        | {"kind": "deployments", "scope": ALL_NAMESPACES_SCOPE, "filter": None},
    }
    bridge = EvalUiBridge(load_interaction(source, "fixture.yaml: interaction"))
    bridge.bind_objects(
        (
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "namespace": "shop",
                    "name": "api",
                    "uid": "deploy-api",
                },
            },
        )
    )

    result = await bridge.apply(DrillDown(name="api"))

    assert result.ok is True
    assert result.context.focused_pane.kind == "replicasets"


async def test_bridge_refuses_a_log_container_missing_from_the_pod() -> None:
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))
    bridge.bind_objects(
        (
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"namespace": "jobs", "name": "worker-1", "uid": "pod-1"},
                "spec": {"containers": [{"name": "main"}]},
            },
        )
    )

    result = await bridge.apply(OpenLogs(pod="worker-1", namespace="jobs", container="ghost"))

    assert result.ok is False


async def test_bridge_drill_uses_the_production_filter_semantics() -> None:
    source = _FULL | {
        "focused_pane": _FULL["focused_pane"] | {"kind": "deployments", "filter": "/api/"},
    }
    bridge = EvalUiBridge(load_interaction(source, "fixture.yaml: interaction"))
    bridge.bind_objects(
        (
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "namespace": "jobs",
                    "name": "api",
                    "uid": "deploy-api",
                    "labels": {"app": "api"},
                },
                "status": {"phase": "Running"},
            },
        )
    )

    result = await bridge.apply(DrillDown(name="api"))

    assert result.ok is True
    assert result.context.focused_pane.kind == "replicasets"


async def test_bridge_drill_filter_uses_the_parent_resources_metadata() -> None:
    source = _FULL | {
        "focused_pane": _FULL["focused_pane"] | {"kind": "deployments", "filter": "-l team=green"},
    }
    bridge = EvalUiBridge(load_interaction(source, "fixture.yaml: interaction"))
    bridge.bind_objects(
        (
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "namespace": "jobs",
                    "name": "api",
                    "uid": "pod-api",
                    "labels": {"team": "green"},
                },
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "namespace": "jobs",
                    "name": "api",
                    "uid": "deploy-api",
                    "labels": {"team": "red"},
                },
            },
        )
    )

    result = await bridge.apply(DrillDown(name="api"))

    assert result.ok is False


async def test_bridge_drill_down_opens_the_child_resource_list() -> None:
    source = _FULL | {
        "focused_pane": _FULL["focused_pane"] | {"kind": "deployments"},
    }
    bridge = EvalUiBridge(load_interaction(source, "fixture.yaml: interaction"))

    result = await bridge.apply(DrillDown(name="api"))

    assert result.ok is True
    pane = result.context.focused_pane
    assert (pane.kind, pane.scope, pane.filter_pattern) == ("replicasets", "jobs", "worker")
    assert pane.selected is None


async def test_bridge_drill_down_resolves_a_supported_navigation_alias() -> None:
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))
    await bridge.apply(Navigate(view="deploy", namespace="all"))

    navigated = bridge.snapshot().focused_pane
    assert (navigated.kind, navigated.scope) == ("deployments", ALL_NAMESPACES_SCOPE)

    result = await bridge.apply(DrillDown(name="api"))

    assert result.ok is True
    assert result.context.focused_pane.kind == "replicasets"


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


# ---------------------------------------------------------------------------
# The recorder is total over the union it is given
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (Navigate(view="pods", namespace="jobs"), "navigate"),
        (SetFilter(filter_pattern="api"), "set_filter"),
        (OpenLogs(pod="worker-1", namespace="jobs"), "open_logs"),
        (OpenDescribe(kind="pods", name="worker-1", namespace="jobs"), "open_describe"),
        (DrillDown(name="worker-1"), "drill_down"),
    ],
)
def test_every_shipped_action_is_recorded_under_its_own_tool_name(
    action: Any, expected: str
) -> None:
    """Each of the five arms is named explicitly, `drill_down` included."""
    from korvid.evals.interaction import _action_call

    name, arguments = _action_call(action)

    assert name == expected
    assert isinstance(arguments, dict)


def test_an_action_outside_the_union_is_refused_not_recorded_as_a_drill() -> None:
    """A sixth action must not be scored as a drill-down.

    The recorder ended in an unguarded `return "drill_down", ...`, so an
    action added to `UiAction` without a branch here was silently written
    into the journey transcript as a drill into `action.name` — a graded
    artifact describing a call the model never made. Refusing is the only
    answer the harness can give that a reader can trust; the union's own
    completeness guard (`tests/test_agent_replacement_guard.py`) is what
    keeps korvid's own actions from reaching this branch.
    """
    from korvid.evals.interaction import _action_call

    class _UnshippedAction:
        name = "worker-1"

    with pytest.raises(TypeError, match="unsupported UI action"):
        _action_call(cast("Any", _UnshippedAction()))
