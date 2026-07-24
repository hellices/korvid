"""Config-driven LLM provider registry."""

from __future__ import annotations

import logging
import os

from korvid.agent.provider import LLMProvider
from korvid.core.config import KorvidConfig
from korvid.providers.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)

_OPENAI_COMPAT_ALIASES = frozenset({"openai-compat", "openai", "ollama", "azure", "vllm"})


def create_provider(config: KorvidConfig) -> LLMProvider | None:
    """Build an LLM provider from config, or None when unconfigured/misconfigured."""
    if not config.agent_enabled:
        return None
    name = (config.agent_provider or "").lower()
    if name not in _OPENAI_COMPAT_ALIASES:
        logger.warning("unknown agent provider %r — agent disabled", config.agent_provider)
        return None
    if not config.agent_base_url or not config.agent_model:
        logger.warning("agent provider %r missing base_url/model — agent disabled", name)
        return None
    api_key = os.environ.get(config.agent_api_key_env) if config.agent_api_key_env else None
    return OpenAICompatProvider(
        base_url=config.agent_base_url,
        model=config.agent_model,
        api_key=api_key,
    )
