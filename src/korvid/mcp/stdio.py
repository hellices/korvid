"""Thin stdio transport for the authenticated MCP server owned by a running TUI."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, NoReturn

import anyio
import httpx2
from mcp import ClientSession, MCPError, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from korvid import __version__
from korvid.mcp._stdio_input import cancellable_stdin
from korvid.mcp._stdio_output import cancellable_stdout
from korvid.mcp.registry import TUIEndpoint, read_endpoints, select_endpoint

_CONNECT_TIMEOUT = 10.0
_CALL_TIMEOUT = 120.0
_CONNECTION_FAILURE = (
    "Cannot communicate with the selected Korvid TUI; enable MCP in that TUI "
    "and restart this stdio connection after a context switch, MCP restart, or TUI restart."
)
_TRANSPORT_ERRORS = (
    httpx2.HTTPError,
    MCPError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
    OSError,
    TimeoutError,
)


class StdioBridgeError(RuntimeError):
    """A content-free, actionable error from the local stdio bridge."""


def _raise_transport_failure(group: BaseExceptionGroup) -> NoReturn:
    _handled, remaining = group.split(_TRANSPORT_ERRORS)
    if remaining is not None:
        raise remaining from None
    raise StdioBridgeError(_CONNECTION_FAILURE) from None


def _raise_stdio_failure(group: BaseExceptionGroup) -> NoReturn:
    task = asyncio.current_task()
    _closed, remaining = group.split((anyio.BrokenResourceError, anyio.ClosedResourceError))
    # SDK internal memory-stream shutdown can replace the main task's
    # cancellation with a closed-stream error. Never hide unrelated failures.
    if task is not None and task.cancelling() and remaining is None:
        raise asyncio.CancelledError from None
    raise group


class _Proxy:
    def __init__(self, http: httpx2.AsyncClient, url: str) -> None:
        self._http = http
        self._url = url
        self.server: Server[Any] = Server(
            "korvid-stdio",
            version=__version__,
            on_list_tools=self.list_tools,
            on_call_tool=self.call_tool,
        )

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[ClientSession]:
        # The TUI endpoint is stateless. Request-owned sessions keep a failed
        # HTTP task group from cancelling the unrelated stdio receive loop.
        try:
            async with (
                streamable_http_client(
                    self._url, http_client=self._http, terminate_on_close=False
                ) as (read, write),
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=_CALL_TIMEOUT,
                    client_info=types.Implementation(name="korvid-stdio", version=__version__),
                ) as session,
            ):
                async with asyncio.timeout(_CONNECT_TIMEOUT):
                    await session.initialize()
                yield session
        except _TRANSPORT_ERRORS:
            raise StdioBridgeError(_CONNECTION_FAILURE) from None
        except BaseExceptionGroup as group:
            _raise_transport_failure(group)

    async def list_tools(
        self,
        ctx: ServerRequestContext[Any],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        async with self.connect() as session:
            return await session.list_tools(params=params)

    async def call_tool(
        self, ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        try:
            async with self.connect() as session:
                result = await session.call_tool(
                    params.name, params.arguments, read_timeout_seconds=_CALL_TIMEOUT
                )
        except StdioBridgeError:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"ERROR: {_CONNECTION_FAILURE}")],
                is_error=True,
            )
        if not isinstance(result, types.CallToolResult):
            raise StdioBridgeError("The selected TUI returned an unsupported tool response.")
        return result


async def _drain_http_response(response: httpx2.Response) -> None:
    # The SDK skips notification/error bodies. Consume finite responses before
    # it closes the stream, rather than discarding an unread TCP response.
    if not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
        await response.aread()


async def _serve(endpoint: TUIEndpoint) -> None:
    # The registry only accepts loopback /mcp URLs. Normalize its optional
    # trailing slash locally rather than following an authenticated redirect.
    url = endpoint.url.rstrip("/") + "/"
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {endpoint.capability}"},
        timeout=httpx2.Timeout(_CALL_TIMEOUT, connect=_CONNECT_TIMEOUT),
        follow_redirects=False,
        trust_env=False,
        event_hooks={"response": [_drain_http_response]},
    ) as http:
        proxy = _Proxy(http, url)
        async with asyncio.timeout(_CONNECT_TIMEOUT), proxy.connect() as session:
            # initialize() queues its notification without awaiting the HTTP
            # acknowledgement. A round trip drains it before SDK teardown can
            # cancel that POST and reset the TUI's accepted TCP connection.
            await session.send_ping()
        with anyio.CancelScope() as lifetime:
            async with (
                cancellable_stdin(sys.stdin) as source,
                cancellable_stdout(sys.stdout) as sink,
            ):
                try:
                    async with stdio_server(stdin=source, stdout=sink) as (stdin, stdout):
                        await proxy.server.run(
                            stdin, stdout, proxy.server.create_initialization_options()
                        )
                        # Host EOF disconnects the session, including a writer
                        # whose host has stopped consuming the output pipe.
                        lifetime.cancel()
                except BaseExceptionGroup as group:
                    _raise_stdio_failure(group)


def run_stdio(*, instance: int | None = None) -> None:
    """Connect stdio to exactly one existing TUI without starting application services."""
    endpoint = select_endpoint(read_endpoints(), instance)
    try:
        asyncio.run(_serve(endpoint))
    except KeyboardInterrupt:
        raise StdioBridgeError("MCP stdio connection interrupted.") from None
    except _TRANSPORT_ERRORS:
        raise StdioBridgeError(_CONNECTION_FAILURE) from None
    except BaseExceptionGroup as group:
        _raise_transport_failure(group)
