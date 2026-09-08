"""Microsoft Entra ID credentials for the transport's own Azure client.

This module is the one place korvid spells this vendor's protocol: the
OAuth scope it requires, the parameter its transport reads a refreshing
credential from, and the extra that has to be installed for either to
work. Everything above it — the factory, the wizard, the composition
root — sees a `ProviderDefaultCredential` and a reference prefix.

Why a token *provider* and not a header: measured on litellm 1.98.0, an
`azure/...` reference built with no `api_key` resolves
`azure_ad_token_provider` to `None`, so `auth.method: provider-default`
authenticates with nothing. LiteLLM does reach `DefaultAzureCredential`,
but only behind the process-global `litellm.enable_azure_ad_token_refresh`
(default `False`), which would change credential resolution for every
profile at once and, with the credential library absent, fails as a bare
`ImportError` from inside the SDK instead of naming the extra. Passing
the provider explicitly needs no global, is scoped to the one profile
that asked for it, and is refused here — with an install hint — when the
extra is missing.

The callable handed over is a coroutine function.
`openai.AsyncAzureOpenAI` types `azure_ad_token_provider` as
`Callable[[], str | Awaitable[str]]` and awaits the result on every
request, so the token is refreshed without blocking the event loop, and
korvid keeps its own refresh margin rather than inheriting one.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from korvid.agent.install_hint import isolated_install_hint
from korvid.providers.provider_default import (
    CredentialUnavailable,
    ProviderDefaultCredential,
    ResolvedCredential,
)

#: The scope Entra requires for this service. The identifier of an
#: external protocol, like a URL — not a branch korvid takes.
ENTRA_SCOPE = "https://cognitiveservices.azure.com/.default"

#: The transport call parameter that carries a refreshing credential.
_TOKEN_PARAMETER = "azure_ad_token_provider"

#: The reference prefix whose `provider-default` this chain answers.
_PREFIX = "azure"

#: The extra that ships the credential library.
_EXTRA = "entra"

_REFRESH_MARGIN_S = 300.0


class EntraTokenSource:
    """A refreshing access token, resolved through azure-identity.

    Holds the credential for the lifetime of the provider that owns it,
    so one sign-in serves every request, and refreshes inside a margin
    rather than on expiry — a token that expires mid-request is a 401 the
    operator has to interpret.
    """

    def __init__(
        self,
        credential: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credential = credential
        self._clock = clock
        self._token: str | None = None
        self._expires_on = 0.0
        self._refresh_lock = asyncio.Lock()

    def _needs_refresh(self) -> bool:
        return self._token is None or self._clock() >= self._expires_on - _REFRESH_MARGIN_S

    async def token(self) -> str:
        """The current access token, refreshed when it is close to expiry.

        Handed to the transport as a callable rather than as a value, so
        a long-lived session re-reads it instead of carrying a token that
        expires while the agent is idle.
        """
        if self._needs_refresh():
            # Serialize refreshes so concurrent requests trigger one token call.
            async with self._refresh_lock:
                if self._needs_refresh():
                    access = await self._require_credential().get_token(ENTRA_SCOPE)
                    self._token = str(access.token)
                    self._expires_on = float(access.expires_on)
        token = self._token
        if token is None:  # pragma: no cover - refresh sets it or raises
            raise CredentialUnavailable("no Entra access token was returned")
        return token

    def _require_credential(self) -> Any:
        if self._credential is None:
            self._credential = _default_credential()
        return self._credential

    async def aclose(self) -> None:
        """Release the credential's own HTTP client, if it opened one."""
        if self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                await close()


def _default_credential() -> Any:
    """`DefaultAzureCredential`, or a refusal naming the missing extra.

    Imported here rather than at module scope: the module is loaded by
    the credential registry on any start that reaches an `azure/`
    profile, and the extra is optional.
    """
    try:
        from azure.identity.aio import DefaultAzureCredential
    except ImportError as exc:
        raise CredentialUnavailable(
            f"Entra auth requires isolated extras — {isolated_install_hint(feature='Entra')}"
        ) from exc
    return DefaultAzureCredential()


def _resolve() -> ResolvedCredential:
    """Build the chain, refusing now rather than at the first request.

    The credential is constructed eagerly so a missing extra is reported
    while the profile is being built — where korvid can name the extra —
    instead of surfacing as an `ImportError` from inside the SDK on the
    operator's first message.
    """
    source = EntraTokenSource(credential=_default_credential())
    return ResolvedCredential(
        parameters={_TOKEN_PARAMETER: source.token},
        aclose=source.aclose,
    )


def korvid_provider_default_credentials() -> tuple[ProviderDefaultCredential, ...]:
    """korvid's own declaration, published on the `korvid.credential` group."""
    return (
        ProviderDefaultCredential(
            prefix=_PREFIX,
            display_name="Microsoft Entra ID",
            resolve=_resolve,
            requires_extra=_EXTRA,
        ),
    )
