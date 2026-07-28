"""Tests for model-capability profiles (issue #71)."""

from __future__ import annotations

import pytest

from korvid.agent.profiles import (
    SMALL_MAX_HISTORY_CHARS,
    SMALL_MAX_ITERATIONS,
    AgentProfile,
    build_profile,
)
from korvid.agent.runtime import MAX_HISTORY_CHARS, SYSTEM_PROMPT, UI_DRIVE_PROMPT
from korvid.tools.executor import READ_TOOLS, RESIZE_TOOLS, UI_TOOLS, WRITE_TOOLS


def _names(tools: list[dict[str, object]]) -> list[str]:
    return [t["function"]["name"] for t in tools]  # type: ignore[index]  # nested Any schema


def test_full_profile_matches_the_unprofiled_surface() -> None:
    """`full` is byte-identical to what the composition root wired before
    profiles existed — frontier-model behavior must not change."""
    profile = build_profile("full", readonly=False, resize_supported=True)
    assert isinstance(profile, AgentProfile)
    assert profile.name == "full"
    assert profile.tools == READ_TOOLS + UI_TOOLS + WRITE_TOOLS + RESIZE_TOOLS
    assert profile.max_iterations == 15
    assert profile.max_history_chars == MAX_HISTORY_CHARS
    assert profile.system_prompt == SYSTEM_PROMPT
    assert profile.ui_prompt == UI_DRIVE_PROMPT


def test_full_profile_respects_readonly_and_resize_gates() -> None:
    readonly = build_profile("full", readonly=True, resize_supported=True)
    assert readonly.tools == READ_TOOLS + UI_TOOLS
    no_resize = build_profile("full", readonly=False, resize_supported=False)
    assert no_resize.tools == READ_TOOLS + UI_TOOLS + WRITE_TOOLS


def test_small_profile_reduces_the_ui_tool_surface() -> None:
    """Small models fall behind sharply on multi-function selection (BFCL):
    the small profile offers the read tools, only the two evidence-showing
    UI tools, and the approval-gated writes."""
    profile = build_profile("small", readonly=False, resize_supported=True)
    names = _names(profile.tools)
    assert names == [
        *_names(READ_TOOLS),
        "open_logs",
        "open_describe",
        *_names(WRITE_TOOLS),
        *_names(RESIZE_TOOLS),
    ]
    assert "navigate" not in names
    assert "set_filter" not in names
    assert "drill_down" not in names


def test_small_profile_readonly_drops_writes() -> None:
    profile = build_profile("small", readonly=True, resize_supported=True)
    names = _names(profile.tools)
    assert names == [*_names(READ_TOOLS), "open_logs", "open_describe"]


def test_small_profile_trims_verbose_descriptions() -> None:
    """Concise schemas reduce per-request token load for models whose real
    serving context is tiny; the full profile's schemas stay untouched."""
    profile = build_profile("small", readonly=False, resize_supported=True)
    by_name = {t["function"]["name"]: t["function"]["description"] for t in profile.tools}
    full_diagnose = next(
        t["function"]["description"] for t in READ_TOOLS if t["function"]["name"] == "diagnose_pod"
    )
    assert len(by_name["diagnose_pod"]) < len(full_diagnose)
    assert all(len(desc) <= 250 for desc in by_name.values())


def test_small_profile_never_mutates_the_shared_tool_schemas() -> None:
    """Trimming must deep-copy: READ_TOOLS is module-level shared state used
    by the full profile and the eval harness."""
    before = [t["function"]["description"] for t in READ_TOOLS]
    build_profile("small", readonly=False, resize_supported=True)
    after = [t["function"]["description"] for t in READ_TOOLS]
    assert before == after


def test_small_profile_budgets_fit_small_serving_contexts() -> None:
    profile = build_profile("small", readonly=False, resize_supported=True)
    assert profile.max_iterations == SMALL_MAX_ITERATIONS == 6
    assert profile.max_history_chars == SMALL_MAX_HISTORY_CHARS == 24_000
    assert profile.max_history_chars < MAX_HISTORY_CHARS


def test_small_profile_prompt_has_example_and_grounding_rules() -> None:
    """1-2 in-context demonstrations measurably improve multi-step tool use
    (ReAct); explicit single-call and no-invention rules replace the longer
    frontier instruction list."""
    profile = build_profile("small", readonly=False, resize_supported=True)
    prompt = profile.system_prompt.lower()
    assert "one tool at a time" in prompt
    assert "never invent" in prompt
    assert "diagnose_pod" in profile.system_prompt  # the worked example
    assert len(profile.system_prompt) < len(SYSTEM_PROMPT) + len(UI_DRIVE_PROMPT) * 2


def test_small_ui_prompt_names_only_the_offered_tools() -> None:
    """The model must never be told about UI capabilities it was not
    offered — the full UI_DRIVE_PROMPT advertises all five."""
    profile = build_profile("small", readonly=False, resize_supported=True)
    assert "open_logs" in profile.ui_prompt
    assert "open_describe" in profile.ui_prompt
    assert "set_filter" not in profile.ui_prompt
    assert "drill_down" not in profile.ui_prompt
    assert "navigate" not in profile.ui_prompt


def test_build_profile_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="unknown agent profile"):
        build_profile("tiny", readonly=False, resize_supported=False)


def test_small_profile_turn_fits_inside_its_history_budget() -> None:
    """MAX_HISTORY_TURNS trimming never removes the most recent turn, so one
    turn of tool results must fit the retained-history budget by
    construction: iterations x per-result cap <= history chars."""
    profile = build_profile("small", readonly=False, resize_supported=True)
    assert profile.max_result_chars is not None
    assert profile.max_iterations * profile.max_result_chars <= profile.max_history_chars


def test_full_profile_keeps_the_executor_result_cap() -> None:
    """`full` must not add a runtime-side cap: the executor's own 8k limit
    is the pre-profile behavior."""
    profile = build_profile("full", readonly=False, resize_supported=True)
    assert profile.max_result_chars is None


def test_small_profile_enforces_one_tool_call_per_iteration() -> None:
    """The 6-iteration x 3k-per-result budget assumes one result per
    iteration; the runtime must enforce it, not just the prompt text."""
    small = build_profile("small", readonly=False, resize_supported=True)
    assert small.max_tool_calls_per_iteration == 1
    full = build_profile("full", readonly=False, resize_supported=True)
    assert full.max_tool_calls_per_iteration is None


def test_strict_history_budget_is_small_only() -> None:
    """The hard history bound (mid-turn guard, oversized-turn drop) is what
    makes the 24k budget real for the small profile; `full` must keep the
    pre-profile soft behavior — a full turn can legitimately hold
    max_iterations executor-capped results."""
    small = build_profile("small", readonly=False, resize_supported=True)
    assert small.strict_history_budget is True
    full = build_profile("full", readonly=False, resize_supported=True)
    assert full.strict_history_budget is False
