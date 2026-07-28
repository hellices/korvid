"""Tests for the embedded Streamable HTTP MCP server (issue #11)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest

from korvid.agent.mcp_server import KorvidMCPServer, MCPController, default_endpoint_path
from korvid.k8s.discovery import PODS_META
from korvid.tools.executor import READ_TOOLS, UI_TOOLS, ToolExecutor


class RecordingExecutor(ToolExecutor):
    """ToolExecutor that records dispatches instead of touching a cluster."""

    def __init__(self) -> None:
        super().__init__(kube=None, aliases={"pods": PODS_META})  # type: ignore[arg-type]  # execute() is overridden; kube is never touched
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = "ok"

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        return self.result


def make_server(
    executor: ToolExecutor | None = None,
    *,
    port: int = 0,
    endpoint_path: Path | None = None,
) -> KorvidMCPServer:
    return KorvidMCPServer(
        executor or RecordingExecutor(),
        READ_TOOLS + UI_TOOLS,
        port=port,
        endpoint_path=endpoint_path,
    )


# ---------------------------------------------------------------------------
# Endpoint discovery file
# ---------------------------------------------------------------------------


def test_default_endpoint_path_uses_xdg_state_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")
    assert default_endpoint_path() == Path("/tmp/xdg-state/korvid/mcp-endpoint.json")


def test_default_endpoint_path_falls_back_to_local_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert (
        default_endpoint_path() == Path.home() / ".local" / "state" / "korvid" / "mcp-endpoint.json"
    )


# ---------------------------------------------------------------------------
# Tool surface: names and schemas mirror agent/tools.py
# ---------------------------------------------------------------------------


async def test_list_tools_exposes_agent_tool_surface() -> None:
    server = make_server()
    tools = await server.list_tools()
    expected = [t["function"]["name"] for t in READ_TOOLS + UI_TOOLS]
    assert [t.name for t in tools] == expected
    by_name = {t.name: t for t in tools}
    for tool_def in READ_TOOLS + UI_TOOLS:
        fn = tool_def["function"]
        assert by_name[fn["name"]].description == fn["description"]
        assert by_name[fn["name"]].inputSchema == fn["parameters"]


async def test_write_tools_are_not_exposed() -> None:
    """The MCP surface is read + UI-drive only: external hosts must not see
    cluster write tools until the approval UX for external callers exists."""
    from korvid.tools.executor import WRITE_TOOL_NAMES

    server = make_server()
    names = {t.name for t in await server.list_tools()}
    assert not names & WRITE_TOOL_NAMES


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def test_call_tool_dispatches_to_executor() -> None:
    executor = RecordingExecutor()
    server = make_server(executor)
    content = await server.call_tool("list_resources", {"kind": "pods"})
    assert executor.calls == [("list_resources", {"kind": "pods"})]
    assert len(content) == 1
    assert content[0].type == "text"
    assert content[0].text == "ok"


async def test_call_tool_none_arguments_become_empty_dict() -> None:
    executor = RecordingExecutor()
    server = make_server(executor)
    await server.call_tool("list_resources", None)
    assert executor.calls == [("list_resources", {})]


async def test_call_tool_passes_error_text_through() -> None:
    """ToolExecutor.execute never raises - 'ERROR: ...' strings flow to the
    MCP host as plain text results, matching the built-in agent contract."""
    executor = RecordingExecutor()
    executor.result = "ERROR: boom"
    server = make_server(executor)
    content = await server.call_tool("get_resource", {"kind": "pods", "name": "x"})
    assert content[0].text == "ERROR: boom"


async def test_call_tool_rejects_names_outside_configured_surface() -> None:
    """Discovery is not an authorization boundary: the executor also knows
    the write tools, so a caller naming `delete_resource` directly must be
    stopped before dispatch."""
    executor = RecordingExecutor()
    server = make_server(executor)
    for name in ("delete_resource", "scale_resource", "rollout_restart", "nope"):
        content = await server.call_tool(name, {"kind": "pods", "name": "x"})
        assert content[0].text == f"ERROR: tool not available over MCP: {name}"
    assert executor.calls == []


# ---------------------------------------------------------------------------
# HTTP round trip: a real MCP client against the embedded server
# ---------------------------------------------------------------------------


async def test_streamable_http_roundtrip(tmp_path: Path) -> None:
    """End-to-end: serve on an ephemeral loopback port inside the running
    loop, connect with the MCP SDK client, list tools and call one."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    executor = RecordingExecutor()
    endpoint_file = tmp_path / "mcp-endpoint.json"
    server = make_server(executor, port=0, endpoint_path=endpoint_file)
    task = asyncio.create_task(server.run())
    try:
        port = await asyncio.wait_for(server.wait_started(), timeout=10)
        entry = json.loads(endpoint_file.read_text())["servers"][str(os.getpid())]
        assert entry["port"] == port
        assert entry["url"] == f"http://127.0.0.1:{port}/mcp"
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            assert {t.name for t in listed.tools} == {
                t["function"]["name"] for t in READ_TOOLS + UI_TOOLS
            }
            result = await session.call_tool("list_resources", {"kind": "pods"})
            assert result.content[0].type == "text"
            assert getattr(result.content[0], "text", None) == "ok"
        assert executor.calls == [("list_resources", {"kind": "pods"})]
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)
    # The discovery file must not outlive the server.
    assert not endpoint_file.exists()


