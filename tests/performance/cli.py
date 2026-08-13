"""Reproducible scale-benchmark CLI for large-cluster qualification (issue #186).

Usage:
    uv run python -m tests.performance.cli replay \\
        --profile PATH [--time-scale FLOAT] [--sample-interval FLOAT] \\
        [--input-ack-timeout FLOAT] [--input-sample-pairs INT] \\
        [--json PATH] [--out PATH] [--cpu-profile PATH] [--allocation-snapshot PATH]
    uv run python -m tests.performance.cli seed-manifests \\
        --run-id TEXT --namespace-count INT --pods-per-namespace INT \\
        --node-selector KEY=VALUE --output PATH
    uv run python -m tests.performance.cli replay-live \\
        --profile tests/performance/profiles/aks-live-1k.json \\
        --context TEXT --expected-cluster-id TEXT --run-id TEXT \\
        [--duration INT] [--sample-interval FLOAT] \\
        [--input-ack-timeout FLOAT] [--input-sample-pairs INT] \\
        [--json PATH] [--out PATH] [--cpu-profile PATH] [--allocation-snapshot PATH]

`aks-live-1k` is the live qualification profile: it encodes the published live
plan (1,000 Pods across 20 namespaces, 30 minutes at 20 events/s with three
30-second bursts at 100 events/s), so the design doc's event-to-render,
backlog-drain, and RSS-slope budgets are measurable. `aks-1k` keeps the short
deterministic schedule used to compare a live run against the synthetic
1k/10k/50k baselines; use `--duration` to shorten a live smoke run (bursts and
failure points are re-validated against the shortened duration).
`steady-24eps-1k` is the deterministic input-latency acceptance profile: 1,000
Pods across 20 namespaces, 30 seconds of burst-free churn at 24 events/s.
Because the replay command publishes cursor metrics sampled *during* churn,
`replay --time-scale` must be at least 1.0: compressed schedules drain before
the probe can finish and are rejected during CLI validation.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import dataclasses
import json
import math
import sys
import tracemalloc
from pathlib import Path
from typing import Any

import yaml

from korvid.k8s.errors import ApiStatusError
from tests.performance.live import run_live_replay
from tests.performance.manifests import build_seed_manifests
from tests.performance.metrics import BenchmarkReport, render_markdown, report_payload
from tests.performance.profile import WorkloadProfile, load_profile, validate_profile
from tests.performance.replay import ReplayAborted, ReplayOptions, ReplayReport, run_replay
from tests.ui.waits import WaitTimeout

_REPLAY_TIME_SCALE_ERROR = (
    "--time-scale must be finite and >= 1.0: cursor sampling requires real-time-or-slower churn"
)


def _to_benchmark_report(replay: ReplayReport) -> BenchmarkReport:
    return BenchmarkReport(
        manifest=replay.manifest,
        event_to_render=replay.event_to_render,
        input_latency=replay.input_latency,
        process=replay.process,
        api=replay.api,
        phases=replay.phases,
        rendered_updates=replay.rendered_updates,
        render_passes=replay.render_passes,
        coalesced_updates=replay.coalesced_updates,
        dropped_updates=replay.dropped_updates,
        final_digest=replay.final_digest,
        expected_digest=replay.expected_digest,
        digest_match=replay.expected_digest == replay.final_digest,
        failures_injected=replay.failures_injected,
        ui_scenarios=replay.ui_scenarios,
        churn=replay.churn,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.performance.cli",
        description="korvid large-cluster benchmark CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    rp = subparsers.add_parser("replay", help="Replay a workload profile and report metrics.")
    rp.add_argument("--profile", required=True, metavar="PATH", help="Workload profile JSON.")
    rp.add_argument(
        "--time-scale",
        type=float,
        default=1.0,
        metavar="FLOAT",
        help="Sleep multiplier for replay churn (must be finite and >= 1.0): "
        "1.0 replays at real time, larger values slow the schedule; cursor "
        "sampling requires churn at real time or slower.",
    )
    rp.add_argument(
        "--sample-interval",
        type=float,
        default=1.0,
        metavar="FLOAT",
        help="Seconds between process-memory samples (positive).",
    )
    _add_input_probe_arguments(rp)
    rp.add_argument(
        "--json",
        dest="json_path",
        default=None,
        metavar="PATH",
        help="Write machine-readable JSON report.",
    )
    rp.add_argument(
        "--out",
        dest="out_path",
        default=None,
        metavar="PATH",
        help="Write Markdown report to file (also printed to stdout).",
    )
    rp.add_argument(
        "--cpu-profile",
        default=None,
        metavar="PATH",
        help="Write cProfile pstats file.",
    )
    rp.add_argument(
        "--allocation-snapshot",
        default=None,
        metavar="PATH",
        help="Write top-100 tracemalloc source locations.",
    )

    sp = subparsers.add_parser(
        "seed-manifests",
        help="Render deterministic Namespace and Pod manifests for live AKS seeding.",
    )
    sp.add_argument("--run-id", required=True, metavar="TEXT", help="Unique run identifier.")
    sp.add_argument(
        "--namespace-count",
        required=True,
        type=int,
        metavar="INT",
        help="Number of namespaces to create.",
    )
    sp.add_argument(
        "--pods-per-namespace",
        required=True,
        type=int,
        metavar="INT",
        help="Number of Pods to create in each namespace.",
    )
    sp.add_argument(
        "--node-selector",
        required=True,
        metavar="KEY=VALUE",
        help="Exactly one nodeSelector key=value pair.",
    )
    sp.add_argument(
        "--output",
        required=True,
        metavar="PATH",
        help="Destination path for the multi-document YAML output.",
    )

    lp = subparsers.add_parser(
        "replay-live",
        help="Replay churn against an already-seeded, owned real AKS cluster.",
    )
    lp.add_argument(
        "--profile",
        required=True,
        metavar="PATH",
        help="Workload profile JSON. Use tests/performance/profiles/aks-live-1k.json "
        "for a qualification run (the published 30-minute live plan); aks-1k is the "
        "short deterministic comparison schedule.",
    )
    lp.add_argument(
        "--context", required=True, metavar="TEXT", help="Exact active kubeconfig context."
    )
    lp.add_argument(
        "--expected-cluster-id",
        dest="expected_cluster_id",
        required=True,
        metavar="TEXT",
        help="Exact AKS cluster ARM resource ID (must exactly match the returned `az aks show` id).",
    )
    lp.add_argument(
        "--run-id", dest="run_id", required=True, metavar="TEXT", help="Unique run identifier."
    )
    lp.add_argument(
        "--duration",
        type=int,
        default=None,
        metavar="INT",
        help="Override profile duration_seconds (positive); rate, bursts, "
        "seed, topology, and failures are preserved.",
    )
    lp.add_argument(
        "--sample-interval",
        type=float,
        default=1.0,
        metavar="FLOAT",
        help="Seconds between process-memory samples (positive).",
    )
    _add_input_probe_arguments(lp)
    lp.add_argument(
        "--json",
        dest="json_path",
        default=None,
        metavar="PATH",
        help="Required live artifact: machine-readable JSON report (filename must "
        "include the run id).",
    )
    lp.add_argument(
        "--out",
        dest="out_path",
        default=None,
        metavar="PATH",
        help="Required live artifact: Markdown report (also printed to stdout; "
        "filename must include the run id).",
    )
    lp.add_argument(
        "--cpu-profile",
        default=None,
        metavar="PATH",
        help="Required live artifact: cProfile pstats file (filename must include the run id).",
    )
    lp.add_argument(
        "--allocation-snapshot",
        default=None,
        metavar="PATH",
        help="Required live artifact: top-100 tracemalloc source locations "
        "(filename must include the run id).",
    )
    return parser


def _add_input_probe_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the cursor-input probe knobs shared by `replay` and `replay-live`.

    Both commands publish the same input-latency metric, so both must be able
    to tune the same two things: how long a key press may go unacknowledged
    before the run is called failed, and how many samples the reported
    percentile is computed from.
    """
    parser.add_argument(
        "--input-ack-timeout",
        dest="input_ack_timeout",
        type=float,
        default=5.0,
        metavar="FLOAT",
        help="Seconds a cursor key press may go unacknowledged before the run "
        "fails (finite and positive); raise it for a slow remote cluster.",
    )
    parser.add_argument(
        "--input-sample-pairs",
        dest="input_sample_pairs",
        type=int,
        default=25,
        metavar="INT",
        help="Number of down/up cursor round trips to sample (positive); the "
        "reported input percentile is computed from twice this many samples.",
    )


