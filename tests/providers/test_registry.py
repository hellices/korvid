from typing import cast

import pytest

from korvid.providers.openai_compat import OpenAICompatProvider
from korvid.providers.registry import create_provider


def test_none_when_agent_disabled() -> None:
    assert (
        create_provider(enabled=False, provider=None, base_url=None, model=None, api_key_env=None)
        is None
    )


def test_openai_compat_created(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K", "sk-1")
    p = create_provider(
        enabled=True,
        provider="openai-compat",
        base_url="http://x/v1",
        model="m",
        api_key_env="K",
    )
    assert isinstance(p, OpenAICompatProvider)


def test_aliases_accepted() -> None:
    for alias in ("openai", "ollama", "azure", "vllm", "github", "anthropic", "claude"):
        p = create_provider(
            enabled=True,
            provider=alias,
            base_url="http://x/v1",
            model="m",
            api_key_env=None,
        )
        assert isinstance(p, OpenAICompatProvider)


def test_none_when_model_missing() -> None:
    assert (
        create_provider(
            enabled=True,
            provider="openai-compat",
            base_url="http://x/v1",
            model=None,
            api_key_env=None,
        )
        is None
    )


def test_none_when_provider_unknown() -> None:
    assert (
        create_provider(
            enabled=True,
            provider="mystery",
            base_url="http://x/v1",
            model="m",
            api_key_env=None,
        )
        is None
    )


def test_none_when_provider_not_a_string() -> None:
    """YAML `provider: true` must disable the agent, not crash on .lower()."""
    assert (
        create_provider(
            enabled=True,
            provider=cast("str", True),
            base_url="http://x/v1",
            model="m",
            api_key_env=None,
        )
        is None
    )
