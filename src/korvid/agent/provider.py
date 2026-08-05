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
        """Identifier of the model this adapter talks to.

        Shown in the status bar and recorded as `OutboundSnapshot.model`,
        so it must name the *model* — every built-in returns its model tag
        (`qwen3:8b`, `gpt-4o`), and a plugin may qualify it
        (`company-llm:v2`). It is not the endpoint or the vendor.
        """

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

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Adapt conversation history to this provider's wire dialect.

        Called *before* the outbound policy, so anything an adapter adds
        here is sanitized, size-checked and recorded in the exact payload
        snapshot the user can inspect — an adapter must never reshape
        messages inside `complete`, because that content would bypass the
        boundary. Default: the identity, so existing adapters keep
        sending exactly the messages the policy prepared.

        Args:
            messages: Conversation history, OpenAI-shaped, safe to consume
                (a private copy — mutating it cannot affect the runtime).

        Returns:
            The messages to hand to the policy, still OpenAI-shaped apart
            from provider-specific fields the policy knows how to
            sanitize.
        """
        return messages

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release provider-owned resources (HTTP clients etc). Default: no-op."""
