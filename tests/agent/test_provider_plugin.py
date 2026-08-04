from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from korvid.agent.provider import LLMProvider
from korvid.agent.provider_plugin import (
    PROVIDER_PLUGIN_API_VERSION,
    ProviderPlugin,
    ProviderPluginConfig,
    ProviderPluginContractError,
    ProviderPluginMetadata,
    ValidatedPluginProvider,
)


class _ScriptedProvider(LLMProvider):
    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.close_calls = 0

    @property
    def name(self) -> str:
        return "scripted-provider"

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        for event in self._events:
            yield event  # type: ignore[misc]  # tests inject malformed payloads on purpose

    async def aclose(self) -> None:
        self.close_calls += 1


class _ConcretePlugin(ProviderPlugin):
    @property
    def metadata(self) -> ProviderPluginMetadata:
        return ProviderPluginMetadata(
            api_version=PROVIDER_PLUGIN_API_VERSION,
            name="scripted",
            display_name="Scripted",
            auth_methods=("none",),
        )

    def create(
        self,
        config: ProviderPluginConfig,
        credentials: object | None,
    ) -> LLMProvider:
        del config, credentials
        return _ScriptedProvider([{"type": "done"}])


async def _collect_events(provider: LLMProvider) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async for event in provider.complete([], []):
        events.append(event)
    return events


def test_metadata_and_config_are_frozen() -> None:
    metadata = ProviderPluginMetadata(
        api_version=PROVIDER_PLUGIN_API_VERSION,
        name="company-gateway",
        display_name="Company Gateway",
        auth_methods=("api_key", "none"),
    )
    config = ProviderPluginConfig(
        base_url="https://llm.infra.local",
        model="internal-k8s-agent",
        auth_method="api_key",
        api_key_env="COMPANY_LLM_TOKEN",
        options={"tenant": "platform"},
    )

    assert metadata.auth_methods == ("api_key", "none")
    assert config.options["tenant"] == "platform"

    with pytest.raises(FrozenInstanceError, match="cannot assign to field"):
        metadata.name = "other"  # type: ignore[misc]  # exercising frozen dataclass runtime guard

    with pytest.raises(FrozenInstanceError, match="cannot assign to field"):
        config.model = "other"  # type: ignore[misc]  # exercising frozen dataclass runtime guard


def test_provider_plugin_is_abstract() -> None:
    with pytest.raises(TypeError, match="abstract"):
        ProviderPlugin()  # type: ignore[abstract]  # instantiating the ABC is the test


def test_validated_plugin_provider_requires_llm_provider_instance() -> None:
    with pytest.raises(ProviderPluginContractError, match="LLMProvider"):
        ValidatedPluginProvider(object())


def test_concrete_plugin_returns_provider_instance() -> None:
    plugin = _ConcretePlugin()
    provider = plugin.create(
        ProviderPluginConfig(
            base_url=None,
            model=None,
            auth_method=None,
            api_key_env=None,
            options={},
        ),
        credentials=None,
    )

    assert plugin.metadata.api_version == PROVIDER_PLUGIN_API_VERSION
    assert isinstance(provider, LLMProvider)


async def test_validated_plugin_provider_normalizes_all_event_shapes() -> None:
    wrapped = ValidatedPluginProvider(
        _ScriptedProvider(
            [
                {"type": "text_delta", "text": "hello", "ignored": "x"},
                {
                    "type": "tool_call",
                    "id": "c1",
                    "name": "get_logs",
                    "arguments": '{"pod":"web-1"}',
                    "ignored": "x",
                },
                {"type": "usage", "input_tokens": 12, "output_tokens": 3, "ignored": "x"},
                {"type": "done", "ignored": "x"},
            ]
        )
    )

    assert wrapped.name == "scripted-provider"
    assert await _collect_events(wrapped) == [
        {"type": "text_delta", "text": "hello"},
        {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": '{"pod":"web-1"}'},
        {"type": "usage", "input_tokens": 12, "output_tokens": 3},
        {"type": "done"},
    ]


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (["done"], "mapping"),
        ({"type": "unknown"}, "unknown provider event type"),
        ({"type": "text_delta", "text": 3}, "text_delta.text"),
        ({"type": "tool_call", "id": "", "name": "get_logs", "arguments": "{}"}, "tool_call.id"),
        (
            {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": {}},
            "tool_call.arguments",
        ),
        ({"type": "usage", "input_tokens": -1, "output_tokens": 0}, "usage.input_tokens"),
        ({"type": "usage", "input_tokens": 0, "output_tokens": True}, "usage.output_tokens"),
    ],
)
async def test_validated_plugin_provider_rejects_malformed_events(
    event: object,
    message: str,
) -> None:
    wrapped = ValidatedPluginProvider(_ScriptedProvider([event]))

    with pytest.raises(ProviderPluginContractError, match=message):
        await _collect_events(wrapped)


async def test_validated_plugin_provider_closes_underlying_provider_once() -> None:
    inner = _ScriptedProvider([{"type": "done"}])
    wrapped = ValidatedPluginProvider(inner)

    await wrapped.aclose()
    await wrapped.aclose()

    assert inner.close_calls == 1
