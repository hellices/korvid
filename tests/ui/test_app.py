import asyncio
from collections.abc import AsyncIterator

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager, WatchSource
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.resource_table import ResourceTable


def _pod(name: str, phase: str = "Running") -> PodSummary:
    return PodSummary(
        name=name, namespace="default", phase=phase, ready="1/1", restarts=0, node=None
    )


def fake_source(pods: list[PodSummary]) -> WatchSource:
    async def source(namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        for p in pods:
            yield ("ADDED", p)
        while True:
            await asyncio.sleep(0.01)

    return source


def make_app(pods: list[PodSummary]) -> KorvidApp:
    store = ResourceStore()
    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, fake_source(pods)),
    )


async def test_pods_appear_in_table() -> None:
    app = make_app([_pod("api-1"), _pod("checkout-2", phase="CrashLoopBackOff")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        assert table.row_count == 2
        assert table.get_row_at(0)[0] == "api-1"


async def test_watch_update_refreshes_table() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        app.store.apply_event("pods", "ADDED", _pod("zzz-new"))
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        assert table.row_count == 2


async def test_colon_opens_command_bar_and_ns_switch() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("colon")
        for ch in "ns prod":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app.current_namespace == "prod"


async def test_slash_filter_narrows_rows() -> None:
    app = make_app([_pod("api-1"), _pod("checkout-2")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("slash")
        for ch in "check":
            await pilot.press(ch)
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        assert table.row_count == 1
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert table.row_count == 2


async def test_watch_update_preserves_filter() -> None:
    app = make_app([_pod("api-1"), _pod("checkout-2")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("slash")
        for ch in "check":
            await pilot.press(ch)
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        assert table.row_count == 1  # only checkout-2
        # A new pod arrives while the filter is active.
        app.store.apply_event("pods", "ADDED", _pod("checkout-3"))
        await pilot.pause(0.1)
        # Filter must still apply: checkout-2 + checkout-3 visible; api-1 filtered out.
        assert table.row_count == 2