def _input_probe_error(args: argparse.Namespace) -> str | None:
    """Reject probe knobs that cannot produce a usable measurement.

    A non-positive timeout aborts before the key can possibly be
    acknowledged, and a non-positive pair count publishes a percentile over
    no samples. Both are harness misconfiguration, reported as such rather
    than as an application failure after the run.
    """
    if not math.isfinite(args.input_ack_timeout) or args.input_ack_timeout <= 0:
        return "--input-ack-timeout must be finite and positive"
    if args.input_sample_pairs <= 0:
        return "--input-sample-pairs must be positive"
    return None


def _replay_time_scale_error(time_scale: float) -> str | None:
    """Reject compressed replay schedules before a long run starts.

    The replay CLI always publishes cursor metrics sampled during churn. Once
    the schedule is compressed below real time it can drain before that probe
    finishes, so the run should fail as a harness misconfiguration instead of
    surfacing a late `WaitTimeout` after the replay work has already happened.
    """
    if not math.isfinite(time_scale) or time_scale < 1.0:
        return _REPLAY_TIME_SCALE_ERROR
    return None


def _run_with_cpu_profile(
    profile: WorkloadProfile,
    options: ReplayOptions,
    cpu_profile_path: str,
) -> ReplayReport:
    """Run *run_replay* and dump a cProfile pstats file to *cpu_profile_path*."""
    pr = cProfile.Profile()
    pr.enable()
    try:
        return asyncio.run(run_replay(profile, options))
    finally:
        pr.disable()
        pr.dump_stats(cpu_profile_path)


