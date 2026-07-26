"""App wiring for the ops hint strip (#26): cursor on an abnormal pod row
shows trouble details and the newest warning event; healthy rows hide it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.models import ContainerTrouble, PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.hint_strip import HintStrip
from korvid.ui.widgets.resource_table import ResourceTable

from .test_app import _DEFAULT_TEST_ALIASES
from .waits import until

_CRASH = ContainerTrouble(
    container="app",
    reason="CrashLoopBackOff",
    message="back-off 5m0s restarting failed container",
    exit_code=137,
    exit_reason="OOMKilled",
    restarts=12,
)


def _pod(name: str, trouble: tuple[ContainerTrouble, ...] = ()) -> PodSummary:
    return PodSummary(
        name=name,
        namespace="default",
        phase="CrashLoopBackOff" if trouble else "Running",
        ready="0/1" if trouble else "1/1",
        restarts=trouble[0].restarts if trouble else 0,
        node=None,
        uid=f"uid-{name}",
        trouble=trouble,
    )


def _source(pods: list[PodSummary]):  # type: ignore[no-untyped-def]  # returns a local async generator fn
    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if kind == "pods":
            for p in pods:
                yield ("ADDED", p)
        while True:
            await asyncio.sleep(0.01)

    return source


def make_app(
    pods: list[PodSummary],
    events: list[dict[str, Any]] | None = None,
) -> tuple[KorvidApp, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        calls.append((namespace, name))
        return events or []

    store = ResourceStore()
    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source(pods)),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        get_events=get_events,
    )
    return app, calls


def _strip_text(app: KorvidApp) -> str:
    return str(app.query_one(HintStrip).render())


async def test_cursor_on_troubled_pod_shows_hint_strip() -> None:
    app, _ = make_app([_pod("web-1", (_CRASH,))])
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1)
        await until(pilot, lambda: app.query_one(HintStrip).display, label="hint strip visible")
        text = _strip_text(app)
        assert "CrashLoopBackOff" in text
        assert "exit 137 (OOMKilled)" in text


async def test_cursor_on_healthy_pod_hides_hint_strip() -> None:
    app, _ = make_app([_pod("api-1"), _pod("web-1", (_CRASH,))])
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 2)
        # rows sort by name: api-1 (healthy) first, cursor starts there
        await pilot.pause(0.1)
        assert app.query_one(HintStrip).display is False
        await pilot.press("down")  # move onto web-1 (troubled)
        await until(pilot, lambda: app.query_one(HintStrip).display, label="strip on web-1")
        await pilot.press("up")  # back to healthy api-1
        await until(pilot, lambda: not app.query_one(HintStrip).display, label="strip hidden again")


async def test_warning_event_is_fetched_and_appended() -> None:
    events = [
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "restarting failed container app",
            "lastTimestamp": "2026-07-26T08:00:00Z",
        },
        {
            "type": "Normal",
            "reason": "Pulled",
            "message": "image pulled",
            "lastTimestamp": "2026-07-26T09:00:00Z",
        },
    ]
    app, calls = make_app([_pod("web-1", (_CRASH,))], events=events)
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: "restarting failed container app" in _strip_text(app),
            label="event line shown",
        )
        text = _strip_text(app)
        assert "BackOff: restarting failed container app" in text
        assert "image pulled" not in text  # Normal events never shown
        assert calls == [("default", "web-1")]


async def test_event_fetch_is_cached_per_pod() -> None:
    app, calls = make_app([_pod("api-1"), _pod("web-1", (_CRASH,))])
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 2)
        await pilot.press("down")  # web-1: triggers fetch
        await until(pilot, lambda: len(calls) == 1, label="first fetch")
        await pilot.press("up")  # api-1 (healthy, no fetch)
        await pilot.press("down")  # web-1 again: cached, no second fetch
        await pilot.pause(0.2)
        assert calls == [("default", "web-1")]


async def test_event_fetch_failure_still_shows_status_trouble() -> None:
    async def failing(namespace: str, name: str) -> list[dict[str, Any]]:
        raise RuntimeError("events unavailable")

    store = ResourceStore()
    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source([_pod("web-1", (_CRASH,))])),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        get_events=failing,
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(HintStrip).display, label="strip visible")
        assert "CrashLoopBackOff" in _strip_text(app)


async def test_strip_absent_on_non_pod_views() -> None:
    app, _ = make_app([_pod("web-1", (_CRASH,))])
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(HintStrip).display, label="strip on pods")
        await pilot.press("colon")
        for ch in "deploy":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(
            pilot, lambda: not app.query_one(HintStrip).display, label="strip off deploy view"
        )
