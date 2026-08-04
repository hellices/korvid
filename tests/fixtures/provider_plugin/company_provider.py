"""Fixture: a valid third-party provider plugin for testing discovery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from korvid.agent.credentials import CredentialSource
from korvid.agent.provider import LLMProvider
from korvid.agent.provider_plugin import (
    PROVIDER_PLUGIN_API_VERSION,
    ProviderPlugin,
    ProviderPluginConfig,
    ProviderPluginMetadata,
)


class _CompanyLLMProvider(LLMProvider):
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
        yield {"type": "text_delta", "text": "hello"}
        yield {"type": "done"}

    async def aclose(self) -> None:
        pass


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
        return _CompanyLLMProvider()
