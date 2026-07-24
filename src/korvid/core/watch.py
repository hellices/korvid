"""Selective watch: one task per (kind, namespace) actually on screen (§5-6).

Streams that end normally (k8s API servers close watches periodically)
reconnect forever. Streams that raise retry up to max_retries consecutive
failures, then the task is removed from `active` and on_error is notified —
watch tasks never die silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable

from korvid.core.errors import explain_api_error
from korvid.core.store import ResourceStore
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import PodSummary

logger = logging.getLogger(__name__)

WatchSource = Callable[[str], AsyncIterator[tuple[str, PodSummary]]]


class WatchManager:
    def __init__(
        self,
        store: ResourceStore,
        watch_source: WatchSource,
        *,
        on_error: Callable[[str], None] | None = None,
        retry_delay: float = 1.0,
        max_retries: int = 5,
    ) -> None:
        self._store = store
        self._source = watch_source
        self.on_error = on_error  # public: the UI wires this after construction
        self._retry_delay = retry_delay
        self._max_retries = max_retries
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    @property
    def active(self) -> set[tuple[str, str]]:
        return set(self._tasks)

    async def start(self, kind: str, namespace: str) -> None:
        key = (kind, namespace)
        if key in self._tasks:
            return
        self._store.clear(kind, namespace)
        self._tasks[key] = asyncio.create_task(self._run(kind, namespace))

    async def stop(self, kind: str, namespace: str) -> None:
        task = self._tasks.pop((kind, namespace), None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def stop_all(self) -> None:
        for kind, namespace in list(self._tasks):
            await self.stop(kind, namespace)

    async def _run(self, kind: str, namespace: str) -> None:
        failures = 0
        while True:
            try:
                async for event_type, obj in self._source(namespace):
                    # A connection that delivers events is healthy — reset the
                    # failure streak so hours-long streams don't inherit old failures.
                    failures = 0
                    self._store.apply_event(kind, event_type, obj)
                # Stream ended normally (server-side watch timeout) -> reconnect.
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # report + retry, never die silently
                failures += 1
                logger.exception(
                    "watch %s/%s attempt %d/%d failed",
                    kind,
                    namespace,
                    failures,
                    self._max_retries,
                )
                if failures >= self._max_retries:
                    if self.on_error is not None:
                        if isinstance(exc, ApiStatusError):
                            msg = explain_api_error(exc.status, exc.reason, kind, namespace)
                        else:
                            msg = f"watch {kind}/{namespace} failed: {exc}"
                        self.on_error(msg)
                    break
            await asyncio.sleep(self._retry_delay)
        self._tasks.pop((kind, namespace), None)
