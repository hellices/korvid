"""Microsoft Entra ID credential source (Azure OpenAI / AI Foundry)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from korvid.agent.credentials import CredentialSource
from korvid.agent.install_hint import isolated_install_hint

ENTRA_SCOPE = "https://cognitiveservices.azure.com/.default"
_REFRESH_MARGIN_S = 300.0


class EntraCredentialSource(CredentialSource):
    """Bearer tokens via azure-identity (DefaultAzureCredential by default)."""

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

    def _get_credential(self) -> Any:
        if self._credential is None:
            try:
                from azure.identity.aio import DefaultAzureCredential
            except ImportError as exc:
                raise RuntimeError(
                    f"Entra auth requires isolated extras — {isolated_install_hint(entra=True)}"
                ) from exc
            self._credential = DefaultAzureCredential()
        return self._credential

    async def headers(self) -> dict[str, str]:
        if self._needs_refresh():
            # Serialize refreshes so concurrent requests trigger one token call.
            async with self._refresh_lock:
                if self._needs_refresh():
                    access = await self._get_credential().get_token(ENTRA_SCOPE)
                    self._token = str(access.token)
                    self._expires_on = float(access.expires_on)
        return {"Authorization": f"Bearer {self._token}"}

    async def aclose(self) -> None:
        if self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                await close()
