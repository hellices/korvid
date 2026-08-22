"""External MCP write proposals, as their own controller (issue #110 /
issue #187 Deep Task 7).

An external MCP caller may *propose* a cluster write; it can never execute
one. `ProposalController` owns that whole inbox, which used to live directly
on `KorvidApp`:

- the `ProposalStore` reference and the submit/get/cancel intake the three
  MCP tools reach through the `AgentProposals` port;
- the immutable provenance line and the terminal-outcome audit records;
- the store update subscription and the external-change/TTL-expiry handling;
- the pending-proposal status-bar label;
- the `:proposals` review loop — oldest first, one decision at a time — with
  its own approval dialog, timeout and stacked-screen refusal;
- the operation rebuild and every re-validation a stored record needs before
  it may be shown or executed (context epoch, kube context, RBAC, UID);
- the claimed execution under the shared `nav_lock`, the interrupted-execution
  settlement, and the failure/resolve paths;
- the audited expiry sweep the `:ctx` switch, the `:mcp` toggles and unmount
  drive.

It owns no security ordering: an approved proposal is executed through
`WriteCoordinator.run`, which is the single implementation of "reserve →
fail-closed intent audit → mutate → outcome audit". Nothing here mutates the
cluster on its own, and the review dialog is resolved only by real user key
input — this is a user-initiated flow (`:proposals`), deliberately separate
from the agent's own write approval.

It reaches Textual only through `UiSurface` and the narrow ports below, and
never imports or holds `KorvidApp`, so the whole proposal surface is
exercised without a running app. The app keeps the Textual message handlers
and the `:ctx`/`:mcp`/unmount call sites as thin delegates.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Literal, Protocol

from textual.screen import Screen

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.writes import restart_stamp
from korvid.tools.proposals import (
    ProposalClosedError,
    ProposalLimitError,
    ProposalState,
    ProposalStore,
    ProposalTooLargeError,
    WriteProposal,
)
from korvid.ui.agent_ui_controller import AgentProposals, WriteOpBuild
from korvid.ui.ui_surface import UiSurface
from korvid.ui.workspace_controller import ContextGuard
from korvid.ui.write_coordinator import WriteCoordinator, gvr_label, write_locus

logger = logging.getLogger(__name__)

#: Seconds a proposal-review dialog stays open before it counts as a
#: dismissal — an unanswered dialog must never wedge the review loop. The
#: agent's own approval budget lives with the flow that owns it
#: (`agent_ui_controller.APPROVAL_TIMEOUT`).
APPROVAL_TIMEOUT = 120.0

#: Worker group the review loop runs in. Named so shutdown (and the tests
#: that simulate it) can cancel exactly this flow.
REVIEW_GROUP = "proposal-review"

#: What one `ConfirmScreen` answer means for the proposal under review.
Decision = Literal["approved", "declined", "dismissed"]


class ProposalScreens(ABC):
    """The one screen fact the review flow must be able to act on.

    A `Screen` handed over whole also carries `dismiss` and `app`, which is
    app access routed around `UiSurface`; the flow only ever needs "pop the
    dialog I pushed, if it is still the one on top".
    """

    @abstractmethod
    def dismiss_if_current(self, screen: Screen[Any]) -> None:
        """Pop *screen* when it is on top; never disturb a later screen."""


class ReviewTasks(ABC):
    """Where the `:proposals` review loop runs.

    A supervised app worker rather than a bare task: shutdown cancels it,
    and the claimed-execution settlement depends on that cancellation being
    delivered. `review_running` exists because the loop must never be
    *replaced* — once a proposal is claimed, cancelling the worker could
    interrupt `WriteCoordinator.run` mid-mutation, so a duplicate `:proposals`
    is refused instead.
    """

    @abstractmethod
    def review_running(self) -> bool:
        """Whether a review worker is still live."""

    @abstractmethod
    def start_review(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run the review loop on a supervised worker of its own group."""


class ProposalEvents(ABC):
    """Marshaling of store callbacks onto the UI loop.

    The store is shared with the MCP server's thread, so `subscribe` and the
    TTL-expiry hook may fire from anywhere. Neither callback may touch a
    widget or await; the app implements this by posting a Textual message.
    """

    @abstractmethod
    def changed(self) -> None:
        """The store's contents changed (submit or state transition)."""

    @abstractmethod
    def expired(self, proposal: WriteProposal, reason: str) -> None:
        """The lazy TTL sweep expired *proposal* and it must be audited."""


