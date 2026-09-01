"""Tests for the embedded Streamable HTTP MCP server (issue #11)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import types

from korvid.core.secrets import MASK_PLACEHOLDER
from korvid.k8s.discovery import PODS_META
from korvid.k8s.logs import LogLine
from korvid.mcp.server import (
    KorvidMCPServer,
    MCPController,
    _replace_atomically,
    default_endpoint_path,
)
from korvid.tools.executor import (
    PROPOSAL_TOOLS,
    READ_TOOLS,
    UI_TOOLS,
    ToolExecutor,
    ToolOutcome,
)
from korvid.tools.registry import mcp_tool_schemas
from korvid.tools.structured import load_structured_document
from tests.platforms import POSIX
from tests.tools.executor_fakes import (
    LONG_NAME_ENV_SENTINEL,
    NESTED_SECRET_SENTINEL,
    PARENT_SECRET,
    FakeBridge,
    ParentCredentialKube,
    _ambiguous_key_manifest,
    _diagnose_executor,
    identity_last_crd,
    oversized_crd_with_nested_credentials,
)


class RecordingExecutor(ToolExecutor):
    """ToolExecutor that records dispatches instead of touching a cluster.

    `execute_recorded` is the canonical method - `ToolExecutor.execute`
    delegates to it - so overriding that one keeps the fake on the same
    contract as the real executor, including the producer's `error` bit.
    """

    def __init__(self) -> None:
        super().__init__(kube=None, aliases={"pods": PODS_META})  # type: ignore[arg-type]  # dispatch is overridden; kube is never touched
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = "ok"
        self.error = False

    async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self.calls.append((name, arguments))
        return ToolOutcome(text=self.result, error=self.error)


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
        assert by_name[fn["name"]].input_schema == fn["parameters"]


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


async def test_mcp_results_are_redacted_like_the_agent_path() -> None:
    """MCP hosts are outside korvid too (PR #197 review).

    The MCP server dispatches through the same `ToolExecutor`, so a
    manifest too large to send whole must be redacted before it is
    shrunk here as well — the reduction removes the nested `kind:
    Secret` and clamps the credential env name that identify the values.
    """

    class ManifestKube:
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            return oversized_crd_with_nested_credentials()

    executor = ToolExecutor(ManifestKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # read-only test double
    server = make_server(executor)

    content = await server.call_tool(
        "get_resource", {"kind": "pods", "name": "composite-0", "namespace": "prod"}
    )

    assert NESTED_SECRET_SENTINEL not in content[0].text
    assert LONG_NAME_ENV_SENTINEL not in content[0].text
    assert yaml.safe_load(content[0].text)["kind"] == "CompositeApp"


async def test_mcp_resource_results_mask_private_key_fields() -> None:
    class ManifestKube:
        async def get_object(
            self,
            meta: Any,
            namespace: str | None,
            name: str,
        ) -> dict[str, Any]:
            return {
                "kind": "ConfigMap",
                "metadata": {"name": name},
                "data": {
                    "client-key-data": "mcp-private-key-sentinel",
                    "publicKeyId": "public-key-id",
                },
            }

    executor = ToolExecutor(ManifestKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # read-only test double
    server = make_server(executor)

    content = await server.call_tool(
        "get_resource", {"kind": "pods", "name": "client-config", "namespace": "default"}
    )

    loaded = yaml.safe_load(content[0].text)
    assert loaded["data"] == {
        "client-key-data": MASK_PLACEHOLDER,
        "publicKeyId": "public-key-id",
    }
    assert "mcp-private-key-sentinel" not in content[0].text


async def test_mcp_reports_a_manifest_too_deep_to_redact_as_a_safe_error() -> None:
    """An MCP host has no turn to stop, so a document the redactor could
    not finish walking comes back as the same safe refusal string every
    other producer error uses — naming the shape, never the document
    (PR #197 review)."""

    class DeepKube:
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            document: Any = {"kind": "Secret", "data": {"password": "cmF3LXNlY3JldA=="}}
            for _ in range(1500):
                document = {"spec": {"nested": document}}
            return {"apiVersion": "v1", "kind": "CompositeApp", **document}

    executor = ToolExecutor(DeepKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # read-only test double
    server = make_server(executor)

    content = await server.call_tool(
        "get_resource", {"kind": "pods", "name": "composite-0", "namespace": "prod"}
    )

    assert content[0].text.startswith("ERROR:")
    assert "too deeply nested" in content[0].text
    assert "cmF3LXNlY3JldA==" not in content[0].text


async def test_mcp_compound_diagnoses_are_masked_in_every_section() -> None:
    """`docs/mcp.md` promises an MCP client the same masked report the
    model sees. Round 9 redacted the per-pod blocks; the workload's own
    conditions, Warning events and child-LIST errors were assembled after
    that pass and crossed to the client verbatim (PR #197 final review)."""
    kube = ParentCredentialKube(
        condition_message=f"probe rejected api_key={PARENT_SECRET}",
        event_message=f"registry auth failed password={PARENT_SECRET}",
    )
    server = make_server(_diagnose_executor(kube))

    content = await server.call_tool(
        "diagnose_workload", {"kind": "deployments", "name": "api", "namespace": "default"}
    )

    assert PARENT_SECRET not in content[0].text
    assert MASK_PLACEHOLDER in content[0].text
    assert "MinimumReplicasUnavailable" in content[0].text
    assert "FailedCreate (3x" in content[0].text


async def test_mcp_log_results_are_redacted_by_the_producer() -> None:
    class LoggingKube:
        async def get_object(
            self,
            meta: Any,
            namespace: str | None,
            name: str,
        ) -> dict[str, Any]:
            return {"spec": {"containers": [{"name": "main"}]}}

        async def stream_logs(
            self,
            namespace: str,
            pod: str,
            container: str,
            *,
            follow: bool,
            tail_lines: int,
        ) -> AsyncIterator[LogLine]:
            yield LogLine(
                pod=pod,
                container=container,
                text="password=mcp-password-sentinel",
            )
            yield LogLine(
                pod=pod,
                container=container,
                text="token=mcp-token-sentinel",
            )

    executor = ToolExecutor(LoggingKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # read-only test double
    server = make_server(executor)

    content = await server.call_tool(
        "get_logs",
        {"pod": "api-0", "namespace": "prod"},
    )

    assert "mcp-password-sentinel" not in content[0].text
    assert "mcp-token-sentinel" not in content[0].text
    assert content[0].text.count(MASK_PLACEHOLDER) == 2


async def test_mcp_event_results_are_redacted_by_the_producer() -> None:
    class EventKube:
        async def get_object(
            self,
            meta: Any,
            namespace: str | None,
            name: str,
        ) -> dict[str, Any]:
            return {"kind": "Pod", "metadata": {"name": name, "uid": "pod-uid"}}

        async def list_events_for(
            self,
            namespace: str,
            name: str,
            *,
            kind: str | None = None,
            uid: str | None = None,
        ) -> list[dict[str, Any]]:
            return [
                {
                    "type": "Warning",
                    "reason": "Failed",
                    "count": 1,
                    "message": "Authorization: mcp-auth-sentinel",
                }
            ]

    executor = ToolExecutor(EventKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # read-only test double
    server = make_server(executor)

    content = await server.call_tool(
        "get_events",
        {"kind": "pods", "name": "api-0", "namespace": "prod"},
    )

    assert "mcp-auth-sentinel" not in content[0].text
    assert MASK_PLACEHOLDER in content[0].text
    assert "Warning Failed (1x)" in content[0].text


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
# MCP follow mode + activity notes (issue #153)
# ---------------------------------------------------------------------------


def make_follow_server(
    executor: ToolExecutor,
    ui: FakeBridge | None,
    *,
    follow: bool = True,
    note_activity: Any = None,
) -> KorvidMCPServer:
    return KorvidMCPServer(
        executor,
        READ_TOOLS + UI_TOOLS,
        port=0,
        ui=ui,
        follow_enabled=lambda: follow,
        note_activity=note_activity,
    )


async def _drain_follow(server: KorvidMCPServer) -> None:
    while server._follow_tasks:
        await asyncio.gather(*server._follow_tasks)


async def test_follow_mirrors_a_cluster_read_without_blocking_the_response() -> None:
    executor = RecordingExecutor()
    ui = FakeBridge()
    server = make_follow_server(executor, ui)
    content = await server.call_tool("get_logs", {"pod": "api-1", "namespace": "prod"})
    assert content[0].text == "ok"  # the MCP response never waits on the UI
    await _drain_follow(server)
    assert ui.calls == [("open_logs", {"pod": "api-1", "namespace": "prod", "container": None})]


async def test_follow_off_notes_activity_instead_of_mirroring() -> None:
    executor = RecordingExecutor()
    ui = FakeBridge()
    notes: list[str] = []
    server = make_follow_server(executor, ui, follow=False, note_activity=notes.append)
    await server.call_tool("get_logs", {"pod": "api-1", "namespace": "prod"})
    await _drain_follow(server)
    assert ui.calls == []
    assert len(notes) == 1
    assert "get_logs" in notes[0]
    assert "api-1" in notes[0]


async def test_failed_read_is_not_mirrored() -> None:
    """Navigating the TUI to a view whose read just failed would mislead;
    the failure is still surfaced as activity."""
    executor = RecordingExecutor()
    executor.result = "ERROR: boom"
    executor.error = True
    ui = FakeBridge()
    notes: list[str] = []
    server = make_follow_server(executor, ui, note_activity=notes.append)
    await server.call_tool("list_resources", {"kind": "pods"})
    await _drain_follow(server)
    assert ui.calls == []
    assert len(notes) == 1


async def test_a_successful_read_whose_text_begins_with_error_is_still_mirrored() -> None:
    """Follow mode must read the producer's verdict, not the first line.

    `get_logs` returns raw log lines, so a pod logging `ERROR: connection
    refused` produces a perfectly successful read. Inferring failure from
    the prefix would silently drop its mirror - the read still happened,
    and issue #153 exists to make external reads visible.
    """
    executor = RecordingExecutor()
    executor.result = "ERROR: connection refused"
    executor.error = False
    ui = FakeBridge()
    notes: list[str] = []
    server = make_follow_server(executor, ui, note_activity=notes.append)
    await server.call_tool("list_resources", {"kind": "pods"})
    await _drain_follow(server)
    assert ui.calls != [], "a successful read was treated as a failure and not mirrored"
    assert notes == []


async def test_ui_only_tools_are_neither_mirrored_nor_noted() -> None:
    """navigate/set_filter already move the screen visibly - double
    surfacing them would be noise."""
    executor = RecordingExecutor()
    ui = FakeBridge()
    notes: list[str] = []
    server = make_follow_server(executor, ui, note_activity=notes.append)
    await server.call_tool("navigate", {"view": "pods"})
    await _drain_follow(server)
    assert ui.calls == []  # the executor handled it; no extra mirror
    assert notes == []


async def test_follow_without_a_bridge_degrades_to_activity_notes() -> None:
    executor = RecordingExecutor()
    notes: list[str] = []
    server = make_follow_server(executor, None, note_activity=notes.append)
    await server.call_tool("list_resources", {"kind": "pods"})
    await _drain_follow(server)
    assert len(notes) == 1


async def test_mirror_failure_never_breaks_the_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ExplodingBridge(FakeBridge):
        async def agent_open_logs(
            self, pod: str, namespace: str, container: str | None = None
        ) -> str:
            raise RuntimeError("UI gone")

    executor = RecordingExecutor()
    server = make_follow_server(executor, ExplodingBridge())
    content = await server.call_tool("get_logs", {"pod": "x", "namespace": "d"})
    assert content[0].text == "ok"
    await _drain_follow(server)  # raises nothing: fire-and-forget swallows


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
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write),
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
            assert result.is_error is False, "a successful call must not be flagged as an error"
        assert executor.calls == [("list_resources", {"kind": "pods"})]
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)
    # The discovery file must not outlive the server.
    assert not endpoint_file.exists()


async def test_get_is_refused_with_405_instead_of_an_sse_stream() -> None:
    """A standalone GET must be answered with 405, not an infinite SSE stream.

    This server is stateless with JSON responses: it never sends
    server-initiated messages, so the SDK's standalone GET SSE stream can
    only ever hang open — holding uvicorn's graceful shutdown hostage until
    the controller hard-cancels it (issue #136). The MCP spec allows a
    server that offers no SSE stream to answer GET with 405.
    """
    import httpx

    server = make_server(port=0)
    task = asyncio.create_task(server.run())
    try:
        port = await asyncio.wait_for(server.wait_started(), timeout=10)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/mcp/",
                headers={"Accept": "text/event-stream"},
            )
        assert resp.status_code == 405
        assert "text/event-stream" not in resp.headers.get("content-type", "")
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)


async def test_shutdown_completes_while_a_client_holds_a_get_connection() -> None:
    """Graceful shutdown must finish even while a GET connection is open.

    Regression for issue #136: the SDK served GET as a never-ending SSE
    stream, uvicorn's graceful shutdown waited forever on the connection,
    and the controller's hard cancel tore down the stream mid-request —
    "Exception in ASGI application" (CancelledError) on the TUI's terminal.
    The GET is held open (response headers received, body still streaming)
    while shutdown is requested, so the old behavior fails this test by
    timing out instead of merely racing the connection close.
    """
    import httpx

    server = make_server(port=0)
    task = asyncio.create_task(server.run())
    port = await asyncio.wait_for(server.wait_started(), timeout=10)
    response_started = asyncio.Event()

    async def issue_get() -> None:
        async with (
            httpx.AsyncClient(timeout=30) as client,
            client.stream(
                "GET",
                f"http://127.0.0.1:{port}/mcp/",
                headers={"Accept": "text/event-stream"},
            ) as resp,
        ):
            response_started.set()
            async for _ in resp.aiter_lines():  # drains nothing on a 405
                pass

    get_task = asyncio.create_task(issue_get())
    try:
        await asyncio.wait_for(response_started.wait(), timeout=10)
        server.request_shutdown()
        # Must complete gracefully — no hard-cancel fallback needed.
        await asyncio.wait_for(task, timeout=5)
        assert task.done()
        await asyncio.wait_for(get_task, timeout=10)
    finally:
        if not get_task.done():
            get_task.cancel()
        server.request_shutdown()
        if not task.done():
            await asyncio.wait_for(task, timeout=10)


async def test_hostile_origin_get_is_rejected_not_answered_405() -> None:
    """DNS-rebinding protection must run before the GET refusal: a
    non-loopback Origin on a GET is refused by the transport security
    check (403), not acknowledged with the generic 405."""
    import httpx

    server = make_server(port=0)
    task = asyncio.create_task(server.run())
    try:
        port = await asyncio.wait_for(server.wait_started(), timeout=10)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/mcp/",
                headers={
                    "Origin": "http://evil.example",
                    "Accept": "text/event-stream",
                },
            )
        assert resp.status_code == 403
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)


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


