"""App wiring for the metrics.k8s.io poller (issue #12).

The poller runs only while the pods view is on screen, tracks the
namespace scope, and its updates re-render the table with live CPU/MEM.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.k8s.metrics import MetricsPoller, PodMetrics
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.resource_table import ResourceTable

from .test_app import _DEFAULT_TEST_ALIASES, _pod
from .waits import until


def _source(pods: list[PodSummary]):  # type: ignore[no-untyped-def]  # returns a local async generator fn; annotating it adds noise, not safety
    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if kind == "pods":
            for p in pods:
                yield ("ADDED", p)
        while True:
            await asyncio.sleep(0.01)

    return source


def make_app_with_metrics(
    pods: list[PodSummary],
    responses: list[object],
) -> tuple[KorvidApp, list[str | None]]:
    from korvid.core.watch import WatchManager

    calls: list[str | None] = []

    async def fetch(namespace: str | None) -> list[PodMetrics]:
        calls.append(namespace)
        result = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, list)
        return result

    store = ResourceStore()
    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source(pods)),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        metrics=MetricsPoller(fetch, interval=0.05),
    )
    return app, calls


_WEB_METRICS = PodMetrics(
    name="api-1", namespace="default", cpu_cores=0.1, memory_bytes=128 * 2**20
)


def _pod_with_requests(name: str) -> PodSummary:
    return PodSummary(
        name=name,
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        cpu_request="200m",
        mem_request="256Mi",
        cpu_request_cores=0.2,
        mem_request_bytes=256 * 2**20,
    )


def _row_for(table: ResourceTable, name: str) -> list[object]:
    for row_index in range(table.row_count):
        row = table.get_row_at(row_index)
        if str(row[0]) == name:
            return list(row)
    raise AssertionError(f"row {name!r} not found")


async def test_pod_table_has_usage_columns() -> None:
    app, _ = make_app_with_metrics([_pod_with_requests("api-1")], [[_WEB_METRICS]])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        labels = [str(col.label) for col in table.columns.values()]
        assert "CPU" in labels
        assert "%CPU/R" in labels
        assert "MEM" in labels
        assert "%MEM/R" in labels


async def test_metrics_join_renders_usage_and_percent() -> None:
    app, _ = make_app_with_metrics([_pod_with_requests("api-1")], [[_WEB_METRICS]])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)

        def _joined() -> bool:
            return table.row_count > 0 and str(_row_for(table, "api-1")[4]) == "100m"

        await until(pilot, _joined, label="metrics join renders usage")
        row = _row_for(table, "api-1")
        assert str(row[5]) == "50"  # 100m of 200m request
        assert str(row[6]) == "128Mi"
        assert str(row[7]) == "50"  # 128Mi of 256Mi request


async def test_no_metrics_server_renders_dashes() -> None:
    from korvid.k8s.errors import ApiStatusError

    app, calls = make_app_with_metrics(
        [_pod_with_requests("api-1")], [ApiStatusError(404, "NotFound")]
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await until(pilot, lambda: len(calls) >= 1, label="metrics poll records initial call")
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        row = _row_for(table, "api-1")
        assert [str(row[i]) for i in (4, 5, 6, 7)] == ["-", "-", "-", "-"]


async def test_poller_scoped_to_current_namespace() -> None:
    app, calls = make_app_with_metrics([_pod_with_requests("api-1")], [[_WEB_METRICS]])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await until(
            pilot, lambda: len(calls) >= 1, label="metrics poll records initial namespace call"
        )
        assert calls[0] == "default"


async def test_all_namespaces_polls_cluster_scope() -> None:
    app, calls = make_app_with_metrics([_pod_with_requests("api-1")], [[_WEB_METRICS]])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("0")  # toggle all-namespaces
        await until(pilot, lambda: None in calls, label="metrics poll records cluster scope")
        assert None in calls
        # ALL_NAMESPACES pod view still renders (with the NAMESPACE column).
        assert app.current_scope == ALL_NAMESPACES


async def test_leaving_pods_view_stops_polling() -> None:
    app, calls = make_app_with_metrics([_pod_with_requests("api-1")], [[_WEB_METRICS]])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await until(pilot, lambda: len(calls) >= 1, label="metrics poll starts before navigation")
        await pilot.press("colon")
        for ch in "deploy":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.2)
        count = len(calls)
        await pilot.pause(0.3)
        assert len(calls) == count  # no polls while off the pods view


async def test_returning_to_pods_view_resumes_polling() -> None:
    app, calls = make_app_with_metrics([_pod_with_requests("api-1")], [[_WEB_METRICS]])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("colon")
        for ch in "deploy":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.2)
        count = len(calls)
        await pilot.press("colon")
        for ch in "pods":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(
            pilot, lambda: len(calls) > count, label="metrics poll resumes after returning to pods"
        )
        assert len(calls) > count


async def test_app_without_poller_still_renders_pods() -> None:
    app = make_app_plain([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        table = app.query_one(ResourceTable)
        assert table.row_count == 1
        row = _row_for(table, "api-1")
        assert str(row[4]) == "-"  # CPU column present but empty


def make_app_plain(pods: list[PodSummary]) -> KorvidApp:
    from korvid.core.watch import WatchManager

    store = ResourceStore()
    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source(pods)),
        aliases=dict(_DEFAULT_TEST_ALIASES),
    )
