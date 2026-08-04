from pathlib import Path
from typing import Any, cast

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


# ---------------------------------------------------------------------------
# Plugin integration — create_provider routes unknown names to the registry
# ---------------------------------------------------------------------------


def test_builtins_never_query_plugin_registry() -> None:
    """Built-in providers (openai-compat, ollama, github-copilot) must never
    touch the plugin_registry even when one is supplied."""
    from unittest.mock import MagicMock

    registry = MagicMock()
    p = create_provider(
        enabled=True,
        provider="openai-compat",
        auth_method=None,
        base_url="http://x/v1",
        model="m",
        api_key_env=None,
        plugin_registry=registry,
    )
    assert isinstance(p, OpenAICompatProvider)
    registry.load_selected.assert_not_called()
    registry.create.assert_not_called()


def test_unknown_name_routes_through_plugin_registry() -> None:
    """An unknown provider name with a registry invokes the plugin path."""
    from collections.abc import AsyncIterator
    from unittest.mock import MagicMock

    from korvid.agent.provider import LLMProvider
    from korvid.agent.provider_plugin import ProviderPluginMetadata

    class _FakeProvider(LLMProvider):
        @property
        def name(self) -> str:
            return "custom"

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "done"}

        async def aclose(self) -> None:
            pass

    meta = ProviderPluginMetadata(
        api_version=1, name="custom-llm", display_name="Custom", auth_methods=("none",)
    )
    fake_plugin = MagicMock()
    fake_plugin.metadata = meta
    registry = MagicMock()
    registry.load_selected.return_value = fake_plugin
    registry.create.return_value = _FakeProvider()

    p = create_provider(
        enabled=True,
        provider="custom-llm",
        auth_method="none",
        base_url="http://x/v1",
        model="m",
        api_key_env=None,
        plugin_registry=registry,
        options={"tenant": "test"},
    )
    assert p is not None
    registry.load_selected.assert_called_once_with("custom-llm")
    registry.create.assert_called_once()
    # Verify config passed includes options
    call_args = registry.create.call_args
    config = call_args[0][1]  # positional: name, config, credentials
    assert config.options == {"tenant": "test"}


def test_plugin_receives_credentials_and_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plugin factory must receive only credentials/config/options — no kube/UI/executor/audit."""
    from collections.abc import AsyncIterator
    from unittest.mock import MagicMock

    from korvid.agent.provider import LLMProvider
    from korvid.agent.provider_plugin import ProviderPluginConfig, ProviderPluginMetadata

    class _FakeProvider(LLMProvider):
        @property
        def name(self) -> str:
            return "c"

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "done"}

        async def aclose(self) -> None:
            pass

    captured_config: list[ProviderPluginConfig] = []

    def fake_create(name: str, config: ProviderPluginConfig, creds: Any) -> LLMProvider:
        captured_config.append(config)
        return _FakeProvider()

    meta = ProviderPluginMetadata(
        api_version=1, name="corp-llm", display_name="Corp", auth_methods=("api_key",)
    )
    registry = MagicMock()
    registry.load_selected.return_value = MagicMock(metadata=meta)
    registry.create = fake_create

    monkeypatch.setenv("CORP_KEY", "sk-1")
    p = create_provider(
        enabled=True,
        provider="corp-llm",
        auth_method="api_key",
        base_url="https://x/v1",
        model="m",
        api_key_env="CORP_KEY",
        plugin_registry=registry,
        options={"region": "us"},
    )
    assert p is not None
    assert len(captured_config) == 1
    cfg = captured_config[0]
    assert cfg.base_url == "https://x/v1"
    assert cfg.model == "m"
    assert cfg.auth_method == "api_key"
    assert cfg.options == {"region": "us"}


async def test_plugin_credentials_closed_on_construction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If plugin construction fails, credentials built for it must be closed
    via a strong-referenced task that consumes close errors."""
    from unittest.mock import MagicMock

    from korvid.agent.provider_plugin import ProviderPluginMetadata
    from korvid.providers.plugin_registry import ProviderPluginError

    meta = ProviderPluginMetadata(
        api_version=1, name="fail-llm", display_name="Fail", auth_methods=("api_key",)
    )
    registry = MagicMock()
    registry.load_selected.return_value = MagicMock(metadata=meta)
    registry.create.side_effect = ProviderPluginError("boom")

    closed: list[bool] = []

    class FakeCred:
        async def headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer x"}

        async def aclose(self) -> None:
            closed.append(True)

    def patched_build(name: str, auth_method: str | None, api_key_env: str | None) -> FakeCred:
        return FakeCred()

    monkeypatch.setattr("korvid.providers.registry.build_credentials", patched_build)

    import asyncio

    with pytest.raises(ProviderPluginError, match="boom"):
        create_provider(
            enabled=True,
            provider="fail-llm",
            auth_method="api_key",
            base_url="http://x/v1",
            model="m",
            api_key_env="K",
            plugin_registry=registry,
        )
    # Credentials aclose is scheduled as a background task with strong ref
    for _ in range(5):
        await asyncio.sleep(0)
    assert closed == [True]


