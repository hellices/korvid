"""App wiring for the ops hint strip (#26): cursor on an abnormal pod row
shows trouble details and the newest warning event; healthy rows hide it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.models import ContainerTrouble, PodSummary
from korvid.ui.app import EventsFetcher, KorvidApp
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


class _FnFetcher(EventsFetcher):
    """Adapts a `(namespace, name, *, uid)` coroutine fn to the fetcher ABC."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    async def fetch(
        self, namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._fn(namespace, name, uid=uid)  # type: ignore[no-any-return]  # test fakes return list[dict]


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
) -> tuple[KorvidApp, list[tuple[str, str, str | None]]]:
    calls: list[tuple[str, str, str | None]] = []

    async def get_events(
        namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]:
        calls.append((namespace, name, uid))
        return events or []

    store = ResourceStore()
    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source(pods)),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        get_events=_FnFetcher(get_events),
    )
    return app, calls


def _hint_detail_workers_done(app: KorvidApp) -> bool:
    """True once no hint-detail worker is pending or running - negative
    overlay assertions are meaningless while the worker could still push
    the screen (PR #51 r7)."""
    return not any(w.group == "hint-detail" and not w.is_finished for w in app.workers)


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
            "reason": "FailedMount",
            "message": "MountVolume.SetUp failed",
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
            lambda: "MountVolume.SetUp failed" in _strip_text(app),
            label="event line shown",
        )
        text = _strip_text(app)
        assert "FailedMount: MountVolume.SetUp failed" in text
        assert "image pulled" not in text  # Normal events never shown
        assert calls == [("default", "web-1", "uid-web-1")]


async def test_event_restating_the_trouble_is_not_appended() -> None:
    """Issue #34 end-to-end: the freshest Warning is usually the BackOff event
    behind the CrashLoopBackOff status - the strip shows only one of them."""
    events = [
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "restarting failed container app",
            "lastTimestamp": "2026-07-26T08:00:00Z",
        },
    ]
    app, calls = make_app([_pod("web-1", (_CRASH,))], events=events)
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: calls == [("default", "web-1", "uid-web-1")],
            label="event fetched",
        )
        await pilot.pause()
        text = _strip_text(app)
        assert "CrashLoopBackOff" in text
        assert "restarting failed container app" not in text


async def test_event_fetch_is_cached_per_pod() -> None:
    app, calls = make_app([_pod("api-1"), _pod("web-1", (_CRASH,))])
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 2)
        await pilot.press("down")  # web-1: triggers fetch
        await until(pilot, lambda: len(calls) == 1, label="first fetch")
        await pilot.press("up")  # api-1 (healthy, no fetch)
        await pilot.press("down")  # web-1 again: cached, no second fetch
        await pilot.pause(0.2)
        assert calls == [("default", "web-1", "uid-web-1")]


async def test_event_fetch_failure_still_shows_status_trouble() -> None:
    attempts: list[str] = []

    async def failing(namespace: str, name: str, *, uid: str | None = None) -> list[dict[str, Any]]:
        attempts.append(name)
        raise RuntimeError("events unavailable")

    store = ResourceStore()
    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source([_pod("web-1", (_CRASH,))])),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        get_events=_FnFetcher(failing),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: len(attempts) == 1, label="fetch attempted")
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
        assert app.query_one(HintStrip).display is False


_OLD_EVENT = {
    "type": "Warning",
    "reason": "FailedScheduling",
    "message": "stale event from a previous incarnation",
    "lastTimestamp": "2026-07-26T06:00:00Z",
}


async def test_event_older_than_status_termination_is_suppressed() -> None:
    # _CRASH terminated at 08:00; a 06:00 warning must not explain it.
    crash = ContainerTrouble(
        container="app",
        reason="CrashLoopBackOff",
        exit_code=137,
        finished_at="2026-07-26T08:00:00Z",
        restarts=3,
    )
    app, calls = make_app([_pod("web-1", (crash,))], events=[_OLD_EVENT])
    async with app.run_test() as pilot:
        await until(pilot, lambda: len(calls) == 1, label="fetch done")
        await pilot.pause(0.2)
        assert "stale event" not in _strip_text(app)
        assert "CrashLoopBackOff" in _strip_text(app)


