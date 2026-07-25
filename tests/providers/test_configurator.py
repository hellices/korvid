from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from korvid.agent.setup import AgentSettings
from korvid.providers.configurator import ProviderConfigurator
from korvid.providers.github_copilot import DeviceCodePrompt, GitHubDeviceFlow
from korvid.providers.token_store import TokenStore

_SETTINGS = AgentSettings(
    provider="ollama",
    auth_method="none",
    base_url="http://localhost:11434/v1",
    model="llama3",
)


class FakeFlow:
    def __init__(self) -> None:
        self.closed = False

    async def start(self) -> DeviceCodePrompt:
        return DeviceCodePrompt("ABCD-1234", "https://github.com/login/device", "d", 5, 900)

    async def poll(self, prompt: DeviceCodePrompt) -> str:
        return "gho_tok"

    async def aclose(self) -> None:
        self.closed = True


class ScriptedProvider:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.closed = False

    @property
    def name(self) -> str:
        return "scripted"

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        async def gen() -> AsyncIterator[dict[str, Any]]:
            for ev in self._events:
                yield ev

        return gen()

    async def aclose(self) -> None:
        self.closed = True


def _store(tmp_path: Path) -> TokenStore:
    return TokenStore(fallback_path=tmp_path / "creds.json")


async def test_device_login_stores_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "keyring", None)
    store = _store(tmp_path)
    flow = FakeFlow()
    cfg = ProviderConfigurator(
        store,
        persist=lambda s: None,
        flow_factory=lambda: cast("GitHubDeviceFlow", flow),
    )
    prompt = await cfg.begin_device_login()
    assert prompt.user_code == "ABCD-1234"
    await cfg.finish_device_login()
    assert store.load("github-oauth") == "gho_tok"
    assert flow.closed


async def test_finish_without_begin_raises(tmp_path: Path) -> None:
    cfg = ProviderConfigurator(_store(tmp_path), persist=lambda s: None)
    with pytest.raises(RuntimeError):
        await cfg.finish_device_login()


async def test_probe_returns_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ScriptedProvider([{"type": "text_delta", "text": "ok"}, {"type": "done"}])
    monkeypatch.setattr("korvid.providers.configurator.create_provider", lambda **kw: provider)
    cfg = ProviderConfigurator(_store(tmp_path), persist=lambda s: None)
    assert await cfg.test(_SETTINGS) == "ok"
    assert provider.closed  # aclose'd even on success


async def test_probe_raises_when_provider_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("korvid.providers.configurator.create_provider", lambda **kw: None)
    cfg = ProviderConfigurator(_store(tmp_path), persist=lambda s: None)
    with pytest.raises(RuntimeError):
        await cfg.test(_SETTINGS)


async def test_probe_raises_on_empty_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ScriptedProvider([{"type": "done"}])
    monkeypatch.setattr("korvid.providers.configurator.create_provider", lambda **kw: provider)
    cfg = ProviderConfigurator(_store(tmp_path), persist=lambda s: None)
    with pytest.raises(RuntimeError):
        await cfg.test(_SETTINGS)
    assert provider.closed


async def test_save_invokes_persist(tmp_path: Path) -> None:
    saved: list[AgentSettings] = []
    cfg = ProviderConfigurator(_store(tmp_path), persist=saved.append)
    await cfg.save(_SETTINGS)
    assert saved == [_SETTINGS]


async def test_begin_device_login_closes_flow_on_start_failure(tmp_path: Path) -> None:
    class FailingFlow(FakeFlow):
        async def start(self) -> DeviceCodePrompt:
            raise RuntimeError("network down")

    flow = FailingFlow()
    cfg = ProviderConfigurator(
        _store(tmp_path),
        persist=lambda s: None,
        flow_factory=lambda: cast("GitHubDeviceFlow", flow),
    )
    with pytest.raises(RuntimeError):
        await cfg.begin_device_login()
    assert flow.closed


def test_provider_configurator_implements_abc() -> None:
    from korvid.agent.setup import AgentConfigurator

    assert issubclass(ProviderConfigurator, AgentConfigurator)
    with pytest.raises(TypeError):
        AgentConfigurator()  # type: ignore[abstract]
