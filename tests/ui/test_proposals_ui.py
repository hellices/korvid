"""External MCP write proposals in the TUI (issue #110): submission never
mutates or steals focus; only a fresh keystroke in the proposal review
dialog can execute; every terminal outcome is distinct and audited.
"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.mcp import MCPControllerBase
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary
from korvid.k8s.writes import WriteOps
from korvid.tools.proposals import ProposalClosedError, ProposalStore
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
    mcp: MCPControllerBase | None = None,
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
        mcp=mcp,
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
    async with app.run_test():
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
        assert app._proposals_label() == ""
        await _submit(app)
        label = app._proposals_label()
        assert "claude-code" in label
        assert "delete" in label
        assert ":proposals" in label
        await until(pilot, lambda: "proposal" in str(app._status_bar.render()))


async def test_multiple_pending_proposals_keep_source_and_target_in_the_label(
    tmp_path: Path,
) -> None:
    """The persistent indicator names source and target; queueing a second
    proposal must not reduce it to a bare count — the next reviewable
    proposal's caller and target stay visible."""
    store = ProposalStore()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store)
    async with app.run_test():
        await _submit(app)
        await app.agent_submit_write_proposal(
            "delete",
            "deployments",
            "web-2",
            "default",
            session_id="sess-2",
            client_name="other-agent",
            client_version="1.0",
        )
        label = app._proposals_label()
    assert "2 proposals" in label
    assert "claude-code" in label
    assert "deployments/web" in label
    assert ":proposals" in label


async def test_shutdown_expires_pending_proposals(tmp_path: Path) -> None:
    store = ProposalStore()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store)
    async with app.run_test():
        await _submit(app)
        pid = store.pending()[0].id
    found = store.get(pid)
    assert found is not None
    assert found[1] == "expired"


# --- review round 1 hardening ------------------------------------------------


class FakeMCP(MCPControllerBase):
    """Minimal lifecycle double for the `:mcp` toggle tests."""

    def __init__(self, *, running: bool) -> None:
        self.is_on = running

    @property
    def running(self) -> bool:
        return self.is_on

    def status(self) -> str:
        return "MCP on" if self.is_on else "MCP off"

    async def start(self) -> str:
        if self.is_on:
            return self.status()  # idempotent: no new server run, no new token
        self.is_on = True
        return "MCP on"

    async def stop(self) -> str:
        self.is_on = False
        return "MCP off"

    async def shutdown(self) -> None:
        self.is_on = False


async def test_submit_fails_closed_when_the_target_uid_cannot_be_captured(
    tmp_path: Path,
) -> None:
    """A proposal without a UID binding could mutate a same-named
    replacement; unlike the interactive path, submission must fail closed."""
    store = ProposalStore()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store, uid=None)
    async with app.run_test():
        result = await _submit(app)
        pending = store.pending()
    assert result.startswith("ERROR:")
    assert pending == []


async def test_context_switch_after_the_approval_claim_fails_the_proposal(
    tmp_path: Path,
) -> None:
    """begin_execution can win just before a context switch; the epoch must
    be rechecked after the claim, immediately before mutation."""
    rec = Recorder()
    store = ProposalStore()
    app = make_app(rec, tmp_path / "a.jsonl", store)
    async with app.run_test():
        await _submit(app)
        proposal = store.pending()[0]
        rebuilt = app._rebuild_proposal_op(proposal)
        assert not isinstance(rebuilt, str)
        meta, ns, op, _operation, _detail = rebuilt
        assert store.begin_execution(proposal.id)
        app._ctx_epoch += 1  # a context switch raced the approval
        await app._execute_proposal(store, proposal, meta, ns, op)
        found = store.get(proposal.id)
    assert rec.calls == []
    assert found is not None
    assert found[1] == "failed"
    assert "context" in found[2]


async def test_review_dialog_shows_the_proposal_safety_bindings(tmp_path: Path) -> None:
    """Issue #110: the dialog must show the bound context, UID, expiry, and
    label the caller as untrusted metadata before approval."""
    store = ProposalStore()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store)
    async with app.run_test() as pilot:
        await _submit(app)
        app._open_proposal_review()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        body = str(app.screen.query_one(".confirm-operation", Static).render())
        await pilot.press("n")
    assert "ctx-a" in body
    assert "uid-1" in body
    assert "expires" in body
    assert "untrusted" in body


