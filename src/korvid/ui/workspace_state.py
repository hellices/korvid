"""The workspace model: panes, focus, and the focused pane's view state.

`WorkspaceState` is the pure-Python owner of everything the split workspace
(issue #48) mutates: the pane collection, which pane is focused, the monotonic
table-id counter that keeps split/close cycles from reusing a widget id, the
`ctrl+w` chord-pending flag, and — through the focused pane — the current
kind, scope, filter, sort and drill state that every action and command
targets.

It is independent of Textual and of `KorvidApp`: `KorvidApp` owns the widget
tree and applies the Textual side effects, but reads and writes the model
here. The invariants live in one place:

- a workspace always has at least one pane;
- focus always references an existing pane;
- table ids are monotonic and never reused;
- closing the only pane is rejected explicitly.

The pane collection is exposed read-only (a tuple snapshot), so a caller can
never append or replace a pane behind the transition methods that keep those
invariants true.
"""

from __future__ import annotations

import dataclasses

from korvid.core.filters import ResourceFilter, parse_filter
from korvid.core.sorting import SortSpec
from korvid.k8s.components import ComponentRef
from korvid.ui.navigation import NavigationStack


@dataclasses.dataclass(frozen=True)
class HierarchyReturn:
    """The way back to a hierarchy tree after a goto jump (issue #135).

    Captured when a tree node's Enter navigates away: Escape on the jump
    target (with no drill level left to pop) rebuilds the tree over the
    origin view, cursor on the picked node. Lives on the pane that jumped
    (`PaneState.hierarchy_return`) — panes neither see nor clear each
    other's returns. One-shot: consumed or dropped on first eligible use,
    and abandoned when its pane explicitly navigates away."""

    origin_view: str  # canonical kind alias the tree was opened over
    origin_scope: str  # that pane's namespace scope at pick time
    title: str
    refs: list[ComponentRef]
    namespace: str
    tree_scope: str  # store-lookup scope the tree was built with
    picked: tuple[str, str, str]  # (kind alias, namespace, name) of the node
    epoch: int


class PaneState:
    """One workspace pane's independent view state (issue #48).

    The workspace keeps one of these per pane; `WorkspaceState.current_kind`
    and friends delegate to the focused pane, so every existing action and
    command naturally targets the pane the user is working in.
    """

    def __init__(self, kind: str, scope: str, table_id: str = "pane-0") -> None:
        self.kind = kind
        self.scope = scope
        self.table_id = table_id  # the ResourceTable widget this pane renders into
        self.filter_pattern = ""
        self.resource_filter: ResourceFilter = parse_filter("")
        self.drill = NavigationStack()
        #: Monotonic navigation counter: every _navigate_locked call on this
        #: pane advances it, including same-target ones. A drill pre-warm
        #: (issue #157) captures it before waiting and revalidates under the
        #: lock - a `:view deployments` while already on deployments is
        #: still the newer command and must not be overridden.
        self.nav_gen = 0
        #: Pending way back to a hierarchy tree a goto jump navigated away
        #: from (issue #135); consumed by Escape in this pane. View state
        #: like the drill stack - never shared across panes.
        self.hierarchy_return: HierarchyReturn | None = None
        #: Per-kind sort state - view state like the filter, so it belongs
        #: to the pane: sorting one pane must not reorder the other.
        self.sorts: dict[str, SortSpec] = {}

    def clone(self, table_id: str) -> PaneState:
        """A split starts as a clone of the focused view: same kind, scope,
        filter and drill position - with independent state from then on.
        A pending hierarchy return is deliberately not cloned: it is a
        one-shot ticket back to one tree, not repeatable view state."""
        pane = PaneState(self.kind, self.scope, table_id)
        pane.filter_pattern = self.filter_pattern
        pane.resource_filter = parse_filter(self.filter_pattern)
        pane.drill = self.drill.copy()
        pane.sorts = dict(self.sorts)
        return pane


@dataclasses.dataclass(frozen=True)
class ClosedPanes:
    """The outcome of a close/collapse transition (issue #48).

    Names both halves the caller needs to apply the Textual side effects —
    which widget to remove and which survivor to un-split — without rereading
    the workspace's private pane list.
    """

    closing: PaneState
    remaining: PaneState


