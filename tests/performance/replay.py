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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
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
from korvid.ui.app import KorvidApp
from korvid.ui.messages import ResourcesUpdated
from korvid.ui.widgets.resource_table import ResourceTable
from tests.performance.metrics import (
    ApiSummary,
    BenchmarkRecorder,
    ChurnSummary,
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
from tests.ui.waits import WaitTimeout, until


async def _sleep_default(delay: float) -> None:
    """Thin wrapper around asyncio.sleep used as the default async sleeper."""
    await asyncio.sleep(delay)


class ReplayAborted(Exception):
    """A replay ended before its schedule completed and can never complete.

    Raised for failure kinds the production `WatchManager` deliberately does
    not retry (403 Forbidden is an authorization boundary, not a transient
    fault): the stream is gone, the store has been cleared, and no further
    event or render can arrive. Surfacing that immediately is strictly better
    than letting the caller wait out a wall-clock timeout on a backlog that
    will never drain.
    """


@dataclass(frozen=True)
class ReplayOptions:
    """Tuning knobs for a replay run.

    Args:
        time_scale: Multiplier applied to every scheduled-event sleep.
            0 skips all sleeps (fastest); 1.0 replays at real time.
        sample_interval: Seconds between process-memory samples.
        monotonic_fn: Monotonic clock callable injected for testing.
            Production runs leave this `None` (uses `time.monotonic`).
        async_sleep: Async sleep callable injected for testing.
            Production runs leave this `None` (uses `_sleep_default`).
            A virtual sleeper can advance a shared clock variable and do
            `asyncio.sleep(0)` to yield without real wall time, making
            timing-sensitive tests instant and mutation-deterministic.
    """

    time_scale: float = 1.0
    sample_interval: float = 1.0
    monotonic_fn: Callable[[], float] | None = field(default=None, hash=False, compare=False)
    async_sleep: Callable[[float], Awaitable[None]] | None = field(
        default=None, hash=False, compare=False
    )


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
    #: Whether at least one churn event had actually been emitted (replay) or
    #: dispatched (live) when input latency was first measured.
    churn_started_before_input: bool
    process: ProcessSummary
    api: ApiSummary
    manifest: RunManifest
    #: Requested-versus-achieved churn accounting; `None` for deterministic
    #: replay, which drives its own source rather than a real API server.
    churn: ChurnSummary | None = None


class MeasuredKorvidApp(KorvidApp):
    """KorvidApp subclass that records the timing of *resource-update* renders.

    The hook is deliberately `on_resources_updated` rather than
    `_render_table`: the latter is also the choke point for cursor, filter,
    sort, namespace-switch, and split-pane repaints (~10 call sites in
    `ui/app.py`). Counting those would inflate `render_passes` and - worse -
    let an unrelated repaint flush the pending-event backlog, attributing a
    watch event's latency to a keypress that happened to repaint first.
    """

    def __init__(self, *args: Any, recorder: BenchmarkRecorder, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._benchmark_recorder = recorder

    def on_resources_updated(self, message: ResourcesUpdated) -> None:
        super().on_resources_updated(message)
        self._benchmark_recorder.record_render(monotonic())


# Maps FailureInjection.kind to the HTTP status code it raises.
_HARD_FAILURE_STATUS: dict[str, int] = {
    "gone": 410,
    "throttled": 429,
    "forbidden": 403,
}

#: Statuses `WatchManager._watch_loop` refuses to retry: the stream ends for
#: good and the store is cleared, so the replay is over the moment one is
#: injected (see `ReplayAborted`).
_TERMINAL_FAILURE_STATUS: frozenset[int] = frozenset({403})


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
        #: Number of scheduled churn events actually yielded to the watch
        #: manager so far; the real signal behind
        #: `ReplayReport.churn_started_before_input`.
        self.emitted_events = 0
        #: Set to the injected failure whose status the watch manager will not
        #: retry, so `run_replay` can abort with a named cause.
        self.terminal_failure: FailureInjection | None = None
        self._replay_start: float = 0.0
        self._current: dict[str, PodSummary] = {
            f"{p.namespace}/{p.name}": p for p in initial_pods(profile)
        }
        # Virtual-time seam: injected in tests; production uses real clock/sleep.
        self._now: Callable[[], float] = (
            options.monotonic_fn if options.monotonic_fn is not None else monotonic
        )
        self._sleep: Callable[[float], Awaitable[None]] = (
            options.async_sleep if options.async_sleep is not None else _sleep_default
        )

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
                await self._sleep(tick * self._options.time_scale)
            return
        status = _HARD_FAILURE_STATUS[failure.kind]
        self._recorder.record_api(ReadTelemetryEvent("error", "/api/v1/pods", status=status))
        self._next_event_index = index + 1
        if status in _TERMINAL_FAILURE_STATUS:
            # The watch manager will not reconnect after this: record the cause
            # now so `run_replay` aborts with it instead of waiting out a
            # timeout on a backlog that can never drain.
            self.terminal_failure = failure
            self._churn_done.set()
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
            # Record the virtual-clock instant when churn begins so that
            # event.offset_seconds (absolute positions within the profile)
            # can be converted to correct inter-event delays below.
            self._replay_start = self._now()

        # --- WATCH phase ---
        for i in range(self._next_event_index, len(self._events)):
            event = self._events[i]
            elapsed = self._now() - self._replay_start
            delay = event.offset_seconds * self._options.time_scale - elapsed
            if delay > 0:
                await self._sleep(delay)

            await self._handle_failure_if_any(event, i)

            key = f"{event.summary.namespace}/{event.summary.name}"
            if event.event_type == "DELETED":
                self._current.pop(key, None)
            else:
                self._current[key] = event.summary

            self._recorder.record_event(event.sequence, monotonic())
            yield (event.event_type, event.summary)
            self.emitted_events += 1
            self._next_event_index = i + 1

        self._churn_done.set()
        # Stay open like a real watch stream so WatchManager does not reconnect.
        while True:
            await asyncio.sleep(3600.0)


def build_manifest(profile: WorkloadProfile) -> RunManifest:
    """Resolved run manifest for *profile* (profile hash plus environment).

    Public because both replay harnesses (`replay.py` and `live.py`) build the
    identical manifest for their reports.
    """
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


async def wait_for(
    pilot: Any,
    condition: Callable[[], object],
    *,
    timeout: float,
    label: str,
    recorder: BenchmarkRecorder,
) -> None:
    """`until`, but a timeout names the API errors that likely caused it.

    `KorvidApp.on_mount` replaces `WatchManager.on_error` with its own TUI
    notification, so a 403/410/429 that killed the watch is otherwise invisible
    to the harness: the operator would see only "not met within 60.0s" after
    paying for a full cluster setup (or a full replay). The read telemetry has
    already recorded those statuses, so they are appended to the message.

    Shared by both harnesses so a deterministic replay and a live run explain a
    stalled wait the same way.
    """
    try:
        await until(pilot, condition, timeout=timeout, label=label)
    except WaitTimeout as exc:
        errors = recorder.api_errors()
        if not errors:
            raise
        detail = ", ".join(
            f"{event.operation} {event.path} status={event.status}" for event in errors
        )
        raise WaitTimeout(f"{exc}; application read path reported API errors: {detail}") from exc


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
    manifest = build_manifest(profile)

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
            await wait_for(
                pilot,
                lambda: table.row_count == profile.object_count,
                timeout=30.0,
                label="initial pods rendered",
                recorder=recorder,
            )

            # Release the source to emit scheduled events, then drive cursor
            # input while churn is active (not before the source is unblocked).
            churn_start.set()

            # Real ordering signal: wait for the source to actually put at
            # least one churn event on the wire before measuring input latency,
            # so the reported flag reflects observed emission rather than the
            # fact that `churn_start.set()` was called on the previous line.
            if events:
                await wait_for(
                    pilot,
                    lambda: source.emitted_events > 0 or source.terminal_failure is not None,
                    timeout=30.0,
                    label="first churn event emitted",
                    recorder=recorder,
                )
            churn_started_before_input = source.emitted_events > 0
            t0 = monotonic()
            await pilot.press("down")
            recorder.record_input(monotonic() - t0)
            t0 = monotonic()
            await pilot.press("up")
            recorder.record_input(monotonic() - t0)

            # Wait for all events to be emitted and all renders to complete.
            await wait_for(
                pilot,
                lambda: (
                    source.terminal_failure is not None
                    or (churn_done.is_set() and recorder.pending_count() == 0)
                ),
                timeout=30.0,
                label="churn complete and all events rendered",
                recorder=recorder,
            )
    finally:
        process_samples = await sampler.stop()
        await watch_manager.stop_all()

    if source.terminal_failure is not None:
        failure = source.terminal_failure
        status = _HARD_FAILURE_STATUS[failure.kind]
        raise ReplayAborted(
            f"replay aborted: injected {failure.kind!r} failure at event "
            f"{failure.at_event} returned HTTP {status}, which the watch "
            f"manager never retries; the stream ended and the store was cleared"
        )

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
