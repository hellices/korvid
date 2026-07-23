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
