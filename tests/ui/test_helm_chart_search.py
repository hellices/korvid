"""HelmChartSearchScreen: search-first chart discovery (issue #106).

Charts are fetched per keyword instead of all upfront; a LoadingIndicator
shows while `helm search repo` runs so the UI never looks frozen. The
injected search callable keeps the screen testable without a helm binary.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Input, LoadingIndicator, OptionList, Static

from korvid.k8s.helmcli import ChartHit, HelmError
from korvid.ui.widgets.helm_chart_search import HelmChartSearchScreen

from .waits import until

_NGINX = ChartHit("bitnami/nginx", "18.1.0", "1.27.0", "NGINX Open Source")
_PG = ChartHit("bitnami/postgresql", "15.5.0", "16.3.0", "PostgreSQL")


class HostApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: object = "unset"
        self.repos_opened = 0

    def compose(self) -> ComposeResult:
        yield Static("host")


class FakeSearch:
    """Recording async search callable with scriptable results/errors."""

    def __init__(self, hits: list[ChartHit] | None = None) -> None:
        self.hits = hits if hits is not None else [_NGINX, _PG]
        self.error: str | None = None
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.cancelled = asyncio.Event()

    async def __call__(self, keyword: str) -> list[ChartHit]:
        self.calls.append(keyword)
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            # Set only after the cancellation reached the awaiting search:
            # the widget's cleanup runs during this same unwind, so awaiting
            # this event observes the cleanup deterministically.
            self.cancelled.set()
            raise
        if self.error is not None:
            raise HelmError(self.error)
        return self.hits


async def _open(
    app: HostApp,
    search: FakeSearch,
    *,
    initial: str = "",
    with_repos: bool = False,
) -> HelmChartSearchScreen:
    def _repos() -> None:
        app.repos_opened += 1

    screen = HelmChartSearchScreen(
        search,
        title="Install chart",
        initial=initial,
        on_manage_repos=_repos if with_repos else None,
    )

    def _done(v: object) -> None:
        app.result = v

    await app.push_screen(screen, _done)
    return screen


async def _composed(app: HostApp, pilot: object) -> None:
    await until(pilot, lambda: bool(app.screen.query(Input)), label="search screen composed")


def _options(app: HostApp) -> int:
    return app.screen.query_one(OptionList).option_count


async def test_search_lists_hits_and_selecting_dismisses_with_hit() -> None:
    app = HostApp()
    search = FakeSearch()
    async with app.run_test() as pilot:
        await _open(app, search)
        await _composed(app, pilot)
        for ch in "nginx":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(pilot, lambda: _options(app) == 2, label="results listed")
        assert search.calls == ["nginx"]
        await pilot.press("enter")  # results list is focused after a search
        await until(pilot, lambda: app.result == _NGINX, label="hit returned")


async def test_initial_keyword_searches_on_mount() -> None:
    """The upgrade flow prefills the release's chart name: the first search
    runs automatically, no extra keystroke needed."""
    app = HostApp()
    search = FakeSearch()
    async with app.run_test() as pilot:
        await _open(app, search, initial="nginx")
        await until(pilot, lambda: search.calls == ["nginx"], label="auto search ran")
        await until(pilot, lambda: _options(app) == 2, label="results listed")


async def test_no_upfront_fetch_without_initial_keyword() -> None:
    """The whole point of issue #106: opening the screen must not fetch
    every chart from every repo."""
    app = HostApp()
    search = FakeSearch()
    async with app.run_test() as pilot:
        await _open(app, search)
        await _composed(app, pilot)
        await pilot.pause()
        assert search.calls == []


async def test_loading_indicator_shows_while_search_runs() -> None:
    app = HostApp()
    search = FakeSearch()
    search.gate = asyncio.Event()
    async with app.run_test() as pilot:
        await _open(app, search)
        await _composed(app, pilot)
        await pilot.press("enter")  # empty keyword: search everything, explicitly
        await until(
            pilot,
            lambda: app.screen.query_one(LoadingIndicator).display,
            label="loading indicator visible",
        )
        search.gate.set()
        await until(
            pilot,
            lambda: not app.screen.query_one(LoadingIndicator).display,
            label="loading indicator hidden",
        )
        assert _options(app) == 2


async def test_search_failure_reports_and_keeps_screen_open() -> None:
    app = HostApp()
    search = FakeSearch()
    search.error = "no repositories configured"
    async with app.run_test() as pilot:
        await _open(app, search)
        await _composed(app, pilot)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (
                "no repositories configured"
                in str(app.screen.query_one("#chart-status", Static).render())
            ),
            label="error shown in status line",
        )
        assert isinstance(app.screen, HelmChartSearchScreen)


async def test_empty_results_hint_at_repos() -> None:
    app = HostApp()
    search = FakeSearch(hits=[])
    async with app.run_test() as pilot:
        await _open(app, search, with_repos=True)
        await _composed(app, pilot)
        for ch in "nope":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (
                "no charts matched" in str(app.screen.query_one("#chart-status", Static).render())
            ),
            label="empty-result hint",
        )


async def test_escape_dismisses_with_none() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app, FakeSearch())
        await _composed(app, pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.result is None, label="dismissed with None")


async def test_ctrl_r_opens_repo_management() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app, FakeSearch(), with_repos=True)
        await _composed(app, pilot)
        await pilot.press("ctrl+r")
        await until(pilot, lambda: app.repos_opened == 1, label="repo callback invoked")


async def test_resubmit_while_pending_keeps_the_spinner_owned_by_the_new_search() -> None:
    """A second Enter cancels the previous exclusive search worker; its
    cleanup must not hide the loading indicator the replacement owns."""
    app = HostApp()
    search = FakeSearch()
    search.gate = asyncio.Event()
    async with app.run_test() as pilot:
        await _open(app, search)
        await _composed(app, pilot)
        await pilot.press("enter")  # search 1: pending on the gate
        await until(
            pilot,
            lambda: app.screen.query_one(LoadingIndicator).display,
            label="spinner visible",
        )
        await pilot.press("enter")  # search 2 replaces search 1
        await until(pilot, lambda: search.cancelled.is_set(), label="search 1 cancelled")
        # the cancelled worker's cleanup ran during that unwind: the spinner
        # owned by search 2 must survive it
        assert app.screen.query_one(LoadingIndicator).display
        search.gate.set()
        await until(
            pilot,
            lambda: not app.screen.query_one(LoadingIndicator).display,
            label="spinner hidden after the live search",
        )
        assert _options(app) == 2
