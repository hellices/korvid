"""The Copilot device-login flow, declared as data on the extension point.

Migrated from `tests/providers/test_github_copilot.py` (Task 17): every
assertion about the device flow — code rendering, polling, expiry, token
storage, cancellation — moves here with the module, and the claim/builder
cases are new.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from korvid.agent.model_policy import ModelDescriptor
from korvid.agent.model_profiles import (
    ConnectionAuthConfig,
    DeviceLoginPrompt,
    EndpointRequirement,
    ModelConnectionConfig,
    SpecialFlow,
)
from korvid.providers.flow_copilot import (
    COPILOT_CHAT_BASE_URL,
    CREDENTIAL_KEY,
    CopilotCredentialSource,
    CopilotDeviceLogin,
    DeviceCodePrompt,
    DeviceLoginError,
    GitHubDeviceFlow,
    ProviderError,
    copilot_flow,
)
from korvid.providers.special_flows import SpecialFlowRegistry
from korvid.providers.token_store import TokenStore


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _profile(
    reference: str = "github-copilot/gpt-4o",
    *,
    method: str = "device-login",
    endpoint: str | None = None,
) -> ModelConnectionConfig:
    return ModelConnectionConfig(
        model=reference,
        endpoint=endpoint,
        auth=ConnectionAuthConfig(method=method),
    )


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TokenStore:
    """A file-backed store: tests must never touch the OS keychain."""
    monkeypatch.setitem(sys.modules, "keyring", None)  # import keyring -> ImportError
    return TokenStore(fallback_path=tmp_path / ".config" / "korvid" / "credentials.json")


class _FakeDeviceFlow:
    """A `GitHubDeviceFlow` that never leaves the process."""

    def __init__(
        self,
        *,
        token: str = "gho_tok",
        error: Exception | None = None,
        prompt: DeviceCodePrompt | None = None,
    ) -> None:
        self.token = token
        self.error = error
        self.prompt = prompt or DeviceCodePrompt(
            user_code="ABCD-1234",
            verification_uri="https://github.com/login/device",
            device_code="d",
            interval=1,
            expires_in=900,
        )
        self.closed = False
        self.polled = False

    async def start(self) -> DeviceCodePrompt:
        return self.prompt

    async def poll(self, prompt: DeviceCodePrompt) -> str:
        self.polled = True
        if self.error is not None:
            raise self.error
        return self.token

    async def aclose(self) -> None:
        self.closed = True


def _login(store: TokenStore, flow: _FakeDeviceFlow) -> CopilotDeviceLogin:
    return CopilotDeviceLogin(flow_factory=lambda: flow, store=store)  # type: ignore[arg-type]  # test double


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def test_the_flow_declares_itself_as_data() -> None:
    flow = copilot_flow()
    assert isinstance(flow, SpecialFlow)
    assert flow.prefix == "github-copilot"
    assert {m.id for m in flow.auth_methods} == {"device-login"}


def test_the_prefix_is_korvids_not_litellms() -> None:
    """LiteLLM's own name is `github_copilot`. korvid declares the hyphen
    spelling because that is what an operator writes and what the docs
    show."""
    assert copilot_flow().prefix == "github-copilot"
    assert "_" not in copilot_flow().prefix


@pytest.mark.parametrize("reference", ["github-copilot/gpt-4o", "github_copilot/gpt-4o"])
def test_both_spellings_reach_this_flow(reference: str) -> None:
    """Declaring the hyphen spelling is not enough on its own: LiteLLM's
    tables publish the underscore form, so a reference arriving from
    search or from a config file will often carry it. If that form did
    not fold onto this claim it would fall through to `get_llm_provider`
    and start the SDK's device flow - the exact failure this module
    exists to prevent."""
    registry = SpecialFlowRegistry([copilot_flow()])
    assert registry.claim(reference) is not None


def test_the_flow_supplies_its_own_host_so_the_wizard_never_asks() -> None:
    """korvid knows the chat host; an operator typing one is the unusual
    case, not the required one."""
    assert copilot_flow().endpoint is EndpointRequirement.UNSUPPORTED


def test_the_flow_carries_a_builder_and_both_auth_hooks() -> None:
    """A declaration-only flow claims the prefix and then refuses. This
    one has to be able to log in and to build."""
    flow = copilot_flow()
    assert flow.build_provider is not None
    assert flow.begin_auth is not None
    assert flow.finish_auth is not None


def test_the_shipped_distribution_registers_the_flow_on_the_entry_point() -> None:
    """korvid's own flows load through the public extension point, so a
    third-party flow cannot be a second-class path that only works in
    theory."""
    from korvid.providers.litellm_runtime import models_by_provider

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes=models_by_provider())
    claimed = registry.claim("github_copilot/gpt-4o")
    assert claimed is not None
    assert claimed.prefix == "github-copilot"


# ---------------------------------------------------------------------------
# Device login — the reason this flow exists
# ---------------------------------------------------------------------------


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

    monkeypatch.setattr("korvid.providers.flow_copilot.asyncio.sleep", fake_sleep)
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

    monkeypatch.setattr("korvid.providers.flow_copilot.asyncio.sleep", fake_sleep)
    prompt = DeviceCodePrompt("u", "https://x", "d", 5, 900)
    assert await flow.poll(prompt) == "gho_tok"
    # First wait uses the base interval; slow_down bumps it for ALL
    # subsequent polls (RFC 8628 §3.5).
    assert sleeps == [5, 10, 10]


async def test_poll_stops_at_the_expiry_the_server_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unanswered login ends; it must not poll GitHub forever."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "authorization_pending"})

    async def fake_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("korvid.providers.flow_copilot.asyncio.sleep", fake_sleep)
    flow = GitHubDeviceFlow(client=_client(handler))
    with pytest.raises(DeviceLoginError, match="timed out"):
        await flow.poll(DeviceCodePrompt("u", "https://x", "d", 1, 0))


# ---------------------------------------------------------------------------
# The auth hooks the wizard drives
# ---------------------------------------------------------------------------


async def test_the_device_prompt_is_returned_not_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """LiteLLM's implementation prints to stdout, which corrupts a TUI."""
    login = _login(_store(tmp_path, monkeypatch), _FakeDeviceFlow())
    prompt = await login.begin_auth(_profile())
    assert isinstance(prompt, DeviceLoginPrompt)
    assert prompt.user_code == "ABCD-1234"
    assert prompt.verification_uri == "https://github.com/login/device"
    assert capsys.readouterr().out == ""


