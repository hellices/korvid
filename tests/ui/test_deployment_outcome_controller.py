from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from korvid.core.deployment_outcome import DeploymentOutcomePhase
from korvid.k8s.deployment_outcomes import (
    DeploymentOutcomeReader,
    RawDeploymentOutcomeSnapshot,
)
from korvid.ui.deployment_outcome_controller import DeploymentOutcomeController
from korvid.ui.write_gate import AcceptedWriteReceipt
from tests.ui.test_write_coordinator import FakeUi

_DEPLOYMENT = {
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
            "metadata": {
                "annotations": {
                    "kubectl.kubernetes.io/restartedAt": "restart-stamp"
                }
            }
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
    )


def _controller(
    reader: DeploymentOutcomeReader,
    *,
    epoch: list[int] | None = None,
    ui: FakeUi | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[DeploymentOutcomeController, FakeUi]:
    surface = ui or FakeUi()
    current_epoch = epoch or [4]
    controller = DeploymentOutcomeController(
        ui=surface,
        reader=reader,
        get_epoch=lambda: current_epoch[0],
        cluster_id=lambda: "context-a|https://cluster.example",
        sleep=sleep or asyncio.sleep,
        poll_delays=(0.0, 0.0),
        max_trackers=3,
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
    assert latest.outcome.phase is DeploymentOutcomePhase.COMPLETED
    assert reader.calls == [("default", "web"), ("default", "web")]


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
    assert latest.outcome.phase is DeploymentOutcomePhase.COMPLETED


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


async def test_missing_identity_refuses_to_build_observer() -> None:
    controller, _ui = _controller(_Reader([_raw()]))

    observer = controller.scale_observer(
        epoch=4,
        namespace="default",
        name="web",
        uid=None,
        replicas=3,
    )

    assert observer is None


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
