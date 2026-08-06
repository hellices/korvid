from __future__ import annotations

import asyncio
import math
import tracemalloc
from collections import Counter
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic
from types import MappingProxyType

import psutil  # type: ignore[import-untyped]  # dependency ships without inline stubs

from korvid.k8s.telemetry import ReadTelemetryEvent

_MIB = 1024 * 1024


def _nearest_rank(samples: Sequence[float], percentile: float) -> float:
    index = math.ceil(percentile * len(samples)) - 1
    return samples[index]


def _max_or_none(samples: Sequence[float]) -> float | None:
    if not samples:
        return None
    return max(samples)


def _least_squares_slope(samples: Sequence[ProcessSample]) -> float | None:
    if len(samples) < 2:
        return None
    xs = [sample.elapsed_seconds for sample in samples]
    ys = [float(sample.rss_bytes) for sample in samples]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return None
    return (numerator / denominator) * 60 / _MIB


@dataclass(frozen=True)
class LatencySummary:
    count: int
    p50_seconds: float | None
    p95_seconds: float | None
    p99_seconds: float | None
    maximum_seconds: float | None

    @classmethod
    def from_samples(cls, samples: Sequence[float]) -> LatencySummary:
        ordered = sorted(samples)
        if not ordered:
            return cls(
                count=0,
                p50_seconds=None,
                p95_seconds=None,
                p99_seconds=None,
                maximum_seconds=None,
            )
        return cls(
            count=len(ordered),
            p50_seconds=_nearest_rank(ordered, 0.50),
            p95_seconds=_nearest_rank(ordered, 0.95),
            p99_seconds=_nearest_rank(ordered, 0.99),
            maximum_seconds=ordered[-1],
        )


@dataclass(frozen=True)
class ProcessSample:
    elapsed_seconds: float
    cpu_percent: float
    rss_bytes: int
    python_bytes: int


@dataclass(frozen=True)
class NodePoolInfo:
    """One AKS node pool's identity, Kubernetes version, and node count.

    Part of the live run manifest so retained evidence records exactly which
    node-pool topology was qualified (issue #186 / design "Fixed target").
    """

    name: str
    kubernetes_version: str
    node_count: int


@dataclass(frozen=True)
class RunManifest:
    profile_id: str
    profile_hash: str
    korvid_sha: str
    python: str
    textual: str
    os: str
    cpu_count: int
    memory_bytes: int
    #: Live-only cluster-matrix fields. Absent for deterministic replay, which
    #: does not run against a real cluster; present for a live evidence run so
    #: the retained report establishes which cluster was actually qualified.
    context: str | None = None
    cluster_id: str | None = None
    kubernetes_version: str | None = None
    node_pools: tuple[NodePoolInfo, ...] = ()


@dataclass(frozen=True)
class ProcessSummary:
    sample_count: int
    cpu_percent_max: float | None
    rss_bytes_max: int | None
    python_bytes_max: int | None
    rss_slope_mib_per_minute: float | None
    #: Elapsed-seconds boundary below which samples are treated as warm-up
    #: (process start plus initial table population) and excluded from the
    #: steady-state RSS slope fit. The published budget is explicitly a
    #: *post-warm-up* slope, so startup allocation must not contaminate it.
    rss_slope_warmup_boundary_seconds: float
    #: Number of post-warm-up samples the slope was actually fitted over.
    rss_slope_sample_count: int

    @classmethod
    def from_samples(
        cls,
        samples: Sequence[ProcessSample],
        *,
        warmup_boundary_seconds: float = 0.0,
    ) -> ProcessSummary:
        if not samples:
            return cls(
                sample_count=0,
                cpu_percent_max=None,
                rss_bytes_max=None,
                python_bytes_max=None,
                rss_slope_mib_per_minute=None,
                rss_slope_warmup_boundary_seconds=warmup_boundary_seconds,
                rss_slope_sample_count=0,
            )
        steady_state = [
            sample for sample in samples if sample.elapsed_seconds >= warmup_boundary_seconds
        ]
        return cls(
            sample_count=len(samples),
            cpu_percent_max=max(sample.cpu_percent for sample in samples),
            rss_bytes_max=max(sample.rss_bytes for sample in samples),
            python_bytes_max=max(sample.python_bytes for sample in samples),
            rss_slope_mib_per_minute=_least_squares_slope(steady_state),
            rss_slope_warmup_boundary_seconds=warmup_boundary_seconds,
            rss_slope_sample_count=len(steady_state),
        )


@dataclass(frozen=True)
class ApiSummary:
    operations: Mapping[str, int]
    paths: Mapping[str, Mapping[str, int]]
    decoded_bytes: int
    object_count: int
    watch_events: int
    reconnects: int
    relists: int
    throttles: int
    authorization_failures: int

    @classmethod
    def from_events(cls, events: Sequence[ReadTelemetryEvent]) -> ApiSummary:
        operations = Counter[str]()
        paths: dict[str, Counter[str]] = {}
        decoded_bytes = 0
        object_count = 0
        watch_events = 0
        throttles = 0
        authorization_failures = 0
        relists = 0
        relist_candidates: set[str] = set()
        reconnects = 0
        #: Paths whose stream errored and has not been re-opened yet. A
        #: reconnect is recovery from a dropped watch, so it must be counted
        #: from that recovery - not inferred from the number of `watch_open`s.
        #: A deliberate stop/start (the live `namespace_switch` scenario scopes
        #: the table down and back) re-opens the same path with no error in
        #: between and is not a reconnect.
        dropped_paths: set[str] = set()
        for event in events:
            operations[event.operation] += 1
            paths.setdefault(event.path, Counter())[event.operation] += 1
            decoded_bytes += event.decoded_bytes
            object_count += event.object_count
            if event.operation == "watch_event":
                watch_events += event.object_count
            if event.status == 429:
                throttles += 1
            if event.status in {401, 403}:
                authorization_failures += 1
            if event.operation == "list" and event.path in relist_candidates:
                relists += 1
                relist_candidates.remove(event.path)
            if event.operation == "watch_open" and event.path in dropped_paths:
                reconnects += 1
                dropped_paths.remove(event.path)
            if event.operation == "error":
                dropped_paths.add(event.path)
            if event.status == 410:
                relist_candidates.add(event.path)
        return cls(
            operations=MappingProxyType(dict(sorted(operations.items()))),
            paths=MappingProxyType(
                {
                    path: MappingProxyType(dict(sorted(counts.items())))
                    for path, counts in sorted(paths.items(), key=lambda item: item[0])
                }
            ),
            decoded_bytes=decoded_bytes,
            object_count=object_count,
            watch_events=watch_events,
            reconnects=reconnects,
            relists=relists,
            throttles=throttles,
            authorization_failures=authorization_failures,
        )


@dataclass(frozen=True)
class ChurnSummary:
    """What the churn generator was *asked* to do versus what it observably did.

    The design doc is explicit that "the generator rate and observed API
    throttling are both recorded; requested rate is never reported as achieved
    rate", so the requested schedule and the measured outcome are separate,
    separately labelled fields. `mutation_throttles` counts 429 responses to
    the harness's own write traffic and is deliberately *not* merged into
    `ApiSummary.throttles`, which reports only the application read path.
    """

    requested_events: int
    requested_events_per_second: float | None
    observed_events: int
    wall_seconds: float | None
    achieved_events_per_second: float | None
    mutation_throttles: int

    @classmethod
    def from_observations(
        cls,
        *,
        requested_events: int,
        requested_duration_seconds: int,
        observed_events: int,
        wall_seconds: float | None,
        mutation_throttles: int,
    ) -> ChurnSummary:
        requested_rate = (
            requested_events / requested_duration_seconds
            if requested_duration_seconds > 0
            else None
        )
        achieved_rate = (
            observed_events / wall_seconds
            if wall_seconds is not None and wall_seconds > 0
            else None
        )
        return cls(
            requested_events=requested_events,
            requested_events_per_second=requested_rate,
            observed_events=observed_events,
            wall_seconds=wall_seconds,
            achieved_events_per_second=achieved_rate,
            mutation_throttles=mutation_throttles,
        )


@dataclass(frozen=True)
class ScenarioResult:
    """Outcome and latency of one scoped UI-at-scale scenario driven through
    the real Textual pilot during live churn (issue #186: filter, sort,
    namespace switch, split pane, describe, multi-log)."""

    name: str
    latency_seconds: float
    ok: bool


@dataclass(frozen=True)
class PhaseSummary:
    """Explicit phase measurements the numeric budgets are stated against.

    These are recorded from named lifecycle marks the harness emits, rather
    than inferred from aggregate update counts, so each budget in the design
    doc (process start to interactive table, LIST completion to populated
    table, backlog depth, and post-burst drain time) has a machine-readable
    counterpart in every report.
    """

    process_start_to_interactive_seconds: float | None
    list_to_populated_table_seconds: float | None
    max_backlog_depth: int
    post_burst_drain_seconds: tuple[float, ...]
    max_post_burst_drain_seconds: float | None


@dataclass(frozen=True)
class BenchmarkReport:
    manifest: RunManifest
    event_to_render: LatencySummary
    input_latency: LatencySummary
    process: ProcessSummary
    api: ApiSummary
    phases: PhaseSummary
    rendered_updates: int
    render_passes: int
    coalesced_updates: int
    dropped_updates: int
    final_digest: str
    #: The workload digest the run is expected to converge to. Persisting it
    #: (and `digest_match`) means a report can demonstrate digest correctness
    #: after the process exits and name which side differed. `None` only for
    #: partial reports built without an oracle digest.
    expected_digest: str | None = None
    #: `True` only when an expected digest was supplied and equals the final
    #: digest; pass/fail depends on this, so it is serialized explicitly.
    digest_match: bool = False
    #: How many of each injected failure kind (gone/throttled/forbidden/slow/
    #: metrics_unavailable/slow_logs) were actually exercised, so a report is
    #: self-describing evidence that a failure profile ran rather than a bare
    #: schema literal.
    failures_injected: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    #: Scoped UI-at-scale scenarios driven during live churn (empty offline).
    ui_scenarios: tuple[ScenarioResult, ...] = ()
    #: Present only for runs that drive real mutations (live replay).
    churn: ChurnSummary | None = None


class ProcessSampler:
    _managed_tracemalloc_users = 0

    def __init__(self, interval_seconds: float, clock: Callable[[], float] = monotonic) -> None:
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._process = psutil.Process()
        self._start_time: float | None = None
        self._samples: list[ProcessSample] = []
        self._task: asyncio.Task[None] | None = None
        self._uses_managed_tracing = False

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError(
                "ProcessSampler.start() cannot run while sampling is already running"
            )
        self._samples.clear()
        self._start_time = self._clock()
        self._uses_managed_tracing = self._acquire_tracemalloc()
        task: Coroutine[object, object, None] | None = None
        try:
            self._process.cpu_percent()
            task = self._run()
            self._task = asyncio.create_task(task)
        except BaseException:
            if task is not None:
                task.close()
            if self._uses_managed_tracing:
                self._release_tracemalloc()
                self._uses_managed_tracing = False
            self._start_time = None
            raise

    async def stop(self) -> tuple[ProcessSample, ...]:
        if self._task is None:
            return tuple(self._samples)
        task = self._task
        self._task = None
        task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await task
        finally:
            # A sampling failure must still release managed tracing: raising
            # past this point would leak the tracemalloc lease *and* skip the
            # caller's own teardown (watch manager, benchmark tasks).
            if self._uses_managed_tracing:
                self._release_tracemalloc()
            self._uses_managed_tracing = False
        return tuple(self._samples)

    @classmethod
    def _acquire_tracemalloc(cls) -> bool:
        if tracemalloc.is_tracing():
            if cls._managed_tracemalloc_users == 0:
                return False
        else:
            tracemalloc.start()
        cls._managed_tracemalloc_users += 1
        return True

    @classmethod
    def _release_tracemalloc(cls) -> None:
        cls._managed_tracemalloc_users -= 1
        if cls._managed_tracemalloc_users == 0 and tracemalloc.is_tracing():
            tracemalloc.stop()

    async def _run(self) -> None:
        assert self._start_time is not None
        while True:
            self._samples.append(
                ProcessSample(
                    elapsed_seconds=self._clock() - self._start_time,
                    cpu_percent=self._process.cpu_percent(),
                    rss_bytes=self._process.memory_info().rss,
                    python_bytes=tracemalloc.get_traced_memory()[0],
                )
            )
            await asyncio.sleep(self._interval_seconds)


