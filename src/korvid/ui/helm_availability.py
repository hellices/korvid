"""Why a helm flow cannot run right now (issue #388).

`HelmController` owns the install/upgrade/rollback/uninstall *flows*; this
module owns their side-effect-free twin: the probe the Action Palette asks
before it offers a row, and the same question a flow answers by refusing
with a notification. Keeping both halves in one place is what keeps them
from drifting - every refusal here is the wording the matching keypress
notifies, behind that flow's own "<Action> cancelled - " prefix.

Pure Python: no Textual, no helm process, no notification. The seams
arrive as plain callables, so probing can never shell out to helm, resolve
an identity the flow would not resolve, or tell the user anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from korvid.k8s.helm import HelmReleaseIdentity, HelmReleaseSummary, HelmRevisionSummary
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason

#: The one wording for "no helm binary", shared by the notification
#: `HelmController.gate()` emits on a real keypress and the silent reason
#: the palette probe returns, so the two can never drift.
HELM_MISSING = UnavailableReason(
    AvailabilityCode.MISSING_CAPABILITY,
    "helm CLI not found on PATH - install/upgrade/rollback/uninstall unavailable",
    severity="error",
)

#: Helm actions that act on the selected row (install creates a new release).
HELM_ROW_ACTIONS: frozenset[str] = frozenset({"helm_upgrade", "helm_history", "helm_rollback"})

#: The release moved under the history on screen: the newest revision in
#: the loaded history and the cached release row name different
#: incarnations, and only a refresh can reconcile them.
STALE_RELEASE_HISTORY = UnavailableReason(
    AvailabilityCode.TRANSITION, "release history changed; refresh and retry"
)

#: The selected release row carries no identity at all, so no helm write on
#: it can be pinned to the incarnation the user is looking at. Not the same
#: fact as the one above - nothing changed, the row simply never arrived
#: complete - and the repair is the same: refresh.
RELEASE_IDENTITY_UNAVAILABLE = UnavailableReason(
    AvailabilityCode.TRANSITION, "release identity unavailable; refresh and retry"
)

#: The newest revision of the release in the *loaded history* carries no
#: usable identity, so the comparison `rollback_selected` makes cannot be
#: built at all.
RELEASE_HISTORY_UNAVAILABLE = UnavailableReason(
    AvailabilityCode.TRANSITION, "release history unavailable; refresh and retry"
)


def cancelled(action: str, reason: UnavailableReason) -> str:
    """The sentence a helm flow notifies when it cancels for *reason*.

    The palette row shows the fact alone, because nothing was cancelled
    there; the keypress leads with what it did and then states the same
    fact, from the same constant.

    Args:
        action: The flow, as the user knows it ("Helm upgrade").
        reason: The refusal the probe would return for the same state.

    Returns:
        The full cancellation sentence for `UiSurface.notify`.
    """
    return f"{action} cancelled - {reason.message}"


@dataclass(frozen=True, slots=True)
class HelmAvailability:
    """The helm flows' refusals, answered without running anything.

    Constructed by `HelmController` from the seams that controller was
    itself injected with, so the probe reads exactly the state the flow
    reads: the same view, the same write gate, the same late-bound helm
    wrapper and the same cached rows.
    """

    readonly: Callable[[], bool]
    audit_configured: Callable[[], bool]
    helm_available: Callable[[], bool]
    #: The selected row, read silently (`notify=False`).
    selected_ns_name: Callable[[], tuple[str | None, str | None]]
    release_row: Callable[[str | None, str], HelmReleaseSummary | None]
    revision_row: Callable[[str | None, str], HelmRevisionSummary | None]
    latest_revision_identity: Callable[[str, str], HelmReleaseIdentity | None]

    def write_gate_reason(self) -> UnavailableReason | None:
        """The read-only and fail-closed-audit halves of `gate()`, without
        reading the helm wrapper: `gate()` must read that exactly once, so
        the shared part stops just short of it."""
        if self.readonly():
            return UnavailableReason(
                AvailabilityCode.READ_ONLY, "Read-only mode: cluster writes are disabled"
            )
        if not self.audit_configured():
            # Fail-closed auditing (AGENTS.md): no audit sink means no writes.
            return UnavailableReason(
                AvailabilityCode.MISSING_CAPABILITY, "Writes disabled: no audit log configured"
            )
        return None

    def cli_unavailable_reason(self) -> UnavailableReason | None:
        """Whether the helm binary is missing right now, or None - the one
        fact `gate()` notifies for a real keypress, returned silently for a
        probe. `ResourceWriteController` injects this so `delete_resource`
        on the helm release browser - which routes to `uninstall_selected()`
        before the generic write path - reports the same missing-CLI
        refusal without redeclaring its wording."""
        return None if self.helm_available() else HELM_MISSING

    def unavailable_reason(self, action: str) -> UnavailableReason | None:
        """Why `action` can't run right now, or None (issue #388 task 4).

        Which view the helm actions belong on stays `ActionPolicy`'s
        (`HelmController._view_guard` mirrors it for direct calls), so this
        answers only what the *controller* would refuse next:

        - `helm_install`/`helm_upgrade`/`helm_rollback` are writes, so they
          carry `gate()`'s refusals verbatim (read-only, missing audit sink,
          missing helm binary).
        - `helm_history` is a read-only drill-down that `history()` gates on
          nothing but the selection, so the probe gates it on nothing more
          either - claiming a read-only refusal there would grey out a key
          that works.
        - `helm_upgrade`/`helm_history`/`helm_rollback` all act on the
          selected row.
        - `helm_upgrade` and `helm_rollback` carry the identity refusals
          their flows make from cached rows alone (see
          `release_identity_reason` and `rollback_identity_reason`).
        """
        if action != "helm_history":
            reason = self.write_gate_reason()
            if reason is not None:
                return reason
            reason = self.cli_unavailable_reason()
            if reason is not None:
                return reason
        if action not in HELM_ROW_ACTIONS:
            return None
        namespace, name = self.selected_ns_name()
        if name is None:
            return UnavailableReason(AvailabilityCode.NO_SELECTION, "No resource selected")
        if action == "helm_upgrade":
            return self.release_identity_reason(namespace, name)
        if action == "helm_rollback":
            return self.rollback_identity_reason(namespace, name)
        return None

    def release_identity_reason(self, namespace: str | None, name: str) -> UnavailableReason | None:
        """Why a write on the selected *release* row would cancel, or None.

        `upgrade()` and `uninstall_selected()` both read the cached release
        row and cancel when it carries no identity, because an unpinned
        helm write could land on a different incarnation than the one on
        screen. Both refusals are made from the store alone, so the probe
        makes them too (#388 round 13).

        A release row that is simply *missing* stays invocable on purpose:
        that is a half-loaded view the next watch event fills in, and the
        flows report it as "no helm release selected" rather than as
        something the user can repair.
        """
        row = self.release_row(namespace, name)
        if row is None:
            return None
        return None if row.identity is not None else RELEASE_IDENTITY_UNAVAILABLE

    def rollback_identity_reason(
        self, namespace: str | None, name: str
    ) -> UnavailableReason | None:
        """Why `r` would cancel on the selected revision row, or None.

        The same cached reads `rollback_selected` makes, in its order and
        with no helm call: the selected revision row, the newest revision
        of its release in the loaded history, then the release row itself.

        Two of its cases stay out. A missing revision row is the
        half-loaded view above ("no helm revision selected"), and a release
        the store has not loaded at all is resolved through the helm CLI,
        which a probe must never do - so the row stays runnable and the
        flow answers for it.
        """
        row = self.revision_row(namespace, name)
        if row is None:
            return None
        scope = namespace or row.namespace
        history_identity = self.latest_revision_identity(scope, row.release)
        if history_identity is None:
            return RELEASE_HISTORY_UNAVAILABLE
        release_row = self.release_row(scope, row.release)
        if release_row is None:
            return None
        if release_row.identity is None:
            return RELEASE_IDENTITY_UNAVAILABLE
        return None if release_row.identity == history_identity else STALE_RELEASE_HISTORY
