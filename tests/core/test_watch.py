import asyncio
from collections.abc import AsyncIterator, Callable

from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import PodSummary


def _pod(name: str) -> PodSummary:
    return PodSummary(
        name=name, namespace="default", phase="Running", ready="1/1", restarts=0, node=None
    )


def make_source(
    events: list[tuple[str, PodSummary]], forever: bool = True
) -> Callable[[str, str], AsyncIterator[tuple[str, Summary]]]:
    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
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

    async def flaky(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
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

    async def broken(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        raise RuntimeError("boom")
        yield ("", _pod(""))  # pragma: no cover - makes this an async generator

    mgr = WatchManager(store, broken, on_error=errors.append, retry_delay=0, max_retries=2)
    await mgr.start("pods", "default")
    await asyncio.sleep(0.05)
    assert mgr.active == set()  # dead task removed, not lying around as "active"
    assert errors
    assert "boom" in errors[0]


async def test_normal_stream_end_resets_failure_streak() -> None:
    """Empty-stream normal end resets the failure counter (focus: no-event connection counts).

    Sequence with max_retries=3:
      calls 1-2: fail  → failures=2
      call  3:   normal empty end (0 events) → failures resets to 0
      calls 4-5: fail  → failures=2
      call  6:   blocks forever (signals `done`)
    With fix:    failures never reach 3, on_error never fires.
    Without fix: call 4 would be the 3rd consecutive failure → on_error fires, task dies.
    """
    store = ResourceStore()
    errors: list[str] = []
    calls = 0
    done = asyncio.Event()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError(f"failure {calls}")
            yield ("", _pod(""))  # pragma: no cover - typing aid
        elif calls == 3:
            return  # normal empty end — must reset streak
            yield ("", _pod(""))  # pragma: no cover - typing aid
        elif calls <= 5:
            raise RuntimeError(f"failure {calls}")
            yield ("", _pod(""))  # pragma: no cover - typing aid
        else:
            done.set()
            await asyncio.Event().wait()  # block; keeps task alive for assertion
            yield ("", _pod(""))  # pragma: no cover - typing aid

    mgr = WatchManager(store, source, on_error=errors.append, retry_delay=0, max_retries=3)
    await mgr.start("pods", "default")
    await asyncio.wait_for(done.wait(), timeout=2.0)
    assert errors == []
    assert mgr.active == {("pods", "default")}
    await mgr.stop_all()


async def test_event_resets_failure_streak() -> None:
    """Receiving an event resets failures so a later exception starts a fresh streak.

    Sequence with max_retries=3:
      call 1: raises  → failures=1
      call 2: yields 1 event → failures resets to 0; then raises → failures=1
      call 3: raises  → failures=2
      call 4: blocks (signals `done`)
    With fix:    failures=2 < 3, on_error never fires.
    Without fix: failures=3 at call 3 → on_error fires, `done` never set (timeout).
    """
    store = ResourceStore()
    errors: list[str] = []
    calls = 0
    done = asyncio.Event()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first failure")
            yield  # pragma: no cover
        elif calls == 2:
            yield ("ADDED", _pod("p"))  # event received — must reset failures
            raise RuntimeError("source closed after event")
        elif calls == 3:
            raise RuntimeError("second failure")
            yield ("", _pod(""))  # pragma: no cover
        else:
            done.set()
            await asyncio.Event().wait()
            yield ("", _pod(""))  # pragma: no cover

    mgr = WatchManager(store, source, on_error=errors.append, retry_delay=0, max_retries=3)
    await mgr.start("pods", "default")
    await asyncio.wait_for(done.wait(), timeout=2.0)
    assert errors == []
    assert mgr.active == {("pods", "default")}
    await mgr.stop_all()


async def test_api_status_error_uses_explain_message() -> None:
    """ApiStatusError(403) → on_error message is the human-readable explain_api_error text."""
    store = ResourceStore()
    errors: list[str] = []

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        raise ApiStatusError(403, "Forbidden")
        yield  # pragma: no cover

    mgr = WatchManager(store, source, on_error=errors.append, retry_delay=0, max_retries=1)
    await mgr.start("pods", "default")
    await asyncio.sleep(0.05)
    assert len(errors) == 1
    # explain_api_error(403, ...) → "No permission to access ... Check your RBAC role bindings."
    assert "permission" in errors[0].lower()
    assert "rbac" in errors[0].lower()


async def test_start_clears_stale_store_data() -> None:
    """Re-starting a watch for a (kind, scope) must purge stale data from the previous session."""
    store = ResourceStore()
    # Simulate stale data left from a previous watch session
    store.apply_event("pods", "default", "ADDED", _pod("stale"))
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


async def test_reconnect_relist_drops_stale_pods() -> None:
    """A pod deleted while the watch is down must vanish after the re-LIST.

    connection 1: LIST yields a+b, then the stream breaks (mid-stream failure).
    connection 2: re-LIST yields only a (b was deleted during the outage) and
                  stays open. Without reconcile-on-reconnect, b remains in the
                  store forever — it never gets a DELETED event.
    """
    store = ResourceStore()
    calls = 0
    reconnected = asyncio.Event()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            yield ("ADDED", _pod("a"))
            yield ("ADDED", _pod("b"))
            raise ApiStatusError(500, "connection reset")
        yield ("ADDED", _pod("a"))
        reconnected.set()
        while True:
            await asyncio.sleep(0.01)

    mgr = WatchManager(store, source, retry_delay=0)
    await mgr.start("pods", "default")
    await asyncio.wait_for(reconnected.wait(), timeout=2.0)
    await asyncio.sleep(0.02)
    assert [p.name for p in store.get("pods", "default")] == ["a"]  # b must be gone
    await mgr.stop_all()


# ---------------------------------------------------------------------------
# New: source invoked with (kind, scope); independent watches per (kind, scope)
# ---------------------------------------------------------------------------


async def test_source_invoked_with_kind_and_scope() -> None:
    """WatchManager passes (kind, scope) to the source, not just scope."""
    store = ResourceStore()
    received: list[tuple[str, str]] = []

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        received.append((kind, scope))
        yield ("ADDED", _pod("a"))

    mgr = WatchManager(store, source, retry_delay=0.01)
    await mgr.start("pods", "default")
    await asyncio.sleep(0.05)
    assert received[0] == ("pods", "default")
    await mgr.stop_all()


async def test_same_kind_different_scope_independent_watches() -> None:
    """(pods, default) and (pods, *) are independent watches, both tracked in active."""
    store = ResourceStore()
    mgr = WatchManager(store, make_source([]))
    await mgr.start("pods", "default")
    await mgr.start("pods", "*")
    assert len(mgr.active) == 2
    assert ("pods", "default") in mgr.active
    assert ("pods", "*") in mgr.active
    await mgr.stop_all()


# ---------------------------------------------------------------------------
# 403 is an authorization boundary (issue #108): reported once, no retry
# loops, no per-namespace fanout — the API server owns authorization.
# ---------------------------------------------------------------------------


def _ns_pod(name: str, ns: str) -> PodSummary:
    return PodSummary(name=name, namespace=ns, phase="Running", ready="1/1", restarts=0, node=None)


async def test_cluster_scope_403_reports_once_without_retries() -> None:
    """A Forbidden cluster-scope watch is deterministic: one attempt, one
    report, no retry loop and no per-namespace watch tasks."""
    store = ResourceStore()
    errors: list[str] = []
    scopes_seen: list[str] = []

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        scopes_seen.append(scope)
        raise ApiStatusError(403, "Forbidden")
        yield ("", _ns_pod("", ""))  # pragma: no cover - async generator typing aid

    mgr = WatchManager(store, source, on_error=errors.append, retry_delay=0, max_retries=5)
    await mgr.start("pods", ALL_NAMESPACES)
    await asyncio.sleep(0.05)
    assert scopes_seen == [ALL_NAMESPACES]
    assert len(errors) == 1
    assert mgr.active == set()


async def test_namespaced_scope_403_reports_once_without_retries() -> None:
    """A forbidden single-namespace watch gets the same one-shot report."""
    store = ResourceStore()
    errors: list[str] = []
    scopes_seen: list[str] = []

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        scopes_seen.append(scope)
        raise ApiStatusError(403, "Forbidden")
        yield ("", _ns_pod("", ""))  # pragma: no cover - async generator typing aid

    mgr = WatchManager(store, source, on_error=errors.append, retry_delay=0, max_retries=5)
    await mgr.start("pods", "secret-ns")
    await asyncio.sleep(0.05)
    assert scopes_seen == ["secret-ns"]
    assert len(errors) == 1
    assert mgr.active == set()


async def test_403_purges_rows_seeded_by_the_forbidden_list() -> None:
    """The source LISTs before it WATCHes: rows can land in the bucket before
    the 403 — they must not stay visible after the denial."""
    store = ResourceStore()
    errors: list[str] = []

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        yield ("ADDED", _ns_pod("stale", "other-ns"))
        raise ApiStatusError(403, "Forbidden")

    mgr = WatchManager(store, source, on_error=errors.append, retry_delay=0)
    await mgr.start("pods", ALL_NAMESPACES)
    await asyncio.sleep(0.05)
    assert store.get("pods", ALL_NAMESPACES) == []
    assert len(errors) == 1


async def test_non_403_errors_keep_the_retry_path() -> None:
    """Network flakes / server errors are transient: retry up to max_retries
    before reporting."""
    store = ResourceStore()
    errors: list[str] = []
    attempts: list[str] = []

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        attempts.append(scope)
        raise ApiStatusError(500, "Internal Server Error")
        yield ("", _ns_pod("", ""))  # pragma: no cover - async generator typing aid

    mgr = WatchManager(store, source, on_error=errors.append, retry_delay=0, max_retries=3)
    await mgr.start("pods", ALL_NAMESPACES)
    await asyncio.sleep(0.05)
    assert len(attempts) == 3
    assert len(errors) == 1


async def test_restarted_watch_after_403_observes_a_new_grant() -> None:
    """A later RBAC grant must be picked up by the next started watch — the
    manager must not stay pinned to the denial."""
    store = ResourceStore()
    errors: list[str] = []
    granted = False

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if not granted:
            raise ApiStatusError(403, "Forbidden")
        yield ("ADDED", _ns_pod("p1", "team-a"))
        while True:
            await asyncio.sleep(0.01)

    mgr = WatchManager(store, source, on_error=errors.append, retry_delay=0)
    await mgr.start("pods", ALL_NAMESPACES)
    await asyncio.sleep(0.05)
    assert mgr.active == set()
    granted = True
    await mgr.start("pods", ALL_NAMESPACES)
    await asyncio.sleep(0.05)
    assert [p.name for p in store.get("pods", ALL_NAMESPACES)] == ["p1"]
    await mgr.stop_all()
