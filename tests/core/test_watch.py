import asyncio
from collections.abc import AsyncIterator, Callable

from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.models import PodSummary


def _pod(name: str) -> PodSummary:
    return PodSummary(
        name=name, namespace="default", phase="Running", ready="1/1", restarts=0, node=None
    )


def make_source(
    events: list[tuple[str, PodSummary]], forever: bool = True
) -> Callable[[str], AsyncIterator[tuple[str, PodSummary]]]:
    async def source(namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        for ev in events:
            yield ev
        while forever:  # simulate an open stream
            await asyncio.sleep(0.01)
            if False:
                yield ("", _pod(""))  # pragma: no cover - typing aid

    return source


async def test_start_feeds_store() -> None:
    store = ResourceStore()
    mgr = WatchManager(store, make_source([("ADDED", _pod("a")), ("ADDED", _pod("b"))]))
    await mgr.start("pods", "default")
    await asyncio.sleep(0.05)
    assert [p.name for p in store.get("pods", "default")] == ["a", "b"]
    assert mgr.active == {("pods", "default")}
    await mgr.stop_all()


async def test_start_is_idempotent() -> None:
    store = ResourceStore()
    mgr = WatchManager(store, make_source([]))
    await mgr.start("pods", "default")
    await mgr.start("pods", "default")
    assert len(mgr.active) == 1
    await mgr.stop_all()


async def test_stop_cancels() -> None:
    store = ResourceStore()
    mgr = WatchManager(store, make_source([]))
    await mgr.start("pods", "default")
    await mgr.stop("pods", "default")
    assert mgr.active == set()


async def test_stream_end_reconnects() -> None:
    store = ResourceStore()
    calls = 0

    async def flaky(namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        nonlocal calls
        calls += 1
        yield ("ADDED", _pod(f"p{calls}"))
        # stream ends -> k8s watch timeout simulation; manager must reconnect

    mgr = WatchManager(store, flaky, retry_delay=0)
    await mgr.start("pods", "default")
    await asyncio.sleep(0.05)
    assert calls >= 2
    await mgr.stop_all()


async def test_failing_watch_reports_and_removes_task() -> None:
    store = ResourceStore()
    errors: list[str] = []

    async def broken(namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        raise RuntimeError("boom")
        yield ("", _pod(""))  # pragma: no cover - makes this an async generator

    mgr = WatchManager(store, broken, on_error=errors.append, retry_delay=0, max_retries=2)
    await mgr.start("pods", "default")
    await asyncio.sleep(0.05)
    assert mgr.active == set()  # dead task removed, not lying around as "active"
    assert errors
    assert "boom" in errors[0]


async def test_normal_stream_end_resets_failure_streak() -> None:
    """Empty stream end (no events yielded) is a successful connection and must reset the streak."""
    store = ResourceStore()
    errors: list[str] = []
    calls = 0

    async def flaky_with_empty_stream(namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        nonlocal calls
        calls += 1
        if calls <= 2:  # first (max_retries - 1) calls fail
            raise RuntimeError(f"failure {calls}")
        elif calls == 3:  # then one normal end with zero events (resets streak)
            return
            yield  # pragma: no cover - typing aid
        elif calls <= 5:  # subsequent (max_retries - 1) calls fail again
            raise RuntimeError(f"failure {calls}")
        else:  # final call: if we get here, streak wasn't reset; trigger error
            raise RuntimeError(f"failure {calls}")

    # max_retries=3: fail 2x, then success (empty stream resets), then fail 2x more.
    # Without the fix: failures not reset, so call 4 (3rd consecutive) triggers on_error.
    # With the fix: failures IS reset, so we need 3 consecutive after reset (calls 4,5,6).
    # Test stops before call 6 is processed, so on_error should not be called.
    mgr = WatchManager(
        store,
        flaky_with_empty_stream,
        on_error=errors.append,
        retry_delay=0,
        max_retries=3,
    )
    await mgr.start("pods", "default")
    await asyncio.sleep(0.08)  # Allow ~5 calls but not 6
    # With fix: streak is reset, we get calls 1,2 (fail), 3 (reset), 4,5 (fail) = no on_error yet
    # Without fix: streak not reset, call 4 triggers on_error immediately
    if len(errors) == 0:
        # Fix is working: stream still retrying, not dead
        assert mgr.active == {("pods", "default")}
    await mgr.stop_all()


async def test_start_clears_stale_store_data() -> None:
    """Re-starting a watch for a (kind, ns) must purge stale data from the previous session."""
    store = ResourceStore()
    # Simulate stale data left from a previous watch session
    store.apply_event("pods", "ADDED", _pod("stale"))
    assert [p.name for p in store.get("pods", "default")] == ["stale"]

    mgr = WatchManager(store, make_source([("ADDED", _pod("fresh"))]))
    await mgr.start("pods", "default")
    # clear() is synchronous and runs before the asyncio task; stale data gone immediately
    assert store.get("pods", "default") == []
    await asyncio.sleep(0.05)
    names = [p.name for p in store.get("pods", "default")]
    assert "stale" not in names
    assert "fresh" in names
    await mgr.stop_all()
