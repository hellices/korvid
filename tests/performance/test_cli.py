"""CLI tests for the large-cluster benchmark tool (issue #186)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from korvid.k8s.errors import ApiStatusError
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
from tests.performance.profile import FailureInjection, WorkloadProfile
from tests.performance.replay import ReplayOptions, ReplayReport

_REPLAY_TIME_SCALE_ERROR = (
    "--time-scale must be finite and >= 1.0: cursor sampling requires real-time-or-slower churn"
)

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
        rss_slope_warmup_boundary_seconds=0.0,
        rss_slope_sample_count=0,
    )


def _make_phases() -> PhaseSummary:
    return PhaseSummary(
        process_start_to_interactive_seconds=None,
        list_to_populated_table_seconds=None,
        max_backlog_depth=0,
        post_burst_drain_seconds=(),
        max_post_burst_drain_seconds=None,
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


def one_object_profile_path(tmp_path: Path) -> Path:
    """Write a schema-valid single-object profile to *tmp_path* and return its path."""
    path = tmp_path / "single-object.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "single-object",
                "seed": 0,
                "object_count": 1,
                "namespace_count": 1,
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
        update_latency=_make_latency(),
        input_latency=_make_latency(),
        churn_started_before_input=True,
        process=_make_process(),
        api=_make_api(),
        phases=_make_phases(),
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
        update_latency=_make_latency(),
        input_latency=_make_latency(),
        churn_started_before_input=True,
        process=_make_process(),
        api=_make_api(),
        phases=_make_phases(),
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
# Fake coroutines for `replay-live` (mirrors `run_replay`'s keyword-only
# identity arguments).
# ---------------------------------------------------------------------------


def _make_recording_live_replay(
    calls: list[dict[str, object]],
) -> Callable[..., Awaitable[ReplayReport]]:
    """Build a fake `run_live_replay` that records every call's arguments into
    *calls* (a fresh list per test, to stay independent of execution order)."""

    async def fake(
        profile: WorkloadProfile,
        options: ReplayOptions,
        *,
        context: str,
        expected_cluster_id: str,
        run_id: str,
    ) -> ReplayReport:
        calls.append(
            {
                "profile": profile,
                "options": options,
                "context": context,
                "expected_cluster_id": expected_cluster_id,
                "run_id": run_id,
            }
        )
        return await fake_run_replay(profile, options)

    return fake


async def fake_live_failed_report(
    profile: WorkloadProfile,
    options: ReplayOptions,
    *,
    context: str,
    expected_cluster_id: str,
    run_id: str,
) -> ReplayReport:
    """Failed live replay — digest mismatch simulates store corruption."""
    return await fake_failed_report(profile, options)


async def fake_live_api_failure(
    profile: WorkloadProfile,
    options: ReplayOptions,
    *,
    context: str,
    expected_cluster_id: str,
    run_id: str,
) -> ReplayReport:
    """Expected live replay failure surfaced by a fail-closed gate."""
    raise ValueError("wrong active context: expected aks-context, got other-context")


async def fake_live_programmer_error(
    profile: WorkloadProfile,
    options: ReplayOptions,
    *,
    context: str,
    expected_cluster_id: str,
    run_id: str,
) -> ReplayReport:
    """Unexpected implementation error that must retain its traceback."""
    raise TypeError("unexpected live replay defect")


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
    """Deterministic replay churns rendered cells, so its artifacts keep the
    `event_to_render` key and label the published render budget is stated
    against - and leave the metadata-only key unavailable."""
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
    assert latency["event_to_render"] == {
        "count": 0,
        "p50_seconds": None,
        "p95_seconds": None,
        "p99_seconds": None,
        "maximum_seconds": None,
    }
    assert latency["watch_to_diff_completion"] is None
    assert "- Event to render p95: `n/a`" in markdown_path.read_text()


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


@pytest.mark.parametrize("value", ["0", "0.1", "0.999", "nan", "inf", "-inf"])
def test_cli_replay_rejects_compressed_time_scales_before_loading_the_profile(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    """Compressed replay churn drains before the cursor probe can finish, so the
    CLI must reject it during validation instead of failing later with a replay
    `WaitTimeout`."""

    def fail_load(_path: Path) -> WorkloadProfile:
        raise AssertionError("load_profile must not run for an invalid time scale")

    monkeypatch.setattr(cli, "load_profile", fail_load)

    time_scale_arg = f"--time-scale={value}" if value.startswith("-") else "--time-scale"
    argv = ["replay", "--profile", "profile.json", time_scale_arg]
    if time_scale_arg == "--time-scale":
        argv.append(value)
    exit_code = cli.main(argv)

    assert exit_code == 1
    assert capsys.readouterr().err == f"error: {_REPLAY_TIME_SCALE_ERROR}\n"


def test_cli_replay_accepts_and_forwards_time_scale_1_point_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _replay_options_for(
        ["replay", "--profile", str(profile_path(tmp_path)), "--time-scale", "1.0"],
        monkeypatch,
    )

    assert options.time_scale == 1.0


def test_cli_replay_help_explains_the_real_time_scale_floor(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        cli.main(["replay", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    assert "must be finite and >= 1.0" in help_text
    assert "cursor sampling requires churn at real time or slower" in help_text


def test_cli_reports_expected_replay_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_profile", lambda _path: _make_minimal_profile())
    monkeypatch.setattr(cli, "run_replay", fake_api_failure)
    assert cli.main(["replay", "--profile", "profile.json"]) == 1
    assert "error during replay: API 503: unavailable" in capsys.readouterr().err


def test_cli_reports_terminal_replay_abort_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile = replace(
        _make_minimal_profile(),
        steady_events_per_second=2,
        failures=(FailureInjection(kind="forbidden", at_event=1),),
    )
    monkeypatch.setattr(cli, "load_profile", lambda _path: profile)

    assert cli.main(["replay", "--profile", "profile.json", "--time-scale", "1.0"]) == 1
    stderr = capsys.readouterr().err
    assert "error during replay: replay aborted:" in stderr
    assert "Traceback" not in stderr


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


def test_cli_reports_one_object_input_sampling_error_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Schema-valid one-object profiles stay loadable, but the benchmark CLI
    must report the run-time input-sampling contract failure cleanly."""

    async def fail_for_one_object(profile: WorkloadProfile, options: ReplayOptions) -> ReplayReport:
        del options
        assert profile.object_count == 1
        raise ValueError("performance input sampling requires object_count >= 2; got 1")

    monkeypatch.setattr(cli, "run_replay", fail_for_one_object)

    exit_code = cli.main(["replay", "--profile", str(one_object_profile_path(tmp_path))])

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert stderr == (
        "error during replay: performance input sampling requires object_count >= 2; got 1\n"
    )
    assert "Traceback" not in stderr


