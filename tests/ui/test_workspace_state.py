"""Direct contract tests for `WorkspaceState` (issue #48, deep task 2).

`WorkspaceState` is the pure-Python owner of the workspace model: the pane
collection, the focused-pane index, the monotonic table-id counter, the
`ctrl+w` chord flag, and the focused pane's view state (kind/scope/filter/
sort/drill). These tests drive the model with no Textual app running, so the
invariants are pinned independently of the widget tree that applies them.
"""

from __future__ import annotations

import pytest

from korvid.core.filters import parse_filter
from korvid.core.sorting import toggle_sort
from korvid.ui.navigation import DrillLevel
from korvid.ui.workspace_state import PaneState, WorkspaceState


def _split_workspace() -> WorkspaceState:
    """A two-pane workspace with the clone focused (pane index 1)."""
    ws = WorkspaceState("pods", "default")
    ws.split()
    return ws


def test_initial_workspace_has_one_pane_on_the_default_scope() -> None:
    """A fresh workspace holds exactly one pane, focused, on the given view."""
    ws = WorkspaceState("pods", "kube-system")

    assert ws.pane_count == 1
    assert not ws.is_split
    assert ws.focused_index == 0
    assert ws.focused.kind == "pods"
    assert ws.focused.scope == "kube-system"
    assert ws.focused.table_id == "pane-0"


def test_focused_view_state_delegates_to_the_focused_pane() -> None:
    """kind/scope/namespace/filter/resource_filter/sorts/drill all read and
    write through the focused pane, so every action targets the current view."""
    ws = WorkspaceState("pods", "default")

    ws.current_kind = "deployments"
    ws.current_scope = "web"
    ws.filter_pattern = "api"
    ws.resource_filter = parse_filter("api")

    assert ws.current_kind == ws.focused.kind == "deployments"
    assert ws.current_scope == ws.focused.scope == "web"
    assert ws.current_namespace == "web"  # namespace aliases scope
    assert ws.filter_pattern == ws.focused.filter_pattern == "api"
    assert ws.resource_filter is ws.focused.resource_filter
    # sorts/drill are the pane's own mutable state, exposed live.
    assert ws.sorts is ws.focused.sorts
    assert ws.drill is ws.focused.drill
    ws.sorts["deployments"] = toggle_sort(None, "name")
    assert ws.focused.sorts["deployments"].column == "name"


def test_current_namespace_writes_through_to_scope() -> None:
    """The namespace alias is a second name for the same scope field."""
    ws = WorkspaceState("pods", "default")

    ws.current_namespace = "prod"

    assert ws.current_scope == "prod"
    assert ws.focused.scope == "prod"


def test_split_clones_the_focused_view_with_a_unique_table_id() -> None:
    """Splitting starts the new pane as a clone of the focused view, but with
    its own, never-before-used table id, and focuses it."""
    ws = WorkspaceState("pods", "default")
    ws.current_kind = "deployments"
    ws.filter_pattern = "web"
    ws.resource_filter = parse_filter("web")
    ws.drill.push(DrillLevel("deployments", "web", "default", "dep-1", "pods"))

    new_pane = ws.split()

    assert ws.pane_count == 2
    assert ws.is_split
    assert ws.focused_index == 1
    assert ws.focused is new_pane
    assert new_pane.table_id == "pane-1"
    assert new_pane.table_id != ws.panes[0].table_id
    # A clone of the source view state...
    assert new_pane.kind == "deployments"
    assert new_pane.filter_pattern == "web"
    assert new_pane.drill.breadcrumb() == ws.panes[0].drill.breadcrumb()
    # ...but an independent drill stack.
    new_pane.drill.pop()
    assert ws.panes[0].drill.active


def test_split_is_rejected_when_already_split() -> None:
    """The workspace caps at two panes: a second split is an error."""
    ws = _split_workspace()

    with pytest.raises(ValueError, match="already split"):
        ws.split()


def test_focus_other_toggles_between_the_two_panes() -> None:
    """`ctrl+w w` moves focus across; the focused view follows."""
    ws = _split_workspace()
    ws.panes[0].kind = "services"
    assert ws.focused_index == 1

    focused = ws.focus_other()

    assert ws.focused_index == 0
    assert focused is ws.panes[0]
    assert ws.current_kind == "services"
    assert ws.focus_other() is ws.panes[1]


