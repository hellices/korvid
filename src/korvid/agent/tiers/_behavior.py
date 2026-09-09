"""Shared immutable contract for explicit agent tiers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from korvid.tools.registry import agent_tool_schemas


@dataclass(frozen=True, slots=True)
class TierBehavior:
    """Prompt, tool surface, and budgets for one agent tier."""

    prompt_id: str
    prompt: str
    tool_surface: str
    max_iterations: int
    max_history_chars: int
    max_result_chars: int
    max_tool_calls_per_iteration: int | None
    strict_history_budget: bool
    tool_descriptions: Mapping[str, str]

    def tool_schemas(
        self,
        *,
        readonly: bool,
        resize_supported: bool,
        observability_backends: frozenset[str],
    ) -> list[dict[str, Any]]:
        """Project this tier from the validated registry surface."""
        schemas = agent_tool_schemas(
            self.tool_surface,
            readonly=readonly,
            resize_supported=resize_supported,
            observability_backends=observability_backends,
        )
        for schema in schemas:
            function = schema.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str):
                continue
            replacement = self.tool_descriptions.get(name)
            if replacement is not None:
                function["description"] = replacement
        return schemas
