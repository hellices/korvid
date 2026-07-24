import asyncio
from collections.abc import AsyncIterator

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager, WatchSource
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.status_bar import StatusBar


def _pod(name: str, phase: str = "Running", qos: str = "-") -> PodSummary:
    return PodSummary(
        name=name, namespace="default", phase=phase, ready="1/1", restarts=0, node=None, qos=qos
    )


def fake_source(pods: list[PodSummary]) -> WatchSource:
    async def source(namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        for p in pods:
            yield ("ADDED", p)
        while True:
            await asyncio.sleep(0.01)

    return source


def make_app(pods: list[PodSummary], namespaces: list[str] | None = None) -> KorvidApp:
    store = ResourceStore()

    async def list_namespaces() -> list[str]:
        return ["default"] if namespaces is None else namespaces

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, fake_source(pods)),
        list_namespaces=list_namespaces,
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


async def test_empty_namespace_shows_guidance() -> None:
    app = make_app([])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        empty = app.query_one("#empty-state")
        assert empty.display is True
        text = str(empty.render())
        assert "default" in text  # names the namespace so users know where they are
        assert ":ns" in text  # tells users how to switch


async def test_empty_state_hidden_when_pods_exist() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert app.query_one("#empty-state").display is False


async def test_empty_state_appears_when_filter_matches_nothing() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("slash")
        for ch in "zzz":
            await pilot.press(ch)
        await pilot.pause(0.1)
        empty = app.query_one("#empty-state")
        assert empty.display is True
        assert "zzz" in str(empty.render())
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert empty.display is False


async def test_status_bar_shows_ns_and_agent_state() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        bar = app.query_one(StatusBar)
        text = str(bar.render())
        assert "default" in text
        assert "AI off" in text


async def test_filter_enter_closes_bar_keeps_filter() -> None:
    app = make_app([_pod("api-1"), _pod("checkout-2")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("slash")
        for ch in "check":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.1)
        from korvid.ui.widgets.filter_bar import FilterBar

        assert app.query_one(FilterBar).display is False
        table = app.query_one(ResourceTable)
        assert table.row_count == 1  # filter still active after Enter
        # focus must be back on the table so app bindings (q, :, /) work again
        assert app.focused is app.query_one(ResourceTable)


async def test_bars_show_mode_placeholder() -> None:
    from korvid.ui.widgets.command_bar import CommandBar
    from korvid.ui.widgets.filter_bar import FilterBar

    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert app.query_one(CommandBar).placeholder != ""
        assert app.query_one(FilterBar).placeholder != ""
        assert app.query_one(CommandBar).placeholder != app.query_one(FilterBar).placeholder


async def test_bare_ns_opens_picker_and_selection_switches() -> None:
    from korvid.ui.widgets.namespace_picker import NamespacePicker

    app = make_app([_pod("api-1")], namespaces=["default", "kube-system", "prod"])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("colon")
        for ch in "ns":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.2)
        picker = app.query_one(NamespacePicker)
        assert picker.display is True
        assert picker.option_count == 3
        # navigate down to kube-system and select it
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert picker.display is False
        assert app.current_namespace == "kube-system"


async def test_picker_escape_dismisses_without_switch() -> None:
    from korvid.ui.widgets.namespace_picker import NamespacePicker

    app = make_app([_pod("api-1")], namespaces=["default", "prod"])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("colon")
        for ch in "ns":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert app.query_one(NamespacePicker).display is False
        assert app.current_namespace == "default"


async def test_rows_sorted_by_eviction_order_reversed() -> None:
    # Last-to-be-evicted first: Guaranteed > Burstable > BestEffort; name tiebreak.
    app = make_app(
        [
            _pod("b-besteffort", qos="BestEffort"),
            _pod("z-guaranteed", qos="Guaranteed"),
            _pod("m-burstable", qos="Burstable"),
            _pod("a-guaranteed", qos="Guaranteed"),
        ]
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        names = [table.get_row_at(i)[0] for i in range(table.row_count)]
        assert names == ["a-guaranteed", "z-guaranteed", "m-burstable", "b-besteffort"]


async def test_qos_cells_are_color_coded() -> None:
    from rich.text import Text

    app = make_app(
        [
            _pod("g", qos="Guaranteed"),
            _pod("u", qos="Burstable"),
            _pod("e", qos="BestEffort"),
        ]
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        styles = {}
        for i in range(table.row_count):
            row = table.get_row_at(i)
            qos_cell = row[6]
            assert isinstance(qos_cell, Text)
            styles[str(qos_cell)] = qos_cell.style
        assert styles["Guaranteed"] == "green"
        assert styles["Burstable"] == "chartreuse2"
        assert styles["BestEffort"] == "yellow"


async def test_bare_ns_with_empty_list_warns_instead_of_empty_picker() -> None:
    from korvid.ui.widgets.namespace_picker import NamespacePicker

    app = make_app([_pod("api-1")], namespaces=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("colon")
        for ch in "ns":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.query_one(NamespacePicker).display is False
