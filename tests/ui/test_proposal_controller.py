"""Direct tests for `ProposalController` — external write proposals
(issue #110 / issue #187 Deep Task 7).

The controller owns everything about an external MCP write proposal that used
to live on `KorvidApp`: the store reference, the submit/get/cancel intake, the
provenance and terminal-outcome audit, the update subscription and the
status-bar label, the one-at-a-time review loop with its approval dialog, the
operation rebuild and every re-validation before execution, the claimed
execution through `WriteCoordinator`, the interrupted-execution settlement,
and the audited expiry sweep the `:ctx` switch, the `:mcp` toggles and unmount
all drive.

It reaches Textual only through `UiSurface` and the narrow ports below, so all
of that is exercised here without a running app. `WriteCoordinator` and
`ProposalStore` are constructed for real, so "no proposal mutation bypasses
the write perimeter" is something these tests observe rather than a claim.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from textual.screen import Screen

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.writes import WriteOps
from korvid.tools.proposals import ProposalStore, WriteProposal
from korvid.ui.agent_ui_controller import AgentProposals, WriteOpBuild
from korvid.ui.proposal_controller import (
    ProposalController,
    ProposalEvents,
    ProposalScreens,
    ReviewTasks,
)
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.workspace_state import WorkspaceState
from korvid.ui.write_coordinator import WriteCoordinator, gvr_label, write_locus

from .test_write_coordinator import BrokenAudit, FakeContext, FakeTimeline, FakeUi, FakeView

_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_NODES_META = ResourceMeta("Node", "nodes", "", "v1", False)

_ALIASES = {"deployments": _DEPLOY_META, "deploy": _DEPLOY_META, "nodes": _NODES_META}


async def until(cond: Callable[[], object], timeout: float = 5.0, label: str = "condition") -> None:
    """Advance the loop until `cond()` is truthy, or fail after `timeout`.

    The proposal flows hop through `asyncio.to_thread` for every audit
    append, so a bare `sleep(0)` spin is not enough; poll the observable
    outcome instead of asserting on wall-clock timing.
    """
    remaining = timeout
    while remaining > 0:
        if cond():
            return
        step = min(0.01, remaining)
        await asyncio.sleep(step)
        remaining -= step
    if cond():
        return
    raise AssertionError(f"{label} not met within {timeout}s")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class Recorder(WriteOps):
    """Records every awaited mutation, with the uid precondition it carried."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.uids: list[str | None] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
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
        self.calls.append(("rollout_restart", meta.plural, namespace, name))

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
        self.calls.append(("replace", meta.plural, namespace, name))


class FakeBuilder:
    """The typed write-op builder the controller depends on (structural).

    Mirrors `AgentUiController`'s contract: validation returns an
    `'ERROR: ...'` string, a success returns the operation *factory* — the
    stored proposal never carries an executable closure, so the rebuild
    happens here at review time.
    """

    def __init__(self, ops: Recorder, *, uid: str | None = "uid-1") -> None:
        self.ops = ops
        self.uid = uid
        self.uid_error: BaseException | None = None
        self.uid_calls: list[tuple[str, str | None, str]] = []
        self.build_error: str | None = None
        self.builds: list[tuple[Any, ...]] = []
        self.preview: list[str] | None = ["- deployments/web"]
        self.preview_calls: list[tuple[Any, ...]] = []

    def build_write_op(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None,
        replicas: int | None,
        resources: dict[str, dict[str, dict[str, str]]] | None,
        *,
        restarted_at: str,
    ) -> WriteOpBuild | str:
        self.builds.append((action, kind, name, namespace, replicas, resources, restarted_at))
        if self.build_error is not None:
            return self.build_error
        meta = _ALIASES.get(kind.strip().lower())
        if meta is None:
            return f"ERROR: unknown kind {kind!r} - not a resource kind in this cluster"
        ns = namespace if meta.namespaced else None
        return (
            meta,
            ns,
            lambda uid: self.ops.delete_object(meta, ns, name, uid=uid),
            f"DELETE {gvr_label(meta)}/{name}{write_locus(ns)}",
            "requested by agent",
        )

    async def preview_for_action(
        self,
        action: str,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        replicas: int | None,
        resources: dict[str, dict[str, dict[str, str]]] | None,
        uid: str | None,
        restarted_at: str,
    ) -> list[str] | None:
        self.preview_calls.append((action, meta.plural, ns, name, uid))
        return list(self.preview) if self.preview is not None else None

    async def target_uid(self, kind_alias: str, ns: str | None, name: str) -> str | None:
        self.uid_calls.append((kind_alias, ns, name))
        if self.uid_error is not None:
            raise self.uid_error
        return self.uid


