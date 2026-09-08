"""Real SDK stdio/HTTP round trips against an existing TUI-owned server."""

from __future__ import annotations

import asyncio
import errno
import io
import json
import os
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, TextIO

import anyio
import httpx2
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client, types

from korvid.mcp.server import KorvidMCPServer
from korvid.tools.executor import ToolOutcome
from korvid.tools.registry import mcp_tool_schemas

from .test_server import RecordingExecutor

_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINT = """
import importlib.abc
import sys
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"korvid.__main__", "korvid.ui", "korvid.providers", "korvid.k8s.client"}:
            raise RuntimeError("stdio must not start application services")
sys.meta_path.insert(0, Blocker())
from korvid.cli import main
main()
"""


def _parameters(state: Path, *args: str, relay: bool = False) -> StdioServerParameters:
    entrypoint = _ENTRYPOINT
    if relay:
        entrypoint = entrypoint.replace(
            "from korvid.cli import main",
            "from korvid.mcp import stdio\n"
            "from korvid.mcp._stdio_input import _relay_stdin\n"
            "stdio.cancellable_stdin = _relay_stdin\n"
            "from korvid.mcp import _stdio_output\n"
            "_stdio_output._posix_stdout = _stdio_output._relay_stdout\n"
            "from korvid.cli import main",
        )
    return StdioServerParameters(
        command=sys.executable,
        args=["-c", entrypoint, "mcp", "stdio", *args],
        env={"PYTHONPATH": str(_ROOT / "src"), "XDG_STATE_HOME": str(state)},
    )


@asynccontextmanager
async def _backend(
    state: Path, executor: RecordingExecutor | None = None
) -> AsyncIterator[tuple[RecordingExecutor, Path]]:
    executor = executor or RecordingExecutor()
    path = state / "korvid" / "mcp-endpoint.json"
    server = KorvidMCPServer(
        executor, mcp_tool_schemas(write_proposals=True), port=0, endpoint_path=path
    )
    task = asyncio.create_task(server.run())
    try:
        await asyncio.wait_for(server.wait_started(), timeout=10)
        yield executor, path
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)


async def _cli_result(state: Path, *args: str) -> tuple[int | None, str, str]:
    params = _parameters(state, *args)
    process = await asyncio.create_subprocess_exec(
        params.command,
        *params.args,
        env={**os.environ, **(params.env or {})},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=20)
        return process.returncode, out.decode(), err.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_no_tui_exits_actionably_without_protocol_noise(tmp_path: Path) -> None:
    code, out, err = await _cli_result(tmp_path)
    assert code == 1
    assert out == ""
    assert "registry" in err
    assert "TUI" in err
    assert "Traceback" not in err


