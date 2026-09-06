"""The one first-party `provider-default` credential chain.

Carries over `EntraCredentialSource`'s coverage — scope, caching inside
the refresh margin, one token call under concurrency, and the install
hint when the extra is absent — onto the shape the transport can
actually consume. The old class produced an `Authorization` header, and
nothing has consumed a `CredentialSource` header for a routed reference
since the OpenAI-compatible transport was deleted: LiteLLM's Azure client
takes a token *provider*, not headers.
"""

from __future__ import annotations

import asyncio
import builtins
import inspect
import re
from types import SimpleNamespace

import pytest

from korvid import __version__
from korvid.providers.entra import (
    ENTRA_SCOPE,
    EntraTokenSource,
    korvid_provider_default_credentials,
)
from korvid.providers.provider_default import CredentialUnavailable, ProviderDefaultCredential


class FakeCredential:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = 0
        self.expires_on = 9_999_999_999

    async def get_token(self, scope: str) -> SimpleNamespace:
        self.calls.append(scope)
        return SimpleNamespace(token=f"tok-{len(self.calls)}", expires_on=self.expires_on)

    async def close(self) -> None:
        self.closed += 1


async def test_the_token_is_requested_for_the_scope_entra_requires() -> None:
    cred = FakeCredential()
    source = EntraTokenSource(credential=cred, clock=lambda: 1000.0)

    assert await source.token() == "tok-1"
    assert cred.calls == [ENTRA_SCOPE]


async def test_the_token_is_cached_until_the_refresh_margin() -> None:
    """A token that expires mid-request is a 401 the operator has to
    interpret, so the refresh happens inside a margin rather than on
    expiry — and outside that margin nothing is fetched twice."""
    cred = FakeCredential()
    now = {"t": 1000.0}
    source = EntraTokenSource(credential=cred, clock=lambda: now["t"])

    await source.token()
    await source.token()
    assert len(cred.calls) == 1

    now["t"] = 9_999_999_999 - 100  # inside the 300s refresh margin
    assert await source.token() == "tok-2"
    assert len(cred.calls) == 2


async def test_concurrent_requests_trigger_one_token_call() -> None:
    class SlowCredential(FakeCredential):
        async def get_token(self, scope: str) -> SimpleNamespace:
            self.calls.append(scope)
            await asyncio.sleep(0.01)
            return SimpleNamespace(token="tok", expires_on=9_999_999_999)

    cred = SlowCredential()
    source = EntraTokenSource(credential=cred, clock=lambda: 1000.0)

    await asyncio.gather(source.token(), source.token(), source.token())

    assert len(cred.calls) == 1


async def test_closing_the_source_releases_the_credential() -> None:
    """The provider owns the chain, so a rebuilt agent must not leak the
    credential's HTTP client."""
    cred = FakeCredential()
    source = EntraTokenSource(credential=cred, clock=lambda: 1000.0)

    await source.token()
    await source.aclose()

    assert cred.closed == 1


def test_the_declaration_names_its_prefix_its_extra_and_a_display_name() -> None:
    (declared,) = korvid_provider_default_credentials()

    assert isinstance(declared, ProviderDefaultCredential)
    assert declared.prefix == "azure"
    assert declared.requires_extra == "entra"
    assert declared.display_name


async def test_the_declaration_hands_over_a_refreshing_provider_and_its_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport is given a callable, never a resolved token.

    A token resolved once at build time would be carried by a profile
    that outlives it; the callable is awaited per request, so the refresh
    margin above is what the wire actually sees. The contribution also
    carries the close, because the provider that owns the profile is the
    only thing that will ever release the credential.
    """
    cred = FakeCredential()
    monkeypatch.setattr("korvid.providers.entra._default_credential", lambda: cred)
    (declared,) = korvid_provider_default_credentials()

    resolved = declared.resolve()

    assert set(resolved.parameters) == {"azure_ad_token_provider"}
    provider = resolved.parameters["azure_ad_token_provider"]
    assert inspect.iscoroutinefunction(provider)
    assert await provider() == "tok-1"  # type: ignore[operator]  # asserted above
    assert resolved.aclose is not None
    await resolved.aclose()
    assert cred.closed == 1


def test_a_missing_extra_is_refused_with_the_isolated_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """korvid names the extra; the SDK would have raised a bare ImportError.

    The refusal happens while the profile is being built, not on the
    operator's first message, which is the whole reason korvid resolves
    the chain itself instead of setting LiteLLM's global.
    """
    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "azure.identity.aio":
            raise ImportError("azure.identity.aio is unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    (declared,) = korvid_provider_default_credentials()
    requirement = f"korvid[all,entra]=={__version__}"

    with pytest.raises(
        CredentialUnavailable,
        match=(
            r"Entra auth requires isolated extras.*"
            r"including Entra.*"
            rf"uv tool install --force '{re.escape(requirement)}'.*"
            rf"pipx install --force '{re.escape(requirement)}'"
        ),
    ) as excinfo:
        declared.resolve()

    assert "pip install" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# The gap this declaration closes, measured against the shipped SDK
# ---------------------------------------------------------------------------


def _clear_azure_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator's own Azure variables must not decide this test."""
    import os

    for name in list(os.environ):
        if name.startswith("AZURE"):
            monkeypatch.delenv(name, raising=False)


