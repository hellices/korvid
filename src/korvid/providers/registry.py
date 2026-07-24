"""LLM provider registry — builds providers from provider-neutral values.

Config interpretation happens at the composition root (__main__.py); this
module never imports korvid.core so providers/ stays decoupled from
application configuration (AGENTS.md layer rules).
"""

from __future__ import annotations

import logging
import os

from korvid.agent.provider import LLMProvider
from korvid.providers.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)

_OPENAI_COMPAT_ALIASES = frozenset(
    {
        "openai-compat",
        "openai",
        "ollama",
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
    base_url: str | None,
    model: str | None,
    api_key_env: str | None,
) -> LLMProvider | None:
    """Build an LLM provider from neutral values, or None when unconfigured/misconfigured."""
    if not enabled:
        return None
    # YAML can hand us non-string scalars (e.g. `provider: true`); only
    # strings are meaningful — anything else falls to the unknown branch.
    name = provider.lower() if isinstance(provider, str) else ""
    if name not in _OPENAI_COMPAT_ALIASES:
        logger.warning("unknown agent provider %r — agent disabled", provider)
        return None
    if not base_url or not model:
        logger.warning("agent provider %r missing base_url/model — agent disabled", name)
        return None
    api_key = os.environ.get(api_key_env) if api_key_env else None
    return OpenAICompatProvider(
        base_url=base_url,
        model=model,
        api_key=api_key,
        # Azure OpenAI authenticates with a raw key in the "api-key" header
        # instead of a Bearer Authorization header.
        auth_header="api-key" if name == "azure" else "Authorization",
    )
