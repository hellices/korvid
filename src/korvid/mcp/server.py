"""Embedded MCP server: korvid's agent tools for external AI hosts.

External MCP hosts (VS Code Copilot Chat, Claude Code, Cursor, Zed) connect
over Streamable HTTP (MCP spec 2025-06-18) and drive the *running* TUI
through the same :class:`~korvid.tools.executor.ToolExecutor` the built-in
agent uses - navigation, filters, log panes and describe views happen on
the screen the user is already watching.

The server binds to loopback only and enforces Host/Origin validation
(DNS-rebinding protection - the MCP spec requires both for local servers)
and exposes read + UI-drive tools
exclusively: cluster write tools stay with the built-in agent until an
approval UX for external callers is designed. On startup the actual
endpoint is published to a small discovery file (see
:func:`default_endpoint_path`) so wrapper scripts can auto-configure hosts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from korvid.core.audit import interprocess_lock
from korvid.core.mcp import MCPControllerBase
from korvid.tools.executor import PROPOSAL_TOOL_NAMES, ToolExecutor

logger = logging.getLogger(__name__)

#: Loopback only: local MCP servers must never listen on external interfaces.
_HOST = "127.0.0.1"

DEFAULT_MCP_PORT = 7878

#: DNS-rebinding protection: loopback binding alone is not enough - a hostile
#: webpage can still issue requests to 127.0.0.1, so the transport must also
#: validate Host and Origin headers (MCP Streamable HTTP requirement).
_SECURITY_SETTINGS = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
)


def default_endpoint_path() -> Path:
    """XDG state dir (falls back to ~/.local/state) / korvid/mcp-endpoint.json."""
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "korvid" / "mcp-endpoint.json"


def _endpoint_lock_path(endpoint_path: Path) -> Path:
    """Sibling lock file serializing endpoint publication/removal across
    korvid processes."""
    return endpoint_path.with_name(endpoint_path.name + ".lock")


def _load_registry(path: Path) -> dict[str, Any] | None:
    """Parse the discovery registry; None when absent, torn, or when the
    file holds foreign (non-registry) data that must be left untouched."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("servers"), dict):
        return data
    return None


