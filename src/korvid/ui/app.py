"""KorvidApp — constructed with injected dependencies (composition in __main__)."""

from __future__ import annotations

import contextlib
import functools
import math
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
)
from time import monotonic
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    # Annotation-only: the base TUI must not import the embedded-agent
    # runtime at startup (issue #73) — the composition root injects it
    # only when the [agent] extra is installed and wired.
    from korvid.agent.session import AgentSession

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.events import DescendantBlur, DescendantFocus, Key
from textual.widgets import DataTable, Static
from textual.widgets.data_table import RowDoesNotExist
from textual.worker import Worker, WorkerState

from korvid.agent.model_profiles import ModelCatalog
from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig, ModelConnectionConfig, ModelConnectionsWriter
from korvid.core.filters import ResourceFilter
from korvid.core.keybindings import plan_keybindings, shift_alias_keys
from korvid.core.mcp import MCPControllerBase
from korvid.core.portforward import (
    ForwardRegistry,
)
from korvid.core.session_timeline import SessionTimeline
from korvid.core.sorting import SortSpec
from korvid.core.store import ALL_NAMESPACES, ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.components import (
    ComponentRef,
)
from korvid.k8s.discovery import PODS_META, ResourceMeta, canonical_resource_alias
from korvid.k8s.helm import (
    HELM_RELEASES_META,
    HELM_REVISIONS_META,
    HelmReleaseIdentity,
)
from korvid.k8s.helmcli import HelmCLI
from korvid.k8s.logs import LogLine
from korvid.k8s.metrics import MetricsPoller
from korvid.k8s.models import GenericSummary
from korvid.k8s.olm import (
    OPERATORS_GROUP,
    PACKAGES_GROUP,
)
from korvid.k8s.portforward import FORWARDABLE_KINDS
from korvid.k8s.relations import owned_by
from korvid.k8s.telepresence import TelepresenceCLI
from korvid.k8s.writes import WriteOps
from korvid.tools.executor import UIBridge
from korvid.tools.proposals import ProposalStore
from korvid.ui.agent_ui_controller import (
    AgentUiController,
)
from korvid.ui.app_bindings import APP_BINDINGS, APP_CSS, APP_HANDLER_KEY_HELP
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
from korvid.ui.command import command_help, command_words
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
from korvid.ui.messages import (
    AgentPromptSubmitted,
    BuiltinCommand,
    ClearFilter,
    ExternalProposalExpired,
    ExternalProposalsChanged,
    FilterCommand,
    NavigateCommand,
    QuitCommand,
    ResourcesUpdated,
    ShowContextPicker,
    ShowError,
    ShowNamespacePicker,
    SortCommand,
    SwitchContextCommand,
    TransferCancelRequested,
    UnknownCommand,
)
from korvid.ui.navigation import NavigationStack
from korvid.ui.operator_controller import OperatorController
from korvid.ui.proposal_controller import (
    ProposalController,
)
from korvid.ui.relationship_controller import RelationshipSnapshotLoader
from korvid.ui.resource_inspect_controller import ResourceInspectController
from korvid.ui.resource_write_controller import (
    RESTARTABLE,
    SCALABLE,
    ResourceWriteController,
)
from korvid.ui.session_timeline_controller import (
    TIMELINE_EVENT_GROUP,
    TIMELINE_NAVIGATION_GROUP,
    SessionTimelineController,
)
from korvid.ui.shell_controller import ShellController, ShellSettings
from korvid.ui.transfer import TransferController
from korvid.ui.widgets.agent_panel import AgentPanel
from korvid.ui.widgets.command_bar import CommandBar
from korvid.ui.widgets.describe_screen import DescribePane, DescribeScreen
from korvid.ui.widgets.filter_bar import FilterBar
from korvid.ui.widgets.help_screen import HelpScreen, collect_help
from korvid.ui.widgets.hint_strip import HintStrip
from korvid.ui.widgets.log_pane import LogPane
from korvid.ui.widgets.logo import SplashLogo
from korvid.ui.widgets.namespace_picker import NamespacePicker
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.widgets.resource_table import ResourceTable, validate_selected_view
from korvid.ui.widgets.status_bar import StatusBar
from korvid.ui.widgets.top_bar import KeyEntry, TopBar
from korvid.ui.workspace_controller import (
    RELATIONSHIP_GROUP,
    WorkspaceController,
)
from korvid.ui.workspace_state import PaneState, WorkspaceState, filtered_rows
from korvid.ui.write_coordinator import (
    WriteCoordinator,
)

_DEFAULT_ALIASES: dict[str, ResourceMeta] = {
    "pods": PODS_META,
    "po": PODS_META,
    "pod": PODS_META,
}

#: How often the app polls the forward registry for dead kubectl processes.
_FORWARD_POLL_SECONDS = 2.0


