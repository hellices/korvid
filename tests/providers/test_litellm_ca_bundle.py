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
import json
import socket
import socketserver
import ssl
import threading
import weakref
from asyncio.transports import BaseTransport
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, ClassVar

import litellm
import pytest

from korvid.agent.provider import OperatorSafeProviderError
from korvid.core.config import ConnectionAuthConfig, ModelConnectionConfig
from korvid.providers.litellm_factory import create_provider_from_profile
from korvid.providers.litellm_provider import LiteLLMProvider
from tests.providers.litellm_clients import drop_cached_clients
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

    protocol_version = "HTTP/1.1"
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


#: Bounded only so a client that never closes cannot pin a reaper thread.
_REJECTED_HOLD_SECONDS: float = 30.0


def _hold_until_the_client_closes(rejected: ssl.SSLSocket) -> None:
    """Keep a connection whose certificate was refused open until its client goes.

    Disposing of it here would disconnect a client that has not torn its own
    transport down yet, and the Windows proactor finalizes such a transport by
    calling `self._sock.shutdown(SHUT_RDWR)` unguarded (#390). Reading to the
    client's EOF costs this endpoint nothing — it has already refused to serve
    the connection — and leaves the client owning both ends of the teardown.

    The descriptor is never detached, so `rejected` owns it throughout and is
    closed exactly once. Nothing may escape this thread either: pytest turns an
    unhandled thread exception into `PytestUnhandledThreadExceptionWarning`,
    which this suite's `filterwarnings = ["error"]` would raise in whichever
    test happened to be running — the very attribution problem #390 is about.
    """
    try:
        rejected.settimeout(_REJECTED_HOLD_SECONDS)
        # Raw reads: a refused handshake leaves no TLS session to decrypt
        # through, and the bytes are only interesting as a path to EOF.
        while socket.socket.recv(rejected, 4096):
            pass
    except Exception:  # a torn-down connection must not reach threading.excepthook
        pass
    finally:
        with suppress(Exception):
            rejected.close()


class _ChatServer(http.server.ThreadingHTTPServer):
    """Runs the TLS handshake per connection instead of inside `accept()`.

    Wrapping the *listening* socket makes `accept()` hand a refused connection
    to `ssl.SSLSocket._create`, which closes it — the endpoint closes first,
    and `_hold_until_the_client_closes` explains why that is not survivable on
    Windows. Wrapping each accepted socket puts that decision here instead.
    """

    tls: ssl.SSLContext

    def get_request(self) -> tuple[socket.socket, Any]:
        raw, address = self.socket.accept()
        connection = self.tls.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
        try:
            connection.do_handshake()
        except OSError:
            threading.Thread(
                target=_hold_until_the_client_closes, args=(connection,), daemon=True
            ).start()
            raise
        return connection, address


class _UnexpectedHandshakeSocket:
    """A TLS-shaped sole descriptor owner whose handshake resets."""

    def __init__(self, raw: socket.socket) -> None:
        self._socket = socket.socket(fileno=raw.detach())
        self.closed = False

    def do_handshake(self) -> None:
        raise ConnectionResetError("peer reset during handshake")

    def close(self) -> None:
        self.closed = True
        self._socket.close()

    def fileno(self) -> int:
        return self._socket.fileno()


class _UnexpectedHandshakeContext:
    """Builds one observable TLS-shaped owner for the failure-path test."""

    def __init__(self) -> None:
        self.wrapped: _UnexpectedHandshakeSocket | None = None

    def wrap_socket(self, raw: socket.socket, **_: object) -> Any:
        self.wrapped = _UnexpectedHandshakeSocket(raw)
        return self.wrapped


class _TeardownProbeServer:
    """Server-shaped lifecycle probe that cannot leak its test thread."""

    server_address = ("127.0.0.1", 443)

    def __init__(self) -> None:
        self._stop = threading.Event()
        self.finished = threading.Event()
        self.shutdown_called = False
        self.close_called = False

    def serve_forever(self) -> None:
        self._stop.wait()
        self.finished.set()

    def shutdown(self) -> None:
        self.shutdown_called = True
        self._stop.set()

    def server_close(self) -> None:
        self.close_called = True


