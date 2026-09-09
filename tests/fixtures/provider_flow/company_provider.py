"""Fixture: a model provider published as a SpecialFlow."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from korvid.agent.model_policy import ModelCapabilities, ModelDescriptor
from korvid.agent.model_profiles import AuthMethodDescriptor, SpecialFlow
from korvid.agent.provider import LLMProvider
from korvid.core.config import ModelConnectionConfig


class _CompanyLLMProvider(LLMProvider):
    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        self._turns = turns
        self.calls: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[dict[str, Any]]] = []
        self.close_calls = 0

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("company-llm", "company-llm-v1")

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
        del stream
        self.calls.append([dict(message) for message in messages])
        self.tools_seen.append([dict(tool) for tool in tools])
        turn = self._turns.pop(0) if self._turns else [{"type": "done"}]
        for event in turn:
            if event.get("type") == "__raise_contract_error__":
                raise RuntimeError("SECRET_INTERNAL_TOKEN_xyz789" * 10)
            yield event

    async def aclose(self) -> None:
        self.close_calls += 1


def build_provider(profile: ModelConnectionConfig) -> LLMProvider:
    raw_turns = profile.options.get("scripted_turns")
    if not isinstance(raw_turns, list | tuple):
        raise ValueError("scripted_turns must be a sequence")
    turns: list[list[dict[str, Any]]] = []
    for raw_turn in raw_turns:
        if not isinstance(raw_turn, list | tuple):
            raise ValueError("each scripted turn must be a sequence")
        turn: list[dict[str, Any]] = []
        for event in raw_turn:
            if not isinstance(event, Mapping):
                raise ValueError("each scripted event must be a mapping")
            turn.append(dict(event))
        turns.append(turn)
    return _CompanyLLMProvider(turns)


def korvid_special_flows() -> tuple[SpecialFlow, ...]:
    return (
        SpecialFlow(
            prefix="company-llm",
            display_name="Company LLM",
            auth_methods=(AuthMethodDescriptor("none", "No authentication"),),
            build_provider=build_provider,
        ),
    )
