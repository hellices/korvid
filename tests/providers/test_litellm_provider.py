"""Tests for litellm_provider: one LLMProvider over `litellm.acompletion`.

RED → GREEN sequence as described in task-14-brief.md.

Every error-path test drives a **real** `MockTransport` wherever the
failure can be produced by one, because the facts under test are facts
about litellm 1.98.0's own translation layer — which exception class it
raises, and what it leaves in the `__context__` chain — not about a
double korvid wrote to agree with itself.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from typing import Any, cast

import pytest

pytest.importorskip("litellm")

import httpx

from korvid.agent.model_policy import (
    CapabilitySource,
    ModelCapabilities,
    ModelDescriptor,
)
from korvid.agent.provider import REQUEST_SENT, OperatorSafeProviderError
from korvid.providers import litellm_provider
from korvid.providers.litellm_provider import LiteLLMProvider, ProviderRequestError
from korvid.providers.litellm_request import RequestPlan, build_plan
from korvid.providers.litellm_runtime import ProviderSDKError, acompletion, exceptions

_MODEL = "openai/gpt-4o"
_SECRET = "sk-secret-value"
_BASE_URL = "https://mock.invalid/v1"
_MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]

Handler = Callable[[httpx.Request], httpx.Response]


# ---------------------------------------------------------------------------
# Wire fixtures
# ---------------------------------------------------------------------------


def _chunk(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-4o",
    }
    base.update(fields)
    return base


def _delta(**fields: Any) -> dict[str, Any]:
    """One chunk carrying a single choice whose delta holds `fields`."""
    return _chunk(choices=[{"index": 0, "delta": dict(fields)}])


def _fragment(
    index: int,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str = "",
) -> dict[str, Any]:
    """One `delta.tool_calls[*]` fragment, as the wire sends it."""
    function: dict[str, Any] = {"arguments": arguments}
    if name is not None:
        function["name"] = name
    fragment: dict[str, Any] = {"index": index, "type": "function", "function": function}
    if call_id is not None:
        fragment["id"] = call_id
    return fragment


def _sse(chunks: list[dict[str, Any]]) -> bytes:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _streaming(chunks: list[dict[str, Any]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(chunks),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    return handler


def _answering(status: int, body: dict[str, Any] | None = None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json=body if body is not None else {"error": {"message": "no"}},
            request=request,
        )

    return handler


def _throwing(make: Callable[[httpx.Request], BaseException]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise make(request)

    return handler


def _truncated_after_first_fragment() -> Handler:
    """A stream that delivers half a tool call and then dies on the wire.

    The body has to be an *async* iterator: httpx drains a sync one while
    the response is still being built, so the failure would land on the
    `await` instead of mid-iteration and test a different path entirely.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        first = _delta(
            tool_calls=[_fragment(0, call_id="c1", name="get_pods", arguments='{"ns": ')]
        )

        async def body() -> AsyncIterator[bytes]:
            yield f"data: {json.dumps(first)}\n\n".encode()
            raise httpx.ReadError("connection reset mid-stream", request=request)

        return httpx.Response(
            200,
            content=body(),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    return handler


# ---------------------------------------------------------------------------
# Provider fixtures
# ---------------------------------------------------------------------------


def _plan(**overrides: Any) -> RequestPlan:
    settings: dict[str, Any] = {
        "model": _MODEL,
        "api_key": _SECRET,
        "base_url": _BASE_URL,
        "options": {},
        "supported": [],
    }
    settings.update(overrides)
    return build_plan(**settings)


def _client(handler: Handler) -> Any:
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        base_url=_BASE_URL,
        api_key=_SECRET,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        # The status matrix drives 429 and 503; the SDK's default retry
        # would sleep between attempts and call the handler again.
        max_retries=0,
    )


def _provider(
    handler: Handler,
    *,
    plan: RequestPlan | None = None,
    capabilities: ModelCapabilities | None = None,
) -> LiteLLMProvider:
    return LiteLLMProvider(
        plan=plan if plan is not None else _plan(),
        descriptor=ModelDescriptor(provider="openai", model="gpt-4o"),
        capabilities=capabilities if capabilities is not None else ModelCapabilities.unknown(),
        client=_client(handler),
    )


async def _events(provider: LiteLLMProvider, **kwargs: Any) -> list[dict[str, Any]]:
    return [event async for event in provider.complete(_MESSAGES, [], **kwargs)]


def _as_generator(stream: AsyncIterator[dict[str, Any]]) -> AsyncGenerator[dict[str, Any], None]:
    """`complete` is declared `AsyncIterator` by the ABC and *is* a generator.

    `native_engine` casts the same way before `contextlib.aclosing`, for the
    same reason: the ABC's return type says nothing about `aclose`.
    """
    return cast("AsyncGenerator[dict[str, Any], None]", stream)


