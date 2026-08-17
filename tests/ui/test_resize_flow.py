"""R keybinding: in-place pod resize behind the approval gate (issue #27).

Only offered on the pods view and only when the cluster exposes the
pods/resize subresource (1.35 GA); the SSAR pre-check asks for patch on
pods/resize; the executed write lands in the audit log.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from textual.css.query import NoMatches
from textual.widgets import Input, Static
from textual.worker import Worker, WorkerState

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
from korvid.ui.widgets.resource_table import ResourceTable

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
    get_manifest: object = None,
    relationship_calls: list[tuple[str, str | None]] | None = None,
    relationship_lister: (
        Callable[[ResourceMeta, str | None], Awaitable[list[GenericSummary]]] | None
    ) = None,
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

    async def default_get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return _POD_MANIFEST

    async def check_permission(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        assert permitted is not None
        if check_calls is not None:
            check_calls.append((verb, resource, sub, ns, group, name))
        return permitted

    async def list_relationship_objects(
        meta: ResourceMeta, namespace: str | None
    ) -> list[GenericSummary]:
        assert relationship_calls is not None
        relationship_calls.append((meta.plural, namespace))
        if meta.plural == "pods":
            return [
                GenericSummary(
                    name="web-1",
                    namespace="default",
                    kind="Pod",
                    created="",
                    uid="pod-uid-1",
                )
            ]
        return []

    return KorvidApp(
        config=KorvidConfig(namespace="default", readonly=readonly),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        get_manifest=get_manifest or default_get_manifest,  # type: ignore[arg-type]  # test seam
        write_ops=recorder,
        audit=AuditLog(audit_path),
        check_permission=None if permitted is None else check_permission,
        pod_resize_supported=resize_supported,
        list_relationship_objects=(
            relationship_lister
            if relationship_lister is not None
            else list_relationship_objects
            if relationship_calls is not None
            else None
        ),
    )


async def _to_view(pilot, view: str) -> None:  # type: ignore[no-untyped-def]  # Pilot's app type isn't exposed by the fixture
    await pilot.press("colon")
    for ch in view:
        await pilot.press(ch)
    await pilot.press("enter")

    def _selected_name() -> str | None:
        try:
            table = pilot.app.query_one(ResourceTable)
        except NoMatches:
            return None
        if table.row_count == 0:
            return None
        return str(table.get_row_at(table.cursor_row)[0])

    expected = {"pods": "web-1", "deployments": "web"}[view]
    await until(
        pilot,
        lambda: pilot.app.current_kind == view and _selected_name() == expected,
        label=f"{view} view selected",
    )


def _row_count(app: KorvidApp) -> int:
    try:
        return app.query_one(ResourceTable).row_count
    except NoMatches:
        return -1


async def _open_resize_confirmation(app: KorvidApp, pilot: object, *, value: str = "200m") -> None:
    await pilot.press("R")  # type: ignore[attr-defined]  # Pilot's app type isn't exposed
    await until(pilot, lambda: isinstance(app.screen, ResizePrompt), label="resize prompt opened")
    field = app.screen.query_one("#resize-0-requests-cpu", Input)
    field.value = value
    field.focus()
    await pilot.press("enter")  # type: ignore[attr-defined]  # Pilot's app type isn't exposed
    await until(
        pilot,
        lambda: isinstance(app.screen, ConfirmScreen),
        label="resize confirmation opened",
    )


class BlockingRelationshipLister:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[str, str | None]] = []
        self.app: KorvidApp | None = None
        self.reservations_during_load: list[int] = []

    async def __call__(self, meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        self.calls.append((meta.plural, namespace))
        if self.app is not None:
            self.reservations_during_load.append(self.app._active_cluster_writes)
        self.entered.set()
        await self.release.wait()
        if meta.plural == "pods":
            return [
                GenericSummary(
                    name="web-1",
                    namespace="default",
                    kind="Pod",
                    created="",
                    uid="pod-uid-1",
                )
            ]
        return []


async def _start_resize_confirmation_worker(app: KorvidApp, pilot: object) -> Worker[Any]:
    started: list[Worker[Any]] = []
    original_run_worker = app.run_worker

    def record_worker(work: Any, *args: Any, **kwargs: Any) -> Worker[Any]:
        worker: Worker[Any] = original_run_worker(work, *args, **kwargs)
        started.append(worker)
        return worker

    with mock.patch.object(app, "run_worker", side_effect=record_worker):
        await pilot.press("R")  # type: ignore[attr-defined]  # Pilot's app type isn't exposed
        await until(
            pilot, lambda: isinstance(app.screen, ResizePrompt), label="resize prompt opened"
        )
        field = app.screen.query_one("#resize-0-requests-cpu", Input)
        field.value = "200m"
        field.focus()
        await pilot.press("enter")  # type: ignore[attr-defined]  # Pilot's app type isn't exposed
        await until(pilot, lambda: len(started) == 1, label="resize worker started")
    return started[0]


async def test_resize_confirm_shows_empty_graph_and_pod_local_impact(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str | None]] = []
    recorder = ResizeRecorder()
    app = make_app(
        recorder,
        tmp_path / "audit.jsonl",
        relationship_calls=calls,
    )
    async with app.run_test() as pilot:
        await _open_resize_confirmation(app, pilot)
        text = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "pod resize Pod/default/web-1" in text
        assert "known direct dependents (may be affected): none in this snapshot" in text
        assert "Pod-local resize impact (advisory):" in text
        assert "graph relations are not traversed" in text
        assert not any(
            word in text
            for word in ("Service/default", "Deployment/default", "PodDisruptionBudget/default")
        )
        assert calls
        assert recorder.calls == []


async def test_resize_keeps_local_notes_without_relationship_loader(tmp_path: Path) -> None:
    recorder = ResizeRecorder()
    app = make_app(recorder, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _open_resize_confirmation(app, pilot)
        text = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "Pod-local resize impact (advisory):" in text
        assert "graph-derived impact" not in text
        assert recorder.calls == []


@pytest.mark.parametrize("mode", ["failure", "timeout"])
async def test_resize_graph_unavailable_keeps_local_notes_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    leaked_detail = "leaked backend response body"
    blocker: BlockingRelationshipLister | None = None

    async def failing_lister(_meta: ResourceMeta, _namespace: str | None) -> list[GenericSummary]:
        raise RuntimeError(leaked_detail)

    relationship_lister: Callable[[ResourceMeta, str | None], Awaitable[list[GenericSummary]]]
    if mode == "timeout":
        monkeypatch.setattr("korvid.ui.app._IMPACT_TIMEOUT", 0.01)
        blocker = BlockingRelationshipLister()
        relationship_lister = blocker
    else:
        relationship_lister = failing_lister

    recorder = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(recorder, audit_path, relationship_lister=relationship_lister)
    async with app.run_test() as pilot:
        try:
            await _open_resize_confirmation(app, pilot)
            text = str(app.screen.query_one(".confirm-impact", Static).render())
            assert "graph-derived impact (advisory):" in text
            assert "impact unavailable; approval remains available" in text
            assert leaked_detail not in text
            assert "Pod-local resize impact (advisory):" in text
            assert "graph relations are not traversed" in text
            assert recorder.calls == []
            assert not audit_path.exists()
        finally:
            if blocker is not None:
                blocker.release.set()


async def test_resize_refuses_uid_drift_while_confirmation_is_open(tmp_path: Path) -> None:
    recorder = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(recorder, audit_path, relationship_calls=[])
    async with app.run_test() as pilot:
        await _open_resize_confirmation(app, pilot)
        app.store.apply_event(
            "pods",
            "default",
            "MODIFIED",
            PodSummary(
                name="web-1",
                namespace="default",
                phase="Running",
                ready="1/1",
                restarts=0,
                node=None,
                uid="pod-uid-2",
            ),
        )
        await until(
            pilot,
            lambda: getattr(app.store.get("pods", "default")[0], "uid", None) == "pod-uid-2",
            label="replacement pod rendered",
        )
        await pilot.press("y")
        await until(
            pilot,
            lambda: any(
                "selection changed during the confirmation dialog" in n.message
                for n in app._notifications
            ),
            label="stale resize approval refused",
        )
        assert recorder.calls == []
        assert not audit_path.exists()


async def test_resize_refuses_replacement_manifest_before_prompt(tmp_path: Path) -> None:
    async def replacement_manifest(
        _kind: str, _namespace: str | None, _name: str
    ) -> dict[str, Any]:
        metadata = _POD_MANIFEST["metadata"]
        assert isinstance(metadata, dict)
        return {
            **_POD_MANIFEST,
            "metadata": {**metadata, "uid": "pod-uid-2"},
        }

    recorder = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(recorder, audit_path, get_manifest=replacement_manifest)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
        await pilot.press("R")
        await until(
            pilot,
            lambda: any(
                "pod changed during the manifest fetch" in notification.message
                for notification in app._notifications
            ),
            label="replacement pod manifest refused",
        )
        assert not isinstance(app.screen, ResizePrompt)
        assert recorder.calls == []
        assert not audit_path.exists()


async def test_cancelled_resize_impact_load_writes_nothing(tmp_path: Path) -> None:
    recorder = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    lister = BlockingRelationshipLister()
    app = make_app(recorder, audit_path, relationship_lister=lister)
    lister.app = app
    async with app.run_test() as pilot:
        worker = await _start_resize_confirmation_worker(app, pilot)
        await until(pilot, lister.entered.is_set, label="resize impact listing")
        try:
            worker.cancel()
            await until(
                pilot,
                lambda: worker.is_cancelled and worker.is_finished,
                label="resize worker cancelled",
            )
            assert worker.state is WorkerState.CANCELLED
            assert not isinstance(app.screen, ConfirmScreen)
            assert len(app.screen_stack) == 1
            assert lister.reservations_during_load
            assert all(value == 0 for value in lister.reservations_during_load)
            assert app._active_cluster_writes == 0
            assert recorder.calls == []
            assert not audit_path.exists()
        finally:
            lister.release.set()


async def test_resize_confirmed_executes_and_audits(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
        await pilot.press("R")
        await until(
            pilot, lambda: isinstance(app.screen, ResizePrompt), label="resize prompt opened"
        )
        prompt_text = " ".join(str(node.render()) for node in app.screen.query(Static))
        assert "In-place pod resize" in prompt_text
        field = app.screen.query_one("#resize-0-requests-cpu", Input)
        assert field.value == "100m"  # prefilled from the live manifest
        field.value = "200m"
        field.focus()
        await pilot.press("enter")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="resize confirmation opened",
        )
        confirm_text = " ".join(str(node.render()) for node in app.screen.query(Static))
        assert "Apply in-place pod resize" in confirm_text
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="resize success audited",
        )
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
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
        await pilot.press("R")
        await until(
            pilot, lambda: isinstance(app.screen, ResizePrompt), label="resize prompt opened"
        )
        field = app.screen.query_one("#resize-0-requests-cpu", Input)
        field.value = "200m"
        field.focus()
        await pilot.press("enter")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="resize confirmation opened",
        )
        assert "200m" in str(app.screen.query_one(".confirm-preview").render())


async def test_resize_gated_when_cluster_lacks_subresource(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", resize_supported=False)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
        await pilot.press("R")
        await until(
            pilot,
            lambda: any("pods/resize" in n.message for n in app._notifications),
            label="resize unsupported warning shown",
        )
        assert not isinstance(app.screen, ResizePrompt)
        assert rec.calls == []


async def test_resize_only_applies_to_pods(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
        await _to_view(pilot, "deployments")
        await pilot.press("R")
        assert not isinstance(app.screen, ResizePrompt)
        assert rec.calls == []


async def test_readonly_blocks_resize(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", readonly=True)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
        await pilot.press("R")
        await until(
            pilot,
            lambda: any("Read-only mode" in n.message for n in app._notifications),
            label="read-only warning shown",
        )
        assert not isinstance(app.screen, ResizePrompt)
        assert rec.calls == []


async def test_resize_precheck_asks_patch_on_pods_resize(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    calls: list[tuple[str, str, str, str | None, str, str]] = []
    app = make_app(rec, tmp_path / "audit.jsonl", permitted=False, check_calls=calls)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
        await pilot.press("R")
        await until(pilot, lambda: bool(calls), label="resize precheck recorded")
        assert calls == [("patch", "pods", "resize", "default", "", "web-1")]
        assert not isinstance(app.screen, ResizePrompt)


async def test_resize_prompt_cancel_does_nothing(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
        await pilot.press("R")
        await until(
            pilot, lambda: isinstance(app.screen, ResizePrompt), label="resize prompt opened"
        )
        await pilot.press("escape")
        await until(
            pilot,
            lambda: not isinstance(app.screen, ResizePrompt),
            label="resize prompt dismissed",
        )
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
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write(
                "resize", "pods", "web-1", namespace="default", resources=resources
            )
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="agent resize confirmation opened",
        )
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


async def test_agent_resize_uses_explicit_namespace_for_impact(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []
    recorder = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"

    async def prod_manifest(_kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return {
            "metadata": {"name": name, "namespace": ns, "uid": "pod-uid-prod"},
            "spec": _POD_MANIFEST["spec"],
        }

    async def list_relationship_objects(
        meta: ResourceMeta, namespace: str | None
    ) -> list[GenericSummary]:
        calls.append((meta.plural, namespace))
        if meta.plural == "pods":
            return [
                GenericSummary(
                    name="web-1",
                    namespace=namespace or "",
                    kind="Pod",
                    created="",
                    uid="pod-uid-prod",
                )
            ]
        return []

    app = make_app(
        recorder,
        audit_path,
        get_manifest=prod_manifest,
        relationship_lister=list_relationship_objects,
    )
    resources = {"app": {"requests": {"cpu": "200m"}}}
    async with app.run_test() as pilot:
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
        assert app.current_scope == "default"
        _expand_panel(app)
        task = asyncio.create_task(
            app.agent_request_write(
                "resize",
                "pods",
                "web-1",
                namespace="prod",
                resources=resources,
            )
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="agent resize confirmation opened",
        )
        text = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "pod resize Pod/prod/web-1" in text
        assert "Pod-local resize impact (advisory):" in text
        assert calls
        assert {namespace for _, namespace in calls} == {"prod"}
        assert app.current_scope == "default"
        assert not recorder.calls
        await pilot.press("n")
        assert "denied" in await task
        assert not audit_path.exists()


async def test_agent_resize_keeps_local_notes_when_manifest_lookup_fails_open(
    tmp_path: Path,
) -> None:
    async def failing_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        raise RuntimeError("manifest backend unavailable")

    recorder = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        recorder,
        audit_path,
        get_manifest=failing_manifest,
        relationship_calls=[],
    )
    async with app.run_test() as pilot:
        _expand_panel(app)
        task = asyncio.create_task(
            app.agent_request_write(
                "resize",
                "pods",
                "web-1",
                namespace="default",
                resources={"app": {"requests": {"cpu": "200m"}}},
            )
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="agent resize confirmation opened",
        )
        text = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "restart requirements could not be determined" in text
        assert recorder.calls == []
        await pilot.press("n")
        assert "denied" in await task
        assert not audit_path.exists()


async def test_agent_resize_expiry_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("korvid.ui.app._APPROVAL_TIMEOUT", 0.2)
    recorder = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        recorder,
        audit_path,
        relationship_calls=[],
    )
    async with app.run_test() as pilot:
        _expand_panel(app)
        task = asyncio.create_task(
            app.agent_request_write(
                "resize",
                "pods",
                "web-1",
                namespace="default",
                resources={"app": {"requests": {"cpu": "200m"}}},
            )
        )
        await until(
            pilot,
            lambda: task.done() or isinstance(app.screen, ConfirmScreen),
            label="agent resize expired or surfaced",
        )
        result = await task
        assert "expired" in result
        assert "declined" not in result
        assert recorder.calls == []
        assert not isinstance(app.screen, ConfirmScreen)
        assert not audit_path.exists()


async def test_non_resize_agent_writes_do_not_gain_impact(tmp_path: Path) -> None:
    recorder = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        recorder,
        audit_path,
        relationship_calls=[],
    )
    async with app.run_test() as pilot:
        _expand_panel(app)
        task = asyncio.create_task(
            app.agent_request_write(
                "delete",
                "deployments",
                "web",
                namespace="default",
            )
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="agent delete confirmation opened",
        )
        assert not app.screen.query(".confirm-impact")
        await pilot.press("n")
        assert "denied" in await task
        assert not audit_path.exists()


async def test_cancelled_agent_resize_impact_load_writes_nothing(tmp_path: Path) -> None:
    recorder = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    lister = BlockingRelationshipLister()
    app = make_app(recorder, audit_path, relationship_lister=lister)
    lister.app = app
    resources = {"app": {"requests": {"cpu": "200m"}}}
    async with app.run_test() as pilot:
        task = asyncio.create_task(
            app.agent_request_write(
                "resize",
                "pods",
                "web-1",
                namespace="default",
                resources=resources,
            )
        )
        try:
            await until(pilot, lister.entered.is_set, label="agent resize impact listing")
            task.cancel()
            with pytest.raises(asyncio.CancelledError, match=r"^$"):
                await task
            assert not isinstance(app.screen, ConfirmScreen)
            assert len(app.screen_stack) == 1
            assert recorder.calls == []
            assert not audit_path.exists()
        finally:
            lister.release.set()


async def test_agent_resize_requires_resources(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
        result = await app.agent_request_write("resize", "pods", "web-1", namespace="default")
        assert result.startswith("ERROR:")
        assert rec.calls == []


async def test_agent_resize_rejected_for_non_pod_kind(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
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
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
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


async def test_resize_banner_reuses_the_prompt_manifest(tmp_path: Path) -> None:
    """The prompt prefill already fetched the pod manifest — the ownership
    banner derives from that snapshot instead of a second GET for the same
    object (issue #119 review)."""
    fetches: list[str] = []
    managed = {
        "metadata": {
            "name": "web-1",
            "namespace": "default",
            "uid": "pod-uid-1",
            "labels": {"app.kubernetes.io/managed-by": "Helm"},
            "annotations": {
                "meta.helm.sh/release-name": "nginx",
                "meta.helm.sh/release-namespace": "web",
            },
        },
        "spec": _POD_MANIFEST["spec"],
    }

    async def counting(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        fetches.append(kind)
        return managed

    app = make_app(ResizeRecorder(), tmp_path / "audit.jsonl", get_manifest=counting)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _row_count(app) == 1, label="pod row rendered")
        await pilot.press("R")
        await until(
            pilot, lambda: isinstance(app.screen, ResizePrompt), label="resize prompt opened"
        )
        field = app.screen.query_one("#resize-0-requests-cpu", Input)
        field.value = "200m"
        field.focus()
        await pilot.press("enter")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="resize confirmation opened",
        )
        banner = app.screen.query_one(".confirm-managed", Static)
        assert "helm release web/nginx" in str(banner.render())
    assert fetches == ["pods"]
