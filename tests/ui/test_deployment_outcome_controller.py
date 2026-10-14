from __future__ import annotations

import asyncio
import dataclasses
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from korvid.core.deployment_outcome import (
    DeploymentOutcome,
    DeploymentOutcomePhase,
    DeploymentRestartIntent,
    DeploymentScaleIntent,
)
from korvid.k8s.deployment_outcomes import (
    DeploymentOutcomeReader,
    RawDeploymentOutcomeSnapshot,
)
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.writes import WriteMutationResult
from korvid.ui.deployment_outcome_controller import DeploymentOutcomeController
from korvid.ui.widgets.deployment_outcome_screen import DeploymentOutcomeScreen
from korvid.ui.write_gate import AcceptedWriteReceipt
from tests.ui.test_write_coordinator import FakeUi

_DEPLOYMENT: dict[str, Any] = {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {
        "name": "web",
        "namespace": "default",
        "uid": "deploy-uid",
        "generation": 2,
    },
    "spec": {
        "replicas": 3,
        "selector": {"matchLabels": {"app": "web"}},
        "template": {
            "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "restart-stamp"}}
        },
    },
    "status": {
        "observedGeneration": 2,
        "replicas": 3,
        "updatedReplicas": 3,
        "readyReplicas": 3,
        "availableReplicas": 3,
        "unavailableReplicas": 0,
    },
}


def _raw(*, ready: int = 3, restart_stamp: str = "restart-stamp") -> RawDeploymentOutcomeSnapshot:
    deployment = {
        **_DEPLOYMENT,
        "metadata": dict(_DEPLOYMENT["metadata"]),
        "spec": dict(_DEPLOYMENT["spec"])
        | {
            "template": {
                "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": restart_stamp}}
            }
        },
        "status": dict(_DEPLOYMENT["status"]) | {"readyReplicas": ready},
    }
    return RawDeploymentOutcomeSnapshot(deployment, (), (), False)


class _Reader(DeploymentOutcomeReader):
    def __init__(
        self,
        snapshots: list[RawDeploymentOutcomeSnapshot | BaseException],
    ) -> None:
        self.snapshots = snapshots
        self.calls: list[tuple[str, str]] = []

    async def snapshot(self, namespace: str, name: str) -> RawDeploymentOutcomeSnapshot:
        self.calls.append((namespace, name))
        item = self.snapshots.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _receipt(action: str = "scale") -> AcceptedWriteReceipt:
    from korvid.k8s.deployment_outcomes import DEPLOYMENT_META

    return AcceptedWriteReceipt(
        action=action,
        meta=DEPLOYMENT_META,
        namespace="default",
        name="web",
        accepted_at="2026-10-09T12:00:00Z",
        mutation=WriteMutationResult(generation=2),
    )


def _controller(
    reader: DeploymentOutcomeReader,
    *,
    epoch: list[int] | None = None,
    ui: FakeUi | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    cluster: list[str] | None = None,
    deadline_seconds: float = 300.0,
    clock: Callable[[], float] | None = None,
) -> tuple[DeploymentOutcomeController, FakeUi]:
    surface = ui or FakeUi()
    current_epoch = epoch or [4]
    controller = DeploymentOutcomeController(
        ui=surface,
        reader=reader,
        get_epoch=lambda: current_epoch[0],
        cluster_id=lambda: (cluster or ["context-a|https://cluster.example"])[0],
        sleep=sleep or asyncio.sleep,
        poll_delays=(0.0, 0.0),
        max_trackers=3,
        deadline_seconds=deadline_seconds,
        clock=clock or time.monotonic,
    )
    return controller, surface


def test_poll_schedule_requires_immediate_first_observation() -> None:
    with pytest.raises(ValueError, match="first poll delay must be zero"):
        DeploymentOutcomeController(
            ui=FakeUi(),
            reader=_Reader([_raw()]),
            get_epoch=lambda: 4,
            cluster_id=lambda: "context-a|https://cluster.example",
            poll_delays=(5.0,),
        )


async def _drain(ui: FakeUi, *, allow_cancelled: bool = False) -> None:
    if not ui.workers:
        return
    results = await asyncio.gather(*ui.workers, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not (
            allow_cancelled and isinstance(result, asyncio.CancelledError)
        ):
            raise result


async def test_scale_observer_reads_immediately_and_completes() -> None:
    reader = _Reader([_raw(ready=1), _raw(ready=3)])
    controller, ui = _controller(reader)
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )

    assert observer is not None
    await observer(_receipt())
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert isinstance(latest.intent, DeploymentScaleIntent)
    assert latest.intent.generation == 2
    assert latest.outcome.phase is DeploymentOutcomePhase.COMPLETED
    assert reader.calls == [("default", "web"), ("default", "web")]
    assert ui.notification_markup[-2:] == [False, False]


