"""Column-width absorption (issue #210).

Textual recomputes column widths from `on_idle` by re-deriving every new
row's renderables and measuring each cell — 700,000 measurements to seed a
50,000-row view, and ~74% of the freeze when a large kind first loads.

`ResourceTable` already holds the exact cells it emitted, so it folds their
widths into the columns itself and hands the superclass only the rows it did
not account for. These tests pin both halves: that the measuring pass is
skipped, and that the widths it produces are indistinguishable from the ones
Textual would have measured.
"""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import DataTable

from korvid.core.store import Summary
from korvid.k8s.models import PodSummary
from korvid.ui.widgets.resource_table import ResourceTable


class _TableApp(App[None]):
    """Bare host for a `ResourceTable`, so a test drives `show()` directly."""

    def compose(self) -> ComposeResult:
        yield ResourceTable()


def _pods(names: list[str]) -> list[Summary]:
    return [
        PodSummary(
            name=name,
            namespace="default",
            phase="Running",
            ready="1/1",
            restarts=0,
            node=None,
            qos="-",
        )
        for name in names
    ]


def _spy_row_renderables(table: DataTable[Any]) -> list[int]:
    """Record every row whose renderables are rebuilt.

    Textual also rebuilds renderables to *paint*, so only distinct data rows
    matter: painting touches the handful on screen, measuring touches all of
    them. Index -1 is the header row.
    """
    seen: list[int] = []
    original = table._get_row_renderables

    def spy(row_index: int) -> Any:
        seen.append(row_index)
        return original(row_index)

    table._get_row_renderables = spy  # type: ignore[method-assign]  # test spy
    return seen


def _widths(table: DataTable[Any]) -> list[int]:
    return [column.content_width for column in table.ordered_columns]


async def test_seeding_does_not_rebuild_every_row_to_measure_it() -> None:
    """Seeding a view must not cost one renderable rebuild per row."""
    app = _TableApp()
    async with app.run_test(size=(120, 12)) as pilot:
        table = app.query_one(ResourceTable)
        await pilot.pause()
        seen = _spy_row_renderables(table)
        rows = _pods([f"pod-{i:04d}" for i in range(400)])
        table.show("pods", rows, all_namespaces=False, pattern="")
        await pilot.pause()
        assert table._require_update_dimensions is False, "dimension pass did not run"
        touched = {index for index in seen if index >= 0}
        assert len(touched) < 100, f"rebuilt {len(touched)} of 400 rows"


async def test_absorbed_widths_match_what_textual_would_measure() -> None:
    """Widths must be identical to the superclass result for every cell shape."""
    names = [
        "a",
        "pod-with-a-fairly-long-generated-name-0001",
        "파드-매우-긴-한글-이름",
        "[bold]not-markup[/bold]",
        "trailing",
    ]
    app = _TableApp()
    async with app.run_test(size=(120, 12)) as pilot:
        table = app.query_one(ResourceTable)
        await pilot.pause()
        table.show("pods", _pods(names), all_namespaces=True, pattern="")
        await pilot.pause()

        control: DataTable[Any] = DataTable()
        await app.mount(control)
        await pilot.pause()
        control.add_columns(*[column.label for column in table.ordered_columns])
        for key, cells in table._emitted.items():
            control.add_row(*cells, key=key)
        await pilot.pause()

        assert _widths(table) == _widths(control)


async def test_rows_added_outside_show_are_still_measured() -> None:
    """A row this widget did not emit must fall through to the superclass."""
    app = _TableApp()
    async with app.run_test(size=(120, 12)) as pilot:
        table = app.query_one(ResourceTable)
        await pilot.pause()
        table.show("pods", _pods(["short"]), all_namespaces=False, pattern="")
        await pilot.pause()
        long_value = "a-value-added-without-going-through-show"
        table.add_row(long_value, key="extra")
        await pilot.pause()
        assert table.ordered_columns[0].content_width >= len(long_value)


async def test_unabsorbed_row_added_before_the_dimension_pass_is_measured() -> None:
    """Absorbing one batch must not suppress measurement of rows added after
    it but before the idle pass runs."""
    app = _TableApp()
    async with app.run_test(size=(120, 12)) as pilot:
        table = app.query_one(ResourceTable)
        await pilot.pause()
        long_value = "a-value-added-between-show-and-idle-00000"
        table.show("pods", _pods(["short"]), all_namespaces=False, pattern="")
        table.add_row(long_value, key="extra")
        await pilot.pause()
        assert table.ordered_columns[0].content_width >= len(long_value)


async def test_row_appearing_in_place_widens_its_column() -> None:
    """A new row appended by the in-place diff still grows the column."""
    app = _TableApp()
    async with app.run_test(size=(120, 12)) as pilot:
        table = app.query_one(ResourceTable)
        await pilot.pause()
        table.show("pods", _pods(["aaa"]), all_namespaces=False, pattern="")
        await pilot.pause()
        narrow = table.ordered_columns[0].content_width
        appeared = "zzz-a-much-longer-pod-name-than-before"
        table.show("pods", _pods(["aaa", appeared]), all_namespaces=False, pattern="")
        await pilot.pause()
        assert table.ordered_columns[0].content_width > narrow
        assert table.ordered_columns[0].content_width >= len(appeared)
