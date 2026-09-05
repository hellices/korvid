"""The corporate trust bundle, proved at the seam that actually opens a socket.

`network.ca_bundle` is the operator's statement about which CA korvid may
trust. The legacy provider path honoured it; the profile-native LiteLLM
path has to as well, and the only evidence worth anything here is a real
TLS handshake against a host signed by a CA the system store has never
heard of.

So these tests stand up a local HTTPS server with a throwaway CA and drive
korvid's own provider against it. Both LiteLLM client shapes are covered
deliberately, because they consult *different* settings (measured on
litellm 1.98.0):

- an OpenAI-SDK-shaped provider builds its client through
  `BaseOpenAILLM._get_async_http_client()`, which reads the process-global
  `litellm.ssl_verify` and ignores any per-call value;
- an OpenAI-*like* provider goes through LiteLLM's own httpx handler,
  which reads the per-call `ssl_verify` litellm_param and, failing that,
  the same global.

A change that satisfied only one of them would leave half the providers
talking to a corporate endpoint with the wrong trust store — which is why
korvid applies the global before any client can be built, and passes
nothing per call. The per-call route was measured and rejected here: the
SDK shape ignores it for TLS and then forwards it into the request
*body*, shipping a local filesystem path to the vendor.
"""

from __future__ import annotations

import asyncio
import http.server
import inspect
import json
import ssl
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, ClassVar

import litellm
import pytest

from korvid.agent.provider import OperatorSafeProviderError
from korvid.core.config import ConnectionAuthConfig, ModelConnectionConfig
from korvid.providers.litellm_factory import create_provider_from_profile
from korvid.providers.litellm_provider import LiteLLMProvider
from tests.providers.tls_ca import mint_ca_and_server_cert

#: One reference per LiteLLM client shape. `openai/` is served by the
#: vendor SDK's own client; `hosted_vllm/` by LiteLLM's httpx handler.
CLIENT_SHAPES = ["openai/gpt-4o", "hosted_vllm/qwen"]

