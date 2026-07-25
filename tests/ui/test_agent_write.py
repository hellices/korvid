"""Agent-initiated writes surface the same ConfirmScreen the user must
approve with a real keystroke (issue #16, spec §6.2): the agent can only
*request*; approval happens in the TUI.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen

_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_ALIASES = {"deployments": _DEPLOY_META, "deploy": _DEPLOY_META}


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def delete(self, kind: str, namespace: str | None, name: str) -> None:
        self.calls.append(("delete", kind, namespace, name))

    async def scale(self, kind: str, namespace: str | None, name: str, replicas: int) -> None:
        self.calls.append(("scale", kind, namespace, name, replicas))

    async def restart(self, kind: str, namespace: str | None, name: str) -> None:
        self.calls.append(("restart", kind, namespace, name))


def make_app(
    recorder: Recorder,
    audit_path: Path,
    *,
    readonly: bool = False,
    permitted: bool | None = None,
) -> KorvidApp:
    store = ResourceStore()
    deploys = [GenericSummary(name="web", namespace="default", kind="Deployment", created="")]

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in deploys if kind == "deployments" else []:
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def check_permission(verb: str, resource: str, sub: str, ns: str | None) -> bool:
        assert permitted is not None
        return permitted

    return KorvidApp(
        config=KorvidConfig(namespace="default", readonly=readonly),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        delete_object=recorder.delete,
        scale_object=recorder.scale,
        rollout_restart=recorder.restart,
        audit=AuditLog(audit_path),
        check_permission=None if permitted is None else check_permission,
    )


async def test_agent_delete_approved_by_user_key(tmp_path: Path) -> None:
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []  # nothing executes before the user's keystroke
        await pilot.press("y")
        await pilot.pause(0.2)
        result = await task
        assert "delete" in result.lower()
        assert rec.calls == [("delete", "deployments", "default", "web")]
        lines = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert lines[0]["outcome"] == "intent"
        entry = lines[-1]
        assert entry["outcome"] == "success"
        assert "agent" in entry["detail"]


async def test_agent_delete_denied_by_user_key(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await pilot.pause(0.2)
        await pilot.press("n")
        await pilot.pause(0.2)
        result = await task
        assert "denied" in result.lower() or "declined" in result.lower()
        assert rec.calls == []


async def test_agent_write_blocked_in_readonly(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl", readonly=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        result = await app.agent_request_write("delete", "deployments", "web", namespace="default")
        assert result.startswith("ERROR:")
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_agent_scale_requires_replicas(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        result = await app.agent_request_write("scale", "deployments", "web", namespace="default")
        assert result.startswith("ERROR:")
        assert rec.calls == []


async def test_agent_scale_approved(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        task = asyncio.ensure_future(
            app.agent_request_write("scale", "deployments", "web", namespace="default", replicas=4)
        )
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await pilot.pause(0.2)
        await task
        assert rec.calls == [("scale", "deployments", "default", "web", 4)]


async def test_agent_restart_rejected_for_wrong_kind(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        result = await app.agent_request_write("rollout_restart", "pods", "web-1")
        assert result.startswith("ERROR:")
        assert rec.calls == []


async def test_agent_unknown_kind_is_error(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        result = await app.agent_request_write("delete", "frobnicators", "x", namespace="default")
        assert result.startswith("ERROR:")
        assert rec.calls == []


async def test_agent_write_blocked_without_permission(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl", permitted=False)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        result = await app.agent_request_write("delete", "deployments", "web", namespace="default")
        assert result.startswith("ERROR:")
        assert "permission" in result.lower()
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []
