"""Tests for the live-eval CLI plumbing (issue #69).

Only the offline parts are tested — env-based provider configuration and
report serialization. The live model round-trip is by definition manual.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from korvid.agent import prompt_harness, prompt_packs
from korvid.evals.__main__ import (
    _parse_args,
    _positive_int,
    _prompt_grind,
    _resolve_policy,
    capture_serving,
    exit_code,
    probe_serving,
    prompt_fingerprint,
    provider_factory_from_env,
    report_payload,
    run_payload,
    warm_up,
    warn_if_unpinned,
)
from korvid.evals.grader import CitationReport, GradeResult, citation_report
from korvid.evals.harness import PromptGrind, resolve_eval_policy
from korvid.evals.runner import RunMetrics, ScenarioReport
from korvid.evals.scripted import ScriptedProvider
from korvid.evals.serving import ProbeResult, serving_metadata
from korvid.providers.ollama import OllamaProvider
from korvid.providers.openai_compat import OpenAICompatProvider
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


def _ground(grind: PromptGrind) -> Any:
    from korvid.evals.harness import ground_eval_policy

    return ground_eval_policy(_policy(), grind)


def test_provider_factory_requires_base_url_and_model() -> None:
    with pytest.raises(SystemExit, match="KORVID_EVAL_BASE_URL"):
        provider_factory_from_env({"KORVID_EVAL_MODEL": "m"})


def test_reps_validation() -> None:
    assert _positive_int("3") == 3
    with pytest.raises(argparse.ArgumentTypeError, match="at least 1"):
        _positive_int("0")


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
    assert first is not second


def test_provider_factory_builds_ollama_with_its_catalog_identity() -> None:
    provider = provider_factory_from_env(
        {
            "KORVID_EVAL_PROVIDER": "ollama",
            "KORVID_EVAL_BASE_URL": "http://localhost:11434/v1",
            "KORVID_EVAL_MODEL": "qwen3:8b",
        }
    )()

    assert isinstance(provider, OllamaProvider)
    assert provider.descriptor.provider == "ollama"


def test_provider_factory_rejects_unknown_provider_identity() -> None:
    with pytest.raises(SystemExit, match="KORVID_EVAL_PROVIDER"):
        provider_factory_from_env(
            {
                "KORVID_EVAL_PROVIDER": "not-a-provider",
                "KORVID_EVAL_BASE_URL": "http://localhost:11434/v1",
                "KORVID_EVAL_MODEL": "qwen3:8b",
            }
        )


def test_automatic_eval_routing_uses_the_ollama_catalog_identity() -> None:
    factory = provider_factory_from_env(
        {
            "KORVID_EVAL_PROVIDER": "ollama",
            "KORVID_EVAL_BASE_URL": "http://localhost:11434/v1",
            "KORVID_EVAL_MODEL": "qwen3:8b",
        }
    )

    policy = _resolve_policy(factory, _parse_args([]))

    assert policy.route_source.value == "catalog"
    assert policy.catalog_version is not None


def test_provider_factory_rejects_invalid_eval_timeout() -> None:
    with pytest.raises(SystemExit, match="KORVID_EVAL_TIMEOUT_SECONDS"):
        provider_factory_from_env(
            {
                "KORVID_EVAL_BASE_URL": "http://localhost:1234/v1",
                "KORVID_EVAL_MODEL": "large-local-model",
                "KORVID_EVAL_TIMEOUT_SECONDS": "nan",
            }
        )


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


def test_model_tier_parsing_uses_automatic_default_and_real_tiers() -> None:
    assert _parse_args([]).model_tier is None
    assert _parse_args(["--model-tier", "low"]).model_tier == "low"
    assert _parse_args(["--model-tier", "high"]).model_tier == "high"


def test_cli_rejects_retired_profile_and_replace_flags() -> None:
    with pytest.raises(SystemExit, match="2"):
        _parse_args(["--model-tier", "full"])
    with pytest.raises(SystemExit, match="2"):
        _parse_args(["--profile", "small"])
    with pytest.raises(SystemExit, match="2"):
        _parse_args(["--system-prompt-file", "a.md"])


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


def test_run_payload_records_high_tier_budgets_when_routed_high() -> None:
    payload = run_payload([_report()], policy=_policy(model_tier="high"))
    assert payload["meta"]["policy"]["tier"] == "high"
    assert payload["meta"]["policy"]["route_source"] == "user"
    assert payload["meta"]["limits"]["max_iterations"] == 15
    assert payload["meta"]["limits"]["max_tool_calls_per_iteration"] is None


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


def test_prompt_fingerprint_covers_the_composed_prompt_not_just_the_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    before = prompt_fingerprint(policy)["sha256"]
    monkeypatch.setattr(prompt_harness, "SAFETY_CONTRACT", "Reworded safety contract.")
    assert prompt_fingerprint(policy)["sha256"] != before


def test_prompt_fingerprint_covers_parameter_schemas() -> None:
    policy = _policy()
    before = prompt_fingerprint(policy)["sha256"]
    narrowed = _policy(omit_tools=frozenset({"get_logs"}))
    assert prompt_fingerprint(narrowed)["sha256"] != before


def test_source_classifies_default_vs_override_prompts() -> None:
    same = PromptGrind(tier_pack=prompt_packs.LOW_KORVID_OPERATOR_PACK)
    different = PromptGrind(tier_pack="You are terse.")
    assert prompt_fingerprint(_policy(), grind=same)["source"] == "default"
    assert prompt_fingerprint(_policy(), grind=different)["source"] == "override"


def test_prompt_grind_flag_parsing_and_loading(tmp_path: Path) -> None:
    default_args = _parse_args([])
    assert default_args.tier_pack_file is None
    assert default_args.prompt_overlay_file is None
    assert default_args.warmup is False
    assert _parse_args(["--warmup"]).warmup is True

    pack = tmp_path / "pack.md"
    pack.write_text("Answer in one sentence.", encoding="utf-8")
    overlay = tmp_path / "overlay.md"
    overlay.write_text("Always name the namespace.", encoding="utf-8")
    args = _parse_args(["--tier-pack-file", str(pack), "--prompt-overlay-file", str(overlay)])

    assert args.tier_pack_file == pack
    assert args.prompt_overlay_file == overlay
    assert _prompt_grind(args) == PromptGrind(
        tier_pack="Answer in one sentence.", overlay="Always name the namespace."
    )


def test_prompt_grind_file_with_invalid_utf8_exits_cleanly(tmp_path: Path) -> None:
    bad = tmp_path / "prompt.md"
    bad.write_bytes(b"\xff\xfe not utf-8")
    args = _parse_args(["--tier-pack-file", str(bad)])
    with pytest.raises(SystemExit, match="--tier-pack-file"):
        _prompt_grind(args)


def test_run_payload_records_or_omits_serving() -> None:
    serving = serving_metadata(model="qwen3:8b", probe=ProbeResult(version={"version": "0.5.1"}))
    payload = run_payload([_report()], policy=_policy(), serving=serving)
    without_serving = run_payload([_report()], policy=_policy())

    assert payload["meta"]["serving"]["engine"] == {"name": "ollama", "version": "0.5.1"}
    assert "serving" not in without_serving["meta"]
    json.dumps(payload)


def test_probe_serving_collects_version_show_and_tags() -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fetch(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        calls.append((url, payload))
        if url.endswith("/api/version"):
            return {"version": "0.5.1"}
        if url.endswith("/api/show"):
            return {"details": {"quantization_level": "Q4_K_M"}}
        return {"models": [{"name": "qwen3:8b", "digest": "bbb"}]}

    result = asyncio.run(probe_serving("http://host:11434/v1", "qwen3:8b", fetch=fetch))
    assert [url for url, _ in calls] == [
        "http://host:11434/api/version",
        "http://host:11434/api/show",
        "http://host:11434/api/tags",
        "http://host:11434/api/ps",
    ]
    assert calls[1][1] == {"model": "qwen3:8b"}
    assert result.error is None
    assert result.version == {"version": "0.5.1"}


def test_probe_serving_reports_failures_but_keeps_partial_results() -> None:
    async def failing_fetch(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        raise OSError("connection refused")

    failed = asyncio.run(probe_serving("http://host:11434/v1", "m", fetch=failing_fetch))
    assert failed.error is not None
    assert "connection refused" in failed.error
    assert failed.version is None

    async def partial_fetch(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if url.endswith("/api/version"):
            return {"version": "0.5.1"}
        raise OSError("404 not found")

    partial = asyncio.run(probe_serving("http://host:11434/v1", "m", fetch=partial_fetch))
    assert partial.version == {"version": "0.5.1"}
    assert partial.show is None
    assert partial.error is not None


def test_warmup_flag_and_request_paths() -> None:
    async def failing_fetch(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        raise OSError("model not found")

    seen: list[tuple[str, dict[str, Any] | None]] = []

    async def ok_fetch(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        seen.append((url, payload))
        return {"done": True}

    assert asyncio.run(warm_up("http://host:11434/v1", "m", fetch=failing_fetch)) is False
    assert asyncio.run(warm_up("http://host:11434/v1", "m", fetch=ok_fetch)) is True
    assert seen == [("http://host:11434/api/generate", {"model": "m"})]


def test_capture_serving_uses_a_separate_client_for_the_warmup() -> None:
    used: list[str] = []

    def fetch_named(name: str) -> Any:
        async def fetch(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
            used.append(f"{name}:{url.rsplit('/', 1)[-1]}")
            return {"version": "0.5.1"}

        return fetch

    asyncio.run(
        capture_serving(
            "http://host/v1",
            "m",
            fetch=fetch_named("probe"),
            warmup_fetch=fetch_named("warm"),
            warmup=True,
        )
    )
    assert used[0] == "warm:generate"
    assert all(entry.startswith("probe:") for entry in used[1:])


def test_warn_unpinned_is_noisy_only_when_needed(capsys: pytest.CaptureFixture[str]) -> None:
    warn_if_unpinned({"unavailable": ["engine", "context_length"]})
    assert "engine, context_length" in capsys.readouterr().err

    warn_if_unpinned({"unavailable": []})
    assert capsys.readouterr().err == ""


def test_a_ground_campaign_publishes_one_agreeing_overlay_list() -> None:
    grind = PromptGrind(overlay="Always name the namespace.")
    payload = run_payload([_report()], policy=_ground(grind), grind=grind)

    overlays = payload["meta"]["policy"]["overlays"]
    assert overlays == ["eval-overlay"]
    assert payload["meta"]["prompts"]["overlays"] == overlays
    assert len(overlays) == len(set(overlays))
    assert payload["meta"]["prompts"]["source"] == "override"


def test_the_campaign_policy_is_resolved_and_ground_once(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.md"
    overlay.write_text("Always name the namespace.", encoding="utf-8")
    args = _parse_args(["--prompt-overlay-file", str(overlay)])
    grind = _prompt_grind(args)

    policy = _resolve_policy(lambda: ScriptedProvider([[{"type": "done"}]]), args, grind)

    assert policy.prompt_overlay_ids == ("eval-overlay",)
    payload = run_payload([_report()], policy=policy, grind=grind)
    assert payload["meta"]["policy"]["overlays"] == ["eval-overlay"]
