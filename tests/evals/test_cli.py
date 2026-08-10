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

from korvid.agent import prompts
from korvid.agent.profiles import AgentProfile, PromptOverrides, build_profile
from korvid.evals.__main__ import (
    _parse_args,
    _positive_int,
    _sweep_overrides,
    capture_serving,
    exit_code,
    probe_serving,
    prompt_fingerprint,
    provider_factory_from_env,
    report_payload,
    run_payload,
    warm_up,
)
from korvid.evals.grader import GradeResult
from korvid.evals.runner import RunMetrics, ScenarioReport
from korvid.evals.serving import ProbeResult, serving_metadata
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


# --- prompt provenance ------------------------------------------------------
#
# A scoreboard row that does not say which prompt produced it is not a
# comparable score. Every run records a fingerprint.


def _built(**kwargs: Any) -> tuple[AgentProfile, PromptOverrides]:
    overrides = PromptOverrides(**kwargs)
    profile = build_profile("small", readonly=True, resize_supported=False, overrides=overrides)
    return profile, overrides


def test_run_payload_wraps_scenarios_with_run_metadata() -> None:
    profile, overrides = _built()
    payload = run_payload([_report()], profile=profile, overrides=overrides)
    assert payload["meta"]["profile"] == "small"
    assert payload["scenarios"][0]["scenario"] == "oom-killed"
    json.dumps(payload)


def test_run_payload_marks_the_shipped_prompts_as_default() -> None:
    profile, overrides = _built()
    payload = run_payload([_report()], profile=profile, overrides=overrides)
    assert payload["meta"]["prompts"]["source"] == "default"


@pytest.mark.parametrize(
    "override",
    [
        {"system": "You are terse."},
        {"append": "Never name nodes."},
        {"tool_descriptions": {"get_logs": "Mine."}},
    ],
)
def test_run_payload_marks_any_override_as_override(override: dict[str, Any]) -> None:
    profile, overrides = _built(**override)
    payload = run_payload([_report()], profile=profile, overrides=overrides)
    assert payload["meta"]["prompts"]["source"] == "override"


def test_prompt_fingerprint_is_stable_and_changes_with_the_prompt() -> None:
    first = prompt_fingerprint(_built()[0])["sha256"]
    again = prompt_fingerprint(_built()[0])["sha256"]
    changed = prompt_fingerprint(_built(system="You are terse.")[0])["sha256"]
    assert first == again
    assert first != changed


def test_prompt_fingerprint_notices_a_reworded_tool_description() -> None:
    """Tool wording is a measured lever, so it must be part of the identity."""
    plain = prompt_fingerprint(_built()[0])["sha256"]
    reworded = prompt_fingerprint(_built(tool_descriptions={"get_logs": "Mine."})[0])["sha256"]
    assert plain != reworded


def test_prompt_sweep_flags_default_to_unset() -> None:
    args = _parse_args([])
    assert args.system_prompt_file is None
    assert args.prompt_append_file is None


def test_prompt_sweep_flags_accept_paths() -> None:
    args = _parse_args(["--system-prompt-file", "a.md", "--prompt-append-file", "b.md"])
    assert args.system_prompt_file == Path("a.md")
    assert args.prompt_append_file == Path("b.md")


def test_prompt_fingerprint_covers_the_composed_prompt_not_just_the_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest must identify the actual model input.

    `AgentRuntime` composes the write/no-write clause onto the role
    statement at request time. That clause is not part of
    `profile.system_prompt`, so a digest over the role statement alone
    would call two behaviourally different runs comparable.
    """
    profile, _ = _built()
    before = prompt_fingerprint(profile)["sha256"]
    monkeypatch.setattr(prompts, "NO_WRITE_PROMPT", "Reworded read-only guidance.")
    assert prompt_fingerprint(profile)["sha256"] != before


def test_prompt_fingerprint_covers_parameter_schemas() -> None:
    """A parameter-schema edit changes what the model sees, so it must
    change the digest — the methodology promises exactly this."""
    profile, _ = _built()
    before = prompt_fingerprint(profile)["sha256"]
    target = next(t for t in profile.tools if t["function"]["name"] == "get_logs")
    target["function"]["parameters"]["properties"]["namespace"]["description"] = "changed"
    assert prompt_fingerprint(profile)["sha256"] != before


def test_prompt_sweep_file_with_invalid_utf8_exits_cleanly(tmp_path: Path) -> None:
    """A non-UTF-8 file must produce the CLI's actionable error, not a
    traceback: `UnicodeDecodeError` is not an `OSError`."""
    bad = tmp_path / "prompt.md"
    bad.write_bytes(b"\xff\xfe not utf-8")
    args = _parse_args(["--system-prompt-file", str(bad)])
    with pytest.raises(SystemExit, match="--system-prompt-file"):
        _sweep_overrides(args)


def test_source_is_default_when_an_override_reproduces_the_shipped_prompt() -> None:
    """`source` decides publishability, so it must reflect the *effect* of
    the configuration, not merely that some was supplied. Pointing at a
    file holding korvid's own prompt yields a comparable run."""
    profile, _ = _built()
    same = PromptOverrides(system=profile.system_prompt)
    rebuilt = build_profile("small", readonly=True, resize_supported=False, overrides=same)
    assert prompt_fingerprint(rebuilt)["source"] == "default"


def test_source_is_default_for_a_tool_description_that_changes_nothing() -> None:
    profile, _ = _built()
    current = {t["function"]["name"]: t["function"]["description"] for t in profile.tools}
    echo = PromptOverrides(tool_descriptions={"get_logs": current["get_logs"]})
    rebuilt = build_profile("small", readonly=True, resize_supported=False, overrides=echo)
    assert prompt_fingerprint(rebuilt)["source"] == "default"


def test_source_is_override_when_the_prompt_actually_differs() -> None:
    profile, _ = _built(system="You are terse.")
    assert prompt_fingerprint(profile)["source"] == "override"


def test_run_payload_omits_serving_when_it_was_not_captured() -> None:
    """Older artifacts have no serving block; absence must stay meaningful."""
    profile, overrides = _built()
    payload = run_payload([_report()], profile=profile, overrides=overrides)
    assert "serving" not in payload["meta"]


def test_run_payload_records_the_serving_block_when_captured() -> None:
    profile, overrides = _built()
    serving = serving_metadata(model="qwen3:8b", probe=ProbeResult(version={"version": "0.5.1"}))
    payload = run_payload([_report()], profile=profile, overrides=overrides, serving=serving)
    assert payload["meta"]["serving"]["engine"] == {"name": "ollama", "version": "0.5.1"}
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