class FakeScreens(ProposalScreens):
    """Records the dialogs the review loop asks to be popped."""

    def __init__(self) -> None:
        self.dismissed: list[Screen[Any]] = []

    def dismiss_if_current(self, screen: Screen[Any]) -> None:
        self.dismissed.append(screen)


class FakeTasks(ReviewTasks):
    """The review worker, as a plain task so tests can await or cancel it."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[None]] = []
        self.refused = 0

    def review_running(self) -> bool:
        return any(not task.done() for task in self.tasks)

    def start_review(self, coro: Coroutine[Any, Any, None]) -> None:
        self.tasks.append(asyncio.ensure_future(coro))

    async def finish(self) -> None:
        for task in list(self.tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task


class FakeEvents(ProposalEvents):
    """The thread-safe marshaling the app performs with `post_message`."""

    def __init__(self) -> None:
        self.changes = 0
        self.expiries: list[tuple[WriteProposal, str]] = []

    def changed(self) -> None:
        self.changes += 1

    def expired(self, proposal: WriteProposal, reason: str) -> None:
        self.expiries.append((proposal, reason))


class FakeNavigation:
    """The `nav_lock` the `:ctx`/`:mcp`/write coordinators share."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def nav_lock(self) -> asyncio.Lock:
        return self._lock


class Env:
    """A `ProposalController` plus every fake it was built from."""

    def __init__(
        self,
        *,
        tmp_path: Path,
        store: ProposalStore | None = None,
        audit: str = "working",
        uid: str | None = "uid-1",
        permitted: bool = True,
        kube_context: str | None = "ctx-a",
    ) -> None:
        self.ui = FakeUi()
        self.view = FakeView(aliases=_ALIASES)
        self.context = FakeContext()
        self.timeline = FakeTimeline()
        self.screens = FakeScreens()
        self.tasks = FakeTasks()
        self.events = FakeEvents()
        self.navigation = FakeNavigation()
        self.workspace = WorkspaceState("deployments", "default")
        self.ops = Recorder()
        self.builder = FakeBuilder(self.ops, uid=uid)
        self.config = KorvidConfig(namespace="default", kube_context=kube_context)
        self.audit_path = tmp_path / "audit.jsonl"
        self.audit: AuditLog | None
        if audit == "working":
            self.audit = AuditLog(self.audit_path, context="ambient")
        elif audit == "broken":
            self.audit = BrokenAudit(self.audit_path, context="ambient")
        else:
            self.audit = None
        self.permitted = permitted
        self.permission_calls: list[tuple[Any, ...]] = []
        self.status_refreshes = 0
        self.store = store
        self.writes = WriteCoordinator(
            ui=self.ui,
            view=self.view,
            context=self.context,
            audit=lambda: self.audit,
            timeline=self.timeline,
            check_permission=lambda: self._check_permission,
            relationship_loader=lambda: None,
            focused_pane=lambda: self.workspace.focused,
            canonical_meta_kind=lambda meta: meta.plural,
        )
        self.controller = ProposalController(
            store=store,
            ui=self.ui,
            screens=self.screens,
            tasks=self.tasks,
            events=self.events,
            context=self.context,
            writes=self.writes,
            navigation=self.navigation,
            builder=lambda: self.builder,
            config=lambda: self.config,
            audit=lambda: self.audit,
            refresh_status=self._refresh_status,
        )

    async def _check_permission(
        self, verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        self.permission_calls.append((verb, resource, sub, ns, group, name))
        return self.permitted

    def _refresh_status(self) -> None:
        self.status_refreshes += 1

    # -- helpers -----------------------------------------------------------

    async def submit(self, name: str = "web", **kwargs: Any) -> str:
        defaults: dict[str, Any] = {
            "session_id": "sess-1",
            "client_name": "claude-code",
            "client_version": "1.0",
        }
        defaults.update(kwargs)
        return await self.controller.submit_write_proposal(
            "delete", "deployments", name, "default", **defaults
        )

    async def dialog(self, count: int = 1) -> ConfirmScreen:
        """Wait for the review loop to surface its *count*-th approval dialog."""
        await until(lambda: len(self.ui.screens) >= count, label=f"approval dialog {count}")
        screen = self.ui.screens[count - 1][0]
        assert isinstance(screen, ConfirmScreen)
        return screen

    def entries(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text().splitlines()]

    def outcomes(self) -> list[str]:
        return [entry.get("outcome", "") for entry in self.entries()]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path=tmp_path, store=ProposalStore())