async def test_controller_shutdown_leaves_a_freshly_started_run_untouched() -> None:
    """A shutdown bound to the old run must not wipe ownership of a fresh
    run installed while it awaited that old task: `running` would report
    False for a live server, and follow-up sweeps would treat the fresh
    run's proposals as stale."""
    from types import SimpleNamespace

    controller = MCPController(make_server)  # factory is never called here
    release = asyncio.Event()
    forever = asyncio.Event()
    fresh_holder: dict[str, asyncio.Task[None]] = {}

    async def fresh_run() -> None:
        await forever.wait()

    async def old_run() -> None:
        # Woken by shutdown()'s own request_shutdown call, so this swap runs
        # while shutdown() awaits *this* task — a racing start().  (Driving
        # the swap from a pre-created sibling task is order-dependent: on
        # 3.11 wait_for wraps shutdown() in a task scheduled *after* the
        # sibling, which would swap before shutdown captured the old run.)
        await release.wait()
        fresh = asyncio.create_task(fresh_run())
        controller._server = SimpleNamespace(request_shutdown=lambda: None)  # type: ignore[assignment]  # test double
        controller._task = fresh
        fresh_holder["task"] = fresh

    old = asyncio.create_task(old_run())
    controller._server = SimpleNamespace(request_shutdown=release.set)  # type: ignore[assignment]  # test double
    controller._task = old
    try:
        result = await asyncio.wait_for(controller.shutdown(), timeout=10)
        assert result is None
        assert controller.running, "old-run shutdown wiped the fresh run's ownership"
        assert controller._task is fresh_holder["task"]
    finally:
        old.cancel()
        pending = fresh_holder.get("task")
        if pending is not None:
            pending.cancel()


