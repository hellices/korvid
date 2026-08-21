"""TUI write keybindings behind approval dialogs (issue #16, spec §5 #4).

Ctrl-D = delete, r = rollout restart, S = scale. Every path goes through a
ConfirmScreen; --readonly disables all of them; executed writes land in the
audit log.
"""

import asyncio
import contextlib
import copy
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml
from textual.css.query import NoMatches
from textual.widgets import Input

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.portforward import OWNER_CHAIN_PLURALS, WORKLOAD_PLURALS
from korvid.core.session_timeline import (
    AppendResult,
    SessionTimeline,
    TimelineResourceRef,
    TimelineSource,
    WriteAuditPayload,
)
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.writes import WriteOps
from korvid.ui.app import KorvidApp, _yaml_equal
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ReplicasPrompt
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_NODES_META = ResourceMeta("Node", "nodes", "", "v1", False, ("no",))
_MESSAGING_SUBS_META = ResourceMeta(
    "Subscription", "subscriptions", "messaging.example.io", "v1", True, ()
)
_OLM_SUBS_META = ResourceMeta(
    "Subscription", "subscriptions", "operators.coreos.com", "v1alpha1", True, ()
)

_ALIASES = {
    "pods": _PODS_META,
    "deployments": _DEPLOY_META,
    "nodes": _NODES_META,
}


class _FailingWriteTimeline(SessionTimeline):
    def append_write(
        self,
        *,
        epoch: int,
        action: str,
        kind_alias: str,
        display_kind: str,
        namespace: str | None,
        name: str,
        uid: str | None,
        outcome: str,
    ) -> AppendResult:
        raise RuntimeError("timeline unavailable")


class Recorder(WriteOps):
    def __init__(self, fail_status: int | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.uids: list[str | None] = []
        self.fail_status = fail_status

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        if self.fail_status is not None:
            raise ApiStatusError(self.fail_status, "boom")
        self.uids.append(uid)
        self.calls.append(("delete", meta.plural, namespace, name))

    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        self.uids.append(uid)
        self.calls.append(("scale", meta.plural, namespace, name, replicas))

    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.uids.append(uid)
        self.calls.append(("restart", meta.plural, namespace, name))

    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        if self.fail_status is not None:
            raise ApiStatusError(self.fail_status, "boom")
        self.uids.append(uid)
        self.calls.append(("replace", meta.plural, namespace, name, manifest))


def make_app(
    recorder: Recorder,
    audit_path: Path,
    *,
    readonly: bool = False,
    permitted: bool | None = None,
    get_manifest: object = None,
    edit_text: object = None,
    check_calls: list[tuple[str, str, str, str | None, str, str]] | None = None,
    extra_pods: list[Summary] | None = None,
    session_timeline: SessionTimeline | None = None,
    aliases: dict[str, ResourceMeta] | None = None,
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
            ),
            *(extra_pods or []),
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
        if check_calls is not None:
            check_calls.append((verb, resource, sub, ns, group, name))
        return permitted

    return KorvidApp(
        config=KorvidConfig(namespace="default", readonly=readonly),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES if aliases is None else aliases),
        get_manifest=get_manifest,  # type: ignore[arg-type]  # tests pass duck-typed callables
        edit_text=edit_text,  # type: ignore[arg-type]  # tests pass duck-typed callables
        write_ops=recorder,
        audit=AuditLog(audit_path),
        check_permission=None if permitted is None else check_permission,
        session_timeline=session_timeline,
    )


def _selected_name(app: KorvidApp) -> str | None:
    try:
        table = app.query_one(ResourceTable)
    except NoMatches:
        return None
    if table.row_count == 0:
        return None
    return str(table.get_row_at(table.cursor_row)[0])


async def _to_view(pilot, view: str) -> None:  # type: ignore[no-untyped-def]  # Pilot's app type isn't exposed by the fixture
    await pilot.press("colon")
    for ch in view:
        await pilot.press(ch)
    await pilot.press("enter")
    expected = {"pods": "web-1", "deployments": "web", "nodes": "worker-1"}[view]
    await until(
        pilot,
        lambda: pilot.app.current_kind == view and _selected_name(pilot.app) == expected,
        label=f"{view} view selected",
    )


async def test_ctrl_d_delete_confirmed_executes_and_audits(tmp_path: Path) -> None:
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="success audit recorded",
        )
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
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        base_screen = app.screen
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="delete confirmation opened",
        )
        await pilot.press("n")
        await until(
            pilot,
            lambda: app.screen is base_screen,
            label="delete cancel returned to base resource screen",
        )
        await pilot.pause(0.2)
        assert rec.calls == []
        assert not audit_path.exists()


