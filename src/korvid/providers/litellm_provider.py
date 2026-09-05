"""One `LLMProvider` for every model reference, over `litellm.acompletion`.

This module replaces the hand-written vendor adapters: LiteLLM already
speaks each vendor's dialect, so korvid's remaining job is to normalize
one stream shape into the event contract the agent runtime consumes, and
to translate the SDK's exceptions into something an operator can act on.

The normalized events are korvid's own, not LiteLLM's:

- `{"type": REQUEST_SENT}` — bookkeeping, defined in `agent/provider.py`.
- `{"type": "text_delta", "text": ...}` — answer text.
- `{"type": "reasoning", "text": ...}` — `delta.reasoning_content`, kept
  separate so chain-of-thought never lands in the transcript as answer.
- `{"type": "tool_call", "id": ..., "name": ..., "arguments": ...}` —
  emitted whole, at stream end.
- `{"type": "usage", "input_tokens": ..., "output_tokens": ...}` — the
  names `conversation.commit_usage` reads, and only ever the counts the
  provider itself reported. The provider's own `total_tokens` rides along
  when it reported one, because a total is not always the sum of the two.
- `{"type": "done"}` — the terminal event, as both replaced adapters emit.

Everything LiteLLM-shaped is imported from `litellm_runtime`, so this
layer still names exactly one module for the SDK. `httpx` is imported
directly for the two marker classes that say whether a request was
answered; it is declared in the `[agent]` extra for exactly that reason.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Final

import httpx

from korvid.agent.model_policy import ModelCapabilities, ModelDescriptor
from korvid.agent.provider import REQUEST_SENT, LLMProvider, OperatorSafeProviderError
from korvid.providers.litellm_request import RequestPlan
from korvid.providers.litellm_runtime import ProviderSDKError, acompletion, exceptions

# ---------------------------------------------------------------------------
# Written messages — evidence-free by construction
# ---------------------------------------------------------------------------

_AUTH: Final = (
    "The provider refused the credential. Check the profile's API key, or "
    "re-run `:ai` to authenticate again."
)
_PERMISSION: Final = (
    "The credential is not permitted to use this model. Check the account's access to it."
)
_RATE_LIMIT: Final = (
    "The provider applied a rate limit. Wait and retry, or switch to another model."
)
_CONTEXT: Final = (
    "The request exceeded the model's context window. Start a new session, or "
    "shorten the conversation."
)
_NOT_FOUND: Final = (
    "The provider does not have this model. Check the model reference in the profile."
)
_BAD_REQUEST: Final = (
    "The provider rejected the request. Check the model reference and any "
    "per-model options in the profile."
)
_TIMEOUT: Final = "The provider timed out before answering. Retry, or raise the request timeout."
_UNREACHABLE: Final = (
    "korvid could not reach the provider: the connection failed. Check the "
    "endpoint, the network and any proxy."
)
_UNAVAILABLE: Final = "The provider is unavailable right now. Retry shortly."
_INTERNAL: Final = "The provider failed with a server error."
_GENERIC: Final = "The provider failed to answer the request."

#: Ordered because the classes nest: `ContextWindowExceededError` is a
#: `BadRequestError`, so the specific row has to be tried first.
_MESSAGES: Final[tuple[tuple[type[Exception], str], ...]] = (
    (exceptions.AuthenticationError, _AUTH),
    (exceptions.PermissionDeniedError, _PERMISSION),
    (exceptions.RateLimitError, _RATE_LIMIT),
    (exceptions.ContextWindowExceededError, _CONTEXT),
    (exceptions.NotFoundError, _NOT_FOUND),
    (exceptions.BadRequestError, _BAD_REQUEST),
    (exceptions.Timeout, _TIMEOUT),
    (exceptions.APIConnectionError, _UNREACHABLE),
    (exceptions.ServiceUnavailableError, _UNAVAILABLE),
    (exceptions.InternalServerError, _INTERNAL),
)

#: Every message this module can put in front of an operator. Derived from
#: the table so a row added there cannot be forgotten here — the runtime
#: shows only declared messages (`OperatorSafeProviderError`), so an
#: undeclared one would read as a translation that silently stopped
#: working. The three that no row names are listed explicitly.
WRITTEN_MESSAGES: Final[frozenset[str]] = frozenset(
    {message for _, message in _MESSAGES} | {_TIMEOUT, _UNREACHABLE, _GENERIC}
)


class ProviderRequestError(OperatorSafeProviderError):
    """A provider call korvid could not complete, in operator language.

    Replaces `openai_compat.ProviderError`. The message is always one of
    the written constants above: no exception text, no endpoint and no
    option value is interpolated into it, because providers echo the
    offending credential back in their own 401 bodies.

    Those constants are also what this class declares safe, so the runtime
    may show them instead of naming the exception type — see
    `agent/provider.py: OperatorSafeProviderError`. The declaration is the
    exact set: an instance carrying anything else is withheld like any
    other exception, so a message built from an SDK error at some later
    date cannot inherit the exemption.
    """

    safe_messages = WRITTEN_MESSAGES


# ---------------------------------------------------------------------------
# What the transport actually did
# ---------------------------------------------------------------------------

_MAX_CONTEXT_DEPTH: Final = 8


def _transport_marker(exc: BaseException) -> type[httpx.HTTPError] | None:
    """The first `httpx` marker in the `__context__` chain, or `None`.

    Measured on 1.98.0, litellm's own exception says nothing about the
    transport: a refused connection and a genuine HTTP 500 are *both*
    `InternalServerError` with `status_code=500`. What does differ is what
    each one wrapped — `httpx.HTTPStatusError` exists only once a response
    arrived, `httpx.TransportError` only when one did not, and they are
    disjoint.

    Walks `__context__`, not `__cause__`: measured, `__cause__` is `None`
    at the level litellm raises from. Bounded so a self-referential chain
    cannot spin.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CONTEXT_DEPTH):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            return httpx.HTTPStatusError
        if isinstance(current, httpx.TimeoutException):
            return httpx.TimeoutException
        if isinstance(current, httpx.TransportError):
            return httpx.TransportError
        current = current.__context__
    return None