async def test_not_ready_running_pod_gets_event_only_hint() -> None:
    # Readiness-probe failures leave trouble empty; the strip is event-only.
    pod = PodSummary(
        name="web-1",
        namespace="default",
        phase="Running",
        ready="0/1",
        restarts=0,
        node=None,
        uid="uid-web-1",
    )
    events = [
        {
            "type": "Warning",
            "reason": "Unhealthy",
            "message": "Readiness probe failed: HTTP 503",
            "lastTimestamp": "2026-07-26T08:00:00Z",
        }
    ]
    app, calls = make_app([pod], events=events)
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: "Readiness probe failed" in _strip_text(app),
            label="event-only hint shown",
        )
        assert calls == [("default", "web-1", "uid-web-1")]


async def test_recovered_pod_is_not_rendered_with_stale_trouble() -> None:
    # The pod recovers while the event fetch is in flight; the completion
    # callback must re-read the store and clear instead of showing old trouble.
    gate = asyncio.Event()
    calls: list[str] = []

    async def gated(namespace: str, name: str, *, uid: str | None = None) -> list[dict[str, Any]]:
        calls.append(name)
        await gate.wait()
        return [
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "old restart loop",
                "lastTimestamp": "2026-07-26T09:00:00Z",
            }
        ]

    store = ResourceStore()
    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source([_pod("web-1", (_CRASH,))])),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        get_events=_FnFetcher(gated),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: len(calls) == 1, label="fetch started")
        # Pod recovers while the fetch is blocked on the gate.
        store.apply_event("pods", "default", "MODIFIED", _pod("web-1"))
        gate.set()
        await until(
            pilot, lambda: not app.query_one(HintStrip).display, label="strip cleared on recovery"
        )
        assert app.query_one(HintStrip).display is False
        assert str(app.query_one(HintStrip).render()) == ""


def test_event_timestamp_prefers_series_last_observed_time() -> None:
    from korvid.ui.app import _newest_warning

    events: list[dict[str, Any]] = [
        {
            "type": "Warning",
            "reason": "Older",
            "message": "initial observation is newer but series is stale",
            "eventTime": "2026-07-26T09:00:00Z",
        },
        {
            "type": "Warning",
            "reason": "Newer",
            "message": "series keeps repeating",
            "eventTime": "2026-07-26T01:00:00Z",
            "series": {"count": 40, "lastObservedTime": "2026-07-26T10:00:00Z"},
        },
    ]
    found = _newest_warning(events)
    assert found is not None
    assert found[0].startswith("Newer:")


def test_abnormal_phase_without_trouble_needs_hint() -> None:
    from korvid.ui.app import _pod_needs_hint

    def pod(phase: str, ready: str = "1/1") -> PodSummary:
        return PodSummary(
            name="p",
            namespace="default",
            phase=phase,
            ready=ready,
            restarts=0,
            node=None,
        )

    assert _pod_needs_hint(pod("Unknown")) is True  # node lost, containers may read 1/1
    assert _pod_needs_hint(pod("Failed", ready="0/0")) is True  # status-only failure
    assert _pod_needs_hint(pod("Running")) is False
    assert _pod_needs_hint(pod("Completed", ready="0/1")) is False  # finished: 0/N by design
    assert _pod_needs_hint(pod("Pending")) is False  # routine startup is not trouble
    assert _pod_needs_hint(pod("Pending", ready="0/1")) is False  # startup is 0/N by design
    assert _pod_needs_hint(pod("Terminating", ready="0/1")) is False  # routine deletion
    assert _pod_needs_hint(pod("ContainerCreating", ready="0/1")) is False
    assert _pod_needs_hint(pod("Running", ready="0/1")) is True  # NotReady while Running
    assert _pod_needs_hint(pod("Init:0/2", ready="0/1")) is False  # routine init progress
    assert _pod_needs_hint(pod("Init:1/2")) is False


async def test_recreated_pod_uid_change_mid_fetch_does_not_render_old_hint() -> None:
    gate = asyncio.Event()

    async def gated(namespace: str, name: str, *, uid: str | None = None) -> list[dict[str, Any]]:
        await gate.wait()
        return [
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "event of the dead incarnation",
                "lastTimestamp": "2026-07-26T09:00:00Z",
            }
        ]

    store = ResourceStore()
    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source([_pod("web-1", (_CRASH,))])),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        get_events=_FnFetcher(gated),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(HintStrip).display, label="initial hint")
        # Same name, new uid, healthy: a recreated pod replaces the old row.
        recreated = PodSummary(
            name="web-1",
            namespace="default",
            phase="Running",
            ready="1/1",
            restarts=0,
            node=None,
            uid="uid-new",
        )
        store.apply_event("pods", "default", "MODIFIED", recreated)
        gate.set()
        await until(
            pilot, lambda: not app.query_one(HintStrip).display, label="no hint for new uid"
        )
        assert "dead incarnation" not in str(app.query_one(HintStrip).render())