def test_the_transport_alone_resolves_no_entra_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measurement the declaration exists for, on litellm 1.98.0.

    `docs/agent.md` promises Entra ID — `az login` or managed identity —
    for an `azure` profile. Built with no api key and no declaration, the
    SDK resolves `azure_ad_token_provider` to `None`: the request would
    go out unauthenticated. LiteLLM *can* reach `DefaultAzureCredential`,
    but only behind `litellm.enable_azure_ad_token_refresh`, a
    process-global that defaults to False and would change credential
    resolution for every profile at once.
    """
    from litellm.llms.azure.common_utils import BaseAzureLLM

    _clear_azure_environment(monkeypatch)
    monkeypatch.setattr("litellm.enable_azure_ad_token_refresh", False)

    params = BaseAzureLLM().initialize_azure_sdk_client(
        litellm_params={},
        api_key=None,
        api_base="https://example.openai.azure.com",
        model_name="gpt-4o",
        api_version="2024-06-01",
        is_async=True,
    )

    assert params["azure_ad_token_provider"] is None
    assert params["azure_ad_token"] is None
    assert params["api_key"] is None


async def test_the_declared_chain_reaches_the_sdk_client_and_is_awaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """korvid's contribution survives every hop it has to survive.

    profile -> `create_provider_from_profile` -> `RequestPlan.call_kwargs`
    -> litellm's azure client parameters -> `AsyncAzureOpenAI`, which
    types `azure_ad_token_provider` as `Callable[[], str | Awaitable[str]]`
    and awaits it per request. That last hop is why the token source is
    async: a synchronous credential call would block the event loop on
    every refresh.
    """
    from litellm.llms.azure.common_utils import BaseAzureLLM
    from openai import AsyncAzureOpenAI

    from korvid.core.config import ConnectionAuthConfig, ModelConnectionConfig
    from korvid.providers.litellm_factory import create_provider_from_profile
    from korvid.providers.litellm_provider import LiteLLMProvider
    from korvid.providers.provider_default import ProviderDefaultRegistry

    _clear_azure_environment(monkeypatch)
    cred = FakeCredential()
    monkeypatch.setattr("korvid.providers.entra._default_credential", lambda: cred)

    registry = ProviderDefaultRegistry.from_entry_points()
    provider = create_provider_from_profile(
        ModelConnectionConfig(
            model="azure/gpt-4o",
            endpoint="https://example.openai.azure.com",
            auth=ConnectionAuthConfig(method="provider-default"),
        ),
        provider_defaults=registry,
    )
    assert isinstance(provider, LiteLLMProvider), registry.errors

    kwargs = provider._plan.call_kwargs([], [], stream=True)
    assert "api_key" not in kwargs
    token_provider = kwargs["azure_ad_token_provider"]

    params = BaseAzureLLM().initialize_azure_sdk_client(
        litellm_params={"azure_ad_token_provider": token_provider},
        api_key=None,
        api_base="https://example.openai.azure.com",
        model_name="gpt-4o",
        api_version="2024-06-01",
        is_async=True,
    )
    assert params["azure_ad_token_provider"] is token_provider

    client = AsyncAzureOpenAI(**params)
    assert await client._get_azure_ad_token() == "tok-1"
    assert cred.calls == [ENTRA_SCOPE]

    await provider.aclose()
    assert cred.closed == 1
