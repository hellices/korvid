from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.k8s.models import GenericSummary, PodSummary, ReplicaSetSummary, summary_for

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
    # Bare-point decimals: the Quantity grammar allows <digits>. and .<digits>
    assert parse_cpu(".5") == 0.5
    assert parse_cpu("1.") == 1.0
    assert parse_memory(".5Gi") == 2**29


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


def test_generic_summary_uid_and_owner_uids() -> None:
    manifest: dict[str, Any] = {
        "metadata": {
            "name": "web-6d9f88",
            "namespace": "prod",
            "uid": "rs-uid-1",
            "ownerReferences": [
                {"kind": "Deployment", "name": "web", "uid": "dep-uid-1"},
            ],
        }
    }
    gs = GenericSummary.from_manifest("ReplicaSet", manifest)
    assert gs.uid == "rs-uid-1"
    assert gs.owner_uids == ("dep-uid-1",)


def test_generic_summary_defaults_without_owner_info() -> None:
    gs = GenericSummary.from_manifest("Pod", {"metadata": {"name": "x", "namespace": "ns"}})
    assert gs.uid == ""
    assert gs.owner_uids == ()


def test_generic_summary_preserves_spec_replicas() -> None:
    """Scalable kinds (Deployment/StatefulSet) keep spec.replicas as
    `desired` so scale prompts can prefill the current count."""
    manifest: dict[str, Any] = {
        "metadata": {"name": "web", "namespace": "prod"},
        "spec": {"replicas": 3},
    }
    gs = GenericSummary.from_manifest("Deployment", manifest)
    assert gs.desired == 3


def test_generic_summary_desired_none_without_replicas() -> None:
    """Kinds without spec.replicas (or with a non-integer value) report
    desired=None, never a misleading 0."""
    gs = GenericSummary.from_manifest("Service", {"metadata": {"name": "svc", "namespace": "ns"}})
    assert gs.desired is None
    bad: dict[str, Any] = {"metadata": {"name": "x", "namespace": "ns"}, "spec": {"replicas": "3"}}
    assert GenericSummary.from_manifest("Deployment", bad).desired is None


def test_generic_summary_tolerates_non_mapping_spec() -> None:
    """CRDs may legally define spec as an array or scalar; summarising such
    objects must not raise and reports desired=None."""
    for spec in (["a", "b"], "raw", 7, True):
        manifest: dict[str, Any] = {"metadata": {"name": "x", "namespace": "ns"}, "spec": spec}
        assert GenericSummary.from_manifest("Widget", manifest).desired is None


def test_pod_summary_owner_uids() -> None:
    manifest: dict[str, Any] = {
        "metadata": {
            "name": "web-6d9f88-abc",
            "namespace": "prod",
            "ownerReferences": [{"kind": "ReplicaSet", "name": "web-6d9f88", "uid": "rs-uid-1"}],
        },
        "spec": {},
        "status": {},
    }
    pod = PodSummary.from_manifest(manifest)
    assert pod.owner_uids == ("rs-uid-1",)


# ---------------------------------------------------------------------------
# ReplicaSetSummary + summary_for
# ---------------------------------------------------------------------------


def _rs_manifest() -> dict[str, Any]:
    return {
        "metadata": {
            "name": "web-6d9f88",
            "namespace": "prod",
            "uid": "rs-uid-1",
            "creationTimestamp": "2024-06-01T10:00:00Z",
            "annotations": {"deployment.kubernetes.io/revision": "3"},
            "ownerReferences": [{"kind": "Deployment", "name": "web", "uid": "dep-uid-1"}],
        },
        "spec": {"replicas": 2},
        "status": {"replicas": 2, "readyReplicas": 1},
    }


def test_replicaset_summary_from_manifest() -> None:
    rs = ReplicaSetSummary.from_manifest("ReplicaSet", _rs_manifest())
    assert rs.name == "web-6d9f88"
    assert rs.revision == "3"
    assert rs.desired == 2
    assert rs.current == 2
    assert rs.ready == "1/2"
    assert rs.owner_uids == ("dep-uid-1",)