async def test_readonly_blocks_delete(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl", readonly=True)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: any("Read-only mode" in n.message for n in app._notifications),
            label="read-only warning shown",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_cluster_scoped_delete_requires_typed_name(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await _to_view(pilot, "nodes")
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="cluster delete confirmation opened",
        )
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")  # goes into the input; must NOT confirm by itself
        await until(
            pilot,
            lambda: app.screen.query_one("#confirm-name", Input).value == "y",
            label="typed confirmation input updated",
        )
        assert rec.calls == []
        await pilot.press("backspace")  # clear the stray 'y'
        for ch in "worker-1":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(pilot, lambda: rec.calls, label="write call recorded")
        assert rec.calls == [("delete", "nodes", None, "worker-1")]


async def test_rollout_restart_on_deployment(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await _to_view(pilot, "deployments")
        await pilot.press("r")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(pilot, lambda: rec.calls, label="write call recorded")
        assert rec.calls == [("restart", "deployments", "default", "web")]


async def test_rollout_restart_rejected_on_pods(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("r")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_scale_flow_prompts_then_confirms(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await _to_view(pilot, "deployments")
        await pilot.press("S")
        await until(
            pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt opened"
        )
        await pilot.press("5")
        await pilot.press("enter")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(pilot, lambda: rec.calls, label="write call recorded")
        assert rec.calls == [("scale", "deployments", "default", "web", 5)]


async def test_failed_write_audits_error(tmp_path: Path) -> None:
    rec = Recorder(fail_status=403)
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "error" in audit_path.read_text(),
            label="error audit recorded",
        )
        entry = json.loads(audit_path.read_text().splitlines()[-1])
        assert entry["outcome"].startswith("error")


async def test_permission_denied_blocks_delete(tmp_path: Path) -> None:
    """A failed SelfSubjectAccessReview pre-check stops the flow before the dialog."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl", permitted=False)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: any("missing permission: delete pods" in n.message for n in app._notifications),
            label="delete permission denial shown",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_permission_allowed_proceeds(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl", permitted=True)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(pilot, lambda: rec.calls, label="write call recorded")
        assert rec.calls == [("delete", "pods", "default", "web-1")]


async def test_unwritable_audit_blocks_write(tmp_path: Path) -> None:
    """Fail-closed auditing: if the intent record cannot be written, the
    cluster write must not run."""
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    audit_path.mkdir()  # a directory at the log path makes appends fail
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("blocked: audit log unavailable" in n.message for n in app._notifications),
            label="audit-blocked write warning shown",
        )
        await pilot.pause(0.3)
        assert rec.calls == []


async def test_scale_prompt_prefills_current_replicas(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await _to_view(pilot, "deployments")
        await pilot.press("S")
        await until(
            pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt opened"
        )
        # Deployment summaries preserve spec.replicas as `desired`, so the
        # prompt starts prefilled with the current count.
        assert app.screen.query_one(Input).value == "3"


async def test_dialog_opened_during_permission_check_aborts_write(tmp_path: Path) -> None:
    """The RBAC pre-check is an API round trip: if the user opened another
    dialog while it ran, the confirmation must not stack on top where a
    queued keystroke could approve it unseen."""
    from korvid.ui.widgets.pick_screen import PickScreen

    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl", permitted=True)
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_check(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        started.set()
        await release.wait()
        return True

    app._check_permission = slow_check
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("ctrl+d")  # handler now awaits the stalled pre-check
        await until(pilot, started.is_set, label="permission check in flight")
        blocker = PickScreen("opened while the check was pending", ["a", "b"])
        await app.push_screen(blocker)
        release.set()
        await pilot.pause(0.3)
        assert app.screen is blocker  # no ConfirmScreen stacked on top
        assert rec.calls == []


async def test_y_queued_during_stalled_check_cannot_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'y' typed while the RBAC pre-check stalled predates the dialog: even
    if it reaches the ConfirmScreen after mounting, it must be discarded -
    the user never saw the operation it would approve."""
    from textual import events

    monkeypatch.setattr("korvid.ui.write_coordinator._PERMISSION_CHECK_TIMEOUT", 0.1)
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl", permitted=True)

    async def stall(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        await asyncio.Event().wait()  # never resolves
        return True

    app._check_permission = stall
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        # The input driver timestamps keys on arrival: one typed during the
        # stalled check predates the dialog, which only exists afterwards.
        stale = events.Key("y", "y")
        await pilot.press("ctrl+d")  # stalls, then fails open into the dialog
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        app.screen.post_message(stale)
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)  # discarded: still open
        assert rec.calls == []
        await pilot.press("y")  # a fresh keystroke still confirms
        await until(
            pilot,
            lambda: rec.calls == [("delete", "pods", "default", "web-1")],
            label="fresh keystroke approves the delete",
        )
        assert rec.calls == [("delete", "pods", "default", "web-1")]


async def test_delete_binds_selected_row_uid(tmp_path: Path) -> None:
    """The uid of the row the user selected rides along as a delete
    precondition, so the approval cannot land on a same-named replacement
    created while the dialog was open."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(pilot, lambda: rec.calls, label="write call recorded")
        assert rec.uids == ["pod-uid-1"]


async def test_scale_binds_selected_row_uid(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await _to_view(pilot, "deployments")
        await pilot.press("S")
        await until(
            pilot, lambda: isinstance(app.screen, ReplicasPrompt), label="replicas prompt opened"
        )
        await pilot.press("5")
        await pilot.press("enter")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(pilot, lambda: rec.calls, label="write call recorded")
        assert rec.uids == ["deploy-uid-1"]


async def test_conflict_reports_target_changed_since_approval(tmp_path: Path) -> None:
    """A 409 (uid precondition tripped: the object was deleted and recreated
    after approval) surfaces as an actionable message, not a bare API error."""
    rec = Recorder(fail_status=409)
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("changed since it was approved" in n.message for n in app._notifications),
            label="target-changed notification shown",
        )


# Inline edit (issue #21) -------------------------------------------------------


_EDIT_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": "web-1",
        "namespace": "default",
        "resourceVersion": "41",
        "managedFields": [{"manager": "kubectl"}],
    },
    "spec": {"replicas": 1, "containers": [{"name": "app", "image": "nginx:1"}]},
}


def _edit_fixtures(
    edited: str | Callable[[str], str] | None,
) -> tuple[
    Callable[..., Awaitable[dict[str, Any]]], Callable[[str], Awaitable[str | None]], list[str]
]:
    """(get_manifest, edit_text, seen_texts): edit_text records what the
    'editor' was shown and returns `edited` (or `edited(text)` if callable)."""
    seen: list[str] = []

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return copy.deepcopy(_EDIT_MANIFEST)

    async def edit_text(text: str) -> str | None:
        seen.append(text)
        return edited(text) if callable(edited) else edited

    return get_manifest, edit_text, seen


async def test_e_edit_confirmed_replaces_with_uid(tmp_path: Path) -> None:
    def bump_image(text: str) -> str:
        assert "nginx:1" in text
        return text.replace("nginx:1", "nginx:2")

    get_manifest, edit_text, _seen = _edit_fixtures(bump_image)
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path, get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="success audit recorded",
        )
    assert len(rec.calls) == 1
    op, plural, ns, name, manifest = rec.calls[0]
    assert (op, plural, ns, name) == ("replace", "pods", "default", "web-1")
    assert isinstance(manifest, dict)
    assert manifest["spec"]["containers"][0]["image"] == "nginx:2"
    assert rec.uids == ["pod-uid-1"]
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    assert entries[0]["action"] == "edit"
    assert entries[-1]["outcome"] == "success"


async def test_e_edit_strips_managed_fields_from_editor_text(tmp_path: Path) -> None:
    get_manifest, edit_text, seen = _edit_fixtures(None)
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(pilot, lambda: bool(seen), label="editor text captured")
    assert "managedFields" not in seen[0]
    assert "resourceVersion" in seen[0]


async def test_e_edit_cancelled_editor_makes_no_call(tmp_path: Path) -> None:
    get_manifest, edit_text, _ = _edit_fixtures(None)
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot,
            lambda: any("cancelled" in n.message for n in app._notifications),
            label="edit cancellation notification shown",
        )
    assert rec.calls == []


async def test_e_edit_unchanged_text_is_a_noop(tmp_path: Path) -> None:
    get_manifest, edit_text, _ = _edit_fixtures(lambda text: text)
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot,
            lambda: any("no changes" in n.message for n in app._notifications),
            label="no-change edit notification shown",
        )
        assert not isinstance(app.screen, ConfirmScreen)
    assert rec.calls == []


async def test_e_edit_invalid_yaml_aborts(tmp_path: Path) -> None:
    get_manifest, edit_text, _ = _edit_fixtures("{invalid: [yaml")
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot,
            lambda: any("invalid YAML" in n.message for n in app._notifications),
            label="invalid-yaml notification shown",
        )
    assert rec.calls == []


async def test_e_edit_non_mapping_yaml_aborts(tmp_path: Path) -> None:
    get_manifest, edit_text, _ = _edit_fixtures("- just\n- a\n- list\n")
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot,
            lambda: any("not a mapping" in n.message for n in app._notifications),
            label="non-mapping notification shown",
        )
    assert rec.calls == []


async def test_e_edit_reinjects_deleted_resource_version(tmp_path: Path) -> None:
    """resourceVersion removed by the user is restored from the fetched
    manifest: an unversioned PUT would silently clobber concurrent changes."""

    def drop_rv(text: str) -> str:
        lines = [
            ln
            for ln in text.replace("nginx:1", "nginx:2").splitlines()
            if "resourceVersion" not in ln
        ]
        return "\n".join(lines) + "\n"

    get_manifest, edit_text, _ = _edit_fixtures(drop_rv)
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path, get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="success audit recorded",
        )
    manifest = rec.calls[0][4]
    assert isinstance(manifest, dict)
    assert manifest["metadata"]["resourceVersion"] == "41"


async def test_e_edit_readonly_blocked(tmp_path: Path) -> None:
    get_manifest, edit_text, seen = _edit_fixtures(None)
    rec = Recorder()
    app = make_app(
        rec, tmp_path / "a.jsonl", readonly=True, get_manifest=get_manifest, edit_text=edit_text
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot,
            lambda: any("Read-only" in n.message for n in app._notifications),
            label="read-only warning shown",
        )
    assert seen == []
    assert rec.calls == []


async def test_e_edit_without_manifest_source_notifies(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot,
            lambda: any("unavailable" in n.message for n in app._notifications),
            label="unavailable notification shown",
        )
    assert rec.calls == []


async def test_e_edit_deleting_only_resource_version_is_a_noop(tmp_path: Path) -> None:
    """Review: restore the version before the semantic comparison, so an edit
    that only deleted resourceVersion still counts as 'no changes'."""

    def drop_rv_only(text: str) -> str:
        lines = [ln for ln in text.splitlines() if "resourceVersion" not in ln]
        return "\n".join(lines) + "\n"

    get_manifest, edit_text, _ = _edit_fixtures(drop_rv_only)
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot,
            lambda: any("no changes" in n.message for n in app._notifications),
            label="no-change edit notification shown",
        )
    assert rec.calls == []


async def test_e_edit_null_metadata_gets_rebuilt_with_resource_version(tmp_path: Path) -> None:
    """Review: `metadata: null` defeats setdefault - the fetched
    resourceVersion must still be restored (inside a mapping) so the PUT
    cannot silently clobber concurrent changes."""

    def null_metadata(text: str) -> str:
        manifest = yaml.safe_load(text)
        manifest["metadata"] = None
        manifest["spec"]["containers"][0]["image"] = "nginx:2"
        return yaml.safe_dump(manifest, sort_keys=False)

    get_manifest, edit_text, _ = _edit_fixtures(null_metadata)
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path, get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="success audit recorded",
        )
    manifest = rec.calls[0][4]
    assert isinstance(manifest, dict)
    assert manifest["metadata"] == {"resourceVersion": "41"}


async def test_external_editor_invocation_failure_notifies_and_cancels(tmp_path: Path) -> None:
    """Review: a broken $EDITOR (malformed quoting / missing executable) must
    abort with a notification instead of an unhandled action error."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        with mock.patch.dict(os.environ, {"VISUAL": "bad 'quote", "EDITOR": ""}):
            result = await app._edit_in_external_editor("a: 1\n")
        assert result is None
        await until(
            pilot,
            lambda: any("editor" in n.message for n in app._notifications),
            label="editor failure notification shown",
        )
    assert rec.calls == []


async def test_e_edit_blank_resource_version_restored(tmp_path: Path) -> None:
    """Review round 2: `resourceVersion:` with a blank value loads as None -
    the key is present so setdefault would leave the PUT unversioned."""

    def blank_rv(text: str) -> str:
        manifest = yaml.safe_load(text)
        manifest["metadata"]["resourceVersion"] = None
        manifest["spec"]["containers"][0]["image"] = "nginx:2"
        return yaml.safe_dump(manifest, sort_keys=False)

    get_manifest, edit_text, _ = _edit_fixtures(blank_rv)
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path, get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="success audit recorded",
        )
    manifest = rec.calls[0][4]
    assert isinstance(manifest, dict)
    assert manifest["metadata"]["resourceVersion"] == "41"


async def test_e_edit_confirm_dialog_summarizes_changed_sections(tmp_path: Path) -> None:
    """Issue #21: the approval dialog must summarize the change, not just the
    target and verb."""
    get_manifest, edit_text, _ = _edit_fixtures(lambda text: text.replace("nginx:1", "nginx:2"))
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        operation = str(app.screen.query_one(".confirm-operation").render())
        assert "PUT pods/web-1" in operation
        assert "spec" in operation  # the edited top-level section is named
        await pilot.press("escape")


async def test_e_edit_scalar_type_change_reaches_approval(tmp_path: Path) -> None:
    """Review round 3: Python equality conflates YAML booleans and integers
    (True == 1), silently discarding an edit that changes only the scalar
    type of an untyped/CRD field."""

    def flip_type(text: str) -> str:
        manifest = yaml.safe_load(text)
        assert manifest["spec"]["replicas"] == 1
        manifest["spec"]["replicas"] = True
        return yaml.safe_dump(manifest, sort_keys=False)

    get_manifest, edit_text, _ = _edit_fixtures(flip_type)
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path, get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        operation = str(app.screen.query_one(".confirm-operation").render())
        assert "spec" in operation  # the type change is named in the summary
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="success audit recorded",
        )
    manifest = rec.calls[0][4]
    assert isinstance(manifest, dict)
    assert manifest["spec"]["replicas"] is True


async def test_e_edit_added_null_section_named_in_summary(tmp_path: Path) -> None:
    """Review round 3: dict.get returns None for both an absent key and a
    present null key, so adding `status: null` produced an empty summary."""

    def add_null_status(text: str) -> str:
        manifest = yaml.safe_load(text)
        assert "status" not in manifest
        manifest["status"] = None
        return yaml.safe_dump(manifest, sort_keys=False)

    get_manifest, edit_text, _ = _edit_fixtures(add_null_status)
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        operation = str(app.screen.query_one(".confirm-operation").render())
        assert "status" in operation
        await pilot.press("escape")


async def test_e_edit_survives_alias_refresh_during_editor(tmp_path: Path) -> None:
    """Review round 4: background discovery replaces alias values with fresh
    (equal) ResourceMeta instances; identity-based revalidation cancelled an
    edit of the same selected row when discovery finished mid-editor."""
    seen: list[str] = []

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return copy.deepcopy(_EDIT_MANIFEST)

    app_holder: list[KorvidApp] = []

    async def edit_text(text: str) -> str | None:
        seen.append(text)
        # Simulate discovery completing while the editor is open: the alias
        # map is updated with equal-valued but distinct instances.
        app_holder[0].aliases["pods"] = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
        return text.replace("nginx:1", "nginx:2")

    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path, get_manifest=get_manifest, edit_text=edit_text)
    app_holder.append(app)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count > 0, label="pod row")
        await pilot.press("e")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="success audit recorded",
        )
    assert len(rec.calls) == 1