async def test_presentation_failure_does_not_abort_observation() -> None:
    class FailingPresentationUi(FakeUi):
        def push_screen(self, screen: Any, callback: Any = None) -> Any:
            assert self.workers
            raise RuntimeError("screen unavailable")

    reader = _Reader([_raw()])
    controller, ui = _controller(reader, ui=FailingPresentationUi())
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None

    await observer(_receipt())
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.COMPLETED
    assert ("Unable to open Deployment outcome details", "warning") in ui.notifications


async def test_worker_registration_failure_marks_tracker_incomplete() -> None:
    class FailingWorkerUi(FakeUi):
        def run_worker(self, work: Any, **kwargs: Any) -> Any:
            raise RuntimeError("worker unavailable")

    controller, _ui = _controller(_Reader([_raw()]), ui=FailingWorkerUi())
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None

    with pytest.raises(RuntimeError, match="worker unavailable"):
        await observer(_receipt())

    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert "worker" in latest.outcome.summary.lower()


async def test_open_latest_presents_newest_retained_tracker() -> None:
    reader = _Reader([_raw()])
    controller, ui = _controller(reader)
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await _drain(ui)

    assert isinstance(ui.screens[0][0], DeploymentOutcomeScreen)
    controller.open_latest()
    screen, callback = ui.screens[-1]
    assert isinstance(screen, DeploymentOutcomeScreen)
    assert screen.snapshot is controller.latest()
    assert callback is not None


async def test_refresh_defers_modal_reopen_until_after_dismissal() -> None:
    controller, ui = _controller(_Reader([_raw()]))
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await _drain(ui)
    snapshot = controller.latest()
    assert snapshot is not None
    screen_count = len(ui.screens)

    controller._on_screen_result(("refresh", snapshot.tracker_id, "", "", ""))
    controller._update(
        snapshot.tracker_id,
        DeploymentOutcome(
            phase=DeploymentOutcomePhase.INCOMPLETE,
            summary="newer retained outcome",
        ),
        snapshot.attempts + 1,
    )

    assert len(ui.screens) == screen_count
    callback, args = ui.deferred[-1]
    callback(*args)
    assert len(ui.screens) == screen_count + 1
    refreshed = ui.screens[-1][0]
    assert isinstance(refreshed, DeploymentOutcomeScreen)
    assert refreshed.snapshot.outcome.summary == "newer retained outcome"


async def test_read_failure_preserves_last_confirmed_evidence() -> None:
    reader = _Reader([_raw(ready=1), RuntimeError("offline")])
    controller, ui = _controller(reader)
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert latest.outcome.evidence == (
        "generation 2/2",
        "replicas current=3 updated=3 ready=1 available=3 desired=3",
    )


@pytest.mark.parametrize(
    ("status", "classification"),
    [
        (403, "API 403: Deployment outcome read access denied"),
        (404, "API 404: Deployment unavailable"),
        (503, "API 503: Deployment outcome read failed"),
    ],
)
async def test_api_read_failure_exposes_safe_status_classification(
    status: int,
    classification: str,
) -> None:
    reader = _Reader([ApiStatusError(status, "token=secret", "credential=secret")])
    controller, ui = _controller(reader)
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert classification in latest.outcome.summary
    assert "secret" not in latest.outcome.summary


async def test_stop_does_not_overwrite_terminal_outcome() -> None:
    controller, ui = _controller(_Reader([_raw()]))
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await _drain(ui)
    latest = controller.latest()
    assert latest is not None

    assert controller.stop(latest.tracker_id) is False
    assert controller.latest() == latest


async def test_pod_result_dispatches_exact_tracker_identity() -> None:
    calls: list[tuple[str, int, str, str, str]] = []

    async def pod_action(
        verb: str,
        epoch: int,
        namespace: str,
        name: str,
        uid: str,
    ) -> None:
        calls.append((verb, epoch, namespace, name, uid))

    reader = _Reader([_raw()])
    surface = FakeUi()
    controller = DeploymentOutcomeController(
        ui=surface,
        reader=reader,
        get_epoch=lambda: 4,
        cluster_id=lambda: "cluster",
        poll_delays=(0.0,),
        pod_action=pod_action,
    )
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await _drain(surface)
    controller.open_latest()
    snapshot = controller.latest()
    assert snapshot is not None
    _screen, callback = surface.screens[-1]
    assert callback is not None

    callback(("logs", snapshot.tracker_id, "default", "web-pod", "pod-uid"))
    await _drain(surface)

    assert calls == [("logs", 4, "default", "web-pod", "pod-uid")]


