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

from korvid.agent.credentials import CredentialSource
from korvid.agent.model_policy import ModelDescriptor
from korvid.agent.model_profiles import (
    ConnectionAuthConfig,
    DeviceLoginPrompt,
    EndpointRequirement,
    ModelConnectionConfig,
    SpecialFlow,
)
from korvid.agent.provider import (
    MAX_TOOL_ARGUMENT_CHARS,
    MAX_TOOL_CALLS_PER_RESPONSE,
    REQUEST_SENT,
    STREAM_MALFORMED,
    STREAM_TRUNCATED,
    OperatorSafeProviderError,
    ProviderStreamError,
    ProviderStreamLimitError,
    ProviderStreamTruncatedError,
)
from korvid.providers import flow_copilot
from korvid.providers.flow_copilot import (
    COPILOT_CHAT_BASE_URL,
    CREDENTIAL_KEY,
    DEVICE_LOGIN_NOT_STARTED,
    DEVICE_LOGIN_TIMED_OUT,
    CopilotChatProvider,
    CopilotCredentialSource,
    CopilotDeviceLogin,
    DeviceCodePrompt,
    DeviceLoginError,
    GitHubDeviceFlow,
    copilot_flow,
)
from korvid.providers.special_flows import SpecialFlowRegistry
from korvid.providers.static_creds import StaticHeaderSource
from korvid.providers.token_store import TokenStore

#: A value shaped like a credential, so a test can prove a refusal did
#: not echo the body it came from.
_SECRET_ISH = "gho_leaked_token_value"


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


