"""CLI contract tests for the large-cluster benchmark tool."""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from tests.performance import cli
from tests.performance.metrics import (
    ApiSummary,
    LatencySummary,
    PhaseSummary,
    ProcessSummary,
    RunManifest,
    ScenarioResult,
    UpdateLatencyKind,
)
from tests.performance.profile import WorkloadProfile
from tests.performance.replay import ReplayOptions, ReplayReport


def _make_report(
    profile: WorkloadProfile,
    *,
    expected_digest: str = "ok",
    final_digest: str = "ok",
    dropped_updates: int = 0,
    update_latency: LatencySummary | None = None,
    update_latency_kind: UpdateLatencyKind = UpdateLatencyKind.EVENT_TO_RENDER,
    ui_scenarios: tuple[ScenarioResult, ...] = (),
) -> ReplayReport:
    return ReplayReport(
        object_count=profile.object_count,
        expected_digest=expected_digest,
        final_digest=final_digest,
        dropped_updates=dropped_updates,
        rendered_updates=0,
        render_passes=0,
        coalesced_updates=0,
        update_latency=update_latency
        or LatencySummary(
            count=0,
            p50_seconds=None,
            p95_seconds=None,
            p99_seconds=None,
            maximum_seconds=None,
        ),
        input_latency=LatencySummary(
            count=0,
            p50_seconds=None,
            p95_seconds=None,
            p99_seconds=None,
            maximum_seconds=None,
        ),
        churn_started_before_input=True,
        process=ProcessSummary(
            sample_count=0,
            cpu_percent_max=None,
            rss_bytes_max=None,
            python_bytes_max=None,
            rss_slope_mib_per_minute=None,
            rss_slope_warmup_boundary_seconds=0.0,
            rss_slope_sample_count=0,
        ),
        api=ApiSummary(
            operations=MappingProxyType({}),
            paths=MappingProxyType({}),
            decoded_bytes=0,
            object_count=0,
            watch_events=0,
            reconnects=0,
            relists=0,
            throttles=0,
            authorization_failures=0,
        ),
        phases=PhaseSummary(
            process_start_to_interactive_seconds=None,
            list_to_populated_table_seconds=None,
            max_backlog_depth=0,
            post_burst_drain_seconds=(),
            max_post_burst_drain_seconds=None,
        ),
        manifest=RunManifest(
            profile_id="test",
            profile_hash="abc123",
            korvid_sha="dev",
            python="3.11",
            textual="0.0.0",
            os="test",
            cpu_count=1,
            memory_bytes=0,
        ),
        update_latency_kind=update_latency_kind,
        ui_scenarios=ui_scenarios,
    )


def profile_path(tmp_path: Path) -> Path:
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


async def fake_run_replay(profile: WorkloadProfile, _options: ReplayOptions) -> ReplayReport:
    return _make_report(profile)


async def fake_failed_replay(profile: WorkloadProfile, _options: ReplayOptions) -> ReplayReport:
    return _make_report(profile, expected_digest="expected-abc", final_digest="actual-xyz")


async def fake_dropped_replay(profile: WorkloadProfile, _options: ReplayOptions) -> ReplayReport:
    return _make_report(profile, dropped_updates=1)


async def fake_run_live_replay(
    profile: WorkloadProfile,
    _options: ReplayOptions,
    *,
    context: str,
    expected_cluster_id: str,
    run_id: str,
) -> ReplayReport:
    del context, expected_cluster_id, run_id
    return _make_report(profile)


async def fake_failed_live_replay(
    profile: WorkloadProfile,
    _options: ReplayOptions,
    *,
    context: str,
    expected_cluster_id: str,
    run_id: str,
) -> ReplayReport:
    del context, expected_cluster_id, run_id
    return _make_report(profile, expected_digest="expected-abc", final_digest="actual-xyz")


async def fake_live_metadata_only_report(
    profile: WorkloadProfile,
    _options: ReplayOptions,
    *,
    context: str,
    expected_cluster_id: str,
    run_id: str,
) -> ReplayReport:
    del context, expected_cluster_id, run_id
    return _make_report(
        profile,
        update_latency=LatencySummary(
            count=3,
            p50_seconds=0.03,
            p95_seconds=0.032,
            p99_seconds=0.032,
            maximum_seconds=0.032,
        ),
        update_latency_kind=UpdateLatencyKind.WATCH_TO_DIFF_COMPLETION,
    )


async def fake_live_failed_ui_scenario(
    profile: WorkloadProfile,
    _options: ReplayOptions,
    *,
    context: str,
    expected_cluster_id: str,
    run_id: str,
) -> ReplayReport:
    del context, expected_cluster_id, run_id
    return _make_report(
        profile,
        ui_scenarios=(
            ScenarioResult(name="filter", latency_seconds=0.4, ok=True),
            ScenarioResult(name="split_pane", latency_seconds=9.9, ok=False),
        ),
    )


_LIVE_IDENTITY_ARGS = [
    "--context",
    "aks-context",
    "--expected-cluster-id",
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/aks",
    "--run-id",
    "aks186",
]