async def test_no_credential_file_is_written_outside_korvids_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LiteLLM writes ~/.config/litellm/github_copilot/api-key.json."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store = _store(tmp_path, monkeypatch)
    login = _login(store, _FakeDeviceFlow(token="gho_stored"))

    await login.begin_auth(_profile())
    await login.finish_auth(_profile())

    assert not (tmp_path / ".config" / "litellm").exists()
    assert store.load(CREDENTIAL_KEY) == "gho_stored"


async def test_a_cancelled_device_login_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    device = _FakeDeviceFlow(error=asyncio.CancelledError())
    login = _login(store, device)

    await login.begin_auth(_profile())
    with pytest.raises(asyncio.CancelledError):
        await login.finish_auth(_profile())

    assert store.load(CREDENTIAL_KEY) is None
    assert device.closed, "a cancelled login must not leak the flow's HTTP client"


async def test_a_failed_device_login_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    login = _login(store, _FakeDeviceFlow(error=DeviceLoginError("device login failed: expired")))

    await login.begin_auth(_profile())
    with pytest.raises(DeviceLoginError, match="expired"):
        await login.finish_auth(_profile())

    assert store.load(CREDENTIAL_KEY) is None


async def test_finish_auth_returns_the_credential_name_never_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wizard renders what this returns. A token in that string would
    be a secret on the operator's screen and in their scrollback."""
    store = _store(tmp_path, monkeypatch)
    login = _login(store, _FakeDeviceFlow(token="gho_secret"))

    await login.begin_auth(_profile())
    result = await login.finish_auth(_profile())

    assert result is not None
    assert "gho_secret" not in result
    assert CREDENTIAL_KEY in result


async def test_finishing_without_beginning_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = _login(_store(tmp_path, monkeypatch), _FakeDeviceFlow())
    with pytest.raises(DeviceLoginError, match="begin"):
        await login.finish_auth(_profile())


