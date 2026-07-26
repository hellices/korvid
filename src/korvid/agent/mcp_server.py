"""Embedded MCP server: korvid's agent tools for external AI hosts.

External MCP hosts (VS Code Copilot Chat, Claude Code, Cursor, Zed) connect
over Streamable HTTP (MCP spec 2025-06-18) and drive the *running* TUI
through the same :class:`~korvid.agent.tools.ToolExecutor` the built-in
agent uses - navigation, filters, log panes and describe views happen on
the screen the user is already watching.

The server binds to loopback only (the MCP spec requires this for local
servers to prevent DNS-rebinding attacks) and exposes read + UI-drive tools
exclusively: cluster write tools stay with the built-in agent until an
approval UX for external callers is designed. On startup the actual
endpoint is published to a small discovery file (see
:func:`default_endpoint_path`) so wrapper scripts can auto-configure hosts.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from korvid.agent.tools import ToolExecutor

logger = logging.getLogger(__name__)

#: Loopback only: local MCP servers must never listen on external interfaces.
_HOST = "127.0.0.1"

DEFAULT_MCP_PORT = 7878


def default_endpoint_path() -> Path:
    """XDG state dir (falls back to ~/.local/state) / korvid/mcp-endpoint.json."""
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "korvid" / "mcp-endpoint.json"


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
        self._port = port
        self._endpoint_path = endpoint_path
        self._started: anyio.Event = anyio.Event()
        self._bound_port: int | None = None
        self._uvicorn: uvicorn.Server | None = None
        self._server: Server[Any, Any] = Server("korvid")
        self._server.list_tools()(self.list_tools)  # type: ignore[no-untyped-call]
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

        ``ToolExecutor.execute`` never raises - failures come back as
        ``"ERROR: ..."`` strings, which is exactly what the MCP host should
        see (same contract as the built-in agent loop).
        """
        result = await self._executor.execute(name, arguments or {})
        return [types.TextContent(type="text", text=result)]

    async def wait_started(self) -> int:
        """Block until the HTTP server is accepting connections; return the
        bound port (useful when constructed with ``port=0``)."""
        await self._started.wait()
        if self._bound_port is None:  # pragma: no cover - set before _started
            raise RuntimeError("MCP server signalled start without a bound port")
        return self._bound_port

    def request_shutdown(self) -> None:
        """Ask the HTTP server to exit gracefully; ``run()`` then returns.

        Preferred over cancelling the ``run()`` task: a hard cancel tears
        down uvicorn mid-request and leaks sockets/streams as warnings.
        """
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True

    async def run(self) -> None:
        """Serve until cancelled (run as a background task in the app loop)."""
        manager = StreamableHTTPSessionManager(app=self._server, stateless=True)

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
        # korvid owns the terminal: uvicorn's SIGINT/SIGTERM capture would
        # fight the TUI for signal handling, so neutralize it.
        server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign, assignment]

        async def _publish_when_started() -> None:
            while not server.started:
                await anyio.sleep(0.02)
            self._bound_port = self._actual_port(server)
            self._write_endpoint(self._bound_port)
            self._started.set()

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_publish_when_started)
                await server.serve()
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
        server is useful even when the state dir is not writable)."""
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
            path.write_text(json.dumps(payload))
        except OSError:
            logger.warning("could not write MCP endpoint file %s", path)

    def _remove_endpoint(self) -> None:
        path = self._endpoint_path
        if path is None:
            return
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