async def test_pod_action_failure_is_reported() -> None:
    async def pod_action(
        verb: str,
        epoch: int,
        namespace: str,
        name: str,
        uid: str,
    ) -> None:
        raise RuntimeError("offline")

    surface = FakeUi()
    controller = DeploymentOutcomeController(
        ui=surface,
        reader=_Reader([_raw()]),
        get_epoch=lambda: 4,
        cluster_id=lambda: "cluster",
        poll_delays=(0.0,),
        pod_action=pod_action,
    )
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await _drain(surface)
    snapshot = controller.latest()
    assert snapshot is not None

    controller._on_screen_result(("events", snapshot.tracker_id, "default", "web-pod", "pod-uid"))
    await _drain(surface)

    assert ("Deployment outcome Pod action failed", "warning") in surface.notifications


async def test_pod_action_worker_registration_failure_closes_coroutine() -> None:
    class FailingPodActionWorkerUi(FakeUi):
        fail_registration = False
        rejected_work: Any = None

        def run_worker(self, work: Any, **kwargs: Any) -> Any:
            if self.fail_registration:
                self.rejected_work = work
                raise RuntimeError("worker unavailable")
            return super().run_worker(work, **kwargs)

    async def pod_action(
        verb: str,
        epoch: int,
        namespace: str,
        name: str,
        uid: str,
    ) -> None:
        raise AssertionError("closed coroutine must not run")

    surface = FailingPodActionWorkerUi()
    controller = DeploymentOutcomeController(
        ui=surface,
        reader=_Reader([_raw()]),
        get_epoch=lambda: 4,
        cluster_id=lambda: "cluster",
        poll_delays=(0.0,),
        pod_action=pod_action,
    )
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await _drain(surface)
    snapshot = controller.latest()
    assert snapshot is not None
    surface.fail_registration = True

    controller._on_screen_result(("events", snapshot.tracker_id, "default", "web-pod", "pod-uid"))

    assert inspect.getcoroutinestate(surface.rejected_work) == inspect.CORO_CLOSED
    assert surface.notifications[-1] == (
        "Deployment outcome Pod action failed",
        "warning",
    )


def test_open_latest_without_tracker_reports_unavailable() -> None:
    controller, ui = _controller(_Reader([]))

    controller.open_latest()

    assert ui.screens == []
    assert ui.notifications[-1] == (
        "No Deployment outcome has been tracked yet",
        "warning",
    )


async def test_restart_observer_carries_exact_restart_stamp() -> None:
    supplied_stamp = "2026-10-12T18:30:00+00:00"
    controller, ui = _controller(_Reader([_raw(restart_stamp=supplied_stamp)]))
    observer = controller.restart_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        restarted_at=supplied_stamp,
    )

    assert observer is not None
    await observer(_receipt("rollout_restart"))
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert isinstance(latest.intent, DeploymentRestartIntent)
    assert latest.intent.generation == 2
    assert latest.intent.restarted_at == supplied_stamp
    assert latest.outcome.phase is DeploymentOutcomePhase.COMPLETED


async def test_restart_without_response_generation_is_retained_incomplete() -> None:
    reader = _Reader([_raw()])
    controller, ui = _controller(reader)
    observer = controller.restart_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        restarted_at="restart-stamp",
    )
    assert observer is not None

    await observer(dataclasses.replace(_receipt(), action="rollout_restart", mutation=None))
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert "generation" in latest.outcome.summary
    assert ui.workers == []
    assert reader.calls == []


async def test_context_change_stops_before_another_cluster_read() -> None:
    epoch = [4]

    async def switch_epoch(_delay: float) -> None:
        epoch[0] = 5

    reader = _Reader([_raw(ready=1), _raw()])
    controller, ui = _controller(reader, epoch=epoch, sleep=switch_epoch)
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )

    assert observer is not None
    await observer(_receipt())
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.STOPPED
    assert latest.outcome.evidence
    assert reader.calls == [("default", "web")]


async def test_reader_failure_is_incomplete_not_failed_write() -> None:
    controller, ui = _controller(_Reader([PermissionError("denied")]))
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )

    assert observer is not None
    await observer(_receipt())
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert "accepted" in latest.outcome.summary.lower()


