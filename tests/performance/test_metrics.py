from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any, cast

import pytest

from korvid.k8s.telemetry import ReadTelemetryEvent
from tests.performance.metrics import (
    BenchmarkRecorder,
    ChurnSummary,
    LatencySummary,
    ProcessSample,
    ProcessSampler,
    RunManifest,
    render_markdown,
    report_payload,
)


class _MemoryInfo:
    def __init__(self, rss: int) -> None:
        self.rss = rss


class _FakeProcess:
    def __init__(self, cpu_values: tuple[float, ...], rss_values: tuple[int, ...]) -> None:
        self.cpu_values = iter(cpu_values)
        self.rss_values = iter(rss_values)

    def cpu_percent(self, interval: float | None = None) -> float:
        return next(self.cpu_values)

    def memory_info(self) -> _MemoryInfo:
        return _MemoryInfo(next(self.rss_values))


def _metrics_module() -> Any:
    return cast(Any, importlib.import_module("tests.performance.metrics"))


def _patch_sampler_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cpu_values: tuple[float, ...],
    rss_values: tuple[int, ...],
    tracing: bool,
    python_bytes: tuple[int, ...],
    strict_tracing: bool = False,
) -> tuple[dict[str, bool], list[str], Any]:
    metrics = _metrics_module()
    state = {"tracing": tracing}
    lifecycle: list[str] = []
    blocker = asyncio.Event()
    original_sleep = asyncio.sleep
    traced_sizes = iter(python_bytes)

    async def _fake_sleep(_: float) -> None:
        await blocker.wait()

    def _start() -> None:
        lifecycle.append("start")
        state["tracing"] = True

    def _stop() -> None:
        lifecycle.append("stop")
        state["tracing"] = False

    def _get_traced_memory() -> tuple[int, int]:
        if strict_tracing and not state["tracing"]:
            raise RuntimeError("tracemalloc not tracing")
        return (next(traced_sizes), 0)

    monkeypatch.setattr(metrics.psutil, "Process", lambda: _FakeProcess(cpu_values, rss_values))
    monkeypatch.setattr(metrics.tracemalloc, "is_tracing", lambda: state["tracing"])
    monkeypatch.setattr(metrics.tracemalloc, "start", _start)
    monkeypatch.setattr(metrics.tracemalloc, "stop", _stop)
    monkeypatch.setattr(metrics.tracemalloc, "get_traced_memory", _get_traced_memory)
    monkeypatch.setattr(metrics.asyncio, "sleep", _fake_sleep)
    return state, lifecycle, original_sleep


def run_manifest() -> RunManifest:
    return RunManifest(
        profile_id="smoke-1k",
        profile_hash="profile-hash",
        korvid_sha="3cbe600996043cd6cc9df114a71d10c938545789",
        python="3.12.10",
        textual="3.2.0",
        os="Darwin",
        cpu_count=8,
        memory_bytes=16 * 1024 * 1024 * 1024,
    )


def process_samples() -> tuple[ProcessSample, ...]:
    mib = 1024 * 1024
    return (
        ProcessSample(
            elapsed_seconds=0.0, cpu_percent=10.0, rss_bytes=100 * mib, python_bytes=5 * mib
        ),
        ProcessSample(
            elapsed_seconds=60.0, cpu_percent=20.0, rss_bytes=101 * mib, python_bytes=6 * mib
        ),
        ProcessSample(
            elapsed_seconds=120.0, cpu_percent=30.0, rss_bytes=102 * mib, python_bytes=7 * mib
        ),
    )


def test_latency_summary_uses_nearest_rank_percentiles() -> None:
    summary = LatencySummary.from_samples([0.50, 0.10, 0.20, 0.40, 0.30])

    assert summary.count == 5
    assert summary.p50_seconds == pytest.approx(0.30)
    assert summary.p95_seconds == pytest.approx(0.50)
    assert summary.p99_seconds == pytest.approx(0.50)
    assert summary.maximum_seconds == pytest.approx(0.50)


