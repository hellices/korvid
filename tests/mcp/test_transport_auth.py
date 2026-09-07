"""Authentication applies before MCP protocol parsing, not just proposal dispatch."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from korvid.mcp.server import KorvidMCPServer, _replace_atomically
from korvid.tools.registry import mcp_tool_schemas

from .test_server import RecordingExecutor

_TOKEN = "private-transport-capability-012345"
_HEADERS = {"Accept": "application/json, text/event-stream"}


@asynccontextmanager
async def running_server(
    path: Path, *, token: str | None = _TOKEN
) -> AsyncIterator[tuple[KorvidMCPServer, RecordingExecutor, str]]:
    executor = RecordingExecutor()
    server = KorvidMCPServer(
        executor,
        mcp_tool_schemas(write_proposals=True),
        port=0,
        endpoint_path=path,
        capability_token=token,
    )
    task = asyncio.create_task(server.run())
    try:
        port = await asyncio.wait_for(server.wait_started(), timeout=10)
        yield server, executor, f"http://127.0.0.1:{port}/mcp/"
    finally:
        server.request_shutdown()
        await asyncio.wait_for(task, timeout=10)


@pytest.mark.parametrize("tool", ["list_resources", "navigate", "propose_write"])
@pytest.mark.parametrize("credential", [None, "Bearer wrong-token", "Basic wrong-token"])
async def test_all_tools_require_transport_authentication(
    tmp_path: Path, tool: str, credential: str | None
) -> None:
    async with running_server(tmp_path / "endpoint.json") as (_server, executor, url):
        headers = dict(_HEADERS)
        if credential is not None:
            headers["Authorization"] = credential
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": {"capability": _TOKEN}},
                },
            )
        assert response.status_code == 401
        assert response.text == "Unauthorized"
        assert _TOKEN not in response.text
        assert executor.calls == []


async def test_invalid_credentials_are_rejected_before_parsing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with running_server(tmp_path / "endpoint.json") as (_server, executor, url):
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                url,
                content=b"not-json",
                headers={**_HEADERS, "Authorization": "Bearer invalid"},
            )
        assert response.status_code == 401
        assert response.text == "Unauthorized"
        assert executor.calls == []
        assert _TOKEN not in caplog.text


async def test_authenticated_proposal_needs_no_credential_in_tool_arguments(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with running_server(tmp_path / "endpoint.json") as (_server, executor, url):
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {_TOKEN}"}, trust_env=False
            ) as http,
            streamable_http_client(url, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "propose_write",
                {
                    "action": "scale",
                    "kind": "deployments",
                    "name": "app",
                    "namespace": "ns",
                    "replicas": 2,
                },
            )
        assert result.is_error is False
        assert executor.calls[0][0] == "propose_write"
        assert "capability" not in executor.calls[0][1]
        assert _TOKEN not in result.model_dump_json()
        assert _TOKEN not in caplog.text


@pytest.mark.parametrize("method", ["GET", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("trailing_slash", [False, True])
async def test_non_post_requests_also_require_auth(
    tmp_path: Path, method: str, trailing_slash: bool
) -> None:
    async with running_server(tmp_path / "endpoint.json") as (_server, _executor, url):
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.request(
                method, url if trailing_slash else url.rstrip("/"), headers=_HEADERS
            )
        assert response.status_code == 401
        assert response.text == "Unauthorized"


async def test_capability_rotates_when_server_restarts(tmp_path: Path) -> None:
    path = tmp_path / "endpoint.json"
    async with running_server(path, token=None):
        first = json.loads(path.read_text())["servers"][str(os.getpid())]["capability"]
        assert isinstance(first, str)
        assert len(first) >= 32
    async with running_server(path, token=None) as (_server, _executor, url):
        second = json.loads(path.read_text())["servers"][str(os.getpid())]["capability"]
        assert first != second
        async with httpx.AsyncClient(trust_env=False) as client:
            old = await client.get(url, headers={"Authorization": f"Bearer {first}"})
            current = await client.get(url, headers={"Authorization": f"Bearer {second}"})
        assert old.status_code == 401
        assert current.status_code == 405


async def test_registry_failure_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    server = KorvidMCPServer(
        RecordingExecutor(),
        mcp_tool_schemas(),
        port=0,
        endpoint_path=tmp_path / "endpoint.json",
    )

    def fail_publication(port: int) -> None:
        raise PermissionError("sensitive-registry-path")

    monkeypatch.setattr(server, "_write_endpoint", fail_publication)
    task = asyncio.create_task(server.run())
    try:
        with pytest.raises(RuntimeError, match="registry"):
            await asyncio.wait_for(server.wait_started(), timeout=10)
        await asyncio.wait_for(task, timeout=10)
        assert server.bound_port is None
        assert "sensitive-registry-path" not in caplog.text
    finally:
        server.request_shutdown()
        if not task.done():
            await asyncio.wait_for(task, timeout=10)


def test_failed_registry_rename_removes_private_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_rename(self: Path, target: Path) -> Path:
        raise PermissionError("rename refused")

    monkeypatch.setattr(Path, "replace", fail_rename)
    with pytest.raises(PermissionError, match="rename refused"):
        _replace_atomically(tmp_path / "endpoint.json", {"servers": {"1": {"capability": _TOKEN}}})
    assert list(tmp_path.iterdir()) == []
