"""GitHub Copilot auth: device flow login + short-lived chat token refresh.

UNOFFICIAL API: token exchange uses api.github.com/copilot_internal/v2/token
and chat uses api.githubcopilot.com — both may change without notice.
Requires an active GitHub Copilot subscription.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from korvid.agent.credentials import CredentialSource

GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"  # GitHub Copilot plugin OAuth app
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
COPILOT_CHAT_BASE_URL = "https://api.githubcopilot.com"
_REFRESH_MARGIN_S = 60.0
_DEFAULT_TOKEN_TTL_S = 600.0  # used when the exchange response omits expires_at
_JSON_ACCEPT = {"Accept": "application/json"}
# GitHub rejects the token exchange (HTTP 403 "Please only use approved
# clients", notification_id: programmatic_token_generation) unless the
# request identifies an editor client.
_EDITOR_HEADERS = {
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.4",
    "User-Agent": "GitHubCopilotChat/0.22.4",
}


class DeviceLoginError(Exception):
    """Device login failed, expired, or the Copilot token exchange was rejected."""


@dataclass(frozen=True)
class DeviceCodePrompt:
    user_code: str
    verification_uri: str
    device_code: str
    interval: int
    expires_in: int


class GitHubDeviceFlow:
    """OAuth 2.0 device flow against github.com."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def start(self) -> DeviceCodePrompt:
        resp = await self._client.post(
            DEVICE_CODE_URL,
            data={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"},
            headers=_JSON_ACCEPT,
        )
        resp.raise_for_status()
        d = resp.json()
        return DeviceCodePrompt(
            user_code=str(d["user_code"]),
            verification_uri=str(d["verification_uri"]),
            device_code=str(d["device_code"]),
            interval=int(d.get("interval", 5)),
            expires_in=int(d.get("expires_in", 900)),
        )

    async def poll(self, prompt: DeviceCodePrompt) -> str:
        """Poll until the user approves; return the OAuth access token."""
        deadline = time.monotonic() + prompt.expires_in
        interval = float(prompt.interval)
        while True:
            # RFC 8628 §3.5: wait `interval` before EVERY token request,
            # including the first one.
            await asyncio.sleep(interval)
            if time.monotonic() >= deadline:
                break
            resp = await self._client.post(
                ACCESS_TOKEN_URL,
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "device_code": prompt.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers=_JSON_ACCEPT,
            )
            resp.raise_for_status()
            d = resp.json()
            if "access_token" in d:
                return str(d["access_token"])
            error = d.get("error")
            if error in ("authorization_pending", "slow_down"):
                if error == "slow_down":
                    # RFC 8628 §3.5: increase the interval for ALL subsequent polls.
                    interval += 5
                continue
            raise DeviceLoginError(f"device login failed: {error}")
        raise DeviceLoginError("device login timed out")

    async def aclose(self) -> None:
        await self._client.aclose()


class CopilotCredentialSource(CredentialSource):
    """Exchanges a GitHub OAuth token for short-lived Copilot chat tokens."""

    def __init__(
        self,
        oauth_token: str,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._oauth_token = oauth_token
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._refresh_lock = asyncio.Lock()

    def _needs_refresh(self) -> bool:
        return self._token is None or self._clock() >= self._expires_at - _REFRESH_MARGIN_S

    async def headers(self) -> dict[str, str]:
        if self._needs_refresh():
            # Serialize refreshes so concurrent requests trigger one exchange.
            async with self._refresh_lock:
                if self._needs_refresh():
                    await self._refresh()
        return {
            "Authorization": f"Bearer {self._token}",
            "Copilot-Integration-Id": "vscode-chat",
            "Editor-Version": "vscode/1.95.0",
        }

    async def _refresh(self) -> None:
        resp = await self._client.get(
            COPILOT_TOKEN_URL,
            headers={
                "Authorization": f"token {self._oauth_token}",
                **_JSON_ACCEPT,
                **_EDITOR_HEADERS,
            },
        )
        if resp.status_code != 200:
            raise DeviceLoginError(
                f"Copilot token exchange failed (HTTP {resp.status_code}) — "
                "check your Copilot subscription or re-run :ai to log in again"
            )
        d = resp.json()
        self._token = str(d["token"])
        if "expires_at" in d:
            self._expires_at = float(d["expires_at"])
        else:
            # No expiry in the response: cache conservatively instead of
            # re-exchanging on every request (extra round-trips, rate limits).
            self._expires_at = self._clock() + _DEFAULT_TOKEN_TTL_S

    async def aclose(self) -> None:
        await self._client.aclose()