async def test_controller_pending_task_reports_the_live_run() -> None:
    """`pending_task()` is the snapshot the app captures under its lock so a
    follow-up teardown wait binds to this exact run."""
    controller = MCPController(make_server)
    assert controller.pending_task() is None

    async def run() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(run())
    controller._task = task
    try:
        assert controller.pending_task() is task
    finally:
        task.cancel()


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


# ---------------------------------------------------------------------------
# External write proposals (issue #110)
# ---------------------------------------------------------------------------


_PROPOSE_ARGS = {"action": "delete", "kind": "pods", "name": "web-1", "namespace": "default"}


def make_proposal_server(
    executor: ToolExecutor | None = None,
    *,
    capability_token: str | None = "cap-tok",
    port: int = 0,
    endpoint_path: Path | None = None,
) -> KorvidMCPServer:
    return KorvidMCPServer(
        executor or RecordingExecutor(),
        READ_TOOLS + UI_TOOLS + PROPOSAL_TOOLS,
        port=port,
        endpoint_path=endpoint_path,
        capability_token=capability_token,
    )


async def test_proposal_tools_are_listed_when_configured() -> None:
    server = make_proposal_server()
    tools = {t.name for t in await server.list_tools()}
    assert {"propose_write", "get_write_proposal", "cancel_write_proposal"} <= tools