def _request_reached_the_provider(exc: BaseException) -> bool:
    """Did response headers come back before this failed?

    `agent/provider.py` defines REQUEST_SENT as "the transport has
    accepted the request (response headers received), before the status
    code is judged", so an answered 401 or 500 counts and a refused
    connection does not.

    Neither marker means litellm rejected the request before building it —
    an unqualified model reference raises `BadRequestError` with an empty
    chain — so nothing left. Defaulting to "not sent" keeps a false alarm
    off the outbound panel for every routing rejection.
    """
    return _transport_marker(exc) is httpx.HTTPStatusError


def _translate(exc: Exception) -> ProviderRequestError:
    """Map an SDK exception to a written, secret-free message.

    Dispatches on the `litellm.exceptions` classes, which are the concrete
    types LiteLLM raises, after checking the transport marker: a refused
    connection surfaces as `InternalServerError`, and telling the operator
    the provider had a server error would send them looking in the wrong
    place.
    """
    marker = _transport_marker(exc)
    if marker is httpx.TimeoutException:
        return ProviderRequestError(_TIMEOUT)
    if marker is httpx.TransportError:
        return ProviderRequestError(_UNREACHABLE)
    for kind, message in _MESSAGES:
        if isinstance(exc, kind):
            return ProviderRequestError(message)
    return ProviderRequestError(_GENERIC)


# ---------------------------------------------------------------------------
# Stream normalization
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PartialToolCall:
    """One call being assembled from the fragments of several chunks."""

    id: str = ""
    name: str = ""
    arguments: str = ""