def test_cli_reports_zero_event_input_sampling_error_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["replay", "--profile", str(profile_path(tmp_path))])

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert stderr == (
        "error during replay: performance input sampling requires at least one scheduled churn event\n"
    )
    assert "Traceback" not in stderr


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


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            [
                "seed-manifests",
                "--run-id",
                "Bad",
                "--namespace-count",
                "1",
                "--pods-per-namespace",
                "1",
                "--node-selector",
                "korvid.dev/pool=perftest",
                "--output",
                "seed.yaml",
            ],
            "error building manifests: run_id must be 1-48 lowercase letters, digits, or hyphens",
        ),
        (
            [
                "seed-manifests",
                "--run-id",
                "aks186",
                "--namespace-count",
                "1",
                "--pods-per-namespace",
                "1",
                "--node-selector",
                "pool",
                "--output",
                "seed.yaml",
            ],
            "error building manifests: node_selector must be exactly one non-empty key=value pair",
        ),
    ],
)
def test_cli_seed_manifests_reports_invalid_inputs(
    arguments: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(arguments) == 1
    assert message in capsys.readouterr().err


def test_cli_seed_manifests_reports_file_write_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_write(self: Path, _text: str, *_args: object, **_kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", fail_write)

    assert (
        cli.main(
            [
                "seed-manifests",
                "--run-id",
                "aks186",
                "--namespace-count",
                "1",
                "--pods-per-namespace",
                "1",
                "--node-selector",
                "korvid.dev/pool=perftest",
                "--output",
                "seed.yaml",
            ]
        )
        == 1
    )
    assert "error writing manifests: disk full" in capsys.readouterr().err


def test_cli_seed_manifests_does_not_hide_unexpected_programmer_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_build(*_args: object, **_kwargs: object) -> object:
        raise TypeError("unexpected manifest defect")

    monkeypatch.setattr(cli, "build_seed_manifests", fail_build, raising=False)

    with pytest.raises(TypeError, match="unexpected manifest defect"):
        cli.main(
            [
                "seed-manifests",
                "--run-id",
                "aks186",
                "--namespace-count",
                "1",
                "--pods-per-namespace",
                "1",
                "--node-selector",
                "korvid.dev/pool=perftest",
                "--output",
                "seed.yaml",
            ]
        )


# ---------------------------------------------------------------------------
# `replay-live`
# ---------------------------------------------------------------------------

_LIVE_IDENTITY_ARGS = [
    "--context",
    "aks-context",
    "--expected-cluster-id",
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/aks",
    "--run-id",
    "aks186",
]


def _live_artifacts(tmp_path: Path, run_id: str = "aks186") -> list[str]:
    """The four run-labelled live artifact destinations a successful live
    qualification must write; every filename carries the run id."""
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


@pytest.mark.parametrize("drop", ["--json", "--out", "--cpu-profile", "--allocation-snapshot"])
def test_cli_replay_live_requires_all_four_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], drop: str
) -> None:
    """A successful live qualification must produce all four externally-retained
    artifacts; dropping any one fails before the run is attempted."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "run_live_replay", _make_recording_live_replay(calls))
    artifacts = _live_artifacts(tmp_path)
    index = artifacts.index(drop)
    del artifacts[index : index + 2]

    exit_code = cli.main(
        ["replay-live", "--profile", str(profile_path(tmp_path)), *_LIVE_IDENTITY_ARGS, *artifacts]
    )

    assert exit_code == 1
    assert "all four artifacts" in capsys.readouterr().err
    assert calls == []


def test_cli_replay_live_rejects_artifact_not_named_for_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each live artifact filename must carry the run id, or it cannot be traced
    back to the run that produced it."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "run_live_replay", _make_recording_live_replay(calls))
    artifacts = _live_artifacts(tmp_path)
    artifacts[1] = str(tmp_path / "result.json")  # no run id in the filename

    exit_code = cli.main(
        ["replay-live", "--profile", str(profile_path(tmp_path)), *_LIVE_IDENTITY_ARGS, *artifacts]
    )

    assert exit_code == 1
    assert "must include the run id" in capsys.readouterr().err
    assert calls == []


def test_cli_replay_live_writes_json_and_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    json_path = tmp_path / "aks186-live.json"
    markdown_path = tmp_path / "aks186-live.md"
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "run_live_replay", _make_recording_live_replay(calls))
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
    assert len(calls) == 1
    assert calls[0]["context"] == "aks-context"
    assert calls[0]["run_id"] == "aks186"
    assert calls[0]["expected_cluster_id"] == _LIVE_IDENTITY_ARGS[3]
    # No --time-scale option: production real-time replay always uses 1.0.
    assert calls[0]["options"].time_scale == 1.0  # type: ignore[attr-defined]


async def fake_live_metadata_only_report(
    profile: WorkloadProfile,
    options: ReplayOptions,
    *,
    context: str,
    expected_cluster_id: str,
    run_id: str,
) -> ReplayReport:
    """A live run exactly as `run_live_replay` reports one: its churn patches
    only the tick label, so its update samples end at a no-op table diff."""
    report = await fake_run_replay(profile, options)
    return replace(
        report,
        update_latency=LatencySummary(
            count=3,
            p50_seconds=0.03,
            p95_seconds=0.032,
            p99_seconds=0.032,
            maximum_seconds=0.032,
        ),
        update_latency_kind=UpdateLatencyKind.WATCH_TO_DIFF_COMPLETION,
    )


def test_cli_replay_live_never_publishes_a_no_op_diff_as_event_to_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artifacts are the evidence. A metadata-only live run must serialize
    its samples under `latency.watch_to_diff_completion`, leave
    `latency.event_to_render` unavailable, and never print an event-to-render
    figure in the Markdown a human reads the render budget from."""
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