async def test_propose_write_without_capability_is_rejected() -> None:
    executor = RecordingExecutor()
    server = make_proposal_server(executor)
    result = await server.call_tool("propose_write", dict(_PROPOSE_ARGS))
    assert result[0].text.startswith("ERROR:")
    assert "capability" in result[0].text
    assert executor.calls == []


async def test_propose_write_with_wrong_capability_is_rejected() -> None:
    executor = RecordingExecutor()
    server = make_proposal_server(executor)
    result = await server.call_tool("propose_write", {**_PROPOSE_ARGS, "capability": "nope"})
    assert result[0].text.startswith("ERROR:")
    assert executor.calls == []


async def test_non_ascii_capability_is_rejected_not_a_crash() -> None:
    """`secrets.compare_digest` raises TypeError on non-ASCII `str` input;
    the capability is untrusted MCP input, so a value like "é" must produce
    the documented error response, not an exception escaping the handler."""
    executor = RecordingExecutor()
    server = make_proposal_server(executor)
    result = await server.call_tool("propose_write", {**_PROPOSE_ARGS, "capability": "é" * 8})
    assert result[0].text == "ERROR: invalid or missing capability token"
    assert executor.calls == []


async def test_lone_surrogate_capability_is_rejected_not_a_crash() -> None:
    """A raw JSON `"\\ud800"` decodes to a lone surrogate, which raises
    UnicodeEncodeError from `str.encode()` — the handler must still return
    the documented capability error for every string input."""
    executor = RecordingExecutor()
    server = make_proposal_server(executor)
    result = await server.call_tool("propose_write", {**_PROPOSE_ARGS, "capability": "\ud800"})
    assert result[0].text == "ERROR: invalid or missing capability token"
    assert executor.calls == []