async def test_e_edit_non_string_top_level_key_aborts(tmp_path: Path) -> None:
    """Review round 5: a YAML mapping can legally have a non-string top-level
    key; sorting it against string keys in the summary raised TypeError."""
    get_manifest, edit_text, _ = _edit_fixtures("1: value\nspec: {}\n")
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(
            pilot,
            lambda: any("non-string" in n.message for n in app._notifications),
            label="non-string-key notification shown",
        )
    assert rec.calls == []


async def test_e_edit_precheck_uses_update_verb(tmp_path: Path) -> None:
    """Review round 5: pin the RBAC pre-check arguments for the edit flow."""
    get_manifest, edit_text, _ = _edit_fixtures(None)
    rec = Recorder()
    calls: list[tuple[str, str, str, str | None, str, str]] = []
    app = make_app(
        rec,
        tmp_path / "a.jsonl",
        permitted=True,
        get_manifest=get_manifest,
        edit_text=edit_text,
        check_calls=calls,
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        await pilot.press("e")
        await until(pilot, lambda: bool(calls), label="edit precheck recorded")
    assert calls[0] == ("update", "pods", "", "default", "", "web-1")


async def test_e_edit_selection_change_during_editor_aborts(tmp_path: Path) -> None:
    """Review round 5: the post-editor revalidation must abort when the user
    actually moved the selection while the editor was open - no confirmation
    is pushed and no replace call occurs."""

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return copy.deepcopy(_EDIT_MANIFEST)

    app_holder: list[KorvidApp] = []

    async def edit_text(text: str) -> str | None:
        app_holder[0].query_one(ResourceTable).move_cursor(row=1)
        return text.replace("nginx:1", "nginx:2")

    rec = Recorder()
    other = PodSummary(
        name="web-2",
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        uid="pod-uid-2",
    )
    app = make_app(
        rec,
        tmp_path / "a.jsonl",
        get_manifest=get_manifest,
        edit_text=edit_text,
        extra_pods=[other],
    )
    app_holder.append(app)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count >= 2, label="pod rows")
        await pilot.press("e")
        await until(
            pilot,
            lambda: any(
                "selection changed during the editor session" in n.message
                for n in app._notifications
            ),
            label="editor-session selection-change shown",
        )
        assert not isinstance(app.screen, ConfirmScreen)
    assert rec.calls == []


