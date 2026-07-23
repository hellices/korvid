"""LLMProvider ABC — the pluggable boundary (design doc §6.3, standards §3).

Concrete adapters live in korvid/providers/ and register via the
entry_points group "korvid.provider". No default provider is bundled.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class LLMProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name shown in the status bar."""

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield completion events (text deltas and tool calls)."""