async def _collect_into(provider: LiteLLMProvider, sink: list[dict[str, Any]]) -> None:
    """Drain `complete` into `sink`, so a failure leaves what arrived first."""
    async for event in provider.complete(_MESSAGES, []):
        sink.append(event)


async def _drain_litellm_logging() -> None:
    """Run the success callback LiteLLM queued instead of awaiting it.

    A successful *non-streaming* `acompletion` hands its logging callback
    to LiteLLM's process-global logging worker rather than awaiting it,
    and that worker binds its queue to the event loop that created it.
    pytest gives each test its own loop, so the next non-streaming call
    anywhere in the session finds a changed loop and drops the old queue —
    queued coroutines and all. The dropped coroutine is then reported as
    never awaited, as an *unraisable* exception charged to whichever test
    happened to be running when it was collected: a failure that belongs
    to no test and moves with the random order.

    Draining it inside the test that queued it keeps that bookkeeping
    where it belongs. `clear_queue` is LiteLLM's own API for this and is
    bounded in both iterations and time.
    """
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    # The ignore below: `clear_queue` carries no return annotation upstream.
    await GLOBAL_LOGGING_WORKER.clear_queue()  # type: ignore[no-untyped-call]


def _returning(value: Any) -> Callable[..., Any]:
    async def _acompletion(**kwargs: Any) -> Any:
        return value

    return _acompletion


def _raising(exc: BaseException) -> Callable[..., Any]:
    async def _acompletion(**kwargs: Any) -> Any:
        raise exc

    return _acompletion