@pytest.fixture
def disabled(tmp_path: Path) -> Env:
    return Env(tmp_path=tmp_path, store=None)


# ---------------------------------------------------------------------------
# Intake: submit / get / cancel
# ---------------------------------------------------------------------------


async def test_submit_without_a_store_reports_the_feature_is_disabled(disabled: Env) -> None:
    result = await disabled.submit()
    assert result.startswith("ERROR:")
    assert "not enabled" in result
    assert disabled.builder.builds == []


async def test_submit_queues_a_pending_proposal_without_mutating(env: Env) -> None:
    result = await env.submit()
    assert env.store is not None
    pending = env.store.pending()
    assert len(pending) == 1
    proposal = pending[0]
    assert (proposal.action, proposal.kind, proposal.namespace, proposal.name) == (
        "delete",
        "deployments",
        "default",
        "web",
    )
    assert proposal.uid == "uid-1"
    assert proposal.context == "ctx-a"
    assert proposal.context_epoch == 0
    assert proposal.session_id == "sess-1"
    assert proposal.client_name == "claude-code"
    assert proposal.preview == ("- deployments/web",)
    assert proposal.id in result
    assert env.ops.calls == []
    assert env.ui.screens == []  # no modal, no focus steal


async def test_submit_surfaces_a_build_error_without_touching_the_cluster(env: Env) -> None:
    env.builder.build_error = "ERROR: read-only mode - cluster writes are disabled"
    result = await env.submit()
    assert result == "ERROR: read-only mode - cluster writes are disabled"
    assert env.store is not None
    assert env.store.pending() == []
    assert env.permission_calls == []
    assert env.builder.uid_calls == []


async def test_submit_rejects_oversized_arguments_before_any_cluster_io(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, store=ProposalStore(max_argument_chars=10))
    result = await env.submit()
    assert "exceed 10 characters" in result
    assert env.permission_calls == []
    assert env.builder.uid_calls == []
    assert env.builder.preview_calls == []


async def test_submit_without_permission_is_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, store=ProposalStore(), permitted=False)
    result = await env.submit()
    assert result == "ERROR: missing permission: delete deployments"
    assert env.store is not None
    assert env.store.pending() == []


async def test_submit_fails_closed_when_the_uid_cannot_be_captured(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, store=ProposalStore(), uid=None)
    result = await env.submit()
    assert result.startswith("ERROR: could not verify the write target")
    assert env.store is not None
    assert env.store.pending() == []


async def test_submit_reports_a_missing_target(env: Env) -> None:
    env.builder.uid_error = ApiStatusError(404, "not found")
    result = await env.submit()
    assert result == "ERROR: deployments.apps/web not found in namespace default"
    assert env.store is not None
    assert env.store.pending() == []


