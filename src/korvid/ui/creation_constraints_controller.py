"""Open the workload creation constraints view for the selected workload (#411).

One exclusive worker reads the workload, its children, the Events and the
namespace's quotas and LimitRanges through `explain_creation`, under a run
token. The view opens only while that token is current: a context switch,
new credentials or a newer run drops the result, as `RunTracker` decides. So
does another dialog that is open when the reads finish, or a change of view,
scope or selection since they started. The context-switch coordinator
cancels the worker group before it swaps the client.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from korvid.core.api_versions import observe_discovery
from korvid.core.capability import RunToken
from korvid.core.creation_constraints import CreationTarget, explain_creation
from korvid.core.creation_workloads import WORKLOAD_KINDS
from korvid.core.run_tokens import RunTracker
from korvid.k8s.discovery import canonical_resource_alias, resolve_resource
from korvid.k8s.evidence_requests import EvidenceClient
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.creation_constraints_screen import CreationConstraintsScreen, Destination
from korvid.ui.workspace_controller import ContextGuard

CREATION_CONSTRAINTS_GROUP = "creation-constraints"
_UNCONNECTED = UnavailableReason(
    AvailabilityCode.MISSING_CAPABILITY, "Workload creation failures need a cluster connection"
)
_WRONG_VIEW = UnavailableReason(
    AvailabilityCode.WRONG_VIEW,
    "Open Deployments, ReplicaSets, StatefulSets, DaemonSets, Jobs or CronJobs to explain "
    "creation failures",
)
_NO_SELECTION = UnavailableReason(AvailabilityCode.NO_SELECTION, "Select a workload")

#: Opens an object in its table: (view alias, namespace, name, epoch, expected UID).
ObjectNavigator = Callable[[str, str, str, int, str | None], Awaitable[None]]
#: The view, scope and (namespace, name) selection a run was started from.
_Origin = tuple[str, str, tuple[str | None, str | None]]


def _now() -> datetime:
    return datetime.now(UTC)


class CreationConstraintsController:
    """Runs the creation diagnostic and shows its result while it is current."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        view: ViewState,
        context: ContextGuard,
        client: EvidenceClient | None,
        navigate: ObjectNavigator,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._ui = ui
        self._view = view
        self._context = context
        self._client = client
        self._navigate = navigate
        self._clock = clock
        self._tracker = RunTracker(
            epoch=context.epoch,
            session_generation=lambda: client.session_generation if client is not None else 0,
            switching=context.switching,
        )

    def _selected_kind(self) -> str | None:
        meta = self._view.aliases().get(self._view.canonical_kind(self._view.current_kind()))
        workload = WORKLOAD_KINDS.get(meta.kind) if meta is not None else None
        if meta is None or workload is None or meta.group != workload.group:
            return None
        return meta.kind

    def unavailable_reason(self) -> UnavailableReason | None:
        """Why the view cannot open now, or None.

        Only for a missing connection, the wrong view or no selection: a
        source the cluster refuses never disables it, the view says so.
        """
        if self._client is None:
            return _UNCONNECTED
        if self._selected_kind() is None:
            return _WRONG_VIEW
        namespace, name = self._view.selected_ns_name(notify=False)
        if not namespace or not name:
            return _NO_SELECTION
        return None

    def open_selected(self) -> None:
        """Explain the selected workload's creation failures in a worker."""
        reason = self.unavailable_reason()
        if reason is not None:
            self._ui.notify(reason.message, severity="warning", markup=False)
            return
        kind = self._selected_kind()
        namespace, name = self._view.selected_ns_name()
        if not self._context.reads_allowed() or kind is None or not namespace or not name:
            return
        target = CreationTarget(kind, namespace, name, self._view.selected_uid(namespace, name))
        token = self._tracker.next_run()
        self._ui.run_worker(
            self._explain(target, token, self._context.epoch(), self._origin()),
            exclusive=True,
            group=CREATION_CONSTRAINTS_GROUP,
            exit_on_error=False,
        )

    def _origin(self) -> _Origin:
        """The view, scope and selected row a run explains."""
        return (
            self._view.current_kind(),
            self._view.current_scope(),
            self._view.selected_ns_name(notify=False),
        )

    async def _explain(
        self, target: CreationTarget, token: RunToken, epoch: int, origin: _Origin
    ) -> None:
        client = self._client
        if client is None:
            return
        snapshot = observe_discovery(await client.discovery_snapshot())
        report = await explain_creation(client, snapshot, target, token=token, clock=self._clock)
        # Another dialog is open, or the user has moved on, as the reads finish.
        if self._ui.screen_depth() > 1 or self._origin() != origin:
            return
        screen = CreationConstraintsScreen(report, self._clock)
        self._tracker.apply_if_current(
            token,
            lambda: self._ui.push_screen(screen, lambda chosen: self._open(chosen, epoch)),
        )

    def _open(self, destination: Destination | None, epoch: int) -> None:
        """Open a row's object by its API identity, never by a colliding display alias."""
        if destination is None:
            return
        aliases = self._view.aliases()
        meta = resolve_resource(aliases, destination.group, destination.resource)
        if meta is None:
            name = destination.resource
            qualified = f"{name}.{destination.group}" if destination.group else name
            self._ui.notify(
                f"{qualified} is not discovered in this session", severity="warning", markup=False
            )
            return
        alias = canonical_resource_alias(aliases, meta)
        self._ui.run_worker(
            self._navigate(alias, destination.namespace, destination.name, epoch, destination.uid),
            group=CREATION_CONSTRAINTS_GROUP,
            exit_on_error=False,
        )

    async def cancel_workers(self) -> None:
        """Stop a run holding the old client, before a context switch swaps it."""
        await self._ui.cancel_workers(CREATION_CONSTRAINTS_GROUP)