class _FakeWrapper:
    """Stands in for `CustomStreamWrapper` where the real one cannot help.

    Only used where the fact under test is korvid's own cleanup (did the
    wrapper get closed?), which a real stream cannot report.
    """

    def __init__(
        self,
        chunks: list[Any],
        *,
        block: bool = False,
        raises: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._block = block
        self._raises = raises
        self._close_error = close_error
        self.aclose_called = False

    def __aiter__(self) -> _FakeWrapper:
        return self

    async def __anext__(self) -> Any:
        if self._chunks:
            return self._chunks.pop(0)
        if self._raises is not None:
            raise self._raises
        if self._block:
            await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclose_called = True
        if self._close_error is not None:
            raise self._close_error


def _fake_chunk(**delta_fields: Any) -> Any:
    """A LiteLLM-shaped chunk object (attributes, not keys)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        choices=[SimpleNamespace(index=0, delta=SimpleNamespace(**delta_fields))],
        usage=None,
    )


class _ExplodingPlan(RequestPlan):
    """A plan whose kwargs assembly raises — i.e. a korvid bug."""

    def call_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        raise TypeError("korvid bug")


# ---------------------------------------------------------------------------
# REQUEST_SENT
# ---------------------------------------------------------------------------


async def test_request_sent_is_yielded_after_the_transport_accepted() -> None:
    """`await acompletion(stream=True)` raises before returning on a
    connection failure, so REQUEST_SENT immediately after the await means
    'sent', not 'intended' — which is exactly the contract."""
    events = await _events(_provider(_streaming([_delta(content="hi")])))
    assert events[0] == {"type": REQUEST_SENT}


@pytest.mark.parametrize(
    "failure",
    [
        lambda request: httpx.ConnectError("refused", request=request),
        lambda request: httpx.ReadError("reset", request=request),
        lambda request: httpx.ConnectTimeout("timed out", request=request),
        lambda request: httpx.ReadTimeout("timed out", request=request),
    ],
    ids=["connect-error", "read-error", "connect-timeout", "read-timeout"],
)
async def test_a_transport_failure_yields_no_request_sent(
    failure: Callable[[httpx.Request], BaseException],
) -> None:
    """Nothing reached the provider, so the outbound panel must not claim
    a payload was delivered."""
    collected: list[dict[str, Any]] = []
    with pytest.raises(ProviderRequestError, match=r"connect|timed out"):
        await _collect_into(_provider(_throwing(failure)), collected)
    assert collected == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
async def test_an_answered_error_status_still_yields_request_sent(status: int) -> None:
    """`agent/provider.py`: REQUEST_SENT fires "as soon as the transport
    has accepted the request (response headers received), before the
    status code is judged: an HTTP 500 answer still means the provider
    has the payload."

    A refused connection and a genuine 500 are indistinguishable by
    exception type — litellm reports both as InternalServerError with
    status_code=500 — so this cannot be keyed on isinstance(exc,
    openai.APIStatusError). It is keyed on httpx.HTTPStatusError appearing
    in the exception's __context__ chain, which only an answered request
    produces.
    """
    collected: list[dict[str, Any]] = []
    with pytest.raises(ProviderRequestError):
        await _collect_into(_provider(_answering(status)), collected)
    assert collected == [{"type": REQUEST_SENT}]


async def test_a_refused_connection_and_a_real_500_are_the_same_exception_class() -> None:
    """The measurement the REQUEST_SENT rule rests on. If a future litellm
    told these apart by class, the context-chain walk could be simplified —
    until then, keying on the class would report every refused connection
    as delivered."""
    refused: BaseException
    answered: BaseException
    with pytest.raises(ProviderSDKError) as refused_info:
        await acompletion(
            model=_MODEL,
            messages=_MESSAGES,
            stream=True,
            api_key=_SECRET,
            client=_client(
                _throwing(lambda request: httpx.ConnectError("refused", request=request))
            ),
        )
    refused = refused_info.value
    with pytest.raises(ProviderSDKError) as answered_info:
        await acompletion(
            model=_MODEL,
            messages=_MESSAGES,
            stream=True,
            api_key=_SECRET,
            client=_client(_answering(500)),
        )
    answered = answered_info.value
    assert type(refused) is type(answered)
    assert getattr(refused, "status_code", None) == getattr(answered, "status_code", None)


async def test_an_error_with_neither_marker_is_treated_as_not_sent() -> None:
    """A request litellm refused before building it never left. Defaulting
    to "sent" would raise a false alarm on every routing rejection.

    An unqualified model reference is the real form of this: litellm
    raises BadRequestError("LLM Provider NOT provided") with an empty
    context chain, before any transport is chosen.
    """
    collected: list[dict[str, Any]] = []
    provider = _provider(_streaming([]), plan=_plan(model="totally-unknown-model"))
    with pytest.raises(ProviderRequestError, match="rejected"):
        await _collect_into(provider, collected)
    assert collected == []


# ---------------------------------------------------------------------------
# Stream translation
# ---------------------------------------------------------------------------


async def test_text_deltas_stream_through_in_order() -> None:
    events = await _events(
        _provider(_streaming([_delta(content="Hello"), _delta(content=" world")]))
    )
    assert "".join(e["text"] for e in events if e["type"] == "text_delta") == "Hello world"


async def test_text_is_normalized_to_korvids_own_event_name() -> None:
    """`native_engine._stream` and `provider_plugin._normalize_event` both
    know exactly one text event, `text_delta`. A provider that invented
    another name would stream into a branch nothing reads."""
    events = await _events(_provider(_streaming([_delta(content="Hello")])))
    assert {"type": "text_delta", "text": "Hello"} in events


async def test_a_fragmented_tool_call_is_reassembled_and_emitted_once() -> None:
    """Verified against 1.98.0: the id and name arrive on the first
    fragment and the arguments arrive split across later ones with
    id=None, name=None."""
    events = await _events(
        _provider(
            _streaming(
                [
                    _delta(
                        tool_calls=[
                            _fragment(0, call_id="c1", name="get_pods", arguments='{"ns": ')
                        ]
                    ),
                    _delta(tool_calls=[_fragment(0, arguments='"kube-')]),
                    _delta(tool_calls=[_fragment(0, arguments='system"}')]),
                ]
            )
        )
    )
    calls = [e for e in events if e["type"] == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["id"] == "c1"
    assert calls[0]["name"] == "get_pods"
    assert json.loads(calls[0]["arguments"]) == {"ns": "kube-system"}


async def test_two_interleaved_tool_calls_are_keyed_by_tool_call_index() -> None:
    """`choice.index` is 0 on every chunk when n=1, so keying on it would
    merge two parallel calls into one malformed call. The index that
    distinguishes them is `delta.tool_calls[*].index`."""
    events = await _events(
        _provider(
            _streaming(
                [
                    _delta(
                        tool_calls=[
                            _fragment(0, call_id="c1", name="get_pods", arguments='{"ns": ')
                        ]
                    ),
                    _delta(
                        tool_calls=[
                            _fragment(1, call_id="c2", name="get_pods", arguments='{"ns": ')
                        ]
                    ),
                    _delta(tool_calls=[_fragment(1, arguments='"default"}')]),
                    _delta(tool_calls=[_fragment(0, arguments='"kube-system"}')]),
                ]
            )
        )
    )
    calls = [e for e in events if e["type"] == "tool_call"]
    assert [c["id"] for c in calls] == ["c1", "c2"]
    assert json.loads(calls[0]["arguments"]) == {"ns": "kube-system"}
    assert json.loads(calls[1]["arguments"]) == {"ns": "default"}


async def test_every_streamed_chunk_reports_choice_index_zero() -> None:
    """The measurement behind the test above: with n=1 the choice index
    never varies, so it carries no information about which call a
    fragment belongs to."""
    seen: list[int] = []
    handler = _streaming(
        [
            _delta(tool_calls=[_fragment(0, call_id="c1", name="get_pods")]),
            _delta(tool_calls=[_fragment(1, call_id="c2", name="get_pods")]),
        ]
    )
    response = await acompletion(
        model=_MODEL,
        messages=_MESSAGES,
        stream=True,
        api_key=_SECRET,
        client=_client(handler),
    )
    async with aclosing(response):
        async for chunk in response:
            seen.extend(choice.index for choice in chunk.choices)
    assert set(seen) == {0}


async def test_a_tool_call_with_unparsable_arguments_surfaces_the_raw_text() -> None:
    """Truncation mid-stream is real. The harness must see what arrived
    and refuse it, rather than the provider inventing `{}`. This is the
    *complete* call whose JSON is bad — distinct from the partial call
    below, which is never emitted at all."""
    events = await _events(
        _provider(
            _streaming(
                [
                    _delta(
                        tool_calls=[_fragment(0, call_id="c1", name="get_pods", arguments='{"ns"')]
                    )
                ]
            )
        )
    )
    calls = [e for e in events if e["type"] == "tool_call"]
    assert calls == [{"type": "tool_call", "id": "c1", "name": "get_pods", "arguments": '{"ns"'}]


async def test_a_partial_tool_call_is_dropped_when_the_stream_fails() -> None:
    """A half-received call is not a call. Emitting one would hand the
    harness arguments the model never finished writing, and the harness
    has no way to tell that from a model that meant to send them."""
    collected: list[dict[str, Any]] = []
    with pytest.raises(ProviderRequestError):
        await _collect_into(_provider(_truncated_after_first_fragment()), collected)
    assert [e for e in collected if e["type"] == "tool_call"] == []
    assert collected == [{"type": REQUEST_SENT}]


@pytest.mark.filterwarnings(
    # litellm 1.98.0 inspects the delta attribute by attribute to decide
    # whether a usage-only chunk is empty, reading `model_fields` and
    # `model_computed_fields` off the instance on the way past. Deprecated
    # in pydantic 2.11; korvid cannot fix it upstream and must not stop
    # exercising the one chunk shape that carries a provider's own counts.
    "ignore:Accessing the 'model_"
)
async def test_usage_from_a_choices_free_chunk_is_passed_through_verbatim() -> None:
    """Verified: with include_usage and usage on a chunk carrying no
    choices, LiteLLM reports the provider's own 11/7/18. Anywhere else it
    substitutes its own tokenizer estimate.

    The event uses korvid's normalized names (`input_tokens`,
    `output_tokens` — what `conversation.commit_usage` reads); the wire's
    own `total_tokens` rides along because a provider's total is not
    always the sum.
    """
    events = await _events(
        _provider(
            _streaming(
                [
                    _delta(content="hi"),
                    _chunk(
                        choices=[],
                        usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                    ),
                ]
            )
        )
    )
    usage = [e for e in events if e["type"] == "usage"]
    assert usage == [{"type": "usage", "input_tokens": 11, "output_tokens": 7, "total_tokens": 18}]


async def test_a_stream_with_no_usage_chunk_reports_none_rather_than_zero() -> None:
    """Zero tokens and unknown tokens are different facts."""
    plan = _plan(
        options={"stream_options": {"include_usage": False}},
        supported=["stream_options"],
    )
    events = await _events(_provider(_streaming([_delta(content="hi")]), plan=plan))
    assert [e for e in events if e["type"] == "usage"] == []


@pytest.mark.filterwarnings("ignore:Accessing the 'model_")
async def test_litellm_synthesizes_usage_the_provider_never_sent() -> None:
    """The measurement the provenance rule rests on, pinned.

    Every production plan sends `stream_options.include_usage` (Task 13
    sets it unconditionally for a streaming call), and LiteLLM 1.98.0
    answers that flag whether or not the provider did: when no frame
    carried counts it appends a final chunk holding its *own* tokenizer
    estimate, built by `stream_chunk_builder` from the text it saw.

    Two facts are pinned here, because the whole usage rule is built on
    them: the synthesized chunk exists, and it is **not** in the wrapper's
    own record of what arrived — `chunks` holds the frames the provider
    really sent, and `usage` is set on one of those only when the provider
    set it. That recording is the provenance signal `complete` reads. If a
    future litellm stops populating it, this test fails and the rule gets
    revisited deliberately instead of silently reporting guesses.
    """
    wrapper = await acompletion(
        model=_MODEL,
        messages=_MESSAGES,
        stream=True,
        stream_options={"include_usage": True},
        api_key=_SECRET,
        client=_client(_streaming([_delta(content="hi"), _delta(content=" there")])),
    )
    yielded = [chunk async for chunk in wrapper]

    synthesized = getattr(yielded[-1], "usage", None)
    assert synthesized is not None
    assert synthesized.prompt_tokens > 0
    assert [getattr(chunk, "usage", None) for chunk in wrapper.chunks] == [None, None]


@pytest.mark.filterwarnings("ignore:Accessing the 'model_")
async def test_the_wrapper_records_the_usage_frame_the_provider_did_send() -> None:
    """The other half of the measurement: when the provider *did* send
    counts, they are on a frame the wrapper recorded in `chunks` — so the
    signal distinguishes the two cases rather than merely detecting one."""
    wrapper = await acompletion(
        model=_MODEL,
        messages=_MESSAGES,
        stream=True,
        stream_options={"include_usage": True},
        api_key=_SECRET,
        client=_client(
            _streaming(
                [
                    _delta(content="hi"),
                    _chunk(
                        choices=[],
                        usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                    ),
                ]
            )
        ),
    )
    async for _ in wrapper:
        pass

    recorded = [getattr(chunk, "usage", None) for chunk in wrapper.chunks]
    reported = [usage for usage in recorded if usage is not None]
    assert [(usage.prompt_tokens, usage.completion_tokens) for usage in reported] == [(11, 7)]


@pytest.mark.filterwarnings("ignore:Accessing the 'model_")
async def test_a_tokenizer_estimate_is_never_reported_as_provider_usage() -> None:
    """A guess must not be committed as a measurement.

    This is the production shape — the plan's own `include_usage`, a
    provider that answers without a usage frame — and it is the shape the
    earlier "no usage chunk" case missed by switching the flag off.
    `conversation.commit_usage` marks the iteration *exactly counted*, so
    passing LiteLLM's tokenizer estimate through would replace the honest
    "estimated" flag on the round with numbers korvid invented.
    """
    events = await _events(_provider(_streaming([_delta(content="hi"), _delta(content=" there")])))
    assert [e for e in events if e["type"] == "usage"] == []
    assert {"type": "text_delta", "text": " there"} in events


@pytest.mark.filterwarnings("ignore:Accessing the 'model_")
async def test_usage_the_provider_sent_beside_its_choices_is_still_the_providers() -> None:
    """Counts come from the frame the provider sent, not the tail chunk.

    A provider that puts usage on its final *content* chunk (vLLM and
    Groq-shaped streams do) is still reporting its own numbers, but
    LiteLLM's synthesized tail chunk does not carry them through — it
    reports a tokenizer estimate instead. Reading the recorded frame keeps
    the provider's measurement, which is the half of this rule that must
    not regress while the guess is being refused.
    """
    events = await _events(
        _provider(
            _streaming(
                [
                    _delta(content="hi"),
                    _chunk(
                        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        usage={"prompt_tokens": 33, "completion_tokens": 4, "total_tokens": 37},
                    ),
                ]
            )
        )
    )
    usage = [e for e in events if e["type"] == "usage"]
    assert usage == [{"type": "usage", "input_tokens": 33, "output_tokens": 4, "total_tokens": 37}]


async def test_a_non_streaming_answer_without_counts_reports_no_usage() -> None:
    """`Usage(0, 0, 0)` is what LiteLLM materializes for a body that
    carried no usage at all — measured on 1.98.0, indistinguishable from a
    provider that really said zero. A request that had a prompt cannot
    have cost zero prompt tokens, so this is an absence, and an absence
    must not be committed as an exact count."""
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello"},
                "finish_reason": "stop",
            }
        ],
    }
    events = await _events(
        _provider(lambda request: httpx.Response(200, json=body, request=request)), stream=False
    )
    assert [e for e in events if e["type"] == "usage"] == []
    assert {"type": "text_delta", "text": "Hello"} in events
    await _drain_litellm_logging()


async def test_reasoning_content_is_surfaced_as_a_distinct_event() -> None:
    """Delta.reasoning_content exists in 1.98.0; folding it into text
    would put chain-of-thought in the transcript as if it were an answer."""
    events = await _events(
        _provider(_streaming([_delta(reasoning_content="thinking..."), _delta(content="hi")]))
    )
    assert {"type": "reasoning", "text": "thinking..."} in events
    assert {"type": "text_delta", "text": "thinking..."} not in events


async def test_the_stream_ends_with_the_terminal_done_event() -> None:
    """The normalized contract's terminal event, which both adapters this
    module replaces already emit."""
    events = await _events(_provider(_streaming([_delta(content="hi")])))
    assert events[-1] == {"type": "done"}


async def test_the_plan_is_what_reaches_the_wire() -> None:
    """The provider adds nothing of its own: the payload is the plan's."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _streaming([_delta(content="hi")])(request)

    tools: list[dict[str, Any]] = [
        {"type": "function", "function": {"name": "get_pods", "parameters": {}}}
    ]
    async for _ in _provider(handler).complete(_MESSAGES, tools):
        pass
    assert seen[0]["messages"] == _MESSAGES
    assert seen[0]["tools"] == tools
    assert seen[0]["stream_options"] == {"include_usage": True}


