"""Tests for the public, TUI-free operation-journey CLI.

Argument parsing, selection, and `run_payload`'s exact JSON shape are unit
tested directly, mirroring `tests/evals/test_cli.py`. `main()`'s own
process exit code is the one exception to that file's "never invoke
main() end-to-end" convention: it is exercised here as a real subprocess
(`python -m korvid.evals.operation_main`), because the promised `2` for a
usage/config/selection/file error and `1` for a systemic failure are a
property of the actual process exit status - `sys.exit("a string")`
prints that string but always exits `1`, never the string's own promised
code, so this is the only way to prove `main()` centralizes that
correctly rather than letting a bare `SystemExit` propagate uncaught.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from korvid.evals.__main__ import EVAL_PROTOCOL_VERSION
from korvid.evals.harness import NO_GRIND
from korvid.evals.operation import bundled_operations_dir, load_operation_journeys
from korvid.evals.operation_main import (
    _parse_args,
    _select_operation_journeys,
    run_payload,
)
from korvid.evals.operation_runner import run_operation_case
from korvid.evals.scripted import ScriptedProvider

from .operation_scripts import OPERATION_SCRIPTS

_JOURNEYS = load_operation_journeys(bundled_operations_dir())
_BUNDLED_IDS = sorted(journey.id for journey in _JOURNEYS)


# --- argument parsing --------------------------------------------------------


def test_operation_id_defaults_to_empty_and_repeatable() -> None:
    args = _parse_args(["--operation-id", "restart-deployment", "--operation-id", "scale-no-op"])
    assert args.operation_id == ["restart-deployment", "scale-no-op"]


def test_operation_id_omitted_defaults_to_empty_list() -> None:
    args = _parse_args([])
    assert args.operation_id == []


def test_operations_dir_defaults_to_the_bundled_pack() -> None:
    args = _parse_args([])
    assert args.operations == bundled_operations_dir()


def test_json_flag_accepts_a_path(tmp_path: Path) -> None:
    args = _parse_args(["--json", str(tmp_path / "out.json")])
    assert args.json == tmp_path / "out.json"


# --- selection: fail-closed exactly like PR #321's --scenario-id -----------


def test_selecting_no_ids_runs_every_bundled_journey_unchanged() -> None:
    selected = _select_operation_journeys(_JOURNEYS, [])
    assert [journey.id for journey in selected] == _BUNDLED_IDS


def test_selecting_one_known_id_returns_only_that_journey() -> None:
    selected = _select_operation_journeys(_JOURNEYS, ["restart-deployment"])
    assert [journey.id for journey in selected] == ["restart-deployment"]


def test_selecting_an_unknown_id_exits_nonzero() -> None:
    with pytest.raises(SystemExit):
        _select_operation_journeys(_JOURNEYS, ["does-not-exist"])


def test_selecting_a_duplicate_id_exits_nonzero() -> None:
    with pytest.raises(SystemExit):
        _select_operation_journeys(_JOURNEYS, ["restart-deployment", "restart-deployment"])


def test_selecting_an_explicit_empty_id_exits_nonzero() -> None:
    with pytest.raises(SystemExit):
        _select_operation_journeys(_JOURNEYS, [""])


# --- run_payload: the exact external-optimizer JSON contract ---------------


async def _run(journey_id: str, tmp_path: Path) -> Any:
    journey = next(journey for journey in _JOURNEYS if journey.id == journey_id)
    return await run_operation_case(
        journey,
        audit_path=tmp_path / f"{journey_id}-audit.jsonl",
        provider_factory=lambda: ScriptedProvider(OPERATION_SCRIPTS[journey_id]),
    )


async def test_run_payload_always_publishes_the_protocol_version(tmp_path: Path) -> None:
    run = await _run("scale-no-op", tmp_path)
    payload = run_payload([run], journeys=[next(j for j in _JOURNEYS if j.id == "scale-no-op")])
    assert payload["meta"]["protocol_version"] == EVAL_PROTOCOL_VERSION


async def test_run_payload_publishes_the_selected_case_pack_identity(tmp_path: Path) -> None:
    journeys = [journey for journey in _JOURNEYS if journey.id in {"scale-no-op", "restart-denied"}]
    runs = [await _run(journey.id, tmp_path) for journey in journeys]
    payload = run_payload(runs, journeys=journeys)
    case_pack = payload["meta"]["operation_case_pack"]
    assert case_pack["operation_ids"] == ["restart-denied", "scale-no-op"]
    assert case_pack["count"] == 2
    assert isinstance(case_pack["sha256"], str)
    assert len(case_pack["sha256"]) == 64


async def test_run_payload_has_one_operations_entry_per_selected_journey(tmp_path: Path) -> None:
    journeys = [journey for journey in _JOURNEYS if journey.id in {"scale-no-op", "restart-denied"}]
    runs = [await _run(journey.id, tmp_path) for journey in journeys]
    payload = run_payload(runs, journeys=journeys)
    assert [operation["journey_id"] for operation in payload["operations"]] == [
        "restart-denied",
        "scale-no-op",
    ]


async def test_run_payload_publishes_journal_audit_decisions_and_grade(tmp_path: Path) -> None:
    run = await _run("scale-deployment-up", tmp_path)
    payload = run_payload(
        [run], journeys=[next(j for j in _JOURNEYS if j.id == "scale-deployment-up")]
    )
    entry = payload["operations"][0]["runs"][0]
    assert entry["grade"]["outcome"] == "completed"
    assert entry["grade"]["safe"] is True
    assert entry["decisions"] == [{"outcome": "approve", "decision_source": "scripted_policy"}]
    assert isinstance(entry["journal"], list)
    assert entry["journal"]
    assert isinstance(entry["audit"], list)
    assert entry["audit"]
    assert entry["prompt"]["pack"]
    assert entry["wall_time_s"] >= 0.0


async def test_run_payload_is_json_serializable(tmp_path: Path) -> None:
    import json

    run = await _run("scale-no-op", tmp_path)
    payload = run_payload([run], journeys=[next(j for j in _JOURNEYS if j.id == "scale-no-op")])
    json.dumps(payload)  # must not raise


# --- policy grounding: meta.policy and meta.prompts must agree -------------
#
# `_resolve_policy` (mirroring `korvid.evals.__main__._resolve_policy`) must
# route and *ground* the policy once, before it is handed to both
# `policy_payload` and `prompt_fingerprint`: grounding only ever adds the
# `eval-overlay` id when `grind.overlay` is set, so an ungrounded policy
# publishes `meta.policy.overlays == []` while `meta.prompts.overlays`
# (which grounds internally) publishes `["eval-overlay"]` for the exact
# same run - a reproducible cross-field inconsistency an external
# optimizer parsing both fields could not reconcile.


async def test_resolve_policy_grounds_once_so_policy_and_prompts_overlays_agree() -> None:
    from korvid.evals.__main__ import policy_payload, prompt_fingerprint, tools_payload
    from korvid.evals.harness import PromptGrind, ground_eval_policy
    from korvid.evals.operation_main import _resolve_policy

    grind = PromptGrind(tier_pack="be extra careful with scale requests", overlay="double-check")
    provider = ScriptedProvider(OPERATION_SCRIPTS["scale-no-op"])
    policy = _resolve_policy(provider, model_tier=None, grind=grind)

    published_policy = policy_payload(policy)
    published_prompts = prompt_fingerprint(policy, grind=grind)
    assert published_policy["overlays"] == published_prompts["overlays"] == ["eval-overlay"]
    assert published_policy["prompt_pack"] == published_prompts["pack"]

    # The tool surface a write-capable operation journey needs stays armed;
    # grounding only ever touches prompt_overlay_ids, never tool arming.
    assert "scale_resource" in tools_payload(policy, [])["armed"]
    assert "rollout_restart" in tools_payload(policy, [])["armed"]

    # Grounding is idempotent: re-grounding an already-ground policy must
    # not name the overlay twice.
    assert ground_eval_policy(policy, grind).prompt_overlay_ids == policy.prompt_overlay_ids


async def test_resolve_policy_without_a_grind_overlay_publishes_no_overlay() -> None:
    """Backward-compatible default: an unselected/no-overlay run's
    `meta.policy.overlays` stays `[]`, matching pre-existing behavior."""
    from korvid.evals.__main__ import policy_payload, prompt_fingerprint
    from korvid.evals.operation_main import _resolve_policy

    provider = ScriptedProvider(OPERATION_SCRIPTS["scale-no-op"])
    policy = _resolve_policy(provider, model_tier=None, grind=NO_GRIND)

    assert policy_payload(policy)["overlays"] == []
    assert prompt_fingerprint(policy, grind=NO_GRIND)["overlays"] == []


# --- main(): exact process exit codes -----------------------------------
#
# `main()`'s own module docstring documents `2` for a usage/argument error
# and `1` for a systemic/harness error - but `_select_operation_journeys`/
# `_read_prompt_file`/`provider_factory_from_env` all raise `SystemExit`
# with a *string* argument, and `sys.exit("...")` only ever prints that
# string and exits `1`, never the promised `2`, when left uncaught. These
# run the real CLI end-to-end (as a subprocess, mirroring how an external
# optimizer actually invokes it) specifically to prove the promised process
# exit code, not just the constituent functions' own `SystemExit` raise.


def _run_cli(args: list[str], env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "korvid.evals.operation_main", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


_VALID_ENV = {
    "KORVID_EVAL_PROVIDER": "openai-compat",
    "KORVID_EVAL_BASE_URL": "http://127.0.0.1:1",
    "KORVID_EVAL_MODEL": "does-not-matter",
}


def test_main_exits_2_for_an_unknown_operation_id() -> None:
    result = _run_cli(["--operation-id", "does-not-exist"], _VALID_ENV)
    assert result.returncode == 2
    assert "does-not-exist" in result.stderr


def test_main_exits_2_for_a_missing_tier_pack_file(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-file.txt"
    result = _run_cli(
        ["--operation-id", "scale-no-op", "--tier-pack-file", str(missing)], _VALID_ENV
    )
    assert result.returncode == 2
    assert "--tier-pack-file" in result.stderr


def test_main_exits_2_for_an_invalid_provider_env_value() -> None:
    env = {**_VALID_ENV, "KORVID_EVAL_PROVIDER": "not-a-real-provider"}
    result = _run_cli(["--operation-id", "scale-no-op"], env)
    assert result.returncode == 2
    assert "KORVID_EVAL_PROVIDER" in result.stderr


def test_main_exits_1_when_the_result_json_cannot_be_written(tmp_path: Path) -> None:
    """`http://127.0.0.1:1` (nothing listens on port 1, an instant local
    connection refusal - not a live endpoint) still lets a run reach a
    graded 'unknown' outcome: this harness already treats a provider-stream
    failure as scored evidence, exit `0`, matching its own documented
    philosophy. The one deterministic, network-free systemic failure this
    CLI still exercises is `--json` naming a path that cannot be written."""
    bad_path = tmp_path / "no-such-directory" / "out.json"
    result = _run_cli(["--operation-id", "scale-no-op", "--json", str(bad_path)], _VALID_ENV)
    assert result.returncode == 1
    assert not bad_path.exists()
