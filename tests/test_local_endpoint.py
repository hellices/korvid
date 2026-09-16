"""The lifecycle every local test endpoint in this suite is built on.

#390 is a socket-ownership bug, not an HTTP bug: a local endpoint that
closes an accepted connection before its client has released the client's
own transport strands that transport on the Windows proactor, which
finalizes it by calling `self._sock.shutdown(SHUT_RDWR)` and
`self._sock.close()` unguarded (CPython `Lib/asyncio/proactor_events.py`
`_call_connection_lost`). The resulting `OSError` surfaces later as a
`PytestUnraisableExceptionWarning` against whichever unrelated test the
collector happened to reach it in.

`tests/local_endpoint.py` is where that ordering is decided once. These
tests pin the two ownership contracts it offers:

- a *served* endpoint answers with HTTP/1.1 and keep-alive, and never
  closes a connection its client still owns;
- a *disconnecting* endpoint half-closes its write side so the client
  observes the EOF a dropped remote produces, yet keeps the accepted peer
  open — and reading — until the client closes.

Everything asserted here is observable from a real socket and bounded by
an event, never by a sleep, a retry, or a warning filter.
"""

from __future__ import annotations

import asyncio
import os
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from typing import Any

import pytest

from tests import local_endpoint
from tests.local_endpoint import (
    ENDPOINT_THREAD_PREFIX,
    KeepAliveHandler,
    LocalEndpoint,
    disconnecting_endpoint,
    drain_transport_closures,
    served_endpoint,
)
from tests.providers.tls_ca import mint_ca_and_server_cert

_HOST = "127.0.0.1"

#: How long a connection is watched for a peer close before its state is
#: taken as settled. A safety bound on a blocking read, not a wait for a
#: result: an endpoint that closes first is observed the instant its FIN
#: lands.
_PEER_SETTLE_SECONDS = 0.5

#: The bound on a client's own connect, handshake, and request — and on
#: every wait that is only here so a *failing* assertion cannot hang. It is
#: deliberately far larger than anything loopback needs, because a busy CI
#: runner is slow and this bound must never decide a passing test.
_CONNECT_SECONDS = 10.0

#: The bound a *negative* lifecycle test spends proving the endpoint gives
#: up on a client that never released. Short because the test's subject is
#: the bound itself.
_IMPATIENT_SECONDS = 0.25

#: How long the stalled-handshake child process may take to exit. Reaching
#: it means teardown never returned, which is the failure being tested.
_CHILD_SECONDS = 120.0

#: The stalled-handshake lifecycle, run in a child interpreter by
#: `run_stalled_handshake`. The client opens a TCP connection to a TLS
#: endpoint and never sends a ClientHello, so the accept loop is inside
#: `do_handshake()` when teardown begins. Each marker it prints is a claim
#: the parent asserts; the child exiting at all is the largest of them,
#: because a teardown thread that cannot be joined keeps the interpreter
#: alive past `asyncio.run()`.
_STALLED_HANDSHAKE_PROGRAM = """
import asyncio
import socket
import sys
from pathlib import Path

from tests.local_endpoint import KeepAliveHandler, served_endpoint

HOST = "127.0.0.1"
BOUND = 10.0


async def main() -> None:
    certificate, key = Path(sys.argv[1]), Path(sys.argv[2])
    silent = None
    try:
        try:
            async with served_endpoint(
                KeepAliveHandler, tls=(certificate, key), settle_seconds=0.25
            ) as endpoint:
                silent = socket.create_connection((HOST, endpoint.port), timeout=BOUND)
                if not await asyncio.to_thread(endpoint.wait_until_tracked, 1, BOUND):
                    raise RuntimeError("the endpoint never tracked the silent client")
                print("tracked", flush=True)
        except AssertionError:
            print("unsettled", flush=True)  # the silent client never released it
        print("retired", flush=True)
        silent.settimeout(BOUND)
        try:
            closed = socket.socket.recv(silent, 4096) == b""
        except OSError:
            closed = True
        print("closed=" + str(closed), flush=True)
    finally:
        if silent is not None:
            silent.close()


asyncio.run(main())
"""


class JsonHandler(KeepAliveHandler):
    """Answers every GET with one keep-alive JSON response."""

    def do_GET(self) -> None:  # http.server API name
        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class LegacyHandler(KeepAliveHandler):
    """A handler that forgot it has to keep the connection alive."""

    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:  # http.server API name
        self.send_response(200)
        self.end_headers()


#: The response a handler that announces its own close writes, byte for
#: byte. A transport test asserts on exactly these bytes, so they are
#: written straight to `wfile`: `send_response` would add headers of its
#: own.
RAW_CLOSE_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"


class DisconnectHandler(KeepAliveHandler):
    """Answers once and drops the connection, the way a remote peer does."""

    def do_GET(self) -> None:  # http.server API name
        self.close_connection = True
        self.wfile.write(RAW_CLOSE_RESPONSE)


class SilentHandler(BaseHTTPRequestHandler):
    """Answers nothing at all, the way a remote that dropped mid-request does.

    Deliberately not a `KeepAliveHandler`: this is the handler shape a
    transport test writes when the subject is the client's retry budget,
    and the answer that never comes is the point.
    """

    def do_GET(self) -> None:  # http.server API name
        self.close_connection = True


class RawCloseHandler(BaseHTTPRequestHandler):
    """Writes its own response bytes, and announces the close itself.

    `DisconnectHandler` writes the same bytes from a keep-alive base; this
    one is the plain `http.server` handler a transport fixture builds, so
    what a disconnecting endpoint owes the two bases is the same claim.
    """

    def do_GET(self) -> None:  # http.server API name
        self.close_connection = True
        self.wfile.write(RAW_CLOSE_RESPONSE)