async def test_external_editor_mkstemp_failure_notifies_and_cancels(tmp_path: Path) -> None:
    """Review round 6 (suppressed): a full/unavailable temp dir must abort
    with a notification instead of raising out of the action."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        with mock.patch("tempfile.mkstemp", side_effect=OSError("no space")):
            result = await app._edit_in_external_editor("a: 1\n")
        assert result is None
        await until(
            pilot,
            lambda: any("temp file failed" in n.message for n in app._notifications),
            label="temp-file failure notification shown",
        )
    assert rec.calls == []


async def test_external_editor_undecodable_output_notifies_and_cancels(tmp_path: Path) -> None:
    """Review round 6 (suppressed): editor output that is not valid UTF-8
    raises UnicodeDecodeError (a ValueError, not OSError) - it must land in
    the cancellation path."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl")

    def write_binary(argv: list[str]) -> int:
        Path(argv[-1]).write_bytes(b"\xff\xfe\x00broken")
        return 0

    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        with (
            mock.patch.dict(os.environ, {"VISUAL": "true", "EDITOR": ""}),
            mock.patch("subprocess.call", side_effect=write_binary),
            mock.patch.object(type(app), "suspend", mock.MagicMock()) as fake_suspend,
        ):
            fake_suspend.return_value.__enter__ = mock.MagicMock(return_value=None)
            fake_suspend.return_value.__exit__ = mock.MagicMock(return_value=False)
            result = await app._edit_in_external_editor("a: 1\n")
        assert result is None
        await until(
            pilot,
            lambda: any("unreadable" in n.message for n in app._notifications),
            label="unreadable-editor-output notification shown",
        )
    assert rec.calls == []


