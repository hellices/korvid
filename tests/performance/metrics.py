from __future__ import annotations

import asyncio
import math
import tracemalloc
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
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
class RunManifest:
    profile_id: str
    profile_hash: str
    korvid_sha: str
    python: str
    textual: str
    os: str
    cpu_count: int
    memory_bytes: int


@dataclass(frozen=True)
class ProcessSummary:
    sample_count: int
    cpu_percent_max: float | None
    rss_bytes_max: int | None
    python_bytes_max: int | None
    rss_slope_mib_per_minute: float | None

    @classmethod
    def from_samples(cls, samples: Sequence[ProcessSample]) -> ProcessSummary:
        if not samples:
            return cls(
                sample_count=0,
                cpu_percent_max=None,
                rss_bytes_max=None,
                python_bytes_max=None,
                rss_slope_mib_per_minute=None,
            )
        return cls(
            sample_count=len(samples),
            cpu_percent_max=max(sample.cpu_percent for sample in samples),
            rss_bytes_max=max(sample.rss_bytes for sample in samples),
            python_bytes_max=max(sample.python_bytes for sample in samples),
            rss_slope_mib_per_minute=_least_squares_slope(samples),
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
            if event.status == 410:
                relist_candidates.add(event.path)
        reconnects = sum(max(counts.get("watch_open", 0) - 1, 0) for counts in paths.values())
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
class BenchmarkReport:
    manifest: RunManifest
    event_to_render: LatencySummary
    input_latency: LatencySummary
    process: ProcessSummary
    api: ApiSummary
    rendered_updates: int
    render_passes: int
    coalesced_updates: int
    dropped_updates: int
    final_digest: str


class ProcessSampler:
    def __init__(self, interval_seconds: float, clock: Callable[[], float] = monotonic) -> None:
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._process = psutil.Process()
        self._start_time: float | None = None
        self._samples: list[ProcessSample] = []
        self._task: asyncio.Task[None] | None = None
        self._owns_tracemalloc = False

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError(
                "ProcessSampler.start() cannot run while sampling is already running"
            )
        self._samples.clear()
        self._start_time = self._clock()
        self._owns_tracemalloc = not tracemalloc.is_tracing()
        if self._owns_tracemalloc:
            tracemalloc.start()
        self._process.cpu_percent()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> tuple[ProcessSample, ...]:
        if self._task is None:
            return tuple(self._samples)
        task = self._task
        self._task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if self._owns_tracemalloc and tracemalloc.is_tracing():
            tracemalloc.stop()
        self._owns_tracemalloc = False
        return tuple(self._samples)

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

    def record_event(self, sequence: int, received_at: float) -> None:
        self._pending_events.append((sequence, received_at))

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

    def record_input(self, latency_seconds: float) -> None:
        self._input_latency.append(latency_seconds)

    def record_api(self, event: ReadTelemetryEvent) -> None:
        self._api_events.append(event)

    def report(
        self,
        manifest: RunManifest,
        process_samples: Sequence[ProcessSample],
        *,
        final_digest: str,
    ) -> BenchmarkReport:
        return BenchmarkReport(
            manifest=manifest,
            event_to_render=LatencySummary.from_samples(self._event_to_render),
            input_latency=LatencySummary.from_samples(self._input_latency),
            process=ProcessSummary.from_samples(process_samples),
            api=ApiSummary.from_events(self._api_events),
            rendered_updates=self._rendered_updates,
            render_passes=self._render_passes,
            coalesced_updates=self._coalesced_updates,
            dropped_updates=len(self._pending_events),
            final_digest=final_digest,
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
        "digests": {"final": report.final_digest},
    }


def render_markdown(report: BenchmarkReport) -> str:
    operation_lines = [
        f"- {operation}: `{count}`" for operation, count in report.api.operations.items()
    ]
    lines = [
        "# Large-cluster benchmark report",
        "",
        "## Run manifest",
        f"- Profile ID: `{report.manifest.profile_id}`",
        f"- Profile hash: `{report.manifest.profile_hash}`",
        f"- Korvid SHA: `{report.manifest.korvid_sha}`",
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
        "",
        "## Updates",
        f"- Rendered updates: `{report.rendered_updates}`",
        f"- Coalesced updates: `{report.coalesced_updates}`",
        f"- Dropped updates: `{report.dropped_updates}`",
        "",
        "## API operations",
        *operation_lines,
        "",
        "## Digests",
        f"- Final digest: `{report.final_digest}`",
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


def _format_slope(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f} MiB/min"
