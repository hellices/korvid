from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import FrozenInstanceError, fields
from typing import Any

import pytest

from korvid.agent.credentials import CredentialSource
from korvid.agent.model_policy import (
    CapabilitySource,
    ModelCapabilities,
    ModelDescriptor,
    ModelTier,
)
from korvid.agent.provider import REQUEST_SENT, LLMProvider
from korvid.agent.provider_plugin import (
    PROVIDER_PLUGIN_API_VERSION,
    ProviderPlugin,
    ProviderPluginConfig,
    ProviderPluginContractError,
    ProviderPluginMetadata,
    ValidatedPluginProvider,
    _known_capability_fact_names,
    _validate_plugin_capabilities,
)


class _ScriptedProvider(LLMProvider):
    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.close_calls = 0

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("test", "scripted-provider")

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
        credentials: CredentialSource | None,
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


def test_provider_plugin_api_version_is_v2() -> None:
    """The plugin contract is bumped to v2 for descriptor/capabilities
    reporting (issue #189) — every third-party plugin metadata must match."""
    assert PROVIDER_PLUGIN_API_VERSION == 2


def test_validated_plugin_provider_requires_llm_provider_instance() -> None:
    with pytest.raises(ProviderPluginContractError, match="LLMProvider"):
        ValidatedPluginProvider(object())


def test_validated_plugin_provider_exposes_descriptor_and_capabilities() -> None:
    """Step 3: the wrapper delegates both facts after validating them."""
    wrapped = ValidatedPluginProvider(_ScriptedProvider([{"type": "done"}]))
    assert wrapped.descriptor == ModelDescriptor("test", "scripted-provider")
    assert wrapped.capabilities == ModelCapabilities.unknown()


def test_validated_plugin_provider_rejects_descriptor_provider_id_mismatch() -> None:
    """The descriptor's provider id must match the plugin's registered
    (normalized) name — a plugin cannot claim to be a different provider."""
    with pytest.raises(ProviderPluginContractError, match="provider id"):
        ValidatedPluginProvider(
            _ScriptedProvider([{"type": "done"}]), provider_id="a-different-plugin"
        )


def test_validated_plugin_provider_translates_a_raising_descriptor_property() -> None:
    """A plugin's own exception from `descriptor` must not reach korvid raw.

    The wrapper reads the property while constructing, so third-party code
    runs before korvid has validated anything. A lazy credential read or
    endpoint probe in there can fail carrying whatever it was holding — so
    the failure becomes the same fixed, bounded contract error every other
    plugin fault does, naming the exception type and nothing else.
    """

    class _ExplodingDescriptor(_ScriptedProvider):
        @property
        def descriptor(self) -> ModelDescriptor:
            raise RuntimeError("PLUGIN_DESCRIPTOR_SECRET_abc123" * 20)

    with pytest.raises(ProviderPluginContractError) as caught:
        ValidatedPluginProvider(_ExplodingDescriptor([{"type": "done"}]))

    message = str(caught.value)
    assert "descriptor" in message
    assert "RuntimeError" in message
    assert "PLUGIN_DESCRIPTOR_SECRET" not in message
    assert len(message) < 512


def test_validated_plugin_provider_translates_a_raising_capabilities_property() -> None:
    """The capability read is wrapped on its own, so the message says which
    read failed without ever repeating what the plugin raised."""

    class _ExplodingCapabilities(_ScriptedProvider):
        @property
        def capabilities(self) -> ModelCapabilities:
            raise RuntimeError("PLUGIN_CAPABILITIES_SECRET_xyz789" * 20)

    with pytest.raises(ProviderPluginContractError) as caught:
        ValidatedPluginProvider(_ExplodingCapabilities([{"type": "done"}]))

    message = str(caught.value)
    assert "capabilities" in message
    assert "RuntimeError" in message
    assert "PLUGIN_CAPABILITIES_SECRET" not in message
    assert len(message) < 512