def _run_live_with_cpu_profile(
    profile: WorkloadProfile,
    options: ReplayOptions,
    *,
    context: str,
    expected_cluster_id: str,
    run_id: str,
    cpu_profile_path: str,
) -> ReplayReport:
    """Run *run_live_replay* and dump a cProfile pstats file to *cpu_profile_path*."""
    pr = cProfile.Profile()
    pr.enable()
    try:
        return asyncio.run(
            run_live_replay(
                profile,
                options,
                context=context,
                expected_cluster_id=expected_cluster_id,
                run_id=run_id,
            )
        )
    finally:
        pr.disable()
        pr.dump_stats(cpu_profile_path)


def _flush_allocation_snapshot(path: str) -> int:
    """Take a tracemalloc snapshot and write the top 100 lines to *path*.

    Runs from a `finally`, so a write failure must not replace whatever
    exception is already propagating (or bury a long run's real failure under a
    traceback). Tracing is always stopped.

    Returns:
        0 on success, 1 when the snapshot could not be written.
    """
    if not tracemalloc.is_tracing():
        return 0
    try:
        snapshot = tracemalloc.take_snapshot()
        stats = snapshot.statistics("lineno")[:100]
        Path(path).write_text("\n".join(str(stat) for stat in stats))
    except OSError as exc:
        print(f"error writing allocation snapshot: {exc}", file=sys.stderr)
        return 1
    finally:
        tracemalloc.stop()
    return 0