def test_latency_summary_is_empty_without_samples() -> None:
    summary = LatencySummary.from_samples([])

    assert summary.count == 0
    assert summary.p50_seconds is None
    assert summary.p95_seconds is None
    assert summary.p99_seconds is None
    assert summary.maximum_seconds is None


def test_render_flushes_all_pending_events_as_coalesced() -> None:
    recorder = BenchmarkRecorder()
    recorder.record_event(1, 1.0)
    recorder.record_event(2, 1.1)
    recorder.record_render(1.2)

    report = recorder.report(run_manifest(), process_samples(), final_digest="abc")

    assert report.rendered_updates == 2
    assert report.render_passes == 1
    assert report.coalesced_updates == 1
    assert report.dropped_updates == 0
    assert report.event_to_render.count == 2
    assert report.event_to_render.p50_seconds == pytest.approx(0.1)
    assert report.event_to_render.maximum_seconds == pytest.approx(0.2)


def test_report_marks_pending_events_as_dropped() -> None:
    recorder = BenchmarkRecorder()
    recorder.record_event(1, 1.0)
    recorder.record_event(2, 1.1)

    report = recorder.report(run_manifest(), (), final_digest="abc")

    assert report.rendered_updates == 0
    assert report.render_passes == 0
    assert report.coalesced_updates == 0
    assert report.dropped_updates == 2
    assert report.event_to_render.count == 0


def test_report_counts_api_operations_without_path_loss() -> None:
    recorder = BenchmarkRecorder()
    recorder.record_api(
        ReadTelemetryEvent("list", "/api/v1/pods", object_count=1000, decoded_bytes=2048)
    )
    recorder.record_api(ReadTelemetryEvent("watch_open", "/api/v1/pods"))

    payload = report_payload(recorder.report(run_manifest(), [], final_digest="abc"))
    api = cast(dict[str, object], payload["api"])
    operations = cast(dict[str, int], api["operations"])
    paths = cast(dict[str, object], api["paths"])
    pod_path = cast(dict[str, int], paths["/api/v1/pods"])

    assert operations == {"list": 1, "watch_open": 1}
    assert pod_path["list"] == 1
    assert pod_path["watch_open"] == 1
    assert api["decoded_bytes"] == 2048
    assert api["object_count"] == 1000


def test_api_summary_does_not_treat_repeated_lists_as_relists() -> None:
    recorder = BenchmarkRecorder()
    recorder.record_api(ReadTelemetryEvent("list", "/api/v1/pods"))
    recorder.record_api(ReadTelemetryEvent("list", "/api/v1/pods"))

    report = recorder.report(run_manifest(), (), final_digest="abc")

    assert report.api.relists == 0
    assert report.api.operations == {"list": 2}
    assert report.api.paths == {"/api/v1/pods": {"list": 2}}


def test_api_summary_counts_relist_only_after_410_then_list() -> None:
    recorder = BenchmarkRecorder()
    recorder.record_api(ReadTelemetryEvent("list", "/api/v1/pods"))
    recorder.record_api(ReadTelemetryEvent("error", "/api/v1/pods", status=410))
    recorder.record_api(ReadTelemetryEvent("list", "/api/v1/pods"))

    report = recorder.report(run_manifest(), (), final_digest="abc")

    assert report.api.relists == 1
    assert report.api.operations == {"error": 1, "list": 2}
    assert report.api.paths == {"/api/v1/pods": {"error": 1, "list": 2}}


