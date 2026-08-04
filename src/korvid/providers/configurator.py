"""Concrete AgentConfigurator wired to real providers and token storage."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx

from korvid.agent.setup import AgentConfigurator, AgentSettings, DeviceLoginPrompt
from korvid.providers.github_copilot import (
    COPILOT_CHAT_BASE_URL,
    CopilotCredentialSource,
    DeviceCodePrompt,
    GitHubDeviceFlow,
)
from korvid.providers.net import make_client
from korvid.providers.ollama import normalize_base_url
from korvid.providers.registry import build_credentials, create_provider
from korvid.providers.token_store import TokenStore

if TYPE_CHECKING:
    from korvid.providers.plugin_registry import ProviderPluginRegistry

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
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        ca_bundle: str | None = None,
        plugin_registry: ProviderPluginRegistry | None = None,
    ) -> None:
        self._store = token_store
        self._persist = persist
        self._flow_factory = flow_factory
        # Test seam: an injected factory serves every listing path. Without
        # one, endpoint calls are CA-aware and public GitHub calls keep
        # default trust (see _endpoint_client/_public_client).
        self._http_client_factory = http_client_factory
        # network.ca_bundle (issue #168): the probe provider must be built
        # with the same trust as the live agent — the wizard's test and the
        # runtime can never disagree about the CA.
        self._ca_bundle = ca_bundle
        self._plugin_registry = plugin_registry
        self._flow: GitHubDeviceFlow | None = None
        self._prompt: DeviceCodePrompt | None = None

    def _endpoint_client(self) -> httpx.AsyncClient:
        """Client for the user's OpenAI-compatible/Ollama endpoint: shares
        the live providers' `network.ca_bundle` trust (issue #168)."""
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return make_client(self._ca_bundle, timeout=15.0)

    def _public_client(self) -> httpx.AsyncClient:
        """Client for GitHub Copilot discovery: public endpoints on default
        trust, matching the live github-copilot provider, which ignores
        `ca_bundle` — a private-only bundle must not break GitHub calls."""
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return _default_http_client()

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
            if settings.provider == "ollama":
                return await self._list_ollama_models(settings)
            return await self._list_openai_compat_models(settings)
        except Exception:  # model listing is best-effort — never break the wizard
            logger.debug("model listing failed", exc_info=True)
            return []

    async def _list_copilot_models(self) -> list[str]:
        oauth = self._store.load("github-oauth")
        if not oauth:
            return []
        # One client serves both round-trips (token exchange + /models GET)
        # so the connection pool is shared; the credential source closes it.
        client = self._public_client()
        creds = CopilotCredentialSource(oauth, client=client)
        try:
            headers = await creds.headers()
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
        # Reuse the registry's credential dispatch so azure gets its raw
        # `api-key` header and entra gets a real bearer token — a plain
        # Bearer-from-env guess would 401 on valid azure configurations.
        creds = build_credentials(settings.provider, settings.auth_method, settings.api_key_env)
        try:
            headers = await creds.headers() if creds is not None else {}
            async with self._endpoint_client() as client:
                resp = await client.get(f"{settings.base_url.rstrip('/')}/models", headers=headers)
        finally:
            if creds is not None:
                await creds.aclose()
        if resp.status_code != 200:
            return []
        data = resp.json().get("data", [])
        return sorted({str(m["id"]) for m in data if isinstance(m, dict) and "id" in m})

    async def _list_ollama_models(self, settings: AgentSettings) -> list[str]:
        """Native model listing via /api/tags (the native API has no /models)."""
        if not settings.base_url:
            return []
        creds = build_credentials(settings.provider, settings.auth_method, settings.api_key_env)
        try:
            headers = await creds.headers() if creds is not None else {}
            async with self._endpoint_client() as client:
                resp = await client.get(
                    f"{normalize_base_url(settings.base_url)}/api/tags", headers=headers
                )
        finally:
            if creds is not None:
                await creds.aclose()
        if resp.status_code != 200:
            return []
        models = resp.json().get("models", [])
        return sorted({str(m["name"]) for m in models if isinstance(m, dict) and "name" in m})

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
            ca_bundle=self._ca_bundle,
            plugin_registry=self._plugin_registry,
            options=settings.options,
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
