from typing import cast

import pytest

from korvid.providers.openai_compat import OpenAICompatProvider
from korvid.providers.registry import create_provider


def test_none_when_agent_disabled() -> None:
    assert (
        create_provider(
            enabled=False,
            provider=None,
            auth_method=None,
            base_url=None,
            model=None,
            api_key_env=None,
        )
        is None
    )


def test_openai_compat_created(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K", "sk-1")
    p = create_provider(
        enabled=True,
        provider="openai-compat",
        auth_method=None,
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
            auth_method=None,
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
            auth_method=None,
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
            auth_method=None,
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
            auth_method=None,
            base_url="http://x/v1",
            model="m",
            api_key_env=None,
        )
        is None
    )


def test_github_copilot_requires_oauth_token() -> None:
    assert (
        create_provider(
            enabled=True,
            provider="github-copilot",
            auth_method="device-login",
            base_url=None,
            model="gpt-4o",
            api_key_env=None,
            oauth_token=None,
        )
        is None
    )


def test_github_copilot_defaults_base_url() -> None:
    p = create_provider(
        enabled=True,
        provider="github-copilot",
        auth_method="device-login",
        base_url=None,
        model="gpt-4o",
        api_key_env=None,
        oauth_token="gho_x",
    )
    assert isinstance(p, OpenAICompatProvider)


def test_entra_auth_builds_provider() -> None:
    p = create_provider(
        enabled=True,
        provider="azure",
        auth_method="entra",
        base_url="https://foo.openai.azure.com/v1",
        model="gpt-4o",
        api_key_env=None,
        oauth_token=None,
    )
    assert isinstance(p, OpenAICompatProvider)


def test_api_key_method_with_missing_env_disables_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_KEY_ENV", raising=False)
    assert (
        create_provider(
            enabled=True,
            provider="openai-compat",
            auth_method="api_key",
            base_url="http://x/v1",
            model="m",
            api_key_env="MISSING_KEY_ENV",
        )
        is None
    )


def test_unknown_auth_method_disables_agent() -> None:
    assert (
        create_provider(
            enabled=True,
            provider="openai-compat",
            auth_method="mystery-auth",
            base_url="http://x/v1",
            model="m",
            api_key_env=None,
        )
        is None
    )


def test_github_copilot_rejects_non_device_login_method() -> None:
    assert (
        create_provider(
            enabled=True,
            provider="github-copilot",
            auth_method="none",
            base_url=None,
            model="gpt-4o",
            api_key_env=None,
            oauth_token="gho_tok",
        )
        is None
    )


def test_github_copilot_default_method_is_device_login() -> None:
    provider = create_provider(
        enabled=True,
        provider="github-copilot",
        auth_method=None,
        base_url=None,
        model="gpt-4o",
        api_key_env=None,
        oauth_token="gho_tok",
    )
    assert provider is not None
