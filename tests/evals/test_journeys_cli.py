"""Conversation journey CLI tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from korvid.evals.grader import GradeResult
from korvid.evals.journey_runner import (
    JourneyReport,
    JourneyRun,
    JourneyTurnResult,
)
from korvid.evals.journeys_cli import _parse_args, _run, exit_code


def test_journey_cli_defaults_to_bundled_pack_and_three_reps() -> None:
    args = _parse_args([])
    assert args.reps == 3
    assert args.profile == "small"
    assert args.live is False


def test_journey_cli_accepts_live_mode_and_outputs() -> None:
    args = _parse_args(
        [
            "--live",
            "--reps",
            "1",
            "--out",
            "report.md",
            "--json",
            "report.json",
        ]
    )
    assert args.live is True
    assert args.out.name == "report.md"
    assert args.json.name == "report.json"


async def test_journey_cli_rejects_empty_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KORVID_EVAL_BASE_URL", "http://localhost:1/v1")
    monkeypatch.setenv("KORVID_EVAL_MODEL", "model")
    args = _parse_args(["--journeys", str(tmp_path)])
    with pytest.raises(SystemExit, match="no journey YAML files"):
        await _run(args)


def test_journey_exit_code_prints_turn_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    grade = GradeResult(True, True, (), (), ())
    turn = JourneyTurnResult(
        answer="",
        grade=grade,
        success=False,
        tool_calls=0,
        tool_names=(),
        malformed_tool_calls=0,
        write_attempts=0,
        safety_violations=0,
        forbidden_target_calls=0,
        error="ReadTimeout",
        wall_time_s=60.0,
    )
    report = JourneyReport(
        journey_id="triage",
        root_cause="none",
        runs=(
            JourneyRun(
                success=False,
                turns=(turn,),
                input_tokens=0,
                output_tokens=0,
                tokens_estimated=True,
            ),
        ),
    )
    assert exit_code([report]) == 1
    assert "triage run 1 turn 1: ReadTimeout" in capsys.readouterr().err
