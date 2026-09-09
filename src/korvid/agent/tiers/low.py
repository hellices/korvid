"""Low-tier prompt, compact tool wording, surface, and budgets.

With every cluster capability enabled, low has the same 10 cluster reads and
four approval-gated writes as high; low is not a read-only agent. Its UI-only
surface is deliberately limited to `open_logs` and `open_describe`. It does
not get high's navigation/filter/drill-down controls or observability reads.
The effective schemas are still derived from the validated registry and its
environment gates rather than duplicated here.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from korvid.agent.tiers._behavior import TierBehavior

PROMPT: Final[str] = (
    "Operate in small, bounded steps: call one tool at a time and wait for "
    "its result before deciding the next step; never write a plan or a "
    "tool call as text instead of calling the tool. Diagnose one target at "
    "a time. Explore before you conclude: list or describe the resource "
    "before making a claim about its state. A listing row that reads "
    "'namespace/name' is two separate fields — split it, and never paste "
    "the combined value into either one. If a tool result is malformed or "
    "empty, or you cannot make progress after a few attempts, stop and ask "
    "the user for guidance instead of retrying indefinitely. For any request to "
    "show, open, or display logs, always call open_logs first; never substitute "
    "get_logs. For any request to show, open, or display details, always call "
    "open_describe first; never substitute get_resource. For a display-only "
    "request, stop after the open_* tool. If the user also asks for analysis, "
    "call the appropriate get_* read tool only after opening the UI. Treat "
    "'show me' and 'on screen' as display. "
    "Diagnose from the reason string in container states and events, never "
    "from an exit code alone: exit 137 only says the container was killed, "
    "and it means OOMKilled only when a state or event says OOMKilled — a "
    "failing liveness probe ends a container the same way, and then the "
    "probe is the cause. When a result points at another object — an "
    "unbound PVC at its storage class, a service at its endpoints, a job "
    "at its pods — read that object before you answer. State exactly one "
    "root cause and never name a fault you ruled out: 'not X but Y' still "
    "claims X, so say only Y. Quote the decisive reason string word for "
    "word and cite the exit codes and counts your evidence shows. Ready is "
    "not healthy while warning events show probe failures: call a resource "
    "healthy only when its status, its conditions, and its recent warning "
    "events all agree, and name the checks that passed. Restarts with no "
    "recent warning are history, not a live fault. "
    "When the next step is clear, dispatch the tool immediately without "
    "narrating the plan first: do not narrate what you are about to do — "
    "call the tool and let the result speak. When opening a UI pane, pass "
    "continue_analysis: true only if the user also asked for analysis after "
    "the display; omit it or set it false for display-only requests, and stop "
    "after the open_* call. Limit every final answer to at most three short "
    "bullets — root cause, decisive evidence, and the next operation the user "
    "or agent should take — no generic advice, no restating what you already "
    "showed, no filler text."
)

TOOL_DESCRIPTIONS_VERSION: Final[int] = 2
TOOL_DESCRIPTION_MAX_CHARS: Final[int] = 250
TOOL_DESCRIPTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "diagnose_pod": (
            "One-call diagnosis of a broken pod: container states, exit codes, "
            "restart counts, failing conditions, Warning events, node/PVC "
            "context, and log excerpts. Prefer this first when a pod is failing."
        ),
        "diagnose_pvc": (
            "Deterministic PVC binding check: one GET for Bound/Lost; fetches "
            "Warning events for unresolved claims; lists StorageClasses only when "
            "no failure event, pre-bound volume, or empty storageClassName applies. "
            "Prefer first for stuck PVCs."
        ),
        "diagnose_workload": (
            "One-call diagnosis of a stuck Deployment rollout: conditions and "
            "Warning events, owned ReplicaSets, and compact diagnoses of its "
            "non-ready pods. Prefer this when a Deployment is not progressing."
        ),
        "get_logs": "Read only; no UI. Not for show/open.",
        "helm_list_releases": (
            "List installed Helm releases with revision, status, chart and app "
            "version. Read-only; parsed from cluster Secrets."
        ),
        "list_operators": (
            "List OLM operator packages and installed subscriptions with their status. Read-only."
        ),
        "open_logs": "Use for show/open/display: open TUI logs.",
        "resize_pod": (
            "Request an in-place CPU/memory resize of a running pod (Kubernetes "
            "1.35+). Runs only after the user approves it in the TUI dialog."
        ),
    }
)

BEHAVIOR: Final[TierBehavior] = TierBehavior(
    prompt_id="low-korvid-operator",
    prompt=PROMPT,
    tool_surface="low_agent",
    max_iterations=6,
    max_history_chars=24_000,
    max_result_chars=3_000,
    max_tool_calls_per_iteration=1,
    strict_history_budget=True,
    tool_descriptions=TOOL_DESCRIPTIONS,
)
