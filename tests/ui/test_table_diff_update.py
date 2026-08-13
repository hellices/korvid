"""In-place table refresh: a pure data tick (same view, same sort) must not
clear-and-rebuild the DataTable — changed cells update in place, vanished rows
are removed, and new rows that land at the bottom are appended. Only an actual
reorder (e.g. a pod inserted mid-table) falls back to the rebuild path."""

from __future__ import annotations

import re
from typing import Any

import pytest
from rich.text import Text

from korvid.ui.widgets import resource_table
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


def _spy_refresh(table: ResourceTable) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Record every refresh() call on *table* as its (args, kwargs) pair."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    original = table.refresh

    def spy(*regions: Any, **kwargs: Any) -> Any:
        calls.append((regions, kwargs))
        return original(*regions, **kwargs)

    table.refresh = spy  # type: ignore[method-assign]  # test spy
    return calls


def _plain_refreshes(
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]],
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """The no-argument `refresh()` calls — the repaint the diff decides on.

    Layout and region refreshes (`refresh(layout=True)`, `refresh(region)`)
    are *not* counted: Textual schedules those for its own reasons — a
    pending scrollbar pass, a virtual-size change from the idle dimension
    pass — and their arrival is timing dependent, so counting them makes the
    assertion flake. Only `refresh()` with no arguments is a decision this
    widget makes in `_apply_in_place`.
    """
    return [call for call in calls if call == ((), {})]


def _bottom_painted_pod(table: ResourceTable) -> str:
    """The pod name Textual actually paints on the table's last line.

    Read back out of `render_line` rather than recomputed from the scroll
    offset, so the test asserts against Textual's own painting contract
    instead of restating the production formula.
    """
    text = table.render_line(table.size.height - 1).text
    match = re.search(r"pod-\d\d", text)
    assert match is not None, f"no pod rendered on the last line: {text!r}"
    return match.group(0)


def _painted_pods(table: ResourceTable) -> list[str]:
    """Every pod name Textual paints, top line to bottom line."""
    names = []
    for line in range(table.size.height):
        match = re.search(r"pod-\d\d", table.render_line(line).text)
        if match is not None:
            names.append(match.group(0))
    assert names, "no pods rendered at all"
    return names


def _painted_line(table: ResourceTable, name: str) -> str:
    """The rendered line *name* currently occupies, straight from Textual."""
    for line in range(table.size.height):
        text = table.render_line(line).text
        if name in text:
            return text
    raise AssertionError(f"{name} is not painted")


def _spy_row_region(table: ResourceTable) -> list[int]:
    """Record every `_get_row_region` call — the per-row O(N) geometry walk."""
    calls: list[int] = []
    original = table._get_row_region

    def spy(row_index: int) -> Any:
        calls.append(row_index)
        return original(row_index)

    table._get_row_region = spy  # type: ignore[method-assign]  # test spy
    return calls


async def test_offscreen_cell_update_changes_data_without_repaint() -> None:
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        await until(pilot, lambda: table.max_scroll_y > 0, label="table scrollable")
        calls = _spy_refresh(table)

        app.store.apply_event("pods", "default", "MODIFIED", _pod("pod-39", phase="Pending"))
        await until(
            pilot,
            lambda: str(table.get_row("default/pod-39")[2]) == "Pending",
            label="offscreen cell updated",
        )

        assert _plain_refreshes(calls) == []


async def test_bottom_painted_row_repaints_at_a_fractional_scroll_offset() -> None:
    # Textual paints from `scroll_offset.y`, which *rounds* `scroll_y`.
    # Truncating instead (int()) shifts the window down by one row at any
    # offset that rounds up, so the last painted row is misread as
    # off-screen and its change never reaches the screen.
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        await until(pilot, lambda: table.max_scroll_y > 0, label="table scrollable")
        table.scroll_to(y=3.6, animate=False, immediate=True, force=True)
        await until(pilot, lambda: table.scroll_offset.y == 4, label="fractional scroll settled")
        assert table.scroll_y == 3.6  # genuinely fractional, rounds up to 4
        name = _bottom_painted_pod(table)
        calls = _spy_refresh(table)

        app.store.apply_event("pods", "default", "MODIFIED", _pod(name, phase="Pending"))
        await until(
            pilot,
            lambda: str(table.get_row(f"default/{name}")[2]) == "Pending",
            label="bottom painted cell updated",
        )

        assert len(_plain_refreshes(calls)) == 1