def test_yaml_equal_ignores_shared_nodes() -> None:
    """Review round 7: safe_dump emits anchors for shared nodes, so dump-text
    comparison reported aliased-but-equal documents as changed."""
    shared = {"a": 1}
    aliased = {"spec": shared, "status": shared}
    plain = {"spec": {"a": 1}, "status": {"a": 1}}
    assert _yaml_equal(aliased, plain)


def test_yaml_equal_is_scalar_type_sensitive() -> None:
    assert not _yaml_equal({"x": 1}, {"x": True})
    assert not _yaml_equal({"x": True}, {"x": 1})
    assert not _yaml_equal([1], [1.0])
    assert _yaml_equal({"x": [1, {"y": "z"}]}, {"x": [1, {"y": "z"}]})


def test_yaml_equal_distinguishes_key_types() -> None:
    assert not _yaml_equal({1: "v"}, {True: "v"})


async def test_external_editor_whitespace_only_editor_cancels(tmp_path: Path) -> None:
    """Review round 8 (suppressed): a whitespace-only $VISUAL/$EDITOR passes
    the fallback expression but shlex.split returns an empty list -
    subprocess.call([]) would raise IndexError out of the action."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        with mock.patch.dict(os.environ, {"VISUAL": "   ", "EDITOR": ""}):
            result = await app._edit_in_external_editor("a: 1\n")
        assert result is None
        await until(
            pilot,
            lambda: any("editor" in n.message for n in app._notifications),
            label="editor failure notification shown",
        )
    assert rec.calls == []


def test_yaml_equal_string_keyed_fast_path_matches_structural_scan() -> None:
    """Review round 8: string-keyed mappings take a direct-lookup fast path;
    it must agree with the structural scan used for unusual key types."""
    big = {f"key-{i}": [i, {"nested": str(i)}] for i in range(500)}
    assert _yaml_equal(big, copy.deepcopy(big))
    other = copy.deepcopy(big)
    other["key-499"][0] = True  # 499 == True is False, but type check matters elsewhere
    assert not _yaml_equal(big, other)
    missing = copy.deepcopy(big)
    del missing["key-0"]
    missing["key-x"] = [0, {"nested": "0"}]
    assert not _yaml_equal(big, missing)


async def test_e_edit_selection_change_during_fetch_never_opens_editor(tmp_path: Path) -> None:
    """Review round 9: the manifest GET is an awaited round-trip after the
    permission revalidation - a selection change while it is in flight must
    abort before the editor opens, not merely discard the completed edit."""
    seen: list[str] = []
    app_holder: list[KorvidApp] = []

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        app_holder[0].query_one(ResourceTable).move_cursor(row=1)
        return copy.deepcopy(_EDIT_MANIFEST)

    async def edit_text(text: str) -> str | None:
        seen.append(text)
        return None

    rec = Recorder()
    other = PodSummary(
        name="web-2",
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        uid="pod-uid-2",
    )
    app = make_app(
        rec,
        tmp_path / "a.jsonl",
        get_manifest=get_manifest,
        edit_text=edit_text,
        extra_pods=[other],
    )
    app_holder.append(app)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count >= 2, label="pod rows")
        await pilot.press("e")
        await until(
            pilot,
            lambda: any(
                "selection changed during the manifest fetch" in n.message
                for n in app._notifications
            ),
            label="manifest-fetch selection-change shown",
        )
    assert seen == []  # the editor never opened for the stale target
    assert rec.calls == []


async def test_external_editor_suspend_not_supported_cancels(tmp_path: Path) -> None:
    """Review round 10: drivers that cannot suspend (e.g. Windows) raise
    SuspendNotSupported outside the OSError/ValueError handler - pressing e
    there must cancel with a notification, not an unhandled action error.
    The headless test driver does not support suspend, so no mocking is
    needed to reproduce."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl")
    async with app.run_test() as pilot:
        await until(pilot, lambda: _selected_name(app) == "web-1", label="pod row selected")
        with mock.patch.dict(os.environ, {"VISUAL": "true", "EDITOR": ""}):
            result = await app._edit_in_external_editor("a: 1\n")
        assert result is None
        await until(
            pilot,
            lambda: any("does not support" in n.message for n in app._notifications),
            label="suspend-unsupported notification shown",
        )
    assert rec.calls == []


# ---------------------------------------------------------------------------
# Issue #119: ownership banner on managed targets
# ---------------------------------------------------------------------------


def _helm_deploy_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {"app.kubernetes.io/managed-by": "Helm"},
            "annotations": {
                "meta.helm.sh/release-name": "nginx",
                "meta.helm.sh/release-namespace": "web",
            },
        }
    }


