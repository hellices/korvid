"""Textual-app adapters for controller boundary interfaces."""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import (
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
    Sequence,
)
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from textual.app import ScreenStackError
from textual.await_complete import AwaitComplete
from textual.containers import Horizontal
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widget import AwaitMount
from textual.widgets import Input, Static
from textual.widgets.data_table import CellDoesNotExist
from textual.worker import Worker, WorkerError

from korvid.agent.events import AgentEvent
from korvid.agent.interaction import PaneContext, ResourceIdentity
from korvid.core.relationships import SummaryLike
from korvid.core.store import ALL_NAMESPACES, Summary
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import ContainerTrouble, GenericSummary
from korvid.tools.proposals import WriteProposal
from korvid.ui.agent_ui_controller import (
    AgentPanelPort,
    AgentScreens,
    AgentToolUIBridge,
    DisplayedPaneContext,
)
from korvid.ui.context_switch_coordinator import (
    ContextSurface,
    ContextSwitchResult,
    SessionConfiguration,
)
from korvid.ui.messages import (
    ExternalProposalExpired,
    ExternalProposalsChanged,
    ResourcesUpdated,
    SwitchContextCommand,
)
from korvid.ui.proposal_controller import (
    REVIEW_GROUP,
    ProposalEvents,
    ProposalScreens,
    ReviewTasks,
)
from korvid.ui.resource_inspect_controller import InspectSurface
from korvid.ui.transfer import TransferScreens
from korvid.ui.ui_surface import ScreenResultT, Severity, UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.agent_panel import AgentPanel
from korvid.ui.widgets.command_bar import CommandBar
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ImagePrompt, ReplicasPrompt
from korvid.ui.widgets.describe_screen import DescribeScreen
from korvid.ui.widgets.filter_bar import FilterBar
from korvid.ui.widgets.helm_install import HelmInstallPrompt
from korvid.ui.widgets.hierarchy_screen import HierarchyScreen
from korvid.ui.widgets.namespace_picker import NamespacePicker
from korvid.ui.widgets.operator_install import OperatorInstallPrompt
from korvid.ui.widgets.resize_prompt import ResizePrompt
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.workspace_controller import (
    WorkspaceSurface,
)
from korvid.ui.workspace_state import PaneState
from korvid.ui.write_coordinator import (
    gvr_label,
    write_locus,
)

if TYPE_CHECKING:
    from korvid.ui.app import KorvidApp


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