@pytest.mark.parametrize(
    "missing_arguments",
    [
        [],
        ["--context", "aks-context"],
        ["--expected-cluster-id", "/subscriptions/sub/x"],
        ["--run-id", "aks186"],
    ],
)
def test_cli_replay_live_requires_identity_arguments(
    tmp_path: Path, missing_arguments: list[str]
) -> None:
    """Every one of --context/--expected-cluster-id/--run-id is mandatory;
    dropping any one of them (while supplying the others is exercised by the
    full identity-args fixture in other tests) must fail argument parsing."""
    with pytest.raises(SystemExit):
        cli.main(
            [
                "replay-live",
                "--profile",
                str(profile_path(tmp_path)),
                *missing_arguments,
            ]
        )


def test_cli_replay_live_rejects_time_scale_option(tmp_path: Path) -> None:
    """`replay-live` must never accept `--time-scale`: live churn always
    replays at real wall-clock time (`ReplayOptions.time_scale == 1.0`)."""
    with pytest.raises(SystemExit):
        cli.main(
            [
                "replay-live",
                "--profile",
                str(profile_path(tmp_path)),
                *_LIVE_IDENTITY_ARGS,
                "--time-scale",
                "0",
            ]
        )


def test_cli_replay_live_rejects_non_positive_duration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli.main(
            [
                "replay-live",
                "--profile",
                str(profile_path(tmp_path)),
                *_LIVE_IDENTITY_ARGS,
                "--duration",
                "0",
            ]
        )
        == 1
    )
    assert "--duration must be positive" in capsys.readouterr().err