class BenchmarkRecorder:
    def __init__(self) -> None:
        self._pending_events: list[tuple[int, float]] = []
        self._event_to_render: list[float] = []
        self._input_latency: list[float] = []
        self._api_events: list[ReadTelemetryEvent] = []
        self._rendered_updates = 0
        self._render_passes = 0
        self._coalesced_updates = 0
        self._max_backlog_depth = 0
        self._process_start_at: float | None = None
        self._list_complete_at: float | None = None
        self._interactive_at: float | None = None
        self._burst_end_pending: list[float] = []
        self._post_burst_drains: list[float] = []
        self._failures_injected: Counter[str] = Counter()
        self._ui_scenarios: list[ScenarioResult] = []

    def record_event(self, sequence: int, received_at: float) -> None:
        self._pending_events.append((sequence, received_at))
        self._max_backlog_depth = max(self._max_backlog_depth, len(self._pending_events))

    def mark_process_start(self, at: float) -> None:
        """Record the instant the benchmarked process/app began starting up.

        Paired with `mark_interactive` to measure process-start-to-interactive
        and to derive the steady-state RSS-slope warm-up boundary. First mark
        wins so a later, redundant call cannot move the origin.
        """
        if self._process_start_at is None:
            self._process_start_at = at

    def mark_list_complete(self, at: float) -> None:
        """Record when the initial LIST finished streaming its rows.

        First mark wins: reconnect re-LISTs must not overwrite the initial
        LIST-to-populated-table measurement.
        """
        if self._list_complete_at is None:
            self._list_complete_at = at

    def mark_interactive(self, at: float) -> None:
        """Record when the table first became fully populated/interactive."""
        if self._interactive_at is None:
            self._interactive_at = at

    def mark_burst_end(self, at: float) -> None:
        """Record the end of a churn burst so post-burst drain can be timed.

        With events still pending the drain is resolved by `record_render` the
        next time the backlog empties at or after this instant. With an already
        empty backlog there is nothing to drain, so the sample is `0.0` right
        away: leaving the marker pending would let the next unrelated
        steady-state render report its own latency as a drain, or drop the
        sample entirely if no later render arrives.
        """
        if not self._pending_events:
            self._post_burst_drains.append(0.0)
            return
        self._burst_end_pending.append(at)

    def pending_count(self) -> int:
        """Number of recorded events not yet flushed by a render pass.

        Public so replay/live harnesses can wait for the backlog to drain
        without reaching into the recorder's internals.
        """
        return len(self._pending_events)

    def api_errors(self) -> tuple[ReadTelemetryEvent, ...]:
        """Every recorded `error` read-telemetry event, in arrival order.

        Public so a harness can turn an opaque wait timeout into a message
        naming the underlying API status (403/410/429) that caused it.
        """
        return tuple(event for event in self._api_events if event.operation == "error")

    def record_render(self, rendered_at: float) -> None:
        if not self._pending_events:
            return
        pending = tuple(self._pending_events)
        self._pending_events.clear()
        self._render_passes += 1
        self._rendered_updates += len(pending)
        self._coalesced_updates += max(len(pending) - 1, 0)
        for _, received_at in pending:
            self._event_to_render.append(rendered_at - received_at)
        # The backlog is empty again: resolve every burst whose end has already
        # passed into a drain measurement (end -> backlog-clear interval).
        if self._burst_end_pending:
            resolved = [end for end in self._burst_end_pending if end <= rendered_at]
            for end in resolved:
                self._post_burst_drains.append(rendered_at - end)
            self._burst_end_pending = [end for end in self._burst_end_pending if end > rendered_at]

    def record_input(self, latency_seconds: float) -> None:
        self._input_latency.append(latency_seconds)

    def record_api(self, event: ReadTelemetryEvent) -> None:
        self._api_events.append(event)
        # The first WATCH open follows the initial LIST completing on both the
        # replay and live paths, so it is a uniform, telemetry-driven signal
        # for the LIST-to-populated-table boundary (first mark wins).
        if event.operation == "watch_open":
            self.mark_list_complete(monotonic())

    def record_failure(self, kind: str) -> None:
        """Record that one injected failure of *kind* was actually exercised.

        Report evidence that a failure profile ran - distinct from the profile
        merely declaring it - covering every versioned kind, including
        `metrics_unavailable` and `slow_logs` which do not raise on the
        resource watch path.
        """
        self._failures_injected[kind] += 1

    def record_scenario(self, name: str, latency_seconds: float, ok: bool) -> None:
        """Record one scoped UI-at-scale scenario outcome and latency."""
        self._ui_scenarios.append(ScenarioResult(name=name, latency_seconds=latency_seconds, ok=ok))

    def phases(self) -> PhaseSummary:
        """The explicit phase measurements derived from the lifecycle marks."""
        startup: float | None = None
        if self._process_start_at is not None and self._interactive_at is not None:
            startup = self._interactive_at - self._process_start_at
        list_to_table: float | None = None
        if self._list_complete_at is not None and self._interactive_at is not None:
            list_to_table = self._interactive_at - self._list_complete_at
        return PhaseSummary(
            process_start_to_interactive_seconds=startup,
            list_to_populated_table_seconds=list_to_table,
            max_backlog_depth=self._max_backlog_depth,
            post_burst_drain_seconds=tuple(self._post_burst_drains),
            max_post_burst_drain_seconds=(
                max(self._post_burst_drains) if self._post_burst_drains else None
            ),
        )

    def _warmup_boundary_seconds(self) -> float:
        if self._process_start_at is None or self._interactive_at is None:
            return 0.0
        return max(self._interactive_at - self._process_start_at, 0.0)

    def report(
        self,
        manifest: RunManifest,
        process_samples: Sequence[ProcessSample],
        *,
        final_digest: str,
        expected_digest: str | None = None,
        churn: ChurnSummary | None = None,
    ) -> BenchmarkReport:
        return BenchmarkReport(
            manifest=manifest,
            event_to_render=LatencySummary.from_samples(self._event_to_render),
            input_latency=LatencySummary.from_samples(self._input_latency),
            process=ProcessSummary.from_samples(
                process_samples,
                warmup_boundary_seconds=self._warmup_boundary_seconds(),
            ),
            api=ApiSummary.from_events(self._api_events),
            phases=self.phases(),
            rendered_updates=self._rendered_updates,
            render_passes=self._render_passes,
            coalesced_updates=self._coalesced_updates,
            dropped_updates=len(self._pending_events),
            final_digest=final_digest,
            expected_digest=expected_digest,
            digest_match=expected_digest is not None and expected_digest == final_digest,
            failures_injected=MappingProxyType(dict(sorted(self._failures_injected.items()))),
            ui_scenarios=tuple(self._ui_scenarios),
            churn=churn,
        )