async def test_stdio_forwards_tools_results_and_proposals_without_credentials(
    tmp_path: Path,
) -> None:
    async with _backend(tmp_path) as (executor, path):
        token = json.loads(path.read_text())["servers"][str(os.getpid())]["capability"]
        with (tmp_path / "stderr.log").open("w+") as errors:
            async with (
                stdio_client(_parameters(tmp_path), errlog=errors) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                assert "propose_write" in {tool.name for tool in tools.tools}
                assert "capability" not in tools.model_dump_json()
                for tool, arguments in (
                    ("list_resources", {"kind": "pods"}),
                    ("navigate", {"view": "pods"}),
                    (
                        "propose_write",
                        {
                            "action": "scale",
                            "kind": "deployments",
                            "name": "web",
                            "namespace": "default",
                            "replicas": 2,
                        },
                    ),
                ):
                    result = await session.call_tool(tool, arguments)
                    assert isinstance(result, types.CallToolResult)
                    assert result.is_error is False
                    assert getattr(result.content[0], "text", None) == "ok"
                    assert token not in result.model_dump_json()
                executor.error = True
                executor.result = "ERROR: test refusal"
                result = await session.call_tool("list_resources", {"kind": "pods"})
                assert isinstance(result, types.CallToolResult)
                assert result.is_error is True
                assert getattr(result.content[0], "text", None) == "ERROR: test refusal"
            errors.seek(0)
            assert token not in errors.read()
        assert len(executor.calls) == 4
        assert all("capability" not in arguments for _, arguments in executor.calls)
        assert executor.calls[2][1]["_session_id"]


async def test_wrong_capability_exits_without_leaking_credentials(tmp_path: Path) -> None:
    async with _backend(tmp_path) as (executor, path):
        registry = json.loads(path.read_text())
        original = registry["servers"][str(os.getpid())]["capability"]
        wrong = "wrong-capability-" * 4
        registry["servers"][str(os.getpid())]["capability"] = wrong
        path.write_text(json.dumps(registry))
        code, out, err = await _cli_result(tmp_path)
        assert code == 1
        assert out == ""
        assert "TUI" in err
        assert "Traceback" not in err
        assert original not in err
        assert wrong not in err
        assert executor.calls == []


async def test_stdio_eof_closes_cleanly_without_protocol_output(tmp_path: Path) -> None:
    async with _backend(tmp_path) as (executor, _path):
        code, out, err = await _cli_result(tmp_path)
        assert code == 0, err
        assert out == ""
        assert "Traceback" not in err
        assert executor.calls == []


async def test_multiple_live_entries_require_an_explicit_instance(tmp_path: Path) -> None:
    async with _backend(tmp_path) as (_executor, path):
        registry = json.loads(path.read_text())
        other = dict(registry["servers"][str(os.getpid())])
        other["pid"] = os.getppid()
        registry["servers"][str(os.getppid())] = other
        path.write_text(json.dumps(registry))
        code, out, err = await _cli_result(tmp_path)
        assert code == 1
        assert out == ""
        assert "--instance" in err
        code, out, err = await _cli_result(tmp_path, "--instance", str(os.getpid()))
        assert code == 0, err
        assert out == ""


async def test_stdio_bypasses_environment_http_proxies(tmp_path: Path) -> None:
    async with _backend(tmp_path):
        params = _parameters(tmp_path)
        params.env = {
            **(params.env or {}),
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
        }
        with (tmp_path / "stderr.log").open("w+") as errors:
            async with (
                stdio_client(params, errlog=errors) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.list_tools()
                assert result.tools


class _PreflightHTTP:
    def __init__(self) -> None:
        self.notification_started = asyncio.Event()
        self.release_acknowledgement = asyncio.Event()
        self.acknowledged = False
        self.cancelled = False
        self.methods: list[str] = []

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.methods.append(body["method"])
        if body["method"] == "notifications/initialized":
            self.notification_started.set()
            try:
                await self.release_acknowledgement.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.acknowledged = True
            return httpx2.Response(202)
        result = (
            {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {},
                "serverInfo": {"name": "preflight", "version": "1"},
            }
            if body["method"] == "initialize"
            else {}
        )
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


class _DelayedJSONClose(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.body = b""
        self.closing = asyncio.Event()
        self.release_close = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body

    async def aclose(self) -> None:
        self.closing.set()
        await self.release_close.wait()
        self.closed = True


@pytest.mark.parametrize("response_hook", [False, True])
async def test_json_stream_closes_before_sdk_delivers_result(response_hook: bool) -> None:
    from korvid.mcp.stdio import _drain_http_response, _Proxy

    peer = _PreflightHTTP()
    peer.release_acknowledgement.set()
    body = _DelayedJSONClose()
    delivered = asyncio.Event()

    async def handle(request: httpx2.Request) -> httpx2.Response:
        message = json.loads(request.content)
        if message["method"] == "tools/list":
            body.body = json.dumps(
                {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}}
            ).encode()
            return httpx2.Response(200, headers={"Content-Type": "application/json"}, stream=body)
        return await peer.handle(request)

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handle),
        event_hooks={"response": [_drain_http_response] if response_hook else []},
    ) as http:

        async def request_tools() -> types.ListToolsResult:
            async with _Proxy(http, "http://127.0.0.1:34567/mcp/").connect() as session:
                result = await session.list_tools()
                assert body.closed
                delivered.set()
                return result

        task = asyncio.create_task(request_tools())
        try:
            await asyncio.wait_for(body.closing.wait(), 5)
            assert not delivered.is_set()
        finally:
            body.release_close.set()
            result = await asyncio.wait_for(task, 5)
        assert delivered.is_set()
        assert result.tools == []


