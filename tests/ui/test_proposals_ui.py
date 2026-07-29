"""External MCP write proposals in the TUI (issue #110): submission never
mutates or steals focus; only a fresh keystroke in the proposal review
dialog can execute; every terminal outcome is distinct and audited.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary
from korvid.k8s.writes import WriteOps
from korvid.tools.proposals import ProposalStore
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen

from .waits import until

_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_ALIASES = {"deployments": _DEPLOY_META, "deploy": _DEPLOY_META}


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
    store: ProposalStore | None,
    *,
    permitted: bool = True,
    readonly: bool = False,
    uid: str | None = "uid-1",
) -> KorvidApp:
    resource_store = ResourceStore()
    deploys = [GenericSummary(name="web", namespace="default", kind="Deployment", created="")]

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in deploys if kind == "deployments" else []:
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def check_permission(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        return permitted

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return {"metadata": {"uid": uid}}

    return KorvidApp(
        config=KorvidConfig(namespace="default", readonly=readonly, kube_context="ctx-a"),
        store=resource_store,
        watch_manager=WatchManager(resource_store, source),
        aliases=dict(_ALIASES),
        get_manifest=get_manifest,
        write_ops=recorder,
        audit=AuditLog(audit_path),
        check_permission=check_permission,
        proposal_store=store,
    )


async def _submit(app: KorvidApp) -> str:
    return await app.agent_submit_write_proposal(
        "delete",
        "deployments",
        "web",
        "default",
        session_id="sess-1",
        client_name="claude-code",
        client_version="1.0",
    )


# --- submission -----------------------------------------------------------


async def test_submit_without_a_store_is_rejected(tmp_path: Path) -> None:
    app = make_app(Recorder(), tmp_path / "a.jsonl", None)
    async with app.run_test():
        result = await _submit(app)
    assert result.startswith("ERROR:")
    assert "not enabled" in result


async def test_submit_queues_a_pending_proposal_without_mutating(tmp_path: Path) -> None:
    rec = Recorder()
    store = ProposalStore()
    app = make_app(rec, tmp_path / "a.jsonl", store)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        result = await _submit(app)
        assert not isinstance(app.screen, ConfirmScreen)  # no modal, no focus steal
        pending = store.pending()  # read before shutdown expires the queue
    assert result.startswith("proposal ")
    assert rec.calls == []
    assert len(pending) == 1
    p = pending[0]
    assert (p.action, p.kind, p.namespace, p.name) == ("delete", "deployments", "default", "web")
    assert p.uid == "uid-1"
    assert p.context == "ctx-a"
    assert p.session_id == "sess-1"
    assert p.client_name == "claude-code"
    assert p.id in result


async def test_submit_readonly_is_rejected(tmp_path: Path) -> None:
    store = ProposalStore()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store, readonly=True)
    async with app.run_test():
        result = await _submit(app)
    assert result.startswith("ERROR:")
    assert store.pending() == []


async def test_submit_without_permission_is_rejected(tmp_path: Path) -> None:
    store = ProposalStore()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store, permitted=False)
    async with app.run_test():
        result = await _submit(app)
    assert result.startswith("ERROR: missing permission")
    assert store.pending() == []


async def test_submit_unknown_kind_is_rejected(tmp_path: Path) -> None:
    store = ProposalStore()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store)
    async with app.run_test():
        result = await app.agent_submit_write_proposal(
            "delete", "gadgets", "web", "default", session_id="s"
        )
    assert result.startswith("ERROR:")
    assert store.pending() == []


# --- status / cancel tools -------------------------------------------------


async def test_get_write_proposal_reports_state(tmp_path: Path) -> None:
    store = ProposalStore()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store)
    async with app.run_test():
        await _submit(app)
        pid = store.pending()[0].id
        line = await app.agent_get_write_proposal(pid)
        assert pid in line
        assert "pending" in line
        missing = await app.agent_get_write_proposal("nope")
    assert missing.startswith("ERROR:")


async def test_cancel_is_bound_to_the_submitting_session(tmp_path: Path) -> None:
    store = ProposalStore()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store)
    async with app.run_test():
        await _submit(app)
        pid = store.pending()[0].id
        stranger = await app.agent_cancel_write_proposal(pid, session_id="other")
        assert stranger.startswith("ERROR:")
        owner = await app.agent_cancel_write_proposal(pid, session_id="sess-1")
        assert "cancelled" in owner
    assert store.pending() == []


# --- review / approval ------------------------------------------------------


async def test_review_approve_executes_with_the_bound_uid(tmp_path: Path) -> None:
    rec = Recorder()
    store = ProposalStore()
    audit_path = tmp_path / "a.jsonl"
    app = make_app(rec, audit_path, store)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _submit(app)
        pid = store.pending()[0].id
        app._open_proposal_review()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await until(pilot, lambda: rec.calls != [])
    assert rec.calls == [("delete", "deployments", "default", "web")]
    assert rec.uids == ["uid-1"]
    found = store.get(pid)
    assert found is not None
    assert found[1] == "executed"
    entries = [json.loads(line) for line in audit_path.read_text().splitlines()]
    details = " ".join(e.get("detail", "") for e in entries)
    assert "external_mcp" in details
    assert pid in details
    assert "claude-code" in details


async def test_review_decline_denies_without_mutating(tmp_path: Path) -> None:
    rec = Recorder()
    store = ProposalStore()
    app = make_app(rec, tmp_path / "a.jsonl", store)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _submit(app)
        pid = store.pending()[0].id
        app._open_proposal_review()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
        await until(pilot, lambda: store.get(pid) is not None and store.get(pid)[1] != "pending")  # type: ignore[index]  # guarded above
    assert rec.calls == []
    found = store.get(pid)
    assert found is not None
    assert found[1] == "denied"


async def test_review_expires_a_stale_context_epoch_without_a_dialog(tmp_path: Path) -> None:
    rec = Recorder()
    store = ProposalStore()
    app = make_app(rec, tmp_path / "a.jsonl", store)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _submit(app)
        pid = store.pending()[0].id
        app._ctx_epoch += 1  # a context switch landed after submission
        app._open_proposal_review()
        await until(pilot, lambda: store.get(pid) is not None and store.get(pid)[1] != "pending")  # type: ignore[index]  # guarded above
        assert not isinstance(app.screen, ConfirmScreen)
    assert rec.calls == []
    found = store.get(pid)
    assert found is not None
    assert found[1] == "expired"


async def test_review_uid_change_fails_the_proposal(tmp_path: Path) -> None:
    rec = Recorder()
    store = ProposalStore()
    app = make_app(rec, tmp_path / "a.jsonl", store)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _submit(app)
        pid = store.pending()[0].id

        async def replaced(kind: str, ns: str | None, name: str) -> dict[str, Any]:
            return {"metadata": {"uid": "uid-2"}}

        app._get_manifest = replaced  # the target was deleted and recreated
        app._open_proposal_review()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await until(pilot, lambda: store.get(pid) is not None and store.get(pid)[1] != "approved")  # type: ignore[index]  # guarded above
    assert rec.calls == []
    found = store.get(pid)
    assert found is not None
    assert found[1] == "failed"
    assert "replaced" in found[2]


# --- indicator / invalidation ----------------------------------------------


async def test_pending_proposals_show_in_the_status_bar(tmp_path: Path) -> None:
    store = ProposalStore()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert app._proposals_label() == ""
        await _submit(app)
        label = app._proposals_label()
        assert "claude-code" in label
        assert "delete" in label
        assert ":proposals" in label
        await until(pilot, lambda: "proposal" in str(app._status_bar.render()))


async def test_shutdown_expires_pending_proposals(tmp_path: Path) -> None:
    store = ProposalStore()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _submit(app)
        pid = store.pending()[0].id
    found = store.get(pid)
    assert found is not None
    assert found[1] == "expired"