async def test_missing_identity_retains_incomplete_outcome() -> None:
    controller, _ui = _controller(_Reader([_raw()]))

    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid=None,
        replicas=3,
    )

    assert observer is not None
    await observer(_receipt())
    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert "UID" in latest.outcome.summary


async def test_registry_keeps_at_most_three_trackers() -> None:
    waiting = asyncio.Event()

    async def blocked_sleep(_delay: float) -> None:
        await waiting.wait()

    controller, ui = _controller(
        _Reader([_raw(ready=1) for _ in range(4)]),
        sleep=blocked_sleep,
    )
    for index in range(4):
        observer = controller.scale_observer(
            epoch=4,
            namespace="default",
            name=f"web-{index}",
            uid=f"deploy-{index}",
            replicas=3,
        )
        assert observer is not None
        await observer(_receipt())

    assert [snapshot.intent.target.name for snapshot in controller.snapshots()] == [
        "web-1",
        "web-2",
        "web-3",
    ]
    waiting.set()
    await _drain(ui, allow_cancelled=True)


async def test_late_read_cannot_overwrite_user_stop() -> None:
    started = asyncio.Event()
    now = [0.0]

    class NonCooperativeReader(DeploymentOutcomeReader):
        async def snapshot(self, namespace: str, name: str) -> RawDeploymentOutcomeSnapshot:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return _raw()
            raise AssertionError("unreachable")

    controller, ui = _controller(NonCooperativeReader(), clock=lambda: now[0])
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await started.wait()
    latest = controller.latest()
    assert latest is not None

    now[0] = 301.0
    assert controller.stop(latest.tracker_id)
    await _drain(ui, allow_cancelled=True)

    final = controller.latest()
    assert final is not None
    assert final.outcome.phase is DeploymentOutcomePhase.STOPPED


async def test_read_snapshot_reraises_caller_cancellation_when_reader_returns() -> None:
    started = asyncio.Event()
    reaped = asyncio.Event()

    class CancellationReturningReader(DeploymentOutcomeReader):
        async def snapshot(self, namespace: str, name: str) -> RawDeploymentOutcomeSnapshot:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return _raw()
            finally:
                reaped.set()
            raise AssertionError("unreachable")

    reader = CancellationReturningReader()
    controller, _ui = _controller(reader)
    read = asyncio.create_task(controller._read_snapshot(reader, "default", "web"))
    await started.wait()

    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read

    assert reaped.is_set()


async def test_exhausted_poll_schedule_is_not_reported_as_deadline() -> None:
    controller, ui = _controller(_Reader([_raw(ready=1), _raw(ready=1)]))
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert "schedule" in latest.outcome.summary
    assert "deadline" not in latest.outcome.summary


async def test_absolute_deadline_uses_configured_duration_in_summary() -> None:
    controller, ui = _controller(_Reader([_raw(ready=1)]), deadline_seconds=0.0)
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert "0-second observation deadline" in latest.outcome.summary


async def test_eviction_notice_uses_configured_tracker_limit() -> None:
    surface = FakeUi()
    controller = DeploymentOutcomeController(
        ui=surface,
        reader=_Reader([]),
        get_epoch=lambda: 4,
        cluster_id=lambda: "cluster",
        max_trackers=1,
    )
    for name in ("first", "second"):
        observer = controller.scale_observer(
            epoch=4,
            namespace="default",
            name=name,
            uid=None,
            replicas=3,
        )
        assert observer is not None
        await observer(_receipt())

    notices = [message for message, _severity in surface.notifications if "1-tracker" in message]
    assert notices == ["Evicted the oldest Deployment outcome at the 1-tracker retention limit"]


async def test_worker_delayed_past_deadline_does_not_start_a_read() -> None:
    class DeferredWork:
        def cancel(self) -> None:
            pass

    class DeferredUi(FakeUi):
        def __init__(self) -> None:
            super().__init__()
            self.work: Awaitable[None] | None = None

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
            self.work = work
            return DeferredWork()

    now = [0.0]
    reader = _Reader([_raw()])
    ui = DeferredUi()
    controller, _surface = _controller(reader, ui=ui, clock=lambda: now[0])
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    now[0] = 301.0
    assert ui.work is not None
    await ui.work

    assert reader.calls == []
    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert "deadline" in latest.outcome.summary


