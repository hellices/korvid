"""The one effective 1-9 namespace map behind dispatch, help and the picker (issue #406).

A namespace the user switches to takes the lowest free slot (`visit`).
Discovery results arrive from the existing namespace listings (the
completion prefetch and the `:ns` picker) and only judge availability. They
carry a generation token captured before the listing awaited. Activation and
deactivation advance the generation, so a listing that outlives a `:ctx`
switch is discarded instead of judging the new cluster's slots.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Sequence

from korvid.core.errors import explain_api_error
from korvid.core.namespace_slot_store import ClusterIdentity, NamespaceSlotStore, SlotStateError
from korvid.core.namespace_slots import SlotEntry, SlotMap, build, place, preview, reallocate
from korvid.core.store import ALL_NAMESPACES
from korvid.k8s.errors import ApiStatusError
from korvid.ui.action_availability import UnavailableReason
from korvid.ui.read_availability import NAMESPACE_LISTING_UNAVAILABLE
from korvid.ui.ui_surface import UiSurface

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
    ) -> None:
        self._ui = ui
        self._pinned = pinned
        self._context = context
        self._list_namespaces = list_namespaces
        self._persistence = persistence
        self._can_open = can_open
        self._generation = 0
        self._identity: ClusterIdentity | None = None
        self._auto: dict[int, SlotEntry] = {}
        self._stale = False
        #: Names from this cluster's latest complete listing, None until one lands.
        self._inventory: frozenset[str] | None = None
        #: Set once a save failed or the document is unreadable for this
        #: cluster: later saves are skipped so one fault reports once.
        self._persist_failed = False

    @property
    def slots(self) -> SlotMap:
        """The effective map: current pins over the automatic entries."""
        return build(self._pinned(), self._auto, None)

    @property
    def stale(self) -> bool:
        """Whether the latest discovery for this cluster failed."""
        return self._stale

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
        generation = self._generation
        persistence = self._persistence
        if persistence is None:
            return
        context = self._context()
        resolved = await asyncio.to_thread(persistence.identity, context)
        if generation != self._generation or resolved is None:
            return
        identity = ClusterIdentity(*resolved)
        try:
            saved = persistence.store.load(identity)
        except (OSError, SlotStateError) as exc:
            self._report_persist_failure(f"Namespace slots will not be saved: {exc}", "warning")
            saved = {}
        self._identity = identity
        self._auto = saved
        # A listing that started before this restore would merge into the
        # pre-restore map; only listings started from here on count.
        self._generation += 1

    def observe(self, token: int, names: Sequence[str]) -> None:
        """Judge availability from a complete listing; it never assigns a slot."""
        if token != self._generation:
            return
        self._stale = False
        self._inventory = frozenset(names)
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

    def _update(self, updated: dict[int, SlotEntry]) -> None:
        if updated == self._auto:
            return
        self._auto = updated
        self._persist(updated)

    def observe_failure(self, token: int) -> None:
        """A failed or denied listing infers nothing; the map is marked stale."""
        if token == self._generation:
            self._stale = True

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
        persistence, identity = self._persistence, self._identity
        if persistence is None or identity is None or self._persist_failed:
            return
        try:
            persistence.store.save(identity, slots)
        except (OSError, SlotStateError) as exc:
            self._report_persist_failure(f"Could not save namespace slots: {exc}", "error")

    # ------------------------------------------------------------------
    # Explicit reallocation (`:slots`)
    # ------------------------------------------------------------------

    def unavailable_reason(self) -> UnavailableReason | None:
        """Why `:slots` would refuse now - a silent probe that never lists."""
        reason = self._can_open()
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
        # The preview compares against the map as it stands: nothing changes
        # in memory or on disk until the user confirms (Escape keeps it all).
        proposed = reallocate(self._pinned(), self._auto, frozenset(names))
        changes = preview(self.slots, proposed)
        if not changes:
            self._ui.notify("Namespace slots are already compact", markup=False)
            return
        from korvid.ui.widgets.namespace_slots_screen import NamespaceSlotsScreen

        def _decided(confirmed: bool | None) -> None:
            if confirmed:
                self._commit(token, proposed)

        self._ui.push_screen(NamespaceSlotsScreen(changes), _decided)

    def _commit(self, token: int, proposed: SlotMap) -> None:
        if token != self._generation:
            self._ui.notify(
                "Namespace slot reallocation cancelled - the kube context changed",
                severity="warning",
            )
            return
        slots = proposed.automatic()
        persistence, identity = self._persistence, self._identity
        if persistence is None or identity is None:
            self._auto = slots
            self._stale = False
            self._ui.notify("Namespace slots reallocated for this session (not saved)")
            return
        try:
            persistence.store.save(identity, slots)
        except (OSError, SlotStateError) as exc:
            self._ui.notify(
                f"Could not save namespace slots: {exc}",
                title="Namespace slots",
                severity="error",
                markup=False,
            )
            return
        self._auto = slots
        self._stale = False
        self._persist_failed = False
        self._ui.notify("Namespace slots reallocated")
