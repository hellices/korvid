"""Tests for ContainerTrouble capture on PodSummary (ops hint strip, #26)."""

from __future__ import annotations

from typing import Any

from korvid.k8s.models import ContainerTrouble, PodSummary, _container_trouble


def _pod(
    container_statuses: list[dict[str, Any]] | None = None,
    init_container_statuses: list[dict[str, Any]] | None = None,
    phase: str = "Running",
) -> dict[str, Any]:
    status: dict[str, Any] = {"phase": phase}
    if container_statuses is not None:
        status["containerStatuses"] = container_statuses
    if init_container_statuses is not None:
        status["initContainerStatuses"] = init_container_statuses
    return {
        "metadata": {"name": "web-1", "namespace": "default", "uid": "u1"},
        "spec": {"containers": [{"name": "app"}]},
        "status": status,
    }


def _manifest(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "metadata": {"name": "web-1", "namespace": "default", "uid": "u1"},
        "spec": {"containers": [{"name": "app"}]},
        "status": status,
    }


def test_healthy_pod_has_no_trouble() -> None:
    summary = PodSummary.from_manifest(
        _pod([{"name": "app", "ready": True, "restartCount": 0, "state": {"running": {}}}])
    )
    assert summary.trouble == ()


def test_crashloop_captures_waiting_reason_message_and_last_termination() -> None:
    summary = PodSummary.from_manifest(
        _pod(
            [
                {
                    "name": "app",
                    "ready": False,
                    "restartCount": 12,
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off 5m0s restarting failed container",
                        }
                    },
                    "lastState": {
                        "terminated": {
                            "exitCode": 137,
                            "reason": "OOMKilled",
                            "finishedAt": "2026-07-26T08:00:00Z",
                        }
                    },
                }
            ]
        )
    )
    assert summary.trouble == (
        ContainerTrouble(
            container="app",
            reason="CrashLoopBackOff",
            message="back-off 5m0s restarting failed container",
            exit_code=137,
            exit_reason="OOMKilled",
            finished_at="2026-07-26T08:00:00Z",
            restarts=12,
        ),
    )


def test_imagepull_backoff_without_last_termination() -> None:
    summary = PodSummary.from_manifest(
        _pod(
            [
                {
                    "name": "app",
                    "ready": False,
                    "restartCount": 0,
                    "state": {
                        "waiting": {
                            "reason": "ImagePullBackOff",
                            "message": 'Back-off pulling image "nginx:nope"',
                        }
                    },
                }
            ],
            phase="Pending",
        )
    )
    assert len(summary.trouble) == 1
    entry = summary.trouble[0]
    assert entry.reason == "ImagePullBackOff"
    assert entry.exit_code is None
    assert entry.exit_reason is None
    assert entry.finished_at is None


def test_currently_terminated_container_is_trouble() -> None:
    """A container sitting in a terminated state with a non-zero exit code is
    captured even without a waiting reason."""
    summary = PodSummary.from_manifest(
        _pod(
            [
                {
                    "name": "app",
                    "ready": False,
                    "restartCount": 3,
                    "state": {
                        "terminated": {
                            "exitCode": 1,
                            "reason": "Error",
                            "finishedAt": "2026-07-26T08:05:00Z",
                        }
                    },
                }
            ],
            phase="Running",
        )
    )
    assert len(summary.trouble) == 1
    entry = summary.trouble[0]
    assert entry.reason == "Error"
    assert entry.exit_code == 1
    assert entry.finished_at == "2026-07-26T08:05:00Z"


def test_completed_zero_exit_is_not_trouble() -> None:
    """A Completed (exit 0) container is a normal terminal state, not trouble."""
    summary = PodSummary.from_manifest(
        _pod(
            [
                {
                    "name": "job",
                    "ready": False,
                    "restartCount": 0,
                    "state": {"terminated": {"exitCode": 0, "reason": "Completed"}},
                }
            ],
            phase="Succeeded",
        )
    )
    assert summary.trouble == ()


def test_container_creating_is_not_trouble() -> None:
    """Routine startup states must not light up the hint strip."""
    for reason in ("ContainerCreating", "PodInitializing"):
        summary = PodSummary.from_manifest(
            _pod(
                [
                    {
                        "name": "app",
                        "ready": False,
                        "restartCount": 0,
                        "state": {"waiting": {"reason": reason}},
                    }
                ],
                phase="Pending",
            )
        )
        assert summary.trouble == ()


