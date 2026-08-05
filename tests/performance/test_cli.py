"""CLI tests for the large-cluster benchmark tool (issue #186)."""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from tests.performance import cli
from tests.performance.metrics import (
    ApiSummary,
    LatencySummary,
    ProcessSummary,
    RunManifest,
)
from tests.performance.profile import WorkloadProfile
from tests.performance.replay import ReplayOptions, ReplayReport

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_manifest() -> RunManifest:
    return RunManifest(
        profile_id="test",
        profile_hash="abc123",
        korvid_sha="dev",
        python="3.11",
        textual="0.0.0",
        os="test",
        cpu_count=1,
        memory_bytes=0,
    )


def _make_latency() -> LatencySummary:
    return LatencySummary(
        count=0,
        p50_seconds=None,
        p95_seconds=None,
        p99_seconds=None,
        maximum_seconds=None,
    )


def _make_process() -> ProcessSummary:
    return ProcessSummary(
        sample_count=0,
        cpu_percent_max=None,
        rss_bytes_max=None,
        python_bytes_max=None,
        rss_slope_mib_per_minute=None,
    )


def _make_api() -> ApiSummary:
    return ApiSummary(
        operations=MappingProxyType({}),
        paths=MappingProxyType({}),
        decoded_bytes=0,
        object_count=0,
        watch_events=0,
        reconnects=0,
        relists=0,
        throttles=0,
        authorization_failures=0,
    )


def _make_minimal_profile() -> WorkloadProfile:
    return WorkloadProfile(
        schema_version=1,
        id="smoke-mini",
        seed=0,
        object_count=4,
        namespace_count=2,
        steady_events_per_second=0,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )


def profile_path(tmp_path: Path) -> Path:
    """Write a minimal valid profile to *tmp_path* and return its path."""
    path = tmp_path / "smoke-mini.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "smoke-mini",
                "seed": 0,
                "object_count": 4,
                "namespace_count": 2,
                "steady_events_per_second": 0,
                "duration_seconds": 1,
                "bursts": [],
                "failures": [],
            }
        )
    )
    return path


# ---------------------------------------------------------------------------
# Fake coroutines for monkeypatching
# ---------------------------------------------------------------------------


async def fake_run_replay(
    profile: WorkloadProfile,
    options: ReplayOptions,
) -> ReplayReport:
    """Successful replay — digests match, no dropped updates."""
    return ReplayReport(
        object_count=profile.object_count,
        expected_digest="ok",
        final_digest="ok",
        dropped_updates=0,
        rendered_updates=0,
        render_passes=0,
        coalesced_updates=0,
        event_to_render=_make_latency(),
        input_latency=_make_latency(),
        churn_started_before_input=True,
        process=_make_process(),
        api=_make_api(),
        manifest=_make_manifest(),
    )


async def fake_failed_report(
    profile: WorkloadProfile,
    options: ReplayOptions,
) -> ReplayReport:
    """Failed replay — digest mismatch simulates store corruption."""
    return ReplayReport(
        object_count=profile.object_count,
        expected_digest="expected-abc",
        final_digest="actual-xyz",
        dropped_updates=0,
        rendered_updates=0,
        render_passes=0,
        coalesced_updates=0,
        event_to_render=_make_latency(),
        input_latency=_make_latency(),
        churn_started_before_input=True,
        process=_make_process(),
        api=_make_api(),
        manifest=_make_manifest(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_writes_json_and_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    json_path = tmp_path / "result.json"
    markdown_path = tmp_path / "result.md"
    monkeypatch.setattr(cli, "run_replay", fake_run_replay)
    result = cli.main(
        [
            "replay",
            "--profile",
            str(profile_path(tmp_path)),
            "--time-scale",
            "0",
            "--json",
            str(json_path),
            "--out",
            str(markdown_path),
        ]
    )
    assert result == 0
    assert json.loads(json_path.read_text())["schema_version"] == 1
    assert "# Large-cluster benchmark" in markdown_path.read_text()


def test_cli_returns_nonzero_for_digest_or_drop_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "load_profile", lambda _path: _make_minimal_profile())
    monkeypatch.setattr(cli, "run_replay", fake_failed_report)
    assert cli.main(["replay", "--profile", "profile.json"]) == 1