async def test_preflight_acknowledges_initialization_before_closing_http_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from korvid.mcp import stdio
    from korvid.mcp.registry import TUIEndpoint

    peer = _PreflightHTTP()
    original_client = httpx2.AsyncClient
    original_initialize = ClientSession.initialize
    original_ping = ClientSession.send_ping
    ready_at_stdio_start: list[bool] = []

    def client(**kwargs: Any) -> httpx2.AsyncClient:
        return original_client(transport=httpx2.MockTransport(peer.handle), **kwargs)

    async def initialize(session: ClientSession) -> types.InitializeResult:
        result = await original_initialize(session)
        await peer.notification_started.wait()
        return result

    async def ping(session: ClientSession) -> types.EmptyResult:
        peer.release_acknowledgement.set()
        return await original_ping(session)

    @asynccontextmanager
    async def memory_stream(source: TextIO) -> AsyncIterator[anyio.AsyncFile[str]]:
        ready_at_stdio_start.append(peer.acknowledged)
        with io.StringIO() as stream:
            yield anyio.wrap_file(stream)

    monkeypatch.setattr(httpx2, "AsyncClient", client)
    monkeypatch.setattr(ClientSession, "initialize", initialize)
    monkeypatch.setattr(ClientSession, "send_ping", ping)
    monkeypatch.setattr(stdio, "cancellable_stdin", memory_stream)
    monkeypatch.setattr(stdio, "cancellable_stdout", memory_stream)
    endpoint = TUIEndpoint(
        pid=os.getpid(),
        port=34567,
        url="http://127.0.0.1:34567/mcp",
        capability="preflight-test-credential-" * 3,
    )
    await asyncio.wait_for(stdio._serve(endpoint), 5)
    assert ready_at_stdio_start == [True, True]
    assert not peer.cancelled
    assert peer.methods == ["initialize", "notifications/initialized", "ping"]


async def test_preflight_consumes_http_acknowledgement_before_reusing_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from korvid.mcp import stdio
    from korvid.mcp.registry import TUIEndpoint

    original_client = httpx2.AsyncClient
    streams: list[object] = []
    methods: list[str] = []

    async def observe(response: httpx2.Response) -> None:
        streams.append(response.extensions["network_stream"])
        methods.append(json.loads(response.request.content)["method"])

    def client(**kwargs: Any) -> httpx2.AsyncClient:
        hooks = kwargs.setdefault("event_hooks", {})
        hooks.setdefault("response", []).append(observe)
        return original_client(**kwargs)

    @asynccontextmanager
    async def empty_stdio(source: TextIO) -> AsyncIterator[anyio.AsyncFile[str]]:
        with io.StringIO() as stream:
            yield anyio.wrap_file(stream)

    monkeypatch.setattr(httpx2, "AsyncClient", client)
    monkeypatch.setattr(stdio, "cancellable_stdin", empty_stdio)
    monkeypatch.setattr(stdio, "cancellable_stdout", empty_stdio)
    async with _backend(tmp_path) as (executor, path):
        record = json.loads(path.read_text())["servers"][str(os.getpid())]
        endpoint = TUIEndpoint(
            pid=record["pid"],
            port=record["port"],
            url=record["url"],
            capability=record["capability"],
        )
        await asyncio.wait_for(stdio._serve(endpoint), 5)
        assert methods == ["initialize", "notifications/initialized", "ping"]
        assert len(streams) == 3
        assert streams[0] is streams[1] is streams[2]
        assert executor.calls == []


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "text/event-stream"])
async def test_http_response_cleanup_drains_finite_bodies_but_preserves_sse(
    content_type: str,
) -> None:
    from korvid.mcp.stdio import _drain_http_response

    response = httpx2.Response(
        202 if content_type == "application/json" else 401,
        headers={"Content-Type": content_type},
        stream=httpx2.ByteStream(b"test response body"),
    )
    try:
        await _drain_http_response(response)
        if content_type == "text/event-stream":
            assert not response.is_stream_consumed
            assert not response.is_closed
        else:
            assert response.is_stream_consumed
            assert response.is_closed
            assert response.content == b"test response body"
    finally:
        await response.aclose()


def test_authenticated_bridge_never_follows_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    from korvid.mcp import stdio
    from korvid.mcp.registry import TUIEndpoint

    endpoint = TUIEndpoint(
        pid=os.getpid(),
        port=34567,
        url="http://127.0.0.1:34567/mcp",
        capability="test-transport-credential-" * 3,
    )
    requested: list[str] = []
    original_client = httpx2.AsyncClient

    def redirect(request: httpx2.Request) -> httpx2.Response:
        requested.append(str(request.url))
        return httpx2.Response(307, headers={"Location": "http://127.0.0.1:34568/stolen"})

    def http_client(
        *,
        headers: dict[str, str],
        timeout: httpx2.Timeout,
        follow_redirects: bool,
        trust_env: bool,
        event_hooks: dict[str, list[Any]],
    ) -> httpx2.AsyncClient:
        return original_client(
            headers=headers,
            timeout=timeout,
            follow_redirects=follow_redirects,
            trust_env=trust_env,
            event_hooks=event_hooks,
            transport=httpx2.MockTransport(redirect),
        )

    monkeypatch.setattr(httpx2, "AsyncClient", http_client)
    monkeypatch.setattr(stdio, "read_endpoints", lambda: [endpoint])
    with pytest.raises(stdio.StdioBridgeError, match="selected Korvid TUI"):
        stdio.run_stdio()
    assert requested == [endpoint.url + "/"]


