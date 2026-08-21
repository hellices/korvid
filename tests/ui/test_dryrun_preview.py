"""Dry-run diff previews in write approval dialogs (issue #19).

The dialog shows what a ``dryRun=All`` replay of the write reports; a failed,
slow, or unsupported preview falls back to the synthesized operation string -
the approval flow itself must never block on a preview.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest import mock

from textual.widgets import Static

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary
from korvid.k8s.writes import WriteOps
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.agent_panel import AgentPanel
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ReplicasPrompt
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_ALIASES = {"deployments": _DEPLOY_META}


class PreviewOps(WriteOps):
    """WriteOps fake with controllable dry-run previews."""

    def __init__(
        self,
        lines: list[str] | None = None,
        *,
        fail: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.lines = lines
        self.fail = fail
        self.delay = delay
        self.calls: list[tuple[object, ...]] = []
        self.preview_calls: list[tuple[object, ...]] = []

    async def _preview(self, call: tuple[object, ...]) -> list[str] | None:
        self.preview_calls.append(call)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ApiStatusError(500, "boom")
        return self.lines

    async def preview_delete(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> list[str] | None:
        return await self._preview(("delete", namespace, name, uid))

    async def preview_scale(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> list[str] | None:
        return await self._preview(("scale", namespace, name, replicas, uid))

    async def preview_rollout_restart(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
    ) -> list[str] | None:
        return await self._preview(("restart", namespace, name, uid, restarted_at))

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.calls.append(("delete", namespace, name))

    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        self.calls.append(("scale", namespace, name, replicas))

    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.calls.append(("restart", namespace, name, None))

    async def rollout_restart_with_stamp(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
    ) -> None:
        self.calls.append(("restart", namespace, name, restarted_at))

    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        self.calls.append(("replace", namespace, name))


def make_app(ops: WriteOps, audit_path: Path) -> KorvidApp:
    store = ResourceStore()
    deploys = [
        GenericSummary(
            name="web", namespace="default", kind="Deployment", created="", desired=3, uid="u-web"
        )
    ]

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in deploys if kind == "deployments" else []:
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        write_ops=ops,
        audit=AuditLog(audit_path),
    )


def _deployments_row_ready(app: KorvidApp) -> bool:
    return app.current_kind == "deployments" and app.query_one(ResourceTable).row_count == 1


async def _to_deployments(app: KorvidApp, pilot) -> None:  # type: ignore[no-untyped-def]  # Pilot's app type isn't exposed by the fixture
    await pilot.press("colon")
    for ch in "deployments":
        await pilot.press(ch)
    await pilot.press("enter")
    await until(
        pilot,
        lambda: _deployments_row_ready(app),
        label="deployments view active with selected row",
    )


def _preview_render(app: KorvidApp) -> str:
    node = app.screen.query_one(".confirm-preview", Static)
    return str(node.render())


async def test_delete_dialog_shows_dry_run_preview(tmp_path: Path) -> None:
    ops = PreviewOps(lines=["- deployments/web (uid u1, created t1)"])
    app = make_app(ops, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _to_deployments(app, pilot)
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="delete confirmation dialog opened",
        )
        assert "- deployments/web (uid u1, created t1)" in _preview_render(app)
        assert ops.preview_calls == [("delete", "default", "web", "u-web")]


async def test_restart_dialog_shows_dry_run_preview(tmp_path: Path) -> None:
    ops = PreviewOps(lines=['+ spec.template.metadata.annotations.restartedAt: "t"'])
    app = make_app(ops, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _to_deployments(app, pilot)
        await pilot.press("r")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="restart confirmation dialog opened",
        )
        assert "restartedAt" in _preview_render(app)
        assert ops.preview_calls[0][:4] == ("restart", "default", "web", "u-web")
        # Exact replay: the stamp shown in the preview is the stamp the
        # approved write executes with - generated once per request.
        stamp = ops.preview_calls[0][4]
        assert stamp
        await pilot.press("y")
        await until(pilot, lambda: bool(ops.calls), label="restart write completed")
        assert ops.calls == [("restart", "default", "web", stamp)]


async def test_scale_dialog_previews_requested_replicas(tmp_path: Path) -> None:
    ops = PreviewOps(lines=["~ spec.replicas: 3 -> 5"])
    app = make_app(ops, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _to_deployments(app, pilot)
        await pilot.press("S")
        await until(
            pilot,
            lambda: isinstance(app.screen, ReplicasPrompt),
            label="replicas prompt opened",
        )
        await pilot.press("5")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="scale confirmation dialog opened",
        )
        assert "~ spec.replicas: 3 -> 5" in _preview_render(app)
        assert ops.preview_calls == [("scale", "default", "web", 5, "u-web")]
        await pilot.press("y")
        await until(pilot, lambda: bool(ops.calls), label="scale write completed")
        assert ops.calls == [("scale", "default", "web", 5)]


async def test_failed_preview_still_opens_dialog(tmp_path: Path) -> None:
    ops = PreviewOps(fail=True)
    app = make_app(ops, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _to_deployments(app, pilot)
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="delete confirmation dialog opened after preview failure",
        )
        assert not app.screen.query(".confirm-preview")
        await pilot.press("y")
        await until(pilot, lambda: bool(ops.calls), label="delete write completed")
        assert ops.calls == [("delete", "default", "web")]


async def test_slow_preview_times_out_and_opens_dialog(tmp_path: Path) -> None:
    ops = PreviewOps(lines=["~ spec.replicas: 3 -> 5"], delay=5.0)
    app = make_app(ops, tmp_path / "audit.jsonl")
    with mock.patch("korvid.ui.write_coordinator._PREVIEW_TIMEOUT", 0.05):
        async with app.run_test() as pilot:
            await _to_deployments(app, pilot)
            await pilot.press("ctrl+d")
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="delete confirmation dialog opened after preview timeout",
            )
            assert not app.screen.query(".confirm-preview")


async def test_no_preview_support_falls_back(tmp_path: Path) -> None:
    """A WriteOps without dry-run support (ABC defaults) shows the dialog
    exactly as before - no preview widget."""

    class Plain(PreviewOps):
        async def preview_delete(
            self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
        ) -> list[str] | None:
            return await WriteOps.preview_delete(self, meta, namespace, name, uid=uid)

    ops = Plain()
    app = make_app(ops, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _to_deployments(app, pilot)
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="delete confirmation dialog opened without preview support",
        )
        assert not app.screen.query(".confirm-preview")


async def test_agent_write_dialog_shows_preview(tmp_path: Path) -> None:
    ops = PreviewOps(lines=["~ spec.replicas: 3 -> 4"])
    app = make_app(ops, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _to_deployments(app, pilot)
        app.query_one(AgentPanel).display = True
        task = asyncio.ensure_future(
            app.agent_request_write("scale", "deployments", "web", namespace="default", replicas=4)
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="agent write confirmation dialog opened",
        )
        assert "~ spec.replicas: 3 -> 4" in _preview_render(app)
        assert ("scale", "default", "web", 4, None) in ops.preview_calls
        await pilot.press("y")
        result = await task
        assert result.startswith("approved and executed")