@asynccontextmanager
async def _https_endpoint(cert_pem: Path, key_pem: Path) -> AsyncIterator[str]:
    """A local HTTPS chat endpoint, served with the minted certificate.

    Teardown is awaited off the event loop. `serve_forever()` runs the
    server-side TLS handshake inline in `accept()`, and the client half of
    that handshake belongs to the loop this context manager is torn down on —
    including the connection closes asyncio only *queues* on the loop instead
    of performing synchronously. Blocking the loop for the length of
    `shutdown()` plus the reader `join()` strands every closure the loop still
    owes, and deadlocks outright when the accept thread is waiting on one.
    """
    server = _ChatServer(("127.0.0.1", 0), _Chat)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert_pem), keyfile=str(key_pem))
    server.tls = context
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        try:
            # The endpoint keeps successful HTTP/1.1 connections alive so
            # the async client owns teardown. Close LiteLLM's cached clients
            # while the server is still serving; their EOF releases the
            # request handlers before shutdown waits for the accept loop.
            await drop_cached_clients()
        finally:
            try:
                await asyncio.to_thread(server.shutdown)
                await asyncio.to_thread(thread.join, 5)
            finally:
                server.server_close()


#: How long a rejected connection is observed for a peer close before its state
#: is taken as settled. A safety bound on a blocking read, not a wait for a
#: result: an endpoint that closes first is observed the instant its FIN lands.
_PEER_SETTLE_SECONDS: float = 0.5


def _port_of(endpoint: str) -> int:
    return int(endpoint.rsplit(":", 1)[1].split("/", 1)[0])


def _reject_the_certificate(port: int) -> ssl.SSLSocket:
    """Fail the handshake the negative-control tests fail, keeping the socket.

    The SDK clients reach the same state through aiohttp; this drives it
    directly so the connection is still available to inspect afterwards. The
    descriptor is never detached, so the returned socket is its only owner and
    the caller closes it exactly once.
    """
    raw = socket.create_connection(("127.0.0.1", port))
    tls = ssl.create_default_context().wrap_socket(
        raw, server_hostname="127.0.0.1", do_handshake_on_connect=False
    )
    rejected = False
    try:
        tls.do_handshake()
    except ssl.SSLCertVerificationError:
        rejected = True
        return tls
    finally:
        if not rejected:
            tls.close()
    raise AssertionError("the throwaway CA was in the system trust store")