async def test_delete_confirm_shows_the_ownership_banner(tmp_path: Path) -> None:
    """A helm-managed target warns in the dialog before approval (issue #119)."""

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return _helm_deploy_manifest(kind, ns, name)

    app = make_app(Recorder(), tmp_path / "audit.jsonl", get_manifest=get_manifest)
    async with app.run_test() as pilot:
        await _to_view(pilot, "deployments")
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        banner = app.screen.query_one(".confirm-managed")
        assert "helm release web/nginx" in str(banner.render())


async def test_pod_banner_walks_the_controller_chain(tmp_path: Path) -> None:
    """A pod owned by rs -> deploy reports the top owner's manager: the pod
    itself carries no helm annotations, the Deployment does (issue #119)."""

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        if kind == "pods":
            return {
                "metadata": {
                    "name": name,
                    "namespace": ns,
                    "ownerReferences": [
                        {
                            "apiVersion": "apps/v1",
                            "kind": "ReplicaSet",
                            "name": "web-abc",
                            "controller": True,
                        }
                    ],
                }
            }
        if kind == "replicasets":
            return {
                "metadata": {
                    "name": name,
                    "namespace": ns,
                    "ownerReferences": [
                        {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "name": "web",
                            "controller": True,
                        }
                    ],
                }
            }
        return _helm_deploy_manifest(kind, ns, name)

    app = make_app(Recorder(), tmp_path / "audit.jsonl", get_manifest=get_manifest)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count > 0, label="pod row")
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        banner = app.screen.query_one(".confirm-managed")
        assert "helm release web/nginx" in str(banner.render())


async def test_banner_walks_through_a_replication_controller(tmp_path: Path) -> None:
    """The owner chain must not stop at a ReplicationController (issue #119).

    Every kind the chain can traverse needs an entry in the plural map; a
    missing one makes the walk end early and silently drop the banner, so
    a helm-managed target looks unmanaged at the approval dialog.
    """

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        if kind == "pods":
            return {
                "metadata": {
                    "name": name,
                    "namespace": ns,
                    "ownerReferences": [
                        {
                            "apiVersion": "v1",
                            "kind": "ReplicationController",
                            "name": "web-rc",
                            "controller": True,
                        }
                    ],
                }
            }
        return _helm_deploy_manifest(kind, ns, name)

    app = make_app(Recorder(), tmp_path / "audit.jsonl", get_manifest=get_manifest)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count > 0, label="pod row")
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        banner = app.screen.query_one(".confirm-managed")
        assert "helm release web/nginx" in str(banner.render())


async def test_unmanaged_target_shows_no_banner(tmp_path: Path) -> None:
    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return {"metadata": {"name": name, "namespace": ns}}

    app = make_app(Recorder(), tmp_path / "audit.jsonl", get_manifest=get_manifest)
    async with app.run_test() as pilot:
        await _to_view(pilot, "deployments")
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        assert not app.screen.query(".confirm-managed")


async def test_banner_lookup_failure_never_blocks_the_dialog(tmp_path: Path) -> None:
    """The banner is best-effort display (fail-open): a broken manifest
    fetch means no banner, never a blocked write flow."""

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        raise RuntimeError("boom")

    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path, get_manifest=get_manifest)
    async with app.run_test() as pilot:
        await _to_view(pilot, "deployments")
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
        assert not app.screen.query(".confirm-managed")
        await pilot.press("y")
        await until(
            pilot,
            lambda: rec.calls == [("delete", "deployments", "default", "web")],
            label="deployment delete recorded",
        )
        assert rec.calls == [("delete", "deployments", "default", "web")]


async def test_owner_chain_walk_follows_cronjobs(tmp_path: Path) -> None:
    """Pod -> Job -> CronJob is a common chain and the helm/OLM markers live
    on the CronJob — the walk must know its plural (issue #119 review)."""

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        if kind == "jobs":
            return {
                "metadata": {
                    "name": name,
                    "ownerReferences": [
                        {
                            "apiVersion": "batch/v1",
                            "kind": "CronJob",
                            "name": "hourly",
                            "controller": True,
                        }
                    ],
                }
            }
        assert kind == "cronjobs"
        return _helm_deploy_manifest(kind, ns, name)

    app = make_app(Recorder(), tmp_path / "audit.jsonl", get_manifest=get_manifest)
    pod = {
        "metadata": {
            "name": "hourly-123-abc",
            "namespace": "default",
            "ownerReferences": [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": "hourly-123",
                    "controller": True,
                }
            ],
        }
    }
    note = await app._managed_note_from(pod, "default")
    assert note is not None
    assert "helm release web/nginx" in note


async def test_owner_chain_walk_shares_one_deadline(tmp_path: Path) -> None:
    """Per-hop bounds would let the target plus two owners delay the dialog
    to ~3x _UID_LOOKUP_TIMEOUT despite the fail-open promise: the whole
    target-and-owner-chain lookup shares a single deadline."""

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        if kind == "pods":
            return {
                "metadata": {
                    "name": name,
                    "ownerReferences": [
                        {
                            "apiVersion": "apps/v1",
                            "kind": "ReplicaSet",
                            "name": "web-abc",
                            "controller": True,
                        }
                    ],
                }
            }
        await asyncio.sleep(0.7)  # each hop alone stays under the 1.0 bound
        if kind == "replicasets":
            return {
                "metadata": {
                    "name": name,
                    "ownerReferences": [
                        {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "name": "web",
                            "controller": True,
                        }
                    ],
                }
            }
        return _helm_deploy_manifest(kind, ns, name)

    app = make_app(Recorder(), tmp_path / "audit.jsonl", get_manifest=get_manifest)
    with mock.patch("korvid.ui.app._UID_LOOKUP_TIMEOUT", 1.0):
        assert await app._managed_note("pods", "default", "web-1") is None


