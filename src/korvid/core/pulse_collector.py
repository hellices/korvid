"""Serial, deadline-bound collection for explicitly registered Pulse inputs."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from korvid.core.pulse import PulseCoverage, PulseCoverageState
from korvid.k8s.errors import ApiStatusError, KubeClientError
from korvid.k8s.pulse import PulseLimitError, PulseReader, PulseSource

#: The three inputs korvid's ambient attention model is fed from, as the
#: composition root registers them. Declared beside the collector that
#: consumes them rather than inline at the wiring: the set is a property of
#: this collector's contract (explicitly registered, namespaced LISTs), not
#: a per-session choice.
DEFAULT_PULSE_SOURCES: tuple[PulseSource, ...] = (
    PulseSource("pods", "", "v1", "pods"),
    PulseSource("deployments", "apps", "v1", "deployments"),
    PulseSource("events", "", "v1", "events", "type=Warning"),
)

PAGE_SIZE = 100
PAGE_BYTES = 256 * 1024
MAX_PAGES = 2
SOURCE_DEADLINE = 5.0


@dataclass(frozen=True, slots=True)
class PulseSourceResult:
    """Transient bounded objects and explicit coverage for one input."""

    source: str
    objects: tuple[dict[str, Any], ...]
    coverage: PulseCoverage


def _now() -> datetime:
    return datetime.now(UTC)


def _failure_state(failure: ApiStatusError | KubeClientError) -> tuple[PulseCoverageState, str]:
    if isinstance(failure, PulseLimitError):
        return "capped", "Response byte or object cap reached"
    if isinstance(failure, ApiStatusError):
        if failure.status in {401, 403}:
            return "forbidden", f"API {failure.status}: snapshot access denied"
        if failure.status in {404, 405}:
            return "unavailable", f"API {failure.status}: source unavailable"
        return "failed", f"API {failure.status}: snapshot request failed"
    return "failed", "Invalid snapshot payload or transport failure"


class PulseCollector:
    """Collect at most two bounded pages per source, one request at a time."""

    def __init__(
        self,
        reader: PulseReader,
        sources: Sequence[PulseSource],
        *,
        clock: Callable[[], datetime] = _now,
        timeout_seconds: float = SOURCE_DEADLINE,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= SOURCE_DEADLINE
        ):
            raise ValueError("Pulse source deadline must be positive and at most five seconds")
        self._reader = reader
        self._sources = tuple(sources)
        self._clock = clock
        self._timeout = timeout_seconds
        self._lock = asyncio.Lock()

    async def collect(self, namespace: str | None) -> tuple[PulseSourceResult, ...]:
        """Return coverage for every source; cancellation is never swallowed."""
        async with self._lock:
            return tuple(
                [await self._collect_source(source, namespace) for source in self._sources]
            )

    async def _collect_source(
        self, source: PulseSource, namespace: str | None
    ) -> PulseSourceResult:
        objects: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(self._timeout):
                state, detail = await self._pages(source, namespace, objects)
        except TimeoutError:
            state, detail = "failed", "Source collection deadline exceeded"
        except (ApiStatusError, KubeClientError) as failure:
            state, detail = _failure_state(failure)
        if objects and state in {"failed", "forbidden", "unavailable"}:
            state = "partial"
        return PulseSourceResult(
            source.key, tuple(objects), PulseCoverage(source.key, state, self._clock(), detail)
        )

    async def _pages(
        self, source: PulseSource, namespace: str | None, objects: list[dict[str, Any]]
    ) -> tuple[PulseCoverageState, str]:
        continuation = ""
        seen: set[str] = set()
        for _page_index in range(MAX_PAGES):
            page = await self._reader.read_pulse_page(
                source, namespace, continuation, PAGE_SIZE, PAGE_BYTES
            )
            objects.extend(page.items[:PAGE_SIZE])
            if len(page.items) > PAGE_SIZE:
                return "capped", "Response object cap reached"
            if not page.continuation:
                return "complete", "Bounded snapshot complete"
            if page.continuation in seen:
                return "partial", "Pagination repeated a continuation token"
            seen.add(page.continuation)
            continuation = page.continuation
        return "capped", "Two-page / 200-object source cap reached"
