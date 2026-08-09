"""Conversation journey CLI tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from korvid.evals.grader import GradeResult
from korvid.evals.journey_runner import (
    JourneyReport,
    JourneyRun,
    JourneyTurnResult,
)
from korvid.evals.journeys_cli import _parse_args, _run, exit_code, journey_run_payload


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
    assert args.journeys.name == "live_journeys"


async def test_journey_cli_rejects_empty_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KORVID_EVAL_BASE_URL", "http://localhost:1/v1")
    monkeypatch.setenv("KORVID_EVAL_MODEL", "model")
    args = _parse_args(["--journeys", str(tmp_path)])
    with pytest.raises(SystemExit, match="no journey YAML files"):
        await _run(args)


async def test_live_cli_closes_environment_when_retargeting_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "cluster-wide.yaml").write_text(
        """
id: cluster-wide
root_cause: none
turns:
  - user: inspect
    screen: nodes
    grading:
      must_mention: [healthy]
      must_not_mention: [broken]
      expected_evidence:
        - tool: list_resources
          args: {kind: nodes}
          contains: node
  - user: stop
    screen: nodes
    grading:
      must_mention: [stop]
      must_not_mention: [broken]
      expected_evidence:
        - tool: list_resources
          args: {kind: nodes}
          contains: node
cluster: {objects: [], events: [], logs: {}}
"""
    )
    monkeypatch.setenv("KORVID_EVAL_BASE_URL", "http://localhost:1/v1")
    monkeypatch.setenv("KORVID_EVAL_MODEL", "model")

    class FakeLiveEnvironment:
        closed = False
        last: ClassVar[FakeLiveEnvironment | None] = None

        @classmethod
        async def connect(cls, context: str, namespace: str) -> FakeLiveEnvironment:
            cls.last = cls()
            return cls.last

        async def close(self) -> None:
            self.closed = True

    import korvid.evals.live_journey as live_module

    monkeypatch.setattr(live_module, "LiveJourneyEnvironment", FakeLiveEnvironment)
    args = _parse_args(
        [
            "--live",
            "--context",
            "aks-korvid-contract-test",
            "--namespace",
            "korvid-agent-eval-run",
            "--journeys",
            str(tmp_path),
        ]
    )
    with pytest.raises(ValueError, match="no namespaced evidence"):
        await _run(args)
    assert FakeLiveEnvironment.last is not None
    assert FakeLiveEnvironment.last.closed is True


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
        wrong_namespace_calls=0,
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


def test_journey_json_records_prompt_provenance(tmp_path: Path) -> None:
    """Journey runs become published scoreboard rows too (#176 tier 2), so
    they must say which prompt and tool schemas produced them."""
    payload = journey_run_payload([], profile_name="small")
    assert payload["meta"]["profile"] == "small"
    assert payload["meta"]["prompts"]["source"] == "default"
    assert len(payload["meta"]["prompts"]["sha256"]) == 64
    assert payload["journeys"] == []
    json.dumps(payload)