class NavigationLock(Protocol):
    """The navigation lock the `:ctx`/`:mcp`/write flows share (structural).

    Satisfied by `WorkspaceController`: the execution claim must linearize
    with the switch/shutdown expiry sweeps, which hold the same lock.
    """

    @property
    def nav_lock(self) -> asyncio.Lock: ...


class WriteOpBuilder(Protocol):
    """Validated write-operation construction (structural).

    Satisfied by `AgentUiController`, which owns exactly this construction
    for the direct agent write. A stored proposal never carries an
    executable closure: the operation is rebuilt here at review time, so
    read-only mode, audit availability, kind resolution and argument
    validation are all re-checked against the *current* session.
    """

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
    ) -> WriteOpBuild | str: ...

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
    ) -> list[str] | None: ...

    async def target_uid(self, kind_alias: str, ns: str | None, name: str) -> str | None: ...


class ProposalController(AgentProposals):
    """Owns the external write-proposal inbox end to end."""

    def __init__(
        self,
        *,
        #: None when `mcp.write_proposals` is off: every entry point then
        #: reports the feature as disabled instead of silently accepting.
        store: ProposalStore | None,
        ui: UiSurface,
        screens: ProposalScreens,
        tasks: ReviewTasks,
        events: ProposalEvents,
        context: ContextGuard,
        writes: WriteCoordinator,
        navigation: NavigationLock,
        #: Late-binding: the builder is the agent controller, which is wired
        #: after this one (it takes this controller as its proposals port).
        builder: Callable[[], WriteOpBuilder],
        config: Callable[[], KorvidConfig],
        audit: Callable[[], AuditLog | None],
        #: Repaint the status bar's pending-proposal label; the bar itself
        #: belongs to the app's widget tree.
        refresh_status: Callable[[], None] = lambda: None,
    ) -> None:
        self._store = store
        self._ui = ui
        self._screens = screens
        self._tasks = tasks
        self._events = events
        self._context = context
        self._writes = writes
        self._navigation = navigation
        self._builder = builder
        self._config = config
        self._audit = audit
        self._refresh_status = refresh_status

    # ------------------------------------------------------------------
    # Intake — the three MCP tools, reached through `AgentProposals`
    # ------------------------------------------------------------------

    async def submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        """External MCP write proposal intake (issue #110).

        Validates exactly like the direct agent write path (kind, RBAC, UID
        capture, dry-run preview) but instead of opening a dialog it queues
        an immutable proposal for later user review — no modal, no focus
        steal. Returns the proposal id; the caller polls
        ``get_write_proposal`` for the terminal outcome.
        """
        store = self._store
        if store is None:
            return "ERROR: external write proposals are not enabled"
        name = name.strip()
        stamp = restart_stamp()
        # Intake is validated against exactly one context: snapshot it here
        # and recheck before queueing — RBAC, UID capture and the preview all
        # await, and a context switch landing mid-intake would otherwise
        # stamp old-context validation onto a new-context proposal.
        epoch = self._context.epoch()
        context = self._config().kube_context
        built = self._builder().build_write_op(
            action, kind, name, namespace, replicas, resources, restarted_at=stamp
        )
        if isinstance(built, str):
            return built
        meta, ns, _op, operation, _detail = built
        arguments_json = json.dumps(
            {
                "action": action,
                "kind": kind.strip().lower(),
                "name": name,
                "namespace": ns,
                "replicas": replicas,
                "resources": resources,
                "restarted_at": stamp,
            }
        )
        # Untrusted-input bound: enforced before any cluster I/O (the store
        # atomically rechecks it at submit) so an oversized payload cannot
        # force an RBAC round trip, UID lookup, or server dry-run.
        if len(arguments_json) > store.max_argument_chars:
            return (
                f"ERROR: proposal arguments exceed {store.max_argument_chars}"
                " characters; the proposal was not queued"
            )
        if not await self._writes.permitted(action, meta, ns, name):
            verb, target = self._writes.perm_target(action, meta)
            return f"ERROR: missing permission: {verb} {target}"
        try:
            uid = await self._builder().target_uid(kind.strip().lower(), ns, name)
        except ApiStatusError:
            return f"ERROR: {gvr_label(meta)}/{name} not found{write_locus(ns)}"
        if uid is None:
            # The interactive path fails open here (a user is watching);
            # an external proposal without a UID binding could mutate a
            # same-named replacement, so it must fail closed instead.
            return (
                "ERROR: could not verify the write target (UID capture"
                " failed); the proposal was not queued — try again"
            )
        preview = await self._builder().preview_for_action(
            action, meta, ns, name, replicas, resources, uid, stamp
        )
        if self._context.crossed(epoch) or self._config().kube_context != context:
            return (
                "ERROR: the kube context changed while the proposal was being"
                " validated; the proposal was not queued — try again"
            )
        try:
            proposal = store.submit(
                action=action,
                group=meta.group,
                version=meta.version,
                kind=meta.plural,
                namespace=ns,
                name=name,
                arguments_json=arguments_json,
                uid=uid,
                context=context,
                context_epoch=epoch,
                summary=operation,
                preview=tuple(preview or ()),
                session_id=session_id,
                client_name=client_name,
                client_version=client_version,
            )
        except (ProposalClosedError, ProposalLimitError, ProposalTooLargeError) as exc:
            return f"ERROR: {exc}"
        source = client_name or "an external MCP caller"
        self._ui.notify(
            f"Write proposal from {source}: {operation} — review with :proposals",
            severity="warning",
            timeout=10,
        )
        ttl = max(0, int(proposal.expires_at - proposal.created_at))
        return (
            f"proposal {proposal.id} is pending user review in the TUI"
            f" (expires in {ttl}s if unreviewed); poll get_write_proposal"
            " for the outcome"
        )

    async def get_write_proposal(self, proposal_id: str) -> str:
        """Terminal-outcome lookup for an external write proposal."""
        store = self._store
        if store is None:
            return "ERROR: external write proposals are not enabled"
        found = store.get(proposal_id)
        if found is None:
            return "ERROR: unknown proposal id"
        proposal, state, reason = found
        line = f"proposal {proposal.id}: {state} — {proposal.summary}"
        return f"{line} ({reason})" if reason else line

    async def cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        """Caller-initiated cancel; only the submitting session may cancel."""
        store = self._store
        if store is None:
            return "ERROR: external write proposals are not enabled"
        found = store.get(proposal_id)
        if found is not None and store.cancel(proposal_id, session_id=session_id):
            await self._audit_outcome(found[0], "cancelled", "cancelled by caller")
            return f"proposal {proposal_id} cancelled"
        return "ERROR: proposal not found, not pending, or owned by another session"

    # ------------------------------------------------------------------
    # Store updates, status label, and the audited expiry sweeps
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """Keep the pending indicator and the expiry audit live.

        Both callbacks may fire from the MCP server's thread, so neither
        does anything but hand the fact to `ProposalEvents`, which marshals
        it onto the UI loop.
        """
        store = self._store
        if store is None:
            return
        store.subscribe(self._events.changed)
        store.set_on_expired(self._events.expired)

    def status_label(self) -> str:
        """Status-bar text for pending external write proposals (issue #110):
        a persistent, non-modal indicator naming source and target."""
        store = self._store
        if store is None:
            return ""
        pending = store.pending()
        if not pending:
            return ""
        p = pending[0]  # oldest — the next proposal a review would surface
        source = p.client_name or "mcp"
        target = f"{p.action} {p.kind}/{p.name}"
        if len(pending) == 1:
            return f"1 proposal from {source}: {target} — :proposals"
        return f"{len(pending)} proposals (next from {source}: {target}) — :proposals"

    def handle_changed(self) -> None:
        """A store change reached the UI loop: repaint the indicator."""
        self._refresh_status()

    async def handle_expired(self, proposal: WriteProposal, reason: str) -> None:
        """Audit a proposal the lazy TTL sweep expired: it reached a terminal
        state like any other and must not vanish from the audit trail."""
        await self._audit_outcome(proposal, "expired", reason)

    async def expire_all(self, reason: str) -> None:
        """Expire every pending proposal and audit each terminal outcome.

        The sweep the `:ctx` switch, the `:mcp on`/`:mcp off` transitions and
        shutdown all drive: a proposal must never outlive the context or the
        server run whose capability token created it.
        """
        store = self._store
        if store is None:
            return
        for proposal in store.expire_all(reason=reason):
            await self._audit_outcome(proposal, "expired", reason)

    async def shutdown(self) -> None:
        """Close the store, then sweep: a proposal must never outlive the
        session that previewed it, and closing first means an in-flight
        submission cannot land after the final sweep."""
        if self._store is not None:
            self._store.close()
        await self.expire_all("the TUI session ended")

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    async def _audit_outcome(self, proposal: WriteProposal, state: str, reason: str) -> None:
        """Best-effort audit for a terminal proposal outcome (issue #110).

        These transitions mutate nothing, so a failed append is logged
        instead of blocking — the mutation path itself stays fail-closed in
        `WriteCoordinator.run` (which separately audits executed/failed writes
        with the same proposal provenance). The blocking file append is
        offloaded per audit.py's contract for async contexts.
        """
        await asyncio.to_thread(self._append_audit, proposal, state, reason)

    def _append_audit(self, proposal: WriteProposal, state: str, reason: str) -> None:
        """Blocking append of a proposal outcome; call via to_thread."""
        audit = self._audit()
        if audit is None:
            return
        try:
            audit.append(
                action=proposal.action,
                kind=proposal.kind,
                group=proposal.group,
                version=proposal.version,
                namespace=proposal.namespace,
                name=proposal.name,
                detail=self.provenance(proposal),
                outcome=f"proposal {state}: {reason}" if reason else f"proposal {state}",
                # These outcome appends are not serialized with `:ctx`'s
                # set_context: bind the entry to the proposal's own cluster.
                context=proposal.context,
            )
        except Exception:
            logger.warning("could not audit proposal %s outcome %s", proposal.id, state)

    @staticmethod
    def provenance(proposal: WriteProposal) -> str:
        """Field-injection-safe audit provenance for an external proposal.

        `client_name`/`client_version` are untrusted MCP client metadata:
        non-printables are stripped at intake, but spaces and `=` could
        still forge field-looking tokens (`session=forged`). Quote them
        (shell-style, so benign values stay bare) so every `key=` field in
        the detail is korvid's own.
        """
        caller = shlex.quote(proposal.client_name or "unknown")
        version = (
            f" version={shlex.quote(proposal.client_version)}" if proposal.client_version else ""
        )
        return (
            f"source=external_mcp proposal={proposal.id}"
            f" caller={caller}{version} session={proposal.session_id}"
        )

    # ------------------------------------------------------------------
    # The `:proposals` review loop
    # ------------------------------------------------------------------

    def open_review(self) -> None:
        """`:proposals` — review pending external proposals one at a time."""
        store = self._store
        if store is None:
            self._ui.notify(
                "External write proposals are disabled (set mcp.write_proposals: true)",
                severity="warning",
            )
            return
        if not store.pending():
            self._ui.notify("No pending write proposals")
            return
        # Never *replace* a live review worker (exclusive=True would cancel
        # it): once a proposal is claimed, cancellation could interrupt
        # `WriteCoordinator.run` mid-mutation and strand the record as
        # `approved` with an uncertain cluster outcome. Duplicate opens are
        # refused.
        if self._tasks.review_running():
            self._ui.notify("A proposal review is already open", severity="warning")
            return
        self._tasks.start_review(self._review_proposals(store))

    async def _review_proposals(self, store: ProposalStore) -> None:
        """Review pending proposals oldest-first until none remain or the
        user dismisses the dialog (a dismissed proposal stays pending)."""
        while True:
            pending = store.pending()
            if not pending:
                self._refresh_status()
                return
            if not await self._review_one(store, pending[0]):
                self._refresh_status()
                return

    async def _review_one(self, store: ProposalStore, proposal: WriteProposal) -> bool:
        """Re-validate and put one proposal in front of the user; False stops
        the review loop (dismissal), True moves on to the next proposal."""
        if proposal.context_epoch != self._context.epoch() or (
            proposal.context != self._config().kube_context
        ):
            await self._resolve_audited(
                store, proposal, "expired", "kube context changed since submission"
            )
            return True
        rebuilt = self._rebuild_op(proposal)
        if isinstance(rebuilt, str):
            await self._resolve_audited(store, proposal, "expired", rebuilt.removeprefix("ERROR: "))
            return True
        meta, ns, op, operation, _detail = rebuilt
        if not await self._writes.permitted(proposal.action, meta, ns, proposal.name):
            await self._resolve_audited(
                store, proposal, "failed", "permission revoked since submission"
            )
            return True
        # The awaited SSAR can be slow: re-read state and context before
        # surfacing the dialog. The proposal may have been cancelled or
        # expired meanwhile, and a `:ctx` switch begun in flight owns its
        # fate (the switch's sweep expires old-context proposals) — never
        # put an already-invalid proposal in front of the user.
        found = store.get(proposal.id)
        if found is None or found[1] != "pending":
            return True
        if self._context.crossed(proposal.context_epoch):
            return False
        source = proposal.client_name or "external MCP caller"
        title = (
            f"External proposal from {source}: {proposal.action}"
            f" {gvr_label(meta)}/{proposal.name}{write_locus(ns)}"
        )
        require = proposal.name if proposal.action == "delete" and not meta.namespaced else None
        decision = await self._await_decision(
            title,
            self._dialog_body(proposal, operation),
            require_name=require,
            preview=list(proposal.preview),
        )
        if decision == "dismissed":
            return False
        if decision == "declined":
            await self._resolve_audited(store, proposal, "denied", "denied by user")
            return True
        await self._execute(store, proposal, meta, ns, op)
        return True

    async def _await_decision(
        self,
        title: str,
        operation: str,
        *,
        require_name: str | None,
        preview: list[str] | None,
    ) -> Decision:
        """One ConfirmScreen decision for a proposal; only real key input can
        resolve it. Unlike agent writes this is user-initiated (:proposals),
        so there is no panel gate — but never stack over another dialog where
        a stray keystroke could approve, and treat an unanswered dialog as a
        dismissal (the proposal stays pending until its own TTL)."""
        if self._ui.screen_depth() != 1:
            self._ui.notify(
                "Close the current dialog, then run :proposals again", severity="warning"
            )
            return "dismissed"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool | None] = loop.create_future()

        def _done(confirmed: bool | None) -> None:
            if not fut.done():
                fut.set_result(confirmed)

        screen = self._writes.confirm_screen(
            title, operation, require_name=require_name, preview=preview
        )
        await self._ui.push_screen(screen, _done)
        try:
            confirmed = await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
        except TimeoutError:
            self._screens.dismiss_if_current(screen)
            return "dismissed"
        if confirmed is None:  # Esc: no decision was made
            return "dismissed"
        return "approved" if confirmed else "declined"

    def _rebuild_op(self, proposal: WriteProposal) -> WriteOpBuild | str:
        """Rebuild the write operation from the proposal's immutable
        arguments at review time: readonly mode, audit availability, kind
        resolution and argument validation are all rechecked — the stored
        record never carries an executable closure."""
        try:
            args = json.loads(proposal.arguments_json)
        except ValueError:
            return "ERROR: proposal arguments are unreadable"
        return self._builder().build_write_op(
            args["action"],
            args["kind"],
            args["name"],
            args["namespace"],
            args["replicas"],
            args["resources"],
            restarted_at=args["restarted_at"],
        )

    async def _resolve_audited(
        self, store: ProposalStore, proposal: WriteProposal, state: ProposalState, reason: str
    ) -> None:
        """Resolve a pending proposal and audit the terminal outcome."""
        if store.resolve(proposal.id, state, reason=reason):
            await self._audit_outcome(proposal, state, reason)

    def _dialog_body(self, proposal: WriteProposal, operation: str) -> str:
        """The dialog body for a proposal: the operation plus the immutable
        safety bindings issue #110 requires the user to see before approval —
        the caller (explicitly untrusted metadata), the bound kube context
        and epoch, the bound target UID, and the expiry."""
        caller = proposal.client_name or "unknown"
        version = f" {proposal.client_version}" if proposal.client_version else ""
        remaining = max(0, int(proposal.expires_at - time.monotonic()))
        return "\n".join(
            (
                operation,
                "",
                f"caller (untrusted metadata): {caller}{version}",
                f"bound kube context: {proposal.context or '(default)'}"
                f" (epoch {proposal.context_epoch})",
                f"bound target uid: {proposal.uid}",
                f"expires in {remaining}s unless approved or denied",
            )
        )

    # ------------------------------------------------------------------
    # Execution of an approved proposal
    # ------------------------------------------------------------------

    async def _fail(
        self, store: ProposalStore, proposal: WriteProposal, meta: ResourceMeta, reason: str
    ) -> None:
        """Record and audit a pre-write failure of a claimed proposal."""
        store.finish_execution(proposal.id, executed=False, reason=reason)
        await self._audit_outcome(proposal, "failed", reason)
        self._ui.notify(
            f"Proposal {proposal.action} {meta.plural}/{proposal.name} failed: {reason}",
            severity="error",
        )

    async def _execute(
        self,
        store: ProposalStore,
        proposal: WriteProposal,
        meta: ResourceMeta,
        ns: str | None,
        op: Callable[[str | None], Awaitable[None]],
    ) -> None:
        """Claim and execute an approved proposal under the nav lock so a
        context switch or `:mcp off` cannot interleave: the claim itself is
        linearized with the shutdown/switch expiry sweeps (if one of those
        won while the dialog was open or the lock was contended, there is no
        pending proposal left to claim). After the claim, the context epoch
        and RBAC are rechecked, then the UID binding, then the same
        fail-closed audit-before-mutation path as every other write."""
        async with self._navigation.nav_lock:
            if not store.begin_execution(proposal.id):
                # Cancelled, TTL-expired, or invalidated (MCP shutdown /
                # context switch) before the claim landed: the approval no
                # longer has a pending proposal to execute.
                self._ui.notify(
                    "The proposal was withdrawn before approval landed", severity="warning"
                )
                return
            if proposal.context_epoch != self._context.epoch() or (
                proposal.context != self._config().kube_context
            ):
                await self._fail(store, proposal, meta, "the kube context changed before execution")
                return
            if not await self._writes.permitted(proposal.action, meta, ns, proposal.name):
                await self._fail(store, proposal, meta, "permission revoked before execution")
                return
            args = json.loads(proposal.arguments_json)
            try:
                current_uid = await self._builder().target_uid(args["kind"], ns, proposal.name)
            except ApiStatusError:
                await self._fail(store, proposal, meta, "the target no longer exists")
                return
            if current_uid != proposal.uid:
                await self._fail(
                    store,
                    proposal,
                    meta,
                    "the target was replaced since the proposal was created",
                )
                return
            detail = self.provenance(proposal)
            write = asyncio.ensure_future(
                self._writes.run(
                    proposal.action,
                    meta,
                    ns,
                    proposal.name,
                    lambda: op(proposal.uid),
                    detail=detail,
                )
            )
            try:
                outcome = await asyncio.shield(write)
            except asyncio.CancelledError:
                # Worker cancellation (TUI shutdown) after the claim: the
                # record must still reach a terminal state, never a
                # permanent `approved` over an uncertain cluster outcome.
                await self._settle_interrupted_execution(store, proposal, write)
                raise
            store.finish_execution(
                proposal.id, executed=outcome == "done", reason="" if outcome == "done" else outcome
            )
            if outcome == "done":
                self._ui.notify(f"Executed proposal: {proposal.summary}")

    async def _settle_interrupted_execution(
        self, store: ProposalStore, proposal: WriteProposal, write: asyncio.Future[str]
    ) -> None:
        """A claimed execution's worker was cancelled mid-write. Use the
        write's real outcome when it already settled; otherwise abandon the
        in-flight call and record the uncertainty — the API server may or
        may not have committed the mutation by the time cancellation lands.
        """
        if write.done() and not write.cancelled() and write.exception() is None:
            # WriteCoordinator.run already audited this outcome itself.
            outcome = write.result()
            store.finish_execution(
                proposal.id, executed=outcome == "done", reason="" if outcome == "done" else outcome
            )
            return
        write.cancel()
        reason = "interrupted before completion — the cluster outcome is uncertain"
        store.finish_execution(proposal.id, executed=False, reason=reason)
        # WriteCoordinator.run only got as far as its intent record: the
        # terminal outcome must reach the audit trail even while cancellation
        # is unwinding — shield the append so a second cancel cannot skip it
        # (the offloaded thread completes regardless).
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(self._audit_outcome(proposal, "failed", reason))
