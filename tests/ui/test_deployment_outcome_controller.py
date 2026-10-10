from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Awaitable, Callable
from typing import Any

from korvid.core.deployment_outcome import (
    DeploymentOutcomePhase,
    DeploymentRestartIntent,
    DeploymentScaleIntent,
)
from korvid.k8s.deployment_outcomes import (
    DeploymentOutcomeReader,
    RawDeploymentOutcomeSnapshot,
)
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


def _raw(*, ready: int = 3) -> RawDeploymentOutcomeSnapshot:
    deployment = {
        **_DEPLOYMENT,
        "metadata": dict(_DEPLOYMENT["metadata"]),
        "spec": dict(_DEPLOYMENT["spec"]),
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


async def _drain(ui: FakeUi) -> None:
    if ui.workers:
        await asyncio.gather(*ui.workers, return_exceptions=True)


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
    assert latest.outcome.evidence


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


def test_open_latest_without_tracker_reports_unavailable() -> None:
    controller, ui = _controller(_Reader([]))

    controller.open_latest()

    assert ui.screens == []
    assert ui.notifications[-1] == (
        "No Deployment outcome has been tracked yet",
        "warning",
    )


async def test_restart_observer_carries_exact_restart_stamp() -> None:
    controller, ui = _controller(_Reader([_raw()]))
    observer = controller.restart_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        restarted_at="restart-stamp",
    )

    assert observer is not None
    await observer(_receipt("rollout_restart"))
    await _drain(ui)

    latest = controller.latest()
    assert latest is not None
    assert isinstance(latest.intent, DeploymentRestartIntent)
    assert latest.intent.generation == 2
    assert latest.outcome.phase is DeploymentOutcomePhase.COMPLETED


async def test_restart_without_response_generation_is_retained_incomplete() -> None:
    controller, _ui = _controller(_Reader([_raw()]))
    observer = controller.restart_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid="deploy-uid",
        restarted_at="restart-stamp",
    )
    assert observer is not None

    await observer(dataclasses.replace(_receipt(), action="rollout_restart", mutation=None))

    latest = controller.latest()
    assert latest is not None
    assert latest.outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert "generation" in latest.outcome.summary


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

    assert len(controller.snapshots()) == 3
    waiting.set()
    await _drain(ui)


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
    await _drain(ui)

    final = controller.latest()
    assert final is not None
    assert final.outcome.phase is DeploymentOutcomePhase.STOPPED


async def test_absolute_deadline_marks_hung_read_incomplete() -> None:
    class HungReader(DeploymentOutcomeReader):
        async def snapshot(self, namespace: str, name: str) -> RawDeploymentOutcomeSnapshot:
            await asyncio.Future()
            raise AssertionError("unreachable")

    controller, ui = _controller(HungReader(), deadline_seconds=0.0)
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
