"""Tests for the embedded Streamable HTTP MCP server (issue #11)."""

from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
from typing import Any

import pytest

from korvid.agent.mcp_server import KorvidMCPServer, default_endpoint_path
from korvid.agent.tools import READ_TOOLS, UI_TOOLS, ToolExecutor
from korvid.k8s.discovery import PODS_META


class RecordingExecutor(ToolExecutor):
    """ToolExecutor that records dispatches instead of touching a cluster."""

    def __init__(self) -> None:
        super().__init__(kube=None, aliases={"pods": PODS_META})  # type: ignore[arg-type]
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
    from korvid.agent.tools import WRITE_TOOL_NAMES

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


# ---------------------------------------------------------------------------
# HTTP round trip: a real MCP client against the embedded server
# ---------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
async def test_streamable_http_roundtrip(tmp_path: Path) -> None:
    """End-to-end: serve on an ephemeral loopback port inside the running
    loop, connect with the MCP SDK client, list tools and call one.

    The filterwarnings mark suppresses ResourceWarning-derived unraisables
    from the MCP SDK's own transport internals (anyio memory streams left
    to the GC during client/session teardown) - an SDK artifact unrelated
    to the server under test; the socket itself closes cleanly."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    executor = RecordingExecutor()
    endpoint_file = tmp_path / "mcp-endpoint.json"
    server = make_server(executor, port=0, endpoint_path=endpoint_file)
    task = asyncio.create_task(server.run())
    try:
        port = await asyncio.wait_for(server.wait_started(), timeout=10)
        info = json.loads(endpoint_file.read_text())
        assert info["port"] == port
        assert info["url"] == f"http://127.0.0.1:{port}/mcp"
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
        # Collect the SDK's abandoned transport objects NOW so their
        # ResourceWarning-driven unraisables fire inside this (filtered)
        # test instead of being attributed to whichever test the GC
        # happens to run under later.
        gc.collect()
    # The discovery file must not outlive the server.
    assert not endpoint_file.exists()