def _write_outputs(args: argparse.Namespace, replay: ReplayReport) -> int:
    """Print Markdown to stdout and write optional --out / --json outputs.

    Returns:
        0 on success, or 1 when a destination path cannot be written - a bad
        `--out`/`--json` path is an operational error (as it already is for
        `seed-manifests --output`), not a traceback after a long run.
    """
    benchmark = _to_benchmark_report(replay)
    markdown = render_markdown(benchmark)
    sys.stdout.write(markdown)
    try:
        if args.out_path:
            Path(args.out_path).write_text(markdown)
        if args.json_path:
            payload: dict[str, Any] = {"schema_version": 1, **report_payload(benchmark)}
            Path(args.json_path).write_text(json.dumps(payload, indent=2, sort_keys=True))
    except OSError as exc:
        print(f"error writing report: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_seed_manifests(args: argparse.Namespace) -> int:
    try:
        manifests = build_seed_manifests(
            run_id=args.run_id,
            namespace_count=args.namespace_count,
            pods_per_namespace=args.pods_per_namespace,
            node_selector=args.node_selector,
        )
    except ValueError as exc:
        print(f"error building manifests: {exc}", file=sys.stderr)
        return 1

    try:
        text = yaml.safe_dump_all(manifests, sort_keys=False, explicit_start=True)
        Path(args.output).write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"error writing manifests: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    time_scale_error = _replay_time_scale_error(args.time_scale)
    if time_scale_error:
        print(f"error: {time_scale_error}", file=sys.stderr)
        return 1
    if args.sample_interval <= 0:
        print("error: --sample-interval must be positive", file=sys.stderr)
        return 1
    probe_error = _input_probe_error(args)
    if probe_error:
        print(f"error: {probe_error}", file=sys.stderr)
        return 1

    try:
        profile = load_profile(Path(args.profile))
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"error loading profile: {exc}", file=sys.stderr)
        return 1

    options = ReplayOptions(
        time_scale=args.time_scale,
        sample_interval=args.sample_interval,
        input_ack_timeout=args.input_ack_timeout,
        input_sample_pairs=args.input_sample_pairs,
    )

    snapshot_failed = 0
    if args.allocation_snapshot:
        tracemalloc.start()

    try:
        if args.cpu_profile:
            replay = _run_with_cpu_profile(profile, options, args.cpu_profile)
        else:
            replay = asyncio.run(run_replay(profile, options))
    except (ReplayAborted, ApiStatusError, WaitTimeout, OSError) as exc:
        print(f"error during replay: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.allocation_snapshot:
            snapshot_failed = _flush_allocation_snapshot(args.allocation_snapshot)

    return _replay_exit_status(args, replay, snapshot_failed=snapshot_failed)


def _replay_exit_status(
    args: argparse.Namespace, replay: ReplayReport, *, snapshot_failed: int
) -> int:
    """Exit status shared by both replay commands: artifacts first, then the
    correctness criteria (no dropped updates, digest parity)."""
    if snapshot_failed:
        return 1
    if _write_outputs(args, replay):
        return 1
    if replay.dropped_updates > 0 or replay.expected_digest != replay.final_digest:
        return 1
    return 0


def _load_live_profile(args: argparse.Namespace) -> WorkloadProfile | None:
    """Load `--profile` and apply `--duration`, or report why it cannot apply.

    `dataclasses.replace` bypasses `load_profile`, so every duration-dependent
    invariant (burst containment/overlap, failure-injection bounds) is
    re-checked here - before any cluster identity, ownership, or mutation work
    is attempted.

    Returns:
        The profile to replay, or `None` when it cannot be used (the reason is
        already printed to stderr).
    """
    try:
        profile = load_profile(Path(args.profile))
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"error loading profile: {exc}", file=sys.stderr)
        return None
    if args.duration is None:
        return profile
    overridden = dataclasses.replace(profile, duration_seconds=args.duration)
    try:
        validate_profile(overridden)
    except ValueError as exc:
        print(f"error: --duration {args.duration} invalidates the profile: {exc}", file=sys.stderr)
        return None
    return overridden


#: The four externally-retained artifacts a *successful* live qualification
#: must produce (design "Results and documentation": raw JSON samples, process
#: traces, allocation snapshots, plus the human-readable summary). Unlike an
#: ordinary offline `replay`, every live run must write all four to a
#: run-labelled destination so the evidence is retained and traceable.
_LIVE_ARTIFACT_FLAGS: dict[str, str] = {
    "json_path": "--json",
    "out_path": "--out",
    "cpu_profile": "--cpu-profile",
    "allocation_snapshot": "--allocation-snapshot",
}


