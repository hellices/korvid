"""CLI tests for the large-cluster benchmark tool (issue #186)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from korvid.k8s.errors import ApiStatusError
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


async def fake_dropped_report(
    profile: WorkloadProfile,
    options: ReplayOptions,
) -> ReplayReport:
    """Failed replay — matching digests with one unrendered update."""
    return replace(await fake_run_replay(profile, options), dropped_updates=1)


async def fake_api_failure(
    profile: WorkloadProfile,
    options: ReplayOptions,
) -> ReplayReport:
    """Expected replay failure surfaced by the Kubernetes boundary."""
    raise ApiStatusError(503, "unavailable")


async def fake_programmer_error(
    profile: WorkloadProfile,
    options: ReplayOptions,
) -> ReplayReport:
    """Unexpected implementation error that must retain its traceback."""
    raise TypeError("unexpected replay defect")


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
    assert json_path.read_text().index('"api"') < json_path.read_text().index('"schema_version"')
    assert "# Large-cluster benchmark" in markdown_path.read_text()


@pytest.mark.parametrize("failed_replay", [fake_failed_report, fake_dropped_report])
def test_cli_returns_nonzero_for_digest_or_drop_failure(
    monkeypatch: pytest.MonkeyPatch,
    failed_replay: object,
) -> None:
    monkeypatch.setattr(cli, "load_profile", lambda _path: _make_minimal_profile())
    monkeypatch.setattr(cli, "run_replay", failed_replay)
    assert cli.main(["replay", "--profile", "profile.json"]) == 1


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("--time-scale", "-1", "--time-scale must be non-negative"),
        ("--sample-interval", "0", "--sample-interval must be positive"),
    ],
)
def test_cli_rejects_invalid_timing_options(
    option: str,
    value: str,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["replay", "--profile", "profile.json", option, value]) == 1
    assert message in capsys.readouterr().err


def test_cli_reports_expected_replay_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_profile", lambda _path: _make_minimal_profile())
    monkeypatch.setattr(cli, "run_replay", fake_api_failure)
    assert cli.main(["replay", "--profile", "profile.json"]) == 1
    assert "error during replay: API 503: unavailable" in capsys.readouterr().err


def test_cli_does_not_hide_unexpected_profile_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_profile(_path: Path) -> WorkloadProfile:
        raise TypeError("unexpected profile defect")

    monkeypatch.setattr(cli, "load_profile", fail_profile)
    with pytest.raises(TypeError, match="unexpected profile defect"):
        cli.main(["replay", "--profile", "profile.json"])


def test_cli_does_not_hide_unexpected_replay_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_profile", lambda _path: _make_minimal_profile())
    monkeypatch.setattr(cli, "run_replay", fake_programmer_error)
    with pytest.raises(TypeError, match="unexpected replay defect"):
        cli.main(["replay", "--profile", "profile.json"])
