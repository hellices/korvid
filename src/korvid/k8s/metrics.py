"""Live pod usage from metrics.k8s.io (metrics-server).

The metrics API does not support watch, so a small poller re-fetches on an
interval. A cluster without metrics-server (404) or without RBAC for it
(403) degrades gracefully: the poller reports unavailable, clears stale
data, and keeps polling so a later install is picked up without a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from korvid.k8s.models import parse_cpu, parse_memory

logger = logging.getLogger(__name__)

#: metrics-server default scrape resolution is 15s; polling faster only
#: re-reads the same sample.
DEFAULT_INTERVAL = 15.0


@dataclass(frozen=True)
class ContainerUsage:
    """One container's usage sample - kept alongside the pod total because
    limits are enforced per container (PR #51 review)."""

    name: str
    cpu_cores: float
    memory_bytes: int


@dataclass(frozen=True)
class PodMetrics:
    """Whole-pod usage: the sum over container samples in one PodMetrics item."""

    name: str
    namespace: str
    cpu_cores: float
    memory_bytes: int
    #: Per-container breakdown; empty when the item carried no samples.
    containers: tuple[ContainerUsage, ...] = ()


def parse_pod_metrics_list(data: dict[str, Any]) -> list[PodMetrics]:
    """Parse a PodMetricsList document; malformed items are skipped, never fatal."""
    items = data.get("items")
    if not isinstance(items, list):
        return []
    metrics: list[PodMetrics] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        parsed = _parse_item(item)
        if parsed is not None:
            metrics.append(parsed)
    return metrics


def _parse_item(item: dict[str, Any]) -> PodMetrics | None:
    meta = item.get("metadata")
    if not isinstance(meta, dict):
        return None
    name = meta.get("name")
    namespace = meta.get("namespace")
    # The UI joins on (namespace, name): an item missing either can never
    # match a pod row, so it is dropped rather than stored unreachable.
    if not isinstance(name, str) or not name or not isinstance(namespace, str) or not namespace:
        return None
    cpu = 0.0
    memory = 0
    containers: list[ContainerUsage] = []
    try:
        for container in item.get("containers") or []:
            usage = container.get("usage") or {}
            c_cpu = parse_cpu(str(usage["cpu"])) if "cpu" in usage else 0.0
            c_mem = parse_memory(str(usage["memory"])) if "memory" in usage else 0
            cpu += c_cpu
            memory += c_mem
            containers.append(
                ContainerUsage(
                    name=str(container.get("name") or ""),
                    cpu_cores=c_cpu,
                    memory_bytes=c_mem,
                )
            )
    except (ValueError, TypeError, AttributeError):
        logger.debug("skipping malformed pod metrics item %r", name, exc_info=True)
        return None
    return PodMetrics(
        name=name,
        namespace=namespace,
        cpu_cores=cpu,
        memory_bytes=memory,
        containers=tuple(containers),
    )


#: Fetches the current PodMetrics for a namespace (None = all namespaces).
MetricsFetch = Callable[[str | None], Awaitable[list[PodMetrics]]]


class MetricsPoller:
    """Polls pod metrics for one scope and exposes a (namespace, name) lookup.

    ``start`` on a new scope restarts the loop and drops data from the old
    scope so a namespace switch never shows another namespace's numbers.
    """

    def __init__(
        self,
        fetch: MetricsFetch,
        *,
        interval: float = DEFAULT_INTERVAL,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        self._fetch = fetch
        self._interval = interval
        self.on_update = on_update  # public: the UI wires this after construction
        self._task: asyncio.Task[None] | None = None
        self._data: dict[tuple[str, str], PodMetrics] = {}
        self._available = False

    @property
    def available(self) -> bool:
        """False until a poll succeeds, and again after any failed poll."""
        return self._available

    def get(self, namespace: str, name: str) -> PodMetrics | None:
        return self._data.get((namespace, name))

    async def start(self, namespace: str | None) -> None:
        await self.stop()
        self._data = {}
        self._available = False
        self._task = asyncio.create_task(self._run(namespace))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self, namespace: str | None) -> None:
        while True:
            try:
                metrics = await self._fetch(namespace)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 404/403 (no metrics-server / no RBAC) and transient errors
                # alike: degrade, drop stale numbers, keep polling.
                logger.debug("pod metrics poll failed", exc_info=True)
                changed = self._available or bool(self._data)
                self._data = {}
                self._available = False
            else:
                new = {(m.namespace, m.name): m for m in metrics}
                changed = new != self._data or not self._available
                self._data = new
                self._available = True
            if changed:
                self._notify()
            await asyncio.sleep(self._interval)

    def _notify(self) -> None:
        if self.on_update is None:
            return
        try:
            self.on_update()
        except Exception:  # a subscriber bug must not kill the poll loop
            logger.exception("metrics poller subscriber failed")
