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

from korvid.agent.interaction import (
    InteractionContext,
    Navigate,
    OpenLogs,
    PaneContext,
    ResourceIdentity,
    SetFilter,
)
from korvid.evals.interaction import (
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


async def test_bridge_records_every_applied_action_in_order() -> None:
    bridge = EvalUiBridge(load_interaction(_minimal(), "fixture.yaml: interaction"))
    await bridge.apply(Navigate(view="deployments", namespace="shop"))
    await bridge.apply(SetFilter(filter_pattern="api"))
    assert bridge.actions == (
        Navigate(view="deployments", namespace="shop"),
        SetFilter(filter_pattern="api"),
    )


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
