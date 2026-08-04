"""LLM provider registry — builds providers from provider-neutral values.

Config interpretation happens at the composition root (__main__.py); this
module never imports korvid.core so providers/ stays decoupled from
application configuration (AGENTS.md layer rules).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from korvid.agent.credentials import CredentialSource
from korvid.agent.provider import LLMProvider
from korvid.providers.entra import EntraCredentialSource
from korvid.providers.github_copilot import COPILOT_CHAT_BASE_URL, CopilotCredentialSource
from korvid.providers.ollama import OllamaOptions, OllamaProvider
from korvid.providers.openai_compat import OpenAICompatProvider
from korvid.providers.plugin_registry import normalize_provider_name
from korvid.providers.static_creds import StaticHeaderSource

if TYPE_CHECKING:
    from korvid.providers.plugin_registry import ProviderPluginRegistry

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
    ca_bundle: str | None = None,
    plugin_registry: ProviderPluginRegistry | None = None,
    options: Mapping[str, object] | None = None,
    options_error: str | None = None,
) -> LLMProvider | None:
    """Build an LLM provider from neutral values, or None when unconfigured/misconfigured."""
    if not enabled:
        return None
    # Normalize once: lowercase, collapse [-_.] separators to hyphens, strip.
    # This ensures `openai_compat`, `OpenAI_Compat`, ` ollama` etc. route to built-ins.
    name = normalize_provider_name(provider) if isinstance(provider, str) else ""
    if name == "github-copilot":
        return _create_github_copilot(
            auth_method=auth_method, base_url=base_url, model=model, oauth_token=oauth_token
        )
    if name not in _OPENAI_COMPAT_ALIASES and name != "ollama":
        # Unknown to built-ins: try the plugin registry before giving up.
        return _create_via_plugin(
            name=name,
            provider_label=provider,
            auth_method=auth_method,
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
            plugin_registry=plugin_registry,
            options=options,
            options_error=options_error,
        )
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
            ca_bundle=ca_bundle,
        )
    return OpenAICompatProvider(
        base_url=base_url,
        model=model,
        credentials=credentials,
        ca_bundle=ca_bundle,
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


def _create_via_plugin(
    *,
    name: str,
    provider_label: str | None,
    auth_method: str | None,
    base_url: str | None,
    model: str | None,
    api_key_env: str | None,
    plugin_registry: ProviderPluginRegistry | None,
    options: Mapping[str, object] | None,
    options_error: str | None = None,
) -> LLMProvider | None:
    """Route an unknown provider name through the plugin registry.

    Returns None when there is no registry. Raises ``ProviderPluginError``
    on any plugin failure — the caller decides whether to convert the error
    to a warning (initial startup) or let it propagate (rebuild).
    """
    if plugin_registry is None:
        logger.warning("unknown agent provider %r — agent disabled", provider_label)
        return None

    from korvid.providers.plugin_registry import ProviderPluginError

    # Gate third-party plugin creation when options are invalid: the plugin
    # must not receive broken configuration silently.
    if options_error is not None:
        raise ProviderPluginError(f"agent.options validation failed: {options_error}")

    from korvid.agent.provider_plugin import ProviderPluginConfig

    # load_selected may raise ProviderPluginError — let it propagate.
    plugin_registry.load_selected(name)

    # Build credentials first (validates auth config) then close on failure.
    credentials: CredentialSource | None = None
    try:
        credentials = build_credentials(name, auth_method, api_key_env)
    except _AuthMisconfigured:
        from korvid.providers.plugin_registry import ProviderPluginError as _PPE

        raise _PPE(f"provider plugin {name!r}: auth misconfigured") from None

    config = ProviderPluginConfig(
        base_url=base_url,
        model=model,
        auth_method=auth_method,
        api_key_env=api_key_env,
        options=options or {},
    )

    try:
        return plugin_registry.create(name, config, credentials)
    except Exception:
        _close_credentials(credentials)
        raise


# Strong references for credential-close tasks — mirrors _close_provider_in_background
# in __main__.py so fire-and-forget aclose() tasks aren't garbage-collected.
_cred_close_tasks: set[asyncio.Task[None]] = set()


def _close_credentials(credentials: CredentialSource | None) -> None:
    """Best-effort async close for credentials when plugin construction fails.

    Uses a strong-reference set with a done-callback that discards the task
    and consumes any exception at debug level, matching the
    ``_close_provider_in_background`` pattern in ``__main__``.
    """
    if credentials is None:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    task = loop.create_task(credentials.aclose())
    _cred_close_tasks.add(task)

    def _reap(t: asyncio.Task[None]) -> None:
        _cred_close_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            # Consume the exception with a fixed message only — never log
            # exc_info or the exception message (may contain secrets/tokens
            # from a third-party credential source).
            logger.debug("credential close failed")

    task.add_done_callback(_reap)


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
