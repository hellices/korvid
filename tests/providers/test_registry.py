from pathlib import Path
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


def test_create_provider_threads_ca_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """network.ca_bundle reaches both provider adapters (issue #168)."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "korvid test CA")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    ca = tmp_path / "ca.pem"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    for provider_name in ("openai", "ollama"):
        provider = create_provider(
            enabled=True,
            provider=provider_name,
            auth_method="api_key" if provider_name == "openai" else "none",
            base_url="https://llm.corp/v1",
            model="m",
            api_key_env="KORVID_TEST_KEY" if provider_name == "openai" else None,
            ca_bundle=str(ca),
        )
        assert provider is not None
        assert isinstance(provider, OllamaProvider | OpenAICompatProvider)
        assert provider._ca_bundle == str(ca)  # reaches the adapter's client build