async def test_row_scrolled_above_the_window_does_not_repaint() -> None:
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        await until(pilot, lambda: table.max_scroll_y > 0, label="table scrollable")
        table.scroll_to(y=6, animate=False, immediate=True, force=True)
        await until(pilot, lambda: table.scroll_offset.y == 6, label="scrolled down")
        above = _painted_pods(table)[0]
        assert above == "pod-06"
        calls = _spy_refresh(table)

        app.store.apply_event("pods", "default", "MODIFIED", _pod("pod-00", phase="Pending"))
        await until(
            pilot,
            lambda: str(table.get_row("default/pod-00")[2]) == "Pending",
            label="scrolled-past cell updated",
        )

        assert _plain_refreshes(calls) == []


async def test_fixed_row_repaints_even_when_scrolled_past() -> None:
    # A fixed row is painted at the top of the viewport whatever the scroll
    # offset, so it is always inside the repaint window.
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        await until(pilot, lambda: table.max_scroll_y > 0, label="table scrollable")
        table.fixed_rows = 1
        table.scroll_to(y=6, animate=False, immediate=True, force=True)
        await until(pilot, lambda: table.scroll_offset.y == 6, label="scrolled down")
        assert _painted_pods(table)[0] == "pod-00"
        calls = _spy_refresh(table)

        app.store.apply_event("pods", "default", "MODIFIED", _pod("pod-00", phase="Pending"))
        await until(
            pilot,
            lambda: str(table.get_row("default/pod-00")[2]) == "Pending",
            label="fixed row cell updated",
        )

        assert len(_plain_refreshes(calls)) == 1


async def test_visibility_decision_does_not_walk_row_geometry() -> None:
    # `_get_row_region` sums every preceding row's height, so consulting it
    # per changed row makes one watch tick O(rows x changes). The batch
    # computes index bounds once instead.
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        await until(pilot, lambda: table.max_scroll_y > 0, label="table scrollable")
        regions = _spy_row_region(table)

        app.store.apply_event("pods", "default", "MODIFIED", _pod("pod-00", phase="Pending"))
        app.store.apply_event("pods", "default", "MODIFIED", _pod("pod-39", phase="Pending"))
        await until(
            pilot,
            lambda: str(table.get_row("default/pod-39")[2]) == "Pending",
            label="both cells updated",
        )

        assert regions == []


async def test_visible_cell_update_repaints_once() -> None:
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        await until(pilot, lambda: table.max_scroll_y > 0, label="table scrollable")
        calls = _spy_refresh(table)

        app.store.apply_event("pods", "default", "MODIFIED", _pod("pod-00", phase="Pending"))
        await until(
            pilot,
            lambda: str(table.get_row("default/pod-00")[2]) == "Pending",
            label="visible cell updated",
        )

        assert len(_plain_refreshes(calls)) == 1


async def test_visible_width_growth_requests_immediate_repaint() -> None:
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        await until(pilot, lambda: table.max_scroll_y > 0, label="table scrollable")
        status = table.ordered_columns[2]
        original_width = status.content_width
        assert original_width < len("CrashLoopBackOff")
        calls = _spy_refresh(table)

        app.store.apply_event(
            "pods", "default", "MODIFIED", _pod("pod-00", phase="CrashLoopBackOff")
        )
        await until(
            pilot,
            lambda: str(table.get_row("default/pod-00")[2]) == "CrashLoopBackOff",
            label="visible width-growing cell updated",
        )

        assert len(_plain_refreshes(calls)) == 1
        await until(
            pilot,
            lambda: status.content_width > original_width,
            label="visible width absorbed",
        )


