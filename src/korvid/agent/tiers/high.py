"""High-tier prompt, broad tool surface, and larger budgets.

With every cluster capability enabled, high has the same 10 cluster reads and
four approval-gated writes as low. Beyond low's `open_logs` and
`open_describe`, its UI surface adds `navigate`, `set_filter`, and
`drill_down`; configured observability backends also add `query_metrics` and
`search_logs`. The effective schemas are still derived from the validated
registry and its environment gates rather than duplicated here.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from korvid.agent.tiers._behavior import TierBehavior

PROMPT: Final[str] = (
    "Gather evidence across as many steps as the question needs: follow a "
    "resource to what it depends on or owns before answering. Call tools "
    "in parallel only when this session's provider has confirmed it "
    "supports that; otherwise call them one at a time like the low tier. "
    "Prefer a richer, well-cited synthesis over a terse guess, while "
    "keeping every write and citation constraint above intact."
)

BEHAVIOR: Final[TierBehavior] = TierBehavior(
    prompt_id="high-korvid-operator",
    prompt=PROMPT,
    tool_surface="high_agent",
    max_iterations=15,
    max_history_chars=120_000,
    max_result_chars=8_000,
    max_tool_calls_per_iteration=None,
    strict_history_budget=False,
    tool_descriptions=MappingProxyType({}),
)
