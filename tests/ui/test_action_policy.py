"""Characterization tests for the extracted action binding policy (#388).

`ActionPolicy` is a behavior-preserving extraction of `KorvidApp.check_action`
(issue #114): the same overloaded-key routing, log-pane gating and helm
synthetic-view exception, now testable without composing the Textual app.
"""

from collections.abc import Callable, Mapping

from korvid.core.store import Summary
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import HELM_RELEASES_META
from korvid.ui.action_policy import ActionPolicy
from korvid.ui.view_state import ViewState


class FakeView(ViewState):
    def __init__(self, meta: ResourceMeta) -> None:
        self.meta = meta

    def current_kind(self) -> str:
        return self.meta.plural

    def current_scope(self) -> str:
        return "default"

    def canonical_kind(self, kind: str) -> str:
        return self.meta.plural

    def aliases(self) -> Mapping[str, ResourceMeta]:
        return {self.meta.plural: self.meta}

    def resources(self, kind: str, scope: str) -> list[Summary]:
        return []

    def readonly(self) -> bool:
        return False

    def default_namespace(self) -> str | None:
        return "default"

    def selected_ns_name(self, *, notify: bool = True) -> tuple[str | None, str | None]:
        return "default", "selected"

    def selected_uid(self, namespace: str | None, name: str) -> str | None:
        return "uid-selected"

    def gvr_label(self, meta: ResourceMeta) -> str:
        return meta.plural

    def write_locus(self, namespace: str | None) -> str:
        return f"in namespace {namespace}" if namespace else "cluster-wide"


def _policy(
    *,
    group: str,
    plural: str,
    synthetic: bool = False,
    log_pane_open: Callable[[], bool] = lambda: False,
    agent_available: Callable[[], bool] = lambda: True,
) -> ActionPolicy:
    meta = ResourceMeta(
        kind=plural.removesuffix("s").title(),
        plural=plural,
        group=group,
        version="v1",
        namespaced=plural != "nodes",
        synthetic=synthetic,
    )
    return ActionPolicy(
        view=FakeView(meta),
        agent_available=agent_available,
        log_pane_open=log_pane_open,
    )


def test_binding_policy_routes_overloaded_actions_by_resource_identity() -> None:
    policy = _policy(group="", plural="pods")
    assert policy.binding_enabled("logs") is True
    assert policy.binding_enabled("hint_details") is True
    assert policy.binding_enabled("helm_install") is False
    assert policy.binding_enabled("cordon_node") is False


def test_binding_policy_uses_pane_and_composition_state() -> None:
    log_open = False
    agent_available = False
    policy = _policy(
        group="",
        plural="pods",
        log_pane_open=lambda: log_open,
        agent_available=lambda: agent_available,
    )
    assert policy.binding_enabled("log_wrap") is False
    assert policy.binding_enabled("toggle_agent") is False
    assert policy.binding_enabled("help") is True


def test_binding_policy_preserves_helm_delete_exception() -> None:
    policy = _policy(
        group=HELM_RELEASES_META.group,
        plural=HELM_RELEASES_META.plural,
        synthetic=True,
    )
    assert policy.binding_enabled("delete_resource") is True
    assert policy.binding_enabled("edit_resource") is False
