"""Model-capability profiles for the agent runtime (issue #71).

korvid's default surface — up to 15 tools, 15 iterations, ~120k chars of
retained history — is tuned for frontier models. Small local models
(3B-14B) tell a different story: BFCL shows they are competitive on
simple single-function calls but fall behind sharply on multi-function
selection, they degrade with context length far below their advertised
windows, and 1-2 in-context demonstrations improve their multi-step tool
use more than longer instruction lists do (ReAct). The `small` profile
gives them a surface they can actually handle; `full` keeps the frontier
tool surface and budgets unchanged (its prompt wording, like `small`'s,
lives in korvid.agent.prompts and evolves with observed failures).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from korvid.agent.prompts import (
    SMALL_SYSTEM_PROMPT,
    SMALL_TOOL_DESCRIPTIONS,
    SMALL_UI_PROMPT,
    SYSTEM_PROMPT,
    UI_DRIVE_PROMPT,
)
from korvid.agent.runtime import MAX_HISTORY_CHARS
from korvid.tools.registry import AGENT_SURFACES, TOOL_DEFS, agent_tool_schemas

PROFILE_NAMES = ("full", "small")

FULL_MAX_ITERATIONS = 15
SMALL_MAX_ITERATIONS = 6
#: ~6k tokens at 4 chars/token — sized to a realistic local serving context
#: (Ollama defaults to a 4k context under 24 GiB VRAM and silently truncates
#: anything longer), not the model's advertised window.
SMALL_MAX_HISTORY_CHARS = 24_000
#: Per-tool-result cap. History trimming never removes the sole most
#: recent turn, so one turn must fit the budget by construction:
#: 6 iterations x 3k chars = 18k, leaving headroom for the prompt and
#: assistant text inside SMALL_MAX_HISTORY_CHARS. The full profile keeps
#: the executor's own 8k ingest cap instead.
SMALL_MAX_RESULT_CHARS = 3_000
#: One result per iteration, enforced at dispatch (extra parallel calls in
#: a response are discarded, never entering history) — the size bound
#: below only holds if the prompt's "call one tool at a time" is a rule,
#: not a suggestion.
SMALL_MAX_TOOL_CALLS_PER_ITERATION = 1
#: A configured system prompt larger than this share of a profile's history
#: budget earns a warning. `small` is sized for a 4k-token serving context,
#: where the shipped prompt already takes ~13%; roughly doubling it starts
#: crowding out the conversation the prompt exists to guide.
PROMPT_BUDGET_SHARE = 0.25

#: All prompt wording — the full/small role statements, UI-drive variants,
#: and the small profile's concise tool-description overrides — lives in
#: korvid.agent.prompts; this module owns budgets and surface selection.


@dataclass(frozen=True, slots=True)
class PromptOverrides:
    """Configured prompt slots (`agent.prompts`); empty means korvid's own.

    Only the role statement and per-tool descriptions are configurable. The
    write/no-write and UI clauses are appended by `compose_system_prompt`
    from the armed tool set, so no override can tell the model about a
    capability it was not offered.
    """

    system: str | None = None
    append: str | None = None
    tool_descriptions: Mapping[str, str] = field(default_factory=dict)

    def apply(self, shipped: str) -> str:
        """The role statement for a profile whose default is *shipped*."""
        prompt = self.system if self.system is not None else shipped
        return f"{prompt} {self.append}" if self.append else prompt


@dataclass(frozen=True)
class AgentProfile:
    """Everything the composition root needs to wire one capability tier."""

    name: str
    tools: list[dict[str, Any]]
    max_iterations: int
    max_history_chars: int
    #: When set, the runtime truncates each tool result to this many chars
    #: (below the executor's 8k cap); None keeps executor-capped results.
    #: Oversized results are compacted keeping head AND tail — reports like
    #: diagnose_pod place their evidence sections last by design.
    max_result_chars: int | None
    #: When set, at most this many tool calls are kept per model response;
    #: extra parallel calls are discarded at dispatch (arguments and all,
    #: so they cannot grow history).
    max_tool_calls_per_iteration: int | None
    #: When True, the history budget is a hard bound: a turn ends early
    #: before any follow-up request would exceed it, and an oversized
    #: completed turn is dropped at trim time instead of resent. False
    #: preserves the pre-profile behavior (budget enforced only across
    #: turns, the most recent turn always retained).
    strict_history_budget: bool
    system_prompt: str
    ui_prompt: str


def _trim(
    tools: list[dict[str, Any]],
    *,
    built_in: Mapping[str, str] = {},
    overrides: Mapping[str, str] = {},
) -> list[dict[str, Any]]:
    """Deep-copied schemas with reworded descriptions where one applies.

    Precedence is user override > built-in profile wording > the schema's
    own text. The shared module-level tool lists must never be mutated, so
    every profile works on its own copy.
    """
    trimmed = copy.deepcopy(tools)
    for tool in trimmed:
        function = tool["function"]
        description = overrides.get(function["name"]) or built_in.get(function["name"])
        if description is not None:
            function["description"] = description
    return trimmed


def build_profile(
    name: str,
    *,
    readonly: bool,
    resize_supported: bool,
    observability_backends: frozenset[str] = frozenset(),
    overrides: PromptOverrides | None = None,
) -> AgentProfile:
    """Build the tool surface, budgets, and prompts for one profile.

    Args:
        name: `full` or `small` (`agent.profile` in config.yaml).
        readonly: when True, write tools are omitted entirely — the model
            is never even told they exist.
        resize_supported: whether discovery found pods/resize; the resize
            tool is offered only when the cluster can honor it.
        observability_backends: which external backends are configured
            (`metrics`, `logs`, issue #193). Gated separately from
            `resize_supported` because it is a local configuration
            question, not something discovery can answer.
        overrides: configured prompt overrides (`agent.prompts`). Only the
            role statement and tool descriptions are configurable; the
            write/no-write and UI clauses stay conditional on what is
            actually armed, so an override can never advertise a capability
            the model was not offered.

    Raises:
        ValueError: for a profile name other than `full` or `small`.
    """
    slots = overrides or PromptOverrides()
    if name == "full":
        tools = agent_tool_schemas(
            "full_agent",
            readonly=readonly,
            resize_supported=resize_supported,
            observability_backends=observability_backends,
        )
        return AgentProfile(
            name="full",
            tools=_trim(tools, overrides=slots.tool_descriptions),
            max_iterations=FULL_MAX_ITERATIONS,
            max_history_chars=MAX_HISTORY_CHARS,
            max_result_chars=None,
            max_tool_calls_per_iteration=None,
            strict_history_budget=False,
            system_prompt=slots.apply(SYSTEM_PROMPT),
            ui_prompt=UI_DRIVE_PROMPT,
        )
    if name == "small":
        tools = agent_tool_schemas(
            "small_agent",
            readonly=readonly,
            resize_supported=resize_supported,
            observability_backends=observability_backends,
        )
        return AgentProfile(
            name="small",
            tools=_trim(
                tools,
                built_in=SMALL_TOOL_DESCRIPTIONS,
                overrides=slots.tool_descriptions,
            ),
            max_iterations=SMALL_MAX_ITERATIONS,
            max_history_chars=SMALL_MAX_HISTORY_CHARS,
            max_result_chars=SMALL_MAX_RESULT_CHARS,
            max_tool_calls_per_iteration=SMALL_MAX_TOOL_CALLS_PER_ITERATION,
            strict_history_budget=True,
            system_prompt=slots.apply(SMALL_SYSTEM_PROMPT),
            ui_prompt=SMALL_UI_PROMPT,
        )
    raise ValueError(f"unknown agent profile: {name!r} (expected one of {PROFILE_NAMES})")


def _known_tool_names() -> frozenset[str]:
    """Every tool an agent profile can offer, armed on this cluster or not.

    Typo detection must not use the startup surface: `resize_pod` is armed
    only where discovery found pods/resize, write tools are absent in
    read-only mode, and the same overrides are reused after a `:ctx` switch
    or a profile change. Validating against the armed set would warn that a
    perfectly valid override "has no effect".

    MCP-only definitions (`propose_write` and friends) are excluded: no
    agent profile can ever offer them, so an override naming one really
    does have no effect and should say so.
    """
    return frozenset(
        definition.name for definition in TOOL_DEFS if definition.surfaces & AGENT_SURFACES
    )


def validate_prompt_overrides(profile: AgentProfile, overrides: PromptOverrides) -> list[str]:
    """Warnings about a built profile's overrides — never fatal.

    Takes the built profile because both checks need it: tool names are
    checked against what was actually armed, and the size guard against the
    already-overridden prompt and that profile's history budget.
    """
    warnings: list[str] = []
    for name in sorted(set(overrides.tool_descriptions) - _known_tool_names()):
        warnings.append(
            f"agent.prompts.tool_descriptions: {name!r} is not a korvid tool; "
            f"the override has no effect"
        )
    limit = int(profile.max_history_chars * PROMPT_BUDGET_SHARE)
    size = len(profile.system_prompt)
    if size > limit:
        warnings.append(
            f"agent.prompts: the {profile.name} system prompt is {size:,} chars, over "
            f"{PROMPT_BUDGET_SHARE:.0%} of the {profile.max_history_chars:,}-char history "
            f"budget; it is still used, but it crowds out the conversation"
        )
    return warnings