def _live_artifacts(tmp_path: Path, run_id: str = "aks186") -> list[str]:
    return [
        "--json",
        str(tmp_path / f"{run_id}-live.json"),
        "--out",
        str(tmp_path / f"{run_id}-live.md"),
        "--cpu-profile",
        str(tmp_path / f"{run_id}-live.pstats"),
        "--allocation-snapshot",
        str(tmp_path / f"{run_id}-live.alloc.txt"),
    ]


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
            "1.0",
            "--json",
            str(json_path),
            "--out",
            str(markdown_path),
        ]
    )

    assert result == 0
    assert json.loads(json_path.read_text())["schema_version"] == 2
    assert json_path.read_text().index('"api"') < json_path.read_text().index('"schema_version"')
    assert "# Large-cluster benchmark" in markdown_path.read_text()


def test_cli_replay_publishes_a_rendered_cell_run_as_event_to_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    json_path = tmp_path / "result.json"
    markdown_path = tmp_path / "result.md"
    monkeypatch.setattr(cli, "run_replay", fake_run_replay)

    result = cli.main(
        [
            "replay",
            "--profile",
            str(profile_path(tmp_path)),
            "--json",
            str(json_path),
            "--out",
            str(markdown_path),
        ]
    )

    assert result == 0
    latency = json.loads(json_path.read_text())["latency"]
    assert latency["update_latency_kind"] == "event_to_render"
    assert latency["event_to_render"]["p95_seconds"] is None
    assert latency["watch_to_diff_completion"] is None
    assert "- Event to render p95: `n/a`" in markdown_path.read_text()


@pytest.mark.parametrize("failed_replay", [fake_failed_replay, fake_dropped_replay])
def test_cli_returns_nonzero_for_digest_or_drop_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_replay: object,
) -> None:
    monkeypatch.setattr(cli, "run_replay", failed_replay)
    assert cli.main(["replay", "--profile", str(profile_path(tmp_path))]) == 1


def test_cli_writes_seed_manifests_yaml(tmp_path: Path) -> None:
    output_path = tmp_path / "seed.yaml"

    result = cli.main(
        [
            "seed-manifests",
            "--run-id",
            "aks186",
            "--namespace-count",
            "2",
            "--pods-per-namespace",
            "2",
            "--node-selector",
            "korvid.dev/pool=perftest",
            "--output",
            str(output_path),
        ]
    )

    assert result == 0
    documents = list(yaml.safe_load_all(output_path.read_text()))
    assert [document["kind"] for document in documents] == [
        "Namespace",
        "Namespace",
        "Pod",
        "Pod",
        "Pod",
        "Pod",
    ]
    assert documents[0]["metadata"]["name"] == "korvid-perf-aks186-0"
    assert documents[2]["spec"]["nodeSelector"] == {"korvid.dev/pool": "perftest"}


def test_cli_replay_live_writes_json_and_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    json_path = tmp_path / "aks186-live.json"
    markdown_path = tmp_path / "aks186-live.md"
    monkeypatch.setattr(cli, "run_live_replay", fake_run_live_replay)

    result = cli.main(
        [
            "replay-live",
            "--profile",
            str(profile_path(tmp_path)),
            *_LIVE_IDENTITY_ARGS,
            *_live_artifacts(tmp_path),
        ]
    )

    assert result == 0
    assert json.loads(json_path.read_text())["schema_version"] == 2
    assert "# Large-cluster benchmark" in markdown_path.read_text()


def test_cli_replay_live_never_publishes_a_no_op_diff_as_event_to_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    json_path = tmp_path / "aks186-live.json"
    markdown_path = tmp_path / "aks186-live.md"
    monkeypatch.setattr(cli, "run_live_replay", fake_live_metadata_only_report)

    result = cli.main(
        [
            "replay-live",
            "--profile",
            str(profile_path(tmp_path)),
            *_LIVE_IDENTITY_ARGS,
            *_live_artifacts(tmp_path),
        ]
    )

    assert result == 0
    latency = json.loads(json_path.read_text())["latency"]
    assert latency["update_latency_kind"] == "watch_to_diff_completion"
    assert latency["event_to_render"] is None
    assert latency["watch_to_diff_completion"]["p95_seconds"] == 0.032
    markdown = markdown_path.read_text()
    assert "- Watch receipt to diff completion p95: `0.032s`" in markdown
    assert "Event to render" not in markdown


def test_cli_replay_live_returns_nonzero_for_digest_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run_live_replay", fake_failed_live_replay)

    assert (
        cli.main(
            [
                "replay-live",
                "--profile",
                str(profile_path(tmp_path)),
                *_LIVE_IDENTITY_ARGS,
                *_live_artifacts(tmp_path),
            ]
        )
        == 1
    )


def test_cli_replay_live_fails_when_a_ui_scenario_did_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "run_live_replay", fake_live_failed_ui_scenario)

    exit_code = cli.main(
        [
            "replay-live",
            "--profile",
            str(profile_path(tmp_path)),
            *_LIVE_IDENTITY_ARGS,
            *_live_artifacts(tmp_path),
        ]
    )

    assert exit_code == 1
    assert "split_pane" in capsys.readouterr().err
