"""Selective watch: one task per (kind, scope) actually on screen (§5-6).

Streams that end normally (k8s API servers close watches periodically)
reconnect forever. Streams that raise retry up to max_retries consecutive
failures, then the task is removed from `active` and on_error is notified —
watch tasks never die silently.

RBAC-limited fallback (issue #49): when the cluster-scope watch answers 403
and fallback namespaces are configured, the task fans out into one watch per
namespace, all feeding the shared ALL_NAMESPACES bucket — instead of an error
dead-end for users without cluster-wide list permissions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Sequence

from korvid.core.errors import explain_api_error
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.k8s.errors import ApiStatusError

logger = logging.getLogger(__name__)

WatchSource = Callable[[str, str], AsyncIterator[tuple[str, Summary]]]


class WatchManager:
    def __init__(
        self,
        store: ResourceStore,
        watch_source: WatchSource,
        *,
        on_error: Callable[[str], None] | None = None,
        retry_delay: float = 1.0,
        max_retries: int = 5,
        fallback_namespaces: Sequence[str] = (),
        is_namespaced: Callable[[str], bool] | None = None,
    ) -> None:
        self._store = store
        self._source = watch_source
        self.on_error = on_error  # public: the UI wires this after construction
        #: Public like on_error: informational messages (e.g. fanout engaged).
        self.on_notice: Callable[[str], None] | None = None
        self._retry_delay = retry_delay
        self._max_retries = max_retries
        self._fallback_namespaces = tuple(fallback_namespaces)
        #: Kind -> namespaced? None assumes namespaced. Cluster-scoped kinds
        #: must not fan out: the source ignores the namespace for them, so a
        #: fanout would just repeat the same forbidden request per namespace.
        self._is_namespaced = is_namespaced
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    @property
    def active(self) -> set[tuple[str, str]]:
        return set(self._tasks)

    async def start(self, kind: str, scope: str) -> None:
        key = (kind, scope)
        if key in self._tasks:
            return
        self._store.clear(kind, scope)
        self._tasks[key] = asyncio.create_task(self._run(kind, scope))

    async def stop(self, kind: str, scope: str) -> None:
        task = self._tasks.pop((kind, scope), None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def stop_all(self) -> None:
        for kind, scope in list(self._tasks):
            await self.stop(kind, scope)

    async def _run(self, kind: str, scope: str) -> None:
        bail_out = self._is_rbac_fanout_case(kind, scope)
        exc = await self._watch_loop(kind, scope, scope, bail_out=bail_out)
        if exc is not None:
            if bail_out(exc):
                # The source LISTs before it WATCHes: rows may have landed in
                # the bucket before the 403 — purge, or rows from namespaces
                # outside the fallback set would stay visible forever.
                self._store.clear(kind, scope)
                await self._fan_out(kind)
            else:
                self._report(kind, scope, exc)
        self._tasks.pop((kind, scope), None)

    def _is_rbac_fanout_case(self, kind: str, scope: str) -> Callable[[Exception], bool]:
        """Fanout applies only when a Forbidden answer proves an RBAC boundary
        on the cluster scope of a namespaced kind and there are namespaces to
        fall back to. Network flakes / server errors keep the normal retry
        path, and cluster-scoped kinds keep the single error path."""

        def check(exc: Exception) -> bool:
            return (
                scope == ALL_NAMESPACES
                and bool(self._fallback_namespaces)
                and (self._is_namespaced is None or self._is_namespaced(kind))
                and isinstance(exc, ApiStatusError)
                and exc.status == 403
            )

        return check

    async def _fan_out(self, kind: str) -> None:
        """Run one watch per fallback namespace, all feeding the ALL bucket.

        Each namespace loop swallows its own permanent failure (reported via
        on_error) so one forbidden namespace never kills the siblings; only
        cancellation propagates, tearing all of them down together.
        """
        names = ", ".join(self._fallback_namespaces)
        logger.info("cluster-wide %s watch forbidden; per-namespace fallback: %s", kind, names)
        if self.on_notice is not None:
            self.on_notice(f"Cluster-wide {kind} watch forbidden — watching namespaces: {names}")
        await asyncio.gather(*(self._watch_namespace(kind, ns) for ns in self._fallback_namespaces))

    async def _watch_namespace(self, kind: str, namespace: str) -> None:
        exc = await self._watch_loop(kind, namespace, ALL_NAMESPACES)
        if exc is not None:
            self._report(kind, namespace, exc)

    async def _watch_loop(
        self,
        kind: str,
        watch_scope: str,
        store_scope: str,
        *,
        bail_out: Callable[[Exception], bool] | None = None,
    ) -> Exception | None:
        """Retry loop for one stream; returns the exception that ended it.

        Ends immediately when *bail_out* matches (a deterministic 403 gains
        nothing from retries), else after max_retries consecutive failures.
        Loops forever while the stream is healthy.
        """
        failures = 0
        first_connection = True
        while True:
            if not first_connection:
                # Reconnect = fresh re-LIST by the source. Purge the store first:
                # pods deleted during the outage never get a DELETED event and
                # would otherwise linger forever. The re-LIST re-seeds immediately.
                self._purge(kind, watch_scope, store_scope)
            first_connection = False
            try:
                async for event_type, obj in self._source(kind, watch_scope):
                    # A connection that delivers events is healthy — reset the
                    # failure streak so hours-long streams don't inherit old failures.
                    failures = 0
                    self._store.apply_event(kind, store_scope, event_type, obj)
                # Stream ended normally (server-side watch timeout) -> reconnect.
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # report + retry, never die silently
                if bail_out is not None and bail_out(exc):
                    return exc
                failures += 1
                logger.exception(
                    "watch %s/%s attempt %d/%d failed",
                    kind,
                    watch_scope,
                    failures,
                    self._max_retries,
                )
                if failures >= self._max_retries:
                    return exc
            await asyncio.sleep(self._retry_delay)

    def _purge(self, kind: str, watch_scope: str, store_scope: str) -> None:
        """Fanout streams share the ALL bucket: purge only their namespace slice."""
        if watch_scope != store_scope:
            self._store.clear_namespace(kind, store_scope, watch_scope)
        else:
            self._store.clear(kind, store_scope)

    def _report(self, kind: str, scope: str, exc: Exception) -> None:
        if self.on_error is None:
            return
        ns_for_explain = None if scope == ALL_NAMESPACES else scope
        if isinstance(exc, ApiStatusError):
            msg = explain_api_error(exc.status, exc.reason, kind, ns_for_explain)
        else:
            msg = f"watch {kind}/{scope} failed: {exc}"
        self.on_error(msg)