def test_api_summary_mappings_are_immutable_and_payloads_are_copied() -> None:
    recorder = BenchmarkRecorder()
    recorder.record_api(ReadTelemetryEvent("list", "/api/v1/pods"))
    recorder.record_api(ReadTelemetryEvent("watch_open", "/api/v1/pods"))

    report = recorder.report(run_manifest(), (), final_digest="abc")
    operations = cast(Any, report.api.operations)
    paths = cast(Any, report.api.paths)

    with pytest.raises(TypeError, match="does not support item assignment"):
        operations["list"] = 99
    with pytest.raises(TypeError, match="does not support item assignment"):
        paths["/api/v1/pods"]["watch_open"] = 99

    first_payload = report_payload(report)
    first_api = cast(dict[str, object], first_payload["api"])
    first_operations = cast(dict[str, int], first_api["operations"])
    first_paths = cast(dict[str, object], first_api["paths"])
    first_pod_path = cast(dict[str, int], first_paths["/api/v1/pods"])
    first_operations["list"] = 41
    first_pod_path["watch_open"] = 42

    second_payload = report_payload(report)
    second_api = cast(dict[str, object], second_payload["api"])
    second_operations = cast(dict[str, int], second_api["operations"])
    second_paths = cast(dict[str, object], second_api["paths"])
    second_pod_path = cast(dict[str, int], second_paths["/api/v1/pods"])

    assert report.api.operations == {"list": 1, "watch_open": 1}
    assert report.api.paths == {"/api/v1/pods": {"list": 1, "watch_open": 1}}
    assert second_operations == {"list": 1, "watch_open": 1}
    assert second_pod_path == {"list": 1, "watch_open": 1}


def test_report_payload_is_json_serializable_and_stable() -> None:
    recorder = BenchmarkRecorder()
    recorder.record_event(1, 1.0)
    recorder.record_render(1.2)
    recorder.record_input(0.05)
    recorder.record_api(ReadTelemetryEvent("error", "/api/v1/pods", status=429))

    payload = report_payload(
        recorder.report(run_manifest(), process_samples(), final_digest="digest-123")
    )
    encoded = json.dumps(payload, sort_keys=True)

    assert payload == {
        "manifest": {
            "profile_id": "smoke-1k",
            "profile_hash": "profile-hash",
            "korvid_sha": "3cbe600996043cd6cc9df114a71d10c938545789",
            "python": "3.12.10",
            "textual": "3.2.0",
            "os": "Darwin",
            "cpu_count": 8,
            "memory_bytes": 17179869184,
        },
        "latency": {
            "event_to_render": {
                "count": 1,
                "p50_seconds": 0.19999999999999996,
                "p95_seconds": 0.19999999999999996,
                "p99_seconds": 0.19999999999999996,
                "maximum_seconds": 0.19999999999999996,
            },
            "input": {
                "count": 1,
                "p50_seconds": 0.05,
                "p95_seconds": 0.05,
                "p99_seconds": 0.05,
                "maximum_seconds": 0.05,
            },
        },
        "process": {
            "sample_count": 3,
            "cpu_percent_max": 30.0,
            "rss_bytes_max": 106954752,
            "python_bytes_max": 7340032,
            "rss_slope_mib_per_minute": 1.0,
        },
        "api": {
            "operations": {"error": 1},
            "paths": {"/api/v1/pods": {"error": 1}},
            "decoded_bytes": 0,
            "object_count": 0,
            "watch_events": 0,
            "reconnects": 0,
            "relists": 0,
            "throttles": 1,
            "authorization_failures": 0,
        },
        "updates": {
            "rendered_updates": 1,
            "render_passes": 1,
            "coalesced_updates": 0,
            "dropped_updates": 0,
        },
        "churn": None,
        "digests": {"final": "digest-123"},
    }
    assert '"rss_slope_mib_per_minute": 1.0' in encoded


def test_render_markdown_uses_stable_labels() -> None:
    recorder = BenchmarkRecorder()
    recorder.record_event(1, 1.0)
    recorder.record_render(1.1)
    recorder.record_input(0.02)
    recorder.record_api(ReadTelemetryEvent("watch_open", "/api/v1/pods"))

    text = render_markdown(recorder.report(run_manifest(), process_samples(), final_digest="abc"))

    assert "# Large-cluster benchmark report" in text
    assert "- Profile ID: `smoke-1k`" in text
    assert "- Event to render p95: `0.100s`" in text
    assert "- Input latency p95: `0.020s`" in text
    assert "- RSS slope: `1.00 MiB/min`" in text
    assert "- Rendered updates: `1`" in text
    assert "- watch_open: `1`" in text
    assert "- Final digest: `abc`" in text


