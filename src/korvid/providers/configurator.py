"""Concrete AgentConfigurator wired to real providers and token storage."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

import httpx

from korvid.agent.setup import AgentConfigurator, AgentSettings, DeviceLoginPrompt
from korvid.providers.github_copilot import (
    COPILOT_CHAT_BASE_URL,
    CopilotCredentialSource,
    DeviceCodePrompt,
    GitHubDeviceFlow,
)
from korvid.providers.registry import create_provider
from korvid.providers.token_store import TokenStore

logger = logging.getLogger(__name__)

_PROBE_MESSAGE = {"role": "user", "content": "Reply with the single word: ok"}


def _default_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15.0)


class ProviderConfigurator(AgentConfigurator):
    """Implements korvid.agent.setup.AgentConfigurator (injected at composition root)."""

    def __init__(
        self,
        token_store: TokenStore,
        persist: Callable[[AgentSettings], None],
        flow_factory: Callable[[], GitHubDeviceFlow] = GitHubDeviceFlow,
        http_client_factory: Callable[[], httpx.AsyncClient] = _default_http_client,
    ) -> None:
        self._store = token_store
        self._persist = persist
        self._flow_factory = flow_factory
        self._http_client_factory = http_client_factory
        self._flow: GitHubDeviceFlow | None = None
        self._prompt: DeviceCodePrompt | None = None

    async def begin_device_login(self) -> DeviceLoginPrompt:
        flow = self._flow_factory()
        try:
            prompt = await flow.start()
        except BaseException:
            # Never leak the flow's HTTP client when the device-code request
            # fails or the worker is cancelled before a prompt is obtained.
            await flow.aclose()
            raise
        self._flow = flow
        self._prompt = prompt
        return DeviceLoginPrompt(prompt.user_code, prompt.verification_uri)

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

    async def list_models(self, settings: AgentSettings) -> list[str]:
        """Fetch selectable model ids; [] on any failure so the wizard falls back to input."""
        try:
            if settings.provider == "github-copilot":
                return await self._list_copilot_models()
            return await self._list_openai_compat_models(settings)
        except Exception:  # model listing is best-effort — never break the wizard
            logger.debug("model listing failed", exc_info=True)
            return []

    async def _list_copilot_models(self) -> list[str]:
        oauth = self._store.load("github-oauth")
        if not oauth:
            return []
        creds = CopilotCredentialSource(oauth, client=self._http_client_factory())
        try:
            headers = await creds.headers()
            async with self._http_client_factory() as client:
                resp = await client.get(f"{COPILOT_CHAT_BASE_URL}/models", headers=headers)
        finally:
            await creds.aclose()
        if resp.status_code != 200:
            return []
        data = resp.json().get("data", [])
        ids = {
            str(m["id"])
            for m in data
            if isinstance(m, dict) and "id" in m and m.get("capabilities", {}).get("type") == "chat"
        }
        return sorted(ids)

    async def _list_openai_compat_models(self, settings: AgentSettings) -> list[str]:
        if not settings.base_url:
            return []
        headers: dict[str, str] = {}
        if settings.auth_method == "api_key" and settings.api_key_env:
            api_key = os.environ.get(settings.api_key_env)
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        async with self._http_client_factory() as client:
            resp = await client.get(f"{settings.base_url.rstrip('/')}/models", headers=headers)
        if resp.status_code != 200:
            return []
        data = resp.json().get("data", [])
        return sorted({str(m["id"]) for m in data if isinstance(m, dict) and "id" in m})

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
