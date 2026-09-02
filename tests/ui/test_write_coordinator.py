"""Direct tests for `WriteCoordinator` — the write security perimeter.

The coordinator owns the single implementation of the ordering every cluster
mutation passes through:

1. user approval;
2. context epoch / identity revalidation;
3. synchronous write reservation;
4. fail-closed intent audit;
5. mutation;
6. outcome audit and notification.

It reaches Textual only through `UiSurface`, reads the view through
`ViewState`, and revalidates the `:ctx` epoch through `ContextGuard`, so
every step is exercised here without a running app. Interactive writes keep
their separate typed contract (`confirm_interactive`) because the facts they
must audit — the pod kubectl actually created, its uid, the session outcome —
are only known after the subprocess starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from textual.await_complete import AwaitComplete
from textual.screen import Screen

from korvid.core.audit import AuditLog
from korvid.core.impact import ImpactAction
from korvid.core.relationships import GraphResource
from korvid.core.store import ALL_NAMESPACES, Summary
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary
from korvid.ui.ui_surface import Severity, UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.workspace_controller import ContextGuard
from korvid.ui.workspace_state import PaneState
from korvid.ui.write_coordinator import WriteCoordinator, WriteOrigin, gvr_label, write_locus

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_NODES_META = ResourceMeta("Node", "nodes", "", "v1", False)
_HELM_META = ResourceMeta("HelmRelease", "helmreleases", "", "v1", True, synthetic=True)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeUi(UiSurface):
    """Records notifications, pushed screens, workers and deferred calls."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []
        self.notification_markup: list[bool] = []
        self.screens: list[tuple[Screen[Any], Callable[[Any], None] | None]] = []
        self._screen_changed = asyncio.Event()
        self.workers: list[asyncio.Task[Any]] = []
        self.deferred: list[tuple[Callable[..., None], tuple[Any, ...]]] = []
        self.progress_labels: list[str] = []

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: Severity = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notifications.append((message, severity))
        self.notification_markup.append(markup)

    def push_screen(
        self,
        screen: Any,
        callback: Any = None,
    ) -> AwaitComplete:
        self.screens.append((screen, callback))
        self._screen_changed.set()
        return AwaitComplete()

    def run_worker(
        self,
        work: Any,
        *,
        exclusive: bool = False,
        group: str = "default",
        name: str = "",
        exit_on_error: bool = True,
        thread: bool = False,
    ) -> Any:
        task = asyncio.ensure_future(work)
        self.workers.append(task)
        return task

    async def cancel_workers(self, group: str) -> None:
        raise NotImplementedError  # pragma: no cover

    def suspend(self) -> contextlib.AbstractContextManager[None]:
        raise NotImplementedError  # pragma: no cover

    def refresh(self) -> None:
        pass  # pragma: no cover

    def call_from_thread(self, callback: Callable[..., Any], *args: Any) -> None:
        callback(*args)  # pragma: no cover

    def call_later(self, callback: Callable[..., None], *args: Any) -> None:
        self.deferred.append((callback, args))

    def progress(self, label: str) -> contextlib.AbstractContextManager[None]:
        self.progress_labels.append(label)
        return contextlib.nullcontext()

    def is_current_screen(self, screen: Any) -> bool:
        return bool(self.screens) and self.screens[-1][0] is screen

    def screen_depth(self) -> int:
        return self.depth

    depth: int = 1
    inline_release_hint: str | None = None

    def inline_focus_release_hint(self) -> str | None:
        return self.inline_release_hint

    # -- helpers -----------------------------------------------------------

    def answer(self, confirmed: bool | None) -> None:
        """Resolve the most recently pushed screen with the user's answer."""
        _screen, callback = self.screens[-1]
        assert callback is not None
        callback(confirmed)

    def flush_deferred(self) -> None:
        pending, self.deferred = self.deferred, []
        for callback, args in pending:
            callback(*args)

    async def settle(self) -> None:
        for task in list(self.workers):
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def wait_for_screens(self, count: int = 1) -> None:
        while len(self.screens) < count:
            self._screen_changed.clear()
            if len(self.screens) >= count:
                return
            await asyncio.wait_for(self._screen_changed.wait(), timeout=5)

    def messages(self) -> list[str]:
        return [message for message, _severity in self.notifications]


