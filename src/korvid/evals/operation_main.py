"""Public, TUI-free CLI for operation-journey runs.

See `docs/superpowers/specs/2026-08-28-operation-journey-runner-design.md`
§10 for the design decision and `docs/evals/operations.md` for the
published external-optimizer contract.

Exit codes: `2` for a usage/argument error, `1` for a systemic/harness
error (a result artifact could not be written, a provider could not be
constructed), `0` whenever every requested operation ran to a graded
result. A model *failing* an operation (an unsafe write, the wrong
target, a missed checkpoint) is scored evidence in the JSON, not a
nonzero exit — matching `python -m korvid.evals`'s own philosophy for
scenario grading. This is a deliberate divergence from
`operation_campaign.py --scripted` mode's convention (a CI regression
gate, which does exit `1` on an unsafe or incomplete run): this CLI is a
scoring function for an external optimizer that needs every requested
run's result, safe or not, back as data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from korvid.evals.__main__ import (
    EVAL_PROTOCOL_VERSION,
    PROBE_TIMEOUT_SECONDS,
    WARMUP_TIMEOUT_SECONDS,
    capabilities_payload,
    capture_serving,
    httpx_fetch,
    limits_payload,
    policy_payload,
    prompt_fingerprint,
    provider_factory_from_env,
    tools_payload,
)
from korvid.evals.harness import NO_GRIND, PromptGrind, resolve_eval_policy
from korvid.evals.operation import (
    OperationJourney,
    bundled_operations_dir,
    load_operation_journeys,
    operation_case_pack_identity,
    select_operation_journeys,
)
from korvid.evals.operation_runner import _WRITE_ENVIRONMENT, OperationRun, run_operation_case


def run_payload(
    runs: list[OperationRun],
    *,
    journeys: Sequence[OperationJourney],
    policy: Any = None,
    grind: PromptGrind = NO_GRIND,
    serving: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The full JSON artifact: run metadata plus per-operation results.

    This is the external-optimizer contract (`docs/evals/operations.md`):
    `meta.protocol_version` is published unconditionally, and
    `meta.operation_case_pack` is the deterministic identity of the exact
    operation journeys this run measured against — sorted ids, count, and
    a content hash of their loaded definitions, never a path or mtime.

    Args:
        runs: One `OperationRun` per journey actually executed, in the
            order they ran.
        journeys: The exact journeys this run selected (unfiltered pack or
            a `select_operation_journeys` subset) — published as
            `meta.operation_case_pack`.
        policy: The resolved policy this run's meta describes, if the
            caller already resolved one; omitted meta fields
            (`policy`/`limits`/`capabilities`/`catalog_version`/`tools`)
            are simply left out when `None`.
        grind: The eval-only prompt levers this run applied.
        serving: The captured serving block, or `None` when not probed.
    """
    meta: dict[str, Any] = {
        "protocol_version": EVAL_PROTOCOL_VERSION,
        "operation_case_pack": operation_case_pack_identity(journeys),
    }
    if policy is not None:
        meta["policy"] = policy_payload(policy)
        meta["limits"] = limits_payload(policy)
        meta["capabilities"] = capabilities_payload(policy)
        meta["catalog_version"] = policy.catalog_version
        meta["prompts"] = prompt_fingerprint(policy, grind=grind)
        meta["tools"] = tools_payload(policy, [])
    if serving is not None:
        meta["serving"] = serving
    return {
        "meta": meta,
        "operations": [
            {
                "journey_id": run.journey_id,
                "runs": [
                    {
                        "answer": run.answer,
                        "grade": _grade_payload(run.grade),
                        "journal": list(run.journal),
                        "audit": list(run.audit),
                        "decisions": list(run.decisions),
                        "wall_time_s": run.wall_time_s,
                        "prompt": run.prompt,
                    }
                ],
            }
            for run in runs
        ],
    }


def _grade_payload(grade: Any) -> dict[str, Any]:
    """A `dataclasses.asdict`-equivalent, without importing `dataclasses`
    here just for one call: `OperationGrade` is frozen and every field is
    already a JSON-plain type or a tuple of them."""
    import dataclasses

    return dataclasses.asdict(grade)


def _select_operation_journeys(
    journeys: Sequence[OperationJourney], operation_ids: list[str]
) -> list[OperationJourney]:
    """Apply `--operation-id`, if given, turning a bad selection into an
    exit message instead of a traceback; an omitted flag returns
    `journeys` unchanged — the pre-existing, unselected behavior."""
    if not operation_ids:
        return list(journeys)
    try:
        return select_operation_journeys(journeys, operation_ids)
    except ValueError as exc:
        raise SystemExit(f"--operation-id: {exc}") from exc


def _read_prompt_file(path: Path | None, flag: str) -> str | None:
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"{flag}: cannot read {path}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{flag}: {path} is not valid UTF-8: {exc}") from exc
    return text or None


