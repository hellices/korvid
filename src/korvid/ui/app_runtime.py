"""Typed data records for the application runtime."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

if TYPE_CHECKING:
    import contextlib

    from korvid.agent.model_profiles import ModelCatalog
    from korvid.agent.session import AgentSession
    from korvid.core.audit import AuditLog
    from korvid.core.config import KorvidConfig, ModelConnectionConfig, ModelConnectionsWriter
    from korvid.core.mcp import MCPControllerBase
    from korvid.core.portforward import ForwardRegistry
    from korvid.core.session_timeline import SessionTimeline
    from korvid.core.store import ResourceStore
    from korvid.core.watch import WatchManager
    from korvid.k8s.components import ComponentRef
    from korvid.k8s.discovery import ResourceMeta
    from korvid.k8s.helm import HelmReleaseIdentity
    from korvid.k8s.helmcli import HelmCLI
    from korvid.k8s.logs import LogLine
    from korvid.k8s.metrics import MetricsPoller
    from korvid.k8s.models import GenericSummary
    from korvid.k8s.telepresence import TelepresenceCLI
    from korvid.k8s.writes import WriteOps
    from korvid.tools.executor import UIBridge
    from korvid.tools.proposals import ProposalStore
    from korvid.ui.agent_ui_controller import AgentUiController
    from korvid.ui.app_surfaces import AppInspectSurface, AppViewState
    from korvid.ui.bridge_dispatch import AppContextDispatch
    from korvid.ui.command_router import CommandRouter
    from korvid.ui.context_switch_coordinator import ContextSwitchCoordinator, ContextSwitchResult
    from korvid.ui.debug import DebugController
    from korvid.ui.drain import DrainController
    from korvid.ui.forward_controller import ForwardController
    from korvid.ui.helm_controller import HelmController
    from korvid.ui.hints import EventsFetcher, HintController
    from korvid.ui.integration_controller import IntegrationController
    from korvid.ui.log_controller import LogController
    from korvid.ui.operator_controller import OperatorController
    from korvid.ui.proposal_controller import ProposalController
    from korvid.ui.relationship_controller import RelationshipSnapshotLoader
    from korvid.ui.resource_inspect_controller import ResourceInspectController
    from korvid.ui.resource_write_controller import ResourceWriteController
    from korvid.ui.session_timeline_controller import SessionTimelineController
    from korvid.ui.shell_controller import ShellController
    from korvid.ui.transfer import TransferController
    from korvid.ui.workspace_controller import WorkspaceController
    from korvid.ui.workspace_state import WorkspaceState
    from korvid.ui.write_coordinator import WriteCoordinator

_T = TypeVar("_T")
_UNBOUND = object()


class _LateReference(Generic[_T]):
    """One-use typed reference for controller-construction cycles."""

    def __init__(self) -> None:
        self._value: _T | object = _UNBOUND

    def bind(self, value: _T) -> None:
        """Set the reference once."""
        if self._value is not _UNBOUND:
            raise RuntimeError("runtime reference already bound")
        self._value = value

    def get(self) -> _T:
        """Return the bound value or fail before construction completes."""
        if self._value is _UNBOUND:
            raise RuntimeError("runtime reference read before binding")
        return cast(_T, self._value)


@dataclasses.dataclass(frozen=True)
class AppRuntimeInputs:
    """External collaborators supplied by the composition root."""

    config: KorvidConfig
    store: ResourceStore
    watch_manager: WatchManager
    list_namespaces: Callable[[], Awaitable[list[str]]] | None
    aliases: dict[str, ResourceMeta] | None
    get_manifest: Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None
    get_helm_components: Callable[[str, str], Awaitable[list[ComponentRef]]] | None
    get_helm_release_identity: Callable[[str, str], Awaitable[HelmReleaseIdentity | None]] | None
    get_events: EventsFetcher | None
    stream_logs: Callable[..., AsyncIterator[LogLine]] | None
    agent_session: AgentSession | None
    agent_model_name: str | None
    agent_catalog: ModelCatalog | None
    agent_save_profiles: ModelConnectionsWriter | None
    rebuild_agent: Callable[[ModelConnectionConfig, str | None], AgentSession | None] | None
    disconnect_agent: Callable[[], None] | None
    agent_available: bool
    write_ops: WriteOps | None
    audit: AuditLog | None
    check_permission: Callable[[str, str, str, str | None, str, str], Awaitable[bool]] | None
    mcp: MCPControllerBase | None
    edit_text: Callable[[str], Awaitable[str | None]] | None
    metrics: MetricsPoller | None
    pod_resize_supported: bool
    forwards: ForwardRegistry | None
    provider_hint: str | None
    protected_context: str | None
    open_pod_exec: Callable[..., contextlib.AbstractAsyncContextManager[Any]] | None
    list_contexts: Callable[[], tuple[list[str], str | None]] | None
    probe_context: Callable[[str], Awaitable[None]] | None
    switch_context: Callable[[str | None], Awaitable[ContextSwitchResult]] | None
    helm: HelmCLI | None
    proposal_store: ProposalStore | None
    save_topbar: Callable[[bool], None] | None
    telepresence: TelepresenceCLI | None
    probe_traffic_manager: Callable[[], Awaitable[bool]] | None
    agent_follow_bridge: UIBridge | None
    list_relationship_objects: (
        Callable[[ResourceMeta, str | None], Awaitable[list[GenericSummary]]] | None
    )
    session_timeline: SessionTimeline | None
    watch_warning_events: Callable[[str | None], AsyncIterator[dict[str, Any]]] | None
    approval_timeout_seconds: float | None


@dataclasses.dataclass(frozen=True)
class AppRuntime:
    """Controller graph and shared state owned by one app session."""

    view: AppViewState
    relationship_loader: RelationshipSnapshotLoader | None
    context: ContextSwitchCoordinator
    timeline: SessionTimelineController
    writes: WriteCoordinator
    bridge_dispatch: AppContextDispatch
    inspect_surface: AppInspectSurface
    inspect: ResourceInspectController
    shell: ShellController
    forward: ForwardController
    transfer: TransferController
    operators: OperatorController
    helm: HelmController
    debug: DebugController
    drain: DrainController
    resource_writes: ResourceWriteController
    workspace: WorkspaceState
    hints: HintController
    logs: LogController
    workspace_controller: WorkspaceController
    proposals: ProposalController
    integrations: IntegrationController
    agent_ui: AgentUiController
    commands: CommandRouter
