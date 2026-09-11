"""Embedded MCP server: korvid's agent tools for external AI hosts.

External MCP hosts (VS Code Copilot Chat, Claude Code, Cursor, Zed) connect
over Streamable HTTP (MCP spec 2025-06-18) and drive the *running* TUI
through the same :class:`~korvid.tools.executor.ToolExecutor` the built-in
agent uses - navigation, filters, log panes and describe views happen on
the screen the user is already watching.

The server binds to loopback, authenticates the internal bridge with a per-run
capability, and preserves Host/Origin validation. Reads, UI actions and opt-in
write proposals share the running TUI's existing approval and audit boundaries.
Startup succeeds only after the owner-only endpoint registry is published.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from mcp import types
from mcp.server import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import (
    TransportSecurityMiddleware,
    TransportSecuritySettings,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from korvid.core.audit import interprocess_lock
from korvid.core.mcp import MCPControllerBase
from korvid.mcp.registry import (
    EndpointRegistryError,
    endpoint_record,
    load_registry_for_update,
    open_private_file,
    serialize_registry,
    validate_capability_token,
)
from korvid.mcp.registry import default_endpoint_path as default_endpoint_path
from korvid.tools.executor import (
    PROPOSAL_TOOL_NAMES,
    ToolExecutor,
    ToolOutcome,
    ToolResultBlocked,
    UIBridge,
    cap_result,
)
from korvid.tools.follow import mirror_read, read_summary
from korvid.tools.registry import TOOLS_BY_NAME
from korvid.tools.structured import ERROR_PREFIX

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


def _endpoint_lock_path(endpoint_path: Path) -> Path:
    """Sibling lock file serializing endpoint publication/removal across
    korvid processes."""
    return endpoint_path.with_name(endpoint_path.name + ".lock")


def _replace_atomically(path: Path, registry: dict[str, Any]) -> None:
    """Temp file + rename so readers never observe a torn record.

    The credential file must be private before writing: POSIX 0600 or a
    protected Windows DACL, never a post-write permission change.
    """
    payload = serialize_registry(registry)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)  # a stale tmp could carry a foreign mode
    fd = open_private_file(tmp)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


async def _await_owned_task(task: asyncio.Task[None]) -> None:
    """Defer cancellation until owned cleanup, including its threads, finishes."""
    cancelled: asyncio.CancelledError | None = None
    # AnyIO level cancellation must not spin at each await; asyncio callers
    # can still cancel repeatedly, so shield and retain the task on every wait.
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
    task.result()
    if cancelled is not None:
        raise cancelled


def _sanitize_client_meta(value: object, *, limit: int = 120) -> str:
    """Bound and flatten caller-supplied clientInfo metadata.

    The value crosses into approval dialogs (whose safety bindings are one
    line each), the status bar and audit records: replace every
    non-printable character (newlines, ANSI escapes) with a space and cap
    the length so a hostile caller cannot inject dialog lines or bloat
    audit entries.
    """
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return text[:limit]


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
        ui: UIBridge | None = None,
        follow_enabled: Callable[[], bool] | None = None,
        note_activity: Callable[[str], None] | None = None,
    ) -> None:
        self._executor = executor
        self._tools = list(tools)
        self._tool_names = {t["function"]["name"] for t in self._tools}
        self._port = port
        self._endpoint_path = endpoint_path or default_endpoint_path()
        #: The stdio adapter consumes this internal bridge credential from the
        #: private registry. It is never a model-visible authentication flow.
        self._capability_token = (
            secrets.token_urlsafe(32) if capability_token is None else capability_token
        )
        validate_capability_token(self._capability_token)
        #: MCP follow mode (issue #153): mirror external cluster reads in
        #: the TUI. All three are optional wiring from the composition root;
        #: without them reads stay response-only (the pre-#153 behavior).
        self._ui = ui
        self._follow_enabled = follow_enabled
        self._note_activity = note_activity
        #: Strong refs to in-flight fire-and-forget mirror tasks (asyncio
        #: keeps only weak refs; an unreferenced task can be GC-collected
        #: mid-flight).
        self._follow_tasks: set[asyncio.Task[Any]] = set()
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
        self._endpoint_port: int | None = None
        self._publication_task: asyncio.Task[None] | None = None
        self._startup_error: str | None = None
        self._uvicorn: uvicorn.Server | None = None
        self._shutdown_requested = False
        # mcp 2.x registers handlers as constructor callbacks rather than
        # decorators, and hands each one the request context instead of
        # exposing it as ambient state.
        self._server: Server[Any] = Server(
            "korvid",
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )

    async def _on_list_tools(
        self,
        ctx: ServerRequestContext[Any],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        """SDK adapter for ``tools/list``; the surface itself is unpaginated."""
        return types.ListToolsResult(tools=await self.list_tools())

    async def _on_call_tool(
        self,
        ctx: ServerRequestContext[Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        """SDK adapter for ``tools/call``.

        Failures stay in-band: the text is the same ``"ERROR: ..."`` the
        built-in agent loop reads, so a model can act on the reason. But
        ``is_error`` is the spec's own signal for that case, not a
        transport-level failure, and a host that trusts it would otherwise
        record a refused proposal as a successful call.

        The verdict comes from whoever produced the text, never from the
        text: ``get_logs`` returns raw log lines, so a pod logging
        ``ERROR: connection refused`` must not be reported as a failed
        call. Pre-dispatch refusals have no producer and say so themselves.
        """
        content, failed = await self._dispatch(params.name, params.arguments, ctx)
        return types.CallToolResult(content=list(content), is_error=failed)

    async def list_tools(self) -> list[types.Tool]:
        """MCP ``tools/list``: mirror the agent tool definitions 1:1."""
        return [
            types.Tool(
                name=t["function"]["name"],
                description=t["function"]["description"],
                input_schema=t["function"]["parameters"],
            )
            for t in self._tools
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        ctx: ServerRequestContext[Any] | None = None,
    ) -> list[types.TextContent]:
        """MCP ``tools/call``: the model-visible content of one dispatch."""
        content, _ = await self._dispatch(name, arguments, ctx)
        return content

    async def _dispatch(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        ctx: ServerRequestContext[Any] | None = None,
    ) -> tuple[list[types.TextContent], bool]:
        """Dispatch one tool call: its content, and whether it failed.

        Discovery is not an authorization boundary - callers choose ``name``
        freely and the shared :class:`ToolExecutor` also knows the write
        tools, so anything outside this server's configured surface is
        rejected *before* dispatch.

        `execute_recorded()` returns a structured `ToolOutcome`; its `error`
        bit is authoritative even when external text begins with ``ERROR:``.
        A blocked structured result raises `ToolResultBlocked`, which this
        boundary converts into a bounded failed outcome for the MCP host.
        """
        if name not in self._tool_names:
            text = f"ERROR: tool not available over MCP: {name}"
            return [types.TextContent(type="text", text=text)], True
        # Underscore-prefixed keys are reserved for server-side injection
        # (transport identity); strip whatever the caller sent so nothing in
        # the executor ever trusts caller-controlled identity metadata.
        args = {k: v for k, v in (arguments or {}).items() if not k.startswith("_")}
        if name in PROPOSAL_TOOL_NAMES:
            if "capability" in args:
                return [
                    types.TextContent(
                        type="text",
                        text="ERROR: capability is transport-only; use korvid mcp stdio",
                    )
                ], True
            client_name, client_version = self._client_info(ctx)
            args["_session_id"] = self._session_id
            args["_client_name"] = client_name
            args["_client_version"] = client_version
        try:
            outcome = await self._executor.execute_recorded(name, args)
        except ToolResultBlocked as exc:
            # `execute_recorded` raises this so the agent can stop its turn;
            # an MCP host has no turn to stop, and this boundary must not
            # start raising (PR #197 review). The string names the shape
            # that failed, never the document behind it.
            outcome = ToolOutcome(text=cap_result(f"{ERROR_PREFIX} {exc}"), error=True)
        self._surface_read(name, args, outcome, ctx)
        return (
            [types.TextContent(type="text", text=outcome.text)],
            outcome.error,
        )

    def _surface_read(
        self,
        name: str,
        args: dict[str, Any],
        outcome: ToolOutcome,
        ctx: ServerRequestContext[Any] | None = None,
    ) -> None:
        """Follow mode (issue #153): make external cluster reads visible.

        With follow on and a successful read, mirror it in the TUI as a
        fire-and-forget task - the MCP response neither waits on nor fails
        with the UI action. Otherwise (follow off, no bridge, or a failed
        read that must not steer the screen to a view it never loaded),
        degrade to a transient activity note so the read is still seen.
        ``ui_only`` tools already move the screen visibly; surfacing them
        again would be noise.

        Success is the producer's verdict, not the prefix of the text - a
        pod that logs ``ERROR:`` on its first line still deserves its
        mirror.
        """
        tool = TOOLS_BY_NAME.get(name)
        if tool is None or tool.effect not in ("cluster_read", "external_read"):
            return
        if tool.effect == "external_read":
            # No screen shows a Prometheus query; the activity note is the
            # whole of "visible in the TUI" for an external read (#193).
            self._note_read(name, args, ctx)
            return
        ui = self._ui
        if (
            ui is not None
            and self._follow_enabled is not None
            and self._follow_enabled()
            and not outcome.error
        ):
            task = asyncio.create_task(self._mirror_or_note(ui, name, args, ctx))
            self._follow_tasks.add(task)
            task.add_done_callback(self._follow_tasks.discard)
            return
        self._note_read(name, args, ctx)

    async def _mirror_or_note(
        self,
        ui: UIBridge,
        name: str,
        args: dict[str, Any],
        ctx: ServerRequestContext[Any] | None = None,
    ) -> None:
        """One detached mirror: on a UI refusal (e.g. `subscriptions` is not
        an alias in this cluster) or an unmapped read, degrade to the
        activity note - a successful external read must never become
        invisible just because its mirror could not land."""
        outcome = await mirror_read(ui, name, args)
        if outcome is not None and not outcome.startswith("ERROR"):
            return
        self._note_read(name, args, ctx)

    def _note_read(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ServerRequestContext[Any] | None = None,
    ) -> None:
        if self._note_activity is None:
            return
        client = self._client_info(ctx)[0] or "mcp"
        try:
            self._note_activity(f"{client}: {read_summary(name, args)}")
        except Exception:  # display-only: never fail the tool call over it
            logger.debug("MCP activity note failed", exc_info=True)

    async def _cancel_follow_tasks(self) -> None:
        """Cancel and reap every in-flight mirror task (run() teardown)."""
        tasks = [task for task in self._follow_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if self._follow_tasks:
            await asyncio.gather(*self._follow_tasks, return_exceptions=True)
        self._follow_tasks.clear()

    def _client_info(self, ctx: ServerRequestContext[Any] | None = None) -> tuple[str, str]:
        """Best-effort caller identity from the MCP initialize handshake.

        Display metadata only — never an authorization input (any caller can
        claim any name). Stateless transport may not carry it; degrade to
        empty strings. Values are sanitized here because they cross into
        approval dialogs, the status bar and audit records."""
        if ctx is None:
            return "", ""
        try:
            params = ctx.session.client_params
            info = params.client_info if params is not None else None
        except AttributeError:
            return "", ""
        if info is None:
            return "", ""
        return _sanitize_client_meta(info.name), _sanitize_client_meta(info.version)

    @property
    def bound_port(self) -> int | None:
        """Actual TCP port while published and not shutting down."""
        return self._bound_port

    async def wait_started(self) -> int:
        """Block until the HTTP server is accepting connections; return the
        bound port (useful when constructed with ``port=0``).

        Raises RuntimeError if startup failed (bind error) - the event is
        set either way so callers never hang on a server that will not come
        up."""
        await self._started.wait()
        if self._startup_error is not None:
            raise RuntimeError(self._startup_error)
        if self._bound_port is None:
            raise RuntimeError(f"MCP server failed to start on {_HOST}:{self._port}")
        return self._bound_port

    def request_shutdown(self) -> None:
        """Ask the HTTP server to exit gracefully; ``run()`` then returns.

        Preferred over cancelling ``run()`` so callers need not handle
        CancelledError. Both paths retain ownership of transport and registry
        cleanup until it finishes.
        Safe to call before ``run()`` has started - the flag is re-checked
        once the uvicorn server exists.
        """
        self._shutdown_requested = True
        self._bound_port = None
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True

    def _authenticate(self, request: Request) -> Response | None:
        values = request.headers.getlist("authorization")
        scheme, _, supplied = values[0].partition(" ") if len(values) == 1 else ("", "", "")
        if scheme.lower() != "bearer" or not supplied.isascii():
            supplied = ""
        if not secrets.compare_digest(supplied, self._capability_token):
            return Response("Unauthorized", status_code=401)
        if self._bound_port is None:
            return Response("MCP unavailable", status_code=503)
        return None

    def _authenticated_app(self, app: ASGIApp) -> ASGIApp:
        security = TransportSecurityMiddleware(_SECURITY_SETTINGS)

        async def handle(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                request = Request(scope, receive)
                rejection = self._authenticate(request)
                if rejection is None:
                    rejection = await security.validate_request(
                        request, is_post=scope["method"] == "POST"
                    )
                if rejection is not None:
                    await rejection(scope, receive, send)
                    return
            await app(scope, receive, send)

        return handle

    def _http_handler(self, manager: StreamableHTTPSessionManager) -> ASGIApp:
        async def handle(scope: Scope, receive: Receive, send: Send) -> None:
            # Only POST carries MCP traffic here. Stateless + JSON responses
            # means no server-initiated messages ever exist, so the SDK's
            # standalone GET SSE stream could only hang open — holding
            # uvicorn's graceful shutdown hostage until the controller
            # hard-cancels it mid-request ("Exception in ASGI application",
            # issue #136). The MCP spec allows a server that offers no SSE
            # stream to answer GET with 405, so refuse everything but POST
            # before it reaches the session manager — after the same
            # DNS-rebinding Host/Origin validation the manager would apply,
            # so a hostile origin is refused, never acknowledged with a 405.
            if scope["type"] == "http" and scope["method"] != "POST":
                response = Response(
                    "Method Not Allowed", status_code=405, headers={"Allow": "POST"}
                )
                await response(scope, receive, send)
                return
            await manager.handle_request(scope, receive, send)

        return handle

    async def run(self) -> None:
        """Serve until cancelled (run as a background task in the app loop)."""
        manager = StreamableHTTPSessionManager(
            app=self._server,
            stateless=True,
            json_response=True,
            security_settings=_SECURITY_SETTINGS,
        )

        @contextlib.asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            async with manager.run():
                yield

        app = Starlette(routes=[Mount("/mcp", app=self._http_handler(manager))], lifespan=lifespan)
        config = uvicorn.Config(
            self._authenticated_app(app), host=_HOST, port=self._port, log_level="error"
        )
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
                port = self._actual_port(server)
            except RuntimeError:
                # Startup lost the race against a pre-run request_shutdown():
                # the sockets are already gone, nothing to publish.
                return
            # Keep cleanup identity separate from authenticated readiness.
            # Cancelling this waiter cannot stop a contending registry thread.
            self._endpoint_port = port
            self._publication_task = asyncio.create_task(self._publish_endpoint(port))
            await asyncio.shield(self._publication_task)
            if not self._shutdown_requested and not server.should_exit:
                self._bound_port = port
            self._started.set()

        serve_task = asyncio.create_task(self._serve(server))
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_publish_when_started)
                try:
                    # Let uvicorn close its sockets/lifespan normally even if
                    # run() is cancelled while publication is blocked.
                    await asyncio.shield(serve_task)
                finally:
                    self.request_shutdown()
                    # Wake anyone blocked in wait_started(); with _bound_port
                    # unset they get a RuntimeError instead of hanging.
                    self._started.set()
                    tg.cancel_scope.cancel()
        finally:
            await _await_owned_task(asyncio.create_task(self._finish_run(serve_task)))

    async def _serve(self, server: uvicorn.Server) -> None:
        try:
            await server.serve()
        except SystemExit:
            # Catch bind failure inside the owned task: SystemExit must not
            # escape into the event loop or the TUI's shutdown path.
            logger.error("MCP server failed to start on %s:%d (port in use?)", _HOST, self._port)

    async def _publish_endpoint(self, port: int) -> None:
        try:
            await asyncio.to_thread(self._write_endpoint, port)
        except (OSError, EndpointRegistryError):
            self._startup_error = "MCP endpoint registry could not be published; check permissions"
            # A cancelled startup still observes failure, without registry
            # credentials or exception text reaching logs.
            logger.warning(self._startup_error)
            self.request_shutdown()
            self._started.set()

    async def _finish_run(self, serve_task: asyncio.Task[None]) -> None:
        try:
            await serve_task
        finally:
            await self._cancel_follow_tasks()
            try:
                if self._publication_task is not None:
                    await self._publication_task
            finally:
                await asyncio.to_thread(self._remove_endpoint)

    @staticmethod
    def _actual_port(server: uvicorn.Server) -> int:
        for srv in server.servers:
            for sock in srv.sockets:
                port = sock.getsockname()[1]
                return int(port)
        raise RuntimeError("uvicorn reported started without a bound socket")

    def _write_endpoint(self, port: int) -> None:
        """Publish this instance into the registry before accepting MCP requests.

        The file holds a ``{"servers": {"<pid>": {...}}}`` registry so that
        concurrent korvid instances each own one entry: publishing merges
        under a cross-process lock and never erases another live instance's
        record.  Runs on a worker thread - the lock may block on a
        contending process."""
        path = self._endpoint_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with interprocess_lock(_endpoint_lock_path(path)):
            registry = load_registry_for_update(path)
            registry["servers"][str(os.getpid())] = endpoint_record(
                pid=os.getpid(),
                port=port,
                url=f"http://{_HOST}:{port}/mcp",
                capability=self._capability_token,
            )
            _replace_atomically(path, registry)

    def _remove_endpoint(self) -> None:
        """Drop only *our* registry entry on exit: other live instances (and
        any foreign/non-registry data) are preserved.  The read-modify-write
        runs under the same cross-process lock as publication, so an entry
        added between the read and the write cannot be lost.  Runs on a
        worker thread."""
        path = self._endpoint_path
        if self._endpoint_port is None:
            return
        try:
            with interprocess_lock(_endpoint_lock_path(path)):
                registry = load_registry_for_update(path)
                entry = registry["servers"].get(str(os.getpid()))
                if (
                    not isinstance(entry, dict)
                    or entry.get("port") != self._endpoint_port
                    or entry.get("capability") != self._capability_token
                ):
                    return
                del registry["servers"][str(os.getpid())]
                if registry["servers"]:
                    _replace_atomically(path, registry)
                else:
                    path.unlink(missing_ok=True)
        except (OSError, EndpointRegistryError):
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
        except (TimeoutError, RuntimeError) as exc:
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
            if isinstance(exc, RuntimeError):
                return f"ERROR: {exc}"
            return "ERROR: MCP failed to start (startup timed out)"
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
        next shutdown to find. Returns the still-pending task if even
        cancellation did not land within its deadline. The composition root
        retains it while closing other clients, then enforces a terminal
        deadline instead of allowing runner finalization to wait forever.
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
        # Clear ownership only while it still points at *this* run: a
        # concurrent start() may have installed a fresh server while the old
        # task was awaited, and wiping its references would orphan a live
        # run behind a `running == False` report.
        if self._task is task:
            self._server = None
            self._task = None
        return None

    def pending_task(self) -> asyncio.Task[None] | None:
        return self._task if self._task is not None and not self._task.done() else None

    @staticmethod
    def _consume_result(task: asyncio.Task[None] | None) -> None:
        if task is None or not task.done() or task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("MCP server task failed")
