"""Real-app Textual replay harness for large-cluster qualification (issue #186).

Drives production KorvidApp, WatchManager, ResourceStore, and ResourceTable
through a recorded WorkloadProfile and captures replay metrics.  This module
is test-only instrumentation; it must not modify any production source file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import Any, cast

import psutil  # type: ignore[import-untyped]  # no inline stubs shipped
from textual import __version__ as _textual_version
from textual import events

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
    NodePoolInfo,
    PhaseSummary,
    ProcessSampler,
    ProcessSummary,
    RunManifest,
    ScenarioResult,
    UpdateLatencyKind,
)
from tests.performance.profile import (
    FailureInjection,
    WorkloadProfile,
    burst_end_offsets,
    planned_event_count,
)
from tests.performance.workload import (
    ScheduledEvent,
    apply_events,
    initial_pods,
    scheduled_events,
    summary_digest,
)
from tests.ui.waits import WaitTimeout, until

#: A resolved korvid revision must be an immutable 40-character git object name.
#: Anything else (e.g. the historical literal ``dev``) is not traceable to a
#: commit and is rejected by `resolve_korvid_sha`.
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
#: Drain allowance on top of the schedule's own scaled wall duration. The
#: completion wait opens right after the *first* churn event, so it has to
#: cover the rest of the schedule and the render drain that follows it. This
#: is the drain half, and it stays at the fixed wait's original value: on the
#: committed `burst-50k` profile the drain alone measures ~27.5 s, so a
#: smaller constant would fail healthy runs on the largest profiles rather
#: than catch a stalled one.
_REPLAY_CHURN_COMPLETION_GRACE_SECONDS = 30.0


def _git_head() -> str | None:
    """Resolve the current repository HEAD commit, or `None` if unavailable.

    Isolated behind a seam so `resolve_korvid_sha` stays deterministic under
    test: production reads the real git tree, tests inject a fixed value.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def resolve_korvid_sha(
    *,
    env: Mapping[str, str] | None = None,
    git_head: Callable[[], str | None] | None = None,
) -> str | None:
    """Resolve the immutable korvid commit under test from trusted sources.

    Prefers the CI-provided `GITHUB_SHA` (GitHub Actions sets it to the exact
    commit being built), then falls back to the local repository HEAD. Returns
    `None` when neither yields an immutable 40-hex object name, so callers can
    decide how to react: offline reports mark it `unknown` (still traceable -
    the report states the SHA could not be resolved), while a live evidence
    run fails closed rather than publish an untraceable artifact.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    candidate = environ.get("GITHUB_SHA", "").strip().lower()
    if _SHA_PATTERN.fullmatch(candidate):
        return candidate
    resolver = git_head if git_head is not None else _git_head
    head = (resolver() or "").strip().lower()
    if _SHA_PATTERN.fullmatch(head):
        return head
    return None


async def _sleep_default(delay: float) -> None:
    """Thin wrapper around asyncio.sleep used as the default async sleeper."""
    await asyncio.sleep(delay)


class ReplayConfigurationError(ValueError):
    """The run was asked for something the harness cannot measure.

    Subclasses `ValueError` so existing callers and tests that expect one keep
    working, but gives the CLI something narrower to catch: an unexpected
    `ValueError` escaping the application must still surface with its
    traceback rather than be reported as a configuration mistake.
    """


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
        input_ack_timeout: Seconds the cursor probe waits for the table to
            acknowledge an injected key before failing the run. Bounded so a
            saturated client aborts with a named timeout instead of hanging;
            raise it for a slow remote cluster, lower it to fail fast.
        input_sample_pairs: Number of `down`/`up` cursor round trips the input
            probe performs, so the reported percentile rests on
            `2 * input_sample_pairs` samples. Two samples make a point
            observation, not a percentile; the default of 25 pairs (50
            samples) is enough for a usable p95 while staying short next to
            the churn schedule it runs inside. Must be positive - each pair
            restores the original cursor row, so the surrounding row and
            digest checks are unaffected by the count.
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
    input_ack_timeout: float = 5.0
    input_sample_pairs: int = 25
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
    #: Watch-event receipt to resource-update-handler completion, qualified by
    #: `update_latency_kind`: a rendered-cell interval for the deterministic
    #: replay, a no-op-diff interval for the metadata-only live workload.
    update_latency: LatencySummary
    input_latency: LatencySummary
    #: Whether the application had really observed live churn when input
    #: latency was first measured: at least one churn event emitted (replay) or
    #: one owned `MODIFIED` event received at watch receipt (live). Dispatching
    #: a mutation is deliberately *not* enough - it is counted before the patch
    #: is awaited, and its watch event arrives over an independent connection.
    churn_started_before_input: bool
    process: ProcessSummary
    api: ApiSummary
    #: Explicit phase measurements (startup, LIST-to-populated table, backlog
    #: depth, post-burst drain) the numeric budgets are stated against.
    phases: PhaseSummary
    manifest: RunManifest
    #: What `update_latency` measured. Defaults to the rendered-cell meaning:
    #: the deterministic replay really does rewrite rendered cells, so every
    #: construction that does not say otherwise keeps its published semantics.
    update_latency_kind: UpdateLatencyKind = UpdateLatencyKind.EVENT_TO_RENDER
    #: Requested-versus-achieved churn accounting; `None` for deterministic
    #: replay, which drives its own source rather than a real API server.
    churn: ChurnSummary | None = None
    #: Per-kind count of injected failures actually exercised (report evidence
    #: that a failure profile ran, including `metrics_unavailable`/`slow_logs`).
    failures_injected: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    #: Scoped UI-at-scale scenarios driven during live churn (empty offline).
    ui_scenarios: tuple[ScenarioResult, ...] = ()


class MeasuredKorvidApp(KorvidApp):
    """KorvidApp subclass that records the timing of *resource-update* renders.

    The hook is deliberately `on_resources_updated` rather than
    `_render_table`: the latter is also the choke point for cursor, filter,
    sort, namespace-switch, and split-pane repaints (~10 call sites in
    `ui/app.py`). Counting those would inflate `render_passes` and - worse -
    let an unrelated repaint flush the pending-event backlog, attributing a
    watch event's latency to a keypress that happened to repaint first.

    What the recorded instant means: the table-update handler for that batch
    has *completed*. When the event changed a cell the table renders, that
    includes the in-place cell writes and the repaint request; when it did not
    - a metadata-only mutation such as the live driver's
    `korvid.dev/performance-tick` label, which no Pod column renders - the
    diff finds nothing to write and the sample times a no-op. Which of the two
    a run produced is declared by `ReplayReport.update_latency_kind`, so the
    artifacts publish the samples under the metric name that was actually
    measured instead of comparing a metadata-only figure with the
    rendered-frame budget.
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
        #: Index of the next burst whose end has not been marked yet. Persisted
        #: across watch generations (see the WATCH phase below).
        self._next_burst = 0
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

        `slow` delays the tick without dropping the event. `metrics_unavailable`
        and `slow_logs` record read telemetry on the metrics / log paths but
        never disturb the resource watch: their whole point is proving resource
        navigation and render progress are independent of the metrics poller and
        of log consumption, so the resource event is still delivered on time.
        Hard faults (gone, throttled, forbidden) record an error telemetry
        entry, advance the next-event cursor, and raise so `WatchManager`
        triggers a reconnect.
        """
        failure = self._failures.get(event.sequence)
        if failure is None:
            return
        self._recorder.record_failure(failure.kind)
        if failure.kind == "slow":
            tick = 1.0 / max(self._profile.steady_events_per_second, 1)
            if tick * self._options.time_scale > 0:
                await self._sleep(tick * self._options.time_scale)
            return
        if failure.kind == "metrics_unavailable":
            # The metrics poller fails (503) while the resource watch continues:
            # evidence lands on the metrics read path, not the pods path.
            self._recorder.record_api(
                ReadTelemetryEvent("error", "/apis/metrics.k8s.io/v1beta1/pods", status=503)
            )
            return
        if failure.kind == "slow_logs":
            # A slow log stream: evidence lands on the log read path, and the
            # resource event is delivered without any added delay.
            self._recorder.record_api(
                ReadTelemetryEvent("error", "/api/v1/namespaces/_/pods/_/log", status=504)
            )
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
        # LIST rows populate the initial table; they are NOT event-to-render
        # samples. Recording them here would let the initial LIST dominate the
        # replay p95 and make it incomparable with the live path, which only
        # records owned watch events. The LIST is timed separately below as the
        # LIST-to-populated-table startup phase.
        for pod in list_pods:
            yield ("ADDED", pod)

        # The first WATCH open marks the LIST-to-populated boundary (via the
        # recorder's telemetry hook), uniformly with the live path.
        self._recorder.record_api(ReadTelemetryEvent("watch_open", "/api/v1/pods"))

        if gen == 0:
            # Pause here until run_replay confirms the table is populated.
            self._churn_ready.set()
            await self._churn_start.wait()
            # Record the virtual-clock instant when churn begins so that
            # event.offset_seconds (absolute positions within the profile)
            # can be converted to correct inter-event delays below.
            self._replay_start = self._now()

        # Burst-end offsets (absolute seconds) mark the moment each burst's
        # window closes, so post-burst backlog drain can be timed on the same
        # real-clock axis the render pass records on. The cursor lives on the
        # source, not this generation: a 410/429 reconnect resumes later in the
        # schedule, and a per-generation cursor would re-mark every burst that
        # already ended, producing duplicate and time-shifted drain samples.
        burst_ends = burst_end_offsets(self._profile)

        # --- WATCH phase ---
        for i in range(self._next_event_index, len(self._events)):
            event = self._events[i]
            elapsed = self._now() - self._replay_start
            delay = event.offset_seconds * self._options.time_scale - elapsed
            if delay > 0:
                await self._sleep(delay)

            while (
                self._next_burst < len(burst_ends)
                and event.offset_seconds >= burst_ends[self._next_burst]
            ):
                self._recorder.mark_burst_end(monotonic())
                self._next_burst += 1

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


def build_manifest(
    profile: WorkloadProfile,
    *,
    korvid_sha: str | None = None,
    context: str | None = None,
    cluster_id: str | None = None,
    kubernetes_version: str | None = None,
    node_pools: tuple[NodePoolInfo, ...] = (),
) -> RunManifest:
    """Resolved run manifest for *profile* (profile hash plus environment).

    Public because both replay harnesses (`replay.py` and `live.py`) build the
    identical manifest for their reports.

    Args:
        korvid_sha: The immutable commit to record. When `None`, it is resolved
            from `resolve_korvid_sha`; an offline run that cannot resolve one
            records `unknown` (still traceable - the report states it is
            unresolved) rather than a fake literal. A live evidence run resolves
            and fails closed *before* calling this, so it never records
            `unknown`.
        context: Live kube context that was verified (live runs only).
        cluster_id: Verified AKS ARM resource id (live runs only).
        kubernetes_version: Kubernetes server version (live runs only).
        node_pools: Node-pool topology metadata (live runs only).
    """
    profile_hash = hashlib.sha256(
        json.dumps(asdict(profile), sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    resolved_sha = korvid_sha if korvid_sha is not None else (resolve_korvid_sha() or "unknown")
    return RunManifest(
        profile_id=profile.id,
        profile_hash=profile_hash,
        korvid_sha=resolved_sha,
        python=sys.version,
        textual=_textual_version,
        os=platform.platform(),
        cpu_count=os.cpu_count() or 1,
        memory_bytes=psutil.virtual_memory().total,
        context=context,
        cluster_id=cluster_id,
        kubernetes_version=kubernetes_version,
        node_pools=node_pools,
    )


def check_rendered_rows(table: Any, pods: Iterable[PodSummary]) -> None:
    """Verify the rendered table against the store, independently of the widget.

    The published digest criterion compares a store digest with a store digest:
    a table showing 1,000 stale rows satisfies it. This projects each owned Pod
    onto the strings its row must display and checks them against the cells the
    `DataTable` actually holds, so a cell the in-place diff skipped - it now
    diffs against its own record of what it last wrote - is caught rather than
    reported as a clean run.

    Deliberately column-order agnostic and written from the Pod summary rather
    than by calling the widget's row builder: reusing the builder would only
    prove the widget agrees with itself.

    Raises:
        ValueError: a Pod is missing from the table, or a rendered row does not
            carry every value the store says it must show.
    """
    rendered = {
        str(row.key.value): [str(cell) for cell in table.get_row(row.key)]
        for row in table.ordered_rows
    }
    for pod in pods:
        key = f"{pod.namespace}/{pod.name}"
        cells = rendered.get(key)
        if cells is None:
            raise ValueError(f"rendered table is missing owned pod {key}")
        expected = (pod.name, pod.ready, pod.phase, str(pod.restarts))
        missing = [value for value in expected if value not in cells]
        if missing:
            raise ValueError(
                f"rendered row for {key} is stale: expected cells {missing} not among {cells}"
            )


async def measure_cursor_input(
    pilot: Any,
    table: ResourceTable,
    key: str,
    *,
    now: Callable[[], float] = monotonic,
    timeout: float = 5.0,
) -> float:
    """Measure `down`/`up` key injection until the expected cursor row is reached.

    The wait is a tight poll rather than a watcher on `cursor_coordinate`,
    which was measured rather than assumed: under saturating churn (1,000
    objects at 240 events/s) the poll costs a median of 6 event-loop turns per
    sample, and an event-driven watcher measured *higher* p95 — 89.7 ms
    against 76.8 ms, consistently across three interleaved rounds. A reactive
    watcher is woken through Textual's own callback machinery and then has to
    be rescheduled itself, so it observes the change later than a poll that is
    already at the front of the ready queue. The poll is therefore the tighter
    upper bound on the acknowledgement, not a source of inflation.
    """
    start_row = table.cursor_row
    try:
        expected_row = start_row + {"down": 1, "up": -1}[key]
    except KeyError as exc:
        raise ValueError("cursor input measurement supports only 'down' and 'up'") from exc
    row_count = table.row_count
    if not 0 <= expected_row < row_count:
        raise ValueError(
            f"cursor input measurement key {key!r} from start row {start_row} "
            f"expected row {expected_row} outside valid range 0..{row_count - 1}"
        )
    app = pilot.app
    driver = app._driver
    if driver is None:
        raise RuntimeError("cursor input measurement requires an active Textual test driver")
    event = events.Key(key, None)
    event.set_sender(app)
    started = now()
    driver.send_message(event)
    try:
        async with asyncio.timeout(timeout):
            while table.cursor_row != expected_row:
                await asyncio.sleep(0)
    except TimeoutError as exc:
        raise WaitTimeout(
            f"{key} cursor input from row {start_row} to expected row {expected_row} "
            f"was not acknowledged within {timeout}s"
        ) from exc
    return now() - started


def validate_input_sample_pairs(options: ReplayOptions) -> None:
    """Reject a sample-pair count that cannot produce a usable percentile.

    Called at the top of both harness entry points - before any cluster
    identity, ownership, or mutation work - so a misconfigured run fails
    immediately rather than after paying for a full setup and then publishing
    an input percentile computed from zero samples.

    Raises:
        ReplayConfigurationError: `input_sample_pairs` is not a positive integer.
    """
    if options.input_sample_pairs < 1:
        raise ReplayConfigurationError(
            f"input_sample_pairs must be positive; got {options.input_sample_pairs}"
        )


def validate_input_ack_timeout(options: ReplayOptions) -> None:
    """Reject a cursor-probe bound that cannot bound anything.

    `sample_cursor_input` wraps each key press in `asyncio.timeout(...)`, and
    that timeout never fires for `inf` or `nan` - an unacknowledged key would
    hang the run forever, contradicting the documented bounded probe. A
    non-positive bound is the mirror image: it aborts before the key can
    possibly be acknowledged. The CLI rejects both on its own arguments; this
    is the programmatic guard, called at the top of both harness entry points
    - before app startup or any live cluster identity, ownership, or mutation
    work - so a misconfigured run fails immediately instead of hanging a
    seeded cluster mid-churn.

    Raises:
        ReplayConfigurationError: `input_ack_timeout` is not finite and positive.
    """
    if not math.isfinite(options.input_ack_timeout) or options.input_ack_timeout <= 0:
        raise ReplayConfigurationError(
            f"input_ack_timeout must be finite and positive; got {options.input_ack_timeout}"
        )


def validate_input_sampling_profile(profile: WorkloadProfile) -> None:
    """Reject benchmark profiles that cannot satisfy the cursor probe.

    The replay harness measures input latency as `down`/`up` cursor round trips
    during churn. With fewer than two rows the first `down` move is impossible,
    and with no scheduled churn events the probe records only idle cursor moves.
    Both are profile contract errors that must fail before app startup or any
    live-cluster external work. `load_profile` intentionally stays generic:
    other consumers can still use a schema-valid profile that this benchmark
    rejects.
    """
    if profile.object_count < 2:
        raise ReplayConfigurationError(
            f"performance input sampling requires object_count >= 2; got {profile.object_count}"
        )
    if planned_event_count(profile) < 1:
        raise ReplayConfigurationError(
            "performance input sampling requires at least one scheduled churn event"
        )


def input_sampling_incomplete_message(pairs: int) -> str:
    """Name the metric-contract failure when churn ends before sampling does."""
    return f"input sampling incomplete: churn finished before all {pairs} cursor sample pairs completed"


def replay_churn_completion_timeout(profile: WorkloadProfile, options: ReplayOptions) -> float:
    """Bound the final wait by the scaled schedule plus the drain allowance.

    The schedule term scales with `time_scale` because a slowed replay really
    does take longer to emit; the drain term does not, because draining is the
    app's own work at full speed no matter how the schedule was paced.
    """
    return profile.duration_seconds * options.time_scale + _REPLAY_CHURN_COMPLETION_GRACE_SECONDS


async def sample_cursor_input(
    pilot: Any,
    table: ResourceTable,
    recorder: BenchmarkRecorder,
    *,
    pairs: int,
    now: Callable[[], float] = monotonic,
    timeout: float = 5.0,
    aborted: Callable[[], bool] = lambda: False,
    incomplete: Callable[[], bool] = lambda: False,
) -> None:
    """Record *pairs* `down`/`up` cursor round trips into *recorder*.

    A pair, not a single key press, is the unit: `up` undoes `down`, so the
    cursor is on its original row both between pairs and when the loop ends.
    Every check that follows the probe - rendered-row freshness, row count,
    digest convergence - therefore sees the selection the run started with,
    whatever the configured count is.

    *aborted* and *incomplete* are re-asked before every single sample rather
    than once per pair. A churn task that dies mid-pair would otherwise leave
    nothing updating the table, and the remaining samples would each record
    their own timeout as input latency. A churn run that finishes cleanly
    before the configured sample count is satisfied would likewise dilute the
    reported percentile with idle cursor moves, so that case fails the run by
    name instead of publishing a partial metric. Stopping mid-pair can leave
    the cursor one row down, which is why the caller's failure path must not
    depend on the cursor position - by the time this returns early the run is
    already failing.

    Shared by the deterministic replay and the live harness so both publish
    the same measurement over the same sample size.
    """
    for _ in range(pairs):
        for key in ("down", "up"):
            if incomplete():
                raise WaitTimeout(input_sampling_incomplete_message(pairs))
            if aborted():
                return
            recorder.record_input(
                await measure_cursor_input(pilot, table, key, now=now, timeout=timeout)
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
    3. Waits until the `ResourceTable` shows all objects.
    4. Signals the source to start scheduled-event churn.
    5. Waits for the first churn event to be emitted.
    6. Records `options.input_sample_pairs` down/up cursor input samples.
    7. Awaits churn completion and the final render pass.
    8. Stops the watch manager and process sampler in `finally`.
    9. Compares the table/store digest to the source's expected state.

    Raises:
        ValueError: `options.input_sample_pairs` is not positive,
            `options.input_ack_timeout` is not finite and positive, or the
            profile cannot support cursor input sampling.
    """
    validate_input_sample_pairs(options)
    validate_input_ack_timeout(options)
    validate_input_sampling_profile(profile)
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

    # Process/app start reference for the process-start-to-interactive phase
    # and the steady-state RSS-slope warm-up boundary.
    recorder.mark_process_start(monotonic())
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
            # The table is fully populated: mark the interactive boundary that
            # closes both the startup and LIST-to-populated-table phases.
            recorder.mark_interactive(monotonic())

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
            await sample_cursor_input(
                pilot,
                table,
                recorder,
                pairs=options.input_sample_pairs,
                now=options.monotonic_fn if options.monotonic_fn is not None else monotonic,
                timeout=options.input_ack_timeout,
                aborted=lambda: source.terminal_failure is not None,
                incomplete=lambda: (
                    bool(events)
                    and churn_done.is_set()
                    and recorder.pending_count() == 0
                    and source.terminal_failure is None
                ),
            )

            # Wait for all events to be emitted and all renders to complete.
            await wait_for(
                pilot,
                lambda: (
                    source.terminal_failure is not None
                    or (churn_done.is_set() and recorder.pending_count() == 0)
                ),
                timeout=replay_churn_completion_timeout(profile, options),
                label="churn complete and all events rendered",
                recorder=recorder,
            )
            if source.terminal_failure is None:
                # The digests below are both computed from data, never from the
                # rendering: a table full of stale cells would satisfy them.
                # Checked here because the table only exists inside this block.
                check_rendered_rows(
                    table, cast(Iterable[PodSummary], store.get("pods", ALL_NAMESPACES))
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

    benchmark = recorder.report(
        manifest, process_samples, final_digest=final_digest, expected_digest=expected_digest
    )

    return ReplayReport(
        object_count=profile.object_count,
        expected_digest=expected_digest,
        final_digest=final_digest,
        dropped_updates=benchmark.dropped_updates,
        rendered_updates=benchmark.rendered_updates,
        render_passes=benchmark.render_passes,
        coalesced_updates=benchmark.coalesced_updates,
        update_latency=benchmark.update_latency,
        input_latency=benchmark.input_latency,
        churn_started_before_input=churn_started_before_input,
        process=benchmark.process,
        api=benchmark.api,
        phases=benchmark.phases,
        manifest=benchmark.manifest,
        failures_injected=benchmark.failures_injected,
    )