async def test_submit_refuses_when_the_context_changed_while_validating(env: Env) -> None:
    async def _switch_mid_validation(*args: Any, **kwargs: Any) -> list[str] | None:
        env.context.value = 7
        return ["- deployments/web"]

    env.builder.preview_for_action = _switch_mid_validation  # type: ignore[method-assign]  # simulating a `:ctx` landing mid-intake
    result = await env.submit()
    assert "the kube context changed while the proposal was being validated" in result
    assert env.store is not None
    assert env.store.pending() == []


async def test_submit_reports_a_closed_store(env: Env) -> None:
    assert env.store is not None
    env.store.close()
    result = await env.submit()
    assert result.startswith("ERROR:")
    assert env.store.pending() == []


async def test_submit_notifies_the_user_that_a_review_is_waiting(env: Env) -> None:
    await env.submit()
    assert any(
        "Write proposal from claude-code" in message and ":proposals" in message
        for message in env.ui.messages()
    )


async def test_get_reports_state_and_rejects_an_unknown_id(env: Env) -> None:
    await env.submit()
    assert env.store is not None
    pid = env.store.pending()[0].id
    line = await env.controller.get_write_proposal(pid)
    assert pid in line
    assert "pending" in line
    assert (await env.controller.get_write_proposal("nope")).startswith("ERROR:")


async def test_get_without_a_store_reports_the_feature_is_disabled(disabled: Env) -> None:
    assert (await disabled.controller.get_write_proposal("p-1")).startswith("ERROR:")


async def test_cancel_is_bound_to_the_submitting_session(env: Env) -> None:
    await env.submit()
    assert env.store is not None
    pid = env.store.pending()[0].id
    stranger = await env.controller.cancel_write_proposal(pid, session_id="other")
    assert stranger.startswith("ERROR:")
    assert env.store.pending() != []
    owner = await env.controller.cancel_write_proposal(pid, session_id="sess-1")
    assert "cancelled" in owner
    assert env.store.pending() == []
    assert env.ops.calls == []
    assert "proposal cancelled: cancelled by caller" in env.outcomes()


async def test_cancel_without_a_store_reports_the_feature_is_disabled(disabled: Env) -> None:
    assert (await disabled.controller.cancel_write_proposal("p-1")).startswith("ERROR:")


async def test_the_controller_is_the_agent_proposals_port(env: Env) -> None:
    assert isinstance(env.controller, AgentProposals)


# ---------------------------------------------------------------------------
# Audit provenance
# ---------------------------------------------------------------------------


async def test_audit_provenance_quotes_untrusted_caller_metadata(env: Env) -> None:
    await env.submit(client_name="evil session=forged", client_version="1 x=y")
    assert env.store is not None
    pid = env.store.pending()[0].id
    await env.controller.cancel_write_proposal(pid, session_id="sess-1")
    detail = " ".join(entry.get("detail", "") for entry in env.entries())
    # The forged `session=` token stays inside the quoted caller value, so
    # the only korvid-owned `session=` field is the real one, at the end.
    assert "caller='evil session=forged'" in detail
    assert "version='1 x=y'" in detail
    assert detail.endswith("session=sess-1")
    assert f"proposal={pid}" in detail
    assert "source=external_mcp" in detail


async def test_outcome_audit_is_bound_to_the_proposals_own_context(env: Env) -> None:
    await env.submit()
    assert env.store is not None
    pid = env.store.pending()[0].id
    env.config = KorvidConfig(namespace="default", kube_context="ctx-b")
    await env.controller.cancel_write_proposal(pid, session_id="sess-1")
    assert [entry["context"] for entry in env.entries()] == ["ctx-a"]


async def test_a_failing_outcome_audit_never_raises(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, store=ProposalStore(), audit="broken")
    await env.submit()
    assert env.store is not None
    pid = env.store.pending()[0].id
    result = await env.controller.cancel_write_proposal(pid, session_id="sess-1")
    assert "cancelled" in result
    assert env.store.pending() == []