def test_cli_replay_live_duration_overrides_profile_duration_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--duration` must override only `duration_seconds`, preserving the
    profile's rate, bursts, seed, topology, and failures untouched."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "run_live_replay", _make_recording_live_replay(calls))
    result = cli.main(
        [
            "replay-live",
            "--profile",
            str(profile_path(tmp_path)),
            *_LIVE_IDENTITY_ARGS,
            "--duration",
            "5",
            *_live_artifacts(tmp_path),
        ]
    )
    assert result == 0
    used_profile = calls[0]["profile"]
    assert isinstance(used_profile, WorkloadProfile)
    original = _make_minimal_profile()
    assert used_profile.duration_seconds == 5
    assert used_profile.steady_events_per_second == original.steady_events_per_second
    assert used_profile.bursts == original.bursts
    assert used_profile.seed == original.seed
    assert used_profile.object_count == original.object_count
    assert used_profile.namespace_count == original.namespace_count
    assert used_profile.failures == original.failures


def test_cli_replay_live_rejects_non_positive_sample_interval(tmp_path: Path) -> None:
    assert (
        cli.main(
            [
                "replay-live",
                "--profile",
                str(profile_path(tmp_path)),
                *_LIVE_IDENTITY_ARGS,
                "--sample-interval",
                "0",
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "failed_replay",
    [fake_live_failed_report],
)
def test_cli_replay_live_returns_nonzero_for_digest_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_replay: object,
) -> None:
    monkeypatch.setattr(cli, "run_live_replay", failed_replay)
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


def test_cli_replay_live_reports_expected_operational_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "run_live_replay", fake_live_api_failure)
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
    assert "error during replay: wrong active context" in capsys.readouterr().err


def test_cli_replay_live_does_not_hide_unexpected_programmer_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run_live_replay", fake_live_programmer_error)
    with pytest.raises(TypeError, match="unexpected live replay defect"):
        cli.main(
            [
                "replay-live",
                "--profile",
                str(profile_path(tmp_path)),
                *_LIVE_IDENTITY_ARGS,
                *_live_artifacts(tmp_path),
            ]
        )


def test_cli_replay_live_does_not_hide_unexpected_profile_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_profile(_path: Path) -> WorkloadProfile:
        raise TypeError("unexpected profile defect")

    monkeypatch.setattr(cli, "load_profile", fail_profile)
    with pytest.raises(TypeError, match="unexpected profile defect"):
        cli.main(
            [
                "replay-live",
                "--profile",
                "profile.json",
                *_LIVE_IDENTITY_ARGS,
            ]
        )


def test_replay_and_seed_manifests_commands_still_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding `replay-live` must not disturb the pre-existing subcommands."""
    monkeypatch.setattr(cli, "run_replay", fake_run_replay)
    assert (
        cli.main(["replay", "--profile", str(profile_path(tmp_path)), "--time-scale", "1.0"]) == 0
    )

    output_path = tmp_path / "seed.yaml"
    assert (
        cli.main(
            [
                "seed-manifests",
                "--run-id",
                "aks186",
                "--namespace-count",
                "1",
                "--pods-per-namespace",
                "1",
                "--node-selector",
                "korvid.dev/pool=perftest",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )


def test_cli_replay_live_rejects_duration_that_orphans_a_burst(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--duration` rewrites the profile with `dataclasses.replace`, skipping
    `load_profile`'s burst containment check. A shortened duration that leaves a
    burst hanging past the end of the run must be rejected with an explicit
    operational message *before* any cluster identity/ownership work, instead of
    tripping the generator's internal assertion mid-run (which printed an empty
    "error during replay: ")."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "run_live_replay", _make_recording_live_replay(calls))
    live_profile = Path("tests/performance/profiles/aks-1k.json")

    exit_code = cli.main(
        [
            "replay-live",
            "--profile",
            str(live_profile),
            *_LIVE_IDENTITY_ARGS,
            "--duration",
            "10",
        ]
    )

    assert exit_code == 1
    assert "falls outside duration_seconds" in capsys.readouterr().err
    assert calls == []


