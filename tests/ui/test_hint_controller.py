"""Unit tests for `HintController` (issue #97 U3b).

The controller owns the pods hint-strip lifecycle — event cache, parked-cursor
refresh timer, background event fetch — without touching widgets or workers:
everything arrives as narrow callables, so it is tested here without an app.
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from typing import Any

from korvid.k8s.models import ContainerTrouble, PodSummary
from korvid.ui.hints import EventsFetcher, HintController


def _pod(
    phase: str = "Running",
    ready: str = "1/1",
    *,
    trouble: tuple[ContainerTrouble, ...] = (),
    uid: str = "u1",
) -> PodSummary:
    return PodSummary(
        name="pod-a",
        namespace="default",
        phase=phase,
        ready=ready,
        restarts=0,
        node=None,
        uid=uid,
        trouble=trouble,
    )


_TROUBLE = (ContainerTrouble(container="app", reason="CrashLoopBackOff"),)


class FakeTimer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeEvents(EventsFetcher):
    def __init__(self, events: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.events = events or []
        self.fail = fail
        self.calls: list[tuple[str, str, str | None]] = []

    async def fetch(
        self, namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((namespace, name, uid))
        if self.fail:
            raise RuntimeError("events API down")
        return self.events


class Harness:
    def __init__(
        self,
        *,
        summary: PodSummary | None = None,
        events: FakeEvents | None = None,
        cursor: str | None = "default/pod-a",
        pods_view: bool = True,
        ctx_crossed: bool = False,
    ) -> None:
        self.summary = summary
        self.events = events
        self.cursor = cursor
        self.pods_view = pods_view
        self.ctx_crossed = ctx_crossed
        self.shown: list[tuple[tuple[ContainerTrouble, ...], str | None]] = []
        self.cleared = 0
        self.fetches: list[Any] = []
        self.timers: list[tuple[float, Any, FakeTimer]] = []
        self.controller = HintController(
            find_pod_summary=lambda _key: self.summary,
            cursor_row_key=lambda: self.cursor,
            on_pods_view=lambda: self.pods_view,
            get_events=lambda: self.events,
            show_trouble=self._show,
            clear_hint=self._clear,
            start_fetch=self.fetches.append,
            set_timer=self._set_timer,
            ctx_epoch=lambda: 0,
            ctx_crossed=lambda _epoch: self.ctx_crossed,
        )

    def _show(self, trouble: tuple[ContainerTrouble, ...], *, event: str | None = None) -> None:
        self.shown.append((trouble, event))

    def _clear(self) -> None:
        self.cleared += 1

    def _set_timer(self, delay: float, callback: Any) -> FakeTimer:
        timer = FakeTimer()
        self.timers.append((delay, callback, timer))
        return timer

    async def drain_fetch(self) -> None:
        """Run the fetch coroutine the controller handed to `start_fetch`."""
        assert self.fetches, "no fetch was started"
        await self.fetches.pop(0)


async def test_row_without_summary_clears_the_strip() -> None:
    h = Harness(summary=None)
    h.controller.show_for_row("default/pod-a")
    assert h.cleared == 1
    assert not h.shown
    assert not h.fetches


async def test_troubled_row_shows_trouble_and_starts_event_fetch() -> None:
    h = Harness(summary=_pod(trouble=_TROUBLE), events=FakeEvents())
    h.controller.show_for_row("default/pod-a")
    assert h.shown == [(_TROUBLE, None)]
    assert len(h.fetches) == 1
    await h.drain_fetch()


async def test_event_only_row_stays_clear_until_the_warning_arrives() -> None:
    events = FakeEvents([{"type": "Warning", "reason": "Unhealthy", "message": "probe failed"}])
    h = Harness(summary=_pod("Running", "0/1"), events=events)
    h.controller.show_for_row("default/pod-a")
    assert h.cleared == 1  # nothing to show yet
    await h.drain_fetch()
    assert h.shown[-1] == ((), "Unhealthy: probe failed")


async def test_fresh_cache_hit_renders_without_a_fetch() -> None:
    h = Harness(summary=_pod(trouble=_TROUBLE), events=FakeEvents())
    h.controller.cache["default/pod-a#u1"] = (
        monotonic(),
        "BackOff: restarting",
        datetime.now(UTC),
    )
    h.controller.show_for_row("default/pod-a")
    assert h.shown == [(_TROUBLE, "BackOff: restarting")]
    assert not h.fetches
    assert h.timers, "parked-cursor refresh must stay armed on a cache hit"


async def test_fetch_success_caches_and_rerenders_while_cursor_holds() -> None:
    events = FakeEvents(
        [{"type": "Warning", "reason": "BackOff", "message": "restarting container"}]
    )
    h = Harness(summary=_pod(trouble=_TROUBLE), events=events)
    h.controller.show_for_row("default/pod-a")
    await h.drain_fetch()
    assert events.calls == [("default", "pod-a", "u1")]
    key = "default/pod-a#u1"
    assert h.controller.cache[key][1] == "BackOff: restarting container"
    assert h.shown[-1] == (_TROUBLE, "BackOff: restarting container")


async def test_fetch_failure_caches_none_and_schedules_a_retry() -> None:
    h = Harness(summary=_pod(trouble=_TROUBLE), events=FakeEvents(fail=True))
    h.controller.show_for_row("default/pod-a")
    await h.drain_fetch()
    assert h.controller.cache["default/pod-a#u1"][1] is None
    assert h.timers, "a transient API failure must not hide the hint forever"


async def test_ctx_switch_during_fetch_discards_the_result() -> None:
    events = FakeEvents([{"type": "Warning", "reason": "BackOff", "message": "x"}])
    h = Harness(summary=_pod(trouble=_TROUBLE), events=events)
    h.controller.show_for_row("default/pod-a")
    h.ctx_crossed = True  # the switch lands while the fetch is in flight
    await h.drain_fetch()
    assert h.controller.cache == {}


async def test_store_sweeps_expired_entries() -> None:
    h = Harness(summary=_pod(trouble=_TROUBLE))
    h.controller.cache["old#u0"] = (monotonic() - 999.0, "stale", None)
    h.controller.store_event("new#u1", "fresh", None)
    assert "old#u0" not in h.controller.cache
    assert h.controller.cache["new#u1"][1] == "fresh"


async def test_teardown_stops_the_timer_and_clears_the_cache() -> None:
    h = Harness(summary=_pod(trouble=_TROUBLE), events=FakeEvents())
    h.controller.cache["k#u"] = (monotonic(), "line", None)
    h.controller.show_for_row("default/pod-a")
    await h.drain_fetch()
    timer = h.timers[-1][2]
    h.controller.teardown()
    assert timer.stopped
    assert h.controller.cache == {}
    assert h.controller.timer is None


async def test_refresh_for_focus_clears_when_off_the_pods_view() -> None:
    h = Harness(summary=_pod(trouble=_TROUBLE), pods_view=False)
    h.controller.refresh_for_focus()
    assert h.cleared == 1
    assert not h.shown


async def test_refresh_for_focus_renders_the_cursor_row() -> None:
    h = Harness(summary=_pod(trouble=_TROUBLE), events=FakeEvents())
    h.controller.refresh_for_focus()
    assert h.shown == [(_TROUBLE, None)]
    await h.drain_fetch()