class _FakeDeviceFlow(GitHubDeviceFlow):
    """A `GitHubDeviceFlow` that never leaves the process.

    A real subclass, not a look-alike: `CopilotDeviceLogin` is typed against
    `GitHubDeviceFlow`, so anything the fake is allowed to stand in for is
    exactly what the production factory may return. `__init__` deliberately
    does not call `super().__init__` — that would open an `httpx` client this
    double must never own.
    """

    def __init__(
        self,
        *,
        token: str = "gho_tok",
        error: BaseException | None = None,
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
    return CopilotDeviceLogin(flow_factory=lambda: flow, store=store)


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


# ---------------------------------------------------------------------------
# Which of this flow's sentences the wizard may repeat
# ---------------------------------------------------------------------------
#
# The setup wizard withholds every failure's own text unless the adapter
# declared it safe, because a sign-in failure can quote the token being
# minted. Two of this flow's refusals are korvid's own written sentences,
# and they are the only part of an unanswered or unstarted login an
# operator can act on — so they are declared, and nothing else is.


@pytest.mark.parametrize(
    "message",
    [DEVICE_LOGIN_TIMED_OUT, DEVICE_LOGIN_NOT_STARTED],
    ids=["timed out", "never started"],
)
def test_the_flows_written_refusals_are_declared_operator_safe(message: str) -> None:
    assert issubclass(DeviceLoginError, OperatorSafeProviderError)
    assert DeviceLoginError(message).operator_message() == message


def test_a_refusal_quoting_githubs_own_answer_is_not_declared_safe() -> None:
    """`poll` names the error code GitHub returned, and the token exchange
    quotes an HTTP status — neither is a sentence this repository wrote,
    so neither inherits the exemption."""
    assert DeviceLoginError(f"device login failed: {_SECRET_ISH}").operator_message() is None
    assert DeviceLoginError("Copilot token exchange failed (HTTP 403)").operator_message() is None


def test_every_declared_message_is_one_this_module_raises() -> None:
    """A declaration that names a string nothing raises is an audit that
    has stopped tracking the code."""
    source = Path(flow_copilot.__file__).read_text(encoding="utf-8")
    for message in DeviceLoginError.safe_messages:
        assert repr(message) in source or message in source


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


async def test_an_operator_endpoint_is_refused_rather_than_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flow declares `endpoint` UNSUPPORTED, so honouring one is a
    hole, not a feature.

    The wizard never writes an endpoint for this flow — but a profile is
    a YAML file an operator (or anything that can write their config)
    can hand-edit. If `build_provider` took that host, the very next
    request would carry a Copilot chat token, minted from the stored
    GitHub OAuth credential, to an address korvid never vetted. Refusing
    is the only answer consistent with what the flow declares.
    """
    store = _store(tmp_path, monkeypatch)
    store.save(CREDENTIAL_KEY, "gho_x")
    login = _login(store, _FakeDeviceFlow())

    assert login.build_provider(_profile(endpoint="https://gw.internal/v1")) is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example/v1",
        "http://127.0.0.1:8080",
        "https://api.githubcopilot.com.evil.example",
        # The official host itself: the flow answers UNSUPPORTED, so there
        # is no spelling of the field that is a valid thing to have written.
        COPILOT_CHAT_BASE_URL,
    ],
)
async def test_a_hand_written_endpoint_never_reaches_a_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    """Fail closed *before* the token is read or a transport is built.

    Order is the point. A refusal that happened after `store.load` would
    still have decrypted the credential into this process's memory, and
    one that happened after the provider was constructed would already
    have a client pointed at the operator's host.
    """
    store = _store(tmp_path, monkeypatch)
    store.save(CREDENTIAL_KEY, "gho_secret_value")
    reads: list[str] = []

    def _record_load(_self: TokenStore, key: str) -> str:
        reads.append(key)
        return "gho_secret_value"

    monkeypatch.setattr(type(store), "load", _record_load)

    built: list[object] = []
    monkeypatch.setattr(
        "korvid.providers.flow_copilot.CopilotChatProvider",
        lambda *args, **kwargs: built.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "korvid.providers.flow_copilot.CopilotCredentialSource",
        lambda *args, **kwargs: built.append((args, kwargs)),
    )

    login = _login(store, _FakeDeviceFlow())

    assert login.build_provider(_profile(endpoint=endpoint)) is None
    assert reads == [], "the stored OAuth token must not be read for a refused profile"
    assert built == [], "no transport and no credential source may be constructed"


async def test_the_refusal_names_the_field_and_never_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An operator has to learn which field to delete — and the log has to
    stay safe to paste into an issue.

    Neither the stored token nor the endpoint is echoed: the token is a
    credential, and a hand-written URL can itself carry `user:password@`.
    """
    store = _store(tmp_path, monkeypatch)
    store.save(CREDENTIAL_KEY, "gho_secret_value")
    login = _login(store, _FakeDeviceFlow())

    with caplog.at_level("WARNING", logger="korvid.providers.flow_copilot"):
        assert login.build_provider(_profile(endpoint="https://user:pw@gw.internal/v1")) is None

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "endpoint" in logged
    assert "github-copilot" in logged
    assert "gho_secret_value" not in logged
    assert "gw.internal" not in logged
    assert "pw" not in logged


@pytest.mark.parametrize("endpoint", [None, "", "   "])
async def test_a_profile_that_names_no_endpoint_still_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str | None
) -> None:
    """Only a *named* host is refused. Blank is what the wizard writes for
    a flow whose endpoint is UNSUPPORTED, so it must stay buildable."""
    store = _store(tmp_path, monkeypatch)
    store.save(CREDENTIAL_KEY, "gho_x")
    login = _login(store, _FakeDeviceFlow())

    provider = login.build_provider(_profile(endpoint=endpoint))

    assert provider is not None
    seen = _serve(provider)
    [e async for e in provider.complete([{"role": "user", "content": "hi"}], [])]
    assert seen["url"] == f"{COPILOT_CHAT_BASE_URL}/chat/completions"
    await provider.aclose()


async def test_the_builder_never_reads_a_host_off_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host is korvid's constant, not profile data.

    `build_provider` is the only place a Copilot token and a URL meet, so
    the one URL it may pass is the module's own — pinned here by
    substituting the constant and watching the built provider follow it.
    """
    store = _store(tmp_path, monkeypatch)
    store.save(CREDENTIAL_KEY, "gho_x")
    monkeypatch.setattr(
        "korvid.providers.flow_copilot.COPILOT_CHAT_BASE_URL", "https://pinned.invalid"
    )
    login = _login(store, _FakeDeviceFlow())

    provider = login.build_provider(_profile())

    assert provider is not None
    assert provider._base_url == "https://pinned.invalid"  # type: ignore[attr-defined]  # pins the one URL source
    await provider.aclose()


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
    with pytest.raises(ProviderStreamError, match="refused the credential"):
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


# ---------------------------------------------------------------------------
# The SSE dialect the flow parses for itself
#
# The flow carries its own reader rather than importing the shared one —
# the vendor guard forbids naming the shared dialect inside this module —
# so the reader needs its own tests. Without them, deleting the shared
# adapter's tests (Task 18) would delete the only coverage fragmented
# tool calls, usage reporting and the acknowledgement contract ever had.
# ---------------------------------------------------------------------------


def _sse(*chunks: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def _sse_provider(
    body: str,
    *,
    status: int = 200,
    capture: dict[str, Any] | None = None,
) -> CopilotChatProvider:
    """The chat transport on a mock wire, with no device flow in the way."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["url"] = str(request.url)
            capture["json"] = json.loads(request.content)
            capture["headers"] = dict(request.headers)
        return httpx.Response(status, text=body, headers={"content-type": "text/event-stream"})

    return CopilotChatProvider(
        base_url=COPILOT_CHAT_BASE_URL,
        model="gpt-4o",
        credentials=StaticHeaderSource("cop-1"),
        client=_client(handler),
    )