def _as_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _count(value: object) -> int | None:
    """A token count as a non-negative int, or `None` when unusable."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage_event(chunk: Any) -> dict[str, Any] | None:
    """The usage event for one frame that carries provider counts.

    Zero on both counts is not a measurement: it is what LiteLLM
    materializes for a response that reported no usage at all (measured on
    1.98.0, a non-streaming body without a `usage` object comes back as
    `Usage(0, 0, 0)`), and a request that had a prompt cannot have cost
    zero prompt tokens. Reporting it would commit an absence as an exact
    count — `conversation.commit_usage` treats any usage event as
    measured — so it is refused here and the round is honestly estimated
    instead.
    """
    usage = getattr(chunk, "usage", None)
    if usage is None:
        return None
    prompt = _count(getattr(usage, "prompt_tokens", None))
    completion = _count(getattr(usage, "completion_tokens", None))
    if prompt is None or completion is None or not (prompt or completion):
        return None
    event: dict[str, Any] = {
        "type": "usage",
        "input_tokens": prompt,
        "output_tokens": completion,
    }
    total = _count(getattr(usage, "total_tokens", None))
    if total is not None:
        event["total_tokens"] = total
    return event


def _provider_usage(response: Any) -> dict[str, Any] | None:
    """The counts the provider itself sent, from the frames it sent them on.

    A streaming plan always sets `stream_options.include_usage`, and
    LiteLLM answers that flag whether or not the provider did: measured on
    1.98.0, a stream whose frames carried no counts still ends with a
    synthesized chunk holding LiteLLM's *own* tokenizer estimate, in the
    same shape a real one has. Passing that on would commit a guess as a
    measurement.

    The wrapper does record the frames it received, in `chunks`, and
    `usage` is set on one of those only when the provider set it — so that
    recording is the provenance signal, pinned by a test against the real
    wrapper. The counts are read from the recorded frame rather than from
    the synthesized tail chunk, because LiteLLM's builder substitutes its
    estimate for a provider that reported *beside* its choices instead of
    on a choices-free frame. The last frame carrying a usable pair wins,
    mirroring LiteLLM's own rule that the most recent usage frame holds
    the total so far.

    No recorded frame carrying counts means no usage event at all: unknown
    tokens and zero tokens are different facts.
    """
    event: dict[str, Any] | None = None
    for chunk in getattr(response, "chunks", None) or ():
        event = _usage_event(chunk) or event
    return event


def _merge_fragment(fragment: Any, partial: _PartialToolCall) -> None:
    """Fold one `delta.tool_calls[*]` fragment into the call it belongs to.

    Verified against 1.98.0: the id and name arrive on the first fragment
    and the arguments arrive split across later ones with `id=None` and
    `name=None`, so the first fragment that carries each wins and the
    arguments are appended in arrival order.
    """
    identifier = _as_text(getattr(fragment, "id", None))
    if identifier and not partial.id:
        partial.id = identifier
    function = getattr(fragment, "function", None)
    name = _as_text(getattr(function, "name", None))
    if name and not partial.name:
        partial.name = name
    partial.arguments += _as_text(getattr(function, "arguments", None))


def _absorb_tool_calls(fragments: Any, calls: dict[int, _PartialToolCall]) -> None:
    """Key fragments by `delta.tool_calls[*].index`, never `choice.index`.

    `choice.index` is `0` on every chunk when `n=1`, so keying on it would
    merge two parallel calls into one malformed call.
    """
    for fragment in fragments or ():
        index = getattr(fragment, "index", None)
        if isinstance(index, int) and not isinstance(index, bool):
            _merge_fragment(fragment, calls.setdefault(index, _PartialToolCall()))


def _chunk_events(chunk: Any, calls: dict[int, _PartialToolCall]) -> Iterator[dict[str, Any]]:
    """Text and reasoning from one chunk; tool fragments go to `calls`."""
    for choice in getattr(chunk, "choices", None) or ():
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        _absorb_tool_calls(getattr(delta, "tool_calls", None), calls)
        content = _as_text(getattr(delta, "content", None))
        if content:
            yield {"type": "text_delta", "text": content}
        reasoning = _as_text(getattr(delta, "reasoning_content", None))
        if reasoning:
            yield {"type": "reasoning", "text": reasoning}


def _tool_call_events(calls: dict[int, _PartialToolCall]) -> Iterator[dict[str, Any]]:
    """Every accumulated call, whole, in ascending call index.

    Arguments are handed over exactly as they arrived, including when they
    do not parse: a truncated call must be refused by the harness, not
    silently repaired into `{}` here.
    """
    for index in sorted(calls):
        partial = calls[index]
        yield {
            "type": "tool_call",
            "id": partial.id,
            "name": partial.name,
            "arguments": partial.arguments,
        }


async def _close_quietly(response: Any) -> None:
    """Close the stream wrapper without masking the error on the way out.

    `CustomStreamWrapper` exposes `aclose()` and no `close()`, so
    `contextlib.closing` would raise instead of cleaning up.
    """
    close = getattr(response, "aclose", None)
    if not callable(close):
        return
    with contextlib.suppress(Exception):
        await close()


async def _stream_events(response: Any) -> AsyncGenerator[dict[str, Any], None]:
    """Normalize a LiteLLM stream into korvid's events.

    Completed tool calls are emitted at stream end so the harness always
    sees whole calls. When the stream fails mid-iteration the accumulated
    calls are dropped: a half-received call is not a call, and the harness
    cannot tell arguments the model never finished writing from arguments
    it meant to send. Usage is likewise taken only from a stream that
    ended — and only from the frames the provider itself sent.
    """
    calls: dict[int, _PartialToolCall] = {}
    usage: dict[str, Any] | None = None
    try:
        async for chunk in response:
            for event in _chunk_events(chunk, calls):
                yield event
        # Read here, inside the `try`: the provenance record belongs to the
        # wrapper, which the `finally` below is about to close.
        usage = _provider_usage(response)
    except asyncio.CancelledError:
        raise
    except ProviderSDKError as exc:
        raise _translate(exc) from exc
    finally:
        await _close_quietly(response)

    for event in _tool_call_events(calls):
        yield event
    if usage is not None:
        yield usage
    yield {"type": "done"}


def _response_events(response: Any) -> Iterator[dict[str, Any]]:
    """Normalize a non-streaming `ModelResponse` into the same events."""
    for choice in getattr(response, "choices", None) or ():
        message = getattr(choice, "message", None)
        content = _as_text(getattr(message, "content", None))
        if content:
            yield {"type": "text_delta", "text": content}
        reasoning = _as_text(getattr(message, "reasoning_content", None))
        if reasoning:
            yield {"type": "reasoning", "text": reasoning}
        for call in getattr(message, "tool_calls", None) or ():
            function = getattr(call, "function", None)
            yield {
                "type": "tool_call",
                "id": _as_text(getattr(call, "id", None)),
                "name": _as_text(getattr(function, "name", None)),
                "arguments": _as_text(getattr(function, "arguments", None)),
            }
    usage = _usage_event(response)
    if usage is not None:
        yield usage
    yield {"type": "done"}


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class LiteLLMProvider(LLMProvider):
    """`LLMProvider` over `litellm.acompletion`.

    Args:
        plan: The resolved request plan (Task 13). It owns the payload;
            this adapter adds nothing of its own to the wire.
        descriptor: What the UI and the router display. Passed in by the
            registry — never parsed back out of the model string.
        capabilities: Translated from catalog data, never inferred from
            the model name. Defaults to `ModelCapabilities.unknown()`.
        client: An optional pre-built SDK client. Only used by tests, and
            passed through `acompletion(client=...)` — which is a
            kwargs-only parameter in 1.98.0.
    """

    def __init__(
        self,
        *,
        plan: RequestPlan,
        descriptor: ModelDescriptor,
        capabilities: ModelCapabilities | None = None,
        client: Any | None = None,
    ) -> None:
        self._plan = plan
        self._descriptor = descriptor
        self._capabilities = (
            capabilities if capabilities is not None else ModelCapabilities.unknown()
        )
        self._client = client

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    @property
    def capabilities(self) -> ModelCapabilities:
        """Report exactly what was translated for this reference.

        The catalog is the only evidence this adapter has; a model tag
        reading `gpt-4o-with-tools-2000k` proves nothing, so an untranslated
        reference stays entirely unknown.
        """
        return self._capabilities

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield normalized completion events as an async generator.

        REQUEST_SENT is yielded *after* the await returns, which is what
        makes it mean "sent": `acompletion` raises before returning when
        the connection failed. It is yielded on the failure path too, but
        only for a request the provider answered — an HTTP 401 or 500
        means it has the payload, and the outbound-inspection panel must
        show that payload rather than a stale one.

        Args:
            messages: Conversation history, already prepared by the policy.
            tools: OpenAI-shaped tool schemas, or empty.
            stream: Whether to stream. Both paths yield the same events.

        Yields:
            Normalized event mappings.

        Raises:
            ProviderRequestError: The provider could not answer. korvid's
                own bugs are not caught — the `except` clause names the
                SDK's base class, so a `TypeError` propagates unchanged.
        """
        kwargs = self._plan.call_kwargs(messages, tools, stream=stream)
        if self._client is not None:
            kwargs["client"] = self._client  # kwargs-only in 1.98.0
        try:
            response = await acompletion(**kwargs)
        except asyncio.CancelledError:
            raise
        except ProviderSDKError as exc:
            if _request_reached_the_provider(exc):
                yield {"type": REQUEST_SENT}
            raise _translate(exc) from exc
        yield {"type": REQUEST_SENT}

        if not hasattr(response, "__aiter__"):
            for event in _response_events(response):
                yield event
            return
        # aclosing, not a bare `async for`: when the consumer abandons this
        # generator the inner one has to be closed then and there, or the
        # HTTP response stays open until the collector gets to it.
        async with aclosing(_stream_events(response)) as events:
            async for event in events:
                yield event
