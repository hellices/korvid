"""The wizard's connection probe, on the profile factory.

These tests carry over `tests/providers/test_configurator.py`'s probe
contract — a prepared copy of the message, an `aclose` on every path, a
refusal when nothing could be built, and a refusal when the provider
streams nothing — onto `ProfileProbe`, which builds its provider with the
same `create_provider_from_profile` the running agent uses.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from korvid.agent.model_policy import ModelDescriptor
from korvid.agent.model_profiles import ConnectionAuthConfig, ModelConnectionConfig
from korvid.agent.outbound import OutboundPolicy, OutboundPolicyError
from korvid.agent.provider import (
    CREDENTIAL_REFUSED,
    STREAM_LIMIT,
    OperatorSafeProviderError,
    ProviderProtocolError,
    ProviderStatusError,
    ProviderStreamLimitError,
)
from korvid.providers.profile_probe import (
    PROBE_MAX_RESPONSE_CHARS,
    PROBE_MESSAGE,
    PROBE_REFUSED,
    ProbeFailed,
    ProfileProbe,
)

#: A failure text shaped like the ones a real transport raises: a status
#: line, the credential it refused, and the endpoint it refused it at.
_LEAKY = "401 Unauthorized: key sk-live-9f3c2a rejected by https://vault.internal.test/v1"

_PROFILE = ModelConnectionConfig(
    model="openai/gpt-4o",
    endpoint="https://example.test/v1",
    auth=ConnectionAuthConfig(method="none"),
)


class ScriptedProvider:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.closed = False
        self.calls: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("test", "scripted")

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append((messages, tools))

        async def gen() -> AsyncIterator[dict[str, Any]]:
            for ev in self._events:
                yield ev

        return gen()

    async def aclose(self) -> None:
        self.closed = True


def _patch_factory(monkeypatch: pytest.MonkeyPatch, result: object) -> list[dict[str, Any]]:
    """Replace the factory and record the keyword arguments it received."""
    seen: list[dict[str, Any]] = []

    def fake(profile: ModelConnectionConfig, **kwargs: Any) -> object:
        seen.append({"profile": profile, **kwargs})
        return result

    monkeypatch.setattr("korvid.providers.profile_probe.create_provider_from_profile", fake)
    return seen


async def test_probe_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ScriptedProvider([{"type": "text_delta", "text": "ok"}, {"type": "done"}])
    _patch_factory(monkeypatch, provider)

    assert await ProfileProbe()(_PROFILE) == "ok"
    assert provider.closed  # aclose'd even on success


async def test_probe_receives_a_prepared_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider must never be handed korvid's own module constant."""
    provider = ScriptedProvider([{"type": "text_delta", "text": "ok"}, {"type": "done"}])
    _patch_factory(monkeypatch, provider)

    assert await ProfileProbe()(_PROFILE) == "ok"

    messages, tools = provider.calls[0]
    assert messages == [dict(PROBE_MESSAGE)]
    assert messages[0] is not PROBE_MESSAGE
    assert tools == []
    messages[0]["content"] = "provider mutation"
    assert PROBE_MESSAGE["content"] == "Reply with the single word: ok"


async def test_probe_policy_failure_prevents_delegation_and_closes_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingPolicy(OutboundPolicy):
        def prepare(self, *args: Any, **kwargs: Any) -> Any:
            raise OutboundPolicyError(_LEAKY)

    provider = ScriptedProvider([{"type": "text_delta", "text": "unexpected"}])
    _patch_factory(monkeypatch, provider)
    probe = ProfileProbe()
    probe._outbound = BlockingPolicy(max_request_chars=4_096)

    with pytest.raises(ProbeFailed, match="connection test failed") as raised:
        await probe(_PROFILE)

    # The policy's own text quotes whatever it refused. It reaches the log,
    # never the wizard.
    assert str(raised.value) == PROBE_REFUSED
    assert "sk-live-9f3c2a" not in str(raised.value)
    assert provider.calls == []
    assert provider.closed


