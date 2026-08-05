from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from korvid.agent.outbound import OutboundPolicyError, provider_prepared_messages
from korvid.agent.provider import LLMProvider


def test_provider_is_abstract() -> None:
    with pytest.raises(TypeError, match="abstract"):
        LLMProvider()  # type: ignore[abstract]  # instantiating ABC is the test


class _ConcreteProvider(LLMProvider):
    """Minimal concrete subclass that lets mypy --strict validate the override seam."""

    @property
    def name(self) -> str:
        return "test-provider"

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "text", "content": "hello"}


async def test_concrete_provider_yields_event() -> None:
    provider = _ConcreteProvider()
    assert provider.name == "test-provider"
    events: list[dict[str, Any]] = []
    async for event in provider.complete([], []):
        events.append(event)
        break
    assert len(events) == 1
    assert events[0]["type"] == "text"


def test_api_v1_provider_keeps_the_identity_message_hook() -> None:
    """Adapters written against API v1 never implemented `prepare_messages`;
    the default must leave their requests exactly as the policy prepared
    them (issue #189)."""
    provider = _ConcreteProvider()
    messages = [{"role": "user", "content": "hi"}]

    assert provider.prepare_messages(messages) == messages


def test_provider_message_hook_runs_before_the_policy() -> None:
    """Whatever an adapter adds must be sanitized and snapshotted, so the
    hook output — not the runtime history — is what the policy sees."""

    class _Augmenting(_ConcreteProvider):
        def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [{**message, "thinking": 'saw api_key: "raw"'} for message in messages]

    history = [{"role": "user", "content": "hi"}]
    prepared = provider_prepared_messages(_Augmenting(), history)

    assert prepared[0]["thinking"] == 'saw api_key: "raw"'
    assert history == [{"role": "user", "content": "hi"}]


def test_a_mutating_hook_cannot_reach_the_runtime_history() -> None:
    class _Mutating(_ConcreteProvider):
        def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            messages[0]["content"] = "rewritten"
            return messages

    history = [{"role": "user", "content": "hi"}]

    prepared = provider_prepared_messages(_Mutating(), history)

    assert prepared[0]["content"] == "rewritten"
    assert history[0]["content"] == "hi"


def test_a_failing_or_malformed_hook_blocks_the_request() -> None:
    class _Raising(_ConcreteProvider):
        def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            raise RuntimeError("secret detail sk-should-not-surface")

    class _Malformed(_ConcreteProvider):
        def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return cast(list[dict[str, Any]], "not a list")

    with pytest.raises(OutboundPolicyError, match="provider message preparation failed") as raised:
        provider_prepared_messages(_Raising(), [])
    assert "sk-should-not-surface" not in str(raised.value)
    with pytest.raises(OutboundPolicyError, match="returned an invalid shape"):
        provider_prepared_messages(_Malformed(), [])
