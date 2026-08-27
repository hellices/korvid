"""The write security perimeter, owned by one coordinator (issue #187).

Every cluster mutation korvid performs - a keybinding, a controller flow, an
agent tool call, or an approved external proposal - passes the same ordering:

1. user approval (a `ConfirmScreen` only real key input can resolve);
2. context-epoch and identity revalidation after every awaited gap;
3. a **synchronous** in-flight write reservation, taken where the write
   coroutine is *constructed*, so a `:ctx` queued before the worker starts it
   already sees the write in flight;
4. a fail-closed intent audit - if the record cannot be persisted, the
   mutation is blocked and never constructed;
5. the mutation;
6. the outcome audit and the user-facing notification.

`WriteCoordinator` is that single implementation. It *is* the `WriteGate`
controllers already depend on (a plain class, so it inherits the ABC directly
rather than through an adapter), and it holds the perimeter's own state: the
in-flight write count `:ctx` switching consults, the one-shot
permission-checker warning, and the protected-context marker every approval
dialog must carry.

It reaches Textual only through `UiSurface`, reads the view through
`ViewState`, and revalidates the switch epoch through `ContextGuard`. It never
imports or holds `KorvidApp`, so the whole perimeter is exercised without a
running app.

Interactive writes keep a separate typed contract (`confirm_interactive`).
Their approved form is a subprocess whose auditable facts - the pod kubectl
actually created, its uid, the session's exit outcome - only exist after it
starts, so they audit themselves, fail-closed, and reserve their own write.
What they must not own is the approval, and they do not.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any, ClassVar, Protocol, TypeVar

from korvid.core.audit import AuditLog
from korvid.core.impact import ImpactAction, summarize_impact
from korvid.core.relationships import GraphResource
from korvid.core.store import ALL_NAMESPACES
from korvid.k8s.discovery import ResourceMeta
from korvid.tools.write_coordinator import (
    WRITE_VERBS,
    gvr_label,
    perm_target,
    run_approved_write,
    write_locus,
)
from korvid.ui.impact_preview import render_impact_lines, render_unavailable_lines
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.workspace_controller import ContextGuard, RelationshipLoading
from korvid.ui.workspace_state import PaneState
from korvid.ui.write_gate import ReservedWrite, WriteGate

logger = logging.getLogger(__name__)

_ResultT = TypeVar("_ResultT")

#: Ceiling for the SubjectAccessReview pre-check: it is advisory, so a
#: stalled API server must cost the user a warning, never the write flow.
_PERMISSION_CHECK_TIMEOUT = 10.0

#: Ceiling for a server-side dry run. A preview is display support: on
#: timeout the dialog opens without it rather than not at all.
_PREVIEW_TIMEOUT = 3.0

#: Ceiling for the advisory blast-radius snapshot. Larger than
#: `_PREVIEW_TIMEOUT` because the snapshot is a bounded LIST fan-out across
#: the relationship catalog rather than one request.
_IMPACT_TIMEOUT = 5.0


def canonical_meta_kind(aliases: Mapping[str, ResourceMeta], meta: ResourceMeta) -> str:
    """The alias that names *meta* in this session's alias table.

    Not always the bare plural: when a same-plural resource from another
    group won the alias collision, the group-qualified alias is the one that
    resolves back to this meta, and anything keyed on the kind (the session
    timeline, the write audit's view label) must use it.
    """
    if aliases.get(meta.plural) == meta:
        return meta.plural
    qualified = gvr_label(meta)
    if aliases.get(qualified) == meta:
        return qualified
    return min(
        (alias for alias, candidate in aliases.items() if candidate == meta),
        default=qualified,
    )


@dataclasses.dataclass(frozen=True)
class WriteOrigin:
    """The pane a write flow was raised from, and the scope it was showing.

    Every awaited gap in a write flow (the RBAC round-trip, the dry-run, the
    managed-by lookup, the impact snapshot) is a gap in which the workspace
    can be split, focused across, or re-scoped. `current_kind`,
    `current_scope` and the selected row all delegate to *whichever* pane is
    focused right now, so a flow that re-reads them after an await is
    answering a different question than the one the user asked.

    Captured before the first await, this pins both halves of "where the
    user acted": the pane object itself (identity, not equality - a second
    pane can hold the very same kind, scope and cursor row) and the scope
    that pane was showing (the same pane can widen to every namespace under
    the flow). The impact snapshot is scoped from the capture, and the
    post-snapshot gate refuses unless the capture is still current.
    """

    pane: PaneState
    scope: str

    def is_current(self, pane: PaneState) -> bool:
        """Whether `pane` is still the pane this flow was raised from, on the
        scope it was raised in."""
        return pane is self.pane and pane.scope == self.scope

    def impact_scope(self, meta: ResourceMeta) -> str | None:
        """The namespace an impact snapshot must cover for this target.

        The captured scope for a namespaced target, and *every* namespace
        for a cluster-scoped one (or a captured all-namespaces pane). A Node
        or PersistentVolume is reachable from every namespace: scoping its
        snapshot to the pane the user happens to be in would both hide the
        Pods it runs elsewhere and let the dialog report complete coverage
        of a namespace that was never the whole question. The same value is
        handed to the loader and to `summarize_impact`, so the scope the
        text states is always the scope that was listed - and it is the
        captured one, so a focus change mid-flow cannot silently re-aim the
        snapshot at another pane's view.
        """
        if not meta.namespaced or self.scope == ALL_NAMESPACES:
            return None
        return self.scope


class TimelineWrites(Protocol):
    """The bounded session timeline's write sink (issue #282), structurally.

    Only the one method the audit chokepoint calls: the timeline is
    non-authoritative display state, so the perimeter can neither read,
    navigate nor clear it. A failure to record is the timeline's own
    problem - it must never turn a persisted write into a failed one.
    """

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
        """Mirror one already-persisted audit record into the timeline."""


#: SubjectAccessReview probe: (verb, plural, subresource, namespace, group, name).
PermissionChecker = Callable[[str, str, str, str | None, str, str], Awaitable[bool]]


class WriteCoordinator(WriteGate):
    """Approval, revalidation, reservation, audit and execution of writes."""

    #: Permission identity per action, exposed for the flows that render a
    #: "missing permission: <verb> <target>" message of their own.
    WRITE_VERBS: ClassVar[dict[str, tuple[str, str]]] = WRITE_VERBS

    def __init__(
        self,
        *,
        ui: UiSurface,
        view: ViewState,
        context: ContextGuard,
        audit: Callable[[], AuditLog | None],
        timeline: TimelineWrites,
        check_permission: Callable[[], PermissionChecker | None],
        relationship_loader: Callable[[], RelationshipLoading | None],
        focused_pane: Callable[[], PaneState],
        canonical_meta_kind: Callable[[ResourceMeta], str],
        protected_context: str | None = None,
    ) -> None:
        self._ui = ui
        self._view = view
        self._context = context
        # Late-binding getters: the audit sink, the permission checker and
        # the relationship loader are optional collaborators the composition
        # root may leave unwired, and tests replace them on the constructed
        # app - resolving per call observes both.
        self._audit = audit
        self._timeline = timeline
        self._check_permission = check_permission
        self._relationship_loader = relationship_loader
        self._focused_pane = focused_pane
        self._canonical_meta_kind = canonical_meta_kind
        #: In-flight cluster mutations. `:ctx` switching consults this, so it
        #: is incremented synchronously where a write coroutine is built -
        #: never when it starts running.
        self._active_writes = 0
        #: One-shot flag so a persistently failing SSAR pre-check warns once
        #: rather than on every write.
        self._permission_check_warned = False
        #: Active context's name when it matches `protected_contexts` (issue
        #: #83); None otherwise. Re-derived by every `:ctx` switch, and the
        #: reason every approval dialog is built here.
        self._protected_context = protected_context

    # ------------------------------------------------------------------
    # Perimeter state, observable through narrow reads
    # ------------------------------------------------------------------

    def active_writes(self) -> int:
        """How many cluster mutations are in flight right now.

        Read-only and synchronous on purpose: `:ctx` switching refuses while
        this is non-zero, and it must see a write reserved by a confirmation
        callback that has not reached its worker yet.
        """
        return self._active_writes

    def reserve_write(self) -> Callable[[], None]:
        self._active_writes += 1
        released = False

        def release() -> None:
            # Idempotent: fired by the write body's finally, by
            # ReservedWrite.close() for a coroutine that never started, and
            # by the finalizer as a backstop - a leaked +1 would block every
            # future `:ctx` switch for the session's lifetime.
            nonlocal released
            if not released:
                released = True
                self._active_writes -= 1

        return release

    def reserved(
        self, factory: Callable[[], Coroutine[Any, Any, _ResultT]]
    ) -> Coroutine[Any, Any, _ResultT]:
        """Wrap an approved write so its reservation is taken *here*.

        Synchronous by contract: a confirmation callback builds the
        coroutine and hands it to `run_worker`, which only starts it on a
        later event-loop iteration, and a `:ctx` processed in that gap must
        already see the write in flight.

        Takes a *factory*, not a coroutine: the body is created inside the
        wrapper, so a wrapper that is closed before it ever runs leaves no
        un-awaited coroutine behind. `ReservedWrite` covers every way a
        write can end without its body running - a worker Task cancelled
        before its first step (which arrives as a thrown `CancelledError`,
        not a `close()`), an explicit `close()`, and app shutdown - so the
        release never depends on the garbage collector. The finalizer stays
        as a backstop.
        """
        release = self.reserve_write()

        async def run() -> _ResultT:
            try:
                return await factory()
            finally:
                release()

        coro = ReservedWrite(run(), release)
        weakref.finalize(coro, release)
        return coro

    def audit_configured(self) -> bool:
        return self._audit() is not None

    def epoch(self) -> int:
        return self._context.epoch()

    def switching(self) -> bool:
        return self._context.switching()

    def reads_allowed(self) -> bool:
        return self._context.reads_allowed()

    @property
    def protected_context(self) -> str | None:
        """The active protected context's name, or None."""
        return self._protected_context

    def set_protected_context(self, name: str | None) -> None:
        """Adopt the marker a `:ctx` switch re-derived for the new cluster."""
        self._protected_context = name

    # ------------------------------------------------------------------
    # Identity: targets, loci, and the pane a flow was raised from
    # ------------------------------------------------------------------

    @staticmethod
    def perm_target(action: str, meta: ResourceMeta) -> tuple[str, str]:
        """(verb, resource[/subresource]) as shown in permission messages."""
        return perm_target(action, meta)

    @staticmethod
    def write_locus(ns: str | None) -> str:
        """Namespace qualifier for approval dialogs and agent tool results."""
        return write_locus(ns)

    def write_origin(self) -> WriteOrigin:
        """Pin the pane a write flow is being raised from, and its scope.

        Called before the flow's first await, next to the target capture:
        everything after that reads "the focused pane", which is a moving
        target across a split workspace.
        """
        pane = self._focused_pane()
        return WriteOrigin(pane, pane.scope)

    def write_target(self) -> tuple[ResourceMeta, str | None, str, str | None] | None:
        """Resolve (meta, namespace, name, uid) of the selected row for a
        write, or None (with a notification) when writes are disabled or
        nothing usable is selected. Cluster-scoped kinds get namespace=None.
        The uid pins the object incarnation the user saw: if it is deleted
        and recreated under the same name while the dialog is open, the API
        server rejects the write with a 409 instead of hitting the
        replacement."""
        if self._view.readonly():
            self._ui.notify("Read-only mode: cluster writes are disabled", severity="warning")
            return None
        if self._audit() is None:
            # Fail-closed auditing (AGENTS.md): no audit sink means no writes.
            self._ui.notify("Writes disabled: no audit log configured", severity="warning")
            return None
        kind = self._view.canonical_kind(self._view.current_kind())
        meta = self._view.aliases().get(kind)
        if meta is None:
            self._ui.notify(f"Unknown resource kind {kind!r}", severity="warning")
            return None
        if meta.synthetic:
            # Helm browser rows etc. are read-only views over other objects.
            self._ui.notify(f"{meta.kind} is a read-only view", severity="warning")
            return None
        ns, name = self._view.selected_ns_name()
        if name is None:
            return None
        namespace = ns if meta.namespaced and ns else None
        return meta, namespace, name, self._view.selected_uid(namespace, name)

    def current_replicas(self, ns: str | None, name: str) -> int | None:
        """Desired replicas of the selected row, or None when the summary type
        does not carry it (0 would be indistinguishable from scaled-to-zero)."""
        for obj in self._view.resources(self._view.current_kind(), self._view.current_scope()):
            if obj.namespace == (ns or "") and obj.name == name:
                desired = getattr(obj, "desired", None)
                return None if desired is None else int(desired)
        return None

    @staticmethod
    def is_scale_down(current: int | None, replicas: int) -> bool:
        """Whether this scale is a *known* decrease.

        A summary that carries no desired count (`None`) is not a decrease:
        korvid cannot tell one from a scale-up, and an impact summary is
        only ever attached to an action whose semantics are known.
        """
        return current is not None and replicas < current

    def uid_intact_after_fetch(
        self, manifest: dict[str, Any], ns: str | None, name: str, uid: str | None
    ) -> bool:
        """Post-await UID guarantee: after a manifest fetch, both the fetched
        object and the selected row must still be the incarnation the user
        acted on. An object deleted and recreated under the same name would
        otherwise render in the dialog while the write pins the stale UID
        (guaranteed conflict at best, wrong-object action at worst)."""
        if not uid:
            return True
        fetched_uid = str(manifest.get("metadata", {}).get("uid") or "")
        if fetched_uid and fetched_uid != uid:
            return False
        return self._view.selected_uid(ns, name) == uid

    # ------------------------------------------------------------------
    # Revalidation after an awaited gap
    # ------------------------------------------------------------------

    def context_intact(
        self,
        action: str,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        *,
        phase: str = "the permission check",
        epoch: int,
        origin: WriteOrigin | None = None,
    ) -> bool:
        """Re-validate after an awaited gap (the RBAC round-trip, a dry-run
        preview, or an editor session - named by ``phase`` so cancellation
        messages state the true cause), before pushing a dialog: the user may
        have opened another screen or moved the selection meanwhile - and
        keystrokes typed during the await must never land on a confirmation
        they did not see. ``epoch`` (captured when the write flow began) also
        aborts on a context switch that started - or fully completed - during
        the gap: a same-named row on the new cluster would otherwise satisfy
        the selection checks. ``origin`` (a `WriteOrigin` captured with the
        target) additionally pins *which pane* the flow was raised from and
        the scope it showed: the selection checks below all read the focused
        pane, so without it a focus change to a second pane whose cursor sits
        on the same object - or a re-scope of the pane itself - passes every
        one of them. Abort (with a notification) unless everything still
        matches."""
        if self._context.crossed(epoch):
            self._ui.notify(
                f"{action} {gvr_label(meta)}/{name} cancelled -"
                f" the kube context changed during {phase}",
                severity="warning",
            )
            return False
        if self._ui.screen_depth() > 1:
            self._ui.notify(
                f"{action} {gvr_label(meta)}/{name} cancelled -"
                f" another dialog opened during {phase}",
                severity="warning",
            )
            return False
        kind = self._view.canonical_kind(self._view.current_kind())
        current_ns, current_name = self._view.selected_ns_name()
        if (
            (origin is not None and not origin.is_current(self._focused_pane()))
            # Value comparison, not identity: background discovery replaces
            # alias values with freshly constructed (equal) ResourceMeta
            # instances, which must not cancel a write on the same row -
            # the editor round-trip in particular is arbitrarily long.
            or self._view.aliases().get(kind) != meta
            or current_name != name
            or (meta.namespaced and (current_ns or None) != ns)
        ):
            self._ui.notify(
                f"{action} {gvr_label(meta)}/{name} cancelled -"
                f" the selection changed during {phase}",
                severity="warning",
            )
            return False
        return True

    def identity_intact(
        self,
        action: str,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        *,
        phase: str,
        epoch: int,
        origin: WriteOrigin | None = None,
    ) -> bool:
        """`context_intact` plus the captured UID.

        Kind, namespace and name all survive a delete-and-recreate under the
        same name, so the context check alone cannot see one: the dialog
        would describe (and the impact summary would explain) an object that
        no longer exists, while the write pins the uid the user saw and can
        only 409. The uid is the one part of the identity that changes, so
        an awaited gap that could span a replacement rechecks it here.

        A row whose summary type carries no uid (``uid is None``) keeps the
        pre-existing behaviour exactly: nothing to compare, no new refusal.
        A captured uid that no longer resolves is *not* the same case: the
        comparison below fails closed, because "korvid can no longer see the
        incarnation the user approved" is exactly what a replacement looks
        like from here.
        """
        if not self.context_intact(action, meta, ns, name, phase=phase, epoch=epoch, origin=origin):
            return False
        if uid is None or self._view.selected_uid(ns, name) == uid:
            return True
        self._ui.notify(
            f"{action} {gvr_label(meta)}/{name} cancelled - the selection changed during {phase}",
            severity="warning",
        )
        return False

    def scale_identity_intact(
        self,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        current: int | None,
        *,
        phase: str,
        epoch: int,
        origin: WriteOrigin,
    ) -> bool:
        """`identity_intact` plus the desired replica count the scale flow
        captured.

        A scale is the one write whose *meaning* is not fixed by its
        identity: the same requested count is a decrease or an increase
        depending on where the object stands when it is requested. The
        captured count decides whether korvid loads a scale-down's blast
        radius and what the approval line says it is changing from, and
        every awaited gap in the flow - the permission round trip, the count
        prompt, the dry run, the snapshot, the approval dialog itself - is
        long enough for a controller, an autoscaler or another operator to
        move `spec.replicas` under an otherwise unchanged incarnation.
        `identity_intact` cannot see that: kind, namespace, name, uid, pane
        and scope all still match.

        So the count is compared too, and a change ends the flow with its
        own banner rather than a stale `old -> new` line or a scale-down
        section attached to what is now an increase. `None` is a captured
        value like any other: a row that gained a readable count mid-flow
        drifted exactly as much as one whose number moved, and comparing
        equality keeps both directions closed.

        Identity is checked first because `current_replicas` reads
        `current_kind` and `current_scope` from the focused pane; only a
        current `origin` makes that count belong to this write flow.
        """
        if not self.identity_intact(
            "scale", meta, ns, name, uid, phase=phase, epoch=epoch, origin=origin
        ):
            return False
        if self.current_replicas(ns, name) == current:
            return True
        self._ui.notify(
            f"scale {gvr_label(meta)}/{name} cancelled -"
            f" the desired replica count changed during {phase}",
            severity="warning",
        )
        return False

    # ------------------------------------------------------------------
    # Permission pre-check
    # ------------------------------------------------------------------

    async def permitted(
        self, action: str, meta: ResourceMeta, namespace: str | None, name: str
    ) -> bool:
        """SubjectAccessReview pre-check at the approval stage (spec §5 #5):
        surface 'missing permission' before the dialog instead of after a
        failed mutation. No checker injected -> allowed (still gated+audited)."""
        check = self._check_permission()
        if check is None:
            return True
        verb, subresource = WRITE_VERBS[action]
        try:
            allowed = await asyncio.wait_for(
                check(verb, meta.plural, subresource, namespace, meta.group, name),
                timeout=_PERMISSION_CHECK_TIMEOUT,
            )
        except Exception:
            # Fail-open, but visibly: warn once so a persistently failing
            # checker (e.g. SSAR forbidden) does not disable the gate silently.
            if self._permission_check_warned:
                logger.debug("permission pre-check failed; allowing", exc_info=True)
            else:
                self._permission_check_warned = True
                logger.warning(
                    "permission pre-check failed; allowing (fail-open) -"
                    " writes remain approval-gated and audited",
                    exc_info=True,
                )
            return True
        if not allowed:
            _, target = self.perm_target(action, meta)
            self._ui.notify(f"missing permission: {verb} {target}", severity="error")
        return allowed

    async def precheck_keybinding_write(
        self, action: str, meta: ResourceMeta, ns: str | None, name: str
    ) -> bool:
        """RBAC pre-check plus post-await re-validation for binding handlers:
        the check is an API round trip, so confirm the screen and selection
        are unchanged before any dialog is pushed."""
        if self._context.switching():
            # The write would race the teardown/retarget and could execute
            # against whichever cluster wins — refuse up front.
            self._ui.notify(
                "A context switch is in progress — try again once it completes",
                severity="warning",
            )
            return False
        epoch = self._context.epoch()
        if not await self.permitted(action, meta, ns, name):
            return False
        # The permission check awaited network I/O — a switch may have
        # started (flag) or fully completed (epoch) meanwhile; the approved
        # intent must not land on a different cluster.
        return self.context_intact(action, meta, ns, name, epoch=epoch)

    # ------------------------------------------------------------------
    # Audited execution
    # ------------------------------------------------------------------

    async def audit_write(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        detail: str,
        outcome: str,
    ) -> None:
        """Append one audit record; raises if it cannot be persisted (the
        caller decides whether that blocks the write - see `run`).

        The single chokepoint where a write reaches the session timeline
        too: the entry is appended only after the durable append returned,
        so a failed audit can never leave a success-shaped record behind
        (auditing is fail-closed — AGENTS.md).
        """
        audit = self._audit()
        if audit is None:
            raise RuntimeError("audit log not configured")
        await asyncio.to_thread(
            lambda: audit.append(
                action=action,
                kind=meta.plural,
                group=meta.group,
                version=meta.version,
                namespace=namespace,
                name=name,
                detail=detail,
                outcome=outcome,
            )
        )
        self._timeline.record_write(
            epoch=self._context.epoch(),
            action=action,
            kind_alias=self._canonical_meta_kind(meta),
            display_kind=meta.kind,
            namespace=namespace,
            name=name,
            outcome=outcome,
        )

    def run(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        op_factory: Callable[[], Awaitable[None]],
        detail: str = "",
    ) -> Coroutine[Any, Any, str]:
        """Execute an approved write with fail-closed auditing (AGENTS.md):
        the intent record must persist *before* the mutation - if it cannot,
        the write is blocked. Returns a short outcome string ('done' /
        'blocked: ...' / 'failed: ...') for callers that report back.

        Synchronous, returning an unstarted coroutine: the in-flight cluster
        write is reserved *here*, so a `:ctx` queued between the confirmation
        callback and `run_worker` starting the coroutine already sees it.
        Wrapping this in an async adapter reintroduces exactly that gap.

        Takes an operation *factory* so the mutation coroutine is never
        created until intent is audited — a declined or blocked write
        produces no unawaited coroutine to leak.
        """
        return self.reserved(
            lambda: self._run_write(action, meta, namespace, name, op_factory, detail)
        )

    async def _run_write(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        op_factory: Callable[[], Awaitable[None]],
        detail: str,
    ) -> str:
        """The reserved body: the whole span publishes an in-flight progress
        label (issue #143) — between approval and the outcome toast there was
        previously no visible state at all. The audit -> mutate -> audit
        sequence itself is `korvid.tools.write_coordinator.
        run_approved_write` (issue TBD) — a pure extraction so the same
        fail-closed ordering is available to any future non-Textual caller."""
        kind = meta.plural
        with self._ui.progress(f"{action} {kind}/{name}"):
            return await run_approved_write(
                action,
                meta,
                namespace,
                name,
                op_factory,
                detail,
                audit=self.audit_write,
                notify=self._ui.notify,
            )

    async def run_shielded(
        self,
        action: str,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        op_factory: Callable[[], Awaitable[None]],
        *,
        detail: str,
    ) -> str:
        """Run an approved agent write to completion even if the turn is
        interrupted mid-flight (issue #170): a half-applied, half-audited
        mutation is worse than finishing what the user explicitly approved.
        Every wait stays shielded — repeated cancellations are absorbed until
        the write reaches a terminal state, then cancellation is re-raised."""
        write = asyncio.ensure_future(self.run(action, meta, ns, name, op_factory, detail=detail))
        interrupted = False
        while not write.done():
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                if write.cancelled():
                    raise
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError
        return write.result()

    # ------------------------------------------------------------------
    # Previews: display support only, and fail-open in every direction
    # ------------------------------------------------------------------

    async def dry_run_preview(self, coro: Awaitable[list[str] | None]) -> list[str] | None:
        """Await a WriteOps preview with a hard deadline; None on timeout or
        any error (the dialog then opens without a preview, exactly as before
        issue #19 - a preview must never block or break the approval flow)."""
        try:
            return await asyncio.wait_for(coro, _PREVIEW_TIMEOUT)
        except Exception:
            logger.debug("dry-run preview failed; dialog opens without it", exc_info=True)
            return None

    async def impact_preview_for_scope(
        self,
        action: ImpactAction,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        *,
        scope: str | None,
    ) -> tuple[str, ...] | None:
        """`impact_preview` against an explicitly chosen snapshot scope, for
        the flows that have no origin pane (an agent-requested write is not
        raised from a pane at all)."""
        loader = self._relationship_loader()
        if loader is None or uid is None:
            return None
        root = GraphResource(
            group=meta.group, kind=meta.kind, namespace=ns or "", name=name, uid=uid
        )
        try:
            async with asyncio.timeout(_IMPACT_TIMEOUT):
                graph = await loader.load(root, scope, self._view.aliases())
                return render_impact_lines(summarize_impact(graph, action, root, scope=scope))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Type only: never the message (CodeQL py/clear-text-logging-
            # sensitive-data), and never anything derived from a manifest.
            logger.debug("impact summary unavailable for %s: %s", action, type(exc).__name__)
            return render_unavailable_lines(action, meta.group, meta.kind)

    async def impact_preview(
        self,
        action: ImpactAction,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        *,
        origin: WriteOrigin,
    ) -> tuple[str, ...] | None:
        """Advisory blast-radius lines for a write dialog (issue #283).

        Reuses the relationship snapshot loader `g` already owns - no new
        LIST/GET interface and no per-node fan-out - with the exact
        group/kind/namespace/name/uid identity the write will target, so a
        recreated same-named object is reported as absent from the snapshot
        rather than summarized as the one on screen. Display support only,
        and fail-open in four distinct ways:

        - no loader wired (no cluster connection) -> None, no section at all;
        - no uid for the selected row -> None, no section and no LIST. The
          summary is keyed on the exact incarnation, so a uid-less identity
          matches no snapshot node and would render `target not found in
          this snapshot` for a row that is plainly on screen - a claim about
          the object rather than about korvid's missing uid. Resolving the
          target by name instead is worse: it would silently reconnect the
          preview to whatever object currently holds that name, exactly the
          reconnection `GraphResource` refuses for an unresolved reference.
          Nothing else changes - the approval gate, the uid-less write, and
          the audit record are what they were;
        - a timeout or unexpected failure *anywhere* in load, summarize, or
          render -> the static "impact unavailable" advisory, because an API
          error message can embed a response body (for a Secret, its data)
          and must never reach the dialog, and because a summarizer or
          renderer bug must cost the user the section, not the approval.
          It is rendered for the action and target *type* in hand - group
          and kind, since only `apps/StatefulSet` has the field the last
          line names - so a scale-down still states the machine-defined
          limitations it always states (PodDisruptionBudgets do not gate a
          controller scale-down, an HPA's own loop is not evaluated, and an
          `apps/StatefulSet`'s PVC retention policy is not either) - none of
          those depends on the snapshot that failed to arrive;
        - cancellation (a `:ctx` switch tearing the client down) propagates
          untouched, exactly like every other awaited read here.

        The summary itself can never approve, execute, or reserve a write:
        it returns text.

        `origin` is the pane the flow was raised from: its captured scope
        decides what the snapshot covers, so a focus change during an
        earlier await cannot silently re-aim the snapshot (and the
        `scope:`/`graph coverage:` lines derived from it) at another pane's
        view of the cluster.
        """
        return await self.impact_preview_for_scope(
            action, meta, ns, name, uid, scope=origin.impact_scope(meta)
        )

    # ------------------------------------------------------------------
    # Approval dialogs
    # ------------------------------------------------------------------

    def confirm_screen(
        self,
        title: str,
        operation: str,
        *,
        require_name: str | None = None,
        preview: list[str] | None = None,
        preview_title: str = "server dry-run preview:",
        managed_note: str | None = None,
        impact_lines: tuple[str, ...] | None = None,
    ) -> ConfirmScreen:
        """Build every write-approval dialog through one place so the
        protected-context layer (issue #83) can never be forgotten: while a
        protected context is active, all confirms carry the red banner and
        demand a typed name instead of `y`."""
        return ConfirmScreen(
            title,
            operation,
            require_name=require_name,
            preview=preview,
            preview_title=preview_title,
            protected_context=self._protected_context,
            managed_note=managed_note,
            impact_lines=impact_lines,
        )

    async def confirm(
        self,
        title: str,
        operation: str,
        *,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        op_factory: Callable[[], Awaitable[None]],
        detail: str = "",
        require_name: str | None = None,
        preview: list[str] | None = None,
        preview_title: str = "server dry-run preview:",
        managed_note: str | None = None,
        impact_lines: tuple[str, ...] | None = None,
        approval_guard: Callable[[], bool] | None = None,
    ) -> None:
        """The standard write-approval flow (issue #91 U1): push a confirm
        dialog and, on approval, launch `run` on a supervised worker.

        Takes an operation *factory*, not a coroutine: a declined dialog
        must never construct the mutation coroutine (nothing to leak
        unawaited, no side effects before approval). Flows with extra
        semantics — operator install's in-callback UID recheck, drain's
        dedicated worker, the agent gate's approval future — stay explicit.

        `approval_guard` is an optional last re-validation for flows whose
        dialog can go stale while it is open. The dialog is the longest
        awaited gap a write has - it stays up until the user answers - and
        every earlier gate has already passed by then, so a flow that
        depends on state outside the target's identity needs one more look
        before it commits. It is deliberately *synchronous*: it must not
        introduce an await of its own between the approval and the write.

        The guard runs only on a fresh approval, so it can never become a
        second path *to* the write - it can only refuse one the user already
        gave - and it runs before `run` is even constructed, so a refusal
        leaves no write reservation, no audit record, and no operation. It
        is deferred by one loop iteration because Textual invokes this
        result callback *before* it pops the dismissed screen: run inline,
        any selection check would see the confirmation itself still on the
        screen stack and read as "another dialog opened".

        Omitting it (the default) keeps every other write flow on exactly
        the callback it had: approval launches the worker in the same
        iteration, unguarded. Scale needs this safeguard because the captured
        replica count defines the action as a decrease or increase. Applying
        the same dialog-gap identity guard to delete/restart is tracked
        separately in issue #297.
        """

        def _launch() -> None:
            self._ui.run_worker(self.run(action, meta, namespace, name, op_factory, detail=detail))

        def _launch_if(guard: Callable[[], bool]) -> None:
            if guard():
                _launch()

        def _done(confirmed: bool | None) -> None:
            if not confirmed:
                return
            if approval_guard is None:
                _launch()
                return
            self._ui.call_later(_launch_if, approval_guard)

        await self._ui.push_screen(
            self.confirm_screen(
                title,
                operation,
                require_name=require_name,
                preview=preview,
                preview_title=preview_title,
                managed_note=managed_note,
                impact_lines=impact_lines,
            ),
            _done,
        )

    async def confirm_interactive(
        self,
        title: str,
        operation: str,
        *,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        epoch: int,
        op_factory: Callable[[], Awaitable[None]],
    ) -> None:
        """Approval for an operation that runs as an interactive subprocess.

        `ConfirmScreen`, like every other write: its creation-time key
        cutoff discards input buffered before the prompt existed, so a
        queued Enter can never approve a privileged pod the user has not
        seen.

        The dialog is an awaited gap, so the epoch is rechecked on the way
        out. Without it an approval left open across a `:ctx` switch would
        start the subprocess against whichever cluster is current when the
        user finally answers - and for these flows kubectl addresses the
        target by name, so a same-named node or pod elsewhere would be
        accepted.

        The intent audit belongs to the operation here, not to the gate:
        these flows record the pod kubectl actually created, its uid, and
        the session outcome, which `run` cannot know.
        """

        def _done(confirmed: bool | None) -> None:
            if not confirmed:
                return
            if self._context.crossed(epoch):
                self._ui.notify(
                    f"{action} {gvr_label(meta)}/{name} cancelled - the kube context changed",
                    severity="warning",
                )
                return
            self._ui.run_worker(op_factory())

        await self._ui.push_screen(self.confirm_screen(title, operation), _done)