async def test_a_non_streaming_call_yields_the_same_event_shapes() -> None:
    """`stream` is part of the LLMProvider signature, so passing False
    must produce the same normalized events rather than an attempt to
    iterate a ModelResponse."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Hello world",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_pods",
                                        "arguments": '{"ns": "kube-system"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
            request=request,
        )

    events = await _events(_provider(handler), stream=False)
    assert events == [
        {"type": REQUEST_SENT},
        {"type": "text_delta", "text": "Hello world"},
        {"type": "tool_call", "id": "c1", "name": "get_pods", "arguments": '{"ns": "kube-system"}'},
        {"type": "usage", "input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        {"type": "done"},
    ]
    await _drain_litellm_logging()


# ---------------------------------------------------------------------------
# Cleanup and cancellation
# ---------------------------------------------------------------------------


async def test_the_real_wrapper_exposes_aclose_and_not_close() -> None:
    """The measurement the cleanup rests on: `CustomStreamWrapper` has
    `aclose()` and no `close()`, so `contextlib.closing` would raise."""
    response = await acompletion(
        model=_MODEL,
        messages=_MESSAGES,
        stream=True,
        api_key=_SECRET,
        client=_client(_streaming([_delta(content="hi")])),
    )
    assert callable(response.aclose)
    assert not hasattr(response, "close")
    await response.aclose()


async def test_a_consumer_that_stops_early_closes_the_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An abandoned generator must not leave the HTTP response open."""
    wrapper = _FakeWrapper([_fake_chunk(content="hi"), _fake_chunk(content="more")])
    monkeypatch.setattr(litellm_provider, "acompletion", _returning(wrapper))
    provider = LiteLLMProvider(
        plan=_plan(),
        descriptor=ModelDescriptor(provider="openai", model="gpt-4o"),
        capabilities=ModelCapabilities.unknown(),
    )
    async with aclosing(_as_generator(provider.complete(_MESSAGES, []))) as events:
        async for event in events:
            if event["type"] == "text_delta":
                break
    assert wrapper.aclose_called is True