def test_cli_replay_live_accepts_duration_that_still_contains_every_burst(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "run_live_replay", _make_recording_live_replay(calls))

    exit_code = cli.main(
        [
            "replay-live",
            "--profile",
            "tests/performance/profiles/aks-1k.json",
            *_LIVE_IDENTITY_ARGS,
            "--duration",
            "26",
            *_live_artifacts(tmp_path),
        ]
    )

    assert exit_code == 0
    used_profile = calls[0]["profile"]
    assert isinstance(used_profile, WorkloadProfile)
    assert used_profile.duration_seconds == 26


@pytest.mark.parametrize("output_option", ["--out", "--json"])
def test_cli_reports_output_write_errors_instead_of_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_option: str,
) -> None:
    """A bad `--out`/`--json` destination is an operational error, exactly like
    `seed-manifests`' `--output`; it must return 1 with a message rather than
    dumping a traceback after a completed (possibly very expensive) run."""

    def fail_write(self: Path, _text: str, *_args: object, **_kwargs: object) -> int:
        raise OSError("disk full")

    written_profile = profile_path(tmp_path)
    monkeypatch.setattr(cli, "run_replay", fake_run_replay)
    monkeypatch.setattr(Path, "write_text", fail_write)

    exit_code = cli.main(
        [
            "replay",
            "--profile",
            str(written_profile),
            "--time-scale",
            "1.0",
            output_option,
            str(tmp_path / "report.out"),
        ]
    )

    assert exit_code == 1
    assert "error writing report: disk full" in capsys.readouterr().err