async def test_banner_lookup_failure_logs_omit_exception_payloads(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An API error message can embed the response body — for a Secret, its
    data. Banner-lookup failure logs name the exception type, never its
    payload (CodeQL py/clear-text-logging-sensitive-data)."""

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        raise RuntimeError("token: SECRET-PAYLOAD")

    app = make_app(Recorder(), tmp_path / "audit.jsonl", get_manifest=get_manifest)
    pod = {
        "metadata": {
            "name": "p",
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": "web",
                    "controller": True,
                }
            ],
        }
    }
    with caplog.at_level(logging.DEBUG, logger="korvid.ui.app"):
        assert await app._managed_note("secrets", "default", "db-creds") is None
        assert await app._managed_note_from(pod, "default") is None
    assert "SECRET-PAYLOAD" not in caplog.text


async def test_edit_cancelled_when_selection_changes_during_ownership_lookup(
    tmp_path: Path,
) -> None:
    """The banner's owner-chain walk is another awaited gap after the editor
    session: a selection change during it must cancel the confirmation, not
    push a dialog for the stale target (issue #119 review)."""
    pod_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "web-1",
            "namespace": "default",
            "uid": "pod-uid-1",
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "web-abc",
                    "controller": True,
                }
            ],
        },
        "spec": {"containers": [{"name": "app", "image": "nginx:1"}]},
    }
    app_holder: list[KorvidApp] = []

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        if kind == "replicasets":
            # The user moves on while the owner lookup is in flight.
            app_holder[0].query_one(ResourceTable).move_cursor(row=1)
            return {"metadata": {"name": name}}
        return copy.deepcopy(pod_manifest)

    async def edit_text(text: str) -> str | None:
        return text.replace("nginx:1", "nginx:2")

    rec = Recorder()
    other = PodSummary(
        name="web-2",
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        uid="pod-uid-2",
    )
    app = make_app(
        rec,
        tmp_path / "audit.jsonl",
        get_manifest=get_manifest,
        edit_text=edit_text,
        extra_pods=[other],
    )
    app_holder.append(app)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count >= 2, label="pod rows")
        await pilot.press("e")
        await until(
            pilot,
            lambda: any(
                "selection changed during the ownership lookup" in n.message
                for n in app._notifications
            ),
            label="ownership-lookup selection-change shown",
        )
        assert not isinstance(app.screen, ConfirmScreen)
    assert rec.calls == []


async def test_banner_kind_captured_before_the_preview_await(tmp_path: Path) -> None:
    """The banner lookup uses the kind captured when the write flow began:
    the view can change during the preview await and change back before the
    final selection check, and the dialog must not carry a note fetched for
    an unrelated kind (issue #119 review)."""
    looked_up: list[str] = []
    app_holder: list[KorvidApp] = []

    class ViewSwitchingRecorder(Recorder):
        async def preview_delete(
            self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
        ) -> list[str] | None:
            # The user browses to another view while the preview is in flight…
            app_holder[0].current_kind = "deployments"
            return None

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        looked_up.append(kind)
        # …and returns to the original view before the final check.
        app_holder[0].current_kind = "pods"
        return {"metadata": {"name": name}}

    app = make_app(ViewSwitchingRecorder(), tmp_path / "audit.jsonl", get_manifest=get_manifest)
    app_holder.append(app)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count > 0, label="pod row")
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirmation dialog opened"
        )
    assert looked_up == ["pods"]


async def test_walk_ignores_string_controller_flags(tmp_path: Path) -> None:
    """A malformed owner edge with controller: "false" must not be followed:
    the walk would otherwise report the parent's manager for an object it
    does not control (issue #119 review)."""

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return _helm_deploy_manifest(kind, ns, name)

    app = make_app(Recorder(), tmp_path / "audit.jsonl", get_manifest=get_manifest)
    pod = {
        "metadata": {
            "name": "web-1",
            "namespace": "default",
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "web-abc",
                    "controller": "false",
                }
            ],
        }
    }
    assert await app._managed_note_from(pod, "default") is None


# -- _run_write factory-based design regression tests -------------------------


async def test_blocked_audit_never_invokes_op_factory(tmp_path: Path) -> None:
    """When the intent audit fails, the operation factory must never be called.

    The factory design guarantees no mutation coroutine is created before
    intent is persisted — a blocked write cannot leak an unawaited coroutine.
    """
    factory_calls: list[str] = []

    async def spy_op() -> None:
        factory_calls.append("invoked")

    def factory() -> Awaitable[None]:
        factory_calls.append("created")
        return spy_op()

    audit_path = tmp_path / "audit.jsonl"
    audit_path.mkdir()  # directory makes appends fail → intent blocked
    app = make_app(Recorder(), audit_path)
    async with app.run_test():
        result = await app._writes.run("delete", _PODS_META, "default", "web-1", factory)
    assert "blocked" in result
    assert factory_calls == [], "factory must not be called when audit intent fails"


async def test_cancelled_before_factory_leaks_no_coroutine(tmp_path: Path) -> None:
    """Cancellation while the audit intent is in-flight must never invoke the
    factory — no unawaited coroutine, no mutation before intent.

    Uses a gated audit (threading.Event inside to_thread) to deterministically
    cancel at the right instant.
    """
    import threading

    factory_calls: list[str] = []

    async def spy_op() -> None:
        factory_calls.append("invoked")

    def factory() -> Awaitable[None]:
        factory_calls.append("created")
        return spy_op()

    audit_gate = threading.Event()
    real_audit_path = tmp_path / "audit.jsonl"
    entered = threading.Event()

    class GatedAudit(AuditLog):
        def append(
            self,
            *,
            action: str,
            kind: str,
            namespace: str | None,
            name: str,
            group: str = "",
            version: str = "",
            detail: str = "",
            outcome: str = "success",
            context: object = None,
        ) -> None:
            entered.set()
            audit_gate.wait()
            super().append(
                action=action,
                kind=kind,
                namespace=namespace,
                name=name,
                group=group,
                version=version,
                detail=detail,
                outcome=outcome,
            )

    audit = GatedAudit(real_audit_path, context="test")
    rec = Recorder()
    app = make_app(rec, real_audit_path)
    app._audit = audit

    async with app.run_test() as pilot:
        task = asyncio.create_task(
            app._writes.run("delete", _PODS_META, "default", "web-1", factory)
        )
        try:
            await until(pilot, entered.is_set, label="audit entered")
            task.cancel()
        finally:
            # Always release so the executor thread cannot hang at exit.
            audit_gate.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert factory_calls == [], "factory must not be called when cancelled during audit"
    assert rec.calls == [], "no mutation must reach the recorder"


