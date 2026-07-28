"""Cursor stability across refreshes (issue #89): the table cursor must stay
on the selected resource when watch/metrics ticks re-render the rows, clamp
when the selected row disappears, and still reset on view changes."""

from __future__ import annotations

from textual.coordinate import Coordinate

from korvid.ui.widgets.resource_table import ResourceTable

from .test_app import _pod, make_app
from .waits import until


def _cursor_key(table: ResourceTable) -> str | None:
    if table.cursor_row < 0 or table.row_count == 0:
        return None
    return str(table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value)


async def test_cursor_stays_on_selected_row_across_watch_refresh() -> None:
    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        table.focus()
        await pilot.press("down")
        await until(pilot, lambda: _cursor_key(table) == "default/beta", label="cursor on beta")
        # A watch event re-renders the table; the cursor must follow beta.
        app.store.apply_event("pods", "default", "ADDED", _pod("zzz-new"))
        await until(pilot, lambda: table.row_count == 4, label="new pod rendered")
        assert _cursor_key(table) == "default/beta"


async def test_cursor_follows_row_when_refresh_reorders() -> None:
    # A new pod sorting *above* the selection shifts its index: following
    # the old index would silently select the wrong pod.
    app = make_app([_pod("bbb"), _pod("ccc")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        table.focus()
        await pilot.press("down")
        await until(pilot, lambda: _cursor_key(table) == "default/ccc", label="cursor on ccc")
        app.store.apply_event("pods", "default", "ADDED", _pod("aaa"))
        await until(pilot, lambda: table.row_count == 3, label="new pod rendered")
        assert _cursor_key(table) == "default/ccc"


async def test_cursor_clamps_when_selected_row_deleted() -> None:
    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        table.focus()
        await pilot.press("down")
        await pilot.press("down")
        await until(pilot, lambda: _cursor_key(table) == "default/gamma", label="cursor on gamma")
        app.store.apply_event("pods", "default", "DELETED", _pod("gamma"))
        await until(pilot, lambda: table.row_count == 2, label="pod removed")
        # Old index 2 no longer exists: clamp to the last row, don't crash.
        assert table.cursor_row == 1
        assert _cursor_key(table) == "default/beta"


async def test_view_change_resets_cursor_to_top() -> None:
    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        table.focus()
        await pilot.press("down")
        await until(pilot, lambda: _cursor_key(table) == "default/beta", label="cursor on beta")
        # Different logical view (kind changed): reset-to-top is correct.
        table.show("deployments", [], all_namespaces=False, pattern="")
        table.show(
            "pods",
            app.store.get("pods", "default"),
            all_namespaces=False,
            pattern="",
        )
        await pilot.pause()
        assert table.cursor_row == 0


async def test_cursor_follows_row_across_sort_toggle() -> None:
    # Issue #89 acceptance: a sort-order change keeps the cursor on the same
    # row key — it moves with the resource, not the row index.
    app = make_app([_pod("alpha"), _pod("bravo"), _pod("charlie"), _pod("delta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 4, label="pods loaded")
        table.focus()
        await pilot.press("down")
        await until(pilot, lambda: _cursor_key(table) == "default/bravo", label="cursor on bravo")
        await pilot.press("N")  # name ascending: same order, cursor must hold
        await until(pilot, lambda: _cursor_key(table) == "default/bravo", label="held ascending")
        await pilot.press("N")  # name descending: bravo shifts from index 1 to 2
        await until(
            pilot,
            lambda: table.get_row_at(0)[0] == "delta",
            label="descending order rendered",
        )
        assert _cursor_key(table) == "default/bravo"
