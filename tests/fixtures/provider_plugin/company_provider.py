"""Fixture: a valid third-party provider plugin for testing discovery."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from korvid.agent.credentials import CredentialSource
from korvid.agent.provider import LLMProvider
from korvid.agent.provider_plugin import (
    PROVIDER_PLUGIN_API_VERSION,
    ProviderPlugin,
    ProviderPluginConfig,
    ProviderPluginContractError,
    ProviderPluginMetadata,
)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class _CompanyLLMProvider(LLMProvider):
    def __init__(self, turns: list[list[object]]) -> None:
        self._turns = turns
        self.calls: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[dict[str, Any]]] = []
        self.close_calls = 0

    @property
    def name(self) -> str:
        return "company-llm"

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
            if isinstance(event, dict) and event.get("type") == "__raise_contract_error__":
                raise ProviderPluginContractError("SECRET_INTERNAL_TOKEN_xyz789" * 10)
            yield event  # type: ignore[misc]  # tests inject malformed payloads on purpose

    async def aclose(self) -> None:
        self.close_calls += 1


class CompanyProviderPlugin(ProviderPlugin):
    @property
    def metadata(self) -> ProviderPluginMetadata:
        return ProviderPluginMetadata(
            api_version=PROVIDER_PLUGIN_API_VERSION,
            name="company-llm",
            display_name="Company LLM",
            auth_methods=("api_key",),
        )

    def create(
        self,
        config: ProviderPluginConfig,
        credentials: CredentialSource | None,
    ) -> LLMProvider:
        del credentials
        if config.options.get("raise_in_create"):
            raise RuntimeError("fixture create failed")
        raw_turns = _thaw(config.options.get("scripted_turns"))
        turns = raw_turns if isinstance(raw_turns, list) else None
        if turns is None:
            turns = [[{"type": "text_delta", "text": "hello"}, {"type": "done"}]]
        return _CompanyLLMProvider(turns)
