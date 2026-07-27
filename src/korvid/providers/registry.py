"""LLM provider registry — builds providers from provider-neutral values.

Config interpretation happens at the composition root (__main__.py); this
module never imports korvid.core so providers/ stays decoupled from
application configuration (AGENTS.md layer rules).
"""

from __future__ import annotations

import logging
import os

from korvid.agent.credentials import CredentialSource
from korvid.agent.provider import LLMProvider
from korvid.providers.entra import EntraCredentialSource
from korvid.providers.github_copilot import COPILOT_CHAT_BASE_URL, CopilotCredentialSource
from korvid.providers.ollama import OllamaOptions, OllamaProvider
from korvid.providers.openai_compat import OpenAICompatProvider
from korvid.providers.static_creds import StaticHeaderSource

logger = logging.getLogger(__name__)

_OPENAI_COMPAT_ALIASES = frozenset(
    {
        "openai-compat",
        "openai",
        "azure",
        "vllm",
        "github",  # GitHub Models (models.github.ai) — OpenAI-compatible
        "anthropic",  # Anthropic's OpenAI SDK compatibility endpoint
        "claude",
    }
)


def create_provider(
    *,
    enabled: bool,
    provider: str | None,
    auth_method: str | None,
    base_url: str | None,
    model: str | None,
    api_key_env: str | None,
    oauth_token: str | None = None,
    ollama: OllamaOptions | None = None,
) -> LLMProvider | None:
    """Build an LLM provider from neutral values, or None when unconfigured/misconfigured."""
    if not enabled:
        return None
    # YAML can hand us non-string scalars (e.g. `provider: true`); only
    # strings are meaningful — anything else falls to the unknown branch.
    name = provider.lower() if isinstance(provider, str) else ""
    if name == "github-copilot":
        return _create_github_copilot(
            auth_method=auth_method, base_url=base_url, model=model, oauth_token=oauth_token
        )
    if name not in _OPENAI_COMPAT_ALIASES and name != "ollama":
        logger.warning("unknown agent provider %r — agent disabled", provider)
        return None
    if not base_url or not model:
        logger.warning("agent provider %r missing base_url/model — agent disabled", name)
        return None
    try:
        credentials = build_credentials(name, auth_method, api_key_env)
    except _AuthMisconfigured as exc:
        logger.warning("%s — agent disabled", exc)
        return None
    if name == "ollama":
        # Native /api/chat adapter (issue #72). The OpenAI-compat shim path
        # stays available via `provider: openai-compat` with an Ollama URL.
        return OllamaProvider(
            base_url=base_url,
            model=model,
            credentials=credentials,
            options=ollama or OllamaOptions(),
        )
    return OpenAICompatProvider(
        base_url=base_url,
        model=model,
        credentials=credentials,
    )


def _create_github_copilot(
    *,
    auth_method: str | None,
    base_url: str | None,
    model: str | None,
    oauth_token: str | None,
) -> LLMProvider | None:
    # Copilot only supports device-login; a stored OAuth token must not be
    # consumed under a mistyped or explicitly different auth method.
    if auth_method not in (None, "device-login"):
        logger.warning(
            "github-copilot requires auth method 'device-login', got %r — agent disabled",
            auth_method,
        )
        return None
    if not model:
        logger.warning("github-copilot missing model — agent disabled")
        return None
    if not oauth_token:
        logger.warning("github-copilot: not logged in — run :ai in the TUI")
        return None
    return OpenAICompatProvider(
        base_url=base_url or COPILOT_CHAT_BASE_URL,
        model=model,
        credentials=CopilotCredentialSource(oauth_token),
    )


class _AuthMisconfigured(Exception):
    """Auth settings are present but unusable — the agent must be disabled."""


def build_credentials(
    name: str, auth_method: str | None, api_key_env: str | None
) -> CredentialSource | None:
    """Credential source for a provider, or None when explicitly unauthenticated.

    Raises when auth settings are present but unusable (unknown method,
    missing API key env var) — callers decide whether that disables the
    agent or merely skips an optional request.
    """
    method = auth_method or ("api_key" if api_key_env else "none")
    if method == "entra":
        return EntraCredentialSource()
    if method == "api_key":
        api_key = os.environ.get(api_key_env) if api_key_env else None
        if not api_key:
            raise _AuthMisconfigured(
                f"auth method 'api_key' but {api_key_env or 'api_key_env'} is not set"
            )
        # Azure OpenAI authenticates with a raw key in the "api-key" header
        # instead of a bearer Authorization header.
        if name == "azure":
            return StaticHeaderSource(api_key, header="api-key", prefix="")
        return StaticHeaderSource(api_key)
    if method == "none":
        return None  # explicitly unauthenticated (e.g. local ollama)
    raise _AuthMisconfigured(f"unknown agent auth method {method!r}")
