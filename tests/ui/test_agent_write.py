"""Agent-initiated writes surface the same ConfirmScreen the user must
approve with a real keystroke (issue #16, spec §6.2): the agent can only
*request*; approval happens in the TUI.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.agent_panel import AgentPanel
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.pick_screen import PickScreen

_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_ALIASES = {"deployments": _DEPLOY_META, "deploy": _DEPLOY_META}


async def _until(pilot, cond, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]  # deterministic wait: poll an observable condition instead of a fixed sleep
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met within timeout")


def _expand_panel(app: KorvidApp) -> None:
    # Approval dialogs only surface while the panel is expanded (spec 6.1);
    # tests that reach the dialog must open the panel first.
    app.query_one(AgentPanel).display = True


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

    async def check_permission(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
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
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert rec.calls == []  # nothing executes before the user's keystroke
        await pilot.press("y")
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
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
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
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("scale", "deployments", "web", namespace="default", replicas=4)
        )
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
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


async def test_agent_write_times_out_as_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unanswered approval dialog resolves as a denial (never hangs the
    agent turn), executes nothing, audits nothing, and clears the dialog."""
    monkeypatch.setattr("korvid.ui.app._APPROVAL_TIMEOUT", 0.2)
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        result = await task  # no keystroke; the timeout resolves it as a denial
        assert "denied" in result.lower()
        assert rec.calls == []
        assert not isinstance(app.screen, ConfirmScreen)
        assert not audit_path.exists()


async def test_agent_dialog_shows_namespace(tmp_path: Path) -> None:
    """The approval dialog must identify the namespace: the agent may target
    any namespace, and default/web vs prod/web must be distinguishable."""
    from textual.widgets import Static

    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("scale", "deployments", "web", namespace="prod", replicas=2)
        )
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        texts = " ".join(str(s.render()) for s in app.screen.query(Static))
        assert "in namespace prod" in texts
        await pilot.press("n")
        await task
        assert rec.calls == []


async def test_agent_write_pending_while_panel_collapsed(tmp_path: Path) -> None:
    """Spec 6.1: approval dialogs never auto-open from the collapsed state.
    The request stays pending and the dialog surfaces only when the user
    expands the panel; normal keystrokes cannot land in a surprise modal."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await pilot.pause(0.3)  # issued while collapsed: no modal appears
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []
        _expand_panel(app)
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        result = await task
        assert "executed" in result.lower()
        assert rec.calls == [("delete", "deployments", "default", "web")]


async def test_agent_write_collapsed_panel_times_out_as_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that is never surfaced (the panel stays collapsed) resolves
    as a denial after the approval window without ever pushing a modal."""
    monkeypatch.setattr("korvid.ui.app._APPROVAL_TIMEOUT", 0.3)
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        result = await app.agent_request_write("delete", "deployments", "web", namespace="default")
        assert "denied" in result.lower()
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_agent_write_waits_for_user_modal_to_close(tmp_path: Path) -> None:
    """An approval never stacks on top of another open dialog: the user's
    next keystroke must not land in a surprise cluster-write confirmation."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        await app.push_screen(PickScreen("pick a thing", ["a", "b"]))
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await pilot.pause(0.3)  # a user dialog is open: the approval waits
        assert isinstance(app.screen, PickScreen)
        app.pop_screen()
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        result = await task
        assert "executed" in result.lower()
        assert rec.calls == [("delete", "deployments", "default", "web")]


async def test_agent_write_rejects_empty_name(tmp_path: Path) -> None:
    """JSON Schema 'required' accepts empty strings; an empty name would
    target a collection path instead of one exact object."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        for bad in ("", "   "):
            result = await app.agent_request_write(
                "delete", "deployments", bad, namespace="default"
            )
            assert result.startswith("ERROR:")
            assert "name" in result
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []
