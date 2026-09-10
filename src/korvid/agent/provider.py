"""LLMProvider ABC — the pluggable boundary (design doc §6.3, standards §3).

Concrete providers are built from a connection profile by
`korvid.providers.litellm_factory.create_provider_from_profile`, which
delegates routing rather than owning a name-to-class table. SpecialFlow
builders return implementations of this contract; their discovery and loading
stay outside this ABC boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, ClassVar, Final

from korvid.agent.model_policy import ModelCapabilities, ModelDescriptor

REQUEST_SENT: Final = "request_sent"
"""Event type an adapter may yield once its request is on the wire.

`complete` is an async generator, so obtaining it transmits nothing: the
body only runs on the first `__anext__`. The runtime records the exact
payload it handed over as the session's latest outbound request, and that
record must mean *sent*, not *intended* — a missing credential or an
unresolvable host must leave the previous real handoff on display.

Built-in adapters therefore yield `{"type": REQUEST_SENT}` as soon as the
transport has accepted the request (response headers received), before
the status code is judged: an HTTP 500 answer still means the provider
has the payload. The runtime consumes it as bookkeeping and never renders
it. A provider that emits no acknowledgement is recorded on its first
completion event instead, which also proves that the request ran.
"""


class OperatorSafeProviderError(Exception):
    """A provider failure whose own message an adapter vouches for.

    The runtime's default is to withhold every exception's text: a
    transport, adapter or protocol error routinely carries the provider's
    response body — a 401 quotes the credential it refused, a validation
    error quotes the prompt — and truncating that does not help, because
    the first characters of a 401 body are exactly the part that
    identifies the key. A failed request is therefore named by its
    exception type and nothing else.

    That default makes an adapter's *translation* unreachable. A written
    sentence like "check the profile's API key" is the only part of a
    provider failure an operator can act on, and it is evidence-free by
    construction — it interpolates nothing. This class is the one narrow
    way to say so, and the promise is per-message rather than per-class:
    a subclass declares in `safe_messages` the exact texts its instances
    may carry, and the runtime shows a message only when it is one of
    them. A later `raise TranslatedError(str(sdk_exc))` therefore still
    reaches the operator as a withheld failure rather than as a leak, so
    the audit cannot silently rot into a blanket exemption.

    Subclassing is a claim about the *messages listed*, not about the
    exception: never list a message built from an exception, a response
    body, an endpoint, a payload or any option value.
    """

    #: Every message instances of this class may show an operator. Each
    #: one must be a constant this repository wrote and audited.
    safe_messages: ClassVar[frozenset[str]] = frozenset()

    def operator_message(self) -> str | None:
        """This failure's text, when the class declared that text safe.

        Returns:
            The message, or `None` when it was never declared — in which
            case the caller must fall back to withholding it.
        """
        text = str(self)
        return text if text in self.safe_messages else None


# ---------------------------------------------------------------------------
# The stream-safety contract every built-in adapter shares (issue #336)
# ---------------------------------------------------------------------------

MAX_TOOL_ARGUMENT_CHARS: Final = 65_536
"""Cumulative cap on one tool call's accumulated argument text.

Adapters enforce this before the engine sees the completed call, so an
unfinished argument stream cannot grow an unbounded transport buffer.
"""

MAX_TOOL_CALLS_PER_RESPONSE: Final = 64
"""Cap on how many distinct calls one response may accumulate.

Every protocol keys parallel calls by an index the provider chooses, so a
stream can open a new accumulator entry per fragment. The engine's own
per-iteration cap is a *policy* number applied after the whole response
arrived; this is the structural one that keeps the response finite. It is
far above any real batch — the policy cap ships single digits — so it can
only be reached by a provider that is malfunctioning or hostile.
"""

MAX_REASONING_CHARS: Final = 65_536
"""Cumulative cap on reasoning text an adapter accumulates for later use.