@pytest.mark.asyncio
async def test_process_sampler_rejects_double_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, original_sleep = _patch_sampler_runtime(
        monkeypatch,
        cpu_values=(0.0, 0.0),
        rss_values=(100,),
        tracing=True,
        python_bytes=(1000,),
    )

    sampler = ProcessSampler(interval_seconds=0.01, clock=lambda: 10.0)
    sampler.start()
    await original_sleep(0)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            sampler.start()
    finally:
        await sampler.stop()


@pytest.mark.asyncio
async def test_process_sampler_rolls_back_tracemalloc_if_task_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, lifecycle, _ = _patch_sampler_runtime(
        monkeypatch,
        cpu_values=(0.0,),
        rss_values=(100,),
        tracing=False,
        python_bytes=(1000,),
    )

    def _fail_create_task(_: object) -> object:
        raise RuntimeError("task creation failed")

    monkeypatch.setattr(_metrics_module().asyncio, "create_task", _fail_create_task)

    sampler = ProcessSampler(interval_seconds=0.01, clock=lambda: 10.0)

    with pytest.raises(RuntimeError, match="task creation failed"):
        sampler.start()

    assert lifecycle == ["start", "stop"]
    assert state["tracing"] is False


@pytest.mark.asyncio
async def test_process_sampler_keeps_owned_tracemalloc_until_last_overlapping_sampler_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, lifecycle, original_sleep = _patch_sampler_runtime(
        monkeypatch,
        cpu_values=(0.0, 12.5),
        rss_values=(100,),
        tracing=False,
        python_bytes=(1000, 2000),
        strict_tracing=True,
    )

    first = ProcessSampler(interval_seconds=0.01, clock=lambda: 11.0)
    second = ProcessSampler(interval_seconds=0.01, clock=lambda: 12.0)

    first.start()
    await original_sleep(0)
    second.start()
    await original_sleep(0)

    await first.stop()

    assert lifecycle == ["start"]
    assert state["tracing"] is True

    await second.stop()

    assert lifecycle == ["start", "stop"]
    assert state["tracing"] is False


@pytest.mark.asyncio
async def test_process_sampler_starts_and_stops_owned_tracemalloc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, lifecycle, original_sleep = _patch_sampler_runtime(
        monkeypatch,
        cpu_values=(0.0, 12.5),
        rss_values=(100,),
        tracing=False,
        python_bytes=(1000,),
        strict_tracing=True,
    )

    sampler = ProcessSampler(interval_seconds=0.01, clock=lambda: 11.0)
    sampler.start()
    await original_sleep(0)
    samples = await sampler.stop()

    assert samples == (
        ProcessSample(
            elapsed_seconds=0.0,
            cpu_percent=12.5,
            rss_bytes=100,
            python_bytes=1000,
        ),
    )
    assert lifecycle == ["start", "stop"]
    assert state["tracing"] is False


@pytest.mark.asyncio
async def test_process_sampler_preserves_preexisting_tracemalloc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, lifecycle, original_sleep = _patch_sampler_runtime(
        monkeypatch,
        cpu_values=(0.0, 8.0),
        rss_values=(120,),
        tracing=True,
        python_bytes=(2000,),
    )

    sampler = ProcessSampler(interval_seconds=0.01, clock=lambda: 5.0)
    sampler.start()
    await original_sleep(0)
    samples = await sampler.stop()

    assert samples == (
        ProcessSample(
            elapsed_seconds=0.0,
            cpu_percent=8.0,
            rss_bytes=120,
            python_bytes=2000,
        ),
    )
    assert lifecycle == []
    assert state["tracing"] is True


