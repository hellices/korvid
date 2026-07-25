from datetime import UTC, datetime
from typing import Any

from korvid.k8s.models import GenericSummary, PodSummary

POD: dict[str, Any] = {
    "metadata": {"name": "checkout-7d9f", "namespace": "prod"},
    "spec": {
        "nodeName": "node-1",
        "containers": [
            {
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "500m", "memory": "256Mi"},
                }
            },
            {
                "resources": {
                    "requests": {"cpu": "1", "memory": "1Gi"},
                    "limits": {"memory": "2Gi"},
                }
            },
        ],
    },
    "status": {
        "phase": "Running",
        "qosClass": "Burstable",
        "containerStatuses": [
            {"ready": True, "restartCount": 0},
            {"ready": False, "restartCount": 7},
        ],
    },
}


def test_from_manifest() -> None:
    pod = PodSummary.from_manifest(POD)
    assert pod.name == "checkout-7d9f"
    assert pod.namespace == "prod"
    assert pod.phase == "Running"
    assert pod.ready == "1/2"
    assert pod.restarts == 7
    assert pod.node == "node-1"
    assert pod.qos == "Burstable"
    # summed across containers: 100m + 1 = 1100m; 128Mi + 1Gi = 1152Mi
    assert pod.cpu_request == "1100m"
    assert pod.mem_request == "1152Mi"
    assert pod.cpu_limit == "500m"  # only one container declares a cpu limit
    assert pod.mem_limit == "2304Mi"


def test_from_manifest_no_statuses() -> None:
    pod = PodSummary.from_manifest(
        {"metadata": {"name": "p", "namespace": "d"}, "spec": {}, "status": {"phase": "Pending"}}
    )
    assert pod.ready == "0/0"
    assert pod.restarts == 0
    assert pod.node is None
    assert pod.qos == "-"
    assert pod.cpu_request == "-"
    assert pod.mem_request == "-"
    assert pod.cpu_limit == "-"
    assert pod.mem_limit == "-"


