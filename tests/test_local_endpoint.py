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
import socket
import ssl
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

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

#: The bound a *negative* lifecycle test spends proving the endpoint gives
#: up on a client that never released. Short because the test's subject is
#: the bound itself.
_IMPATIENT_SECONDS = 0.25


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


class DisconnectHandler(KeepAliveHandler):
    """Answers once and drops the connection, the way a remote peer does."""

    def do_GET(self) -> None:  # http.server API name
        self.close_connection = True
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")


def endpoint_threads() -> list[str]:
    """Names of endpoint-owned threads still alive right now."""
    return sorted(
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith(ENDPOINT_THREAD_PREFIX)
    )


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
    raw = socket.create_connection((_HOST, port), timeout=_PEER_SETTLE_SECONDS)
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
    client = socket.create_connection((_HOST, port), timeout=_PEER_SETTLE_SECONDS * 20)
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
    raw = socket.create_connection((_HOST, port), timeout=_PEER_SETTLE_SECONDS * 20)
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


def listener_refused(port: int) -> bool:
    """Whether the endpoint's listening socket is gone."""
    try:
        leftover = socket.create_connection((_HOST, port), timeout=_PEER_SETTLE_SECONDS)
    except OSError:
        return True
    leftover.close()
    return False


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

    assert endpoint_threads() == []


async def test_idle_keep_alive_connections_do_not_block_teardown() -> None:
    async with served_endpoint(JsonHandler) as endpoint:
        clients = [await asyncio.to_thread(open_keepalive_request, endpoint.port) for _ in range(3)]
        try:
            assert endpoint.open_connections() == 3
        finally:
            for client in clients:
                client.close()

    assert endpoint_threads() == []


async def test_served_endpoint_reports_a_connection_its_client_never_released() -> None:
    opened: list[socket.socket] = []
    ports: list[int] = []
    try:
        with pytest.raises(AssertionError, match="still held 1 connection"):
            await abandon_one_client(opened, ports)

        [client], [port] = opened, ports
        assert await asyncio.to_thread(peer_state, client) != "open"
        assert await asyncio.to_thread(listener_refused, port)
        assert endpoint_threads() == []
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
        assert endpoint_threads() == []
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
    assert endpoint_threads() == []


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
    assert endpoint_threads() == []


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
    assert endpoint_threads() == []


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
    assert endpoint_threads() == []


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
    assert endpoint_threads() == []


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
    assert endpoint_threads() == []


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
        assert endpoint_threads() == []
    finally:
        if client is not None:
            client.close()
