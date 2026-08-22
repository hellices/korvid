"""KorvidApp — constructed with injected dependencies (composition in __main__)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import shlex
import shutil
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Literal

if TYPE_CHECKING:
    # Annotation-only: the base TUI must not import the embedded-agent
    # runtime at startup (issue #73) — the composition root injects it
    # only when the [agent] extra is installed and wired.
    from korvid.agent.runtime import AgentRuntime

from rich.text import Text
from textual.app import App, ComposeResult, ScreenStackError
from textual.await_complete import AwaitComplete
from textual.binding import Binding
from textual.containers import Horizontal
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.events import DescendantBlur, DescendantFocus, Key
from textual.screen import Screen
from textual.widget import AwaitMount
from textual.widgets import DataTable, Static
from textual.widgets.data_table import CellDoesNotExist, RowDoesNotExist
from textual.worker import Worker, WorkerError, WorkerState

from korvid.agent.events import AgentEvent
from korvid.agent.install_hint import isolated_install_hint
from korvid.agent.setup import AgentConfigurator, AgentSettings
from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.debugimage import (
    FALLBACK_IMAGE,
    same_image_ref,
)
from korvid.core.errors import explain_api_error
from korvid.core.filters import ResourceFilter
from korvid.core.keybindings import plan_keybindings, shift_alias_keys
from korvid.core.mcp import MCPControllerBase
from korvid.core.portforward import (
    ForwardRegistry,
)
from korvid.core.relationships import SummaryLike
from korvid.core.session_timeline import SessionTimeline, TimelineResourceRef
from korvid.core.sorting import SortSpec
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.transfer import RemoteEntry, TransferError, TransferSpec, list_remote_dir
from korvid.core.watch import WatchManager
from korvid.k8s.components import (
    ComponentRef,
)
from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.helm import (
    HELM_RELEASES_META,
    HELM_REVISIONS_META,
)
from korvid.k8s.helmcli import HelmCLI
from korvid.k8s.logs import LogLine
from korvid.k8s.metrics import MetricsPoller
from korvid.k8s.models import ContainerTrouble, GenericSummary, PodSummary
from korvid.k8s.olm import (
    OPERATORS_GROUP,
    PACKAGES_GROUP,
)
from korvid.k8s.portforward import FORWARDABLE_KINDS
from korvid.k8s.relations import owned_by
from korvid.k8s.telepresence import TelepresenceCLI, TelepresenceError
from korvid.k8s.writes import WriteOps, restart_stamp
from korvid.tools.executor import UIBridge
from korvid.tools.proposals import (
    ProposalClosedError,
    ProposalLimitError,
    ProposalState,
    ProposalStore,
    ProposalTooLargeError,
    WriteProposal,
)
from korvid.ui.agent_ui_controller import (
    AgentPanelPort,
    AgentProposals,
    AgentScreens,
    AgentUIBridge,
    AgentUiController,
)
from korvid.ui.bridge_dispatch import AppContextDispatch
from korvid.ui.command import command_help
from korvid.ui.debug import DebugController
from korvid.ui.drain import DrainController
from korvid.ui.forward_controller import ForwardController
from korvid.ui.helm_controller import HelmController
from korvid.ui.hints import EventsFetcher, HintController, pod_needs_hint
from korvid.ui.log_controller import LogController
from korvid.ui.messages import (
    AgentPromptSubmitted,
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
from korvid.ui.relationship_controller import RelationshipSnapshotLoader
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
from korvid.ui.transfer import TransferController, TransferProgress
from korvid.ui.ui_surface import ScreenResultT, Severity, UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.agent_panel import AgentPanel
from korvid.ui.widgets.command_bar import CommandBar
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ImagePrompt, ReplicasPrompt
from korvid.ui.widgets.containers_screen import ContainersScreen, build_container_rows
from korvid.ui.widgets.describe_screen import DescribePane, DescribeScreen, provider_footer_note
from korvid.ui.widgets.filter_bar import FilterBar
from korvid.ui.widgets.helm_install import HelmInstallPrompt
from korvid.ui.widgets.help_screen import HelpScreen, collect_help
from korvid.ui.widgets.hierarchy_screen import HierarchyScreen
from korvid.ui.widgets.hint_detail import HintDetailScreen
from korvid.ui.widgets.hint_strip import HintStrip
from korvid.ui.widgets.log_pane import LogPane
from korvid.ui.widgets.logo import SplashLogo
from korvid.ui.widgets.namespace_picker import NamespacePicker
from korvid.ui.widgets.operator_install import OperatorInstallPrompt
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.widgets.port_forward_screen import PortForwardScreen
from korvid.ui.widgets.resize_prompt import ResizePrompt
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.secret_screen import SecretScreen
from korvid.ui.widgets.status_bar import StatusBar
from korvid.ui.widgets.telepresence_screen import TelepresenceScreen
from korvid.ui.widgets.top_bar import KeyEntry, TopBar
from korvid.ui.widgets.transfer_screen import TransferProgressScreen, TransferScreen
from korvid.ui.workspace_controller import (
    RELATIONSHIP_GROUP,
    ContextGuard,
    WorkspaceController,
    WorkspaceSurface,
)
from korvid.ui.workspace_state import PaneState, WorkspaceState
from korvid.ui.write_coordinator import (
    WriteCoordinator,
    canonical_meta_kind,
    gvr_label,
    write_locus,
)

_DEFAULT_ALIASES: dict[str, ResourceMeta] = {
    "pods": PODS_META,
    "po": PODS_META,
    "pod": PODS_META,
}

logger = logging.getLogger(__name__)

#: How often the app polls the forward registry for dead kubectl processes.
_FORWARD_POLL_SECONDS = 2.0

#: Seconds a proposal-review dialog stays open before it counts as a
#: dismissal - an unanswered dialog must never wedge the review loop. The
#: agent's own approval budget lives with the flow that owns it
#: (`agent_ui_controller.APPROVAL_TIMEOUT`).
_APPROVAL_TIMEOUT = 120.0
#: Upper bound on the all-namespaces LIST SubjectAccessReview probe: a
#: stalled authorization endpoint must never hang the `0` keypress. Writes
#: have their own budget - the pre-check they use lives with the perimeter
#: that owns it (`write_coordinator._PERMISSION_CHECK_TIMEOUT`).
_PERMISSION_CHECK_TIMEOUT = 10.0


#: Upper bound on a helm preview (issue #31): `helm ... --dry-run` shells out
#: and may pull the chart from a repo, so it gets more budget than an API
#: server dry-run - still bounded, the approval dialog is never wedged.
_HELM_PREVIEW_TIMEOUT = 20.0

#: A rendered chart can run to thousands of lines; the approval dialog shows
#: at most this many so the operation summary stays reviewable.
_HELM_PREVIEW_MAX_LINES = 60

#: Upper bound on the hint-overlay events fetch (issue #34): the trouble half
#: comes from the status the app already holds, so a stalled API connection
#: must not delay the overlay past this - the events are marked unavailable
#: instead.
_HINT_EVENTS_TIMEOUT = 3.0


@dataclasses.dataclass(frozen=True)
class ContextSwitchResult:
    """What the composition root re-derived for the new cluster (issue #36).

    Returned by the injected ``switch_context`` callable once the connection
    is retargeted: capability gates are per-cluster facts the app must adopt
    atomically with the switch.
    """

    pod_resize_supported: bool
    provider_hint: str | None
    context_namespace: str | None
    #: helm CLI wrapper rebound to the new context (issue #31 x #36): the
    #: startup HelmCLI pins --kube-context, so keeping it across a switch
    #: would send approval-gated helm writes to the OLD cluster.
    helm: HelmCLI | None = None
    #: The new context's name when it matches `protected_contexts` (issue
    #: #83), None otherwise — the marker is re-derived on every switch.
    protected_context: str | None = None


class _RelationshipLister:
    """Adapts the injected `list_relationship_objects` callable to the
    `Lister` protocol `RelationshipSnapshotLoader` (issue #281, Task 5)
    expects — the loader itself never imports the app, so it needs a small
    object with a `list_objects` method rather than a bare callable."""

    def __init__(
        self,
        list_objects: Callable[[ResourceMeta, str | None], Awaitable[list[GenericSummary]]],
    ) -> None:
        self._list_objects = list_objects

    async def list_objects(
        self, meta: ResourceMeta, namespace: str | None
    ) -> Sequence[SummaryLike]:
        return await self._list_objects(meta, namespace)


class KorvidApp(App[None]):
    # Every binding carries an ``id`` so the `keybindings:` config section
    # can remap it via Textual's keymap (issue #35); uppercase duplicates
    # of shift+<letter> keys share the action under an ``--alt`` id.
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit", id="quit"),
        Binding("question_mark", "help", "Help", id="help"),
        # Top bar collapse/expand (issue #142): the grouped legend's toggle.
        Binding("tilde", "toggle_topbar", "Legend", show=False, id="toggle_topbar"),
        Binding("colon", "open_command", "Command", id="open_command"),
        Binding("slash", "open_filter", "Filter/Search", id="open_filter"),
        Binding("0", "toggle_all_namespaces", "All NS", id="toggle_all_namespaces"),
        # `favorite_namespaces` shortcuts (issue #108): UI-only jumps, bound
        # in config order. Hidden from the footer; the help overlay merges
        # the nine bindings into a single row.
        *[
            Binding(
                str(i),
                f"favorite_namespace({i})",
                "Jump to favorite namespace (1-9)",
                show=False,
            )
            for i in range(1, 10)
        ],
        Binding("d", "describe", "Describe", id="describe"),
        Binding("g", "relationships", "Relationships", id="relationships"),
        Binding("T", "timeline", "Timeline", id="timeline"),
        Binding("s", "shell", "Shell", id="shell"),
        Binding("l", "logs", "Logs", id="logs"),
        Binding("shift+l", "logs_multi", "Multi-log", id="logs_multi"),
        # Real terminals deliver Shift+<letter> as the uppercase character,
        # not "shift+x"; bind both so the shortcut works outside Pilot tests.
        Binding("L", "logs_multi", "Multi-log", show=False, id="logs_multi--alt"),
        Binding("f", "log_format", "JSON/raw", id="log_format"),
        Binding("w", "log_wrap", "Wrap", show=False, id="log_wrap"),
        Binding("t", "log_timestamps", "Timestamps", show=False, id="log_timestamps"),
        Binding("ctrl+s", "log_save", "Save logs", show=False, id="log_save"),
        Binding("p", "log_previous", "Prev logs", id="log_previous"),
        Binding("n", "log_search_next", "Next hit", id="log_search_next"),
        Binding("shift+n", "log_search_prev", "Prev hit / Sort name", id="log_search_prev"),
        Binding(
            "N", "log_search_prev", "Prev hit / Sort name", show=False, id="log_search_prev--alt"
        ),
        # Column sorting (issue #37); shift+n doubles as sort-by-name when
        # no search pane is open (see action_log_search_prev).
        Binding("shift+a", "sort_by_age", "Sort age", show=False, id="sort_by_age"),
        Binding("A", "sort_by_age", "Sort age", show=False, id="sort_by_age--alt"),
        Binding("shift+c", "sort_by_cpu", "Sort CPU", show=False, id="sort_by_cpu"),
        Binding("C", "sort_by_cpu", "Sort CPU", show=False, id="sort_by_cpu--alt"),
        Binding("shift+m", "sort_by_mem", "Sort MEM", show=False, id="sort_by_mem"),
        Binding("M", "sort_by_mem", "Sort MEM", show=False, id="sort_by_mem--alt"),
        # Interactive column picker (issue #138): every sortable column of
        # the current view, no exact names to remember.
        Binding("o", "sort_picker", "Sort by column", show=False, id="sort_picker"),
        Binding("ctrl+a", "toggle_agent", "AI", priority=True, id="toggle_agent"),
        Binding(
            "ctrl+x",
            "interrupt_agent",
            "Stop agent",
            priority=True,
            show=False,
            id="interrupt_agent",
        ),
        Binding("ctrl+d", "delete_resource", "Delete", id="delete_resource"),
        Binding("r", "rollout_restart", "Restart", id="rollout_restart"),
        Binding(
            "R",
            "resize_pod",
            "Resize pod CPU/memory in place (K8s 1.35+)",
            show=False,
            id="resize_pod",
        ),
        Binding("S", "scale_resource", "Scale", id="scale_resource"),
        Binding("e", "edit_resource", "Edit", show=False, id="edit_resource"),
        Binding("i", "hint_details", "Hint details", show=False, id="hint_details"),
        Binding(
            "I",
            "operator_install",
            "Install operator / approve InstallPlan",
            id="operator_install",
        ),
        # Real terminals deliver Shift+F as "F" (see shift+l above).
        Binding("shift+f", "port_forward", "Port-forward", id="port_forward"),
        Binding("F", "port_forward", "Port-forward", show=False, id="port_forward--alt"),
        # Node ops (issue #40): cordon / uncordon / drain, nodes view only.
        Binding("c", "cordon_node", "Cordon", id="cordon_node"),
        Binding("u", "uncordon_node", "Uncordon", id="uncordon_node"),
        Binding("shift+d", "drain_node", "Drain", id="drain_node"),
        Binding("D", "drain_node", "Drain", show=False, id="drain_node--alt"),
        # Helm ops (issues #31/#114): dedicated per-view bindings so the
        # overloaded i/u/r keys carry the right footer label and remain
        # independently remappable; `check_action` routes each key to the
        # binding whose view is on screen.
        Binding("i", "helm_install", "Install chart", id="helm_install"),
        Binding("u", "helm_upgrade", "Upgrade", id="helm_upgrade"),
        Binding("r", "helm_rollback", "Rollback", id="helm_rollback"),
        # Revision history moved off Enter (issue #120): Enter opens the
        # hierarchy tree, `h` keeps the flat history drill-down.
        Binding("h", "helm_history", "History", id="helm_history"),
        Binding("ctrl+t", "transfer", "Transfer", show=False, id="transfer"),
    ]

    # User-facing keys handled in event handlers rather than BINDINGS:
    # Enter drills down via `on_data_table_row_selected`, Escape closes
    # panes / pops a drill level via `on_key`.  Listed here so the help
    # overlay (`?`) renders them alongside the real bindings.
    #: user-facing keys handled in event handlers or via dispatch rather than
    #: dedicated bindings: (help group, default key, description, action id).
    #: A non-empty action id ties the row to a remappable binding so the help
    #: overlay shows the effective key (issue #35), not the default.
    HANDLER_KEY_HELP: ClassVar[tuple[tuple[str, str, str, str], ...]] = (
        (
            "Table",
            "enter",
            "Drill down (pods → containers, deploy → rs → pods, helm/operator → hierarchy tree)",
            "",
        ),
        ("Table", "escape", "Pop one drill-down level", ""),
        ("Table", "ctrl+w v", "Split workspace into two panes", ""),
        ("Table", "ctrl+w w", "Focus the other pane", ""),
        ("Table", "ctrl+w q", "Close the focused pane", ""),
        ("Logs", "escape", "Close pane (or dismiss search)", ""),
    )

    DEFAULT_CSS = """
    #workspace {
        height: 1fr;
    }
    #workspace ResourceTable {
        width: 1fr;
        height: 1fr;
    }
    ResourceTable.split-pane {
        border: round $panel;
    }
    /* A class, not `:focus`: the accent border marks the command-routing
       target (the focused pane; see `WorkspaceState.focused_index`), which must
       stay visible while an Input (command/filter bar, agent panel) owns
       keyboard focus. */
    ResourceTable.split-pane.focused-pane {
        border: round $accent;
    }
    """

    def __init__(
        self,
        config: KorvidConfig,
        store: ResourceStore,
        watch_manager: WatchManager,
        list_namespaces: Callable[[], Awaitable[list[str]]] | None = None,
        aliases: dict[str, ResourceMeta] | None = None,
        get_manifest: (Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None) = None,
        get_helm_components: (Callable[[str, str], Awaitable[list[ComponentRef]]] | None) = None,
        get_events: EventsFetcher | None = None,
        stream_logs: Callable[..., AsyncIterator[LogLine]] | None = None,
        agent_runtime: AgentRuntime | None = None,
        agent_model_name: str | None = None,
        agent_configurator: AgentConfigurator | None = None,
        rebuild_agent: Callable[[AgentSettings], AgentRuntime | None] | None = None,
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
    ) -> None:
        super().__init__()
        self.config = config
        self.store = store
        self.watch_manager = watch_manager
        self._list_namespaces = list_namespaces
        #: `:ctx` collaborators (issue #36), wired by the composition root:
        #: kubeconfig context listing, the pre-switch auth probe, and the
        #: connection/capability retarget. All None in builds without a
        #: cluster connection.
        self._list_contexts = list_contexts
        self._probe_context = probe_context
        self._switch_context = switch_context
        #: Optional telepresence integration (issue #159): None = binary
        #: absent or kill-switched; the `:tp` panel simply reports that.
        self._telepresence = telepresence
        self._probe_traffic_manager = probe_traffic_manager
        self._telepresence_hinted = False
        self._telepresence_probing = False
        self._telepresence_reprobe = False
        #: True while a context switch is tearing down / retargeting;
        #: refuses concurrent switches.
        self._ctx_switching = False
        #: Bumped every time a switch is applied: pre-approval awaits capture
        #: it and refuse to proceed if the cluster changed under them.
        self._ctx_epoch = 0
        self._get_manifest = get_manifest
        self._get_helm_components = get_helm_components
        self._get_events = get_events
        self._stream_logs = stream_logs
        self._write_ops = write_ops
        self._audit = audit
        self._check_permission = check_permission
        self._mcp = mcp
        #: MCP follow mode (issue #153): mirror external cluster reads in
        #: the TUI. Config seeds the state; `:mcp follow on|off` toggles it.
        self._mcp_follow: bool = config.mcp_follow
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
        #: Bounded session timeline (issue #282, Task 3): producers, the
        #: Warning-event feed lifecycle, and the modal open/navigate flow
        #: all live in the controller. None (the constructor's `timeline`
        #: kwarg) disables every producer - the watch sink stays unwired
        #: and `action_timeline` warns instead of opening a screen, so a
        #: build without the feature pays nothing per event.
        self._timeline = SessionTimelineController(
            ui=AppUiSurface(self),
            view=AppViewState(self),
            watch_manager=self.watch_manager,
            timeline=session_timeline,
            get_epoch=lambda: self._ctx_epoch,
            epoch_crossed=self._ctx_switch_crossed,
            watch_warning_events=watch_warning_events,
            selected_resource=self._selected_timeline_resource,
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
            view=AppViewState(self),
            context=AppContextGuard(self),
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
        #: External MCP write proposals (issue #110): shared with the MCP
        #: server; None when the feature is disabled.
        self._proposal_store = proposal_store
        #: Top bar collapse/expand (issue #142): seeded from `ui.topbar`,
        #: toggled at runtime, persisted through the injected callback.
        self._topbar_expanded = config.ui_topbar_expanded
        self._save_topbar = save_topbar
        self._edit_text = edit_text
        self._metrics = metrics
        self._forwards = forwards
        #: interactive sessions (issue #187): pod exec, the kubectl debug
        #: fallback, and the approval-gated node shell. run_worker ownership
        #: and the write perimeter stay here.
        self._shell = ShellController(
            gate=self._writes,
            view=AppViewState(self),
            ui=AppUiSurface(self),
            debug=lambda: self._debug,
            audit=lambda: self._audit,
            get_manifest=lambda: self._get_manifest,
            pod_containers=lambda ns, name: self._get_pod_containers(ns, name),
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
        #: transfer execution lifecycle (issue #91 U3a): the controller owns
        #: the stream task and in-flight serialization; the app keeps the
        #: dialogs, approval gate, epoch checks, and run_worker ownership.
        self._transfer = TransferController(
            notify=self.notify,
            open_pod_exec=lambda: self._open_pod_exec,
            audit=lambda: self._audit,
            pod_uid_unchanged=self._pod_uid_unchanged,
            show_progress=self._show_transfer_progress,
            close_progress=self._close_transfer_progress,
        )
        #: OLM workflows (issue #187): the wizard, InstallPlan approval and
        #: the CSV-aware uninstall. The install dialog re-checks the
        #: subscription UID in its own callback, so it drives the gate's
        #: permitted/run directly rather than the standard confirm flow.
        self._olm = OperatorController(
            gate=self._writes,
            view=AppViewState(self),
            ui=AppUiSurface(self),
            write_ops=lambda: self._write_ops,
            get_manifest=lambda: self._get_manifest,
            confirm_screen=self._writes.confirm_screen,
            uid_intact_after_fetch=self._writes.uid_intact_after_fetch,
            precheck_keybinding_write=self._writes.precheck_keybinding_write,
        )
        #: helm write workflows (issue #187): the controller owns the wizard,
        #: preview and command construction; the approval gate, context
        #: revalidation and audited execution stay here, so the write
        #: perimeter keeps a single implementation.
        self._helm_ctl = HelmController(
            helm=lambda: self._helm,
            gate=self._writes,
            view=AppViewState(self),
            ui=AppUiSurface(self),
            # Late-binding, like DebugController's suspend/refresh: the editor
            # entry points are patched per test, so binding the bound method
            # at construction would freeze whatever existed then.
            edit_in_external_editor=lambda *a, **k: self._edit_in_external_editor(*a, **k),
            edit_text=lambda: self._edit_text,
        )
        # Debug-fallback execution (issue #97 U3c): the controller owns the
        # gated, audited kubectl debug run; dialogs, RBAC pre-check and
        # run_worker ownership stay here. suspend/refresh are late-binding so
        # tests that patch the app's methods keep working.
        self._debug = DebugController(
            notify=self.notify,
            audit=lambda: self._audit,
            readonly=lambda: self.config.readonly,
            kube_context=lambda: self.config.kube_context,
            pod_uid_unchanged=self._pod_uid_unchanged,
            suspend=lambda: self.suspend(),
            refresh=lambda: self.refresh(),
            offer_pull_retry=self._offer_pull_retry,
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
            view=AppViewState(self),
            ui=AppUiSurface(self),
            drain=self._drain,
            write_ops=lambda: self._write_ops,
            get_manifest=lambda: self._get_manifest,
            edit_text=lambda: self._edit_text,
            managed_note=self._managed_note,
            managed_note_from=self._managed_note_from,
            pod_resize_supported=lambda: self._pod_resize_supported,
            helm_uninstall=self._helm_uninstall_start,
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
        self._ns_prefetch_task: asyncio.Task[None] | None = None
        self._ctx_prefetch_task: asyncio.Task[None] | None = None
        self._splash_shown_at: float = monotonic()
        # Hint-strip lifecycle (issue #97 U3b): the controller owns the event
        # cache and the parked-cursor refresh timer; widget access and worker
        # scheduling stay here, injected as narrow callables.
        self._hints = HintController(
            find_pod_summary=self._find_pod_summary,
            cursor_row_key=self._cursor_row_key,
            on_pods_view=lambda: self.current_kind == "pods",
            get_events=lambda: self._get_events,
            show_trouble=self._hint_show_trouble,
            clear_hint=self._hint_clear,
            start_fetch=lambda coro: self.run_worker(coro, exclusive=True, group="hint-events"),
            set_timer=self.set_timer,
            ctx_epoch=lambda: self._ctx_epoch,
            ctx_crossed=self._ctx_switch_crossed,
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
            pod_containers=self._get_pod_containers,
            selected_ns_name=self._selected_ns_name,
            visible_pod_keys=lambda: [
                str(row.key.value) for row in self._focused_table().ordered_rows
            ],
            current_kind=lambda: self.current_kind,
            focused_pane=lambda: self._pane,
            ctx_epoch=lambda: self._ctx_epoch,
            ctx_switch_crossed=self._ctx_switch_crossed,
            ctx_reads_allowed=self._ctx_reads_allowed,
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
            view=AppViewState(self),
            context=AppContextGuard(self),
            logs=self._logs,
            hints=self._hints,
            config=lambda: self.config,
            get_manifest=lambda: self._get_manifest,
            get_helm_components=lambda: self._get_helm_components,
            olm_alias_key=self._olm.alias_key,
            describe_named=self._describe_named,
            cluster_list_permitted=self._cluster_list_permitted,
        )
        #: The built-in agent's session and UI ownership (issue #187 / Deep
        #: Task 6): the runtime/settings/profile/follow state, the turn task
        #: with its interrupt-and-submit lifecycle, the screen context the
        #: model is told about, and every `UIBridge` read plus the direct,
        #: approval-gated agent write. It composes the same
        #: `WriteCoordinator` perimeter every other write path uses, and
        #: reaches proposals only through `AppProposalOps`. The app keeps the
        #: Textual action/message entry points as thin delegates and the
        #: widget surfaces the controller drives.
        self._agent_ui = AgentUiController(
            panel=AppAgentPanel(self),
            screens=AppAgentScreens(self),
            ui=AppUiSurface(self),
            view=AppViewState(self),
            context=AppContextGuard(self),
            writes=self._writes,
            workspace=self._workspace,
            navigation=self._workspace_ctl,
            logs=self._logs,
            proposals=AppProposalOps(self),
            dispatch=self._bridge_dispatch,
            config=lambda: self.config,
            get_manifest=lambda: self._get_manifest,
            get_events=lambda: self._get_events,
            stream_logs=lambda: self._stream_logs,
            pod_containers=self._get_pod_containers,
            write_ops=lambda: self._write_ops,
            audit=lambda: self._audit,
            pod_resize_supported=lambda: self._pod_resize_supported,
            provider_hint=lambda: self._provider_hint,
            # Late-binding, like the other controllers' app callables: tests
            # patch `_refresh_status` after the app is constructed.
            refresh_status=lambda: self._refresh_status(),
            # Agent-follow mirrors route through the shared serialized bridge
            # (the composition root's `_UIBridgeProxy`) so they serialize with
            # the agent's own UI tools and concurrent MCP UI calls - log-pane
            # swaps and describes must never interleave. None (tests, degraded
            # wiring) falls back to the controller's own adapter.
            follow_bridge=lambda: agent_follow_bridge,
            runtime=agent_runtime,
            model_name=agent_model_name,
            configurator=agent_configurator,
            rebuild=rebuild_agent,
            disconnect=disconnect_agent,
            available=agent_available,
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
    def agent_runtime(self) -> AgentRuntime | None:
        """The live runtime — the :ai wizard may have replaced the initial
        one, so per-cluster retargeting (issue #36) must read it here."""
        return self._agent_ui.runtime

    @property
    def current_namespace(self) -> str:
        """Alias for current_scope; kept for backward-compatible test access."""
        return self.current_scope

    @current_namespace.setter
    def current_namespace(self, value: str) -> None:
        self.current_scope = value

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
        command_bar.command_words = sorted(
            {*self.aliases, "ns", "namespaces", "ctx", "context", "contexts", "q", "quit"}
        )
        # Seed session-scoped log display settings from config (logs.wrap /
        # logs.timestamps); the w/t keys toggle them from there.
        log_pane = self._log_pane
        log_pane.wrap_lines = self.config.log_wrap
        log_pane.show_timestamps = self.config.log_timestamps
        if self._forwards is not None:
            # Liveness is the point of tracked forwards (issue #38): a toast
            # must fire when one breaks even while :pf is closed.
            self.set_interval(_FORWARD_POLL_SECONDS, self._forward.poll)
        self._prefetch_namespaces()
        if self._list_contexts is not None:
            # Kubeconfig contexts feed the `:ctx` completion; a local file
            # read, but off-loop so a slow filesystem never blocks mount.
            list_contexts = self._list_contexts

            async def _fetch_contexts() -> None:
                names, _ = await asyncio.to_thread(list_contexts)
                self._command_bar.context_words = names

            self._ctx_prefetch_task = asyncio.create_task(_fetch_contexts())
        for warning in self.config.warnings:
            # Config problems (e.g. an invalid custom column) surface once at
            # startup instead of hiding in a log file (issue #45).
            self.notify(warning, title="Config warning", severity="warning")

        if self._proposal_store is not None:
            self._subscribe_proposal_updates(self._proposal_store)

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
        self.run_worker(self._maybe_hint_telepresence(), exclusive=False)

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
        command_bar.command_words = sorted(
            {*self.aliases, "ns", "namespaces", "ctx", "context", "contexts", "q", "quit"}
        )
        # A kind discovered late can turn display-only tree nodes navigable.
        self._workspace_ctl.refresh_hierarchy()

    def on_resources_updated(self, message: ResourcesUpdated) -> None:
        self._workspace_ctl.on_resources_updated(message.kind)

    def _prefetch_namespaces(self) -> None:
        """Warm the command-bar namespace completions in the background."""
        if self._list_namespaces is None:
            return

        async def _fetch() -> None:
            try:
                namespaces = await self._list_namespaces()  # type: ignore[misc]
            except Exception:
                logger.debug("namespace prefetch for completion failed", exc_info=True)
                return
            self._command_bar.namespace_words = namespaces

        self._ns_prefetch_task = asyncio.create_task(_fetch())

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
        rows = self._filtered_rows(rows, pane.resource_filter)
        all_namespaces = pane.scope == ALL_NAMESPACES
        metrics = None
        if kind == "pods" and self._metrics is not None and self._metrics.available:
            metrics = self._metrics.get
        # Dispatch rendering on the resolved meta: a group-qualified view
        # kind (alias collision) must still get its typed table, and the
        # serving group scopes group-specific renderings (the OLM tables).
        meta = self.aliases.get(kind)
        plural = meta.plural if meta is not None else kind
        table.show(
            plural,
            rows,
            all_namespaces=all_namespaces,
            # Filtering happened upstream (issue #44: labels/regex/fuzzy need
            # the full summaries, not just names) — no name pattern remains.
            pattern="",
            metrics=metrics,
            group=meta.group if meta is not None else "",
            sort=pane.sorts.get(kind),
            view=self.config.views.get(plural),
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
            HelpScreen(groups, command_help(telepresence=self._telepresence is not None))
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

    def _filtered_rows(
        self, rows: list[Summary], resource_filter: ResourceFilter | None = None
    ) -> list[Summary]:
        """Apply the active filter the way the table renders it (issue #44)."""
        flt = resource_filter if resource_filter is not None else self._resource_filter
        if not flt.active:
            return rows
        return [
            r
            for r in rows
            if flt.matches(
                r.name,
                labels=dict(getattr(r, "labels", ())),
                phase=getattr(r, "phase", None),
            )
        ]

    async def on_navigate_command(self, message: NavigateCommand) -> None:
        await self._workspace_ctl.navigate_command(message.view, message.namespace)

    async def action_toggle_all_namespaces(self) -> None:
        """Toggle scope between ALL_NAMESPACES and the config-default namespace."""
        await self._workspace_ctl.toggle_all_namespaces()

    async def action_favorite_namespace(self, index: int) -> None:
        """Jump to `favorite_namespaces[index-1]` (issue #108, keys 1-9)."""
        await self._workspace_ctl.favorite_namespace(index)

    async def _cluster_list_permitted(self) -> bool:
        """All-namespaces guard (issue #108): a forbidden cluster-wide LIST
        would only loop error cards — SSAR-check first and stay put with an
        inline notice instead. Checked fresh on every `0` press so a later
        RBAC grant is observed; SSAR failures pass through (fail-open; the
        watch reports real errors on its own)."""
        if self._check_permission is None:
            return True
        meta = self.aliases.get(self.current_kind)
        if meta is None:
            return True  # unknown kind: the watch reports its own error
        if meta.synthetic:
            if meta.backing is None:
                return True  # nothing to probe
            plural, group = meta.backing  # e.g. helm views LIST Secrets
        else:
            plural, group = meta.plural, meta.group
        try:
            allowed = await asyncio.wait_for(
                self._check_permission("list", plural, "", None, group, ""),
                timeout=_PERMISSION_CHECK_TIMEOUT,
            )
        except Exception:
            logger.debug("cluster-wide list pre-check failed; allowing", exc_info=True)
            return True
        if not allowed:
            self.notify(
                f"Cluster-wide {plural} list forbidden — staying in"
                f" {self.current_scope!r}. Press 0 again after access is"
                " granted, or switch namespaces with `:ns <name>`.",
                severity="warning",
            )
        return allowed

    async def on_show_namespace_picker(self, message: ShowNamespacePicker) -> None:
        if self._list_namespaces is None:
            self.notify("Namespace listing unavailable", severity="warning")
            return
        # The listing would race the client swap and could return either
        # cluster's namespaces — refuse up front.
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch
        try:
            namespaces = await self._list_namespaces()
        except ApiStatusError as exc:  # API failures get the actionable mapping (§5-5)
            if self._ctx_switch_crossed(epoch):
                return  # a stale old-cluster error is not worth surfacing
            self._handle_namespace_list_error(exc)
            return
        except Exception as exc:  # surface any other listing failure to the user
            if self._ctx_switch_crossed(epoch):
                return
            self.notify(str(exc), title="Failed to list namespaces", severity="error")
            return
        if self._ctx_switch_crossed(epoch):
            # The listing awaited through a :ctx switch: opening the picker
            # now would offer old-cluster namespaces to the new session.
            self.notify(
                "Namespace picker cancelled - the kube context changed",
                severity="warning",
            )
            return
        if not namespaces:
            self.notify("No namespaces visible (check RBAC)", severity="warning")
            return
        self._command_bar.namespace_words = namespaces
        self._namespace_picker.open(namespaces)

    def _ctx_switch_crossed(self, epoch: int) -> bool:
        """True when a :ctx switch started or completed since *epoch* was taken."""
        return self._ctx_switching or epoch != self._ctx_epoch

    def _ctx_reads_allowed(self) -> bool:
        """Refuse read actions that spawn cluster streams during a :ctx switch.

        Streams started mid-swap would attach to whichever cluster wins the
        switch while still labeled with the old selection (issue #84).
        Returns True when it is safe to proceed.
        """
        if self._ctx_switching:
            self.notify(
                "A context switch is in progress — try again once it completes",
                severity="warning",
            )
            return False
        return True

    def _handle_namespace_list_error(self, exc: ApiStatusError) -> None:
        """403 is an authorization boundary (issue #108): show one concise
        permission notice pointing at `:ns <name>` free-text entry — never
        manufacture a namespace list from configuration."""
        msg = explain_api_error(exc.status, exc.reason, "namespaces", None)
        if exc.status == 403:
            msg += " Switch directly with `:ns <name>`."
        self.notify(msg, title="Failed to list namespaces", severity="error")

    # ------------------------------------------------------------------
    # `:ctx` — runtime context switching (issue #36)
    # ------------------------------------------------------------------

    def on_show_context_picker(self, message: ShowContextPicker) -> None:
        self.run_worker(self._show_context_picker(), exclusive=False)

    def on_switch_context_command(self, message: SwitchContextCommand) -> None:
        self.run_worker(self._switch_context_flow(message.name), exclusive=False)

    _CURRENT_CTX_SUFFIX = " (current)"

    async def _show_context_picker(self) -> None:
        if self._list_contexts is None:
            self.notify("Context switching unavailable in this build", severity="warning")
            return
        names, active = await asyncio.to_thread(self._list_contexts)
        if not names:
            self.notify("No contexts found in kubeconfig", severity="warning")
            return
        self._command_bar.context_words = names
        # Sessions started from the kubeconfig current-context have no
        # explicit config value — fall back to what the kubeconfig reports.
        current = self.config.kube_context or active
        # Explicit display->name mapping: decoding the label (suffix strip)
        # would corrupt a real context whose name ends in " (current)".
        labels: dict[str, str] = {}
        for n in names:
            label = f"{n}{self._CURRENT_CTX_SUFFIX}" if n == current else n
            if label != n and (label in names or label in labels):
                label = n  # marker collides with another context's name
            labels[label] = n

        def _on_pick(choice: str | None) -> None:
            if choice is None:
                return
            self.post_message(SwitchContextCommand(labels.get(choice, choice)))

        self.push_screen(PickScreen("Switch context:", list(labels)), _on_pick)

    async def _switch_context_flow(self, name: str) -> None:
        """Orchestrate a context switch: guards, auth probe, teardown, swap.

        The probe runs against a private client configuration first — on any
        failure nothing has been torn down and the old context keeps working
        (issue #36's "don't strand the user" requirement). Only a proven
        target proceeds to teardown and retarget.
        """
        if self._probe_context is None or self._switch_context is None:
            self.notify("Context switching unavailable in this build", severity="warning")
            return
        # Claim before the first await: two queued SwitchContextCommands
        # must not both pass the guards and race the teardown.
        if self._ctx_switching:
            self.notify("A context switch is already in progress", severity="warning")
            return
        self._ctx_switching = True
        try:
            await self._switch_context_locked(name)
        finally:
            self._ctx_switching = False

    async def _switch_context_locked(self, name: str) -> None:
        """The body of `_switch_context_flow`; runs with the claim held."""
        old = self.config.kube_context
        if await self._is_ctx_noop(name, old):
            self.notify(f"Already on context {name}")
            return
        if not await self._ctx_switch_guards_pass(name):
            return
        # The switch is now committed to attempting: recorded before the
        # probe, on the epoch that is still serving this session. Anything
        # refused above never started, and inventing a `started` for it
        # would put a transition in the record that never happened.
        epoch = self._ctx_epoch
        self._timeline.record_context_switch(
            epoch=epoch, phase="started", from_context=old, to_context=name
        )
        try:
            await self._probe_context(name)  # type: ignore[misc]  # guarded by caller
        except Exception as exc:
            # Nothing was torn down and no cluster was applied, so the
            # failure belongs to the epoch that is still live.
            self._timeline.record_context_switch(
                epoch=epoch,
                phase="failed",
                from_context=old,
                to_context=name,
                note=self._describe_ctx_error(exc),
            )
            self.notify(
                f"Cannot switch to context {name!r}: {self._describe_ctx_error(exc)}"
                f" — staying on {old or 'the current context'}",
                severity="error",
                timeout=10,
            )
            return
        async with self._workspace_ctl.nav_lock:
            # The probe awaited network I/O — an agent turn or a dialog may
            # have started meanwhile; re-check before anything is torn down.
            blocker = self._ctx_switch_blocker()
            if blocker is not None:
                # A `started` with no terminal phase would read as a switch
                # still in flight; the abort is the outcome, on the old epoch.
                self._timeline.record_context_switch(
                    epoch=epoch,
                    phase="failed",
                    from_context=old,
                    to_context=name,
                    note=blocker,
                )
                self.notify(blocker, severity="warning")
                return
            # Quiesce the embedded MCP server BEFORE any teardown: external
            # callers share the client and alias map being swapped, and an
            # undrainable server must abort while the old context is still
            # fully usable (watches, forwards, store all intact).
            mcp_restart = await self._quiesce_mcp_for_switch()
            if mcp_restart is None:
                self._timeline.record_context_switch(
                    epoch=epoch,
                    phase="failed",
                    from_context=old,
                    to_context=name,
                    note="embedded MCP server did not stop in time",
                )
                return
            # Old-context proposals are stale the moment this committed
            # transition begins: the old MCP run (and its capability) is
            # already stopped, and both the teardown below and the retarget
            # perform fallible awaits — expire them now, not at a later
            # point that an exception could keep from ever being reached.
            await self._expire_proposals_audited("kube context switched")
            await self._teardown_for_context_switch()
            ok, applied = await self._retarget_context(name, old)
            if not ok:
                if mcp_restart:
                    self.notify(
                        "Embedded MCP server was stopped for the switch —"
                        " restart it with :mcp on once reconnected",
                        severity="warning",
                        timeout=15,
                    )
                return
            self._resume_timeline_after_retarget(name, old, applied)
            if mcp_restart and self._mcp is not None:
                # Resume on the same endpoint, now serving whichever context
                # was actually applied (target, or the restored old one).
                msg = await self._mcp.start()
                self.notify(msg, severity="error" if msg.startswith("ERROR") else "information")
            await self.watch_manager.start(self.current_kind, self.current_scope)
            await self._workspace_ctl.sync_metrics_poller()
        self.post_message(ResourcesUpdated(self.current_kind))
        self._refresh_status()
        self._prefetch_namespaces()
        self.on_aliases_updated()
        if applied == name:
            self.notify(f"Switched to context {name} (ns: {self.current_scope})")

    def _resume_timeline_after_retarget(
        self, name: str, old: str | None, applied: str | None
    ) -> None:
        """Close out the switch on the timeline and rebind its Warning feed.

        `completed` belongs to the epoch the switch created, so the new
        cluster's record opens with the switch that started it — but only
        when the requested target is what got applied: `_apply_context_switch`
        also runs while *restoring* the old context after a failed swap, and
        recording completion there would report the target that failed as if
        it had succeeded. The feed restarts either way: teardown cancelled the
        old epoch's, and whichever context is now applied deserves one.
        """
        if applied == name:
            self._timeline.record_context_switch(
                epoch=self._ctx_epoch,
                phase="completed",
                from_context=old,
                to_context=name,
                note="all cluster state was reset",
            )
        self._timeline.start_warning_watch()

    async def _quiesce_mcp_for_switch(self) -> bool | None:
        """Drain and stop the embedded MCP server ahead of a context switch.

        Returns True when a restart is owed after the switch, False when the
        server was not running, and None when the server could not be drained
        in time — the switch must then abort with nothing torn down.
        """
        if self._mcp is None or not self._mcp.running:
            return False
        pending = await self._mcp.shutdown()
        if pending is not None:
            # Even cancellation didn't land within its deadline: an in-flight
            # tool call could cross the context boundary if we proceeded.
            self.notify(
                "Embedded MCP server did not stop in time — context"
                " switch aborted (old context untouched)",
                severity="error",
                timeout=10,
            )
            return None
        return True

    async def _is_ctx_noop(self, name: str, old: str | None) -> bool:
        """True when *name* is already the active context — explicitly, or as
        the kubeconfig's active context for sessions started without
        -c/--context (old stays None there for the recovery path)."""
        effective = old
        if effective is None and self._list_contexts is not None:
            with contextlib.suppress(Exception):
                _, effective = await asyncio.to_thread(self._list_contexts)
        return name == effective

    def _ctx_switch_blocker(self) -> str | None:
        """Why a switch cannot proceed right now, or None when it can."""
        if self._agent_ui.busy:
            return "Agent is busy — wait for the current turn to finish before switching contexts"
        if self._writes.active_writes():
            return (
                "A cluster write is in progress — wait for it to finish before switching contexts"
            )
        if len(self.screen_stack) > 1:
            return "Close open dialogs before switching contexts"
        try:
            # The inline namespace picker is not a screen: its old-cluster
            # options would survive teardown and a later selection would
            # navigate the new cluster to a namespace picked from the old.
            if self._namespace_picker.display:
                return "Close the namespace picker before switching contexts"
        except NoMatches:  # widget tree not composed (shutdown/startup)
            pass
        return None

    async def _ctx_switch_guards_pass(self, name: str) -> bool:
        """Pre-probe refusals; each states why the switch cannot start now."""
        blocker = self._ctx_switch_blocker()
        if blocker is not None:
            self.notify(blocker, severity="warning")
            return False
        if self._list_contexts is not None:
            names, _ = await asyncio.to_thread(self._list_contexts)
            if names and name not in names:
                self.notify(
                    f"Unknown context {name!r} — kubeconfig has: {', '.join(names)}",
                    severity="error",
                )
                return False
        return True

    @staticmethod
    def _describe_ctx_error(exc: Exception) -> str:
        if isinstance(exc, TimeoutError):
            return "authentication check timed out"
        return str(exc) or type(exc).__name__

    async def _teardown_for_context_switch(self) -> None:
        """Stop every consumer of the old cluster before the client swaps.

        Order matters: streams and pollers first (they hold the old
        connection), then session state that would otherwise leak old-cluster
        rows, breadcrumbs, or hints into the new one.
        """
        await self._logs.close()
        self._describe_pane.hide()
        # The workspace controller folds the split back to one pane, stops and
        # de-targets the metrics poller, clears the drill breadcrumb, cancels
        # the relationship workers holding the old client, and clears the
        # filter — the workspace-only halves of the teardown (issue #187 /
        # Deep Task 3). It runs first (under this coordinator's nav lock, held
        # by the caller) so those pollers and workers release the old
        # connection before the wholesale watch stop below.
        await self._workspace_ctl.quiesce_for_context_switch()
        # An old-cluster namespace prefetch still in flight could land after
        # the new cluster's and overwrite its completions — cancel it first.
        if self._ns_prefetch_task is not None:
            self._ns_prefetch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ns_prefetch_task
            self._ns_prefetch_task = None
        # Completions that already loaded are old-cluster names — drop them
        # now so they aren't offered while (or if) the new prefetch fails.
        self._command_bar.namespace_words = []
        await self.watch_manager.stop_all()
        if self._forwards is not None:
            # Same quiesce-stop-audit sequence as app exit: in-flight
            # launches land first, stop_all runs off-loop (it polls up to
            # the grace deadline), and every stop is enqueued for audit.
            stopped = await self._forward.teardown(self._forwards)
            if stopped:
                self.notify(f"Stopped {len(stopped)} port-forward(s) targeting the old cluster")
        # Old-cluster audit entries resolve their context only at append();
        # flush them before _apply_context_switch re-points the audit log,
        # or they would be written as belonging to the new cluster.
        await self._forward.flush_audits()
        self.store.clear_all()
        # The hint-events worker holds the old client and its exception path
        # re-populates the cache — cancel it (and the parked-cursor refresh
        # timer) before the cache is cleared, so no late result or retry can
        # resurrect old-cluster hints.
        self.workers.cancel_group(self, "hint-events")
        self._hints.teardown()
        await self._timeline.stop()

    async def _retarget_context(self, name: str, old: str | None) -> tuple[bool, str | None]:
        """Swap the connection to *name*; on failure fall back to *old*.

        Returns ``(ok, applied)``: ``ok`` is False only when even the
        fallback failed (the session then needs a restart — everything is
        already torn down and nothing is connected). ``applied`` is the
        context actually in effect, which may legitimately be None (the
        kubeconfig default) — that is why success is a separate flag.

        Old-context proposals were already expired by the caller (right
        after MCP quiescing), before teardown or either switch attempt.
        """
        # Captured before the first attempt: `_apply_context_switch` bumps
        # the epoch as its first action, so reading it in the handler below
        # could file a failed swap under the epoch it failed to create.
        epoch = self._ctx_epoch
        try:
            result = await self._switch_context(name)  # type: ignore[misc]  # guarded by caller
            self._apply_context_switch(name, old, result)
            return True, name
        except Exception as exc:
            self._timeline.record_context_switch(
                epoch=epoch,
                phase="failed",
                from_context=old,
                to_context=name,
                note=self._describe_ctx_error(exc),
            )
            self.notify(
                f"Context switch to {name!r} failed mid-swap: {self._describe_ctx_error(exc)}",
                severity="error",
                timeout=10,
            )
        try:
            result = await self._switch_context(old)  # type: ignore[misc]  # guarded by caller
            self._apply_context_switch(old, old, result)
            self.notify(f"Restored context {old or '(kubeconfig default)'}")
            return True, old
        except Exception as exc:
            self.notify(
                f"Could not restore context {old or '(kubeconfig default)'}:"
                f" {self._describe_ctx_error(exc)} — restart korvid",
                severity="error",
                timeout=15,
            )
            return False, None

    def _apply_context_switch(
        self, name: str | None, old: str | None, result: ContextSwitchResult
    ) -> None:
        """Adopt the new cluster's identity and re-probed capabilities."""
        self._ctx_epoch += 1
        # Adopt the target context's kubeconfig namespace as the session
        # default too: `ns` toggle-back and the helm/operator namespace
        # fallbacks read config.namespace, and jumping to the *startup*
        # context's namespace after a switch would cross clusters.
        self.config = dataclasses.replace(
            self.config,
            kube_context=name,
            namespace=result.context_namespace or self.config.namespace,
        )
        self._pod_resize_supported = result.pod_resize_supported
        self._provider_hint = result.provider_hint
        self._writes.set_protected_context(result.protected_context)
        if result.protected_context is not None:
            self.notify(
                f"Context {result.protected_context!r} is protected — writes"
                " require typing the context name",
                severity="warning",
                timeout=10,
            )
        if self._audit is not None:
            self._audit.set_context(name)
        if self._forwards is not None:
            # Reopen the registry that teardown latched closed; forwards
            # started from now on target the new cluster.
            self._forwards.retarget(name)
            self._forward.reopen()
        # Adopt the new cluster's default view (pods in its default namespace)
        # through the workspace controller, which owns the view state and the
        # view-scoped binding refresh.
        self._workspace_ctl.reset_view_after_switch()
        # Rebind the helm wrapper: it pins --kube-context per instance, and
        # helm writes must follow the active cluster (None when helm is off).
        self._helm = result.helm
        # The new cluster may run a telepresence traffic-manager the old one
        # lacked: re-probe (a no-op once the session's hint was shown).
        self.run_worker(self._maybe_hint_telepresence(), exclusive=False)
        if name != old:
            self._agent_ui.note_context_switch(
                f"kube context switched from {old or '(default)'} to {name};"
                " all cluster state was reset"
            )

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
            self._hint_clear()
            return
        self._hints.show_for_row(str(event.row_key.value))

    def _hint_show_trouble(
        self, trouble: tuple[ContainerTrouble, ...], *, event: str | None = None
    ) -> None:
        """Strip adapter for the hint controller (widget access stays here).

        A render dispatched during shutdown/teardown can arrive after the
        strip is unmounted; nothing to render then.
        """
        with contextlib.suppress(NoMatches):
            self._hint_strip.show_trouble(trouble, event=event)

    def _hint_clear(self) -> None:
        """Strip adapter for the hint controller (widget access stays here)."""
        with contextlib.suppress(NoMatches):  # strip unmounted during shutdown/teardown
            self._hint_strip.clear_hint()

    def _cursor_row_key(self) -> str | None:
        """Row key under the table cursor, or None (empty table / no cursor)."""
        try:
            table = self._focused_table()
        except NoMatches:  # timer fired while the app is shutting down
            return None
        if table.cursor_row < 0:
            return None
        try:
            key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        except CellDoesNotExist:
            return None
        return None if key is None else str(key.value)

    def _find_pod_summary(self, row_key: str) -> PodSummary | None:
        parts = row_key.split("/", 1)
        if len(parts) != 2:
            return None
        namespace, name = parts
        for obj in self.store.get("pods", self.current_scope):
            if obj.namespace == namespace and obj.name == name and isinstance(obj, PodSummary):
                return obj
        return None

    def action_hint_details(self) -> None:
        """Open the read-only detail overlay for the hinted pod row (issue #34):
        the full trouble list plus recent Warning events - everything the
        two-line strip folded away."""
        if self.current_kind != "pods":
            return
        row_key = self._cursor_row_key()
        if row_key is None:
            return
        summary = self._find_pod_summary(row_key)
        if summary is None or not pod_needs_hint(summary):
            return
        self.run_worker(
            self._open_hint_details(row_key, summary), exclusive=True, group="hint-detail"
        )

    async def _open_hint_details(self, row_key: str, summary: PodSummary) -> None:
        """Fetch events best-effort, then push the overlay: the trouble half
        renders even when the events API fails ("unavailable" is stated, not
        conflated with "no events"). The context is revalidated after the
        await - the cursor, view, or screen stack may have changed meanwhile,
        and stale details for the wrong pod are worse than none."""
        events: list[dict[str, Any]] = []
        events_unavailable = False
        if self._get_events is not None:
            try:
                events = await asyncio.wait_for(
                    self._get_events.fetch(
                        summary.namespace, summary.name, uid=summary.uid or None
                    ),
                    timeout=_HINT_EVENTS_TIMEOUT,
                )
            except Exception:  # events are decoration; trouble alone still helps
                events_unavailable = True
        if len(self.screen_stack) > 1:  # another dialog opened during the fetch
            return
        if self.current_kind != "pods" or self._cursor_row_key() != row_key:
            return
        fresh = self._find_pod_summary(row_key)
        if fresh is None or fresh.uid != summary.uid:
            return  # deleted or recreated mid-fetch
        if not pod_needs_hint(fresh):
            return  # recovered mid-fetch: the strip is gone, details would be noise
        await self.push_screen(
            HintDetailScreen(
                f"{summary.namespace}/{summary.name}",
                fresh.trouble,
                events,
                events_unavailable=events_unavailable,
            )
        )

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
        await self._open_containers_screen(parts[0], parts[1])

    async def _open_containers_screen(self, namespace: str, name: str) -> None:
        """Push the containers screen for a pod; shell/logs run per pick.

        The row fetch and the open screen both span awaited gaps, so the
        context epoch captured here cancels stale picks: a shell or log
        stream started after a completed switch would target the new cluster
        with the old cluster's pod selection.
        """
        epoch = self._ctx_epoch
        rows = await self._build_container_rows(namespace, name)
        if not rows:
            self.notify("No containers found for this pod", severity="warning")
            return
        if self._ctx_switching or epoch != self._ctx_epoch:
            # The row fetch awaited through a context switch: the selection
            # belongs to the old cluster.
            return

        def _on_pick(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            if self._ctx_switching or epoch != self._ctx_epoch:
                self.notify(
                    f"container action on {name} cancelled - the kube context"
                    " changed while the containers screen was open",
                    severity="warning",
                )
                return
            action, container = result
            if action == "shell":
                self._shell.run_shell(namespace, name, container)
            else:
                if self._stream_logs is None:
                    self.notify("Log streaming unavailable", severity="warning")
                    return
                self.run_worker(self._logs.open_pane(namespace, [(name, container)]))

        await self.push_screen(ContainersScreen(name, rows), _on_pick)

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
        return canonical_meta_kind(self.aliases, meta)

    def _focus_row(self, row_key: str) -> bool:
        """Move the focused table's cursor to *row_key*; False when absent."""
        table = self._focused_table()
        try:
            index = table.get_row_index(row_key)
        except RowDoesNotExist:
            return False
        table.move_cursor(row=index)
        return True

    async def _describe_named(self, kind: str, namespace: str, name: str) -> None:
        """Describe an object named by a hierarchy tree node (no table row
        to read the selection from - `action_describe`'s selection-bound
        path does not apply)."""
        if self._get_manifest is None:
            self.notify("Describe unavailable", severity="warning")
            return
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch
        try:
            manifest = await self._get_manifest(kind, namespace or None, name)
        except ApiStatusError as exc:
            self.notify(
                explain_api_error(exc.status, exc.reason, kind, namespace or None),
                severity="error",
            )
            return
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        if self._ctx_switching or epoch != self._ctx_epoch:
            return
        title = f"{kind}/{namespace}/{name}" if namespace else f"{kind}/{name}"
        if manifest.get("kind") == "Secret":
            # Same masking rule as action_describe (spec §5 #9).
            await self.push_screen(SecretScreen(title, manifest, audit=self._audit))
            return
        await self.push_screen(
            DescribeScreen(title, manifest, [], footer_note=self._provider_footer(manifest))
        )

    async def _build_container_rows(
        self, namespace: str, name: str
    ) -> list[tuple[str, str, str, str, str]]:
        """Container rows from the live manifest; store names as fallback."""
        if self._get_manifest is not None:
            try:
                manifest = await self._get_manifest("pods", namespace, name)
            except (ApiStatusError, ValueError) as exc:
                logger.debug("manifest fetch for container list failed: %s", exc)
            else:
                rows = build_container_rows(manifest)
                if rows:
                    return rows
        return [(ctr, "-", "-", "-", "-") for ctr in self._get_pod_containers(namespace, name)]

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

    def _selected_timeline_resource(self) -> TimelineResourceRef | None:
        """The exact resource under the cursor when `T` is pressed, captured
        once so the timeline's `r` toggle keeps pinning it even as the
        table underneath changes (issue #282). Unlike
        `_selected_relationship_root`, this never `notify`s: the timeline
        opens with or without a selection, so an empty, unselected, or
        synthetic view just means `r` has nothing to toggle - the modal's
        own status line says so, not a warning toast the user didn't ask for."""
        meta = self.aliases.get(self.current_kind)
        if meta is None or meta.synthetic:
            return None
        table = self._focused_table()
        if table.row_count == 0:
            return None
        row_index = table.cursor_row
        ordered = table.ordered_rows
        if row_index >= len(ordered):
            return None
        parts = str(ordered[row_index].key.value).split("/", 1)
        if len(parts) != 2:
            return None
        namespace, name = parts
        return TimelineResourceRef(
            kind_alias=self._canonical_kind(self.current_kind),
            display_kind=meta.kind,
            namespace=namespace,
            name=name,
            uid=self._selected_uid(namespace or None, name),
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
        """Fetch and display the manifest + events for the currently highlighted row."""
        if self._get_manifest is None:
            self.notify("Describe unavailable", severity="warning")
            return
        # The fetch would race the client swap and could render either
        # cluster's manifest — refuse up front.
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch

        namespace, name = self._selected_ns_name()
        if namespace is None or name is None:
            return
        ns: str | None = namespace if namespace else None

        try:
            manifest = await self._get_manifest(self.current_kind, ns, name)
        except ApiStatusError as exc:
            msg = explain_api_error(exc.status, exc.reason, self.current_kind, namespace or None)
            self.notify(msg, severity="error")
            return
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return

        events: list[dict[str, Any]] = []
        # Events are filtered by involvedObject.name only, so restrict to pods
        # to avoid showing events for unrelated objects with the same name.
        if self._get_events is not None and ns is not None and self.current_kind == "pods":
            try:
                events = await self._get_events.fetch(namespace, name)
            except ApiStatusError as exc:
                # Events are best-effort; surface but still show the manifest.
                msg = explain_api_error(exc.status, exc.reason, "events", namespace)
                self.notify(msg, severity="warning")

        if self._ctx_switching or epoch != self._ctx_epoch:
            # The fetches awaited through a context switch: the manifest (or
            # a mixed manifest/events pair) describes the old cluster and
            # must not be pushed over the new session.
            self.notify(
                f"describe {name} cancelled - the kube context changed during the fetch",
                severity="warning",
            )
            return
        title = f"{self.current_kind}/{namespace}/{name}"
        if manifest.get("kind") == "Secret":
            # Secrets get the dedicated masked viewer (spec §5 #9): values
            # render masked; per-key reveal is explicit and audit-logged.
            await self.push_screen(SecretScreen(title, manifest, audit=self._audit))
            return
        await self.push_screen(
            DescribeScreen(title, manifest, events, footer_note=self._provider_footer(manifest))
        )

    def on_unknown_command(self, message: UnknownCommand) -> None:
        parts = message.text.strip().split()
        head = parts[0] if parts else ""
        if head in {"ai", "agent"} and self._agent_ui.available:
            self._agent_ui.handle_command(parts[1:])
            return
        if head == "model" and self._agent_ui.available:
            self._agent_ui.handle_model_command(parts[1:])
            return
        if head == "mcp":
            self._handle_mcp_command(parts[1:])
            return
        if head in {"tp", "telepresence"}:
            self._handle_telepresence_command()
            return
        if head == "proposals":
            self._open_proposal_review()
            return
        if head == "pf":
            self._forward.open_list()
            return
        if head == "operators" and "operators" not in self.aliases:
            # The catalog view only exists where OLM serves PackageManifests;
            # explain the absence instead of a generic unknown-kind error.
            # Only when the alias is genuinely unavailable: a syntax error on
            # a discovered view (":operators ns extra") falls through to the
            # normal unknown-command message.
            # "Not discovered" and not "absent": background discovery may
            # still be running, or may have failed (pods-only fallback) -
            # indistinguishable states from here.
            self.notify(
                "The operator catalog is unavailable: the"
                " packages.operators.coreos.com API group was not discovered"
                " (OLM may be absent, or discovery may still be running)",
                severity="warning",
            )
            return
        self.notify(
            f"Unknown resource or command: {message.text}"
            " — not found in this cluster's API (CRD not installed?)",
            severity="warning",
        )

    def _handle_telepresence_command(self) -> None:
        """`:tp` / `:telepresence` (issue #159): open the read-only status
        panel. Queries run only here, on the explicit user action - the
        telepresence CLI spawns its local user daemon to answer, so korvid
        never polls it in the background."""
        tp = self._telepresence
        if tp is None:
            self.notify(
                "telepresence not available — binary not on PATH, or disabled "
                "via `integrations: {telepresence: off}`",
                severity="warning",
            )
            return

        async def _open() -> None:
            with self._progress("querying telepresence"):
                try:
                    status = await tp.status()
                    intercepts = (
                        await tp.list_intercepts(daemon=status.daemon_name or None)
                        if status.connected
                        else []
                    )
                except TelepresenceError as exc:
                    # stderr tails are hostile input for a markup toast.
                    self.notify(str(exc), title="telepresence", severity="error", markup=False)
                    return
            await self.push_screen(TelepresenceScreen(status, intercepts))

        self.run_worker(_open(), exclusive=True, group="telepresence")

    async def _maybe_hint_telepresence(self) -> None:
        """One dim hint per session (issue #159): the cluster runs a
        traffic-manager but the local client is absent. The probe is an
        injected pure API check - never the telepresence binary; a missing
        probe, a failure or the kill-switch all silently mean no hint.

        Re-runnable until the hint actually shows: the startup cluster may
        lack a manager while a later `:ctx` target runs one. Results are
        epoch-bound - a probe answering for a context that was already left
        is discarded - and a re-probe requested while one is in flight is
        queued instead of lost.
        """
        if (
            self._telepresence is not None
            or self._telepresence_hinted
            or not self.config.telepresence_enabled
            or self._probe_traffic_manager is None
        ):
            return
        if self._telepresence_probing:
            # A :ctx switch mid-probe: run again once the old probe (whose
            # answer describes the old cluster) unwinds.
            self._telepresence_reprobe = True
            return
        self._telepresence_probing = True
        epoch = self._ctx_epoch
        try:
            present = await self._probe_traffic_manager()
        except Exception:
            return  # absent / forbidden / transient: all mean "no hint"
        finally:
            self._telepresence_probing = False
            if self._telepresence_reprobe:
                self._telepresence_reprobe = False
                self.run_worker(self._maybe_hint_telepresence(), exclusive=False)
        if not present or epoch != self._ctx_epoch:
            return  # no manager, or the answer describes a left context
        self._telepresence_hinted = True
        self.notify(
            "telepresence traffic-manager detected in this cluster — install "
            "the client and restart korvid to inspect intercepts (`:tp`)",
            severity="information",
            timeout=8,
        )

    def _handle_mcp_follow_command(self, args: list[str]) -> None:
        """`:mcp follow [on|off]` (issue #153): toggle mirroring of external
        cluster reads in the TUI. Bare `:mcp follow` flips the state."""
        if args and args[0].lower() not in ("on", "off"):
            self.notify("Usage: :mcp follow [on|off]", severity="warning")
            return
        self._mcp_follow = args[0].lower() == "on" if args else not self._mcp_follow
        state = "on" if self._mcp_follow else "off"
        self.notify(
            f"MCP follow {state} — external reads are {'mirrored on screen' if self._mcp_follow else 'no longer mirrored'}"
        )
        self._refresh_status()

    @property
    def mcp_follow_enabled(self) -> bool:
        """Current follow-mode state; read by the MCP server's wiring."""
        return self._mcp_follow

    def note_mcp_activity(self, line: str) -> None:
        """Transient activity note for an external MCP read (issue #153):
        with follow off, this is the only trace an external host leaves on
        screen. Display only — never raises into the caller."""
        with contextlib.suppress(Exception):
            # markup=False: parts of the line are caller-controlled (pod and
            # namespace names from the MCP host) - Rich tags must render
            # literally, never restyle or forge toast content.
            self.notify(line, title="MCP", severity="information", timeout=3, markup=False)

    def _handle_mcp_command(self, args: list[str]) -> None:
        """`:mcp` shows server state; `:mcp on` / `:mcp off` toggle it live."""
        mcp = self._mcp
        if mcp is None:
            self.notify(
                f"MCP unavailable — {isolated_install_hint(feature='mcp')}",
                severity="warning",
                markup=False,
            )
            return
        if not args:
            follow = "follow on" if self._mcp_follow else "follow off"
            self.notify(f"{mcp.status()} · {follow}")
            return
        action = args[0].lower()
        if action == "follow":
            self._handle_mcp_follow_command(args[1:])
            return
        if action not in ("on", "off"):
            self.notify("Usage: :mcp [on|off] | :mcp follow [on|off]", severity="warning")
            return
        self._toggle_mcp_server(mcp, action)

    def _toggle_mcp_server(self, mcp: MCPControllerBase, action: str) -> None:
        """Start/stop the MCP server live (`:mcp on` / `:mcp off`)."""
        if self._ctx_switching:
            # The switch quiesced the server before swapping the client and
            # alias map; a toggle landing mid-swap could restart it against
            # state that is being replaced.
            self.notify(
                "A context switch is in progress — try again once it completes",
                severity="warning",
            )
            return

        async def _switch() -> None:
            # Serialize with the `:ctx` flow (which holds _nav_lock through
            # quiesce/teardown/retarget) and re-check inside the lock: a
            # toggle queued just before the switch claimed _ctx_switching
            # could otherwise start the server against the client/alias map
            # mid-swap, or have its stop undone by the switch's restart.
            async with self._workspace_ctl.nav_lock:
                if self._ctx_switching:
                    self.notify(
                        "A context switch is in progress — try again once it completes",
                        severity="warning",
                    )
                    return
                was_running = mcp.running
                if action == "on" and not was_running:
                    # Any pending proposal predates the run about to start —
                    # its capability token is from an older, ended run.
                    # Sweep BEFORE the endpoint goes live: once start()
                    # returns, the new run's callers may already have
                    # submitted, and their work must not be expired as
                    # old-run stragglers.
                    await self._expire_proposals_audited("the MCP server was restarted")
                msg = await (mcp.start() if action == "on" else mcp.stop())
                # A real stop invalidates every capability token handed out
                # for that run (issue #110): pending proposals from it must
                # not survive. A stop whose bounded teardown timed out
                # (`running` still True) has still ended that run's
                # authority, so it expires too; only an idempotent
                # status-preserving toggle (`:mcp on` while already
                # running) keeps pending work.
                stopped = action == "off" and was_running
                # Captured under the lock and BEFORE the audited sweep: the
                # sweep awaits audit appends, and the dying run's task can
                # finish during that wait — `running` re-read afterwards
                # would be False, skipping the follow-up sweep that catches
                # the run's last in-flight submissions. The wait below must
                # bind to *this* dying run, never to whichever run the
                # controller owns once the lock is released.
                old_task = mcp.pending_task() if stopped and mcp.running else None
                if stopped:
                    await self._expire_proposals_audited("the MCP server was stopped")
            self.notify(msg, severity="error" if msg.startswith("ERROR") else "information")
            self._refresh_status()
            if old_task is not None:
                # The bounded stop timed out and the old run is still dying
                # in the background: wait it out, then sweep again so an
                # in-flight submission that raced the teardown cannot
                # outlive its server run.
                await self._sweep_after_mcp_teardown(mcp, old_task)

        self.run_worker(_switch(), exclusive=False)

    async def _sweep_after_mcp_teardown(
        self, mcp: MCPControllerBase, task: asyncio.Task[None]
    ) -> None:
        """Final proposal sweep once a dragged-out MCP teardown completes.

        `stop()`'s bounded wait can return while the old server run is still
        dying; an in-flight proposal call on that run may land *after* the
        stop-time sweep. *task* is the old run's server task, captured under
        `_nav_lock` before it was released: the follow-up wait binds to that
        exact run, so a fresh server started by a racing `:mcp on` is never
        the one waited on or torn down here.
        """
        with contextlib.suppress(Exception):
            await asyncio.wait({task})
        # Serialize the sweep decision against a racing `:mcp on`: if a
        # fresh run came up while the old teardown dragged on, pending
        # proposals belong to that live run (its start transition already
        # swept old-run stragglers) and must not be expired here.
        async with self._workspace_ctl.nav_lock:
            if mcp.running:
                return
            await self._expire_proposals_audited("the MCP server was stopped")

    async def action_port_forward(self) -> None:
        """Open the port-forward dialog for the selected pod or service (shift+f)."""
        if self.current_kind not in FORWARDABLE_KINDS:
            self.notify("Port-forward is only available for pods and services", severity="warning")
            return
        # The forward would race the teardown/retarget and could spawn
        # against whichever cluster wins — refuse up front.
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch
        if self._forwards is None:
            self.notify("Port-forward unavailable in this build", severity="warning")
            return
        if shutil.which("kubectl") is None:
            self.notify(
                "kubectl not found on PATH — port-forward requires kubectl", severity="error"
            )
            return
        ns, name = self._selected_ns_name()
        if ns is None or name is None:
            return
        kind = self.current_kind
        ports, manifest_ok = await self._forward.prefill_ports(kind, ns, name)
        if kind == "services" and manifest_ok and not ports:
            # A fetched Service with no TCP ports can never be forwarded —
            # kubectl port-forward is TCP-only. (A failed fetch still opens
            # the dialog: the port list is a convenience, not the source of
            # truth, and kubectl gives the authoritative error.)
            self.notify(
                f"{name} declares no TCP ports — kubectl port-forward is TCP-only",
                severity="error",
            )
            return

        def _on_result(result: tuple[int, int] | None) -> None:
            if result is None:
                return
            if self._ctx_switching or epoch != self._ctx_epoch:
                # The dialog stayed open across a context switch (or the
                # port prefill awaited through one): the selection belongs
                # to the old cluster while kubectl and the reopened forward
                # registry now target the new one.
                self.notify(
                    f"port-forward to {name} cancelled - the kube context"
                    " changed while the dialog was open",
                    severity="warning",
                )
                return
            self.run_worker(
                self._forward.start(
                    kind, ns, name, local_port=result[0], remote_port=result[1], epoch=epoch
                )
            )

        await self.push_screen(
            PortForwardScreen(f"{kind}/{ns}/{name}", ports, restrict_remote=kind == "services"),
            _on_result,
        )

    #: Pod controller kinds a re-attach can follow, mapped to their plural.
    #: ReplicaSets are chased one level up so the forward survives rollouts,
    #: not just single pod replacements.

    # -- File transfer (issue #47): download/upload over the exec API as a
    # -- tar stream; uploads are approval-gated, both directions audited
    # -- fail-closed.

    def action_transfer(self) -> None:
        """Open the ctrl+t transfer dialog for the selected pod."""
        if self.current_kind != "pods":
            self.notify("File transfer is only available for pods", severity="warning")
            return
        if self._open_pod_exec is None:
            self.notify("File transfer unavailable (no cluster connection)", severity="warning")
            return
        if self._transfer_in_flight:
            self.notify("A transfer is already in progress", severity="warning")
            return
        # The stream would race the teardown/retarget and could address
        # whichever cluster wins — refuse up front.
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch

        ns, name = self._selected_ns_name()
        if ns is None or name is None:
            return
        namespace = ns

        summary = self._find_pod(namespace, name)
        containers = summary.containers if summary is not None else ()
        # Bind the transfer to this pod *incarnation*: the exec API addresses
        # the pod by namespace/name only, so the uid is re-verified in
        # _run_transfer right before streaming (a same-named replacement
        # created while the dialogs are open must never receive the bytes).
        uid = (summary.uid or None) if summary is not None else None
        if len(containers) > 1:

            def _on_pick(container: str | None) -> None:
                if container is not None:
                    self._open_transfer_dialog(namespace, name, container, uid, epoch)

            self.push_screen(PickScreen(f"Container in {name}:", list(containers)), _on_pick)
            return
        self._open_transfer_dialog(
            namespace, name, containers[0] if containers else None, uid, epoch
        )

    def _open_transfer_dialog(
        self, namespace: str, name: str, container: str | None, uid: str | None, epoch: int
    ) -> None:
        target = f"{namespace}/{name}" + (f" ({container})" if container else "")

        def _on_spec(spec: TransferSpec | None) -> None:
            if spec is not None:
                self._start_transfer(namespace, name, container, spec, uid, epoch)

        self.push_screen(
            TransferScreen(
                target,
                remote_lister=self._remote_lister(namespace, name, container, uid, epoch),
            ),
            _on_spec,
        )

    def _remote_lister(
        self, namespace: str, name: str, container: str | None, uid: str | None, epoch: int
    ) -> Callable[[str], Awaitable[list[RemoteEntry]]] | None:
        """Directory-listing callable for the ctrl+o remote path picker.

        A read-only `ls` over the exec API (issue #124): names only, so the
        masking pipeline does not apply, and it is never exposed to the
        agent — browsing is user-driven like the transfer itself.

        Bound to the dialog's context *epoch* and pod *uid*: a :ctx switch
        retargets the shared exec client, and a same-named replacement pod
        does not change the epoch — either way the listing would come from
        somewhere other than what the dialog shows. Raises TransferError
        (the picker's degradation path) when either binding is stale,
        checked before each exec and again after the await so a listing
        that raced the change is discarded.
        """
        open_pod_exec = self._open_pod_exec
        if open_pod_exec is None:
            return None

        async def _guard() -> None:
            def check_epoch() -> None:
                if self._ctx_switch_crossed(epoch):
                    raise TransferError(
                        f"the kube context changed while the dialog for {namespace}/{name} was open"
                    )

            check_epoch()
            await self._verify_listing_pod(namespace, name, uid)
            # The uid lookup awaited the manifest source: a switch that
            # completed during that await retargeted the shared exec client,
            # so re-check before any exec follows.
            check_epoch()

        async def _list(path: str) -> list[RemoteEntry]:
            await _guard()

            def open_exec(
                command: list[str], stdin: bool
            ) -> contextlib.AbstractAsyncContextManager[Any]:
                return open_pod_exec(namespace, name, container, command, stdin=stdin)

            entries = await list_remote_dir(open_exec, path)
            # Re-check: a listing that raced a switch or a pod replacement
            # must never be presented under the old selection.
            await _guard()
            return entries

        return _list

    async def _verify_listing_pod(self, namespace: str, name: str, uid: str | None) -> None:
        """Raise TransferError unless pod `uid` is still the incarnation the
        transfer dialog was opened for. Fails open when no uid was captured
        (matching the transfer's own uid gate in ui/transfer.py); with a
        captured uid an unverifiable lookup fails closed — browsing is
        optional, so degrading to manual entry beats listing a same-named
        replacement pod."""
        if uid is None:
            return
        try:
            current = await self._target_uid("pods", namespace, name)
        except ApiStatusError as exc:
            raise TransferError(f"pod {name} no longer exists") from exc
        if current is None:
            raise TransferError(f"pod {name} could not be verified — enter the path manually")
        if current != uid:
            raise TransferError(f"pod {name} was replaced since the dialog was opened")

    def _start_transfer(
        self,
        namespace: str,
        name: str,
        container: str | None,
        spec: TransferSpec,
        uid: str | None,
        epoch: int,
    ) -> None:
        """Gate then launch: uploads write into the container filesystem, so
        they are blocked in read-only mode and pass the approval dialog."""
        if self._ctx_switching or epoch != self._ctx_epoch:
            # The picker/transfer dialogs stayed open across a context
            # switch: the pod selection (and its uid, which fails open when
            # missing) belongs to the old cluster while the shared exec
            # client now targets the new one.
            self.notify(
                f"transfer to {namespace}/{name} cancelled - the kube context"
                " changed while the dialog was open",
                severity="warning",
            )
            return
        if spec.direction == "upload":
            if self.config.readonly:
                self.notify("Upload disabled in read-only mode", severity="warning")
                return

            def _approved(approved: bool | None) -> None:
                if not approved:
                    return
                if self._ctx_switching or epoch != self._ctx_epoch:
                    self.notify(
                        f"transfer to {namespace}/{name} cancelled - the kube"
                        " context changed while the approval was open",
                        severity="warning",
                    )
                    return
                self.run_worker(
                    self._writes.reserved(
                        lambda: self._run_transfer(namespace, name, container, spec, uid)
                    )
                )

            self.push_screen(
                self._writes.confirm_screen(
                    f"Upload file to {namespace}/{name}",
                    f"{spec.local_path} → {container or 'pod'}:{spec.remote_path}\n"
                    "This writes into the container filesystem.",
                ),
                _approved,
            )
            return
        self.run_worker(
            self._writes.reserved(lambda: self._run_transfer(namespace, name, container, spec, uid))
        )

    async def _run_transfer(
        self,
        namespace: str,
        name: str,
        container: str | None,
        spec: TransferSpec,
        uid: str | None,
    ) -> None:
        """Delegate to the transfer controller.

        Callers wrap this in `WriteCoordinator.reserved`, which counts the
        transfer as an in-flight cluster write so `:ctx` switching refuses
        while it runs.
        """
        await self._transfer.run(namespace, name, container, spec, uid)

    async def _show_transfer_progress(self, label: str) -> TransferProgressScreen:
        """Widget access stays here: the controller only sees the protocol."""
        screen = TransferProgressScreen(label)
        await self.push_screen(screen)
        return screen

    def _close_transfer_progress(self, screen: TransferProgress) -> None:
        if self.screen is screen:  # type: ignore[comparison-overlap]  # protocol narrows the Screen type away
            self.pop_screen()

    def on_transfer_cancel_requested(self, message: TransferCancelRequested) -> None:
        message.stop()
        self._transfer.cancel()

    @property
    def _transfer_in_flight(self) -> bool:
        """True from transfer-worker launch through the outcome audit; a
        single task slot only works with one transfer at a time, so a
        second launch is refused for its whole lifecycle (issue #47)."""
        return self._transfer.in_flight

    @property
    def _transfer_task(self) -> asyncio.Task[int] | None:
        """The in-flight transfer stream; the progress screen's escape
        cancels it (never the surrounding worker)."""
        return self._transfer.task

    async def _pod_uid_unchanged(
        self, namespace: str, name: str, approved_uid: str, *, action: str
    ) -> bool:
        """Re-verify the approved pod incarnation just before `action`
        executes; notifies and returns False when the pod is gone or replaced."""
        try:
            current_uid = await self._target_uid("pods", namespace, name)
        except ApiStatusError:
            self.notify(
                f"{action} cancelled - pod {name} no longer exists.",
                severity="warning",
            )
            return False
        if current_uid is not None and current_uid != approved_uid:
            self.notify(
                f"{action} cancelled - pod {name} was replaced since the prompt was shown.",
                severity="warning",
            )
            return False
        return True

    def _offer_pull_retry(
        self,
        namespace: str,
        name: str,
        container: str | None,
        approved_uid: str | None,
        image: str,
        reason: str,
    ) -> None:
        """Offer an immediate retry with the fallback image after a pull failure.

        Air-gapped guard: when `debug.images` is configured without a
        `debug.default_image`, no public busybox is offered - notify only.
        """
        if self.config.debug_images is not None and not self.config.debug_default_image:
            fallback = None
        else:
            fallback = self.config.debug_default_image or FALLBACK_IMAGE
        target = f"{name}/{container}" if container else name
        # Equivalent references (untagged vs :latest) would retry the very
        # image that just failed - and each retry permanently adds another
        # ephemeral container entry to the pod spec.
        if fallback is None or same_image_ref(fallback, image) or len(self.screen_stack) > 1:
            self.notify(
                f"kubectl debug: image pull failed for {image} ({reason})",
                severity="error",
            )
            return

        def _on_choice(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(
                    self._shell.run_debug(namespace, name, container, approved_uid, fallback)
                )

        self.push_screen(
            self._writes.confirm_screen(
                f"Image pull failed for {image} ({reason})",
                f"Retry kubectl debug on {target}{write_locus(namespace)} with"
                f" {fallback}? Note: the failed ephemeral container entry cannot be"
                " removed from the pod spec; the retry attaches an additional"
                " container.",
            ),
            _on_choice,
        )

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

    def _selected_ns_name(self) -> tuple[str | None, str | None]:
        """Return (namespace, name) of the currently selected row or (None, None) + warn."""
        table = self._focused_table()
        if table.row_count == 0:
            self.notify("No resource selected", severity="warning")
            return None, None

        row_index = table.cursor_row
        ordered = table.ordered_rows
        if row_index >= len(ordered):
            self.notify("No resource selected", severity="warning")
            return None, None

        row_key = str(ordered[row_index].key.value)
        parts = row_key.split("/", 1)
        if len(parts) != 2:
            self.notify("Cannot determine resource from selection", severity="warning")
            return None, None
        return parts[0], parts[1]

    def _get_pod_containers(self, namespace: str, name: str) -> tuple[str, ...]:
        """Return container names for pod from the store, or empty tuple if not found."""
        summary = self._find_pod(namespace, name)
        return summary.containers if summary is not None else ()

    def _find_pod(self, namespace: str, name: str) -> PodSummary | None:
        """The store's PodSummary for the selection, or None once it is gone."""
        for obj in self.store.get(self.current_kind, self.current_scope):
            if obj.namespace == namespace and obj.name == name and isinstance(obj, PodSummary):
                return obj
        return None

    # -- Write operations (issue #16): every path goes through a ConfirmScreen
    # -- confirmed only by a user keystroke; executed writes are audited.

    #: Workload eligibility, owned by `ResourceWriteController` (the flows
    #: that enforce it) and re-exported here because `_ACTION_VIEWS` and the
    #: agent write ops gate the same identities. Keyed on (group, plural): a
    #: custom-group CRD whose plural collides with a built-in (e.g.
    #: 'deployments') must never be treated as an apps/* workload.
    _RESTARTABLE: ClassVar[frozenset[tuple[str, str]]] = RESTARTABLE
    _SCALABLE: ClassVar[frozenset[tuple[str, str]]] = SCALABLE

    def _selected_uid(self, ns: str | None, name: str) -> str | None:
        """Uid of the selected row's object from the store, binding an
        approval to the exact incarnation on screen; None when the summary
        type carries no uid (the write then runs without a precondition)."""
        for obj in self.store.get(self.current_kind, self.current_scope):
            if obj.namespace == (ns or "") and obj.name == name:
                uid = str(getattr(obj, "uid", "") or "")
                return uid or None
        return None

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

    def _helm_view_guard(self, meta: ResourceMeta, what: str) -> bool:
        """`check_action` gates only key dispatch (issue #114); a direct
        action call must not trust the focused row of an unrelated view as a
        Helm release/revision name and open a write flow with it."""
        current = self.aliases.get(self._canonical_kind(self.current_kind))
        if current is not None and (current.group, current.plural) == (meta.group, meta.plural):
            return True
        self.notify(f"{what} is only available on the {meta.plural} view", severity="warning")
        return False

    def action_helm_install(self) -> None:
        """i on the helm browser: start the chart install wizard (issue #31).
        Synchronous: the picker must open with the keypress, before any
        buffered navigation input could change the view the namespace is
        derived from."""
        if not self._helm_view_guard(HELM_RELEASES_META, "Helm install"):
            return
        self._helm_ctl.install()

    def action_helm_upgrade(self) -> None:
        """u on the helm browser: upgrade the selected release (issue #31).
        Synchronous: the picker opens with the keypress; buffered cursor
        keys must not retarget the upgrade."""
        if not self._helm_view_guard(HELM_RELEASES_META, "Helm upgrade"):
            return
        self._helm_ctl.upgrade()

    async def action_helm_history(self) -> None:
        """h on the helm release browser: the flat revision drill-down.
        Revision history moved off Enter when Enter became the hierarchy
        tree (issue #120); rollback keeps working from the revisions view."""
        if not self._helm_view_guard(HELM_RELEASES_META, "Helm history"):
            return
        namespace, name = self._selected_ns_name()
        if namespace is None or name is None:
            return
        error = await self._workspace_ctl.drill_into(namespace, name)
        if error is not None:
            self.notify(error, severity="warning")

    def action_helm_rollback(self) -> None:
        """r on the helm revision drill-down: roll the release back to the
        selected revision (issue #31). Target captured synchronously with
        the keypress; only the slow preview + confirmation run in a worker
        (the diff render can block for up to _HELM_PREVIEW_TIMEOUT and must
        not freeze the message pump, or the status-bar progress could never
        paint)."""
        if not self._helm_view_guard(HELM_REVISIONS_META, "Helm rollback"):
            return
        helm = self._helm_ctl.gate()
        if helm is None:
            return
        epoch = self._ctx_epoch
        ns, name = self._selected_ns_name()
        if name is None:
            return
        row = self._helm_ctl.revision_row(ns, name)
        if row is None:
            self.notify("no helm revision selected", severity="warning")
            return
        namespace = ns or row.namespace
        self.run_worker(
            self._helm_ctl.rollback(helm, row, ns, name, namespace, epoch),
            exclusive=True,
            group="helm-write",
        )

    def _helm_uninstall_start(self) -> None:
        """Ctrl+D on the helm release browser: uninstall the selected release
        (issue #117). Target captured synchronously with the keypress; the
        slow dry-run preview + confirmation run in a worker, exactly like
        rollback."""
        helm = self._helm_ctl.gate()
        if helm is None:
            return
        epoch = self._ctx_epoch
        ns, name = self._selected_ns_name()
        if name is None:
            return
        row = self._helm_ctl.release_row(ns, name)
        if row is None:
            self.notify("no helm release selected", severity="warning")
            return
        namespace = ns or row.namespace
        self.run_worker(
            self._helm_ctl.uninstall(helm, row, ns, name, namespace, epoch),
            exclusive=True,
            group="helm-write",
        )

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
        """I: on the operator catalog, install the selected package (wizard,
        then approval with the full Subscription manifest); on InstallPlans,
        approve a pending manual plan. Everything offered comes from the
        cluster's own catalog objects - no hardcoded operator knowledge."""
        if self._write_ops is None:
            self.notify("Install unavailable in this session", severity="warning")
            return
        target = self._writes.write_target()
        if target is None:
            return
        meta, ns, name, uid = target
        if (meta.group, meta.plural) == (PACKAGES_GROUP, "packagemanifests"):
            await self._olm.install(meta, ns, name, uid)
        elif (meta.group, meta.plural) == (OPERATORS_GROUP, "installplans"):
            await self._olm.approve_plan(meta, ns, name, uid)
        else:
            self.notify(
                f"Install/Approve does not apply to {gvr_label(meta)}"
                " (use it on packagemanifests or installplans)",
                severity="warning",
            )

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
        # create_provider may return None (unknown provider, missing base_url/
        # model) while agent_enabled is still true in config.
        label = "AI on" if self._agent_ui.runtime is not None else "AI off"
        if self._agent_ui.runtime is not None and self._agent_ui.blocked_in_protected():
            label = "AI blocked"
        mcp_label = self._mcp.status() if self._mcp is not None else ""
        if mcp_label and self._mcp is not None and self._mcp.running and self._mcp_follow:
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
                proposals_label=self._proposals_label(),
                protected=self._writes.protected_context is not None,
            )
        except NoMatches:
            return  # StatusBar unmounted during teardown

    def _proposals_label(self) -> str:
        """Status-bar text for pending external write proposals (issue #110):
        a persistent, non-modal indicator naming source and target."""
        store = self._proposal_store
        if store is None:
            return ""
        pending = store.pending()
        if not pending:
            return ""
        p = pending[0]  # oldest — the next proposal a review would surface
        source = p.client_name or "mcp"
        target = f"{p.action} {p.kind}/{p.name}"
        if len(pending) == 1:
            return f"1 proposal from {source}: {target} — :proposals"
        return f"{len(pending)} proposals (next from {source}: {target}) — :proposals"

    def on_external_proposals_changed(self, message: ExternalProposalsChanged) -> None:
        self._refresh_status()

    async def on_external_proposal_expired(self, message: ExternalProposalExpired) -> None:
        """Audit a proposal the lazy TTL sweep expired: it reached a terminal
        state like any other and must not vanish from the audit trail."""
        await self._audit_proposal_outcome(message.proposal, "expired", message.reason)

    def _subscribe_proposal_updates(self, store: ProposalStore) -> None:
        """Keep the pending indicator live: post_message is loop-safe, so the
        callback may fire from the MCP server's task (or any thread) and
        never touches widgets directly."""

        def _proposals_changed() -> None:
            self.post_message(ExternalProposalsChanged())

        def _proposal_expired(proposal: WriteProposal, reason: str) -> None:
            self.post_message(ExternalProposalExpired(proposal, reason))

        store.subscribe(_proposals_changed)
        store.set_on_expired(_proposal_expired)

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
    # loop logic in AgentRuntime.
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

    async def agent_submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        """External MCP write proposal intake (issue #110).

        Validates exactly like the direct agent write path (kind, RBAC, UID
        capture, dry-run preview) but instead of opening a dialog it queues
        an immutable proposal for later user review — no modal, no focus
        steal. Returns the proposal id; the caller polls
        ``get_write_proposal`` for the terminal outcome.
        """
        store = self._proposal_store
        if store is None:
            return "ERROR: external write proposals are not enabled"
        name = name.strip()
        stamp = restart_stamp()
        # Intake is validated against exactly one context: snapshot it here
        # and recheck before queueing — RBAC, UID capture and the preview all
        # await, and a context switch landing mid-intake would otherwise
        # stamp old-context validation onto a new-context proposal.
        epoch = self._ctx_epoch
        context = self.config.kube_context
        built = self._agent_ui.build_write_op(
            action, kind, name, namespace, replicas, resources, restarted_at=stamp
        )
        if isinstance(built, str):
            return built
        meta, ns, _op, operation, _detail = built
        arguments_json = json.dumps(
            {
                "action": action,
                "kind": kind.strip().lower(),
                "name": name,
                "namespace": ns,
                "replicas": replicas,
                "resources": resources,
                "restarted_at": stamp,
            }
        )
        # Untrusted-input bound: enforced before any cluster I/O (the store
        # atomically rechecks it at submit) so an oversized payload cannot
        # force an RBAC round trip, UID lookup, or server dry-run.
        if len(arguments_json) > store.max_argument_chars:
            return (
                f"ERROR: proposal arguments exceed {store.max_argument_chars}"
                " characters; the proposal was not queued"
            )
        if not await self._writes.permitted(action, meta, ns, name):
            verb, target = self._writes.perm_target(action, meta)
            return f"ERROR: missing permission: {verb} {target}"
        try:
            uid = await self._target_uid(kind.strip().lower(), ns, name)
        except ApiStatusError:
            return f"ERROR: {gvr_label(meta)}/{name} not found{write_locus(ns)}"
        if uid is None:
            # The interactive path fails open here (a user is watching);
            # an external proposal without a UID binding could mutate a
            # same-named replacement, so it must fail closed instead.
            return (
                "ERROR: could not verify the write target (UID capture"
                " failed); the proposal was not queued — try again"
            )
        preview = await self._agent_ui.preview_for_action(
            action, meta, ns, name, replicas, resources, uid, stamp
        )
        if self._ctx_switching or self._ctx_epoch != epoch or self.config.kube_context != context:
            return (
                "ERROR: the kube context changed while the proposal was being"
                " validated; the proposal was not queued — try again"
            )
        try:
            proposal = store.submit(
                action=action,
                group=meta.group,
                version=meta.version,
                kind=meta.plural,
                namespace=ns,
                name=name,
                arguments_json=arguments_json,
                uid=uid,
                context=context,
                context_epoch=epoch,
                summary=operation,
                preview=tuple(preview or ()),
                session_id=session_id,
                client_name=client_name,
                client_version=client_version,
            )
        except (ProposalClosedError, ProposalLimitError, ProposalTooLargeError) as exc:
            return f"ERROR: {exc}"
        source = client_name or "an external MCP caller"
        self.notify(
            f"Write proposal from {source}: {operation} — review with :proposals",
            severity="warning",
            timeout=10,
        )
        ttl = max(0, int(proposal.expires_at - proposal.created_at))
        return (
            f"proposal {proposal.id} is pending user review in the TUI"
            f" (expires in {ttl}s if unreviewed); poll get_write_proposal"
            " for the outcome"
        )

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        """Terminal-outcome lookup for an external write proposal."""
        store = self._proposal_store
        if store is None:
            return "ERROR: external write proposals are not enabled"
        found = store.get(proposal_id)
        if found is None:
            return "ERROR: unknown proposal id"
        proposal, state, reason = found
        line = f"proposal {proposal.id}: {state} — {proposal.summary}"
        return f"{line} ({reason})" if reason else line

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        """Caller-initiated cancel; only the submitting session may cancel."""
        store = self._proposal_store
        if store is None:
            return "ERROR: external write proposals are not enabled"
        found = store.get(proposal_id)
        if found is not None and store.cancel(proposal_id, session_id=session_id):
            await self._audit_proposal_outcome(found[0], "cancelled", "cancelled by caller")
            return f"proposal {proposal_id} cancelled"
        return "ERROR: proposal not found, not pending, or owned by another session"

    async def _audit_proposal_outcome(
        self, proposal: WriteProposal, state: str, reason: str
    ) -> None:
        """Best-effort audit for a terminal proposal outcome (issue #110).

        These transitions mutate nothing, so a failed append is logged
        instead of blocking — the mutation path itself stays fail-closed in
        `WriteCoordinator.run` (which separately audits executed/failed writes with
        the same proposal provenance). The blocking file append is offloaded
        per audit.py's contract for async contexts.
        """
        await asyncio.to_thread(self._append_proposal_audit, proposal, state, reason)

    def _append_proposal_audit(self, proposal: WriteProposal, state: str, reason: str) -> None:
        """Blocking append of a proposal outcome; call via to_thread."""
        audit = self._audit
        if audit is None:
            return
        try:
            audit.append(
                action=proposal.action,
                kind=proposal.kind,
                group=proposal.group,
                version=proposal.version,
                namespace=proposal.namespace,
                name=proposal.name,
                detail=self._proposal_provenance(proposal),
                outcome=f"proposal {state}: {reason}" if reason else f"proposal {state}",
                # These outcome appends are not serialized with `:ctx`'s
                # set_context: bind the entry to the proposal's own cluster.
                context=proposal.context,
            )
        except Exception:
            logger.warning("could not audit proposal %s outcome %s", proposal.id, state)

    @staticmethod
    def _proposal_provenance(proposal: WriteProposal) -> str:
        """Field-injection-safe audit provenance for an external proposal.

        `client_name`/`client_version` are untrusted MCP client metadata:
        non-printables are stripped at intake, but spaces and `=` could
        still forge field-looking tokens (`session=forged`). Quote them
        (shell-style, so benign values stay bare) so every `key=` field in
        the detail is korvid's own.
        """
        caller = shlex.quote(proposal.client_name or "unknown")
        version = (
            f" version={shlex.quote(proposal.client_version)}" if proposal.client_version else ""
        )
        return (
            f"source=external_mcp proposal={proposal.id}"
            f" caller={caller}{version} session={proposal.session_id}"
        )

    async def _expire_proposals_audited(self, reason: str) -> None:
        """Expire every pending proposal and audit each terminal outcome."""
        store = self._proposal_store
        if store is None:
            return
        for proposal in store.expire_all(reason=reason):
            await self._audit_proposal_outcome(proposal, "expired", reason)

    def _open_proposal_review(self) -> None:
        """`:proposals` — review pending external proposals one at a time."""
        store = self._proposal_store
        if store is None:
            self.notify(
                "External write proposals are disabled (set mcp.write_proposals: true)",
                severity="warning",
            )
            return
        if not store.pending():
            self.notify("No pending write proposals")
            return
        # Never *replace* a live review worker (exclusive=True would cancel
        # it): once a proposal is claimed, cancellation could interrupt
        # `WriteCoordinator.run` mid-mutation and strand the record as `approved`
        # with an uncertain cluster outcome. Duplicate opens are refused.
        if any(w.group == "proposal-review" and not w.is_finished for w in self.workers):
            self.notify("A proposal review is already open", severity="warning")
            return
        self.run_worker(self._review_proposals(store), group="proposal-review")

    async def _review_proposals(self, store: ProposalStore) -> None:
        """Review pending proposals oldest-first until none remain or the
        user dismisses the dialog (a dismissed proposal stays pending)."""
        while True:
            pending = store.pending()
            if not pending:
                self._refresh_status()
                return
            if not await self._review_one_proposal(store, pending[0]):
                self._refresh_status()
                return

    async def _review_one_proposal(self, store: ProposalStore, proposal: WriteProposal) -> bool:
        """Re-validate and put one proposal in front of the user; False stops
        the review loop (dismissal), True moves on to the next proposal."""
        if proposal.context_epoch != self._ctx_epoch or (
            proposal.context != self.config.kube_context
        ):
            await self._resolve_proposal_audited(
                store, proposal, "expired", "kube context changed since submission"
            )
            return True
        rebuilt = self._rebuild_proposal_op(proposal)
        if isinstance(rebuilt, str):
            await self._resolve_proposal_audited(
                store, proposal, "expired", rebuilt.removeprefix("ERROR: ")
            )
            return True
        meta, ns, op, operation, _detail = rebuilt
        if not await self._writes.permitted(proposal.action, meta, ns, proposal.name):
            await self._resolve_proposal_audited(
                store, proposal, "failed", "permission revoked since submission"
            )
            return True
        # The awaited SSAR can be slow: re-read state and context before
        # surfacing the dialog. The proposal may have been cancelled or
        # expired meanwhile, and a `:ctx` switch begun in flight owns its
        # fate (the switch's sweep expires old-context proposals) — never
        # put an already-invalid proposal in front of the user.
        found = store.get(proposal.id)
        if found is None or found[1] != "pending":
            return True
        if self._ctx_switching or proposal.context_epoch != self._ctx_epoch:
            return False
        source = proposal.client_name or "external MCP caller"
        title = (
            f"External proposal from {source}: {proposal.action}"
            f" {gvr_label(meta)}/{proposal.name}{write_locus(ns)}"
        )
        require = proposal.name if proposal.action == "delete" and not meta.namespaced else None
        decision = await self._await_proposal_decision(
            title,
            self._proposal_dialog_body(proposal, operation),
            require_name=require,
            preview=list(proposal.preview),
        )
        if decision == "dismissed":
            return False
        if decision == "declined":
            await self._resolve_proposal_audited(store, proposal, "denied", "denied by user")
            return True
        await self._execute_proposal(store, proposal, meta, ns, op)
        return True

    async def _await_proposal_decision(
        self,
        title: str,
        operation: str,
        *,
        require_name: str | None,
        preview: list[str] | None,
    ) -> Literal["approved", "declined", "dismissed"]:
        """One ConfirmScreen decision for a proposal; only real key input can
        resolve it. Unlike agent writes this is user-initiated (:proposals),
        so there is no panel gate — but never stack over another dialog where
        a stray keystroke could approve, and treat an unanswered dialog as a
        dismissal (the proposal stays pending until its own TTL)."""
        if len(self.screen_stack) != 1:
            self.notify("Close the current dialog, then run :proposals again", severity="warning")
            return "dismissed"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool | None] = loop.create_future()

        def _done(confirmed: bool | None) -> None:
            if not fut.done():
                fut.set_result(confirmed)

        screen = self._writes.confirm_screen(
            title, operation, require_name=require_name, preview=preview
        )
        await self.push_screen(screen, _done)
        try:
            confirmed = await asyncio.wait_for(fut, timeout=_APPROVAL_TIMEOUT)
        except TimeoutError:
            if self.screen is screen:
                with contextlib.suppress(Exception):
                    self.pop_screen()
            return "dismissed"
        if confirmed is None:  # Esc: no decision was made
            return "dismissed"
        return "approved" if confirmed else "declined"

    def _rebuild_proposal_op(
        self, proposal: WriteProposal
    ) -> tuple[ResourceMeta, str | None, Callable[[str | None], Awaitable[None]], str, str] | str:
        """Rebuild the write operation from the proposal's immutable
        arguments at review time: readonly mode, audit availability, kind
        resolution and argument validation are all rechecked — the stored
        record never carries an executable closure."""
        try:
            args = json.loads(proposal.arguments_json)
        except ValueError:
            return "ERROR: proposal arguments are unreadable"
        return self._agent_ui.build_write_op(
            args["action"],
            args["kind"],
            args["name"],
            args["namespace"],
            args["replicas"],
            args["resources"],
            restarted_at=args["restarted_at"],
        )

    async def _resolve_proposal_audited(
        self, store: ProposalStore, proposal: WriteProposal, state: ProposalState, reason: str
    ) -> None:
        """Resolve a pending proposal and audit the terminal outcome."""
        if store.resolve(proposal.id, state, reason=reason):
            await self._audit_proposal_outcome(proposal, state, reason)

    def _proposal_dialog_body(self, proposal: WriteProposal, operation: str) -> str:
        """The dialog body for a proposal: the operation plus the immutable
        safety bindings issue #110 requires the user to see before approval —
        the caller (explicitly untrusted metadata), the bound kube context
        and epoch, the bound target UID, and the expiry."""
        caller = proposal.client_name or "unknown"
        version = f" {proposal.client_version}" if proposal.client_version else ""
        remaining = max(0, int(proposal.expires_at - time.monotonic()))
        return "\n".join(
            (
                operation,
                "",
                f"caller (untrusted metadata): {caller}{version}",
                f"bound kube context: {proposal.context or '(default)'}"
                f" (epoch {proposal.context_epoch})",
                f"bound target uid: {proposal.uid}",
                f"expires in {remaining}s unless approved or denied",
            )
        )

    async def _fail_proposal(
        self, store: ProposalStore, proposal: WriteProposal, meta: ResourceMeta, reason: str
    ) -> None:
        """Record and audit a pre-write failure of a claimed proposal."""
        store.finish_execution(proposal.id, executed=False, reason=reason)
        await self._audit_proposal_outcome(proposal, "failed", reason)
        self.notify(
            f"Proposal {proposal.action} {meta.plural}/{proposal.name} failed: {reason}",
            severity="error",
        )

    async def _execute_proposal(
        self,
        store: ProposalStore,
        proposal: WriteProposal,
        meta: ResourceMeta,
        ns: str | None,
        op: Callable[[str | None], Awaitable[None]],
    ) -> None:
        """Claim and execute an approved proposal under the nav lock so a
        context switch or `:mcp off` cannot interleave: the claim itself is
        linearized with the shutdown/switch expiry sweeps (if one of those
        won while the dialog was open or the lock was contended, there is no
        pending proposal left to claim). After the claim, the context epoch
        and RBAC are rechecked, then the UID binding, then the same
        fail-closed audit-before-mutation path as every other write."""
        async with self._workspace_ctl.nav_lock:
            if not store.begin_execution(proposal.id):
                # Cancelled, TTL-expired, or invalidated (MCP shutdown /
                # context switch) before the claim landed: the approval no
                # longer has a pending proposal to execute.
                self.notify("The proposal was withdrawn before approval landed", severity="warning")
                return
            if proposal.context_epoch != self._ctx_epoch or (
                proposal.context != self.config.kube_context
            ):
                await self._fail_proposal(
                    store, proposal, meta, "the kube context changed before execution"
                )
                return
            if not await self._writes.permitted(proposal.action, meta, ns, proposal.name):
                await self._fail_proposal(
                    store, proposal, meta, "permission revoked before execution"
                )
                return
            args = json.loads(proposal.arguments_json)
            try:
                current_uid = await self._target_uid(args["kind"], ns, proposal.name)
            except ApiStatusError:
                await self._fail_proposal(store, proposal, meta, "the target no longer exists")
                return
            if current_uid != proposal.uid:
                await self._fail_proposal(
                    store,
                    proposal,
                    meta,
                    "the target was replaced since the proposal was created",
                )
                return
            detail = self._proposal_provenance(proposal)
            write = asyncio.ensure_future(
                self._writes.run(
                    proposal.action,
                    meta,
                    ns,
                    proposal.name,
                    lambda: op(proposal.uid),
                    detail=detail,
                )
            )
            try:
                outcome = await asyncio.shield(write)
            except asyncio.CancelledError:
                # Worker cancellation (TUI shutdown) after the claim: the
                # record must still reach a terminal state, never a
                # permanent `approved` over an uncertain cluster outcome.
                await self._settle_interrupted_execution(store, proposal, write)
                raise
            store.finish_execution(
                proposal.id, executed=outcome == "done", reason="" if outcome == "done" else outcome
            )
            if outcome == "done":
                self.notify(f"Executed proposal: {proposal.summary}")

    async def _settle_interrupted_execution(
        self, store: ProposalStore, proposal: WriteProposal, write: asyncio.Future[str]
    ) -> None:
        """A claimed execution's worker was cancelled mid-write. Use the
        write's real outcome when it already settled; otherwise abandon the
        in-flight call and record the uncertainty — the API server may or
        may not have committed the mutation by the time cancellation lands.
        """
        if write.done() and not write.cancelled() and write.exception() is None:
            # WriteCoordinator.run already audited this outcome itself.
            outcome = write.result()
            store.finish_execution(
                proposal.id, executed=outcome == "done", reason="" if outcome == "done" else outcome
            )
            return
        write.cancel()
        reason = "interrupted before completion — the cluster outcome is uncertain"
        store.finish_execution(proposal.id, executed=False, reason=reason)
        # WriteCoordinator.run only got as far as its intent record: the terminal
        # outcome must reach the audit trail even while cancellation is
        # unwinding — shield the append so a second cancel cannot skip it
        # (the offloaded thread completes regardless).
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(self._audit_proposal_outcome(proposal, "failed", reason))

    def _provider_footer(self, manifest: dict[str, Any]) -> str | None:
        """Describe footer for the user-triggered views (issue #30); the
        agent's own describe renders the identical note."""
        return provider_footer_note(manifest, self._provider_hint)

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
        # Refuse new foreign UI work and reap in-flight bridge dispatches
        # (issue #165): the MCP server stays live until after run_async()
        # returns, so a request racing teardown could otherwise spawn work
        # (log streams) after the unmount sweeps and leave it alive against
        # an unmounted app.
        await self._bridge_dispatch.shutdown()
        # Cancel any active log stream tasks before the event loop shuts down.
        # A proposal must never outlive the session that previewed it; close
        # the store first so an in-flight submission cannot land after the
        # final sweep.
        if self._proposal_store is not None:
            self._proposal_store.close()
        await self._expire_proposals_audited("the TUI session ended")
        if self._ns_prefetch_task is not None:
            self._ns_prefetch_task.cancel()
        if self._ctx_prefetch_task is not None:
            self._ctx_prefetch_task.cancel()
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


class AppUIBridge(AgentUIBridge):
    """The app's `UIBridge`: `AgentUiController` plus the app's dispatcher.

    The layer-boundary interface must be an `abc.ABC` (AGENTS.md), but
    Textual's `App` metaclass conflicts with `ABCMeta`, so the app cannot
    inherit `UIBridge` directly. The behaviour lives in `AgentUIBridge`; this
    subclass exists only so the composition root can name one bridge for one
    app - it holds no app reference and routes no agent operation through app
    methods.
    """

    def __init__(self, app: KorvidApp) -> None:
        super().__init__(app._agent_ui, app._bridge_dispatch)


class AppAgentPanel(AgentPanelPort):
    """Nominal `AgentPanelPort` adapter over `KorvidApp`'s chat panel.

    Adapter for the same metaclass reason as the other app surfaces. Every
    call is a live widget lookup: the panel is composed only when the [agent]
    extra is wired (issue #73), and `NoMatches` there means "no panel", not
    an error the agent session must handle.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def expanded(self) -> bool:
        panels = self._app.query(AgentPanel)
        return bool(panels) and panels.first(AgentPanel).display

    def show(self) -> None:
        self._app._agent_panel.display = True

    def hide(self) -> None:
        self._app._agent_panel.display = False
        self._app._focused_table().focus()

    def focus_input(self) -> None:
        self._app._agent_panel.query_one("#agent-input").focus()

    def enable_input(self) -> None:
        self._app._agent_panel.query_one("#agent-input").disabled = False

    def set_header(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        estimated: bool,
        profile: str,
    ) -> None:
        self._app._agent_panel.set_header(
            model, input_tokens, output_tokens, estimated=estimated, profile=profile
        )

    def show_setup_hint(self) -> None:
        self._app._agent_panel.show_setup_hint()

    def show_reconnect_hint(self) -> None:
        self._app._agent_panel.show_reconnect_hint()

    def set_stop_key(self, key: str) -> None:
        self._app._agent_panel.stop_key = key

    def interrupt_key(self) -> str:
        """The effective stop key's name, resolved by action from the active
        bindings so an `interrupt_agent` remap moves the advertised hint."""
        for active in self._app.screen.active_bindings.values():
            if active.binding.action == "interrupt_agent":
                return active.binding.key
        return "ctrl+x"

    def begin_turn(self, text: str, *, echo: bool) -> None:
        self._app._agent_panel.begin_turn(text, echo=echo)

    def echo_user(self, text: str) -> None:
        self._app._agent_panel.echo_user(text)

    def apply_event(self, event: AgentEvent) -> None:
        self._app._agent_panel.apply_event(event)


class AppAgentScreens(AgentScreens):
    """Nominal `AgentScreens` adapter over `KorvidApp`'s screen stack.

    The two guards here are security-relevant: an approval dialog (or a
    write-parameter wizard, each of which feeds a cluster write) is confirmed
    only by user keystrokes, and a describe screen the user is reading is
    never covered by an agent- or follow-driven view.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def approval_dialog_active(self) -> bool:
        return isinstance(
            self._app.screen,
            (
                ConfirmScreen,
                ReplicasPrompt,
                ImagePrompt,
                ResizePrompt,
                OperatorInstallPrompt,
                HelmInstallPrompt,
            ),
        )

    def describe_screen_open(self) -> bool:
        return isinstance(self._app.screen, DescribeScreen)

    def top_screen(self) -> object | None:
        stack = self._app.screen_stack
        return stack[-1] if stack else None

    def is_stacked(self, screen: Screen[Any]) -> bool:
        return screen in self._app.screen_stack

    def dismiss_if_current(self, screen: Screen[Any]) -> None:
        if self._app.screen is screen:
            with contextlib.suppress(Exception):
                self._app.pop_screen()

    def selected_row_key(self) -> str | None:
        table = self._app._focused_table()
        if table.row_count == 0:
            return None
        ordered = table.ordered_rows
        if table.cursor_row >= len(ordered):
            return None
        return str(ordered[table.cursor_row].key.value)

    def show_describe_pane(
        self,
        title: str,
        manifest: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        footer_note: str | None,
    ) -> None:
        self._app._describe_pane.show(title, manifest, events, footer_note=footer_note)


class AppProposalOps(AgentProposals):
    """Nominal `AgentProposals` adapter over `KorvidApp`.

    External write proposals (issue #110) keep their store, TTL, review loop
    and execution path on the app for now; the agent session reaches them
    only through this port, so the two can be separated without touching the
    agent's own flows.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    async def submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        return await self._app.agent_submit_write_proposal(
            action,
            kind,
            name,
            namespace,
            replicas,
            resources,
            session_id=session_id,
            client_name=client_name,
            client_version=client_version,
        )

    async def get_write_proposal(self, proposal_id: str) -> str:
        return await self._app.agent_get_write_proposal(proposal_id)

    async def cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        return await self._app.agent_cancel_write_proposal(proposal_id, session_id=session_id)


class AppViewState(ViewState):
    """Nominal `ViewState` adapter over `KorvidApp` (issue #187).

    Textual's `App` metaclass conflicts with `ABCMeta`, so the app conforms
    through an adapter rather than inheriting - the same arrangement as
    `AppUIBridge`. Every method is a live read: a `:ctx` switch rebuilds the
    alias table and the store, and the selection moves constantly, so nothing
    here may be cached by a caller.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def current_kind(self) -> str:
        return self._app._workspace.current_kind

    def current_scope(self) -> str:
        return self._app._workspace.current_scope

    def current_namespace(self) -> str:
        return self._app._workspace.current_namespace

    def canonical_kind(self, kind: str) -> str:
        return self._app._canonical_kind(kind)

    def aliases(self) -> Mapping[str, ResourceMeta]:
        return MappingProxyType(self._app.aliases)

    def resources(self, kind: str, scope: str) -> list[Summary]:
        return self._app.store.get(kind, scope)

    def readonly(self) -> bool:
        return self._app.config.readonly

    def default_namespace(self) -> str | None:
        return self._app.config.namespace

    def selected_ns_name(self) -> tuple[str | None, str | None]:
        return self._app._selected_ns_name()

    def selected_uid(self, namespace: str | None, name: str) -> str | None:
        return self._app._selected_uid(namespace, name)

    def gvr_label(self, meta: ResourceMeta) -> str:
        return gvr_label(meta)

    def write_locus(self, namespace: str | None) -> str:
        return write_locus(namespace)


class AppUiSurface(UiSurface):
    """Nominal `UiSurface` adapter over `KorvidApp` (issue #187).

    Adapter for the same metaclass reason as the others. `run_worker` stays
    the app's, so controller work is supervised and cancelled on shutdown
    rather than left as a bare task.

    It does not make controller work context-safe:
    `_teardown_for_context_switch` cancels the `hint-events`, `relationships`,
    and `timeline-warning-events` groups. Workers in other groups may keep
    running against the cluster they captured, so controllers revalidate
    explicitly through the epoch or `WriteGate.context_intact`.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: Severity = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self._app.notify(message, title=title, severity=severity, timeout=timeout, markup=markup)

    def push_screen(
        self,
        screen: Screen[ScreenResultT],
        callback: Callable[[ScreenResultT | None], None] | None = None,
    ) -> AwaitMount | AwaitComplete:
        return self._app.push_screen(screen, callback)

    def run_worker(
        self,
        work: Awaitable[Any] | Callable[[], Any],
        *,
        exclusive: bool = False,
        group: str = "default",
        name: str = "",
        exit_on_error: bool = True,
        thread: bool = False,
    ) -> Worker[Any]:
        return self._app.run_worker(
            work,
            exclusive=exclusive,
            group=group,
            name=name,
            exit_on_error=exit_on_error,
            thread=thread,
        )

    async def cancel_workers(self, group: str) -> None:
        for worker in self._app.workers.cancel_group(self._app, group):
            with contextlib.suppress(WorkerError):
                await worker.wait()

    def suspend(self) -> contextlib.AbstractContextManager[None]:
        return self._app.suspend()

    def refresh(self) -> None:
        self._app.refresh()

    def call_from_thread(self, callback: Callable[..., Any], *args: Any) -> None:
        self._app.call_from_thread(callback, *args)

    def call_later(self, callback: Callable[..., None], *args: Any) -> None:
        self._app.call_later(callback, *args)

    def progress(self, label: str) -> contextlib.AbstractContextManager[None]:
        return self._app._progress(label)

    def is_current_screen(self, screen: Screen[Any]) -> bool:
        return self._app.screen is screen

    def screen_depth(self) -> int:
        return len(self._app.screen_stack)


class AppContextGuard(ContextGuard):
    """Nominal `ContextGuard` adapter over `KorvidApp` (issue #187).

    Same metaclass reason as the other adapters: the boundary is an
    `abc.ABC`, but Textual's `App` metaclass conflicts with `ABCMeta`, so the
    app conforms through a thin adapter. The `:ctx`-switch epoch, the
    in-flight flag, and the stream-read guard stay the app's single
    implementation, which the workspace controller revalidates against.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def epoch(self) -> int:
        return self._app._ctx_epoch

    def switching(self) -> bool:
        return self._app._ctx_switching

    def reads_allowed(self) -> bool:
        return self._app._ctx_reads_allowed()


class AppWorkspaceSurface(WorkspaceSurface):
    """Nominal `WorkspaceSurface` adapter over `KorvidApp` (issue #187).

    The workspace controller drives the pane tables, the empty-state overlay,
    the describe pane, the focus classes, and the open hierarchy tree only
    through this named surface; `KorvidApp` keeps ownership of the widget tree
    and its construction. Adapter for the same metaclass reason as the others.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def render_table(self, kind: str, *, only: PaneState | None = None) -> None:
        self._app._render_table(kind, only=only)

    def refresh_empty_state(self, kind: str) -> None:
        self._app._refresh_empty_state(kind, self._app._focused_table().row_count)

    def hide_empty_state(self) -> None:
        self._app.query_one("#empty-state", Static).display = False

    async def mount_pane_table(self, pane: PaneState) -> None:
        table = ResourceTable(id=pane.table_id)
        await self._app.query_one("#workspace", Horizontal).mount(table)
        for pane_table in self._app.query(ResourceTable):
            pane_table.add_class("split-pane")

    async def remove_pane_table(self, table_id: str) -> None:
        await self._app.query_one(f"#{table_id}", ResourceTable).remove()

    def unsplit_survivor(self, table_id: str) -> None:
        self._app.query_one(f"#{table_id}", ResourceTable).remove_class("split-pane")

    def focus_table(self, table_id: str) -> None:
        self._app.query_one(f"#{table_id}", ResourceTable).focus()

    def focused_is_table(self) -> bool:
        return isinstance(self._app.focused, ResourceTable)

    def has_tables(self) -> bool:
        return bool(self._app.query(ResourceTable))

    def has_focus(self) -> bool:
        return self._app.focused is not None

    def update_pane_focus_classes(self) -> None:
        self._app._update_pane_focus_classes()

    def focus_row(self, row_key: str) -> bool:
        return self._app._focus_row(row_key)

    def hide_describe(self) -> None:
        self._app._describe_pane.hide()

    def refresh_status(self) -> None:
        self._app._refresh_status()

    def refresh_bindings(self) -> None:
        self._app.refresh_bindings()

    def hierarchy_open(self) -> bool:
        try:
            return isinstance(self._app.screen, HierarchyScreen)
        except ScreenStackError:
            # A ResourcesUpdated dispatched during app teardown can land after
            # the screen stack is emptied (flaky-CI issue #147): no screen
            # simply means no tree to refresh.
            return False

    def update_hierarchy_tree(self, root: Any) -> None:
        screen = self._app.screen
        if isinstance(screen, HierarchyScreen):
            screen.update_tree(root)