# ---------------------------------------------------------------------------
# Status label, subscription and refresh
# ---------------------------------------------------------------------------


async def test_status_label_is_empty_without_a_store_or_pending_work(
    disabled: Env, env: Env
) -> None:
    assert disabled.controller.status_label() == ""
    assert env.controller.status_label() == ""


async def test_status_label_names_the_source_and_the_next_target(env: Env) -> None:
    await env.submit()
    assert env.controller.status_label() == (
        "1 proposal from claude-code: delete deployments/web — :proposals"
    )
    await env.submit("api")
    assert env.controller.status_label() == (
        "2 proposals (next from claude-code: delete deployments/web) — :proposals"
    )


async def test_subscribing_marshals_store_changes_and_ttl_expiry(tmp_path: Path) -> None:
    clock = [0.0]
    store = ProposalStore(ttl=10.0, clock=lambda: clock[0])
    env = Env(tmp_path=tmp_path, store=store)
    env.controller.subscribe()
    await env.submit()
    assert env.events.changes >= 1
    clock[0] = 100.0
    assert store.pending() == []
    assert [reason for _proposal, reason in env.events.expiries] == [
        "proposal expired before review"
    ]


async def test_handling_a_change_repaints_the_status_bar(env: Env) -> None:
    env.controller.handle_changed()
    assert env.status_refreshes == 1


async def test_handling_a_ttl_expiry_audits_the_terminal_outcome(env: Env) -> None:
    await env.submit()
    assert env.store is not None
    proposal = env.store.pending()[0]
    await env.controller.handle_expired(proposal, "proposal expired before review")
    assert "proposal expired: proposal expired before review" in env.outcomes()
    assert env.ops.calls == []


# ---------------------------------------------------------------------------
# Expiry sweeps: `:ctx`, `:mcp`, unmount
# ---------------------------------------------------------------------------


async def test_expire_all_audits_every_pending_proposal(env: Env) -> None:
    await env.submit()
    await env.submit("api")
    await env.controller.expire_all("kube context switched")
    assert env.store is not None
    assert env.store.pending() == []
    assert env.outcomes().count("proposal expired: kube context switched") == 2
    assert env.ops.calls == []


async def test_expire_all_without_a_store_is_a_no_op(disabled: Env) -> None:
    await disabled.controller.expire_all("kube context switched")
    assert disabled.outcomes() == []


async def test_shutdown_closes_the_store_before_the_final_sweep(env: Env) -> None:
    await env.submit()
    await env.controller.shutdown()
    assert env.store is not None
    assert env.store.pending() == []
    assert "proposal expired: the TUI session ended" in env.outcomes()
    late = await env.submit()
    assert late.startswith("ERROR:")


# ---------------------------------------------------------------------------
# Opening the review
# ---------------------------------------------------------------------------


async def test_opening_a_review_without_a_store_explains_the_setting(disabled: Env) -> None:
    disabled.controller.open_review()
    assert any("mcp.write_proposals" in message for message in disabled.ui.messages())
    assert disabled.tasks.tasks == []


async def test_opening_a_review_with_nothing_pending_says_so(env: Env) -> None:
    env.controller.open_review()
    assert "No pending write proposals" in env.ui.messages()
    assert env.tasks.tasks == []


async def test_a_second_review_is_refused_while_one_is_open(env: Env) -> None:
    await env.submit()
    env.controller.open_review()
    await env.dialog()
    env.controller.open_review()
    assert len(env.tasks.tasks) == 1
    assert any("already open" in message for message in env.ui.messages())
    env.ui.answer(None)
    await env.tasks.finish()


# ---------------------------------------------------------------------------
# The review loop: ordering, one-at-a-time decisions, dismissal
# ---------------------------------------------------------------------------


