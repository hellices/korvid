"""ScriptedProvider: deterministic provider for harness smoke tests (issue #69).

Replays a fixed sequence of completions so CI can exercise the real
agent session (engine, gateway, tool harness, executor) without a live
model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from korvid.agent.model_policy import ModelCapabilities, ModelDescriptor
from korvid.agent.provider import LLMProvider


class ScriptedProvider(LLMProvider):
    """Yields one pre-scripted event batch per ``complete()`` call.

    Each batch is a list of provider events (``text_delta``, ``tool_call``,
    ``usage``) exactly as a live adapter would stream them. Exhausting the
    script raises — a scripted run must never need more turns than scripted.
    """

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        self._script = list(script)
        self._cursor = 0

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("scripted", "scripted")

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities.unknown()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        if self._cursor >= len(self._script):
            raise RuntimeError(f"scripted provider exhausted after {self._cursor} completion(s)")
        batch = self._script[self._cursor]
        self._cursor += 1
        for event in batch:
            yield event