def test_a_raising_property_is_translated_even_when_it_raises_a_contract_error() -> None:
    """A plugin may raise korvid's own error type with a payload of its own.

    Re-raising it unchanged would publish that payload under a name the
    rest of korvid treats as safe, so the wrapper translates by exception
    type rather than by trusting the class.
    """

    class _ContractErrorDescriptor(_ScriptedProvider):
        @property
        def descriptor(self) -> ModelDescriptor:
            raise ProviderPluginContractError("PLUGIN_TOKEN_LEAK_qqq" * 20)

    with pytest.raises(ProviderPluginContractError) as caught:
        ValidatedPluginProvider(_ContractErrorDescriptor([{"type": "done"}]))

    assert "PLUGIN_TOKEN_LEAK" not in str(caught.value)


def test_a_raising_property_does_not_hide_cancellation() -> None:
    """`BaseException` is not a contract violation: a cancelled start must
    still cancel, not be reported as a broken plugin."""

    class _CancelledDescriptor(_ScriptedProvider):
        @property
        def descriptor(self) -> ModelDescriptor:
            raise KeyboardInterrupt("start interrupted")

    with pytest.raises(KeyboardInterrupt, match="start interrupted"):
        ValidatedPluginProvider(_CancelledDescriptor([{"type": "done"}]))


def test_validated_plugin_provider_rejects_empty_model_id() -> None:
    class _EmptyModelProvider(_ScriptedProvider):
        @property
        def descriptor(self) -> ModelDescriptor:
            return ModelDescriptor("test", "")

    with pytest.raises(ProviderPluginContractError, match="model"):
        ValidatedPluginProvider(_EmptyModelProvider([{"type": "done"}]))


def test_validated_plugin_provider_rejects_oversized_model_id() -> None:
    class _HugeModelProvider(_ScriptedProvider):
        @property
        def descriptor(self) -> ModelDescriptor:
            return ModelDescriptor("test", "m" * 10_000)

    with pytest.raises(ProviderPluginContractError, match="model"):
        ValidatedPluginProvider(_HugeModelProvider([{"type": "done"}]))


@pytest.mark.parametrize("field", ["provider", "model"])
@pytest.mark.parametrize("value", [42, " ", "m" * 257])
def test_validated_plugin_provider_rejects_invalid_descriptor_fields(
    field: str, value: object
) -> None:
    class _InvalidDescriptorProvider(_ScriptedProvider):
        @property
        def descriptor(self) -> ModelDescriptor:
            values: dict[str, object] = {
                "provider": "test",
                "model": "scripted-provider",
            }
            values[field] = value
            return ModelDescriptor(**values)  # type: ignore[arg-type]  # malformed plugin object

    with pytest.raises(ProviderPluginContractError, match=field):
        ValidatedPluginProvider(_InvalidDescriptorProvider([{"type": "done"}]))


@pytest.mark.parametrize("value", [True, 0, -1, "4096"])
def test_validated_plugin_provider_rejects_invalid_context_window(value: object) -> None:
    class _InvalidCapabilitiesProvider(_ScriptedProvider):
        @property
        def capabilities(self) -> ModelCapabilities:
            return ModelCapabilities(
                context_window_tokens=value  # type: ignore[arg-type]  # malformed plugin object
            )

    with pytest.raises(ProviderPluginContractError, match="context_window_tokens"):
        ValidatedPluginProvider(_InvalidCapabilitiesProvider([{"type": "done"}]))


@pytest.mark.parametrize(
    "field", ["supports_tools", "supports_parallel_tools", "supports_reasoning"]
)
@pytest.mark.parametrize("value", ["false", 0, 1])
def test_validated_plugin_provider_rejects_invalid_boolean_capabilities(
    field: str, value: object
) -> None:
    class _InvalidCapabilitiesProvider(_ScriptedProvider):
        @property
        def capabilities(self) -> ModelCapabilities:
            return ModelCapabilities(
                **{field: value}  # type: ignore[arg-type]  # malformed plugin object
            )

    with pytest.raises(ProviderPluginContractError, match=field):
        ValidatedPluginProvider(_InvalidCapabilitiesProvider([{"type": "done"}]))


