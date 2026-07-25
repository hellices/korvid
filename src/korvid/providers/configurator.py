"""Concrete AgentConfigurator wired to real providers and token storage."""

from __future__ import annotations

from collections.abc import Callable

from korvid.agent.setup import AgentSettings, DeviceLoginPrompt
from korvid.providers.github_copilot import DeviceCodePrompt, GitHubDeviceFlow
from korvid.providers.registry import create_provider
from korvid.providers.token_store import TokenStore

_PROBE_MESSAGE = {"role": "user", "content": "Reply with the single word: ok"}


class ProviderConfigurator:
    """Implements korvid.agent.setup.AgentConfigurator (injected at composition root)."""

    def __init__(
        self,
        token_store: TokenStore,
        persist: Callable[[AgentSettings], None],
        flow_factory: Callable[[], GitHubDeviceFlow] = GitHubDeviceFlow,
    ) -> None:
        self._store = token_store
        self._persist = persist
        self._flow_factory = flow_factory
        self._flow: GitHubDeviceFlow | None = None
        self._prompt: DeviceCodePrompt | None = None

    async def begin_device_login(self) -> DeviceLoginPrompt:
        self._flow = self._flow_factory()
        self._prompt = await self._flow.start()
        return DeviceLoginPrompt(self._prompt.user_code, self._prompt.verification_uri)

    async def finish_device_login(self) -> None:
        if self._flow is None or self._prompt is None:
            raise RuntimeError("begin_device_login must be called first")
        try:
            token = await self._flow.poll(self._prompt)
        finally:
            await self._flow.aclose()
            self._flow = None
            self._prompt = None
        self._store.save("github-oauth", token)

    async def test(self, settings: AgentSettings) -> str:
        oauth = self._store.load("github-oauth")
        provider = create_provider(
            enabled=True,
            provider=settings.provider,
            auth_method=settings.auth_method,
            base_url=settings.base_url,
            model=settings.model,
            api_key_env=settings.api_key_env,
            oauth_token=oauth,
        )
        if provider is None:
            raise RuntimeError("configuration incomplete — provider could not be created")
        text = ""
        try:
            async for ev in provider.complete([_PROBE_MESSAGE], []):
                if ev.get("type") == "text_delta":
                    text += str(ev.get("text", ""))
        finally:
            await provider.aclose()
        if not text.strip():
            raise RuntimeError("provider returned no text")
        return text.strip()

    async def save(self, settings: AgentSettings) -> None:
        self._persist(settings)
