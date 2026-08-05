"""Real-app Textual replay harness for large-cluster qualification (issue #186).

Drives production KorvidApp, WatchManager, ResourceStore, and ResourceTable
through a recorded WorkloadProfile and captures replay metrics.  This module
is test-only instrumentation; it must not modify any production source file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import sys
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any, cast

import psutil  # type: ignore[import-untyped]  # no inline stubs shipped
from textual import __version__ as _textual_version

from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import PodSummary
from korvid.k8s.telemetry import ReadTelemetryEvent
from korvid.ui.app import KorvidApp, PaneState
from korvid.ui.widgets.resource_table import ResourceTable
from tests.performance.metrics import (
    ApiSummary,
    BenchmarkRecorder,
    LatencySummary,
    ProcessSampler,
    ProcessSummary,
    RunManifest,
)
from tests.performance.profile import FailureInjection, WorkloadProfile
from tests.performance.workload import (
    ScheduledEvent,
    apply_events,
    initial_pods,
    scheduled_events,
    summary_digest,
)
from tests.ui.waits import until


@dataclass(frozen=True)
class ReplayOptions:
    """Tuning knobs for a replay run.

    Args:
        time_scale: Multiplier applied to every scheduled-event sleep.
            0 skips all sleeps (fastest); 1.0 replays at real time.
        sample_interval: Seconds between process-memory samples.
    """

    time_scale: float = 1.0
    sample_interval: float = 1.0


@dataclass(frozen=True)
class ReplayReport:
    """Metrics collected by a complete real-app replay run.

    Extends the fields of `BenchmarkReport` with replay-specific metadata
    (`object_count`, `expected_digest`) so tests can assert both performance
    counters and digest correctness in one object.
    """

    object_count: int
    expected_digest: str
    final_digest: str
    dropped_updates: int
    rendered_updates: int
    render_passes: int
    coalesced_updates: int
    event_to_render: LatencySummary
    input_latency: LatencySummary
    churn_started_before_input: bool
    process: ProcessSummary
    api: ApiSummary
    manifest: RunManifest


class MeasuredKorvidApp(KorvidApp):
    """KorvidApp subclass that hooks `_render_table` to record render timing."""

    def __init__(self, *args: Any, recorder: BenchmarkRecorder, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._benchmark_recorder = recorder

    def _render_table(self, kind: str, *, only: PaneState | None = None) -> None:
        super()._render_table(kind, only=only)
        self._benchmark_recorder.record_render(monotonic())


# Maps FailureInjection.kind to the HTTP status code it raises.
_HARD_FAILURE_STATUS: dict[str, int] = {
    "gone": 410,
    "throttled": 429,
    "forbidden": 403,
}


class _ReplaySource:
    """Stateful async watch source that replays a `WorkloadProfile`.

    Each call (each watch connection) advances through the pre-computed event
    stream.  On reconnect the source re-LISTs from its tracked current state
    so the `WatchManager`'s store-clear + reseed cycle produces the correct
    snapshot.
    """

    def __init__(
        self,
        profile: WorkloadProfile,
        events: tuple[ScheduledEvent, ...],
        options: ReplayOptions,
        recorder: BenchmarkRecorder,
        churn_ready: asyncio.Event,
        churn_start: asyncio.Event,
        churn_done: asyncio.Event,
        failures: dict[int, FailureInjection],
    ) -> None:
        self._profile = profile
        self._events = events
        self._options = options
        self._recorder = recorder
        self._churn_ready = churn_ready
        self._churn_start = churn_start
        self._churn_done = churn_done
        self._failures = failures
        self._generation = 0
        self._next_event_index = 0
        self._replay_start: float = 0.0
        self._current: dict[str, PodSummary] = {
            f"{p.namespace}/{p.name}": p for p in initial_pods(profile)
        }

    def current_digest(self) -> str:
        """Digest of the source's tracked expected state."""
        return summary_digest(self._current.values())

    async def _handle_failure_if_any(self, event: ScheduledEvent, index: int) -> None:
        """Apply failure injection for *event*; raises `ApiStatusError` for hard faults.

        `slow` delays the tick without dropping the event.  Hard faults (gone,
        throttled, forbidden) record an error telemetry entry, advance the
        next-event cursor, and raise so `WatchManager` triggers a reconnect.
        """
        failure = self._failures.get(event.sequence)
        if failure is None:
            return
        if failure.kind == "slow":
            tick = 1.0 / max(self._profile.steady_events_per_second, 1)
            if tick * self._options.time_scale > 0:
                await asyncio.sleep(tick * self._options.time_scale)
            return
        status = _HARD_FAILURE_STATUS[failure.kind]
        self._recorder.record_api(ReadTelemetryEvent("error", "/api/v1/pods", status=status))
        self._next_event_index = index + 1
        raise ApiStatusError(status, failure.kind)

    async def __call__(self, kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        gen = self._generation
        self._generation += 1

        # --- LIST phase ---
        if gen == 0:
            list_pods: list[PodSummary] = list(initial_pods(self._profile))
        else:
            list_pods = sorted(self._current.values(), key=lambda p: (p.namespace, p.name))

        self._recorder.record_api(
            ReadTelemetryEvent(
                "list",
                "/api/v1/pods",
                object_count=len(list_pods),
                decoded_bytes=len(list_pods) * 200,
            )
        )
        for pod in list_pods:
            self._recorder.record_event(0, monotonic())
            yield ("ADDED", pod)

        self._recorder.record_api(ReadTelemetryEvent("watch_open", "/api/v1/pods"))

        if gen == 0:
            # Pause here until run_replay confirms the table is populated.
            self._churn_ready.set()
            await self._churn_start.wait()
            # Record the wall-clock instant when churn begins so that
            # event.offset_seconds (absolute positions within the profile)
            # can be converted to correct inter-event delays below.
            self._replay_start = monotonic()

        # --- WATCH phase ---
        for i in range(self._next_event_index, len(self._events)):
            event = self._events[i]
            elapsed = monotonic() - self._replay_start
            delay = event.offset_seconds * self._options.time_scale - elapsed
            if delay > 0:
                await asyncio.sleep(delay)

            await self._handle_failure_if_any(event, i)

            key = f"{event.summary.namespace}/{event.summary.name}"
            if event.event_type == "DELETED":
                self._current.pop(key, None)
            else:
                self._current[key] = event.summary

            self._recorder.record_event(event.sequence, monotonic())
            yield (event.event_type, event.summary)
            self._next_event_index = i + 1

        self._churn_done.set()
        # Stay open like a real watch stream so WatchManager does not reconnect.
        while True:
            await asyncio.sleep(3600.0)


def _build_manifest(profile: WorkloadProfile) -> RunManifest:
    profile_hash = hashlib.sha256(
        json.dumps(asdict(profile), sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return RunManifest(
        profile_id=profile.id,
        profile_hash=profile_hash,
        korvid_sha="dev",
        python=sys.version,
        textual=_textual_version,
        os=platform.platform(),
        cpu_count=os.cpu_count() or 1,
        memory_bytes=psutil.virtual_memory().total,
    )


async def run_replay(profile: WorkloadProfile, options: ReplayOptions) -> ReplayReport:
    """Run the full production Textual app against *profile* and return metrics.

    The function:
    1. Creates `ResourceStore`, `BenchmarkRecorder`, and one `_ReplaySource`.
    2. Emits initial Pods as ``ADDED`` (one logical LIST + WATCH OPEN).
    3. Waits on `churn_ready` after initial population.
    4. Waits until the `ResourceTable` shows all objects.
    5. Records key-press input latency.
    6. Signals the source to start scheduled-event churn.
    7. Awaits churn completion and the final render pass.
    8. Stops the watch manager and process sampler in `finally`.
    9. Compares the table/store digest to the source's expected state.
    """
    events = scheduled_events(profile)
    failures: dict[int, FailureInjection] = {f.at_event: f for f in profile.failures}

    store = ResourceStore()
    recorder = BenchmarkRecorder()
    sampler = ProcessSampler(options.sample_interval)

    churn_ready = asyncio.Event()
    churn_start = asyncio.Event()
    churn_done = asyncio.Event()

    source = _ReplaySource(
        profile,
        events,
        options,
        recorder,
        churn_ready,
        churn_start,
        churn_done,
        failures,
    )
    watch_manager = WatchManager(store, source, retry_delay=0.0)
    manifest = _build_manifest(profile)

    app = MeasuredKorvidApp(
        config=KorvidConfig(namespace=ALL_NAMESPACES),
        store=store,
        watch_manager=watch_manager,
        recorder=recorder,
    )

    sampler.start()
    churn_started_before_input = False
    try:
        async with app.run_test() as pilot:
            table = app.query_one(ResourceTable)

            # Wait for the initial LIST to populate the table.
            await until(
                pilot,
                lambda: table.row_count == profile.object_count,
                timeout=30.0,
                label="initial pods rendered",
            )

            # Release the source to emit scheduled events, then drive cursor
            # input while churn is active (not before the source is unblocked).
            churn_start.set()

            churn_started_before_input = churn_start.is_set()
            t0 = monotonic()
            await pilot.press("down")
            recorder.record_input(monotonic() - t0)
            t0 = monotonic()
            await pilot.press("up")
            recorder.record_input(monotonic() - t0)

            # Wait for all events to be emitted and all renders to complete.
            await until(
                pilot,
                lambda: churn_done.is_set() and not recorder._pending_events,
                timeout=30.0,
                label="churn complete and all events rendered",
            )
    finally:
        process_samples = await sampler.stop()
        await watch_manager.stop_all()

    # Compute expected digest using the independent apply_events oracle.
    # Hard failures (gone, throttled, forbidden) raise before yielding the
    # event, so the corresponding watch events are never applied; filter them
    # from the oracle to match the actual replay outcome.
    hard_failure_seqs: frozenset[int] = frozenset(
        f.at_event for f in profile.failures if f.kind in _HARD_FAILURE_STATUS
    )
    oracle_events = tuple(e for e in events if e.sequence not in hard_failure_seqs)
    expected_digest = summary_digest(apply_events(initial_pods(profile), oracle_events))

    # Compute final digest from the store (actual state).
    final_digest = summary_digest(cast(Iterable[PodSummary], store.get("pods", ALL_NAMESPACES)))

    benchmark = recorder.report(manifest, process_samples, final_digest=final_digest)

    return ReplayReport(
        object_count=profile.object_count,
        expected_digest=expected_digest,
        final_digest=final_digest,
        dropped_updates=benchmark.dropped_updates,
        rendered_updates=benchmark.rendered_updates,
        render_passes=benchmark.render_passes,
        coalesced_updates=benchmark.coalesced_updates,
        event_to_render=benchmark.event_to_render,
        input_latency=benchmark.input_latency,
        churn_started_before_input=churn_started_before_input,
        process=benchmark.process,
        api=benchmark.api,
        manifest=benchmark.manifest,
    )