async def _stream(
    provider: CopilotChatProvider, tools: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    return [e async for e in provider.complete([{"role": "user", "content": "hi"}], tools or [])]


async def _drain(provider: CopilotChatProvider, seen: list[dict[str, Any]]) -> None:
    """Consume a stream into `seen` so `pytest.raises` wraps one call."""
    async for event in provider.complete([{"role": "user", "content": "hi"}], []):
        seen.append(event)


async def test_text_deltas_stream_in_order_and_end_with_done() -> None:
    events = await _stream(
        _sse_provider(
            _sse(
                {"choices": [{"delta": {"content": "Wor"}}]},
                {"choices": [{"delta": {"content": "ld"}}]},
            )
        )
    )
    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["Wor", "ld"]
    assert events[-1] == {"type": "done"}


async def test_the_first_event_acknowledges_the_transport() -> None:
    """The runtime treats an acknowledged request as sent, whatever
    happens next: a retry that assumed otherwise would double-charge the
    operator's subscription."""
    events = await _stream(_sse_provider(_sse({"choices": [{"delta": {"content": "hi"}}]})))
    assert events[0] == {"type": REQUEST_SENT}


async def test_an_http_error_acknowledges_before_it_raises() -> None:
    seen: list[dict[str, Any]] = []
    provider = _sse_provider("nope", status=401)
    with pytest.raises(ProviderStreamError, match="refused the credential"):
        await _drain(provider, seen)
    assert seen == [{"type": REQUEST_SENT}]


async def test_a_credential_source_that_refuses_sends_nothing() -> None:
    """No headers, no request: nothing was handed to anyone, so the
    acknowledgement must not be emitted either."""

    class _Refusing(CredentialSource):
        async def headers(self) -> dict[str, str]:
            raise RuntimeError("keyring locked")

    provider = CopilotChatProvider(
        base_url=COPILOT_CHAT_BASE_URL,
        model="gpt-4o",
        credentials=_Refusing(),
        client=_client(lambda request: httpx.Response(200)),
    )

    seen: list[dict[str, Any]] = []
    with pytest.raises(RuntimeError, match="keyring locked"):
        await _drain(provider, seen)
    assert seen == []


async def test_a_tool_call_split_across_chunks_is_reassembled() -> None:
    """The host streams a tool call as fragments: the id and name arrive
    once, the arguments arrive a few characters at a time. Emitting a
    fragment would hand the executor half a JSON document."""
    events = await _stream(
        _sse_provider(
            _sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "c1",
                                        "function": {"name": "get_logs", "arguments": '{"po'},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {}}]}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"arguments": 'd": "a'}}]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"}'}}]}}
                    ]
                },
            )
        ),
        tools=[{"type": "function", "function": {"name": "get_logs"}}],
    )
    assert [e for e in events if e["type"] == "tool_call"] == [
        {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": '{"pod": "a"}'}
    ]


async def test_two_interleaved_tool_calls_stay_separate_and_ordered() -> None:
    """Parallel tool calls arrive interleaved and out of order, keyed only
    by `index`. Folding them by arrival order would splice one call's
    arguments into another's."""
    events = await _stream(
        _sse_provider(
            _sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 1,
                                        "id": "second",
                                        "function": {"name": "describe", "arguments": '{"b'},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "first",
                                        "function": {"name": "get_logs", "arguments": '{"a'},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [{"index": 1, "function": {"arguments": '": 2}'}}]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"arguments": '": 1}'}}]
                            }
                        }
                    ]
                },
            )
        ),
        tools=[{"type": "function", "function": {"name": "get_logs"}}],
    )
    assert [e for e in events if e["type"] == "tool_call"] == [
        {"type": "tool_call", "id": "first", "name": "get_logs", "arguments": '{"a": 1}'},
        {"type": "tool_call", "id": "second", "name": "describe", "arguments": '{"b": 2}'},
    ]


