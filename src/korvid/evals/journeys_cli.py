"""CLI for multi-turn conversational journey evaluations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from korvid.evals.__main__ import _positive_int, provider_factory_from_env
from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.journey import bundled_journeys_dir, load_journeys
from korvid.evals.journey_runner import (
    JourneyReport,
    RecordingUI,
    render_markdown,
    report_payload,
    run_journey,
)
from korvid.tools.executor import ToolExecutor


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m korvid.evals.journeys_cli",
        description="Run persistent multi-turn conversational journeys.",
    )
    parser.add_argument(
        "--journeys",
        type=Path,
        default=None,
    )
    parser.add_argument("--reps", type=_positive_int, default=3)
    parser.add_argument("--profile", choices=("full", "small"), default="small")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument(
        "--live",
        action="store_true",
        help="use the guarded real-cluster adapter instead of fixture state",
    )
    parser.add_argument(
        "--context",
        default=os.environ.get("KORVID_LIVE_EVAL_CONTEXT", ""),
    )
    parser.add_argument(
        "--namespace",
        default=os.environ.get("KORVID_LIVE_EVAL_NAMESPACE", ""),
    )
    args = parser.parse_args(argv)
    if args.journeys is None:
        args.journeys = (
            Path(__file__).parent / "live_journeys" if args.live else bundled_journeys_dir()
        )
    return args


def _fake_executor(fixture: Any) -> ToolExecutor:
    return ToolExecutor(
        FakeKubeClient(fixture),
        builtin_aliases(),
        ui=RecordingUI(),
    )


async def _run(args: argparse.Namespace) -> list[JourneyReport]:
    journeys = load_journeys(args.journeys)
    if not journeys:
        raise SystemExit(f"no journey YAML files found in {args.journeys}")
    provider_factory = provider_factory_from_env(os.environ)
    live_environment: Any | None = None
    if args.live:
        from korvid.evals.live_journey import (
            LiveJourneyEnvironment,
            retarget_journey_namespace,
        )

        live_environment = await LiveJourneyEnvironment.connect(
            args.context,
            args.namespace,
        )
        try:
            journeys = [retarget_journey_namespace(journey, args.namespace) for journey in journeys]
        except Exception:
            await live_environment.close()
            raise
        executor_factory: Callable[[Any], ToolExecutor] = live_environment.executor_factory
    else:
        executor_factory = _fake_executor
    reports: list[JourneyReport] = []
    try:
        for journey in journeys:
            print(f"running journey {journey.id} x{args.reps} ...", file=sys.stderr)
            reports.append(
                await run_journey(
                    journey,
                    provider_factory=provider_factory,
                    executor_factory=executor_factory,
                    repetitions=args.reps,
                    profile=args.profile,
                )
            )
    finally:
        if live_environment is not None:
            await live_environment.close()
    return reports


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    reports = asyncio.run(_run(args))
    markdown = render_markdown(reports)
    print(markdown)
    if args.out:
        args.out.write_text(markdown + "\n")
    if args.json:
        args.json.write_text(json.dumps(report_payload(reports), indent=2) + "\n")
    return exit_code(reports)


def exit_code(reports: list[JourneyReport]) -> int:
    """Nonzero for runtime/provider failures, with actionable stderr."""
    errors = 0
    for report in reports:
        for run_index, run in enumerate(report.runs, 1):
            for turn_index, turn in enumerate(run.turns, 1):
                if turn.error is None:
                    continue
                errors += 1
                print(
                    f"error: {report.journey_id} run {run_index} turn {turn_index}: {turn.error}",
                    file=sys.stderr,
                )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