def test_effective_request_includes_init_containers() -> None:
    # Effective request = max(max(initContainers), sum(containers)) per k8s scheduler.
    pod = PodSummary.from_manifest(
        {
            "metadata": {"name": "p", "namespace": "d"},
            "spec": {
                "initContainers": [
                    {"resources": {"requests": {"cpu": "2", "memory": "4Gi"}}},
                ],
                "containers": [
                    {"resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}},
                    {"resources": {"requests": {"cpu": "200m", "memory": "128Mi"}}},
                ],
            },
            "status": {"phase": "Running"},
        }
    )
    assert pod.cpu_request == "2000m"  # init container dominates
    assert pod.mem_request == "4096Mi"


def test_small_memory_renders_in_ki() -> None:
    pod = PodSummary.from_manifest(
        {
            "metadata": {"name": "p", "namespace": "d"},
            "spec": {"containers": [{"resources": {"requests": {"memory": "512Ki"}}}]},
            "status": {"phase": "Running"},
        }
    )
    assert pod.mem_request == "512Ki"  # not rounded down to a misleading 0Mi


def test_sidecar_init_containers_add_to_sum() -> None:
    # K8s 1.28+ sidecars (initContainers with restartPolicy: Always) run for the
    # pod's lifetime: the scheduler adds them to the main-container sum instead
    # of folding them into the classic init-container max.
    pod = PodSummary.from_manifest(
        {
            "metadata": {"name": "p", "namespace": "d"},
            "spec": {
                "initContainers": [
                    {  # classic init: compared via max
                        "resources": {"requests": {"cpu": "300m", "memory": "64Mi"}},
                    },
                    {  # sidecar: added to the sum
                        "restartPolicy": "Always",
                        "resources": {"requests": {"cpu": "100m", "memory": "32Mi"}},
                    },
                ],
                "containers": [
                    {"resources": {"requests": {"cpu": "200m", "memory": "128Mi"}}},
                ],
            },
            "status": {"phase": "Running"},
        }
    )
    # sum = 200m(main) + 100m(sidecar) = 300m; classic init max = 300m -> 300m
    assert pod.cpu_request == "300m"
    # sum = 128Mi + 32Mi = 160Mi > classic init 64Mi
    assert pod.mem_request == "160Mi"


def test_parse_full_quantity_grammar() -> None:
    from korvid.k8s.models import parse_cpu, parse_memory

    # DecimalSI suffixes beyond the common ones
    assert parse_memory("400m") == 0  # millibytes: 0.4 bytes truncates to 0
    assert parse_memory("1Pi") == 2**50
    assert parse_memory("1Ei") == 2**60
    assert parse_memory("5P") == 5 * 10**15
    assert parse_memory("5E") == 5 * 10**18
    # Decimal exponent form
    assert parse_memory("1e3") == 1000
    assert parse_memory("12E2") == 1200
    assert parse_cpu("1e-3") == 0.001
    # Plain and fractional
    assert parse_cpu("2") == 2.0
    assert parse_cpu("0.5") == 0.5
    assert parse_cpu("250m") == 0.25
    assert parse_memory("128Mi") == 128 * 2**20


def test_parse_invalid_quantity_raises_value_error() -> None:
    import pytest

    from korvid.k8s.models import parse_memory

    with pytest.raises(ValueError, match="invalid Kubernetes quantity"):
        parse_memory("12abc")


# ---------------------------------------------------------------------------
# GenericSummary
# ---------------------------------------------------------------------------


def test_generic_summary_from_manifest() -> None:
    manifest: dict[str, Any] = {
        "metadata": {
            "name": "my-dep",
            "namespace": "prod",
            "creationTimestamp": "2024-06-01T10:00:00Z",
        }
    }
    gs = GenericSummary.from_manifest("Deployment", manifest)
    assert gs.name == "my-dep"
    assert gs.namespace == "prod"
    assert gs.kind == "Deployment"
    assert gs.created == "2024-06-01T10:00:00Z"


def test_generic_summary_missing_creation_timestamp() -> None:
    manifest: dict[str, Any] = {"metadata": {"name": "x", "namespace": "ns"}}
    gs = GenericSummary.from_manifest("Pod", manifest)
    assert gs.created == ""


def test_age_5m() -> None:
    gs = GenericSummary(name="x", namespace="ns", kind="Pod", created="2024-01-01T12:00:00Z")
    now = datetime(2024, 1, 1, 12, 5, 0, tzinfo=UTC)
    assert gs.age(now) == "5m"


def test_age_3h() -> None:
    gs = GenericSummary(name="x", namespace="ns", kind="Pod", created="2024-01-01T12:00:00Z")
    now = datetime(2024, 1, 1, 15, 0, 0, tzinfo=UTC)
    assert gs.age(now) == "3h"


def test_age_2d() -> None:
    gs = GenericSummary(name="x", namespace="ns", kind="Pod", created="2024-01-01T12:00:00Z")
    now = datetime(2024, 1, 3, 12, 0, 0, tzinfo=UTC)
    assert gs.age(now) == "2d"


def test_age_empty_created() -> None:
    gs = GenericSummary(name="x", namespace="ns", kind="Pod", created="")
    assert gs.age() == "-"


# ---------------------------------------------------------------------------
# PodSummary — containers field
# ---------------------------------------------------------------------------


def test_pod_summary_containers_from_manifest() -> None:
    manifest: dict[str, Any] = {
        "metadata": {"name": "p", "namespace": "d"},
        "spec": {
            "containers": [
                {"name": "app", "image": "nginx"},
                {"name": "sidecar", "image": "envoy"},
            ]
        },
        "status": {"phase": "Running"},
    }
    pod = PodSummary.from_manifest(manifest)
    assert pod.containers == ("app", "sidecar")


def test_pod_summary_containers_defaults_to_empty_tuple() -> None:
    manifest: dict[str, Any] = {
        "metadata": {"name": "p", "namespace": "d"},
        "spec": {},
        "status": {"phase": "Pending"},
    }
    pod = PodSummary.from_manifest(manifest)
    assert pod.containers == ()


class TestDisplayPhase:
    """Displayed phase mirrors kubectl: container waiting/terminated reasons win."""

    @staticmethod
    def _pod(status: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "metadata": {"name": "p", "namespace": "d", **(metadata or {})},
            "spec": {},
            "status": status,
        }

    def test_crashloopbackoff_overrides_running_phase(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "ready": False,
                            "restartCount": 12,
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        }
                    ],
                }
            )
        )
        assert pod.phase == "CrashLoopBackOff"

    def test_imagepullbackoff_overrides_pending_phase(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Pending",
                    "containerStatuses": [
                        {"ready": False, "state": {"waiting": {"reason": "ImagePullBackOff"}}}
                    ],
                }
            )
        )
        assert pod.phase == "ImagePullBackOff"

    def test_terminated_reason_shown_for_succeeded_pod(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Succeeded",
                    "containerStatuses": [
                        {
                            "ready": False,
                            "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
                        }
                    ],
                }
            )
        )
        assert pod.phase == "Completed"

    def test_oomkilled_terminated_reason(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Failed",
                    "containerStatuses": [
                        {
                            "ready": False,
                            "state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
                        }
                    ],
                }
            )
        )
        assert pod.phase == "OOMKilled"

    def test_deletion_timestamp_shows_terminating(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {"phase": "Running", "containerStatuses": [{"ready": True, "state": {}}]},
                metadata={"deletionTimestamp": "2026-01-01T00:00:00Z"},
            )
        )
        assert pod.phase == "Terminating"

    def test_deletion_timestamp_wins_over_waiting_reason(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Running",
                    "containerStatuses": [
                        {"ready": False, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}
                    ],
                },
                metadata={"deletionTimestamp": "2026-01-01T00:00:00Z"},
            )
        )
        assert pod.phase == "Terminating"

    def test_running_container_keeps_phase(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Running",
                    "containerStatuses": [
                        {"ready": True, "state": {"running": {"startedAt": "2026-01-01"}}}
                    ],
                }
            )
        )
        assert pod.phase == "Running"

    def test_status_reason_used_when_no_container_reason(self) -> None:
        pod = PodSummary.from_manifest(self._pod({"phase": "Failed", "reason": "Evicted"}))
        assert pod.phase == "Evicted"

    def test_init_container_crashloop_shows_init_prefix(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Pending",
                    "initContainerStatuses": [
                        {"ready": False, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}
                    ],
                    "containerStatuses": [
                        {"ready": False, "state": {"waiting": {"reason": "PodInitializing"}}}
                    ],
                }
            )
        )
        assert pod.phase == "Init:CrashLoopBackOff"

    def test_init_container_terminated_error_shows_init_prefix(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Pending",
                    "initContainerStatuses": [
                        {
                            "ready": False,
                            "state": {"terminated": {"reason": "Error", "exitCode": 1}},
                        }
                    ],
                }
            )
        )
        assert pod.phase == "Init:Error"

    def test_init_container_succeeded_falls_through_to_containers(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Running",
                    "initContainerStatuses": [
                        {
                            "ready": False,
                            "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
                        }
                    ],
                    "containerStatuses": [
                        {"ready": True, "state": {"running": {"startedAt": "2026-01-01"}}}
                    ],
                }
            )
        )
        assert pod.phase == "Running"

    def test_init_container_in_progress_shows_counter(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Pending",
                    "initContainerStatuses": [
                        {"ready": False, "state": {"waiting": {"reason": "PodInitializing"}}}
                    ],
                }
            )
        )
        assert pod.phase == "Init:0/1"

    def test_started_sidecar_init_container_skipped(self) -> None:
        pod = PodSummary.from_manifest(
            {
                "metadata": {"name": "p", "namespace": "d"},
                "spec": {"initContainers": [{"name": "sc", "restartPolicy": "Always"}]},
                "status": {
                    "phase": "Running",
                    "initContainerStatuses": [
                        {
                            "name": "sc",
                            "ready": True,
                            "started": True,
                            "state": {"running": {"startedAt": "2026-01-01"}},
                        }
                    ],
                    "containerStatuses": [
                        {"ready": True, "state": {"running": {"startedAt": "2026-01-01"}}}
                    ],
                },
            }
        )
        assert pod.phase == "Running"

    def test_completed_sidecar_with_running_main_shows_running(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [
                        {"ready": True, "state": {"running": {"startedAt": "2026-01-01"}}},
                        {
                            "ready": False,
                            "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
                        },
                    ],
                }
            )
        )
        assert pod.phase == "Running"

    def test_completed_sidecar_without_pod_ready_shows_not_ready(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "False"}],
                    "containerStatuses": [
                        {"ready": True, "state": {"running": {"startedAt": "2026-01-01"}}},
                        {
                            "ready": False,
                            "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
                        },
                    ],
                }
            )
        )
        assert pod.phase == "NotReady"

    def test_terminated_without_reason_falls_back_to_exit_code(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Failed",
                    "containerStatuses": [
                        {"ready": False, "state": {"terminated": {"exitCode": 2}}}
                    ],
                }
            )
        )
        assert pod.phase == "ExitCode:2"

    def test_terminated_without_reason_prefers_signal(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Failed",
                    "containerStatuses": [
                        {"ready": False, "state": {"terminated": {"exitCode": 137, "signal": 9}}}
                    ],
                }
            )
        )
        assert pod.phase == "Signal:9"

    def test_terminated_zero_exit_no_reason_keeps_phase(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Succeeded",
                    "containerStatuses": [
                        {"ready": False, "state": {"terminated": {"exitCode": 0}}}
                    ],
                }
            )
        )
        assert pod.phase == "Succeeded"

    def test_init_terminated_without_reason_falls_back_to_exit_code(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Pending",
                    "initContainerStatuses": [
                        {"ready": False, "state": {"terminated": {"exitCode": 3}}}
                    ],
                }
            )
        )
        assert pod.phase == "Init:ExitCode:3"

    def test_init_progress_denominator_uses_spec_count(self) -> None:
        pod = PodSummary.from_manifest(
            {
                "metadata": {"name": "p", "namespace": "d"},
                "spec": {"initContainers": [{"name": "a"}, {"name": "b"}]},
                "status": {
                    "phase": "Pending",
                    "initContainerStatuses": [
                        {
                            "name": "a",
                            "ready": False,
                            "state": {"waiting": {"reason": "PodInitializing"}},
                        }
                    ],
                },
            }
        )
        assert pod.phase == "Init:0/2"

    def test_initialized_condition_true_skips_init_status(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Running",
                    "conditions": [{"type": "Initialized", "status": "True"}],
                    "initContainerStatuses": [
                        {"ready": False, "state": {"waiting": {"reason": "PodInitializing"}}}
                    ],
                    "containerStatuses": [
                        {"ready": False, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}
                    ],
                }
            )
        )
        assert pod.phase == "CrashLoopBackOff"

    def test_initialized_condition_false_keeps_init_status(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Pending",
                    "conditions": [{"type": "Initialized", "status": "False"}],
                    "initContainerStatuses": [
                        {"ready": False, "state": {"waiting": {"reason": "PodInitializing"}}}
                    ],
                }
            )
        )
        assert pod.phase == "Init:0/1"

    def test_deleting_pod_on_lost_node_shows_unknown(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {"phase": "Running", "reason": "NodeLost"},
                metadata={"deletionTimestamp": "2026-01-01T00:00:00Z"},
            )
        )
        assert pod.phase == "Unknown"

    def test_deleting_terminal_pod_on_lost_node_shows_unknown(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {"phase": "Failed", "reason": "NodeLost"},
                metadata={"deletionTimestamp": "2026-01-01T00:00:00Z"},
            )
        )
        assert pod.phase == "Unknown"

    def test_deleting_succeeded_pod_keeps_completed(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Succeeded",
                    "containerStatuses": [
                        {
                            "ready": False,
                            "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
                        }
                    ],
                },
                metadata={"deletionTimestamp": "2026-01-01T00:00:00Z"},
            )
        )
        assert pod.phase == "Completed"

    def test_deleting_failed_pod_keeps_failure_reason(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Failed",
                    "containerStatuses": [
                        {
                            "ready": False,
                            "state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
                        }
                    ],
                },
                metadata={"deletionTimestamp": "2026-01-01T00:00:00Z"},
            )
        )
        assert pod.phase == "OOMKilled"

    def test_scheduling_gated_pod_shows_scheduling_gated(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                {
                    "phase": "Pending",
                    "conditions": [
                        {
                            "type": "PodScheduled",
                            "status": "False",
                            "reason": "SchedulingGated",
                        }
                    ],
                }
            )
        )
        assert pod.phase == "SchedulingGated"
