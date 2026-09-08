"""Tests for the live-eval CLI plumbing (issue #69).

Only the offline parts are tested — env-based provider configuration and
report serialization. The live model round-trip is by definition manual.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from korvid.agent import prompt_harness, prompt_packs
from korvid.evals.__main__ import (
    DEFAULT_EVAL_TIMEOUT_SECONDS,
    eval_api_key,
    eval_model_tag,
    exit_code,
    prompt_fingerprint,
    provider_factory_from_env,
    report_payload,
    run_payload,
)
from korvid.evals.grader import CitationReport, GradeResult, citation_report
from korvid.evals.harness import PromptGrind, resolve_eval_policy
from korvid.evals.runner import RunMetrics, ScenarioReport
from korvid.evals.scripted import ScriptedProvider
from korvid.providers.litellm_provider import LiteLLMProvider
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


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"KORVID_EVAL_MODEL": "m"}, "KORVID_EVAL_BASE_URL"),
        ({"KORVID_EVAL_BASE_URL": "http://localhost/v1"}, "KORVID_EVAL_MODEL"),
        (
            {
                "KORVID_EVAL_BASE_URL": "http://localhost/v1",
                "KORVID_EVAL_MODEL": "m",
                "KORVID_EVAL_PROVIDER": "unknown",
            },
            # The prefix is refused by routing the reference it composes,
            # not by a vendor list korvid keeps.
            "unknown/m",
        ),
        (
            {
                "KORVID_EVAL_BASE_URL": "http://localhost/v1",
                "KORVID_EVAL_MODEL": "m",
                "KORVID_EVAL_TIMEOUT_SECONDS": "nan",
            },
            "KORVID_EVAL_TIMEOUT_SECONDS",
        ),
    ],
)
def test_provider_factory_rejects_invalid_environment(
    env: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(SystemExit, match=message):
        provider_factory_from_env(env)


def _shipped_provider(env: dict[str, str]) -> LiteLLMProvider:
    """Build the eval provider and narrow it to the type the product ships."""
    provider = provider_factory_from_env(env)()
    assert isinstance(provider, LiteLLMProvider)
    return provider


def test_provider_factory_defaults_the_eval_timeout() -> None:
    """An unset timeout is still bound — a local model must not hang forever."""
    provider = _shipped_provider(
        {
            "KORVID_EVAL_BASE_URL": "http://localhost:1234/v1",
            "KORVID_EVAL_MODEL": "openai/large-local-model",
        }
    )

    assert provider._plan.timeout == DEFAULT_EVAL_TIMEOUT_SECONDS


# --- the timeout is one rule, however it is spelled -------------------------
#
# `KORVID_EVAL_OPTIONS_JSON` carries profile options verbatim, so it can
# also carry `timeout`. A value that is not a duration used to suppress the
# eval default here and then be dropped by `build_plan`'s own strictness,
# leaving the run with *no* bound at all — the opposite of what an operator
# who wrote a timeout was asking for.


@pytest.mark.parametrize(
    "value",
    [0, -1, True, float("nan"), float("inf"), "900", None, [900]],
    ids=["zero", "negative", "bool", "nan", "inf", "string", "null", "list"],
)
def test_provider_factory_rejects_an_unusable_timeout_in_the_options_json(
    value: object,
) -> None:
    """Refused where the operator can still fix it, and named so they can."""
    with pytest.raises(SystemExit, match="KORVID_EVAL_OPTIONS_JSON") as refusal:
        provider_factory_from_env(
            {
                "KORVID_EVAL_BASE_URL": "http://localhost:1234/v1",
                "KORVID_EVAL_MODEL": "openai/large-local-model",
                "KORVID_EVAL_OPTIONS_JSON": json.dumps({"timeout": value}),
            }
        )
    message = str(refusal.value)
    assert "timeout" in message
    assert "positive" in message


def test_a_timeout_in_the_options_json_bounds_the_request() -> None:
    """A usable JSON timeout reaches the shared plan as a number."""
    provider = _shipped_provider(
        {
            "KORVID_EVAL_BASE_URL": "http://localhost:1234/v1",
            "KORVID_EVAL_MODEL": "openai/large-local-model",
            "KORVID_EVAL_OPTIONS_JSON": json.dumps({"timeout": 120}),
        }
    )

    assert provider._plan.timeout == 120.0


def test_the_timeout_variable_wins_over_the_options_json() -> None:
    """Documented precedence: the explicit variable, then the JSON, then the default."""
    provider = _shipped_provider(
        {
            "KORVID_EVAL_BASE_URL": "http://localhost:1234/v1",
            "KORVID_EVAL_MODEL": "openai/large-local-model",
            "KORVID_EVAL_OPTIONS_JSON": json.dumps({"timeout": 120}),
            "KORVID_EVAL_TIMEOUT_SECONDS": "900",
        }
    )

    assert provider._plan.timeout == 900.0


def test_both_timeout_spellings_are_refused_the_same_way() -> None:
    """One rule, one wording — only the source that carried it differs."""
    common = {
        "KORVID_EVAL_BASE_URL": "http://localhost:1234/v1",
        "KORVID_EVAL_MODEL": "openai/large-local-model",
    }
    with pytest.raises(SystemExit) as from_variable:
        provider_factory_from_env({**common, "KORVID_EVAL_TIMEOUT_SECONDS": "0"})
    with pytest.raises(SystemExit) as from_json:
        provider_factory_from_env({**common, "KORVID_EVAL_OPTIONS_JSON": '{"timeout": 0}'})

    variable_message = str(from_variable.value)
    json_message = str(from_json.value)
    assert variable_message.startswith("KORVID_EVAL_TIMEOUT_SECONDS")
    assert json_message.startswith("KORVID_EVAL_OPTIONS_JSON")
    tail = "must be a positive, finite number of seconds"
    assert tail in variable_message
    assert tail in json_message


# --- the probe asks the endpoint about the model the endpoint has -----------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"KORVID_EVAL_MODEL": "ollama/qwen3:8b"}, "qwen3:8b"),
        ({"KORVID_EVAL_PROVIDER": "ollama", "KORVID_EVAL_MODEL": "qwen3:8b"}, "qwen3:8b"),
        ({"KORVID_EVAL_MODEL": "openai/gpt-4o"}, "gpt-4o"),
        ({"KORVID_EVAL_MODEL": "qwen3:8b"}, "qwen3:8b"),
        ({"KORVID_EVAL_MODEL": "openrouter/qwen/qwen3-8b"}, "qwen/qwen3-8b"),
    ],
    ids=["prefixed", "legacy-prefix-variable", "other-vendor", "bare", "nested"],
)
def test_the_probe_uses_the_tag_the_serving_endpoint_knows(
    env: dict[str, str], expected: str
) -> None:
    """The routing prefix is korvid's, not the server's.

    `/api/show` and `/api/tags` answer about `qwen3:8b`; asking them about
    `ollama/qwen3:8b` returns nothing, so digest, quantization and context
    length silently go unpinned. The reference is split by the shared
    `split_reference`, which takes no vendor branch.
    """
    assert eval_model_tag(env) == expected


def test_the_campaign_probes_with_the_tag_rather_than_the_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`main` hands the probe the endpoint's own name for the model."""
    from korvid.evals import __main__ as cli

    probed: list[tuple[str, str]] = []

    async def fake_capture(base_url: str, model: str, **kwargs: Any) -> dict[str, Any]:
        probed.append((base_url, model))
        return {"unavailable": []}

    monkeypatch.setenv("KORVID_EVAL_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("KORVID_EVAL_MODEL", "ollama/qwen3:8b")
    monkeypatch.setattr(cli, "capture_serving", fake_capture)
    monkeypatch.setattr(cli, "provider_factory_from_env", lambda env: lambda: None)
    monkeypatch.setattr(cli, "load_scenarios", lambda path: ["scenario"])
    monkeypatch.setattr(cli, "_resolve_policy", lambda factory, args, grind: _policy())

    async def fake_run_all(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(cli, "_run_all", fake_run_all)
    monkeypatch.setattr(cli, "render_markdown", lambda reports: "")

    assert cli.main(["--scenarios", str(tmp_path)]) == 0
    assert probed == [("http://localhost:11434/v1", "qwen3:8b")]


def test_serving_probe_reads_the_named_credential_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe follows the profile's convention rather than its own."""
    monkeypatch.setenv("EVAL_TOKEN", "sk-probe")

    assert eval_api_key({"KORVID_EVAL_API_KEY_ENV": "EVAL_TOKEN"}) == "sk-probe"


def test_serving_probe_falls_back_to_the_deprecated_variable() -> None:
    assert eval_api_key({"KORVID_EVAL_API_KEY": "sk-legacy"}) == "sk-legacy"


def test_serving_probe_has_no_credential_when_none_is_configured() -> None:
    assert eval_api_key({}) == ""


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


def test_prompt_source_reflects_the_effective_prompt_not_the_flag() -> None:
    """`source` decides publishability: a campaign run against a ground
    prompt must not publish provenance claiming the shipped prompts.

    Both polarities are asserted here because either constant — a
    hard-coded `"default"`, or `"override"` whenever a grind was supplied —
    makes the published attribution wrong.
    """
    reproduces_the_pack = PromptGrind(tier_pack=prompt_packs.LOW_KORVID_OPERATOR_PACK)
    differs = PromptGrind(tier_pack="You are terse.")

    assert prompt_fingerprint(_policy(), grind=reproduces_the_pack)["source"] == "default"
    assert prompt_fingerprint(_policy(), grind=differs)["source"] == "override"
    payload = run_payload([_report()], policy=_policy(), grind=differs)
    assert payload["meta"]["prompts"]["source"] == "override"
    assert len(payload["meta"]["prompts"]["sha256"]) == 64


def test_prompt_fingerprint_covers_the_composed_prompt_not_just_the_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest must identify the actual model input.

    The safety contract and the armed-capability clauses are composed onto
    every request; a digest over the tier pack alone would call two
    behaviourally different runs comparable.
    """
    policy = _policy()
    before = prompt_fingerprint(policy)["sha256"]
    monkeypatch.setattr(prompt_harness, "SAFETY_CONTRACT", "Reworded safety contract.")
    assert prompt_fingerprint(policy)["sha256"] != before