Reasoning that is yielded straight through is charged to the engine's
response budget as it streams. This bounds the other kind: text an adapter
holds in memory to re-attach to a later request.
"""

STREAM_TRUNCATED: Final = (
    "The provider's answer ended before its protocol said it was complete, "
    "so korvid discarded it. Retry the request."
)

STREAM_LIMIT: Final = (
    "The provider's answer grew past korvid's limit for one response and was "
    "stopped. Retry, or switch to another model."
)

STREAM_MALFORMED: Final = (
    "The provider sent an answer korvid could not read. Retry, or check that "
    "the endpoint speaks this provider's protocol."
)

STREAM_FAILED: Final = (
    "The provider reported a failure while it was answering. Retry, or check "
    "the provider's own logs."
)

CREDENTIAL_REFUSED: Final = (
    "The provider refused the credential. Check the profile's API key, or "
    "re-run `:ai` to authenticate again."
)

NOT_PERMITTED: Final = (
    "The credential is not permitted to use this model. Check the account's access to it."
)

RATE_LIMITED: Final = (
    "The provider applied a rate limit. Wait and retry, or switch to another model."
)

MODEL_UNKNOWN: Final = (
    "The provider does not have this model. Check the model reference in the profile."
)

REQUEST_REJECTED: Final = (
    "The provider rejected the request. Check the model reference and any "
    "per-model options in the profile."
)

UNAVAILABLE: Final = "The provider is unavailable right now. Retry shortly."

SERVER_ERROR: Final = "The provider failed with a server error."

TIMED_OUT: Final = "The provider timed out before answering. Retry, or raise the request timeout."

UNREACHABLE: Final = (
    "korvid could not reach the provider: the connection failed. Check the "
    "endpoint, the network and any proxy."
)

STREAM_MESSAGES: Final[tuple[str, ...]] = (
    STREAM_TRUNCATED,
    STREAM_LIMIT,
    STREAM_MALFORMED,
    STREAM_FAILED,
    CREDENTIAL_REFUSED,
    NOT_PERMITTED,
    RATE_LIMITED,
    MODEL_UNKNOWN,
    REQUEST_REJECTED,
    UNAVAILABLE,
    SERVER_ERROR,
    TIMED_OUT,
    UNREACHABLE,
)
"""Every sentence this contract may show an operator, written and audited.

Each one is evidence-free by construction: it interpolates no exception,
no response body, no endpoint and no option value. Adapters translate
*into* this vocabulary; none of them writes a message of its own from a
provider's answer, because the first characters of a 401 body are exactly
the part that identifies the key.
"""


class ProviderStreamError(OperatorSafeProviderError):
    """Base of the typed answers an adapter may refuse a response with.

    A caller that only needs "korvid would not accept this answer" catches
    this; the subclasses say which of the five situations it was.
    """

    safe_messages = frozenset(STREAM_MESSAGES)


class ProviderStreamTruncatedError(ProviderStreamError):
    """A stream ended without the terminal marker its protocol requires.

    Partial text may already have reached the transcript — it really was
    streamed — but the response is not a completed one, so no adapter may
    follow it with tool calls, usage or `done`.
    """

    safe_messages = frozenset({STREAM_TRUNCATED})


class ProviderStreamLimitError(ProviderStreamError):
    """A stream exhausted one of the cumulative bounds above."""

    safe_messages = frozenset({STREAM_LIMIT})


class ProviderProtocolError(ProviderStreamError):
    """The answer was not this protocol's, or declared its own failure."""

    safe_messages = frozenset({STREAM_MALFORMED, STREAM_FAILED})


class ProviderStatusError(ProviderStreamError):
    """The provider answered the request by refusing it."""

    safe_messages = frozenset(
        {
            CREDENTIAL_REFUSED,
            NOT_PERMITTED,
            RATE_LIMITED,
            MODEL_UNKNOWN,
            REQUEST_REJECTED,
            UNAVAILABLE,
            SERVER_ERROR,
        }
    )


class ProviderTransportError(ProviderStreamError):
    """The request or the answer never made it across the network."""

    safe_messages = frozenset({TIMED_OUT, UNREACHABLE})


_STATUS_MESSAGES: Final[tuple[tuple[int, str], ...]] = (
    (401, CREDENTIAL_REFUSED),
    (403, NOT_PERMITTED),
    (404, MODEL_UNKNOWN),
    (429, RATE_LIMITED),
    (503, UNAVAILABLE),
)


