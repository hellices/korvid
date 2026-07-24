from collections.abc import AsyncIterator
from typing import Any

import pytest

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
