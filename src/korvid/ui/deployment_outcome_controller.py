"""Session-scoped lifecycle for bounded Deployment outcome tracking."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from korvid.core.deployment_outcome import (
    DeploymentOperationIntent,
    DeploymentOperationTarget,
    DeploymentOutcome,
    DeploymentOutcomePhase,
    DeploymentRestartIntent,
    DeploymentScaleIntent,
    evaluate_deployment_outcome,
    normalize_deployment_observation,
)
from korvid.k8s.deployment_outcomes import DeploymentOutcomeReader
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.ui_surface import Severity, UiSurface
from korvid.ui.write_gate import AcceptedWriteObserver, AcceptedWriteReceipt

logger = logging.getLogger(__name__)

OUTCOME_WORKER_GROUP = "deployment-outcomes"
OUTCOMES_UNAVAILABLE = UnavailableReason(
    AvailabilityCode.NO_SELECTION,
    "No Deployment outcome has been tracked yet",
)
DEFAULT_POLL_DELAYS: tuple[float, ...] = (
    0.0,
    1.0,
    2.0,
    5.0,
    10.0,
    15.0,
    30.0,
    60.0,
    90.0,
    82.0,
)


class CancellableWork(Protocol):
    """The worker operation needed for bounded tracker eviction."""

    def cancel(self) -> None:
        """Cancel this tracker worker."""


OutcomePodAction: TypeAlias = Callable[[str, int, str, str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DeploymentTrackerSnapshot:
    """Immutable display state for one accepted operation."""

    tracker_id: str
    intent: DeploymentOperationIntent
    accepted_at: str
    outcome: DeploymentOutcome
    attempts: int = 0
    elapsed_seconds: float = 0.0


def _accepted_outcome() -> DeploymentOutcome:
    return DeploymentOutcome(
        phase=DeploymentOutcomePhase.ACCEPTED,
        summary="API request accepted; Deployment convergence has not been verified",
    )


def _stopped_outcome(reason: str) -> DeploymentOutcome:
    return DeploymentOutcome(
        phase=DeploymentOutcomePhase.STOPPED,
        summary=f"Outcome observation stopped: {reason}",
    )


def _incomplete_outcome(
    reason: str,
    previous: DeploymentOutcome | None = None,
) -> DeploymentOutcome:
    return DeploymentOutcome(
        phase=DeploymentOutcomePhase.INCOMPLETE,
        summary=f"API request accepted, but outcome verification is incomplete: {reason}",
        evidence=previous.evidence if previous is not None else (),
        pods=previous.pods if previous is not None else (),
        partial_evidence=previous.partial_evidence if previous is not None else False,
    )


class DeploymentOutcomeController:
    """Own bounded tracker state and supervised polling workers."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        reader: DeploymentOutcomeReader | None,
        get_epoch: Callable[[], int],
        cluster_id: Callable[[], str | None],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_delays: Sequence[float] = DEFAULT_POLL_DELAYS,
        max_trackers: int = 3,
        pod_action: OutcomePodAction | None = None,
        clock: Callable[[], float] = time.monotonic,
        deadline_seconds: float = 300.0,
    ) -> None:
        if max_trackers < 1:
            raise ValueError("max_trackers must be positive")
        if not poll_delays:
            raise ValueError("poll_delays must not be empty")
        if any(delay < 0 for delay in poll_delays):
            raise ValueError("poll delays must not be negative")
        if deadline_seconds < 0:
            raise ValueError("deadline_seconds must not be negative")
        self._ui = ui
        self._reader = reader
        self._get_epoch = get_epoch
        self._cluster_id = cluster_id
        self._sleep = sleep
        self._poll_delays = tuple(poll_delays)
        self._max_trackers = max_trackers
        self._snapshots: OrderedDict[str, DeploymentTrackerSnapshot] = OrderedDict()
        self._workers: dict[str, CancellableWork] = {}
        self._next_id = 1
        self._pod_action = pod_action
        self._clock = clock
        self._deadline_seconds = deadline_seconds
        self._started: dict[str, float] = {}

    def scale_observer(
        self,
        *,
        epoch: int,
        namespace: str | None,
        name: str,
        uid: str | None,
        replicas: int,
    ) -> AcceptedWriteObserver | None:
        """Build the post-success observer for an exact Deployment scale."""

        target, correlation_error = self._target(
            epoch=epoch, namespace=namespace, name=name, uid=uid
        )

        async def _accepted(receipt: AcceptedWriteReceipt) -> None:
            if receipt.action != "scale":
                raise ValueError("scale observer received another write action")
            generation = receipt.mutation.generation if receipt.mutation is not None else None
            error = correlation_error
            if generation is None:
                error = "scale response did not include metadata.generation"
            intent = DeploymentScaleIntent(
                target=target,
                replicas=replicas,
                generation=generation if generation is not None else -1,
            )
            self._start(intent, receipt.accepted_at, incomplete_reason=error)

        return _accepted

    def restart_observer(
        self,
        *,
        epoch: int,
        namespace: str | None,
        name: str,
        uid: str | None,
        restarted_at: str,
    ) -> AcceptedWriteObserver | None:
        """Build the post-success observer for an exact Deployment restart."""

        target, correlation_error = self._target(
            epoch=epoch, namespace=namespace, name=name, uid=uid
        )

        async def _accepted(receipt: AcceptedWriteReceipt) -> None:
            if receipt.action != "rollout_restart":
                raise ValueError("restart observer received another write action")
            generation = receipt.mutation.generation if receipt.mutation is not None else None
            error = correlation_error
            if generation is None:
                error = "restart response did not include metadata.generation"
            intent = DeploymentRestartIntent(
                target=target,
                restarted_at=restarted_at,
                generation=generation if generation is not None else -1,
            )
            self._start(intent, receipt.accepted_at, incomplete_reason=error)

        return _accepted

    def _target(
        self,
        *,
        epoch: int,
        namespace: str | None,
        name: str,
        uid: str | None,
    ) -> tuple[DeploymentOperationTarget, str | None]:
        cluster_id = self._cluster_id()
        missing = [
            label
            for value, label in (
                (self._reader, "Deployment reader"),
                (namespace, "namespace"),
                (uid, "UID"),
                (cluster_id, "cluster identity"),
            )
            if value is None
        ]
        target = DeploymentOperationTarget(
            epoch=epoch,
            cluster_id=cluster_id or "",
            namespace=namespace or "",
            name=name,
            uid=uid or "",
        )
        reason = f"missing correlation evidence: {', '.join(missing)}" if missing else None
        return target, reason

    def _start(
        self,
        intent: DeploymentOperationIntent,
        accepted_at: str,
        *,
        incomplete_reason: str | None = None,
    ) -> None:
        tracker_id = f"deployment-outcome-{self._next_id}"
        self._next_id += 1
        self._evict_if_full()
        self._snapshots[tracker_id] = DeploymentTrackerSnapshot(
            tracker_id=tracker_id,
            intent=intent,
            accepted_at=accepted_at,
            outcome=(
                _incomplete_outcome(incomplete_reason)
                if incomplete_reason is not None
                else _accepted_outcome()
            ),
        )
        self._started[tracker_id] = self._clock()
        if incomplete_reason is not None:
            self._ui.notify(
                self._snapshots[tracker_id].outcome.summary,
                severity="warning",
            )
            self.open_latest()
            return
        self._ui.notify(
            f"{intent.target.name}: API request accepted; verifying Deployment convergence"
        )
        self.open_latest()
        self._workers[tracker_id] = self._ui.run_worker(
            self._observe(tracker_id),
            exclusive=False,
            group=OUTCOME_WORKER_GROUP,
            name=tracker_id,
            exit_on_error=False,
        )

    def _evict_if_full(self) -> None:
        if len(self._snapshots) < self._max_trackers:
            return
        tracker_id, _snapshot = self._snapshots.popitem(last=False)
        self._started.pop(tracker_id, None)
        worker = self._workers.pop(tracker_id, None)
        if worker is not None:
            worker.cancel()
        self._ui.notify(
            "Stopped the oldest Deployment outcome tracker at the three-tracker limit",
            severity="warning",
        )

    async def _observe(self, tracker_id: str) -> None:
        try:
            async with asyncio.timeout(self._deadline_seconds):
                for attempt, delay in enumerate(self._poll_delays, start=1):
                    if attempt > 1:
                        await self._sleep(delay)
                    if await self._observe_once(tracker_id, attempt):
                        return
                self._mark_deadline_elapsed(tracker_id)
        except TimeoutError:
            self._mark_deadline_elapsed(tracker_id)
        except asyncio.CancelledError:
            snapshot = self._snapshots.get(tracker_id)
            if snapshot is not None and snapshot.outcome.phase in {
                DeploymentOutcomePhase.ACCEPTED,
                DeploymentOutcomePhase.OBSERVING,
            }:
                self._update(
                    tracker_id,
                    _stopped_outcome("observation was cancelled"),
                    snapshot.attempts,
                )
            raise
        finally:
            self._workers.pop(tracker_id, None)

    def _mark_deadline_elapsed(self, tracker_id: str) -> None:
        snapshot = self._snapshots.get(tracker_id)
        self._update(
            tracker_id,
            _incomplete_outcome(
                "the five-minute observation deadline elapsed",
                snapshot.outcome if snapshot is not None else None,
            ),
            snapshot.attempts if snapshot is not None else len(self._poll_delays),
        )

    async def _observe_once(self, tracker_id: str, attempt: int) -> bool:
        snapshot = self._snapshots.get(tracker_id)
        if snapshot is None:
            return True
        if not self._target_current(snapshot):
            self._update(tracker_id, _stopped_outcome("kube context changed"), attempt - 1)
            return True
        reader = self._reader
        if reader is None:
            return True
        try:
            raw = await reader.snapshot(
                snapshot.intent.target.namespace,
                snapshot.intent.target.name,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Deployment outcome read failed: %s", type(exc).__name__)
            if not self._can_apply_read(tracker_id, snapshot):
                return True
            self._update(
                tracker_id,
                _incomplete_outcome(type(exc).__name__, snapshot.outcome),
                attempt,
            )
            return True
        if not self._can_apply_read(tracker_id, snapshot):
            return True
        outcome = evaluate_deployment_outcome(
            snapshot.intent,
            normalize_deployment_observation(raw),
        )
        self._update(tracker_id, outcome, attempt)
        return outcome.phase is not DeploymentOutcomePhase.OBSERVING

    def _target_current(self, snapshot: DeploymentTrackerSnapshot) -> bool:
        target = snapshot.intent.target
        return self._get_epoch() == target.epoch and self._cluster_id() == target.cluster_id

    def _can_apply_read(
        self,
        tracker_id: str,
        expected: DeploymentTrackerSnapshot,
    ) -> bool:
        current = self._snapshots.get(tracker_id)
        if current is not expected or current.outcome.phase not in {
            DeploymentOutcomePhase.ACCEPTED,
            DeploymentOutcomePhase.OBSERVING,
        }:
            return False
        if self._target_current(current):
            return True
        self._update(
            tracker_id,
            _stopped_outcome("kube context changed"),
            current.attempts,
        )
        return False

    def _update(
        self,
        tracker_id: str,
        outcome: DeploymentOutcome,
        attempts: int,
    ) -> None:
        snapshot = self._snapshots.get(tracker_id)
        if snapshot is None:
            return
        previous = snapshot.outcome.phase
        self._snapshots[tracker_id] = dataclasses.replace(
            snapshot,
            outcome=outcome,
            attempts=attempts,
            elapsed_seconds=max(
                0.0,
                self._clock() - self._started.get(tracker_id, self._clock()),
            ),
        )
        if outcome.phase != previous:
            severity: Severity = (
                "error"
                if outcome.phase is DeploymentOutcomePhase.STALLED
                else "warning"
                if outcome.phase
                in {
                    DeploymentOutcomePhase.INCOMPLETE,
                    DeploymentOutcomePhase.REPLACED,
                    DeploymentOutcomePhase.STOPPED,
                    DeploymentOutcomePhase.SUPERSEDED,
                }
                else "information"
            )
            self._ui.notify(outcome.summary, severity=severity)

    def latest(self) -> DeploymentTrackerSnapshot | None:
        """Return the newest immutable tracker snapshot."""

        return next(reversed(self._snapshots.values()), None)

    def unavailable_reason(self) -> UnavailableReason | None:
        """Why the latest-outcome command cannot open, if anything."""

        return None if self._snapshots else OUTCOMES_UNAVAILABLE

    def open_latest(self) -> None:
        """Present the newest retained Deployment outcome snapshot."""

        snapshot = self.latest()
        if snapshot is None:
            self._ui.notify(
                OUTCOMES_UNAVAILABLE.message,
                severity=OUTCOMES_UNAVAILABLE.severity,
                markup=False,
            )
            return
        from korvid.ui.widgets.deployment_outcome_screen import DeploymentOutcomeScreen

        self._ui.push_screen(DeploymentOutcomeScreen(snapshot), self._on_screen_result)

    def _on_screen_result(self, result: object) -> None:
        if not isinstance(result, tuple) or len(result) != 5:
            return
        verb, tracker_id, _namespace, _name, _uid = result
        if verb == "stop" and isinstance(tracker_id, str):
            self.stop(tracker_id)
        elif verb == "refresh" and isinstance(tracker_id, str):
            snapshot = self._snapshots.get(tracker_id)
            if snapshot is not None:
                from korvid.ui.widgets.deployment_outcome_screen import DeploymentOutcomeScreen

                self._ui.push_screen(
                    DeploymentOutcomeScreen(snapshot),
                    self._on_screen_result,
                )
        elif (
            verb in {"goto", "events", "describe", "logs"}
            and isinstance(tracker_id, str)
            and isinstance(_namespace, str)
            and isinstance(_name, str)
            and isinstance(_uid, str)
        ):
            snapshot = self._snapshots.get(tracker_id)
            if snapshot is None:
                return
            if self._pod_action is None:
                self._ui.notify(
                    "Pod actions are unavailable in this session",
                    severity="warning",
                )
                return
            self._ui.run_worker(
                self._pod_action(
                    verb,
                    snapshot.intent.target.epoch,
                    _namespace,
                    _name,
                    _uid,
                ),
                group="deployment-outcome-pod-action",
                exit_on_error=False,
            )

    def snapshots(self) -> tuple[DeploymentTrackerSnapshot, ...]:
        """Return every retained tracker, oldest first."""

        return tuple(self._snapshots.values())

    def stop(self, tracker_id: str, reason: str = "stopped by user") -> bool:
        """Stop one active tracker while retaining its terminal snapshot."""

        snapshot = self._snapshots.get(tracker_id)
        if snapshot is None:
            return False
        if snapshot.outcome.phase not in {
            DeploymentOutcomePhase.ACCEPTED,
            DeploymentOutcomePhase.OBSERVING,
        }:
            return False
        worker = self._workers.pop(tracker_id, None)
        if worker is not None:
            worker.cancel()
        self._update(tracker_id, _stopped_outcome(reason), snapshot.attempts)
        return True

    async def stop_all(self, reason: str) -> None:
        """Stop all trackers before a context client is replaced."""

        for tracker_id, snapshot in tuple(self._snapshots.items()):
            if snapshot.outcome.phase in {
                DeploymentOutcomePhase.ACCEPTED,
                DeploymentOutcomePhase.OBSERVING,
            }:
                self._update(tracker_id, _stopped_outcome(reason), snapshot.attempts)
        await self._ui.cancel_workers(OUTCOME_WORKER_GROUP)
        self._workers.clear()

    async def shutdown(self) -> None:
        """Cancel all active observation workers during app shutdown."""

        await self.stop_all("application shutting down")