class _BlockedExecutor(RecordingExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self.started.set()
        await self.release.wait()
        return await super().execute_recorded(name, arguments)


@asynccontextmanager
async def _stdio_process(
    state: Path, *, relay: bool = False, stdout: int | TextIO = asyncio.subprocess.PIPE
) -> AsyncIterator[asyncio.subprocess.Process]:
    params = _parameters(state, relay=relay)
    process = await asyncio.create_subprocess_exec(
        params.command,
        *params.args,
        env={**os.environ, **(params.env or {})},
        stdin=asyncio.subprocess.PIPE,
        stdout=stdout,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield process
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            process.kill()
            await process.wait()


async def _send_frame(process: asyncio.subprocess.Process, frame: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write((json.dumps(frame) + "\n").encode())
    await process.stdin.drain()


async def _initialize_process(
    process: asyncio.subprocess.Process, *, output: anyio.AsyncFile[str] | None = None
) -> None:
    await _send_frame(
        process,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )
    reader = output or process.stdout
    assert reader is not None
    initialized = json.loads(await asyncio.wait_for(reader.readline(), 10))
    assert initialized["jsonrpc"] == "2.0"
    assert initialized["id"] == 1
    assert "result" in initialized
    await _send_frame(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGINT delivery")
@pytest.mark.parametrize("blocked_call", [False, True])
@pytest.mark.parametrize("relay", [False, True])
async def test_sigint_exits_without_host_stdin_eof(
    tmp_path: Path, blocked_call: bool, relay: bool
) -> None:
    executor = _BlockedExecutor()
    async with _backend(tmp_path, executor), _stdio_process(tmp_path, relay=relay) as process:
        try:
            await _initialize_process(process)
            if blocked_call:
                await _send_frame(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "list_resources", "arguments": {"kind": "pods"}},
                    },
                )
                await asyncio.wait_for(executor.started.wait(), 10)
            assert process.stdin is not None
            assert not process.stdin.is_closing()
            process.send_signal(signal.SIGINT)
            # communicate() would close stdin and conceal the cancellation deadlock.
            await asyncio.wait_for(process.wait(), 5)
            assert process.stdout is not None
            assert process.stderr is not None
            out = await process.stdout.read()
            err = await process.stderr.read()
            assert process.returncode == 1, err.decode()
            assert all(json.loads(line)["jsonrpc"] == "2.0" for line in out.splitlines())
            assert err.decode() == "korvid: MCP stdio connection interrupted.\n"
        finally:
            executor.release.set()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGINT and pipe readiness")
@pytest.mark.parametrize("relay", [False, True])
@pytest.mark.parametrize("interrupt", [False, True])
async def test_shutdown_exits_without_draining_stdout(
    tmp_path: Path, relay: bool, interrupt: bool
) -> None:
    from korvid.mcp._stdio_input import cancellable_stdin

    executor = RecordingExecutor()
    executor.result = "x" * (8 * 1024 * 1024)
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "r", encoding="utf-8") as host,
        os.fdopen(write_fd, "w", encoding="utf-8") as wire,
    ):
        async with (
            _backend(tmp_path, executor),
            _stdio_process(tmp_path, relay=relay, stdout=wire) as process,
            cancellable_stdin(host) as reader,
        ):
            try:
                await _initialize_process(process, output=reader)
                await _send_frame(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "list_resources", "arguments": {"kind": "pods"}},
                    },
                )
                # Observe the large response starting, then leave the pipe entirely unread.
                await asyncio.wait_for(anyio.wait_readable(read_fd), 10)
                if interrupt:
                    process.send_signal(signal.SIGINT)
                else:
                    assert process.stdin is not None
                    process.stdin.close()
                # stdout is a raw pipe: exit cannot be confused with asyncio's
                # separate wait for its own paused stdout transport to drain.
                await asyncio.wait_for(process.wait(), 5)
                assert process.stderr is not None
                assert process.returncode == (1 if interrupt else 0)
                expected = b"korvid: MCP stdio connection interrupted.\n" if interrupt else b""
                assert await process.stderr.read() == expected
            finally:
                host.close()


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("unexpected", [False, True])
async def test_stdio_shutdown_preserves_cancellation_without_hiding_errors(
    cancelled: bool, unexpected: bool
) -> None:
    from korvid.mcp.stdio import _raise_stdio_failure

    errors: list[Exception] = [anyio.BrokenResourceError()]
    if unexpected:
        errors.append(ValueError("unexpected failure"))
    group = ExceptionGroup("SDK stream closed", errors)

    async def fail() -> None:
        task = asyncio.current_task()
        assert task is not None
        if cancelled:
            task.cancel()
        _raise_stdio_failure(group)

    task = asyncio.create_task(fail())
    if cancelled and not unexpected:
        with pytest.raises(asyncio.CancelledError, match=r"^$"):
            await task
    else:
        with pytest.raises(ExceptionGroup, match="SDK stream closed") as raised:
            await task
        assert raised.value is group


