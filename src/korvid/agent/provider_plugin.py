"""Public provider-plugin contract for third-party LLM adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from korvid.agent.credentials import CredentialSource
from korvid.agent.provider import LLMProvider

PROVIDER_PLUGIN_API_VERSION: Final[int] = 1
_MAX_TEXT_DELTA_LENGTH: Final[int] = 65_536
_MAX_TOOL_CALL_FIELD_LENGTH: Final[int] = 256
_MAX_TOOL_ARGUMENTS_LENGTH: Final[int] = 65_536
_MAX_USAGE_TOKENS: Final[int] = 1_000_000_000


@dataclass(frozen=True)
class ProviderPluginMetadata:
    api_version: int
    name: str
    display_name: str
    auth_methods: tuple[str, ...]
    supports_generic_setup: bool = True


@dataclass(frozen=True)
class ProviderPluginConfig:
    base_url: str | None
    model: str | None
    auth_method: str | None
    api_key_env: str | None
    options: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


class ProviderPlugin(ABC):
    @property
    @abstractmethod
    def metadata(self) -> ProviderPluginMetadata: ...

    @abstractmethod
    def create(
        self,
        config: ProviderPluginConfig,
        credentials: CredentialSource | None,
    ) -> LLMProvider: ...


class ProviderPluginContractError(Exception):
    """Raised when a provider plugin violates the published contract."""


_MAX_EXCEPTION_BYTES: Final[int] = 2048


class ValidatedPluginProvider(LLMProvider):
    """LLMProvider wrapper that enforces the provider plugin event contract."""

    def __init__(self, provider: object) -> None:
        if not isinstance(provider, LLMProvider):
            raise ProviderPluginContractError("provider plugin must return an LLMProvider instance")
        self._provider = provider
        self._closed = False

    @property
    def name(self) -> str:
        return self._provider.name

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            async for event in self._provider.complete(messages, tools, stream=stream):
                yield _normalize_event(event)
        except ProviderPluginContractError:
            raise
        except Exception as exc:
            exc_type = type(exc).__name__
            raise ProviderPluginContractError(
                f"provider stream failed: {_bounded_exception(exc_type)}"
            ) from None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._provider.aclose()


def _bounded_exception(label: str) -> str:
    """Truncate an exception type name to _MAX_EXCEPTION_BYTES UTF-8 bytes."""
    encoded = label.encode("utf-8")
    if len(encoded) <= _MAX_EXCEPTION_BYTES:
        return label
    return encoded[:_MAX_EXCEPTION_BYTES].decode("utf-8", errors="ignore") + "..."


def _normalize_event(event: object) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ProviderPluginContractError("provider events must be mapping objects")

    event_type = event.get("type")
    if event_type == "text_delta":
        return {
            "type": "text_delta",
            "text": _require_bounded_str(
                event,
                "text_delta.text",
                "text",
                max_length=_MAX_TEXT_DELTA_LENGTH,
            ),
        }
    if event_type == "tool_call":
        return {
            "type": "tool_call",
            "id": _require_non_empty_str(
                event,
                "tool_call.id",
                "id",
                max_length=_MAX_TOOL_CALL_FIELD_LENGTH,
            ),
            "name": _require_non_empty_str(
                event,
                "tool_call.name",
                "name",
                max_length=_MAX_TOOL_CALL_FIELD_LENGTH,
            ),
            "arguments": _require_bounded_str(
                event,
                "tool_call.arguments",
                "arguments",
                max_length=_MAX_TOOL_ARGUMENTS_LENGTH,
            ),
        }
    if event_type == "usage":
        return {
            "type": "usage",
            "input_tokens": _require_non_negative_int(
                event,
                "usage.input_tokens",
                "input_tokens",
                max_value=_MAX_USAGE_TOKENS,
            ),
            "output_tokens": _require_non_negative_int(
                event,
                "usage.output_tokens",
                "output_tokens",
                max_value=_MAX_USAGE_TOKENS,
            ),
        }
    if event_type == "done":
        return {"type": "done"}
    raise ProviderPluginContractError("unknown provider event type")


def _require_str(event: Mapping[object, object], label: str, key: str) -> str:
    value = event.get(key)
    if not isinstance(value, str):
        raise ProviderPluginContractError(f"{label} must be str")
    return value


def _require_bounded_str(
    event: Mapping[object, object],
    label: str,
    key: str,
    *,
    max_length: int,
) -> str:
    value = _require_str(event, label, key)
    if len(value) > max_length:
        raise ProviderPluginContractError(f"{label} exceeds max length {max_length}")
    return value


def _require_non_empty_str(
    event: Mapping[object, object],
    label: str,
    key: str,
    *,
    max_length: int,
) -> str:
    value = _require_bounded_str(event, label, key, max_length=max_length)
    if not value:
        raise ProviderPluginContractError(f"{label} must be non-empty")
    return value


def _require_non_negative_int(
    event: Mapping[object, object],
    label: str,
    key: str,
    *,
    max_value: int,
) -> int:
    value = event.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > max_value:
        raise ProviderPluginContractError(f"{label} must be a non-negative int <= {max_value}")
    return value
