import asyncio
from collections.abc import AsyncIterator

from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager, WatchSource
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.status_bar import StatusBar

# ---------------------------------------------------------------------------
# Shared resource meta
# ---------------------------------------------------------------------------

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))

_DEFAULT_TEST_ALIASES: dict[str, ResourceMeta] = {
    "pods": _PODS_META,
    "po": _PODS_META,
    "pod": _PODS_META,
    "deployments": _DEPLOY_META,
    "deploy": _DEPLOY_META,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pod(
    name: str, phase: str = "Running", qos: str = "-", namespace: str = "default"
) -> PodSummary:
    return PodSummary(
        name=name,
        namespace=namespace,
        phase=phase,
        ready="1/1",
        restarts=0,
        node=None,
        qos=qos,
    )


def _deploy(name: str, namespace: str = "default") -> GenericSummary:
    return GenericSummary(name=name, namespace=namespace, kind="Deployment", created="")


def fake_source(pods: list[PodSummary]) -> WatchSource:
    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for p in pods:
            yield ("ADDED", p)
        while True:
            await asyncio.sleep(0.01)

    return source


def make_app(
    pods: list[PodSummary],
    namespaces: list[str] | None = None,
    *,
    extra_data: dict[str, list[Summary]] | None = None,
    aliases: dict[str, ResourceMeta] | None = None,
) -> KorvidApp:
    store = ResourceStore()
    all_data: dict[str, list[Summary]] = {"pods": list(pods)}
    if extra_data:
        all_data.update(extra_data)

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in all_data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def list_namespaces() -> list[str]:
        return ["default"] if namespaces is None else namespaces

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=aliases if aliases is not None else dict(_DEFAULT_TEST_ALIASES),
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
        app.store.apply_event("pods", "default", "ADDED", _pod("zzz-new"))
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
        app.store.apply_event("pods", "default", "ADDED", _pod("checkout-3"))
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


async def test_namespace_picker_api_error_shows_actionable_message() -> None:
    """A 403 from list_namespaces surfaces the RBAC guidance, not a raw client dump."""
    from korvid.k8s.errors import ApiStatusError

    store = ResourceStore()

    async def failing_list() -> list[str]:
        raise ApiStatusError(403, "Forbidden")

    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, fake_source([])),
        list_namespaces=failing_list,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        from korvid.ui.messages import ShowNamespacePicker

        app.post_message(ShowNamespacePicker())
        await pilot.pause(0.1)
        notifications = [n.message for n in app._notifications]
        assert any("RBAC" in m for m in notifications)
        assert not any("API 403" in m for m in notifications)


# ---------------------------------------------------------------------------
# Task 4: Universal views — grammar v2, dynamic columns, `0` key
# ---------------------------------------------------------------------------


async def test_deployments_view_renders_generic_columns() -> None:
    """`:deployments` switches kind and renders NAME/AGE columns (not pod columns)."""
    app = make_app(
        [_pod("api-1")],
        extra_data={"deployments": [_deploy("frontend"), _deploy("backend")]},
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("colon")
        for ch in "deployments":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.2)
        table = app.query_one(ResourceTable)
        assert app.current_kind == "deployments"
        assert table.row_count == 2
        # Generic view: 2 columns (NAME, AGE) in single-ns scope
        assert len(table.columns) == 2


async def test_pods_all_adds_namespace_column() -> None:
    """`pods all` switches to ALL_NAMESPACES scope and adds NAMESPACE as first column."""
    pods = [
        _pod("api-1", namespace="default"),
        _pod("svc-1", namespace="kube-system"),
    ]
    app = make_app(pods)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("colon")
        for ch in "pods":
            await pilot.press(ch)
        await pilot.press("space")
        for ch in "all":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.2)
        table = app.query_one(ResourceTable)
        assert app.current_scope == ALL_NAMESPACES
        assert table.row_count == 2
        # Pod view all-ns: 9 columns (NAMESPACE + 8 pod columns)
        assert len(table.columns) == 9


async def test_zero_key_toggles_all_namespaces() -> None:
    """`0` toggles current_scope between default and ALL_NAMESPACES."""
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert app.current_scope == "default"
        await pilot.press("0")
        await pilot.pause(0.15)
        assert app.current_scope == ALL_NAMESPACES
        await pilot.press("0")
        await pilot.pause(0.15)
        assert app.current_scope == "default"


async def test_status_bar_shows_star_in_all_namespaces() -> None:
    """StatusBar must show `ns: *` (or `ns:*`) when scope is ALL_NAMESPACES."""
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("0")
        await pilot.pause(0.15)
        bar = app.query_one(StatusBar)
        text = str(bar.render())
        assert "*" in text


async def test_row_keys_are_namespace_slash_name() -> None:
    """Row keys must be `namespace/name` in ALL cases (for describe/logs/shell)."""
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        # RowKey wraps our string key; .value holds the string we passed
        keys = [str(k.value) for k in table.rows]
        assert "default/api-1" in keys


async def test_filter_works_in_generic_view() -> None:
    """Filter by name still works when displaying generic (non-pod) resources."""
    app = make_app(
        [],
        extra_data={
            "deployments": [_deploy("frontend"), _deploy("backend-api")],
        },
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        # Navigate to deployments
        await pilot.press("colon")
        for ch in "deployments":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.2)
        table = app.query_one(ResourceTable)
        assert table.row_count == 2
        # Apply filter
        await pilot.press("slash")
        for ch in "front":
            await pilot.press(ch)
        await pilot.pause(0.1)
        assert table.row_count == 1
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert table.row_count == 2


async def test_ns_command_keeps_current_kind() -> None:
    """`:ns kube-system` changes namespace but keeps the current kind."""
    app = make_app(
        [_pod("api-1")],
        extra_data={"deployments": [_deploy("frontend")]},
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        # Switch to deployments first
        await pilot.press("colon")
        for ch in "deployments":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.current_kind == "deployments"
        # Now switch namespace only
        await pilot.press("colon")
        for ch in "ns kube-system":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.current_kind == "deployments"
        assert app.current_scope == "kube-system"
