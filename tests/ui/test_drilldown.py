"""Hierarchy drill-down: deploy -> replicasets (history) -> pods (issue #14)."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary, PodSummary, ReplicaSetSummary
from korvid.k8s.relations import owned_by
from korvid.ui.app import KorvidApp
from korvid.ui.messages import FilterCommand, NavigateCommand
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.status_bar import StatusBar

from .waits import until

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


def _row_names(table: ResourceTable) -> list[str]:
    return [str(table.get_row_at(i)[0]) for i in range(table.row_count)]


def _status_text(app: KorvidApp) -> str:
    return str(app.query_one(StatusBar).content)


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


async def test_replicasets_view_has_history_columns() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "replicasets")
        await until(
            pilot,
            lambda: app.current_kind == "replicasets" and table.row_count == 3,
            label="replicasets rendered",
        )
        labels = [str(col.label) for col in table.columns.values()]
        assert labels == ["NAME", "REVISION", "DESIRED", "CURRENT", "READY", "AGE"]
        row = table.get_row_at(0)  # newest revision first (rollout-history order)
        assert str(row[0]) == "web-6d9f88"
        assert str(row[1]) == "2"


async def test_replicasets_sorted_by_revision_descending() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "replicasets")
        await until(
            pilot,
            lambda: app.current_kind == "replicasets" and table.row_count == 3,
            label="replicasets rendered",
        )
        names = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        # rev 2 first; rev-1 ties break by name (api-777 before web-5c4e77).
        assert names == ["web-6d9f88", "api-777", "web-5c4e77"]


async def test_enter_on_deployment_drills_into_owned_replicasets() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and table.row_count == 2,
            label="deployments rendered",
        )
        assert table.row_count == 2  # api, web
        await pilot.press("down")  # cursor: api -> web
        await pilot.press("enter")
        await until(
            pilot,
            lambda: app.current_kind == "replicasets" and table.row_count == 2,
            label="web replicasets rendered",
        )
        assert app.current_kind == "replicasets"
        # Only web's replicasets: revision history for dep-1.
        names = {str(table.get_row_at(i)[0]) for i in range(table.row_count)}
        assert names == {"web-6d9f88", "web-5c4e77"}


async def test_drill_to_pods_and_breadcrumb() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: (
                app.current_kind == "deployments"
                and _row_names(table) == ["api", "web"]
                and app._cursor_row_key() == "default/api"
            ),
            label="deployments rendered",
        )
        await pilot.press("down")
        await pilot.press("enter")  # web -> replicasets
        await until(
            pilot,
            lambda: (
                app.current_kind == "replicasets"
                and _row_names(table) == ["web-6d9f88", "web-5c4e77"]
                and app._cursor_row_key() == "default/web-6d9f88"
            ),
            label="replicasets drilled",
        )
        await pilot.press("enter")  # first rs (web-6d9f88) -> pods
        await until(
            pilot,
            lambda: (
                app.current_kind == "pods"
                and _row_names(table) == ["web-6d9f88-aaa"]
                and app._cursor_row_key() == "default/web-6d9f88-aaa"
                and "deployments/web" in _status_text(app)
                and "replicasets/web-6d9f88" in _status_text(app)
            ),
            label="pod breadcrumb rendered",
        )
        assert app.current_kind == "pods"
        status = _status_text(app)
        assert "deployments/web" in status
        assert "replicasets/" in status


async def test_drilled_pods_filtered_by_owner() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and table.row_count == 2,
            label="deployments rendered",
        )
        await pilot.press("down")
        await pilot.press("enter")  # web -> rs history
        await until(
            pilot,
            lambda: app.current_kind == "replicasets" and table.row_count == 2,
            label="replicasets drilled",
        )
        # move to web-6d9f88 (rs-1) which owns the pod
        names = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        idx = names.index("web-6d9f88")
        for _ in range(idx):
            await pilot.press("down")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: app.current_kind == "pods" and table.row_count == 1,
            label="owned pod rendered",
        )
        assert app.current_kind == "pods"
        assert table.row_count == 1
        assert str(table.get_row_at(0)[0]) == "web-6d9f88-aaa"


async def test_escape_pops_one_drill_level() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and table.row_count == 2,
            label="deployments rendered",
        )
        await pilot.press("down")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (
                app.current_kind == "replicasets"
                and _row_names(table) == ["web-6d9f88", "web-5c4e77"]
            ),
            label="replicasets drilled",
        )
        assert app.current_kind == "replicasets"
        await pilot.press("escape")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments restored",
        )
        assert app.current_kind == "deployments"
        assert table.row_count == 2  # unfiltered deployments view again


async def test_escape_that_closes_a_modal_does_not_pop_a_drill_level() -> None:
    """Escape belongs to the modal on top: closing Help over a drilled
    view must not also silently pop the drill level underneath."""
    from .waits import until

    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        await pilot.press("down")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (
                app.current_kind == "replicasets"
                and _row_names(table) == ["web-6d9f88", "web-5c4e77"]
            ),
            label="replicaset drill active",
        )
        await pilot.press("question_mark")  # help modal over the drill
        await until(pilot, lambda: len(app.screen_stack) > 1, label="help open")
        await pilot.press("escape")  # closes help - the drill must survive
        await until(pilot, lambda: len(app.screen_stack) == 1, label="help closed")
        assert app.current_kind == "replicasets"
        await pilot.press("escape")  # now Escape pops the drill as usual
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployment drill restored",
        )


async def test_command_navigation_clears_drill_state() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and table.row_count == 2,
            label="deployments rendered",
        )
        await pilot.press("down")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (
                app.current_kind == "replicasets"
                and _row_names(table) == ["web-6d9f88", "web-5c4e77"]
            ),
            label="replicaset drill active",
        )
        assert app.current_kind == "replicasets"
        await _navigate(pilot, "pods")
        await until(
            pilot,
            lambda: app.current_kind == "pods" and table.row_count == 2,
            label="command navigation cleared drill",
        )
        # Explicit :pods shows ALL pods, not the drilled subset.
        assert table.row_count == 2
        status = str(app.query_one(StatusBar).content)
        assert "deployments/" not in status


async def test_agent_drill_down_from_deployments_view() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and table.row_count == 2,
            label="deployments rendered",
        )
        out = await app.agent_drill_down("web")
        await until(
            pilot,
            lambda: app.current_kind == "replicasets" and table.row_count == 2,
            label="agent drill rendered",
        )
        assert "replicasets" in out
        assert app.current_kind == "replicasets"
        assert table.row_count == 2  # web's revision history only


async def test_agent_drill_down_unknown_name_is_error() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        out = await app.agent_drill_down("nope")
        assert out.startswith("ERROR:")
        assert app.current_kind == "deployments"


async def test_agent_drill_down_without_child_kind_is_error() -> None:
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: app.query_one(ResourceTable).row_count == 2,
            label="pod rows visible",
        )  # pods view: containers need a picker, not a kind
        out = await app.agent_drill_down("web-6d9f88-aaa")
        assert out.startswith("ERROR:")


async def test_agent_drill_down_respects_visible_filter() -> None:
    """drill_down acts on the visible table: a name hidden by the active
    filter pattern must not be drillable."""
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and table.row_count == 2,
            label="deployments rendered",
        )
        app.on_filter_command(FilterCommand("api"))  # the real filter path (#44)
        await until(pilot, lambda: table.row_count == 1, label="deployment filter applied")
        out = await app.agent_drill_down("web")
        assert out.startswith("ERROR:")
        assert app.current_kind == "deployments"


async def test_enter_without_drill_chain_leaves_event_unconsumed() -> None:
    """Kinds with no drill chain must not consume Enter, so future handlers
    (e.g. a default describe) can claim it."""
    from types import SimpleNamespace

    services_meta = ResourceMeta("Service", "services", "", "v1", True, ("svc",))
    data = _default_data()
    data["services"] = [
        GenericSummary(name="web-svc", namespace="default", kind="Service", created="")
    ]
    app = make_app(data)
    app.aliases["services"] = services_meta
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "services")
        await until(
            pilot,
            lambda: app.current_kind == "services" and table.row_count == 1,
            label="services rendered",
        )
        stopped = False

        def _stop() -> None:
            nonlocal stopped
            stopped = True

        event = SimpleNamespace(
            data_table=table, row_key=SimpleNamespace(value="default/web-svc"), stop=_stop
        )
        await app.on_data_table_row_selected(event)  # type: ignore[arg-type]  # duck-typed stand-in for DataTable.RowSelected
        assert not stopped
        assert app.current_kind == "services"


async def test_replicaset_view_renders_generic_fallback_rows() -> None:
    """A replicaset row that arrives as a plain GenericSummary still shows
    NAME/AGE instead of silently disappearing."""
    data = _default_data()
    data["replicasets"].append(
        GenericSummary(name="odd-rs", namespace="default", kind="ReplicaSet", created="")
    )
    app = make_app(data)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "replicasets")
        await until(
            pilot,
            lambda: app.current_kind == "replicasets" and table.row_count == 4,
            label="replicasets rendered",
        )
        names = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        assert "odd-rs" in names
        idx = names.index("odd-rs")
        assert str(table.get_row_at(idx)[1]) == ""  # no revision info


async def test_agent_drill_down_rejected_while_describe_screen_open() -> None:
    """Same user-priority guard as agent_navigate: never change the table
    hidden under a describe modal the user is reading."""
    from korvid.ui.widgets.describe_screen import DescribeScreen

    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        await app.push_screen(DescribeScreen("deployments/default/web", {"kind": "Deployment"}, []))
        await until(
            pilot,
            lambda: app.screen.__class__ is DescribeScreen,
            label="describe screen opened",
        )
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
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        gate = asyncio.Event()
        entered = asyncio.Event()
        orig_stop = app.watch_manager.stop

        async def slow_stop(kind: str, scope: str) -> None:
            entered.set()
            await gate.wait()
            await orig_stop(kind, scope)

        app.watch_manager.stop = slow_stop  # type: ignore[method-assign]  # test seam to widen the race window
        drill = asyncio.create_task(app.agent_drill_down("web"))
        # The drill pre-warms before taking the lock (issue #157): wait until
        # it is really inside the critical section, blocked in stop().
        await asyncio.wait_for(entered.wait(), timeout=5)
        nav = asyncio.create_task(app.on_navigate_command(NavigateCommand("pods", None)))
        await asyncio.sleep(0.02)
        gate.set()
        await drill
        await nav
        await until(
            pilot,
            lambda: app.current_kind == "pods" and table.row_count == 2,
            label="queued navigation landed",
        )
        # The user navigation queued behind the drill and lands last: the
        # drill stack was cleared inside the same critical section, so the
        # final pods view is unfiltered with no breadcrumb.
        assert app.current_kind == "pods"
        table = app.query_one(ResourceTable)
        assert table.row_count == 2
        status = str(app.query_one(StatusBar).content)
        assert "deployments/" not in status


# ---------------------------------------------------------------------------
# drill pre-warm (issue #157): no empty-view flash between push/pop and rows
# ---------------------------------------------------------------------------


def _make_slow_app(
    data: dict[str, list[Summary]],
    *,
    delay_kinds: dict[str, float],
    starts: list[str] | None = None,
) -> KorvidApp:
    """App whose watch source stalls before LISTing `delay_kinds[kind]`
    seconds - a stand-in for the network RTT that produced the empty flash."""
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if starts is not None:
            starts.append(kind)
        delay = delay_kinds.get(kind, 0.0)
        if delay:
            await asyncio.sleep(delay)
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


def _spy_renders(app: KorvidApp, renders: list[tuple[str, int]]) -> None:
    original = type(app)._render_pane

    def spy(self, kind, pane, table, *, empty_state):  # type: ignore[no-untyped-def]  # test seam
        rows = self.store.get(kind, pane.scope)
        drill_uid = pane.drill.parent_uid
        if drill_uid is not None and kind == pane.drill.child_kind:
            rows = [r for r in rows if owned_by(r, drill_uid)]
        renders.append((kind, len(rows)))
        original(self, kind, pane, table, empty_state=empty_state)

    app._render_pane = spy.__get__(app)  # type: ignore[method-assign]  # test seam


async def test_drill_push_never_renders_an_empty_child_view() -> None:
    """The old flow switched the pane first and LISTed after: one visibly
    empty replicasets render, then the fill. The pre-warm starts the child
    watch before the switch, so the first child render already has rows."""
    renders: list[tuple[str, int]] = []
    app = _make_slow_app(_default_data(), delay_kinds={"replicasets": 0.15})
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: app.query_one(ResourceTable).row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        _spy_renders(app, renders)
        await pilot.press("down")  # api -> web
        await pilot.press("enter")
        await until(
            pilot, lambda: app.current_kind == "replicasets", label="replicaset drill active"
        )
        await until(pilot, lambda: table.row_count == 2, label="owned rows visible")
        assert ("replicasets", 0) not in renders


async def test_drill_pop_never_renders_an_empty_parent_view() -> None:
    """Esc re-LISTs the parent kind (its watch stopped when we drilled
    away): the pre-warm must cover the pop direction too."""
    delays = {"deployments": 0.0}
    renders: list[tuple[str, int]] = []
    app = _make_slow_app(_default_data(), delay_kinds=delays)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        await pilot.press("down")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (
                app.current_kind == "replicasets"
                and _row_names(table) == ["web-6d9f88", "web-5c4e77"]
            ),
            label="replicaset drill active",
        )
        delays["deployments"] = 0.15  # the re-LIST on the way back is slow
        _spy_renders(app, renders)
        await pilot.press("escape")
        await until(
            pilot, lambda: app.current_kind == "deployments", label="deployment drill restored"
        )
        await until(pilot, lambda: table.row_count == 2, label="parents visible")
        assert ("deployments", 0) not in renders


async def test_drill_prewarm_shows_a_progress_label_while_waiting() -> None:
    """The bounded wait must read as *working*, not frozen: the status bar
    carries a loading label (which the corvid busy indicator animates)."""
    app = _make_slow_app(_default_data(), delay_kinds={"replicasets": 0.3})
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        # Drive the drill as a task: pilot.press would await the whole
        # transition, leaving no window to observe the in-flight label.
        drill = asyncio.create_task(app._drill_into("default", "web"))
        await until(
            pilot,
            lambda: any("replicasets" in v for v in app._progress_labels.values()),
            label="loading label published",
        )
        assert app.current_kind == "deployments"  # still on the parent view
        assert (await drill) is None
        assert app.current_kind == "replicasets"
        assert not app._progress_labels  # cleared once the switch landed


async def test_drill_prewarm_times_out_and_still_switches() -> None:
    """A cluster that never answers must not wedge the drill: after the
    bounded wait the transition proceeds exactly as before the pre-warm."""
    data = _default_data()
    data["replicasets"] = []  # LIST returns nothing to own
    app = _make_slow_app(data, delay_kinds={})
    app.DRILL_PREWARM_TIMEOUT = 0.1
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        await pilot.press("down")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: app.current_kind == "replicasets" and table.row_count == 0,
            label="empty replicasets view rendered",
        )
        assert table.row_count == 0  # genuinely empty child view is correct


async def test_drill_prewarm_skips_the_wait_when_the_watch_is_live() -> None:
    """A (kind, scope) another pane already watches has a warm bucket: the
    drill must not re-clear it or wait."""
    starts: list[str] = []
    app = _make_slow_app(_default_data(), delay_kinds={}, starts=starts)
    async with app.run_test() as pilot:
        first = app.query_one("#pane-0", ResourceTable)
        await until(
            pilot,
            lambda: first.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(first) == ["api", "web"],
            label="deployments rendered",
        )
        await pilot.press("ctrl+w")
        await pilot.press("v")  # split: both panes on deployments
        await until(pilot, lambda: len(app._panes) == 2, label="split pane opened")
        second = app.query_one("#pane-1", ResourceTable)
        await _navigate(pilot, "replicasets")  # focused pane -> rs watch live
        await until(
            pilot,
            lambda: app.current_kind == "replicasets" and second.row_count == 3,
            label="replicasets pane active",
        )
        await pilot.press("ctrl+w")
        await pilot.press("w")  # focus back to the deployments pane
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(first) == ["api", "web"],
            label="focus returned",
        )
        starts.clear()
        await pilot.press("down")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (
                app.current_kind == "replicasets"
                and _row_names(first) == ["web-6d9f88", "web-5c4e77"]
            ),
            label="replicaset drill active",
        )
        assert "replicasets" not in starts  # live watch reused, not restarted


async def test_drill_abandons_when_a_newer_navigation_lands_during_prewarm() -> None:
    """The pre-warm widens the window between Enter and the locked
    transaction: a `:view` issued meanwhile is the newer command and must
    win - the stale drill must not override it or strand a level."""
    app = _make_slow_app(_default_data(), delay_kinds={"replicasets": 0.3})
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        drill = asyncio.create_task(app._drill_into("default", "web"))
        await until(
            pilot,
            lambda: ("replicasets", "default") in app.watch_manager.active,
            label="prewarm started",
        )
        await app.on_navigate_command(NavigateCommand("pods", None))
        result = await drill
        assert result is not None  # an accurate outcome, not a false success
        assert "abandoned" in result
        await until(
            pilot,
            lambda: app.current_kind == "pods" and table.row_count == 2,
            label="newer pods navigation kept",
        )
        assert app.current_kind == "pods"  # the newer command won
        assert not app._pane.drill.active  # no stranded drill level
        # the pre-warmed replicasets stream was reaped, not leaked
        assert ("replicasets", "default") not in app.watch_manager.active


async def test_drill_abandons_across_a_context_epoch_change() -> None:
    """A context switch during the pre-warm invalidates the captured UID
    (it names an object in the old cluster): the drill must abandon."""
    app = _make_slow_app(_default_data(), delay_kinds={"replicasets": 0.3})
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        drill = asyncio.create_task(app._drill_into("default", "web"))
        await until(
            pilot,
            lambda: ("replicasets", "default") in app.watch_manager.active,
            label="prewarm started",
        )
        app._ctx_epoch += 1  # what a :ctx switch does
        result = await drill
        assert result is not None
        assert "abandoned" in result
        assert app.current_kind == "deployments"  # stayed put
        assert not app._pane.drill.active


async def test_pop_abandons_when_the_view_changed_during_prewarm() -> None:
    """Esc's pop pre-warms the parent kind: a navigation landing during
    that wait cleared the drill stack - the stale pop must not navigate."""
    delays = {"deployments": 0.0}
    app = _make_slow_app(_default_data(), delay_kinds=delays)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pod rows visible")
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        await pilot.press("down")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (
                app.current_kind == "replicasets"
                and _row_names(table) == ["web-6d9f88", "web-5c4e77"]
            ),
            label="replicaset drill active",
        )
        delays["deployments"] = 0.3  # slow re-LIST on the way back
        pop = asyncio.create_task(app._pop_drill())
        await until(
            pilot,
            lambda: ("deployments", "default") in app.watch_manager.active,
            label="pop prewarm started",
        )
        await app.on_navigate_command(NavigateCommand("pods", None))
        assert await pop is True  # consumed, but did not override
        await until(
            pilot,
            lambda: app.current_kind == "pods" and table.row_count == 2,
            label="pods navigation preserved",
        )
        assert app.current_kind == "pods"  # the newer command won


async def test_drill_abandons_when_a_same_target_navigation_lands_during_prewarm() -> None:
    """`:view deployments` while already on deployments is still the newer
    command (it clears drill state): a (kind, scope) tuple comparison alone
    cannot see it - the per-pane navigation generation must."""
    app = _make_slow_app(_default_data(), delay_kinds={"replicasets": 0.3})
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        drill = asyncio.create_task(app._drill_into("default", "web"))
        await until(
            pilot,
            lambda: ("replicasets", "default") in app.watch_manager.active,
            label="prewarm started",
        )
        await app.on_navigate_command(NavigateCommand("deployments", None))  # same target
        result = await drill
        assert result is not None
        assert "abandoned" in result
        assert app.current_kind == "deployments"
        assert not app._pane.drill.active  # the newer command's clear stands


async def test_pane_closed_during_prewarm_reports_abandonment() -> None:
    """agent_drill_down reports success on a None result: a pane closed
    during the pre-warm must yield an accurate abandonment, not a false
    'drilled into ...' with a breadcrumb."""
    app = _make_slow_app(_default_data(), delay_kinds={"replicasets": 0.3})
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: app.query_one(ResourceTable).row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await pilot.press("ctrl+w")
        await pilot.press("v")  # split so a pane *can* close
        await until(pilot, lambda: len(app._panes) == 2, label="split pane opened")
        drill = asyncio.create_task(app._drill_into("default", "web"))
        await until(
            pilot,
            lambda: ("replicasets", "default") in app.watch_manager.active,
            label="prewarm started",
        )
        await pilot.press("ctrl+w")
        await pilot.press("q")  # close the initiating pane mid-wait
        result = await drill
        assert result is not None
        assert "abandoned" in result
        # the pre-warmed stream was reaped, not leaked
        await until(
            pilot,
            lambda: ("replicasets", "default") not in app.watch_manager.active,
            label="prewarm reaped",
        )


async def test_overlapping_drills_do_not_skip_each_others_prewarm() -> None:
    """Two drills racing to the same (kind, scope): the second must wait on
    its *own* readiness instead of treating the first's in-flight pre-warm
    watch as warm - skipping recreated the empty-view flash."""
    renders: list[tuple[str, int]] = []
    app = _make_slow_app(_default_data(), delay_kinds={"replicasets": 0.25})
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        _spy_renders(app, renders)
        first = asyncio.create_task(app._drill_into("default", "web"))
        await until(
            pilot,
            lambda: ("replicasets", "default") in app.watch_manager.active,
            label="first prewarm started",
        )
        second = asyncio.create_task(app._drill_into("default", "api"))
        results = [await first, await second]
        await until(pilot, lambda: app.current_kind == "replicasets", label="winning drill landed")
        assert app.current_kind == "replicasets"
        assert ("replicasets", 0) not in renders  # neither drill flashed empty
        # exactly one drill landed; the loser abandoned with an accurate result
        assert sum(1 for r in results if r is None) == 1
        assert any(r is not None and "abandoned" in r for r in results)


async def test_cancelled_prewarm_releases_its_lease_and_watch() -> None:
    """A drill task cancelled mid-pre-warm (e.g. app teardown, :ctx) must
    not leave a permanent lease - that would block stream reaping for every
    later drill on the same (kind, scope) - nor leak the started watch."""
    app = _make_slow_app(_default_data(), delay_kinds={"replicasets": 5.0})
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and _row_names(table) == ["api", "web"],
            label="deployments rendered",
        )
        drill = asyncio.create_task(app._drill_into("default", "web"))
        await until(
            pilot,
            lambda: ("replicasets", "default") in app.watch_manager.active,
            label="prewarm started",
        )
        drill.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drill
        await until(
            pilot,
            lambda: not app._prewarm_leases,
            label="lease released",
        )
        assert ("replicasets", "default") not in app.watch_manager.active


async def test_two_level_pop_waits_for_rows_the_drill_filter_will_show() -> None:
    """Popping pods -> replicasets keeps the deployment-UID filter: an
    unrelated ReplicaSet arriving first must not satisfy the readiness and
    flash a zero-row filtered view."""
    data = _default_data()
    store = ResourceStore()
    rs_lists = {"n": 0}

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if kind == "replicasets":
            rs_lists["n"] += 1
            if rs_lists["n"] > 1:  # the re-LIST on the way back
                yield ("ADDED", _rs("api-777", "rs-9", "dep-9"))  # unrelated first
                await asyncio.sleep(0.2)
        for obj in data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def list_namespaces() -> list[str]:
        return ["default"]

    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=dict(_ALIASES),
    )
    renders: list[tuple[str, int]] = []
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "deployments")
        await until(
            pilot,
            lambda: (
                app.current_kind == "deployments"
                and _row_names(table) == ["api", "web"]
                and app._cursor_row_key() == "default/api"
            ),
            label="deployments rendered",
        )
        await pilot.press("down")
        await pilot.press("enter")  # web -> replicasets
        await until(
            pilot,
            lambda: (
                app.current_kind == "replicasets"
                and _row_names(table) == ["web-6d9f88", "web-5c4e77"]
                and app._cursor_row_key() == "default/web-6d9f88"
            ),
            label="replicaset drill active",
        )
        await pilot.press("enter")  # -> pods
        await until(
            pilot,
            lambda: (
                app.current_kind == "pods"
                and _row_names(table) == ["web-6d9f88-aaa"]
                and app._cursor_row_key() == "default/web-6d9f88-aaa"
            ),
            label="pod drill active",
        )
        _spy_renders(app, renders)
        await pilot.press("escape")
        await until(
            pilot, lambda: app.current_kind == "replicasets", label="replicaset drill restored"
        )
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="owned rows visible")
        assert ("replicasets", 0) not in renders


async def test_prewarm_restarts_a_dead_watch_even_when_pane_backed() -> None:
    """A pane displaying the target is only warm while its watch lives: a
    teardown racing the check must not skip the start and the wait."""
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "replicasets")
        await until(
            pilot,
            lambda: app.current_kind == "replicasets" and table.row_count == 3,
            label="replicasets rendered",
        )
        await app.watch_manager.stop("replicasets", "default")  # teardown race stand-in
        await app._prewarm_view("replicasets", "default", lambda rows: bool(rows))
        assert ("replicasets", "default") in app.watch_manager.active  # restarted
        await app._stop_watch_if_unused("replicasets", "default")
        # still displayed by the pane: the release must not reap it
        assert ("replicasets", "default") in app.watch_manager.active


async def test_navigation_teardown_honors_outstanding_prewarm_leases() -> None:
    """_navigate_locked stops the view it leaves - unless a drill pre-warm
    still holds a lease on that stream; killing it would force the drill's
    own navigate to re-LIST into the empty flash."""
    app = make_app(_default_data())
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(
            pilot,
            lambda: table.row_count == 2,
            label="pod rows visible",
        )
        await _navigate(pilot, "replicasets")
        await until(
            pilot,
            lambda: app.current_kind == "replicasets" and table.row_count == 3,
            label="replicasets rendered",
        )
        app._prewarm_leases[("replicasets", "default")] = 1  # an in-flight drill's lease
        await app.on_navigate_command(NavigateCommand("pods", None))
        await until(
            pilot,
            lambda: (
                app.current_kind == "pods"
                and _row_names(table) == ["api-777-bbb", "web-6d9f88-aaa"]
            ),
            label="pods view active",
        )
        assert app.current_kind == "pods"
        assert ("replicasets", "default") in app.watch_manager.active  # lease honored
        await app._stop_watch_if_unused("replicasets", "default")  # last release
        assert ("replicasets", "default") not in app.watch_manager.active  # now reaped