def test_cli_replay_live_help_points_at_the_live_qualification_profile(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The published live plan (30 minutes at 20 events/s with three 30-second
    100 events/s bursts) lives in `aks-live-1k.json`; the command an operator
    reaches for must name it, so the deterministic comparison profile is not
    used by accident for a qualification run."""
    with pytest.raises(SystemExit):
        cli.main(["replay-live", "--help"])

    help_text = capsys.readouterr().out
    assert "tests/performance/profiles/aks-live-1k.json" in help_text
    assert Path("tests/performance/profiles/aks-live-1k.json").exists()


async def fake_live_failed_ui_scenario(
    profile: WorkloadProfile,
    options: ReplayOptions,
    *,
    context: str,
    expected_cluster_id: str,
    run_id: str,
) -> ReplayReport:
    """Live replay whose churn and digests are clean but whose UI-at-scale
    evidence is not: the split-pane scenario did not reach its target state."""
    report = await fake_run_replay(profile, options)
    return replace(
        report,
        ui_scenarios=(
            ScenarioResult(name="filter", latency_seconds=0.4, ok=True),
            ScenarioResult(name="split_pane", latency_seconds=9.9, ok=False),
        ),
    )


def test_cli_replay_live_fails_when_a_ui_scenario_did_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`drive_ui_scenarios` records a failed key sequence as `ok=False` instead
    of raising, so the live command must fold those outcomes into its exit
    status — otherwise a qualification "passes" with no UI-at-scale evidence."""
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


def test_cli_replay_live_rejects_artifact_paths_that_alias_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Distinctness must be decided on resolved paths: `sub/../aks186-live.json`
    and `aks186-live.json` are different strings but the same file, so the
    second artifact write would silently destroy the first."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "run_live_replay", _make_recording_live_replay(calls))
    (tmp_path / "sub").mkdir()
    artifacts = _live_artifacts(tmp_path)
    artifacts[3] = str(tmp_path / "sub" / ".." / "aks186-live.json")

    exit_code = cli.main(
        ["replay-live", "--profile", str(profile_path(tmp_path)), *_LIVE_IDENTITY_ARGS, *artifacts]
    )

    assert exit_code == 1
    assert "distinct destinations" in capsys.readouterr().err
    assert calls == []


def test_cli_replay_live_reports_an_unwritable_allocation_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The snapshot is flushed from a `finally`, so an unwritable destination
    would raise straight out of the command - producing a traceback after a
    30-minute run and masking whatever actually failed inside the run."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "run_live_replay", _make_recording_live_replay(calls))
    artifacts = _live_artifacts(tmp_path)
    unwritable = tmp_path / "missing-dir" / "aks186-live.alloc.txt"
    artifacts[artifacts.index("--allocation-snapshot") + 1] = str(unwritable)

    exit_code = cli.main(
        ["replay-live", "--profile", str(profile_path(tmp_path)), *_LIVE_IDENTITY_ARGS, *artifacts]
    )

    assert exit_code == 1
    assert "allocation snapshot" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Cursor-input probe knobs (`--input-ack-timeout`, `--input-sample-pairs`)
# ---------------------------------------------------------------------------


def _make_recording_replay(
    calls: list[dict[str, object]],
) -> Callable[..., Awaitable[ReplayReport]]:
    """Build a fake `run_replay` that records every call's arguments into
    *calls* (a fresh list per test, to stay independent of execution order)."""

    async def fake(profile: WorkloadProfile, options: ReplayOptions) -> ReplayReport:
        calls.append({"profile": profile, "options": options})
        return await fake_run_replay(profile, options)

    return fake


def _replay_options_for(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> ReplayOptions:
    """Run `replay` with *argv* and return the `ReplayOptions` it constructed."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "run_replay", _make_recording_replay(calls))
    assert cli.main(argv) == 0
    options = calls[0]["options"]
    assert isinstance(options, ReplayOptions)
    return options


def _live_options_for(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> ReplayOptions:
    """Run `replay-live` with *argv* and return the `ReplayOptions` it built."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "run_live_replay", _make_recording_live_replay(calls))
    assert cli.main(argv) == 0
    options = calls[0]["options"]
    assert isinstance(options, ReplayOptions)
    return options


def test_cli_replay_passes_the_input_probe_knobs_into_replay_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ReplayOptions` documents the cursor-probe timeout and the sample-pair
    count as run knobs; if the CLI never forwards them, every real run is
    pinned to the defaults and the documentation is a false claim."""
    options = _replay_options_for(
        [
            "replay",
            "--profile",
            str(profile_path(tmp_path)),
            "--time-scale",
            "1.0",
            "--input-ack-timeout",
            "12.5",
            "--input-sample-pairs",
            "7",
        ],
        monkeypatch,
    )

    assert options.input_ack_timeout == 12.5
    assert options.input_sample_pairs == 7


def test_cli_replay_defaults_the_input_probe_knobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _replay_options_for(
        ["replay", "--profile", str(profile_path(tmp_path)), "--time-scale", "1.0"],
        monkeypatch,
    )

    assert options.input_ack_timeout == 5.0
    assert options.input_sample_pairs == 25


def test_cli_replay_live_passes_the_input_probe_knobs_into_replay_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _live_options_for(
        [
            "replay-live",
            "--profile",
            str(profile_path(tmp_path)),
            *_LIVE_IDENTITY_ARGS,
            "--input-ack-timeout",
            "30",
            "--input-sample-pairs",
            "40",
            *_live_artifacts(tmp_path),
        ],
        monkeypatch,
    )

    assert options.input_ack_timeout == 30.0
    assert options.input_sample_pairs == 40


def test_cli_replay_live_defaults_the_input_probe_knobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _live_options_for(
        [
            "replay-live",
            "--profile",
            str(profile_path(tmp_path)),
            *_LIVE_IDENTITY_ARGS,
            *_live_artifacts(tmp_path),
        ],
        monkeypatch,
    )

    assert options.input_ack_timeout == 5.0
    assert options.input_sample_pairs == 25


@pytest.mark.parametrize("value", ["0", "-1.5"])
def test_cli_replay_rejects_a_non_positive_input_ack_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """A zero or negative acknowledgement bound would abort the probe before
    the key can possibly be acknowledged, reporting a harness misconfiguration
    as an application timeout."""
    exit_code = cli.main(
        ["replay", "--profile", str(profile_path(tmp_path)), "--input-ack-timeout", value]
    )

    assert exit_code == 1
    assert "--input-ack-timeout must be finite and positive" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_cli_replay_rejects_a_non_finite_input_ack_timeout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setattr(cli, "run_replay", fake_run_replay)

    exit_code = cli.main(
        ["replay", "--profile", str(profile_path(tmp_path)), f"--input-ack-timeout={value}"]
    )

    assert exit_code == 1
    assert "--input-ack-timeout must be finite and positive" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-3"])
def test_cli_replay_rejects_a_non_positive_input_sample_pair_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """Zero pairs would publish an input percentile computed from no samples."""
    exit_code = cli.main(
        ["replay", "--profile", str(profile_path(tmp_path)), "--input-sample-pairs", value]
    )

    assert exit_code == 1
    assert "--input-sample-pairs must be positive" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-1.5"])
def test_cli_replay_live_rejects_a_non_positive_input_ack_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    exit_code = cli.main(
        [
            "replay-live",
            "--profile",
            str(profile_path(tmp_path)),
            *_LIVE_IDENTITY_ARGS,
            f"--input-ack-timeout={value}",
            *_live_artifacts(tmp_path),
        ]
    )

    assert exit_code == 1
    assert "--input-ack-timeout must be finite and positive" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_cli_replay_live_rejects_a_non_finite_input_ack_timeout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setattr(cli, "run_live_replay", _make_recording_live_replay([]))

    exit_code = cli.main(
        [
            "replay-live",
            "--profile",
            str(profile_path(tmp_path)),
            *_LIVE_IDENTITY_ARGS,
            f"--input-ack-timeout={value}",
            *_live_artifacts(tmp_path),
        ]
    )

    assert exit_code == 1
    assert "--input-ack-timeout must be finite and positive" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-3"])
def test_cli_replay_live_rejects_a_non_positive_input_sample_pair_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    exit_code = cli.main(
        [
            "replay-live",
            "--profile",
            str(profile_path(tmp_path)),
            *_LIVE_IDENTITY_ARGS,
            "--input-sample-pairs",
            value,
            *_live_artifacts(tmp_path),
        ]
    )

    assert exit_code == 1
    assert "--input-sample-pairs must be positive" in capsys.readouterr().err


def test_cli_rejects_a_fractional_input_sample_pair_count(tmp_path: Path) -> None:
    """The pair count is a count, not a rate: argparse must reject `2.5`
    outright rather than silently truncating it to a different workload."""
    with pytest.raises(SystemExit, match="2"):
        cli.main(
            ["replay", "--profile", str(profile_path(tmp_path)), "--input-sample-pairs", "2.5"]
        )
