"""Workspace orchestration: navigation, drill, hierarchy, relationships, panes.

`WorkspaceController` owns the cohesive workflows the split workspace (issue
#48) drives:

- resource-view navigation and namespace/scope transitions (`navigate`,
  `navigate_command`, `toggle_all_namespaces`, `favorite_namespace`), each
  serialized through `_nav_lock` so the keyboard path and the agent path
  never interleave across a watch stop/start;
- filter and per-kind sort transitions;
- the drill down/pop flow with the bounded pre-warm (issue #157) and its
  per-`(kind, scope)` lease accounting, so an overlapping drill never reaps a
  stream a pane or another pre-warm still needs;
- the component-hierarchy tree lookup/open/refresh/return/goto flow (issues
  #120/#135), including the stale-result guard that discards a tree action
  taken across a `:ctx` switch;
- the operational relationship-graph snapshot load/open/goto flow (issue
  #281), run in the `relationships` worker group with `exit_on_error=False`;
- the two-pane split/focus/close/collapse lifecycle and its focus-class,
  hint, status and binding refreshes.

It also owns the workspace-only mutable state these flows mutate — the
navigation lock, the drill pre-warm leases, the open tree's rebuild context,
the hierarchy-goto cursor-poll budget, the render-coalescing set, and the
metrics poller's served target.

Textual is reached only through the narrow `UiSurface`, `WorkspaceSurface`
and `ContextGuard` boundaries plus a handful of typed collaborator ports
(`WatchLifecycle`, `MetricsLifecycle`, `RelationshipLoading`, the log and hint
ports, and the injected read callables). The controller never imports or
holds `KorvidApp`, so every flow here is exercised without a running app.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from time import monotonic
from typing import Any, Protocol

from rich.text import Text

from korvid.core.config import KorvidConfig, ViewConfig
from korvid.core.filters import parse_filter
from korvid.core.relationships import GraphResource
from korvid.core.sorting import SORT_COLUMNS, toggle_sort
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.k8s.components import (
    MAX_COMPONENT_DOCS,
    ComponentRef,
    installplan_components,
    reference_components,
)
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.helm import HELM_RELEASES_META
from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP
from korvid.k8s.relations import drill_child, owned_by
from korvid.ui.navigation import DrillLevel
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.hierarchy_screen import HierarchyScreen, build_hierarchy
from korvid.ui.widgets.relationship_screen import GotoResult, RelationshipScreen
from korvid.ui.workspace_state import HierarchyReturn, PaneState, WorkspaceState

logger = logging.getLogger(__name__)

#: The worker group every operational relationship graph load and its goto
#: follow-up run in (issue #281): cancelled as a unit on a `:ctx` switch, and
#: (with `exit_on_error=False`) reported rather than fatal on failure.
RELATIONSHIP_GROUP = "relationships"

#: Exclusive worker group for the hierarchy tree open and its goto follow-up.
HIERARCHY_GROUP = "hierarchy"

#: Header label -> builtin sort column (issue #138): the reverse of the
#: table's ▲/▼ decoration map, for header-click sorting.
_HEADER_SORT_COLUMNS = {"NAME": "name", "AGE": "age", "CPU": "cpu", "MEM": "mem"}


class WorkspaceSurface(ABC):
    """The Textual widget capabilities the workspace flows drive (issue #187).

    Widget lookup, construction, mounting, removal, focus and the derived
    presentation refreshes stay owned by `KorvidApp` (it owns the widget
    tree); the controller reaches them only through this named surface, so it
    never queries or mutates arbitrary app widgets.
    """

    @abstractmethod
    def render_table(self, kind: str, *, only: PaneState | None = None) -> None:
        """Re-render every pane showing *kind*, or just *only* when given."""

    @abstractmethod
    def refresh_empty_state(self, kind: str) -> None:
        """Refresh the single-pane empty-state overlay from the focused table."""

    @abstractmethod
    def hide_empty_state(self) -> None:
        """Hide the single-pane empty-state overlay (a split has per-pane content)."""

    @abstractmethod
    async def mount_pane_table(self, pane: PaneState) -> None:
        """Construct and mount *pane*'s table widget and mark every table split."""

    @abstractmethod
    async def remove_pane_table(self, table_id: str) -> None:
        """Remove the table widget for *table_id*."""

    @abstractmethod
    def unsplit_survivor(self, table_id: str) -> None:
        """Drop the split-pane decoration from the surviving table."""

    @abstractmethod
    def focus_table(self, table_id: str) -> None:
        """Move keyboard focus to *table_id*'s table."""

    @abstractmethod
    def focused_is_table(self) -> bool:
        """Whether a resource table currently owns keyboard focus (chord arm)."""

    @abstractmethod
    def has_tables(self) -> bool:
        """Whether any resource table is still mounted (shutdown guard)."""

    @abstractmethod
    def has_focus(self) -> bool:
        """Whether any widget currently owns focus."""

    @abstractmethod
    def update_pane_focus_classes(self) -> None:
        """Sync the `focused-pane` marker to the model and refresh bindings."""

    @abstractmethod
    def focus_row(self, row_key: str) -> bool:
        """Move the focused table's cursor to *row_key*; False when absent."""

    @abstractmethod
    def hide_describe(self) -> None:
        """Dismiss the describe pane covering the table."""

    @abstractmethod
    def refresh_status(self) -> None:
        """Repaint the status bar and top-bar legend."""

    @abstractmethod
    def refresh_bindings(self) -> None:
        """Prompt Textual to re-evaluate view-scoped binding visibility."""

    @abstractmethod
    def hierarchy_open(self) -> bool:
        """Whether the top screen is the live hierarchy tree (rebuild guard)."""

    @abstractmethod
    def update_hierarchy_tree(self, root: Any) -> None:
        """Replace the open hierarchy tree's nodes with *root*."""


class ContextGuard(ABC):
    """The `:ctx`-switch epoch/quiesce state the workspace flows revalidate against."""

    @abstractmethod
    def epoch(self) -> int:
        """The current context epoch, captured at the start of a flow."""

    @abstractmethod
    def switching(self) -> bool:
        """Whether a context switch is tearing down / retargeting right now."""

    @abstractmethod
    def reads_allowed(self) -> bool:
        """Whether a read that spawns a cluster stream may start (notifies if not)."""

    def crossed(self, epoch: int) -> bool:
        """True when a switch started or completed since *epoch* was captured."""
        return self.switching() or epoch != self.epoch()


class WatchLifecycle(Protocol):
    """The watch start/stop surface the workspace flows need (structural)."""

    @property
    def active(self) -> set[tuple[str, str]]: ...

    async def start(self, kind: str, scope: str) -> None: ...

    async def stop(self, kind: str, scope: str) -> None: ...


class MetricsLifecycle(Protocol):
    """The metrics poller start/stop surface (structural)."""

    async def start(self, namespace: str | None) -> None: ...

    async def stop(self) -> None: ...


class RelationshipLoading(Protocol):
    """The bounded relationship-snapshot loader (structural)."""

    async def load(
        self, root: GraphResource, namespace: str | None, aliases: Mapping[str, ResourceMeta]
    ) -> Any: ...


class WorkspaceLogs(Protocol):
    """The log-pane teardown the navigation and close flows trigger."""

    async def close_if_owned_by(self, pane: object) -> None: ...


class WorkspaceHints(Protocol):
    """The pods hint-strip refresh the focus flows trigger."""

    def refresh_for_focus(self) -> None: ...


class KeyEvent(Protocol):
    """The subset of a Textual key event the pane chord consumes.

    `stop`/`prevent_default` mirror `textual.events.Key`'s own signatures
    (an optional bool, a `Message` return) so the real event satisfies this
    structurally while a test can pass a lightweight fake.
    """

    key: str

    def stop(self, stop: bool = ...) -> Any: ...

    def prevent_default(self, prevent: bool = ...) -> Any: ...


class WorkspaceController:
    """Owns the workspace workflows and the workspace-only mutable state."""

    #: Longest a drill transition waits for the target view's initial LIST
    #: before switching anyway (issue #157). A slow cluster degrades to the
    #: old switch-then-fill behavior, never worse.
    DRILL_PREWARM_TIMEOUT = 1.0

    def __init__(
        self,
        *,
        state: WorkspaceState,
        store: ResourceStore,
        watch_manager: WatchLifecycle,
        metrics: MetricsLifecycle | None,
        relationship_loader: RelationshipLoading | None,
        ui: UiSurface,
        surface: WorkspaceSurface,
        view: ViewState,
        context: ContextGuard,
        logs: WorkspaceLogs,
        hints: WorkspaceHints,
        config: Callable[[], KorvidConfig],
        get_manifest: Callable[
            [], Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None
        ],
        get_helm_components: Callable[
            [], Callable[[str, str], Awaitable[list[ComponentRef]]] | None
        ],
        olm_alias_key: Callable[[str], str | None],
        describe_named: Callable[[str, str, str], Coroutine[Any, Any, None]],
        cluster_list_permitted: Callable[[], Awaitable[bool]],
    ) -> None:
        self._state = state
        self._store = store
        self._watch_manager = watch_manager
        self._metrics = metrics
        self._relationship_loader = relationship_loader
        self._ui = ui
        self._surface = surface
        self._view = view
        self._context = context
        self._logs = logs
        self._hints = hints
        self._config = config
        # Late-binding getters, not plain callables: `get_helm_components` is
        # an optional collaborator (None when helm support isn't wired), and
        # tests monkeypatch `app._get_manifest`/`app._get_helm_components` in
        # place on the already-constructed app (e.g. the `slow_components`
        # seam in test_hierarchy_nav.py) — resolving at each call observes
        # both, matching the established `HelmController`/`OperatorController`
        # getter pattern.
        self._get_manifest = get_manifest
        self._get_helm_components = get_helm_components
        self._olm_alias_key = olm_alias_key
        self._describe_named = describe_named
        self._cluster_list_permitted = cluster_list_permitted
        # Serializes view/scope switches: keyboard NavigateCommands and the
        # agent's navigate tool share this handler, which yields while
        # stopping/starting watches — interleaving would corrupt state. The
        # `:ctx`, `:mcp` and write-execution coordinators serialize with it
        # through `nav_lock` too.
        self._nav_lock = asyncio.Lock()
        #: Outstanding drill pre-warm leases per (kind, scope) (issue #157):
        #: overlapping drills each hold one; only the last release may reap a
        #: stream no pane displays.
        self._prewarm_leases: dict[tuple[str, str], int] = {}
        #: Rebuild inputs for an open HierarchyScreen: (title, refs, namespace,
        #: scope). Store updates rebuild the tree in place while it is open.
        self._hierarchy_ctx: tuple[str, list[ComponentRef], str, str] | None = None
        #: Cursor-placement poll budget for hierarchy goto (50ms per attempt);
        #: an attribute so tests can shrink the give-up window.
        self._jump_poll_attempts: int = 200
        #: Kinds with a table render already queued — coalesces the per-object
        #: notifications of a LIST seed into a single rebuild.
        self._render_pending: set[str] = set()
        #: Scope the metrics poller currently serves (None = stopped); a
        #: restart drops collected data, so equal targets are skipped.
        self._metrics_target: tuple[str | None] | None = None

    # ------------------------------------------------------------------
    # Exposed workspace-only state (owned here, read by the coordinators)
    # ------------------------------------------------------------------

    @property
    def nav_lock(self) -> asyncio.Lock:
        """The navigation serialization lock the `:ctx`/`:mcp`/write coordinators share."""
        return self._nav_lock

    @property
    def chord_pending(self) -> bool:
        """Whether `ctrl+w` was pressed and the second chord key is awaited."""
        return self._state.chord_pending

    # ------------------------------------------------------------------
    # Render coalescing
    # ------------------------------------------------------------------

    def mark_render_pending(self, kind: str) -> bool:
        """Record that *kind* needs a rebuild; False when one is already queued.

        The initial LIST seeds objects one apply_event at a time in a single
        event-loop slice; posting one message per object would rebuild the
        whole table N times. The caller posts a single `ResourcesUpdated` only
        when this returns True.
        """
        if kind in self._render_pending:
            return False
        self._render_pending.add(kind)
        return True

    def on_resources_updated(self, kind: str) -> None:
        """Consume the pending mark and re-render every pane showing *kind*."""
        self._render_pending.discard(kind)
        self._surface.render_table(kind)
        self.refresh_hierarchy()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate_command(self, view: str | None, namespace: str | None) -> None:
        """`:view`/agent navigate: abandon drill + hierarchy-return, then navigate.

        The stack clear happens inside the navigation lock so a concurrent
        drill (agent path) can never interleave between clear and the
        kind/scope transition. The pane is bound now: a focus flip while this
        waits for the lock must not redirect the clear to another pane.
        """
        pane = self._state.focused
        drill = pane.drill

        def _abandon() -> None:
            drill.clear()
            # Walking away also drops this pane's pending hierarchy-tree
            # return (issue #135) - Escape afterwards must not teleport back.
            pane.hierarchy_return = None

        await self.navigate(view, namespace, drill_op=_abandon)

    async def navigate(
        self,
        view: str | None,
        namespace: str | None,
        *,
        drill_op: Callable[[], None] | None = None,
    ) -> None:
        """Serialize a kind/scope transition on the focused pane under the lock.

        The lock serializes the agent path (direct call from the agent task)
        with the keyboard path (message pump): both mutate the pane's
        kind/scope across awaits. `drill_op` mutates the drill stack inside
        the same critical section so stack and view transition as one
        transaction. The pane identity is captured before waiting on the lock
        so the transition lands in the pane that initiated it.
        """
        pane = self._state.focused
        async with self._nav_lock:
            if not self._state.contains(pane):
                return  # the initiating pane was closed while queued
            if drill_op is not None:
                drill_op()
            await self._navigate_locked(pane, view, self._default_scope_for(view, namespace))
        self._surface.render_table(pane.kind, only=pane)
        self._surface.refresh_status()

    def _default_scope_for(self, view: str | None, namespace: str | None) -> str | None:
        """Catalog entries live in catalog namespaces, not the user's workload
        namespace: any packagemanifests view without an explicit namespace
        defaults to the cluster-wide scope or the table would commonly come up
        empty. Applied inside `navigate` so every entry path behaves alike."""
        if namespace is not None or view is None:
            return namespace
        meta = self._view.aliases().get(view)
        if meta is not None and (meta.group, meta.plural) == (PACKAGES_GROUP, "packagemanifests"):
            return ALL_NAMESPACES
        return None

    async def _navigate_locked(
        self, pane: PaneState, view: str | None, namespace: str | None
    ) -> None:
        """Kind/scope transition body; caller must hold `_nav_lock`."""
        # Advance the pane's navigation generation first: a queued drill
        # revalidating after its pre-warm must observe this command even when
        # the kind/scope tuple ends up unchanged.
        pane.nav_gen += 1
        # A describe pane covering the table would show a stale manifest over
        # the new view — dismiss it on any navigation.
        self._surface.hide_describe()
        new_kind = view if view is not None else pane.kind
        new_scope = namespace if namespace is not None else pane.scope
        if new_kind != pane.kind or new_scope != pane.scope:
            # Only the owning pane's navigation closes the logs: the other
            # pane must keep its stream (issue #48 workflow).
            await self._logs.close_if_owned_by(pane)
            old = (pane.kind, pane.scope)
            # Another pane may still be watching the old (kind, scope):
            # stopping it would freeze that pane's view (issue #48).
            others = {(p.kind, p.scope) for p in self._state.panes if p is not pane}
            if old not in others and self._prewarm_leases.get(old, 0) == 0:
                # An outstanding drill pre-warm lease keeps the stream alive
                # (issue #157): killing it here would force that drill's own
                # navigate to re-LIST into the empty flash.
                await self._watch_manager.stop(*old)
            pane.kind = new_kind
            pane.scope = new_scope
            # The footer legend follows the focused pane's kind (issue #114).
            self._surface.refresh_bindings()
            await self._watch_manager.start(new_kind, new_scope)
            await self.sync_metrics_poller()

    async def sync_metrics_poller(self) -> None:
        """Poll metrics only while a pods view is on screen, in its scope.

        metrics.k8s.io has no watch support, so this poller is the one
        recurring request the app makes - stopping it off the pods view keeps
        background load at zero. With a split workspace two pod panes in
        different scopes poll cluster-wide so neither goes stale. A restart
        drops collected data, so a target the poller already serves is left
        running untouched.
        """
        if self._metrics is None:
            return
        scopes = {p.scope for p in self._state.panes if p.kind == "pods"}
        if not scopes:
            if self._metrics_target is not None:
                self._metrics_target = None
                await self._metrics.stop()
            return
        scope = scopes.pop() if len(scopes) == 1 else ALL_NAMESPACES
        namespace = None if scope == ALL_NAMESPACES else scope
        target = (namespace,)
        if target == self._metrics_target:
            return
        self._metrics_target = target
        await self._metrics.start(namespace)

    async def toggle_all_namespaces(self) -> None:
        """Toggle scope between ALL_NAMESPACES and the config-default namespace.

        Routed through the locked navigate handler so it serializes with
        agent-driven navigation (both stop/start watches across awaits).
        """
        if self._state.current_scope == ALL_NAMESPACES:
            new_scope = self._config().namespace or "default"
        else:
            if not await self._cluster_list_permitted():
                return  # notified inside; stay in the current namespace
            new_scope = ALL_NAMESPACES
        await self.navigate_command(None, new_scope)

    async def favorite_namespace(self, index: int) -> None:
        """Jump to `favorite_namespaces[index-1]` (issue #108, keys 1-9).

        A favorite is a UI-only shortcut: it uses the exact same navigation
        path as `:ns <name>` — no access is granted, no namespace list is
        derived, and a forbidden watch reports its own concise notice.
        """
        favorites = self._config().favorite_namespaces
        if index > len(favorites):
            return
        await self.navigate_command(None, favorites[index - 1])

    # ------------------------------------------------------------------
    # Filter
    # ------------------------------------------------------------------

    def set_filter(self, pattern: str) -> None:
        """Apply *pattern* to the focused pane and re-render it."""
        self._state.filter_pattern = pattern
        self._state.resource_filter = parse_filter(pattern)
        self._surface.render_table(self._state.current_kind, only=self._state.focused)
        self._surface.refresh_status()

    def clear_filter(self) -> None:
        """Clear the focused pane's filter and re-render it."""
        self._state.filter_pattern = ""
        self._state.resource_filter = parse_filter("")
        self._surface.render_table(self._state.current_kind, only=self._state.focused)
        self._surface.refresh_status()

    # ------------------------------------------------------------------
    # Sort (issue #37 / #45 / #138)
    # ------------------------------------------------------------------

    def _view_for(self, kind: str) -> ViewConfig | None:
        """The `views:` config entry for a view kind, resolved via its meta."""
        meta = self._view.aliases().get(kind)
        return self._config().views.get(meta.plural if meta is not None else kind)

    def sort_by(self, column: str) -> None:
        """Apply/flip a sort column for the current view kind and re-render."""
        kind = self._state.current_kind
        if column in ("cpu", "mem") and kind != "pods":
            # Only the pods view has CPU/MEM columns and a metrics feed;
            # elsewhere the keypress would silently discard the current order.
            return
        view = self._view_for(kind)
        if column != "name" and view is not None and view.replace:
            # `replace: true` hides AGE/CPU/MEM — sorting by an invisible
            # column would reorder rows with no indicator, so ignore it.
            return
        sorts = self._state.sorts
        sorts[kind] = toggle_sort(sorts.get(kind), column)
        self._surface.render_table(kind, only=self._state.focused)

    def sort_command(self, column: str | None) -> None:
        """`:sort <column>` (issue #45): builtin or custom column; None clears."""
        kind = self._state.current_kind
        sorts = self._state.sorts
        if column is None:
            sorts.pop(kind, None)
            self._surface.render_table(kind, only=self._state.focused)
            return
        view = self._view_for(kind)
        custom_names = tuple(col.name for col in view.columns) if view is not None else ()
        builtins = ("name",) if view is not None and view.replace else SORT_COLUMNS
        if column.lower() in builtins:
            self.sort_by(column.lower())
            return
        matched = next((name for name in custom_names if name.lower() == column.lower()), None)
        if matched is None:
            columns = ", ".join((*builtins, *custom_names))
            self._ui.notify(
                f"Unknown sort column {column!r} — available: {columns}", severity="warning"
            )
            return
        sorts[kind] = toggle_sort(sorts.get(kind), matched)
        self._surface.render_table(kind, only=self._state.focused)

    def _sortable_columns(self, kind: str) -> tuple[str, ...]:
        """Every column the current view can sort by (issue #138)."""
        view = self._view_for(kind)
        custom = tuple(col.name for col in view.columns) if view is not None else ()
        if view is not None and view.replace:
            builtins: tuple[str, ...] = ("name",)
        elif kind == "pods":
            builtins = SORT_COLUMNS
        else:
            builtins = ("name", "age")
        return (*builtins, *custom)

    def _apply_sort_column(self, column: str, *, pane: PaneState) -> None:
        """Apply/flip *column* (already validated for *pane*'s view) and re-render."""
        kind = pane.kind
        pane.sorts[kind] = toggle_sort(pane.sorts.get(kind), column)
        self._surface.render_table(kind, only=pane)

    def sort_picker_options(self) -> tuple[str, tuple[str, ...], PaneState] | None:
        """The picker title, decorated column labels, and the pane they target.

        None when a modal is already open (the picker must never stack).
        """
        if self._ui.screen_depth() > 1:
            return None
        pane = self._state.focused
        kind = pane.kind
        columns = self._sortable_columns(kind)
        current = self._state.sorts.get(kind)
        options = tuple(
            f"{column} {'▼' if current.descending else '▲'}"
            if current is not None and current.column == column
            else column
            for column in columns
        )
        return f"Sort {kind} by:", options, pane

    def apply_sort_choice(self, choice: str, pane: PaneState, kind: str) -> None:
        """Apply a picked sort label to *pane* if it is still on *kind*."""
        # Strip only the active-sort arrow: custom column names may
        # legitimately contain spaces.
        column = choice.removesuffix(" ▲").removesuffix(" ▼")
        if (
            self._state.contains(pane)
            and pane.kind == kind
            and column in self._sortable_columns(kind)
        ):
            self._apply_sort_column(column, pane=pane)

    def header_sort(self, table_id: str | None, label: str) -> None:
        """A header click sorts by that column in the clicked pane (issue #138)."""
        pane = next((p for p in self._state.panes if p.table_id == table_id), None)
        if pane is None:
            return  # a table without a live pane (mid-teardown)
        # Labels may carry the ▲/▼ decoration of the active sort.
        label = label.removesuffix(" ▲").removesuffix(" ▼")
        kind = pane.kind
        columns = self._sortable_columns(kind)
        builtin = _HEADER_SORT_COLUMNS.get(label)
        # Configured custom names may carry Rich markup ([red]TEAM[/]) that
        # DataTable parses for display: match on the rendered plain text.
        custom = next((name for name in columns if Text.from_markup(name).plain == label), None)
        column = builtin if builtin in columns else custom
        if column is None:
            self._ui.notify(
                f"{label} is not sortable — sortable: {', '.join(columns)}", severity="warning"
            )
            return
        self._apply_sort_column(column, pane=pane)

    # ------------------------------------------------------------------
    # Drill (issue #157)
    # ------------------------------------------------------------------

    def can_drill(self) -> bool:
        """True when Enter drills on the current view (topbar hint)."""
        if self._state.current_kind == "pods":
            return True
        if self.hierarchy_root_kind() is not None:
            return True
        return drill_child(self._view.canonical_kind(self._state.current_kind)) is not None

    async def drill_down_selected(self, row_key: str) -> bool:
        """Keyboard Enter: push a drill level for the selected row.

        Returns whether the current kind has a drill-down chain at all -
        False means Enter is a no-op and the caller should leave the
        keypress unconsumed for a later handler.
        """
        if drill_child(self._view.canonical_kind(self._state.current_kind)) is None:
            return False  # kind has no drill-down chain; Enter is a no-op
        parts = row_key.split("/", 1)
        if len(parts) == 2:
            error = await self.drill_into(parts[0], parts[1])
            if error is not None:
                self._ui.notify(error, severity="warning")
        return True

    async def handle_non_pods_row_selected(self, row_key: str) -> bool:
        """Enter on a non-pods row: open the hierarchy tree or push a drill
        level (issue #120/#157).

        Returns whether the caller should stop the event - False leaves an
        undrillable kind's Enter for a later handler (e.g. a default
        describe) instead of silently swallowing it.
        """
        if self.hierarchy_root_kind() is not None:
            # Helm release / OLM Subscription / CSV: Enter opens the
            # component hierarchy tree (issue #120).
            parts = row_key.split("/", 1)
            if len(parts) == 2:
                self.open_hierarchy(parts[0], parts[1])
            return True
        return await self.drill_down_selected(row_key)

    async def prewarm_view(
        self, kind: str, scope: str, ready: Callable[[list[Summary]], bool]
    ) -> None:
        """Warm the drill target before the pane switches (issue #157).

        Starting the watch for a kind no pane displays renders nowhere, so the
        LIST happens invisibly while the current view stays up; the bounded
        wait ends as soon as `ready` sees the expected rows. A pane-backed and
        *live* watch is already warm - restart and wait are both skipped. The
        lease count makes `stop_watch_if_unused` reap the stream only when the
        last pre-warm released it.
        """
        key = (kind, scope)
        self._prewarm_leases[key] = self._prewarm_leases.get(key, 0) + 1
        if (
            any((p.kind, p.scope) == key for p in self._state.panes)
            and key in self._watch_manager.active
        ):
            return
        await self._watch_manager.start(kind, scope)
        deadline = monotonic() + self.DRILL_PREWARM_TIMEOUT
        with self._ui.progress(f"loading {kind}"):
            while monotonic() < deadline:
                if ready(self._store.get(kind, scope)):
                    return
                await asyncio.sleep(0.03)

    async def stop_watch_if_unused(self, kind: str, scope: str) -> None:
        """Release one pre-warm lease; reap the stream when it was the last
        lease and no pane displays the (kind, scope) (issue #157)."""
        key = (kind, scope)
        remaining = self._prewarm_leases.get(key, 0) - 1
        if remaining > 0:
            self._prewarm_leases[key] = remaining
            return
        self._prewarm_leases.pop(key, None)
        if all((p.kind, p.scope) != key for p in self._state.panes):
            await self._watch_manager.stop(kind, scope)

    async def drill_into(self, namespace: str, name: str) -> str | None:
        """Push a drill level for (namespace, name) and navigate to the child
        kind. Returns an error message, or None on success."""
        canonical = self._view.canonical_kind(self._state.current_kind)
        child = drill_child(canonical)
        if child is None:
            return f"{canonical} has no drill-down chain"
        if child not in self._view.aliases():
            return f"{child} not discovered yet, try again shortly"
        obj = next(
            (
                o
                for o in self._store.get(self._state.current_kind, self._state.current_scope)
                if o.namespace == namespace and o.name == name
            ),
            None,
        )
        if obj is None:
            return f"no {canonical} named {name!r} in the current view"
        uid = str(getattr(obj, "uid", "") or "")
        if not uid:
            return f"cannot drill into {name}: no uid available"
        level = DrillLevel(
            parent_kind=canonical,
            parent_name=name,
            parent_namespace=namespace,
            parent_uid=uid,
            child_kind=child,
        )
        # Push and navigate as one transaction under the navigation lock.
        # Capture before waiting on the lock: focus may move (or the pane may
        # close) while this drill queues behind another navigation.
        pane = self._state.focused
        origin = (pane.kind, pane.scope)
        epoch = self._context.epoch()
        nav_gen = pane.nav_gen
        prewarm_scope = pane.scope
        try:
            await self.prewarm_view(
                child, prewarm_scope, lambda rows: any(owned_by(r, uid) for r in rows)
            )
            async with self._nav_lock:
                if not self._state.contains(pane):
                    return "the pane closed while preparing the drill — drill abandoned"
                if (
                    (pane.kind, pane.scope) != origin
                    or pane.nav_gen != nav_gen
                    or self._context.switching()
                    or epoch != self._context.epoch()
                ):
                    return (
                        "the view changed while preparing the drill — drill abandoned "
                        "(the newer navigation takes priority)"
                    )
                pane.drill.push(level)
                try:
                    await self._navigate_locked(pane, child, None)
                except BaseException:
                    pane.drill.pop()
                    raise
        finally:
            await self.stop_watch_if_unused(child, prewarm_scope)
        self._surface.render_table(pane.kind, only=pane)
        self._surface.refresh_status()
        return None

    async def pop_drill(self) -> bool:
        """Pop one drill level and navigate back to its parent kind as one
        transaction under the navigation lock. False when the stack was empty."""
        pane = self._state.focused
        peeked = pane.drill.peek()
        if peeked is None:
            return False
        origin = (pane.kind, pane.scope)
        epoch = self._context.epoch()
        nav_gen = pane.nav_gen
        # Warm the parent view first (issue #157): a remaining drill level
        # keeps filtering by its parent UID, so an unrelated row must not
        # satisfy the wait; only a pop back to the root accepts any row.
        under = pane.drill.copy()
        under.pop()
        uid_after = under.parent_uid
        if uid_after is None:
            ready: Callable[[list[Summary]], bool] = bool
        else:

            def ready(rows: list[Summary]) -> bool:
                return any(owned_by(r, uid_after) for r in rows)

        prewarm_scope = pane.scope
        try:
            await self.prewarm_view(peeked.parent_kind, prewarm_scope, ready)
            async with self._nav_lock:
                if not self._state.contains(pane):
                    return False  # the initiating pane was closed while queued
                if (
                    (pane.kind, pane.scope) != origin
                    or pane.nav_gen != nav_gen
                    or self._context.switching()
                    or epoch != self._context.epoch()
                    or pane.drill.peek() is not peeked
                ):
                    # A newer navigation landed during the pre-warm: it wins.
                    # Consume the Esc (True) so it does not cascade into the
                    # hierarchy-return fallback against the changed view.
                    return True
                popped = pane.drill.pop()
                if popped is None:
                    return False
                await self._navigate_locked(pane, popped.parent_kind, None)
        finally:
            await self.stop_watch_if_unused(peeked.parent_kind, prewarm_scope)
        self._surface.render_table(pane.kind, only=pane)
        self._surface.refresh_status()
        return True

    # ------------------------------------------------------------------
    # Hierarchy tree (issues #120 / #135)
    # ------------------------------------------------------------------

    def hierarchy_root_kind(self) -> str | None:
        """The current view's hierarchy root kind, or None (issue #120)."""
        meta = self._view.aliases().get(self._view.canonical_kind(self._state.current_kind))
        if meta is None:
            return None
        ident = (meta.group, meta.plural)
        if ident == (HELM_RELEASES_META.group, HELM_RELEASES_META.plural):
            return "HelmRelease" if self._get_helm_components() is not None else None
        if ident == (OPERATORS_GROUP, "subscriptions"):
            return "Subscription"
        if ident == (OPERATORS_GROUP, "clusterserviceversions"):
            return "ClusterServiceVersion"
        return None

    def _view_for_component(self, ref: ComponentRef) -> tuple[str, bool] | None:
        """Canonical view alias plus namespacedness for a component ref, or
        None when no real (non-synthetic) view was discovered for it."""
        group = ref.api_version.rpartition("/")[0]  # core "v1" -> ""
        fallback: tuple[str, bool] | None = None
        for alias, meta in self._view.aliases().items():
            if meta.kind != ref.kind or meta.synthetic or self._view.canonical_kind(alias) != alias:
                continue
            if meta.group == group:
                return alias, meta.namespaced
            if not ref.api_version and fallback is None:
                fallback = (alias, meta.namespaced)
        return fallback

    async def _hierarchy_refs(
        self, root: str, namespace: str, name: str
    ) -> list[ComponentRef] | None:
        """Component refs for the root, or None when unavailable (notified)."""
        if root == "HelmRelease":
            fetch = self._get_helm_components()
            if fetch is None:
                return None
            try:
                return await fetch(namespace, name)
            except (ApiStatusError, ValueError) as exc:
                self._ui.notify(f"hierarchy for {name} unavailable: {exc}", severity="error")
                return None
        return await self._operator_component_refs(root, namespace, name)

    def open_hierarchy(self, namespace: str, name: str) -> None:
        """Launch the exclusive worker that gathers refs and pushes the tree."""
        self._ui.run_worker(
            self._open_hierarchy(namespace, name), exclusive=True, group=HIERARCHY_GROUP
        )

    async def _open_hierarchy(self, namespace: str, name: str) -> None:
        """Gather component refs for the selected root and push the tree."""
        root = self.hierarchy_root_kind()
        if root is None or not self._context.reads_allowed():
            return
        epoch = self._context.epoch()
        pane = self._state.focused
        kind, scope = pane.kind, pane.scope
        refs = await self._hierarchy_refs(root, namespace, name)
        if refs is None:
            return
        if self._context.switching() or epoch != self._context.epoch():
            return
        if self._state.focused is not pane or pane.kind != kind or pane.scope != scope:
            return  # the user moved on while components were being fetched
        if self._ui.screen_depth() > 1:  # another dialog opened during the fetch
            return
        title = f"{root} {namespace}/{name}" if namespace else f"{root} {name}"
        tree_root = build_hierarchy(
            title,
            refs,
            namespace=namespace,
            resolve=self._view_for_component,
            lookup=self._hierarchy_lookup(scope),
        )
        self._hierarchy_ctx = (title, refs, namespace, scope)
        origin = (pane, self._view.canonical_kind(kind), scope)
        self._ui.push_screen(
            HierarchyScreen(title, tree_root),
            functools.partial(self._on_hierarchy_pick, epoch, origin),
        )

    def _on_hierarchy_pick(
        self,
        epoch: int,
        origin: tuple[PaneState, str, str],
        result: tuple[str, str, str, str] | None,
    ) -> None:
        """A tree node action: jump to the object's view or describe it."""
        ctx = self._hierarchy_ctx
        self._hierarchy_ctx = None  # tree closed: stop live rebuilds
        if result is None:
            return
        if self._context.switching() or epoch != self._context.epoch():
            self._ui.notify(
                "hierarchy action cancelled - the kube context changed while the tree was open",
                severity="warning",
            )
            return
        action, kind, ns, obj = result
        if action == "describe":
            coro = self._describe_named(kind, ns, obj)
        else:
            if ctx is not None:
                # The jump must stay reversible (issue #135): Escape on the
                # target reopens this tree over the view it was opened from.
                title, refs, namespace, scope = ctx
                origin_pane, origin_view, origin_scope = origin
                origin_pane.hierarchy_return = HierarchyReturn(
                    origin_view=origin_view,
                    origin_scope=origin_scope,
                    title=title,
                    refs=refs,
                    namespace=namespace,
                    tree_scope=scope,
                    picked=(kind, ns, obj),
                    epoch=epoch,
                )
            coro = self.jump_to_object(kind, ns, obj, epoch=epoch)
        self._ui.run_worker(coro, exclusive=True, group=HIERARCHY_GROUP)

    async def reopen_hierarchy_return(self) -> bool:
        """Escape on a hierarchy jump target: rebuild the tree over its origin
        view, cursor on the picked node (issue #135)."""
        pane = self._state.focused
        ret = pane.hierarchy_return
        if ret is None:
            return False
        if self._ui.screen_depth() > 1:
            # This Escape belongs to the modal on top - the return stays
            # pending for the next Escape on the base view.
            return False
        if self._context.crossed(ret.epoch):
            pane.hierarchy_return = None
            return False
        if self._view.canonical_kind(self._state.current_kind) != ret.picked[0]:
            # The pane navigated elsewhere; nothing to return to.
            pane.hierarchy_return = None
            return False
        pane.hierarchy_return = None  # consumed - never replayed
        await self.navigate(ret.origin_view, ret.origin_scope)
        if self._context.crossed(ret.epoch):
            # A :ctx switch started while the navigate held the nav lock: the
            # refs describe the old cluster - do not expose the tree.
            return True
        tree_root = build_hierarchy(
            ret.title,
            ret.refs,
            namespace=ret.namespace,
            resolve=self._view_for_component,
            lookup=self._hierarchy_lookup(ret.tree_scope),
        )
        self._hierarchy_ctx = (ret.title, ret.refs, ret.namespace, ret.tree_scope)
        origin = (pane, ret.origin_view, ret.origin_scope)
        self._ui.push_screen(
            HierarchyScreen(ret.title, tree_root, initial_cursor=ret.picked),
            functools.partial(self._on_hierarchy_pick, ret.epoch, origin),
        )
        return True

    def _hierarchy_lookup(self, scope: str) -> Callable[[str, str], list[Summary] | None]:
        """Store lookup for the tree: a list only for views a live watch is
        actually feeding, else None. Results are memoized per (view, watch
        scope) for this lookup's lifetime (one tree build)."""
        buckets: dict[tuple[str, str], list[Summary]] = {}

        def lookup(view: str, namespace: str) -> list[Summary] | None:
            active = self._watch_manager.active
            for view_scope in (namespace or scope, ALL_NAMESPACES):
                if (view, view_scope) in active:
                    key = (view, view_scope)
                    if key not in buckets:
                        buckets[key] = self._store.get(view, view_scope)
                    return buckets[key]
            return None

        return lookup

    def refresh_hierarchy(self) -> None:
        """Rebuild an open hierarchy tree from the current store state."""
        ctx = self._hierarchy_ctx
        if ctx is None or not self._surface.hierarchy_open():
            return
        title, refs, namespace, scope = ctx
        self._surface.update_hierarchy_tree(
            build_hierarchy(
                title,
                refs,
                namespace=namespace,
                resolve=self._view_for_component,
                lookup=self._hierarchy_lookup(scope),
            )
        )

    async def _operator_component_refs(
        self, root: str, namespace: str, name: str
    ) -> list[ComponentRef] | None:
        """Component refs for an OLM root, in the issue #120 preference order."""
        get_manifest = self._get_manifest()
        if get_manifest is None:
            self._ui.notify("Hierarchy unavailable in this session", severity="warning")
            return None
        try:
            manifest = await get_manifest(self._state.current_kind, namespace or None, name)
        except (ApiStatusError, ValueError) as exc:
            self._ui.notify(f"hierarchy for {name} unavailable: {exc}", severity="error")
            return None
        refs = await self._refs_from_operator_object(manifest, namespace, root)
        # The Operator's refs include the root object itself; a root listed as
        # its own child would just loop Enter back to the same tree.
        refs = [
            r
            for r in refs
            if (r.kind, r.name) != (root, name) or (r.namespace and r.namespace != namespace)
        ]
        if refs:
            return refs
        if root == "Subscription":
            return await self._refs_from_installplan(manifest, namespace)
        return self._refs_from_owned_workloads(manifest, namespace)

    def _refs_from_owned_workloads(
        self, manifest: dict[str, Any], namespace: str
    ) -> list[ComponentRef]:
        """CSV fallback (issue #120 third source): Deployments whose
        ownerReferences point at the CSV, from buckets a live watch feeds."""
        uid = str((manifest.get("metadata") or {}).get("uid") or "")
        if not uid:
            return []
        lookup = self._hierarchy_lookup(self._state.current_scope)
        refs: list[ComponentRef] = []
        for obj in lookup("deployments", namespace) or []:
            if obj.namespace != namespace or uid not in getattr(obj, "owner_uids", ()):
                continue
            refs.append(ComponentRef(kind="Deployment", name=str(obj.name), namespace=namespace))
            if len(refs) >= MAX_COMPONENT_DOCS:
                break
        return refs

    async def _refs_from_installplan(
        self, manifest: dict[str, Any], namespace: str
    ) -> list[ComponentRef]:
        """InstallPlan fallback: ``status.plan`` records exactly what the
        install created (older OLM without the Operator API)."""
        key = self._olm_alias_key("installplans")
        ref = (manifest.get("status") or {}).get("installPlanRef") or {}
        plan_name = str(ref.get("name") or "")
        get_manifest = self._get_manifest()
        if key is None or not plan_name or get_manifest is None:
            return []
        plan_ns = str(ref.get("namespace") or namespace)
        try:
            plan = await get_manifest(key, plan_ns or None, plan_name)
        except (ApiStatusError, ValueError):
            return []
        return installplan_components((plan.get("status") or {}).get("plan"))

    async def _refs_from_operator_object(
        self, manifest: dict[str, Any], namespace: str, root: str
    ) -> list[ComponentRef]:
        """Refs from the cluster-scoped Operator object or via the component
        labels OLM stamps on CSVs."""
        key = self._olm_alias_key("operators")
        get_manifest = self._get_manifest()
        if key is None or get_manifest is None:
            return []
        names: list[str] = []
        if root == "Subscription":
            package = str((manifest.get("spec") or {}).get("name") or "")
            if package:
                names.append(f"{package}.{namespace}")
        labels = (manifest.get("metadata") or {}).get("labels") or {}
        prefix = f"{OPERATORS_GROUP}/"
        names += [
            k.removeprefix(prefix) for k in labels if isinstance(k, str) and k.startswith(prefix)
        ]
        for op_name in dict.fromkeys(names):
            try:
                operator = await get_manifest(key, None, op_name)
            except (ApiStatusError, ValueError):
                continue  # Operator object missing: fall through to the next source
            components = (operator.get("status") or {}).get("components") or {}
            refs = reference_components(components.get("refs"))
            if refs:
                return refs
        return []

    # ------------------------------------------------------------------
    # Navigate-to-object (shared by hierarchy, relationship, timeline goto)
    # ------------------------------------------------------------------

    async def jump_to_object(
        self, kind: str, namespace: str, name: str, *, epoch: int | None = None
    ) -> None:
        """Navigate to *kind*'s view and put the cursor on the object. A
        context switch crossing *epoch* aborts: the same-named object in the
        new cluster is not what the user picked."""
        if epoch is not None and self._context.crossed(epoch):
            return
        meta = self._view.aliases().get(kind)
        if meta is None:
            self._ui.notify(f"{kind} is not a discovered view", severity="warning", markup=False)
            return
        await self.navigate(kind, namespace if meta.namespaced and namespace else None)
        row_key = f"{namespace}/{name}"
        for _ in range(self._jump_poll_attempts):
            if epoch is not None and self._context.crossed(epoch):
                return
            if self._state.current_kind != kind:
                return  # the user moved on - stop quietly
            if self._surface.focus_row(row_key):
                return
            await asyncio.sleep(0.05)
        self._ui.notify(
            f"{name} is not visible in {kind} - it may be gone or outside the current scope",
            severity="warning",
            markup=False,
        )

    # ------------------------------------------------------------------
    # Relationships (issue #281)
    # ------------------------------------------------------------------

    def _selected_relationship_root(self) -> GraphResource | None:
        """The exact root identity for `g`: discovery meta plus the selected
        row's namespace/name/UID. None only after an already-visible warning."""
        meta = self._view.aliases().get(self._state.current_kind)
        if meta is None:
            self._ui.notify(
                f"{self._state.current_kind} is not a discovered view", severity="warning"
            )
            return None
        if meta.synthetic:
            self._ui.notify(f"{meta.kind} is a read-only view", severity="warning")
            return None
        namespace, name = self._view.selected_ns_name()
        if namespace is None or name is None:
            return None
        uid = self._view.selected_uid(namespace or None, name)
        return GraphResource(
            group=meta.group, kind=meta.kind, namespace=namespace, name=name, uid=uid
        )

    def show_relationships(self) -> None:
        """Load and show the operational relationship graph for the selected
        row (issue #281). Every LIST runs inside the exclusive worker."""
        if self._relationship_loader is None:
            self._ui.notify("Relationships unavailable in this session", severity="warning")
            return
        if not self._context.reads_allowed():
            return
        target = self._selected_relationship_root()
        if target is None:
            return
        pane = self._state.focused
        origin = (pane, pane.kind, pane.scope)
        namespace = None if pane.scope == ALL_NAMESPACES else pane.scope
        self._run_relationship_worker(
            self._load_relationships(target, namespace, self._context.epoch(), origin)
        )

    def _run_relationship_worker(self, work: Coroutine[Any, Any, None]) -> None:
        """Start one exclusive `relationships` worker with an error boundary.

        `exit_on_error=False` keeps an unexpected failure from tearing the
        whole TUI down over a read-only view; the app's worker-state handler
        turns it into a visible notification instead.
        """
        self._ui.run_worker(work, exclusive=True, group=RELATIONSHIP_GROUP, exit_on_error=False)

    async def _load_relationships(
        self,
        target: GraphResource,
        namespace: str | None,
        epoch: int,
        origin: tuple[PaneState, str, str],
    ) -> None:
        """Load one bounded snapshot and open `RelationshipScreen` over it."""
        loader = self._relationship_loader
        if loader is None:
            return  # composition changed under us since show_relationships checked
        graph = await loader.load(target, namespace, self._view.aliases())
        if self._context.crossed(epoch):
            return
        pane, kind, scope = origin
        if self._state.focused is not pane or pane.kind != kind or pane.scope != scope:
            return
        if self._ui.screen_depth() > 1:  # another dialog opened during the LISTs
            return
        self._ui.push_screen(
            RelationshipScreen(graph, target),
            functools.partial(self.on_relationship_result, epoch),
        )

    def on_relationship_result(self, epoch: int, result: GotoResult | None) -> None:
        """Enter on a resolved row: reuse `jump_to_object` after translating
        the graph's (group, kind) back to the discovered view alias."""
        if result is None:
            return
        if self._context.crossed(epoch):
            self._ui.notify(
                "relationship navigation cancelled - the kube context changed"
                " while the graph was open",
                severity="warning",
            )
            return
        _, group, kind, namespace, name = result
        api_version = f"{group}/v1" if group else ""
        resolved = self._view_for_component(
            ComponentRef(kind=kind, name=name, api_version=api_version, namespace=namespace)
        )
        if resolved is None:
            self._ui.notify(f"{kind} is not a discovered view", severity="warning", markup=False)
            return
        alias, _namespaced = resolved
        self._run_relationship_worker(self.jump_to_object(alias, namespace, name, epoch=epoch))

    async def cancel_relationship_workers(self) -> None:
        """Stop the `g` graph load (and its goto follow-up) before the swap."""
        await self._ui.cancel_workers(RELATIONSHIP_GROUP)

    # ------------------------------------------------------------------
    # Split-pane lifecycle (issue #48)
    # ------------------------------------------------------------------

    async def handle_pane_chord(self, event: KeyEvent) -> None:
        """`ctrl+w` chord state machine: the prefix always swallows the next
        key - an unmapped second key must not fall through to normal handling."""
        if not self._state.chord_pending:
            # Arm only while a table is focused: with an Input focused the
            # second key never reaches App.on_key, which would orphan the
            # pending flag and swallow the next table keypress.
            if not self._surface.focused_is_table():
                return
            self._state.chord_pending = True
            event.stop()
            event.prevent_default()
            return
        self._state.chord_pending = False
        event.stop()
        event.prevent_default()
        if event.key == "v":
            await self.split_pane()
        elif event.key in ("w", "ctrl+w"):
            self.focus_other_pane()
        elif event.key == "q":
            await self.close_focused_pane()

    async def split_pane(self) -> None:
        """`ctrl+w v`: clone the focused view into a second pane and focus it."""
        if self._state.is_split:
            self._ui.notify(
                "workspace is already split - ctrl+w q closes a pane", severity="warning"
            )
            return
        # The pane list and watch lifecycle are also mutated by navigation:
        # take the same lock so a concurrent transition never interleaves.
        async with self._nav_lock:
            if self._state.is_split:
                return  # lost the race to another split
            pane = self._state.split()
            await self._surface.mount_pane_table(pane)
            # start() is idempotent - the clone usually shares the source's watch.
            await self._watch_manager.start(pane.kind, pane.scope)
        # A single-pane empty-state overlay must not linger over the split.
        self._surface.hide_empty_state()
        # Render only the new pane: the source is already current, and a
        # repaint would reset its cursor/scroll.
        self._surface.render_table(pane.kind, only=pane)
        self._surface.update_pane_focus_classes()
        self._surface.focus_table(pane.table_id)
        self._surface.refresh_status()

    def focus_other_pane(self) -> None:
        """`ctrl+w w`: move focus (commands, filters, keybindings) across."""
        if not self._state.is_split:
            return
        self._state.focus_other()
        self._surface.update_pane_focus_classes()
        self._surface.focus_table(self._state.focused.table_id)
        self._hints.refresh_for_focus()
        self._surface.refresh_status()

    async def close_focused_pane(self) -> None:
        """`ctrl+w q`: back to the single view; the other pane survives."""
        if not self._state.is_split:
            return
        async with self._nav_lock:
            if not self._state.is_split:
                return  # lost the race to another close
            closed = self._state.close_focused()
            closing, remaining = closed.closing, closed.remaining
            # The pane whose selection drove the stream is gone: don't leave
            # orphaned logs pinned over the survivor's view.
            await self._logs.close_if_owned_by(closing)
            if (closing.kind, closing.scope) != (remaining.kind, remaining.scope):
                await self._watch_manager.stop(closing.kind, closing.scope)
            # The survivor keeps its own table widget - and with it the
            # cursor/scroll state the user had in that pane.
            await self._surface.remove_pane_table(closing.table_id)
            self._surface.unsplit_survivor(remaining.table_id)
            await self.sync_metrics_poller()
        self._surface.update_pane_focus_classes()
        # No repaint: the survivor's table is already current. The single-pane
        # empty-state does need a refresh (an empty survivor must show
        # guidance, and a stale overlay must clear).
        self._surface.refresh_empty_state(remaining.kind)
        self._surface.focus_table(remaining.table_id)
        self._hints.refresh_for_focus()
        self._surface.refresh_status()

    async def collapse_split(self) -> None:
        """Fold the workspace back to a single pane (context-switch teardown).

        The caller already holds the nav lock and stops all watches wholesale
        right after, so this only removes the extra pane's state and table
        widget. The survivor is pane 0; the switch resets its kind/scope/filter
        afterwards, so which pane survives is cosmetic.
        """
        collapsed = self._state.collapse()
        if collapsed is None:
            return
        await self._surface.remove_pane_table(collapsed.closing.table_id)
        self._surface.unsplit_survivor(collapsed.remaining.table_id)
        self._surface.update_pane_focus_classes()

    def on_descendant_focus(self, table_id: str | None) -> None:
        """Clicking a pane focuses it - command routing must follow. Any focus
        change also disarms a pending `ctrl+w` chord."""
        self._state.chord_pending = False
        if table_id is None:
            return
        if self._state.focus_by_table_id(table_id):
            self._surface.update_pane_focus_classes()
            self._hints.refresh_for_focus()
            self._surface.refresh_status()

    def restore_table_focus(self) -> None:
        """Refocus the focused pane's table when nothing else holds focus."""
        if self._surface.has_focus() or self._ui.screen_depth() != 1:
            return
        if self._surface.has_tables():
            self._surface.focus_table(self._state.focused.table_id)

    # ------------------------------------------------------------------
    # Context-switch reset/quiesce API (called by the app coordinator)
    # ------------------------------------------------------------------

    async def quiesce_for_context_switch(self) -> None:
        """Reset every workspace consumer of the old cluster before the swap.

        Called under `nav_lock` by `KorvidApp`'s context-switch coordinator,
        right after it closes the logs and describe pane and before it stops
        all watches wholesale. Folds the split back to one pane, stops the
        metrics poller and drops its served target (so a same-namespace switch
        still restarts it), clears the drill breadcrumb, cancels the
        relationship workers holding the old client, and clears the filter.
        These are the workspace-only halves of the teardown; the app still
        owns the watch/store/hint/forward/timeline teardown around this call.
        """
        await self.collapse_split()
        if self._metrics is not None:
            await self._metrics.stop()
            # The poller is gone: drop the served-target cache too, or a
            # same-namespace switch would look already-served.
            self._metrics_target = None
        self._state.drill.clear()
        await self.cancel_relationship_workers()
        self._state.filter_pattern = ""
        self._state.resource_filter = parse_filter("")

    def reset_view_after_switch(self) -> None:
        """Adopt the new cluster's default view (pods in its default namespace)."""
        self._state.current_kind = "pods"
        self._state.current_scope = self._config().namespace or "default"
        # The footer legend is view-scoped (issue #114): prompt Textual to
        # re-evaluate check_action now the kind is back to pods.
        self._surface.refresh_bindings()
