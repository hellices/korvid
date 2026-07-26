"""TUI write keybindings behind approval dialogs (issue #16, spec §5 #4).

Ctrl-D = delete, r = rollout restart, S = scale. Every path goes through a
ConfirmScreen; --readonly disables all of them; executed writes land in the
audit log.
"""

import asyncio
import copy
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.writes import WriteOps
from korvid.ui.app import KorvidApp, _yaml_equal
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ReplicasPrompt
from korvid.ui.widgets.resource_table import ResourceTable

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_NODES_META = ResourceMeta("Node", "nodes", "", "v1", False, ("no",))

_ALIASES = {
    "pods": _PODS_META,
    "deployments": _DEPLOY_META,
    "nodes": _NODES_META,
}


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
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
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
        aliases=dict(_ALIASES),
        get_manifest=get_manifest,  # type: ignore[arg-type]  # tests pass duck-typed callables
        edit_text=edit_text,  # type: ignore[arg-type]  # tests pass duck-typed callables
        write_ops=recorder,
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
    rec = Recorder(fail_status=403)
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

    async def slow_check(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        await release.wait()
        return True

    app._check_permission = slow_check
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("ctrl+d")  # handler now awaits the stalled pre-check
        await pilot.pause(0.1)
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

    monkeypatch.setattr("korvid.ui.app._PERMISSION_CHECK_TIMEOUT", 0.1)
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl", permitted=True)

    async def stall(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        await asyncio.Event().wait()  # never resolves
        return True

    app._check_permission = stall
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        # The input driver timestamps keys on arrival: one typed during the
        # stalled check predates the dialog, which only exists afterwards.
        stale = events.Key("y", "y")
        await pilot.press("ctrl+d")  # stalls, then fails open into the dialog
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        app.screen.post_message(stale)
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)  # discarded: still open
        assert rec.calls == []
        await pilot.press("y")  # a fresh keystroke still confirms
        await pilot.pause(0.2)
        assert rec.calls == [("delete", "pods", "default", "web-1")]


async def test_delete_binds_selected_row_uid(tmp_path: Path) -> None:
    """The uid of the row the user selected rides along as a delete
    precondition, so the approval cannot land on a same-named replacement
    created while the dialog was open."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("ctrl+d")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _until(pilot, lambda: rec.calls)
        assert rec.uids == ["pod-uid-1"]


async def test_scale_binds_selected_row_uid(tmp_path: Path) -> None:
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
        assert rec.uids == ["deploy-uid-1"]


async def test_conflict_reports_target_changed_since_approval(tmp_path: Path) -> None:
    """A 409 (uid precondition tripped: the object was deleted and recreated
    after approval) surfaces as an actionable message, not a bare API error."""
    rec = Recorder(fail_status=409)
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("ctrl+d")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _until(
            pilot,
            lambda: any("changed since it was approved" in n.message for n in app._notifications),
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
    edited: str | None | Callable[[str], str],
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _until(pilot, lambda: audit_path.exists() and "success" in audit_path.read_text())
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: bool(seen))
    assert "managedFields" not in seen[0]
    assert "resourceVersion" in seen[0]


async def test_e_edit_cancelled_editor_makes_no_call(tmp_path: Path) -> None:
    get_manifest, edit_text, _ = _edit_fixtures(None)
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: any("cancelled" in n.message for n in app._notifications))
    assert rec.calls == []


async def test_e_edit_unchanged_text_is_a_noop(tmp_path: Path) -> None:
    get_manifest, edit_text, _ = _edit_fixtures(lambda text: text)
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: any("no changes" in n.message for n in app._notifications))
        assert not isinstance(app.screen, ConfirmScreen)
    assert rec.calls == []


async def test_e_edit_invalid_yaml_aborts(tmp_path: Path) -> None:
    get_manifest, edit_text, _ = _edit_fixtures("{invalid: [yaml")
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: any("invalid YAML" in n.message for n in app._notifications))
    assert rec.calls == []


async def test_e_edit_non_mapping_yaml_aborts(tmp_path: Path) -> None:
    get_manifest, edit_text, _ = _edit_fixtures("- just\n- a\n- list\n")
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: any("not a mapping" in n.message for n in app._notifications))
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _until(pilot, lambda: audit_path.exists() and "success" in audit_path.read_text())
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: any("Read-only" in n.message for n in app._notifications))
    assert seen == []
    assert rec.calls == []


async def test_e_edit_without_manifest_source_notifies(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: any("unavailable" in n.message for n in app._notifications))
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: any("no changes" in n.message for n in app._notifications))
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _until(pilot, lambda: audit_path.exists() and "success" in audit_path.read_text())
    manifest = rec.calls[0][4]
    assert isinstance(manifest, dict)
    assert manifest["metadata"] == {"resourceVersion": "41"}


async def test_external_editor_invocation_failure_notifies_and_cancels(tmp_path: Path) -> None:
    """Review: a broken $EDITOR (malformed quoting / missing executable) must
    abort with a notification instead of an unhandled action error."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        with mock.patch.dict(os.environ, {"VISUAL": "bad 'quote", "EDITOR": ""}):
            result = await app._edit_in_external_editor("a: 1\n")
        assert result is None
        await _until(pilot, lambda: any("editor" in n.message for n in app._notifications))
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _until(pilot, lambda: audit_path.exists() and "success" in audit_path.read_text())
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        operation = str(app.screen.query_one(".confirm-operation").render())
        assert "spec" in operation  # the type change is named in the summary
        await pilot.press("y")
        await _until(pilot, lambda: audit_path.exists() and "success" in audit_path.read_text())
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await _until(pilot, lambda: audit_path.exists() and "success" in audit_path.read_text())
    assert len(rec.calls) == 1


async def test_e_edit_non_string_top_level_key_aborts(tmp_path: Path) -> None:
    """Review round 5: a YAML mapping can legally have a non-string top-level
    key; sorting it against string keys in the summary raised TypeError."""
    get_manifest, edit_text, _ = _edit_fixtures("1: value\nspec: {}\n")
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl", get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: any("non-string" in n.message for n in app._notifications))
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(pilot, lambda: bool(calls))
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(
            pilot,
            lambda: any(
                "selection changed during the editor session" in n.message
                for n in app._notifications
            ),
        )
        assert not isinstance(app.screen, ConfirmScreen)
    assert rec.calls == []


async def test_external_editor_mkstemp_failure_notifies_and_cancels(tmp_path: Path) -> None:
    """Review round 6 (suppressed): a full/unavailable temp dir must abort
    with a notification instead of raising out of the action."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "a.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        with mock.patch("tempfile.mkstemp", side_effect=OSError("no space")):
            result = await app._edit_in_external_editor("a: 1\n")
        assert result is None
        await _until(
            pilot, lambda: any("temp file failed" in n.message for n in app._notifications)
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
        await pilot.pause(0.1)
        with (
            mock.patch.dict(os.environ, {"VISUAL": "true", "EDITOR": ""}),
            mock.patch("subprocess.call", side_effect=write_binary),
            mock.patch.object(type(app), "suspend", mock.MagicMock()) as fake_suspend,
        ):
            fake_suspend.return_value.__enter__ = mock.MagicMock(return_value=None)
            fake_suspend.return_value.__exit__ = mock.MagicMock(return_value=False)
            result = await app._edit_in_external_editor("a: 1\n")
        assert result is None
        await _until(pilot, lambda: any("unreadable" in n.message for n in app._notifications))
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
        await pilot.pause(0.1)
        with mock.patch.dict(os.environ, {"VISUAL": "   ", "EDITOR": ""}):
            result = await app._edit_in_external_editor("a: 1\n")
        assert result is None
        await _until(pilot, lambda: any("editor" in n.message for n in app._notifications))
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
        await pilot.pause(0.1)
        await pilot.press("e")
        await _until(
            pilot,
            lambda: any(
                "selection changed during the manifest fetch" in n.message
                for n in app._notifications
            ),
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
        await pilot.pause(0.1)
        with mock.patch.dict(os.environ, {"VISUAL": "true", "EDITOR": ""}):
            result = await app._edit_in_external_editor("a: 1\n")
        assert result is None
        await _until(
            pilot,
            lambda: any("does not support" in n.message for n in app._notifications),
        )
    assert rec.calls == []
