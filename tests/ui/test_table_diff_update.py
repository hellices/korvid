"""In-place table refresh: a pure data tick (same view, same sort) must not
clear-and-rebuild the DataTable — changed cells update in place, vanished rows
are removed, and new rows that land at the bottom are appended. Only an actual
reorder (e.g. a pod inserted mid-table) falls back to the rebuild path."""

from __future__ import annotations

import re
from typing import Any

from korvid.k8s.metrics import PodMetrics
from korvid.ui.widgets.resource_table import ResourceTable

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


def _spy_emit(table: ResourceTable) -> list[str]:
    built: list[str] = []
    original = table._emit_row

    def spy(obj: Any, cells: Any, **kwargs: Any) -> Any:
        built.append(f"{obj.namespace}/{obj.name}")
        return original(obj, cells, **kwargs)

    table._emit_row = spy  # type: ignore[method-assign]  # documented performance contract
    return built


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


async def test_repaint_rebuilds_only_the_changed_row() -> None:
    """The documented row memo keeps repaint work proportional to changed rows."""
    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        built = _spy_emit(table)

        app.store.apply_event("pods", "default", "MODIFIED", _pod("beta", phase="Failed"))
        await until(
            pilot,
            lambda: str(table.get_row("default/beta")[2]) == "Failed",
            label="phase cell updated",
        )

        assert built == ["default/beta"]


async def test_unchanged_repaint_rebuilds_nothing() -> None:
    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        built = _spy_emit(table)

        table.show("pods", app.store.get("pods", "default"), all_namespaces=False, pattern="")

        assert built == []


async def test_metrics_refresh_for_an_unchanged_summary() -> None:
    samples: dict[tuple[str, str], PodMetrics] = {}

    def lookup(namespace: str, name: str) -> PodMetrics | None:
        return samples.get((namespace, name))

    app = make_app([_pod("alpha")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        rows = app.store.get("pods", "default")
        table.show("pods", rows, all_namespaces=False, pattern="", metrics=lookup)
        assert str(table.get_row("default/alpha")[4]) == "-"

        samples[("default", "alpha")] = PodMetrics(
            name="alpha",
            namespace="default",
            cpu_cores=0.5,
            memory_bytes=1024 * 1024,
        )
        table.show("pods", rows, all_namespaces=False, pattern="", metrics=lookup)

        assert str(table.get_row("default/alpha")[4]) != "-"


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


async def test_removing_a_row_above_the_cursor_restores_the_selection() -> None:
    """Textual's cursor is a coordinate, so removing a row above it keeps the
    index and slides a different row under the selection. The in-place path
    skips cursor work only when the selected *key* is unchanged, which is what
    makes that case restore rather than silently reselect its neighbour."""
    app = make_app([_pod(f"pod-{i:02d}") for i in range(10)])
    async with app.run_test(size=(120, 20)) as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 10, label="pods loaded")
        table.focus()
        table.move_cursor(row=5)
        await pilot.pause()
        assert table._cursor_snapshot() == ("default/pod-05", 5)

        app.store.apply_event("pods", "default", "DELETED", _pod("pod-01"))
        await until(pilot, lambda: table.row_count == 9, label="row removed")

        assert table._cursor_snapshot() == ("default/pod-05", 4)