def test_undated_event_suppressed_when_status_is_dated() -> None:
    from korvid.ui.app import _event_line_fresh

    dated = _pod("web-1", (_CRASH,))  # _CRASH has no finished_at
    crash_dated = ContainerTrouble(
        container="app",
        reason="CrashLoopBackOff",
        finished_at="2026-07-26T08:00:00Z",
    )
    pod_dated = _pod("web-1", (crash_dated,))
    assert _event_line_fresh(None, pod_dated) is False  # undated event, dated status
    assert _event_line_fresh(None, dated) is True  # no dated status to compare against
    ts_new = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    ts_old = datetime(2026, 7, 26, 7, 0, tzinfo=UTC)
    assert _event_line_fresh(ts_new, pod_dated) is True
    assert _event_line_fresh(ts_old, pod_dated) is False


async def test_failed_fetch_is_retried_after_ttl_while_parked() -> None:
    attempts: list[str] = []

    async def failing(namespace: str, name: str, *, uid: str | None = None) -> list[dict[str, Any]]:
        attempts.append(name)
        raise RuntimeError("transient")

    store = ResourceStore()
    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source([_pod("web-1", (_CRASH,))])),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        get_events=_FnFetcher(failing),
    )
    app._HINT_EVENT_TTL = 0.1  # shrink the TTL so the retry fits in a test
    async with app.run_test() as pilot:
        await until(pilot, lambda: len(attempts) >= 2, label="fetch retried after TTL")
        assert len(attempts) >= 2


async def test_hint_strip_sits_above_status_bar() -> None:
    from korvid.ui.widgets.status_bar import StatusBar

    app, _calls = make_app([_pod("web-1", (_CRASH,))])
    async with app.run_test() as pilot:
        strip = app.query_one(HintStrip)
        await until(pilot, lambda: strip.display, label="hint strip visible")
        await pilot.pause()
        bar = app.query_one(StatusBar)
        assert strip.region.y < bar.region.y  # hint renders above the status bar


def test_event_timestamp_falls_back_to_first_and_creation_timestamp() -> None:
    from korvid.ui.app import _event_timestamp

    first_only = {"firstTimestamp": "2026-07-26T08:00:00Z"}
    creation_only = {"metadata": {"creationTimestamp": "2026-07-26T07:00:00Z"}}
    assert _event_timestamp(first_only) == datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    assert _event_timestamp(creation_only) == datetime(2026, 7, 26, 7, 0, tzinfo=UTC)
    assert _event_timestamp({}) is None
    # lastTimestamp still wins over the deprecated fallbacks
    both = {"lastTimestamp": "2026-07-26T09:00:00Z", "firstTimestamp": "2026-07-26T08:00:00Z"}
    assert _event_timestamp(both) == datetime(2026, 7, 26, 9, 0, tzinfo=UTC)


def test_event_older_than_ready_transition_is_stale_for_event_only_hint() -> None:
    from korvid.ui.app import _event_line_fresh

    base = _pod("web-1", ())
    not_ready = PodSummary(
        name=base.name,
        namespace=base.namespace,
        phase="Running",
        ready="0/1",
        restarts=0,
        node=None,
        uid=base.uid,
        ready_transition_at="2026-07-26T08:00:00Z",
    )
    before = datetime(2026, 7, 26, 7, 0, tzinfo=UTC)
    after = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    assert _event_line_fresh(before, not_ready) is False  # explains a previous failure
    assert _event_line_fresh(after, not_ready) is True
    # Nearly every pod has a Ready condition, so an undated Warning is in
    # practice always suppressed: a wrong "cause" is worse than none.
    assert _event_line_fresh(None, not_ready) is False


async def test_i_on_troubled_row_opens_detail_overlay() -> None:
    """Issue #34: `i` opens the read-only detail overlay for the hinted row."""
    from korvid.ui.widgets.hint_detail import HintDetailScreen

    events = [
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "restarting failed container app",
            "lastTimestamp": "2026-07-26T08:00:00Z",
        },
    ]
    app, _calls = make_app([_pod("web-1", (_CRASH,))], events=events)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(HintStrip).display, label="strip visible")
        await pilot.press("i")
        await until(
            pilot,
            lambda: isinstance(app.screen, HintDetailScreen),
            label="detail overlay open",
        )
        text = str(app.screen.query_one("#hint-detail-body").render())
        assert "CrashLoopBackOff" in text
        assert "restarting failed container app" in text
        await pilot.press("escape")
        await until(
            pilot,
            lambda: not isinstance(app.screen, HintDetailScreen),
            label="overlay dismissed",
        )


