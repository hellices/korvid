"""SDK stdio -> authenticated HTTP -> the existing TUI's real write perimeter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, stdio_client, types
from textual.pilot import Pilot

from korvid.mcp.registry import read_endpoints
from korvid.mcp.server import KorvidMCPServer, MCPController
from korvid.tools.executor import ToolExecutor
from korvid.tools.proposals import ProposalState, ProposalStore
from korvid.tools.registry import mcp_tool_schemas
from korvid.ui.app import AppUIBridge, KorvidApp
from korvid.ui.context_switch_coordinator import ContextSwitchResult
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.describe_screen import DescribeScreen
from tests.mcp.test_stdio import _parameters
from tests.tools.executor_fakes import FakeKube
from tests.ui.test_proposals_ui import Recorder, make_app
from tests.ui.test_write_coordinator import BrokenAudit
from tests.ui.waits import until

_TARGET = {"kind": "deployments", "name": "web", "namespace": "default"}
_PROPOSAL = {"action": "delete", **_TARGET}


@dataclass
class _Running:
    app: KorvidApp
    pilot: Pilot[None]
    session: ClientSession
    store: ProposalStore
    writes: Recorder
    kube: FakeKube
    audit_path: Path
    controller: MCPController
    servers: list[KorvidMCPServer]

    async def call(self, name: str, arguments: dict[str, Any], *, error: bool = False) -> str:
        result = await self.session.call_tool(name, arguments)
        assert isinstance(result, types.CallToolResult)
        assert result.is_error is error, result
        assert len(result.content) == 1
        content = result.content[0]
        assert isinstance(content, types.TextContent)
        return content.text

    def state(self, proposal_id: str) -> ProposalState | None:
        found = self.store.get(proposal_id)
        return found[1] if found is not None else None

    async def review(self) -> None:
        await self.pilot.press(":", *"proposals", "enter")
        await until(
            self.pilot,
            lambda: isinstance(self.app.screen, ConfirmScreen),
            label="user opened proposal review",
        )


@asynccontextmanager
async def _running(
    state: Path,
    *,
    write_proposals: bool = True,
    readonly: bool = False,
    managed_mcp: bool = False,
) -> AsyncIterator[_Running]:
    writes = Recorder()
    store = ProposalStore()
    audit_path = state / "audit.jsonl"
    servers: list[KorvidMCPServer] = []
    port = 0

    def make_server() -> KorvidMCPServer:
        # Rebind the same endpoint so a stale session is rejected by its old
        # capability, not merely by losing contact with a retired port.
        server = KorvidMCPServer(
            executor,
            mcp_tool_schemas(write_proposals=write_proposals),
            port=port,
            endpoint_path=state / "korvid" / "mcp-endpoint.json",
            ui=bridge,
        )
        servers.append(server)
        return server

    controller = MCPController(make_server)
    app = make_app(
        writes,
        audit_path,
        store if write_proposals else None,
        readonly=readonly,
        mcp=controller if managed_mcp else None,
    )
    kube = FakeKube()
    kube.manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "default", "uid": "uid-1"},
        "spec": {"replicas": 1},
    }

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return await kube.get_object(app.aliases[kind], namespace, name)

    app._get_manifest = get_manifest
    bridge = AppUIBridge(app)
    executor = ToolExecutor(
        kube,  # type: ignore[arg-type]  # get_object-only fake; no real cluster client
        app.aliases,
        ui=bridge,
        proposal_tools=write_proposals,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        try:
            assert (await controller.start()).startswith("MCP on :")
            bound_port = servers[0].bound_port
            assert bound_port is not None
            port = bound_port
            with (state / "stdio-stderr.log").open("w+") as errors:
                async with (
                    stdio_client(_parameters(state), errlog=errors) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    yield _Running(
                        app, pilot, session, store, writes, kube, audit_path, controller, servers
                    )
        finally:
            pending = await controller.shutdown()
            if pending is not None:
                await asyncio.wait_for(pending, timeout=10)


async def _queue(env: _Running) -> str:
    screen, focus = env.app.screen, env.app.focused
    reply = await env.call("propose_write", _PROPOSAL)
    pending = env.store.pending()
    assert len(pending) == 1
    proposal = pending[0]
    assert proposal.id in reply
    assert proposal.uid == "uid-1"
    assert proposal.context == "ctx-a"
    assert proposal.session_id.startswith("mcp-")
    assert env.app.screen is screen
    assert env.app.focused is focus
    assert env.writes.calls == []
    status = await env.call("get_write_proposal", {"proposal_id": proposal.id})
    assert f"proposal {proposal.id}: pending" in status
    await until(
        env.pilot,
        lambda: "proposal" in str(env.app._status_bar.render()),
        label="pending proposal indicator",
    )
    assert env.state(proposal.id) == "pending"
    assert env.writes.calls == []
    return proposal.id


async def test_stdio_reads_navigates_and_describes_the_existing_app(tmp_path: Path) -> None:
    async with _running(tmp_path) as env:
        assert env.app.current_kind != "deployments"
        assert "switched" in await env.call("navigate", {"view": "deployments"})
        await until(
            env.pilot,
            lambda: bool(env.app.store.get("deployments", env.app.current_scope)),
            label="existing app loaded deployment rows",
        )
        assert env.app.current_kind == "deployments"
        assert env.app.store.get("deployments", env.app.current_scope)[0].name == "web"
        manifest = await env.call("get_resource", _TARGET)
        assert "uid-1" in manifest
        assert "Deployment" in manifest
        assert "describe screen opened" in await env.call("open_describe", _TARGET)
        await until(
            env.pilot,
            lambda: isinstance(env.app.screen, DescribeScreen),
            label="stdio opened describe in the existing app",
        )
        screen = env.app.screen
        assert isinstance(screen, DescribeScreen)
        assert screen.app is env.app
        assert screen._manifest == env.kube.manifest
        await env.pilot.press("escape")
        await until(
            env.pilot,
            lambda: not isinstance(env.app.screen, DescribeScreen),
            label="describe closed",
        )
        assert env.writes.calls == []


@pytest.mark.parametrize(
    ("write_proposals", "readonly", "reason"),
    [
        (False, False, "not available over MCP"),
        (True, True, "read-only mode"),
    ],
    ids=["proposals-not-opted-in", "read-only-app"],
)
async def test_stdio_proposals_require_opt_in_and_a_writable_app(
    tmp_path: Path, write_proposals: bool, readonly: bool, reason: str
) -> None:
    async with _running(tmp_path, write_proposals=write_proposals, readonly=readonly) as env:
        offered = {tool.name for tool in (await env.session.list_tools()).tools}
        assert ("propose_write" in offered) is write_proposals
        assert "delete_resource" not in offered
        assert reason in await env.call("propose_write", _PROPOSAL, error=True)
        assert "not available over MCP" in await env.call("delete_resource", _TARGET, error=True)
        assert env.store.pending() == []
        assert not isinstance(env.app.screen, ConfirmScreen)
        assert env.writes.calls == []


async def test_stdio_queued_proposal_waits_for_user_and_denial_never_writes(
    tmp_path: Path,
) -> None:
    async with _running(tmp_path) as env:
        proposal_id = await _queue(env)
        assert not isinstance(env.app.screen, ConfirmScreen)
        await env.review()
        assert env.state(proposal_id) == "pending"
        assert env.writes.calls == []
        assert "approval dialog is open" in await env.call(
            "navigate", {"view": "deployments"}, error=True
        )
        assert isinstance(env.app.screen, ConfirmScreen)
        assert env.state(proposal_id) == "pending"
        assert env.writes.calls == []
        await env.pilot.press("n")
        await until(
            env.pilot, lambda: env.state(proposal_id) == "denied", label="user denied proposal"
        )
        status = await env.call("get_write_proposal", {"proposal_id": proposal_id})
        assert f"proposal {proposal_id}: denied" in status
        assert "denied by user" in status
        assert env.writes.calls == []
    entries = [json.loads(line) for line in env.audit_path.read_text().splitlines()]
    assert any(
        entry["outcome"] == "proposal denied: denied by user" and proposal_id in entry["detail"]
        for entry in entries
    )
    assert not any(entry["outcome"] == "intent" for entry in entries)


async def test_stdio_proposal_executes_only_after_a_real_approval_keystroke(
    tmp_path: Path,
) -> None:
    async with _running(tmp_path) as env:
        proposal_id = await _queue(env)
        await env.review()
        assert "approval dialog is open" in await env.call("open_describe", _TARGET, error=True)
        assert env.state(proposal_id) == "pending"
        assert env.writes.calls == []
        await env.pilot.press("y")
        await until(
            env.pilot,
            lambda: env.state(proposal_id) == "executed",
            label="approved proposal executed against fake WriteOps",
        )
        status = await env.call("get_write_proposal", {"proposal_id": proposal_id})
        assert f"proposal {proposal_id}: executed" in status
        assert env.writes.calls == [("delete", "deployments", "default", "web")]
        assert env.writes.uids == ["uid-1"]
    entries = [json.loads(line) for line in env.audit_path.read_text().splitlines()]
    assert [entry["outcome"] for entry in entries] == ["intent", "success"]
    assert all(
        proposal_id in entry["detail"] and "source=external_mcp" in entry["detail"]
        for entry in entries
    )


async def test_stdio_proposal_expires_when_the_app_changes_context(tmp_path: Path) -> None:
    async with _running(tmp_path) as env:
        proposal_id = await _queue(env)

        async def probe(name: str | None) -> None:
            return None

        async def switch(name: str | None) -> ContextSwitchResult:
            return ContextSwitchResult(False, None, "default")

        env.app._ctx._probe_context = probe
        env.app._ctx._switch_context = switch
        await env.pilot.press(":", *"ctx ctx-b", "enter")
        await until(
            env.pilot,
            lambda: env.app.config.kube_context == "ctx-b" and not env.app._ctx.switching(),
            label="fake cluster context switch completed",
        )
        assert env.state(proposal_id) == "expired"
        status = await env.call("get_write_proposal", {"proposal_id": proposal_id})
        assert "expired" in status
        assert "context switched" in status
        await env.pilot.press(":", *"proposals", "enter")
        assert not isinstance(env.app.screen, ConfirmScreen)
        assert env.writes.calls == []


async def test_managed_context_switch_rotates_capability_and_requires_fresh_stdio(
    tmp_path: Path,
) -> None:
    endpoint_path = tmp_path / "korvid" / "mcp-endpoint.json"
    async with _running(tmp_path, managed_mcp=True) as env:
        assert env.app._mcp is env.controller
        assert len(env.servers) == 1
        old_run = env.controller.pending_task()
        assert old_run is not None
        (original,) = read_endpoints(endpoint_path)
        proposal_id = await _queue(env)
        assert "uid-1" in await env.call("get_resource", _TARGET)

        async def probe(name: str | None) -> None:
            return None

        async def switch(name: str | None) -> ContextSwitchResult:
            assert old_run.done()
            assert not env.controller.running
            assert env.state(proposal_id) == "expired"
            env.kube.manifest = {
                **env.kube.manifest,
                "metadata": {**env.kube.manifest["metadata"], "uid": "uid-ctx-b"},
            }
            return ContextSwitchResult(False, None, "default")

        env.app._ctx._probe_context = probe
        env.app._ctx._switch_context = switch
        await env.pilot.press(":", *"ctx ctx-b", "enter")
        await until(
            env.pilot,
            lambda: (
                env.app.config.kube_context == "ctx-b"
                and not env.app._ctx.switching()
                and env.controller.running
                and len(env.servers) == 2
            ),
            label="managed MCP restarted after context switch",
        )
        (current,) = read_endpoints(endpoint_path)
        assert current.url == original.url
        capability_rotated = current.capability != original.capability
        assert capability_rotated, "context switching must rotate the MCP capability"
        assert old_run.done()
        assert env.controller.pending_task() is not old_run
        assert env.servers[0] is not env.servers[1]
        assert env.state(proposal_id) == "expired"

        refusal = await env.call("get_resource", _TARGET, error=True)
        assert "Cannot communicate with the selected Korvid TUI" in refusal
        assert "restart this stdio connection" in refusal
        assert "restart this stdio connection" in await env.call(
            "propose_write", _PROPOSAL, error=True
        )
        assert env.store.pending() == []
        assert env.writes.calls == []

        with (tmp_path / "reconnected-stdio-stderr.log").open("w+") as errors:
            async with (
                stdio_client(_parameters(tmp_path), errlog=errors) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                reconnected = replace(env, session=session)
                manifest = await reconnected.call("get_resource", _TARGET)
                assert "uid-ctx-b" in manifest
                assert "uid-1" not in manifest
                status = await reconnected.call("get_write_proposal", {"proposal_id": proposal_id})
                assert f"proposal {proposal_id}: expired" in status
                assert "context switched" in status
                await env.pilot.press(":", *"proposals", "enter")
                assert not isinstance(env.app.screen, ConfirmScreen)
                assert env.writes.calls == []
                assert env.writes.uids == []
    assert env.controller.pending_task() is None
    assert not env.controller.running


async def test_stdio_approval_refuses_a_replaced_target_uid(tmp_path: Path) -> None:
    async with _running(tmp_path) as env:
        proposal_id = await _queue(env)
        await env.review()
        env.kube.manifest["metadata"]["uid"] = "uid-2"
        await env.pilot.press("y")
        await until(
            env.pilot,
            lambda: env.state(proposal_id) == "failed",
            label="replacement rejected after approval",
        )
        status = await env.call("get_write_proposal", {"proposal_id": proposal_id})
        assert f"proposal {proposal_id}: failed" in status
        assert "replaced" in status
        assert env.writes.calls == []
        assert env.writes.uids == []


async def test_stdio_approval_fails_closed_when_intent_audit_cannot_persist(
    tmp_path: Path,
) -> None:
    async with _running(tmp_path) as env:
        proposal_id = await _queue(env)
        await env.review()
        env.app._audit = BrokenAudit(env.audit_path)
        await env.pilot.press("y")
        await until(
            env.pilot,
            lambda: env.state(proposal_id) == "failed",
            label="failed audit intent blocked mutation",
        )
        status = await env.call("get_write_proposal", {"proposal_id": proposal_id})
        assert f"proposal {proposal_id}: failed" in status
        assert "blocked: audit log unavailable" in status
        assert env.writes.calls == []
        assert env.writes.uids == []