class FakeView(ViewState):
    """A single selected row on one view; every field is directly settable."""

    def __init__(
        self,
        *,
        kind: str = "pods",
        scope: str = "default",
        selected: tuple[str | None, str | None] = ("default", "web-1"),
        uid: str | None = "uid-1",
        aliases: Mapping[str, ResourceMeta] | None = None,
        readonly: bool = False,
        rows: list[Summary] | None = None,
    ) -> None:
        self.kind = kind
        self.scope = scope
        self.selected = selected
        self.uid = uid
        self._aliases: dict[str, ResourceMeta] = dict(
            aliases if aliases is not None else {"pods": _PODS_META, "nodes": _NODES_META}
        )
        self._readonly = readonly
        self.rows: list[Summary] = rows if rows is not None else []

    def current_kind(self) -> str:
        return self.kind

    def current_scope(self) -> str:
        return self.scope

    def current_namespace(self) -> str:
        return self.scope

    def canonical_kind(self, kind: str) -> str:
        meta = self._aliases.get(kind)
        return kind if meta is None else meta.plural

    def aliases(self) -> Mapping[str, ResourceMeta]:
        return self._aliases

    def resources(self, kind: str, scope: str) -> list[Summary]:
        return self.rows

    def readonly(self) -> bool:
        return self._readonly

    def default_namespace(self) -> str | None:
        return "default"

    def selected_ns_name(self) -> tuple[str | None, str | None]:
        return self.selected

    def selected_uid(self, namespace: str | None, name: str) -> str | None:
        return self.uid

    def gvr_label(self, meta: ResourceMeta) -> str:
        return gvr_label(meta)

    def write_locus(self, namespace: str | None) -> str:
        return write_locus(namespace)


class FakeContext(ContextGuard):
    def __init__(self) -> None:
        self.value = 0
        self.is_switching = False
        self.reads = True

    def epoch(self) -> int:
        return self.value

    def switching(self) -> bool:
        return self.is_switching

    def reads_allowed(self) -> bool:
        return self.reads


class FakeTimeline:
    """Records the write entries the coordinator mirrors into the timeline."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, str]] = []

    def record_write(
        self,
        *,
        epoch: int,
        action: str,
        kind_alias: str,
        display_kind: str,
        namespace: str | None,
        name: str,
        outcome: str,
    ) -> None:
        self.records.append((action, kind_alias, outcome))


class FakeLoader:
    """A relationship snapshot loader with a scripted outcome."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.scopes: list[str | None] = []

    async def load(
        self,
        root: GraphResource,
        namespace: str | None,
        aliases: Mapping[str, ResourceMeta],
    ) -> Any:
        self.scopes.append(namespace)
        if self.error is not None:
            raise self.error
        return object()


class BrokenAudit(AuditLog):
    """Every append fails: the fail-closed path must block the mutation."""

    def append(self, **kwargs: Any) -> None:
        raise OSError("audit sink unavailable")


class Env:
    """A coordinator plus every fake it was built from."""

    def __init__(
        self,
        *,
        tmp_path: Path,
        audit: AuditLog | None,
        view: FakeView,
        check_permission: Callable[..., Awaitable[bool]] | None = None,
        loader: FakeLoader | None = None,
        protected_context: str | None = None,
    ) -> None:
        self.ui = FakeUi()
        self.view = view
        self.context = FakeContext()
        self.timeline = FakeTimeline()
        self.audit = audit
        self.audit_path = tmp_path / "audit.jsonl"
        self.loader = loader
        self.check_permission = check_permission
        self.pane = PaneState(view.kind, view.scope)
        self.coordinator = WriteCoordinator(
            ui=self.ui,
            view=view,
            context=self.context,
            audit=lambda: self.audit,
            timeline=self.timeline,
            check_permission=lambda: self.check_permission,
            relationship_loader=lambda: self.loader,
            focused_pane=lambda: self.pane,
            canonical_meta_kind=lambda meta: meta.plural,
            protected_context=protected_context,
        )

    def audit_outcomes(self) -> list[str]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line)["outcome"] for line in self.audit_path.read_text().splitlines()]


def make_env(
    tmp_path: Path,
    *,
    view: FakeView | None = None,
    audit: str = "working",
    check_permission: Callable[..., Awaitable[bool]] | None = None,
    loader: FakeLoader | None = None,
    protected_context: str | None = None,
) -> Env:
    path = tmp_path / "audit.jsonl"
    log: AuditLog | None
    if audit == "working":
        log = AuditLog(path, context="test")
    elif audit == "broken":
        log = BrokenAudit(path, context="test")
    else:
        log = None
    return Env(
        tmp_path=tmp_path,
        audit=log,
        view=view if view is not None else FakeView(),
        check_permission=check_permission,
        loader=loader,
        protected_context=protected_context,
    )


