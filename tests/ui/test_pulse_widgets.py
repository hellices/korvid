import importlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static

from korvid.core.pulse import PulseCoverage, PulseItem, PulseSnapshot, PulseTarget
from tests.ui.waits import until

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def item(name: str, uid: str | None = "uid-1") -> PulseItem:
    return PulseItem(
        name,
        "current",
        PulseTarget("", "Pod", "default", name, uid),
        "NotReady",
        "Observed Ready=False",
        NOW,
        source="pods",
    )


def snapshot() -> PulseSnapshot:
    return PulseSnapshot(
        0, "default", (item("web-1"),), (), (PulseCoverage("pods", "complete", NOW),)
    )


class PulseApp(App[None]):
    def __init__(self, initial: PulseSnapshot) -> None:
        super().__init__()
        self.latest = initial
        self.refreshes = 0
        self.results: list[Any] = []
        module = importlib.import_module("korvid.ui.widgets.pulse")
        self.detail = module.PulseScreen(
            initial,
            current=lambda: self.latest,
            refresh=self.refresh_inputs,
            target_problem=lambda target: None if target.uid else "Target has no UID",
        )

    def refresh_inputs(self) -> None:
        self.refreshes += 1

    def compose(self) -> ComposeResult:
        module = importlib.import_module("korvid.ui.widgets.pulse")
        yield module.PulseSummary()

    def on_mount(self) -> None:
        self.push_screen(self.detail, self.results.append)


def test_summary_does_not_claim_cluster_health_with_missing_coverage() -> None:
    module = importlib.import_module("korvid.ui.widgets.pulse")
    initial = replace(snapshot(), current=(), coverage=(PulseCoverage("pods", "forbidden", NOW),))
    text = module.summary_text(initial)
    assert "1 gap" in text
    assert "healthy" not in text.lower()
    assert ":pulse" in text


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_detail_fits_and_keeps_evidence_sections_visible(size: tuple[int, int]) -> None:
    app = PulseApp(snapshot())
    async with app.run_test(size=size) as pilot:
        await until(
            pilot, lambda: app.detail.query_one(DataTable).row_count > 0, label="Pulse rows"
        )
        table = app.detail.query_one(DataTable)
        rendered = " ".join(
            str(cell) for row in range(table.row_count) for cell in table.get_row_at(row)
        )
        assert "Current problems" in rendered
        assert "Recent warnings" in rendered
        assert "Coverage" in rendered
        assert table.region.width <= size[0]
        assert table.region.height > 0
        assert app.detail.query_one("#pulse-status", Static).region.bottom <= size[1]
        await pilot.press("escape")
        assert app.screen is not app.detail


async def test_live_updates_never_reorder_or_retarget_selected_rows() -> None:
    app = PulseApp(snapshot())
    async with app.run_test() as pilot:
        await until(
            pilot, lambda: app.detail.query_one(DataTable).row_count > 0, label="Pulse rows"
        )
        table = app.detail.query_one(DataTable)
        table.move_cursor(row=1)
        original = table.get_row_at(table.cursor_row)
        app.latest = replace(app.latest, current=(item("first"), item("web-1")))
        app.detail.note_update(app.latest)
        assert table.cursor_row == 1
        assert table.get_row_at(table.cursor_row) == original
        assert "New data" in str(app.detail.query_one("#pulse-status", Static).render())
        await pilot.press("r")
        assert table.cursor_row == 2
        assert table.get_row_at(table.cursor_row) == original
        assert app.refreshes == 1


async def test_removed_selection_does_not_silently_select_another_target() -> None:
    app = PulseApp(snapshot())
    async with app.run_test() as pilot:
        await until(
            pilot, lambda: app.detail.query_one(DataTable).row_count > 0, label="Pulse rows"
        )
        table = app.detail.query_one(DataTable)
        table.move_cursor(row=1)
        app.latest = replace(app.latest, current=(item("different"),))
        await pilot.press("r", "enter")
        assert table.cursor_row == 0
        assert app.screen is app.detail


async def test_missing_uid_stays_visible_but_cannot_navigate() -> None:
    app = PulseApp(replace(snapshot(), current=(item("web-1", None),)))
    async with app.run_test() as pilot:
        await until(
            pilot, lambda: app.detail.query_one(DataTable).row_count > 0, label="Pulse rows"
        )
        app.detail.query_one(DataTable).move_cursor(row=1)
        await pilot.press("enter")
        assert app.screen is app.detail
        assert "UID" in str(app.detail.query_one("#pulse-status", Static).render())


async def test_markup_is_literal_and_navigation_returns_typed_identity() -> None:
    initial = replace(snapshot(), current=(replace(item("web-1"), message="[bold]literal[/bold]"),))
    app = PulseApp(initial)
    async with app.run_test() as pilot:
        await until(
            pilot, lambda: app.detail.query_one(DataTable).row_count > 0, label="Pulse rows"
        )
        app.detail.query_one(DataTable).move_cursor(row=1)
        await until(
            pilot,
            lambda: "literal" in str(app.detail.query_one("#pulse-evidence", Static).render()),
            label="Pulse evidence",
        )
        assert "[bold]literal[/bold]" in str(
            app.detail.query_one("#pulse-evidence", Static).render()
        )
        await pilot.press("enter")
        assert app.results[0].target == initial.current[0].target
        assert app.results[0].epoch == 0
        assert app.results[0].scope == "default"