@pytest.mark.parametrize("relay", [False, True])
async def test_stdio_delivers_large_utf8_output_without_truncation(
    tmp_path: Path, relay: bool
) -> None:
    executor = RecordingExecutor()
    executor.result = "테스트" * 100000
    async with _backend(tmp_path, executor):
        with (tmp_path / "large-output-stderr.log").open("w+") as errors:
            async with (
                stdio_client(_parameters(tmp_path, relay=relay), errlog=errors) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await asyncio.wait_for(
                    session.call_tool("list_resources", {"kind": "pods"}), 10
                )
                assert isinstance(result, types.CallToolResult)
                assert not result.is_error
                assert getattr(result.content[0], "text", None) == executor.result
            errors.seek(0)
            assert errors.read() == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX nonblocking partial writes")
async def test_stdout_retries_backpressure_and_partial_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from korvid.mcp._stdio_output import _posix_stdout

    read_fd, write_fd = os.pipe()
    original_write = os.write
    sizes: list[int] = []

    def short_write(fd: int, data: Any) -> int:
        if fd == write_fd:
            sizes.append(len(data))
            if len(sizes) == 1:
                raise BlockingIOError("pipe is full")
            return original_write(fd, data[:3])
        return original_write(fd, data)

    monkeypatch.setattr(os, "write", short_write)
    payload = "테스트" * 100 + "\n"
    with (
        os.fdopen(read_fd, "rb", buffering=0) as host,
        os.fdopen(write_fd, "w", encoding="utf-8") as wire,
    ):
        async with _posix_stdout(wire) as output:
            assert await output.write(payload) == len(payload)
            await output.flush()
        assert os.get_blocking(write_fd)
        wire.close()
        assert host.read() == payload.encode()
        assert len(sizes) > 2


@pytest.mark.parametrize("relay", [False, True])
async def test_stdout_normal_close_drains_accepted_utf8_bytes(tmp_path: Path, relay: bool) -> None:
    from korvid.mcp import _stdio_output

    path = tmp_path / "complete-wire.jsonl"
    payload = json.dumps({"text": "테스트" * 100000}, ensure_ascii=False) + "\n"
    transport = (
        _stdio_output._relay_stdout
        if relay or sys.platform == "win32"
        else _stdio_output._posix_stdout
    )
    with path.open("w", encoding="utf-8") as wire:
        async with transport(wire) as output:
            assert await output.write(payload) == len(payload)
    assert path.read_text(encoding="utf-8") == payload


async def test_stdout_relay_failure_is_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    from korvid.mcp import _stdio_output

    monkeypatch.setattr(_stdio_output, "_RELAY", "raise SystemExit(7)")
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "rb"),
        os.fdopen(write_fd, "w", encoding="utf-8") as wire,
        pytest.raises(OSError, match="MCP stdout relay failed"),
    ):
        async with _stdio_output._relay_stdout(wire) as output:
            await output.flush()