class Recorder:
    """The approved mutation, plus a record of when its factory was called."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.built = 0
        self.ran = 0
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    def factory(self) -> Awaitable[None]:
        self.built += 1
        return self._run()

    async def _run(self) -> None:
        self.ran += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error


def _workload(name: str, *, desired: int | None) -> GenericSummary:
    return GenericSummary(
        name=name,
        namespace="default",
        kind="Deployment",
        created="",
        uid="uid-1",
        desired=desired,
    )


# ---------------------------------------------------------------------------
# 0. The whole ordering, in one place
# ---------------------------------------------------------------------------


async def test_the_perimeter_runs_its_steps_in_the_required_order(tmp_path: Path) -> None:
    """approval -> reservation -> intent audit -> mutation -> outcome audit.

    The individual steps are pinned below; this pins that they happen in
    this order and nowhere else, because the order *is* the invariant: a
    mutation that reaches the API before its intent record persisted is not
    auditable, and one that starts before the reservation is taken can be
    overtaken by a `:ctx` switch.
    """
    env = make_env(tmp_path)
    events: list[str] = []
    real = env.audit
    assert real is not None

    def record_append(**kwargs: Any) -> None:
        events.append(f"audit:{kwargs['outcome']}")
        AuditLog.append(real, **kwargs)

    real.append = record_append  # type: ignore[method-assign]  # narrow fake seam
    env.timeline.record_write = lambda **kwargs: events.append(  # type: ignore[method-assign]  # narrow fake seam
        f"timeline:{kwargs['outcome']}"
    )

    async def mutate() -> None:
        events.append("mutation")

    def factory() -> Awaitable[None]:
        events.append("factory")
        return mutate()

    await env.coordinator.confirm(
        "Delete pods/web-1?",
        "DELETE pods/web-1",
        action="delete",
        meta=_PODS_META,
        namespace="default",
        name="web-1",
        op_factory=factory,
    )
    assert events == [], "pushing the dialog must do nothing on its own"
    env.ui.answer(True)
    events.append(f"reserved:{env.coordinator.active_writes()}")
    await env.ui.settle()
    assert events == [
        "reserved:1",
        "audit:intent",
        "timeline:intent",
        "factory",
        "mutation",
        "audit:success",
        "timeline:success",
    ]
    assert env.coordinator.active_writes() == 0


# ---------------------------------------------------------------------------
# 1. Approval: a declined dialog constructs and runs nothing
# ---------------------------------------------------------------------------


async def test_declined_confirmation_constructs_no_operation(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    await env.coordinator.confirm(
        "Delete pods/web-1?",
        "DELETE pods/web-1",
        action="delete",
        meta=_PODS_META,
        namespace="default",
        name="web-1",
        op_factory=rec.factory,
    )
    env.ui.answer(False)
    await env.ui.settle()
    assert rec.built == 0, "a declined dialog must never construct the mutation"
    assert env.ui.workers == []
    assert env.audit_outcomes() == []


async def test_dismissed_confirmation_constructs_no_operation(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    await env.coordinator.confirm(
        "Delete pods/web-1?",
        "DELETE pods/web-1",
        action="delete",
        meta=_PODS_META,
        namespace="default",
        name="web-1",
        op_factory=rec.factory,
    )
    env.ui.answer(None)  # Esc
    await env.ui.settle()
    assert rec.built == 0
    assert env.audit_outcomes() == []


async def test_approved_confirmation_launches_the_audited_write(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    await env.coordinator.confirm(
        "Delete pods/web-1?",
        "DELETE pods/web-1",
        action="delete",
        meta=_PODS_META,
        namespace="default",
        name="web-1",
        op_factory=rec.factory,
        detail="by keybinding",
    )
    env.ui.answer(True)
    await env.ui.settle()
    assert rec.ran == 1
    assert env.audit_outcomes() == ["intent", "success"]


# ---------------------------------------------------------------------------
# 2. Revalidation after the awaited dialog gap
# ---------------------------------------------------------------------------


async def test_approval_guard_refusal_blocks_the_write(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    await env.coordinator.confirm(
        "Scale pods/web-1?",
        "PATCH pods/web-1/scale",
        action="scale",
        meta=_PODS_META,
        namespace="default",
        name="web-1",
        op_factory=rec.factory,
        approval_guard=lambda: False,
    )
    env.ui.answer(True)
    # The guard is deferred one loop iteration: Textual runs the result
    # callback before it pops the dismissed screen.
    assert env.ui.workers == []
    env.ui.flush_deferred()
    await env.ui.settle()
    assert rec.built == 0, "a refused guard must leave no operation, audit or reservation"
    assert env.audit_outcomes() == []
    assert env.coordinator.active_writes() == 0


async def test_approval_guard_pass_launches_the_write(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    await env.coordinator.confirm(
        "Scale pods/web-1?",
        "PATCH pods/web-1/scale",
        action="scale",
        meta=_PODS_META,
        namespace="default",
        name="web-1",
        op_factory=rec.factory,
        approval_guard=lambda: True,
    )
    env.ui.answer(True)
    env.ui.flush_deferred()
    await env.ui.settle()
    assert rec.ran == 1


async def test_declined_dialog_never_runs_the_approval_guard(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    guard_calls: list[int] = []

    def guard() -> bool:
        guard_calls.append(1)
        return True

    await env.coordinator.confirm(
        "Scale pods/web-1?",
        "PATCH pods/web-1/scale",
        action="scale",
        meta=_PODS_META,
        namespace="default",
        name="web-1",
        op_factory=rec.factory,
        approval_guard=guard,
    )
    env.ui.answer(False)
    env.ui.flush_deferred()
    await env.ui.settle()
    assert guard_calls == []


async def test_epoch_change_after_the_interactive_dialog_blocks_the_write(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    epoch = env.coordinator.epoch()
    await env.coordinator.confirm_interactive(
        "Node shell on nodes/node-a?",
        "kubectl debug node/node-a",
        action="node-shell",
        meta=_NODES_META,
        namespace=None,
        name="node-a",
        epoch=epoch,
        op_factory=rec.factory,
    )
    env.context.value += 1  # a `:ctx` switch completed while the dialog was open
    env.ui.answer(True)
    await env.ui.settle()
    assert rec.built == 0
    assert any("the kube context changed" in message for message in env.ui.messages())


async def test_switch_in_flight_after_the_interactive_dialog_blocks_the_write(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    await env.coordinator.confirm_interactive(
        "Node shell on nodes/node-a?",
        "kubectl debug node/node-a",
        action="node-shell",
        meta=_NODES_META,
        namespace=None,
        name="node-a",
        epoch=env.coordinator.epoch(),
        op_factory=rec.factory,
    )
    env.context.is_switching = True
    env.ui.answer(True)
    await env.ui.settle()
    assert rec.built == 0


async def test_approved_interactive_write_runs_without_a_gate_intent_audit(tmp_path: Path) -> None:
    """The interactive contract: the gate approves, the operation audits.

    `confirm` audits intent before the mutation because it *is* the API
    call. An interactive session's auditable facts (the pod kubectl created,
    its uid, the exit outcome) only exist after the subprocess starts, so
    those flows audit themselves and reserve their own write.
    """
    env = make_env(tmp_path)
    rec = Recorder()
    await env.coordinator.confirm_interactive(
        "Node shell on nodes/node-a?",
        "kubectl debug node/node-a",
        action="node-shell",
        meta=_NODES_META,
        namespace=None,
        name="node-a",
        epoch=env.coordinator.epoch(),
        op_factory=rec.factory,
    )
    env.ui.answer(True)
    await env.ui.settle()
    assert rec.ran == 1
    assert env.audit_outcomes() == []
    assert env.coordinator.active_writes() == 0


async def test_declined_interactive_dialog_constructs_no_operation(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    await env.coordinator.confirm_interactive(
        "Node shell on nodes/node-a?",
        "kubectl debug node/node-a",
        action="node-shell",
        meta=_NODES_META,
        namespace=None,
        name="node-a",
        epoch=env.coordinator.epoch(),
        op_factory=rec.factory,
    )
    env.ui.answer(False)
    await env.ui.settle()
    assert rec.built == 0


# ---------------------------------------------------------------------------
# 3. Reservation: taken synchronously, released exactly once
# ---------------------------------------------------------------------------


async def test_reservation_is_taken_before_the_write_coroutine_starts(tmp_path: Path) -> None:
    """`:ctx` consults the count, so it must be reserved at construction.

    A confirmation callback builds the coroutine and hands it to a worker
    that only starts it on a later loop iteration; a switch queued in that
    gap must already see the write in flight.
    """
    env = make_env(tmp_path)
    rec = Recorder()
    coro = env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory)
    assert env.coordinator.active_writes() == 1, "reserved synchronously, before the first await"
    assert rec.built == 0
    assert await coro == "done"
    assert env.coordinator.active_writes() == 0


async def test_unstarted_write_releases_its_reservation_on_close(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    coro = env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory)
    assert env.coordinator.active_writes() == 1
    coro.close()
    assert env.coordinator.active_writes() == 0, "a coroutine that never ran must not leak a slot"
    assert rec.built == 0


async def test_cancelled_write_worker_releases_its_reservation(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    rec.release = asyncio.Event()
    task = asyncio.ensure_future(
        env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory)
    )
    await rec.started.wait()
    assert env.coordinator.active_writes() == 1
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert env.coordinator.active_writes() == 0


async def test_reservation_count_moves_by_exactly_one_per_write(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    counts: list[int] = []
    rec = Recorder()
    rec.release = asyncio.Event()

    async def watch() -> None:
        await rec.started.wait()
        counts.append(env.coordinator.active_writes())
        rec.release.set()  # type: ignore[union-attr]  # set above

    task = asyncio.ensure_future(
        env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory)
    )
    await watch()
    assert await task == "done"
    assert counts == [1]
    assert env.coordinator.active_writes() == 0


async def test_reserve_write_release_is_idempotent(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    release = env.coordinator.reserve_write()
    assert env.coordinator.active_writes() == 1
    release()
    release()
    assert env.coordinator.active_writes() == 0


# ---------------------------------------------------------------------------
# 4. Fail-closed intent audit
# ---------------------------------------------------------------------------


async def test_intent_audit_failure_blocks_the_mutation(tmp_path: Path) -> None:
    env = make_env(tmp_path, audit="broken")
    rec = Recorder()
    outcome = await env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory)
    assert outcome == "blocked: audit log unavailable"
    assert rec.built == 0, "the factory must never be called when intent could not persist"
    assert ("delete pods/web-1 blocked: audit log unavailable", "error") in env.ui.notifications


async def test_missing_audit_sink_blocks_the_mutation(tmp_path: Path) -> None:
    env = make_env(tmp_path, audit="none")
    rec = Recorder()
    outcome = await env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory)
    assert outcome == "blocked: audit log unavailable"
    assert rec.built == 0


async def test_blocked_intent_records_no_timeline_entry(tmp_path: Path) -> None:
    env = make_env(tmp_path, audit="broken")
    rec = Recorder()
    await env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory)
    assert env.timeline.records == [], "the timeline must not show a write that never ran"


async def test_timeline_records_only_after_the_durable_audit_succeeded(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    assert (
        await env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory) == "done"
    )
    assert env.timeline.records == [
        ("delete", "pods", "intent"),
        ("delete", "pods", "success"),
    ]
    assert env.audit_outcomes() == ["intent", "success"]


async def test_timeline_failure_never_replaces_the_durable_audit(tmp_path: Path) -> None:
    """The durable append happens first, and stands whatever the mirror does.

    The intent record is on disk even though the timeline raised, and the
    mutation factory was never built. Note what this does *not* claim: the
    perimeter has no `try` around `record_write`, so a raising
    `TimelineWrites` propagates here, and inside `run` it would be reported
    as a failed intent audit and block the write. That is safe because the
    only production implementation is
    `SessionTimelineController.record_write`, which is a non-raising
    boundary by construction — it returns early with no timeline wired, and
    routes the append through `_append_timeline`, which logs and notifies
    instead of raising. The invariant pinned here is the *ordering*: the
    timeline can never leave a record the audit log does not have.
    """
    env = make_env(tmp_path)

    def explode(**kwargs: Any) -> None:
        raise RuntimeError("timeline full")

    env.timeline.record_write = explode  # type: ignore[method-assign]  # narrow fake seam
    rec = Recorder()
    with pytest.raises(RuntimeError, match="timeline full"):
        await env.coordinator.audit_write("delete", _PODS_META, "default", "web-1", "", "intent")
    assert env.audit_outcomes() == ["intent"]
    assert rec.built == 0


# ---------------------------------------------------------------------------
# 5. Mutation outcomes
# ---------------------------------------------------------------------------


async def test_forbidden_mutation_keeps_the_rbac_message_contract(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder(error=ApiStatusError(403, "Forbidden"))
    outcome = await env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory)
    assert outcome == "failed: missing permission: delete pods"
    assert env.audit_outcomes()[0] == "intent"
    assert env.audit_outcomes()[1].startswith("error:")
    assert (
        "delete pods/web-1 failed: missing permission: delete pods",
        "error",
    ) in env.ui.notifications


async def test_conflicting_mutation_explains_the_uid_precondition(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder(error=ApiStatusError(409, "Conflict"))
    outcome = await env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory)
    assert outcome == (
        "failed: conflict: the target changed since it was approved - refresh and retry"
    )


async def test_unexpected_mutation_failure_audits_and_reports(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder(error=RuntimeError("boom"))
    outcome = await env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory)
    assert outcome == "failed: boom"
    assert env.timeline.records == [
        ("delete", "pods", "intent"),
        ("delete", "pods", "error: boom"),
    ]


async def test_outcome_audit_failure_warns_but_keeps_the_executed_write(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    real = env.audit
    assert real is not None
    calls: list[str] = []

    def flaky(**kwargs: Any) -> None:
        calls.append(str(kwargs["outcome"]))
        if kwargs["outcome"] != "intent":
            raise OSError("disk full")
        AuditLog.append(real, **kwargs)

    real.append = flaky  # type: ignore[method-assign]  # narrow fake seam
    outcome = await env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory)
    assert outcome == "done"
    assert rec.ran == 1
    assert (
        "Audit log write failed (operation already executed)",
        "warning",
    ) in env.ui.notifications


async def test_cancelled_mutation_leaves_the_intent_record_and_propagates(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    rec.release = asyncio.Event()
    task = asyncio.ensure_future(
        env.coordinator.run("delete", _PODS_META, "default", "web-1", rec.factory)
    )
    await rec.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.audit_outcomes() == ["intent"]
    assert env.coordinator.active_writes() == 0


async def test_shielded_write_finishes_after_an_interrupted_turn(tmp_path: Path) -> None:
    """An approved agent write completes even when the turn is interrupted."""
    env = make_env(tmp_path)
    rec = Recorder()
    rec.release = asyncio.Event()
    task = asyncio.ensure_future(
        env.coordinator.run_shielded(
            "delete", _PODS_META, "default", "web-1", rec.factory, detail="requested by agent"
        )
    )
    await rec.started.wait()
    task.cancel()
    rec.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rec.ran == 1, "the approved mutation must run to completion"
    assert env.audit_outcomes() == ["intent", "success"]
    assert env.coordinator.active_writes() == 0


async def test_shielded_write_returns_the_outcome_when_uninterrupted(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = Recorder()
    outcome = await env.coordinator.run_shielded(
        "delete", _PODS_META, "default", "web-1", rec.factory, detail="requested by agent"
    )
    assert outcome == "done"


# ---------------------------------------------------------------------------
# 6. Permission pre-check
# ---------------------------------------------------------------------------


async def test_permission_denial_notifies_and_refuses(tmp_path: Path) -> None:
    async def deny(*args: Any) -> bool:
        return False

    env = make_env(tmp_path, check_permission=deny)
    assert await env.coordinator.permitted("delete", _PODS_META, "default", "web-1") is False
    assert ("missing permission: delete pods", "error") in env.ui.notifications


async def test_permission_check_names_the_subresource(tmp_path: Path) -> None:
    async def deny(*args: Any) -> bool:
        return False

    env = make_env(tmp_path, check_permission=deny)
    assert await env.coordinator.permitted("scale", _PODS_META, "default", "web-1") is False
    assert ("missing permission: patch pods/scale", "error") in env.ui.notifications


async def test_no_permission_checker_allows_the_gated_write(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    assert await env.coordinator.permitted("delete", _PODS_META, "default", "web-1") is True
    assert env.ui.notifications == []


async def test_failing_permission_checker_fails_open_and_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def explode(*args: Any) -> bool:
        raise RuntimeError("SSAR forbidden")

    env = make_env(tmp_path, check_permission=explode)
    with caplog.at_level("WARNING", logger="korvid.ui.write_coordinator"):
        assert await env.coordinator.permitted("delete", _PODS_META, "default", "web-1") is True
        assert await env.coordinator.permitted("delete", _PODS_META, "default", "web-1") is True
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, "a persistently failing checker must warn once, not per write"


async def test_precheck_refuses_while_a_switch_is_in_flight(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.context.is_switching = True
    assert (
        await env.coordinator.precheck_keybinding_write("delete", _PODS_META, "default", "web-1")
        is False
    )
    assert any("A context switch is in progress" in m for m in env.ui.messages())


async def test_precheck_refuses_when_the_epoch_moved_during_the_check(tmp_path: Path) -> None:
    env = make_env(tmp_path)

    async def switch_midcheck(*args: Any) -> bool:
        env.context.value += 1
        return True

    env.check_permission = switch_midcheck
    assert (
        await env.coordinator.precheck_keybinding_write("delete", _PODS_META, "default", "web-1")
        is False
    )
    assert any("the kube context changed" in m for m in env.ui.messages())


# ---------------------------------------------------------------------------
# 7. Target resolution and revalidation
# ---------------------------------------------------------------------------


def test_write_target_refuses_in_readonly_mode(tmp_path: Path) -> None:
    env = make_env(tmp_path, view=FakeView(readonly=True))
    assert env.coordinator.write_target() is None
    assert ("Read-only mode: cluster writes are disabled", "warning") in env.ui.notifications


def test_write_target_refuses_without_an_audit_sink(tmp_path: Path) -> None:
    env = make_env(tmp_path, audit="none")
    assert env.coordinator.write_target() is None
    assert ("Writes disabled: no audit log configured", "warning") in env.ui.notifications


def test_write_target_refuses_a_synthetic_view(tmp_path: Path) -> None:
    view = FakeView(kind="helmreleases", aliases={"helmreleases": _HELM_META})
    env = make_env(tmp_path, view=view)
    assert env.coordinator.write_target() is None
    assert ("HelmRelease is a read-only view", "warning") in env.ui.notifications


def test_write_target_resolves_the_selected_row(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    assert env.coordinator.write_target() == (_PODS_META, "default", "web-1", "uid-1")


def test_write_target_drops_the_namespace_for_cluster_scoped_kinds(tmp_path: Path) -> None:
    view = FakeView(kind="nodes", selected=("default", "node-a"))
    env = make_env(tmp_path, view=view)
    assert env.coordinator.write_target() == (_NODES_META, None, "node-a", "uid-1")


def test_context_intact_refuses_after_a_context_switch(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    epoch = env.coordinator.epoch()
    env.context.value += 1
    assert (
        env.coordinator.context_intact("delete", _PODS_META, "default", "web-1", epoch=epoch)
        is False
    )
    assert any(
        "the kube context changed during the permission check" in m for m in env.ui.messages()
    )


def test_context_intact_refuses_while_another_dialog_is_open(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.ui.depth = 2
    assert (
        env.coordinator.context_intact("delete", _PODS_META, "default", "web-1", epoch=0) is False
    )
    assert any("another dialog opened" in m for m in env.ui.messages())


def test_context_intact_refuses_when_the_selection_moved(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.view.selected = ("default", "web-2")
    assert (
        env.coordinator.context_intact("delete", _PODS_META, "default", "web-1", epoch=0) is False
    )
    assert any("the selection changed" in m for m in env.ui.messages())


def test_context_intact_refuses_when_the_origin_pane_lost_focus(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    origin = env.coordinator.write_origin()
    env.pane = PaneState("pods", "default")  # a second pane on the same view
    assert (
        env.coordinator.context_intact(
            "delete", _PODS_META, "default", "web-1", epoch=0, origin=origin
        )
        is False
    )


def test_context_intact_accepts_an_equal_but_rebuilt_meta(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.view._aliases["pods"] = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
    assert env.coordinator.context_intact("delete", _PODS_META, "default", "web-1", epoch=0) is True


def test_identity_intact_refuses_a_replaced_incarnation(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.view.uid = "uid-2"
    assert (
        env.coordinator.identity_intact(
            "delete", _PODS_META, "default", "web-1", "uid-1", phase="the dry-run preview", epoch=0
        )
        is False
    )
    assert any("the selection changed during the dry-run preview" in m for m in env.ui.messages())


def test_identity_intact_accepts_a_row_without_a_uid(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.view.uid = None
    assert (
        env.coordinator.identity_intact(
            "delete", _PODS_META, "default", "web-1", None, phase="the dry-run preview", epoch=0
        )
        is True
    )


def test_uid_intact_after_fetch_refuses_a_replaced_object(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    manifest = {"metadata": {"uid": "uid-other"}}
    assert env.coordinator.uid_intact_after_fetch(manifest, "default", "web-1", "uid-1") is False


def test_uid_intact_after_fetch_accepts_the_same_incarnation(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    manifest = {"metadata": {"uid": "uid-1"}}
    assert env.coordinator.uid_intact_after_fetch(manifest, "default", "web-1", "uid-1") is True


def test_scale_identity_intact_refuses_a_moved_replica_count(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    origin = env.coordinator.write_origin()
    assert (
        env.coordinator.scale_identity_intact(
            _PODS_META,
            "default",
            "web-1",
            "uid-1",
            3,
            phase="the confirmation dialog",
            epoch=0,
            origin=origin,
        )
        is False
    ), "no row reports 3 replicas, so the captured count drifted"
    assert any("the desired replica count changed" in m for m in env.ui.messages())


# ---------------------------------------------------------------------------
# 8. Previews fail open, never block or leak
# ---------------------------------------------------------------------------


async def test_dry_run_preview_returns_none_on_failure(tmp_path: Path) -> None:
    env = make_env(tmp_path)

    async def explode() -> list[str] | None:
        raise ApiStatusError(500, "Internal", body="secret-data")

    assert await env.coordinator.dry_run_preview(explode()) is None
    assert env.ui.notifications == [], "a failed preview must not disturb the approval flow"


async def test_dry_run_preview_returns_the_lines_it_got(tmp_path: Path) -> None:
    env = make_env(tmp_path)

    async def lines() -> list[str] | None:
        return ["- name: web"]

    assert await env.coordinator.dry_run_preview(lines()) == ["- name: web"]


async def test_impact_preview_without_a_loader_returns_no_section(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    origin = env.coordinator.write_origin()
    assert (
        await env.coordinator.impact_preview(
            ImpactAction.DELETE, _PODS_META, "default", "web-1", "uid-1", origin=origin
        )
        is None
    )


async def test_impact_preview_without_a_uid_returns_no_section(tmp_path: Path) -> None:
    env = make_env(tmp_path, loader=FakeLoader())
    origin = env.coordinator.write_origin()
    assert (
        await env.coordinator.impact_preview(
            ImpactAction.DELETE, _PODS_META, "default", "web-1", None, origin=origin
        )
        is None
    )


async def test_impact_preview_failure_returns_the_static_advisory(tmp_path: Path) -> None:
    env = make_env(tmp_path, loader=FakeLoader(error=ApiStatusError(500, "Internal", body="creds")))
    origin = env.coordinator.write_origin()
    lines = await env.coordinator.impact_preview(
        ImpactAction.DELETE, _PODS_META, "default", "web-1", "uid-1", origin=origin
    )
    assert lines is not None
    assert any("impact unavailable" in line for line in lines)
    assert not any("creds" in line for line in lines), "an API body must never reach the dialog"


async def test_impact_preview_propagates_cancellation(tmp_path: Path) -> None:
    env = make_env(tmp_path, loader=FakeLoader(error=asyncio.CancelledError()))
    origin = env.coordinator.write_origin()
    with pytest.raises(asyncio.CancelledError):
        await env.coordinator.impact_preview(
            ImpactAction.DELETE, _PODS_META, "default", "web-1", "uid-1", origin=origin
        )


async def test_impact_preview_scopes_the_snapshot_to_the_origin_pane(tmp_path: Path) -> None:
    loader = FakeLoader()
    env = make_env(tmp_path, loader=loader)
    origin = env.coordinator.write_origin()
    env.pane = PaneState("pods", "kube-system")  # focus moves under the flow
    await env.coordinator.impact_preview(
        ImpactAction.DELETE, _PODS_META, "default", "web-1", "uid-1", origin=origin
    )
    assert loader.scopes == ["default"]


async def test_impact_preview_covers_every_namespace_for_a_cluster_scoped_target(
    tmp_path: Path,
) -> None:
    loader = FakeLoader()
    env = make_env(tmp_path, loader=loader)
    await env.coordinator.impact_preview(
        ImpactAction.DRAIN_NODE,
        _NODES_META,
        None,
        "node-a",
        "uid-1",
        origin=env.coordinator.write_origin(),
    )
    assert loader.scopes == [None]


def test_write_origin_pins_the_pane_and_its_scope(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    origin = env.coordinator.write_origin()
    assert origin.is_current(env.pane) is True
    env.pane.scope = ALL_NAMESPACES
    assert origin.is_current(env.pane) is False, (
        "a re-scoped pane is not the pane the flow began on"
    )


def test_write_origin_impact_scope_widens_for_all_namespaces() -> None:
    pane = PaneState("pods", ALL_NAMESPACES)
    assert WriteOrigin(pane, ALL_NAMESPACES).impact_scope(_PODS_META) is None


# ---------------------------------------------------------------------------
# 9. Protected contexts
# ---------------------------------------------------------------------------


def test_protected_context_marks_every_confirm_dialog(tmp_path: Path) -> None:
    env = make_env(tmp_path, protected_context="prod")
    screen = env.coordinator.confirm_screen("Delete pods/web-1?", "DELETE pods/web-1")
    assert isinstance(screen, ConfirmScreen)
    assert screen._protected_context == "prod"


def test_unprotected_context_leaves_the_dialog_unmarked(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    screen = env.coordinator.confirm_screen("Delete pods/web-1?", "DELETE pods/web-1")
    assert screen._protected_context is None


async def test_protected_context_marks_the_api_confirmation(tmp_path: Path) -> None:
    env = make_env(tmp_path, protected_context="prod")
    rec = Recorder()
    await env.coordinator.confirm(
        "Delete pods/web-1?",
        "DELETE pods/web-1",
        action="delete",
        meta=_PODS_META,
        namespace="default",
        name="web-1",
        op_factory=rec.factory,
    )
    screen, _callback = env.ui.screens[-1]
    assert isinstance(screen, ConfirmScreen)
    assert screen._protected_context == "prod"


async def test_protected_context_marks_the_interactive_confirmation(tmp_path: Path) -> None:
    env = make_env(tmp_path, protected_context="prod")
    rec = Recorder()
    await env.coordinator.confirm_interactive(
        "Node shell on nodes/node-a?",
        "kubectl debug node/node-a",
        action="node-shell",
        meta=_NODES_META,
        namespace=None,
        name="node-a",
        epoch=0,
        op_factory=rec.factory,
    )
    screen, _callback = env.ui.screens[-1]
    assert isinstance(screen, ConfirmScreen)
    assert screen._protected_context == "prod"


def test_protected_context_is_readopted_on_a_switch(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    assert env.coordinator.protected_context is None
    env.coordinator.set_protected_context("prod")
    assert env.coordinator.protected_context == "prod"
    env.coordinator.set_protected_context(None)
    assert env.coordinator.protected_context is None


# ---------------------------------------------------------------------------
# 10. The gate's narrow observable state
# ---------------------------------------------------------------------------


def test_audit_configured_reports_the_sink(tmp_path: Path) -> None:
    assert make_env(tmp_path).coordinator.audit_configured() is True
    assert make_env(tmp_path, audit="none").coordinator.audit_configured() is False


def test_gate_epoch_switching_and_reads_track_the_context_guard(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.context.value = 7
    env.context.is_switching = True
    env.context.reads = False
    assert env.coordinator.epoch() == 7
    assert env.coordinator.switching() is True
    assert env.coordinator.reads_allowed() is False


def test_write_locus_names_the_namespace_or_cluster_scope() -> None:
    assert write_locus("default") == " in namespace default"
    assert write_locus(None) == " (cluster-scoped)"


def test_gvr_label_qualifies_the_group(tmp_path: Path) -> None:
    assert gvr_label(_PODS_META) == "pods"
    assert gvr_label(ResourceMeta("Sub", "subscriptions", "operators.coreos.com", "v1", True)) == (
        "subscriptions.operators.coreos.com"
    )


def test_current_replicas_reads_the_selected_row(tmp_path: Path) -> None:
    env = make_env(tmp_path, view=FakeView(rows=[_workload("web-1", desired=3)]))
    assert env.coordinator.current_replicas("default", "web-1") == 3
    assert env.coordinator.current_replicas("default", "web-2") is None


def test_is_scale_down_needs_a_known_current_count() -> None:
    assert WriteCoordinator.is_scale_down(None, 0) is False
    assert WriteCoordinator.is_scale_down(3, 1) is True
    assert WriteCoordinator.is_scale_down(1, 3) is False
