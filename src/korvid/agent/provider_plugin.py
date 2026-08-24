"""Public provider-plugin contract for third-party LLM adapters."""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Final

from korvid.agent.credentials import CredentialSource
from korvid.agent.model_policy import CapabilitySource, ModelCapabilities, ModelDescriptor
from korvid.agent.provider import LLMProvider

PROVIDER_PLUGIN_API_VERSION: Final[int] = 2
_MAX_TEXT_DELTA_BYTES: Final[int] = 65_536
_MAX_TOOL_CALL_FIELD_LENGTH: Final[int] = 256
_MAX_TOOL_ARGUMENTS_BYTES: Final[int] = 65_536
_MAX_USAGE_TOKENS: Final[int] = 1_000_000_000
_MAX_MODEL_ID_LENGTH: Final[int] = 256


def _known_capability_fact_names() -> frozenset[str]:
    """Return the `ModelCapabilities` fact names allowed in provenance."""
    return frozenset(
        field.name for field in fields(ModelCapabilities) if field.name != "provenance"
    )


_KNOWN_CAPABILITY_FACTS: Final[frozenset[str]] = _known_capability_fact_names()


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


def _validate_plugin_descriptor(descriptor: object, provider_id: str | None) -> ModelDescriptor:
    """Validate and copy-own a plugin-reported `ModelDescriptor`.

    `provider_id` is the plugin's normalized, registered name — when the
    caller (the plugin registry) supplies it, a mismatching
    `descriptor.provider` means the plugin is claiming to be a different
    provider than the one it was loaded as, which is rejected.
    """
    if not isinstance(descriptor, ModelDescriptor):
        raise ProviderPluginContractError(
            "provider plugin descriptor must be a ModelDescriptor instance"
        )
    if provider_id is not None and descriptor.provider != provider_id:
        raise ProviderPluginContractError(
            "provider plugin descriptor's provider id does not match its registered name"
        )
    if not descriptor.model or len(descriptor.model) > _MAX_MODEL_ID_LENGTH:
        raise ProviderPluginContractError(
            f"provider plugin descriptor model id must be non-empty and at most "
            f"{_MAX_MODEL_ID_LENGTH} characters"
        )
    return ModelDescriptor(descriptor.provider, descriptor.model)


def _validate_plugin_capabilities(capabilities: object) -> ModelCapabilities:
    """Validate and copy-own a plugin-reported `ModelCapabilities`.

    Every provenance key must name a known capability fact and every value
    must be a real `CapabilitySource` member — a plugin's own object is
    never trusted; a fresh instance is built from its (validated) fields.
    """
    if not isinstance(capabilities, ModelCapabilities):
        raise ProviderPluginContractError(
            "provider plugin capabilities must be a ModelCapabilities instance"
        )
    for fact, source in capabilities.provenance.items():
        if fact not in _KNOWN_CAPABILITY_FACTS:
            raise ProviderPluginContractError(
                "provider plugin capabilities provenance names an unknown fact"
            )
        if not isinstance(source, CapabilitySource):
            raise ProviderPluginContractError(
                "provider plugin capabilities provenance values must be CapabilitySource"
            )
    return ModelCapabilities(
        context_window_tokens=capabilities.context_window_tokens,
        supports_tools=capabilities.supports_tools,
        supports_parallel_tools=capabilities.supports_parallel_tools,
        supports_reasoning=capabilities.supports_reasoning,
        recommended_tier=capabilities.recommended_tier,
        provenance=dict(capabilities.provenance),
    )


def _read_plugin_fact(provider: LLMProvider, name: str) -> object:
    """Read one plugin-reported property, translating anything it raises.

    A `descriptor`/`capabilities` property is plugin code: it may probe an
    endpoint or read a credential, and fail carrying whatever it was
    holding. Re-raising that unchanged would end a start with a
    third party's exception — including one spelling itself
    `ProviderPluginContractError` with a payload of its own — so the type
    name is all that survives.

    `BaseException` is deliberately not caught: a cancellation or a
    `KeyboardInterrupt` during startup is not a contract violation.
    """
    try:
        return getattr(provider, name)
    except Exception as exc:
        exc_type = type(exc).__name__
        raise ProviderPluginContractError(
            f"provider plugin {name} read failed: {_bounded_exception(exc_type)}"
        ) from None


