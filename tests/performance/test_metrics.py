from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any, cast

import pytest

from korvid.k8s.telemetry import ReadTelemetryEvent
from tests.performance.metrics import (
    BenchmarkRecorder,
    LatencySummary,
    ProcessSample,
    ProcessSampler,
    RunManifest,
    render_markdown,
    report_payload,
)


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
async def test_process_sampler_skips_warmup_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = cast(Any, importlib.import_module("tests.performance.metrics"))

    class _MemoryInfo:
        def __init__(self, rss: int) -> None:
            self.rss = rss

    class _FakeProcess:
        def __init__(self) -> None:
            self.cpu_values = iter((0.0, 12.5, 15.0))
            self.rss_values = iter((100, 110))

        def cpu_percent(self, interval: float | None = None) -> float:
            return next(self.cpu_values)

        def memory_info(self) -> _MemoryInfo:
            return _MemoryInfo(next(self.rss_values))

    times = iter((10.0, 11.0))
    python_bytes = iter((1000, 1200))
    blocker = asyncio.Event()
    original_sleep = asyncio.sleep

    async def _fake_sleep(_: float) -> None:
        await blocker.wait()

    monkeypatch.setattr(metrics.psutil, "Process", _FakeProcess)
    monkeypatch.setattr(metrics.tracemalloc, "get_traced_memory", lambda: (next(python_bytes), 0))
    monkeypatch.setattr(metrics.asyncio, "sleep", _fake_sleep)

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