async def test_a_profile_that_does_not_ask_for_device_login_starts_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wizard skips the stage when `begin_auth` returns None. A login
    started under a method the profile never named would write a
    credential the operator did not ask for."""
    device = _FakeDeviceFlow()
    login = _login(_store(tmp_path, monkeypatch), device)
    assert await login.begin_auth(_profile(method="environment")) is None
    assert not device.polled


async def test_a_second_login_replaces_the_stored_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)

    await _run_login(store, _FakeDeviceFlow(token="gho_first"))
    await _run_login(store, _FakeDeviceFlow(token="gho_second"))

    assert store.load(CREDENTIAL_KEY) == "gho_second"


async def _run_login(store: TokenStore, device: _FakeDeviceFlow) -> None:
    login = _login(store, device)
    await login.begin_auth(_profile())
    await login.finish_auth(_profile())


# ---------------------------------------------------------------------------
# The short-lived chat token
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# The transport the flow owns
# ---------------------------------------------------------------------------


async def test_the_builder_refuses_a_profile_that_is_not_signed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = _login(_store(tmp_path, monkeypatch), _FakeDeviceFlow())
    assert login.build_provider(_profile()) is None


@pytest.mark.parametrize("method", ["environment", "keyring", "none", "provider-default"])
async def test_the_builder_refuses_any_auth_method_but_device_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """A stored OAuth token must never be consumed under a method the
    operator did not name — the legacy adapter refused this too."""
    store = _store(tmp_path, monkeypatch)
    store.save(CREDENTIAL_KEY, "gho_x")
    login = _login(store, _FakeDeviceFlow())
    assert login.build_provider(_profile(method=method)) is None


async def test_the_builder_refuses_a_reference_with_no_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.save(CREDENTIAL_KEY, "gho_x")
    login = _login(store, _FakeDeviceFlow())
    assert login.build_provider(_profile("github-copilot/")) is None


@pytest.mark.parametrize("reference", ["github-copilot/gpt-4o", "github_copilot/gpt-4o"])
async def test_a_signed_in_profile_builds_a_provider_for_the_model_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.save(CREDENTIAL_KEY, "gho_x")
    login = _login(store, _FakeDeviceFlow())

    provider = login.build_provider(_profile(reference))

    assert provider is not None
    assert provider.descriptor == ModelDescriptor("github-copilot", "gpt-4o")
    await provider.aclose()


async def test_requests_go_to_the_copilot_host_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.save(CREDENTIAL_KEY, "gho_x")
    login = _login(store, _FakeDeviceFlow())
    provider = login.build_provider(_profile())
    assert provider is not None

    seen = _serve(provider)
    events = [e async for e in provider.complete([{"role": "user", "content": "hi"}], [])]

    assert seen["url"] == f"{COPILOT_CHAT_BASE_URL}/chat/completions"
    assert seen["json"]["model"] == "gpt-4o"
    assert {"type": "text_delta", "text": "ok"} in events
    assert events[-1] == {"type": "done"}


async def test_an_operator_endpoint_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile that names a gateway is not overridden by the default."""
    store = _store(tmp_path, monkeypatch)
    store.save(CREDENTIAL_KEY, "gho_x")
    login = _login(store, _FakeDeviceFlow())
    provider = login.build_provider(_profile(endpoint="https://gw.internal/v1"))
    assert provider is not None

    seen = _serve(provider)
    [e async for e in provider.complete([{"role": "user", "content": "hi"}], [])]

    assert seen["url"] == "https://gw.internal/v1/chat/completions"


async def test_the_transport_reports_a_refused_request_rather_than_an_empty_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.save(CREDENTIAL_KEY, "gho_x")
    login = _login(store, _FakeDeviceFlow())
    provider = login.build_provider(_profile())
    assert provider is not None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    _serve(provider)
    provider._client = httpx.AsyncClient(  # type: ignore[attr-defined]  # injected transport
        transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ProviderError, match="HTTP 401"):
        [e async for e in provider.complete([{"role": "user", "content": "hi"}], [])]


def _serve(provider: Any) -> dict[str, Any]:
    """Put the whole provider on mock transports and report what it sent.

    Both halves need one: the chat client *and* the credential source,
    which exchanges the stored OAuth token for a short-lived chat token
    on the first request. A test that repointed only the first would
    reach api.github.com for real.
    """
    seen: dict[str, Any] = {}
    provider._client = _chat_client(seen)
    provider._credentials._client = _token_client()
    return seen


def _token_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "cop-1", "expires_at": 9_999_999_999})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _chat_client(seen: dict[str, Any]) -> httpx.AsyncClient:
    """A chat endpoint that streams one text delta and a usage report."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        body = (
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
            'data: {"usage":{"prompt_tokens":3,"completion_tokens":4}}\n'
            "data: [DONE]\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