async def _cancel_mid_stream(
    monkeypatch: pytest.MonkeyPatch, wrapper: _FakeWrapper
) -> asyncio.Task[None]:
    monkeypatch.setattr(litellm_provider, "acompletion", _returning(wrapper))
    provider = LiteLLMProvider(
        plan=_plan(),
        descriptor=ModelDescriptor(provider="openai", model="gpt-4o"),
        capabilities=ModelCapabilities.unknown(),
    )
    streaming = asyncio.Event()

    async def consume() -> None:
        async for event in provider.complete(_MESSAGES, []):
            if event["type"] == "text_delta":
                streaming.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(streaming.wait(), timeout=5)
    task.cancel()
    return task


async def test_cancelling_mid_stream_closes_the_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """CustomStreamWrapper exposes aclose(), not close()."""
    wrapper = _FakeWrapper([_fake_chunk(content="hi")], block=True)
    task = await _cancel_mid_stream(monkeypatch, wrapper)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wrapper.aclose_called is True


async def test_cancellation_propagates_rather_than_being_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`except Exception` around the stream would turn an interrupt into a
    provider failure; CancelledError is a BaseException and must escape."""
    wrapper = _FakeWrapper([_fake_chunk(content="hi")], block=True)
    task = await _cancel_mid_stream(monkeypatch, wrapper)
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancellation_during_the_await_is_never_translated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `except ProviderSDKError` clause sits over the await too; a
    cancellation there must not be reported as a provider error."""
    monkeypatch.setattr(litellm_provider, "acompletion", _raising(asyncio.CancelledError()))
    provider = LiteLLMProvider(
        plan=_plan(),
        descriptor=ModelDescriptor(provider="openai", model="gpt-4o"),
        capabilities=ModelCapabilities.unknown(),
    )
    with pytest.raises(asyncio.CancelledError):
        await _events(provider)


