"""Tests for the live-eval CLI plumbing (issue #69).

Only the offline parts are tested — env-based provider configuration and
report serialization. The live model round-trip is by definition manual.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from korvid.evals.__main__ import (
    exit_code,
    prompt_fingerprint,
    report_payload,
    run_payload,
)
from korvid.evals.grader import CitationReport, GradeResult, citation_report
from korvid.evals.harness import PromptGrind, resolve_eval_policy
from korvid.evals.runner import RunMetrics, ScenarioReport
from korvid.evals.scripted import ScriptedProvider
from tests.evals.fixtures import EVAL_INTERACTION


def _no_citations() -> CitationReport:
    return citation_report("", minted=())


def _report(error: str | None = None) -> ScenarioReport:
    grade = GradeResult(
        diagnosis_success=True,
        evidence_fetched=True,
        missing_mentions=(),
        forbidden_mentions=(),
        missing_evidence=(),
    )
    run = RunMetrics(
        citations=_no_citations(),
        grade=grade,
        answer="OOMKilled, exit 137",
        iterations=2,
        tool_calls=1,
        resolvable_tool_calls=1,
        on_target_tool_calls=1,
        malformed_tool_calls=0,
        write_attempts=0,
        safety_violations=0,
        input_tokens=100,
        output_tokens=20,
        tokens_estimated=False,
        wall_time_s=1.5,
        error=error,
        outcome="error" if error else "success",
        failure_class="provider_error" if error else None,
    )
    return ScenarioReport(
        scenario_id="oom-killed",
        root_cause="oom_killed",
        runs=[run],
        interaction=EVAL_INTERACTION,
    )


def _policy(**kwargs: Any) -> Any:
    return resolve_eval_policy(ScriptedProvider([[{"type": "done"}]]), **kwargs)


def test_report_payload_is_json_serializable_with_summary_counts() -> None:
    payload = report_payload([_report()])
    text = json.dumps(payload)
    assert '"scenario": "oom-killed"' in text
    assert payload[0]["successes"] == 1
    assert payload[0]["evidence_hits"] == 1
    assert payload[0]["runs"][0]["grade"]["diagnosis_success"] is True


def test_exit_code_uses_clean_and_error_paths(capsys: pytest.CaptureFixture[str]) -> None:
    assert exit_code([_report()]) == 0
    assert exit_code([_report(), _report(error="connection refused")]) == 1
    stderr = capsys.readouterr().err
    assert "oom-killed: connection refused" in stderr
    assert "1 run(s) errored." in stderr


def test_run_payload_records_resolved_metadata() -> None:
    policy = _policy()
    payload = run_payload([_report()], policy=policy)

    assert payload["meta"]["policy"] == {
        "provider": "scripted",
        "model": "scripted",
        "tier": "low",
        "route_source": "fallback",
        "prompt_pack": "low-korvid-operator",
        "overlays": [],
    }
    assert payload["meta"]["limits"] == {
        "max_iterations": 6,
        "max_history_chars": 24_000,
        "max_result_chars": 3_000,
        "max_tool_calls_per_iteration": 1,
        "allow_parallel_tool_calls": False,
        "strict_history_budget": True,
    }
    capabilities = payload["meta"]["capabilities"]
    assert set(capabilities) == {
        "context_window_tokens",
        "supports_tools",
        "supports_parallel_tools",
        "supports_reasoning",
        "recommended_tier",
        "provenance",
    }
    assert payload["meta"]["prompts"] == {
        "pack": "low-korvid-operator",
        "overlays": [],
        "source": "default",
        "sha256": payload["meta"]["prompts"]["sha256"],
    }
    assert len(payload["meta"]["prompts"]["sha256"]) == 64
    armed = payload["meta"]["tools"]["armed"]
    assert payload["meta"]["tools"]["count"] == len(armed)
    assert armed == sorted(tool["function"]["name"] for tool in policy.tools)
    assert "scale_resource" not in armed
    row = payload["scenarios"][0]
    assert row["scenario"] == "oom-killed"
    assert row["interaction"]["kube_context"] == "eval-cluster"
    assert row["interaction"]["focused_pane"]["kind"] == "pods"
    assert row["max_tool_calls"] == 1
    json.dumps(payload)


def test_run_payload_records_outcome_and_failure_class_per_run() -> None:
    payload = run_payload([_report(), _report(error="connection refused")], policy=_policy())
    assert payload["scenarios"][0]["runs"][0]["outcome"] == "success"
    assert payload["scenarios"][0]["runs"][0]["failure_class"] is None
    assert payload["scenarios"][1]["runs"][0]["outcome"] == "error"
    assert payload["scenarios"][1]["runs"][0]["failure_class"] == "provider_error"


def test_prompt_fingerprint_is_stable_and_changes_with_the_prompt() -> None:
    policy = _policy()
    first = prompt_fingerprint(policy)["sha256"]
    again = prompt_fingerprint(policy)["sha256"]
    changed = prompt_fingerprint(policy, grind=PromptGrind(tier_pack="Be terse."))["sha256"]
    assert first == again
    assert first != changed
