"""The wizard's connection probe, on the profile factory.

These tests carry over `tests/providers/test_configurator.py`'s probe
contract — a prepared copy of the message, an `aclose` on every path, a
refusal when nothing could be built, and a refusal when the provider
streams nothing — onto `ProfileProbe`, which builds its provider with the
same `create_provider_from_profile` the running agent uses.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from korvid.agent.model_policy import ModelDescriptor
from korvid.agent.model_profiles import ConnectionAuthConfig, ModelConnectionConfig
from korvid.agent.outbound import OutboundPolicy, OutboundPolicyError
from korvid.providers.profile_probe import PROBE_MESSAGE, ProfileProbe

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
            raise OutboundPolicyError("injected policy failure")

    provider = ScriptedProvider([{"type": "text_delta", "text": "unexpected"}])
    _patch_factory(monkeypatch, provider)
    probe = ProfileProbe()
    probe._outbound = BlockingPolicy(max_request_chars=4_096)

    with pytest.raises(OutboundPolicyError, match="injected policy failure"):
        await probe(_PROFILE)

    assert provider.calls == []
    assert provider.closed


async def test_probe_raises_when_the_factory_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_provider_from_profile` logs its reason and returns None.

    The probe must turn that into an error the wizard can render, not a
    blank success.
    """
    _patch_factory(monkeypatch, None)

    with pytest.raises(RuntimeError, match="configuration incomplete"):
        await ProfileProbe()(_PROFILE)


async def test_probe_raises_on_empty_text(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ScriptedProvider([{"type": "done"}])
    _patch_factory(monkeypatch, provider)

    with pytest.raises(RuntimeError, match="provider returned no text"):
        await ProfileProbe()(_PROFILE)
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

    probe = ProfileProbe(
        catalog=catalog,  # type: ignore[arg-type]  # a stub is enough: the probe only forwards it
        flows=flows,  # type: ignore[arg-type]  # ditto
        credentials=credentials,  # type: ignore[arg-type]  # ditto
        ca_bundle="/etc/ssl/corp.pem",
    )
    await probe(_PROFILE)

    assert seen == [
        {
            "profile": _PROFILE,
            "catalog": catalog,
            "flows": flows,
            "credentials": credentials,
            "ca_bundle": "/etc/ssl/corp.pem",
        }
    ]