_ANSWER: dict[str, Any] = {
    "id": "chatcmpl-korvid-test",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-4o",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class _Chat(http.server.BaseHTTPRequestHandler):
    """Answers any POST with one canned chat completion, recording the body."""

    bodies: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:  # http.server API name
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            type(self).bodies.append(json.loads(raw))
        except ValueError:
            type(self).bodies.append({})
        payload = json.dumps(_ANSWER).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        return None


@contextmanager
def _https_endpoint(cert_pem: Path, key_pem: Path) -> Iterator[str]:
    """A local HTTPS chat endpoint, served with the minted certificate."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Chat)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert_pem), keyfile=str(key_pem))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


async def _drop_cached_clients() -> None:
    """Close and forget every client LiteLLM cached, without leaking one.

    The cache holds live `httpx`/`aiohttp` sessions. Flushing it alone
    drops the last reference to an *open* session, whose finalizer raises
    a `ResourceWarning` that this suite's `filterwarnings = ["error"]`
    turns into a failure in whichever unrelated test happens to be
    running when the collector gets to it.
    """
    cache = getattr(litellm.in_memory_llm_clients_cache, "cache_dict", {})
    for client in list(cache.values()):
        for name in ("aclose", "close"):
            closer = getattr(client, name, None)
            if closer is None:
                continue
            with suppress(Exception):  # a half-built client must not fail a test
                result = closer()
                if inspect.isawaitable(result):
                    await result
            break
    litellm.in_memory_llm_clients_cache.flush_cache()


@pytest.fixture(autouse=True)
async def _isolated_litellm_trust(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Restore LiteLLM's global trust and drop its cached clients.

    Measured on 1.98.0: the SDK-client cache key is built from the api
    key, the base URL, the timeout and the retry count — the SSL
    configuration takes no part in it, so a client built before the trust
    was applied is handed back afterwards with the old trust still on it.
    korvid applies the bundle at construction, before the first request
    can happen, which is why that ordering is safe in production; a test
    that reuses one process has to flush.
    """
    monkeypatch.setattr(litellm, "ssl_verify", True, raising=False)
    await _drop_cached_clients()
    _Chat.bodies = []
    yield
    await _drop_cached_clients()


def _profile(reference: str, endpoint: str, **options: object) -> ModelConnectionConfig:
    return ModelConnectionConfig(
        model=reference,
        endpoint=endpoint,
        auth=ConnectionAuthConfig(method="none"),
        options=dict(options),
    )


async def _answer(provider: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def _drain() -> None:
        async for event in provider.complete([{"role": "user", "content": "hi"}], [], stream=False):
            events.append(event)

    try:
        await asyncio.wait_for(_drain(), timeout=30)
    finally:
        # LiteLLM schedules its own post-call logging work; give it the
        # loop back so nothing is still pending when the loop closes.
        await asyncio.sleep(0)
    return events


@pytest.mark.parametrize("reference", CLIENT_SHAPES)
async def test_the_configured_bundle_reaches_the_tls_handshake(
    tmp_path: Path, reference: str
) -> None:
    """The whole point: a private-CA endpoint answers because the operator
    named the bundle, not because verification was relaxed."""
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    with _https_endpoint(cert_pem, key_pem) as endpoint:
        provider = create_provider_from_profile(
            _profile(reference, endpoint), ca_bundle=str(ca_pem)
        )
        assert isinstance(provider, LiteLLMProvider)
        events = await _answer(provider)
    assert {"type": "text_delta", "text": "hi"} in events


@pytest.mark.parametrize("reference", CLIENT_SHAPES)
async def test_without_the_bundle_the_same_endpoint_is_unreachable(
    tmp_path: Path, reference: str
) -> None:
    """The negative control. Without it the test above could pass against
    an endpoint korvid trusted for some other reason."""
    _ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    with _https_endpoint(cert_pem, key_pem) as endpoint:
        provider = create_provider_from_profile(_profile(reference, endpoint), ca_bundle=None)
        assert isinstance(provider, LiteLLMProvider)
        with pytest.raises(OperatorSafeProviderError):
            await _answer(provider)


@pytest.mark.parametrize("reference", CLIENT_SHAPES)
async def test_a_profile_option_can_never_turn_verification_off(
    tmp_path: Path, reference: str
) -> None:
    """`ssl_verify: false` in a profile's options is a request to talk to
    a corporate endpoint with no verification at all.

    LiteLLM's httpx handlers do honour that key, so this is the one
    profile option that could downgrade korvid's TLS from config. It is
    korvid's transport setting, so it is dropped: the untrusted endpoint
    is still refused, exactly as it is with no option at all.
    """
    _ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    with _https_endpoint(cert_pem, key_pem) as endpoint:
        provider = create_provider_from_profile(
            _profile(reference, endpoint, ssl_verify=False), ca_bundle=None
        )
        assert isinstance(provider, LiteLLMProvider)
        with pytest.raises(OperatorSafeProviderError):
            await _answer(provider)
    assert _Chat.bodies == [], "the handshake must fail before any request is sent"


async def test_the_bundle_still_applies_when_an_option_asks_to_ignore_it(
    tmp_path: Path,
) -> None:
    """The other half of the same rule: with a bundle configured, the
    option changes nothing and the private-CA endpoint answers."""
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    with _https_endpoint(cert_pem, key_pem) as endpoint:
        provider = create_provider_from_profile(
            _profile("openai/gpt-4o", endpoint, ssl_verify=False), ca_bundle=str(ca_pem)
        )
        assert isinstance(provider, LiteLLMProvider)
        events = await _answer(provider)
    assert {"type": "text_delta", "text": "hi"} in events
    assert "ssl_verify" not in _Chat.bodies[0]


@pytest.mark.parametrize("reference", CLIENT_SHAPES)
async def test_the_bundle_is_a_transport_setting_not_a_model_parameter(
    tmp_path: Path, reference: str
) -> None:
    """It configures korvid's client. It must not travel in the request
    body, where a provider would reject it as an unknown field."""
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    with _https_endpoint(cert_pem, key_pem) as endpoint:
        provider = create_provider_from_profile(
            _profile(reference, endpoint), ca_bundle=str(ca_pem)
        )
        assert isinstance(provider, LiteLLMProvider)
        await _answer(provider)
    assert _Chat.bodies, "the endpoint was never reached"
    assert "ssl_verify" not in _Chat.bodies[0]
    assert str(ca_pem) not in json.dumps(_Chat.bodies[0])