def test_owner_chain_is_a_superset_of_the_workload_map() -> None:
    """The banner walks at least as far as a re-attach follows.

    The two were restated independently once and immediately drifted:
    `ReplicationController` was dropped from the chain, so the banner
    stopped there. Deriving one from the other is what keeps them honest.
    """
    assert set(WORKLOAD_PLURALS) < set(OWNER_CHAIN_PLURALS)
    assert all(OWNER_CHAIN_PLURALS[k] == v for k, v in WORKLOAD_PLURALS.items())


# -- bounded session timeline: write records (issue #282) ---------------------


def _write_records(timeline: SessionTimeline) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    for entry in timeline.snapshot(epoch=None, source=TimelineSource.WRITE, resource=None).entries:
        assert isinstance(entry.payload, WriteAuditPayload)
        records.append((entry.payload.action, entry.payload.outcome))
    return records


async def test_run_write_records_timeline_after_intent_and_success_audit(tmp_path: Path) -> None:
    """The timeline mirrors the durable audit trail: both records appear,
    in the same order the audit log persisted them."""
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(Recorder(), audit_path, session_timeline=timeline)

    async def op() -> None:
        return None

    async with app.run_test():
        result = await app._writes.run("delete", _PODS_META, "default", "web-1", lambda: op())
    assert result == "done"
    assert _write_records(timeline) == [("delete", "intent"), ("delete", "success")]
    entry = timeline.snapshot(epoch=0, source=TimelineSource.WRITE, resource=None).entries[0]
    assert entry.resource is not None
    assert (entry.resource.kind_alias, entry.resource.namespace, entry.resource.name) == (
        "pods",
        "default",
        "web-1",
    )
    assert [json.loads(line)["outcome"] for line in audit_path.read_text().splitlines()] == [
        "intent",
        "success",
    ]


async def test_timeline_failure_does_not_replace_durable_write_audit(tmp_path: Path) -> None:
    timeline = _FailingWriteTimeline(max_entries=8, max_bytes=4096)
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(Recorder(), audit_path, session_timeline=timeline)
    ran = False

    async def op() -> None:
        nonlocal ran
        ran = True

    async with app.run_test():
        result = await app._writes.run("delete", _PODS_META, "default", "web-1", lambda: op())
        assert result == "done"
        assert ran is True
        assert any("Timeline skipped write entry" in item.message for item in app._notifications)
    assert [json.loads(line)["outcome"] for line in audit_path.read_text().splitlines()] == [
        "intent",
        "success",
    ]


async def test_write_timeline_uses_qualified_alias_when_bare_plural_collides(
    tmp_path: Path,
) -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    aliases = {
        "subscriptions": _MESSAGING_SUBS_META,
        "sub": _OLM_SUBS_META,
        "subscriptions.operators.coreos.com": _OLM_SUBS_META,
    }
    app = make_app(
        Recorder(),
        tmp_path / "audit.jsonl",
        session_timeline=timeline,
        aliases=aliases,
    )

    async def op() -> None:
        return None

    async with app.run_test():
        result = await app._writes.run(
            "delete", _OLM_SUBS_META, "default", "database", lambda: op()
        )
    assert result == "done"
    entry = timeline.snapshot(epoch=0, source=TimelineSource.WRITE, resource=None).entries[0]
    assert entry.resource is not None
    assert entry.resource.kind_alias == "subscriptions.operators.coreos.com"
    # Drive the watch delta through the installed sink itself - the
    # controller's `record_watch_event` (issue #282 Task 3) - rather than a
    # removed app-private method.
    assert app.watch_manager.on_event is not None
    app.watch_manager.on_event(
        "sub",
        "default",
        "ADDED",
        GenericSummary(
            name="database",
            namespace="default",
            kind="Subscription",
            created="",
            uid="sub-uid",
        ),
    )
    resource = TimelineResourceRef(
        kind_alias="subscriptions.operators.coreos.com",
        display_kind="Subscription",
        namespace="default",
        name="database",
    )
    matching = timeline.snapshot(epoch=0, source=None, resource=resource).entries
    assert [item.source for item in matching] == [
        TimelineSource.WRITE,
        TimelineSource.WRITE,
        TimelineSource.WATCH,
    ]


async def test_blocked_intent_does_not_record_write_timeline(tmp_path: Path) -> None:
    """Auditing is fail-closed: an intent that could not be persisted blocks
    the write, so the timeline must not show one that looks like it ran."""
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    audit_path = tmp_path / "audit.jsonl"
    audit_path.mkdir()  # directory makes appends fail → intent blocked

    async def op() -> None:
        raise AssertionError("must not run")

    app = make_app(Recorder(), audit_path, session_timeline=timeline)
    async with app.run_test():
        result = await app._writes.run("delete", _PODS_META, "default", "web-1", lambda: op())
    assert "blocked" in result
    assert timeline.snapshot(epoch=None, source=TimelineSource.WRITE, resource=None).entries == ()


async def test_failed_write_records_the_error_outcome_it_audited(tmp_path: Path) -> None:
    """A write that reached the API and failed is part of the session
    history; the timeline outcome must match the audited one."""
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    app = make_app(Recorder(fail_status=403), tmp_path / "audit.jsonl", session_timeline=timeline)

    async with app.run_test():
        result = await app._writes.run(
            "delete",
            _PODS_META,
            "default",
            "web-1",
            lambda: app._write_ops.delete_object(_PODS_META, "default", "web-1"),  # type: ignore[union-attr]  # wired above
        )
    assert "failed" in result
    actions = _write_records(timeline)
    assert actions[0] == ("delete", "intent")
    assert actions[1][0] == "delete"
    assert actions[1][1].startswith("error:")