async def test_a_close_failure_does_not_mask_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator needs the reason the stream failed, not the reason the
    cleanup failed on the way out."""
    wrapper = _FakeWrapper(
        [_fake_chunk(content="hi")],
        raises=exceptions.RateLimitError(
            message="slow down", llm_provider="openai", model="gpt-4o"
        ),
        close_error=RuntimeError("close blew up"),
    )
    monkeypatch.setattr(litellm_provider, "acompletion", _returning(wrapper))
    provider = LiteLLMProvider(
        plan=_plan(),
        descriptor=ModelDescriptor(provider="openai", model="gpt-4o"),
        capabilities=ModelCapabilities.unknown(),
    )
    with pytest.raises(ProviderRequestError, match="rate limit"):
        await _events(provider)
    assert wrapper.aclose_called is True


# ---------------------------------------------------------------------------
# Contract surface
# ---------------------------------------------------------------------------


async def test_prepare_messages_is_the_identity() -> None:
    """LiteLLM already translates the OpenAI dialect per provider, so
    korvid must not reshape messages — anything added here would bypass
    the outbound policy."""
    provider = _provider(_streaming([]))
    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    assert provider.prepare_messages(messages) == messages


def test_the_descriptor_is_what_the_factory_passed_in() -> None:
    """`descriptor.provider` is the canonical provider id the registry
    resolved — never parsed back out of the model string."""
    provider = LiteLLMProvider(
        plan=_plan(model="openai/gpt-4o"),
        descriptor=ModelDescriptor(provider="azure", model="gpt-4o"),
        capabilities=ModelCapabilities.unknown(),
    )
    assert provider.descriptor == ModelDescriptor(provider="azure", model="gpt-4o")


def test_capabilities_are_never_inferred_from_the_model_name() -> None:
    provider = LiteLLMProvider(
        plan=_plan(model="openai/gpt-4o-with-tools-2000k"),
        descriptor=ModelDescriptor(provider="openai", model="gpt-4o-with-tools-2000k"),
        capabilities=ModelCapabilities.unknown(),
    )
    assert provider.capabilities.supports_tools is None
    assert provider.capabilities.context_window_tokens is None


def test_translated_catalog_capabilities_are_reported_unchanged() -> None:
    """What the catalog proved is what the router sees — the provider
    neither widens nor narrows it."""
    known = ModelCapabilities(
        context_window_tokens=128_000,
        supports_tools=True,
        provenance={"supports_tools": CapabilitySource.CATALOG},
    )
    provider = LiteLLMProvider(
        plan=_plan(),
        descriptor=ModelDescriptor(provider="openai", model="gpt-4o"),
        capabilities=known,
    )
    assert provider.capabilities == known


def test_capabilities_default_to_unknown_when_none_were_translated() -> None:
    provider = LiteLLMProvider(
        plan=_plan(), descriptor=ModelDescriptor(provider="openai", model="gpt-4o")
    )
    assert provider.capabilities == ModelCapabilities.unknown()


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        ("AuthenticationError", "credential"),
        ("RateLimitError", "rate limit"),
        ("ContextWindowExceededError", "context window"),
        ("APIConnectionError", "could not reach"),
        ("BadRequestError", "rejected"),
        ("NotFoundError", "does not have"),
        ("Timeout", "timed out"),
        ("InternalServerError", "provider failed"),
        ("ServiceUnavailableError", "unavailable"),
    ],
)
async def test_sdk_errors_become_actionable_messages(
    raised: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator sees what to do, not a stack trace naming an SDK
    they did not install on purpose."""
    exc = getattr(exceptions, raised)(message="raw", llm_provider="openai", model="gpt-4o")
    monkeypatch.setattr(litellm_provider, "acompletion", _raising(exc))
    provider = LiteLLMProvider(
        plan=_plan(),
        descriptor=ModelDescriptor(provider="openai", model="gpt-4o"),
        capabilities=ModelCapabilities.unknown(),
    )
    with pytest.raises(ProviderRequestError, match=expected):
        await _events(provider)