async def test_endpoint_closes_server_when_cached_client_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client-cleanup error must not strand the listener or server thread."""
    _ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    server = _TeardownProbeServer()

    async def fail_client_cleanup() -> None:
        raise RuntimeError("client cleanup failed")

    try:
        with monkeypatch.context() as scoped:
            scoped.setitem(globals(), "_ChatServer", lambda *_args, **_kwargs: server)
            scoped.setitem(globals(), "drop_cached_clients", fail_client_cleanup)
            with pytest.raises(RuntimeError, match="client cleanup failed"):
                async with _https_endpoint(cert_pem, key_pem):
                    pass
        assert server.shutdown_called
        assert server.close_called
        assert await asyncio.to_thread(server.finished.wait, 1)
    finally:
        server.shutdown()
        await asyncio.to_thread(server.finished.wait, 1)


def test_chat_server_keeps_request_handlers_off_the_accept_loop() -> None:
    """Idle HTTP/1.1 keep-alive handlers must not block server shutdown."""
    assert issubclass(_ChatServer, http.server.ThreadingHTTPServer)
    assert _ChatServer.daemon_threads is True


def _complete_one_chat_request(port: int, ca_pem: Path) -> ssl.SSLSocket:
    """Complete one trusted HTTP request while retaining the client socket."""
    raw = socket.create_connection(("127.0.0.1", port))
    client = ssl.create_default_context(cafile=str(ca_pem)).wrap_socket(
        raw, server_hostname="127.0.0.1"
    )
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 2\r\n"
        b"Connection: keep-alive\r\n"
        b"\r\n"
        b"{}"
    )
    try:
        client.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = client.recv(4096)
            if not chunk:
                raise AssertionError("the endpoint closed before sending response headers")
            response += chunk
        header, body = response.split(b"\r\n\r\n", 1)
        length_line = next(
            line for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:")
        )
        content_length = int(length_line.split(b":", 1)[1].strip())
        while len(body) < content_length:
            chunk = client.recv(4096)
            if not chunk:
                raise AssertionError("the endpoint closed before sending the response body")
            body += chunk
        assert b" 200 " in header.split(b"\r\n", 1)[0]
        return client
    except BaseException:
        client.close()
        raise


def test_an_unexpected_handshake_failure_closes_the_socket_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, peer = socket.socketpair()
    context = _UnexpectedHandshakeContext()
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(ssl, "create_default_context", lambda: context)

    try:
        with pytest.raises(ConnectionResetError, match="peer reset during handshake"):
            _reject_the_certificate(443)
        assert context.wrapped is not None
        assert context.wrapped.closed
        assert context.wrapped.fileno() == -1
    finally:
        peer.close()
        if context.wrapped is not None and not context.wrapped.closed:
            context.wrapped.close()


def _settle_peer_state(rejected: socket.socket) -> str:
    """Report whether the endpoint closed the rejected connection first.

    Returns `"closed"` when the endpoint's EOF (or reset) arrives, and
    `"open"` when the connection is still the client's to close. The bound is a
    safety net on a blocking read, not a wait for a result: an endpoint that
    closes first is observed the instant its FIN lands.
    """
    rejected.settimeout(_PEER_SETTLE_SECONDS)
    try:
        while socket.socket.recv(rejected, 4096):
            pass
    except TimeoutError:
        return "open"
    except OSError as exc:  # a reset peer has also closed first
        return f"closed (errno {exc.errno})"
    return "closed (EOF)"


async def test_the_endpoint_lets_a_served_client_close_first(tmp_path: Path) -> None:
    """A successful HTTP response must leave the cached client owning teardown."""
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    async with _https_endpoint(cert_pem, key_pem) as endpoint:
        client = await asyncio.to_thread(_complete_one_chat_request, _port_of(endpoint), ca_pem)
        try:
            peer_state = await asyncio.to_thread(_settle_peer_state, client)
        finally:
            client.close()

    assert peer_state == "open", (
        f"the endpoint closed a served connection before its client did ({peer_state})"
    )


async def test_the_endpoint_lets_a_rejecting_client_close_first(tmp_path: Path) -> None:
    """A client that refuses the certificate must still own its own teardown.

    The property asserted is the *ordering*: within the settle window the
    endpoint must not close a connection it refused. That is observable the
    same way on every platform — either its EOF arrives or it does not — and it
    is what #390 turns on.

    What the ordering protects is platform-specific. The Windows proactor
    finalizes a connection by calling `self._sock.shutdown(SHUT_RDWR)` and
    `self._sock.close()` *unguarded* before `self._sock = None` (CPython 3.12.10
    `Lib/asyncio/proactor_events.py::_call_connection_lost`); the selector loop
    makes neither call. Against a peer that closed first those raise, the
    socket is never released, and it surfaces later as #390's
    `PytestUnraisableExceptionWarning` against whichever unrelated test the
    collector happened to reach it in. The resulting errno is *not* the
    criterion here — after an orderly FIN a Linux socket sits in `CLOSE_WAIT`
    where `shutdown()` still succeeds, so asserting on it would pass
    vacuously — it is only reported alongside the ordering it accompanies.

    Measured on this endpoint over 60 rejected handshakes each: closing first
    gave `ENOTCONN` 60/60 on macOS, holding the connection gave a clean
    teardown 60/60. Wrapping the accepted socket more politely changed nothing
    — the close is an orderly FIN either way — so ordering is the property, not
    the manner of the close.
    """
    _ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    async with _https_endpoint(cert_pem, key_pem) as endpoint:
        rejected = await asyncio.to_thread(_reject_the_certificate, _port_of(endpoint))
        try:
            peer_state = await asyncio.to_thread(_settle_peer_state, rejected)
            teardown = "clean"
            try:
                rejected.shutdown(socket.SHUT_RDWR)
            except OSError as exc:
                teardown = f"shutdown failed with errno {exc.errno}"
        finally:
            rejected.close()

    assert peer_state == "open", (
        "the endpoint closed a connection it refused before its client did"
        f" ({peer_state}); the client's own teardown was then {teardown}"
    )


@pytest.fixture(autouse=True)
async def _connection_ownership_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    """Attribute a stranded connection to the test that actually opened it.

    #390's `PytestUnraisableExceptionWarning` names whichever test happened to
    be running when the collector reached the object, never the one that owned
    it, so the allocator has never been identified. Two mechanisms can leave a
    `_ProactorSocketTransport` holding `_sock`, and they need telling apart:

    1. `_call_connection_lost()` raised. On the proactor loop — and only there
       — it calls `self._sock.shutdown(SHUT_RDWR)` and `self._sock.close()`
       unguarded before `self._sock = None` (CPython 3.12.10
       `Lib/asyncio/proactor_events.py`); the selector loop closes the socket
       and clears the attribute with neither call. A peer that reset the
       connection therefore strands the socket on Windows, and the `OSError`
       surfaces only through the loop's exception handler, in the test that
       opened the connection rather than the one that reports the warning.
    2. The queued `_call_connection_lost()` never ran, because the loop was
       closed first. Then no error is raised anywhere and the transport is
       simply still holding its socket when this test is over.

    Case 1 lands in `callback_failures`, case 2 in `stranded`. Both name this
    test. Neither collects garbage, filters a warning, nor waits on a clock.
    """
    live: weakref.WeakSet[BaseTransport] = weakref.WeakSet()
    build_transport = BaseTransport.__init__

    def recording_init(transport: BaseTransport, *args: Any, **kwargs: Any) -> None:
        build_transport(transport, *args, **kwargs)
        live.add(transport)

    monkeypatch.setattr(BaseTransport, "__init__", recording_init)

    callback_failures: list[str] = []
    loop = asyncio.get_running_loop()
    delegate = loop.get_exception_handler()

    def record(failing_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        callback_failures.append(f"{context.get('message')}: {context.get('exception')!r}")
        if delegate is None:
            failing_loop.default_exception_handler(context)
        else:
            delegate(failing_loop, context)

    loop.set_exception_handler(record)
    try:
        yield
    finally:
        loop.set_exception_handler(delegate)

    stranded = sorted(repr(t) for t in live if getattr(t, "_sock", None) is not None)
    assert not callback_failures, (
        f"an event-loop callback failed while this test ran: {callback_failures}"
    )
    assert not stranded, f"a transport still owned its socket after teardown: {stranded}"


@pytest.fixture(autouse=True)
async def _isolated_litellm_trust(
    monkeypatch: pytest.MonkeyPatch, _connection_ownership_probe: None
) -> AsyncIterator[None]:
    """Restore LiteLLM's global trust and drop its cached clients.

    Measured on 1.98.0: the SDK-client cache key is built from the api
    key, the base URL, the timeout and the retry count — the SSL
    configuration takes no part in it, so a client built before the trust
    was applied is handed back afterwards with the old trust still on it.
    korvid applies the bundle at construction, before the first request
    can happen, which is why that ordering is safe in production; a test
    that reuses one process has to flush.

    Requests the ownership probe so the probe is torn down *after* this
    fixture: a client still open is not yet a stranded connection.
    """
    monkeypatch.setattr(litellm, "ssl_verify", True, raising=False)
    await drop_cached_clients()
    _Chat.bodies = []
    yield
    await drop_cached_clients()


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

    await asyncio.wait_for(_drain(), timeout=30)
    return events


async def test_the_endpoint_shuts_its_server_down_off_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`shutdown()` must execute on some other thread than this one.

    It blocks until the accept loop notices the request, and that loop runs
    the server-side TLS handshake inline — so it can be waiting for a client
    close this very event loop has only *queued*. Which thread runs it is the
    property under test: an outcome probe cannot stand in for it, because any
    later `await` in the same teardown drains a queued callback and would hide
    a `shutdown()` that had gone back to blocking the loop.
    """
    ran_on: list[int] = []
    real_shutdown = socketserver.BaseServer.shutdown

    def recording_shutdown(server: socketserver.BaseServer) -> None:
        ran_on.append(threading.get_ident())
        real_shutdown(server)

    monkeypatch.setattr(socketserver.BaseServer, "shutdown", recording_shutdown)

    _ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    async with _https_endpoint(cert_pem, key_pem):
        pass

    assert ran_on, "the endpoint never shut its server down"
    assert threading.get_ident() not in ran_on, (
        "the endpoint shut its server down on its own event-loop thread"
    )


@pytest.mark.parametrize("reference", CLIENT_SHAPES)
async def test_the_configured_bundle_reaches_the_tls_handshake(
    tmp_path: Path, reference: str
) -> None:
    """The whole point: a private-CA endpoint answers because the operator
    named the bundle, not because verification was relaxed."""
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    async with _https_endpoint(cert_pem, key_pem) as endpoint:
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
    async with _https_endpoint(cert_pem, key_pem) as endpoint:
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
    async with _https_endpoint(cert_pem, key_pem) as endpoint:
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
    async with _https_endpoint(cert_pem, key_pem) as endpoint:
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
    async with _https_endpoint(cert_pem, key_pem) as endpoint:
        provider = create_provider_from_profile(
            _profile(reference, endpoint), ca_bundle=str(ca_pem)
        )
        assert isinstance(provider, LiteLLMProvider)
        await _answer(provider)
    assert _Chat.bodies, "the endpoint was never reached"
    assert "ssl_verify" not in _Chat.bodies[0]
    assert str(ca_pem) not in json.dumps(_Chat.bodies[0])
