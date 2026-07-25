"""Hierarchy drill-down: deploy -> replicasets (history) -> pods (issue #14)."""

import asyncio
from collections.abc import AsyncIterator

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary, PodSummary, ReplicaSetSummary
from korvid.ui.app import KorvidApp
from korvid.ui.messages import NavigateCommand
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.status_bar import StatusBar

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_RS_META = ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True, ("rs",))

_ALIASES: dict[str, ResourceMeta] = {
    "pods": _PODS_META,
    "po": _PODS_META,
    "deployments": _DEPLOY_META,
    "deploy": _DEPLOY_META,
    "replicasets": _RS_META,
    "rs": _RS_META,
}


def _deploy(name: str, uid: str) -> GenericSummary:
    return GenericSummary(name=name, namespace="default", kind="Deployment", created="", uid=uid)


def _rs(name: str, uid: str, owner: str, revision: str = "1") -> ReplicaSetSummary:
    return ReplicaSetSummary(
        name=name,
        namespace="default",
        kind="ReplicaSet",
        created="",
        uid=uid,
        owner_uids=(owner,),
        revision=revision,
        desired=2,
        current=2,
        ready="2/2",
    )


def _pod(name: str, owner: str) -> PodSummary:
    return PodSummary(
        name=name,
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        owner_uids=(owner,),
    )


def make_app(data: dict[str, list[Summary]]) -> KorvidApp:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def list_namespaces() -> list[str]:
        return ["default"]

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=dict(_ALIASES),
    )


def _default_data() -> dict[str, list[Summary]]:
    return {
        "pods": [_pod("web-6d9f88-aaa", "rs-1"), _pod("api-777-bbb", "rs-9")],
        "deployments": [_deploy("web", "dep-1"), _deploy("api", "dep-9")],
        "replicasets": [
            _rs("web-6d9f88", "rs-1", "dep-1", revision="2"),
            _rs("web-5c4e77", "rs-2", "dep-1", revision="1"),
            _rs("api-777", "rs-9", "dep-9"),
        ],
    }


async def _navigate(pilot, command: str) -> None:  # type: ignore[no-untyped-def]  # Pilot is generic over the app's result type; the fixture's concrete type isn't exposed
    await pilot.press("colon")
    for ch in command:
        await pilot.press(ch if ch != " " else "space")
    await pilot.press("enter")
    await pilot.pause(0.1)


async def test_replicasets_view_has_history_columns() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "replicasets")
        table = app.query_one(ResourceTable)
        labels = [str(col.label) for col in table.columns.values()]
        assert labels == ["NAME", "REVISION", "DESIRED", "CURRENT", "READY", "AGE"]
        row = table.get_row_at(0)  # newest revision first (rollout-history order)
        assert str(row[0]) == "web-6d9f88"
        assert str(row[1]) == "2"


async def test_replicasets_sorted_by_revision_descending() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "replicasets")
        table = app.query_one(ResourceTable)
        names = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        # rev 2 first; rev-1 ties break by name (api-777 before web-5c4e77).
        assert names == ["web-6d9f88", "api-777", "web-5c4e77"]


async def test_enter_on_deployment_drills_into_owned_replicasets() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "deployments")
        table = app.query_one(ResourceTable)
        assert table.row_count == 2  # api, web
        await pilot.press("down")  # cursor: api -> web
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.current_kind == "replicasets"
        # Only web's replicasets: revision history for dep-1.
        names = {str(table.get_row_at(i)[0]) for i in range(table.row_count)}
        assert names == {"web-6d9f88", "web-5c4e77"}


async def test_drill_to_pods_and_breadcrumb() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "deployments")
        await pilot.press("down")
        await pilot.press("enter")  # web -> replicasets
        await pilot.pause(0.2)
        await pilot.press("enter")  # first rs (web-5c4e77? sorted) -> pods
        await pilot.pause(0.2)
        assert app.current_kind == "pods"
        status = str(app.query_one(StatusBar).content)
        assert "deployments/web" in status
        assert "replicasets/" in status


async def test_drilled_pods_filtered_by_owner() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "deployments")
        table = app.query_one(ResourceTable)
        await pilot.press("down")
        await pilot.press("enter")  # web -> rs history
        await pilot.pause(0.2)
        # move to web-6d9f88 (rs-1) which owns the pod
        names = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        idx = names.index("web-6d9f88")
        for _ in range(idx):
            await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.current_kind == "pods"
        assert table.row_count == 1
        assert str(table.get_row_at(0)[0]) == "web-6d9f88-aaa"


async def test_escape_pops_one_drill_level() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "deployments")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.current_kind == "replicasets"
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app.current_kind == "deployments"
        table = app.query_one(ResourceTable)
        assert table.row_count == 2  # unfiltered deployments view again


async def test_command_navigation_clears_drill_state() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "deployments")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.current_kind == "replicasets"
        await _navigate(pilot, "pods")
        # Explicit :pods shows ALL pods, not the drilled subset.
        table = app.query_one(ResourceTable)
        assert table.row_count == 2
        status = str(app.query_one(StatusBar).content)
        assert "deployments/" not in status


async def test_agent_drill_down_from_deployments_view() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "deployments")
        out = await app.agent_drill_down("web")
        await pilot.pause(0.2)
        assert "replicasets" in out
        assert app.current_kind == "replicasets"
        table = app.query_one(ResourceTable)
        assert table.row_count == 2  # web's revision history only


async def test_agent_drill_down_unknown_name_is_error() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "deployments")
        out = await app.agent_drill_down("nope")
        assert out.startswith("ERROR:")
        assert app.current_kind == "deployments"


async def test_agent_drill_down_without_child_kind_is_error() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)  # pods view: containers need a picker, not a kind
        out = await app.agent_drill_down("web-6d9f88-aaa")
        assert out.startswith("ERROR:")


async def test_agent_drill_down_rejected_while_describe_screen_open() -> None:
    """Same user-priority guard as agent_navigate: never change the table
    hidden under a describe modal the user is reading."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "deployments")
        await app.push_screen(DescribeScreen("deployments/default/web", {"kind": "Deployment"}, []))
        await pilot.pause()
        out = await app.agent_drill_down("web")
        assert out.startswith("ERROR:")
        assert app.current_kind == "deployments"
        assert isinstance(app.screen, DescribeScreen)


async def test_concurrent_drill_and_navigate_stay_consistent() -> None:
    """An agent drill and a user :view navigation racing must never strand a
    child view without its drill level (or vice versa): stack mutation and the
    kind transition are one transaction under the nav lock."""
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "deployments")
        gate = asyncio.Event()
        orig_stop = app.watch_manager.stop

        async def slow_stop(kind: str, scope: str) -> None:
            await gate.wait()
            await orig_stop(kind, scope)

        app.watch_manager.stop = slow_stop  # type: ignore[method-assign]  # test seam to widen the race window
        drill = asyncio.create_task(app.agent_drill_down("web"))
        await asyncio.sleep(0.02)  # drill enters the lock and blocks in stop()
        nav = asyncio.create_task(app.on_navigate_command(NavigateCommand("pods", None)))
        await asyncio.sleep(0.02)
        gate.set()
        await drill
        await nav
        await pilot.pause(0.2)
        # The user navigation queued behind the drill and lands last: the
        # drill stack was cleared inside the same critical section, so the
        # final pods view is unfiltered with no breadcrumb.
        assert app.current_kind == "pods"
        table = app.query_one(ResourceTable)
        assert table.row_count == 2
        status = str(app.query_one(StatusBar).content)
        assert "deployments/" not in status