class StallingHandler(KeepAliveHandler):
    """Stays inside its handler until the test releases it.

    The events are class attributes because `http.server` constructs a
    handler per request; every test that uses this handler clears them
    first and releases them in its own `finally`.
    """

    started = threading.Event()
    released = threading.Event()
    finished = threading.Event()

    def do_GET(self) -> None:  # http.server API name
        StallingHandler.started.set()
        try:
            StallingHandler.released.wait(_CONNECT_SECONDS)
        finally:
            StallingHandler.finished.set()


class ExplodingHandler(KeepAliveHandler):
    """Fails the way a broken handler fails: with an exception, mid-request."""

    def do_GET(self) -> None:  # http.server API name
        raise RuntimeError("the handler exploded")


class FailOnceContext(ssl.SSLContext):
    """A server context whose first `wrap_socket` raises a non-`OSError`.

    `socketserver` drops an `OSError` from `get_request()` and keeps
    accepting; anything else escapes `serve_forever` and kills the accept
    loop, which is the failure this context provokes.
    """

    failures = 1

    def wrap_socket(self, sock: socket.socket, *args: Any, **kwargs: Any) -> ssl.SSLSocket:
        if self.failures:
            self.failures -= 1
            raise ValueError("no TLS for you")
        return super().wrap_socket(sock, *args, **kwargs)


def failing_once_context(tls: tuple[Path, Path] | None) -> ssl.SSLContext | None:
    """Stand in for the endpoint's own server context, failing once."""
    assert tls is not None
    certificate, key = tls
    context = FailOnceContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(certificate), keyfile=str(key))
    return context


@pytest.fixture(autouse=True)
def no_thread_outlives_the_test() -> Iterator[None]:
    """Fail the test that leaves a background thread of its own behind.

    The endpoint's threads are daemons and the executor's are not, so the
    survivors are found by comparing `threading.enumerate()` against the
    identities that were alive before the test — never by trusting the
    endpoint to have named its own threads.
    """
    before = {thread.ident for thread in threading.enumerate()}
    yield
    newcomers = [
        thread for thread in threading.enumerate() if thread.ident not in before and thread.daemon
    ]
    for thread in newcomers:
        # Bounded, so a thread that is already finishing is not a failure
        # and a genuinely leaked one still is.
        thread.join(_CONNECT_SECONDS)
    assert [thread.name for thread in newcomers if thread.is_alive()] == []


@contextmanager
def raw_endpoint(
    *,
    half_close: bool = False,
    tls: ssl.SSLContext | None = None,
    handshake_seconds: float = _CONNECT_SECONDS,
) -> Iterator[local_endpoint._EndpointServer]:
    """A bare endpoint server, retired and closed when the test is done.

    Retirement is a teardown state the public context manager never hands
    out mid-flight, and its handshake bound is derived from
    `settle_seconds` rather than chosen, so the claims about *what an
    endpoint refuses* and *what it gives up on* are made against the
    server object directly.

    The server and its accept loop still come from `tests/local_endpoint.py`:
    a test that built its own would be the seventh hand-written endpoint,
    which is the drift #390 closed.
    """
    with local_endpoint._unmanaged_endpoint(
        JsonHandler,
        tls=tls,
        half_close=half_close,
        handshake_seconds=handshake_seconds,
        join_seconds=_CONNECT_SECONDS,
    ) as server:
        yield server


def client_context(cafile: Path) -> ssl.SSLContext:
    """A verifying client context with the same TLS floor as the endpoint."""
    context = ssl.create_default_context(cafile=str(cafile))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def content_length(header: bytes) -> int:
    for line in header.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            return int(line.split(b":", 1)[1])
    raise AssertionError(f"the endpoint answered without a content-length: {header!r}")


def read_response(client: socket.socket) -> bytes:
    """Read one complete response, returning its header block."""
    received = b""
    while b"\r\n\r\n" not in received:
        chunk = client.recv(4096)
        if not chunk:
            raise AssertionError("the endpoint closed before sending response headers")
        received += chunk
    header, body = received.split(b"\r\n\r\n", 1)
    while len(body) < content_length(header):
        chunk = client.recv(4096)
        if not chunk:
            raise AssertionError("the endpoint closed before sending the response body")
        body += chunk
    return header


def open_keepalive_request(port: int, *, cafile: Path | None = None) -> socket.socket:
    """Complete one HTTP/1.1 request and keep the client socket open.

    The descriptor is never detached from the returned socket, so the
    caller owns it and closes it exactly once.
    """
    raw = socket.create_connection((_HOST, port), timeout=_CONNECT_SECONDS)
    client: socket.socket = (
        raw if cafile is None else client_context(cafile).wrap_socket(raw, server_hostname=_HOST)
    )
    try:
        client.sendall(
            b"GET /api.json HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n"
        )
        header = read_response(client)
        assert b" 200 " in header.split(b"\r\n", 1)[0]
        return client
    except BaseException:
        client.close()
        raise


def request_until_eof(port: int) -> tuple[socket.socket, bytes]:
    """Send one request and read to the endpoint's EOF, keeping the socket."""
    client = socket.create_connection((_HOST, port), timeout=_CONNECT_SECONDS)
    try:
        client.sendall(b"GET /drop HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        received = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                return client, received
            received += chunk
    except BaseException:
        client.close()
        raise


def refuse_certificate(port: int) -> ssl.SSLSocket:
    """Fail the handshake a client with the wrong trust store fails."""
    raw = socket.create_connection((_HOST, port), timeout=_CONNECT_SECONDS)
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    rejecting = context.wrap_socket(raw, server_hostname=_HOST, do_handshake_on_connect=False)
    refused = False
    try:
        rejecting.do_handshake()
    except ssl.SSLCertVerificationError:
        refused = True
        return rejecting
    finally:
        if not refused:
            rejecting.close()
    raise AssertionError("the throwaway CA was in the system trust store")


def peer_state(client: socket.socket) -> str:
    """Report whether the endpoint closed this connection before its client.

    Returns `"open"` while the connection is still the client's to close,
    and a `"closed ..."` description once the endpoint's EOF or reset
    arrives. The bound is a safety net on a blocking read: an endpoint
    that closes first is observed the instant its FIN lands.
    """
    client.settimeout(_PEER_SETTLE_SECONDS)
    try:
        while socket.socket.recv(client, 4096):
            pass
    except TimeoutError:
        return "open"
    except OSError as exc:
        return f"closed (errno {exc.errno})"
    return "closed (EOF)"


def live_endpoint_threads(port: int) -> list[threading.Thread]:
    """The threads this endpoint still owns; it names each one with its port."""
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(f"{ENDPOINT_THREAD_PREFIX}-") and thread.name.endswith(f"-{port}")
    ]


