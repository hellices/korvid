"""Tests for the live-eval CLI plumbing (issue #69).

Only the offline parts are tested — env-based provider configuration and
report serialization. The live model round-trip is by definition manual.
"""

from __future__ import annotations

import argparse
import json

import pytest

from korvid.evals.__main__ import (
    _parse_args,
    _positive_int,
    exit_code,
    provider_factory_from_env,
    report_payload,
)
from korvid.evals.grader import GradeResult
from korvid.evals.runner import RunMetrics, ScenarioReport
from korvid.providers.openai_compat import OpenAICompatProvider


def test_provider_factory_requires_base_url_and_model() -> None:
    with pytest.raises(SystemExit, match="KORVID_EVAL_BASE_URL"):
        provider_factory_from_env({"KORVID_EVAL_MODEL": "m"})
    with pytest.raises(SystemExit, match="KORVID_EVAL_MODEL"):
        provider_factory_from_env({"KORVID_EVAL_BASE_URL": "http://localhost:1234/v1"})


@pytest.mark.parametrize("value", ["0", "-1"])
def test_reps_rejects_non_positive_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="at least 1"):
        _positive_int(value)


def test_reps_accepts_positive_values() -> None:
    assert _positive_int("3") == 3


def test_provider_factory_builds_a_fresh_openai_compat_provider() -> None:
    factory = provider_factory_from_env(
        {
            "KORVID_EVAL_BASE_URL": "http://localhost:1234/v1",
            "KORVID_EVAL_MODEL": "test-model",
            "KORVID_EVAL_API_KEY": "sk-test",
        }
    )
    first = factory()
    second = factory()
    assert isinstance(first, OpenAICompatProvider)
    assert first is not second  # fresh provider per run


def test_provider_factory_applies_eval_timeout() -> None:
    provider = provider_factory_from_env(
        {
            "KORVID_EVAL_BASE_URL": "http://localhost:1234/v1",
            "KORVID_EVAL_MODEL": "large-local-model",
            "KORVID_EVAL_TIMEOUT_SECONDS": "900",
        }
    )()
    assert provider._get_client().timeout.read == 900.0


@pytest.mark.parametrize("value", ["0", "-1", "nope", "nan", "inf"])
def test_provider_factory_rejects_invalid_eval_timeout(value: str) -> None:
    with pytest.raises(SystemExit, match="KORVID_EVAL_TIMEOUT_SECONDS"):
        provider_factory_from_env(
            {
                "KORVID_EVAL_BASE_URL": "http://localhost:1234/v1",
                "KORVID_EVAL_MODEL": "large-local-model",
                "KORVID_EVAL_TIMEOUT_SECONDS": value,
            }
        )


def _report(error: str | None = None) -> ScenarioReport:
    grade = GradeResult(
        diagnosis_success=True,
        evidence_fetched=True,
        missing_mentions=(),
        forbidden_mentions=(),
        missing_evidence=(),
    )
    run = RunMetrics(
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
    )
    return ScenarioReport(scenario_id="oom-killed", root_cause="oom_killed", runs=[run])


def test_report_payload_is_json_serializable_with_summary_counts() -> None:
    payload = report_payload([_report()])
    text = json.dumps(payload)
    assert '"scenario": "oom-killed"' in text
    assert payload[0]["successes"] == 1
    assert payload[0]["evidence_hits"] == 1
    assert payload[0]["runs"][0]["grade"]["diagnosis_success"] is True


def test_exit_code_is_zero_for_a_clean_evaluation() -> None:
    assert exit_code([_report()]) == 0


def test_exit_code_is_nonzero_when_any_run_errored(capsys: pytest.CaptureFixture[str]) -> None:
    """A failed evaluation (e.g. unreachable endpoint) must be
    distinguishable from a completed one, and the underlying reason must
    surface on stderr (the markdown report only carries aggregate counts)."""
    assert exit_code([_report(), _report(error="connection refused")]) == 1
    stderr = capsys.readouterr().err
    assert "oom-killed: connection refused" in stderr


def test_profile_flag_defaults_to_full_and_rejects_unknown_values() -> None:
    """`--profile` selects which capability tier the pack measures
    (issue #71); anything but the two real profiles is a usage error."""
    assert _parse_args([]).profile == "full"
    assert _parse_args(["--profile", "small"]).profile == "small"
    with pytest.raises(SystemExit, match="2"):
        _parse_args(["--profile", "huge"])
