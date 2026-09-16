"""One lifecycle for every local HTTP/TLS endpoint this suite stands up.

A test endpoint decides something no test means to decide: *who closes
first*. Closing an accepted connection before its client has released the
client's own transport strands that transport on the Windows proactor,
which finalizes it by calling `self._sock.shutdown(SHUT_RDWR)` and
`self._sock.close()` unguarded (CPython `Lib/asyncio/proactor_events.py`
`_call_connection_lost`); the selector loop makes neither call. The
resulting `OSError` is reported much later, as a
`PytestUnraisableExceptionWarning` against whichever unrelated test the
collector happened to reach the object in — which is #390, and why the
decision is made once, here, instead of six times by hand.

Two ownership contracts are offered:

`served_endpoint()`
    The endpoint answers HTTP/1.1 with keep-alive and never closes a
    connection its client still owns. Teardown releases the clients
    first, lets the event loop finish the closes it has only queued, then
    waits — bounded, on an event — for the accepted sockets to go.

`disconnecting_endpoint()`
    For tests whose subject *is* a dropped remote. The endpoint
    half-closes its write side, so the client observes the same EOF a
    disconnect produces, but keeps reading the accepted peer until the
    client closes it. The client-visible behaviour is unchanged; what
    changes is that no client transport is ever finalized against a peer
    that is already gone.

Every wait here is bounded and event-driven. Nothing in this module
filters a warning, collects garbage, sleeps for correctness, or retries.
TLS is wrapped per *accepted* socket, never on the listening socket:
wrapping the listener makes `accept()` hand a refused connection to
`ssl.SSLSocket._create`, which closes it — the endpoint closing first,
exactly what this module exists to prevent.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: Every thread an endpoint owns is named with this prefix, so a test can
#: prove none of them outlived the endpoint.
ENDPOINT_THREAD_PREFIX = "korvid-test-endpoint"

_HOST = "127.0.0.1"

#: How often the accept loop looks for a shutdown request. A latency
#: bound on teardown, not a correctness wait.
_ACCEPT_POLL_SECONDS = 0.05

#: Bounded only so a client that never closes cannot pin a holder thread
#: forever. Teardown wakes a holder long before this, by closing the
#: socket it is reading.
_HOLD_SECONDS = 30.0

#: How long teardown waits for the endpoint's clients to release the
#: connections they own before it declares the ownership unsettled.
_SETTLE_SECONDS = 5.0


class KeepAliveHandler(BaseHTTPRequestHandler):
    """A quiet HTTP/1.1 handler whose connections outlive the response.

    HTTP/1.1 is the contract, not a preference: under HTTP/1.0 the
    handler closes the connection after the response, taking the
    ownership decision away from the client.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # http.server API name
        return None


@dataclass(frozen=True)
class LocalEndpoint:
    """Where a running endpoint listens, and what it still holds."""

    url: str
    port: int
    #: Live count of accepted connections the endpoint has not closed.
    #: Ownership is the subject of these tests, so it is observable.
    open_connections: Callable[[], int]

    def path(self, suffix: str) -> str:
        """The endpoint's URL with `suffix` appended as a path."""
        return f"{self.url.rstrip('/')}/{suffix.lstrip('/')}"


async def drain_transport_closures() -> None:
    """Let the loop finish the transport closes it has only queued.

    `transport.close()` returns before the socket is released: the
    proactor queues `connection_lost()`, which owns the socket until that
    callback runs. TLS transports chain a second callback behind the
    first, so two turns are taken, not one.
    """
    loop = asyncio.get_running_loop()
    for _ in range(2):
        barrier = asyncio.Event()
        loop.call_soon(barrier.set)
        await barrier.wait()