async def test_denied_and_cancelled_outcomes_are_audited_with_provenance(
    tmp_path: Path,
) -> None:
    rec = Recorder()
    store = ProposalStore()
    audit_path = tmp_path / "a.jsonl"
    app = make_app(rec, audit_path, store)
    async with app.run_test() as pilot:
        await _submit(app)
        denied_id = store.pending()[0].id
        app._open_proposal_review()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
        await until(pilot, lambda: (f := store.get(denied_id)) is not None and f[1] == "denied")
        await _submit(app)
        cancelled_id = store.pending()[0].id
        await app.agent_cancel_write_proposal(cancelled_id, session_id="sess-1")
    assert rec.calls == []
    entries = [json.loads(line) for line in audit_path.read_text().splitlines()]
    outcomes = {e["outcome"]: e["detail"] for e in entries}
    denied = next(v for k, v in outcomes.items() if "denied" in k)
    assert denied_id in denied
    assert "external_mcp" in denied
    cancelled = next(v for k, v in outcomes.items() if "cancelled" in k)
    assert cancelled_id in cancelled


async def test_mcp_on_when_already_running_keeps_pending_proposals(tmp_path: Path) -> None:
    """`:mcp on` on an already-running server is a status query: no new
    server run, no new capability token, so pending work must survive."""
    store = ProposalStore()
    mcp = FakeMCP(running=True)
    app = make_app(Recorder(), tmp_path / "a.jsonl", store, mcp=mcp)
    async with app.run_test() as pilot:
        await _submit(app)
        pid = store.pending()[0].id
        app._handle_mcp_command(["on"])
        await until(pilot, lambda: not any(w.is_running for w in app.workers))
        found = store.get(pid)
    assert found is not None
    assert found[1] == "pending"


async def test_mcp_off_expires_pending_proposals(tmp_path: Path) -> None:
    store = ProposalStore()
    mcp = FakeMCP(running=True)
    app = make_app(Recorder(), tmp_path / "a.jsonl", store, mcp=mcp)
    async with app.run_test() as pilot:
        await _submit(app)
        pid = store.pending()[0].id
        app._handle_mcp_command(["off"])
        await until(pilot, lambda: (f := store.get(pid)) is not None and f[1] != "pending")
        found = store.get(pid)
    assert found is not None
    assert found[1] == "expired"
    assert "restarted" in found[2] or "stopped" in found[2]


async def test_review_escape_leaves_the_proposal_pending(tmp_path: Path) -> None:
    """Esc dismisses the review dialog without deciding: the proposal must
    stay pending (until its own TTL), not be recorded as denied."""
    rec = Recorder()
    store = ProposalStore()
    app = make_app(rec, tmp_path / "a.jsonl", store)
    async with app.run_test() as pilot:
        await _submit(app)
        pid = store.pending()[0].id
        app._open_proposal_review()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
        found = store.get(pid)
        assert found is not None
        assert found[1] == "pending"
    assert rec.calls == []


