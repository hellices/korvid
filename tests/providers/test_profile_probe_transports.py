"""The wizard's probe on the transports korvid actually ships.

`tests/providers/test_profile_probe.py` drives `ProfileProbe` against a
scripted provider, which is the right level for the probe's own rules.
This module answers the question that level cannot: does a *real* adapter,
reading a *real* wire, still refuse a connection whose answer never said
it finished — when that answer is longer than the probe's bound?

That combination is the regression. The probe used to stop reading at its
bound and return the truncated text, so `provider.complete` was abandoned
before it could apply its terminal-marker postcondition: any server that
streamed more than 4,096 characters and then died mid-answer green-lit the
profile with a success string. Both dialects are covered, because the
terminal marker is per-protocol.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from korvid.agent.model_policy import ModelCapabilities, ModelDescriptor
from korvid.agent.model_profiles import ConnectionAuthConfig, ModelConnectionConfig
from korvid.agent.provider import (
    STREAM_LIMIT,
    STREAM_TRUNCATED,
    OperatorSafeProviderError,
    ProviderStreamLimitError,
    ProviderStreamTruncatedError,
)
from korvid.providers.flow_copilot import COPILOT_CHAT_BASE_URL, CopilotChatProvider
from korvid.providers.profile_probe import PROBE_MAX_RESPONSE_CHARS, ProfileProbe
from korvid.providers.static_creds import StaticHeaderSource

_PROFILE = ModelConnectionConfig(
    model="openai/gpt-4o",
    endpoint="https://mock.invalid/v1",
    auth=ConnectionAuthConfig(method="none"),
)

#: One character past the probe's bound, so the refusal cannot be read as
#: an accident of chunking.
_TOO_LONG = "z" * (PROBE_MAX_RESPONSE_CHARS + 1)


def _probe_with(monkeypatch: pytest.MonkeyPatch, provider: object) -> ProfileProbe:
    """Give the probe a real adapter instead of the factory's."""
    monkeypatch.setattr(
        "korvid.providers.profile_probe.create_provider_from_profile",
        lambda profile, **kwargs: provider,
    )
    return ProfileProbe()


# ---------------------------------------------------------------------------
# The shared LiteLLM transport: `finish_reason` is its terminal evidence
# ---------------------------------------------------------------------------


def _openai_sse(*frames: dict[str, Any]) -> bytes:
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    return (body + "data: [DONE]\n\n").encode()


def _content(text: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-4o",
        "choices": [{"index": 0, "delta": {"content": text}}],
    }


def _finish_frame() -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-4o",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }


def _litellm_provider(frames: list[dict[str, Any]]) -> Any:
    """A real `LiteLLMProvider` whose wire is an `httpx.MockTransport`."""
    from openai import AsyncOpenAI

    from korvid.providers.litellm_provider import LiteLLMProvider
    from korvid.providers.litellm_request import build_plan

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_openai_sse(*frames),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    return LiteLLMProvider(
        plan=build_plan(
            model="openai/gpt-4o",
            api_key="sk-secret-value",
            base_url="https://mock.invalid/v1",
            options={},
            supported=[],
        ),
        descriptor=ModelDescriptor(provider="openai", model="gpt-4o"),
        capabilities=ModelCapabilities.unknown(),
        client=AsyncOpenAI(
            base_url="https://mock.invalid/v1",
            api_key="sk-secret-value",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            max_retries=0,
        ),
    )


async def test_a_long_answer_that_never_finished_fails_the_real_shared_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression, end to end: over the bound *and* never finished.

    Truncating at the bound returned this stream's first 4,096 characters
    as a passing connection test. The bound is now the refusal, so the
    wizard is told the answer was not one.
    """
    pytest.importorskip("litellm")
    probe = _probe_with(monkeypatch, _litellm_provider([_content(_TOO_LONG)]))

    with pytest.raises(ProviderStreamLimitError) as raised:
        await probe(_PROFILE)

    assert raised.value.operator_message() == STREAM_LIMIT


async def test_a_short_answer_that_never_finished_also_fails_the_real_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the bound the adapter's own postcondition is what refuses —
    and the probe now reads far enough to reach it."""
    pytest.importorskip("litellm")
    probe = _probe_with(monkeypatch, _litellm_provider([_content("ok")]))

    with pytest.raises(ProviderStreamTruncatedError) as raised:
        await probe(_PROFILE)

    assert raised.value.operator_message() == STREAM_TRUNCATED


async def test_a_finished_short_answer_still_passes_the_real_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness has to be able to say yes, or the refusals above prove
    nothing about the terminal marker."""
    pytest.importorskip("litellm")
    probe = _probe_with(monkeypatch, _litellm_provider([_content("ok"), _finish_frame()]))

    assert await probe(_PROFILE) == "ok"


async def test_a_finished_but_over_long_answer_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that finishes properly and answers with a document is
    still not a connection test the wizard may show."""
    pytest.importorskip("litellm")
    probe = _probe_with(monkeypatch, _litellm_provider([_content(_TOO_LONG), _finish_frame()]))

    with pytest.raises(ProviderStreamLimitError):
        await probe(_PROFILE)


async def test_a_finished_answer_exactly_at_the_bound_still_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound may be reached over a real wire, not only passed.

    Split across two frames so the check is proved cumulative rather than
    per-chunk, and finished properly, because "at the bound" and "never
    finished" are two different verdicts.
    """
    pytest.importorskip("litellm")
    at_cap = "z" * PROBE_MAX_RESPONSE_CHARS
    probe = _probe_with(
        monkeypatch,
        _litellm_provider(
            [_content(at_cap[:-1]), _content(at_cap[-1]), _finish_frame()],
        ),
    )

    assert await probe(_PROFILE) == at_cap


# ---------------------------------------------------------------------------
# The Copilot dialect: `[DONE]` on the wire is its terminal evidence
# ---------------------------------------------------------------------------


def _copilot_provider(body: str) -> CopilotChatProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    return CopilotChatProvider(
        base_url=COPILOT_CHAT_BASE_URL,
        model="gpt-4o",
        credentials=StaticHeaderSource("cop-1"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _copilot_body(text: str, *, terminal: bool) -> str:
    frame = json.dumps({"choices": [{"delta": {"content": text}}]})
    return f"data: {frame}\n\n" + ("data: [DONE]\n\n" if terminal else "")


async def test_a_long_answer_that_never_finished_fails_the_real_copilot_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _probe_with(monkeypatch, _copilot_provider(_copilot_body(_TOO_LONG, terminal=False)))

    with pytest.raises(OperatorSafeProviderError) as raised:
        await probe(_PROFILE)

    assert isinstance(raised.value, ProviderStreamLimitError)
    assert raised.value.operator_message() == STREAM_LIMIT


async def test_a_short_answer_without_the_wire_terminal_fails_the_copilot_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _probe_with(monkeypatch, _copilot_provider(_copilot_body("ok", terminal=False)))

    with pytest.raises(ProviderStreamTruncatedError):
        await probe(_PROFILE)


async def test_a_finished_copilot_answer_still_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _probe_with(monkeypatch, _copilot_provider(_copilot_body("ok", terminal=True)))

    assert await probe(_PROFILE) == "ok"