def served_endpoint(
    handler: type[BaseHTTPRequestHandler],
    *,
    tls: tuple[Path, Path] | None = None,
    release_clients: Callable[[], Awaitable[None]] | None = None,
    settle_seconds: float = _SETTLE_SECONDS,
) -> AbstractAsyncContextManager[LocalEndpoint]:
    """A local endpoint whose connections are the client's to close.

    Args:
        handler: An HTTP/1.1 request handler, normally a
            `KeepAliveHandler` subclass.
        tls: A `(certificate, private key)` PEM pair. Each accepted
            socket is wrapped for TLS 1.2 or newer; the listening socket
            never is.
        release_clients: Awaited first during teardown, while the
            endpoint is still serving, so the clients' own EOF releases
            the handlers before anything waits on them.
        settle_seconds: Bound on every teardown wait.

    Raises:
        ValueError: If `handler` does not serve HTTP/1.1.
    """
    protocol = getattr(handler, "protocol_version", "")
    if protocol != "HTTP/1.1":
        raise ValueError(
            f"{handler.__name__} serves {protocol!r}; a served endpoint answers HTTP/1.1 so"
            " that the client, not the endpoint, closes the connection"
        )
    return _endpoint(
        handler,
        tls=tls,
        release_clients=release_clients,
        settle_seconds=settle_seconds,
        half_close=False,
        reason=None,
    )


def disconnecting_endpoint(
    handler: type[BaseHTTPRequestHandler],
    *,
    reason: str,
    tls: tuple[Path, Path] | None = None,
    settle_seconds: float = _SETTLE_SECONDS,
) -> AbstractAsyncContextManager[LocalEndpoint]:
    """A local endpoint that drops connections on purpose, and says why.

    The client observes the EOF a disconnected remote produces, because
    the endpoint half-closes its write side. It then keeps reading the
    accepted peer until the client closes, so no client transport is
    finalized against a peer that is already gone.

    Args:
        handler: The request handler; any protocol version, since a
            dropped connection is the point.
        reason: What the intentional disconnect proves. Reported with
            any ownership failure, so a future reader learns why this
            endpoint is not a `served_endpoint`.
        tls: A `(certificate, private key)` PEM pair, wrapped per
            accepted socket.
        settle_seconds: Bound on every teardown wait.

    Raises:
        ValueError: If `reason` is empty.
    """
    if not reason.strip():
        raise ValueError(
            "disconnecting_endpoint needs a reason naming what the intentional disconnect"
            " proves; a served_endpoint is the default"
        )
    return _endpoint(
        handler,
        tls=tls,
        release_clients=None,
        settle_seconds=settle_seconds,
        half_close=True,
        reason=reason,
    )


