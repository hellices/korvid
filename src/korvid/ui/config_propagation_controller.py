"""Open the configuration propagation view for the selected ConfigMap or Secret (#410).

One exclusive worker reads the object's metadata and its namespace's Pods
through `explain_propagation`, under a run token. The view opens only while
that token is current: a context switch, new credentials or a newer run
drops the result, as `RunTracker` decides. So does another dialog that is
open when the reads finish, or a change of view, scope or selection since
they started, as the relationship view does. The context-switch coordinator
cancels the worker group before it swaps the client.

Each run's sighting of the object's resource version goes into a
`ChangeLog`, under the run's context epoch and session generation, so a later
run of the same incarnation can show that it changed in between.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from korvid.core.api_versions import observe_discovery
from korvid.core.capability import RunToken
from korvid.core.config_changes import ChangeLog, ObjectKey, ObservedChange
from korvid.core.config_consumers import Consumer
from korvid.core.config_propagation import ConfigTarget, PropagationReport, explain_propagation
from korvid.core.run_tokens import RunTracker
from korvid.k8s.evidence_requests import EvidenceClient
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.config_propagation_screen import ConfigPropagationScreen
from korvid.ui.workspace_controller import ContextGuard

CONFIG_PROPAGATION_GROUP = "config-propagation"
_KINDS = frozenset({"ConfigMap", "Secret"})
_UNCONNECTED = UnavailableReason(
    AvailabilityCode.MISSING_CAPABILITY, "Configuration propagation needs a cluster connection"
)
_WRONG_VIEW = UnavailableReason(
    AvailabilityCode.WRONG_VIEW, "Open ConfigMaps or Secrets to explain their propagation"
)
_NO_SELECTION = UnavailableReason(AvailabilityCode.NO_SELECTION, "Select a ConfigMap or Secret")

#: Opens a consumer's Pod: (namespace, name, epoch, expected UID).
PodNavigator = Callable[[str, str, int, str | None], Awaitable[None]]
#: The view, scope and (namespace, name) selection a run was started from.
_Origin = tuple[str, str, tuple[str | None, str | None]]


def _now() -> datetime:
    return datetime.now(UTC)


class ConfigPropagationController:
    """Runs the propagation diagnostic and shows its result while it is current."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        view: ViewState,
        context: ContextGuard,
        client: EvidenceClient | None,
        navigate: PodNavigator,
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
        self._changes = ChangeLog()

    def _selected_kind(self) -> str | None:
        meta = self._view.aliases().get(self._view.canonical_kind(self._view.current_kind()))
        if meta is None or meta.group or meta.kind not in _KINDS:
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
        """Explain the selected ConfigMap or Secret in a worker."""
        reason = self.unavailable_reason()
        if reason is not None:
            self._ui.notify(reason.message, severity="warning", markup=False)
            return
        kind = self._selected_kind()
        namespace, name = self._view.selected_ns_name()
        if not self._context.reads_allowed() or kind is None or not namespace or not name:
            return
        target = ConfigTarget(kind, namespace, name, self._view.selected_uid(namespace, name))
        token = self._tracker.next_run()
        self._ui.run_worker(
            self._explain(target, token, self._context.epoch(), self._origin()),
            exclusive=True,
            group=CONFIG_PROPAGATION_GROUP,
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
        self, target: ConfigTarget, token: RunToken, epoch: int, origin: _Origin
    ) -> None:
        client = self._client
        if client is None:
            return
        snapshot = observe_discovery(await client.discovery_snapshot())
        report = await explain_propagation(client, snapshot, target, token=token, clock=self._clock)
        change = self._record(report, token)
        # Another dialog is open, or the user has moved on, as the reads finish.
        if self._ui.screen_depth() > 1 or self._origin() != origin:
            return
        screen = ConfigPropagationScreen(report, self._clock, change=change)
        self._tracker.apply_if_current(
            token,
            lambda: self._ui.push_screen(screen, lambda chosen: self._open_pod(chosen, epoch)),
        )

    def _record(self, report: PropagationReport, token: RunToken) -> ObservedChange | None:
        """The object's latest change seen this session, after this run's sighting."""
        uid = report.object.observed_uid
        if report.sighting is None or uid is None:
            return None
        target = report.target
        key = ObjectKey(
            token.epoch, token.session_generation, target.kind, target.namespace, target.name, uid
        )
        return self._changes.record(key, report.sighting)

    def _open_pod(self, consumer: Consumer | None, epoch: int) -> None:
        if consumer is None:
            return
        pod = consumer.pod
        self._ui.run_worker(
            self._navigate(pod.namespace, pod.name, epoch, pod.uid),
            group=CONFIG_PROPAGATION_GROUP,
            exit_on_error=False,
        )

    async def cancel_workers(self) -> None:
        """Stop a run holding the old client, before a context switch swaps it."""
        await self._ui.cancel_workers(CONFIG_PROPAGATION_GROUP)
