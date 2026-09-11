"""Typed assembly of the session-scoped Textual controller graph."""

from __future__ import annotations

import contextlib
import dataclasses
import functools
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
)
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

if TYPE_CHECKING:
    # Annotation-only: the base TUI must not import the embedded-agent
    # runtime at startup (issue #73) — the composition root injects it
    # only when the [agent] extra is installed and wired.
    from korvid.agent.session import AgentSession
    from korvid.ui.app import KorvidApp


from korvid.agent.model_profiles import ModelCatalog
from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig, ModelConnectionConfig, ModelConnectionsWriter
from korvid.core.mcp import MCPControllerBase
from korvid.core.portforward import (
    ForwardRegistry,
)
from korvid.core.session_timeline import SessionTimeline
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.components import (
    ComponentRef,
)
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import (
    HelmReleaseIdentity,
)
from korvid.k8s.helmcli import HelmCLI
from korvid.k8s.logs import LogLine
from korvid.k8s.metrics import MetricsPoller
from korvid.k8s.models import GenericSummary
from korvid.k8s.telepresence import TelepresenceCLI
from korvid.k8s.writes import WriteOps
from korvid.tools.executor import UIBridge
from korvid.tools.proposals import ProposalStore
from korvid.ui.agent_ui_controller import (
    AgentUiController,
)
from korvid.ui.app_surfaces import (
    AppAgentPanel,
    AppAgentScreens,
    AppContextSurface,
    AppInspectSurface,
    AppProposalEvents,
    AppProposalScreens,
    AppReviewTasks,
    AppSessionConfiguration,
    AppTransferScreens,
    AppUiSurface,
    AppViewState,
    AppWorkspaceSurface,
    _RelationshipLister,
)
from korvid.ui.bridge_dispatch import AppContextDispatch
from korvid.ui.command_router import CommandRouter
from korvid.ui.context_switch_coordinator import (
    ContextSwitchCoordinator,
    ContextSwitchResult,
)
from korvid.ui.debug import DebugController, DebugSettings
from korvid.ui.drain import DrainController
from korvid.ui.forward_controller import ForwardController
from korvid.ui.helm_controller import HelmController
from korvid.ui.hints import EventsFetcher, HintController
from korvid.ui.integration_controller import IntegrationController
from korvid.ui.log_controller import LogController
from korvid.ui.operator_controller import OperatorController
from korvid.ui.proposal_controller import (
    ProposalController,
)
from korvid.ui.relationship_controller import RelationshipSnapshotLoader
from korvid.ui.resource_inspect_controller import ResourceInspectController
from korvid.ui.resource_write_controller import (
    ResourceWriteController,
)
from korvid.ui.session_timeline_controller import (
    SessionTimelineController,
)
from korvid.ui.shell_controller import ShellController, ShellSettings
from korvid.ui.transfer import TransferController
from korvid.ui.workspace_controller import (
    WorkspaceController,
)
from korvid.ui.workspace_state import WorkspaceState
from korvid.ui.write_coordinator import (
    WriteCoordinator,
)

_T = TypeVar("_T")
_UNBOUND = object()


class _LateReference(Generic[_T]):
    """One-use typed reference for controller-construction cycles."""

    def __init__(self) -> None:
        self._value: _T | object = _UNBOUND

    def bind(self, value: _T) -> None:
        if self._value is not _UNBOUND:
            raise RuntimeError("runtime reference already bound")
        self._value = value

    def get(self) -> _T:
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


