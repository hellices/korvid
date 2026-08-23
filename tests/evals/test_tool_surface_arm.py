"""Dropping tools from the measured surface (#221).

`diagnose_service` and `diagnose_pvc` were added to the small surface
without being costed against its design premise, which is a *small*
selection space for 3B-14B models. Deciding it needs a controlled arm: the
same models, the same scenarios, the same prompts, and one variable — the
tool surface. Nothing in the harness could express that variable, so the
question could not be asked.

Since issue #316 task 13 the variable is expressed on the *resolved
policy*, so a dropped tool is genuinely unarmed rather than merely hidden:
the production `ToolHarness` refuses it and the executor is never reached.

These tests pin the mechanism, not the answer.
"""

from __future__ import annotations

from typing import Any

import pytest
from korvid.evals.harness import resolve_eval_policy

from korvid.evals.__main__ import _parse_args, prompt_fingerprint
from korvid.evals.scripted import ScriptedProvider


def _policy(**kwargs: Any) -> Any:
    return resolve_eval_policy(ScriptedProvider([[{"type": "done"}]]), **kwargs)


def _names(policy: Any) -> set[str]:
    return {tool["function"]["name"] for tool in policy.tools}


def test_omitting_a_tool_removes_it_from_the_armed_surface() -> None:
    full = _names(_policy())
    assert "diagnose_pvc" in full, "fixture assumption: the low surface arms it today"

    reduced = _names(_policy(omit_tools=frozenset({"diagnose_pvc"})))
    assert reduced == full - {"diagnose_pvc"}


def test_omitting_nothing_is_the_current_surface() -> None:
    assert _names(_policy()) == _names(_policy(omit_tools=frozenset()))


def test_the_fingerprint_separates_the_arms() -> None:
    """Two arms must not be able to claim the same digest.

    The digest is what makes a published row comparable; if dropping a tool
    left it unchanged, an artifact could not say which arm produced it.
    """
    full = prompt_fingerprint(_policy())
    reduced = prompt_fingerprint(_policy(omit_tools=frozenset({"diagnose_pvc"})))
    assert full["sha256"] != reduced["sha256"]


def test_dropping_a_tool_is_not_reported_as_a_prompt_override() -> None:
    """Only the surface moved, so the prompts are still the shipped ones."""
    reduced = prompt_fingerprint(_policy(omit_tools=frozenset({"diagnose_pvc"})))
    assert reduced["source"] == "default"


def test_without_tool_is_repeatable_and_defaults_to_empty() -> None:
    assert _parse_args([]).without_tool == []
    parsed = _parse_args(["--without-tool", "diagnose_pvc", "--without-tool", "diagnose_service"])
    assert parsed.without_tool == ["diagnose_pvc", "diagnose_service"]


def test_an_unknown_tool_name_is_refused() -> None:
    """A typo would silently measure the *unreduced* arm.

    The run would then be published as arm 3 while actually being arm 2,
    which is worse than not running it at all.
    """
    with pytest.raises(SystemExit, match="diagnose_pvcs"):
        _parse_args(["--without-tool", "diagnose_pvcs"])


def test_a_write_tool_name_is_refused_because_no_eval_ever_arms_one() -> None:
    """Naming a write tool would record an omission that never happened.

    The eval environment is read-only, so no write schema is ever offered;
    an arm published as reduced would be byte-identical to the full one.
    """
    with pytest.raises(SystemExit, match="scale_resource"):
        _parse_args(["--without-tool", "scale_resource"])


def test_a_high_tier_only_tool_is_refused_on_the_low_surface() -> None:
    with pytest.raises(SystemExit, match="navigate"):
        _parse_args(["--without-tool", "navigate"])


def test_a_high_tier_only_tool_is_accepted_when_the_tier_arms_it() -> None:
    parsed = _parse_args(["--model-tier", "high", "--without-tool", "navigate"])
    assert parsed.without_tool == ["navigate"]


def test_the_artifact_records_which_arm_it_measured() -> None:
    """A reduced-surface run must say so in its own JSON.

    Recovering the arm from the digest alone means keeping a lookup table
    of digests, which is exactly the kind of out-of-band bookkeeping that
    made the 2026-08-05 rows unusable.
    """
    from korvid.evals.__main__ import run_payload

    policy = _policy(omit_tools=frozenset({"diagnose_pvc"}))
    payload = run_payload([], policy=policy, omitted_tools=["diagnose_pvc"])
    assert payload["meta"]["tools"]["omitted"] == ["diagnose_pvc"]
    assert payload["meta"]["tools"]["count"] == len(policy.tools)
    assert "diagnose_pvc" not in payload["meta"]["tools"]["armed"]


def test_an_unreduced_run_records_the_full_count_and_no_omissions() -> None:
    from korvid.evals.__main__ import run_payload

    policy = _policy()
    payload = run_payload([], policy=policy)
    assert payload["meta"]["tools"]["omitted"] == []
    assert payload["meta"]["tools"]["count"] == len(policy.tools)


def test_a_repeated_name_is_recorded_once() -> None:
    """`--without-tool x --without-tool x` removed one tool, not two."""
    from korvid.evals.__main__ import run_payload

    payload = run_payload(
        [],
        policy=_policy(omit_tools=frozenset({"diagnose_pvc"})),
        omitted_tools=["diagnose_pvc", "diagnose_pvc"],
    )
    assert payload["meta"]["tools"]["omitted"] == ["diagnose_pvc"]


async def test_an_omitted_tool_call_is_refused_and_not_credited() -> None:
    """Hiding the schema is not the same as removing the tool.

    An omitted name must be unarmed on the resolved policy, so the
    production tool harness refuses it and the executor is never reached —
    otherwise a model that remembered the tool would still get its answer
    and the reduced arm would be measuring the full surface.
    """
    from korvid.evals.runner import run_scenario
    from tests.evals.test_runner import _executor_factory, _oom_scenario, _tool_call

    scenario = _oom_scenario()
    script: list[list[dict[str, Any]]] = [
        [
            _tool_call("diagnose_pod", {"pod": "checkout-1", "namespace": "shop"}),
            {"type": "usage", "input_tokens": 10, "output_tokens": 1},
        ],
        [
            {"type": "text_delta", "text": "OOMKilled — raise the memory limit."},
            {"type": "usage", "input_tokens": 10, "output_tokens": 1},
        ],
    ]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
        omit_tools=frozenset({"diagnose_pod"}),
    )
    run = report.runs[0]
    assert run.malformed_tool_calls == 1, "an unoffered name is a malformed call"
    assert run.resolvable_tool_calls == 0
    assert run.on_target_tool_calls == 0