def _replace_atomically(path: Path, registry: dict[str, Any]) -> None:
    """Temp file + rename so readers never observe a torn record.

    Owner-only mode: the registry may carry a write-proposal capability
    token (issue #110), so the file must be *created* 0600 via an atomic
    open — a chmod after a umask-default create would leave the token
    briefly world-readable at a predictable path."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)  # a stale tmp could carry a foreign mode
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(registry))
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)


class KorvidMCPServer:
    """Streamable HTTP MCP server wrapping the agent tool surface.

    The tool definitions are the OpenAI-style schemas from
    ``agent/tools.py`` - the single source of truth; this class only
    translates them to MCP ``Tool`` objects and forwards calls to the
    shared :class:`ToolExecutor` (which serializes UI actions through the
    app's own locks, so external hosts and the built-in agent coexist).
    """

    def __init__(
        self,
        executor: ToolExecutor,
        tools: list[dict[str, Any]],
        *,
        port: int = DEFAULT_MCP_PORT,
        endpoint_path: Path | None = None,
        capability_token: str | None = None,
    ) -> None:
        self._executor = executor
        self._tools = list(tools)
        self._tool_names = {t["function"]["name"] for t in self._tools}
        self._port = port
        self._endpoint_path = endpoint_path
        #: Per-run secret gating the write-proposal tools (issue #110): it is
        #: published only in the owner-readable endpoint file, so a caller
        #: echoing it has proven local same-user file access — the same trust
        #: level as the kubeconfig itself. None means proposals are disabled.
        self._capability_token = capability_token
        #: Transport is stateless (no persistent MCP session), so proposals
        #: are keyed to the server run: one id per start, injected
        #: server-side and never taken from the caller. Every caller of one
        #: run therefore shares this identity — by construction they all
        #: hold the same capability token from the same owner-only file, so
        #: they are a single local trust domain: the per-session pending cap
        #: degenerates to a per-run cap and any authorized caller may cancel
        #: (cancellation never executes anything, so it is fail-safe).
        self._session_id = f"mcp-{os.getpid()}-{secrets.token_urlsafe(8)}"
        self._started: anyio.Event = anyio.Event()
        self._bound_port: int | None = None
        self._uvicorn: uvicorn.Server | None = None
        self._shutdown_requested = False
        self._server: Server[Any, Any] = Server("korvid")
        self._server.list_tools()(self.list_tools)  # type: ignore[no-untyped-call]  # SDK decorator factory is untyped
        self._server.call_tool()(self.call_tool)

    async def list_tools(self) -> list[types.Tool]:
        """MCP ``tools/list``: mirror the agent tool definitions 1:1."""
        return [
            types.Tool(
                name=t["function"]["name"],
                description=t["function"]["description"],
                inputSchema=t["function"]["parameters"],
            )
            for t in self._tools
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None
    ) -> list[types.TextContent]:
        """MCP ``tools/call``: dispatch through the shared executor.

        Discovery is not an authorization boundary - callers choose ``name``
        freely and the shared :class:`ToolExecutor` also knows the write
        tools, so anything outside this server's configured surface is
        rejected *before* dispatch.

        ``ToolExecutor.execute`` never raises - failures come back as
        ``"ERROR: ..."`` strings, which is exactly what the MCP host should
        see (same contract as the built-in agent loop).
        """
        if name not in self._tool_names:
            return [
                types.TextContent(type="text", text=f"ERROR: tool not available over MCP: {name}")
            ]
        # Underscore-prefixed keys are reserved for server-side injection
        # (transport identity); strip whatever the caller sent so nothing in
        # the executor ever trusts caller-controlled identity metadata.
        args = {k: v for k, v in (arguments or {}).items() if not k.startswith("_")}
        if name in PROPOSAL_TOOL_NAMES:
            error = self._authorize_proposal_call(args)
            if error is not None:
                return [types.TextContent(type="text", text=error)]
            client_name, client_version = self._client_info()
            args["_session_id"] = self._session_id
            args["_client_name"] = client_name
            args["_client_version"] = client_version
        result = await self._executor.execute(name, args)
        return [types.TextContent(type="text", text=result)]

    def _authorize_proposal_call(self, args: dict[str, Any]) -> str | None:
        """Capability check for the write-proposal tools; error text or None.

        Pops ``capability`` from ``args`` so the secret never reaches the
        executor, the store, or a log line. Constant-time comparison: the
        token is the only thing standing between a local process and the
        proposal queue, so it must not be guessable byte-by-byte.
        """
        supplied = args.pop("capability", None)
        if self._capability_token is None:
            return "ERROR: write proposals are not enabled on this server"
        if not isinstance(supplied, str) or not secrets.compare_digest(
            supplied, self._capability_token
        ):
            return "ERROR: invalid or missing capability token"
        return None

    def _client_info(self) -> tuple[str, str]:
        """Best-effort caller identity from the MCP initialize handshake.

        Display metadata only — never an authorization input (any caller can
        claim any name). Stateless transport may not carry it; degrade to
        empty strings."""
        try:
            params = self._server.request_context.session.client_params
            info = params.clientInfo if params is not None else None
        except (LookupError, AttributeError):
            return "", ""
        if info is None:
            return "", ""
        return str(info.name), str(info.version)

    @property
    def bound_port(self) -> int | None:
        """Actual TCP port once started; None before startup completes."""
        return self._bound_port

    async def wait_started(self) -> int:
        """Block until the HTTP server is accepting connections; return the
        bound port (useful when constructed with ``port=0``).

        Raises RuntimeError if startup failed (bind error) - the event is
        set either way so callers never hang on a server that will not come
        up."""
        await self._started.wait()
        if self._bound_port is None:
            raise RuntimeError(f"MCP server failed to start on {_HOST}:{self._port}")
        return self._bound_port

    def request_shutdown(self) -> None:
        """Ask the HTTP server to exit gracefully; ``run()`` then returns.

        Preferred over cancelling the ``run()`` task: a hard cancel tears
        down uvicorn mid-request and leaks sockets/streams as warnings.
        Safe to call before ``run()`` has started - the flag is re-checked
        once the uvicorn server exists.
        """
        self._shutdown_requested = True
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True

    async def run(self) -> None:
        """Serve until cancelled (run as a background task in the app loop)."""
        # json_response=True: our tools are single request/response - no
        # server->client streaming - and the SDK's SSE path (1.28.x) leaks
        # its sse_stream_reader on normal completion (ResourceWarning),
        # while the JSON path cleans up its streams in a finally block.
        manager = StreamableHTTPSessionManager(
            app=self._server,
            stateless=True,
            json_response=True,
            security_settings=_SECURITY_SETTINGS,
        )

        async def handle(scope: Scope, receive: Receive, send: Send) -> None:
            await manager.handle_request(scope, receive, send)

        @contextlib.asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            async with manager.run():
                yield

        app = Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)
        config = uvicorn.Config(app, host=_HOST, port=self._port, log_level="error")
        server = uvicorn.Server(config)
        self._uvicorn = server
        # Close the startup/shutdown race: a request_shutdown() issued
        # before this point set only the flag, so mirror it now.
        if self._shutdown_requested:
            server.should_exit = True
        # korvid owns the terminal: uvicorn's SIGINT/SIGTERM capture would
        # fight the TUI for signal handling, so neutralize it.
        server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign, assignment]  # TUI owns signals

        async def _publish_when_started() -> None:
            while not server.started:
                await anyio.sleep(0.02)
            try:
                self._bound_port = self._actual_port(server)
            except RuntimeError:
                # Startup lost the race against a pre-run request_shutdown():
                # the sockets are already gone, nothing to publish.
                return
            # File I/O + interprocess flock may block on a contending korvid
            # instance - never on the event-loop thread.
            await asyncio.to_thread(self._write_endpoint, self._bound_port)
            self._started.set()

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_publish_when_started)
                try:
                    await server.serve()
                except SystemExit:
                    # uvicorn raises SystemExit when it cannot bind (port in
                    # use).  Catch it here, before the task group would wrap
                    # it in a BaseExceptionGroup that escapes the caller's
                    # shutdown path - the TUI must keep running without MCP.
                    logger.error(
                        "MCP server failed to start on %s:%d (port in use?)",
                        _HOST,
                        self._port,
                    )
                finally:
                    # Wake anyone blocked in wait_started(); with _bound_port
                    # unset they get a RuntimeError instead of hanging.
                    self._started.set()
                    tg.cancel_scope.cancel()
        finally:
            # Same offload as publication: the flock may wait on another
            # korvid instance.  Shielded + suppressed so a cancellation
            # arriving mid-cleanup still lets the worker thread finish
            # (and the original CancelledError, if any, re-raises after
            # this finally as usual).
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(asyncio.to_thread(self._remove_endpoint))

    @staticmethod
    def _actual_port(server: uvicorn.Server) -> int:
        for srv in server.servers:
            for sock in srv.sockets:
                port = sock.getsockname()[1]
                return int(port)
        raise RuntimeError("uvicorn reported started without a bound socket")

    def _write_endpoint(self, port: int) -> None:
        """Publish this instance into the discovery registry (best-effort:
        the server is useful even when the state dir is not writable).

        The file holds a ``{"servers": {"<pid>": {...}}}`` registry so that
        concurrent korvid instances each own one entry: publishing merges
        under a cross-process lock and never erases another live instance's
        record.  Runs on a worker thread - the lock may block on a
        contending process."""
        path = self._endpoint_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with interprocess_lock(_endpoint_lock_path(path)):
                registry = _load_registry(path)
                if registry is None:  # absent, torn, or foreign data
                    registry = {"servers": {}}
                registry["servers"][str(os.getpid())] = {
                    "url": f"http://{_HOST}:{port}/mcp",
                    "port": port,
                    "pid": os.getpid(),
                    **(
                        {"capability": self._capability_token}
                        if self._capability_token is not None
                        else {}
                    ),
                }
                _replace_atomically(path, registry)
        except OSError:
            logger.warning("could not write MCP endpoint file %s", path)

    def _remove_endpoint(self) -> None:
        """Drop only *our* registry entry on exit: other live instances (and
        any foreign/non-registry data) are preserved.  The read-modify-write
        runs under the same cross-process lock as publication, so an entry
        added between the read and the write cannot be lost.  Runs on a
        worker thread."""
        path = self._endpoint_path
        if path is None:
            return
        try:
            with interprocess_lock(_endpoint_lock_path(path)):
                registry = _load_registry(path)
                if registry is None:
                    return
                entry = registry["servers"].get(str(os.getpid()))
                if not isinstance(entry, dict) or entry.get("port") != self._bound_port:
                    return
                del registry["servers"][str(os.getpid())]
                if registry["servers"]:
                    _replace_atomically(path, registry)
                else:
                    path.unlink(missing_ok=True)
        except OSError:
            return


class MCPController(MCPControllerBase):
    """Runtime lifecycle for the embedded MCP server.

    Backs the TUI's ``:mcp`` command and status display: start/stop the
    server while korvid runs and report its state.  Each start builds a
    fresh :class:`KorvidMCPServer` via the injected factory - uvicorn
    servers are single-use.
    """

    def __init__(self, factory: Callable[[], KorvidMCPServer]) -> None:
        self._factory = factory
        self._server: KorvidMCPServer | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> str:
        """One-line state for the status bar / bare ``:mcp``."""
        if self.running and self._server is not None:
            port = self._server.bound_port
            return f"MCP on :{port}" if port is not None else "MCP starting"
        return "MCP off"

    async def start(self) -> str:
        """Start the server; return a user-facing status/error line."""
        if self._task is not None:
            if not self._task.done():
                return self.status()
            self._consume_result(self._task)
            self._server = None
            self._task = None
        server = self._factory()
        task = asyncio.create_task(server.run())
        self._server = server
        self._task = task
        try:
            port = await asyncio.wait_for(server.wait_started(), timeout=10)
        except (TimeoutError, RuntimeError):
            # Bind failure: run() is already returning on its own.  Reap it
            # with a non-cancelling deadline (cancelling and awaiting the
            # cancellation could hang in stream cleanup); if it is somehow
            # still pending, ownership is retained so a later shutdown can
            # finish the job instead of orphaning the task.
            server.request_shutdown()
            done, _ = await asyncio.wait({task}, timeout=5)
            if done:
                self._consume_result(task)
                self._server = None
                self._task = None
            return "ERROR: MCP failed to start (port in use?)"
        return f"MCP on :{port}"

    async def stop(self) -> str:
        """Gracefully stop the server; bounded so the TUI never blocks."""
        pending = await self.shutdown()
        if pending is not None:
            # shutdown() kept ownership, so the eventual completion stays
            # observable (and awaitable at app teardown) instead of orphaned.
            return "MCP stopping (cleanup is taking long)"
        return "MCP off"

    async def shutdown(self) -> asyncio.Task[None] | None:
        """Stop the server with bounded waits; never raises.

        Cancellation-safe: ownership is cleared only once the task is
        *observed* done, so a ``:mcp off`` worker cancelled mid-wait (or a
        timed-out earlier attempt) leaves the references in place for the
        next shutdown to find.  Returns the still-pending task if even
        cancellation did not land within its deadline, so the caller can
        await it after more urgent cleanup - abandoning it would leave
        asyncio.run()'s final task-gathering to block on it invisibly.
        """
        server, task = self._server, self._task
        if server is None or task is None:
            return None
        if not task.done():
            server.request_shutdown()
            done, _ = await asyncio.wait({task}, timeout=5)
            if not done:
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=5)
            if not done:
                return task
        self._consume_result(task)
        self._server = None
        self._task = None
        return None

    @staticmethod
    def _consume_result(task: asyncio.Task[None] | None) -> None:
        if task is None or not task.done() or task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("MCP server task failed", exc_info=exc)