class AppUIBridge(AgentToolUIBridge):
    """The app's `UIBridge`: `AgentUiController` plus the app's dispatcher.

    The layer-boundary interface must be an `abc.ABC` (AGENTS.md), but
    Textual's `App` metaclass conflicts with `ABCMeta`, so the app cannot
    inherit `UIBridge` directly. The behaviour lives in `AgentToolUIBridge`;
    this subclass exists only so the composition root can name one bridge for
    one app - it holds no app reference and routes no agent operation through
    app methods.
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
        tier: str | None = None,
    ) -> None:
        self._app._agent_panel.set_header(
            model, input_tokens, output_tokens, estimated=estimated, tier=tier
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


def _displayed_resource_context(
    identity: ResourceIdentity, *, owner: object | None
) -> DisplayedPaneContext:
    """Build display context for one resource shown by describe."""
    return DisplayedPaneContext(
        context=PaneContext(
            kind=identity.kind,
            scope=identity.namespace or ALL_NAMESPACES,
            filter_pattern=None,
            selected=identity,
        ),
        owner=owner,
    )


def _row_key_at_cursor(table: ResourceTable) -> str | None:
    if table.row_count == 0:
        return None
    row_index = table.cursor_row
    ordered = table.ordered_rows
    if row_index < 0 or row_index >= len(ordered):
        return None
    return str(ordered[row_index].key.value)


class AppAgentScreens(AgentScreens):
    """Nominal `AgentScreens` adapter over `KorvidApp`'s screen stack.

    The two guards here are security-relevant: an approval dialog (or a
    write-parameter wizard, each of which feeds a cluster write) is confirmed
    only by user keystrokes, and a describe screen the user is reading is
    never covered by an agent- or follow-driven view.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app
        self._describe_owner: object | None = None
        self._log_generation: int | None = None
        self._log_uids: dict[tuple[str, str], str | None] = {}

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
        return _row_key_at_cursor(self._app._focused_table())

    def show_describe_pane(
        self,
        title: str,
        manifest: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        footer_note: str | None,
    ) -> None:
        self._describe_owner = self._app._pane
        self._app._describe_pane.show(title, manifest, events, footer_note=footer_note)

    def selected_identity(self, table_id: str, kind: str) -> ResourceIdentity | None:
        """The resource under the cursor in the named pane table.

        Reads the row key from the table widget identified by *table_id*,
        parses the namespace and name from it, then looks up the uid from the
        current store bucket so the identity is complete.  Returns None when
        the table is absent, has no rows, or the cursor is out of range.
        """
        try:
            table = self._app.query_one(f"#{table_id}", ResourceTable)
        except NoMatches:
            return None
        row_key = _row_key_at_cursor(table)
        if row_key is None:
            return None
        # Row keys use the 'namespace/name' composite when namespaced.
        if "/" in row_key:
            namespace, _, name = row_key.partition("/")
        else:
            namespace, name = "", row_key
        # Look up the uid from the live store for the pane's current scope.
        pane_state = next((p for p in self._app._workspace.panes if p.table_id == table_id), None)
        scope = pane_state.scope if pane_state is not None else ""
        uid: str | None = None
        for summary in self._app._view.resources(kind, scope):
            if summary.name == name and (not namespace or summary.namespace == namespace):
                uid = getattr(summary, "uid", None) or None
                break
        return ResourceIdentity(
            kind=kind,
            namespace=namespace or None,
            name=name,
            uid=uid,
        )

    def displayed_pane_context(self) -> DisplayedPaneContext | None:
        """Describe or log target currently shown instead of the table selection."""
        if isinstance(self._app.screen, DescribeScreen):
            identity = self._app.screen.resource_identity
            if identity is not None:
                return _displayed_resource_context(identity, owner=None)
            return DisplayedPaneContext(
                context=PaneContext(
                    kind="unknown",
                    scope=ALL_NAMESPACES,
                    filter_pattern=None,
                    selected=None,
                ),
                owner=None,
            )

        triples = self._app._logs.current_triples if self._app._logs.mode else []
        if triples:
            pods = {(namespace, pod) for namespace, pod, _container in triples}
            generation = int(getattr(self._app._logs, "pane_gen", 0))
            if generation != self._log_generation:
                self._log_generation = generation
                self._log_uids = {
                    (namespace, pod): self._displayed_resource_uid(
                        "pods",
                        namespace,
                        pod,
                        owner=self._app._logs.owner,
                    )
                    for namespace, pod in pods
                }
            namespaces = {namespace for namespace, _pod in pods}
            scope = namespaces.pop() if len(namespaces) == 1 else ALL_NAMESPACES
            selected: ResourceIdentity | None = None
            if len(pods) == 1:
                namespace, pod = pods.pop()
                selected = ResourceIdentity(
                    kind="pods",
                    namespace=namespace,
                    name=pod,
                    uid=self._log_uids.get((namespace, pod)),
                )
            return DisplayedPaneContext(
                context=PaneContext(
                    kind="pods",
                    scope=scope,
                    filter_pattern=None,
                    selected=selected,
                ),
                owner=self._app._logs.owner,
            )

        if self._app._describe_pane.display:
            identity = self._app._describe_pane.resource_identity
            if identity is not None:
                return _displayed_resource_context(identity, owner=self._describe_owner)
        return None

    def _displayed_resource_uid(
        self,
        kind: str,
        namespace: str,
        name: str,
        *,
        owner: object | None,
    ) -> str | None:
        """Resolve the current store UID for a displayed namespaced resource."""
        owner_scope = getattr(owner, "scope", None)
        scopes = dict.fromkeys(
            scope for scope in (owner_scope, namespace, ALL_NAMESPACES) if isinstance(scope, str)
        )
        for scope in scopes:
            for summary in self._app._view.resources(kind, scope):
                if summary.name == name and summary.namespace == namespace:
                    return getattr(summary, "uid", None) or None
        return None