async def test_i_on_healthy_row_is_a_noop() -> None:
    from korvid.ui.widgets.hint_detail import HintDetailScreen

    app, calls = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1)
        await pilot.pause(0.1)
        await pilot.press("i")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, HintDetailScreen)
        assert calls == []  # no hint -> no event fetch for the overlay either


async def test_overlay_aborts_when_cursor_moves_during_event_fetch() -> None:
    """Review fix (PR #51): the overlay fetch awaits the events API; if the
    cursor moved meanwhile the details describe the wrong pod - abort."""
    from korvid.ui.widgets.hint_detail import HintDetailScreen

    gate = asyncio.Event()

    async def get_events(
        namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]:
        await gate.wait()
        return []

    store = ResourceStore()
    pods = [_pod("aaa-1", (_CRASH,)), _pod("zzz-1")]
    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source(pods)),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        get_events=_FnFetcher(get_events),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(HintStrip).display, label="strip on aaa-1")
        await pilot.press("i")  # overlay fetch now blocked on the gate
        await pilot.press("down")  # cursor leaves the hinted row
        gate.set()
        await until(pilot, lambda: _hint_detail_workers_done(app), label="overlay worker done")
        assert not isinstance(app.screen, HintDetailScreen)


async def test_overlay_reports_unavailable_events_on_fetch_failure() -> None:
    async def get_events(
        namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]:
        raise RuntimeError("events API down")

    store = ResourceStore()
    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source([_pod("web-1", (_CRASH,))])),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        get_events=_FnFetcher(get_events),
    )
    async with app.run_test() as pilot:
        from korvid.ui.widgets.hint_detail import HintDetailScreen

        await until(pilot, lambda: app.query_one(HintStrip).display, label="strip visible")
        await pilot.press("i")
        await until(
            pilot,
            lambda: isinstance(app.screen, HintDetailScreen),
            label="overlay open despite event failure",
        )
        text = str(app.screen.query_one("#hint-detail-body").render())
        assert "CrashLoopBackOff" in text
        assert "warning events unavailable" in text


async def test_overlay_aborts_when_pod_recovers_during_event_fetch() -> None:
    """Review fix (PR #51 r2): a pod that recovered mid-fetch no longer
    qualifies for a hint - opening an empty detail modal would be noise."""
    from korvid.ui.widgets.hint_detail import HintDetailScreen

    gate = asyncio.Event()

    async def get_events(
        namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]:
        await gate.wait()
        return []

    store = ResourceStore()
    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source([_pod("web-1", (_CRASH,))])),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        get_events=_FnFetcher(get_events),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(HintStrip).display, label="strip visible")
        await pilot.press("i")  # overlay fetch blocked on the gate
        recovered = _pod("web-1")  # healthy now, same uid
        store.apply_event("pods", app.current_scope, "MODIFIED", recovered)
        gate.set()
        await until(pilot, lambda: _hint_detail_workers_done(app), label="overlay worker done")
        assert not isinstance(app.screen, HintDetailScreen)


async def test_overlay_opens_when_event_fetch_stalls(monkeypatch: Any) -> None:
    """Review fix (PR #51 r6): a stalled events API must not hold the overlay
    hostage for the HTTP client's full timeout - bound the wait with a short
    UI timeout and open with the events marked unavailable."""
    from korvid.ui import app as app_mod
    from korvid.ui.widgets.hint_detail import HintDetailScreen

    monkeypatch.setattr(app_mod, "_HINT_EVENTS_TIMEOUT", 0.05)
    stall = asyncio.Event()

    async def get_events(
        namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]:
        await stall.wait()  # never set: simulates a hung API connection
        return []

    store = ResourceStore()
    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source([_pod("web-1", (_CRASH,))])),
        aliases=dict(_DEFAULT_TEST_ALIASES),
        get_events=_FnFetcher(get_events),
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(HintStrip).display, label="strip visible")
        await pilot.press("i")
        await until(
            pilot,
            lambda: isinstance(app.screen, HintDetailScreen),
            label="overlay open despite stalled event fetch",
        )
        text = str(app.screen.query_one("#hint-detail-body").render())
        assert "CrashLoopBackOff" in text
        assert "warning events unavailable" in text
        stall.set()
