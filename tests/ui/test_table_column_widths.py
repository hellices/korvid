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

from rich.text import Text
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
    return _pods_with_phase([(name, "Running") for name in names])


def _pods_with_phase(rows: list[tuple[str, str]]) -> list[Summary]:
    return [
        PodSummary(
            name=name,
            namespace="default",
            phase=phase,
            ready="1/1",
            restarts=0,
            node=None,
            qos="-",
        )
        for name, phase in rows
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


def _spy_column_rescan(table: DataTable[Any]) -> list[Any]:
    """Record full-column width rescans.

    `_update_column_widths` reads a whole column back out — and measures every
    cell in it — only when a queued cell looks *narrower* than the column it
    sits in. `get_column` is that read, so any call means the O(total rows)
    rescan this widget exists to avoid has just run.
    """
    seen: list[Any] = []
    original = table.get_column

    def spy(column_key: Any) -> Any:
        seen.append(column_key)
        return original(column_key)

    table.get_column = spy  # type: ignore[method-assign]  # test spy
    return seen


async def test_widening_new_row_does_not_trigger_a_full_column_rescan() -> None:
    """A repaint that both widens a column and changes an existing cell must
    not make Textual re-measure the whole column.

    Textual drains queued cell updates *before* the dimension pass. If the
    column has already been widened by then, the queued cell reads as a
    shrink and every cell in the column is measured again — reintroducing the
    very cost this widget avoids.
    """
    app = _TableApp()
    async with app.run_test(size=(120, 12)) as pilot:
        table = app.query_one(ResourceTable)
        await pilot.pause()
        table.show(
            "pods",
            _pods_with_phase([("alpha", "Running"), ("beta", "Running")]),
            all_namespaces=False,
            pattern="",
        )
        await pilot.pause()
        rescans = _spy_column_rescan(table)
        table.show(
            "pods",
            _pods_with_phase(
                [
                    ("alpha", "Running"),
                    ("beta", "CrashLoop"),
                    ("zeta", "ContainerCreating"),
                ]
            ),
            all_namespaces=False,
            pattern="",
        )
        await pilot.pause()
        assert rescans == [], f"rescanned {len(rescans)} column(s)"
        status = table.ordered_columns[2]
        assert status.content_width >= len("ContainerCreating")


async def test_absorption_never_skips_a_labelled_or_auto_height_row() -> None:
    """Width absorption must not swallow the rest of the dimension pass.

    Textual's `_update_dimensions` also assigns auto-height rows their height
    and widens the row-label column. Absorption only accounts for cell
    *widths*, so a row carrying either of those must still reach the
    superclass — otherwise a future `height=None` row would render zero rows
    tall and a labelled row would never size its label column.
    """
    app = _TableApp()
    async with app.run_test(size=(120, 12)) as pilot:
        table = app.query_one(ResourceTable)
        await pilot.pause()
        table.show(
            "pods",
            _pods(["alpha", "beta"]),
            all_namespaces=False,
            pattern="",
        )
        await pilot.pause()

        first, second = table.ordered_rows
        first.auto_height = True
        first.height = 0
        second.label = Text("a-very-long-row-label")

        table._absorbed.update(key for key in (first.key.value, second.key.value) if key)
        table._update_dimensions([first.key, second.key])

        assert first.height > 0, "auto-height row was skipped and stayed 0 tall"
        assert table._label_column.content_width >= len("a-very-long-row-label")


async def test_a_repaint_that_widens_nothing_leaves_no_stale_skip() -> None:
    """Absorption must not license skipping a row it never accounted for.

    When a repaint absorbs widths but grows no column, no dimension pass is
    scheduled. If the "widths were absorbed" state survives to whatever pass
    runs next, that pass filters out rows this widget merely *emitted* once —
    including a row re-added since, whose wider content then never reaches
    the column.
    """
    app = _TableApp()
    async with app.run_test(size=(120, 12)) as pilot:
        table = app.query_one(ResourceTable)
        await pilot.pause()
        table.show("pods", _pods(["alpha", "beta"]), all_namespaces=False, pattern="")
        await pilot.pause()

        # A repaint that changes a cell but widens nothing: absorption runs,
        # no column grows, so nothing schedules a dimension pass.
        table.show(
            "pods",
            _pods_with_phase([("alpha", "Pending"), ("beta", "Running")]),
            all_namespaces=False,
            pattern="",
        )

        name_column = table.ordered_columns[1]
        wide = "a" * (name_column.content_width + 30)
        key = next(iter(table._emitted))
        table.remove_row(key)
        table.add_row(*([wide] * len(table.ordered_columns)), height=1, key=key)
        await pilot.pause()

        assert name_column.content_width >= len(wide), (
            f"column stayed {name_column.content_width} wide, needed {len(wide)}"
        )