async def test_propose_write_with_capability_dispatches_with_identity() -> None:
    executor = RecordingExecutor()
    server = make_proposal_server(executor)
    result = await server.call_tool("propose_write", {**_PROPOSE_ARGS, "capability": "cap-tok"})
    assert result[0].text == "ok"
    assert len(executor.calls) == 1
    name, args = executor.calls[0]
    assert name == "propose_write"
    assert "capability" not in args
    assert isinstance(args["_session_id"], str)
    assert args["_session_id"]
    assert isinstance(args["_client_name"], str)
    assert isinstance(args["_client_version"], str)
    assert {k: v for k, v in args.items() if not k.startswith("_")} == _PROPOSE_ARGS


async def test_caller_supplied_reserved_args_are_overridden() -> None:
    executor = RecordingExecutor()
    server = make_proposal_server(executor)
    await server.call_tool(
        "propose_write",
        {**_PROPOSE_ARGS, "capability": "cap-tok", "_session_id": "spoofed"},
    )
    _, args = executor.calls[0]
    assert args["_session_id"] != "spoofed"


async def test_reserved_args_are_stripped_from_plain_tools() -> None:
    executor = RecordingExecutor()
    server = make_proposal_server(executor)
    await server.call_tool("list_resources", {"kind": "pods", "_session_id": "spoofed"})
    assert executor.calls == [("list_resources", {"kind": "pods"})]


async def test_capability_is_required_for_status_and_cancel() -> None:
    executor = RecordingExecutor()
    server = make_proposal_server(executor)
    for tool in ("get_write_proposal", "cancel_write_proposal"):
        result = await server.call_tool(tool, {"proposal_id": "p1"})
        assert result[0].text.startswith("ERROR:")
    assert executor.calls == []
    result = await server.call_tool(
        "cancel_write_proposal", {"proposal_id": "p1", "capability": "cap-tok"}
    )
    assert result[0].text == "ok"


async def test_proposal_tools_without_a_configured_token_are_rejected() -> None:
    executor = RecordingExecutor()
    server = make_proposal_server(executor, capability_token=None)
    result = await server.call_tool("propose_write", {**_PROPOSE_ARGS, "capability": ""})
    assert result[0].text.startswith("ERROR:")
    assert "not enabled" in result[0].text
    assert executor.calls == []


async def test_streamable_http_proposal_roundtrip(tmp_path: Path) -> None:
    """Issue #110 requires a real MCP round trip for the proposal surface:
    an SDK client over Streamable HTTP submits a proposal with the published
    capability, and the only dispatch is to the proposal bridge — no
    mutation tool is ever reached."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    executor = RecordingExecutor()
    endpoint_file = tmp_path / "mcp-endpoint.json"
    server = make_proposal_server(executor, port=0, endpoint_path=endpoint_file)
    task = asyncio.create_task(server.run())
    try:
        port = await asyncio.wait_for(server.wait_started(), timeout=10)
        # The capability travels via the discovery file, exactly as a local
        # agent would learn it.
        entry = json.loads(endpoint_file.read_text())["servers"][str(os.getpid())]
        capability = entry["capability"]
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            assert "propose_write" in {t.name for t in listed.tools}
            result = await session.call_tool(
                "propose_write", {**_PROPOSE_ARGS, "capability": capability}
            )
            assert result.content[0].type == "text"
            assert getattr(result.content[0], "text", None) == "ok"
            assert result.is_error is False, "an accepted proposal must not be flagged as an error"
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)
    # Exactly one dispatch, to the proposal bridge — nothing mutating.
    assert len(executor.calls) == 1
    name, args = executor.calls[0]
    assert name == "propose_write"
    assert "capability" not in args
    assert {k: v for k, v in args.items() if not k.startswith("_")} == _PROPOSE_ARGS


async def test_endpoint_file_publishes_capability_with_owner_only_mode(tmp_path: Path) -> None:
    """The endpoint file carries a capability token and must be created 0600.

    On POSIX we verify the effective stat mode bits. On Windows/NTFS, Python's
    POSIX-mode emulation does not enforce real ACLs; we verify the file was
    created and the server published the expected capability value.
    """
    endpoint_file = tmp_path / "mcp-endpoint.json"
    server = make_proposal_server(port=0, endpoint_path=endpoint_file)
    task = asyncio.create_task(server.run())
    try:
        await asyncio.wait_for(server.wait_started(), timeout=10)
        entry = json.loads(endpoint_file.read_text())["servers"][str(os.getpid())]
        assert entry["capability"] == "cap-tok"
        if POSIX:
            assert (endpoint_file.stat().st_mode & 0o777) == 0o600
        else:
            # Windows: POSIX mode bits are not meaningful on NTFS; assert the
            # file was created atomically (exists) with the capability token.
            assert endpoint_file.is_file()
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)


async def test_endpoint_file_omits_capability_when_proposals_are_off(tmp_path: Path) -> None:
    endpoint_file = tmp_path / "mcp-endpoint.json"
    server = make_server(port=0, endpoint_path=endpoint_file)
    task = asyncio.create_task(server.run())
    try:
        await asyncio.wait_for(server.wait_started(), timeout=10)
        entry = json.loads(endpoint_file.read_text())["servers"][str(os.getpid())]
        assert "capability" not in entry
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)


def test_endpoint_file_is_created_owner_only_not_merely_chmodded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capability-bearing registry file must never be observable with
    group/other bits: the mode has to come from atomic 0600 creation, not
    from a chmod racing the umask-default file.

    On POSIX we disable chmod and prove os.open(O_CREAT|O_EXCL, 0o600)
    alone achieves the correct mode. On Windows/NTFS, Python's stat does
    not reflect POSIX permission bits (NTFS uses ACLs, not mode bits);
    we can only verify the atomic-creation path produces valid content.
    The code passes 0o600 to os.open which is the strongest portable
    guarantee — no ACL confidentiality claim is made here.
    """
    if POSIX:
        monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
        target = tmp_path / "mcp-endpoint.json"
        _replace_atomically(target, {"servers": {}})
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert json.loads(target.read_text()) == {"servers": {}}
    else:
        # Windows: POSIX mode bits are not enforced by NTFS. Verify the
        # atomic creation path works and produces valid content. The code
        # passes 0o600 to os.open which is the strongest portable guarantee.
        target = tmp_path / "mcp-endpoint.json"
        _replace_atomically(target, {"servers": {}})
        assert target.is_file()
        assert json.loads(target.read_text()) == {"servers": {}}