class AppProposalScreens(ProposalScreens):
    """Nominal `ProposalScreens` adapter over `KorvidApp`'s screen stack.

    One method, because the review flow needs one screen action: pop the
    dialog it pushed when an unanswered approval times out. The screen is
    never handed over — a live `Screen` also carries `dismiss` and `app`.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def dismiss_if_current(self, screen: Screen[Any]) -> None:
        if self._app.screen is screen:
            with contextlib.suppress(Exception):
                self._app.pop_screen()


class AppInspectSurface(InspectSurface):
    """Nominal `InspectSurface` adapter over `KorvidApp`'s mounted widgets.

    Adapter for the same metaclass reason as the others. Two widgets: the
    focused table's row cursor, and the ops hint strip. Every call is a live
    lookup and tolerates the widget being gone - a render or a timer
    dispatched during shutdown/teardown can arrive after the tree is
    unmounted, which simply means "no row" or "nothing to clear".
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def cursor_row_key(self) -> str | None:
        try:
            table = self._app._focused_table()
        except NoMatches:  # timer fired while the app is shutting down
            return None
        if table.cursor_row < 0:
            return None
        try:
            key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        except CellDoesNotExist:
            return None
        return None if key is None else str(key.value)

    def show_trouble(
        self, trouble: tuple[ContainerTrouble, ...], *, event: str | None = None
    ) -> None:
        with contextlib.suppress(NoMatches):  # strip unmounted during shutdown
            self._app._hint_strip.show_trouble(trouble, event=event)

    def clear_hint(self) -> None:
        with contextlib.suppress(NoMatches):  # strip unmounted during shutdown
            self._app._hint_strip.clear_hint()


class AppTransferScreens(TransferScreens):
    """Nominal `TransferScreens` adapter over `KorvidApp`'s screen stack.

    One method, because the transfer lifecycle needs one screen action: pop
    the progress modal it pushed, once the stream has ended. The screen is
    never handed over — a live `Screen` also carries `dismiss` and `app`.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def dismiss_if_current(self, screen: Screen[Any]) -> None:
        if self._app.screen is screen:
            self._app.pop_screen()


class AppReviewTasks(ReviewTasks):
    """Nominal `ReviewTasks` adapter: the review loop as an app worker.

    A supervised worker in its own named group, never `exclusive`: replacing
    a live review would cancel a claimed execution mid-mutation, so a
    duplicate `:proposals` is refused against `review_running` instead.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def review_running(self) -> bool:
        return any(w.group == REVIEW_GROUP and not w.is_finished for w in self._app.workers)

    def start_review(self, coro: Coroutine[Any, Any, None]) -> None:
        self._app.run_worker(coro, group=REVIEW_GROUP)