async def test_credential_close_consumes_exceptions_without_secret_leak(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If credential.aclose() raises, the error is consumed/logged
    without leaking secret payload in logs."""
    from unittest.mock import MagicMock

    from korvid.agent.provider_plugin import ProviderPluginMetadata
    from korvid.providers.plugin_registry import ProviderPluginError

    meta = ProviderPluginMetadata(
        api_version=1, name="leak-llm", display_name="Leak", auth_methods=("api_key",)
    )
    registry = MagicMock()
    registry.load_selected.return_value = MagicMock(metadata=meta)
    registry.create.side_effect = ProviderPluginError("factory error")

    close_called: list[bool] = []

    class ExplodingCred:
        async def headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer x"}

        async def aclose(self) -> None:
            close_called.append(True)
            raise RuntimeError("SUPER_SECRET_TOKEN_xyz123 leaked in close")

    def patched_build(name: str, auth_method: str | None, api_key_env: str | None) -> ExplodingCred:
        return ExplodingCred()

    monkeypatch.setattr("korvid.providers.registry.build_credentials", patched_build)

    import asyncio

    with pytest.raises(ProviderPluginError, match="factory error"):
        create_provider(
            enabled=True,
            provider="leak-llm",
            auth_method="api_key",
            base_url="http://x/v1",
            model="m",
            api_key_env="K",
            plugin_registry=registry,
        )
    # Let the close task run and fail
    for _ in range(10):
        await asyncio.sleep(0)
    assert close_called == [True]
    # No unhandled-task-exception: the done callback consumed it.
    # Caplog must NOT contain the secret payload or unbounded traceback.
    full_log = caplog.text
    assert "SUPER_SECRET_TOKEN" not in full_log


def test_unknown_without_registry_returns_none() -> None:
    """Without a plugin_registry, unknown names still return None (backward compat)."""
    assert (
        create_provider(
            enabled=True,
            provider="mystery",
            auth_method=None,
            base_url="http://x/v1",
            model="m",
            api_key_env=None,
            plugin_registry=None,
        )
        is None
    )


def test_invalid_options_disable_only_the_plugin() -> None:
    """A ProviderPluginError from the registry propagates — callers decide policy."""
    from unittest.mock import MagicMock

    from korvid.providers.plugin_registry import ProviderPluginError

    registry = MagicMock()
    registry.load_selected.side_effect = ProviderPluginError("bad plugin")

    with pytest.raises(ProviderPluginError, match="bad plugin"):
        create_provider(
            enabled=True,
            provider="bad-plugin",
            auth_method=None,
            base_url="http://x/v1",
            model="m",
            api_key_env=None,
            plugin_registry=registry,
        )


def test_plugin_create_failure_propagates() -> None:
    """ProviderPluginError from registry.create() must propagate, not be swallowed."""
    from unittest.mock import MagicMock

    from korvid.agent.provider_plugin import ProviderPluginMetadata
    from korvid.providers.plugin_registry import ProviderPluginError

    meta = ProviderPluginMetadata(
        api_version=1, name="fail-llm", display_name="Fail", auth_methods=("none",)
    )
    registry = MagicMock()
    registry.load_selected.return_value = MagicMock(metadata=meta)
    registry.create.side_effect = ProviderPluginError("factory boom")

    with pytest.raises(ProviderPluginError, match="factory boom"):
        create_provider(
            enabled=True,
            provider="fail-llm",
            auth_method="none",
            base_url="http://x/v1",
            model="m",
            api_key_env=None,
            plugin_registry=registry,
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


# ---------------------------------------------------------------------------
# Blocker 2: Reserved-name normalization routes variants to built-ins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variant", "expected_type"),
    [
        ("openai_compat", OpenAICompatProvider),
        ("OpenAI_Compat", OpenAICompatProvider),
        ("OPENAI-COMPAT", OpenAICompatProvider),
        (" ollama", OllamaProvider),
        ("Ollama", OllamaProvider),
        ("OLLAMA", OllamaProvider),
    ],
)
def test_normalized_variants_route_to_builtins(variant: str, expected_type: type) -> None:
    """Separator/case variants of built-in names must route to built-in providers."""
    p = create_provider(
        enabled=True,
        provider=variant,
        auth_method=None,
        base_url="http://localhost:11434",
        model="m",
        api_key_env=None,
    )
    assert isinstance(p, expected_type)


@pytest.mark.parametrize(
    "variant",
    ["github_copilot", "GitHub_Copilot", "GITHUB-COPILOT"],
)
def test_github_copilot_variants_route_to_builtin(variant: str) -> None:
    """github-copilot casing/separator variants route to the copilot path."""
    # Without oauth token, returns None — but exercises the copilot code path
    p = create_provider(
        enabled=True,
        provider=variant,
        auth_method=None,
        base_url=None,
        model="gpt-4o",
        api_key_env=None,
        oauth_token=None,
    )
    # The copilot path returns None when not logged in — that's correct routing
    assert p is None


def test_normalized_variant_never_queries_plugin_registry() -> None:
    """Built-in variant names must never touch the plugin registry."""
    from unittest.mock import MagicMock

    registry = MagicMock()
    # openai_compat should normalize to openai-compat (a built-in alias)
    p = create_provider(
        enabled=True,
        provider="openai_compat",
        auth_method=None,
        base_url="http://x/v1",
        model="m",
        api_key_env=None,
        plugin_registry=registry,
    )
    assert isinstance(p, OpenAICompatProvider)
    registry.load_selected.assert_not_called()
    registry.create.assert_not_called()


# ---------------------------------------------------------------------------
# Finding #4: Plugin auth misconfiguration → ProviderPluginError
# ---------------------------------------------------------------------------


def test_plugin_auth_misconfigured_raises_provider_plugin_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_AuthMisconfigured on the third-party path must raise ProviderPluginError
    (not log+None), so initial startup warning and rebuild error surface work."""
    from unittest.mock import MagicMock

    from korvid.agent.provider_plugin import ProviderPluginMetadata
    from korvid.providers.plugin_registry import ProviderPluginError

    meta = ProviderPluginMetadata(
        api_version=1, name="my-llm", display_name="My", auth_methods=("api_key",)
    )
    registry = MagicMock()
    registry.load_selected.return_value = MagicMock(metadata=meta)

    with pytest.raises(ProviderPluginError, match="auth misconfigured"):
        create_provider(
            enabled=True,
            provider="my-llm",
            auth_method="api_key",
            base_url="http://x/v1",
            model="m",
            api_key_env="MISSING_KEY_ENV",  # not set → _AuthMisconfigured
            plugin_registry=registry,
        )


def test_builtin_auth_misconfigured_still_returns_none() -> None:
    """Built-in providers must keep their log+None behaviour on auth failure."""
    p = create_provider(
        enabled=True,
        provider="openai",
        auth_method="api_key",
        base_url="http://x/v1",
        model="m",
        api_key_env="NONEXISTENT_KEY_abc",
    )
    assert p is None


# ---------------------------------------------------------------------------
# Finding #2 round 5: variant dispatch tests remain correct
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider",
    ["openai", "azure", "vllm", "github", "anthropic", "claude", "openai-compat"],
)
def test_openai_compat_aliases_route_to_builtin(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every OpenAI-compat alias must route to the built-in path (not plugin)."""
    monkeypatch.setenv("KORVID_TEST_REG_KEY", "k")
    p = create_provider(
        enabled=True,
        provider=provider,
        auth_method="api_key",
        base_url="http://x/v1",
        model="m",
        api_key_env="KORVID_TEST_REG_KEY",
    )
    from korvid.providers.openai_compat import OpenAICompatProvider

    assert isinstance(p, OpenAICompatProvider)
