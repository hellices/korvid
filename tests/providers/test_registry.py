from typing import cast

import pytest

from korvid.providers.ollama import OllamaOptions, OllamaProvider
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
    for alias in ("openai", "azure", "vllm", "github", "anthropic", "claude"):
        p = create_provider(
            enabled=True,
            provider=alias,
            auth_method=None,
            base_url="http://x/v1",
            model="m",
            api_key_env=None,
        )
        assert isinstance(p, OpenAICompatProvider)


def test_ollama_routes_to_native_provider() -> None:
    p = create_provider(
        enabled=True,
        provider="ollama",
        auth_method=None,
        base_url="http://localhost:11434",
        model="qwen3:8b",
        api_key_env=None,
    )
    assert isinstance(p, OllamaProvider)
    assert p.name == "qwen3:8b"


def test_ollama_receives_options() -> None:
    options = OllamaOptions(num_ctx=8192, think=True)
    p = create_provider(
        enabled=True,
        provider="ollama",
        auth_method=None,
        base_url="http://localhost:11434",
        model="m",
        api_key_env=None,
        ollama=options,
    )
    assert isinstance(p, OllamaProvider)
    assert p._options == options


def test_ollama_none_when_base_url_missing() -> None:
    assert (
        create_provider(
            enabled=True,
            provider="ollama",
            auth_method=None,
            base_url=None,
            model="m",
            api_key_env=None,
        )
        is None
    )


def test_ollama_api_key_auth_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_KEY", "sk-1")
    p = create_provider(
        enabled=True,
        provider="ollama",
        auth_method="api_key",
        base_url="http://remote:11434",
        model="m",
        api_key_env="OLLAMA_KEY",
    )
    assert isinstance(p, OllamaProvider)


def test_ollama_bad_auth_disables_agent() -> None:
    assert (
        create_provider(
            enabled=True,
            provider="ollama",
            auth_method="api_key",
            base_url="http://x",
            model="m",
            api_key_env=None,
        )
        is None
    )


def test_openai_compat_with_ollama_base_url_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """No breaking change: the shim path stays available via openai-compat."""
    p = create_provider(
        enabled=True,
        provider="openai-compat",
        auth_method=None,
        base_url="http://localhost:11434/v1",
        model="llama3",
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