@pytest.mark.parametrize("relay", [False, True])
async def test_stdout_cancellation_reaps_writer_and_restores_descriptor(
    monkeypatch: pytest.MonkeyPatch, relay: bool
) -> None:
    from korvid.mcp import _stdio_output
    from korvid.mcp._stdio_input import _relay_stdin

    if relay:
        monkeypatch.setattr(_stdio_output, "_posix_stdout", _stdio_output._relay_stdout)
    processes: list[anyio.abc.Process] = []
    duplicates: list[int] = []
    original_spawn = anyio.open_process
    original_dup = os.dup

    async def spawn(*args: Any, **kwargs: Any) -> anyio.abc.Process:
        process = await original_spawn(*args, **kwargs)
        if kwargs.get("stdout") != asyncio.subprocess.PIPE:
            processes.append(process)
        return process

    def duplicate(fd: int) -> int:
        owned = original_dup(fd)
        if fd == write_fd:
            duplicates.append(owned)
        return owned

    monkeypatch.setattr(anyio, "open_process", spawn)
    monkeypatch.setattr(os, "dup", duplicate)
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "r", encoding="utf-8") as host,
        os.fdopen(write_fd, "w", encoding="utf-8") as wire,
    ):
        before = os.fstat(write_fd)
        inheritable = os.get_inheritable(write_fd)

        async def send() -> None:
            async with _stdio_output.cancellable_stdout(wire) as output:
                await output.write("ready\n" + "x" * (8 * 1024 * 1024))

        async with _relay_stdin(host) as reader:
            task = asyncio.create_task(send())
            try:
                assert await asyncio.wait_for(reader.readline(), 5) == "ready\n"
                task.cancel()
                with pytest.raises(asyncio.CancelledError, match=r"^$"):
                    await asyncio.wait_for(task, 5)
                assert not host.closed
                assert not wire.closed
                after = os.fstat(write_fd)
                assert (after.st_dev, after.st_ino, after.st_mode) == (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                )
                assert os.get_inheritable(write_fd) is inheritable
                if sys.platform != "win32":
                    assert os.get_blocking(write_fd)
                assert bool(processes) is (relay or sys.platform == "win32")
                assert all(process.returncode is not None for process in processes)
                assert len(duplicates) == 1
                with pytest.raises(OSError, match=r".+") as closed:
                    os.fstat(duplicates[0])
                assert (
                    closed.value.errno == errno.EBADF
                    or getattr(closed.value, "winerror", None) == 6
                )
            finally:
                if not task.done():
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError, match=r"^$"):
                        await task


@pytest.mark.parametrize("relay", [False, True])
async def test_stdout_noise_is_diverted_and_wire_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relay: bool
) -> None:
    noise = """
import json, os
from korvid.mcp import stdio
_original_list_tools = stdio._Proxy.list_tools
async def noisy_list_tools(self, ctx, params):
    print("python-handler-noise")
    os.write(1, b"fd-handler-noise\\n")
    return await _original_list_tools(self, ctx, params)
stdio._Proxy.list_tools = noisy_list_tools
_saved_stdout = sys.stdout
_saved_stat = os.fstat(1)
_saved_inheritable = os.get_inheritable(1)
_saved_blocking = os.get_blocking(1) if sys.platform != "win32" else None
"""
    restored = """
try:
    main()
finally:
    current = os.fstat(1)
    restored = (
        sys.stdout is _saved_stdout
        and (current.st_dev, current.st_ino, current.st_mode)
            == (_saved_stat.st_dev, _saved_stat.st_ino, _saved_stat.st_mode)
        and os.get_inheritable(1) == _saved_inheritable
    )
    if _saved_blocking is not None:
        restored = restored and os.get_blocking(1) == _saved_blocking
    if sys.platform == "win32":
        import ctypes, msvcrt
        from ctypes import wintypes
        get_std_handle = ctypes.WinDLL("kernel32").GetStdHandle
        get_std_handle.argtypes = [wintypes.DWORD]
        get_std_handle.restype = wintypes.HANDLE
        restored = restored and get_std_handle(-11) == msvcrt.get_osfhandle(1)
    os.write(1, (json.dumps({"restored": restored}) + "\\n").encode())
"""
    entrypoint = _ENTRYPOINT.replace(
        "from korvid.cli import main", noise + "\nfrom korvid.cli import main"
    ).replace("\nmain()\n", restored)
    monkeypatch.setattr(sys.modules[__name__], "_ENTRYPOINT", entrypoint)
    async with _backend(tmp_path), _stdio_process(tmp_path, relay=relay) as process:
        await _initialize_process(process)
        await _send_frame(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert process.stdout is not None
        result = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert result["id"] == 2
        assert result["result"]["tools"]
        assert process.stdin is not None
        process.stdin.close()
        out, err = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, err.decode()
        assert json.loads(out) == {"restored": True}
        assert sorted(err.decode().splitlines()) == ["fd-handler-noise", "python-handler-noise"]


@pytest.mark.parametrize("relay", [False, True])
async def test_pipe_input_cancellation_reaps_owned_resources(
    monkeypatch: pytest.MonkeyPatch, relay: bool
) -> None:
    from korvid.mcp._stdio_input import _relay_stdin, cancellable_stdin

    processes: list[anyio.abc.Process] = []
    duplicates: list[int] = []
    original_spawn = anyio.open_process
    original_dup = os.dup

    async def spawn(*args: Any, **kwargs: Any) -> anyio.abc.Process:
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    def duplicate(fd: int) -> int:
        owned_fd = original_dup(fd)
        duplicates.append(owned_fd)
        return owned_fd

    monkeypatch.setattr(anyio, "open_process", spawn)
    monkeypatch.setattr(os, "dup", duplicate)
    read_fd, write_fd = os.pipe()
    ready = asyncio.Event()
    with (
        os.fdopen(read_fd, "r", encoding="utf-8") as source,
        os.fdopen(write_fd, "wb") as host,
    ):
        initially_blocking = os.get_blocking(read_fd) if sys.platform != "win32" else None

        async def read() -> None:
            context = _relay_stdin(source) if relay else cancellable_stdin(source)
            async with context as stdin:
                ready.set()
                await stdin.readline()

        task = asyncio.create_task(read())
        try:
            await asyncio.wait_for(ready.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError, match=r"^$"):
                await asyncio.wait_for(task, 5)
            assert not host.closed
            assert not source.closed
            if initially_blocking is not None:
                assert os.get_blocking(read_fd) is initially_blocking
            assert all(process.returncode is not None for process in processes)
            assert bool(processes) is (relay or sys.platform == "win32")
            for owned_fd in duplicates:
                with pytest.raises(OSError, match=r".+") as closed:
                    os.fstat(owned_fd)
                assert (
                    closed.value.errno == errno.EBADF
                    or getattr(closed.value, "winerror", None) == 6
                )
        finally:
            host.close()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError, match=r"^$"):
                    await task


