"""Tests for model-capability profiles (issue #71)."""

from __future__ import annotations

import pytest

from korvid.agent.profiles import (
    SMALL_MAX_HISTORY_CHARS,
    SMALL_MAX_ITERATIONS,
    AgentProfile,
    build_profile,
)
from korvid.agent.prompts import SYSTEM_PROMPT, UI_DRIVE_PROMPT
from korvid.agent.runtime import MAX_HISTORY_CHARS
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


def test_small_profile_prompt_pins_tools_only_and_recovery_rules() -> None:
    """Observed small-model failures (issue: 404 loops): stale names reused
    from earlier turns, name/namespace pairs mixed across resources, and
    describe calls issued without listing first. The prompt must pin the
    tools-only boundary, list-before-inspect grounding, and 404 recovery -
    asserted as the defining phrases, not independent tokens a deleted
    sentence could still satisfy."""
    profile = build_profile("small", readonly=False, resize_supported=True)
    prompt = profile.system_prompt.lower()
    # tools-only boundary: the whole defining clause
    assert "only through the provided tools" in prompt
    assert "no shell, no kubectl" in prompt
    # list-before-inspect grounding, without reinforcing the composite bug:
    # list rows are 'namespace/name' and must be split into the two fields
    assert "list_resources first" in prompt
    assert "split that into the separate namespace and name fields" in prompt
    assert "never paste the combined value" in prompt
    # 404 recovery: re-list, never retry the same call
    assert "re-list instead of retrying" in prompt


def test_small_profile_prompt_pins_measured_failure_mode_rules() -> None:
    """Baseline eval failures on qwen3:4b (24-scenario pack, small profile)
    exposed four prompt-fixable behaviors; each rule below is pinned by its
    defining phrase so a reworded prompt cannot silently drop it.

    1. exit-code over-anchoring: a liveness-probe kill (exit 137) was
       misdiagnosed as OOM because the worked example taught 137=OOMKilled.
    2. healthy resources: all three negative controls failed — old restart
       history was reported as a live fault, and no plain healthy verdict
       was given.
    3. reason-string citation: a correct backoff-limit diagnosis failed the
       grade because the decisive `BackoffLimitExceeded` reason was never
       quoted.
    4. one-hop-short exploration: unbound-PVC and service-endpoint answers
       stopped at the pointer instead of fetching the object it named.
    """
    profile = build_profile("small", readonly=False, resize_supported=True)
    prompt = profile.system_prompt.lower()
    # 1. the reason string, not the exit code, names the cause — and a
    #    liveness kill is attributed to the kubelet, not memory (the weak
    #    first-round wording still produced an OOM misdiagnosis); round 2
    #    measured contrastive rule-outs ("not OOM, but...") tripping the
    #    misdiagnosis gate, so ruled-out faults must go unmentioned
    assert "reason string" in prompt
    assert "not the exit code" in prompt
    assert "kubelet killed" in prompt
    assert "never name faults you ruled out" in prompt
    # round 3 measured the example's OOM answer parroted verbatim for a
    # liveness kill, plans narrated instead of tool calls, and healthy
    # verdicts too terse to name the passing checks
    assert "shows the method only" in prompt
    assert "call the tool instead" in prompt
    assert "name the checks that pass" in prompt
    # round 4 measured the OOM parrot surviving the method-only marker
    # (liveness kills still answered with the example's OOM text), so the
    # example itself now demonstrates the 137 discrimination: probe kill,
    # quoted event reason, no OOM anywhere in the example
    assert "liveness probe failed" in prompt
    assert "oom" not in prompt.split("worked example")[1]
    # round 5 fixed the liveness parrot but dropped the exit-code citation
    # habit (oom-killed answers stopped naming 137) and healthy verdicts
    # still hedged; the example now cites the exit code alongside the
    # event reason, and a ready pod's answer must start with healthy
    assert "last exit 137" in prompt
    assert "start your answer with healthy" in prompt
    # 2. healthy is a valid verdict; stopped restarts are history — made
    #    mechanical only after current conditions/events also pass
    assert "healthy" in prompt
    assert "ready is not healthy" in prompt
    assert "history, not a live fault" in prompt
    # 3. copy decisive reasons word-for-word
    assert "word-for-word" in prompt
    # 4. fetch the object a result points at before answering — including
    #    a PVC's storage class (round-1 answers stopped at the PVC)
    assert "fetch it before answering" in prompt
    assert "storage class" in prompt


def test_full_profile_prompt_pins_tools_only_and_grounding_rules() -> None:
    """Same invariants for the frontier prompt: the agent explores only
    through session tools and never fabricates names or namespaces -
    pinned as the defining phrases."""
    prompt = SYSTEM_PROMPT.lower()
    assert "only through the tools provided" in prompt
    assert "no shell" in prompt
    assert "never invent resource names or namespaces" in prompt
    assert "paired with the namespace" in prompt
    assert "404" in prompt or "notfound" in prompt


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
