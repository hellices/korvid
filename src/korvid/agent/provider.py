"""LLMProvider ABC — the pluggable boundary (design doc §6.3, standards §3).

Concrete adapters live in korvid/providers/ and are selected by the
config-driven factory in korvid.providers.registry. Third-party adapters
target the public contract in korvid.agent.provider_plugin; their
discovery/loading stays outside this ABC boundary.
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
        """Yield completion events (text deltas and tool calls).

        Implementations must be async generators (async def with yield),
        which satisfy this AsyncIterator signature under mypy --strict.
        Do NOT write a plain async function returning an iterator—that
        produces a coroutine and fails the override check.
        """

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release provider-owned resources (HTTP clients etc). Default: no-op."""