async def test_text_and_tool_calls_in_one_answer_both_survive() -> None:
    """A tool call is emitted after the stream ends, so text that arrived
    around it must not be swallowed by the fold."""
    events = await _stream(
        _sse_provider(
            _sse(
                {"choices": [{"delta": {"content": "thinking"}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "content": " out loud",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "c1",
                                        "function": {"name": "get_logs", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        )
    )
    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["thinking", " out loud"]
    assert [e["name"] for e in events if e["type"] == "tool_call"] == ["get_logs"]


async def test_tool_call_arguments_reach_the_executor_exactly_as_sent() -> None:
    """The transport does not parse arguments. A model that emits invalid
    JSON must produce a tool-call event the executor can refuse with the
    model's own text, not a transport-shaped guess at what it meant."""
    events = await _stream(
        _sse_provider(
            _sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "c1",
                                        "function": {
                                            "name": "get_logs",
                                            "arguments": '{"pod": "a"',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
        )
    )
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["arguments"] == '{"pod": "a"'


async def test_a_tool_call_that_never_names_a_function_still_reaches_the_executor() -> None:
    """A fragment with no name and no id is malformed, and the refusal
    belongs to the executor: dropping it here would end the turn with a
    silent nothing instead of a reported failure."""
    events = await _stream(
        _sse_provider(
            _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {}}]}}]})
        )
    )
    assert [e for e in events if e["type"] == "tool_call"] == [
        {"type": "tool_call", "id": "", "name": "", "arguments": ""}
    ]


async def test_usage_is_reported_from_the_final_chunk() -> None:
    events = await _stream(
        _sse_provider(
            _sse(
                {"choices": [{"delta": {"content": "x"}}]},
                {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
            )
        )
    )
    assert {"type": "usage", "input_tokens": 12, "output_tokens": 3} in events


async def test_a_later_usage_report_replaces_an_earlier_one() -> None:
    """`stream_options.include_usage` sends running counts; the last one
    is the total, and reporting the first would under-report the turn."""
    events = await _stream(
        _sse_provider(
            _sse(
                {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 1}},
                {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 9}},
            )
        )
    )
    assert [e for e in events if e["type"] == "usage"] == [
        {"type": "usage", "input_tokens": 12, "output_tokens": 9}
    ]


async def test_partial_usage_is_not_reported_as_if_it_were_exact() -> None:
    """The runtime treats any usage event as authoritative, so a missing
    count defaulted to 0 would make an incomplete report look exact."""
    events = await _stream(
        _sse_provider(
            _sse(
                {"choices": [{"delta": {"content": "x"}}]},
                {"choices": [], "usage": {"total_tokens": 9}},
            )
        )
    )
    assert not [e for e in events if e["type"] == "usage"]


async def test_a_stream_that_reports_no_usage_reports_none() -> None:
    events = await _stream(_sse_provider(_sse({"choices": [{"delta": {"content": "x"}}]})))
    assert not [e for e in events if e["type"] == "usage"]


async def test_sse_without_a_space_after_the_colon_is_still_read() -> None:
    """SSE permits `data:<value>`. Skipping those lines would drop the
    whole answer against a host that writes them."""
    chunk = json.dumps({"choices": [{"delta": {"content": "hi"}}]})
    body = f"data:{chunk}\n\ndata:[DONE]\n\n"
    events = await _stream(_sse_provider(body))
    assert {"type": "text_delta", "text": "hi"} in events
    assert events[-1] == {"type": "done"}


async def test_keepalive_and_comment_lines_are_ignored() -> None:
    """Hosts send `:` comments and blank lines to hold the connection
    open; parsing one as a chunk would raise mid-answer."""
    chunk = json.dumps({"choices": [{"delta": {"content": "hi"}}]})
    body = f": ping\n\ndata: {chunk}\n\n\ndata: [DONE]\n\n"
    events = await _stream(_sse_provider(body))
    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["hi"]