def build_app_runtime(app: KorvidApp, inputs: AppRuntimeInputs) -> AppRuntime:
    """Construct the session-scoped controller graph for the app."""

    config = inputs.config
    list_contexts = inputs.list_contexts
    probe_context = inputs.probe_context
    switch_context = inputs.switch_context
    list_relationship_objects = inputs.list_relationship_objects
    session_timeline = inputs.session_timeline
    watch_warning_events = inputs.watch_warning_events
    protected_context = inputs.protected_context
    proposal_store = inputs.proposal_store
    approval_timeout_seconds = inputs.approval_timeout_seconds
    telepresence = inputs.telepresence
    probe_traffic_manager = inputs.probe_traffic_manager
    agent_follow_bridge = inputs.agent_follow_bridge
    agent_session = inputs.agent_session
    agent_model_name = inputs.agent_model_name
    agent_catalog = inputs.agent_catalog
    agent_save_profiles = inputs.agent_save_profiles
    rebuild_agent = inputs.rebuild_agent
    disconnect_agent = inputs.disconnect_agent
    agent_available = inputs.agent_available

    workspace_ref = _LateReference[WorkspaceController]()
    logs_ref = _LateReference[LogController]()
    hints_ref = _LateReference[HintController]()
    timeline_ref = _LateReference[SessionTimelineController]()
    proposals_ref = _LateReference[ProposalController]()
    forward_ref = _LateReference[ForwardController]()
    writes_ref = _LateReference[WriteCoordinator]()
    agent_ref = _LateReference[AgentUiController]()
    shell_ref = _LateReference[ShellController]()
    debug_ref = _LateReference[DebugController]()

    #: The session's one typed view surface (issue #187): every
    #: controller reads the focused pane through it, and the selection
    #: reads (`selected_ns_name`, `selected_uid`) live on it rather than
    #: on the app - they are widget/store reads about "what the user is
    #: looking at", which is exactly what this boundary names.
    view = AppViewState(app)
    #: Operational relationship graph (issue #281): the loader is a
    #: pure orchestrator built once around the injected LIST callable;
    #: it performs no Textual operations, so the app owns the worker
    #: that runs it (see action_relationships). None disables `g`
    #: entirely (no cluster connection, or the composition root chose
    #: not to wire it).
    relationship_loader: RelationshipSnapshotLoader | None = (
        RelationshipSnapshotLoader(_RelationshipLister(list_relationship_objects))
        if list_relationship_objects is not None
        else None
    )
    #: Runtime `:ctx` switching (issue #36 / Deep Task 8): the switch
    #: epoch, the in-flight claim, the listing/probe/swap collaborators,
    #: the picker with its completion prefetch, the blocker set, the MCP
    #: quiesce, the ordered teardown, the retarget with its recovery, and
    #: the timeline/watch/metrics resume all live in the coordinator. It
    #: *is* this session's single `ContextGuard`, so every controller
    #: below revalidates against the same state the transaction mutates.
    #: The late-bound participant accessors exist because those
    #: controllers take this coordinator as their guard and so cannot be
    #: constructed first; each one hands over the real collaborator, and
    #: no step of the transaction routes back through the app.
    context = ContextSwitchCoordinator(
        ui=AppUiSurface(app),
        surface=AppContextSurface(app),
        view=view,
        session=AppSessionConfiguration(app),
        store=app.store,
        watches=app.watch_manager,
        workspace=workspace_ref.get,
        logs=logs_ref.get,
        hints=hints_ref.get,
        timeline=timeline_ref.get,
        proposals=proposals_ref.get,
        forwards=forward_ref.get,
        registry=lambda: app._forwards,
        writes=writes_ref.get,
        agent=agent_ref.get,
        mcp=lambda: app._mcp,
        audit=lambda: app._audit,
        list_contexts=list_contexts,
        probe_context=probe_context,
        switch_context=switch_context,
    )
    #: Bounded session timeline (issue #282, Task 3): producers, the
    #: Warning-event feed lifecycle, and the modal open/navigate flow
    #: all live in the controller. None (the constructor's `timeline`
    #: kwarg) disables every producer - the watch sink stays unwired
    #: and `action_timeline` warns instead of opening a screen, so a
    #: build without the feature pays nothing per event.
    timeline = SessionTimelineController(
        ui=AppUiSurface(app),
        view=view,
        watch_manager=app.watch_manager,
        timeline=session_timeline,
        get_epoch=context.epoch,
        epoch_crossed=context.crossed,
        watch_warning_events=watch_warning_events,
        selected_resource=lambda: workspace_ref.get().selected_timeline_resource(),
        navigate=lambda kind, namespace, name, epoch: workspace_ref.get().jump_to_object(
            kind, namespace, name, epoch=epoch
        ),
    )
    timeline_ref.bind(timeline)

    #: The write security perimeter (issue #187): approval, epoch and
    #: identity revalidation, the synchronous in-flight write
    #: reservation `:ctx` consults, the fail-closed intent audit, the
    #: audited mutation, and every approval dialog - including the
    #: protected-context layer (issue #83) it owns the marker for. This
    #: *is* the `WriteGate` the controllers hold, so there is exactly
    #: one implementation of that ordering in the app.
    writes = WriteCoordinator(
        ui=AppUiSurface(app),
        view=view,
        context=context,
        audit=lambda: app._audit,
        timeline=timeline,
        check_permission=lambda: app._check_permission,
        relationship_loader=lambda: relationship_loader,
        focused_pane=lambda: app._pane,
        canonical_meta_kind=app._canonical_meta_kind,
        protected_context=protected_context,
    )
    writes_ref.bind(writes)

    #: Where foreign `UIBridge` calls run (issue #165): activated in
    #: on_mount with the app's own execution context and invalidated on
    #: unmount, so a pre-mount MCP request or one racing teardown is
    #: refused as 'UI not ready' instead of composing widgets in the
    #: caller's context or against an unmounted app.
    bridge_dispatch = AppContextDispatch()
    #: Read-only resource inspection (issue #187 / Deep Task 9): describe
    #: (selected and named) with its Secret masking rule and provider
    #: footer, the container pick behind Enter, the hint-details overlay,
    #: the store lookups those share, and the pod-identity guard the
    #: interactive flows bind an approved action to. The shell and log
    #: collaborators are late-bound because they are constructed below.
    inspect_surface = AppInspectSurface(app)
    inspect_controller = ResourceInspectController(
        ui=AppUiSurface(app),
        view=view,
        context=context,
        surface=inspect_surface,
        shell=shell_ref.get,
        logs=logs_ref.get,
        get_manifest=lambda: app._get_manifest,
        get_events=lambda: app._get_events,
        stream_logs=lambda: app._stream_logs,
        target_uid=lambda kind, ns, name: app._target_uid(kind, ns, name),
        audit=lambda: app._audit,
        provider_hint=lambda: app._provider_hint,
    )
    #: interactive sessions (issue #187): pod exec, the kubectl debug
    #: fallback, and the approval-gated node shell. run_worker ownership
    #: and the write perimeter stay here.
    shell = ShellController(
        gate=writes,
        view=view,
        ui=AppUiSurface(app),
        debug=debug_ref.get,
        audit=lambda: app._audit,
        get_manifest=lambda: app._get_manifest,
        pod_containers=inspect_controller.pod_containers,
        node_target=lambda action: app._node_target(action),
        target_uid=lambda kind, ns, name: app._target_uid(kind, ns, name),
        settings=lambda: ShellSettings(
            kube_context=app.config.kube_context,
            debug_default_image=app.config.debug_default_image,
            debug_images=app.config.debug_images,
            node_shell_image=app.config.node_shell_image,
            node_shell_namespace=app.config.node_shell_namespace,
        ),
    )
    shell_ref.bind(shell)

    #: port-forward session lifecycle (issue #187): launch, reattach,
    #: liveness polling and the off-pump audit queue. The controller owns
    #: that state - nothing else reads it.
    forward_controller = ForwardController(
        gate=writes,
        ui=AppUiSurface(app),
        view=view,
        forwards=lambda: app._forwards,
        audit=lambda: app._audit,
        get_manifest=lambda: app._get_manifest,
    )
    forward_ref.bind(forward_controller)

    #: the ctrl+t transfer journey (issue #91 U3a / Deep Task 9): the
    #: controller owns the selection guards, the container pick, the
    #: dialog with its read-only remote listing, the upload approval it
    #: composes out of `WriteCoordinator`, the stream task and the
    #: in-flight serialization. The app keeps run_worker ownership and
    #: the Textual entry points as thin delegates.
    transfer = TransferController(
        ui=AppUiSurface(app),
        view=view,
        writes=writes,
        screens=AppTransferScreens(app),
        open_pod_exec=lambda: app._open_pod_exec,
        audit=lambda: app._audit,
        find_pod=inspect_controller.find_pod,
        target_uid=lambda kind, ns, name: app._target_uid(kind, ns, name),
        pod_uid_unchanged=inspect_controller.pod_uid_unchanged,
    )
    #: OLM workflows (issue #187): the wizard, InstallPlan approval and
    #: the CSV-aware uninstall. The install dialog re-checks the
    #: subscription UID in its own callback, so it drives the gate's
    #: permitted/run directly rather than the standard confirm flow.
    operators = OperatorController(
        gate=writes,
        view=view,
        ui=AppUiSurface(app),
        write_ops=lambda: app._write_ops,
        get_manifest=lambda: app._get_manifest,
        confirm_screen=writes.confirm_screen,
        uid_intact_after_fetch=writes.uid_intact_after_fetch,
        precheck_keybinding_write=writes.precheck_keybinding_write,
        write_target=writes.write_target,
    )
    #: helm write workflows (issue #187): the controller owns the wizard,
    #: preview and command construction; the approval gate, context
    #: revalidation and audited execution stay here, so the write
    #: perimeter keeps a single implementation.
    helm_controller = HelmController(
        helm=lambda: app._helm,
        get_release_identity=lambda: app._get_helm_release_identity,
        gate=writes,
        view=view,
        ui=AppUiSurface(app),
        # Late-binding for the same reason as everywhere else: the
        # workspace controller is constructed after this one.
        navigation=workspace_ref.get,
        # Late-binding, like the other controllers' app callables: the
        # editor entry points are patched per test, so binding the bound
        # method at construction would freeze whatever existed then.
        edit_in_external_editor=lambda *a, **k: app._edit_in_external_editor(*a, **k),
        edit_text=lambda: app._edit_text,
    )
    # Debug-fallback execution (issue #97 U3c / Deep Task 10): the
    # controller owns the gated, audited kubectl debug run *and* the
    # image-pull retry offer. The initial image picker, the RBAC
    # pre-check and the first approval stay with `ShellController`.
    debug = DebugController(
        ui=AppUiSurface(app),
        audit=lambda: app._audit,
        readonly=lambda: app.config.readonly,
        settings=lambda: DebugSettings(
            kube_context=app.config.kube_context,
            default_image=app.config.debug_default_image,
            images=app.config.debug_images,
        ),
        pod_uid_unchanged=inspect_controller.pod_uid_unchanged,
        get_epoch=context.epoch,
        epoch_crossed=context.crossed,
        confirm_screen=writes.confirm_screen,
        # Late-binding: the retry reruns through `ShellController`, which
        # keeps the write decorator and the tests patch per case.
        run_debug=lambda: shell_ref.get().run_debug,
    )
    debug_ref.bind(debug)

    # Drain execution (issue #97 U3d): the controller owns the approved
    # drain's cordon/evict/wait/audit lifecycle. Keybinding routing, the
    # press-again-to-cancel semantics and the approval dialog belong to
    # `ResourceWriteController` below, which owns the worker handle.
    drain = DrainController(
        notify=app.notify,
        audit_write=writes.audit_write,
        set_progress=functools.partial(app._set_progress, "drain"),
    )
    #: Resource and node write workflows (issue #187): delete, rollout
    #: restart, the editor round-trip, scale, in-place pod resize,
    #: cordon/uncordon and drain - plus the drain's worker/target state.
    #: It composes those flows out of `WriteCoordinator` and holds no
    #: mutation path around it; the app keeps only the Textual action
    #: handlers as thin delegates.
    resource_writes = ResourceWriteController(
        writes=writes,
        view=view,
        ui=AppUiSurface(app),
        drain=drain,
        write_ops=lambda: app._write_ops,
        get_manifest=lambda: app._get_manifest,
        edit_text=lambda: app._edit_text,
        managed_note=app._managed_note,
        managed_note_from=app._managed_note_from,
        pod_resize_supported=lambda: app._pod_resize_supported,
        helm_uninstall=lambda: helm_controller.uninstall_selected(),
        operators=operators,
    )
    # Workspace model (issue #48): the panes, focus, table-id counter and
    # the `ctrl+w` chord flag live in one pure owner; `current_kind` & co.
    # delegate to the focused pane so commands and keybindings target it.
    # `WorkspaceController` (constructed below) owns the transitions and
    # the workspace-only mutable state (nav lock, pre-warm leases, tree
    # rebuild context, jump-poll budget, render-coalescing set, metrics
    # target) that used to live directly on the app.
    workspace = WorkspaceState("pods", config.namespace or "default")
    # Hint-strip lifecycle (issue #97 U3b): the controller owns the event
    # cache and the parked-cursor refresh timer; widget access and worker
    # scheduling stay here, injected as narrow callables.
    hints = HintController(
        find_pod_summary=inspect_controller.find_pod_summary,
        cursor_row_key=inspect_surface.cursor_row_key,
        on_pods_view=lambda: app.current_kind == "pods",
        get_events=lambda: app._get_events,
        show_trouble=inspect_surface.show_trouble,
        clear_hint=inspect_surface.clear_hint,
        start_fetch=lambda coro: app.run_worker(coro, exclusive=True, group="hint-events"),
        set_timer=app.set_timer,
        ctx_epoch=context.epoch,
        ctx_crossed=context.crossed,
    )
    hints_ref.bind(hints)

    #: Log subsystem ownership (issue #187): the controller owns the stream
    #: tasks, display buffer, reconnect/error flags, selected triples, pane
    #: generation, pane mode and pane owner, plus the open/stream/display
    #: workflows. The app keeps only the Textual action/message entry points
    #: as thin delegates; widget access and app state arrive as callables.
    logs = LogController(
        ui=AppUiSurface(app),
        get_log_pane=lambda: app._log_pane,
        get_stream_logs=lambda: app._stream_logs,
        pod_containers=inspect_controller.pod_containers,
        selected_ns_name=view.selected_ns_name,
        visible_pod_keys=lambda: [str(row.key.value) for row in app._focused_table().ordered_rows],
        current_kind=lambda: app.current_kind,
        focused_pane=lambda: app._pane,
        ctx_epoch=context.epoch,
        ctx_switch_crossed=context.crossed,
        ctx_reads_allowed=context.reads_allowed,
        refresh_bindings=app.refresh_bindings,
        buffer_max_lines=config.log_buffer_lines,
    )
    logs_ref.bind(logs)

    #: Workspace orchestration (issue #187 / Deep Task 3): navigation,
    #: filter/sort transitions, the drill pre-warm/watch-release flow, the
    #: hierarchy tree and relationship-graph flows, and the split-pane
    #: lifecycle all live in the controller, together with the
    #: workspace-only mutable state (nav lock, pre-warm leases, tree
    #: rebuild context, jump-poll budget, render-coalescing set, metrics
    #: target). The app keeps compose/widget construction, the Textual
    #: action/message entry points as thin delegates, and the narrow
    #: widget surface the controller drives.
    workspace_controller = WorkspaceController(
        state=workspace,
        store=app.store,
        watch_manager=app.watch_manager,
        metrics=app._metrics,
        relationship_loader=relationship_loader,
        ui=AppUiSurface(app),
        surface=AppWorkspaceSurface(app),
        view=view,
        context=context,
        logs=logs,
        hints=hints,
        config=lambda: app.config,
        get_manifest=lambda: app._get_manifest,
        get_helm_components=lambda: app._get_helm_components,
        olm_alias_key=operators.alias_key,
        describe_named=inspect_controller.describe_named,
        check_permission=lambda: app._check_permission,
        list_namespaces=lambda: app._list_namespaces,
    )
    workspace_ref.bind(workspace_controller)

    #: External MCP write proposals (issue #110 / Deep Task 7): the
    #: controller owns the store, the submit/get/cancel intake, the
    #: provenance and terminal-outcome audit, the pending indicator, the
    #: one-at-a-time `:proposals` review with its own approval dialog,
    #: the claimed execution through `WriteCoordinator`, and the audited
    #: expiry sweeps `:ctx`, `:mcp` and unmount drive. Wired before the
    #: agent controller, which reaches it through `AgentProposals`; the
    #: write-op builder late-binds back to the agent controller, which
    #: owns that construction for the direct write path.
    proposals = ProposalController(
        store=proposal_store,
        ui=AppUiSurface(app),
        screens=AppProposalScreens(app),
        tasks=AppReviewTasks(app),
        events=AppProposalEvents(app),
        context=context,
        writes=writes,
        navigation=workspace_controller,
        builder=agent_ref.get,
        config=lambda: app.config,
        audit=lambda: app._audit,
        approval_timeout_seconds=approval_timeout_seconds,
        # Late-binding, like the other controllers' app callables: tests
        # patch `_refresh_status` after the app is constructed.
        refresh_status=lambda: app._refresh_status(),
    )
    proposals_ref.bind(proposals)

    #: The optional integrations (issue #187 / Deep Task 9): the `:mcp`
    #: on/off toggle with its proposal sweeps, the follow mirror flag,
    #: and the `:tp` status panel with its one-shot traffic-manager
    #: hint - together with all four pieces of state those keep. Wired
    #: after the proposal controller and the workspace, because a
    #: server-run transition sweeps the one and serializes on the
    #: other's `:ctx` navigation lock.
    integrations = IntegrationController(
        ui=AppUiSurface(app),
        context=context,
        proposals=proposals,
        serializer=workspace_controller,
        mcp=lambda: app._mcp,
        telepresence=telepresence,
        probe_traffic_manager=probe_traffic_manager,
        telepresence_enabled=lambda: app.config.telepresence_enabled,
        follow_enabled=config.mcp_follow,
        # Late-binding, like the other controllers' app callables: tests
        # patch `_refresh_status` after the app is constructed.
        refresh_status=lambda: app._refresh_status(),
    )
    #: The built-in agent's session and UI ownership (issue #187 / Deep
    #: Task 6): the runtime/settings/model-tier/follow state, the turn
    #: task with its interrupt-and-submit lifecycle, the screen context the
    #: model is told about, and every `UIBridge` read plus the direct,
    #: approval-gated agent write. It composes the same
    #: `WriteCoordinator` perimeter every other write path uses, and
    #: reaches proposals only through `ProposalController`'s
    #: `AgentProposals` port. The app keeps the Textual action/message
    #: entry points as thin delegates and the widget surfaces the
    #: controller drives.
    agent_ui = AgentUiController(
        panel=AppAgentPanel(app),
        screens=AppAgentScreens(app),
        ui=AppUiSurface(app),
        view=view,
        context=context,
        writes=writes,
        workspace=workspace,
        navigation=workspace_controller,
        logs=logs,
        proposals=proposals,
        dispatch=bridge_dispatch,
        config=lambda: app.config,
        get_manifest=lambda: app._get_manifest,
        get_events=lambda: app._get_events,
        stream_logs=lambda: app._stream_logs,
        pod_containers=inspect_controller.pod_containers,
        write_ops=lambda: app._write_ops,
        audit=lambda: app._audit,
        pod_resize_supported=lambda: app._pod_resize_supported,
        provider_hint=lambda: app._provider_hint,
        approval_timeout_seconds=approval_timeout_seconds,
        # Late-binding, like the other controllers' app callables: tests
        # patch `_refresh_status` after the app is constructed.
        refresh_status=lambda: app._refresh_status(),
        # Agent-follow mirrors route through the shared serialized bridge
        # (the composition root's tool-bridge proxy) so they serialize with
        # the agent's own UI tools and concurrent MCP UI calls - log-pane
        # swaps and describes must never interleave. None (tests, degraded
        # wiring) falls back to the controller's own adapter.
        follow_bridge=lambda: agent_follow_bridge,
        session=agent_session,
        model_name=agent_model_name,
        catalog=agent_catalog,
        save_profiles=agent_save_profiles,
        rebuild=rebuild_agent,
        disconnect=disconnect_agent,
        available=agent_available,
    )
    agent_ref.bind(agent_ui)

    #: Where an unresolved `:` command goes (issue #187 / Deep Task 9):
    #: one typed dispatch to the owner that implements it, so no feature
    #: flow stays reachable through the app itself. Wired last because it
    #: names every other owner.
    commands = CommandRouter(
        ui=AppUiSurface(app),
        agent=agent_ui,
        integrations=integrations,
        proposals=proposals,
        forwards=forward_controller,
        operators=operators,
    )

    return AppRuntime(
        view=view,
        relationship_loader=relationship_loader,
        context=context,
        timeline=timeline,
        writes=writes,
        bridge_dispatch=bridge_dispatch,
        inspect_surface=inspect_surface,
        inspect=inspect_controller,
        shell=shell,
        forward=forward_controller,
        transfer=transfer,
        operators=operators,
        helm=helm_controller,
        debug=debug,
        drain=drain,
        resource_writes=resource_writes,
        workspace=workspace,
        hints=hints,
        logs=logs,
        workspace_controller=workspace_controller,
        proposals=proposals,
        integrations=integrations,
        agent_ui=agent_ui,
        commands=commands,
    )
