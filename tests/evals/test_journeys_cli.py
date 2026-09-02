"""Conversation journey CLI tests."""

from __future__ import annotations

import json
from typing import Any

import pytest

from korvid.evals.grader import GradeResult
from korvid.evals.harness import resolve_eval_policy
from korvid.evals.interaction import interaction_payload
from korvid.evals.journey_runner import JourneyReport, JourneyRun, JourneyTurnResult
from korvid.evals.journeys_cli import exit_code, journey_run_payload
from korvid.evals.scripted import ScriptedProvider
from tests.evals.fixtures import EVAL_INTERACTION, eval_interaction


def _policy(**kwargs: Any) -> Any:
    return resolve_eval_policy(ScriptedProvider([[{"type": "done"}]]), **kwargs)


def _turn_result(**overrides: Any) -> JourneyTurnResult:
    fields: dict[str, Any] = {
        "answer": "payments needs registry credentials",
        "grade": GradeResult(True, True, (), (), ()),
        "tool_calls": 1,
        "tool_names": ("diagnose_pod",),
        "malformed_tool_calls": 0,
        "write_attempts": 0,
        "safety_violations": 0,
        "forbidden_target_calls": 0,
        "wrong_namespace_calls": 0,
        "error": None,
        "wall_time_s": 1.0,
        "interaction": EVAL_INTERACTION,
        "final_interaction": eval_interaction(scope="jobs"),
        "outcome": "success",
        "failure_class": None,
    }
    fields.update(overrides)
    return JourneyTurnResult(**fields)


def _journey_report(**overrides: Any) -> JourneyReport:
    fields: dict[str, Any] = {
        "journey_id": "triage-and-correct",
        "root_cause": "image_pull_auth",
        "runs": (
            JourneyRun(
                turns=(_turn_result(),),
                input_tokens=10,
                output_tokens=5,
                tokens_estimated=False,
            ),
        ),
        "interaction": EVAL_INTERACTION,
    }
    fields.update(overrides)
    return JourneyReport(**fields)


def test_journey_exit_code_uses_clean_and_error_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean = _journey_report()
    assert exit_code([clean]) == 0

    errored_turn = _turn_result(
        error="ReadTimeout", outcome="error", failure_class="provider_error"
    )
    report = _journey_report(
        journey_id="triage",
        root_cause="none",
        runs=(
            JourneyRun(
                turns=(errored_turn,),
                input_tokens=0,
                output_tokens=0,
                tokens_estimated=True,
            ),
        ),
    )
    assert exit_code([report]) == 1
    assert "triage run 1 turn 1: ReadTimeout" in capsys.readouterr().err


def test_journey_json_records_run_metadata() -> None:
    low_payload = journey_run_payload([], policy=_policy())
    high_payload = journey_run_payload([], policy=_policy(model_tier="high"))

    assert low_payload["meta"]["policy"] == {
        "provider": "scripted",
        "model": "scripted",
        "tier": "low",
        "route_source": "fallback",
        "prompt_pack": "low-korvid-operator",
        "overlays": [],
    }
    assert low_payload["meta"]["limits"]["max_tool_calls_per_iteration"] == 1
    assert low_payload["meta"]["prompts"]["source"] == "default"
    assert len(low_payload["meta"]["prompts"]["sha256"]) == 64
    armed = low_payload["meta"]["tools"]["armed"]
    assert "open_describe" in armed
    assert not {"scale_resource", "delete_resource"} & set(armed)
    assert low_payload["meta"]["prompts"]["sha256"] != high_payload["meta"]["prompts"]["sha256"]
    assert low_payload["journeys"] == []
    json.dumps(low_payload)


def test_journey_payload_publishes_interactions_and_verdicts() -> None:
    payload = journey_run_payload([_journey_report()], policy=_policy())
    row = payload["journeys"][0]
    turn = row["runs"][0]["turns"][0]

    assert row["interaction"] == interaction_payload(EVAL_INTERACTION)
    assert set(row["interaction"]["focused_pane"]) == {"kind", "scope", "filter", "selected"}
    assert turn["interaction"] == interaction_payload(EVAL_INTERACTION)
    assert turn["final_interaction"] == interaction_payload(eval_interaction(scope="jobs"))
    assert turn["outcome"] == "success"
    assert turn["failure_class"] is None
    json.dumps(payload)


def test_journey_payload_counts_all_turn_success_and_turn_flags() -> None:
    success_row = journey_run_payload([_journey_report()], policy=_policy())["journeys"][0]
    mixed_report = _journey_report(
        runs=(
            JourneyRun(
                turns=(
                    _turn_result(),
                    _turn_result(outcome="failure", failure_class="missing_evidence"),
                    _turn_result(
                        outcome="error", error="ReadTimeout", failure_class="provider_error"
                    ),
                ),
                input_tokens=10,
                output_tokens=5,
                tokens_estimated=False,
            ),
        )
    )
    mixed_row = journey_run_payload([mixed_report], policy=_policy())["journeys"][0]

    assert success_row["successful_journeys"] == 1
    assert mixed_report.successful_journeys == 0
    assert mixed_row["successful_journeys"] == 0
    assert "successes" not in mixed_row
    assert [turn["success"] for turn in mixed_row["runs"][0]["turns"]] == [True, False, False]


def test_a_journey_run_with_no_turns_is_not_a_success() -> None:
    run = JourneyRun(turns=(), input_tokens=0, output_tokens=0, tokens_estimated=True)

    assert run.success is False