def status_error(status: int) -> ProviderStatusError:
    """Translate a refusing HTTP status into an operator-safe failure.

    The status *class* is what an operator can act on — a wrong key, a
    model the account cannot reach, a rate limit — and it is knowable
    without reading a single byte of the body, which is the point: the
    body is where a provider echoes the credential it just refused.

    Args:
        status: The response status an adapter received.

    Returns:
        The failure to raise, carrying a written message and nothing else.
    """
    for code, message in _STATUS_MESSAGES:
        if status == code:
            return ProviderStatusError(message)
    return ProviderStatusError(SERVER_ERROR if status >= 500 else REQUEST_REJECTED)


def append_bounded(accumulated: str, fragment: str, *, limit: int) -> str:
    """Append *fragment*, refusing before the total can pass *limit*.

    The bound is on the total rather than on one fragment because that is
    the shape of the failure: arguments and reasoning both arrive a few
    characters at a time, so any per-fragment check is unbounded overall.

    Args:
        accumulated: What this call or turn has collected so far.
        fragment: The piece that just arrived.
        limit: The cumulative character bound to hold.

    Returns:
        The new accumulated text.

    Raises:
        ProviderStreamLimitError: Appending would pass *limit*. Nothing is
            stored, so the caller's accumulator cannot grow past it.
    """
    if len(accumulated) + len(fragment) > limit:
        raise ProviderStreamLimitError(STREAM_LIMIT)
    return accumulated + fragment


def guard_tool_call_count(count: int) -> None:
    """Refuse a response that opened more accumulators than the bound.

    Args:
        count: How many distinct calls the response has opened, including
            the one about to be opened.

    Raises:
        ProviderStreamLimitError: *count* passes `MAX_TOOL_CALLS_PER_RESPONSE`.
    """
    if count > MAX_TOOL_CALLS_PER_RESPONSE:
        raise ProviderStreamLimitError(STREAM_LIMIT)


class LLMProvider(ABC):
    @property
    @abstractmethod
    def descriptor(self) -> ModelDescriptor:
        """Identify the model this adapter talks to by provider and model tag.

        `descriptor.model` is shown in the status bar and recorded as
        `OutboundSnapshot.model`, so it must name the *model* — every
        built-in returns its model tag (`qwen3:8b`, `gpt-4o`), and a
        plugin may qualify it (`company-llm:v2`). It is not the endpoint.

        `descriptor.provider` is the canonical provider id (`ollama`,
        `openai-compat`, `github-copilot`, or a plugin's registered name)
        — a built-in adapter never guesses it from the base URL or model
        name; the registry/factory that constructed it passes it in.
        """

    @property
    @abstractmethod
    def capabilities(self) -> ModelCapabilities:
        """Report the model facts this adapter directly knows.

        Any fact the adapter cannot directly prove — from an explicit
        per-request option (e.g. Ollama's `num_ctx`) or explicit config —
        stays unknown (`None`). Adapters must never infer capability from
        the model or provider name; `ModelCapabilities.unknown()` is the
        correct answer absent direct evidence.
        """

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield completion events (text deltas and tool calls).

        Implementations must be async generators (async def with yield),
        which satisfy this AsyncIterator signature under mypy --strict.
        Do NOT write a plain async function returning an iterator—that
        produces a coroutine and fails the override check.

        Adapters may emit `{"type": REQUEST_SENT}` once the
        transport has accepted the request, so the runtime can tell a
        payload that was really handed over from one whose generator was
        never started. This includes custom SpecialFlow adapters. Without
        an acknowledgement, the first completion event proves the handoff.
        """

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Adapt conversation history to this provider's wire dialect.

        Called *before* the outbound policy, so anything an adapter adds
        here is sanitized, size-checked and recorded in the exact payload
        snapshot the user can inspect — an adapter must never reshape
        messages inside `complete`, because that content would bypass the
        boundary. Default: the identity, so existing adapters keep
        sending exactly the messages the policy prepared.

        Args:
            messages: Conversation history, OpenAI-shaped, safe to consume
                (a private copy — mutating it cannot affect the runtime).

        Returns:
            The messages to hand to the policy, still OpenAI-shaped apart
            from provider-specific fields the policy knows how to
            sanitize.
        """
        return messages

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release provider-owned resources (HTTP clients etc). Default: no-op."""