def report_payload(report: BenchmarkReport) -> dict[str, object]:
    api_operations = dict(report.api.operations)
    api_paths = {path: dict(counts) for path, counts in report.api.paths.items()}
    return {
        "manifest": {
            "profile_id": report.manifest.profile_id,
            "profile_hash": report.manifest.profile_hash,
            "korvid_sha": report.manifest.korvid_sha,
            "python": report.manifest.python,
            "textual": report.manifest.textual,
            "os": report.manifest.os,
            "cpu_count": report.manifest.cpu_count,
            "memory_bytes": report.manifest.memory_bytes,
            "context": report.manifest.context,
            "cluster_id": report.manifest.cluster_id,
            "kubernetes_version": report.manifest.kubernetes_version,
            "node_pools": [
                {
                    "name": pool.name,
                    "kubernetes_version": pool.kubernetes_version,
                    "node_count": pool.node_count,
                }
                for pool in report.manifest.node_pools
            ],
        },
        "latency": {
            "event_to_render": {
                "count": report.event_to_render.count,
                "p50_seconds": report.event_to_render.p50_seconds,
                "p95_seconds": report.event_to_render.p95_seconds,
                "p99_seconds": report.event_to_render.p99_seconds,
                "maximum_seconds": report.event_to_render.maximum_seconds,
            },
            "input": {
                "count": report.input_latency.count,
                "p50_seconds": report.input_latency.p50_seconds,
                "p95_seconds": report.input_latency.p95_seconds,
                "p99_seconds": report.input_latency.p99_seconds,
                "maximum_seconds": report.input_latency.maximum_seconds,
            },
        },
        "process": {
            "sample_count": report.process.sample_count,
            "cpu_percent_max": report.process.cpu_percent_max,
            "rss_bytes_max": report.process.rss_bytes_max,
            "python_bytes_max": report.process.python_bytes_max,
            "rss_slope_mib_per_minute": report.process.rss_slope_mib_per_minute,
            "rss_slope_warmup_boundary_seconds": (report.process.rss_slope_warmup_boundary_seconds),
            "rss_slope_sample_count": report.process.rss_slope_sample_count,
        },
        "phases": {
            "process_start_to_interactive_seconds": (
                report.phases.process_start_to_interactive_seconds
            ),
            "list_to_populated_table_seconds": (report.phases.list_to_populated_table_seconds),
            "max_backlog_depth": report.phases.max_backlog_depth,
            "post_burst_drain_seconds": list(report.phases.post_burst_drain_seconds),
            "max_post_burst_drain_seconds": report.phases.max_post_burst_drain_seconds,
        },
        "api": {
            "operations": api_operations,
            "paths": api_paths,
            "decoded_bytes": report.api.decoded_bytes,
            "object_count": report.api.object_count,
            "watch_events": report.api.watch_events,
            "reconnects": report.api.reconnects,
            "relists": report.api.relists,
            "throttles": report.api.throttles,
            "authorization_failures": report.api.authorization_failures,
        },
        "updates": {
            "rendered_updates": report.rendered_updates,
            "render_passes": report.render_passes,
            "coalesced_updates": report.coalesced_updates,
            "dropped_updates": report.dropped_updates,
        },
        "churn": _churn_payload(report.churn),
        "failures_injected": dict(report.failures_injected),
        "ui_scenarios": [
            {
                "name": scenario.name,
                "latency_seconds": scenario.latency_seconds,
                "ok": scenario.ok,
            }
            for scenario in report.ui_scenarios
        ],
        "digests": {
            "expected": report.expected_digest,
            "final": report.final_digest,
            "match": report.digest_match,
        },
    }


