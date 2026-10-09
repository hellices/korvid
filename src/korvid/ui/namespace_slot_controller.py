"""The one effective 1-9 namespace map behind dispatch, help and the picker (issue #406).

A namespace the user switches to takes the lowest free slot (`visit`).
Discovery results arrive from the existing namespace listings (the
completion prefetch and the `:ns` picker) and only judge availability. They
carry a generation token captured before the listing awaited. Activation and
deactivation advance the generation, so a listing that outlives a `:ctx`
switch is discarded instead of judging the new cluster's slots.

Saves take a cross-process lock and fsync, so they never run on the event
loop: one worker writes queued maps in a thread, oldest first. Activation
reads under the same lock, and `shutdown` writes what the app's worker
sweep left queued.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import Awaitable, Callable, Sequence

from korvid.core.errors import explain_api_error
from korvid.core.namespace_slot_store import ClusterIdentity, NamespaceSlotStore, SlotStateError
from korvid.core.namespace_slots import SlotEntry, SlotMap, build, place, preview, reallocate
from korvid.core.store import ALL_NAMESPACES
from korvid.k8s.errors import ApiStatusError
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.read_availability import NAMESPACE_LISTING_UNAVAILABLE
from korvid.ui.ui_surface import UiSurface

_STILL_LOADING = "Namespace slots are still loading - run :slots again"
_PICKER_OPEN = UnavailableReason(AvailabilityCode.PROTECTED_UI, "Close the namespace picker first")

#: The kubeconfig-scoped namespace listing, None when it is not wired.
ListNamespaces = Callable[[], Awaitable[list[str]]]


def notify_namespace_list_error(ui: UiSurface, exc: ApiStatusError) -> None:
    """403 is an authorization boundary (issue #108): show one concise
    permission notice pointing at `:ns <name>` free-text entry - never
    manufacture a namespace list from configuration."""
    msg = explain_api_error(exc.status, exc.reason, "namespaces", None)
    if exc.status == 403:
        msg += " Switch directly with `:ns <name>`."
    ui.notify(msg, title="Failed to list namespaces", severity="error")


@dataclasses.dataclass(frozen=True)
class SlotPersistence:
    """Where automatic slots are saved and how the live cluster is named."""

    store: NamespaceSlotStore
    #: `(context, server)` for the explicit context, or None if unresolved.
    identity: Callable[[str | None], tuple[str, str] | None]


class NamespaceSlotController:
    """Owns the automatic slots, their generation and their persistence."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        pinned: Callable[[], Sequence[str]],
        context: Callable[[], str | None],
        list_namespaces: Callable[[], ListNamespaces | None],
        persistence: SlotPersistence | None,
        can_open: Callable[[], UnavailableReason | None],
        picker_open: Callable[[], bool],
    ) -> None:
        self._ui = ui
        self._pinned = pinned
        self._context = context
        self._list_namespaces = list_namespaces
        self._persistence = persistence
        self._can_open = can_open
        self._picker_open = picker_open
        self._generation = 0
        self._identity: ClusterIdentity | None = None
        self._auto: dict[int, SlotEntry] = {}
        self._stale = False
        #: Names from this cluster's latest complete listing, None until one lands.
        self._inventory: frozenset[str] | None = None
        #: Bumped by every listing outcome, so a slower one never replaces it.
        self._observed = 0
        #: The latest listing outcome (None when it failed), its cluster and
        #: revision: a save rebased after its cluster was left is judged by it.
        self._listed: tuple[ClusterIdentity, int, frozenset[str] | None] | None = None
        #: Set once a save failed or the document is unreadable for this
        #: cluster: later saves are skipped so one fault reports once.
        self._persist_failed = False
        #: The latest unsaved automatic map per cluster, drained by `_flush`.
        self._pending: dict[ClusterIdentity, dict[int, SlotEntry]] = {}
        self._flushing = False
        #: Serializes every write, so an older map never lands after a newer one.
        self._save_lock = asyncio.Lock()
        #: The generation `activate` is restoring, None once it settled.
        self._restoring: int | None = None
        #: The generation whose restore was cancelled; `shutdown` finishes it.
        self._interrupted: int | None = None
        #: The latest thread write; it outlives a cancelled worker.
        self._writing: asyncio.Future[None] | None = None

    @property
    def slots(self) -> SlotMap:
        """The effective map: current pins over the automatic entries."""
        return build(self._pinned(), self._auto, None)

    @property
    def stale(self) -> bool:
        """Whether the latest discovery for this cluster failed."""
        return self._stale

    @property
    def saving(self) -> bool:
        """Whether a slot map is still queued or being written."""
        return self._flushing or self._save_lock.locked()

    def token(self) -> int:
        """Capture before a listing awaits; pass back with its result."""
        return self._generation

    def deactivate(self) -> None:
        """Forget the cluster's automatic slots while a `:ctx` switch runs."""
        self._generation += 1
        self._identity = None
        self._auto = {}
        self._stale = False
        self._inventory = None
        self._persist_failed = False

    async def activate(self) -> None:
        """Restore the saved automatic slots of the cluster now connected."""
        self.deactivate()
        generation = self._restoring = self._generation
        try:
            await self._restore(generation)
        except asyncio.CancelledError:
            self._interrupted = generation  # unmount or `:ctx` reaped it
            raise
        finally:
            if self._restoring == generation:
                self._restoring = None

    async def _restore(self, generation: int) -> None:
        persistence = self._persistence
        if persistence is None:
            return
        context = self._context()
        resolved = await asyncio.to_thread(persistence.identity, context)
        if generation != self._generation or resolved is None:
            return
        identity = ClusterIdentity(*resolved)
        failure: Exception | None = None
        # A write in flight or a map still queued for this cluster is newer
        # than the file: wait for the write, and adopt the queued map.
        async with self._save_lock:
            queued = self._pending.get(identity)
            try:
                saved = (
                    dict(queued)
                    if queued is not None
                    else await asyncio.to_thread(persistence.store.load, identity)
                )
            except (OSError, SlotStateError) as exc:
                saved, failure = {}, exc
        if generation != self._generation:
            return
        if failure is not None:
            self._report_persist_failure(f"Namespace slots will not be saved: {failure}", "warning")
        # Visits made while the identity resolved belong to this cluster:
        # they take free slots of the restored map instead of being lost.
        visited = [entry.namespace for _, entry in sorted(self._auto.items())]
        self._identity = identity
        self._auto = saved
        self._replay(visited)

    def observe(self, token: int, names: Sequence[str]) -> None:
        """Judge availability from a complete listing; it never assigns a slot."""
        if token != self._generation:
            return
        self._observed += 1
        self._stale = False
        self._inventory = frozenset(names)
        if self._identity is not None:
            self._listed = (self._identity, self._observed, self._inventory)
        self._update(build(self._pinned(), self._auto, self._inventory).automatic())

    def visit(self, namespace: str) -> None:
        """Give a namespace the user switched to the lowest free slot.

        A name the latest complete listing lacks (a mistyped `:ns`) is not
        assigned. Without a listing the visit is trusted, and the next
        complete listing marks it unavailable if it does not exist.
        """
        if not namespace or namespace == ALL_NAMESPACES:
            return
        if self._inventory is not None and namespace not in self._inventory:
            return
        self._update(place(self.slots, namespace).automatic())

    def _replay(self, visited: Sequence[str]) -> None:
        """Give *visited* namespaces free slots of the current map, in order,
        judged against the latest complete listing (one may land mid-restore)."""
        merged = self.slots
        for namespace in visited:
            merged = place(merged, namespace)
        self._update(build(self._pinned(), merged.automatic(), self._inventory).automatic())

    def _update(self, updated: dict[int, SlotEntry]) -> None:
        if updated == self._auto:
            return
        self._auto = updated
        self._persist(updated)

    def observe_failure(self, token: int) -> None:
        """A failed or denied listing infers nothing; the map is marked stale.

        The inventory becomes unknown again, so visits are trusted until the
        next complete listing; the entries keep their availability.
        """
        if token == self._generation:
            self._observed += 1
            self._stale = True
            self._inventory = None
            if self._identity is not None:
                self._listed = (self._identity, self._observed, None)

    def target(self, slot: int) -> str | None:
        """The namespace key *slot* navigates to, or None (notifies if unavailable)."""
        entry = self.slots.get(slot)
        if entry is None:
            return None
        if not entry.available:
            self._ui.notify(
                f"Slot {slot} ({entry.namespace}) is no longer listed - "
                "run :slots to reallocate, or :ns <name> to switch directly",
                severity="warning",
                markup=False,
            )
            return None
        return entry.namespace

    def guard(self, slot: int, namespace: str, outer: Callable[[], bool]) -> Callable[[], bool]:
        """*outer*, narrowed to *slot* still dispatching to *namespace*: a key
        waits for the navigation lock, and discovery may change its slot."""

        def held() -> bool:
            entry = self.slots.get(slot)
            return (
                outer() and entry is not None and entry.available and entry.namespace == namespace
            )

        return held

    def _report_persist_failure(self, message: str, severity: str) -> None:
        if self._persist_failed:
            return
        self._persist_failed = True
        self._ui.notify(
            message,
            title="Namespace slots",
            severity="error" if severity == "error" else "warning",
            markup=False,
        )

    def _persist(self, slots: dict[int, SlotEntry]) -> None:
        """Queue *slots* for this cluster; `_flush` writes them off the loop."""
        identity = self._identity
        if self._persistence is None or identity is None or self._persist_failed:
            return
        self._pending[identity] = slots
        if not self._flushing:
            self._flushing = True
            self._ui.run_worker(self._flush(), group="namespace-slot-save", exit_on_error=False)

    async def _flush(self) -> None:
        try:
            while self._pending:
                async with self._save_lock:
                    if not self._pending:
                        break  # a confirmed reallocation superseded the queue
                    identity = next(iter(self._pending))
                    slots = self._pending.pop(identity)
                    try:
                        await self._save(identity, slots)
                    except (OSError, SlotStateError) as exc:
                        self._report_save_failure(identity, exc)
        finally:
            self._flushing = False

    def _report_save_failure(self, identity: ClusterIdentity, exc: Exception) -> None:
        if identity == self._identity:
            self._report_persist_failure(f"Could not save namespace slots: {exc}", "error")
            return
        # A cluster switched away from: its map is lost on restart, so say
        # so, without suppressing the active cluster's saves.
        self._ui.notify(
            f"Could not save namespace slots for context {identity.context!r}: {exc}",
            title="Namespace slots",
            severity="error",
            markup=False,
        )

    async def _save(self, identity: ClusterIdentity, slots: dict[int, SlotEntry]) -> None:
        """Write *slots* in a thread; the caller holds `_save_lock`.

        The write is shielded: a cancelled worker leaves it running, and
        `shutdown` waits for it before writing anything newer.
        """
        if self._persistence is not None:
            store = self._persistence.store
            self._writing = asyncio.ensure_future(asyncio.to_thread(store.save, identity, slots))
            await asyncio.shield(self._writing)

    async def shutdown(self) -> None:
        """Write what the app's worker sweep left: Textual cancels workers
        before `on_unmount`, so queued maps would otherwise be lost on quit.

        A restore that quitting cut short is finished first, so visits made
        while it ran merge into the saved map. Errors are dropped - nothing
        is left on screen to report them.
        """
        self._flushing = True  # no worker runs any more: queue, then drain here
        if self._interrupted is not None and self._interrupted == self._generation:
            await self._restore(self._generation)
        async with self._save_lock:
            if self._writing is not None:
                with contextlib.suppress(OSError, SlotStateError):
                    await self._writing
            while self._pending:
                identity = next(iter(self._pending))
                with contextlib.suppress(OSError, SlotStateError):
                    await self._save(identity, self._pending.pop(identity))

    # ------------------------------------------------------------------
    # Explicit reallocation (`:slots`)
    # ------------------------------------------------------------------

    def _blocked(self) -> UnavailableReason | None:
        """An open dialog, or the `:ns` picker whose slot labels a confirmed
        reallocation would leave stale while the keys dispatch the new map."""
        return self._can_open() or (_PICKER_OPEN if self._picker_open() else None)

    def unavailable_reason(self) -> UnavailableReason | None:
        """Why `:slots` would refuse now - a silent probe that never lists."""
        reason = self._blocked()
        if reason is None and self._list_namespaces() is None:
            return NAMESPACE_LISTING_UNAVAILABLE
        return reason

    def open_reallocation(self) -> None:
        """List namespaces afresh, then preview a rebuilt map for confirmation."""
        reason = self.unavailable_reason()
        lister = self._list_namespaces()
        if reason is None and lister is not None:
            self._ui.run_worker(
                self._reallocate(lister),
                group="namespace-slots",
                exclusive=True,
                exit_on_error=False,
            )
            return
        reason = reason or NAMESPACE_LISTING_UNAVAILABLE
        self._ui.notify(reason.message, severity=reason.severity, markup=False)

    async def _list(self, lister: ListNamespaces, token: int) -> list[str] | None:
        try:
            return await lister()
        except ApiStatusError as exc:
            self.observe_failure(token)
            if token == self._generation:
                notify_namespace_list_error(self._ui, exc)
        except Exception as exc:  # any other listing failure is reported, never inferred
            self.observe_failure(token)
            if token == self._generation:
                self._ui.notify(str(exc), title="Failed to list namespaces", severity="error")
        return None

    async def _reallocate(self, lister: ListNamespaces) -> None:
        token = self._generation
        names = await self._list(lister, token)
        if names is None or token != self._generation:
            return
        if self._restoring is not None:
            # A preview now would be built on the map the restore replaces.
            self._ui.notify(_STILL_LOADING, severity="warning")
            return
        blocked = self._blocked()
        if blocked is not None:  # a dialog or the picker opened while the listing ran
            self._ui.notify(blocked.message, severity=blocked.severity, markup=False)
            return
        inventory, seen, before = frozenset(names), self._observed, dict(self._auto)
        # The preview compares against the map as it stands: nothing changes
        # in memory or on disk until the user confirms (Escape keeps it all).
        proposed = reallocate(self._pinned(), self._auto, inventory)
        changes = preview(self.slots, proposed)
        if not changes:
            self.observe(token, names)  # a fresh listing still counts
            self._ui.notify("Namespace slots are already compact", markup=False)
            return
        from korvid.ui.widgets.namespace_slots_screen import NamespaceSlotsScreen

        def _decided(confirmed: bool | None) -> None:
            if confirmed:
                self._ui.run_worker(
                    self._commit(token, proposed, inventory, seen, before),
                    group="namespace-slot-save",
                    exit_on_error=False,
                )

        self._ui.push_screen(NamespaceSlotsScreen(changes), _decided)

    async def _commit(
        self,
        token: int,
        proposed: SlotMap,
        inventory: frozenset[str],
        seen: int,
        before: dict[int, SlotEntry],
    ) -> None:
        """Save and adopt a confirmed *proposed* map; *before* is the map it
        was built on, so visits made since the preview carry over."""
        if token != self._generation:
            self._ui.notify(
                "Namespace slot reallocation cancelled - the kube context changed",
                severity="warning",
            )
            return
        persistence, identity = self._persistence, self._identity
        if persistence is None or identity is None:
            self._install(proposed, inventory, before, seen)
            self._ui.notify("Namespace slots reallocated for this session (not saved)")
            return
        async with self._save_lock:
            try:
                await self._save(identity, proposed.automatic())
            except (OSError, SlotStateError) as exc:
                # Maps still queued stay queued: their visits are saved anyway.
                self._ui.notify(
                    f"Could not save namespace slots: {exc}",
                    title="Namespace slots",
                    severity="error",
                    markup=False,
                )
                return
            except asyncio.CancelledError:
                # Quitting cancelled the worker, but the shielded write still
                # lands: `shutdown` must drain a queue rebased on it.
                self._rebase(identity, proposed, inventory, before, seen)
                raise
            if token != self._generation:
                # Switched while writing: requeue for the old cluster's file.
                self._rebase(identity, proposed, inventory, before, seen)
                return
            self._pending.pop(identity, None)  # `_install` carries its visits
            self._persist_failed = False
            self._install(proposed, inventory, before, seen)
        self._ui.notify("Namespace slots reallocated")

    def _rebase(
        self,
        identity: ClusterIdentity,
        proposed: SlotMap,
        inventory: frozenset[str] | None,
        before: dict[int, SlotEntry],
        seen: int,
    ) -> None:
        """Rebuild the map queued for *identity* on the saved *proposed* one.

        The queued map was built on the old map, so only its new visits carry
        over. A listing outcome of that cluster newer than *seen* judges the
        result; a failed one trusts every visit.
        """
        queued = self._pending.pop(identity, None)
        listed = self._listed
        newer = listed is not None and listed[0] == identity and listed[1] > seen
        if listed is not None and newer:
            inventory = listed[2]
        elif queued is None:
            return
        merged = _with_visits(proposed, inventory, before, queued or {})
        rebased = build(self._pinned(), merged, inventory).automatic()
        if rebased != proposed.automatic():
            self._pending[identity] = rebased

    def _install(
        self,
        proposed: SlotMap,
        inventory: frozenset[str],
        before: dict[int, SlotEntry],
        seen: int,
    ) -> None:
        """Adopt a confirmed reallocation, keeping visits made since *before*.

        Its listing becomes the inventory unless a listing outcome landed
        after it (*seen*); then that newer one judges the map instead.
        """
        if self._observed == seen:
            self._inventory, self._stale = inventory, False
        merged = _with_visits(proposed, self._inventory, before, self._auto)
        self._auto = proposed.automatic()
        self._update(build(self._pinned(), merged, self._inventory).automatic())


def _with_visits(
    proposed: SlotMap,
    inventory: frozenset[str] | None,
    before: dict[int, SlotEntry],
    current: dict[int, SlotEntry],
) -> dict[int, SlotEntry]:
    """*proposed* plus the namespaces *current* gained since *before* that
    *inventory* still lists (all of them when it is unknown), in slot order."""
    known = {entry.namespace for entry in before.values()}
    merged = proposed
    for _, entry in sorted(current.items()):
        if entry.namespace not in known and (inventory is None or entry.namespace in inventory):
            merged = place(merged, entry.namespace)
    return merged.automatic()