class KorvidApp(App[None]):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = APP_BINDINGS
    HANDLER_KEY_HELP: ClassVar[tuple[tuple[str, str, str, str], ...]] = APP_HANDLER_KEY_HELP
    DEFAULT_CSS = APP_CSS

    def __init__(
        self,
        config: KorvidConfig,
        store: ResourceStore,
        watch_manager: WatchManager,
        list_namespaces: Callable[[], Awaitable[list[str]]] | None = None,
        aliases: dict[str, ResourceMeta] | None = None,
        get_manifest: (Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None) = None,
        get_helm_components: (Callable[[str, str], Awaitable[list[ComponentRef]]] | None) = None,
        get_helm_release_identity: (
            Callable[[str, str], Awaitable[HelmReleaseIdentity | None]] | None
        ) = None,
        get_events: EventsFetcher | None = None,
        stream_logs: Callable[..., AsyncIterator[LogLine]] | None = None,
        agent_session: AgentSession | None = None,
        agent_model_name: str | None = None,
        #: Answers every question the profile screens ask; None without the
        #: [agent] extra, which degrades `:ai` to an install hint.
        agent_catalog: ModelCatalog | None = None,
        #: Writes `agent.active`/`agent.profiles` — and the first-run model
        #: tier that belongs with them — back to config.yaml.
        agent_save_profiles: ModelConnectionsWriter | None = None,
        rebuild_agent: (
            Callable[[ModelConnectionConfig, str | None], AgentSession | None] | None
        ) = None,
        disconnect_agent: Callable[[], None] | None = None,
        agent_available: bool = True,
        write_ops: WriteOps | None = None,
        audit: AuditLog | None = None,
        check_permission: Callable[[str, str, str, str | None, str, str], Awaitable[bool]]
        | None = None,
        mcp: MCPControllerBase | None = None,
        edit_text: Callable[[str], Awaitable[str | None]] | None = None,
        metrics: MetricsPoller | None = None,
        pod_resize_supported: bool = False,
        forwards: ForwardRegistry | None = None,
        provider_hint: str | None = None,
        protected_context: str | None = None,
        open_pod_exec: Callable[..., contextlib.AbstractAsyncContextManager[Any]] | None = None,
        list_contexts: Callable[[], tuple[list[str], str | None]] | None = None,
        probe_context: Callable[[str], Awaitable[None]] | None = None,
        switch_context: Callable[[str | None], Awaitable[ContextSwitchResult]] | None = None,
        helm: HelmCLI | None = None,
        proposal_store: ProposalStore | None = None,
        save_topbar: Callable[[bool], None] | None = None,
        telepresence: TelepresenceCLI | None = None,
        probe_traffic_manager: Callable[[], Awaitable[bool]] | None = None,
        agent_follow_bridge: UIBridge | None = None,
        list_relationship_objects: (
            Callable[[ResourceMeta, str | None], Awaitable[list[GenericSummary]]] | None
        ) = None,
        session_timeline: SessionTimeline | None = None,
        watch_warning_events: (Callable[[str | None], AsyncIterator[dict[str, Any]]] | None) = None,
        approval_timeout_seconds: float | None = None,
    ) -> None:
        super().__init__()
        if approval_timeout_seconds is not None and (
            not math.isfinite(approval_timeout_seconds) or approval_timeout_seconds <= 0
        ):
            raise ValueError("approval_timeout_seconds must be finite and positive")
        #: The session's one typed view surface (issue #187): every
        #: controller reads the focused pane through it, and the selection
        #: reads (`selected_ns_name`, `selected_uid`) live on it rather than
        #: on the app - they are widget/store reads about "what the user is
        #: looking at", which is exactly what this boundary names.
        self._view = AppViewState(self)
        self.config = config
        self._reported_view_warnings: set[str] = set()
        self.store = store
        self.watch_manager = watch_manager
        self._list_namespaces = list_namespaces
        self._get_manifest = get_manifest
        self._get_helm_components = get_helm_components
        self._get_helm_release_identity = get_helm_release_identity
        self._get_events = get_events
        self._stream_logs = stream_logs
        self._write_ops = write_ops
        self._audit = audit
        self._check_permission = check_permission
        self._mcp = mcp
        #: Operational relationship graph (issue #281): the loader is a
        #: pure orchestrator built once around the injected LIST callable;
        #: it performs no Textual operations, so the app owns the worker
        #: that runs it (see action_relationships). None disables `g`
        #: entirely (no cluster connection, or the composition root chose
        #: not to wire it).
        self._relationship_loader: RelationshipSnapshotLoader | None = (
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
        self._ctx = ContextSwitchCoordinator(
            ui=AppUiSurface(self),
            surface=AppContextSurface(self),
            view=self._view,
            session=AppSessionConfiguration(self),
            store=self.store,
            watches=self.watch_manager,
            workspace=lambda: self._workspace_ctl,
            logs=lambda: self._logs,
            hints=lambda: self._hints,
            timeline=lambda: self._timeline,
            proposals=lambda: self._proposals,
            forwards=lambda: self._forward,
            registry=lambda: self._forwards,
            writes=lambda: self._writes,
            agent=lambda: self._agent_ui,
            mcp=lambda: self._mcp,
            audit=lambda: self._audit,
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
        self._timeline = SessionTimelineController(
            ui=AppUiSurface(self),
            view=self._view,
            watch_manager=self.watch_manager,
            timeline=session_timeline,
            get_epoch=self._ctx.epoch,
            epoch_crossed=self._ctx.crossed,
            watch_warning_events=watch_warning_events,
            selected_resource=lambda: self._workspace_ctl.selected_timeline_resource(),
            navigate=lambda kind, namespace, name, epoch: self._workspace_ctl.jump_to_object(
                kind, namespace, name, epoch=epoch
            ),
        )
        #: The write security perimeter (issue #187): approval, epoch and
        #: identity revalidation, the synchronous in-flight write
        #: reservation `:ctx` consults, the fail-closed intent audit, the
        #: audited mutation, and every approval dialog - including the
        #: protected-context layer (issue #83) it owns the marker for. This
        #: *is* the `WriteGate` the controllers hold, so there is exactly
        #: one implementation of that ordering in the app.
        self._writes = WriteCoordinator(
            ui=AppUiSurface(self),
            view=self._view,
            context=self._ctx,
            audit=lambda: self._audit,
            timeline=self._timeline,
            check_permission=lambda: self._check_permission,
            relationship_loader=lambda: self._relationship_loader,
            focused_pane=lambda: self._pane,
            canonical_meta_kind=self._canonical_meta_kind,
            protected_context=protected_context,
        )
        #: Where foreign `UIBridge` calls run (issue #165): activated in
        #: on_mount with the app's own execution context and invalidated on
        #: unmount, so a pre-mount MCP request or one racing teardown is
        #: refused as 'UI not ready' instead of composing widgets in the
        #: caller's context or against an unmounted app.
        self._bridge_dispatch = AppContextDispatch()
        #: Top bar collapse/expand (issue #142): seeded from `ui.topbar`,
        #: toggled at runtime, persisted through the injected callback.
        self._topbar_expanded = config.ui_topbar_expanded
        self._save_topbar = save_topbar
        self._edit_text = edit_text
        self._metrics = metrics
        self._forwards = forwards
        #: Read-only resource inspection (issue #187 / Deep Task 9): describe
        #: (selected and named) with its Secret masking rule and provider
        #: footer, the container pick behind Enter, the hint-details overlay,
        #: the store lookups those share, and the pod-identity guard the
        #: interactive flows bind an approved action to. The shell and log
        #: collaborators are late-bound because they are constructed below.
        self._inspect_surface = AppInspectSurface(self)
        self._inspect = ResourceInspectController(
            ui=AppUiSurface(self),
            view=self._view,
            context=self._ctx,
            surface=self._inspect_surface,
            shell=lambda: self._shell,
            logs=lambda: self._logs,
            get_manifest=lambda: self._get_manifest,
            get_events=lambda: self._get_events,
            stream_logs=lambda: self._stream_logs,
            target_uid=lambda kind, ns, name: self._target_uid(kind, ns, name),
            audit=lambda: self._audit,
            provider_hint=lambda: self._provider_hint,
        )
        #: interactive sessions (issue #187): pod exec, the kubectl debug
        #: fallback, and the approval-gated node shell. run_worker ownership
        #: and the write perimeter stay here.
        self._shell = ShellController(
            gate=self._writes,
            view=self._view,
            ui=AppUiSurface(self),
            debug=lambda: self._debug,
            audit=lambda: self._audit,
            get_manifest=lambda: self._get_manifest,
            pod_containers=self._inspect.pod_containers,
            node_target=lambda action: self._node_target(action),
            target_uid=lambda kind, ns, name: self._target_uid(kind, ns, name),
            settings=lambda: ShellSettings(
                kube_context=self.config.kube_context,
                debug_default_image=self.config.debug_default_image,
                debug_images=self.config.debug_images,
                node_shell_image=self.config.node_shell_image,
                node_shell_namespace=self.config.node_shell_namespace,
            ),
        )
        #: port-forward session lifecycle (issue #187): launch, reattach,
        #: liveness polling and the off-pump audit queue. The controller owns
        #: that state - nothing else reads it.
        self._forward = ForwardController(
            gate=self._writes,
            ui=AppUiSurface(self),
            view=self._view,
            forwards=lambda: self._forwards,
            audit=lambda: self._audit,
            get_manifest=lambda: self._get_manifest,
        )
        #: pods/resize subresource discovered on the connected cluster
        #: (1.35 GA); gates the R keybinding and the resize agent tool.
        self._pod_resize_supported = pod_resize_supported
        #: status-bar progress labels keyed by owner (drain, helm preview):
        #: concurrent operations must not clear each other's feedback.
        self._progress_labels: dict[str, str] = {}
        #: monotonic token for `_progress()` scopes: an exclusive-worker
        #: replacement can publish before its cancelled predecessor's
        #: cleanup runs, which must then not clear the replacement's label.
        self._progress_seq = 0
        #: detected cloud provider short name ("aks", "aws", ...) or None;
        #: drives the Service/Ingress describe footer (issue #30).
        self._provider_hint = provider_hint
        #: KubeClient.open_pod_exec, bound at composition; None means no
        #: cluster connection, so file transfer (issue #47) is unavailable.
        self._open_pod_exec = open_pod_exec
        #: detected helm binary wrapper (issue #31), or None when helm is
        #: not on PATH - the install/upgrade/rollback keys then explain
        #: their absence instead of doing nothing.
        self._helm = helm
        #: the ctrl+t transfer journey (issue #91 U3a / Deep Task 9): the
        #: controller owns the selection guards, the container pick, the
        #: dialog with its read-only remote listing, the upload approval it
        #: composes out of `WriteCoordinator`, the stream task and the
        #: in-flight serialization. The app keeps run_worker ownership and
        #: the Textual entry points as thin delegates.
        self._transfer = TransferController(
            ui=AppUiSurface(self),
            view=self._view,
            writes=self._writes,
            screens=AppTransferScreens(self),
            open_pod_exec=lambda: self._open_pod_exec,
            audit=lambda: self._audit,
            find_pod=self._inspect.find_pod,
            target_uid=lambda kind, ns, name: self._target_uid(kind, ns, name),
            pod_uid_unchanged=self._inspect.pod_uid_unchanged,
        )
        #: OLM workflows (issue #187): the wizard, InstallPlan approval and
        #: the CSV-aware uninstall. The install dialog re-checks the
        #: subscription UID in its own callback, so it drives the gate's
        #: permitted/run directly rather than the standard confirm flow.
        self._olm = OperatorController(
            gate=self._writes,
            view=self._view,
            ui=AppUiSurface(self),
            write_ops=lambda: self._write_ops,
            get_manifest=lambda: self._get_manifest,
            confirm_screen=self._writes.confirm_screen,
            uid_intact_after_fetch=self._writes.uid_intact_after_fetch,
            precheck_keybinding_write=self._writes.precheck_keybinding_write,
            write_target=self._writes.write_target,
        )
        #: helm write workflows (issue #187): the controller owns the wizard,
        #: preview and command construction; the approval gate, context
        #: revalidation and audited execution stay here, so the write
        #: perimeter keeps a single implementation.
        self._helm_ctl = HelmController(
            helm=lambda: self._helm,
            get_release_identity=lambda: self._get_helm_release_identity,
            gate=self._writes,
            view=self._view,
            ui=AppUiSurface(self),
            # Late-binding for the same reason as everywhere else: the
            # workspace controller is constructed after this one.
            navigation=lambda: self._workspace_ctl,
            # Late-binding, like the other controllers' app callables: the
            # editor entry points are patched per test, so binding the bound
            # method at construction would freeze whatever existed then.
            edit_in_external_editor=lambda *a, **k: self._edit_in_external_editor(*a, **k),
            edit_text=lambda: self._edit_text,
        )
        # Debug-fallback execution (issue #97 U3c / Deep Task 10): the
        # controller owns the gated, audited kubectl debug run *and* the
        # image-pull retry offer. The initial image picker, the RBAC
        # pre-check and the first approval stay with `ShellController`.
        self._debug = DebugController(
            ui=AppUiSurface(self),
            audit=lambda: self._audit,
            readonly=lambda: self.config.readonly,
            settings=lambda: DebugSettings(
                kube_context=self.config.kube_context,
                default_image=self.config.debug_default_image,
                images=self.config.debug_images,
            ),
            pod_uid_unchanged=self._inspect.pod_uid_unchanged,
            get_epoch=self._ctx.epoch,
            epoch_crossed=self._ctx.crossed,
            confirm_screen=self._writes.confirm_screen,
            # Late-binding: the retry reruns through `ShellController`, which
            # keeps the write decorator and the tests patch per case.
            run_debug=lambda: self._shell.run_debug,
        )
        # Drain execution (issue #97 U3d): the controller owns the approved
        # drain's cordon/evict/wait/audit lifecycle. Keybinding routing, the
        # press-again-to-cancel semantics and the approval dialog belong to
        # `ResourceWriteController` below, which owns the worker handle.
        self._drain = DrainController(
            notify=self.notify,
            audit_write=self._writes.audit_write,
            set_progress=functools.partial(self._set_progress, "drain"),
        )
        #: Resource and node write workflows (issue #187): delete, rollout
        #: restart, the editor round-trip, scale, in-place pod resize,
        #: cordon/uncordon and drain - plus the drain's worker/target state.
        #: It composes those flows out of `WriteCoordinator` and holds no
        #: mutation path around it; the app keeps only the Textual action
        #: handlers as thin delegates.
        self._resource_writes = ResourceWriteController(
            writes=self._writes,
            view=self._view,
            ui=AppUiSurface(self),
            drain=self._drain,
            write_ops=lambda: self._write_ops,
            get_manifest=lambda: self._get_manifest,
            edit_text=lambda: self._edit_text,
            managed_note=self._managed_note,
            managed_note_from=self._managed_note_from,
            pod_resize_supported=lambda: self._pod_resize_supported,
            helm_uninstall=lambda: self._helm_ctl.uninstall_selected(),
            operators=self._olm,
        )
        self.aliases: dict[str, ResourceMeta] = (
            aliases if aliases is not None else dict(_DEFAULT_ALIASES)
        )
        # Workspace model (issue #48): the panes, focus, table-id counter and
        # the `ctrl+w` chord flag live in one pure owner; `current_kind` & co.
        # delegate to the focused pane so commands and keybindings target it.
        # `WorkspaceController` (constructed below) owns the transitions and
        # the workspace-only mutable state (nav lock, pre-warm leases, tree
        # rebuild context, jump-poll budget, render-coalescing set, metrics
        # target) that used to live directly on the app.
        self._workspace = WorkspaceState("pods", config.namespace or "default")
        #: Validated `keybindings:` overrides (action → key), applied via the
        #: keymap in on_mount; the help overlay renders these keys (issue #35).
        self._keybinding_overrides: dict[str, str] = {}
        #: Active sort per view kind (issue #37): the choice survives watch
        #: updates (every render re-applies it) and switching views restores
        #: each kind's own sort. Lives in PaneState (see `_sorts` property).
        self._splash_shown_at: float = monotonic()
        # Hint-strip lifecycle (issue #97 U3b): the controller owns the event
        # cache and the parked-cursor refresh timer; widget access and worker
        # scheduling stay here, injected as narrow callables.
        self._hints = HintController(
            find_pod_summary=self._inspect.find_pod_summary,
            cursor_row_key=self._inspect_surface.cursor_row_key,
            on_pods_view=lambda: self.current_kind == "pods",
            get_events=lambda: self._get_events,
            show_trouble=self._inspect_surface.show_trouble,
            clear_hint=self._inspect_surface.clear_hint,
            start_fetch=lambda coro: self.run_worker(coro, exclusive=True, group="hint-events"),
            set_timer=self.set_timer,
            ctx_epoch=self._ctx.epoch,
            ctx_crossed=self._ctx.crossed,
        )
        #: Log subsystem ownership (issue #187): the controller owns the stream
        #: tasks, display buffer, reconnect/error flags, selected triples, pane
        #: generation, pane mode and pane owner, plus the open/stream/display
        #: workflows. The app keeps only the Textual action/message entry points
        #: as thin delegates; widget access and app state arrive as callables.
        self._logs = LogController(
            ui=AppUiSurface(self),
            get_log_pane=lambda: self._log_pane,
            get_stream_logs=lambda: self._stream_logs,
            pod_containers=self._inspect.pod_containers,
            selected_ns_name=self._view.selected_ns_name,
            visible_pod_keys=lambda: [
                str(row.key.value) for row in self._focused_table().ordered_rows
            ],
            current_kind=lambda: self.current_kind,
            focused_pane=lambda: self._pane,
            ctx_epoch=self._ctx.epoch,
            ctx_switch_crossed=self._ctx.crossed,
            ctx_reads_allowed=self._ctx.reads_allowed,
            refresh_bindings=self.refresh_bindings,
            buffer_max_lines=config.log_buffer_lines,
        )
        #: Workspace orchestration (issue #187 / Deep Task 3): navigation,
        #: filter/sort transitions, the drill pre-warm/watch-release flow, the
        #: hierarchy tree and relationship-graph flows, and the split-pane
        #: lifecycle all live in the controller, together with the
        #: workspace-only mutable state (nav lock, pre-warm leases, tree
        #: rebuild context, jump-poll budget, render-coalescing set, metrics
        #: target). The app keeps compose/widget construction, the Textual
        #: action/message entry points as thin delegates, and the narrow
        #: widget surface the controller drives.
        self._workspace_ctl = WorkspaceController(
            state=self._workspace,
            store=self.store,
            watch_manager=self.watch_manager,
            metrics=self._metrics,
            relationship_loader=self._relationship_loader,
            ui=AppUiSurface(self),
            surface=AppWorkspaceSurface(self),
            view=self._view,
            context=self._ctx,
            logs=self._logs,
            hints=self._hints,
            config=lambda: self.config,
            get_manifest=lambda: self._get_manifest,
            get_helm_components=lambda: self._get_helm_components,
            olm_alias_key=self._olm.alias_key,
            describe_named=self._inspect.describe_named,
            check_permission=lambda: self._check_permission,
            list_namespaces=lambda: self._list_namespaces,
        )
        #: External MCP write proposals (issue #110 / Deep Task 7): the
        #: controller owns the store, the submit/get/cancel intake, the
        #: provenance and terminal-outcome audit, the pending indicator, the
        #: one-at-a-time `:proposals` review with its own approval dialog,
        #: the claimed execution through `WriteCoordinator`, and the audited
        #: expiry sweeps `:ctx`, `:mcp` and unmount drive. Wired before the
        #: agent controller, which reaches it through `AgentProposals`; the
        #: write-op builder late-binds back to the agent controller, which
        #: owns that construction for the direct write path.
        self._proposals = ProposalController(
            store=proposal_store,
            ui=AppUiSurface(self),
            screens=AppProposalScreens(self),
            tasks=AppReviewTasks(self),
            events=AppProposalEvents(self),
            context=self._ctx,
            writes=self._writes,
            navigation=self._workspace_ctl,
            builder=lambda: self._agent_ui,
            config=lambda: self.config,
            audit=lambda: self._audit,
            approval_timeout_seconds=approval_timeout_seconds,
            # Late-binding, like the other controllers' app callables: tests
            # patch `_refresh_status` after the app is constructed.
            refresh_status=lambda: self._refresh_status(),
        )
        #: The optional integrations (issue #187 / Deep Task 9): the `:mcp`
        #: on/off toggle with its proposal sweeps, the follow mirror flag,
        #: and the `:tp` status panel with its one-shot traffic-manager
        #: hint - together with all four pieces of state those keep. Wired
        #: after the proposal controller and the workspace, because a
        #: server-run transition sweeps the one and serializes on the
        #: other's `:ctx` navigation lock.
        self._integrations = IntegrationController(
            ui=AppUiSurface(self),
            context=self._ctx,
            proposals=self._proposals,
            serializer=self._workspace_ctl,
            mcp=lambda: self._mcp,
            telepresence=telepresence,
            probe_traffic_manager=probe_traffic_manager,
            telepresence_enabled=lambda: self.config.telepresence_enabled,
            follow_enabled=config.mcp_follow,
            # Late-binding, like the other controllers' app callables: tests
            # patch `_refresh_status` after the app is constructed.
            refresh_status=lambda: self._refresh_status(),
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
        self._agent_ui = AgentUiController(
            panel=AppAgentPanel(self),
            screens=AppAgentScreens(self),
            ui=AppUiSurface(self),
            view=self._view,
            context=self._ctx,
            writes=self._writes,
            workspace=self._workspace,
            navigation=self._workspace_ctl,
            logs=self._logs,
            proposals=self._proposals,
            dispatch=self._bridge_dispatch,
            config=lambda: self.config,
            get_manifest=lambda: self._get_manifest,
            get_events=lambda: self._get_events,
            stream_logs=lambda: self._stream_logs,
            pod_containers=self._inspect.pod_containers,
            write_ops=lambda: self._write_ops,
            audit=lambda: self._audit,
            pod_resize_supported=lambda: self._pod_resize_supported,
            provider_hint=lambda: self._provider_hint,
            approval_timeout_seconds=approval_timeout_seconds,
            # Late-binding, like the other controllers' app callables: tests
            # patch `_refresh_status` after the app is constructed.
            refresh_status=lambda: self._refresh_status(),
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
        #: Where an unresolved `:` command goes (issue #187 / Deep Task 9):
        #: one typed dispatch to the owner that implements it, so no feature
        #: flow stays reachable through the app itself. Wired last because it
        #: names every other owner.
        self._commands = CommandRouter(
            ui=AppUiSurface(self),
            agent=self._agent_ui,
            integrations=self._integrations,
            proposals=self._proposals,
            forwards=self._forward,
            operators=self._olm,
        )

    # -- Focused-pane delegation (issue #48): `WorkspaceState` owns the pane
    # list and the focused-view state; these properties keep the whole action
    # surface (and tests) working against "the view the user is focused on".

    @property
    def _pane(self) -> PaneState:
        return self._workspace.focused

    @property
    def current_kind(self) -> str:
        return self._workspace.current_kind

    @current_kind.setter
    def current_kind(self, value: str) -> None:
        self._workspace.current_kind = value
        # The footer legend is view-scoped (issue #114): Textual cannot see
        # internal kind switches, so prompt it to re-evaluate check_action.
        self.refresh_bindings()

    @property
    def current_scope(self) -> str:
        return self._workspace.current_scope

    @current_scope.setter
    def current_scope(self, value: str) -> None:
        self._workspace.current_scope = value

    @property
    def filter_pattern(self) -> str:
        return self._workspace.filter_pattern

    @filter_pattern.setter
    def filter_pattern(self, value: str) -> None:
        self._workspace.filter_pattern = value

    @property
    def _resource_filter(self) -> ResourceFilter:
        """Parsed form of filter_pattern (issue #44); single matcher shared
        by the table render and the agent's view of "what the user sees"."""
        return self._workspace.resource_filter

    @_resource_filter.setter
    def _resource_filter(self, value: ResourceFilter) -> None:
        self._workspace.resource_filter = value

    @property
    def _sorts(self) -> dict[str, SortSpec]:
        """Per-kind sort state of the focused pane (view state, issue #37)."""
        return self._workspace.sorts

    @property
    def _drill(self) -> NavigationStack:
        """Drill-down levels (deploy -> rs -> pods) of the focused pane."""
        return self._workspace.drill

    @property
    def agent_session(self) -> AgentSession | None:
        """The live session — the :ai wizard may have replaced the initial
        one, so per-cluster retargeting (issue #36) must read it here."""
        return self._agent_ui.session

    @property
    def agent_ui(self) -> AgentUiController:
        """The agent's UI controller.

        Exposed so the composition root can bind the session's workspace
        port to the live controller after construction — the app owns the
        controller, but the port has to point somewhere real.
        """
        return self._agent_ui

    def _focused_table(self) -> ResourceTable:
        return self.query_one(f"#{self._pane.table_id}", ResourceTable)

    # -- Typed root-widget accessors (issue #91 U2): these widgets are
    # composed once and stay mounted for the app's lifetime, so the hot
    # call sites read them through one named property each instead of
    # repeating raw `query_one(Class)` lookups. The accessors raise
    # `NoMatches` exactly like a raw query, so the intentional
    # startup/shutdown guards around them keep working unchanged.

    @property
    def _log_pane(self) -> LogPane:
        return self.query_one(LogPane)

    @property
    def _describe_pane(self) -> DescribePane:
        return self.query_one(DescribePane)

    @property
    def _command_bar(self) -> CommandBar:
        return self.query_one(CommandBar)

    @property
    def _filter_bar(self) -> FilterBar:
        return self.query_one(FilterBar)

    @property
    def _namespace_picker(self) -> NamespacePicker:
        return self.query_one(NamespacePicker)

    @property
    def _hint_strip(self) -> HintStrip:
        return self.query_one(HintStrip)

    @property
    def _status_bar(self) -> StatusBar:
        return self.query_one(StatusBar)

    @property
    def _agent_panel(self) -> AgentPanel:
        """Composed only when the agent is available; raises `NoMatches`
        otherwise, matching the guarded call sites' expectations."""
        return self.query_one(AgentPanel)

    def compose(self) -> ComposeResult:
        # The grouped top bar (issue #142) replaces the stock Footer at the
        # top: the key legend lives where users look first, grouped and
        # collapsible instead of one flat run of uniform keys.
        yield TopBar()
        yield SplashLogo()
        # The workspace hosts one or two side-by-side panes (issue #48);
        # pane 1 is composed here, pane 2 mounts on `ctrl+w v`.
        table = ResourceTable(id="pane-0")
        table.display = False  # hidden behind the splash until first data
        workspace = Horizontal(table, id="workspace")
        workspace.display = False
        yield workspace
        empty_state = Static(id="empty-state")
        empty_state.display = False  # hidden until the first store notification
        yield empty_state
        yield LogPane()
        yield DescribePane()
        if self._agent_ui.available:
            agent_panel = AgentPanel()
            agent_panel.display = False
            yield agent_panel
        yield CommandBar()
        yield FilterBar()
        yield NamespacePicker()
        yield HintStrip()
        yield StatusBar()

    @classmethod
    def _binding_actions(cls) -> dict[str, tuple[str, ...]]:
        """Every remappable app action mapped to its default keys.

        Bindings without a keymap ``id`` (the parametrised 1-9 favorites)
        are excluded: Textual's keymap moves bindings by id, so an
        id-less binding cannot actually be remapped.
        """
        actions: dict[str, tuple[str, ...]] = {}
        for raw in cls.BINDINGS:
            binding = raw if isinstance(raw, Binding) else Binding(*raw)
            if binding.id is None:
                continue
            actions[binding.action] = (*actions.get(binding.action, ()), binding.key)
        return actions

    def _apply_keybindings(self) -> None:
        """Apply the `keybindings:` config overrides via the keymap (issue #35).

        Only app bindings carry keymap ids, so the approval dialogs'
        confirm keys are structurally out of reach; `plan_keybindings`
        additionally rejects their action names and blocks priority
        actions from taking the dialogs' keys. Shifted-letter overrides
        expand to both spellings (`shift+g,G`) because real terminals
        deliver Shift+<letter> as the uppercase character.
        """
        bindings = [raw if isinstance(raw, Binding) else Binding(*raw) for raw in self.BINDINGS]
        priority_actions = {binding.action for binding in bindings if binding.priority}
        # Id-less bindings (the 1-9 favorites) are not remappable, but their
        # keys stay reserved so an override cannot silently shadow them.
        reserved_keys = {binding.key: binding.action for binding in bindings if binding.id is None}
        plan = plan_keybindings(
            self.config.keybindings,
            self._binding_actions(),
            priority_actions,
            reserved_keys=reserved_keys,
        )
        self._keybinding_overrides = plan.overrides
        keymap: dict[str, str] = {}
        for binding in bindings:
            if binding.id is not None and binding.action in plan.overrides:
                keymap[binding.id] = shift_alias_keys(plan.overrides[binding.action])
        if keymap:
            self.set_keymap(keymap)
        for warning in plan.warnings:
            self.notify(warning, title="Keybindings", severity="warning")

    async def on_mount(self) -> None:
        # Snapshot the app-owned execution context (issue #165): on_mount
        # runs inside Textual's message pump, so the snapshot the dispatcher
        # takes here carries `active_app` (and the pump ContextVars). Every
        # foreign bridge call - MCP requests, follow mirrors - is marshaled
        # onto a copy of it, because composing a widget tree outside it
        # raises NoActiveAppError and terminates the app.
        self._bridge_dispatch.activate()
        # AUTO_FOCUS skips the hidden #workspace container: the table must
        # take initial focus explicitly or keys land on the CommandBar.
        self.query_one("#pane-0", ResourceTable).focus()
        # The top bar re-renders whenever the active bindings change (view
        # navigation, log pane open/close) - the same signal the stock
        # Footer subscribed to, so check_action stays the single source of
        # which keys are visible (issue #142).
        self.screen.bindings_updated_signal.subscribe(self, self._on_bindings_updated)
        self._refresh_top_bar()
        self._apply_keybindings()
        # Wire the `known` closure into CommandBar so parse_command can resolve aliases.
        command_bar = self._command_bar
        command_bar.known = lambda a: self._canonical_kind(a) if a in self.aliases else None
        command_bar.command_words = command_words(self.aliases)
        # Seed session-scoped log display settings from config (logs.wrap /
        # logs.timestamps); the w/t keys toggle them from there.
        log_pane = self._log_pane
        log_pane.wrap_lines = self.config.log_wrap
        log_pane.show_timestamps = self.config.log_timestamps
        if self._forwards is not None:
            # Liveness is the point of tracked forwards (issue #38): a toast
            # must fire when one breaks even while :pf is closed.
            self.set_interval(_FORWARD_POLL_SECONDS, self._forward.poll)
        self._workspace_ctl.start_namespace_prefetch()
        # Kubeconfig contexts feed the `:ctx` completion; the coordinator owns
        # that prefetch task and reaps it on unmount.
        self._ctx.start()
        for warning in self.config.warnings:
            # Config problems (e.g. an invalid custom column) surface once at
            # startup instead of hiding in a log file (issue #45).
            self.notify(warning, title="Config warning", severity="warning")

        self._proposals.subscribe()

        # Both callbacks fire from watch tasks on the same loop; post_message is
        # loop-safe. Watch tasks are cancelled in on_unmount before shutdown to
        # avoid posting to a closing app.
        def _on_store_update(kind: str) -> None:
            # The initial LIST seeds objects one apply_event at a time in a
            # single event-loop slice; posting one message per object would
            # rebuild the whole table N times. The controller coalesces to at
            # most one render request per kind until it is consumed —
            # _render_table reads the current store state, so a single deferred
            # rebuild covers every event.
            if self._workspace_ctl.mark_render_pending(kind):
                self.post_message(ResourcesUpdated(kind))

        def _on_watch_error(detail: str) -> None:
            self.post_message(ShowError("Watch failed", detail))

        self.store.subscribe(_on_store_update)
        self.watch_manager.on_error = _on_watch_error
        self._timeline.start()
        if self._metrics is not None:
            # Metrics updates reuse the pods render path; the pending guard in
            # _on_store_update coalesces them with watch events.
            self._metrics.on_update = lambda: _on_store_update("pods")
        await self._workspace_ctl.sync_metrics_poller()
        self._splash_shown_at = monotonic()
        await self.watch_manager.start(self.current_kind, self.current_scope)
        self._refresh_status()
        # Safety net: never leave the splash up if the watch produces nothing
        # (e.g. connection failure) — swap to the table after a short grace.
        self.set_timer(5.0, self._dismiss_splash)
        # Telepresence install hint (issue #159): fire-and-forget probe; a
        # failed or slow GET never delays startup.
        self.run_worker(self._integrations.maybe_hint_telepresence(), exclusive=False)

    #: Minimum time the startup splash stays visible in a real terminal.
    #: Skipped in headless (test) mode so Pilot tests see the table at once.
    SPLASH_MIN_SECONDS = 1.2

    def _dismiss_splash(self) -> None:
        try:
            splash = self.query_one(SplashLogo)
            workspace = self.query_one("#workspace")
            table = self.query_one("#pane-0", ResourceTable)
        except NoMatches:
            return  # app is shutting down; a queued render must not crash
        if not splash.display:
            return
        if not self.is_headless:
            remaining = self._splash_shown_at + self.SPLASH_MIN_SECONDS - monotonic()
            if remaining > 0:
                self.set_timer(remaining, self._dismiss_splash)
                return
        splash.display = False
        workspace.display = True
        table.display = True

    def on_aliases_updated(self) -> None:
        """Refresh command autocompletion after background resource discovery."""
        try:
            command_bar = self._command_bar
        except Exception:
            return  # app is shutting down or not composed yet
        command_bar.command_words = command_words(self.aliases)
        # A kind discovered late can turn display-only tree nodes navigable.
        self._workspace_ctl.refresh_hierarchy()

    def on_resources_updated(self, message: ResourcesUpdated) -> None:
        self._workspace_ctl.on_resources_updated(message.kind)

    def _render_table(self, kind: str, *, only: PaneState | None = None) -> None:
        """Single choke point: every pane showing `kind` re-renders, and the
        empty-state stays in step (single-pane only - a split has its own
        per-pane content as guidance).

        `only` restricts the render to the initiating pane: view-state
        changes (filter, navigation) must not repaint the other pane -
        `show()` clears and rebuilds, resetting its cursor/scroll. Store
        and metrics updates fan out to every pane (data really changed).
        """
        # First store notification: replace the startup splash with real content.
        self._dismiss_splash()
        for pane in self._workspace.panes:
            if pane.kind != kind or (only is not None and pane is not only):
                continue
            try:
                table = self.query_one(f"#{pane.table_id}", ResourceTable)
            except NoMatches:
                return  # shutdown race: a queued render after widgets are removed
            self._render_pane(kind, pane, table, empty_state=self._workspace.pane_count == 1)

    def _render_pane(
        self, kind: str, pane: PaneState, table: ResourceTable, *, empty_state: bool
    ) -> None:
        rows = self.store.get(kind, pane.scope)
        drill_uid = pane.drill.parent_uid
        if drill_uid is not None and kind == pane.drill.child_kind:
            rows = [r for r in rows if owned_by(r, drill_uid)]
        rows = filtered_rows(rows, pane.resource_filter)
        all_namespaces = pane.scope == ALL_NAMESPACES
        metrics = None
        if kind == "pods" and self._metrics is not None and self._metrics.available:
            metrics = self._metrics.get
        # Dispatch rendering on the resolved meta: a group-qualified view
        # kind (alias collision) must still get its typed table, and the
        # serving group scopes group-specific renderings (the OLM tables).
        meta = self.aliases.get(kind)
        plural = meta.plural if meta is not None else kind
        group = meta.group if meta is not None else ""
        synthetic = meta.synthetic if meta is not None else False
        configured_view = (
            meta.configured_value(self.config.views)
            if meta is not None
            else self.config.views.get(kind)
        )
        selected_view, warnings = validate_selected_view(
            plural,
            group=group,
            synthetic=synthetic,
            view=configured_view,
        )
        for warning in warnings:
            if warning in self._reported_view_warnings:
                continue
            self._reported_view_warnings.add(warning)
            self.notify(warning, title="Config warning", severity="warning")
        table.show(
            plural,
            rows,
            all_namespaces=all_namespaces,
            # Filtering happened upstream (issue #44: labels/regex/fuzzy need
            # the full summaries, not just names) — no name pattern remains.
            pattern="",
            metrics=metrics,
            group=group,
            synthetic=synthetic,
            sort=pane.sorts.get(kind),
            view=selected_view,
        )
        if empty_state:
            self._refresh_empty_state(kind, table.row_count)
        # The strip is driven by RowHighlighted on the pods view; anything
        # else (view switch, table now empty) must not leave a stale hint.
        if pane is self._pane and (kind != "pods" or table.row_count == 0):
            with contextlib.suppress(NoMatches):  # shutdown race, same as the table guard
                self._hint_strip.clear_hint()

    def on_show_error(self, message: ShowError) -> None:
        self.notify(message.detail, title=message.title, severity="error")

    def action_help(self) -> None:
        """Open the help overlay generated from the live binding lists (issue #41)."""
        overrides = self._keybinding_overrides
        handler_keys = [
            (group, overrides.get(action, key) if action else key, description)
            for group, key, description, action in self.HANDLER_KEY_HELP
        ]
        # The static BINDINGS list bypasses check_action, so drop entries
        # whose action is unavailable in this composition (e.g. Ctrl-A
        # without the [agent] extra, issue #73). View-gated actions stay:
        # the overlay documents every view, not just the current one.
        app_bindings = [
            binding
            for binding in (
                raw if isinstance(raw, Binding) else Binding(*raw) for raw in self.BINDINGS
            )
            if self._action_available(binding.action)
        ]
        groups = collect_help(
            app_bindings,
            list(DescribeScreen.BINDINGS),
            handler_keys=handler_keys,
            overrides=overrides,
        )
        self.push_screen(
            HelpScreen(groups, command_help(telepresence=self._integrations.telepresence_available))
        )

    def action_open_command(self) -> None:
        # Dismiss the filter bar first so no invisible filter stays active.
        self._filter_bar.dismiss_bar()
        self._command_bar.open()

    def action_open_filter(self) -> None:
        # When the describe pane is open, / searches inside it (issue #42).
        describe_pane = self._describe_pane
        if describe_pane.display:
            describe_pane.open_search()
            return
        # When the log pane is open, / opens the pane's inline search instead.
        log_pane = self._log_pane
        if log_pane.display:
            log_pane.open_search()
            return
        # Dismiss the command bar first to enforce mutual exclusion.
        self._command_bar.dismiss_bar()
        self._filter_bar.open()

    def on_filter_command(self, message: FilterCommand) -> None:
        self._workspace_ctl.set_filter(message.pattern)

    def on_clear_filter(self, message: ClearFilter) -> None:
        self._workspace_ctl.clear_filter()

    async def on_navigate_command(self, message: NavigateCommand) -> None:
        await self._workspace_ctl.navigate_command(message.view, message.namespace)

    async def action_toggle_all_namespaces(self) -> None:
        """Toggle scope between ALL_NAMESPACES and the config-default namespace."""
        await self._workspace_ctl.toggle_all_namespaces()

    async def action_favorite_namespace(self, index: int) -> None:
        """Jump to `favorite_namespaces[index-1]` (issue #108, keys 1-9)."""
        await self._workspace_ctl.favorite_namespace(index)

    async def on_show_namespace_picker(self, message: ShowNamespacePicker) -> None:
        """The listing, its permission mapping, the `:ctx` staleness guards
        and the picker open belong to `WorkspaceController`."""
        await self._workspace_ctl.show_namespace_picker()

    # ------------------------------------------------------------------
    # `:ctx` — runtime context switching (issue #36)
    #
    # Both handlers are thin delegates: `ContextSwitchCoordinator` owns the
    # switch epoch, the in-flight claim and the whole quiesce/retarget/resume
    # transaction (see ui/context_switch_coordinator.py).
    # ------------------------------------------------------------------

    def on_show_context_picker(self, message: ShowContextPicker) -> None:
        self._ctx.show_picker()

    def on_switch_context_command(self, message: SwitchContextCommand) -> None:
        self._ctx.switch(message.name)

    def on_quit_command(self, message: QuitCommand) -> None:
        self.exit()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Cursor movement drives the ops hint strip (pods view only)."""
        if not isinstance(event.data_table, ResourceTable):
            return
        if event.data_table.id != self._pane.table_id:
            # Highlight from the non-focused pane (e.g. a watch-driven
            # re-render moving its cursor): the hint strip reflects the
            # focused pane's selection only.
            return
        if self.current_kind != "pods" or event.row_key is None:
            self._inspect_surface.clear_hint()
            return
        self._hints.show_for_row(str(event.row_key.value))

    def action_hint_details(self) -> None:
        """`h` — open the read-only detail overlay for the hinted pod row.

        Textual resolves `action_*` on the app, so the binding entry point
        stays here; the overlay flow belongs to `ResourceInspectController`.
        """
        self._inspect.hint_details()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter drills down: pods -> containers; kinds with a
        registered ownership child (deploy -> rs -> pods) push a drill level."""
        if not isinstance(event.data_table, ResourceTable):
            return
        if self.current_kind != "pods":
            # The hierarchy-open/drill-chain guard and the no-op-vs-consume
            # decision both live on the controller (issue #120/#157): this
            # handler stays a thin delegate over the row key.
            if await self._workspace_ctl.handle_non_pods_row_selected(str(event.row_key.value)):
                event.stop()
            return
        event.stop()
        row_key = str(event.row_key.value)
        parts = row_key.split("/", 1)
        if len(parts) != 2:
            return
        await self._inspect.open_containers(parts[0], parts[1])

    def action_shell(self) -> None:
        """`s` - exec into the selected pod, or open a node shell.

        Textual resolves `action_*` on the app, so the binding entry point
        stays here; the flow itself belongs to `ShellController`.
        """
        self._shell.shell()

    def _canonical_kind(self, kind: str) -> str:
        meta = self.aliases.get(kind)
        if meta is None:
            return kind
        return self._canonical_meta_kind(meta)

    def _canonical_meta_kind(self, meta: ResourceMeta) -> str:
        return canonical_resource_alias(self.aliases, meta)

    def _focus_row(self, row_key: str) -> bool:
        """Move the focused table's cursor to *row_key*; False when absent."""
        table = self._focused_table()
        try:
            index = table.get_row_index(row_key)
        except RowDoesNotExist:
            return False
        table.move_cursor(row=index)
        return True

    def action_relationships(self) -> None:
        """Load and show the operational relationship graph for the selected
        row (issue #281). The controller owns the load/open/goto flow."""
        self._workspace_ctl.show_relationships()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Report a failed timeline or relationship worker instead of exiting.

        Scoped to this app's own `relationships`, `timeline-warning-events`,
        and `timeline` (goto) workers, which are the only ones started with
        `exit_on_error=False`: every other group keeps Textual's default
        crash-on-error behaviour, and a cancelled worker never reaches
        `WorkerState.ERROR`, so it is never reported.
        """
        if event.worker.node is not self or event.state is not WorkerState.ERROR:
            return
        if event.worker.group == TIMELINE_EVENT_GROUP:
            self._notify_worker_error("Warning-event timeline feed failed", event.worker)
        elif event.worker.group == RELATIONSHIP_GROUP:
            self._notify_worker_error("Relationships failed", event.worker)
        elif event.worker.group == TIMELINE_NAVIGATION_GROUP:
            self._notify_worker_error("Timeline navigation failed", event.worker)

    def _notify_worker_error(self, label: str, worker: Worker[Any]) -> None:
        """One visible report for a worker that failed instead of crashing."""
        error = worker.error
        detail = f"{type(error).__name__}: {error}" if error is not None else "unknown error"
        # markup=False: the detail can quote cluster-controlled text (a
        # resource name inside a parser error), which must never be read
        # as Rich markup.
        self.notify(
            f"{label} - {detail[:200]}",
            severity="error",
            timeout=10,
            markup=False,
        )

    def action_timeline(self) -> None:
        """Open the read-only session timeline (issue #282 Task 4).

        Unlike `action_relationships`, opening this performs no I/O -
        `SessionTimeline.snapshot()` is a bounded in-memory read - so `T`
        opens the modal even with nothing selected on the active pane;
        only the post-Enter goto is async and epoch-guarded, reusing the
        exact same `_jump_to_object` path as every other navigation. The
        controller owns the rest of the flow (issue #282 Task 3)."""
        self._timeline.open()

    async def action_describe(self) -> None:
        """`d` — describe the currently highlighted row.

        Textual resolves `action_*` on the app, so the binding entry point
        stays here; the flow belongs to `ResourceInspectController`.
        """
        await self._inspect.describe_selected()

    def on_builtin_command(self, message: BuiltinCommand) -> None:
        """Dispatch the catalog's typed operation to its feature owner."""
        self._commands.route_builtin(message)

    def on_unknown_command(self, message: UnknownCommand) -> None:
        """Report commands the catalog and resource discovery cannot resolve."""
        self._commands.route_unknown(message)

    @property
    def integrations(self) -> IntegrationController:
        """The optional-integration owner (`:mcp`, `:tp`).

        Public because the composition root wires the MCP server's follow
        hooks straight to it: follow state and the activity note belong to
        the controller that owns them, not to a pair of app forwarders.
        """
        return self._integrations

    async def action_port_forward(self) -> None:
        """Open the port-forward dialog for the selected pod or service (shift+f).

        Textual resolves `action_*` on the app, so the binding entry point
        stays here; the flow itself belongs to `ForwardController`.
        """
        await self._forward.open_dialog()

    # -- File transfer (issue #47): download/upload over the exec API as a
    # -- tar stream; uploads are approval-gated, both directions audited
    # -- fail-closed.

    def action_transfer(self) -> None:
        """Open the ctrl+t transfer dialog for the selected pod.

        Textual resolves `action_*` on the app, so the binding entry point
        stays here; the journey itself belongs to `TransferController`.
        """
        self._transfer.start()

    def on_transfer_cancel_requested(self, message: TransferCancelRequested) -> None:
        message.stop()
        self._transfer.cancel()

    async def on_key(self, event: Key) -> None:
        """Pane chords (`ctrl+w` v/w/q) and Escape (closes describe/log
        panes, then pops one drill-down level)."""
        if self._workspace_ctl.chord_pending or event.key == "ctrl+w":
            await self._workspace_ctl.handle_pane_chord(event)
            return
        if event.key != "escape":
            return
        if len(self.screen_stack) > 1:
            # A modal owns this Escape (its close binding handles it):
            # neither a drill pop nor a hierarchy return may piggyback on
            # the keystroke that merely dismissed Help or a dialog.
            return
        filter_bar = self._filter_bar
        command_bar = self._command_bar
        namespace_picker = self._namespace_picker
        if filter_bar.display or command_bar.display or namespace_picker.display:
            return  # bars and pickers own Escape while open
        describe_pane = self._describe_pane
        if describe_pane.display:
            # An active pane search (input open or submitted hits) consumes
            # Escape first; a second Escape closes the pane itself.
            if not describe_pane.dismiss_search():
                describe_pane.hide()
            event.stop()
            return
        log_pane = self._log_pane
        if log_pane.display:
            await self._logs.close()
            event.stop()
            return
        popped = await self._workspace_ctl.pop_drill()
        if popped:
            event.stop()
            return
        # No drill level left: a pending hierarchy return (issue #135)
        # reopens the component tree the last goto jumped away from.
        if await self._workspace_ctl.reopen_hierarchy_return():
            event.stop()
            # Without this the same Escape continues into binding
            # processing and hits the freshly pushed tree's own
            # escape=close binding, dismissing it on arrival.
            event.prevent_default()

    # -- 2-pane split workspace (issue #48) ---------------------------------

    def _update_pane_focus_classes(self) -> None:
        """Mark the command-routing target with `focused-pane`. A class, not
        `:focus`: opening the command/filter bar or agent panel moves keyboard
        focus to an Input, but the focused pane still decides where the command
        goes - the indicator must not vanish at that moment."""
        for index, pane in enumerate(self._workspace.panes):
            try:
                table = self.query_one(f"#{pane.table_id}", ResourceTable)
            except NoMatches:
                continue
            table.set_class(
                self._workspace.is_split and index == self._workspace.focused_index,
                "focused-pane",
            )
        # Every focused-pane change funnels through here; the panes may show
        # different kinds, so the view-scoped footer legend must follow the
        # focus (issue #114).
        self.refresh_bindings()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        """Clicking a pane focuses it - command routing must follow. Any
        focus change also disarms a pending `ctrl+w` chord: the second key
        would go to the newly focused widget, leaving the flag set to
        swallow a later table keypress."""
        widget = event.widget
        table_id = widget.id if isinstance(widget, ResourceTable) else None
        self._workspace_ctl.on_descendant_focus(table_id)

    def on_descendant_blur(self, event: DescendantBlur) -> None:
        """Overlay widgets (command/filter bars, describe/log panes) hide
        themselves while focused. The tables now live inside #workspace, so
        Textual's sibling-fallback in `_reset_focus` finds nothing focusable
        and focus drops to None - restore it to the focused pane's table."""
        del event
        self.call_later(self._workspace_ctl.restore_table_focus)

    async def action_logs(self) -> None:
        """Open logs for the selected pod, or toggle it in/out of the pane (``l``)."""
        await self._logs.action_logs()

    async def action_logs_multi(self) -> None:
        """Stream all filtered pods' containers (``L`` binding); cap at 8."""
        await self._logs.action_logs_multi()

    # -- Write operations (issue #16): every path goes through a ConfirmScreen
    # -- confirmed only by a user keystroke; executed writes are audited.

    #: Workload eligibility, owned by `ResourceWriteController` (the flows
    #: that enforce it) and re-exported here because `_ACTION_VIEWS` and the
    #: agent write ops gate the same identities. Keyed on (group, plural): a
    #: custom-group CRD whose plural collides with a built-in (e.g.
    #: 'deployments') must never be treated as an apps/* workload.
    _RESTARTABLE: ClassVar[frozenset[tuple[str, str]]] = RESTARTABLE
    _SCALABLE: ClassVar[frozenset[tuple[str, str]]] = SCALABLE

    async def action_delete_resource(self) -> None:
        """Ctrl-D: delete the selected resource (issue #16)."""
        await self._resource_writes.delete()

    async def action_rollout_restart(self) -> None:
        """r: rolling restart of the selected deployment/statefulset/daemonset."""
        await self._resource_writes.rollout_restart()

    async def action_edit_resource(self) -> None:
        """e: open the selected resource's manifest in $EDITOR and PUT the
        edited version back (kubectl edit parity)."""
        await self._resource_writes.edit()

    async def action_scale_resource(self) -> None:
        """S: scale the selected deployment/replicaset/statefulset."""
        await self._resource_writes.scale()

    async def action_resize_pod(self) -> None:
        """R: in-place resize of the selected pod (pods/resize, 1.35 GA)."""
        await self._resource_writes.resize_pod()

    async def _edit_in_external_editor(self, text: str) -> str | None:
        """Suspend the TUI and open $VISUAL/$EDITOR on *text*.

        The helm chart-values editor shares the resource-write controller's
        implementation rather than carrying a second one.
        """
        return await self._resource_writes.edit_in_external_editor(text)

    def _node_target(self, action: str) -> tuple[WriteOps, ResourceMeta, str, str | None] | None:
        """The selected node for a node op; `ShellController` shares it."""
        return self._resource_writes.node_target(action)

    async def action_cordon_node(self) -> None:
        """c: mark the selected node unschedulable (kubectl cordon parity)."""
        await self._resource_writes.cordon()

    async def action_uncordon_node(self) -> None:
        """u: mark the selected node schedulable again (kubectl uncordon)."""
        await self._resource_writes.uncordon()

    # -- Helm writes (issue #31 / #117): `HelmController` owns the view
    # -- guard, the target capture, the previews and the audited mutations;
    # -- these are the Textual entry points Textual resolves on the app.

    def action_helm_install(self) -> None:
        """i on the helm browser: start the chart install wizard."""
        self._helm_ctl.install()

    def action_helm_upgrade(self) -> None:
        """u on the helm browser: upgrade the selected release."""
        self._helm_ctl.upgrade()

    async def action_helm_history(self) -> None:
        """h on the helm release browser: the flat revision drill-down."""
        await self._helm_ctl.history()

    def action_helm_rollback(self) -> None:
        """r on the helm revision drill-down: roll the release back."""
        self._helm_ctl.rollback_selected()

    async def action_drain_node(self) -> None:
        """shift+d: drain the selected node behind a typed-name approval
        (issue #40). Pressing the key again on it cancels the running drain.
        """
        await self._resource_writes.drain_node()

    def _set_progress(self, owner: str, label: str) -> None:
        """Publish transient progress on the status bar, scoped to its
        owner (drain, helm preview): overlapping operations must never
        overwrite or clear each other's label. A failure to render must
        never interrupt the operation itself."""
        if label:
            self._progress_labels[owner] = label
        else:
            self._progress_labels.pop(owner, None)
        with contextlib.suppress(Exception):
            self._refresh_status()

    @contextlib.contextmanager
    def _progress(self, label: str) -> Iterator[None]:
        """Status-bar progress scoped exactly to the wrapped await: shown on
        entry, cleared on exit however the operation ends. Each scope gets a
        unique owner token so a cancelled predecessor's late cleanup cannot
        clear the label its exclusive-worker replacement published."""
        self._progress_seq += 1
        owner = f"helm:{self._progress_seq}"
        self._set_progress(owner, label)
        try:
            yield
        finally:
            self._set_progress(owner, "")

    async def action_operator_install(self) -> None:
        """I: install a catalog package or approve a pending InstallPlan.

        Textual resolves `action_*` on the app; the routing, the refusals
        and both flows belong to `OperatorController`.
        """
        await self._olm.install_selected()

    def _on_bindings_updated(self, _screen: object) -> None:
        self._refresh_top_bar()

    def _legend_entries(self) -> list[KeyEntry]:
        """The visible bindings as top-bar entries: pre-filtered by
        Textual's binding machinery (check_action / _ACTION_VIEWS - the
        single visibility source), deduplicated across --alt spellings and
        parametrised favorites."""
        entries: list[KeyEntry] = []
        seen: set[str] = set()
        for active in self.screen.active_bindings.values():
            binding = active.binding
            base = (binding.id or binding.action).removesuffix("--alt")
            action = binding.action.partition("(")[0]
            if action == "favorite_namespace" or base in seen:
                continue
            seen.add(base)
            entries.append(
                KeyEntry(
                    key=self.get_key_display(binding),
                    action=action,
                    description=binding.description,
                )
            )
        return entries

    def _topbar_toggle_key(self) -> str:
        """The effective toggle key's display form: resolved by action from
        the active bindings so a `toggle_topbar` remap moves the advertised
        hint with it (`active_bindings` is keyed by declared key names like
        "tilde", never by the display form)."""
        for active in self.screen.active_bindings.values():
            if active.binding.action == "toggle_topbar":
                return self.get_key_display(active.binding)
        return "~"

    def _topbar_can_drill(self) -> bool:
        """True when Enter drills on the current view (mirrors
        on_data_table_row_selected); the controller owns the decision."""
        return self._workspace_ctl.can_drill()

    def _refresh_top_bar(self) -> None:
        """Re-render the grouped legend for the current view (issue #142)."""
        bars = self.query(TopBar)
        if not bars:
            return
        bars.first(TopBar).update_legend(
            self.current_kind,
            self._legend_entries(),
            expanded=self._topbar_expanded,
            toggle_key=self._topbar_toggle_key(),
            can_drill=self._topbar_can_drill(),
        )

    def action_toggle_topbar(self) -> None:
        """`~` (issue #142): collapse/expand the grouped key legend; the
        choice persists to config through the injected save callback."""
        self._topbar_expanded = not self._topbar_expanded
        self._refresh_top_bar()
        if self._save_topbar is None:
            return
        try:
            self._save_topbar(self._topbar_expanded)
        except Exception as exc:  # in-memory toggle stays; disk is stale
            self.notify(
                f"Top bar toggled, but save failed: {exc} — the previous state returns on restart",
                severity="warning",
            )

    def _refresh_status(self) -> None:
        # The top bar shows the view name: keep it in step with every
        # status refresh (navigation always lands here).
        self._refresh_top_bar()
        # Availability comes from the actual runtime, not the config flag —
        # the provider factory refuses an unusable profile (no endpoint for
        # keyless auth, a reference it cannot resolve) and returns None while
        # a profile is still active in config.
        label = "AI on" if self._agent_ui.session is not None else "AI off"
        if self._agent_ui.session is not None and self._agent_ui.blocked_in_protected():
            label = "AI blocked"
        mcp_label = self._mcp.status() if self._mcp is not None else ""
        follow = self._integrations.follow_enabled
        if mcp_label and self._mcp is not None and self._mcp.running and follow:
            mcp_label += " ·follow"
        try:
            self._status_bar.update_status(
                self.config.kube_context,
                self.current_scope,
                label,
                breadcrumb=self._drill.breadcrumb(),
                mcp_label=mcp_label,
                filter_label=self._resource_filter.describe(),
                progress_label=" · ".join(
                    label for label in self._progress_labels.values() if label
                ),
                proposals_label=self._proposals.status_label(),
                protected=self._writes.protected_context is not None,
            )
        except NoMatches:
            return  # StatusBar unmounted during teardown

    def on_external_proposals_changed(self, message: ExternalProposalsChanged) -> None:
        self._proposals.handle_changed()

    async def on_external_proposal_expired(self, message: ExternalProposalExpired) -> None:
        await self._proposals.handle_expired(message.proposal, message.reason)

    # ------------------------------------------------------------------
    # Task-10 actions: JSON toggle, previous logs, search navigation
    # ------------------------------------------------------------------

    async def action_log_format(self) -> None:
        """Toggle JSON/raw formatting and re-render the buffer (``f`` key)."""
        await self._logs.action_log_format()

    async def action_log_wrap(self) -> None:
        """Toggle line wrapping and re-render the buffer (``w`` key)."""
        await self._logs.action_log_wrap()

    async def action_log_timestamps(self) -> None:
        """Toggle the timestamp prefix and re-render the buffer (``t`` key)."""
        await self._logs.action_log_timestamps()

    def action_log_save(self) -> None:
        """Save the current log buffer to a generated file (``ctrl+s``)."""
        self._logs.action_log_save()

    async def action_log_previous(self) -> None:
        """Re-open the same streams in previous-container-log mode (``p`` key)."""
        await self._logs.action_log_previous()

    def action_log_search_next(self) -> None:
        """Advance to the next search hit (``n`` key)."""
        describe_pane = self._describe_pane
        if describe_pane.display:
            describe_pane.search_next()
            return
        self._logs.search_next()

    def action_log_search_prev(self) -> None:
        """Previous search hit in an open pane; sort by name otherwise (``N``)."""
        describe_pane = self._describe_pane
        if describe_pane.display:
            describe_pane.search_prev()
            return
        if self._logs.search_prev():
            return
        self._workspace_ctl.sort_by("name")

    # ------------------------------------------------------------------
    # Column sorting (issue #37) — data-model sort keys, per-kind state.
    # ------------------------------------------------------------------

    def action_sort_by_age(self) -> None:
        self._workspace_ctl.sort_by("age")

    def action_sort_by_cpu(self) -> None:
        self._workspace_ctl.sort_by("cpu")

    def action_sort_by_mem(self) -> None:
        self._workspace_ctl.sort_by("mem")

    def on_sort_command(self, message: SortCommand) -> None:
        """`:sort <column>` (issue #45): builtin or custom column; bare `:sort` clears."""
        self._workspace_ctl.sort_command(message.column)

    def action_sort_picker(self) -> None:
        """`o` (issue #138): pick the sort column from a list instead of
        typing its exact name; re-picking the active column flips the
        direction, exactly like `:sort`."""
        options = self._workspace_ctl.sort_picker_options()
        if options is None:
            return  # never stack over another dialog
        title, columns, pane = options
        kind = pane.kind

        def _picked(choice: str | None) -> None:
            if choice is not None:
                self._workspace_ctl.apply_sort_choice(choice, pane, kind)

        self.push_screen(PickScreen(title, list(columns)), _picked)

    async def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """A header click sorts by that column (issue #138); clicking the
        active column flips the direction, same as the keys. The sort lands
        in the pane that owns the clicked table - not the focused one, so
        the split workspace never sorts the wrong pane."""
        if not isinstance(event.data_table, ResourceTable):
            return
        event.stop()
        self._workspace_ctl.header_sort(event.data_table.id, str(event.label))

    # ------------------------------------------------------------------
    # Agent panel (Ctrl-A) — wiring only; rendering lives in AgentPanel,
    # loop logic in the agent session.
    # ------------------------------------------------------------------

    #: Resource identities — (group, plural), see `_RESTARTABLE` — where each
    #: view-specific action applies; actions absent from the map work on
    #: every view. `check_action` consults this so the footer legend shows
    #: only the current view's keys and overloaded keys (i/u/r) dispatch to
    #: the binding whose view is on screen (issue #114). Identity, not the
    #: kind string: a foreign CRD claiming a bare plural (e.g.
    #: `packagemanifests`) must not surface another view's actions.
    #: `log_search_next`/`log_search_prev` stay unlisted on purpose: they
    #: also serve the describe pane's search (any view) and the
    #: sort-by-name fallback. Fail-closed by design: a kind missing from
    #: `aliases` (e.g. mid-discovery) hides every listed action until its
    #: identity is known.
    _ACTION_VIEWS: ClassVar[dict[str, frozenset[tuple[str, str]]]] = {
        "shell": frozenset({("", "pods"), ("", "nodes")}),
        "logs": frozenset({("", "pods")}),
        "logs_multi": frozenset({("", "pods")}),
        "hint_details": frozenset({("", "pods")}),
        "resize_pod": frozenset({("", "pods")}),
        "transfer": frozenset({("", "pods")}),
        # Core-group identities of FORWARDABLE_KINDS (pods, services).
        "port_forward": frozenset(("", plural) for plural in FORWARDABLE_KINDS),
        "cordon_node": frozenset({("", "nodes")}),
        "uncordon_node": frozenset({("", "nodes")}),
        "drain_node": frozenset({("", "nodes")}),
        "rollout_restart": _RESTARTABLE,
        "scale_resource": _SCALABLE,
        "operator_install": frozenset(
            {(PACKAGES_GROUP, "packagemanifests"), (OPERATORS_GROUP, "installplans")}
        ),
        # The synthetic helm views (group "", client-side plurals).
        "helm_install": frozenset({(HELM_RELEASES_META.group, HELM_RELEASES_META.plural)}),
        "helm_upgrade": frozenset({(HELM_RELEASES_META.group, HELM_RELEASES_META.plural)}),
        "helm_history": frozenset({(HELM_RELEASES_META.group, HELM_RELEASES_META.plural)}),
        "helm_rollback": frozenset({(HELM_REVISIONS_META.group, HELM_REVISIONS_META.plural)}),
    }

    #: Actions that operate on the visible log pane, not the focused view:
    #: the split workflow tails logs from one pane while the other shows a
    #: different kind, so these gate on pane visibility (review of #114).
    _LOG_PANE_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"log_format", "log_wrap", "log_timestamps", "log_save", "log_previous"}
    )

    #: Generic write actions that `WriteCoordinator.write_target` rejects on synthetic
    #: (client-side, read-only) views such as the helm browser: advertising
    #: them there would be a lie (review of #114). The dedicated helm write
    #: actions stay available through `_ACTION_VIEWS`.
    _SYNTHETIC_GATED_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"delete_resource", "edit_resource"}
    )

    def _action_available(self, action: str) -> bool:
        """Composition availability, independent of the current view: the
        help overlay filters on this alone so off-view keys stay documented
        (issues #73, #114)."""
        return not (action == "toggle_agent" and not self._agent_ui.available)

    def _log_pane_open(self) -> bool:
        """Whether a log pane is currently visible (pre-compose: no)."""
        try:
            return bool(self._log_pane.display)
        except NoMatches:
            return False

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Gate bindings on composition availability and the current view.

        Returning False both hides the binding from the footer and skips it
        during key dispatch, so overloaded keys fall through to the binding
        whose view is on screen (issue #114).
        """
        if not self._action_available(action):
            return False
        if action in self._LOG_PANE_ACTIONS:
            return self._log_pane_open()
        if action in self._SYNTHETIC_GATED_ACTIONS:
            meta = self.aliases.get(self._canonical_kind(self.current_kind))
            if (
                action == "delete_resource"
                and meta is not None
                and (meta.group, meta.plural)
                == (HELM_RELEASES_META.group, HELM_RELEASES_META.plural)
            ):
                # Ctrl+D on the release browser is `helm uninstall`
                # (issue #117) - the one synthetic view where delete works.
                return True
            # Unknown kinds keep the keys: the handler's own guards decide.
            return meta is None or not meta.synthetic
        views = self._ACTION_VIEWS.get(action)
        if views is None:
            return True
        meta = self.aliases.get(self._canonical_kind(self.current_kind))
        return meta is not None and (meta.group, meta.plural) in views

    def action_toggle_agent(self) -> None:
        """Toggle the agent chat panel (Ctrl-A)."""
        self._agent_ui.toggle_panel()

    def on_agent_prompt_submitted(self, message: AgentPromptSubmitted) -> None:
        """A prompt submitted in the chat input starts (or replaces) a turn."""
        self._agent_ui.submit_prompt(message.text)

    def action_interrupt_agent(self) -> None:
        """Stop the running agent turn (Ctrl-X, issue #170)."""
        self._agent_ui.interrupt()

    async def _target_uid(self, kind_alias: str, ns: str | None, name: str) -> str | None:
        """Uid of a write target at request time, for the flows that are not
        the agent's own: the interactive shell, the transfer pre-checks and
        the proposal execution path all bind their approval to one exact
        object incarnation through the same lookup."""
        return await self._agent_ui.target_uid(kind_alias, ns, name)

    async def _managed_note(self, kind_alias: str, ns: str | None, name: str) -> str | None:
        """Ownership banner for a write target (issue #119) —
        `ResourceWriteController` shares the agent path's lookup."""
        return await self._agent_ui.managed_note(kind_alias, ns, name)

    async def _managed_note_from(self, manifest: dict[str, Any], ns: str | None) -> str | None:
        """Ownership banner for an already-fetched manifest (issue #119) —
        `ResourceWriteController` shares the agent path's lookup."""
        return await self._agent_ui.managed_note_from(manifest, ns)

    def _refresh_empty_state(self, kind: str, visible_rows: int) -> None:
        """Show guidance instead of a silent blank table (empty ns or no filter match)."""
        empty = self.query_one("#empty-state", Static)
        if visible_rows > 0:
            empty.display = False
            return
        if self.filter_pattern:
            message = f"No {kind} matching '{self.filter_pattern}' — Esc to clear the filter"
        else:
            message = f"No {kind} in namespace '{self.current_scope}' — :ns <name> to switch"
        # Text keeps user-entered filter text literal (never Rich markup).
        empty.update(Text(message))
        empty.display = True

    async def on_unmount(self) -> None:
        # Mark the agent session down before the *first* await below: a turn
        # an interrupt-and-submit left cancelling can settle inside any of
        # these teardown awaits, and its drain callback would then start the
        # queued replacement against a screen stack being torn down.
        self._agent_ui.begin_shutdown()
        # Refuse new foreign UI work and reap in-flight bridge dispatches
        # (issue #165): the MCP server stays live until after run_async()
        # returns, so a request racing teardown could otherwise spawn work
        # (log streams) after the unmount sweeps and leave it alive against
        # an unmounted app.
        await self._bridge_dispatch.shutdown()
        # Cancel any active log stream tasks before the event loop shuts down.
        # A proposal must never outlive the session that previewed it: the
        # controller closes the store first so an in-flight submission cannot
        # land after its final audited sweep.
        await self._proposals.shutdown()
        # The `:ns` completion prefetch belongs to the workspace controller;
        # this cancels and reaps it, exactly as the `:ctx` teardown does.
        await self._workspace_ctl.cancel_namespace_prefetch()
        # The `:ctx` completion prefetch belongs to the switch coordinator;
        # this is the narrow lifecycle call that cancels and reaps it.
        await self._ctx.shutdown()
        await self._agent_ui.shutdown()
        await self._logs.shutdown()
        if self._metrics is not None:
            await self._metrics.stop()
        if self._forwards is not None:
            await self._forward.teardown(self._forwards)
        # Flush pending forward audits (e.g. a Ctrl-D pressed right before
        # quit) so no queued entry is lost.
        await self._forward.flush_audits()
        await self.watch_manager.stop_all()