async def test_a_factory_failure_is_reported_as_the_written_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building the provider is where a credential is resolved.

    A store that refuses, a flow that raises, a keyring that is locked —
    each raises with its own text, and that text routinely quotes the
    credential and the endpoint it was refused at. The wizard renders the
    exception, so the probe answers with a written constant instead.
    """

    def explode(profile: ModelConnectionConfig, **kwargs: Any) -> object:
        raise RuntimeError(_LEAKY)

    monkeypatch.setattr("korvid.providers.profile_probe.create_provider_from_profile", explode)

    with pytest.raises(ProbeFailed) as raised:
        await ProfileProbe()(_PROFILE)

    message = str(raised.value)
    assert message == PROBE_REFUSED
    assert raised.value.operator_message() == message
    assert "sk-live-9f3c2a" not in message
    assert "vault.internal.test" not in message
    assert "RuntimeError" not in message


async def test_a_streaming_failure_is_reported_as_the_written_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport failure mid-stream carries the response body with it."""

    class _Exploding(ScriptedProvider):
        def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            self.calls.append((messages, tools))

            async def gen() -> AsyncIterator[dict[str, Any]]:
                yield {"type": "text_delta", "text": "partial"}
                raise RuntimeError(_LEAKY)

            return gen()

    provider = _Exploding([])
    _patch_factory(monkeypatch, provider)

    with pytest.raises(ProbeFailed) as raised:
        await ProfileProbe()(_PROFILE)

    assert str(raised.value) == PROBE_REFUSED
    assert "sk-live-9f3c2a" not in str(raised.value)
    assert provider.closed


async def test_a_close_failure_is_reported_as_the_written_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`aclose` runs on every path, so it is a leak path of its own."""

    class _UncloseableProvider(ScriptedProvider):
        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError(_LEAKY)

    provider = _UncloseableProvider([{"type": "text_delta", "text": "ok"}, {"type": "done"}])
    _patch_factory(monkeypatch, provider)

    with pytest.raises(ProbeFailed) as raised:
        await ProfileProbe()(_PROFILE)

    assert str(raised.value) == PROBE_REFUSED
    assert "sk-live-9f3c2a" not in str(raised.value)
    assert provider.closed


class _FailingProvider(ScriptedProvider):
    """A provider whose stream and whose `aclose` each fail on demand.

    Both halves of the probe's unwind path are failure paths, and which
    exception survives the other is the contract these tests pin.
    """

    def __init__(self, *, body_error: BaseException, close_error: BaseException | None) -> None:
        super().__init__([])
        self._body_error = body_error
        self._close_error = close_error

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append((messages, tools))
        error = self._body_error

        async def gen() -> AsyncIterator[dict[str, Any]]:
            raise error
            yield {}  # pragma: no cover - unreachable, makes this a generator

        return gen()

    async def aclose(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


async def test_a_close_failure_does_not_mask_a_declared_safe_refusal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The stream's refusal is the answer; the close is bookkeeping.

    A provider that refused with a sentence it declared operator-safe has
    told the operator the one thing they can act on. If `aclose` then
    fails, letting the close failure out would replace that sentence with
    the probe's generic refusal — the operator would lose the real reason
    because a socket misbehaved on the way out.
    """
    provider = _FailingProvider(
        body_error=ProviderStreamLimitError(STREAM_LIMIT),
        close_error=RuntimeError(_LEAKY),
    )
    _patch_factory(monkeypatch, provider)

    with (
        caplog.at_level("WARNING", logger="korvid.providers.profile_probe"),
        pytest.raises(ProviderStreamLimitError) as raised,
    ):
        await ProfileProbe()(_PROFILE)

    assert str(raised.value) == STREAM_LIMIT
    assert raised.value.operator_message() == STREAM_LIMIT
    assert "sk-live-9f3c2a" not in str(raised.value)
    assert provider.closed
    # Suppressed, not lost: the close failure is still on the record.
    assert caplog.records


async def test_a_close_failure_over_an_unsafe_failure_withholds_both_texts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two secret-bearing failures still leave exactly one written sentence."""
    close_leak = "500 from https://vault.internal.test/v1 while releasing key sk-live-9f3c2a"
    provider = _FailingProvider(
        body_error=RuntimeError(_LEAKY),
        close_error=RuntimeError(close_leak),
    )
    _patch_factory(monkeypatch, provider)

    with pytest.raises(ProbeFailed) as raised:
        await ProfileProbe()(_PROFILE)

    message = str(raised.value)
    assert message == PROBE_REFUSED
    assert "sk-live-9f3c2a" not in message
    assert "vault.internal.test" not in message
    assert provider.closed


async def test_a_failing_close_never_converts_a_cancelled_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation outranks a close that fails while unwinding it."""
    provider = _FailingProvider(
        body_error=asyncio.CancelledError(),
        close_error=RuntimeError(_LEAKY),
    )
    _patch_factory(monkeypatch, provider)

    with pytest.raises(asyncio.CancelledError):
        await ProfileProbe()(_PROFILE)

    assert provider.closed