async def test_hostile_origin_is_rejected() -> None:
    """DNS-rebinding protection: loopback binding alone does not stop a
    malicious webpage from reaching 127.0.0.1, so requests carrying a
    non-loopback Origin must be refused at the transport layer."""
    import httpx

    executor = RecordingExecutor()
    server = make_server(executor, port=0)
    task = asyncio.create_task(server.run())
    try:
        port = await asyncio.wait_for(server.wait_started(), timeout=10)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_resources", "arguments": {"kind": "pods"}},
        }
        headers = {
            "Origin": "http://evil.example",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient() as client:
            # POST to /mcp/ directly: Starlette's Mount answers bare /mcp
            # with a 307 to the slash form, and a browser-driven attack
            # would follow it (307 preserves method and body).
            resp = await client.post(f"http://127.0.0.1:{port}/mcp/", json=payload, headers=headers)
        assert resp.status_code == 403
        assert executor.calls == []
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)


async def test_port_in_use_fails_without_raising(tmp_path: Path) -> None:
    """A bind failure (uvicorn raises SystemExit internally) must not escape
    run(): the TUI keeps going without MCP instead of crashing its shutdown
    path with a BaseExceptionGroup."""
    endpoint_file = tmp_path / "mcp-endpoint.json"
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]
        server = make_server(port=taken, endpoint_path=endpoint_file)
        await asyncio.wait_for(server.run(), timeout=10)  # returns; must not raise
    # Startup never happened, so no discovery record was published.
    assert not endpoint_file.exists()


async def test_remove_endpoint_spares_foreign_record(tmp_path: Path) -> None:
    """Exiting must not delete a discovery record published by *another*
    korvid instance at the same default path."""
    endpoint_file = tmp_path / "mcp-endpoint.json"
    server = make_server(port=0, endpoint_path=endpoint_file)
    task = asyncio.create_task(server.run())
    try:
        await asyncio.wait_for(server.wait_started(), timeout=10)
        foreign = {"url": "http://127.0.0.1:9999/mcp", "port": 9999, "pid": 999999}
        endpoint_file.write_text(json.dumps(foreign))
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)
    assert json.loads(endpoint_file.read_text()) == foreign


async def test_shutdown_requested_before_run_exits_promptly(tmp_path: Path) -> None:
    """request_shutdown() issued before run() reaches uvicorn must not be
    lost - the flag is mirrored onto the server once it exists, so shutdown
    does not stall for the caller's hard-cancel deadline."""
    endpoint_file = tmp_path / "mcp-endpoint.json"
    server = make_server(port=0, endpoint_path=endpoint_file)
    server.request_shutdown()
    await asyncio.wait_for(server.run(), timeout=5)
    # The server never served: no discovery record survives the early exit.
    assert not endpoint_file.exists()


async def test_remove_endpoint_spares_non_dict_record(tmp_path: Path) -> None:
    """A non-dict JSON body (e.g. `[]` from another tool) is foreign data:
    removal must neither crash nor delete it."""
    endpoint_file = tmp_path / "mcp-endpoint.json"
    server = make_server(port=0, endpoint_path=endpoint_file)
    task = asyncio.create_task(server.run())
    try:
        await asyncio.wait_for(server.wait_started(), timeout=10)
        endpoint_file.write_text("[]")
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)
    assert endpoint_file.read_text() == "[]"


# ---------------------------------------------------------------------------
# MCPController (:mcp on/off runtime lifecycle)
# ---------------------------------------------------------------------------


