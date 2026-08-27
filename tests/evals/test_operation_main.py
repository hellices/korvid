"""Tests for the public, TUI-free operation-journey CLI.

Mirrors `tests/evals/test_cli.py`'s own approach for `python -m
korvid.evals`: `main()` itself is exercised only through a manual smoke
test (see the plan), while every constituent piece — argument parsing,
selection, and `run_payload`'s exact JSON shape — is unit tested directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from korvid.evals.__main__ import EVAL_PROTOCOL_VERSION
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