def test_focus_other_is_rejected_on_a_single_pane() -> None:
    """There is nowhere to move focus to in a single-pane workspace."""
    ws = WorkspaceState("pods", "default")

    with pytest.raises(ValueError, match="single pane"):
        ws.focus_other()


def test_focus_index_selects_an_existing_pane() -> None:
    """Focus can be set to any existing pane; out-of-range is rejected."""
    ws = _split_workspace()

    ws.focus_index(0)
    assert ws.focused_index == 0

    with pytest.raises(IndexError, match="no pane"):
        ws.focus_index(2)


def test_focus_by_table_id_reports_whether_focus_moved() -> None:
    """Clicking a pane focuses it; the return says whether focus changed so
    the caller only runs its side effects on a real change."""
    ws = _split_workspace()  # focus on pane 1
    other_id = ws.panes[0].table_id

    assert ws.focus_by_table_id(other_id) is True
    assert ws.focused_index == 0
    # Re-selecting the already-focused pane is a no-op.
    assert ws.focus_by_table_id(other_id) is False
    # An unknown table id never moves focus.
    assert ws.focus_by_table_id("pane-does-not-exist") is False
    assert ws.focused_index == 0


def test_close_focused_returns_the_closed_and_surviving_panes() -> None:
    """`ctrl+w q` removes the focused pane; the survivor becomes pane 0 and
    focus returns to it. The transition names both panes for the caller."""
    ws = _split_workspace()  # focus on pane 1 (the clone)
    survivor = ws.panes[0]
    clone = ws.panes[1]

    result = ws.close_focused()

    assert result.closing is clone
    assert result.remaining is survivor
    assert ws.pane_count == 1
    assert not ws.is_split
    assert ws.focused_index == 0
    assert ws.focused is survivor


def test_closing_the_only_pane_is_rejected() -> None:
    """A workspace always keeps at least one pane."""
    ws = WorkspaceState("pods", "default")

    with pytest.raises(ValueError, match="only pane"):
        ws.close_focused()


def test_collapse_removes_the_second_pane_regardless_of_focus() -> None:
    """Context-switch teardown folds back to pane 0 even when pane 1 is
    focused; it returns the removed and surviving panes, or None if single."""
    ws = _split_workspace()  # focus on pane 1
    survivor = ws.panes[0]
    extra = ws.panes[1]

    result = ws.collapse()

    assert result is not None
    assert result.closing is extra
    assert result.remaining is survivor
    assert ws.pane_count == 1
    assert ws.focused_index == 0
    assert ws.focused is survivor
    # Collapsing a single pane is a no-op.
    assert ws.collapse() is None


def test_table_ids_are_monotonic_and_never_reused() -> None:
    """A survivor keeps its widget across split/close cycles, so ids must
    stay unique: split -> close -> split must not hand out `pane-1` twice."""
    ws = WorkspaceState("pods", "default")

    first = ws.split()
    assert first.table_id == "pane-1"
    ws.close_focused()
    second = ws.split()

    assert second.table_id == "pane-2"
    assert second.table_id != first.table_id


def test_pane_collection_is_read_only() -> None:
    """`panes` is a tuple snapshot: callers cannot append/replace panes and
    corrupt the invariants behind the transition methods."""
    ws = WorkspaceState("pods", "default")

    panes = ws.panes
    assert isinstance(panes, tuple)
    with pytest.raises((AttributeError, TypeError)):
        panes.append(PaneState("pods", "default", "pane-9"))  # type: ignore[attr-defined]  # tuple has no append
    # A returned snapshot never grows as the real workspace splits.
    ws.split()
    assert len(panes) == 1
    assert ws.pane_count == 2


def test_chord_pending_is_owned_by_the_workspace() -> None:
    """The `ctrl+w` chord-pending flag lives on the workspace."""
    ws = WorkspaceState("pods", "default")

    assert ws.chord_pending is False
    ws.chord_pending = True
    assert ws.chord_pending is True
    ws.chord_pending = False
    assert ws.chord_pending is False


def test_contains_reports_pane_membership() -> None:
    """`contains` answers whether a captured pane is still in the workspace,
    so a queued flow can tell its initiating pane was closed."""
    ws = _split_workspace()
    clone = ws.panes[1]

    assert ws.contains(clone)
    ws.close_focused()
    assert not ws.contains(clone)
