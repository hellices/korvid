"""Reproducible scale-benchmark CLI for large-cluster qualification (issue #186).

Usage:
    uv run python -m tests.performance.cli replay \\
        --profile PATH [--time-scale FLOAT] [--sample-interval FLOAT] \\
        [--json PATH] [--out PATH] [--cpu-profile PATH] [--allocation-snapshot PATH]
    uv run python -m tests.performance.cli seed-manifests \\
        --run-id TEXT --namespace-count INT --pods-per-namespace INT \\
        --node-selector KEY=VALUE --output PATH
    uv run python -m tests.performance.cli replay-live \\
        --profile tests/performance/profiles/aks-live-1k.json \\
        --context TEXT --expected-cluster-id TEXT --run-id TEXT \\
        [--duration INT] [--sample-interval FLOAT] \\
        [--json PATH] [--out PATH] [--cpu-profile PATH] [--allocation-snapshot PATH]

`aks-live-1k` is the live qualification profile: it encodes the published live
plan (1,000 Pods across 20 namespaces, 30 minutes at 20 events/s with three
30-second bursts at 100 events/s), so the design doc's event-to-render,
backlog-drain, and RSS-slope budgets are measurable. `aks-1k` keeps the short
deterministic schedule used to compare a live run against the synthetic
1k/10k/50k baselines; use `--duration` to shorten a live smoke run (bursts and
failure points are re-validated against the shortened duration).
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import dataclasses
import json
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


def _to_benchmark_report(replay: ReplayReport) -> BenchmarkReport:
    return BenchmarkReport(
        manifest=replay.manifest,
        event_to_render=replay.event_to_render,
        input_latency=replay.input_latency,
        process=replay.process,
        api=replay.api,
        rendered_updates=replay.rendered_updates,
        render_passes=replay.render_passes,
        coalesced_updates=replay.coalesced_updates,
        dropped_updates=replay.dropped_updates,
        final_digest=replay.final_digest,
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
        help="Sleep multiplier: 0 skips all sleeps, 1.0 replays at real time.",
    )
    rp.add_argument(
        "--sample-interval",
        type=float,
        default=1.0,
        metavar="FLOAT",
        help="Seconds between process-memory samples (positive).",
    )
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
        help="Exact AKS cluster ARM resource ID (`az aks show --ids ...`).",
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
    lp.add_argument(
        "--json",
        dest="json_path",
        default=None,
        metavar="PATH",
        help="Write machine-readable JSON report.",
    )
    lp.add_argument(
        "--out",
        dest="out_path",
        default=None,
        metavar="PATH",
        help="Write Markdown report to file (also printed to stdout).",
    )
    lp.add_argument(
        "--cpu-profile",
        default=None,
        metavar="PATH",
        help="Write cProfile pstats file.",
    )
    lp.add_argument(
        "--allocation-snapshot",
        default=None,
        metavar="PATH",
        help="Write top-100 tracemalloc source locations.",
    )
    return parser


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


def _flush_allocation_snapshot(path: str) -> None:
    """Take a tracemalloc snapshot and write the top 100 lines to *path*."""
    if not tracemalloc.is_tracing():
        return
    snapshot = tracemalloc.take_snapshot()
    stats = snapshot.statistics("lineno")[:100]
    Path(path).write_text("\n".join(str(stat) for stat in stats))
    tracemalloc.stop()


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
    if args.time_scale < 0:
        print("error: --time-scale must be non-negative", file=sys.stderr)
        return 1
    if args.sample_interval <= 0:
        print("error: --sample-interval must be positive", file=sys.stderr)
        return 1

    try:
        profile = load_profile(Path(args.profile))
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"error loading profile: {exc}", file=sys.stderr)
        return 1

    options = ReplayOptions(time_scale=args.time_scale, sample_interval=args.sample_interval)

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
            _flush_allocation_snapshot(args.allocation_snapshot)

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


def _cmd_replay_live(args: argparse.Namespace) -> int:
    if args.duration is not None and args.duration <= 0:
        print("error: --duration must be positive", file=sys.stderr)
        return 1
    if args.sample_interval <= 0:
        print("error: --sample-interval must be positive", file=sys.stderr)
        return 1

    profile = _load_live_profile(args)
    if profile is None:
        return 1

    # No --time-scale option: live churn always replays at real wall-clock
    # time (ReplayOptions.time_scale defaults to 1.0).
    options = ReplayOptions(sample_interval=args.sample_interval)

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
            _flush_allocation_snapshot(args.allocation_snapshot)

    if _write_outputs(args, replay):
        return 1
    if replay.dropped_updates > 0 or replay.expected_digest != replay.final_digest:
        return 1
    return 0


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