class ValidatedPluginProvider(LLMProvider):
    """LLMProvider wrapper that enforces the provider plugin event contract."""

    def __init__(self, provider: object, *, provider_id: str | None = None) -> None:
        if not isinstance(provider, LLMProvider):
            raise ProviderPluginContractError("provider plugin must return an LLMProvider instance")
        self._provider = provider
        self._closed = False
        # Both reads run third-party code (a lazy probe, a credential
        # read), so each is wrapped on its own: the message says which
        # fact korvid could not obtain, and names only the exception type
        # — never what the plugin's exception carried.
        self._descriptor = _validate_plugin_descriptor(
            _read_plugin_fact(provider, "descriptor"), provider_id
        )
        self._capabilities = _validate_plugin_capabilities(
            _read_plugin_fact(provider, "capabilities")
        )

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        # Iterator creation itself may raise (synchronous Exception from
        # plugin code or wrong return type causing protocol error later).
        try:
            iterator = self._provider.complete(messages, tools, stream=stream)
        except Exception as exc:
            exc_type = type(exc).__name__
            raise ProviderPluginContractError(
                f"provider stream creation failed: {_bounded_exception(exc_type)}"
            ) from None
        done_seen = False
        try:
            while True:
                # Advance the underlying plugin iterator — translate ALL
                # exceptions (including ProviderPluginContractError with
                # secret payload) to a fixed bounded message.
                try:
                    event = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                except Exception as exc:
                    exc_type = type(exc).__name__
                    raise ProviderPluginContractError(
                        f"provider stream failed: {_bounded_exception(exc_type)}"
                    ) from None
                # Our own validation — errors here are safe (fixed messages).
                normalized = _normalize_event(event)
                if done_seen:
                    raise ProviderPluginContractError("provider emitted event after terminal done")
                if normalized.get("type") == "done":
                    done_seen = True
                yield normalized
            if not done_seen:
                raise ProviderPluginContractError(
                    "provider stream ended without terminal done event"
                )
        except BaseException:
            # Close iterator, suppress close errors to preserve the primary
            # exception/cancellation.  Never leak close exception payload.
            await _close_iterator_suppress(iterator)
            raise
        else:
            # Normal completion — close iterator; translate close Exception
            # to fixed ProviderPluginContractError.
            await _close_iterator_or_raise(iterator)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._provider.aclose()
        except Exception:
            raise ProviderPluginContractError("provider plugin close failed") from None


async def _close_iterator_suppress(iterator: object) -> None:
    """Close the underlying async iterator, suppressing all exceptions.

    Used when preserving a primary exception/cancellation — the close error
    must never replace or leak into the primary error.
    """
    aclose = getattr(iterator, "aclose", None)
    if callable(aclose):
        with contextlib.suppress(BaseException):
            await aclose()


async def _close_iterator_or_raise(iterator: object) -> None:
    """Close the underlying async iterator on normal completion.

    Translates a close Exception to a fixed ProviderPluginContractError;
    BaseException (cancellation) propagates unchanged.
    """
    aclose = getattr(iterator, "aclose", None)
    if callable(aclose):
        try:
            await aclose()
        except Exception:
            raise ProviderPluginContractError("provider iterator close failed") from None


def _bounded_exception(label: str) -> str:
    """Truncate an exception type name to _MAX_EXCEPTION_BYTES UTF-8 bytes."""
    encoded = label.encode("utf-8")
    if len(encoded) <= _MAX_EXCEPTION_BYTES:
        return label
    return encoded[:_MAX_EXCEPTION_BYTES].decode("utf-8", errors="ignore") + "..."


def _safe_mapping_get(event: Mapping[object, object], key: str) -> object:
    """Read a key from a plugin-controlled Mapping, translating any exception.

    Plugin Mappings may override __getitem__/get/keys to raise arbitrary
    exceptions with secret payloads.  This isolates every read so only our
    own validation code produces diagnostic messages.
    """
    try:
        return event.get(key)
    except Exception:
        raise ProviderPluginContractError(
            "provider event mapping raised during field access"
        ) from None


def _normalize_event(event: object) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ProviderPluginContractError("provider events must be mapping objects")

    event_type = _safe_mapping_get(event, "type")
    if event_type == "text_delta":
        return {
            "type": "text_delta",
            "text": _require_byte_bounded_str(
                event,
                "text_delta.text",
                "text",
                max_bytes=_MAX_TEXT_DELTA_BYTES,
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
            "arguments": _require_byte_bounded_str(
                event,
                "tool_call.arguments",
                "arguments",
                max_bytes=_MAX_TOOL_ARGUMENTS_BYTES,
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
    value = _safe_mapping_get(event, key)
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


def _require_byte_bounded_str(
    event: Mapping[object, object],
    label: str,
    key: str,
    *,
    max_bytes: int,
) -> str:
    """Validate a string field against a UTF-8 byte limit."""
    value = _require_str(event, label, key)
    if len(value.encode("utf-8")) > max_bytes:
        raise ProviderPluginContractError(f"{label} exceeds max {max_bytes} UTF-8 bytes")
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
    value = _safe_mapping_get(event, key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > max_value:
        raise ProviderPluginContractError(f"{label} must be a non-negative int <= {max_value}")
    return value