async def test_review_surfaces_the_oldest_proposal_first_one_at_a_time(env: Env) -> None:
    await env.submit("web")
    await env.submit("api")
    env.controller.open_review()
    first = await env.dialog()
    assert "web" in first._title
    assert len(env.ui.screens) == 1
    env.ui.answer(False)
    second = await env.dialog(2)
    assert "api" in second._title
    env.ui.answer(False)
    await env.tasks.finish()
    assert env.store is not None
    assert env.store.pending() == []
    assert env.outcomes().count("proposal denied: denied by user") == 2
    assert env.ops.calls == []


async def test_a_dismissed_dialog_stops_the_loop_and_keeps_the_proposal_pending(env: Env) -> None:
    await env.submit("web")
    await env.submit("api")
    env.controller.open_review()
    await env.dialog()
    env.ui.answer(None)
    await env.tasks.finish()
    assert env.store is not None
    assert len(env.store.pending()) == 2
    assert env.ops.calls == []
    assert env.status_refreshes >= 1


async def test_a_stacked_dialog_refuses_the_review_instead_of_stealing_the_keystroke(
    env: Env,
) -> None:
    await env.submit()
    env.ui.depth = 2
    env.controller.open_review()
    await env.tasks.finish()
    assert env.ui.screens == []
    assert any("Close the current dialog" in message for message in env.ui.messages())
    assert env.store is not None
    assert len(env.store.pending()) == 1