async def test_offscreen_width_growth_still_repaints() -> None:
    # Off-screen growth does not take the plain-refresh path: absorbing the
    # wider column sets `_require_update_dimensions`, and Textual's idle
    # dimension pass republishes `virtual_size`, which repaints via layout
    # without any unrelated scroll/sort/table operation to wake it up.
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        await until(pilot, lambda: table.max_scroll_y > 0, label="table scrollable")
        status = table.ordered_columns[2]
        original_width = status.content_width
        original_virtual_width = table.virtual_size.width
        calls = _spy_refresh(table)

        app.store.apply_event(
            "pods", "default", "MODIFIED", _pod("pod-39", phase="CrashLoopBackOff")
        )
        await until(
            pilot,
            lambda: status.content_width > original_width,
            label="offscreen width absorbed",
        )
        await until(
            pilot,
            lambda: table.virtual_size.width > original_virtual_width,
            label="virtual width republished from idle layout",
        )

        assert _plain_refreshes(calls) == []


async def test_offscreen_update_is_painted_once_scrolled_into_view() -> None:
    # The repaint skipped above must not survive as a stale cell: the row is
    # painted first (populating DataTable's line cache), updated while it is
    # off-screen, then scrolled back in. Batching bumps `_update_count`,
    # which is part of that cache's key, so the new value wins.
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        await until(pilot, lambda: table.max_scroll_y > 0, label="table scrollable")
        table.scroll_end(animate=False, immediate=True, force=True)
        await until(pilot, lambda: "pod-39" in _painted_pods(table), label="pod-39 painted once")
        assert "Running" in _painted_line(table, "pod-39")
        table.scroll_home(animate=False, immediate=True, force=True)
        await until(
            pilot, lambda: "pod-39" not in _painted_pods(table), label="pod-39 scrolled off"
        )
        calls = _spy_refresh(table)

        app.store.apply_event("pods", "default", "MODIFIED", _pod("pod-39", phase="Pending"))
        await until(
            pilot,
            lambda: str(table.get_row("default/pod-39")[2]) == "Pending",
            label="offscreen cell updated",
        )
        assert _plain_refreshes(calls) == []

        table.scroll_end(animate=False, immediate=True, force=True)
        await until(pilot, lambda: "pod-39" in _painted_pods(table), label="pod-39 scrolled in")

        assert "Pending" in _painted_line(table, "pod-39")


async def test_patching_a_row_advances_the_cache_generation() -> None:
    # Synchronous companion to the scroll-back test above: no event loop turn
    # runs between the two reads, so nothing else (a Resize, a sort) can
    # advance the generation and mask a missing bump.
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        cells: list[str | Text] = list(table.get_row("default/pod-39"))
        cells[2] = Text("Pending")
        before = table._update_count

        assert table._patch_row("default/pod-39", cells, table.ordered_columns) is True

        assert table._update_count == before + 1


async def test_cell_writes_fall_back_to_update_cell_on_an_unverified_textual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Another Textual major may have moved `_data`/`_update_count`, so the
    # private batching is disabled and the public API keeps the cell correct.
    monkeypatch.setattr(resource_table, "_BATCH_CELL_WRITES", False)
    app = make_app([_pod(f"pod-{i:02d}") for i in range(40)])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 40, label="pods loaded")
        await until(pilot, lambda: table.max_scroll_y > 0, label="table scrollable")
        updates: list[tuple[Any, ...]] = []
        original = table.update_cell

        def spy(*args: Any, **kwargs: Any) -> Any:
            updates.append(args)
            return original(*args, **kwargs)

        table.update_cell = spy  # type: ignore[method-assign]  # test spy

        app.store.apply_event("pods", "default", "MODIFIED", _pod("pod-39", phase="Pending"))
        await until(
            pilot,
            lambda: str(table.get_row("default/pod-39")[2]) == "Pending",
            label="offscreen cell updated through update_cell",
        )

        assert updates
        table.scroll_end(animate=False, immediate=True, force=True)
        await until(pilot, lambda: "pod-39" in _painted_pods(table), label="pod-39 scrolled in")
        assert "Pending" in _painted_line(table, "pod-39")


def test_cell_batching_is_limited_to_the_verified_textual_major() -> None:
    assert resource_table._supports_cell_batching("8.0.0") is True
    assert resource_table._supports_cell_batching("8.2.8") is True
    assert resource_table._supports_cell_batching("9.0.0") is False
    assert resource_table._supports_cell_batching("7.9.9") is False
    assert resource_table._supports_cell_batching("unreleased") is False


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
