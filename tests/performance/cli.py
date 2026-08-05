"""Reproducible scale-benchmark CLI for large-cluster qualification (issue #186).

Usage:
    uv run python -m tests.performance.cli replay \\
        --profile PATH [--time-scale FLOAT] [--sample-interval FLOAT] \\
        [--json PATH] [--out PATH] [--cpu-profile PATH] [--allocation-snapshot PATH]
    uv run python -m tests.performance.cli seed-manifests \\
        --run-id TEXT --namespace-count INT --pods-per-namespace INT \\
        --node-selector KEY=VALUE --output PATH
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import json
import sys
import tracemalloc
from pathlib import Path
from typing import Any

import yaml

from korvid.k8s.errors import ApiStatusError
from tests.performance.manifests import build_seed_manifests
from tests.performance.metrics import BenchmarkReport, render_markdown, report_payload
from tests.performance.profile import WorkloadProfile, load_profile
from tests.performance.replay import ReplayOptions, ReplayReport, run_replay


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


def _flush_allocation_snapshot(path: str) -> None:
    """Take a tracemalloc snapshot and write the top 100 lines to *path*."""
    if not tracemalloc.is_tracing():
        return
    snapshot = tracemalloc.take_snapshot()
    stats = snapshot.statistics("lineno")[:100]
    Path(path).write_text("\n".join(str(stat) for stat in stats))
    tracemalloc.stop()


def _write_outputs(args: argparse.Namespace, replay: ReplayReport) -> None:
    """Print Markdown to stdout and write optional --out / --json outputs."""
    benchmark = _to_benchmark_report(replay)
    markdown = render_markdown(benchmark)
    sys.stdout.write(markdown)
    if args.out_path:
        Path(args.out_path).write_text(markdown)
    if args.json_path:
        payload: dict[str, Any] = {"schema_version": 1, **report_payload(benchmark)}
        Path(args.json_path).write_text(json.dumps(payload, indent=2, sort_keys=True))


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
    except (ApiStatusError, AssertionError, OSError) as exc:
        print(f"error during replay: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.allocation_snapshot:
            _flush_allocation_snapshot(args.allocation_snapshot)

    _write_outputs(args, replay)
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
    return 1


if __name__ == "__main__":
    sys.exit(main())
