"""Source-checkout campaign entry point for the operation pack.

    uv run python -m tests.evals.operation_campaign --help

Campaign tooling lives under `tests/` on purpose: it is never shipped in
the wheel, and it is the only place allowed to compose the Textual
`KorvidApp` for evaluation.

Scripted mode gates CI and must pass; live-provider mode is the grinding
mode and never fails the process on model quality (issue #307 release
policy: model scores are informational).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from uuid import uuid4

from korvid.agent.profiles import PromptOverrides, build_profile
from korvid.evals.__main__ import (
    PROBE_TIMEOUT_SECONDS,
    capture_serving,
    httpx_fetch,
    prompt_fingerprint,
    warn_if_unpinned,
)
from korvid.evals.operation import (
    OPERATION_SCHEMA_VERSION,
    OperationJourney,
    bundled_operations_dir,
    load_operation_journeys,
)
from korvid.evals.operation_generation import GenerationRecord, generate_instance
from korvid.evals.scripted import ScriptedProvider

from .operation_app import MIN_APPROVAL_TIMEOUT, OperationRun, run_operation_journey
from .operation_scripts import OPERATION_SCRIPTS


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tests.evals.operation_campaign",
        description="Run the stateful operation-journey pack.",
    )
    parser.add_argument("--operations", type=Path, default=bundled_operations_dir())
    parser.add_argument("--only", action="append", default=[], help="journey id (repeatable)")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--profile", choices=("full", "small"), default="small")
    parser.add_argument("--seeds", default="", help="comma-separated metamorphic seeds")
    parser.add_argument(
        "--scripted",
        action="store_true",
        help="use the deterministic scripts instead of the configured provider",
    )
    parser.add_argument("--approval-timeout", type=float, default=5.0)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--artifacts", type=Path, default=Path("operation-artifacts"))
    return parser.parse_args(argv)


def _korvid_revision() -> str:
    override = os.environ.get("KORVID_EVAL_REVISION", "").strip()
    if override:
        return override
    root = Path(__file__).resolve().parents[2]
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return f"{revision}+dirty" if dirty else revision
    except (OSError, subprocess.CalledProcessError):
        try:
            return version("korvid")
        except PackageNotFoundError:  # source checkout without an installed dist
            return "source"


def _seeds(raw: str) -> list[int]:
    seeds: list[int] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            seeds.append(int(token))
        except ValueError as exc:
            raise ValueError("--seeds must be a comma-separated list of integers") from exc
    if len(seeds) != len(set(seeds)):
        raise ValueError("--seeds must not contain duplicates")
    return seeds


def _selected(journeys: list[OperationJourney], only: list[str]) -> list[OperationJourney]:
    if not only:
        return journeys
    known = {journey.id for journey in journeys}
    unknown = sorted(set(only) - known)
    if unknown:
        raise KeyError(f"unknown journey ids: {unknown}")
    return [journey for journey in journeys if journey.id in set(only)]


def _instances(
    journeys: list[OperationJourney], seeds: list[int]
) -> list[tuple[OperationJourney, GenerationRecord | None]]:
    if not seeds:
        return [(journey, None) for journey in journeys]
    pairs: list[tuple[OperationJourney, GenerationRecord | None]] = []
    for journey in journeys:
        for seed in seeds:
            instance, record = generate_instance(journey, seed)
            pairs.append((instance, record))
    return pairs


def _provider_factory(journey_id: str, scripted: bool) -> Callable[[], Any]:
    if scripted:
        script = OPERATION_SCRIPTS[journey_id]
        return lambda: ScriptedProvider(script)
    from korvid.evals.__main__ import provider_factory_from_env

    factory: Callable[[], Any] = provider_factory_from_env(os.environ)
    return factory


def approval_timeout_for(journey: OperationJourney, default: float) -> float:
    """The approval window to inject for *journey*.

    An expiry fixture waits out the whole window on purpose, so the
    default costs `--approval-timeout` seconds per repetition for nothing.
    Give it the shortest window the harness accepts instead — still >= 1s,
    because a sub-second window can expire between two 0.05s polls. A
    caller whose default is already at or below the floor keeps it, so an
    invalid value surfaces as the harness's own error rather than being
    silently corrected here.
    """

    if journey.approval == "expired" and default > MIN_APPROVAL_TIMEOUT:
        return MIN_APPROVAL_TIMEOUT
    return default


def _record(
    run: OperationRun,
    template_id: str,
    generation: GenerationRecord | None,
    repetition: int,
    *,
    audit_path: Path,
    run_id: str,
) -> dict[str, Any]:
    grade = run.grade
    return {
        "run_id": run_id,
        "template_id": template_id,
        "instance_id": run.journey_id,
        "seed": None if generation is None else generation.seed,
        "generation": None if generation is None else asdict(generation),
        "repetition": repetition,
        "audit_path": str(audit_path),
        "safe": grade.safe,
        "hard_failures": list(grade.hard_failures),
        "outcome": grade.outcome,
        "truthful": grade.truthful,
        "completion": grade.completion,
        "verification": grade.verification,
        "request_match": grade.request_match,
        "efficiency": grade.efficiency,
        "quality": grade.quality,
        "checkpoints": list(grade.checkpoints),
        "missing_checkpoints": list(grade.missing_checkpoints),
        "provisional_assertions": [asdict(item) for item in grade.provisional_assertions],
        "scored_assertions": [asdict(item) for item in grade.scored_assertions],
        "tool_calls": grade.tool_calls,
        "iterations": grade.iterations,
        "wall_time_s": run.wall_time_s,
        # The graded answer and the audit lines are deliberate artifacts:
        # the answer is what `classify_operation_outcome` judged, and the
        # audit file is the product's own record. The *journal* is the part
        # that must never carry a payload, and `ActionJournal` enforces it.
        "answer": run.answer,
        "journal": list(run.journal),
        "audit": list(run.audit),
        "error": None,
    }


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"run-{timestamp}-{uuid4().hex[:8]}"


def _create_run_dir(base: Path) -> tuple[str, Path]:
    base.mkdir(parents=True, exist_ok=True)
    while True:
        run_id = _new_run_id()
        run_dir = base / run_id
        try:
            run_dir.mkdir()
        except FileExistsError:
            continue
        return run_id, run_dir


def _exit_code(exc: SystemExit) -> int:
    code = exc.code
    return code if isinstance(code, int) else 2


def _validated_inputs(
    args: argparse.Namespace,
) -> tuple[list[int], list[tuple[OperationJourney, GenerationRecord | None]]]:
    if args.reps < 1:
        raise ValueError("--reps must be >= 1")
    seeds = _seeds(args.seeds)
    if args.scripted and seeds:
        raise ValueError(
            "--seeds requires a live provider; the deterministic scripts are written "
            "against the template instances"
        )
    if not math.isfinite(args.approval_timeout) or args.approval_timeout < MIN_APPROVAL_TIMEOUT:
        raise ValueError(
            f"--approval-timeout must be at least {MIN_APPROVAL_TIMEOUT}s; a shorter "
            "window can expire between two 0.05s polls"
        )
    try:
        journeys = _selected(load_operation_journeys(args.operations), args.only)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc
    if not journeys:
        raise ValueError("operation pack must contain at least one journey")
    if args.scripted:
        missing_scripts = sorted(
            journey.id for journey in journeys if journey.id not in OPERATION_SCRIPTS
        )
        if missing_scripts:
            raise ValueError(
                f"scripted mode requires OPERATION_SCRIPTS entries for: {missing_scripts}"
            )
    return seeds, _instances(journeys, seeds)


async def _run(
    args: argparse.Namespace,
    pairs: list[tuple[OperationJourney, GenerationRecord | None]],
    *,
    run_id: str,
    run_dir: Path,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for instance, generation in pairs:
        template_id = instance.id if generation is None else generation.template_id
        for repetition in range(1, args.reps + 1):
            print(f"running {instance.id} rep {repetition}/{args.reps} ...", file=sys.stderr)
            audit_path = run_dir / f"{instance.id}-{repetition}-audit.jsonl"
            try:
                run = await run_operation_journey(
                    instance,
                    audit_path=audit_path,
                    provider_factory=_provider_factory(template_id, args.scripted),
                    profile_name=args.profile,
                    approval_timeout_seconds=approval_timeout_for(instance, args.approval_timeout),
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                print(
                    f"error: {instance.id} rep {repetition}: {error}",
                    file=sys.stderr,
                )
                records.append(
                    {
                        "run_id": run_id,
                        "template_id": template_id,
                        "instance_id": instance.id,
                        "seed": None if generation is None else generation.seed,
                        "generation": None if generation is None else asdict(generation),
                        "repetition": repetition,
                        "audit_path": str(audit_path),
                        "safe": False,
                        "hard_failures": [],
                        "outcome": "unknown",
                        "truthful": False,
                        "completion": False,
                        "verification": False,
                        "request_match": False,
                        "efficiency": 0.0,
                        "quality": 0.0,
                        "checkpoints": [],
                        "missing_checkpoints": list(instance.required_checkpoints),
                        "provisional_assertions": [],
                        "scored_assertions": [],
                        "tool_calls": 0,
                        "iterations": 0,
                        "wall_time_s": 0.0,
                        "answer": "",
                        "journal": [],
                        "audit": [],
                        "error": error,
                    }
                )
                continue
            records.append(
                _record(
                    run,
                    template_id,
                    generation,
                    repetition,
                    audit_path=audit_path,
                    run_id=run_id,
                )
            )
    return records


def render_markdown(records: list[dict[str, Any]]) -> str:
    """Compact per-instance summary table."""

    lines = [
        "| journey | rep | safe | outcome | completion | verification | quality | tools | wall s |",
        "|---|---:|---|---|---|---|---:|---:|---:|",
    ]
    for record in records:
        lines.append(
            f"| {record['instance_id']} | {record['repetition']} | "
            f"{'yes' if record['safe'] else 'NO'} | {record['outcome']} | "
            f"{record['completion']} | {record['verification']} | "
            f"{record['quality']:.2f} | {record['tool_calls']} | {record['wall_time_s']:.1f} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the pack.

    Returns:
        `0` when every run met the contract (and always, in live mode);
        `1` when scripted mode produced an unsafe or incomplete run — that
        is the CI contract, and live mode never fails on model quality;
        `2` for a usage error (an unknown journey id, seeds in scripted
        mode, or an approval timeout below the harness floor).
    """

    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return _exit_code(exc)
    try:
        seeds, pairs = _validated_inputs(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    revision = _korvid_revision()
    run_id, run_dir = _create_run_dir(args.artifacts)
    serving = None
    if not args.scripted:
        serving = asyncio.run(
            capture_serving(
                os.environ.get("KORVID_EVAL_BASE_URL", "").strip(),
                os.environ.get("KORVID_EVAL_MODEL", "").strip(),
                fetch=httpx_fetch(
                    api_key=os.environ.get("KORVID_EVAL_API_KEY", "").strip(),
                    timeout_seconds=PROBE_TIMEOUT_SECONDS,
                ),
                warmup=False,
            )
        )
        warn_if_unpinned(serving)
    records = asyncio.run(_run(args, pairs, run_id=run_id, run_dir=run_dir))
    profile = build_profile(
        args.profile, readonly=False, resize_supported=False, overrides=PromptOverrides()
    )
    payload = {
        "meta": {
            "schema_version": OPERATION_SCHEMA_VERSION,
            "korvid_revision": revision,
            "profile": profile.name,
            "prompts": prompt_fingerprint(profile, tools=profile.tools),
            "repetitions": args.reps,
            "mode": "scripted" if args.scripted else "live",
            "seeds": seeds,
            "run_id": run_id,
            "artifact_base": str(args.artifacts),
            "artifact_dir": str(run_dir),
            **({"serving": serving} if serving is not None else {}),
        },
        "runs": records,
    }
    markdown = render_markdown(records)
    print(markdown)
    if args.out:
        args.out.write_text(markdown + "\n", encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if not args.scripted:
        return 0
    failed = [record for record in records if not record["safe"] or not record["completion"]]
    for record in failed:
        print(
            f"error: {record['instance_id']} rep {record['repetition']}: "
            f"safe={record['safe']} completion={record['completion']} "
            f"hard_failures={record['hard_failures']}",
            file=sys.stderr,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
