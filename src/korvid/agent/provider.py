"""LLMProvider ABC — the pluggable boundary (design doc §6.3, standards §3).

Concrete adapters live in korvid/providers/ and are selected by the
config-driven factory in korvid.providers.registry. Third-party adapters
target the public contract in korvid.agent.provider_plugin; their
discovery/loading stays outside this ABC boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Final

from korvid.agent.model_policy import ModelCapabilities, ModelDescriptor

REQUEST_SENT: Final = "request_sent"
"""Event type a built-in adapter yields once its request is on the wire.

`complete` is an async generator, so obtaining it transmits nothing: the
body only runs on the first `__anext__`. The runtime records the exact
payload it handed over as the session's latest outbound request, and that
record must mean *sent*, not *intended* — a missing credential or an
unresolvable host must leave the previous real handoff on display.

Built-in adapters therefore yield `{"type": REQUEST_SENT}` as soon as the
transport has accepted the request (response headers received), before
the status code is judged: an HTTP 500 answer still means the provider
has the payload. The runtime consumes it as bookkeeping and never renders
it. It is internal to the built-ins — the plugin contract (API v1) knows
four event types and rejects anything else, so a plugin's request is
recorded on its first completion event instead, which is equally proof
that the request ran.
"""


class LLMProvider(ABC):
    @property
    @abstractmethod
    def descriptor(self) -> ModelDescriptor:
        """Identify the model this adapter talks to by provider and model tag.

        `descriptor.model` is shown in the status bar and recorded as
        `OutboundSnapshot.model`, so it must name the *model* — every
        built-in returns its model tag (`qwen3:8b`, `gpt-4o`), and a
        plugin may qualify it (`company-llm:v2`). It is not the endpoint.

        `descriptor.provider` is the canonical provider id (`ollama`,
        `openai-compat`, `github-copilot`, or a plugin's registered name)
        — a built-in adapter never guesses it from the base URL or model
        name; the registry/factory that constructed it passes it in.
        """

    @property
    @abstractmethod
    def capabilities(self) -> ModelCapabilities:
        """Report the model facts this adapter directly knows.

        Any fact the adapter cannot directly prove — from an explicit
        per-request option (e.g. Ollama's `num_ctx`) or explicit config —
        stays unknown (`None`). Adapters must never infer capability from
        the model or provider name; `ModelCapabilities.unknown()` is the
        correct answer absent direct evidence.
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

        A built-in adapter yields `{"type": REQUEST_SENT}` once the
        transport has accepted the request, so the runtime can tell a
        payload that was really handed over from one whose generator was
        never started. Adapters that cannot say (including every plugin)
        simply do not, and are taken at their first completion event.
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
