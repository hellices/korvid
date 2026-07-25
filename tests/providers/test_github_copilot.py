import asyncio
from collections.abc import Callable

import httpx
import pytest

from korvid.providers.github_copilot import (
    CopilotCredentialSource,
    DeviceCodePrompt,
    DeviceLoginError,
    GitHubDeviceFlow,
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_start_returns_prompt() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "device_code": "d",
                "user_code": "ABCD-1234",
                "verification_uri": "https://github.com/login/device",
                "interval": 5,
                "expires_in": 900,
            },
        )

    prompt = await GitHubDeviceFlow(client=_client(handler)).start()
    assert prompt.user_code == "ABCD-1234"


async def test_poll_pending_then_token(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            {"error": "authorization_pending"},
            {"access_token": "gho_tok", "token_type": "bearer"},
        ]
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    flow = GitHubDeviceFlow(client=_client(handler))
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("korvid.providers.github_copilot.asyncio.sleep", fake_sleep)
    prompt = DeviceCodePrompt("u", "https://x", "d", 1, 900)
    assert await flow.poll(prompt) == "gho_tok"
    # RFC 8628 §3.5: wait `interval` before EVERY token request, incl. the first.
    assert sleeps == [1, 1]


async def test_poll_denied_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "access_denied"})

    with pytest.raises(DeviceLoginError, match="access_denied"):
        await GitHubDeviceFlow(client=_client(handler)).poll(
            DeviceCodePrompt("u", "x", "d", 0, 900)
        )


async def test_copilot_source_exchanges_and_caches() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"token": f"cop-{calls['n']}", "expires_at": 9_999_999_999})

    src = CopilotCredentialSource("gho_x", client=_client(handler), clock=lambda: 1000.0)
    h1 = await src.headers()
    h2 = await src.headers()
    assert h1["Authorization"] == "Bearer cop-1"
    assert h1["Copilot-Integration-Id"] == "vscode-chat"
    assert "Editor-Version" in h1
    assert calls["n"] == 1  # cached
    assert h2 == h1


async def test_copilot_source_refreshes_when_expired() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"token": f"cop-{calls['n']}", "expires_at": 1100})

    now = {"t": 1000.0}
    src = CopilotCredentialSource("gho_x", client=_client(handler), clock=lambda: now["t"])
    await src.headers()
    now["t"] = 1090.0  # inside 60s refresh window before 1100
    await src.headers()
    assert calls["n"] == 2


async def test_copilot_exchange_failure_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad token"})

    src = CopilotCredentialSource("gho_bad", client=_client(handler))
    with pytest.raises(DeviceLoginError, match="token exchange failed"):
        await src.headers()


async def test_poll_slow_down_increases_interval_persistently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            {"error": "slow_down"},
            {"error": "authorization_pending"},
            {"access_token": "gho_tok", "token_type": "bearer"},
        ]
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    flow = GitHubDeviceFlow(client=_client(handler))
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("korvid.providers.github_copilot.asyncio.sleep", fake_sleep)
    prompt = DeviceCodePrompt("u", "https://x", "d", 5, 900)
    assert await flow.poll(prompt) == "gho_tok"
    # First wait uses the base interval; slow_down bumps it for ALL
    # subsequent polls (RFC 8628 §3.5).
    assert sleeps == [5, 10, 10]


async def test_missing_expires_at_defaults_to_short_ttl() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"token": "tok-1"})  # no expires_at

    src = CopilotCredentialSource("gho_x", client=_client(handler), clock=lambda: 1000.0)
    await src.headers()
    await src.headers()
    # Without expires_at the token must still be cached (conservative TTL),
    # not re-exchanged on every request.
    assert calls["n"] == 1


async def test_concurrent_headers_refresh_once() -> None:
    calls = {"n": 0}

    async def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"token": "tok-1", "expires_at": 5000})

    src = CopilotCredentialSource(
        "gho_x",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: 1000.0,
    )
    await asyncio.gather(src.headers(), src.headers(), src.headers())
    assert calls["n"] == 1


async def test_token_exchange_sends_editor_identification_headers() -> None:
    """GitHub rejects the copilot_internal/v2/token exchange with HTTP 403
    ("Please only use approved clients") unless the request identifies an
    editor client. Verified live: Editor-Version/plugin/User-Agent pass."""
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in req.headers.items()})
        return httpx.Response(200, json={"token": "ct", "expires_at": 2000})

    src = CopilotCredentialSource("gho_x", client=_client(handler), clock=lambda: 1000.0)
    await src.headers()
    assert "editor-version" in seen
    assert "editor-plugin-version" in seen
    assert "githubcopilot" in seen.get("user-agent", "").lower()
