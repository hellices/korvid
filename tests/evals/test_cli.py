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
    EVAL_PROTOCOL_VERSION,
    _parse_args,
    _positive_int,
    _prompt_grind,
    _resolve_policy,
    _select_scenarios,
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
from korvid.evals.scenario import Evidence, Scenario
from korvid.evals.scripted import ScriptedProvider
from korvid.evals.serving import ProbeResult, serving_metadata
from korvid.providers.ollama import OllamaProvider
from korvid.providers.openai_compat import OpenAICompatProvider
from tests.evals.fixtures import EVAL_INTERACTION


def _no_citations() -> CitationReport:
    """An answer that cited nothing - the shape these fixtures assume."""
    return citation_report("", minted=())


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
    args = _parse_args([])

    policy = _resolve_policy(factory, args)

    assert policy.route_source.value == "catalog"
    assert policy.catalog_version is not None


def test_provider_factory_applies_eval_timeout() -> None:
    provider = provider_factory_from_env(
        {
            "KORVID_EVAL_BASE_URL": "http://localhost:1234/v1",
            "KORVID_EVAL_MODEL": "large-local-model",
            "KORVID_EVAL_TIMEOUT_SECONDS": "900",
        }
    )()
    assert provider._get_client().timeout.read == 900.0


def test_provider_factory_applies_eval_timeout_to_ollama() -> None:
    provider = provider_factory_from_env(
        {
            "KORVID_EVAL_PROVIDER": "ollama",
            "KORVID_EVAL_BASE_URL": "http://localhost:11434/v1",
            "KORVID_EVAL_MODEL": "qwen3:8b",
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


# --- resolved-tier routing --------------------------------------------------
#
# There is no capability "profile" any more: the eval CLI names a model
# tier or lets the production router decide, exactly as the TUI does.


def test_model_tier_defaults_to_automatic_routing() -> None:
    assert _parse_args([]).model_tier is None


@pytest.mark.parametrize("tier", ["low", "high"])
def test_model_tier_accepts_the_two_real_tiers(tier: str) -> None:
    assert _parse_args(["--model-tier", tier]).model_tier == tier


@pytest.mark.parametrize("value", ["full", "small", "huge"])
def test_model_tier_rejects_retired_profile_names(value: str) -> None:
    """`full`/`small` were capability profiles; they are not tiers."""
    with pytest.raises(SystemExit, match="2"):
        _parse_args(["--model-tier", value])


def test_the_profile_flag_is_gone() -> None:
    with pytest.raises(SystemExit, match="2"):
        _parse_args(["--profile", "small"])


def _policy(**kwargs: Any) -> Any:
    return resolve_eval_policy(ScriptedProvider([[{"type": "done"}]]), **kwargs)


# --- prompt provenance ------------------------------------------------------
#
# A scoreboard row that does not say which prompt produced it is not a
# comparable score. Every run records the resolved policy and a fingerprint.


def test_run_payload_records_the_resolved_policy() -> None:
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
    assert payload["scenarios"][0]["scenario"] == "oom-killed"
    json.dumps(payload)


def test_run_payload_records_every_budget_the_policy_imposes() -> None:
    payload = run_payload([_report()], policy=_policy())
    assert payload["meta"]["limits"] == {
        "max_iterations": 6,
        "max_history_chars": 24_000,
        "max_result_chars": 3_000,
        "max_tool_calls_per_iteration": 1,
        "allow_parallel_tool_calls": False,
        "strict_history_budget": True,
    }


def test_run_payload_records_the_high_tier_budgets_when_routed_high() -> None:
    payload = run_payload([_report()], policy=_policy(model_tier="high"))
    assert payload["meta"]["policy"]["tier"] == "high"
    assert payload["meta"]["policy"]["route_source"] == "user"
    assert payload["meta"]["limits"]["max_iterations"] == 15
    assert payload["meta"]["limits"]["max_tool_calls_per_iteration"] is None


def test_run_payload_records_the_catalog_version_and_capability_provenance() -> None:
    payload = run_payload([_report()], policy=_policy())
    assert payload["meta"]["catalog_version"] is None
    capabilities = payload["meta"]["capabilities"]
    assert set(capabilities) == {
        "context_window_tokens",
        "supports_tools",
        "supports_parallel_tools",
        "supports_reasoning",
        "recommended_tier",
        "provenance",
    }
    json.dumps(payload)


def test_run_payload_names_the_exact_armed_tools() -> None:
    policy = _policy()
    payload = run_payload([_report()], policy=policy)
    armed = payload["meta"]["tools"]["armed"]
    assert armed == sorted(tool["function"]["name"] for tool in policy.tools)
    assert payload["meta"]["tools"]["count"] == len(armed)
    assert "scale_resource" not in armed


def test_run_payload_records_the_starting_interaction_per_scenario() -> None:
    payload = run_payload([_report()], policy=_policy())
    interaction = payload["scenarios"][0]["interaction"]
    assert interaction["kube_context"] == "eval-cluster"
    assert interaction["focused_pane"]["kind"] == "pods"


def test_run_payload_records_outcome_and_failure_class_per_run() -> None:
    payload = run_payload([_report(), _report(error="connection refused")], policy=_policy())
    assert payload["scenarios"][0]["runs"][0]["outcome"] == "success"
    assert payload["scenarios"][0]["runs"][0]["failure_class"] is None
    assert payload["scenarios"][1]["runs"][0]["outcome"] == "error"
    assert payload["scenarios"][1]["runs"][0]["failure_class"] == "provider_error"


def test_run_payload_records_the_maximum_calls_a_scenario_made() -> None:
    payload = run_payload([_report()], policy=_policy())
    assert payload["scenarios"][0]["max_tool_calls"] == 1


def test_run_payload_marks_the_shipped_prompts_as_default() -> None:
    payload = run_payload([_report()], policy=_policy())
    assert payload["meta"]["prompts"]["source"] == "default"
    assert payload["meta"]["prompts"]["pack"] == "low-korvid-operator"
    assert payload["meta"]["prompts"]["overlays"] == []


@pytest.mark.parametrize(
    "grind",
    [
        PromptGrind(tier_pack="Answer in one sentence."),
        PromptGrind(overlay="Always name the namespace."),
    ],
)
def test_run_payload_marks_a_ground_prompt_as_override(grind: PromptGrind) -> None:
    payload = run_payload([_report()], policy=_policy(), grind=grind)
    assert payload["meta"]["prompts"]["source"] == "override"


def test_run_payload_names_the_eval_overlay_it_layered() -> None:
    payload = run_payload(
        [_report()], policy=_policy(), grind=PromptGrind(overlay="Always cite the namespace.")
    )
    assert payload["meta"]["prompts"]["overlays"] == ["eval-overlay"]


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
    """The digest must identify the actual model input.

    The safety contract and the armed-capability clauses are composed onto
    every request; a digest over the tier pack alone would call two
    behaviourally different runs comparable.
    """
    policy = _policy()
    before = prompt_fingerprint(policy)["sha256"]
    monkeypatch.setattr(prompt_harness, "SAFETY_CONTRACT", "Reworded safety contract.")
    assert prompt_fingerprint(policy)["sha256"] != before


def test_prompt_fingerprint_covers_parameter_schemas() -> None:
    """A parameter-schema edit changes what the model sees, so it must
    change the digest — the methodology promises exactly this."""
    policy = _policy()
    before = prompt_fingerprint(policy)["sha256"]
    narrowed = _policy(omit_tools=frozenset({"get_logs"}))
    assert prompt_fingerprint(narrowed)["sha256"] != before


def test_a_low_and_a_high_tier_run_are_never_confused() -> None:
    assert (
        prompt_fingerprint(_policy())["sha256"]
        != (prompt_fingerprint(_policy(model_tier="high"))["sha256"])
    )


def test_source_is_default_when_a_grind_reproduces_the_shipped_pack() -> None:
    """`source` decides publishability, so it must reflect the *effect* of
    the configuration, not merely that some was supplied."""
    same = PromptGrind(tier_pack=prompt_packs.LOW_KORVID_OPERATOR_PACK)
    assert prompt_fingerprint(_policy(), grind=same)["source"] == "default"


def test_source_is_override_when_the_prompt_actually_differs() -> None:
    ground = PromptGrind(tier_pack="You are terse.")
    assert prompt_fingerprint(_policy(), grind=ground)["source"] == "override"


# --- prompt grinding flags --------------------------------------------------


def test_prompt_grind_flags_default_to_unset() -> None:
    args = _parse_args([])
    assert args.tier_pack_file is None
    assert args.prompt_overlay_file is None


def test_prompt_grind_flags_accept_paths() -> None:
    args = _parse_args(["--tier-pack-file", "a.md", "--prompt-overlay-file", "b.md"])
    assert args.tier_pack_file == Path("a.md")
    assert args.prompt_overlay_file == Path("b.md")


def test_the_retired_replace_system_flags_are_gone() -> None:
    for flag in ("--system-prompt-file", "--prompt-append-file"):
        with pytest.raises(SystemExit, match="2"):
            _parse_args([flag, "a.md"])


def test_prompt_grind_reads_both_layers(tmp_path: Path) -> None:
    pack = tmp_path / "pack.md"
    pack.write_text("Answer in one sentence.", encoding="utf-8")
    overlay = tmp_path / "overlay.md"
    overlay.write_text("Always name the namespace.", encoding="utf-8")
    grind = _prompt_grind(
        _parse_args(["--tier-pack-file", str(pack), "--prompt-overlay-file", str(overlay)])
    )
    assert grind == PromptGrind(
        tier_pack="Answer in one sentence.", overlay="Always name the namespace."
    )


def test_prompt_grind_file_with_invalid_utf8_exits_cleanly(tmp_path: Path) -> None:
    """A non-UTF-8 file must produce the CLI's actionable error, not a
    traceback: `UnicodeDecodeError` is not an `OSError`."""
    bad = tmp_path / "prompt.md"
    bad.write_bytes(b"\xff\xfe not utf-8")
    args = _parse_args(["--tier-pack-file", str(bad)])
    with pytest.raises(SystemExit, match="--tier-pack-file"):
        _prompt_grind(args)


def test_run_payload_omits_serving_when_it_was_not_captured() -> None:
    """Older artifacts have no serving block; absence must stay meaningful."""
    payload = run_payload([_report()], policy=_policy())
    assert "serving" not in payload["meta"]


def test_run_payload_records_the_serving_block_when_captured() -> None:
    serving = serving_metadata(model="qwen3:8b", probe=ProbeResult(version={"version": "0.5.1"}))
    payload = run_payload([_report()], policy=_policy(), serving=serving)
    assert payload["meta"]["serving"]["engine"] == {"name": "ollama", "version": "0.5.1"}
    json.dumps(payload)


# --- machine protocol: version, exact selection, case-pack identity --------
#
# The stable, versioned surface external prompt optimizers parse. Backward
# compatibility means: an old caller that never passes `scenarios=` and
# never sets `--scenario-id` gets the same `meta` shape it always did, plus
# the new `protocol_version` key.


def _scenario(scenario_id: str, question: str = "Why does it fail?") -> Scenario:
    return Scenario(
        id=scenario_id,
        question=question,
        interaction=EVAL_INTERACTION,
        root_cause="oom_killed",
        must_mention=(("oomkilled", "oom"),),
        expected_evidence=(
            (Evidence(tool="diagnose_pod", contains="exit=137", args={"pod": "checkout-1"}),),
        ),
    )


def test_protocol_version_is_a_stable_published_constant() -> None:
    """A version bump is a deliberate, reviewable act: the constant is a
    plain literal, not derived from the package version, so an external
    optimizer's pin survives an unrelated korvid release."""
    assert isinstance(EVAL_PROTOCOL_VERSION, str)
    assert EVAL_PROTOCOL_VERSION


def test_run_payload_always_publishes_the_protocol_version() -> None:
    payload = run_payload([_report()], policy=_policy())
    assert payload["meta"]["protocol_version"] == EVAL_PROTOCOL_VERSION


def test_run_payload_omits_case_pack_when_scenarios_is_not_given() -> None:
    """Backward compatibility: a caller that predates this contract (and
    every caller that never selects a subset) gets the exact previous
    `meta` shape, aside from the always-published protocol version."""
    payload = run_payload([_report()], policy=_policy())
    assert "case_pack" not in payload["meta"]


def test_run_payload_publishes_the_case_pack_identity_when_scenarios_is_given() -> None:
    scenarios = [_scenario("oom-killed"), _scenario("crashloop-app-panic")]
    payload = run_payload([_report()], policy=_policy(), scenarios=scenarios)
    case_pack = payload["meta"]["case_pack"]
    assert case_pack["scenario_ids"] == ["crashloop-app-panic", "oom-killed"]
    assert case_pack["count"] == 2
    assert len(case_pack["sha256"]) == 64
    json.dumps(payload)


def test_scenario_id_flag_defaults_to_empty_and_is_repeatable() -> None:
    assert _parse_args([]).scenario_id == []
    args = _parse_args(["--scenario-id", "oom-killed", "--scenario-id", "crashloop-app-panic"])
    assert args.scenario_id == ["oom-killed", "crashloop-app-panic"]


def test_select_scenarios_helper_returns_all_scenarios_when_the_flag_is_omitted() -> None:
    scenarios = [_scenario("oom-killed"), _scenario("crashloop-app-panic")]
    assert _select_scenarios(scenarios, []) is scenarios


def test_select_scenarios_helper_narrows_to_the_named_ids() -> None:
    scenarios = [_scenario("oom-killed"), _scenario("crashloop-app-panic")]
    selected = _select_scenarios(scenarios, ["oom-killed"])
    assert [s.id for s in selected] == ["oom-killed"]


def test_select_scenarios_helper_exits_on_an_unknown_id() -> None:
    scenarios = [_scenario("oom-killed")]
    with pytest.raises(SystemExit, match="unknown scenario id"):
        _select_scenarios(scenarios, ["not-a-real-scenario"])


def test_select_scenarios_helper_exits_on_a_duplicate_id() -> None:
    scenarios = [_scenario("oom-killed"), _scenario("crashloop-app-panic")]
    with pytest.raises(SystemExit, match="duplicate scenario id"):
        _select_scenarios(scenarios, ["oom-killed", "oom-killed"])


def test_select_scenarios_helper_exits_on_a_blank_id() -> None:
    scenarios = [_scenario("oom-killed")]
    with pytest.raises(SystemExit, match="non-empty strings"):
        _select_scenarios(scenarios, [" "])


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


def test_probe_serving_reports_a_failure_instead_of_propagating_it() -> None:
    """A metadata probe must never take down a multi-hour campaign."""

    async def fetch(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        raise OSError("connection refused")

    result = asyncio.run(probe_serving("http://host:11434/v1", "m", fetch=fetch))
    assert result.error is not None
    assert "connection refused" in result.error
    assert result.version is None


def test_probe_serving_keeps_the_endpoints_that_did_answer() -> None:
    async def fetch(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if url.endswith("/api/version"):
            return {"version": "0.5.1"}
        raise OSError("404 not found")

    result = asyncio.run(probe_serving("http://host:11434/v1", "m", fetch=fetch))
    assert result.version == {"version": "0.5.1"}
    assert result.show is None
    assert result.error is not None


def test_warmup_flag_defaults_to_off() -> None:
    assert _parse_args([]).warmup is False
    assert _parse_args(["--warmup"]).warmup is True


def test_warmup_records_false_when_the_request_failed() -> None:
    """A warm-up that did not happen must not be recorded as if it had."""

    async def fetch(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        raise OSError("model not found")

    assert asyncio.run(warm_up("http://host:11434/v1", "m", fetch=fetch)) is False


def test_warmup_loads_the_model_and_records_true() -> None:
    seen: list[tuple[str, dict[str, Any] | None]] = []

    async def fetch(url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        seen.append((url, payload))
        return {"done": True}

    assert asyncio.run(warm_up("http://host:11434/v1", "m", fetch=fetch)) is True
    assert seen == [("http://host:11434/api/generate", {"model": "m"})]


def test_capture_serving_uses_a_separate_client_for_the_warmup() -> None:
    """A slow model load must not be charged to the probe's short budget."""
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


def test_warn_unpinned_names_every_missing_field(capsys: pytest.CaptureFixture[str]) -> None:
    warn_if_unpinned({"unavailable": ["engine", "context_length"]})
    assert "engine, context_length" in capsys.readouterr().err


def test_warn_unpinned_is_silent_for_a_publishable_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    warn_if_unpinned({"unavailable": []})
    assert capsys.readouterr().err == ""


# --- one policy per campaign, one overlay list ------------------------------
#
# `meta.policy.overlays` and `meta.prompts.overlays` are read side by side.
# They describe the same composed system message, so they must be the same
# list — and the eval overlay must appear once, not once per grounding.


def _ground(grind: PromptGrind) -> Any:
    from korvid.evals.harness import ground_eval_policy

    return ground_eval_policy(_policy(), grind)


def test_a_ground_campaign_publishes_one_agreeing_overlay_list() -> None:
    grind = PromptGrind(overlay="Always name the namespace.")
    payload = run_payload([_report()], policy=_ground(grind), grind=grind)

    overlays = payload["meta"]["policy"]["overlays"]
    assert overlays == ["eval-overlay"]
    assert payload["meta"]["prompts"]["overlays"] == overlays


def test_a_ground_campaign_never_publishes_a_duplicate_overlay_id() -> None:
    grind = PromptGrind(overlay="Always name the namespace.")
    payload = run_payload([_report()], policy=_ground(grind), grind=grind)

    overlays = payload["meta"]["prompts"]["overlays"]
    assert len(overlays) == len(set(overlays))


def test_a_ground_campaign_is_still_marked_as_an_override() -> None:
    """The baseline digest is the shipped prompt, not the ground one."""
    grind = PromptGrind(overlay="Always name the namespace.")
    payload = run_payload([_report()], policy=_ground(grind), grind=grind)

    assert payload["meta"]["prompts"]["source"] == "override"
    assert len(payload["meta"]["prompts"]["sha256"]) == 64


def test_an_ungrounded_campaign_still_agrees_with_itself() -> None:
    payload = run_payload([_report()], policy=_policy())

    assert payload["meta"]["policy"]["overlays"] == payload["meta"]["prompts"]["overlays"] == []


def test_the_campaign_policy_is_resolved_and_ground_once(tmp_path: Path) -> None:
    """The CLI hands one object to the session, the report and the metadata."""
    overlay = tmp_path / "overlay.md"
    overlay.write_text("Always name the namespace.", encoding="utf-8")
    args = _parse_args(["--prompt-overlay-file", str(overlay)])
    grind = _prompt_grind(args)

    policy = _resolve_policy(lambda: ScriptedProvider([[{"type": "done"}]]), args, grind)

    assert policy.prompt_overlay_ids == ("eval-overlay",)
    payload = run_payload([_report()], policy=policy, grind=grind)
    assert payload["meta"]["policy"]["overlays"] == ["eval-overlay"]