class AppProposalEvents(ProposalEvents):
    """Nominal `ProposalEvents` adapter: store callbacks onto the UI loop.

    The store is shared with the MCP server's thread, so both callbacks may
    fire from anywhere; `post_message` is loop-safe and touches no widget,
    and the app's handlers turn each message into a controller call.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def changed(self) -> None:
        self._app.post_message(ExternalProposalsChanged())

    def expired(self, proposal: WriteProposal, reason: str) -> None:
        self._app.post_message(ExternalProposalExpired(proposal, reason))


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
        table = self._app._focused_table()
        row_key = _row_key_at_cursor(table)
        if row_key is None:
            self._app.notify("No resource selected", severity="warning")
            return None, None
        parts = row_key.split("/", 1)
        if len(parts) != 2:
            self._app.notify("Cannot determine resource from selection", severity="warning")
            return None, None
        return parts[0], parts[1]

    def selected_uid(self, namespace: str | None, name: str) -> str | None:
        """Uid of the selected row's object from the store, binding an
        approval to the exact incarnation on screen; None when the summary
        type carries no uid (the write then runs without a precondition)."""
        for obj in self._app.store.get(self._app.current_kind, self._app.current_scope):
            if obj.namespace == (namespace or "") and obj.name == name:
                uid = str(getattr(obj, "uid", "") or "")
                return uid or None
        return None

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

    def inline_focus_release_hint(self) -> str | None:
        focused = self._app.focused
        if isinstance(focused, CommandBar):
            return "close the command bar using Esc to review"
        if isinstance(focused, FilterBar):
            return "close the filter bar using Esc to review"
        if isinstance(focused, NamespacePicker):
            return "dismiss the namespace picker using Esc to review"
        if isinstance(focused, Input):
            return "leave the active input using Tab to review"
        return None


class AppContextSurface(ContextSurface):
    """Nominal `ContextSurface` adapter over `KorvidApp` (issue #36 / #187).

    Adapter for the same metaclass reason as the others. Everything here is
    a widget the app owns (the command bar's completion words, the describe
    pane, the inline namespace picker), an app worker group, or a UI-bus
    post — no method routes any part of the switch transaction back into the
    app, which is `ContextSwitchCoordinator`'s alone.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def request_switch(self, name: str) -> None:
        self._app.post_message(SwitchContextCommand(name))

    def namespace_picker_open(self) -> bool:
        try:
            return bool(self._app._namespace_picker.display)
        except NoMatches:  # widget tree not composed (shutdown/startup)
            return False

    def hide_describe(self) -> None:
        self._app._describe_pane.hide()

    def set_context_words(self, names: list[str]) -> None:
        self._app._command_bar.context_words = names

    def cancel_worker_group(self, group: str) -> None:
        self._app.workers.cancel_group(self._app, group)

    def refresh_completions(self) -> None:
        self._app.on_aliases_updated()

    def refresh_status(self) -> None:
        self._app._refresh_status()

    def resources_updated(self, kind: str) -> None:
        self._app.post_message(ResourcesUpdated(kind))


class AppSessionConfiguration(SessionConfiguration):
    """Nominal `SessionConfiguration` adapter over `KorvidApp` (issue #36).

    The active context, the session default namespace, the per-cluster
    capability gates and the context-pinned CLI wrappers are app state every
    flow reads directly, so the app keeps them; the coordinator decides
    *when* a proven switch is adopted, and adopts it through here.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    def kube_context(self) -> str | None:
        return self._app.config.kube_context

    def default_namespace(self) -> str:
        return self._app.config.namespace or "default"

    def adopt(
        self,
        context: str | None,
        result: ContextSwitchResult,
        *,
        namespace: str | None = None,
    ) -> None:
        # Adopt the target context's kubeconfig namespace as the session
        # default too: `ns` toggle-back and the helm/operator namespace
        # fallbacks read config.namespace. Recovery may override the adopted
        # namespace with the pre-switch concrete session default; otherwise an
        # unset target namespace materializes Kubernetes' `default`.
        adopted_namespace = (
            namespace if namespace is not None else (result.context_namespace or "default")
        )
        self._app.config = dataclasses.replace(
            self._app.config,
            kube_context=context,
            namespace=adopted_namespace,
        )
        self._app._pod_resize_supported = result.pod_resize_supported
        self._app._provider_hint = result.provider_hint

    def retarget_tools(self, result: ContextSwitchResult) -> None:
        # Rebind the helm wrapper: it pins --kube-context per instance, and
        # helm writes must follow the active cluster (None when helm is off).
        self._app._helm = result.helm
        # The new cluster may run a telepresence traffic-manager the old one
        # lacked: re-probe (a no-op once the session's hint was shown).
        self._app.run_worker(self._app._integrations.maybe_hint_telepresence(), exclusive=False)


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

    def set_namespace_words(self, names: list[str]) -> None:
        self._app._command_bar.namespace_words = names

    def open_namespace_picker(self, names: list[str]) -> None:
        self._app._namespace_picker.open(names)

    def focused_row_key(self) -> str | None:
        return _row_key_at_cursor(self._app._focused_table())

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
        try:
            screen = self._app.screen
        except ScreenStackError:
            return
        if isinstance(screen, HierarchyScreen):
            screen.update_tree(root)