async def test_a_cancellation_while_closing_is_never_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other order: the body failed, then the close was cancelled.

    Suppressing this one would leave the task looking like an ordinary
    probe failure while its cancellation was quietly dropped.
    """
    provider = _FailingProvider(
        body_error=RuntimeError(_LEAKY),
        close_error=asyncio.CancelledError(),
    )
    _patch_factory(monkeypatch, provider)

    with pytest.raises(asyncio.CancelledError):
        await ProfileProbe()(_PROFILE)

    assert provider.closed


async def test_a_declared_operator_safe_refusal_is_re_raised_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adapter's audited sentence is the whole point of the type.

    "The provider refused the credential — check the profile's API key" is
    what an operator can act on; replacing it with the probe's generic
    answer would make the wizard less useful, not safer.
    """

    class _Refusing(ScriptedProvider):
        def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            self.calls.append((messages, tools))

            async def gen() -> AsyncIterator[dict[str, Any]]:
                raise ProviderStatusError(CREDENTIAL_REFUSED)
                yield {}  # pragma: no cover - unreachable, makes this a generator

            return gen()

    provider = _Refusing([])
    _patch_factory(monkeypatch, provider)

    with pytest.raises(ProviderStatusError) as raised:
        await ProfileProbe()(_PROFILE)

    assert str(raised.value) == CREDENTIAL_REFUSED
    assert provider.closed


async def test_an_undeclared_operator_safe_message_is_still_withheld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The promise is per-message, not per-class (see `OperatorSafeProviderError`).

    A third-party adapter is free to raise `ProviderProtocolError(str(sdk_exc))`.
    That subclass declares two constants and that text is neither, so the
    class's own contract already calls it unsafe — the probe must not hand
    it to a wizard that renders `str(exc)`.
    """

    class _Leaking(ScriptedProvider):
        def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            self.calls.append((messages, tools))

            async def gen() -> AsyncIterator[dict[str, Any]]:
                raise ProviderProtocolError(_LEAKY)
                yield {}  # pragma: no cover - unreachable, makes this a generator

            return gen()

    provider = _Leaking([])
    _patch_factory(monkeypatch, provider)

    with pytest.raises(ProbeFailed) as raised:
        await ProfileProbe()(_PROFILE)

    assert str(raised.value) == PROBE_REFUSED
    assert "sk-live-9f3c2a" not in str(raised.value)
    assert provider.closed


async def test_cancellation_is_never_converted_into_a_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the wizard's probe must cancel the task, not fail it.

    Swallowing `CancelledError` into an ordinary refusal breaks the
    cancellation protocol: the awaiting worker would carry on as if the
    probe had merely failed.
    """

    class _Cancelled(ScriptedProvider):
        def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            self.calls.append((messages, tools))

            async def gen() -> AsyncIterator[dict[str, Any]]:
                raise asyncio.CancelledError
                yield {}  # pragma: no cover - unreachable, makes this a generator

            return gen()

    provider = _Cancelled([])
    _patch_factory(monkeypatch, provider)

    with pytest.raises(asyncio.CancelledError):
        await ProfileProbe()(_PROFILE)

    assert provider.closed


async def test_every_refusal_the_probe_writes_is_declared_safe() -> None:
    """`ProbeFailed`'s messages are the wizard's rendered text.

    The class vouches per message, so a constant added to the module
    without being declared would be a silent hole.
    """
    assert PROBE_REFUSED in ProbeFailed.safe_messages
    assert ProbeFailed(PROBE_REFUSED).operator_message() == PROBE_REFUSED


