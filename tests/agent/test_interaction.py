"""Interaction contracts for the agent UI bridge."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from inspect import Signature, signature
from typing import get_type_hints

import pytest

import korvid.agent.interaction as interaction_module
from korvid.agent.interaction import (
    AgentUiBridge,
    ClusterFacts,
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


def _assign(target: object, field: str, value: object) -> None:
    """Attempt a field write the type checker rejects statically.

    Every contract below is a frozen dataclass, so mypy reports a direct
    assignment as writing a read-only property — while the point of these
    tests is that the *runtime* refuses it too. Routing through `setattr`
    keeps the runtime behaviour identical and leaves the static half of
    the guarantee to the frozen declaration itself.
    """
    setattr(target, field, value)


def test_resource_identity_is_frozen() -> None:
    identity = ResourceIdentity("Pod", "default", "api-1", "uid-1")

    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        _assign(identity, "name", "api-2")


def test_resource_identity_contract_allows_optional_namespace_and_uid() -> None:
    hints = get_type_hints(ResourceIdentity)

    assert hints == {
        "kind": str,
        "namespace": str | None,
        "name": str,
        "uid": str | None,
    }

    identity = ResourceIdentity(kind="Pod", namespace=None, name="api-1", uid=None)

    assert identity.kind == "Pod"
    assert identity.namespace is None
    assert identity.name == "api-1"
    assert identity.uid is None


def test_pane_context_is_frozen() -> None:
    pane = PaneContext(
        kind="pods",
        scope="default",
        filter_pattern="api",
        selected=ResourceIdentity("Pod", "default", "api-1", "uid-1"),
    )

    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        _assign(pane, "scope", "prod")


def test_cluster_facts_is_frozen() -> None:
    facts = ClusterFacts(provider="azure", distribution="aks")

    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        _assign(facts, "provider", "aws")


def test_interaction_context_is_frozen() -> None:
    context = _context()

    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        _assign(context, "context_epoch", 4)


def test_navigate_contract_uses_optional_namespace_default() -> None:
    params = signature(Navigate).parameters

    assert params["view"].default is Signature.empty
    assert params["namespace"].default is None

    action = Navigate(view="deployments")

    assert action.view == "deployments"
    assert action.namespace is None


def test_ui_action_result_is_frozen() -> None:
    result = UiActionResult(ok=True, message="ok", context=_context())

    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        _assign(result, "message", "changed")


def test_navigate_rejects_blank_view() -> None:
    with pytest.raises(ValueError, match="view"):
        Navigate(view=" ", namespace="default")


def test_select_resource_contract_requires_kind_and_name() -> None:
    params = signature(SelectResource).parameters

    assert params["kind"].default is Signature.empty
    assert params["name"].default is Signature.empty
    assert params["namespace"].default is None
    assert params["uid"].default is None

    action = SelectResource(kind="Pod", name="api-1")

    assert action.kind == "Pod"
    assert action.name == "api-1"
    assert action.namespace is None
    assert action.uid is None


def test_select_resource_rejects_blank_name() -> None:
    with pytest.raises(ValueError, match="name"):
        SelectResource(kind="Pod", namespace="default", name=" ", uid="uid-1")


def test_set_filter_rejects_blank_pattern() -> None:
    with pytest.raises(ValueError, match="filter_pattern"):
        SetFilter(filter_pattern=" ")


def test_set_filter_none_clears_the_filter_explicitly() -> None:
    assert SetFilter().filter_pattern is None
    assert SetFilter(None).filter_pattern is None


def test_focus_pane_requires_left_or_right_pane() -> None:
    with pytest.raises(ValueError, match="index"):
        FocusPane(index=2)


def test_open_logs_contract_requires_pod_and_namespace() -> None:
    params = signature(OpenLogs).parameters

    assert params["pod"].default is Signature.empty
    assert params["namespace"].default is Signature.empty
    assert params["container"].default is None

    action = OpenLogs(pod="api-1", namespace="default")

    assert action.pod == "api-1"
    assert action.namespace == "default"
    assert action.container is None


def test_open_logs_rejects_blank_required_fields() -> None:
    with pytest.raises(ValueError, match="pod"):
        OpenLogs(pod=" ", namespace="default")
    with pytest.raises(ValueError, match="namespace"):
        OpenLogs(pod="api-1", namespace=" ")


def test_open_describe_contract_requires_kind_and_name() -> None:
    params = signature(OpenDescribe).parameters

    assert params["kind"].default is Signature.empty
    assert params["name"].default is Signature.empty
    assert params["namespace"].default is None

    action = OpenDescribe(kind="Pod", name="api-1")

    assert action.kind == "Pod"
    assert action.name == "api-1"
    assert action.namespace is None


def test_open_describe_rejects_blank_required_fields() -> None:
    with pytest.raises(ValueError, match="kind"):
        OpenDescribe(kind=" ", name="api-1")
    with pytest.raises(ValueError, match="name"):
        OpenDescribe(kind="Pod", name=" ")


def test_drill_down_contract_requires_name() -> None:
    params = signature(DrillDown).parameters

    assert params["name"].default is Signature.empty

    action = DrillDown(name="deployments")

    assert action.name == "deployments"


def test_drill_down_rejects_blank_name() -> None:
    with pytest.raises(ValueError, match="name"):
        DrillDown(name=" ")


def test_open_evidence_contract_requires_ref() -> None:
    params = signature(OpenEvidence).parameters

    assert params["ref"].default is Signature.empty

    action = OpenEvidence(ref="trace://event-123")

    assert action.ref == "trace://event-123"


def test_open_evidence_rejects_blank_ref() -> None:
    with pytest.raises(ValueError, match="ref"):
        OpenEvidence(ref=" ")


def test_interaction_module_exports_live_at_package_level() -> None:
    assert not hasattr(interaction_module, "__all__")


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