async def test_terminal_outcome_audits_run_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AuditLog.append does blocking file I/O; best-effort proposal-outcome
    audits must be offloaded (audit.py's contract for async contexts), not
    stall the UI loop."""
    rec = Recorder()
    store = ProposalStore()
    app = make_app(rec, tmp_path / "a.jsonl", store)
    append_threads: list[int] = []
    original_append = AuditLog.append

    def recording_append(self: AuditLog, **kwargs: Any) -> None:
        append_threads.append(threading.get_ident())
        original_append(self, **kwargs)

    monkeypatch.setattr(AuditLog, "append", recording_append)
    loop_thread = threading.get_ident()
    async with app.run_test() as pilot:
        await _submit(app)
        pid = store.pending()[0].id
        app._open_proposal_review()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
        await until(pilot, lambda: (f := store.get(pid)) is not None and f[1] == "denied")
        await until(pilot, lambda: bool(append_threads))
    assert append_threads
    assert all(t != loop_thread for t in append_threads)


async def test_lazy_ttl_expiry_is_audited_with_provenance(tmp_path: Path) -> None:
    """A proposal the lazy TTL sweep expires reaches a terminal state like
    any other: its outcome must land in the audit log, not vanish silently."""
    rec = Recorder()
    clock_now = [1000.0]
    store = ProposalStore(ttl=10.0, clock=lambda: clock_now[0])
    audit_path = tmp_path / "a.jsonl"
    app = make_app(rec, audit_path, store)
    async with app.run_test() as pilot:
        await _submit(app)
        pid = store.pending()[0].id
        clock_now[0] += 11.0
        assert store.pending() == []  # triggers the lazy sweep
        await until(
            pilot,
            lambda: audit_path.exists() and pid in audit_path.read_text(),
        )
    entries = [json.loads(line) for line in audit_path.read_text().splitlines()]
    expired = [e for e in entries if pid in e["detail"] and "expired" in e["outcome"]]
    assert len(expired) == 1
    assert "external_mcp" in expired[0]["detail"]


async def test_submit_during_a_context_switch_is_rejected(tmp_path: Path) -> None:
    """RBAC, UID capture and the dry-run preview were all validated against
    the context at intake start; if a switch lands during those awaits the
    proposal must be rejected, not stamped with the new context/epoch."""
    rec = Recorder()
    store = ProposalStore()
    app = make_app(rec, tmp_path / "a.jsonl", store)
    async with app.run_test():
        original = app._preview_for_action

        async def switching_preview(*args: Any, **kwargs: Any) -> list[str] | None:
            app._ctx_epoch += 1  # a context switch lands mid-intake
            return await original(*args, **kwargs)

        app._preview_for_action = switching_preview  # type: ignore[method-assign]  # test seam
        result = await _submit(app)
        assert result.startswith("ERROR:")
        assert "context" in result
        assert store.pending() == []
    assert rec.calls == []


async def test_oversized_arguments_are_rejected_before_any_cluster_io(tmp_path: Path) -> None:
    """The size bound is untrusted-input validation: it must run before the
    RBAC check, UID lookup, and server dry-run, or a caller can force
    cluster I/O with an arbitrarily large payload."""
    rec = Recorder()
    store = ProposalStore()
    manifest_calls: list[str] = []
    app = make_app(rec, tmp_path / "a.jsonl", store)
    original_uid = app._target_uid

    async def counting_uid(kind: str, ns: str | None, name: str) -> str | None:
        manifest_calls.append(name)
        return await original_uid(kind, ns, name)

    app._target_uid = counting_uid  # type: ignore[assignment]  # test seam
    async with app.run_test():
        result = await app.agent_submit_write_proposal(
            "delete",
            "deployments",
            "web",
            "default",
            resources={"app": {"requests": {"cpu": "1" * 10_000}}},
            session_id="sess-1",
        )
    assert result.startswith("ERROR:")
    assert "exceed" in result
    assert manifest_calls == []  # no UID lookup: rejected before cluster I/O
    assert store.pending() == []


async def test_submit_after_shutdown_began_is_rejected(tmp_path: Path) -> None:
    """on_unmount closes the store before the final expiry sweep: an
    in-flight MCP call landing after the sweep gets an error instead of
    queueing a proposal nobody will ever review, expire, or audit."""
    store = ProposalStore()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store)
    async with app.run_test():
        pass
    # The app has unmounted: the store must refuse new submissions.
    with pytest.raises(ProposalClosedError, match="closed"):
        store.submit(
            action="delete",
            group="apps",
            version="v1",
            kind="deployments",
            namespace="default",
            name="web",
            arguments_json="{}",
            uid="uid-1",
            context="ctx-a",
            context_epoch=0,
            summary="delete",
            preview=(),
            session_id="sess-1",
            client_name="",
            client_version="",
        )


async def test_a_failed_context_switch_still_expires_pending_proposals(tmp_path: Path) -> None:
    """Proposals are invalidated when the committed retarget phase begins:
    if both the target switch and the fallback raise, the old-context
    validation is still stale (the client may be half-retargeted) and no
    proposal may stay reviewable."""
    rec = Recorder()
    store = ProposalStore()
    app = make_app(rec, tmp_path / "a.jsonl", store)
    async with app.run_test():
        await _submit(app)
        pid = store.pending()[0].id

        async def failing_switch(name: str | None) -> Any:
            raise RuntimeError("cluster probe failed")

        app._switch_context = failing_switch
        ok, _applied = await app._retarget_context("ctx-b", "ctx-a")
        assert ok is False
        found = store.get(pid)
        assert found is not None
        assert found[1] == "expired"
    assert rec.calls == []


class SlowStopMCP(FakeMCP):
    """`stop()` times out (the run keeps dying in the background); only the
    teardown task captured at stop time observes completion."""

    def __init__(self) -> None:
        super().__init__(running=True)
        self.late_submit: ProposalStore | None = None
        self._task: asyncio.Task[None] | None = None

    async def stop(self) -> str:
        self._task = asyncio.create_task(self._die())
        return "MCP stopping (cleanup is taking long)"  # is_on stays True

    def pending_task(self) -> asyncio.Task[None] | None:
        return self._task

    async def _die(self) -> None:
        if self.late_submit is not None:
            # An in-flight old-run submission lands while teardown drags on.
            self.late_submit.submit(
                action="delete",
                group="apps",
                version="v1",
                kind="deployments",
                namespace="default",
                name="web",
                arguments_json="{}",
                uid="uid-1",
                context="ctx-a",
                context_epoch=0,
                summary="delete deployments/web",
                preview=(),
                session_id="sess-old",
                client_name="",
                client_version="",
            )
        self.is_on = False


async def test_mcp_off_that_times_out_still_expires_pending_proposals(tmp_path: Path) -> None:
    store = ProposalStore()
    mcp = SlowStopMCP()
    app = make_app(Recorder(), tmp_path / "a.jsonl", store, mcp=mcp)
    async with app.run_test() as pilot:
        await _submit(app)
        pid = store.pending()[0].id
        app._handle_mcp_command(["off"])
        await until(pilot, lambda: (f := store.get(pid)) is not None and f[1] != "pending")
        found = store.get(pid)
    assert found is not None
    assert found[1] == "expired"


async def test_mcp_off_sweeps_again_after_a_late_shutdown(tmp_path: Path) -> None:
    """A submission racing the dragged-out teardown must not outlive its
    server run: a second sweep runs once the old run actually dies."""
    store = ProposalStore()
    mcp = SlowStopMCP()
    mcp.late_submit = store
    app = make_app(Recorder(), tmp_path / "a.jsonl", store, mcp=mcp)
    async with app.run_test() as pilot:
        app._handle_mcp_command(["off"])
        await until(pilot, lambda: not mcp.running and store.pending() == [])
        assert store.pending() == []


class RestartRacingMCP(SlowStopMCP):
    """While the old run's teardown drags on, a racing `:mcp on` completes a
    fresh run and that run accepts a new proposal."""

    async def _die(self) -> None:
        if self.late_submit is not None:
            # The new run's proposal, submitted after the restart.
            self.late_submit.submit(
                action="delete",
                group="apps",
                version="v1",
                kind="deployments",
                namespace="default",
                name="web",
                arguments_json="{}",
                uid="uid-1",
                context="ctx-a",
                context_epoch=0,
                summary="delete deployments/web",
                preview=(),
                session_id="sess-new",
                client_name="",
                client_version="",
            )
        self.is_on = True  # the fresh run is live


async def test_a_restart_racing_the_late_shutdown_keeps_new_run_proposals(
    tmp_path: Path,
) -> None:
    """The final old-run sweep must not expire a proposal that belongs to a
    fresh run started while the old teardown was still dragging on: only a
    server that stayed down gets the unconditional sweep."""
    store = ProposalStore()
    mcp = RestartRacingMCP()
    mcp.late_submit = store
    app = make_app(Recorder(), tmp_path / "a.jsonl", store, mcp=mcp)
    async with app.run_test() as pilot:
        app._handle_mcp_command(["off"])
        await until(pilot, lambda: mcp.running and len(store.pending()) > 0)
        # Wait for the off-worker (and its final sweep decision) to finish.
        await until(pilot, lambda: all(w.is_finished for w in app.workers))
        pending = store.pending()
    assert len(pending) == 1
    assert pending[0].session_id == "sess-new"
