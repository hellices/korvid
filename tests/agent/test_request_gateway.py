"""Contract for the RequestGateway — the single seam every provider request
crosses.

The gateway owns the exact handoff proof: the session's latest outbound
payload changes only once a request has demonstrably reached the provider,
never when a payload was merely built. A built-in adapter proves it with
`REQUEST_SENT`; a plugin (which cannot emit that bookkeeping event) proves
it with its first completion event. The gateway consumes `REQUEST_SENT`
and never exposes it to the engine.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from types import MappingProxyType
from typing import Any

import pytest

from korvid.agent.model_policy import ModelCapabilities, ModelDescriptor
from korvid.agent.outbound import OutboundPolicy
from korvid.agent.provider import REQUEST_SENT, LLMProvider
from korvid.agent.request_gateway import PreparedGatewayRequest, RequestGateway
from korvid.core.redaction import RedactionRecord


class _Provenance:
    """Minimal `RequestView`-shaped provenance carrier for the gateway."""

    def __init__(
        self,
        ingress: Mapping[int, Sequence[RedactionRecord]] | None = None,
        tool_errors: frozenset[int] | None = None,
    ) -> None:
        self.ingress = dict(ingress or {})
        self.tool_errors = tool_errors or frozenset()


class _StreamProvider(LLMProvider):
    """Async-generator provider that records what it was handed and can fail.

    An item in `events` that is a `BaseException` is raised at that point in
    the stream, standing in for a provider that dies mid-flight. The
    generator's `finally` records that the iterator was closed on every exit
    path (normal, error, cancellation).
    """

    def __init__(
        self,
        events: Sequence[Any],
        *,
        model: str = "qwen3:8b",
        prepare: Any = None,
    ) -> None:
        self._events = list(events)
        self._model = model
        self._prepare = prepare
        self.received_messages: list[dict[str, Any]] | None = None
        self.received_tools: list[dict[str, Any]] | None = None
        self.iterator_closed = False

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("ollama", self._model)

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities.unknown()

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self._prepare is None:
            return messages
        return self._prepare(messages)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        self.received_messages = messages
        self.received_tools = tools
        try:
            for event in self._events:
                if isinstance(event, BaseException):
                    raise event
                yield event
        finally:
            self.iterator_closed = True


class _SequenceProvider(LLMProvider):
    """Provider that streams a different event list on each `complete` call."""

    def __init__(self, sequences: Sequence[Sequence[Any]]) -> None:
        self._sequences = [list(sequence) for sequence in sequences]
        self._call = 0
        self.received_messages: list[dict[str, Any]] | None = None
        self.received_tools: list[dict[str, Any]] | None = None

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("ollama", "qwen3:8b")

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
        self.received_messages = messages
        self.received_tools = tools
        events = self._sequences[self._call]
        self._call += 1
        for event in events:
            if isinstance(event, BaseException):
                raise event
            yield event


class _CreationFailsProvider(LLMProvider):
    """Provider whose `complete` raises synchronously when the stream is built."""

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("ollama", "qwen3:8b")

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
        raise RuntimeError("stream creation failed")


def _policy() -> OutboundPolicy:
    return OutboundPolicy(max_request_chars=200_000)


def _gateway(provider: LLMProvider) -> RequestGateway:
    return RequestGateway(provider, _policy())


def _prepare(
    gateway: RequestGateway,
    *,
    messages: list[dict[str, Any]] | None = None,
    tools: Any = None,
    iteration: int = 1,
    provenance: _Provenance | None = None,
) -> PreparedGatewayRequest:
    return gateway.prepare(
        messages if messages is not None else [{"role": "user", "content": "hi"}],
        tools if tools is not None else [],
        iteration=iteration,
        provenance=provenance or _Provenance(),
    )


class _Counter:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


async def _drain(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event async for event in stream]


# --- Handoff proof: built-in REQUEST_SENT --------------------------------


async def test_builtin_request_sent_records_handoff_and_is_consumed() -> None:
    provider = _StreamProvider(
        [
            {"type": REQUEST_SENT},
            {"type": "text_delta", "text": "hello"},
            {"type": "done"},
        ]
    )
    gateway = _gateway(provider)
    prepared = _prepare(gateway)
    callback = _Counter()

    assert gateway.latest_outbound_payload is None
    events = await _drain(gateway.stream(prepared, callback))

    # REQUEST_SENT is bookkeeping — the engine never sees it.
    assert all(event.get("type") != REQUEST_SENT for event in events)
    assert {"type": "text_delta", "text": "hello"} in events
    assert callback.count == 1
    assert gateway.latest_outbound_payload is prepared.prepared.snapshot
    assert json.loads(gateway.latest_outbound_payload.payload_json) == {
        "messages": provider.received_messages,
        "tools": provider.received_tools,
    }


# --- Handoff proof: plugin first completion event ------------------------


async def test_plugin_first_completion_event_records_handoff() -> None:
    provider = _StreamProvider(
        [
            {"type": "text_delta", "text": "a"},
            {"type": "text_delta", "text": "b"},
            {"type": "done"},
        ]
    )
    gateway = _gateway(provider)
    prepared = _prepare(gateway)
    callback = _Counter()

    events = await _drain(gateway.stream(prepared, callback))

    assert [event["type"] for event in events] == ["text_delta", "text_delta", "done"]
    assert callback.count == 1
    assert gateway.latest_outbound_payload is prepared.prepared.snapshot
    assert json.loads(gateway.latest_outbound_payload.payload_json) == {
        "messages": provider.received_messages,
        "tools": provider.received_tools,
    }


# --- No handoff: provider raises before any signal -----------------------


async def test_provider_error_before_handoff_leaves_latest_unchanged() -> None:
    provider = _StreamProvider([RuntimeError("boom")])
    gateway = _gateway(provider)
    prepared = _prepare(gateway)
    callback = _Counter()

    with pytest.raises(RuntimeError, match="boom"):
        await _drain(gateway.stream(prepared, callback))

    assert gateway.latest_outbound_payload is None
    assert callback.count == 0
    assert provider.iterator_closed


async def test_iterator_creation_error_before_handoff_leaves_latest_unchanged() -> None:
    provider = _CreationFailsProvider()
    gateway = _gateway(provider)
    prepared = _prepare(gateway)
    callback = _Counter()

    with pytest.raises(RuntimeError, match="stream creation failed"):
        await _drain(gateway.stream(prepared, callback))

    assert gateway.latest_outbound_payload is None
    assert callback.count == 0


# --- Handoff then failure: latest remains the acknowledged request -------


async def test_provider_error_after_handoff_keeps_latest() -> None:
    provider = _StreamProvider(
        [
            {"type": REQUEST_SENT},
            {"type": "text_delta", "text": "partial"},
            RuntimeError("late failure"),
        ]
    )
    gateway = _gateway(provider)
    prepared = _prepare(gateway)
    callback = _Counter()

    with pytest.raises(RuntimeError, match="late failure"):
        await _drain(gateway.stream(prepared, callback))

    assert gateway.latest_outbound_payload is prepared.prepared.snapshot
    assert callback.count == 1
    assert provider.iterator_closed


# --- Exact payload equality and caller-mutation immunity -----------------


async def test_provider_receives_exactly_the_prepared_payload() -> None:
    messages = [{"role": "user", "content": "what pods are failing?"}]
    tools = [{"type": "function", "function": {"name": "get_resource"}}]
    provider = _StreamProvider([{"type": REQUEST_SENT}, {"type": "done"}])
    gateway = _gateway(provider)
    prepared = _prepare(gateway, messages=messages, tools=tools)

    # Mutating the caller-facing prepared payload before streaming must not
    # change what the provider is handed.
    prepared.prepared.messages.append({"role": "user", "content": "injected"})
    prepared.prepared.tools.clear()

    await _drain(gateway.stream(prepared, _Counter()))

    assert json.loads(prepared.prepared.snapshot.payload_json) == {
        "messages": provider.received_messages,
        "tools": provider.received_tools,
    }
    # The injected message never reached the provider.
    assert provider.received_messages is not None
    assert all(m.get("content") != "injected" for m in provider.received_messages)


# --- prepare(): message hook runs before sanitization --------------------


async def test_prepare_runs_message_hook_before_sanitize_masking_secret() -> None:
    def _inject(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        adapted = [dict(message) for message in messages]
        # Role and content are untouched (the position contract), but the
        # dialect field carries a credential that must be masked by the
        # outbound policy, proving the hook ran before sanitization.
        adapted[1]["thinking"] = "password=hunter2secret"
        return adapted

    provider = _StreamProvider([{"type": "done"}], prepare=_inject)
    gateway = _gateway(provider)
    prepared = _prepare(
        gateway,
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
        ],
    )

    payload_json = prepared.prepared.snapshot.payload_json
    assert "hunter2secret" not in payload_json
    assert any(
        record.reason == "credential-assignment" for record in prepared.prepared.snapshot.redactions
    )


# --- prepare(): tool thaw + copy isolation -------------------------------


async def test_prepare_thaws_frozen_tools_and_isolates_copies() -> None:
    inner = {"type": "object", "properties": {}}
    tool = {"type": "function", "function": {"name": "get_resource", "parameters": inner}}
    frozen_tool = MappingProxyType(
        {"type": "function", "function": MappingProxyType({"name": "describe"})}
    )
    gateway = _gateway(_StreamProvider([{"type": "done"}]))

    prepared = _prepare(gateway, tools=(tool, frozen_tool))

    # Frozen MappingProxy schemas are thawed into plain, mutable copies.
    assert isinstance(prepared.prepared.tools[0], dict)
    assert isinstance(prepared.prepared.tools[0]["function"], dict)
    assert isinstance(prepared.prepared.tools[1]["function"], dict)

    # Mutating the caller's original tool objects cannot reach back into the
    # prepared payload.
    inner["properties"]["injected"] = {"secret": "x"}
    tool["function"]["name"] = "changed"
    assert prepared.prepared.tools[0]["function"]["name"] == "get_resource"
    assert "injected" not in prepared.prepared.tools[0]["function"]["parameters"]["properties"]


# --- callback fires exactly once -----------------------------------------


async def test_callback_fires_exactly_once_across_many_events() -> None:
    provider = _StreamProvider(
        [
            {"type": REQUEST_SENT},
            {"type": "text_delta", "text": "a"},
            {"type": "text_delta", "text": "b"},
            {"type": "usage", "input_tokens": 1, "output_tokens": 2},
            {"type": "done"},
        ]
    )
    gateway = _gateway(provider)
    prepared = _prepare(gateway)
    callback = _Counter()

    await _drain(gateway.stream(prepared, callback))

    assert callback.count == 1


# --- cancellation after handoff ------------------------------------------


async def test_cancellation_after_request_sent_keeps_latest_and_closes_iterator() -> None:
    provider = _StreamProvider(
        [
            {"type": REQUEST_SENT},
            {"type": "text_delta", "text": "first"},
            {"type": "text_delta", "text": "second"},
            {"type": "done"},
        ]
    )
    gateway = _gateway(provider)
    prepared = _prepare(gateway)
    callback = _Counter()

    stream = gateway.stream(prepared, callback)
    first = await stream.__anext__()
    assert first == {"type": "text_delta", "text": "first"}
    assert gateway.latest_outbound_payload is prepared.prepared.snapshot

    await stream.aclose()

    assert provider.iterator_closed
    assert gateway.latest_outbound_payload is prepared.prepared.snapshot
    assert callback.count == 1


# --- iterator closed on normal completion --------------------------------


async def test_iterator_closed_on_normal_completion() -> None:
    provider = _StreamProvider([{"type": REQUEST_SENT}, {"type": "done"}])
    gateway = _gateway(provider)
    prepared = _prepare(gateway)

    await _drain(gateway.stream(prepared, _Counter()))

    assert provider.iterator_closed


# --- latest snapshot semantics across sequential requests ----------------


async def test_latest_snapshot_survives_a_later_failed_request() -> None:
    provider = _SequenceProvider(
        [
            [{"type": REQUEST_SENT}, {"type": "done"}],
            [RuntimeError("no handoff")],
        ]
    )
    gateway = _gateway(provider)

    first = _prepare(gateway, iteration=1)
    await _drain(gateway.stream(first, _Counter()))
    assert gateway.latest_outbound_payload is first.prepared.snapshot

    # A second request that dies before handoff must not disturb the last
    # real handoff on display.
    second = _prepare(gateway, iteration=2)
    with pytest.raises(RuntimeError, match="no handoff"):
        await _drain(gateway.stream(second, _Counter()))
    assert gateway.latest_outbound_payload is first.prepared.snapshot


# --- prompt estimate + Gateway.aclose ------------------------------------


async def test_prompt_estimate_is_measured_from_the_exact_payload() -> None:
    gateway = _gateway(_StreamProvider([{"type": "done"}]))
    prepared = _prepare(gateway)

    expected = len(prepared.prepared.snapshot.payload_json) // 4
    assert prepared.prompt_estimate == expected


async def test_gateway_aclose_closes_the_active_iterator() -> None:
    provider = _StreamProvider(
        [
            {"type": REQUEST_SENT},
            {"type": "text_delta", "text": "one"},
            {"type": "text_delta", "text": "two"},
            {"type": "done"},
        ]
    )
    gateway = _gateway(provider)
    prepared = _prepare(gateway)

    stream = gateway.stream(prepared, _Counter())
    await stream.__anext__()

    await gateway.aclose()

    assert provider.iterator_closed