class WorkspaceState:
    """Owns the split-workspace model and its invariants (issue #48).

    Constructed with the initial view (kind and scope). From then on the
    pane collection, the focused index, the table-id counter and the chord
    flag are mutated only through the methods and setters here, so the
    invariants in the module docstring always hold.
    """

    def __init__(self, kind: str, scope: str) -> None:
        self._panes: list[PaneState] = [PaneState(kind, scope, "pane-0")]
        self._focused: int = 0
        #: Monotonic id source for split-pane table widgets: a survivor keeps
        #: its widget (and cursor/scroll state) when the other pane closes,
        #: so ids must stay unique across split/close cycles.
        self._counter: int = 0
        #: `ctrl+w` pressed, waiting for the chord's second key (v/w/q).
        self._chord_pending: bool = False

    # -- pane collection / focus -------------------------------------------

    @property
    def panes(self) -> tuple[PaneState, ...]:
        """A read-only snapshot of the panes, left-to-right."""
        return tuple(self._panes)

    @property
    def focused(self) -> PaneState:
        """The pane that receives commands, filters and keybindings."""
        return self._panes[self._focused]

    @property
    def focused_index(self) -> int:
        """Position of the focused pane in `panes`."""
        return self._focused

    @property
    def pane_count(self) -> int:
        """How many panes the workspace currently holds (1 or 2)."""
        return len(self._panes)

    @property
    def is_split(self) -> bool:
        """Whether the workspace is showing two panes side by side."""
        return len(self._panes) > 1

    def contains(self, pane: PaneState) -> bool:
        """Whether *pane* is still one of the live panes.

        A flow that captured its initiating pane before an await checks this
        to detect that the pane was closed while it waited.
        """
        return pane in self._panes

    # -- focused-pane view state (issue #48) -------------------------------
    # The pane list is the single source of view state; these accessors keep
    # the whole action surface working against "the view the user is focused
    # on" while the pane objects stay owned here.

    @property
    def current_kind(self) -> str:
        return self.focused.kind

    @current_kind.setter
    def current_kind(self, value: str) -> None:
        self.focused.kind = value

    @property
    def current_scope(self) -> str:
        return self.focused.scope

    @current_scope.setter
    def current_scope(self, value: str) -> None:
        self.focused.scope = value

    @property
    def current_namespace(self) -> str:
        """Alias of `current_scope`; both names are in use across the app."""
        return self.focused.scope

    @current_namespace.setter
    def current_namespace(self, value: str) -> None:
        self.focused.scope = value

    @property
    def filter_pattern(self) -> str:
        return self.focused.filter_pattern

    @filter_pattern.setter
    def filter_pattern(self, value: str) -> None:
        self.focused.filter_pattern = value

    @property
    def resource_filter(self) -> ResourceFilter:
        """Parsed form of `filter_pattern` (issue #44); the single matcher
        shared by the table render and the agent's view of "what the user
        sees"."""
        return self.focused.resource_filter

    @resource_filter.setter
    def resource_filter(self, value: ResourceFilter) -> None:
        self.focused.resource_filter = value

    @property
    def sorts(self) -> dict[str, SortSpec]:
        """Per-kind sort state of the focused pane (view state, issue #37)."""
        return self.focused.sorts

    @property
    def drill(self) -> NavigationStack:
        """Drill-down levels (deploy -> rs -> pods) of the focused pane."""
        return self.focused.drill

    # -- pane chord (issue #48) --------------------------------------------

    @property
    def chord_pending(self) -> bool:
        """`ctrl+w` was pressed and the workspace awaits its second key."""
        return self._chord_pending

    @chord_pending.setter
    def chord_pending(self, value: bool) -> None:
        self._chord_pending = value

    # -- state transitions -------------------------------------------------

    def split(self) -> PaneState:
        """Clone the focused view into a new, focused pane (`ctrl+w v`).

        The clone gets a fresh, never-reused table id. Rejected when the
        workspace is already split — it caps at two panes.

        Returns:
            The new pane, so the caller can mount its table and start its
            watch.
        """
        if self.is_split:
            raise ValueError("workspace is already split")
        self._counter += 1
        pane = self.focused.clone(f"pane-{self._counter}")
        self._panes.append(pane)
        self._focused = len(self._panes) - 1
        return pane

    def focus_other(self) -> PaneState:
        """Move focus to the other pane (`ctrl+w w`).

        Rejected on a single-pane workspace — there is nowhere to move to.

        Returns:
            The newly focused pane.
        """
        if not self.is_split:
            raise ValueError("cannot move focus in a single pane workspace")
        self._focused = 1 - self._focused
        return self.focused

    def focus_index(self, index: int) -> None:
        """Focus the pane at *index*; raise if it does not exist."""
        if not 0 <= index < len(self._panes):
            raise IndexError(f"no pane at index {index}")
        self._focused = index

    def focus_by_table_id(self, table_id: str) -> bool:
        """Focus the pane rendering into *table_id* (a click landed there).

        Returns:
            True if focus actually moved, False when the id is unknown or its
            pane was already focused — so the caller only runs its focus side
            effects on a real change.
        """
        for index, pane in enumerate(self._panes):
            if pane.table_id == table_id:
                if index == self._focused:
                    return False
                self._focused = index
                return True
        return False

    def close_focused(self) -> ClosedPanes:
        """Close the focused pane, back to a single view (`ctrl+w q`).

        The survivor becomes pane 0 and takes focus. Rejected when only one
        pane is left — a workspace always keeps at least one.

        Returns:
            The closed pane and the survivor, for the caller's widget teardown.
        """
        if not self.is_split:
            raise ValueError("cannot close the only pane")
        closing = self._panes.pop(self._focused)
        self._focused = 0
        return ClosedPanes(closing=closing, remaining=self._panes[0])

    def collapse(self) -> ClosedPanes | None:
        """Fold back to a single pane, keeping pane 0 (context-switch teardown).

        Unlike `close_focused`, this always removes the *second* pane
        regardless of focus: the caller resets pane 0's view right after, so
        which pane survives is cosmetic.

        Returns:
            The removed pane and the survivor, or None if already single.
        """
        if not self.is_split:
            return None
        closing = self._panes.pop(1)
        self._focused = 0
        return ClosedPanes(closing=closing, remaining=self._panes[0])