def test_validated_plugin_provider_rejects_invalid_recommended_tier() -> None:
    class _InvalidCapabilitiesProvider(_ScriptedProvider):
        @property
        def capabilities(self) -> ModelCapabilities:
            return ModelCapabilities(
                recommended_tier="low"  # type: ignore[arg-type]  # malformed plugin object
            )

    with pytest.raises(ProviderPluginContractError, match="recommended_tier"):
        ValidatedPluginProvider(_InvalidCapabilitiesProvider([{"type": "done"}]))


def test_validated_plugin_provider_accepts_valid_capability_values() -> None:
    capabilities = ModelCapabilities(
        context_window_tokens=4096,
        supports_tools=True,
        supports_parallel_tools=False,
        supports_reasoning=None,
        recommended_tier=ModelTier.LOW,
    )

    assert _validate_plugin_capabilities(capabilities) == capabilities


def test_validated_plugin_provider_rejects_unknown_provenance_fact() -> None:
    class _BadProvenanceProvider(_ScriptedProvider):
        @property
        def capabilities(self) -> ModelCapabilities:
            return ModelCapabilities(provenance={"not_a_real_fact": CapabilitySource.PROVIDER})

    with pytest.raises(ProviderPluginContractError, match="provenance"):
        ValidatedPluginProvider(_BadProvenanceProvider([{"type": "done"}]))


def test_known_capability_fact_names_follow_model_capabilities_fields() -> None:
    expected_fact_names = frozenset(
        field.name for field in fields(ModelCapabilities) if field.name != "provenance"
    )

    assert _known_capability_fact_names() == expected_fact_names


