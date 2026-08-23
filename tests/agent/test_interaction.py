"""Interaction contracts for the agent UI bridge."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from korvid.agent.interaction import (
    AgentUiBridge,
    ClusterFacts,
    FocusPane,
    InteractionContext,
    Navigate,
    PaneContext,
    ResourceIdentity,
    SelectResource,
    SetFilter,
    UiAction,
    UiActionResult,
)


def _context() -> InteractionContext:
    return InteractionContext(
        kube_context="dev",
        context_epoch=3,
        focused_pane=PaneContext(
            kind="pods",
            scope="default",
            filter_pattern="api",
            selected=ResourceIdentity("Pod", "default", "api-1", "uid-1"),
        ),
        secondary_pane=None,
        timeline_cursor=None,
    )


def test_resource_identity_is_frozen() -> None:
    identity = ResourceIdentity("Pod", "default", "api-1", "uid-1")

    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        identity.name = "api-2"


def test_pane_context_is_frozen() -> None:
    pane = PaneContext(
        kind="pods",
        scope="default",
        filter_pattern="api",
        selected=ResourceIdentity("Pod", "default", "api-1", "uid-1"),
    )

    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        pane.scope = "prod"


def test_cluster_facts_is_frozen() -> None:
    facts = ClusterFacts(provider="azure", distribution="aks")

    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        facts.provider = "aws"


def test_interaction_context_is_frozen() -> None:
    context = _context()

    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        context.context_epoch = 4


def test_ui_action_result_is_frozen() -> None:
    result = UiActionResult(ok=True, message="ok", context=_context())

    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        result.message = "changed"


def test_navigate_rejects_blank_view() -> None:
    with pytest.raises(ValueError, match="view"):
        Navigate(view=" ", namespace="default")


def test_select_resource_rejects_blank_name() -> None:
    with pytest.raises(ValueError, match="name"):
        SelectResource(kind="Pod", namespace="default", name=" ", uid="uid-1")


def test_set_filter_rejects_blank_pattern() -> None:
    with pytest.raises(ValueError, match="filter_pattern"):
        SetFilter(filter_pattern=" ")


def test_focus_pane_requires_left_or_right_pane() -> None:
    with pytest.raises(ValueError, match="index"):
        FocusPane(index=2)


class FakeBridge(AgentUiBridge):
    def snapshot(self) -> InteractionContext:
        return _context()

    async def apply(self, action: UiAction) -> UiActionResult:
        assert isinstance(action, Navigate)
        return UiActionResult(ok=True, message=action.view, context=_context())


@pytest.mark.asyncio
async def test_bridge_returns_updated_typed_context() -> None:
    result = await FakeBridge().apply(Navigate(view="deployments", namespace="default"))

    assert result.ok is True
    assert result.message == "deployments"
    assert result.context.context_epoch == 3
