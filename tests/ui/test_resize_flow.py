"""R keybinding: in-place pod resize behind the approval gate (issue #27).

Only offered on the pods view and only when the cluster exposes the
pods/resize subresource (1.35 GA); the SSAR pre-check asks for patch on
pods/resize; the executed write lands in the audit log.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from textual.widgets import Input

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.writes import WriteOps
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.resize_prompt import ResizePrompt

from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_ALIASES = {"pods": _PODS_META, "deployments": _DEPLOY_META}

_POD_MANIFEST = {
    "metadata": {"name": "web-1", "namespace": "default", "uid": "pod-uid-1"},
    "spec": {
        "containers": [
            {"name": "app", "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}}
        ]
    },
}


class ResizeRecorder(WriteOps):
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.uids: list[str | None] = []

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        raise AssertionError("unexpected delete")

    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        raise AssertionError("unexpected scale")

    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        raise AssertionError("unexpected restart")

    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        raise AssertionError("unexpected replace")

    async def resize_pod(
        self,
        namespace: str,
        name: str,
        resources: dict[str, dict[str, dict[str, str]]],
        *,
        uid: str | None = None,
    ) -> None:
        self.uids.append(uid)
        self.calls.append(("resize", namespace, name, resources))

    async def preview_resize(
        self,
        namespace: str,
        name: str,
        resources: dict[str, dict[str, dict[str, str]]],
        *,
        uid: str | None = None,
    ) -> list[str] | None:
        return ['~ spec.containers: "100m" -> "200m"']


def make_app(
    recorder: ResizeRecorder,
    audit_path: Path,
    *,
    resize_supported: bool = True,
    readonly: bool = False,
    permitted: bool | None = None,
    check_calls: list[tuple[str, str, str, str | None, str, str]] | None = None,
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
                uid="pod-uid-1",
            )
        ],
        "deployments": [
            GenericSummary(
                name="web",
                namespace="default",
                kind="Deployment",
                created="",
                desired=3,
                uid="deploy-uid-1",
            )
        ],
    }

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return _POD_MANIFEST

    async def check_permission(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        assert permitted is not None
        if check_calls is not None:
            check_calls.append((verb, resource, sub, ns, group, name))
        return permitted

    return KorvidApp(
        config=KorvidConfig(namespace="default", readonly=readonly),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        get_manifest=get_manifest,
        write_ops=recorder,
        audit=AuditLog(audit_path),
        check_permission=None if permitted is None else check_permission,
        pod_resize_supported=resize_supported,
    )


async def _to_view(pilot, view: str) -> None:  # type: ignore[no-untyped-def]  # Pilot's app type isn't exposed by the fixture
    await pilot.press("colon")
    for ch in view:
        await pilot.press(ch)
    await pilot.press("enter")
    await pilot.pause(0.1)


async def test_resize_confirmed_executes_and_audits(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("R")
        await until(pilot, lambda: isinstance(app.screen, ResizePrompt))
        field = app.screen.query_one("#resize-0-requests-cpu", Input)
        assert field.value == "100m"  # prefilled from the live manifest
        field.value = "200m"
        field.focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await until(pilot, lambda: audit_path.exists() and "success" in audit_path.read_text())
        assert rec.calls == [("resize", "default", "web-1", {"app": {"requests": {"cpu": "200m"}}})]
        assert rec.uids == ["pod-uid-1"]
        lines = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert lines[0]["outcome"] == "intent"
        assert lines[-1]["action"] == "resize"
        assert lines[-1]["outcome"] == "success"


async def test_resize_confirm_shows_preview(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("R")
        await until(pilot, lambda: isinstance(app.screen, ResizePrompt))
        field = app.screen.query_one("#resize-0-requests-cpu", Input)
        field.value = "200m"
        field.focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert "200m" in str(app.screen.query_one(".confirm-preview").render())


async def test_resize_gated_when_cluster_lacks_subresource(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", resize_supported=False)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("R")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ResizePrompt)
        assert rec.calls == []


async def test_resize_only_applies_to_pods(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _to_view(pilot, "deployments")
        await pilot.press("R")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ResizePrompt)
        assert rec.calls == []


async def test_readonly_blocks_resize(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", readonly=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("R")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ResizePrompt)
        assert rec.calls == []


async def test_resize_precheck_asks_patch_on_pods_resize(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    calls: list[tuple[str, str, str, str | None, str, str]] = []
    app = make_app(rec, tmp_path / "audit.jsonl", permitted=False, check_calls=calls)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("R")
        await pilot.pause(0.2)
        assert calls == [("patch", "pods", "resize", "default", "", "web-1")]
        assert not isinstance(app.screen, ResizePrompt)


async def test_resize_prompt_cancel_does_nothing(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("R")
        await until(pilot, lambda: isinstance(app.screen, ResizePrompt))
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert rec.calls == []
        assert not audit_path.exists()


# -- Agent-requested resize (same approval gate) ------------------------------


def _expand_panel(app: KorvidApp) -> None:
    from korvid.ui.widgets.agent_panel import AgentPanel

    # Approval dialogs only surface while the panel is expanded (spec 6.1).
    app.query_one(AgentPanel).display = True


async def test_agent_resize_approved_by_user_key(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    resources = {"app": {"requests": {"cpu": "200m"}}}
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write(
                "resize", "pods", "web-1", namespace="default", resources=resources
            )
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert rec.calls == []  # nothing executes before the user's keystroke
        await pilot.press("y")
        result = await task
        assert "approved and executed" in result
        assert rec.calls == [("resize", "default", "web-1", resources)]
        assert rec.uids == ["pod-uid-1"]
        lines = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert lines[-1]["action"] == "resize"
        assert lines[-1]["outcome"] == "success"
        assert "agent" in lines[-1]["detail"]


async def test_agent_resize_requires_resources(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        result = await app.agent_request_write("resize", "pods", "web-1", namespace="default")
        assert result.startswith("ERROR:")
        assert rec.calls == []


async def test_agent_resize_rejected_for_non_pod_kind(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        result = await app.agent_request_write(
            "resize",
            "deployments",
            "web",
            namespace="default",
            resources={"app": {"requests": {"cpu": "1"}}},
        )
        assert result.startswith("ERROR:")
        assert "does not apply" in result
        assert rec.calls == []


async def test_agent_resize_rejected_when_cluster_lacks_subresource(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", resize_supported=False)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        result = await app.agent_request_write(
            "resize",
            "pods",
            "web-1",
            namespace="default",
            resources={"app": {"requests": {"cpu": "1"}}},
        )
        assert result.startswith("ERROR:")
        assert "1.35" in result
        assert rec.calls == []