def test_replicaset_summary_scaled_to_zero() -> None:
    manifest = _rs_manifest()
    manifest["spec"] = {"replicas": 0}
    manifest["status"] = {}
    rs = ReplicaSetSummary.from_manifest("ReplicaSet", manifest)
    assert rs.desired == 0
    assert rs.current == 0
    assert rs.ready == "0/0"


def test_replicaset_summary_missing_revision() -> None:
    manifest = _rs_manifest()
    manifest["metadata"].pop("annotations")
    rs = ReplicaSetSummary.from_manifest("ReplicaSet", manifest)
    assert rs.revision == "-"


def test_summary_for_dispatches_replicaset() -> None:
    summary = summary_for("ReplicaSet", _rs_manifest())
    assert isinstance(summary, ReplicaSetSummary)


def test_summary_for_falls_back_to_generic() -> None:
    summary = summary_for("Deployment", {"metadata": {"name": "web", "namespace": "prod"}})
    assert isinstance(summary, GenericSummary)
    assert not isinstance(summary, ReplicaSetSummary)


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


class TestExactRequestValues:
    """PodSummary carries exact effective request values (issue #12 review:
    percent-of-request must not be computed from display-rounded strings)."""

    def test_from_manifest_fills_exact_requests(self) -> None:
        obj = {
            "metadata": {"name": "p", "namespace": "ns"},
            "spec": {
                "containers": [
                    {
                        "name": "c",
                        "resources": {"requests": {"cpu": "150m", "memory": "1500Ki"}},
                    }
                ]
            },
            "status": {},
        }
        pod = PodSummary.from_manifest(obj)
        assert pod.cpu_request_cores == pytest.approx(0.15)
        assert pod.mem_request_bytes == 1500 * 2**10
        assert pod.mem_request == "1Mi"  # display string still rounds

    def test_no_requests_gives_none(self) -> None:
        obj = {
            "metadata": {"name": "p", "namespace": "ns"},
            "spec": {"containers": [{"name": "c"}]},
            "status": {},
        }
        pod = PodSummary.from_manifest(obj)
        assert pod.cpu_request_cores is None
        assert pod.mem_request_bytes is None

    def test_from_manifest_fills_exact_limits(self) -> None:
        """Issue #50: severity coloring needs exact limit values too - per
        container, since the kubelet enforces each limit independently."""
        obj = {
            "metadata": {"name": "p", "namespace": "ns"},
            "spec": {
                "containers": [
                    {
                        "name": "c",
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "32Mi"},
                            "limits": {"cpu": "500m", "memory": "200Mi"},
                        },
                    }
                ]
            },
            "status": {},
        }
        pod = PodSummary.from_manifest(obj)
        (limit,) = pod.container_limits
        assert limit.cpu_cores == pytest.approx(0.5)
        assert limit.mem_bytes == 200 * 2**20

    def test_no_limits_gives_none(self) -> None:
        obj = {
            "metadata": {"name": "p", "namespace": "ns"},
            "spec": {"containers": [{"name": "c"}]},
            "status": {},
        }
        pod = PodSummary.from_manifest(obj)
        assert pod.cpu_limit_cores is None
        assert pod.mem_limit_bytes is None


class TestPodLevelRequests:
    """K8s 1.34+ pod-level resources (spec.resources) take precedence over
    the container-derived calculation (issue #12 review round 2)."""

    def test_pod_level_requests_take_precedence(self) -> None:
        obj = {
            "metadata": {"name": "p", "namespace": "ns"},
            "spec": {
                "resources": {"requests": {"cpu": "1", "memory": "2Gi"}},
                "containers": [
                    {
                        "name": "c",
                        "resources": {"requests": {"cpu": "150m", "memory": "1500Ki"}},
                    }
                ],
            },
            "status": {},
        }
        pod = PodSummary.from_manifest(obj)
        assert pod.cpu_request_cores == pytest.approx(1.0)
        assert pod.mem_request_bytes == 2 * 2**30
        assert pod.cpu_request == "1000m"
        assert pod.mem_request == "2048Mi"

    def test_pod_level_partial_falls_back_per_resource(self) -> None:
        """Pod-level sets only CPU: memory still comes from containers."""
        obj = {
            "metadata": {"name": "p", "namespace": "ns"},
            "spec": {
                "resources": {"requests": {"cpu": "2"}},
                "containers": [
                    {
                        "name": "c",
                        "resources": {
                            "requests": {"cpu": "150m", "memory": "64Mi"},
                            "limits": {"memory": "128Mi"},
                        },
                    }
                ],
            },
            "status": {},
        }
        pod = PodSummary.from_manifest(obj)
        assert pod.cpu_request_cores == pytest.approx(2.0)
        assert pod.mem_request_bytes == 64 * 2**20
        assert pod.mem_limit == "128Mi"

    def test_pod_level_limits_apply_too(self) -> None:
        obj = {
            "metadata": {"name": "p", "namespace": "ns"},
            "spec": {
                "resources": {"limits": {"cpu": "4"}},
                "containers": [{"name": "c", "resources": {"limits": {"cpu": "500m"}}}],
            },
            "status": {},
        }
        pod = PodSummary.from_manifest(obj)
        assert pod.cpu_limit == "4000m"


class TestSidecarInitOrdering:
    """Native sidecars keep running while later classic inits execute, so the
    init peak is cumulative-prior-sidecar-requests + that init's request
    (issue #12 review round 3)."""

    def _pod(self, init_containers: list[dict[str, object]]) -> dict[str, object]:
        return {
            "metadata": {"name": "p", "namespace": "ns"},
            "spec": {
                "initContainers": init_containers,
                "containers": [{"name": "c", "resources": {"requests": {"cpu": "50m"}}}],
            },
            "status": {},
        }

    def test_sidecar_before_init_adds_to_init_peak(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                [
                    {
                        "name": "sc",
                        "restartPolicy": "Always",
                        "resources": {"requests": {"cpu": "100m"}},
                    },
                    {"name": "init", "resources": {"requests": {"cpu": "500m"}}},
                ]
            )
        )
        # peak: sidecar (100m) still running while init (500m) runs = 600m
        assert pod.cpu_request_cores == pytest.approx(0.6)

    def test_sidecar_after_init_does_not_add_to_that_init(self) -> None:
        pod = PodSummary.from_manifest(
            self._pod(
                [
                    {"name": "init", "resources": {"requests": {"cpu": "500m"}}},
                    {
                        "name": "sc",
                        "restartPolicy": "Always",
                        "resources": {"requests": {"cpu": "100m"}},
                    },
                ]
            )
        )
        # init runs alone (500m); steady state is 50m + 100m = 150m
        assert pod.cpu_request_cores == pytest.approx(0.5)


class TestContainerLimits:
    """Review fix (PR #51 r4): per-container limits are enforced
    independently by the kubelet, so severity must key off per-container
    ratios - a pod-aggregate sum hides a sidecar sitting at its own limit.
    The pod-level aggregate fields now carry only spec.resources.limits
    (K8s 1.34+), which bound the whole pod by definition."""

    @staticmethod
    def _pod(spec: dict[str, Any]) -> PodSummary:
        return PodSummary.from_manifest(
            {"metadata": {"name": "p", "namespace": "ns"}, "spec": spec, "status": {}}
        )

    def test_per_container_limits_are_captured(self) -> None:
        pod = self._pod(
            {
                "containers": [
                    {"name": "a", "resources": {"limits": {"cpu": "500m", "memory": "256Mi"}}},
                    {"name": "b", "resources": {"limits": {"memory": "2Gi"}}},
                ]
            }
        )
        limits = {c.name: c for c in pod.container_limits}
        assert limits["a"].cpu_cores == pytest.approx(0.5)
        assert limits["a"].mem_bytes == 256 * 2**20
        assert limits["b"].cpu_cores is None
        assert limits["b"].mem_bytes == 2 * 2**30

    def test_init_containers_are_included(self) -> None:
        # Classic inits appear in pod metrics while they run; sidecars all
        # the time - both need their ceilings known.
        pod = self._pod(
            {
                "containers": [{"name": "a"}],
                "initContainers": [
                    {
                        "name": "sc",
                        "restartPolicy": "Always",
                        "resources": {"limits": {"memory": "64Mi"}},
                    },
                    {"name": "init", "resources": {"limits": {"memory": "128Mi"}}},
                ],
            }
        )
        limits = {c.name: c for c in pod.container_limits}
        assert limits["sc"].mem_bytes == 64 * 2**20
        assert limits["init"].mem_bytes == 128 * 2**20
        assert limits["a"].mem_bytes is None

    def test_pod_level_limits_fill_the_aggregate_fields(self) -> None:
        pod = self._pod(
            {
                "resources": {"limits": {"cpu": "2", "memory": "1Gi"}},
                "containers": [{"name": "a"}, {"name": "b"}],
            }
        )
        assert pod.cpu_limit_cores == pytest.approx(2.0)
        assert pod.mem_limit_bytes == 2**30

    def test_container_limits_never_fill_the_aggregate_fields(self) -> None:
        # Summed container limits are not a whole-pod ceiling: each is
        # enforced independently by the kubelet.
        pod = self._pod(
            {
                "containers": [
                    {"name": "a", "resources": {"limits": {"cpu": "500m", "memory": "256Mi"}}},
                ]
            }
        )
        assert pod.cpu_limit_cores is None
        assert pod.mem_limit_bytes is None


def test_pod_summary_carries_creation_timestamp() -> None:
    """Feeds age sorting (issue #37): timestamps compare, not '3h' strings."""
    manifest = {
        "metadata": {
            "name": "web-1",
            "namespace": "default",
            "creationTimestamp": "2026-07-26T09:00:00Z",
        },
        "spec": {},
        "status": {},
    }
    pod = PodSummary.from_manifest(manifest)
    assert pod.created == "2026-07-26T09:00:00Z"


def test_pod_summary_created_defaults_to_empty() -> None:
    pod = PodSummary.from_manifest({"metadata": {"name": "x"}, "spec": {}, "status": {}})
    assert pod.created == ""


def test_pod_summary_age_renders_like_generic() -> None:
    """Feeds the pods AGE column (issue #37 sort indicator visibility)."""
    pod = PodSummary.from_manifest(
        {
            "metadata": {"name": "x", "creationTimestamp": "2024-06-01T10:00:00Z"},
            "spec": {},
            "status": {},
        }
    )
    now = datetime(2024, 6, 1, 13, 0, 0, tzinfo=UTC)
    assert pod.age(now=now) == "3h"


def test_pod_summary_age_dash_when_created_missing() -> None:
    pod = PodSummary.from_manifest({"metadata": {"name": "x"}, "spec": {}, "status": {}})
    assert pod.age() == "-"


def test_pod_summary_carries_labels() -> None:
    """Labels feed the client-side `-l` filter (issue #44)."""
    manifest = {
        "metadata": {
            "name": "web-1",
            "namespace": "default",
            "labels": {"app": "web", "tier": "front"},
        },
        "spec": {},
        "status": {},
    }
    pod = PodSummary.from_manifest(manifest)
    assert dict(pod.labels) == {"app": "web", "tier": "front"}


def test_generic_summary_carries_labels() -> None:
    manifest = {
        "metadata": {"name": "web", "namespace": "default", "labels": {"app": "web"}},
        "spec": {},
    }
    gs = GenericSummary.from_manifest("Deployment", manifest)
    assert dict(gs.labels) == {"app": "web"}


def test_replicaset_summary_carries_labels() -> None:
    manifest = {
        "metadata": {"name": "web-abc", "namespace": "default", "labels": {"app": "web"}},
        "spec": {"replicas": 1},
        "status": {},
    }
    rs = ReplicaSetSummary.from_manifest("ReplicaSet", manifest)
    assert dict(rs.labels) == {"app": "web"}


def test_summaries_default_to_no_labels() -> None:
    gs = GenericSummary.from_manifest("ConfigMap", {"metadata": {"name": "cm"}})
    pod = PodSummary.from_manifest({"metadata": {"name": "p"}, "spec": {}, "status": {}})
    assert gs.labels == ()
    assert pod.labels == ()