async def test_controller_start_stop_roundtrip(tmp_path: Path) -> None:
    endpoint_file = tmp_path / "mcp-endpoint.json"
    controller = MCPController(lambda: make_server(port=0, endpoint_path=endpoint_file))
    assert controller.status() == "MCP off"
    msg = await asyncio.wait_for(controller.start(), timeout=15)
    assert msg.startswith("MCP on :")
    assert controller.running
    assert controller.status() == msg
    assert endpoint_file.exists()
    msg = await asyncio.wait_for(controller.stop(), timeout=15)
    assert msg == "MCP off"
    assert not controller.running
    assert not endpoint_file.exists()


async def test_controller_start_is_idempotent(tmp_path: Path) -> None:
    """A second :mcp on while running reports state instead of spawning a
    second server."""
    built: list[KorvidMCPServer] = []

    def factory() -> KorvidMCPServer:
        server = make_server(port=0, endpoint_path=tmp_path / "mcp-endpoint.json")
        built.append(server)
        return server

    controller = MCPController(factory)
    first = await asyncio.wait_for(controller.start(), timeout=15)
    second = await asyncio.wait_for(controller.start(), timeout=15)
    assert second == first
    assert len(built) == 1
    await asyncio.wait_for(controller.stop(), timeout=15)


async def test_controller_restart_builds_fresh_server(tmp_path: Path) -> None:
    """uvicorn servers are single-use: on -> off -> on must run a new one."""
    built: list[KorvidMCPServer] = []

    def factory() -> KorvidMCPServer:
        server = make_server(port=0, endpoint_path=tmp_path / "mcp-endpoint.json")
        built.append(server)
        return server

    controller = MCPController(factory)
    await asyncio.wait_for(controller.start(), timeout=15)
    await asyncio.wait_for(controller.stop(), timeout=15)
    msg = await asyncio.wait_for(controller.start(), timeout=15)
    assert msg.startswith("MCP on :")
    assert len(built) == 2
    assert built[0] is not built[1]
    await asyncio.wait_for(controller.stop(), timeout=15)


async def test_controller_start_reports_bind_failure() -> None:
    """Port already taken: the user gets an ERROR line and the controller is
    back to a startable state (task fully reaped)."""
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]
        controller = MCPController(lambda: make_server(port=taken))
        msg = await asyncio.wait_for(controller.start(), timeout=15)
    assert msg.startswith("ERROR")
    assert not controller.running
    assert controller.status() == "MCP off"


async def test_controller_stop_without_start_is_noop() -> None:
    controller = MCPController(lambda: make_server(port=0))
    assert await asyncio.wait_for(controller.stop(), timeout=5) == "MCP off"
    assert await controller.shutdown() is None


async def test_controller_shutdown_survives_cancellation(tmp_path: Path) -> None:
    """A cancelled shutdown (e.g. Textual killing a :mcp off worker at app
    exit) must not lose ownership: the next shutdown still finds the server
    task and stops it."""
    controller = MCPController(
        lambda: make_server(port=0, endpoint_path=tmp_path / "mcp-endpoint.json")
    )
    await asyncio.wait_for(controller.start(), timeout=15)
    shutdown_attempt = asyncio.create_task(controller.shutdown())
    await asyncio.sleep(0)  # let it reach the non-cancelling wait
    shutdown_attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown_attempt
    # Ownership retained: teardown can still reach and stop the server.
    assert await asyncio.wait_for(controller.shutdown(), timeout=15) is None
    assert not controller.running


async def test_remove_endpoint_preserves_other_live_instances(tmp_path: Path) -> None:
    """The discovery file is a pid-keyed registry: instance B exiting must
    drop only its own entry, leaving instance A's record discoverable."""
    endpoint_file = tmp_path / "mcp-endpoint.json"
    other = {"url": "http://127.0.0.1:9999/mcp", "port": 9999, "pid": 999999}
    endpoint_file.write_text(json.dumps({"servers": {"999999": other}}))
    server = make_server(port=0, endpoint_path=endpoint_file)
    task = asyncio.create_task(server.run())
    try:
        port = await asyncio.wait_for(server.wait_started(), timeout=10)
        registry = json.loads(endpoint_file.read_text())["servers"]
        assert registry[str(os.getpid())]["port"] == port
        assert registry["999999"] == other
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)
    assert json.loads(endpoint_file.read_text()) == {"servers": {"999999": other}}
