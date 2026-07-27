"""Live eval CLI: `python -m korvid.evals` (issue #69).

Runs the bundled (or a custom) scenario pack against a live
OpenAI-compatible endpoint and prints a markdown report. This is a
manual, on-demand tool — it talks to a real model and is never part of
CI (CI covers the harness itself with scripted-provider smoke tests).

Configuration comes from the environment:

- `KORVID_EVAL_BASE_URL` — OpenAI-compatible endpoint base URL (required)
- `KORVID_EVAL_MODEL` — model name (required)
- `KORVID_EVAL_API_KEY` — bearer token, if the endpoint needs one
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from korvid.agent.tools import ToolExecutor
from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.runner import (
    DEFAULT_REPETITIONS,
    ScenarioReport,
    render_markdown,
    run_scenario,
)
from korvid.evals.scenario import Scenario, bundled_scenarios_dir, load_scenarios
from korvid.providers.openai_compat import OpenAICompatProvider
from korvid.providers.static_creds import StaticHeaderSource


def provider_factory_from_env(env: Mapping[str, str]) -> Callable[[], Any]:
    """Build a live-provider factory from `KORVID_EVAL_*` variables."""
    base_url = env.get("KORVID_EVAL_BASE_URL", "").strip()
    model = env.get("KORVID_EVAL_MODEL", "").strip()
    if not base_url or not model:
        raise SystemExit(
            "korvid.evals needs a live model endpoint: set KORVID_EVAL_BASE_URL"
            " and KORVID_EVAL_MODEL (and KORVID_EVAL_API_KEY if required)."
        )
    api_key = env.get("KORVID_EVAL_API_KEY", "").strip()

    def factory() -> OpenAICompatProvider:
        credentials = StaticHeaderSource(api_key) if api_key else None
        return OpenAICompatProvider(base_url, model, credentials=credentials)

    return factory


def report_payload(reports: list[ScenarioReport]) -> list[dict[str, Any]]:
    """JSON-serializable form of the reports, for machine consumption."""
    return [
        {
            "scenario": report.scenario_id,
            "root_cause": report.root_cause,
            "successes": report.successes,
            "evidence_hits": report.evidence_hits,
            "runs": [dataclasses.asdict(run) for run in report.runs],
        }
        for report in reports
    ]


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m korvid.evals",
        description="Run the agent eval scenario pack against a live model.",
    )
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=bundled_scenarios_dir(),
        help="directory of scenario YAML files (default: bundled pack)",
    )
    parser.add_argument(
        "--reps",
        type=_positive_int,
        default=DEFAULT_REPETITIONS,
        help=f"repetitions per scenario, at least 1 (default: {DEFAULT_REPETITIONS})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="also write the markdown report to this file",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="also write per-run metrics as JSON to this file",
    )
    return parser.parse_args(argv)


def _executor_factory(scenario: Scenario) -> Callable[[], ToolExecutor]:
    def factory() -> ToolExecutor:
        return ToolExecutor(FakeKubeClient(scenario), builtin_aliases())

    return factory


async def _run_all(
    scenarios: list[Scenario],
    provider_factory: Callable[[], Any],
    repetitions: int,
) -> list[ScenarioReport]:
    reports: list[ScenarioReport] = []
    for scenario in scenarios:
        print(f"running {scenario.id} x{repetitions} ...", file=sys.stderr)
        reports.append(
            await run_scenario(
                scenario,
                provider_factory=provider_factory,
                executor_factory=_executor_factory(scenario),
                repetitions=repetitions,
            )
        )
    return reports


def exit_code(reports: list[ScenarioReport]) -> int:
    """Nonzero when any run errored — an unreachable or misconfigured
    endpoint must not look like a completed evaluation to calling scripts.
    The underlying reasons go to stderr because the markdown report only
    carries aggregate counts."""
    errored = 0
    for report in reports:
        for run in report.runs:
            if run.error is not None:
                errored += 1
                print(f"error: {report.scenario_id}: {run.error}", file=sys.stderr)
    if errored:
        print(f"{errored} run(s) errored.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    args = _parse_args(argv)
    provider_factory = provider_factory_from_env(os.environ)
    scenarios = load_scenarios(args.scenarios)
    if not scenarios:
        raise SystemExit(f"no scenario YAML files found in {args.scenarios}")
    reports = asyncio.run(_run_all(scenarios, provider_factory, args.reps))
    markdown = render_markdown(reports)
    print(markdown)
    if args.out is not None:
        args.out.write_text(markdown + "\n")
    if args.json is not None:
        args.json.write_text(json.dumps(report_payload(reports), indent=2) + "\n")
    return exit_code(reports)


if __name__ == "__main__":
    raise SystemExit(main())