class _EndpointServer(ThreadingHTTPServer):
    """A threading HTTP server that tracks what it accepted.

    Request handlers run off the accept loop (`daemon_threads`), so an
    idle keep-alive connection never blocks `shutdown()`. Every accepted
    socket is registered until it is closed, which is what lets teardown
    wait on an event instead of a clock.
    """

    daemon_threads = True

    def __init__(
        self,
        handler: type[BaseHTTPRequestHandler],
        *,
        tls: ssl.SSLContext | None,
        half_close: bool,
    ) -> None:
        self._tls = tls
        self._half_close = half_close
        self._changed = threading.Condition()
        self._connections: dict[socket.socket, str] = {}
        self._workers: list[threading.Thread] = []
        self._retiring = threading.Event()
        super().__init__((_HOST, 0), handler)

    def open_connections(self) -> int:
        """How many accepted connections the endpoint still holds."""
        with self._changed:
            return len(self._connections)

    def get_request(self) -> tuple[Any, Any]:  # socketserver API name
        connection, address = self.socket.accept()
        if self._tls is not None:
            connection = self._handshake(connection)
        self._remember(connection)
        return connection, address

    def process_request(self, request: Any, client_address: Any) -> None:  # socketserver API name
        worker = threading.Thread(
            target=self.process_request_thread,
            args=(request, client_address),
            name=f"{ENDPOINT_THREAD_PREFIX}-handler-{self.server_port}",
            daemon=self.daemon_threads,
        )
        self._remember_thread(worker)
        worker.start()

    def shutdown_request(self, request: Any) -> None:  # socketserver API name
        if not self._half_close or self._retiring.is_set():
            super().shutdown_request(request)
            return
        # The client is meant to see EOF, so the write side goes; the peer
        # itself stays until the client has released its own transport.
        with suppress(OSError):
            request.shutdown(socket.SHUT_WR)
        self._hold_until_client_eof(request)

    def close_request(self, request: Any) -> None:  # socketserver API name
        self._forget(request)
        super().close_request(request)

    def handle_error(self, request: Any, client_address: Any) -> None:  # socketserver API name
        if self._retiring.is_set():
            return  # a connection this endpoint force-closed is not a handler fault
        super().handle_error(request, client_address)

    def wait_for_connections(self, timeout: float) -> tuple[str, ...]:
        """Wait for the clients to close, naming whatever is left.

        Blocking, so callers run it off the event loop: the closes being
        waited for belong to that loop.
        """
        with self._changed:
            if self._changed.wait_for(lambda: not self._connections, timeout=timeout):
                return ()
            return tuple(sorted(self._connections.values()))

    def force_close_connections(self) -> None:
        """Close whatever the clients left, so nothing outlives teardown."""
        self._retiring.set()
        with self._changed:
            leftover = list(self._connections)
        for connection in leftover:
            # shutdown() first: it wakes a handler or holder thread blocked
            # in recv() on this socket, which close() alone does not.
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                connection.close()
            self._forget(connection)

    def join_workers(self, timeout: float) -> tuple[str, ...]:
        """Join every handler and holder thread, naming any that stayed."""
        deadline = time.monotonic() + timeout
        with self._changed:
            workers = list(self._workers)
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        return tuple(sorted({worker.name for worker in workers if worker.is_alive()}))

    def _handshake(self, raw: socket.socket) -> socket.socket:
        assert self._tls is not None
        try:
            connection = self._tls.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
        except OSError:
            raw.close()  # nothing was handed to a client, so nothing is owed one
            raise
        try:
            connection.do_handshake()
        except OSError:
            # A client that refused this certificate still owns its own
            # transport; disposing of the peer here is what #390 is about.
            self._remember(connection)
            self._hold_until_client_eof(connection)
            raise
        return connection

    def _hold_until_client_eof(self, connection: socket.socket) -> None:
        holder = threading.Thread(
            target=self._read_to_client_eof,
            args=(connection,),
            name=f"{ENDPOINT_THREAD_PREFIX}-holder-{self.server_port}",
            daemon=True,
        )
        self._remember_thread(holder)
        holder.start()

    def _read_to_client_eof(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(_HOLD_SECONDS)
            # Raw reads: a refused handshake leaves no TLS session to
            # decrypt through, and the bytes are only a path to EOF.
            while socket.socket.recv(connection, 4096):
                pass
        except Exception:  # a torn-down connection must not reach threading.excepthook
            pass
        finally:
            self.close_request(connection)

    def _remember(self, connection: socket.socket) -> None:
        with self._changed:
            self._connections[connection] = _describe(connection)
            self._changed.notify_all()

    def _forget(self, connection: socket.socket) -> None:
        with self._changed:
            self._connections.pop(connection, None)
            self._changed.notify_all()

    def _remember_thread(self, worker: threading.Thread) -> None:
        with self._changed:
            self._workers.append(worker)


@asynccontextmanager
async def _endpoint(
    handler: type[BaseHTTPRequestHandler],
    *,
    tls: tuple[Path, Path] | None,
    release_clients: Callable[[], Awaitable[None]] | None,
    settle_seconds: float,
    half_close: bool,
    reason: str | None,
) -> AsyncIterator[LocalEndpoint]:
    server = _EndpointServer(handler, tls=_server_context(tls), half_close=half_close)
    accept = threading.Thread(
        target=partial(server.serve_forever, poll_interval=_ACCEPT_POLL_SECONDS),
        name=f"{ENDPOINT_THREAD_PREFIX}-accept-{server.server_port}",
        daemon=True,
    )
    endpoint = LocalEndpoint(
        url=f"{'http' if tls is None else 'https'}://{_HOST}:{server.server_port}",
        port=server.server_port,
        open_connections=server.open_connections,
    )
    try:
        accept.start()
    except BaseException:
        server.server_close()
        raise

    body_error: BaseException | None = None
    try:
        yield endpoint
    except BaseException as error:
        body_error = error
        raise
    finally:
        await _retire(
            server,
            accept,
            release_clients=release_clients,
            settle_seconds=settle_seconds,
            body_error=body_error,
            reason=reason,
        )


async def _retire(
    server: _EndpointServer,
    accept: threading.Thread,
    *,
    release_clients: Callable[[], Awaitable[None]] | None,
    settle_seconds: float,
    body_error: BaseException | None,
    reason: str | None,
) -> None:
    """Release the clients, then close everything the endpoint still owns.

    The body's failure stays primary and a release failure comes second;
    an ownership failure is only reported when neither happened, because
    a stranded connection is what a *passing* test would otherwise hide.
    Whatever is raised, every socket and thread is closed and joined.
    """
    release_error = await _release(release_clients)
    stranded: tuple[str, ...] = ()
    try:
        # Transports whose close the loop has only queued still own their
        # socket; let those callbacks run before ownership is judged.
        await drain_transport_closures()
        stranded = await asyncio.to_thread(server.wait_for_connections, settle_seconds)
    finally:
        threads = await _stop(server, accept, settle_seconds)

    if body_error is not None:
        if release_error is not None:
            body_error.add_note(f"releasing this endpoint's clients also failed: {release_error!r}")
        return
    if release_error is not None:
        raise release_error
    if stranded or threads:
        raise AssertionError(_unsettled(stranded, threads, settle_seconds, reason))


async def _release(
    release_clients: Callable[[], Awaitable[None]] | None,
) -> BaseException | None:
    """Await the caller's client release, keeping any failure for later."""
    if release_clients is None:
        return None
    try:
        await release_clients()
    except BaseException as error:  # re-raised by _retire unless the body already failed
        return error
    return None


async def _stop(
    server: _EndpointServer, accept: threading.Thread, settle_seconds: float
) -> tuple[str, ...]:
    """Close every socket and join every thread, naming the threads that stayed."""
    leftover: tuple[str, ...] = ()
    try:
        await asyncio.to_thread(server.force_close_connections)
    finally:
        try:
            await _stop_accepting(server, accept, settle_seconds)
        finally:
            try:
                leftover = await asyncio.to_thread(server.join_workers, settle_seconds)
            finally:
                server.server_close()
    if accept.is_alive():
        leftover = (*leftover, accept.name)
    return tuple(sorted(leftover))


async def _stop_accepting(
    server: _EndpointServer, accept: threading.Thread, settle_seconds: float
) -> None:
    """Stop the accept loop off the event loop, then join its thread.

    `shutdown()` blocks until the accept loop has finished, and that loop
    can be waiting on a handshake whose client half belongs to the event
    loop. Running it on the loop would strand every closure the loop
    still owes, and deadlock outright when the accept thread is waiting
    on one.
    """
    if accept.ident is None:
        return
    try:
        await asyncio.to_thread(server.shutdown)
    finally:
        await asyncio.to_thread(accept.join, settle_seconds)


def _unsettled(
    stranded: tuple[str, ...],
    threads: tuple[str, ...],
    settle_seconds: float,
    reason: str | None,
) -> str:
    subject = "" if reason is None else f" (intentional disconnect: {reason})"
    detail = []
    if stranded:
        detail.append(
            f"still held {len(stranded)} connection(s) after {settle_seconds}s:"
            f" {', '.join(stranded)}"
        )
    if threads:
        detail.append(f"still ran {', '.join(threads)}")
    return (
        f"the endpoint{subject} {'; '.join(detail)}."
        " Its clients have to close before the endpoint context exits, or the"
        " transports they left behind are finalized against a peer that is gone (#390)."
    )


def _server_context(tls: tuple[Path, Path] | None) -> ssl.SSLContext | None:
    if tls is None:
        return None
    certfile, keyfile = tls
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2  # no legacy TLS
    context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    return context


def _describe(connection: socket.socket) -> str:
    try:
        peer = connection.getpeername()
    except OSError:
        return repr(connection)
    if isinstance(peer, tuple) and len(peer) >= 2:
        return f"{peer[0]}:{peer[1]}"
    return str(peer)
