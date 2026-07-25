"""TUI write keybindings behind approval dialogs (issue #16, spec §5 #4).

Ctrl-D = delete, r = rollout restart, S = scale. Every path goes through a
ConfirmScreen; --readonly disables all of them; executed writes land in the
audit log.
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
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ReplicasPrompt

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_NODES_META = ResourceMeta("Node", "nodes", "", "v1", False, ("no",))

_ALIASES = {
    "pods": _PODS_META,
    "deployments": _DEPLOY_META,
    "nodes": _NODES_META,
}


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.fail = fail

    async def delete(self, kind: str, namespace: str | None, name: str) -> None:
        if self.fail:
            raise ApiStatusError(403, "Forbidden")
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
    data: dict[str, list[Summary]] = {
        "pods": [
            PodSummary(
                name="web-1",
                namespace="default",
                phase="Running",
                ready="1/1",
                restarts=0,
                node=None,
            )
        ],
        "deployments": [
            GenericSummary(name="web", namespace="default", kind="Deployment", created="")
        ],
        "nodes": [GenericSummary(name="worker-1", namespace="", kind="Node", created="")],
    }

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
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


async def _to_view(pilot, view: str) -> None:  # type: ignore[no-untyped-def]  # Pilot's app type isn't exposed by the fixture
    await pilot.press("colon")
    for ch in view:
        await pilot.press(ch)
    await pilot.press("enter")
    await pilot.pause(0.1)


async def _until(pilot, cond, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]  # deterministic wait: poll an observable condition instead of a fixed sleep
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met within timeout")


async def test_ctrl_d_delete_confirmed_executes_and_audits(tmp_path: Path) -> None:
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("ctrl+d")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _until(pilot, lambda: audit_path.exists() and "success" in audit_path.read_text())
        assert rec.calls == [("delete", "pods", "default", "web-1")]
        lines = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert lines[0]["outcome"] == "intent"  # recorded before the write ran
        entry = lines[-1]
        assert entry["action"] == "delete"
        assert entry["name"] == "web-1"
        assert entry["outcome"] == "success"


async def test_ctrl_d_cancelled_does_nothing(tmp_path: Path) -> None:
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("ctrl+d")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause(0.2)
        assert rec.calls == []
        assert not audit_path.exists()


async def test_readonly_blocks_delete(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl", readonly=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_cluster_scoped_delete_requires_typed_name(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _to_view(pilot, "nodes")
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")  # goes into the input; must NOT confirm by itself
        await pilot.pause(0.2)
        assert rec.calls == []
        await pilot.press("backspace")  # clear the stray 'y'
        for ch in "worker-1":
            await pilot.press(ch)
        await pilot.press("enter")
        await _until(pilot, lambda: rec.calls)
        assert rec.calls == [("delete", "nodes", None, "worker-1")]


async def test_rollout_restart_on_deployment(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _to_view(pilot, "deployments")
        await pilot.press("r")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _until(pilot, lambda: rec.calls)
        assert rec.calls == [("restart", "deployments", "default", "web")]


async def test_rollout_restart_rejected_on_pods(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("r")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_scale_flow_prompts_then_confirms(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _to_view(pilot, "deployments")
        await pilot.press("S")
        await _until(pilot, lambda: isinstance(app.screen, ReplicasPrompt))
        await pilot.press("5")
        await pilot.press("enter")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _until(pilot, lambda: rec.calls)
        assert rec.calls == [("scale", "deployments", "default", "web", 5)]


async def test_failed_write_audits_error(tmp_path: Path) -> None:
    rec = Recorder(fail=True)
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("ctrl+d")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _until(pilot, lambda: audit_path.exists() and "error" in audit_path.read_text())
        entry = json.loads(audit_path.read_text().splitlines()[-1])
        assert entry["outcome"].startswith("error")


async def test_permission_denied_blocks_delete(tmp_path: Path) -> None:
    """A failed SelfSubjectAccessReview pre-check stops the flow before the dialog."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl", permitted=False)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("ctrl+d")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_permission_allowed_proceeds(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl", permitted=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("ctrl+d")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _until(pilot, lambda: rec.calls)
        assert rec.calls == [("delete", "pods", "default", "web-1")]


async def test_unwritable_audit_blocks_write(tmp_path: Path) -> None:
    """Fail-closed auditing: if the intent record cannot be written, the
    cluster write must not run."""
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    audit_path.mkdir()  # a directory at the log path makes appends fail
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("ctrl+d")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await pilot.pause(0.3)  # write path must stay blocked; nothing to wait on
        assert rec.calls == []


async def test_scale_prompt_prefills_current_replicas(tmp_path: Path) -> None:
    from textual.widgets import Input

    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _to_view(pilot, "deployments")
        await pilot.press("S")
        await _until(pilot, lambda: isinstance(app.screen, ReplicasPrompt))
        # GenericSummary carries no desired count, so the field starts empty
        # and the label reports it as unknown instead of a misleading 0.
        assert app.screen.query_one(Input).value == ""
