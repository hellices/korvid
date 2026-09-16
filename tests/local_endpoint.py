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

Two orderings inside the endpoint carry the same weight as the ownership
contract itself, because both decide whether teardown can *finish*:

- an accepted socket is registered **before** anything blocks on it,
  including its own TLS handshake. Until it is registered, a client that
  opens a connection and says nothing holds the accept loop — and with
  it `shutdown()`, and with that the executor thread waiting on it;
- teardown marks the endpoint retiring **before** it closes anything.
  Closing first leaves a window in which the accept loop answers one more
  client, and that connection outlives the endpoint that accepted it.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    contextmanager,
    suppress,
)
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

#: Every thread an endpoint owns is named with this prefix, so an
#: ownership failure can say which of them outlived the endpoint.
ENDPOINT_THREAD_PREFIX = "korvid-test-endpoint"

_HOST = "127.0.0.1"

#: How often the accept loop looks for a shutdown request. A latency
#: bound on teardown, not a correctness wait.
_ACCEPT_POLL_SECONDS = 0.05

#: How often a holder looks up from its read to see whether the endpoint
#: has started retiring. A latency bound on teardown, not a correctness
#: wait: whether closing a socket wakes the thread blocked in `recv()` on
#: it is a platform question, and a holder that has to be joined cannot
#: depend on the answer.
_HOLD_POLL_SECONDS = 0.05

#: How long teardown waits for the endpoint's clients to release the
#: connections they own before it declares the ownership unsettled.
_SETTLE_SECONDS = 5.0

#: Floor under the per-connection handshake bound. A TLS handshake on a
#: loaded CI runner is slow, and this bound is not here to time one: it is
#: the backstop that stops a client which never sends a ClientHello from
#: pinning the accept loop. Teardown does not wait for it — it closes the
#: socket the handshake is blocked on — so a generous floor costs nothing.
_MIN_HANDSHAKE_SECONDS = 10.0

#: How many stranded peers an ownership failure names before it counts the
#: rest. Enough to identify the client; not a page of them.
_MAX_REPORTED_PEERS = 5


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
class EndpointActivity:
    """What an endpoint has accepted and started, as counts a test can assert on.

    The registry exists because the interesting claims are about work the
    endpoint *did not* do — a holder that must not start once teardown has
    begun, a connection that must not be served after it. A count is
    checkable; a thread that was never created is not.
    """

    accepted: int
    handlers_started: int
    holders_started: int
    refused_while_retiring: int


@dataclass(frozen=True)
class LocalEndpoint:
    """Where a running endpoint listens, and what it still holds."""

    url: str
    port: int
    #: Live count of accepted connections the endpoint has not closed.
    #: Ownership is the subject of these tests, so it is observable.
    open_connections: Callable[[], int]
    #: A snapshot of what the endpoint has accepted and started.
    activity: Callable[[], EndpointActivity]
    #: `wait_until_tracked(count, timeout)`: block until at least `count`
    #: connections are registered, returning whether they were. Driven by
    #: the same condition the endpoint notifies, so no test polls.
    wait_until_tracked: Callable[[int, float], bool]

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
        settle_seconds: Bound on every teardown wait, and the floor
            under the per-connection handshake bound.

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
        settle_seconds: Bound on every teardown wait, and the floor
            under the per-connection handshake bound.

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
    socket is registered *before* anything can block on it — including the
    TLS handshake — which is what lets teardown reach a peer the accept
    loop is currently inside, and wait on an event instead of a clock.

    Once `begin_retiring()` has been called the endpoint accepts nothing,
    starts no thread, and holds no peer: a connection that arrives behind
    teardown is closed where it lands, rather than surviving it.
    """

    daemon_threads = True

    def __init__(
        self,
        handler: type[BaseHTTPRequestHandler],
        *,
        tls: ssl.SSLContext | None,
        half_close: bool,
        handshake_seconds: float,
    ) -> None:
        self._tls = tls
        self._half_close = half_close
        self._handshake_seconds = handshake_seconds
        self._changed = threading.Condition()
        self._connections: dict[socket.socket, str] = {}
        self._workers: list[threading.Thread] = []
        self._failures: list[str] = []
        #: Guarded by `_changed`, not an `Event`, so that "is this endpoint
        #: retiring?" and "start this thread" are one atomic decision.
        self._retiring = False
        self._counts = {"accepted": 0, "handlers": 0, "holders": 0, "refused": 0}
        super().__init__((_HOST, 0), handler)

    def open_connections(self) -> int:
        """How many accepted connections the endpoint still holds."""
        with self._changed:
            return len(self._connections)

    def activity(self) -> EndpointActivity:
        """A snapshot of what this endpoint has accepted and started."""
        with self._changed:
            counts = dict(self._counts)
        return EndpointActivity(
            accepted=counts["accepted"],
            handlers_started=counts["handlers"],
            holders_started=counts["holders"],
            refused_while_retiring=counts["refused"],
        )

    def failures(self) -> tuple[str, ...]:
        """Everything that failed while serving, in the order it failed."""
        with self._changed:
            return tuple(self._failures)

    def get_request(self) -> tuple[Any, Any]:  # socketserver API name
        connection, address = self.socket.accept()
        with self._changed:
            self._counts["accepted"] += 1
            if self._retiring:
                self._counts["refused"] += 1
                retiring = True
            else:
                retiring = False
                self._track(connection)
        if retiring:
            # Behind teardown there is no one left to own this peer, and a
            # tracked connection nobody serves would outlive the endpoint.
            _close(connection)
            raise _dropped("the endpoint is retiring")
        if self._tls is not None:
            connection = self._handshake(connection, address)
        return connection, address

    def verify_request(self, request: Any, client_address: Any) -> bool:  # socketserver API name
        with self._changed:
            if not self._retiring:
                return True
            self._counts["refused"] += 1
        return False  # socketserver closes it through shutdown_request()

    def process_request(self, request: Any, client_address: Any) -> None:  # socketserver API name
        worker = threading.Thread(
            target=self.process_request_thread,
            args=(request, client_address),
            name=f"{ENDPOINT_THREAD_PREFIX}-handler-{self.server_port}",
            daemon=self.daemon_threads,
        )
        if not self._start_worker(worker, kind="handlers"):
            self.shutdown_request(request)

    def shutdown_request(self, request: Any) -> None:  # socketserver API name
        if self._half_close and not self._refused_by_teardown():
            # The client is meant to see EOF, so the write side goes; the peer
            # itself stays until the client has released its own transport.
            with suppress(OSError):
                request.shutdown(socket.SHUT_WR)
            if self._hold_until_client_eof(request):
                return
        super().shutdown_request(request)

    def close_request(self, request: Any) -> None:  # socketserver API name
        self._forget(request)
        super().close_request(request)

    def handle_error(self, request: Any, client_address: Any) -> None:  # socketserver API name
        if self._is_retiring():
            return  # a connection this endpoint force-closed is not a handler fault
        self._record(f"the handler for {client_address} raised {sys.exc_info()[1]!r}")
        super().handle_error(request, client_address)

    def wait_until_released(self, timeout: float) -> tuple[str, ...]:
        """Wait for the clients to close, naming whatever is left.

        Blocking, so callers run it off the event loop: the closes being
        waited for belong to that loop.
        """
        with self._changed:
            if self._changed.wait_for(lambda: not self._connections, timeout=timeout):
                return ()
            return tuple(sorted(self._connections.values()))

    def wait_until_tracked(self, count: int, timeout: float) -> bool:
        """Wait until at least `count` connections are registered."""
        with self._changed:
            return self._changed.wait_for(lambda: len(self._connections) >= count, timeout=timeout)

    def begin_retiring(self) -> None:
        """Stop accepting and refuse new work, without waiting for anything.

        Cheap and non-blocking on purpose: teardown has to mark the
        endpoint *before* it closes sockets, or a connection accepted
        behind the force-close survives it. Waking whatever is blocked in
        `accept()` or `do_handshake()` is the force-close's job.
        """
        with self._changed:
            self._retiring = True
            self._changed.notify_all()

    def force_close_connections(self) -> tuple[str, ...]:
        """Close whatever the clients left, naming what had to be closed."""
        with self._changed:
            leftover = dict(self._connections)
        for connection in leftover:
            # shutdown() first: it wakes a handler thread blocked in recv()
            # on this socket, which close() alone does not. (A holder polls
            # instead, because that wake-up is a platform's to promise.)
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            _close(connection)
            self._forget(connection)
        return tuple(sorted(leftover.values()))

    def close_remaining(self, timeout: float) -> tuple[str, ...]:
        """Close a second time, after the join, naming what would not go.

        The first force-close races the accept loop and the handler
        threads; this one runs once both have stopped, so anything it finds
        is the endpoint's own leak rather than a client's.
        """
        self.force_close_connections()
        return self.wait_until_released(timeout)

    def join_workers(self, timeout: float) -> tuple[str, ...]:
        """Join every handler and holder thread, naming any that stayed."""
        deadline = time.monotonic() + timeout
        with self._changed:
            workers = list(self._workers)
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        return tuple(sorted({worker.name for worker in workers if worker.is_alive()}))

    def _handshake(self, raw: socket.socket, address: Any) -> socket.socket:
        assert self._tls is not None
        try:
            connection = self._tls.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
        except OSError:
            self._discard(raw)  # nothing was handed to a client, so nothing is owed one
            raise
        except BaseException as error:
            self._discard(raw)
            raise self._undroppable(address, error) from error
        # wrap_socket() detached the accepted socket, so the registration
        # has to follow the fd onto the object that now owns it.
        self._retrack(raw, connection)
        return self._complete_handshake(connection, address)

    def _complete_handshake(self, connection: ssl.SSLSocket, address: Any) -> socket.socket:
        # A client that never sends a ClientHello would otherwise hold the
        # accept loop — and therefore shutdown() — for as long as it liked.
        connection.settimeout(self._handshake_seconds)
        try:
            connection.do_handshake()
        except OSError:
            # A client that refused this certificate still owns its own
            # transport; disposing of the peer here is what #390 is about.
            if not self._hold_until_client_eof(connection):
                self._discard(connection)
            raise
        except BaseException as error:
            self._discard(connection)
            raise self._undroppable(address, error) from error
        connection.settimeout(None)
        return connection

    def _undroppable(self, address: Any, error: BaseException) -> OSError:
        """Record a handshake failure and restate it as one `socketserver` survives.

        `_handle_request_noblock` drops an `OSError` and keeps accepting;
        anything else escapes `serve_forever` and kills the accept loop
        silently, which turns one failed connection into a dead endpoint.
        """
        self._record(f"the handshake with {address} failed: {error!r}")
        return OSError(f"the endpoint dropped a connection it could not wrap: {error!r}")

    def _hold_until_client_eof(self, connection: socket.socket) -> bool:
        """Read the peer until the client closes it. False if teardown said no."""
        holder = threading.Thread(
            target=self._read_to_client_eof,
            args=(connection,),
            name=f"{ENDPOINT_THREAD_PREFIX}-holder-{self.server_port}",
            daemon=True,
        )
        return self._start_worker(holder, kind="holders")

    def _read_to_client_eof(self, connection: socket.socket) -> None:
        """Read the peer until the client closes it, or teardown retires it.

        The read is polled rather than left blocked, because whether
        closing a socket wakes the thread blocked in `recv()` on it is a
        platform question — and teardown has to be able to join this
        thread on every platform, not on the ones where the answer is yes.
        """
        try:
            connection.settimeout(_HOLD_POLL_SECONDS)
            # Raw reads: a refused handshake leaves no TLS session to
            # decrypt through, and the bytes are only a path to EOF.
            while not self._is_retiring():
                try:
                    if not socket.socket.recv(connection, 4096):
                        return
                except TimeoutError:
                    continue
        except Exception:  # a torn-down connection must not reach threading.excepthook
            pass
        finally:
            self.close_request(connection)

    def _start_worker(self, worker: threading.Thread, *, kind: str) -> bool:
        """Register and start a thread, unless teardown has already begun.

        Registration and the retiring check happen under one lock, so a
        thread can never be started by a racing accept after `join_workers`
        has taken its list — the join is complete by construction.
        """
        with self._changed:
            if self._retiring:
                self._counts["refused"] += 1
                return False
            self._workers.append(worker)
            self._counts[kind] += 1
            worker.start()
        return True

    def _is_retiring(self) -> bool:
        with self._changed:
            return self._retiring

    def _refused_by_teardown(self) -> bool:
        """Whether teardown has begun — counting the refusal when it has."""
        with self._changed:
            if not self._retiring:
                return False
            self._counts["refused"] += 1
            return True

    def _track(self, connection: socket.socket) -> None:
        """Register an accepted socket. Caller holds the condition."""
        self._connections[connection] = _describe(connection)
        self._changed.notify_all()

    def _retrack(self, previous: socket.socket, connection: socket.socket) -> None:
        with self._changed:
            self._connections.pop(previous, None)
            if not self._retiring:
                self._track(connection)
                return
            self._counts["refused"] += 1
            self._changed.notify_all()
        _close(connection)
        raise _dropped("the endpoint is retiring")

    def _discard(self, connection: socket.socket) -> None:
        _close(connection)
        self._forget(connection)

    def _forget(self, connection: socket.socket) -> None:
        with self._changed:
            self._connections.pop(connection, None)
            self._changed.notify_all()

    def _record(self, failure: str) -> None:
        with self._changed:
            self._failures.append(failure)


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
    server, accept = _start(
        handler,
        tls=_server_context(tls),
        half_close=half_close,
        handshake_seconds=max(settle_seconds, _MIN_HANDSHAKE_SECONDS),
    )
    endpoint = LocalEndpoint(
        url=f"{'http' if tls is None else 'https'}://{_HOST}:{server.server_port}",
        port=server.server_port,
        open_connections=server.open_connections,
        activity=server.activity,
        wait_until_tracked=server.wait_until_tracked,
    )

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


def _start(
    handler: type[BaseHTTPRequestHandler],
    *,
    tls: ssl.SSLContext | None,
    half_close: bool,
    handshake_seconds: float,
) -> tuple[_EndpointServer, threading.Thread]:
    """Stand one endpoint up: the only place this suite builds a server.

    Every local endpoint in the suite is born here, accept loop and all,
    so the ownership ordering #390 turns on is decided once rather than
    re-derived by whichever module needed an endpoint next. The accept
    thread is a daemon and is named after its port, so a thread that
    outlives its endpoint can be attributed to it.
    """
    server = _EndpointServer(
        handler,
        tls=tls,
        half_close=half_close,
        handshake_seconds=handshake_seconds,
    )
    accept = threading.Thread(
        target=partial(server.serve_forever, poll_interval=_ACCEPT_POLL_SECONDS),
        name=f"{ENDPOINT_THREAD_PREFIX}-accept-{server.server_port}",
        daemon=True,
    )
    try:
        accept.start()
    except BaseException:
        server.server_close()
        raise
    return server, accept


@contextmanager
def _unmanaged_endpoint(
    handler: type[BaseHTTPRequestHandler],
    *,
    tls: ssl.SSLContext | None = None,
    half_close: bool = False,
    handshake_seconds: float,
    join_seconds: float,
) -> Iterator[_EndpointServer]:
    """A running endpoint server, without the ownership contract around it.

    The suite's own lifecycle tests make claims about the server object:
    the retiring state the public contexts never hand out mid-flight, and
    a handshake bound chosen rather than derived from `settle_seconds`.
    They get that object from here instead of building a second endpoint
    by hand, which is what keeps this module the only one that constructs
    an HTTP server and starts an accept loop (#390).

    Teardown is the bare ordering, with nothing waiting on a client: the
    endpoint is marked retiring, its connections are force-closed, the
    accept loop is stopped and joined, and the listener is closed last.
    Deliberately synchronous — its callers are asserting on the server,
    not on an event loop's transports.

    Args:
        handler: The request handler the endpoint serves with.
        tls: A server context, applied per accepted socket.
        half_close: Whether the endpoint half-closes its write side.
        handshake_seconds: Bound on one connection's TLS handshake.
        join_seconds: Bound on each teardown join.
    """
    server, accept = _start(
        handler, tls=tls, half_close=half_close, handshake_seconds=handshake_seconds
    )
    try:
        yield server
    finally:
        server.begin_retiring()
        server.force_close_connections()
        server.shutdown()
        accept.join(join_seconds)
        server.join_workers(join_seconds)
        server.server_close()


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

    A cancellation outranks everything, because swallowing one strands
    whoever asked for it; then the body's own failure; then a release
    failure. What the endpoint found is only *raised* when none of those
    happened, since a stranded connection or a failed handler is what a
    passing test would otherwise hide. Whatever is raised, every socket is
    closed and every thread is joined first.
    """
    release_error = await _release(release_clients)
    stranded: tuple[str, ...] = ()
    leftovers = _Leftovers(threads=(), unclosed=())
    cleanup_errors: list[BaseException] = []
    try:
        # Transports whose close the loop has only queued still own their
        # socket; let those callbacks run before ownership is judged.
        await drain_transport_closures()
        stranded = await asyncio.to_thread(server.wait_until_released, settle_seconds)
    except BaseException as error:
        cleanup_errors.append(error)
    try:
        leftovers = await _stop(server, accept, settle_seconds)
    except BaseException as error:
        cleanup_errors.append(error)

    _report(
        body_error,
        release_error,
        tuple(cleanup_errors),
        _Unsettled(
            stranded=stranded,
            leftovers=leftovers,
            failures=server.failures(),
            settle_seconds=settle_seconds,
            reason=reason,
        ),
    )


async def _release(
    release_clients: Callable[[], Awaitable[None]] | None,
) -> BaseException | None:
    """Await the caller's client release, keeping any failure for later."""
    if release_clients is None:
        return None
    try:
        await release_clients()
    except BaseException as error:  # re-raised by _report unless the body outranks it
        return error
    return None


async def _stop(
    server: _EndpointServer, accept: threading.Thread, settle_seconds: float
) -> _Leftovers:
    """Finish endpoint cleanup before propagating cancellation."""
    cleanup = asyncio.create_task(_stop_once(server, accept, settle_seconds))
    cancellation: asyncio.CancelledError | None = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
        except BaseException:
            break
    try:
        leftovers = cleanup.result()
    except BaseException as error:
        if cancellation is None:
            raise
        cancellation.add_note(f"endpoint cleanup also failed: {error!r}")
        raise cancellation from error
    if cancellation is not None:
        raise cancellation
    return leftovers


async def _stop_once(
    server: _EndpointServer, accept: threading.Thread, settle_seconds: float
) -> _Leftovers:
    """Retire the endpoint, then close and join everything it still owns.

    Order is the whole point. Retiring is marked first, and cheaply, so
    nothing is accepted or started behind the closes that follow; the
    force-close then wakes whatever is blocked in `accept()`,
    `do_handshake()` or `recv()`, which is what lets `shutdown()` return.
    The second close runs after the join, when only the endpoint itself
    could still be holding a socket.
    """
    server.begin_retiring()
    threads: tuple[str, ...] = ()
    unclosed: tuple[str, ...] = ()
    try:
        await asyncio.to_thread(server.force_close_connections)
        await _stop_accepting(server, accept, settle_seconds)
    finally:
        try:
            threads = await asyncio.to_thread(server.join_workers, settle_seconds)
        finally:
            try:
                unclosed = await asyncio.to_thread(server.close_remaining, settle_seconds)
            finally:
                server.server_close()
    if accept.is_alive():
        threads = (*threads, accept.name)
    return _Leftovers(threads=tuple(sorted(threads)), unclosed=unclosed)


async def _stop_accepting(
    server: _EndpointServer, accept: threading.Thread, settle_seconds: float
) -> None:
    """Stop the accept loop off the event loop, then join its thread.

    `shutdown()` blocks until the accept loop has finished, and that loop
    can be waiting on a handshake whose client half belongs to the event
    loop. Running it on the loop would strand every closure the loop
    still owes, and deadlock outright when the accept thread is waiting
    on one. It is bounded because the endpoint is already retiring and its
    sockets are already closed, so the loop has nothing left to block on.
    """
    if accept.ident is None:
        return
    try:
        await asyncio.to_thread(server.shutdown)
    finally:
        await asyncio.to_thread(accept.join, settle_seconds)


def _report(
    body_error: BaseException | None,
    release_error: BaseException | None,
    cleanup_errors: tuple[BaseException, ...],
    unsettled: _Unsettled,
) -> None:
    """Raise the one failure that outranks the others, noting the rest."""
    primary = _primary(body_error, release_error, cleanup_errors)
    if primary is not None:
        _annotate(primary, body_error, release_error, cleanup_errors)
        if primary is not body_error:
            raise primary  # the body's own exception is already propagating
        return
    problem = unsettled.describe()
    if problem:
        raise AssertionError(problem)


def _primary(
    body_error: BaseException | None,
    release_error: BaseException | None,
    cleanup_errors: tuple[BaseException, ...],
) -> BaseException | None:
    """Pick cancellation, then the body, release, and finally cleanup."""
    ordered = (body_error, release_error, *cleanup_errors)
    for error in ordered:
        if isinstance(error, asyncio.CancelledError):
            # Downgrading a cancellation to a note leaves whoever requested it
            # waiting on a task that quietly decided not to be cancelled.
            return error
    return next((error for error in ordered if error is not None), None)


def _annotate(
    primary: BaseException,
    body_error: BaseException | None,
    release_error: BaseException | None,
    cleanup_errors: tuple[BaseException, ...],
) -> None:
    """Attach the failures that lost to `primary`, so none of them is lost."""
    if release_error is not None and release_error is not primary:
        primary.add_note(f"releasing this endpoint's clients also failed: {release_error!r}")
    if body_error is not None and body_error is not primary:
        primary.add_note(f"this endpoint's body also failed: {body_error!r}")
    for cleanup_error in cleanup_errors:
        if cleanup_error is not primary:
            primary.add_note(f"cleaning up this endpoint also failed: {cleanup_error!r}")


@dataclass(frozen=True)
class _Leftovers:
    """What teardown could not get rid of: threads, and sockets."""

    threads: tuple[str, ...]
    unclosed: tuple[str, ...]


@dataclass(frozen=True)
class _Unsettled:
    """Everything a clean run has to be able to say nothing about."""

    stranded: tuple[str, ...]
    leftovers: _Leftovers
    failures: tuple[str, ...]
    settle_seconds: float
    reason: str | None

    def describe(self) -> str:
        """The ownership failure, or `""` when the endpoint settled."""
        detail = self._detail()
        if not detail:
            return ""
        subject = "" if self.reason is None else f" (intentional disconnect: {self.reason})"
        return (
            f"the endpoint{subject} {'; '.join(detail)}."
            " Its clients have to close before the endpoint context exits, or the"
            " transports they left behind are finalized against a peer that is gone (#390)."
        )

    def _detail(self) -> list[str]:
        detail = []
        if self.stranded:
            detail.append(
                f"still held {len(self.stranded)} connection(s) after {self.settle_seconds}s:"
                f" {_peers(self.stranded)}"
            )
        if self.leftovers.threads:
            detail.append(f"still ran {', '.join(self.leftovers.threads)}")
        if self.leftovers.unclosed:
            detail.append(f"could not close {_peers(self.leftovers.unclosed)}")
        if self.failures:
            detail.append(f"failed while serving: {'; '.join(self.failures)}")
        return detail


def _peers(peers: tuple[str, ...]) -> str:
    """Name a few peers and count the rest: a report, not a transcript."""
    named = ", ".join(peers[:_MAX_REPORTED_PEERS])
    hidden = len(peers) - _MAX_REPORTED_PEERS
    return named if hidden <= 0 else f"{named} (+{hidden} more)"


def _dropped(why: str) -> OSError:
    """The error `socketserver` expects when an accepted connection is not served."""
    return OSError(why)


def _close(connection: socket.socket) -> None:
    with suppress(OSError):
        connection.close()


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
