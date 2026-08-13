"""In-place table refresh: a pure data tick (same view, same sort) must not
clear-and-rebuild the DataTable — changed cells update in place, vanished rows
are removed, and new rows that land at the bottom are appended. Only an actual
reorder (e.g. a pod inserted mid-table) falls back to the rebuild path."""

from __future__ import annotations

from typing import Any

from rich.text import Text

from korvid.ui.widgets.resource_table import ResourceTable, _cell_width, _cells_equal

from .test_app import _pod, make_app
from .waits import until


def _spy_clear(table: ResourceTable) -> list[bool]:
    """Record every clear() call on *table* (True = columns cleared too)."""
    calls: list[bool] = []
    original = table.clear

    def spy(columns: bool = False) -> Any:
        calls.append(columns)
        return original(columns=columns)

    table.clear = spy  # type: ignore[method-assign]  # test spy
    return calls


async def test_modified_pod_updates_cells_without_clear() -> None:
    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        calls = _spy_clear(table)
        app.store.apply_event("pods", "default", "MODIFIED", _pod("beta", phase="CrashLoopBackOff"))
        await until(
            pilot,
            lambda: str(table.get_row("default/beta")[2]) == "CrashLoopBackOff",
            label="phase cell updated",
        )
        assert calls == []


async def test_deleted_pod_removed_without_clear() -> None:
    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        calls = _spy_clear(table)
        app.store.apply_event("pods", "default", "DELETED", _pod("beta"))
        await until(pilot, lambda: table.row_count == 2, label="pod removed")
        assert calls == []
        assert [row.key.value for row in table.ordered_rows] == [
            "default/alpha",
            "default/gamma",
        ]


async def test_added_pod_at_bottom_appends_without_clear() -> None:
    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        calls = _spy_clear(table)
        app.store.apply_event("pods", "default", "ADDED", _pod("zzz-new"))
        await until(pilot, lambda: table.row_count == 3, label="new pod rendered")
        assert calls == []
        assert [row.key.value for row in table.ordered_rows] == [
            "default/alpha",
            "default/beta",
            "default/zzz-new",
        ]


async def test_added_pod_mid_table_falls_back_to_rebuild() -> None:
    # add_row can only append: a pod that sorts above existing rows needs
    # the rebuild path, which preserves cursor and viewport (issue #89).
    app = make_app([_pod("bbb"), _pod("ccc")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        calls = _spy_clear(table)
        app.store.apply_event("pods", "default", "ADDED", _pod("aaa"))
        await until(pilot, lambda: table.row_count == 3, label="new pod rendered")
        assert calls == [False]
        assert [row.key.value for row in table.ordered_rows] == [
            "default/aaa",
            "default/bbb",
            "default/ccc",
        ]


async def test_scroll_untouched_by_in_place_update() -> None:
    # The in-place path never resets the scroll offset, so no restore
    # (and no transient snap) is needed at all.
    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        await until(pilot, lambda: table.max_scroll_x >= 10, label="table wider than viewport")
        table.scroll_to(x=10, animate=False, immediate=True, force=True)
        await until(pilot, lambda: table.scroll_x == 10, label="scrolled right")
        calls = _spy_clear(table)
        app.store.apply_event("pods", "default", "MODIFIED", _pod("beta", phase="Pending"))
        await until(
            pilot,
            lambda: str(table.get_row("default/beta")[2]) == "Pending",
            label="phase cell updated",
        )
        assert calls == []
        assert table.scroll_x == 10


async def test_viewport_survives_in_place_delete_above_cursor() -> None:
    # Deleting a row above the cursor slides the cursor index up; even with
    # move_cursor(scroll=False) Textual's cursor watcher schedules a
    # deferred _scroll_cursor_into_view, which would snap a vertically
    # scrolled viewport back to the cursor row on the in-place path.
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        table.focus()
        for _ in range(30):
            await pilot.press("down")
        await until(pilot, lambda: table.cursor_row == 30, label="cursor below the fold")
        table.scroll_to(y=0, animate=False, immediate=True, force=True)
        await until(pilot, lambda: table.scroll_y == 0, label="viewport back at top")
        calls = _spy_clear(table)
        app.store.apply_event("pods", "default", "DELETED", _pod("pod-00"))
        await until(pilot, lambda: table.row_count == 39, label="row removed")
        assert calls == []
        assert table.cursor_row == 29  # cursor followed pod-30 up one slot
        assert table.scroll_y == 0


def test_cell_width_measures_str_cells_as_rendered_markup() -> None:
    # DataTable renders str cells through Text.from_markup, so the width
    # guard must measure the rendered text, not the raw markup: otherwise a
    # markup-valued custom column (issue #45) like "[red]x[/red]" measures
    # 12 here, requests update_width=True, and Textual's rescan shrinks the
    # column to 1 — the exact layout jump the guard exists to prevent.
    assert _cell_width("[red]x[/red]") == 1
    assert _cell_width("plain") == 5
    assert _cell_width(Text("Running", style="green")) == 7


def test_cell_width_measures_multiline_cells_by_widest_line() -> None:
    # For height-1 rows DataTable truncates str cells at the first newline
    # before measuring, but measures Text cells by their widest rendered
    # line. Mismatched measurement would falsely report growth and request
    # a column-shrinking width rescan.
    assert _cell_width("abcdefghij\nk") == 10
    assert _cell_width("x\nvery-wide") == 1
    assert _cell_width("abc\n") == 3
    assert _cell_width(Text("abcdefghij\nk")) == 10
    assert _cell_width(Text("x\nvery-wide")) == 9
    assert _cell_width("") == 0


async def test_in_place_update_does_not_move_unchanged_cursor() -> None:
    # The cursor already stays on beta when the row key survives in place;
    # moving it again would only trigger a no-op repaint.
    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        table.focus()
        await pilot.press("down")
        await until(pilot, lambda: table.cursor_row == 1, label="cursor on beta")
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        original = table.move_cursor

        def spy(*args: Any, **kwargs: Any) -> None:
            calls.append((args, kwargs))
            original(*args, **kwargs)

        table.move_cursor = spy  # type: ignore[method-assign]  # test spy
        app.store.apply_event("pods", "default", "MODIFIED", _pod("beta", phase="Pending"))
        await until(
            pilot,
            lambda: str(table.get_row("default/beta")[2]) == "Pending",
            label="phase cell updated",
        )

        assert calls == []
        assert table.cursor_row == 1


async def test_no_deferred_scroll_scheduled_when_cursor_unchanged() -> None:
    # The deferred viewport re-assert exists only to counter Textual's
    # deferred _scroll_cursor_into_view, which is scheduled solely on a
    # cursor-index change. Scheduling it on every in-place refresh opens a
    # window where a user scroll landing before the callback is yanked
    # back to the stale captured offset.
    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        scheduled: list[Any] = []
        original = table.call_after_refresh

        def spy(callback: Any, *args: Any, **kwargs: Any) -> Any:
            scheduled.append(callback)
            return original(callback, *args, **kwargs)

        table.call_after_refresh = spy  # type: ignore[method-assign]  # test spy
        app.store.apply_event("pods", "default", "MODIFIED", _pod("beta", phase="Pending"))
        await until(
            pilot,
            lambda: str(table.get_row("default/beta")[2]) == "Pending",
            label="phase cell updated",
        )
        assert table.scroll_to not in scheduled


def test_cells_equal_is_style_aware() -> None:
    # rich.Text.__eq__ compares plain text only — a phase flipping color
    # with the same wording must still count as a change.
    assert _cells_equal(Text("Running", style="green"), Text("Running", style="green"))
    assert not _cells_equal(Text("Running", style="green"), Text("Running", style="red"))
    assert _cells_equal("5m", "5m")
    assert not _cells_equal("5m", "6m")
    assert not _cells_equal(Text("5m"), "5m")


async def test_column_width_does_not_shrink_on_in_place_update() -> None:
    # DataTable's update_cell(update_width=True) is NOT grow-only: a
    # narrower replacement rescans the column and shrinks content_width,
    # shifting every column to its right — the exact jitter this diff path
    # exists to prevent. Width updates are only requested for wider cells.
    app = make_app([_pod("alpha", phase="CrashLoopBackOff"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        status = table.ordered_columns[2]
        await until(
            pilot,
            lambda: status.content_width >= len("CrashLoopBackOff"),
            label="status column sized to widest value",
        )
        wide = status.content_width
        app.store.apply_event("pods", "default", "MODIFIED", _pod("alpha", phase="Running"))
        await until(
            pilot,
            lambda: str(table.get_row("default/alpha")[2]) == "Running",
            label="phase cell updated",
        )
        await pilot.pause()  # let on_idle run any pending width recompute
        assert status.content_width == wide


async def test_column_width_grows_for_wider_in_place_value() -> None:
    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        status = table.ordered_columns[2]
        assert status.content_width < len("CrashLoopBackOff")
        app.store.apply_event(
            "pods", "default", "MODIFIED", _pod("alpha", phase="CrashLoopBackOff")
        )
        await until(
            pilot,
            lambda: status.content_width >= len("CrashLoopBackOff"),
            label="status column widened for new value",
        )
        assert status.content_width >= len("CrashLoopBackOff")


async def test_cell_count_mismatch_falls_back_to_rebuild() -> None:
    # A row with fewer cells than columns would raise ValueError from the
    # diff's zip(strict=True) mid-update. The eligibility guard declines
    # before touching the table and delegates to the rebuild path, where
    # add_row pads short rows. (Overlong rows cannot occur: _emit_row sizes
    # custom extras to the view's column count.)
    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        short: list[str | Text] = ["alpha", "1/1"]
        assert table._apply_in_place([("default/alpha", short), ("default/beta", short)]) is False


async def test_bulk_removal_falls_back_to_rebuild() -> None:
    # DataTable.remove_row rebuilds its whole row-location map per call, so
    # removing rows one by one is O(rows x removals). A filter change can
    # drop most of a large list in one show() — that must take the linear
    # rebuild path, keeping in-place for small watch-sized deletions.
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        keep = [row.key.value for row in table.ordered_rows][:2]
        pending = [(key, list(table.get_row(key))) for key in keep if key is not None]
        assert table._apply_in_place(pending) is False
