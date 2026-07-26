"""Filter enhancement tests (issue #44): label selector, regex/fuzzy,
inverse, hide-completed, and the active-filter indicator."""

from __future__ import annotations

from dataclasses import replace

from korvid.ui.widgets.filter_bar import FilterBar
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.status_bar import StatusBar

from .test_app import _pod, make_app
from .waits import until


async def _apply_filter(pilot, app, text: str) -> None:  # type: ignore[no-untyped-def]  # test helper
    await pilot.press("slash")
    bar = app.query_one(FilterBar)
    bar.value = text  # Input.Changed fires → FilterCommand, same as typing
    await pilot.pause()


async def test_label_selector_filters_rows() -> None:
    pods = [
        replace(_pod("web-1"), labels=(("app", "web"),)),
        replace(_pod("db-1"), labels=(("app", "db"),)),
    ]
    app = make_app(pods)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        await _apply_filter(pilot, app, "-l app=web")
        await until(pilot, lambda: table.row_count == 1, label="label filtered")
        assert table.get_row_at(0)[0] == "web-1"


async def test_hide_completed_token_hides_succeeded_pods() -> None:
    app = make_app([_pod("web-1"), _pod("job-x", phase="Completed")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        await _apply_filter(pilot, app, "-s")
        await until(pilot, lambda: table.row_count == 1, label="completed hidden")
        assert table.get_row_at(0)[0] == "web-1"


async def test_regex_filter_narrows_rows() -> None:
    app = make_app([_pod("web-12"), _pod("web-abc"), _pod("db-1")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        await _apply_filter(pilot, app, "/^web-[0-9]+$/")
        await until(pilot, lambda: table.row_count == 1, label="regex filtered")
        assert table.get_row_at(0)[0] == "web-12"


async def test_invalid_regex_shows_error_and_does_not_crash() -> None:
    app = make_app([_pod("web-1"), _pod("db-1")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        await _apply_filter(pilot, app, "/[unclosed/")
        await pilot.pause()
        # Broken token is ignored: all rows stay visible, error surfaced inline.
        assert table.row_count == 2
        status = str(app.query_one(StatusBar).render())
        assert "invalid regex" in status


async def test_inverse_filter_excludes_matches() -> None:
    app = make_app([_pod("web-stable"), _pod("web-canary")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        await _apply_filter(pilot, app, "!canary")
        await until(pilot, lambda: table.row_count == 1, label="inverse filtered")
        assert table.get_row_at(0)[0] == "web-stable"


async def test_fuzzy_filter_matches_subsequence() -> None:
    app = make_app([_pod("web-backend-a"), _pod("database")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        await _apply_filter(pilot, app, "~wba")
        await until(pilot, lambda: table.row_count == 1, label="fuzzy filtered")
        assert table.get_row_at(0)[0] == "web-backend-a"


async def test_status_bar_shows_active_filter_and_clears_on_escape() -> None:
    app = make_app([_pod("web-1"), _pod("db-1")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        await _apply_filter(pilot, app, "web -s")
        await until(
            pilot,
            lambda: "web" in str(app.query_one(StatusBar).render()),
            label="filter indicator",
        )
        status = str(app.query_one(StatusBar).render())
        assert "hide-completed" in status
        await pilot.press("escape")
        await until(pilot, lambda: table.row_count == 2, label="filter cleared")
        assert "hide-completed" not in str(app.query_one(StatusBar).render())