def test_init_container_trouble_is_prefixed() -> None:
    summary = PodSummary.from_manifest(
        _pod(
            container_statuses=[
                {
                    "name": "app",
                    "ready": False,
                    "restartCount": 0,
                    "state": {"waiting": {"reason": "PodInitializing"}},
                }
            ],
            init_container_statuses=[
                {
                    "name": "migrate",
                    "ready": False,
                    "restartCount": 4,
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                    "lastState": {"terminated": {"exitCode": 2, "reason": "Error"}},
                }
            ],
            phase="Pending",
        )
    )
    assert len(summary.trouble) == 1
    entry = summary.trouble[0]
    assert entry.container == "init:migrate"
    assert entry.reason == "CrashLoopBackOff"
    assert entry.exit_code == 2


def test_multiple_troubled_containers_all_captured() -> None:
    summary = PodSummary.from_manifest(
        _pod(
            [
                {
                    "name": "app",
                    "ready": False,
                    "restartCount": 2,
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                },
                {
                    "name": "sidecar",
                    "ready": False,
                    "restartCount": 0,
                    "state": {"waiting": {"reason": "ImagePullBackOff"}},
                },
            ]
        )
    )
    assert [t.container for t in summary.trouble] == ["app", "sidecar"]


def test_evicted_pod_reports_pod_level_trouble() -> None:
    manifest = _manifest(
        {
            "phase": "Failed",
            "reason": "Evicted",
            "message": "The node was low on resource: memory.",
        }
    )
    pod = PodSummary.from_manifest(manifest)
    assert pod.trouble == (
        ContainerTrouble(
            container="pod",
            reason="Evicted",
            message="The node was low on resource: memory.",
        ),
    )


def test_unschedulable_pending_pod_reports_pod_level_trouble() -> None:
    manifest = _manifest(
        {
            "phase": "Pending",
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": "0/3 nodes are available: 3 Insufficient cpu.",
                }
            ],
        }
    )
    pod = PodSummary.from_manifest(manifest)
    assert pod.trouble == (
        ContainerTrouble(
            container="pod",
            reason="Unschedulable",
            message="0/3 nodes are available: 3 Insufficient cpu.",
        ),
    )


def test_succeeded_pod_with_status_reason_is_not_trouble() -> None:
    manifest = _manifest({"phase": "Succeeded", "reason": "Completed"})
    pod = PodSummary.from_manifest(manifest)
    assert pod.trouble == ()


def test_pod_level_trouble_precedes_container_trouble() -> None:
    manifest = _manifest(
        {
            "phase": "Failed",
            "reason": "Evicted",
            "message": "node pressure",
            "containerStatuses": [
                {
                    "name": "app",
                    "restartCount": 3,
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                }
            ],
        }
    )
    pod = PodSummary.from_manifest(manifest)
    assert pod.trouble[0].container == "pod"
    assert pod.trouble[0].reason == "Evicted"
    assert pod.trouble[1].container == "app"


def test_running_not_ready_with_abnormal_last_termination_is_trouble() -> None:
    cs = {
        "name": "app",
        "ready": False,
        "restartCount": 3,
        "state": {"running": {"startedAt": "2026-07-26T08:00:00Z"}},
        "lastState": {
            "terminated": {
                "reason": "OOMKilled",
                "exitCode": 137,
                "finishedAt": "2026-07-26T07:59:00Z",
            }
        },
    }
    entry = _container_trouble(cs)
    assert entry is not None
    assert entry.reason == "NotReady"
    assert entry.exit_code == 137
    assert entry.exit_reason == "OOMKilled"
    assert entry.finished_at == "2026-07-26T07:59:00Z"
    assert entry.restarts == 3


def test_running_not_ready_with_completed_last_termination_is_not_trouble() -> None:
    cs = {
        "name": "app",
        "ready": False,
        "state": {"running": {}},
        "lastState": {"terminated": {"reason": "Completed", "exitCode": 0}},
    }
    assert _container_trouble(cs) is None


def test_running_ready_with_abnormal_last_termination_is_not_trouble() -> None:
    cs = {
        "name": "app",
        "ready": True,
        "state": {"running": {}},
        "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
    }
    assert _container_trouble(cs) is None


def test_running_state_empty_object_still_counts_as_running() -> None:
    cs = {
        "name": "app",
        "ready": False,
        "state": {"running": {}},  # startedAt is optional
        "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
    }
    entry = _container_trouble(cs)
    assert entry is not None
    assert entry.reason == "NotReady"


def test_ready_transition_time_is_captured() -> None:
    manifest = _pod([])
    manifest["status"]["conditions"] = [
        {"type": "PodScheduled", "status": "True", "lastTransitionTime": "2026-07-26T06:00:00Z"},
        {"type": "Ready", "status": "False", "lastTransitionTime": "2026-07-26T08:30:00Z"},
    ]
    summary = PodSummary.from_manifest(manifest)
    assert summary.ready_transition_at == "2026-07-26T08:30:00Z"
    assert PodSummary.from_manifest(_pod([])).ready_transition_at is None
