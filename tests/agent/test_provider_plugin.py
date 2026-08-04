from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from korvid.agent.credentials import CredentialSource
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


def test_provider_plugin_api_version_is_v1() -> None:
    assert PROVIDER_PLUGIN_API_VERSION == 1


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
    def name(self) -> str:
        return "exploding-provider"

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
        def name(self) -> str:
            return "boom-closer"

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
        def name(self) -> str:
            return "one-shot"

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
    def name(self) -> str:
        return "secret-error-provider"

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
    def name(self) -> str:
        return "tracking-close"

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
        def name(self) -> str:
            return "blocking"

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
        def name(self) -> str:
            return "after-done-close-raises"

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
        def name(self) -> str:
            return "close-fails-exhaust"

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