async def test_an_sdk_error_with_no_mapping_still_becomes_a_provider_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ProviderSDKError` has 20-odd subclasses; the ones without a
    written message must not escape as themselves."""
    monkeypatch.setattr(
        litellm_provider,
        "acompletion",
        _raising(
            exceptions.APIError(
                message="raw", llm_provider="openai", model="gpt-4o", status_code=418
            )
        ),
    )
    provider = LiteLLMProvider(
        plan=_plan(),
        descriptor=ModelDescriptor(provider="openai", model="gpt-4o"),
        capabilities=ModelCapabilities.unknown(),
    )
    with pytest.raises(ProviderRequestError, match="provider failed"):
        await _events(provider)


@pytest.mark.parametrize(
    "name",
    [
        "AuthenticationError",
        "RateLimitError",
        "ContextWindowExceededError",
        "APIConnectionError",
        "BadRequestError",
        "NotFoundError",
        "PermissionDeniedError",
        "InternalServerError",
        "ServiceUnavailableError",
        "Timeout",
    ],
)
def test_every_sdk_error_the_transport_must_map_is_actually_caught(name: str) -> None:
    """The `except` clause has to name a base these classes inherit from.

    Measured on 1.98.0: `litellm.exceptions.APIError` is a base for only
    itself, so `except exceptions.APIError` would let every one of these
    escape the transport unmapped and make the REQUEST_SENT branch dead
    code. The transport catches `ProviderSDKError` (openai.OpenAIError).
    """
    assert issubclass(getattr(exceptions, name), ProviderSDKError), name


@pytest.mark.parametrize(
    "name",
    [
        "AuthenticationError",
        "RateLimitError",
        "ContextWindowExceededError",
        "APIConnectionError",
        "BadRequestError",
        "NotFoundError",
        "PermissionDeniedError",
        "InternalServerError",
        "ServiceUnavailableError",
        "Timeout",
    ],
)
def test_the_litellm_rooted_base_would_have_caught_none_of_them(name: str) -> None:
    """The other half of the measurement above, stated as a tripwire: if a
    future litellm reparents these under its own APIError, this test fails
    and the `except` clause can be revisited deliberately."""
    assert not issubclass(getattr(exceptions, name), exceptions.APIError), name


async def test_an_unmapped_auth_failure_never_escapes_the_transport() -> None:
    """The end-to-end version of the test above: drive a real 401 through
    `complete` and assert korvid's own error type comes out, not the
    SDK's. This is the assertion that fails if someone narrows the
    `except` clause back to a litellm-rooted base."""
    provider = _provider(_answering(401))
    with pytest.raises(ProviderRequestError, match="credential"):
        async for _ in provider.complete(_MESSAGES, []):
            pass


async def test_the_transport_does_not_catch_korvids_own_bugs() -> None:
    """`except Exception` would report a korvid TypeError to the operator
    as a provider failure. The clause is scoped to the SDK's base class,
    so a programming error propagates unchanged."""
    provider = LiteLLMProvider(
        plan=_ExplodingPlan(
            model=_MODEL, api_key=_SECRET, base_url=_BASE_URL, api_version=None, extra={}
        ),
        descriptor=ModelDescriptor(provider="openai", model="gpt-4o"),
        capabilities=ModelCapabilities.unknown(),
    )
    with pytest.raises(TypeError, match="korvid bug"):
        async for _ in provider.complete(_MESSAGES, []):
            pass


async def test_no_secret_appears_in_any_error_message() -> None:
    """Providers echo the offending credential back in 401 bodies. The
    written messages carry no interpolation at all, which is the only
    version of this rule that cannot rot."""
    body = {"error": {"message": f"Incorrect API key provided: {_SECRET}"}}
    with pytest.raises(ProviderRequestError) as excinfo:
        async for _ in _provider(_answering(401, body)).complete(_MESSAGES, []):
            pass
    assert _SECRET not in str(excinfo.value)
    assert _BASE_URL not in str(excinfo.value)


async def test_a_transport_failure_message_names_the_connection_not_the_endpoint() -> None:
    """A base URL can carry a SAS token or a tenant name; the message says
    what happened without quoting it back."""
    handler = _throwing(lambda request: httpx.ConnectError("refused", request=request))
    with pytest.raises(ProviderRequestError) as excinfo:
        async for _ in _provider(handler).complete(_MESSAGES, []):
            pass
    assert _BASE_URL not in str(excinfo.value)
    assert _SECRET not in str(excinfo.value)


# ---------------------------------------------------------------------------
# The safe-message contract
# ---------------------------------------------------------------------------


async def test_a_translated_failure_declares_its_message_operator_safe() -> None:
    """Translation only helps if the runtime is allowed to show it.

    `native_engine` withholds `str(exc)` for every provider exception —
    correctly, since an SDK error quotes the request or the credential it
    was refused with. `OperatorSafeProviderError` is the one narrow way
    out: this class declares the exact texts it may carry, and the runtime
    shows only those. Without the declaration the whole table above is
    dead code the operator never reads.
    """
    with pytest.raises(ProviderRequestError) as excinfo:
        async for _ in _provider(_answering(401)).complete(_MESSAGES, []):
            pass
    error = excinfo.value
    assert isinstance(error, OperatorSafeProviderError)
    assert error.operator_message() is not None
    assert "credential" in str(error.operator_message())


def test_every_message_the_transport_can_raise_is_declared_safe() -> None:
    """The declaration is a list, so it can rot. This is the tripwire: a
    message added to the table but not to `safe_messages` would be
    withheld at the panel and read as a translation that silently stopped
    working."""
    for message in litellm_provider.WRITTEN_MESSAGES:
        assert ProviderRequestError(message).operator_message() == message


def test_a_message_this_class_never_declared_is_not_safe() -> None:
    """Subclassing is not a blanket licence to speak.

    The promise is auditable precisely because it is per-text: a later
    `raise ProviderRequestError(str(sdk_exc))` — the exact refactor this
    contract exists to survive — carries a message no audit approved, so
    the runtime falls back to withholding it.
    """
    assert ProviderRequestError(f"Incorrect API key provided: {_SECRET}").operator_message() is None


async def test_a_stream_iterator_is_returned_not_a_coroutine() -> None:
    """`complete` must be an async generator: a plain async function
    returning an iterator fails the LLMProvider override check."""
    provider = _provider(_streaming([_delta(content="hi")]))
    stream = provider.complete(_MESSAGES, [])
    assert isinstance(stream, AsyncIterator)
    async with aclosing(_as_generator(stream)) as events:
        async for _ in events:
            break