def test_validated_plugin_provider_accepts_all_known_provenance_facts() -> None:
    for fact in _known_capability_fact_names():
        caps = ModelCapabilities(provenance={fact: CapabilitySource.PROVIDER})
        validated = _validate_plugin_capabilities(caps)
        assert validated.provenance[fact] is CapabilitySource.PROVIDER


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

    assert wrapped.descriptor == ModelDescriptor("test", "scripted-provider")
    assert wrapped.capabilities == ModelCapabilities.unknown()
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
        # The built-in handoff acknowledgement is not part of API v1:
        # a plugin is recorded on its first completion event instead.
        ({"type": REQUEST_SENT}, "unknown provider event type"),
        ({"type": "text_delta", "text": 3}, "text_delta.text"),
        ({"type": "tool_call", "id": "", "name": "get_logs", "arguments": "{}"}, "tool_call.id"),
        ({"type": "tool_call", "id": "c1", "name": "", "arguments": "{}"}, "tool_call.name"),
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


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ({"type": "text_delta", "text": "x" * 65_537}, "text_delta.text"),
        (
            {
                "type": "tool_call",
                "id": "c1",
                "name": "get_logs",
                "arguments": "x" * 65_537,
            },
            "tool_call.arguments",
        ),
        (
            {
                "type": "tool_call",
                "id": "x" * 257,
                "name": "get_logs",
                "arguments": "{}",
            },
            "tool_call.id",
        ),
        (
            {
                "type": "tool_call",
                "id": "c1",
                "name": "x" * 257,
                "arguments": "{}",
            },
            "tool_call.name",
        ),
        (
            {"type": "usage", "input_tokens": 1_000_000_001, "output_tokens": 0},
            "usage.input_tokens",
        ),
        (
            {"type": "usage", "input_tokens": 0, "output_tokens": 1_000_000_001},
            "usage.output_tokens",
        ),
    ],
)
async def test_validated_plugin_provider_rejects_out_of_bounds_events(
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


# --- Finding #2: stream exception containment ---


class _ExplodingProvider(LLMProvider):
    """Provider whose complete() raises an exception with secret payload."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("test", "exploding-provider")

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
        yield {"type": "text_delta", "text": "hello"}
        raise self._exc

    async def aclose(self) -> None:
        pass


async def test_validated_stream_translates_secret_exception_to_bounded_contract_error() -> None:
    """Exceptions from the underlying provider stream must be translated to
    bounded ProviderPluginContractError without raw payload."""
    secret_exc = RuntimeError("SUPER_SECRET_API_KEY_abc123xyz789" * 10)
    wrapped = ValidatedPluginProvider(_ExplodingProvider(secret_exc))

    with pytest.raises(ProviderPluginContractError, match="provider stream failed") as exc_info:
        async for _ in wrapped.complete([], []):
            pass
    msg = str(exc_info.value)
    assert "SUPER_SECRET" not in msg
    assert len(msg.encode("utf-8")) <= 2200  # bounded


async def test_validated_stream_preserves_contract_error() -> None:
    """ProviderPluginContractError from normalization must pass through."""
    bad_event_provider = _ScriptedProvider([{"type": "unknown_garbage"}])
    wrapped = ValidatedPluginProvider(bad_event_provider)

    with pytest.raises(ProviderPluginContractError, match="unknown provider event type"):
        async for _ in wrapped.complete([], []):
            pass


async def test_validated_stream_huge_exception_is_bounded() -> None:
    """Even a huge exception type name is bounded."""
    huge_exc = type("A" * 5000, (Exception,), {})("payload")
    wrapped = ValidatedPluginProvider(_ExplodingProvider(huge_exc))

    with pytest.raises(ProviderPluginContractError, match="provider stream failed") as exc_info:
        async for _ in wrapped.complete([], []):
            pass
    msg = str(exc_info.value)
    assert len(msg.encode("utf-8")) <= 2200


# --- Finding #2 (round 2): aclose() exception translation ---


async def test_validated_aclose_translates_secret_exception() -> None:
    """ValidatedPluginProvider.aclose() must translate underlying exceptions
    to fixed/bounded ProviderPluginContractError without raw payload."""

    class _ExplodingCloseProvider(LLMProvider):
        @property
        def descriptor(self) -> ModelDescriptor:
            return ModelDescriptor("test", "boom-closer")

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
            yield {"type": "done"}

        async def aclose(self) -> None:
            raise RuntimeError("SECRET_CREDENTIAL_abc123" * 20)

    wrapped = ValidatedPluginProvider(_ExplodingCloseProvider())
    with pytest.raises(ProviderPluginContractError, match="close failed") as exc_info:
        await wrapped.aclose()
    msg = str(exc_info.value)
    assert "SECRET_CREDENTIAL" not in msg


async def test_validated_aclose_preserves_exactly_once_guard() -> None:
    """Second aclose() on a ValidatedPluginProvider must be swallowed
    even after the first one raised."""

    class _OneShotExploder(LLMProvider):
        def __init__(self) -> None:
            self.close_calls = 0

        @property
        def descriptor(self) -> ModelDescriptor:
            return ModelDescriptor("test", "one-shot")

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
            yield {"type": "done"}

        async def aclose(self) -> None:
            self.close_calls += 1
            raise RuntimeError("boom")

    inner = _OneShotExploder()
    wrapped = ValidatedPluginProvider(inner)
    with pytest.raises(ProviderPluginContractError, match="close failed"):
        await wrapped.aclose()
    # Second close must be a no-op (exactly-once guard)
    await wrapped.aclose()
    assert inner.close_calls == 1


# --- Finding #2 (round 3): exactly one terminal done ---


async def test_validated_stream_zero_done_raises_contract_error() -> None:
    """Stream exhaustion without a done event must raise ProviderPluginContractError."""
    provider = _ScriptedProvider([{"type": "text_delta", "text": "hi"}])
    wrapped = ValidatedPluginProvider(provider)

    with pytest.raises(ProviderPluginContractError, match="without terminal done"):
        await _collect_events(wrapped)


async def test_validated_stream_event_after_done_raises_contract_error() -> None:
    """Any event after done (including a second done) must raise before yielding."""
    provider = _ScriptedProvider(
        [
            {"type": "text_delta", "text": "hi"},
            {"type": "done"},
            {"type": "text_delta", "text": "ghost"},
        ]
    )
    wrapped = ValidatedPluginProvider(provider)

    with pytest.raises(ProviderPluginContractError, match="after terminal done"):
        await _collect_events(wrapped)


async def test_validated_stream_double_done_raises() -> None:
    """A second done must raise."""
    provider = _ScriptedProvider([{"type": "done"}, {"type": "done"}])
    wrapped = ValidatedPluginProvider(provider)

    with pytest.raises(ProviderPluginContractError, match="after terminal done"):
        await _collect_events(wrapped)


async def test_validated_stream_normal_done_passes() -> None:
    """Normal stream with exactly one terminal done must pass."""
    provider = _ScriptedProvider(
        [
            {"type": "text_delta", "text": "hello"},
            {"type": "usage", "input_tokens": 5, "output_tokens": 2},
            {"type": "done"},
        ]
    )
    wrapped = ValidatedPluginProvider(provider)
    events = await _collect_events(wrapped)
    assert events == [
        {"type": "text_delta", "text": "hello"},
        {"type": "usage", "input_tokens": 5, "output_tokens": 2},
        {"type": "done"},
    ]


# --- Finding #4 (round 3): UTF-8 byte limits for text and arguments ---


async def test_text_delta_accepts_exact_utf8_byte_boundary() -> None:
    """A text_delta with exactly 65,536 UTF-8 bytes must pass."""
    # Each CJK char is 3 UTF-8 bytes; 65_536 / 3 = 21845.33 → use 21845 CJK chars (65535 bytes)
    # then one ASCII byte = 65536 total
    text = "\u4e00" * 21845 + "x"  # 21845*3 + 1 = 65536 bytes
    assert len(text.encode("utf-8")) == 65_536
    provider = _ScriptedProvider([{"type": "text_delta", "text": text}, {"type": "done"}])
    wrapped = ValidatedPluginProvider(provider)
    events = await _collect_events(wrapped)
    assert events[0] == {"type": "text_delta", "text": text}


async def test_text_delta_rejects_one_byte_over_utf8_boundary() -> None:
    """A text_delta with 65,537 UTF-8 bytes must be rejected."""
    text = "\u4e00" * 21845 + "xy"  # 65537 bytes
    assert len(text.encode("utf-8")) == 65_537
    provider = _ScriptedProvider([{"type": "text_delta", "text": text}, {"type": "done"}])
    wrapped = ValidatedPluginProvider(provider)

    with pytest.raises(ProviderPluginContractError, match=r"text_delta\.text"):
        await _collect_events(wrapped)


async def test_tool_arguments_accepts_exact_utf8_byte_boundary() -> None:
    """tool_call.arguments with exactly 65,536 UTF-8 bytes must pass."""
    args = "\u4e00" * 21845 + "x"  # 65536 bytes
    assert len(args.encode("utf-8")) == 65_536
    provider = _ScriptedProvider(
        [
            {"type": "tool_call", "id": "c1", "name": "run", "arguments": args},
            {"type": "done"},
        ]
    )
    wrapped = ValidatedPluginProvider(provider)
    events = await _collect_events(wrapped)
    assert events[0]["arguments"] == args


async def test_tool_arguments_rejects_one_byte_over_utf8_boundary() -> None:
    """tool_call.arguments with 65,537 UTF-8 bytes must be rejected."""
    args = "\u4e00" * 21845 + "xy"  # 65537 bytes
    assert len(args.encode("utf-8")) == 65_537
    provider = _ScriptedProvider(
        [
            {"type": "tool_call", "id": "c1", "name": "run", "arguments": args},
            {"type": "done"},
        ]
    )
    wrapped = ValidatedPluginProvider(provider)

    with pytest.raises(ProviderPluginContractError, match=r"tool_call\.arguments"):
        await _collect_events(wrapped)


# --- Finding #1 round 7: underlying ProviderPluginContractError with secret ---


class _SecretContractErrorProvider(LLMProvider):
    """Provider that raises ProviderPluginContractError with secret payload."""

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("test", "secret-error-provider")

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
        yield {"type": "text_delta", "text": "hi"}
        raise ProviderPluginContractError("SECRET_INTERNAL_TOKEN_xyz789_LEAKED" * 10)

    async def aclose(self) -> None:
        pass


async def test_underlying_contract_error_with_secret_is_translated() -> None:
    """ProviderPluginContractError raised by the plugin iterator (not our
    normalization) must be translated to a fixed bounded message without
    exposing the raw secret payload."""
    wrapped = ValidatedPluginProvider(_SecretContractErrorProvider())

    with pytest.raises(ProviderPluginContractError, match="provider stream failed") as exc_info:
        await _collect_events(wrapped)
    msg = str(exc_info.value)
    assert "SECRET_INTERNAL_TOKEN" not in msg
    assert len(msg) <= 300


async def test_our_normalize_event_errors_are_still_preserved() -> None:
    """Errors from _normalize_event (our own validation) must still produce
    specific error messages (e.g. 'unknown provider event type')."""
    provider = _ScriptedProvider([{"type": "bogus_event"}, {"type": "done"}])
    wrapped = ValidatedPluginProvider(provider)

    with pytest.raises(ProviderPluginContractError, match="unknown provider event type"):
        await _collect_events(wrapped)


async def test_our_done_check_errors_are_preserved() -> None:
    """Errors from our done-state checks must have specific messages."""
    provider = _ScriptedProvider([{"type": "done"}, {"type": "done"}])
    wrapped = ValidatedPluginProvider(provider)

    with pytest.raises(ProviderPluginContractError, match="after terminal done"):
        await _collect_events(wrapped)


# --- Round 7 follow-up: iterator cleanup on all exit paths ---


class _TrackingCloseProvider(LLMProvider):
    """Provider whose async generator tracks close calls."""

    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.close_calls = 0
        self.close_exception: Exception | None = None

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("test", "tracking-close")

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
        try:
            for event in self._events:
                yield event  # type: ignore[misc]
        finally:
            self.close_calls += 1
            if self.close_exception is not None:
                raise self.close_exception

    async def aclose(self) -> None:
        pass


async def test_iterator_closed_on_malformed_event_rejection() -> None:
    """When wrapper rejects a malformed event, the underlying iterator's
    finally/aclose must still fire."""
    inner = _TrackingCloseProvider(
        [
            {"type": "text_delta", "text": "hi"},
            {"type": "bogus"},  # triggers normalization error
        ]
    )
    wrapped = ValidatedPluginProvider(inner)

    with pytest.raises(ProviderPluginContractError, match="unknown provider event type"):
        await _collect_events(wrapped)
    assert inner.close_calls == 1


async def test_iterator_closed_on_after_done_rejection() -> None:
    """When wrapper rejects an event after done, the underlying iterator
    must be closed."""
    inner = _TrackingCloseProvider(
        [
            {"type": "done"},
            {"type": "text_delta", "text": "ghost"},
        ]
    )
    wrapped = ValidatedPluginProvider(inner)

    with pytest.raises(ProviderPluginContractError, match="after terminal done"):
        await _collect_events(wrapped)
    assert inner.close_calls == 1


async def test_iterator_closed_on_consumer_cancellation() -> None:
    """When the driving task is cancelled, the underlying iterator must
    still be closed (exactly once)."""
    import asyncio

    event_reached = asyncio.Event()

    class _BlockingProvider(LLMProvider):
        def __init__(self) -> None:
            self.close_calls = 0

        @property
        def descriptor(self) -> ModelDescriptor:
            return ModelDescriptor("test", "blocking")

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
            try:
                yield {"type": "text_delta", "text": "hi"}
                event_reached.set()
                await asyncio.sleep(999)  # block until cancelled
                yield {"type": "done"}
            finally:
                self.close_calls += 1

    inner = _BlockingProvider()
    wrapped = ValidatedPluginProvider(inner)

    async def _drive() -> list[dict[str, Any]]:
        return await _collect_events(wrapped)

    task = asyncio.create_task(_drive())
    await event_reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Give the event loop a tick for cleanup
    await asyncio.sleep(0)
    assert inner.close_calls == 1


async def test_close_raises_secret_primary_error_preserved() -> None:
    """If iterator close raises with secret payload while a primary wrapper
    error is active, the primary error is preserved and no secret leaks."""

    class _AfterDoneCloseRaises(LLMProvider):
        def __init__(self) -> None:
            self.close_calls = 0

        @property
        def descriptor(self) -> ModelDescriptor:
            return ModelDescriptor("test", "after-done-close-raises")

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
            try:
                yield {"type": "done"}
                yield {"type": "text_delta", "text": "after-done"}
            finally:
                self.close_calls += 1
                raise RuntimeError("SECRET_CLOSE_PAYLOAD_xyz" * 10)

    inner = _AfterDoneCloseRaises()
    wrapped = ValidatedPluginProvider(inner)

    with pytest.raises(ProviderPluginContractError, match="after terminal done") as exc_info:
        await _collect_events(wrapped)
    msg = str(exc_info.value)
    assert "SECRET_CLOSE_PAYLOAD" not in msg
    assert inner.close_calls == 1


async def test_normal_close_failure_becomes_contract_error() -> None:
    """On normal exhaustion, if iterator aclose() raises, it must become a
    fixed ProviderPluginContractError without raw payload."""

    class _CloseFailsOnExhaust(LLMProvider):
        """Provider whose iterator aclose raises after normal exhaustion."""

        def __init__(self) -> None:
            self.close_calls = 0

        @property
        def descriptor(self) -> ModelDescriptor:
            return ModelDescriptor("test", "close-fails-exhaust")

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
            # This async generator yields events, and its finally (aclose) will
            # be called by our wrapper. We raise only in the aclose path.
            try:
                yield {"type": "text_delta", "text": "hi"}
                yield {"type": "done"}
            finally:
                self.close_calls += 1
                raise RuntimeError("SECRET_CLEANUP_FAIL_abc" * 10)

    inner = _CloseFailsOnExhaust()
    wrapped = ValidatedPluginProvider(inner)

    # The generator exhausts normally (StopAsyncIteration after done), then
    # the else-branch calls _close_iterator_or_raise which triggers finally.
    # But: Python's async generator finalization means the finally fires during
    # the StopAsyncIteration __anext__ call. So the exception comes from the
    # iterator advancement path. Either way, no secret should leak.
    with pytest.raises(ProviderPluginContractError) as exc_info:
        await _collect_events(wrapped)
    msg = str(exc_info.value)
    assert "SECRET_CLEANUP_FAIL" not in msg
    assert inner.close_calls == 1


# --- Round 8: iterator creation and hostile mapping ---


class _CreationFailsProvider(LLMProvider):
    """Provider whose complete() raises synchronously (non-generator)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("test", "creation-fails")

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities.unknown()

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        raise self._exc

    async def aclose(self) -> None:
        pass


async def test_iterator_creation_secret_exception_translated() -> None:
    """Synchronous exception from complete() call itself must be translated."""
    secret_exc = ProviderPluginContractError("SECRET_INIT_PAYLOAD" * 20)
    wrapped = ValidatedPluginProvider(_CreationFailsProvider(secret_exc))

    with pytest.raises(ProviderPluginContractError, match="stream creation failed") as exc_info:
        await _collect_events(wrapped)
    msg = str(exc_info.value)
    assert "SECRET_INIT_PAYLOAD" not in msg
    assert len(msg) <= 300


async def test_iterator_creation_huge_exception_bounded() -> None:
    """Huge exception type name from complete() must be bounded."""
    huge_exc = type("A" * 5000, (Exception,), {})("payload")
    wrapped = ValidatedPluginProvider(_CreationFailsProvider(huge_exc))

    with pytest.raises(ProviderPluginContractError, match="stream creation failed") as exc_info:
        await _collect_events(wrapped)
    assert len(str(exc_info.value)) <= 2200


class _HostileMapping(Mapping[object, object]):
    """Mapping whose get() raises with secret payload."""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def __getitem__(self, key: object) -> object:
        raise RuntimeError(self._secret)

    def __iter__(self) -> Iterator[object]:
        raise RuntimeError(self._secret)

    def __len__(self) -> int:
        raise RuntimeError(self._secret)

    def get(self, key: object, default: object = None) -> object:
        raise RuntimeError(self._secret)


async def test_hostile_mapping_get_raises_translated() -> None:
    """A Mapping whose get() raises must produce fixed contract error."""
    hostile = _HostileMapping("SECRET_MAP_PAYLOAD_xyz789" * 10)
    provider = _ScriptedProvider([hostile, {"type": "done"}])
    wrapped = ValidatedPluginProvider(provider)

    with pytest.raises(ProviderPluginContractError, match="field access") as exc_info:
        await _collect_events(wrapped)
    msg = str(exc_info.value)
    assert "SECRET_MAP_PAYLOAD" not in msg


async def test_hostile_mapping_in_text_delta_field() -> None:
    """A text_delta event whose Mapping raises on field get() must be caught."""

    class _HostileTextMapping(Mapping[object, object]):
        def __getitem__(self, key: object) -> object:
            if key == "type":
                return "text_delta"
            raise RuntimeError("SECRET_FIELD_READ" * 20)

        def __iter__(self) -> Iterator[object]:
            return iter(["type", "text"])

        def __len__(self) -> int:
            return 2

        def get(self, key: object, default: object = None) -> object:
            if key == "type":
                return "text_delta"
            raise RuntimeError("SECRET_FIELD_READ" * 20)

    provider = _ScriptedProvider([_HostileTextMapping(), {"type": "done"}])
    wrapped = ValidatedPluginProvider(provider)

    with pytest.raises(ProviderPluginContractError, match="field access") as exc_info:
        await _collect_events(wrapped)
    assert "SECRET_FIELD_READ" not in str(exc_info.value)


async def test_hostile_mapping_iterator_still_closed() -> None:
    """When normalization fails on hostile mapping, the underlying iterator
    must still be closed (deterministic cleanup)."""
    hostile = _HostileMapping("SECRET" * 100)

    class _TrackClose(LLMProvider):
        def __init__(self) -> None:
            self.close_calls = 0

        @property
        def descriptor(self) -> ModelDescriptor:
            return ModelDescriptor("test", "track-close")

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
            try:
                yield hostile  # type: ignore[misc]
            finally:
                self.close_calls += 1

        async def aclose(self) -> None:
            pass

    inner = _TrackClose()
    wrapped = ValidatedPluginProvider(inner)

    with pytest.raises(ProviderPluginContractError, match="field access"):
        await _collect_events(wrapped)
    assert inner.close_calls == 1


async def test_plugin_message_hook_is_not_forwarded() -> None:
    """A plugin must only ever see sanitized content.

    `prepare_messages` runs before the outbound policy, so forwarding it to
    third-party code would hand it the raw conversation — and let it inject
    fields the policy never inspected (issue #189)."""

    class _HookProvider(LLMProvider):
        def __init__(self) -> None:
            self.hook_calls = 0

        @property
        def descriptor(self) -> ModelDescriptor:
            return ModelDescriptor("test", "hook-plugin")

        @property
        def capabilities(self) -> ModelCapabilities:
            return ModelCapabilities.unknown()

        def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            self.hook_calls += 1
            return [{"role": "user", "content": "smuggled"}]

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "done"}

    inner = _HookProvider()
    wrapped = ValidatedPluginProvider(inner)
    history = [{"role": "user", "content": "hi"}]

    assert wrapped.prepare_messages(history) == history
    assert inner.hook_calls == 0