@pytest.mark.parametrize("relay", [False, True])
async def test_pipe_input_preserves_final_utf8_line_at_eof(relay: bool) -> None:
    from korvid.mcp._stdio_input import _relay_stdin, cancellable_stdin

    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "r", encoding="utf-8") as source,
        os.fdopen(write_fd, "wb", buffering=0) as host,
    ):
        host.write('{"message":"테스트"}\r\n'.encode() + b"last\xff")
        host.close()
        context = _relay_stdin(source) if relay else cancellable_stdin(source)
        async with context as stdin:
            assert await asyncio.wait_for(stdin.readline(), 5) == '{"message":"테스트"}\r\n'
            assert await asyncio.wait_for(stdin.readline(), 5) == "last\ufffd"
            assert await asyncio.wait_for(stdin.readline(), 5) == ""


@pytest.mark.parametrize("relay", [False, True])
async def test_stdio_transport_parses_unterminated_final_ping(relay: bool) -> None:
    from mcp.server.stdio import stdio_server

    from korvid.mcp._stdio_input import _relay_stdin, cancellable_stdin

    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "ping",
        "params": {"_meta": {"label": "테스트"}},
    }
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "r", encoding="utf-8") as source,
        os.fdopen(write_fd, "wb", buffering=0) as host,
        io.StringIO() as output,
    ):
        host.write(json.dumps(request, ensure_ascii=False).encode("utf-8"))
        host.close()
        context = _relay_stdin(source) if relay else cancellable_stdin(source)
        async with (
            context as stdin,
            stdio_server(stdin=stdin, stdout=anyio.wrap_file(output)) as (read, write),
            read,
            write,
        ):
            message = await asyncio.wait_for(read.receive(), 5)
            assert not isinstance(message, Exception)
            assert isinstance(message.message, types.JSONRPCRequest)
            assert message.message.id == 2
            assert message.message.method == "ping"
            assert message.message.params == request["params"]
            with pytest.raises(anyio.EndOfStream, match=r"^$"):
                await asyncio.wait_for(read.receive(), 5)


async def test_relay_failure_is_not_silent_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    from korvid.mcp import _stdio_input

    monkeypatch.setattr(_stdio_input, "_RELAY", "raise SystemExit(7)")
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "r", encoding="utf-8") as source,
        os.fdopen(write_fd, "wb"),
    ):
        async with _stdio_input._relay_stdin(source) as stdin:
            with pytest.raises(OSError, match="MCP stdin relay failed"):
                await asyncio.wait_for(stdin.readline(), 5)


async def test_relay_cancellation_closes_backpressured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from korvid.mcp._stdio_input import _relay_stdin

    buffered = asyncio.Event()
    processes: list[anyio.abc.Process] = []
    original_spawn = anyio.open_process
    original_init = asyncio.StreamReader.__init__
    original_feed = asyncio.StreamReader.feed_data

    async def spawn(*args: Any, **kwargs: Any) -> anyio.abc.Process:
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    def init(
        reader: asyncio.StreamReader,
        limit: int = 65536,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        original_init(reader, limit=1, loop=loop)

    def feed(reader: asyncio.StreamReader, data: bytes) -> None:
        original_feed(reader, data)
        if len(data) > 2:
            buffered.set()

    monkeypatch.setattr(anyio, "open_process", spawn)
    monkeypatch.setattr(asyncio.StreamReader, "__init__", init)
    monkeypatch.setattr(asyncio.StreamReader, "feed_data", feed)
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "r", encoding="utf-8") as source,
        os.fdopen(write_fd, "wb", buffering=0) as host,
    ):
        host.write(b"x" * 1024)

        async def read() -> None:
            async with _relay_stdin(source):
                await asyncio.Event().wait()

        task = asyncio.create_task(read())
        try:
            await asyncio.wait_for(buffered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError, match=r"^$"):
                await asyncio.wait_for(task, 5)
            assert not host.closed
            assert len(processes) == 1
            assert processes[0].returncode is not None
        finally:
            host.close()
            for process in processes:
                await process.aclose()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError, match=r"^$"):
                    await task


