"""Dropping tools from the measured surface (#221).

`diagnose_service` and `diagnose_pvc` were added to `small_agent` without
being costed against that profile's design premise, which is a *small*
selection space for 3B-14B models. Deciding it needs a controlled arm: the
same models, the same scenarios, the same prompts, and one variable — the
tool surface. Nothing in the harness could express that variable, so the
question could not be asked.

These tests pin the mechanism, not the answer.
"""

from __future__ import annotations

import pytest

from korvid.agent.profiles import PromptOverrides, build_profile
from korvid.evals.__main__ import _parse_args, prompt_fingerprint
from korvid.evals.runner import _eval_tools


def _profile() -> object:
    return build_profile(
        "small", readonly=False, resize_supported=True, overrides=PromptOverrides()
    )


def test_omitting_a_tool_removes_it_from_the_offered_surface() -> None:
    profile = _profile()
    full = {t["function"]["name"] for t in _eval_tools(profile)}  # type: ignore[arg-type]
    assert "diagnose_pvc" in full, "fixture assumption: the small surface arms it today"

    reduced = {
        t["function"]["name"]
        for t in _eval_tools(profile, omit=frozenset({"diagnose_pvc"}))  # type: ignore[arg-type]
    }
    assert reduced == full - {"diagnose_pvc"}


def test_omitting_nothing_is_the_current_surface() -> None:
    profile = _profile()
    assert _eval_tools(profile) == _eval_tools(profile, omit=frozenset())  # type: ignore[arg-type]


def test_the_fingerprint_separates_the_arms() -> None:
    """Two arms must not be able to claim the same digest.

    The digest is what makes a published row comparable; if dropping a tool
    left it unchanged, an artifact could not say which arm produced it.
    """
    profile = _profile()
    full = prompt_fingerprint(profile)  # type: ignore[arg-type]
    reduced = prompt_fingerprint(
        profile,  # type: ignore[arg-type]
        tools=_eval_tools(profile, omit=frozenset({"diagnose_pvc"})),  # type: ignore[arg-type]
    )
    assert full["sha256"] != reduced["sha256"]


def test_dropping_a_tool_is_not_reported_as_a_prompt_override() -> None:
    """Only the surface moved, so the prompts are still the shipped ones."""
    profile = _profile()
    reduced = prompt_fingerprint(
        profile,  # type: ignore[arg-type]
        tools=_eval_tools(profile, omit=frozenset({"diagnose_pvc"})),  # type: ignore[arg-type]
    )
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


def test_the_artifact_records_which_arm_it_measured() -> None:
    """A reduced-surface run must say so in its own JSON.

    Recovering the arm from the digest alone means keeping a lookup table
    of digests, which is exactly the kind of out-of-band bookkeeping that
    made the 2026-08-05 rows unusable.
    """
    from korvid.evals.__main__ import run_payload
    from korvid.evals.runner import ScenarioReport

    profile = _profile()
    payload = run_payload(
        [ScenarioReport(scenario_id="s", root_cause="rc", runs=[])],
        profile=profile,  # type: ignore[arg-type]
        overrides=PromptOverrides(),
        omitted_tools=["diagnose_pvc"],
    )
    assert payload["meta"]["tools"]["omitted"] == ["diagnose_pvc"]
    assert payload["meta"]["tools"]["count"] == len(
        _eval_tools(profile, omit=frozenset({"diagnose_pvc"}))  # type: ignore[arg-type]
    )


def test_an_unreduced_run_records_the_full_count_and_no_omissions() -> None:
    from korvid.evals.__main__ import run_payload
    from korvid.evals.runner import ScenarioReport

    profile = _profile()
    payload = run_payload(
        [ScenarioReport(scenario_id="s", root_cause="rc", runs=[])],
        profile=profile,  # type: ignore[arg-type]
        overrides=PromptOverrides(),
    )
    assert payload["meta"]["tools"] == {
        "omitted": [],
        "count": len(_eval_tools(profile)),  # type: ignore[arg-type]
    }
