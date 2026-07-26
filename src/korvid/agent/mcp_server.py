"""Embedded MCP server: korvid's agent tools for external AI hosts.

External MCP hosts (VS Code Copilot Chat, Claude Code, Cursor, Zed) connect
over Streamable HTTP (MCP spec 2025-06-18) and drive the *running* TUI
through the same :class:`~korvid.agent.tools.ToolExecutor` the built-in
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

from korvid.agent.tools import ToolExecutor
from korvid.core.audit import interprocess_lock

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
    ) -> None:
        self._executor = executor
        self._tools = list(tools)
        self._tool_names = {t["function"]["name"] for t in self._tools}
        self._port = port
        self._endpoint_path = endpoint_path
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
        result = await self._executor.execute(name, arguments or {})
        return [types.TextContent(type="text", text=result)]

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
            self._write_endpoint(self._bound_port)
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
            self._remove_endpoint()

    @staticmethod
    def _actual_port(server: uvicorn.Server) -> int:
        for srv in server.servers:
            for sock in srv.sockets:
                port = sock.getsockname()[1]
                return int(port)
        raise RuntimeError("uvicorn reported started without a bound socket")

    def _write_endpoint(self, port: int) -> None:
        """Publish the endpoint for host auto-discovery (best-effort: the
        server is useful even when the state dir is not writable).

        Publication and removal are serialized across processes via a
        sibling lock file, and the record is written atomically (temp file
        + rename), so concurrent korvid instances never tear or lose each
        other's records."""
        path = self._endpoint_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "url": f"http://{_HOST}:{port}/mcp",
                "port": port,
                "pid": os.getpid(),
            }
            with interprocess_lock(_endpoint_lock_path(path)):
                tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(payload))
                tmp.replace(path)
        except OSError:
            logger.warning("could not write MCP endpoint file %s", path)

    def _remove_endpoint(self) -> None:
        """Remove the discovery file, but only if it is still *ours*: another
        korvid instance may have published a newer record at the same
        default path, and exiting must not delete that.  The read-check-
        unlink runs under the same cross-process lock as publication, so a
        record replaced between the read and the unlink cannot be lost."""
        path = self._endpoint_path
        if path is None:
            return
        try:
            with interprocess_lock(_endpoint_lock_path(path)):
                info = json.loads(path.read_text())
                if (
                    isinstance(info, dict)
                    and info.get("pid") == os.getpid()
                    and info.get("port") == self._bound_port
                ):
                    path.unlink(missing_ok=True)
        except (OSError, ValueError):
            return


class MCPController:
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
        if self.running:
            return self.status()
        server = self._factory()
        task = asyncio.create_task(server.run())
        self._server = server
        self._task = task
        try:
            port = await asyncio.wait_for(server.wait_started(), timeout=10)
        except (TimeoutError, RuntimeError):
            # Bind failure: run() already logged and is returning; reap the
            # task so the failure is fully consumed.
            self._server = None
            self._task = None
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=5)
            return "ERROR: MCP failed to start (port in use?)"
        return f"MCP on :{port}"

    async def stop(self) -> str:
        """Gracefully stop the server; bounded so the TUI never blocks."""
        pending = await self.shutdown()
        if pending is not None:
            # Keep the reference so the eventual completion is observable
            # (and awaitable at process exit) instead of orphaned.
            self._task = pending
            return "MCP stopping (cleanup is taking long)"
        return "MCP off"

    async def shutdown(self) -> asyncio.Task[None] | None:
        """Stop the server with bounded waits; never raises.

        Returns the still-pending task if even cancellation did not land
        within its deadline, so the caller can decide to await it after
        more urgent cleanup - abandoning it would leave asyncio.run()'s
        final task-gathering to block on it invisibly.
        """
        server, task = self._server, self._task
        self._server = None
        self._task = None
        if server is None or task is None or task.done():
            self._consume_result(task)
            return None
        server.request_shutdown()
        done, _ = await asyncio.wait({task}, timeout=5)
        if not done:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=5)
        if not done:
            return task
        self._consume_result(task)
        return None

    @staticmethod
    def _consume_result(task: asyncio.Task[None] | None) -> None:
        if task is None or not task.done() or task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("MCP server task failed", exc_info=exc)