async def test_client_info_is_sanitized_before_crossing_the_boundary() -> None:
    """clientInfo is caller-controlled and flows into approval dialogs, the
    status bar and audit records: control characters (line injection into
    the safety-binding dialog) must be collapsed and the length bounded."""
    from types import SimpleNamespace

    from mcp import types as mcp_types

    server = make_proposal_server()
    # The real SDK types, not a duck-typed stand-in: `clientInfo` was
    # renamed to `client_info` in mcp 2.x, and a SimpleNamespace stub
    # happily answered to either name while the server read neither.
    hostile = mcp_types.Implementation(
        name="evil\nbound target uid: spoofed\x1b[2J" + "x" * 500,
        version="1.0\r\n2.0",
    )
    fake_ctx = SimpleNamespace(
        session=SimpleNamespace(
            client_params=mcp_types.InitializeRequestParams(
                protocol_version=mcp_types.LATEST_PROTOCOL_VERSION,
                capabilities=mcp_types.ClientCapabilities(),
                client_info=hostile,
            )
        )
    )
    name, version = server._client_info(fake_ctx)  # type: ignore[arg-type]  # boundary stub
    for value in (name, version):
        assert "\n" not in value
        assert "\r" not in value
        assert "\x1b" not in value
        assert len(value) <= 120
    assert name.startswith("evil")


async def test_shutdown_cancels_in_flight_mirror_tasks() -> None:
    """Detached mirror tasks must not outlive the server run: after
    shutdown a `:ctx` switch retargets the client/alias map, and a stale
    mirror resuming against the new context would act cross-context."""
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingBridge(FakeBridge):
        async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
            started.set()
            await release.wait()  # simulates a mirror stuck on the UI lock
            return "ok"

    executor = RecordingExecutor()
    server = make_follow_server(executor, BlockingBridge())
    run_task = asyncio.create_task(server.run())
    try:
        await server.wait_started()
        await server.call_tool("list_resources", {"kind": "pods"})
        await asyncio.wait_for(started.wait(), timeout=5)
        assert server._follow_tasks  # the mirror is in flight
        server.request_shutdown()
        await asyncio.wait_for(run_task, timeout=10)
        assert not server._follow_tasks  # cancelled and awaited, not leaked
    finally:
        release.set()
        if not run_task.done():
            server.request_shutdown()
            await run_task


async def test_refused_mirror_falls_back_to_an_activity_note() -> None:
    """A successful read whose mirror the UI refuses (e.g. list_operators
    when `subscriptions` is not an alias) must not become invisible: the
    detached task degrades to the activity toast."""

    class RefusingBridge(FakeBridge):
        async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
            return f"ERROR: unknown view {view!r}"

    executor = RecordingExecutor()
    notes: list[str] = []
    server = make_follow_server(executor, RefusingBridge(), note_activity=notes.append)
    await server.call_tool("list_operators", {})
    await _drain_follow(server)
    assert len(notes) == 1
    assert "list_operators" in notes[0]