async def test_anything_after_done_is_not_read() -> None:
    """`[DONE]` ends the answer. A host that keeps writing must not be
    able to append to a turn the runtime already considers finished."""
    body = (
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        "data: [DONE]\n\n"
        'data: {"choices":[{"delta":{"content":" and more"}}]}\n\n'
    )
    events = await _stream(_sse_provider(body))
    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["hi"]


# ---------------------------------------------------------------------------
# The terminal marker and the cumulative bounds (issue #336)
# ---------------------------------------------------------------------------


async def test_a_stream_that_never_sent_done_is_refused() -> None:
    """This dialect's terminal marker is on the wire, so its absence is
    knowable: a connection that closed tidily after half an answer is a
    truncated answer, not a short one."""
    seen: list[dict[str, Any]] = []
    provider = _sse_provider('data: {"choices":[{"delta":{"content":"par"}}]}\n\n')

    with pytest.raises(ProviderStreamTruncatedError, match="ended before") as raised:
        await _drain(provider, seen)

    assert raised.value.operator_message() == STREAM_TRUNCATED
    assert [e["type"] for e in seen] == [REQUEST_SENT, "text_delta"]
    assert {"type": "done"} not in seen


async def test_a_truncated_stream_emits_neither_its_calls_nor_its_usage() -> None:
    """A tool call the model never finished writing must not be
    dispatched, and a cost for an answer that never arrived is not a
    measurement."""
    seen: list[dict[str, Any]] = []
    body = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"get_logs","arguments":"{}"}}]}}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":4}}\n\n'
    )

    with pytest.raises(ProviderStreamTruncatedError):
        await _drain(_sse_provider(body), seen)

    assert [e["type"] for e in seen] == [REQUEST_SENT]


async def test_the_stream_stops_at_the_first_done_even_mid_answer() -> None:
    """Two terminal markers are not two answers: everything after the
    first one belongs to a turn the runtime has already closed."""
    body = (
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        "data: [DONE]\n\n"
        'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":9}}\n\n'
        "data: [DONE]\n\n"
    )

    events = await _stream(_sse_provider(body))

    assert [e for e in events if e["type"] == "usage"] == []
    assert events[-1] == {"type": "done"}


async def test_an_unreadable_chunk_is_refused_without_quoting_the_host() -> None:
    """A host that writes something other than JSON is a protocol
    failure. The refusal must not carry the bytes: this stream's body is
    the one place a leaked credential would be echoed straight back."""
    seen: list[dict[str, Any]] = []
    body = f"data: not json {_SECRET_ISH}\n\ndata: [DONE]\n\n"

    with pytest.raises(ProviderStreamError) as raised:
        await _drain(_sse_provider(body), seen)

    assert _SECRET_ISH not in str(raised.value)
    assert raised.value.operator_message() == STREAM_MALFORMED


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
async def test_a_refused_request_never_quotes_the_hosts_answer(status: int) -> None:
    """Providers echo the credential they refused in their own 401
    bodies, so the status is reported and the body never is."""
    seen: list[dict[str, Any]] = []
    provider = _sse_provider(f"denied: {_SECRET_ISH}", status=status)

    with pytest.raises(ProviderStreamError) as raised:
        await _drain(provider, seen)

    message = str(raised.value)
    assert _SECRET_ISH not in message
    assert raised.value.operator_message() == message
    assert seen == [{"type": REQUEST_SENT}]