def endpoint_threads(port: int) -> list[str]:
    """Name the threads this endpoint still owns; it names each one with its port."""
    return sorted(thread.name for thread in live_endpoint_threads(port))


def listener_refused(port: int) -> bool:
    """Whether the endpoint's listening socket is gone."""
    try:
        leftover = socket.create_connection((_HOST, port), timeout=_CONNECT_SECONDS)
    except OSError:
        return True
    leftover.close()
    return False


def connect_without_speaking(port: int) -> socket.socket:
    """Open a TCP connection and say nothing — no request, no ClientHello."""
    return socket.create_connection((_HOST, port), timeout=_CONNECT_SECONDS)


def send_request_only(port: int) -> socket.socket:
    """Send one request without reading the answer, keeping the socket."""
    client = socket.create_connection((_HOST, port), timeout=_CONNECT_SECONDS)
    try:
        client.sendall(b"GET /api.json HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    except BaseException:
        client.close()
        raise
    return client


def peer_closed_within(client: socket.socket, timeout: float) -> bool:
    """Whether the endpoint closed this connection, waiting at most `timeout`.

    The bound only exists so a *failing* claim reports instead of hanging:
    a closed peer is observed the instant its FIN or reset lands.
    """
    client.settimeout(timeout)
    try:
        while socket.socket.recv(client, 4096):
            pass
    except TimeoutError:
        return False
    except OSError:
        return True
    return True


def connect_after_signal(
    port: int,
    start: threading.Event,
    finished: threading.Event,
    opened: list[socket.socket],
    peer_closed: list[bool],
    errors: list[Exception],
) -> None:
    """Open one connection after `start` and record how the endpoint treats it."""
    try:
        if not start.wait(_CONNECT_SECONDS):
            raise TimeoutError("teardown never began")
        client = socket.create_connection((_HOST, port), timeout=_CONNECT_SECONDS)
        opened.append(client)
        peer_closed.append(peer_closed_within(client, _CONNECT_SECONDS))
    except Exception as error:
        errors.append(error)
    finally:
        finished.set()


def run_stalled_handshake(certificate: Path, key: Path) -> subprocess.CompletedProcess[str]:
    """Run the stalled-handshake lifecycle in a child that has to exit.

    A child, because the claim is that teardown *returns* — and that the
    threads it used are joinable, so the interpreter can shut down. Inside
    this process a teardown that never returns would hang the whole run
    instead of failing; `timeout` turns that into a reported failure.
    """
    root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, "-c", _STALLED_HANDSHAKE_PROGRAM, str(certificate), str(key)],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=_CHILD_SECONDS,
        check=False,
    )