async def test_mcp_gets_a_safe_error_when_redaction_blocks_a_result() -> None:
    """A blocked result stops korvid's *turn*; MCP has no turn to stop.

    The string contract holds: the host sees an `ERROR: ...` result
    naming the shape that failed, never the document behind it, and
    `call_tool` does not start raising at this boundary (PR #197 review).
    """

    class UnredactableKube:
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            return {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": "not-a-mapping",
                "data": {"password": "cmF3LXNlY3JldA=="},
            }

    executor = ToolExecutor(UnredactableKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # read-only test double
    server = make_server(executor)

    content = await server.call_tool(
        "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
    )

    assert content[0].text.startswith("ERROR:")
    assert "cmF3LXNlY3JldA==" not in content[0].text
    # The adapter must convert the raised block into a flagged result, not
    # let it escape: `execute_recorded` raises where `execute` did not.
    result = await server._on_call_tool(
        None,  # type: ignore[arg-type]  # identity is not consulted on this path
        types.CallToolRequestParams(
            name="get_resource", arguments={"kind": "pods", "name": "s", "namespace": "d"}
        ),
    )
    assert result.is_error is True
    assert "cmF3LXNlY3JldA==" not in str(result.content[0])


async def test_mcp_bounded_manifests_still_name_their_object() -> None:
    """An MCP client gets the same reduced document the model does, and it
    has to be identifiable there too (PR #197 review)."""

    class IdentityLastKube:
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            return identity_last_crd()

    executor = ToolExecutor(IdentityLastKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # read-only test double
    server = make_server(executor)

    content = await server.call_tool(
        "get_resource", {"kind": "pods", "name": "composite-0", "namespace": "prod"}
    )

    manifest = yaml.safe_load(content[0].text)
    assert manifest["kind"] == "CompositeApp"
    assert manifest["metadata"]["name"] == "composite-0"


async def test_mcp_manifests_stay_readable_by_the_strict_reader() -> None:
    """MCP shares the producer, so what it hands a client is the same
    unambiguous document the model's boundary re-reads (round 13)."""

    class AmbiguousKeyKube:
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            return _ambiguous_key_manifest()

    executor = ToolExecutor(AmbiguousKeyKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # read-only test double
    server = make_server(executor)

    content = await server.call_tool(
        "get_resource", {"kind": "pods", "name": "flags", "namespace": "prod"}
    )
    loaded = load_structured_document(content[0].text)

    assert loaded == _ambiguous_key_manifest()


async def test_a_wrong_capability_is_refused_over_the_real_transport(tmp_path: Path) -> None:
    """The approval gate must hold on the path the SDK actually dispatches.

    Every other capability test calls `call_tool` directly. mcp 2.x moved
    registration into the constructor, so a handler wired to the wrong
    callable would leave those green while the wire path bypassed the gate
    entirely. This drives a real client over Streamable HTTP.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    executor = RecordingExecutor()
    server = make_proposal_server(executor, port=0, endpoint_path=tmp_path / "e.json")
    task = asyncio.create_task(server.run())
    try:
        port = await asyncio.wait_for(server.wait_started(), timeout=10)
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "propose_write", {**_PROPOSE_ARGS, "capability": "not-the-token"}
            )
            assert getattr(result.content[0], "text", "").startswith("ERROR:")
            # `isError` is the spec's in-band failure signal, not a
            # transport error: a host that trusts it would otherwise file a
            # refused proposal as a successful call.
            assert result.is_error is True
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)
    assert executor.calls == [], "a refused proposal reached the executor"


async def test_client_identity_is_threaded_from_the_request_context() -> None:
    """`_client_name` lands in approval dialogs and audit records.

    mcp 2.x hands the request context to the handler instead of exposing it
    as ambient state, so the value now has to be threaded through the call.
    A handler that dropped its `ctx` would still inject the key — as an
    empty string — and every type-level assertion would stay green, so this
    drives the registered callback and asserts the name that arrives.

    (Over the wire the server is stateless, so `client_params` is genuinely
    absent and the identity degrades to `""` by design; this exercises the
    handshake-carrying case that a stateful client produces.)
    """
    from types import SimpleNamespace

    from mcp import types as mcp_types

    executor = RecordingExecutor()
    server = make_proposal_server(executor)
    ctx = SimpleNamespace(
        session=SimpleNamespace(
            client_params=mcp_types.InitializeRequestParams(
                protocol_version=mcp_types.LATEST_PROTOCOL_VERSION,
                capabilities=mcp_types.ClientCapabilities(),
                client_info=mcp_types.Implementation(name="probe-host", version="9.9.9"),
            )
        )
    )
    await server._on_call_tool(
        ctx,  # type: ignore[arg-type]  # boundary stub
        mcp_types.CallToolRequestParams(
            name="propose_write", arguments={**_PROPOSE_ARGS, "capability": "cap-tok"}
        ),
    )
    assert len(executor.calls) == 1
    args = executor.calls[0][1]
    assert args["_client_name"] == "probe-host"
    assert args["_client_version"] == "9.9.9"


async def test_a_log_line_beginning_with_error_is_not_a_failed_call(tmp_path: Path) -> None:
    """`is_error` must come from the producer, never from the text.

    `ToolOutcome.error` exists precisely because successful content can
    begin with `ERROR:` — `get_logs` returns raw log lines, and a pod that
    logs `ERROR: connection refused` would otherwise be reported to every
    MCP host as a failed tool call.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    executor = RecordingExecutor()
    executor.result = "ERROR: connection refused\nERROR: retrying"
    server = make_server(executor, port=0, endpoint_path=tmp_path / "e.json")
    task = asyncio.create_task(server.run())
    try:
        port = await asyncio.wait_for(server.wait_started(), timeout=10)
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("get_logs", {"pod": "api-0", "namespace": "prod"})
            assert getattr(result.content[0], "text", "").startswith("ERROR: connection refused")
            assert result.is_error is False, "a successful read was reported as a failed call"
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)
    assert executor.calls != []


async def test_a_refusal_before_dispatch_is_flagged_as_an_error() -> None:
    """Refusals never reach a producer, so nothing else can supply the bit.

    Both pre-dispatch exits — a name outside the configured surface and a
    failed capability check — have to mark themselves.
    """
    from mcp import types as mcp_types

    executor = RecordingExecutor()
    server = make_proposal_server(executor)
    for arguments in (
        {"kind": "pods", "name": "x"},
        {**_PROPOSE_ARGS, "capability": "wrong"},
    ):
        name = "delete_resource" if "kind" in arguments else "propose_write"
        result = await server._on_call_tool(
            None,  # type: ignore[arg-type]  # identity is not consulted on a refusal
            mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
        assert result.is_error is True, f"{name} refusal was reported as a successful call"
    assert executor.calls == []


async def test_a_failed_proposal_is_flagged_even_though_its_producer_says_nothing(
    tmp_path: Path,
) -> None:
    """The UI bridges answer with strings, not outcomes.

    `agent_get_write_proposal` returns `ERROR: unknown proposal id` and
    `ToolExecutor` wraps that plain string with the default `error=False`,
    so a capability-valid but failed proposal would reach the host marked
    successful. korvid authored that text, so its `ERROR:` prefix is a
    contract rather than content — unlike a log line, which is why the
    judgement is made per effect and not globally.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    executor = RecordingExecutor()
    executor.result = "ERROR: unknown proposal id"
    executor.error = False  # exactly what the string-returning bridges produce
    endpoint_file = tmp_path / "mcp-endpoint.json"
    server = make_proposal_server(executor, port=0, endpoint_path=endpoint_file)
    task = asyncio.create_task(server.run())
    try:
        port = await asyncio.wait_for(server.wait_started(), timeout=10)
        capability = json.loads(endpoint_file.read_text())["servers"][str(os.getpid())][
            "capability"
        ]
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "get_write_proposal", {"proposal_id": "nope", "capability": capability}
            )
            assert getattr(result.content[0], "text", "") == "ERROR: unknown proposal id"
            assert result.is_error is True, "a failed proposal was reported as successful"
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)
    assert executor.calls != []


# ---------------------------------------------------------------------------
# External reads (issue #193)
# ---------------------------------------------------------------------------


async def test_an_external_read_is_noted_rather_than_mirrored() -> None:
    """There is no resource view for a Prometheus query.

    Mirroring would have to pick some screen; the activity note is what
    makes the query visible in the TUI, which is what the issue asks for.
    """
    executor = RecordingExecutor()
    ui = FakeBridge()
    notes: list[str] = []
    server = KorvidMCPServer(
        executor,
        READ_TOOLS + UI_TOOLS + mcp_tool_schemas(observability_backends=frozenset({"metrics"})),
        port=0,
        ui=ui,
        follow_enabled=lambda: True,
        note_activity=notes.append,
    )
    await server.call_tool("query_metrics", {"signal": "cpu", "namespace": "prod"})
    await _drain_follow(server)
    assert ui.calls == []
    assert len(notes) == 1
    assert "query_metrics" in notes[0]


async def test_an_external_read_result_beginning_with_error_is_not_a_failed_call() -> None:
    """A log line is not korvid's text, so the prefix decides nothing."""
    from korvid.mcp.server import _failed

    outcome = ToolOutcome(text="ERROR: connection refused", error=False)
    assert _failed("search_logs", outcome) is False


async def test_a_failed_external_read_is_reported_as_a_failed_call() -> None:
    from korvid.mcp.server import _failed

    outcome = ToolOutcome(text="ERROR: [network] prom is unreachable", error=True)
    assert _failed("query_metrics", outcome) is True