def _validate_live_artifacts(args: argparse.Namespace, *, run_id: str) -> str | None:
    """Require exactly the four run-labelled live artifact destinations.

    Returns an error message when any of the four is missing, when two point at
    the same destination, or when a destination's filename does not carry the
    run id (so a retained artifact can always be traced back to its run).
    Returns `None` when all four are present, distinct, and run-labelled.
    """
    missing = [flag for attr, flag in _LIVE_ARTIFACT_FLAGS.items() if not getattr(args, attr)]
    if missing:
        return (
            "a successful live qualification must write all four artifacts "
            f"({', '.join(_LIVE_ARTIFACT_FLAGS.values())}); missing: {', '.join(missing)}"
        )
    paths = {attr: Path(getattr(args, attr)) for attr in _LIVE_ARTIFACT_FLAGS}
    # Distinctness is decided on resolved paths: `sub/../run.json` and
    # `run.json` are different strings but the same file, and the second write
    # would silently destroy the first artifact.
    if len({path.resolve() for path in paths.values()}) != len(paths):
        return "the four live artifacts must be four distinct destinations"
    for attr, path in paths.items():
        if run_id not in path.name:
            return (
                f"live artifact {_LIVE_ARTIFACT_FLAGS[attr]} filename {path.name!r} "
                f"must include the run id {run_id!r}"
            )
    return None


def _cmd_replay_live(args: argparse.Namespace) -> int:
    if args.duration is not None and args.duration <= 0:
        print("error: --duration must be positive", file=sys.stderr)
        return 1
    if args.sample_interval <= 0:
        print("error: --sample-interval must be positive", file=sys.stderr)
        return 1
    probe_error = _input_probe_error(args)
    if probe_error:
        print(f"error: {probe_error}", file=sys.stderr)
        return 1

    profile = _load_live_profile(args)
    if profile is None:
        return 1

    artifact_error = _validate_live_artifacts(args, run_id=args.run_id)
    if artifact_error:
        print(f"error: {artifact_error}", file=sys.stderr)
        return 1

    # No --time-scale option: live churn always replays at real wall-clock
    # time (ReplayOptions.time_scale defaults to 1.0).
    options = ReplayOptions(
        sample_interval=args.sample_interval,
        input_ack_timeout=args.input_ack_timeout,
        input_sample_pairs=args.input_sample_pairs,
    )
    return _execute_live_replay(args, profile, options)


def _execute_live_replay(
    args: argparse.Namespace, profile: WorkloadProfile, options: ReplayOptions
) -> int:
    snapshot_failed = 0
    if args.allocation_snapshot:
        tracemalloc.start()

    try:
        if args.cpu_profile:
            replay = _run_live_with_cpu_profile(
                profile,
                options,
                context=args.context,
                expected_cluster_id=args.expected_cluster_id,
                run_id=args.run_id,
                cpu_profile_path=args.cpu_profile,
            )
        else:
            replay = asyncio.run(
                run_live_replay(
                    profile,
                    options,
                    context=args.context,
                    expected_cluster_id=args.expected_cluster_id,
                    run_id=args.run_id,
                )
            )
    except (ValueError, ApiStatusError, WaitTimeout, OSError) as exc:
        print(f"error during replay: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.allocation_snapshot:
            snapshot_failed = _flush_allocation_snapshot(args.allocation_snapshot)

    if snapshot_failed:
        return 1
    failed_scenarios = [scenario.name for scenario in replay.ui_scenarios if not scenario.ok]
    if failed_scenarios:
        # `drive_ui_scenarios` records a key sequence that never reached its
        # target state as `ok=False` instead of raising, so without this the
        # run would "pass" with no UI-at-scale evidence behind it. Reported
        # before the artifacts are written so the reason is not buried under
        # the Markdown report.
        print(
            "error: UI-at-scale scenarios did not pass: " + ", ".join(failed_scenarios),
            file=sys.stderr,
        )
        _write_outputs(args, replay)
        return 1
    return _replay_exit_status(args, replay, snapshot_failed=snapshot_failed)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the large-cluster benchmark.

    Args:
        argv: Argument list; defaults to `sys.argv[1:]` when `None`.

    Returns:
        0 on success; 1 for runtime error, dropped updates, or digest mismatch.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "seed-manifests":
        return _cmd_seed_manifests(args)
    if args.command == "replay-live":
        return _cmd_replay_live(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