async def test_probe_raises_when_the_factory_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_provider_from_profile` logs its reason and returns None.

    The probe must turn that into an error the wizard can render, not a
    blank success.
    """
    _patch_factory(monkeypatch, None)

    with pytest.raises(ProbeFailed, match="configuration incomplete") as raised:
        await ProfileProbe()(_PROFILE)

    # The wizard renders the message, so it has to be one the contract
    # declared safe rather than whatever text a failure carried.
    assert isinstance(raised.value, OperatorSafeProviderError)
    assert raised.value.operator_message() == str(raised.value)


async def test_probe_raises_on_empty_text(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ScriptedProvider([{"type": "done"}])
    _patch_factory(monkeypatch, provider)

    with pytest.raises(ProbeFailed, match="returned no text") as raised:
        await ProfileProbe()(_PROFILE)
    assert raised.value.operator_message() == str(raised.value)
    assert provider.closed


async def test_probe_threads_its_wiring_into_the_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe and the running agent must build the same provider.

    A probe that quietly used different trust, a different credential
    store or a different flow registry would answer a question the
    operator did not ask — issue #168's failure mode.
    """
    provider = ScriptedProvider([{"type": "text_delta", "text": "ok"}])
    seen = _patch_factory(monkeypatch, provider)
    catalog = object()
    flows = object()
    credentials = object()
    provider_defaults = object()

    probe = ProfileProbe(
        catalog=catalog,  # type: ignore[arg-type]  # a stub is enough: the probe only forwards it
        flows=flows,  # type: ignore[arg-type]  # ditto
        credentials=credentials,  # type: ignore[arg-type]  # ditto
        provider_defaults=provider_defaults,  # type: ignore[arg-type]  # ditto
        ca_bundle="/etc/ssl/corp.pem",
    )
    await probe(_PROFILE)

    assert seen == [
        {
            "profile": _PROFILE,
            "catalog": catalog,
            "flows": flows,
            "credentials": credentials,
            "provider_defaults": provider_defaults,
            "ca_bundle": "/etc/ssl/corp.pem",
        }
    ]


async def test_an_answer_past_the_bound_fails_the_probe_instead_of_being_cut_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog's contract is "a short human-readable result".

    Truncating to the bound and returning was the bug: the probe stopped
    reading at the cap, so it never reached the point where a provider
    says whether it finished, and a cut-off stream longer than the cap
    green-lit the profile with a success string. Passing the bound is now
    the answer itself — a typed, operator-safe refusal.
    """
    provider = ScriptedProvider(
        [{"type": "text_delta", "text": "z" * 4_096} for _ in range(64)] + [{"type": "done"}]
    )
    _patch_factory(monkeypatch, provider)

    with pytest.raises(ProviderStreamLimitError, match="grew past") as raised:
        await ProfileProbe()(_PROFILE)

    assert raised.value.operator_message() == STREAM_LIMIT
    assert provider.closed


async def test_an_over_long_answer_is_refused_even_when_the_stream_ends_properly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal marker after the bound does not rescue the answer.

    The wizard's result is shown to an operator and held outside any turn,
    so "the provider did finish, eventually" is not the question. One
    character past the cap is a refusal whatever follows it.
    """
    provider = ScriptedProvider(
        [
            {"type": "text_delta", "text": "z" * PROBE_MAX_RESPONSE_CHARS},
            {"type": "text_delta", "text": "!"},
            {"type": "done"},
        ]
    )
    _patch_factory(monkeypatch, provider)

    with pytest.raises(ProviderStreamLimitError):
        await ProfileProbe()(_PROFILE)

    assert provider.closed


async def test_an_answer_exactly_at_the_bound_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is a ceiling that may be reached, not one that may be passed."""
    provider = ScriptedProvider(
        [
            {"type": "text_delta", "text": "z" * (PROBE_MAX_RESPONSE_CHARS - 1)},
            {"type": "text_delta", "text": "z"},
            {"type": "done"},
        ]
    )
    _patch_factory(monkeypatch, provider)

    result = await ProfileProbe()(_PROFILE)

    assert len(result) == PROBE_MAX_RESPONSE_CHARS
    assert provider.closed


async def test_the_refusal_is_one_the_wizard_may_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard prints the exception. A limit refusal has to be a
    written, evidence-free sentence that tells the operator what to do —
    and it must not name the exception type or quote the answer."""
    provider = ScriptedProvider([{"type": "text_delta", "text": "z" * (4_096 * 2)}])
    _patch_factory(monkeypatch, provider)

    with pytest.raises(OperatorSafeProviderError) as raised:
        await ProfileProbe()(_PROFILE)

    message = raised.value.operator_message()
    assert message == str(raised.value)
    assert "z" * 64 not in message
    assert "Retry" in message


async def test_an_answer_that_fits_is_returned_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is a ceiling, not a truncation every probe pays."""
    provider = ScriptedProvider([{"type": "text_delta", "text": "ok"}, {"type": "done"}])
    _patch_factory(monkeypatch, provider)

    assert await ProfileProbe()(_PROFILE) == "ok"


async def test_the_probe_reads_nothing_after_the_bound_is_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing the answer is not enough on its own: the stream has to
    stop at the refusal, or the provider still bills for everything it
    goes on to send."""
    sent = 0

    class _Endless(ScriptedProvider):
        def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            self.calls.append((messages, tools))

            async def gen() -> AsyncIterator[dict[str, Any]]:
                nonlocal sent
                while True:
                    sent += 1
                    yield {"type": "text_delta", "text": "z" * 1_024}

            return gen()

    provider = _Endless([])
    _patch_factory(monkeypatch, provider)

    with pytest.raises(ProviderStreamLimitError):
        await ProfileProbe()(_PROFILE)

    # Exactly one fragment past the bound is read, and not one more: the
    # refusal happens before the accumulator can grow.
    assert sent == (PROBE_MAX_RESPONSE_CHARS // 1_024) + 1
    assert provider.closed