async def test_an_unanswered_dialog_expires_as_a_dismissal(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("korvid.ui.proposal_controller.APPROVAL_TIMEOUT", 0.01)
    await env.submit()
    env.controller.open_review()
    screen = await env.dialog()
    await env.tasks.finish()
    assert env.screens.dismissed == [screen]
    assert env.store is not None
    assert len(env.store.pending()) == 1
    assert env.ops.calls == []


async def test_the_dialog_shows_the_immutable_safety_bindings(env: Env) -> None:
    await env.submit()
    env.controller.open_review()
    screen = await env.dialog()
    body = screen._operation
    assert "caller (untrusted metadata): claude-code 1.0" in body
    assert "bound kube context: ctx-a (epoch 0)" in body
    assert "bound target uid: uid-1" in body
    assert "expires in" in body
    assert screen._preview == ["- deployments/web"]
    env.ui.answer(None)
    await env.tasks.finish()


async def test_a_cluster_scoped_delete_demands_the_typed_name(env: Env) -> None:
    await env.controller.submit_write_proposal(
        "delete", "nodes", "node-1", None, session_id="sess-1"
    )
    env.controller.open_review()
    screen = await env.dialog()
    assert screen._require_name == "node-1"
    env.ui.answer(None)
    await env.tasks.finish()


# ---------------------------------------------------------------------------
# Re-validation before the dialog
# ---------------------------------------------------------------------------


async def test_a_proposal_from_another_context_expires_instead_of_being_shown(env: Env) -> None:
    await env.submit()
    env.context.value = 1
    env.controller.open_review()
    await env.tasks.finish()
    assert env.ui.screens == []
    assert "proposal expired: kube context changed since submission" in env.outcomes()
    assert env.ops.calls == []


async def test_a_proposal_whose_rebuild_fails_expires(env: Env) -> None:
    await env.submit()
    env.builder.build_error = "ERROR: writes disabled - no audit log configured"
    env.controller.open_review()
    await env.tasks.finish()
    assert env.ui.screens == []
    assert "proposal expired: writes disabled - no audit log configured" in env.outcomes()


async def test_unreadable_stored_arguments_expire_the_proposal(env: Env) -> None:
    await env.submit()
    assert env.store is not None
    proposal = env.store.pending()[0]
    object.__setattr__(proposal, "arguments_json", "{not json")
    env.controller.open_review()
    await env.tasks.finish()
    assert env.ui.screens == []
    assert "proposal expired: proposal arguments are unreadable" in env.outcomes()


async def test_a_revoked_permission_fails_the_proposal_before_the_dialog(env: Env) -> None:
    await env.submit()
    env.permitted = False
    env.controller.open_review()
    await env.tasks.finish()
    assert env.ui.screens == []
    assert "proposal failed: permission revoked since submission" in env.outcomes()
    assert env.ops.calls == []


async def test_a_switch_started_during_the_permission_check_stops_the_loop(env: Env) -> None:
    await env.submit()
    original = env._check_permission

    async def _switch_during_ssar(*args: Any, **kwargs: Any) -> bool:
        env.context.is_switching = True
        return await original(*args, **kwargs)

    env._check_permission = _switch_during_ssar  # type: ignore[method-assign]  # a `:ctx` landing while the SSAR is in flight
    env.controller.open_review()
    await env.tasks.finish()
    assert env.ui.screens == []
    assert env.store is not None
    assert len(env.store.pending()) == 1  # the switch's own sweep owns its fate
    assert env.ops.calls == []


async def test_a_proposal_cancelled_during_the_permission_check_is_never_shown(env: Env) -> None:
    await env.submit()
    assert env.store is not None
    store = env.store
    original = env._check_permission

    async def _cancel_during_ssar(*args: Any, **kwargs: Any) -> bool:
        store.cancel(store.pending()[0].id, session_id="sess-1")
        return await original(*args, **kwargs)

    env._check_permission = _cancel_during_ssar  # type: ignore[method-assign]  # a caller cancel racing the review
    env.controller.open_review()
    await env.tasks.finish()
    assert env.ui.screens == []
    assert env.ops.calls == []


# ---------------------------------------------------------------------------
# Approved execution: the write perimeter, exactly once
# ---------------------------------------------------------------------------


async def test_an_approved_proposal_executes_once_through_the_write_coordinator(
    env: Env,
) -> None:
    runs: list[tuple[Any, ...]] = []
    original_run = env.writes.run

    def _counted(*args: Any, **kwargs: Any) -> Any:
        runs.append(args)
        return original_run(*args, **kwargs)

    env.writes.run = _counted  # type: ignore[method-assign]  # counting perimeter entries
    await env.submit()
    assert env.store is not None
    pid = env.store.pending()[0].id
    env.controller.open_review()
    await env.dialog()
    env.ui.answer(True)
    await env.tasks.finish()
    assert len(runs) == 1
    assert env.ops.calls == [("delete", "deployments", "default", "web")]
    assert env.ops.uids == ["uid-1"]  # the mutation carries the bound uid
    found = env.store.get(pid)
    assert found is not None
    assert found[1] == "executed"
    assert "intent" in env.outcomes()
    assert "success" in env.outcomes()
    detail = " ".join(entry.get("detail", "") for entry in env.entries())
    assert f"proposal={pid}" in detail


async def test_a_declined_proposal_never_reaches_the_cluster(env: Env) -> None:
    await env.submit()
    assert env.store is not None
    pid = env.store.pending()[0].id
    env.controller.open_review()
    await env.dialog()
    env.ui.answer(False)
    await env.tasks.finish()
    assert env.ops.calls == []
    found = env.store.get(pid)
    assert found is not None
    assert found[1] == "denied"
    assert "intent" not in env.outcomes()


async def test_a_proposal_withdrawn_before_the_claim_is_not_executed(env: Env) -> None:
    await env.submit()
    assert env.store is not None
    store = env.store
    pid = store.pending()[0].id
    env.controller.open_review()
    await env.dialog()
    store.cancel(pid, session_id="sess-1")
    env.ui.answer(True)
    await env.tasks.finish()
    assert env.ops.calls == []
    assert any("withdrawn before approval landed" in message for message in env.ui.messages())


async def test_a_context_switch_between_approval_and_execution_fails_the_proposal(
    env: Env,
) -> None:
    await env.submit()
    assert env.store is not None
    pid = env.store.pending()[0].id
    env.controller.open_review()
    await env.dialog()
    env.context.value = 4
    env.ui.answer(True)
    await env.tasks.finish()
    assert env.ops.calls == []
    found = env.store.get(pid)
    assert found is not None
    assert found[1] == "failed"
    assert "proposal failed: the kube context changed before execution" in env.outcomes()


async def test_a_permission_revoked_after_approval_fails_the_proposal(env: Env) -> None:
    await env.submit()
    env.controller.open_review()
    await env.dialog()
    env.permitted = False
    env.ui.answer(True)
    await env.tasks.finish()
    assert env.ops.calls == []
    assert "proposal failed: permission revoked before execution" in env.outcomes()


async def test_a_replaced_target_fails_the_proposal_instead_of_mutating(env: Env) -> None:
    await env.submit()
    env.controller.open_review()
    await env.dialog()
    env.builder.uid = "uid-2"
    env.ui.answer(True)
    await env.tasks.finish()
    assert env.ops.calls == []
    assert "proposal failed: the target was replaced since the proposal was created" in (
        env.outcomes()
    )


async def test_a_vanished_target_fails_the_proposal(env: Env) -> None:
    await env.submit()
    env.controller.open_review()
    await env.dialog()
    env.builder.uid_error = ApiStatusError(404, "not found")
    env.ui.answer(True)
    await env.tasks.finish()
    assert env.ops.calls == []
    assert "proposal failed: the target no longer exists" in env.outcomes()


async def test_an_unwritable_audit_blocks_the_approved_mutation(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, store=ProposalStore(), audit="broken")
    await env.submit()
    assert env.store is not None
    pid = env.store.pending()[0].id
    env.controller.open_review()
    await env.dialog()
    env.ui.answer(True)
    await env.tasks.finish()
    assert env.ops.calls == []
    found = env.store.get(pid)
    assert found is not None
    assert found[1] == "failed"
    assert found[2] == "blocked: audit log unavailable"


async def test_an_interrupted_execution_settles_as_uncertain(env: Env) -> None:
    env.ops.gate = asyncio.Event()
    await env.submit()
    assert env.store is not None
    store = env.store
    pid = store.pending()[0].id
    env.controller.open_review()
    await env.dialog()
    env.ui.answer(True)
    await asyncio.wait_for(env.ops.started.wait(), 2)
    for task in env.tasks.tasks:
        task.cancel()
    await env.tasks.finish()
    found = store.get(pid)
    assert found is not None
    assert found[1] == "failed"
    assert "the cluster outcome is uncertain" in found[2]
    assert any("uncertain" in outcome for outcome in env.outcomes())
    env.ops.gate.set()


async def test_an_interrupted_execution_keeps_a_settled_outcome(env: Env) -> None:
    await env.submit()
    assert env.store is not None
    store = env.store
    proposal = store.pending()[0]
    assert store.begin_execution(proposal.id)
    write: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    write.set_result("done")
    await env.controller._settle_interrupted_execution(store, proposal, write)
    found = store.get(proposal.id)
    assert found is not None
    assert found[1] == "executed"


# ---------------------------------------------------------------------------
# Typing
# ---------------------------------------------------------------------------


def test_ports_are_abstract() -> None:
    for port in (ProposalScreens, ReviewTasks, ProposalEvents):
        with pytest.raises(TypeError, match="abstract"):
            port()  # type: ignore[abstract]  # the port must not be instantiable


def test_the_controller_module_imports_nothing_from_the_app_module() -> None:
    """The module must never import `korvid.ui.app` or name `KorvidApp`.

    An import/name check, and no more than that. Three of the ports this
    controller is handed *are* app-backed adapters at runtime
    (`AppProposalScreens`, `AppReviewTasks`, `AppProposalEvents`) — that is
    fine and deliberate. What this pins is the direction of the dependency:
    the controller is written against the port interfaces declared here, so
    the app can be replaced by a fake without touching this module.
    """
    import ast

    module_path = Path(__file__).parents[2] / "src" / "korvid" / "ui" / "proposal_controller.py"
    source = module_path.read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    referenced: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
            imported.extend(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            referenced.append(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.append(node.attr)
    assert "korvid.ui.app" not in imported
    assert not any(module.startswith("korvid.ui.app.") for module in imported)
    assert not any(name.endswith("KorvidApp") for name in imported)
    assert "KorvidApp" not in referenced