async def abandon_one_client(
    opened: list[socket.socket],
    ports: list[int],
    *,
    release_clients: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Leave one served connection open when the endpoint context exits.

    The client is handed back through `opened` rather than returned,
    because the endpoint is expected to raise on the way out.
    """
    async with served_endpoint(
        JsonHandler, release_clients=release_clients, settle_seconds=_IMPATIENT_SECONDS
    ) as endpoint:
        ports.append(endpoint.port)
        opened.append(await asyncio.to_thread(open_keepalive_request, endpoint.port))


def schedule_after_loop_turns(
    loop: asyncio.AbstractEventLoop, turns: int, finish: Callable[[], None]
) -> None:
    """Run `finish` only after the loop has taken `turns` more turns.

    `drain_transport_closures()` yields a fixed, small number of turns, so
    a deep chain cannot complete inside it. It completes only while the
    endpoint's teardown leaves the loop free — which is the property
    under test.
    """
    if turns == 0:
        finish()
        return
    loop.call_soon(schedule_after_loop_turns, loop, turns - 1, finish)


async def test_the_endpoint_path_joins_suffixes_onto_its_base_url() -> None:
    async with served_endpoint(JsonHandler) as endpoint:
        assert endpoint.url == f"http://{_HOST}:{endpoint.port}"
        assert endpoint.path("/v1") == f"{endpoint.url}/v1"
        assert endpoint.path("api.json") == f"{endpoint.url}/api.json"


def test_the_keep_alive_handler_serves_http_1_1_without_logging(
    capsys: pytest.CaptureFixture[str],
) -> None:
    KeepAliveHandler.log_message(None, "%s answered", "handler")  # type: ignore[arg-type]  # class-level call: no connection is needed to prove silence.
    captured = capsys.readouterr()

    assert KeepAliveHandler.protocol_version == "HTTP/1.1"
    assert captured.err == ""
    assert captured.out == ""


async def test_served_endpoint_leaves_the_connection_open_for_the_client() -> None:
    async with served_endpoint(JsonHandler) as endpoint:
        client = await asyncio.to_thread(open_keepalive_request, endpoint.port)
        try:
            assert endpoint.open_connections() == 1
            assert await asyncio.to_thread(peer_state, client) == "open"
        finally:
            client.close()


async def test_idle_keep_alive_connections_do_not_block_teardown() -> None:
    async with served_endpoint(JsonHandler) as endpoint:
        clients = [await asyncio.to_thread(open_keepalive_request, endpoint.port) for _ in range(3)]
        try:
            assert endpoint.open_connections() == 3
        finally:
            for client in clients:
                client.close()


async def test_a_handler_runs_on_a_daemon_thread_off_the_accept_loop() -> None:
    """A handler mid-request must never be able to hold the interpreter open.

    `_EndpointServer.daemon_threads` is the whole of that guarantee, and
    nothing else in this suite would notice it flipping: `process_request` is
    overridden, so `ThreadingMixIn._threads` stays empty and `server_close()`
    never joins, and the autouse survivor guard only looks at threads that are
    already daemons. So the flag is pinned here, on the live thread that is
    currently carrying a request — a non-daemon handler that outlived its
    endpoint would hang interpreter shutdown instead of failing a test.
    """
    StallingHandler.started.clear()
    StallingHandler.released.clear()
    StallingHandler.finished.clear()
    async with served_endpoint(StallingHandler) as endpoint:
        client = await asyncio.to_thread(send_request_only, endpoint.port)
        try:
            assert await asyncio.to_thread(StallingHandler.started.wait, _CONNECT_SECONDS)
            live = live_endpoint_threads(endpoint.port)
            handlers = [thread for thread in live if "-handler-" in thread.name]
            assert [(thread.name, thread.daemon) for thread in handlers] == [
                (f"{ENDPOINT_THREAD_PREFIX}-handler-{endpoint.port}", True)
            ]
            # The accept loop is a separate live thread, so the handler stalled
            # above is demonstrably running off it rather than inside it.
            accepting = [thread for thread in live if "-accept-" in thread.name]
            assert [thread.is_alive() for thread in accepting] == [True]
        finally:
            StallingHandler.released.set()
            assert await asyncio.to_thread(StallingHandler.finished.wait, _CONNECT_SECONDS)
            client.close()


async def test_served_endpoint_reports_a_connection_its_client_never_released() -> None:
    opened: list[socket.socket] = []
    ports: list[int] = []
    try:
        with pytest.raises(AssertionError, match="still held 1 connection"):
            await abandon_one_client(opened, ports)

        [client], [port] = opened, ports
        assert await asyncio.to_thread(peer_state, client) != "open"
        assert await asyncio.to_thread(listener_refused, port)
    finally:
        for client in opened:
            client.close()


async def test_release_failure_still_closes_the_listener_and_its_connections() -> None:
    async def fail_release() -> None:
        raise RuntimeError("release failed")

    opened: list[socket.socket] = []
    ports: list[int] = []
    try:
        with pytest.raises(RuntimeError, match="release failed"):
            await abandon_one_client(opened, ports, release_clients=fail_release)

        [client], [port] = opened, ports
        assert await asyncio.to_thread(peer_state, client) != "open"
        assert await asyncio.to_thread(listener_refused, port)
    finally:
        for client in opened:
            client.close()


async def test_a_body_failure_stays_primary_and_records_the_release_failure() -> None:
    async def fail_release() -> None:
        raise RuntimeError("release failed")

    with pytest.raises(ValueError, match="the body failed") as caught:
        async with served_endpoint(JsonHandler, release_clients=fail_release):
            raise ValueError("the body failed")

    notes = getattr(caught.value, "__notes__", [])
    assert any("release failed" in note for note in notes), notes


async def test_the_release_callback_runs_while_the_endpoint_still_serves() -> None:
    """Clients are released first, so their EOF frees the handlers shutdown waits on."""
    live: list[LocalEndpoint] = []
    observed: list[int] = []

    async def release() -> None:
        [endpoint] = live
        client = await asyncio.to_thread(open_keepalive_request, endpoint.port)
        try:
            observed.append(endpoint.open_connections())
        finally:
            client.close()

    async with served_endpoint(JsonHandler, release_clients=release) as endpoint:
        live.append(endpoint)

    assert observed == [1]


def test_served_endpoint_refuses_a_handler_that_is_not_http_1_1() -> None:
    with pytest.raises(ValueError, match=r"HTTP/1\.1"):
        served_endpoint(LegacyHandler)


async def test_teardown_leaves_the_event_loop_free_to_close_clients() -> None:
    loop = asyncio.get_running_loop()
    async with served_endpoint(JsonHandler) as endpoint:
        reader, writer = await asyncio.open_connection(_HOST, endpoint.port)
        writer.write(b"GET /api.json HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        header = await reader.readuntil(b"\r\n\r\n")
        await reader.readexactly(content_length(header))
        assert b" 200 " in header.split(b"\r\n", 1)[0]
        # A loop blocked by teardown cannot reach this close, and the
        # endpoint then fails on the connection nobody released.
        schedule_after_loop_turns(loop, 64, writer.close)

    await writer.wait_closed()


async def test_teardown_shuts_the_server_down_off_the_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`shutdown()` must run on some thread other than the loop's.

    It blocks until the accept loop notices the request, and that loop can
    be inside a TLS handshake whose client half belongs to this very event
    loop — including the closes asyncio has only *queued* on it. Which
    thread runs it is the property under test, because an outcome probe
    cannot stand in for it: any later `await` in the same teardown drains a
    queued callback and would hide a `shutdown()` that had gone back to
    blocking the loop.
    """
    ran_on: list[int] = []
    real_shutdown = socketserver.BaseServer.shutdown

    def recording_shutdown(server: socketserver.BaseServer) -> None:
        ran_on.append(threading.get_ident())
        real_shutdown(server)

    monkeypatch.setattr(socketserver.BaseServer, "shutdown", recording_shutdown)

    async with served_endpoint(JsonHandler):
        pass

    assert ran_on, "teardown never shut the endpoint's server down"
    assert threading.get_ident() not in ran_on, (
        "teardown shut the endpoint's server down on its own event-loop thread"
    )


async def test_drain_transport_closures_runs_chained_loop_callbacks() -> None:
    loop = asyncio.get_running_loop()
    ran: list[str] = []

    def queued() -> None:
        ran.append("queued")

    def closing() -> None:
        ran.append("closing")
        loop.call_soon(queued)

    loop.call_soon(closing)
    await drain_transport_closures()

    assert ran == ["closing", "queued"]


async def test_a_served_tls_client_keeps_the_connection_it_opened(tmp_path: Path) -> None:
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    async with served_endpoint(JsonHandler, tls=(cert_pem, key_pem)) as endpoint:
        assert endpoint.url.startswith("https://")
        client = await asyncio.to_thread(open_keepalive_request, endpoint.port, cafile=ca_pem)
        try:
            state = await asyncio.to_thread(peer_state, client)
        finally:
            client.close()

    assert state == "open", f"the endpoint closed a served connection first ({state})"


async def test_a_refused_tls_handshake_leaves_the_client_owning_teardown(tmp_path: Path) -> None:
    _ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    async with served_endpoint(JsonHandler, tls=(cert_pem, key_pem)) as endpoint:
        rejecting = await asyncio.to_thread(refuse_certificate, endpoint.port)
        try:
            state = await asyncio.to_thread(peer_state, rejecting)
            teardown = "clean"
            try:
                rejecting.shutdown(socket.SHUT_RDWR)
            except OSError as exc:
                teardown = f"shutdown failed with errno {exc.errno}"
        finally:
            rejecting.close()

    assert state == "open", (
        "the endpoint closed a connection it refused before its client did"
        f" ({state}); the client's own teardown was then {teardown}"
    )


async def test_a_disconnecting_endpoint_keeps_the_peer_the_client_saw_eof_from() -> None:
    """The EOF is the endpoint's write side only; the peer is still the client's.

    The client's own `shutdown()` result is deliberately not asserted: a
    connection that has received a FIN sits in `CLOSE_WAIT` on Linux,
    where `shutdown()` succeeds, and reports `ENOTCONN` on macOS, so it
    describes the platform rather than the ownership. What is portable —
    and what #390 turns on — is that the endpoint has not disposed of the
    peer the client's transport is still attached to.
    """
    async with disconnecting_endpoint(
        DisconnectHandler, reason="the retry budget is the subject"
    ) as endpoint:
        client, received = await asyncio.to_thread(request_until_eof, endpoint.port)
        try:
            assert b" 200 " in received.split(b"\r\n", 1)[0]
            assert client.fileno() != -1
            held = endpoint.open_connections()
        finally:
            client.close()

    assert held == 1, "the endpoint disposed of a peer whose client had not closed yet"


async def test_a_disconnect_holder_has_no_wall_clock_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only client EOF or endpoint retirement may release a held peer."""
    observed: Queue[str] = Queue()
    real_is_retiring = local_endpoint._EndpointServer._is_retiring
    real_close_request = local_endpoint._EndpointServer.close_request

    def recording_is_retiring(server: local_endpoint._EndpointServer) -> bool:
        observed.put("checked retirement")
        return real_is_retiring(server)

    def recording_close_request(server: local_endpoint._EndpointServer, request: Any) -> None:
        observed.put("closed peer")
        real_close_request(server, request)

    async with disconnecting_endpoint(
        DisconnectHandler, reason="the retry budget is the subject"
    ) as endpoint:
        client: socket.socket | None = None
        try:
            ticks = iter((0.0, 10_000.0))
            accelerated_time = SimpleNamespace(monotonic=lambda: next(ticks, 10_000.0))
            with monkeypatch.context() as patch:
                patch.setattr(local_endpoint, "time", accelerated_time)
                patch.setattr(
                    local_endpoint._EndpointServer,
                    "_is_retiring",
                    recording_is_retiring,
                )
                patch.setattr(
                    local_endpoint._EndpointServer,
                    "close_request",
                    recording_close_request,
                )
                client, _received = await asyncio.to_thread(request_until_eof, endpoint.port)
                first_event = await asyncio.to_thread(observed.get, True, _CONNECT_SECONDS)
                assert first_event == "checked retirement"
                assert endpoint.open_connections() == 1
        finally:
            if client is not None:
                client.close()


@pytest.mark.parametrize(
    ("handler", "answer"),
    [
        pytest.param(SilentHandler, b"", id="no-response"),
        pytest.param(RawCloseHandler, RAW_CLOSE_RESPONSE, id="connection-close"),
    ],
)
async def test_an_intentional_disconnect_leaves_the_endpoint_holding_nothing(
    handler: type[BaseHTTPRequestHandler], answer: bytes
) -> None:
    """A raw responder's disconnect is the write side only, and it settles.

    Both shapes a transport test writes are covered: the handler that
    answers nothing, and the one that writes its own bytes and announces
    `Connection: close`. Either way the client reads the EOF a dropped
    remote produces while the accepted peer stays the client's to close —
    and once it has closed, the endpoint is left holding no socket and
    running no thread of its own.
    """
    async with disconnecting_endpoint(
        handler, reason="the retry budget is the subject"
    ) as endpoint:
        client, received = await asyncio.to_thread(request_until_eof, endpoint.port)
        try:
            assert received == answer
            assert client.fileno() != -1
            held = endpoint.open_connections()
        finally:
            client.close()

    assert held == 1, "the endpoint disposed of a peer whose client had not closed yet"
    # Read once teardown has settled: the endpoint half-closes before it starts
    # the holder, so a client that has just seen EOF can still outrun the count.
    assert endpoint.activity().holders_started == 1, (
        "the endpoint never held the peer it had half-closed"
    )
    assert endpoint.open_connections() == 0
    assert endpoint_threads(endpoint.port) == []


def test_a_disconnecting_endpoint_requires_a_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        disconnecting_endpoint(DisconnectHandler, reason="   ")


async def test_a_disconnecting_endpoint_names_its_reason_when_a_client_stays() -> None:
    client: socket.socket | None = None
    try:
        with pytest.raises(AssertionError, match="the retry budget is the subject"):
            async with disconnecting_endpoint(
                DisconnectHandler,
                reason="the retry budget is the subject",
                settle_seconds=_IMPATIENT_SECONDS,
            ) as endpoint:
                client, _received = await asyncio.to_thread(request_until_eof, endpoint.port)

        assert client is not None
    finally:
        if client is not None:
            client.close()


async def stall_a_tls_handshake(
    tls: tuple[Path, Path], opened: list[socket.socket], live: list[LocalEndpoint]
) -> None:
    """Open a TLS connection that never sends a ClientHello, and leave it open."""
    async with served_endpoint(JsonHandler, tls=tls, settle_seconds=_IMPATIENT_SECONDS) as endpoint:
        live.append(endpoint)
        opened.append(await asyncio.to_thread(connect_without_speaking, endpoint.port))
        assert await asyncio.to_thread(endpoint.wait_until_tracked, 1, _CONNECT_SECONDS)


async def test_a_tls_peer_is_tracked_before_its_handshake(tmp_path: Path) -> None:
    """A client that opens the socket and says nothing is still the endpoint's to close.

    Until the peer is registered, nothing in teardown can reach the socket
    the accept loop is blocked on inside `do_handshake()`, and `shutdown()`
    waits for an accept loop that is never coming back.
    """
    _ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    opened: list[socket.socket] = []
    live: list[LocalEndpoint] = []
    try:
        with pytest.raises(AssertionError, match="still held 1 connection"):
            await stall_a_tls_handshake((cert_pem, key_pem), opened, live)

        [silent], [endpoint] = opened, live
        assert await asyncio.to_thread(peer_closed_within, silent, _CONNECT_SECONDS)
        assert endpoint.open_connections() == 0
    finally:
        for client in opened:
            client.close()


def test_a_stalled_handshake_cannot_pin_the_teardown(tmp_path: Path) -> None:
    _ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)

    finished = run_stalled_handshake(cert_pem, key_pem)

    assert finished.returncode == 0, finished.stderr
    printed = finished.stdout.split()
    assert "tracked" in printed, finished.stdout
    assert "retired" in printed, finished.stdout
    assert "closed=True" in printed, finished.stdout


async def test_the_unmanaged_endpoint_serves_and_then_closes_what_it_started() -> None:
    """The raw lifecycle is the helper's too, even without the ownership contract.

    These tests need the server object itself, so the accept loop they run
    is started by `tests/local_endpoint.py` rather than by a second
    hand-written endpoint here — which is the whole point of #390 having
    one lifecycle. What the helper owes them is the same either way: an
    endpoint that answers while the context is open, and a listener and
    threads that are gone once it has closed.
    """
    with local_endpoint._unmanaged_endpoint(
        JsonHandler, handshake_seconds=_CONNECT_SECONDS, join_seconds=_CONNECT_SECONDS
    ) as server:
        port = server.server_port
        client = await asyncio.to_thread(open_keepalive_request, port)
        client.close()
        assert server.activity().accepted == 1

    assert await asyncio.to_thread(listener_refused, port)
    assert endpoint_threads(port) == []


async def test_a_retiring_endpoint_closes_a_late_connection_instead_of_serving_it() -> None:
    """Teardown stops accepting *before* it force-closes, so nothing is accepted behind it."""
    with raw_endpoint() as server:
        server.begin_retiring()
        late = await asyncio.to_thread(connect_without_speaking, server.server_port)
        try:
            assert await asyncio.to_thread(peer_closed_within, late, _CONNECT_SECONDS)
            assert server.open_connections() == 0
            assert server.activity().refused_while_retiring >= 1
        finally:
            late.close()


async def test_a_handshake_gives_up_at_its_deadline_and_keeps_the_endpoint_serving(
    tmp_path: Path,
) -> None:
    """The accept loop is single-threaded, so a silent client is everyone's problem.

    Force-closing the tracked peer wakes a blocked handshake on some
    platforms; the deadline is what makes that true on all of them. It is
    proven here by the client *behind* the silent one being served.
    """
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    with raw_endpoint(
        tls=local_endpoint._server_context((cert_pem, key_pem)),
        handshake_seconds=_IMPATIENT_SECONDS,
    ) as server:
        silent = await asyncio.to_thread(connect_without_speaking, server.server_port)
        try:
            served = await asyncio.to_thread(
                open_keepalive_request, server.server_port, cafile=ca_pem
            )
            served.close()
        finally:
            silent.close()

        assert server.activity().accepted == 2


def test_a_tls_wrapper_cannot_be_retracked_after_retirement() -> None:
    """A wrapper handed over behind force-close is rejected where it lands."""
    with raw_endpoint() as server:
        previous, previous_peer = socket.socketpair()
        wrapper, wrapper_peer = socket.socketpair()
        try:
            with server._changed:
                server._track(previous)
            server.begin_retiring()
            server.force_close_connections()

            with pytest.raises(OSError, match="retiring"):
                server._retrack(previous, wrapper)

            assert wrapper.fileno() == -1
            assert server.open_connections() == 0
            assert server.activity().refused_while_retiring >= 1
        finally:
            for connection in (previous, previous_peer, wrapper, wrapper_peer):
                connection.close()


async def test_a_retiring_endpoint_will_not_start_a_client_eof_holder() -> None:
    """A holder started after the join has run would outlive the endpoint."""
    with raw_endpoint(half_close=True) as server:
        held, client = socket.socketpair()
        late, late_client = socket.socketpair()
        try:
            server.shutdown_request(held)
            assert server.activity().holders_started == 1

            client.close()  # the client releases, so the holder reaches EOF and exits
            assert await asyncio.to_thread(server.join_workers, _CONNECT_SECONDS) == ()

            server.begin_retiring()
            server.shutdown_request(late)

            assert server.activity().holders_started == 1
            assert late.fileno() == -1, "the endpoint held a peer after teardown had begun"
            assert server.activity().refused_while_retiring >= 1
        finally:
            for leftover in (held, client, late, late_client):
                leftover.close()


async def test_teardown_closes_a_connection_arriving_after_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public teardown refuses a connection that arrives behind retirement."""
    opened: list[socket.socket] = []
    peer_closed: list[bool] = []
    errors: list[Exception] = []
    racers: list[threading.Thread] = []
    live: list[LocalEndpoint] = []
    retiring = threading.Event()
    finished = threading.Event()
    real_begin_retiring = local_endpoint._EndpointServer.begin_retiring
    real_force_close = local_endpoint._EndpointServer.force_close_connections

    def signal_retirement(server: local_endpoint._EndpointServer) -> None:
        real_begin_retiring(server)
        retiring.set()

    def wait_for_late_connection(
        server: local_endpoint._EndpointServer,
    ) -> tuple[str, ...]:
        assert finished.wait(_CONNECT_SECONDS), "the late connection never completed"
        return real_force_close(server)

    async def race() -> None:
        [endpoint] = live
        racer = threading.Thread(
            target=connect_after_signal,
            args=(endpoint.port, retiring, finished, opened, peer_closed, errors),
            daemon=True,
        )
        racers.append(racer)
        racer.start()

    monkeypatch.setattr(local_endpoint._EndpointServer, "begin_retiring", signal_retirement)
    monkeypatch.setattr(
        local_endpoint._EndpointServer,
        "force_close_connections",
        wait_for_late_connection,
    )
    try:
        async with served_endpoint(JsonHandler, release_clients=race) as endpoint:
            live.append(endpoint)
        [racer] = racers
        await asyncio.to_thread(racer.join, _CONNECT_SECONDS)
        assert not racer.is_alive()
        assert errors == []
        assert len(opened) == 1
        assert peer_closed == [True]
        assert endpoint.open_connections() == 0
        activity = endpoint.activity()
        assert activity.accepted == 1
        assert activity.handlers_started == 0
        assert activity.refused_while_retiring >= 1
        assert endpoint_threads(endpoint.port) == []
    finally:
        retiring.set()
        finished.set()
        for racing_thread in racers:
            await asyncio.to_thread(racing_thread.join, _CONNECT_SECONDS)
        for client in opened:
            client.close()


async def test_teardown_retires_the_endpoint_before_it_closes_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both force-close passes run, and the first one runs on a retired endpoint.

    A force-close that lands while the endpoint is still accepting closes
    the peers it can see and leaves whatever arrives behind it, which is
    the connection that then outlives the endpoint.
    """
    real_close = local_endpoint._EndpointServer.force_close_connections
    retiring_at_each_pass: list[bool] = []

    def recording_close(server: local_endpoint._EndpointServer) -> tuple[str, ...]:
        retiring_at_each_pass.append(server._is_retiring())
        return real_close(server)

    monkeypatch.setattr(local_endpoint._EndpointServer, "force_close_connections", recording_close)

    async with served_endpoint(JsonHandler) as endpoint:
        client = await asyncio.to_thread(open_keepalive_request, endpoint.port)
        client.close()

    assert retiring_at_each_pass == [True, True]


async def test_teardown_drains_queued_closures_while_the_endpoint_still_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain runs after the release and before anything waits or closes.

    Recorded rather than timed: the phases are observed from inside the
    teardown itself, and the connection count taken at the drain proves the
    endpoint had not disposed of anything yet.
    """
    phases: list[str] = []
    real_drain = local_endpoint.drain_transport_closures
    live: list[LocalEndpoint] = []

    async def recording_drain() -> None:
        [endpoint] = live
        phases.append(f"drain with {endpoint.open_connections()} held")
        await real_drain()

    async def release() -> None:
        phases.append("release")

    monkeypatch.setattr(local_endpoint, "drain_transport_closures", recording_drain)

    async with served_endpoint(JsonHandler, release_clients=release) as endpoint:
        live.append(endpoint)
        reader, writer = await asyncio.open_connection(_HOST, endpoint.port)
        writer.write(b"GET /api.json HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        header = await reader.readuntil(b"\r\n\r\n")
        await reader.readexactly(content_length(header))
        writer.close()  # the loop has only *queued* this close

    await writer.wait_closed()
    assert phases == ["release", "drain with 1 held"]


async def stall_one_handler(opened: list[socket.socket]) -> None:
    """Leave one handler thread inside its own request when teardown begins."""
    async with served_endpoint(StallingHandler, settle_seconds=_IMPATIENT_SECONDS) as endpoint:
        opened.append(await asyncio.to_thread(send_request_only, endpoint.port))
        assert await asyncio.to_thread(StallingHandler.started.wait, _CONNECT_SECONDS)
        assert endpoint.activity().handlers_started == 1


async def test_a_handler_thread_that_outlives_the_join_is_named_by_teardown() -> None:
    """Every handler thread is registered before it starts, so the join is complete."""
    StallingHandler.started.clear()
    StallingHandler.released.clear()
    StallingHandler.finished.clear()
    opened: list[socket.socket] = []
    try:
        with pytest.raises(AssertionError, match="still ran") as caught:
            await stall_one_handler(opened)

        assert ENDPOINT_THREAD_PREFIX in str(caught.value)
    finally:
        StallingHandler.released.set()
        assert await asyncio.to_thread(StallingHandler.finished.wait, _CONNECT_SECONDS)
        for client in opened:
            client.close()


async def test_the_endpoint_requires_tls_1_2_or_newer(tmp_path: Path) -> None:
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    context = local_endpoint._server_context((cert_pem, key_pem))
    assert context is not None
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2

    async with served_endpoint(JsonHandler, tls=(cert_pem, key_pem)) as endpoint:
        client = await asyncio.to_thread(open_keepalive_request, endpoint.port, cafile=ca_pem)
        try:
            assert isinstance(client, ssl.SSLSocket)
            negotiated = client.version()
        finally:
            client.close()

    assert negotiated in {"TLSv1.2", "TLSv1.3"}, negotiated


async def abandon_several_clients(opened: list[socket.socket], count: int) -> None:
    """Leave `count` served connections open when the endpoint context exits."""
    async with served_endpoint(JsonHandler, settle_seconds=_IMPATIENT_SECONDS) as endpoint:
        for _ in range(count):
            opened.append(await asyncio.to_thread(open_keepalive_request, endpoint.port))


async def test_the_stranded_peer_report_names_a_few_peers_and_counts_the_rest() -> None:
    opened: list[socket.socket] = []
    try:
        with pytest.raises(AssertionError, match=r"\(\+2 more\)") as caught:
            await abandon_several_clients(opened, 7)

        message = str(caught.value)
        assert "still held 7 connection(s)" in message
        assert message.count(f"{_HOST}:") == 5, message
    finally:
        for client in opened:
            client.close()


async def drop_one_handshake(
    tls: tuple[Path, Path], cafile: Path, opened: list[socket.socket]
) -> None:
    """Fail one handshake the way `socketserver` does not expect, then serve a client."""
    async with served_endpoint(JsonHandler, tls=tls) as endpoint:
        dropped = await asyncio.to_thread(connect_without_speaking, endpoint.port)
        opened.append(dropped)
        assert await asyncio.to_thread(peer_closed_within, dropped, _CONNECT_SECONDS)

        survivor = await asyncio.to_thread(open_keepalive_request, endpoint.port, cafile=cafile)
        survivor.close()


async def test_a_handshake_failure_that_is_not_an_oserror_keeps_the_accept_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`socketserver` only survives an `OSError`, and a dropped peer is still reported."""
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    monkeypatch.setattr(local_endpoint, "_server_context", failing_once_context)
    opened: list[socket.socket] = []
    try:
        with pytest.raises(AssertionError, match="no TLS for you"):
            await drop_one_handshake((cert_pem, key_pem), ca_pem, opened)
    finally:
        for client in opened:
            client.close()


async def explode_one_handler(opened: list[socket.socket]) -> None:
    """Let one handler raise, and watch the endpoint close the peer it was serving."""
    async with served_endpoint(ExplodingHandler) as endpoint:
        client = await asyncio.to_thread(send_request_only, endpoint.port)
        opened.append(client)
        assert await asyncio.to_thread(peer_closed_within, client, _CONNECT_SECONDS)


async def test_a_handler_failure_is_reported_when_the_body_and_release_were_clean() -> None:
    opened: list[socket.socket] = []
    try:
        with pytest.raises(AssertionError, match="the handler exploded"):
            await explode_one_handler(opened)
    finally:
        for client in opened:
            client.close()


async def test_a_cancelled_release_is_not_downgraded_to_a_note() -> None:
    """Cancellation outranks the body's own failure: swallowing it strands the canceller."""

    async def cancel_release() -> None:
        raise asyncio.CancelledError("the release was cancelled")

    with pytest.raises(asyncio.CancelledError, match="the release was cancelled") as caught:
        async with served_endpoint(JsonHandler, release_clients=cancel_release):
            raise ValueError("the body failed")

    notes = getattr(caught.value, "__notes__", [])
    assert any("the body failed" in note for note in notes), notes


async def test_a_cancelled_body_outranks_a_release_failure() -> None:
    async def fail_release() -> None:
        raise RuntimeError("release failed")

    with pytest.raises(asyncio.CancelledError, match="the body was cancelled") as caught:
        async with served_endpoint(JsonHandler, release_clients=fail_release):
            raise asyncio.CancelledError("the body was cancelled")

    notes = getattr(caught.value, "__notes__", [])
    assert any("release failed" in note for note in notes), notes


async def test_cancellation_during_force_close_still_stops_the_accept_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation propagates only after the endpoint has stopped every thread."""
    entered = threading.Event()
    release = threading.Event()
    servers: list[local_endpoint._EndpointServer] = []
    ports: list[int] = []
    real_force_close = local_endpoint._EndpointServer.force_close_connections

    def blocking_force_close(
        server: local_endpoint._EndpointServer,
    ) -> tuple[str, ...]:
        if not entered.is_set():
            servers.append(server)
            entered.set()
            assert release.wait(_CONNECT_SECONDS), "the force-close was never released"
        return real_force_close(server)

    async def close_endpoint() -> None:
        async with served_endpoint(JsonHandler) as endpoint:
            ports.append(endpoint.port)

    monkeypatch.setattr(
        local_endpoint._EndpointServer,
        "force_close_connections",
        blocking_force_close,
    )
    task = asyncio.create_task(close_endpoint())
    assert await asyncio.to_thread(entered.wait, _CONNECT_SECONDS)
    task.cancel("teardown was cancelled")
    release.set()
    try:
        with pytest.raises(asyncio.CancelledError, match="teardown was cancelled"):
            await task

        [port] = ports
        assert await asyncio.to_thread(listener_refused, port)
        assert endpoint_threads(port) == []
    finally:
        release.set()
        if servers:
            await asyncio.to_thread(servers[0].shutdown)
        for port in ports:
            for thread in live_endpoint_threads(port):
                await asyncio.to_thread(thread.join, _CONNECT_SECONDS)
