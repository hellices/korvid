"""Model-capability profiles for the agent runtime (issue #71).

korvid's default surface — up to 15 tools, 15 iterations, ~120k chars of
retained history — is tuned for frontier models. Small local models
(3B-14B) tell a different story: BFCL shows they are competitive on
simple single-function calls but fall behind sharply on multi-function
selection, they degrade with context length far below their advertised
windows, and 1-2 in-context demonstrations improve their multi-step tool
use more than longer instruction lists do (ReAct). The `small` profile
gives them a surface they can actually handle; `full` reproduces the
pre-profile wiring byte-for-byte so the frontier experience is unchanged.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from korvid.agent.runtime import MAX_HISTORY_CHARS, SYSTEM_PROMPT, UI_DRIVE_PROMPT
from korvid.agent.tools import READ_TOOLS, RESIZE_TOOLS, UI_TOOLS, WRITE_TOOLS

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
#: a response are refused with an instructive error) — the size bound
#: below only holds if the prompt's "call one tool at a time" is a rule,
#: not a suggestion.
SMALL_MAX_TOOL_CALLS_PER_ITERATION = 1

#: Short role statement, explicit grounding rules, and ONE worked example
#: (question -> tool call -> result -> grounded answer) instead of the
#: longer frontier instruction list.
SMALL_SYSTEM_PROMPT = (
    "You are korvid's Kubernetes diagnostic agent, embedded in a live TUI. "
    "Use tools to inspect cluster state and cite evidence from tool results. "
    "Call one tool at a time and wait for its result before deciding the "
    "next step. Never invent resource names: use only names from the screen "
    "context or from tool results. "
    "Worked example — user: why does pod checkout-1 in namespace shop keep "
    'restarting? -> you call diagnose_pod with {"pod": "checkout-1", '
    '"namespace": "shop"} -> the result shows lastState terminated '
    "exit=137 (OOMKilled) -> you answer: checkout-1 is OOMKilled (exit "
    "code 137); its container exceeds the memory limit, so raise the limit "
    "or reduce usage."
)

#: The full UI_DRIVE_PROMPT advertises all five UI tools; the small profile
#: offers only the two evidence-showing ones, and the model must never be
#: told about capabilities it was not offered.
SMALL_UI_PROMPT = (
    "You can also show evidence on the user's screen: open_logs (show a "
    "pod's live logs) and open_describe (show a resource's manifest and "
    "events). These change nothing in the cluster. Keep your text concise; "
    "the screen carries the detail."
)

_SMALL_UI_TOOL_NAMES = ("open_logs", "open_describe")

#: Concise description overrides for schemas that are verbose in the full
#: profile — every request retransmits the schemas, so on a 4k-token
#: serving context the wording is a real cost (EasyTool). The effect is
#: measurable per endpoint with the #69 harness (`--profile small`).
_SMALL_DESCRIPTIONS: dict[str, str] = {
    "diagnose_pod": (
        "One-call diagnosis of a broken pod: container states, exit codes, "
        "restart counts, failing conditions, Warning events, node/PVC "
        "context, and log excerpts. Prefer this first when a pod is failing."
    ),
    "list_operators": (
        "List OLM operator packages and installed subscriptions with their status. Read-only."
    ),
    "open_logs": "Open the live log pane for a pod on the user's screen.",
    "resize_pod": (
        "Request an in-place CPU/memory resize of a running pod (Kubernetes "
        "1.35+). Runs only after the user approves it in the TUI dialog."
    ),
}


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
    #: When set, at most this many tool calls are executed per model
    #: response; extra parallel calls are refused at dispatch.
    max_tool_calls_per_iteration: int | None
    system_prompt: str
    ui_prompt: str


def _trim(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deep-copied schemas with concise descriptions where an override
    exists — the shared module-level tool lists must never be mutated."""
    trimmed = copy.deepcopy(tools)
    for tool in trimmed:
        function = tool["function"]
        override = _SMALL_DESCRIPTIONS.get(function["name"])
        if override is not None:
            function["description"] = override
    return trimmed


def build_profile(name: str, *, readonly: bool, resize_supported: bool) -> AgentProfile:
    """Build the tool surface, budgets, and prompts for one profile.

    Args:
        name: `full` or `small` (`agent.profile` in config.yaml).
        readonly: when True, write tools are omitted entirely — the model
            is never even told they exist.
        resize_supported: whether discovery found pods/resize; the resize
            tool is offered only when the cluster can honor it.

    Raises:
        ValueError: for a profile name other than `full` or `small`.
    """
    if name == "full":
        tools = READ_TOOLS + UI_TOOLS
        if not readonly:
            tools = tools + WRITE_TOOLS
            if resize_supported:
                tools = tools + RESIZE_TOOLS
        return AgentProfile(
            name="full",
            tools=tools,
            max_iterations=FULL_MAX_ITERATIONS,
            max_history_chars=MAX_HISTORY_CHARS,
            max_result_chars=None,
            max_tool_calls_per_iteration=None,
            system_prompt=SYSTEM_PROMPT,
            ui_prompt=UI_DRIVE_PROMPT,
        )
    if name == "small":
        small_ui = [t for t in UI_TOOLS if t["function"]["name"] in _SMALL_UI_TOOL_NAMES]
        tools = READ_TOOLS + small_ui
        if not readonly:
            tools = tools + WRITE_TOOLS
            if resize_supported:
                tools = tools + RESIZE_TOOLS
        return AgentProfile(
            name="small",
            tools=_trim(tools),
            max_iterations=SMALL_MAX_ITERATIONS,
            max_history_chars=SMALL_MAX_HISTORY_CHARS,
            max_result_chars=SMALL_MAX_RESULT_CHARS,
            max_tool_calls_per_iteration=SMALL_MAX_TOOL_CALLS_PER_ITERATION,
            system_prompt=SMALL_SYSTEM_PROMPT,
            ui_prompt=SMALL_UI_PROMPT,
        )
    raise ValueError(f"unknown agent profile: {name!r} (expected one of {PROFILE_NAMES})")