@pytest.mark.parametrize("relay", [False, True])
async def test_stdio_round_trip_preserves_fragmented_large_utf8_frames(
    tmp_path: Path, relay: bool
) -> None:
    async with _backend(tmp_path), _stdio_process(tmp_path, relay=relay) as process:
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "테스트" * 12000, "version": "1"},
            },
        }
        wire = (json.dumps(initialize, ensure_ascii=False) + "\r\n").encode()
        assert process.stdin is not None
        for offset in range(0, len(wire), 1021):
            process.stdin.write(wire[offset : offset + 1021])
            await process.stdin.drain()
        assert process.stdout is not None
        initialized = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert initialized["id"] == 1
        assert "result" in initialized
        process.stdin.write(
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
        )
        await process.stdin.drain()
        ping_response = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert ping_response["id"] == 2
        assert "result" in ping_response
        process.stdin.close()
        out, err = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, err.decode()
        responses = [json.loads(line) for line in out.splitlines()]
        assert all(response["jsonrpc"] == "2.0" for response in responses)
        assert "Traceback" not in err.decode()


async def test_stdio_eof_during_a_tool_call_reaps_the_adapter(tmp_path: Path) -> None:
    executor = _BlockedExecutor()
    async with _backend(tmp_path, executor):
        params = _parameters(tmp_path)
        process = await asyncio.create_subprocess_exec(
            params.command,
            *params.args,
            env={**os.environ, **(params.env or {})},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            initialize = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
            process.stdin.write((json.dumps(initialize) + "\n").encode())
            await process.stdin.drain()
            initialized = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
            assert initialized["id"] == 1
            request = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_resources", "arguments": {"kind": "pods"}},
            }
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            process.stdin.write((json.dumps(request) + "\n").encode())
            await process.stdin.drain()
            await asyncio.wait_for(executor.started.wait(), 10)
            process.stdin.close()
            out, err = await asyncio.wait_for(process.communicate(), 5)
            assert process.returncode == 0, err.decode()
            assert all(json.loads(line)["jsonrpc"] == "2.0" for line in out.splitlines())
            assert "Traceback" not in err.decode()
        finally:
            executor.release.set()
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_cancelled_tool_call_does_not_break_stdio_session(tmp_path: Path) -> None:
    executor = _BlockedExecutor()
    async with _backend(tmp_path, executor):
        with (tmp_path / "stderr.log").open("w+") as errors:
            async with (
                stdio_client(_parameters(tmp_path), errlog=errors) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                call = asyncio.create_task(session.call_tool("list_resources", {"kind": "pods"}))
                try:
                    await asyncio.wait_for(executor.started.wait(), 10)
                    call.cancel()
                    with pytest.raises(asyncio.CancelledError, match=r"^$"):
                        await asyncio.wait_for(call, 10)
                    tools = await asyncio.wait_for(session.list_tools(), 10)
                    assert tools.tools
                finally:
                    executor.release.set()
                    if not call.done():
                        call.cancel()
                        with pytest.raises(asyncio.CancelledError, match=r"^$"):
                            await call


async def test_running_stdio_never_retargets_after_a_tui_restart(tmp_path: Path) -> None:
    async with AsyncExitStack() as backend:
        await backend.enter_async_context(_backend(tmp_path))
        with (tmp_path / "stderr.log").open("w+") as errors:
            async with (
                stdio_client(_parameters(tmp_path), errlog=errors) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                await asyncio.wait_for(backend.aclose(), 10)
                async with _backend(tmp_path) as (replacement, _path):
                    result = await asyncio.wait_for(
                        session.call_tool("list_resources", {"kind": "pods"}), 15
                    )
                    assert isinstance(result, types.CallToolResult)
                    assert result.is_error is True
                    assert "TUI" in getattr(result.content[0], "text", "")
                    assert replacement.calls == []