async def test_deadline_suppressed_by_sleep_does_not_start_another_read() -> None:
    now = [0.0]
    sleep_started = asyncio.Event()

    async def suppress_deadline_cancellation(delay: float) -> None:
        sleep_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            now[0] = 301.0

    reader = _Reader([_raw(ready=2), _raw()])
    controller, ui = _controller(
        reader,
        sleep=suppress_deadline_cancellation,
        deadline_seconds=0.01,
        clock=lambda: now[0],
    )
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await asyncio.wait_for(sleep_started.wait(), timeout=1.0)
    await asyncio.wait_for(_drain(ui), timeout=1.0)

    assert reader.calls == [("default", "web")]
    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert "deadline" in latest.outcome.summary


async def test_absolute_deadline_reaps_reader_that_suppresses_cancellation() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    reaped = asyncio.Event()

    class HungReader(DeploymentOutcomeReader):
        async def snapshot(self, namespace: str, name: str) -> RawDeploymentOutcomeSnapshot:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await asyncio.Future()
            finally:
                reaped.set()
            raise AssertionError("unreachable")

    controller, ui = _controller(HungReader(), deadline_seconds=0.01)
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await asyncio.wait_for(_drain(ui), timeout=1.0)

    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert "deadline" in latest.outcome.summary
    assert cancelled.is_set()
    assert reaped.is_set()


async def test_stop_all_is_bounded_when_reader_repeatedly_suppresses_cancellation() -> None:
    started = asyncio.Event()
    second_cancellation = asyncio.Event()
    release = asyncio.Event()
    reaped = asyncio.Event()
    cancellation_count = 0

    class RepeatedlySuppressingReader(DeploymentOutcomeReader):
        async def snapshot(self, namespace: str, name: str) -> RawDeploymentOutcomeSnapshot:
            nonlocal cancellation_count
            started.set()
            try:
                while True:
                    try:
                        await release.wait()
                        return _raw()
                    except asyncio.CancelledError:
                        cancellation_count += 1
                        if cancellation_count == 2:
                            second_cancellation.set()
            finally:
                reaped.set()

    class CancellingUi(FakeUi):
        async def cancel_workers(self, group: str) -> None:
            assert group == "deployment-outcomes"
            for worker in self.workers:
                worker.cancel()
            await asyncio.gather(*self.workers, return_exceptions=True)

    controller, _ui = _controller(RepeatedlySuppressingReader(), ui=CancellingUi())
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await started.wait()

    stopping = asyncio.create_task(controller.stop_all("context changed"))
    await asyncio.wait_for(second_cancellation.wait(), timeout=1.0)
    done, _pending = await asyncio.wait({stopping}, timeout=0.1)
    stopped_promptly = stopping in done
    release.set()
    await asyncio.wait_for(reaped.wait(), timeout=1.0)
    await asyncio.wait_for(stopping, timeout=1.0)

    assert stopped_promptly
    assert cancellation_count == 2


async def test_deadline_wins_when_reader_translates_timeout_cancellation() -> None:
    started = asyncio.Event()

    class CancellationTranslatingReader(DeploymentOutcomeReader):
        async def snapshot(self, namespace: str, name: str) -> RawDeploymentOutcomeSnapshot:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as exc:
                raise RuntimeError("translated cancellation") from exc
            raise AssertionError("unreachable")

    controller, ui = _controller(
        CancellationTranslatingReader(),
        clock=lambda: 0.0,
        deadline_seconds=0.05,
    )
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert "deadline" in latest.outcome.summary
    assert "RuntimeError" not in latest.outcome.summary


async def test_late_snapshot_cannot_complete_after_absolute_deadline() -> None:
    now = [0.0]

    class LateReader(DeploymentOutcomeReader):
        async def snapshot(self, namespace: str, name: str) -> RawDeploymentOutcomeSnapshot:
            now[0] = 301.0
            return _raw()

    controller, ui = _controller(LateReader(), clock=lambda: now[0])
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert "deadline" in latest.outcome.summary


async def test_cluster_change_during_read_discards_returned_evidence() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    cluster = ["context-a|https://cluster.example"]

    class DelayedReader(DeploymentOutcomeReader):
        async def snapshot(self, namespace: str, name: str) -> RawDeploymentOutcomeSnapshot:
            started.set()
            await release.wait()
            return _raw()

    controller, ui = _controller(DelayedReader(), cluster=cluster)
    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        replicas=3,
    )
    assert observer is not None
    await observer(_receipt())
    await started.wait()

    cluster[0] = "context-b|https://other.example"
    release.set()
    await _drain(ui)

    final = controller.latest()
    assert final is not None
    assert final.outcome.phase is DeploymentOutcomePhase.STOPPED
    assert final.outcome.evidence == ()
