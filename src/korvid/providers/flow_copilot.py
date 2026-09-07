"""GitHub Copilot: device login and chat transport, declared as data.

The shared transport cannot own this one. Routing `github_copilot/...`
through the SDK starts an interactive device login *inside the routing
call* and writes a credential file korvid does not control, so korvid
claims the prefix on the extension point and runs the login itself:
prompt returned to the wizard, token in korvid's own store.

UNOFFICIAL API: the token exchange uses api.github.com/copilot_internal
and chat uses api.githubcopilot.com — both may change without notice.
Requires an active GitHub Copilot subscription.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx

from korvid.agent.credentials import CredentialSource
from korvid.agent.model_policy import ModelCapabilities, ModelDescriptor
from korvid.agent.model_profiles import (
    AuthMethodDescriptor,
    DeviceLoginPrompt,
    EndpointRequirement,
    ModelConnectionConfig,
    SpecialFlow,
    split_reference,
)
from korvid.agent.provider import (
    MAX_TOOL_ARGUMENT_CHARS,
    REQUEST_SENT,
    STREAM_MALFORMED,
    STREAM_TRUNCATED,
    TIMED_OUT,
    UNREACHABLE,
    LLMProvider,
    ProviderProtocolError,
    ProviderStreamTruncatedError,
    ProviderTransportError,
    append_bounded,
    guard_tool_call_count,
    status_error,
)
from korvid.providers.net import make_client
from korvid.providers.token_store import TokenStore

logger = logging.getLogger(__name__)

#: The prefix an operator writes, and the one this flow declares. The
#: registry folds the SDK's underscore spelling onto it.
PREFIX = "github-copilot"
#: Where the OAuth token lives. Shared with the profile field an operator
#: sees, so a re-login replaces exactly one entry.
CREDENTIAL_KEY = "github-oauth"
AUTH_METHOD = "device-login"

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
    """Device login failed, expired, or the token exchange was rejected."""


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
    """Exchanges a GitHub OAuth token for short-lived chat tokens."""

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


class CopilotChatProvider(LLMProvider):
    """The chat transport this flow owns.

    A claimed reference never reaches the shared transport, so the flow
    has to carry a streaming client of its own. It speaks the chat
    completions dialect the Copilot host publishes.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        credentials: CredentialSource,
        client: httpx.AsyncClient | None = None,
        *,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._credentials = credentials
        self._client = client  # injected or lazily created on first call
        self._owns_client = client is None
        self._timeout_seconds = timeout_seconds

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor(PREFIX, self._model)

    @property
    def capabilities(self) -> ModelCapabilities:
        """Nothing in the profile proves a capability fact.

        The flow is handed a model tag and a stored token, neither of
        which is evidence of a context window, tool support, or tier, so
        every fact stays unknown rather than guessed from the name.
        """
        return ModelCapabilities.unknown()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # No operator bundle: the chat host is a public endpoint, so
            # the default trust store is the right one (issue #168 covers
            # private hosts, which this flow does not talk to).
            self._client = make_client(
                None, timeout=httpx.Timeout(self._timeout_seconds, connect=10.0)
            )
        return self._client

    async def aclose(self) -> None:
        """Close the lazily created client and credentials (injected clients stay open)."""
        try:
            if self._owns_client and self._client is not None:
                await self._client.aclose()
                self._client = None
        finally:
            await self._credentials.aclose()

    def _payload(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
        return payload

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield completion events as an async generator.

        This dialect's terminal marker is on the wire — `data: [DONE]` —
        so its absence is knowable, and it decides whether the answer was
        an answer (issue #336). Reading stops at the first one, and a
        stream that ended without one yields no call, no usage and no
        `done`: text that arrived really was streamed and stays, but a
        truncated response must not be reported as a completed one.

        Raises:
            ProviderStatusError: The host refused the request.
            ProviderProtocolError: The host wrote something this dialect
                cannot read.
            ProviderTransportError: The connection failed or timed out.
            ProviderStreamTruncatedError: The stream ended without `[DONE]`.
            ProviderStreamLimitError: The response passed a cumulative bound.
        """
        # tool_calls[index] = {"id": str, "name": str, "arguments": str}
        tool_acc: dict[int, dict[str, str]] = {}
        answer = _Answer()

        client = self._get_client()
        try:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=self._payload(messages, tools),
                headers=await self._credentials.headers(),
            ) as resp:
                # The request is on the wire: headers came back, so whatever
                # the status says, this provider has the payload (PR #197).
                yield {"type": REQUEST_SENT}
                if resp.status_code >= 300:
                    # The body is read so the connection can be released,
                    # and then discarded: a 401 body quotes the credential
                    # it refused, so only the status class is reported.
                    await resp.aread()
                    raise status_error(resp.status_code)
                async for event in _read_frames(resp, tool_acc, answer):
                    yield event
        except httpx.TimeoutException as exc:
            raise ProviderTransportError(TIMED_OUT) from exc
        except httpx.HTTPError as exc:
            # An `httpx` error carries the request it failed on, headers
            # included, so it is translated rather than re-raised.
            raise ProviderTransportError(UNREACHABLE) from exc

        if not answer.finished:
            raise ProviderStreamTruncatedError(STREAM_TRUNCATED)

        for idx in sorted(tool_acc):
            acc = tool_acc[idx]
            yield {
                "type": "tool_call",
                "id": acc["id"],
                "name": acc["name"],
                "arguments": acc["arguments"],
            }

        # Emit usage only when both component counts are present —
        # defaulting a missing count to 0 would make an incomplete report
        # look exact (the runtime treats any usage event as authoritative).
        usage = answer.usage
        if usage and "prompt_tokens" in usage and "completion_tokens" in usage:
            yield {
                "type": "usage",
                "input_tokens": int(usage["prompt_tokens"]),
                "output_tokens": int(usage["completion_tokens"]),
            }

        yield {"type": "done"}


@dataclass
class _Answer:
    """What the stream said about itself, beside the text it yielded."""

    finished: bool = False
    usage: dict[str, int] | None = None


async def _read_frames(
    resp: httpx.Response,
    tool_acc: dict[int, dict[str, str]],
    answer: _Answer,
) -> AsyncIterator[dict[str, Any]]:
    """Yield this dialect's text deltas, stopping at the first `[DONE]`."""
    async for line in resp.aiter_lines():
        # SSE permits both "data:<value>" and "data: <value>" — strip at
        # most one optional leading space.
        if not line.startswith("data:"):
            continue
        payload_str = line[len("data:") :].removeprefix(" ")
        if payload_str == "[DONE]":
            answer.finished = True
            return

        chunk = _parse_chunk(payload_str)
        raw_usage = chunk.get("usage")
        if raw_usage:
            answer.usage = raw_usage

        text = _chunk_text(chunk, tool_acc)
        if text:
            yield {"type": "text_delta", "text": text}


def _parse_chunk(payload_str: str) -> dict[str, Any]:
    """Read one SSE frame, refusing anything this dialect cannot read.

    The decoder's own message quotes the document it choked on, and this
    body is the one place a leaked credential would come straight back, so
    the refusal carries a written sentence instead.
    """
    try:
        chunk = json.loads(payload_str)
    except ValueError as exc:
        raise ProviderProtocolError(STREAM_MALFORMED) from exc
    if not isinstance(chunk, dict):
        raise ProviderProtocolError(STREAM_MALFORMED)
    return chunk


def _chunk_text(chunk: dict[str, Any], tool_acc: dict[int, dict[str, str]]) -> str | None:
    """Extract one chunk's text delta; fold tool-call fragments into `tool_acc`.

    The fold is bounded (issue #336): a new index is counted before its
    accumulator is created, and arguments go through the shared cumulative
    bound, because both the number of indices and the length of one call's
    arguments are the host's to choose.

    Raises:
        ProviderStreamLimitError: A cumulative bound was passed.
    """
    choices: list[dict[str, Any]] = chunk.get("choices", [])
    if not choices:
        return None

    delta: dict[str, Any] = choices[0].get("delta", {})
    for frag in delta.get("tool_calls") or []:
        idx: int = frag["index"]
        if idx not in tool_acc:
            guard_tool_call_count(len(tool_acc) + 1)
        acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        if frag.get("id"):
            acc["id"] = frag["id"]
        fn: dict[str, str] = frag.get("function", {})
        if fn.get("name"):
            acc["name"] = fn["name"]
        acc["arguments"] = append_bounded(
            acc["arguments"], fn.get("arguments", ""), limit=MAX_TOOL_ARGUMENT_CHARS
        )

    content: str | None = delta.get("content")
    return content or None


class CopilotDeviceLogin:
    """The flow's stateful half: sign in, store, build.

    The wizard drives `begin_auth`/`finish_auth` across two turns, so the
    started device flow has to outlive the first call.
    """

    def __init__(
        self,
        *,
        flow_factory: Callable[[], GitHubDeviceFlow] = GitHubDeviceFlow,
        store: TokenStore | None = None,
    ) -> None:
        self._flow_factory = flow_factory
        self._store = store if store is not None else TokenStore()
        self._pending: GitHubDeviceFlow | None = None
        self._prompt: DeviceCodePrompt | None = None

    async def begin_auth(self, profile: ModelConnectionConfig) -> DeviceLoginPrompt | None:
        """Start a login and return what the operator has to act on.

        Returns `None` when the profile did not ask for a device login:
        starting one anyway would write a credential nobody requested.
        """
        if profile.auth.method != AUTH_METHOD:
            return None
        await self._discard_pending()
        device = self._flow_factory()
        try:
            prompt = await device.start()
        except BaseException:
            await device.aclose()
            raise
        self._pending = device
        self._prompt = prompt
        return DeviceLoginPrompt(
            verification_uri=prompt.verification_uri,
            user_code=prompt.user_code,
            expires_in_seconds=prompt.expires_in,
        )

    async def finish_auth(self, profile: ModelConnectionConfig) -> str | None:
        """Wait for approval, store the token, and name where it went.

        The return value is the credential *key*, never the token: the
        wizard renders it, and a secret there is a secret in the
        operator's scrollback.
        """
        if profile.auth.method != AUTH_METHOD:
            return None
        device, prompt = self._pending, self._prompt
        if device is None or prompt is None:
            raise DeviceLoginError("no device login to finish — begin the login first")
        try:
            token = await device.poll(prompt)
        finally:
            self._pending = None
            self._prompt = None
            await device.aclose()
        self._store.save(CREDENTIAL_KEY, token)
        return f"stored as {CREDENTIAL_KEY}"

    def build_provider(self, profile: ModelConnectionConfig) -> LLMProvider | None:
        """Build the claimed reference's provider, or refuse.

        Every refusal is silent-by-design: the factory turns `None` into
        a disabled agent with the reason it already logged, and a token
        must never be consumed under an auth method the operator did not
        name.

        The chat host is korvid's own constant and never profile data.
        This flow answers `EndpointRequirement.UNSUPPORTED`, so the wizard
        never writes one — but a profile is a YAML file, and honouring a
        hand-written `endpoint` would send a Copilot chat token, minted
        from the stored GitHub OAuth credential, to an address korvid
        never vetted. So a named endpoint is refused *first*, before the
        credential is read and before any client exists.
        """
        if profile.auth.method != AUTH_METHOD:
            return None
        if _names_an_endpoint(profile):
            # Neither the endpoint nor the token is echoed: the token is a
            # credential, and a hand-written URL can carry `user:pass@`.
            logger.warning(
                "a %s profile named an endpoint, which this flow does not support; "
                "refusing it rather than sending a Copilot token to that host — "
                "remove the profile's endpoint field",
                PREFIX,
            )
            return None
        _prefix, tag = split_reference(profile.model)
        if not tag:
            return None
        token = self._store.load(CREDENTIAL_KEY)
        if not token:
            return None
        return CopilotChatProvider(
            base_url=COPILOT_CHAT_BASE_URL,
            model=tag,
            credentials=CopilotCredentialSource(token),
        )

    async def _discard_pending(self) -> None:
        if self._pending is not None:
            await self._pending.aclose()
            self._pending = None
            self._prompt = None


def _names_an_endpoint(profile: ModelConnectionConfig) -> bool:
    """Did the profile actually name a host?

    Blank is what a profile carries for a flow whose endpoint is
    UNSUPPORTED, so only a non-blank value is a refusal. Whitespace is
    stripped: `endpoint: "   "` is not a host, and treating it as one
    once produced a request to the relative URL `/%20%20%20/…`.
    """
    return bool((profile.endpoint or "").strip())


def copilot_flow() -> SpecialFlow:
    """Declare the flow the entry point publishes."""
    login = CopilotDeviceLogin()
    return SpecialFlow(
        prefix=PREFIX,
        display_name="GitHub Copilot",
        auth_methods=(AuthMethodDescriptor(id=AUTH_METHOD, display_name="Sign in with GitHub"),),
        endpoint=EndpointRequirement.UNSUPPORTED,
        build_provider=login.build_provider,
        begin_auth=login.begin_auth,
        finish_auth=login.finish_auth,
    )


@lru_cache(maxsize=1)
def _shared_flow() -> SpecialFlow:
    """One declaration per process.

    The wizard begins a login in one turn and finishes it in the next, so
    both calls have to reach the same `CopilotDeviceLogin`.
    """
    return copilot_flow()


def korvid_special_flows() -> tuple[SpecialFlow, ...]:
    """The extension point's contract: what this module contributes."""
    return (_shared_flow(),)