def _prompt_grind(args: argparse.Namespace) -> PromptGrind:
    return PromptGrind(
        tier_pack=_read_prompt_file(args.tier_pack_file, "--tier-pack-file"),
        overlay=_read_prompt_file(args.prompt_overlay_file, "--prompt-overlay-file"),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m korvid.evals.operation_main",
        description=(
            "Run the operation-journey pack (real cluster-write approval "
            "flows) against a live model, TUI-free."
        ),
    )
    parser.add_argument(
        "--operations",
        type=Path,
        default=bundled_operations_dir(),
        help="directory of operation-journey YAML files (default: bundled pack)",
    )
    parser.add_argument(
        "--operation-id",
        action="append",
        default=[],
        metavar="ID",
        dest="operation_id",
        help=(
            "run only this operation id, repeatable, for an exact and "
            "repeatable case pack without copying fixture files into a "
            "separate directory. An id --operations does not contain, an "
            "empty id, or a repeated id is refused rather than silently "
            "narrowed or widened. Omit to run every operation in "
            "--operations (unchanged default behavior)"
        ),
    )
    parser.add_argument(
        "--model-tier",
        choices=("low", "high"),
        default=None,
        help=(
            "evaluate this capability tier; omit to let the shipped model "
            "catalog route the model exactly as the TUI does"
        ),
    )
    parser.add_argument(
        "--tier-pack-file",
        type=Path,
        default=None,
        help=(
            "replace the tier's operating pack with this file's contents. "
            "Eval-only prompt grinding: it is layered after korvid's "
            "immutable safety contract and can never widen it. The result "
            "JSON records the override so the run is not mistaken for a "
            "default-prompt score"
        ),
    )
    parser.add_argument(
        "--prompt-overlay-file",
        type=Path,
        default=None,
        help=(
            "layer this file's contents on top of the tier pack as an "
            "eval overlay, published as 'eval-overlay'"
        ),
    )
    parser.add_argument(
        "--warmup",
        action="store_true",
        help=(
            "send one throwaway request before the first scored operation "
            "so model load time does not land in it; recorded in the "
            "result JSON"
        ),
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="also write the full result JSON to this file",
    )
    return parser.parse_args(argv)


async def _run_operations(
    journeys: Sequence[OperationJourney],
    provider_factory: Callable[[], Any],
    *,
    model_tier: str | None,
    grind: PromptGrind,
) -> list[OperationRun]:
    import tempfile

    runs: list[OperationRun] = []
    for journey in journeys:
        print(f"running {journey.id} ...", file=sys.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.jsonl"
            runs.append(
                await run_operation_case(
                    journey,
                    audit_path=audit_path,
                    provider_factory=provider_factory,
                    model_tier=model_tier,
                    grind=grind,
                )
            )
    return runs


#: `sys.exit("...")`'s message, bounded so a pathological SystemExit payload
#: (e.g. an argument value echoed back unbounded) cannot blow up stderr.
_USAGE_ERROR_MESSAGE_LIMIT = 4000


def _usage_error(exc: SystemExit) -> int:
    """Print `exc`'s message (bounded) to stderr and return this CLI's
    documented usage/config/selection/file-error exit code.

    `sys.exit("some string")` only ever prints that string and exits with
    status `1` - the interpreter's own `code` attribute is not used
    literally unless it is already an `int`. So every function this CLI
    calls that raises `SystemExit(f"...")` for a bad `--operation-id`
    selection, an unreadable `--tier-pack-file`/`--prompt-overlay-file`, or
    an invalid `KORVID_EVAL_*` value must have that `SystemExit` caught
    here in `main()` rather than left to propagate - the promised `2`
    otherwise silently becomes `1`.
    """
    message = exc.code if isinstance(exc.code, str) else str(exc)
    print(message[:_USAGE_ERROR_MESSAGE_LIMIT], file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    args = _parse_args(argv)
    try:
        provider_factory = provider_factory_from_env(os.environ)
        journeys = load_operation_journeys(args.operations)
        if not journeys:
            raise SystemExit(f"no operation YAML files found in {args.operations}")
        journeys = _select_operation_journeys(journeys, args.operation_id)
        grind = _prompt_grind(args)
    except SystemExit as exc:
        return _usage_error(exc)
    try:
        policy = resolve_eval_policy(
            provider_factory(), model_tier=args.model_tier, environment=_WRITE_ENVIRONMENT
        )
    except Exception as exc:  # a provider that cannot even route is systemic
        print(f"error: {exc}", file=sys.stderr)
        return 1
    serving = asyncio.run(
        capture_serving(
            os.environ.get("KORVID_EVAL_BASE_URL", "").strip(),
            os.environ.get("KORVID_EVAL_MODEL", "").strip(),
            fetch=httpx_fetch(
                api_key=os.environ.get("KORVID_EVAL_API_KEY", "").strip(),
                timeout_seconds=PROBE_TIMEOUT_SECONDS,
            ),
            warmup_fetch=httpx_fetch(
                api_key=os.environ.get("KORVID_EVAL_API_KEY", "").strip(),
                timeout_seconds=WARMUP_TIMEOUT_SECONDS,
            ),
            warmup=args.warmup,
        )
    )
    if serving["unavailable"]:
        print(
            f"warning: serving environment not fully pinned: {', '.join(serving['unavailable'])}",
            file=sys.stderr,
        )
    try:
        runs = asyncio.run(
            _run_operations(journeys, provider_factory, model_tier=args.model_tier, grind=grind)
        )
    except Exception as exc:  # systemic/harness failure, not a graded outcome
        print(f"error: {exc}", file=sys.stderr)
        return 1
    payload = run_payload(runs, journeys=journeys, policy=policy, grind=grind, serving=serving)
    for run in runs:
        print(f"{run.journey_id}: {run.grade.outcome} (safe={run.grade.safe})", file=sys.stderr)
    if args.json is not None:
        try:
            args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write {args.json}: {exc}", file=sys.stderr)
            return 1
    else:
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