def _churn_payload(churn: ChurnSummary | None) -> dict[str, object] | None:
    if churn is None:
        return None
    return {
        "requested_events": churn.requested_events,
        "requested_events_per_second": churn.requested_events_per_second,
        "observed_events": churn.observed_events,
        "wall_seconds": churn.wall_seconds,
        "achieved_events_per_second": churn.achieved_events_per_second,
        "mutation_throttles": churn.mutation_throttles,
    }


def render_markdown(report: BenchmarkReport) -> str:
    churn = report.churn
    operation_lines = [
        f"- {operation}: `{count}`" for operation, count in report.api.operations.items()
    ]
    failure_lines = [
        f"- {kind}: `{count}`" for kind, count in report.failures_injected.items()
    ] or ["- none: `0`"]
    scenario_lines = [
        f"- {scenario.name}: `{_format_seconds(scenario.latency_seconds)}` "
        f"(ok={str(scenario.ok).lower()})"
        for scenario in report.ui_scenarios
    ] or ["- none"]
    manifest_lines = [
        f"- Profile ID: `{report.manifest.profile_id}`",
        f"- Profile hash: `{report.manifest.profile_hash}`",
        f"- Korvid SHA: `{report.manifest.korvid_sha}`",
    ]
    if report.manifest.context is not None:
        manifest_lines.append(f"- Context: `{report.manifest.context}`")
    if report.manifest.cluster_id is not None:
        manifest_lines.append(f"- Cluster ARM id: `{report.manifest.cluster_id}`")
    if report.manifest.kubernetes_version is not None:
        manifest_lines.append(f"- Kubernetes version: `{report.manifest.kubernetes_version}`")
    for pool in report.manifest.node_pools:
        manifest_lines.append(
            f"- Node pool `{pool.name}`: {pool.node_count} node(s) at `{pool.kubernetes_version}`"
        )
    lines = [
        "# Large-cluster benchmark report",
        "",
        "## Run manifest",
        *manifest_lines,
        "",
        "## Latency",
        f"- Event to render p95: `{_format_seconds(report.event_to_render.p95_seconds)}`",
        f"- Event to render p99: `{_format_seconds(report.event_to_render.p99_seconds)}`",
        f"- Event to render max: `{_format_seconds(report.event_to_render.maximum_seconds)}`",
        f"- Input latency p95: `{_format_seconds(report.input_latency.p95_seconds)}`",
        "",
        "## Process",
        f"- CPU max: `{_format_float(report.process.cpu_percent_max)}`",
        f"- RSS max: `{_format_int(report.process.rss_bytes_max)}`",
        f"- RSS slope: `{_format_slope(report.process.rss_slope_mib_per_minute)}`",
        f"- RSS slope warm-up boundary: "
        f"`{_format_seconds(report.process.rss_slope_warmup_boundary_seconds)}`",
        f"- RSS slope samples: `{report.process.rss_slope_sample_count}`",
        "",
        "## Phases",
        f"- Process start to interactive: "
        f"`{_format_seconds(report.phases.process_start_to_interactive_seconds)}`",
        f"- LIST to populated table: "
        f"`{_format_seconds(report.phases.list_to_populated_table_seconds)}`",
        f"- Max backlog depth: `{report.phases.max_backlog_depth}`",
        f"- Max post-burst drain: `{_format_seconds(report.phases.max_post_burst_drain_seconds)}`",
        "",
        "## Churn",
        f"- Requested events: `{_format_int(churn.requested_events if churn else None)}`",
        f"- Requested churn rate: `{_format_rate(churn.requested_events_per_second if churn else None)}`",
        f"- Observed events: `{_format_int(churn.observed_events if churn else None)}`",
        f"- Churn wall time: `{_format_seconds(churn.wall_seconds if churn else None)}`",
        f"- Achieved churn rate: `{_format_rate(churn.achieved_events_per_second if churn else None)}`",
        f"- Mutation throttles (429): `{_format_int(churn.mutation_throttles if churn else None)}`",
        "",
        "## Updates",
        f"- Rendered updates: `{report.rendered_updates}`",
        f"- Coalesced updates: `{report.coalesced_updates}`",
        f"- Dropped updates: `{report.dropped_updates}`",
        "",
        "## API operations",
        *operation_lines,
        "",
        "## Failures injected",
        *failure_lines,
        "",
        "## UI-at-scale scenarios",
        *scenario_lines,
        "",
        "## Digests",
        f"- Expected digest: `{report.expected_digest or 'n/a'}`",
        f"- Final digest: `{report.final_digest}`",
        f"- Digest match: `{str(report.digest_match).lower()}`",
    ]
    return "\n".join(lines) + "\n"


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}s"


def _format_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}"


def _format_int(value: int | None) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _format_rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f} events/s"


def _format_slope(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f} MiB/min"
