"""Agent-initiated writes surface the same ConfirmScreen the user must
approve with a real keystroke (issue #16, spec §6.2): the agent can only
*request*; approval happens in the TUI.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

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
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.pick_screen import PickScreen

from .waits import until

_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_ALIASES = {"deployments": _DEPLOY_META, "deploy": _DEPLOY_META}


def _expand_panel(app: KorvidApp) -> None:
    # Approval dialogs only surface while the panel is expanded (spec 6.1);
    # tests that reach the dialog must open the panel first.
    app.query_one(AgentPanel).display = True


class Recorder(WriteOps):
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.uids: list[str | None] = []

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
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
        self.uids.append(uid)
        self.calls.append(("replace", meta.plural, namespace, name, manifest))


def make_app(
    recorder: Recorder,
    audit_path: Path,
    *,
    readonly: bool = False,
    permitted: bool | None = None,
    get_manifest: Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None = None,
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
        get_manifest=get_manifest,
        write_ops=recorder,
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
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
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
        # the audit record carries the full GVR of the mutated resource
        assert (entry["group"], entry["version"], entry["kind"]) == ("apps", "v1", "deployments")


async def test_agent_delete_denied_by_user_key(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
        result = await task
        assert "denied" in result.lower() or "declined" in result.lower()
        assert rec.calls == []


async def test_agent_write_rejects_same_plural_custom_group(tmp_path: Path) -> None:
    """A custom-group CRD whose plural collides with apps/deployments must not
    be treated as an apps/* workload: eligibility keys on (group, plural)."""
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    app.aliases["xdeploy"] = ResourceMeta(
        "Deployment", "deployments", "example.io", "v1", True, ("xdeploy",)
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        scaled = await app.agent_request_write(
            "scale", "xdeploy", "web", namespace="default", replicas=2
        )
        assert scaled == "ERROR: scale does not apply to deployments.example.io"
        restarted = await app.agent_request_write(
            "rollout_restart", "xdeploy", "web", namespace="default"
        )
        assert restarted == "ERROR: rollout restart does not apply to deployments.example.io"
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
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
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


async def test_stalled_permission_check_times_out_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung authorization endpoint must never hang the agent turn: the
    pre-check is bounded and fails open into the normal approval gate."""
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
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        # reaching the dialog proves the stalled check timed out fail-open
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        result = await task
        assert result.startswith("approved and executed")


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


async def test_agent_write_times_out_as_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unanswered approval dialog resolves as expired (never hangs the
    agent turn, and never claims the user declined), executes nothing,
    audits nothing, and clears the dialog."""
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
        # On a slow runner the 0.2s window can open AND expire between two
        # 0.05s polls, so waiting on the ConfirmScreen alone is a race:
        # accept task completion as the equally-valid observable outcome.
        # (Dialog surfacing itself is covered by the other tests here.)
        await until(pilot, lambda: task.done() or isinstance(app.screen, ConfirmScreen))
        result = await task  # no keystroke; the timeout resolves it as expired
        assert "expired" in result.lower()
        assert "declined" not in result.lower()
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
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
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
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        result = await task
        assert "executed" in result.lower()
        assert rec.calls == [("delete", "deployments", "default", "web")]


async def test_agent_write_collapsed_panel_times_out_as_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that is never surfaced (the panel stays collapsed) resolves
    as expired after the approval window without ever pushing a modal."""
    monkeypatch.setattr("korvid.ui.app._APPROVAL_TIMEOUT", 0.3)
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        result = await app.agent_request_write("delete", "deployments", "web", namespace="default")
        assert "expired" in result.lower()
        assert "declined" not in result.lower()
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
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
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


async def test_agent_write_normalizes_whitespace_name(tmp_path: Path) -> None:
    """' web ' must delete, permission-check, and audit the same 'web': a
    mismatch would break the exact-target safety record."""
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "  web  ", namespace="default")
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        result = await task
        assert "deployments.apps/web" in result
        assert rec.calls == [("delete", "deployments", "default", "web")]
        entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
        assert all(e["name"] == "web" for e in entries)


async def test_agent_write_executes_with_exact_validated_meta(tmp_path: Path) -> None:
    """Alias maps are first-wins across API groups: the executed operation
    must use the exact ResourceMeta that was validated and permission-checked,
    never one re-resolved from the plural string."""
    seen: list[ResourceMeta] = []

    class MetaRecorder(Recorder):
        async def delete_object(
            self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
        ) -> None:
            seen.append(meta)
            await super().delete_object(meta, namespace, name, uid=uid)

    rec = MetaRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await task
        assert seen == [_DEPLOY_META]
        assert seen[0] is _DEPLOY_META  # identity, not a lookalike from another group


async def test_blocked_audit_result_omits_local_path(tmp_path: Path) -> None:
    """An audit failure message can embed the local log path (and therefore
    the user's home directory): the tool result goes to the LLM provider,
    so it must stay generic."""
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    audit_path.mkdir()  # a directory at the log path makes appends fail
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        result = await task
        assert "blocked: audit log unavailable" in result
        assert str(tmp_path) not in result  # no filesystem details leak
        assert rec.calls == []


async def test_write_403_reports_actionable_permission_message(tmp_path: Path) -> None:
    """The SSAR pre-check fails open and permissions can change mid-flight:
    a 403 from the actual mutation must keep the actionable RBAC contract
    ('missing permission: {verb} {resource}'), not a bare 'API 403'."""
    from korvid.k8s.errors import ApiStatusError

    rec = Recorder()

    async def forbidden(
        meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        raise ApiStatusError(403, "Forbidden")

    rec.delete_object = forbidden  # type: ignore[method-assign]  # simulate a mid-flight RBAC change
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        result = await task
        assert "missing permission: delete deployments" in result
        assert "403" not in result
        entry = json.loads(audit_path.read_text().splitlines()[-1])
        assert entry["outcome"].startswith("error")  # the failure is still audited


async def test_agent_write_expired_budget_never_grants_extra_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expiry contract is exact: if surfacing the dialog consumed the
    whole budget, the request expires instead of granting a minimum
    approval window past the deadline."""
    monkeypatch.setattr("korvid.ui.app._APPROVAL_TIMEOUT", 0.0)
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        result = await app.agent_request_write("delete", "deployments", "web", namespace="default")
        assert "expired" in result
        await pilot.pause(0.1)
        assert not isinstance(app.screen, ConfirmScreen)  # nothing lingers
        assert rec.calls == []


async def test_agent_write_binds_target_uid_as_precondition(tmp_path: Path) -> None:
    """The uid fetched at request time rides along to the executed write, so
    the approval is bound to the exact object incarnation - a same-named
    replacement created while the dialog is open gets a 409 from the API
    server instead of the mutation."""
    rec = Recorder()

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return {"metadata": {"uid": "deploy-uid-7"}}

    app = make_app(rec, tmp_path / "audit.jsonl", get_manifest=get_manifest)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        result = await task
        assert "executed" in result
        assert rec.uids == ["deploy-uid-7"]


async def test_agent_write_missing_target_errors_before_dialog(tmp_path: Path) -> None:
    """A 404 on the uid lookup means the target does not exist: the agent
    gets an actionable error and the user is never shown a dialog for it."""
    rec = Recorder()

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        raise ApiStatusError(404, "Not Found")

    app = make_app(rec, tmp_path / "audit.jsonl", get_manifest=get_manifest)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        result = await app.agent_request_write(
            "delete", "deployments", "ghost", namespace="default"
        )
        assert result.startswith("ERROR:")
        assert "not found" in result
        await pilot.pause(0.1)
        assert not isinstance(app.screen, ConfirmScreen)
        assert rec.calls == []


async def test_agent_uid_lookup_uses_validated_alias(tmp_path: Path) -> None:
    """The uid lookup resolves through the same alias the write was validated
    with - not meta.plural, whose first-wins resolution could address a
    different resource when plurals collide across groups."""
    rec = Recorder()
    kinds: list[str] = []

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        kinds.append(kind)
        return {"metadata": {"uid": "u-1"}}

    app = make_app(rec, tmp_path / "audit.jsonl", get_manifest=get_manifest)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "Deploy", "web", namespace="default")
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await task
    # Every manifest lookup (uid + ownership banner) resolves through the
    # caller's alias, normalized — not "deployments" via meta.plural.
    assert kinds == ["deploy", "deploy"]


async def test_uid_lookup_times_out_fail_open(tmp_path: Path) -> None:
    """A stalled manifest lookup must not hang the caller past the approval
    deadline: the uid lookup is bounded by _UID_LOOKUP_TIMEOUT and fails open
    (None -> no precondition) like other infrastructure failures."""
    from unittest.mock import patch

    started = asyncio.Event()

    async def hanging(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        started.set()
        await asyncio.Event().wait()  # never resolves
        return {}

    app = make_app(Recorder(), tmp_path / "audit.jsonl", get_manifest=hanging)
    with patch("korvid.ui.app._UID_LOOKUP_TIMEOUT", 0.05):
        assert await app._target_uid("pods", "default", "api-1") is None
    assert started.is_set()  # the lookup really ran and was cancelled by the bound


async def test_agent_write_dialog_shows_the_ownership_banner(tmp_path: Path) -> None:
    """Agent-requested writes go through the same ConfirmScreen — the
    ownership banner covers them too (issue #119)."""

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return {
            "metadata": {
                "name": name,
                "namespace": ns,
                "uid": "deploy-uid-1",
                "labels": {"olm.owner": "kafka-operator.v0.38.0"},
            }
        }

    app = make_app(Recorder(), tmp_path / "audit.jsonl", get_manifest=get_manifest)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("delete", "deployments", "web", namespace="default")
        )
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        banner = app.screen.query_one(".confirm-managed")
        assert "kafka-operator.v0.38.0" in str(banner.render())
        await pilot.press("escape")
        await task