async def test_a_connection_that_fails_mid_stream_is_translated() -> None:
    """An `httpx` error carries the request — including its headers — so
    it is translated here rather than allowed to escape as itself."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError(f"reset while reading {_SECRET_ISH}", request=request)

    provider = CopilotChatProvider(
        base_url=COPILOT_CHAT_BASE_URL,
        model="gpt-4o",
        credentials=StaticHeaderSource("cop-1"),
        client=_client(handler),
    )

    with pytest.raises(ProviderStreamError) as raised:
        await _drain(provider, seen)

    assert _SECRET_ISH not in str(raised.value)


def _fragment_chunks(index: int, total: int, *, piece: int = 4_096) -> list[dict[str, Any]]:
    """One call whose arguments arrive `piece` characters at a time."""
    chunks: list[dict[str, Any]] = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": index, "id": f"c{index}", "function": {"name": "get_logs"}}
                        ]
                    }
                }
            ]
        }
    ]
    written = 0
    while written < total:
        step = min(piece, total - written)
        chunks.append(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": index, "function": {"arguments": "x" * step}}]
                        }
                    }
                ]
            }
        )
        written += step
    return chunks


async def test_arguments_that_stop_exactly_at_the_bound_are_kept() -> None:
    events = await _stream(_sse_provider(_sse(*_fragment_chunks(0, MAX_TOOL_ARGUMENT_CHARS))))

    call = next(e for e in events if e["type"] == "tool_call")
    assert len(call["arguments"]) == MAX_TOOL_ARGUMENT_CHARS


async def test_arguments_one_character_past_the_bound_are_refused() -> None:
    seen: list[dict[str, Any]] = []
    provider = _sse_provider(_sse(*_fragment_chunks(0, MAX_TOOL_ARGUMENT_CHARS + 1)))

    with pytest.raises(ProviderStreamLimitError, match="limit"):
        await _drain(provider, seen)

    assert [e["type"] for e in seen] == [REQUEST_SENT]


async def test_two_calls_are_bounded_one_at_a_time() -> None:
    half = MAX_TOOL_ARGUMENT_CHARS // 2
    events = await _stream(
        _sse_provider(_sse(*_fragment_chunks(0, half), *_fragment_chunks(1, half)))
    )

    assert [len(e["arguments"]) for e in events if e["type"] == "tool_call"] == [half, half]


def _opening(index: int) -> dict[str, Any]:
    return {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": index, "id": f"c{index}", "function": {"name": "get_logs"}}
                    ]
                }
            }
        ]
    }


async def test_exactly_the_permitted_number_of_calls_is_kept() -> None:
    events = await _stream(
        _sse_provider(_sse(*(_opening(i) for i in range(MAX_TOOL_CALLS_PER_RESPONSE))))
    )

    assert len([e for e in events if e["type"] == "tool_call"]) == MAX_TOOL_CALLS_PER_RESPONSE


async def test_one_call_too_many_stops_the_stream_before_it_grows() -> None:
    """Sparse indices: the host chooses them, so what is counted is the
    number of accumulators, not the largest index seen."""
    seen: list[dict[str, Any]] = []
    provider = _sse_provider(
        _sse(*(_opening(i * 7) for i in range(MAX_TOOL_CALLS_PER_RESPONSE + 1)))
    )

    with pytest.raises(ProviderStreamLimitError, match="limit"):
        await _drain(provider, seen)

    assert [e["type"] for e in seen] == [REQUEST_SENT]


async def test_more_fragments_for_one_open_call_are_not_new_calls() -> None:
    chunks = [
        _opening(0),
        *(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "x"}}]}}]}
            for _ in range(MAX_TOOL_CALLS_PER_RESPONSE * 4)
        ),
    ]

    events = await _stream(_sse_provider(_sse(*chunks)))

    assert len([e for e in events if e["type"] == "tool_call"]) == 1


async def test_a_chunk_with_no_choices_is_skipped_rather_than_raising() -> None:
    events = await _stream(
        _sse_provider(_sse({"choices": []}, {"id": "chatcmpl-1"}, {"choices": [{"delta": {}}]}))
    )
    assert [e["type"] for e in events] == [REQUEST_SENT, "done"]


async def test_the_request_asks_for_a_stream_that_carries_usage() -> None:
    capture: dict[str, Any] = {}
    tools = [{"type": "function", "function": {"name": "get_logs"}}]
    provider = _sse_provider(_sse({"choices": [{"delta": {"content": "x"}}]}), capture=capture)
    await _stream(provider, tools=tools)
    assert capture["json"]["model"] == "gpt-4o"
    assert capture["json"]["stream"] is True
    assert capture["json"]["stream_options"] == {"include_usage": True}
    assert capture["json"]["tools"] == tools


async def test_a_turn_without_tools_does_not_offer_an_empty_tool_list() -> None:
    """An empty `tools: []` is not the same request as no `tools` key —
    some hosts refuse it."""
    capture: dict[str, Any] = {}
    await _stream(_sse_provider(_sse({"choices": [{"delta": {"content": "x"}}]}), capture=capture))
    assert "tools" not in capture["json"]