@pytest.mark.asyncio
async def test_process_sampler_skips_warmup_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter((10.0, 11.0))
    _, _, original_sleep = _patch_sampler_runtime(
        monkeypatch,
        cpu_values=(0.0, 12.5, 15.0),
        rss_values=(100, 110),
        tracing=True,
        python_bytes=(1000, 1200),
    )

    sampler = ProcessSampler(interval_seconds=0.01, clock=lambda: next(times))
    sampler.start()
    await original_sleep(0)
    samples = await sampler.stop()

    assert samples == (
        ProcessSample(
            elapsed_seconds=1.0,
            cpu_percent=12.5,
            rss_bytes=100,
            python_bytes=1000,
        ),
    )


def test_recorder_exposes_public_pending_count_and_api_errors() -> None:
    """Replay/live harnesses must observe the backlog and API errors through
    a public accessor instead of reaching into `_pending_events`/`_api_events`
    across module boundaries."""
    recorder = BenchmarkRecorder()
    assert recorder.pending_count() == 0
    assert recorder.api_errors() == ()

    recorder.record_event(1, 10.0)
    recorder.record_event(2, 11.0)
    assert recorder.pending_count() == 2

    recorder.record_api(ReadTelemetryEvent("list", "/api/v1/pods"))
    recorder.record_api(ReadTelemetryEvent("error", "/api/v1/pods", status=403))
    recorder.record_api(ReadTelemetryEvent("error", "/api/v1/pods", status=410))

    assert [event.status for event in recorder.api_errors()] == [403, 410]

    recorder.record_render(12.0)
    assert recorder.pending_count() == 0


def test_report_separates_requested_churn_rate_from_achieved_rate() -> None:
    """The design doc forbids reporting a requested rate as an achieved rate.
    Both must be present, distinctly labelled, alongside the observed event
    count, wall time, and the mutation-side throttle count (which is *not* the
    application read path's `api.throttles`)."""
    recorder = BenchmarkRecorder()
    recorder.record_api(ReadTelemetryEvent("list", "/api/v1/pods"))
    churn = ChurnSummary.from_observations(
        requested_events=8400,
        requested_duration_seconds=30,
        observed_events=900,
        wall_seconds=30.0,
        mutation_throttles=7,
    )

    report = recorder.report(run_manifest(), (), final_digest="abc", churn=churn)
    payload = cast(dict[str, object], report_payload(report)["churn"])
    markdown = render_markdown(report)

    assert report.churn is churn
    assert churn.requested_events_per_second == 280.0
    assert churn.achieved_events_per_second == 30.0
    assert payload == {
        "requested_events": 8400,
        "requested_events_per_second": 280.0,
        "observed_events": 900,
        "wall_seconds": 30.0,
        "achieved_events_per_second": 30.0,
        "mutation_throttles": 7,
    }
    assert "Requested churn rate: `280.00 events/s`" in markdown
    assert "Achieved churn rate: `30.00 events/s`" in markdown
    assert "Mutation throttles (429): `7`" in markdown
    # The application read path's throttle counter stays independent.
    assert report.api.throttles == 0


def test_report_payload_keeps_a_stable_churn_key_when_no_churn_was_driven() -> None:
    recorder = BenchmarkRecorder()
    report = recorder.report(run_manifest(), (), final_digest="abc")

    assert report.churn is None
    assert report_payload(report)["churn"] is None
    assert "Achieved churn rate: `n/a`" in render_markdown(report)


def test_churn_summary_reports_no_achieved_rate_without_elapsed_time() -> None:
    churn = ChurnSummary.from_observations(
        requested_events=10,
        requested_duration_seconds=0,
        observed_events=0,
        wall_seconds=None,
        mutation_throttles=0,
    )

    assert churn.achieved_events_per_second is None
    assert churn.requested_events_per_second is None
