from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from korvid.k8s import models
from korvid.k8s.models import (
    CSVSummary,
    EndpointSliceSummary,
    GenericSummary,
    OLMSubscriptionSummary,
    PackageManifestSummary,
    PodSummary,
    ReplicaSetSummary,
    StorageClassSummary,
    format_age,
    summary_for,
)
from korvid.k8s.relationship_facts import RelationKind, RelationshipFacts

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


def test_endpoint_slice_summary_counts_nil_ready_as_ready() -> None:
    summary = summary_for(
        "EndpointSlice",
        {
            "apiVersion": "discovery.k8s.io/v1",
            "kind": "EndpointSlice",
            "metadata": {
                "name": "api-x1",
                "namespace": "shop",
                "labels": {"kubernetes.io/service-name": "api"},
                "ownerReferences": [{"uid": "svc-1"}],
            },
            "addressType": "IPv4",
            "endpoints": [
                {"conditions": {"ready": True}},
                {"conditions": {}},
                {"conditions": {"ready": False}},
            ],
        },
    )
    assert isinstance(summary, EndpointSliceSummary)
    assert summary.service_name == "api"
    assert summary.address_type == "IPv4"
    assert summary.endpoints == 3
    assert summary.ready_endpoints == 2


def test_same_named_endpoint_slice_crd_stays_generic() -> None:
    summary = summary_for(
        "EndpointSlice",
        {"apiVersion": "example.io/v1", "metadata": {"name": "custom"}},
    )
    assert type(summary) is GenericSummary


def test_endpoint_slice_summary_ignores_malformed_endpoints() -> None:
    summary = summary_for(
        "EndpointSlice",
        {
            "apiVersion": "discovery.k8s.io/v1",
            "metadata": {"name": "api-x1", "namespace": "shop"},
            "endpoints": "oops",
        },
    )
    assert isinstance(summary, EndpointSliceSummary)
    assert summary.endpoints == 0
    assert summary.ready_endpoints == 0


def test_endpoint_slice_summary_treats_non_mapping_conditions_as_not_ready() -> None:
    summary = summary_for(
        "EndpointSlice",
        {
            "apiVersion": "discovery.k8s.io/v1",
            "metadata": {"name": "api-x1", "namespace": "shop"},
            "endpoints": [{"conditions": "oops"}],
        },
    )
    assert isinstance(summary, EndpointSliceSummary)
    assert summary.endpoints == 1
    assert summary.ready_endpoints == 0


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


# ---------------------------------------------------------------------------
# summary_for: authoritative group kwarg (issue #191 fix)
# ---------------------------------------------------------------------------


def test_summary_for_endpointslice_without_api_version_dispatched_by_group() -> None:
    """A LIST item that omits apiVersion becomes EndpointSliceSummary when the
    authoritative group is provided by the caller (e.g. KubeClient._object_summary)."""
    manifest: dict[str, Any] = {
        "metadata": {
            "name": "api-x1",
            "namespace": "shop",
            "labels": {"kubernetes.io/service-name": "api"},
            "ownerReferences": [{"uid": "svc-1"}],
        },
        "addressType": "IPv4",
        "endpoints": [
            {"conditions": {"ready": True}},
            {"conditions": {}},
        ],
    }
    summary = summary_for("EndpointSlice", manifest, group="discovery.k8s.io")
    assert isinstance(summary, EndpointSliceSummary)
    assert summary.service_name == "api"
    assert summary.ready_endpoints == 2


def test_summary_for_endpointslice_explicit_non_discovery_group_stays_generic() -> None:
    """When an authoritative non-discovery group is provided, even a manifest that
    claims apiVersion='discovery.k8s.io/v1' must remain GenericSummary."""
    manifest: dict[str, Any] = {
        "apiVersion": "discovery.k8s.io/v1",
        "metadata": {"name": "custom"},
    }
    summary = summary_for("EndpointSlice", manifest, group="example.io")
    assert type(summary) is GenericSummary


def test_summary_for_storage_class_without_api_version_dispatched_by_group() -> None:
    manifest: dict[str, Any] = {
        "metadata": {
            "name": "managed",
            "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
        },
        "provisioner": "disk.csi.azure.com",
        "volumeBindingMode": "WaitForFirstConsumer",
        "allowVolumeExpansion": True,
    }
    summary = summary_for("StorageClass", manifest, group="storage.k8s.io")
    assert isinstance(summary, StorageClassSummary)
    assert summary.is_default
    assert summary.provisioner == "disk.csi.azure.com"
    assert summary.volume_binding_mode == "WaitForFirstConsumer"
    assert summary.allow_volume_expansion is True


def test_summary_for_same_named_storage_class_crd_stays_generic() -> None:
    summary = summary_for("StorageClass", {"metadata": {"name": "custom"}}, group="example.io")
    assert type(summary) is GenericSummary


@pytest.mark.parametrize(
    "key",
    [
        "storageclass.kubernetes.io/is-default-class",
        "storageclass.beta.kubernetes.io/is-default-class",
    ],
)
def test_storage_class_default_annotation_exact_true_string(key: str) -> None:
    summary = StorageClassSummary.from_manifest(
        "StorageClass", {"metadata": {"name": "managed", "annotations": {key: "true"}}}
    )
    assert summary.is_default


@pytest.mark.parametrize(
    "key",
    [
        "storageclass.kubernetes.io/is-default-class",
        "storageclass.beta.kubernetes.io/is-default-class",
    ],
)
@pytest.mark.parametrize("value", ["TRUE", "True"])
def test_storage_class_non_lowercase_true_is_not_default(key: str, value: str) -> None:
    summary = StorageClassSummary.from_manifest(
        "StorageClass", {"metadata": {"name": "managed", "annotations": {key: value}}}
    )
    assert not summary.is_default


def test_storage_class_malformed_values_fall_back_safely() -> None:
    summary = StorageClassSummary.from_manifest(
        "StorageClass",
        {
            "metadata": {"name": "managed"},
            "provisioner": ["bad"],
            "volumeBindingMode": {"bad": True},
            "allowVolumeExpansion": "yes",
        },
    )
    assert summary.provisioner == ""
    assert summary.volume_binding_mode == "Immediate"
    assert summary.allow_volume_expansion is False


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


# ---------------------------------------------------------------------------
# OLM summaries (issue #29)
# ---------------------------------------------------------------------------


def _pkg_manifest() -> dict[str, Any]:
    return {
        "apiVersion": "packages.operators.coreos.com/v1",
        "kind": "PackageManifest",
        "metadata": {
            "name": "cert-manager",
            "namespace": "olm",
            "creationTimestamp": "2026-07-20T00:00:00Z",
        },
        "status": {
            "catalogSource": "operatorhubio-catalog",
            "defaultChannel": "stable",
            "channels": [{"name": "candidate"}, {"name": "stable"}],
        },
    }


def test_summary_for_dispatches_packagemanifest() -> None:
    summary = summary_for("PackageManifest", _pkg_manifest())
    assert isinstance(summary, PackageManifestSummary)
    assert summary.catalog == "operatorhubio-catalog"
    assert summary.default_channel == "stable"
    assert summary.channels == ("candidate", "stable")


def test_packagemanifest_summary_tolerates_malformed_status() -> None:
    manifest = _pkg_manifest()
    manifest["status"] = "oops"
    summary = summary_for("PackageManifest", manifest)
    assert isinstance(summary, PackageManifestSummary)
    assert summary.catalog == ""
    assert summary.channels == ()


def test_summary_for_dispatches_olm_subscription() -> None:
    manifest = {
        "apiVersion": "operators.coreos.com/v1alpha1",
        "kind": "Subscription",
        "metadata": {"name": "cert-manager", "namespace": "operators"},
        "spec": {"channel": "stable", "source": "operatorhubio-catalog"},
        "status": {"installedCSV": "cert-manager.v1.14.4", "state": "AtLatestKnown"},
    }
    summary = summary_for("Subscription", manifest)
    assert isinstance(summary, OLMSubscriptionSummary)
    assert summary.channel == "stable"
    assert summary.source == "operatorhubio-catalog"
    assert summary.installed_csv == "cert-manager.v1.14.4"
    assert summary.state == "AtLatestKnown"


def test_summary_for_leaves_non_olm_subscription_kinds_generic() -> None:
    """Other API groups also define a Subscription kind (e.g. eventing);
    only operators.coreos.com objects get the OLM columns."""
    manifest = {
        "apiVersion": "messaging.example.com/v1",
        "kind": "Subscription",
        "metadata": {"name": "events", "namespace": "prod"},
    }
    summary = summary_for("Subscription", manifest)
    assert not isinstance(summary, OLMSubscriptionSummary)


def test_summary_for_dispatches_csv() -> None:
    manifest = {
        "apiVersion": "operators.coreos.com/v1alpha1",
        "kind": "ClusterServiceVersion",
        "metadata": {"name": "cert-manager.v1.14.4", "namespace": "operators"},
        "spec": {"version": "1.14.4", "displayName": "cert-manager"},
        "status": {"phase": "Succeeded"},
    }
    summary = summary_for("ClusterServiceVersion", manifest)
    assert isinstance(summary, CSVSummary)
    assert summary.version == "1.14.4"
    assert summary.phase == "Succeeded"
    assert summary.display_name == "cert-manager"


def test_packagemanifest_summary_tolerates_non_list_channels() -> None:
    """status.channels as a scalar must not break summary construction
    (a malformed catalog entry must never kill the watch)."""
    summary = summary_for(
        "PackageManifest",
        {
            "apiVersion": "packages.operators.coreos.com/v1",
            "kind": "PackageManifest",
            "metadata": {"name": "p", "namespace": "olm", "uid": "u1"},
            "status": {"channels": 1},
        },
    )
    assert isinstance(summary, PackageManifestSummary)
    assert summary.channels == ()


def test_packagemanifest_summary_extracts_short_description() -> None:
    """The default channel's CSV description annotation is the catalog's own
    short description; it is capped so a hostile entry cannot bloat rows."""
    manifest = _pkg_manifest()
    manifest["status"]["channels"] = [
        {"name": "candidate", "currentCSVDesc": {"annotations": {"description": "wrong"}}},
        {
            "name": "stable",
            "currentCSVDesc": {"annotations": {"description": "X.509 certificate management"}},
        },
    ]
    summary = summary_for("PackageManifest", manifest)
    assert isinstance(summary, PackageManifestSummary)
    assert summary.description == "X.509 certificate management"


def test_packagemanifest_summary_description_caps_and_falls_back() -> None:
    manifest = _pkg_manifest()
    manifest["status"]["channels"] = [
        {
            "name": "stable",
            "currentCSVDesc": {"description": "line one " + "x" * 200 + "\nline two"},
        }
    ]
    summary = summary_for("PackageManifest", manifest)
    assert isinstance(summary, PackageManifestSummary)
    assert summary.description.startswith("line one")
    assert "line two" not in summary.description
    assert len(summary.description) <= 80
    assert summary.description.endswith("\u2026")


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


# -- EndpointSliceSummary.service_owner_uids projection (PR #212) ------------


def test_endpoint_slice_summary_service_owner_uids_filters_to_core_service() -> None:
    """Mixed ownerReferences: generic owner_uids contains all UIDs, but
    service_owner_uids contains only refs whose kind=='Service' and apiVersion=='v1'."""
    manifest: dict[str, Any] = {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSlice",
        "metadata": {
            "name": "api-x1",
            "namespace": "shop",
            "labels": {"kubernetes.io/service-name": "api"},
            "ownerReferences": [
                # core/v1 Service — should appear in service_owner_uids
                {"kind": "Service", "apiVersion": "v1", "uid": "svc-uid-1"},
                # custom CRD controller — must NOT appear in service_owner_uids
                {"kind": "MeshController", "apiVersion": "mesh.example.io/v1", "uid": "crd-uid-2"},
            ],
        },
        "addressType": "IPv4",
        "endpoints": [{"conditions": {"ready": True}}],
    }
    summary = summary_for("EndpointSlice", manifest)
    assert isinstance(summary, EndpointSliceSummary)
    # Generic owner_uids carries all UIDs (unchanged relation behaviour)
    assert set(summary.owner_uids) == {"svc-uid-1", "crd-uid-2"}
    # service_owner_uids carries only the core/v1 Service UID
    assert summary.service_owner_uids == ("svc-uid-1",)


def test_endpoint_slice_summary_wrong_api_version_service_excluded() -> None:
    """A 'Service' ownerRef with a non-core apiVersion must be excluded from
    service_owner_uids (it might be a CRD impersonating the name 'Service')."""
    manifest: dict[str, Any] = {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSlice",
        "metadata": {
            "name": "api-x2",
            "namespace": "shop",
            "ownerReferences": [
                {"kind": "Service", "apiVersion": "custom.io/v1", "uid": "fake-svc-uid"},
            ],
        },
        "addressType": "IPv4",
        "endpoints": [],
    }
    summary = summary_for("EndpointSlice", manifest)
    assert isinstance(summary, EndpointSliceSummary)
    # Generic owner_uids still carries the UID
    assert "fake-svc-uid" in summary.owner_uids
    # service_owner_uids must exclude the malformed/wrong-apiVersion ref
    assert summary.service_owner_uids == ()


def test_endpoint_slice_summary_no_owner_refs_gives_empty_service_owner_uids() -> None:
    """EndpointSlice without ownerReferences has empty service_owner_uids."""
    manifest: dict[str, Any] = {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSlice",
        "metadata": {"name": "manual-slice", "namespace": "shop"},
        "addressType": "IPv4",
        "endpoints": [{"conditions": {"ready": True}}],
    }
    summary = summary_for("EndpointSlice", manifest)
    assert isinstance(summary, EndpointSliceSummary)
    assert summary.service_owner_uids == ()


# ============================================================
# PR #216 review findings — Item 4: annotation key/value preservation
# ============================================================


def test_storage_class_stable_annotation_key_is_preserved() -> None:
    """Stable annotation key must be captured in default_annotation_key."""
    summary = StorageClassSummary.from_manifest(
        "StorageClass",
        {
            "metadata": {
                "name": "managed",
                "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
            }
        },
    )
    assert summary.is_default
    assert summary.default_annotation_key == "storageclass.kubernetes.io/is-default-class"
    assert summary.default_annotation_value == "true"


def test_storage_class_beta_annotation_key_is_preserved() -> None:
    """Beta annotation key must be captured in default_annotation_key."""
    summary = StorageClassSummary.from_manifest(
        "StorageClass",
        {
            "metadata": {
                "name": "managed",
                "annotations": {"storageclass.beta.kubernetes.io/is-default-class": "true"},
            }
        },
    )
    assert summary.is_default
    assert summary.default_annotation_key == "storageclass.beta.kubernetes.io/is-default-class"
    assert summary.default_annotation_value == "true"


def test_storage_class_stable_takes_priority_when_both_set() -> None:
    """When both stable and beta keys are set to 'true', stable wins."""
    summary = StorageClassSummary.from_manifest(
        "StorageClass",
        {
            "metadata": {
                "name": "managed",
                "annotations": {
                    "storageclass.kubernetes.io/is-default-class": "true",
                    "storageclass.beta.kubernetes.io/is-default-class": "true",
                },
            }
        },
    )
    assert summary.is_default
    assert summary.default_annotation_key == "storageclass.kubernetes.io/is-default-class"


def test_storage_class_non_default_has_empty_annotation_fields() -> None:
    """Non-default StorageClass must have empty annotation key/value fields."""
    summary = StorageClassSummary.from_manifest("StorageClass", {"metadata": {"name": "managed"}})
    assert not summary.is_default
    assert summary.default_annotation_key == ""
    assert summary.default_annotation_value == ""


def test_storage_class_non_true_value_produces_empty_fields() -> None:
    """Non-'true' annotation value must produce is_default=False and empty key/value."""
    summary = StorageClassSummary.from_manifest(
        "StorageClass",
        {
            "metadata": {
                "name": "managed",
                "annotations": {"storageclass.kubernetes.io/is-default-class": "True"},
            }
        },
    )
    assert not summary.is_default
    assert summary.default_annotation_key == ""
    assert summary.default_annotation_value == ""


def test_generic_summary_carries_relationship_facts() -> None:
    """GenericSummary must expose the resource's extracted relationship facts (issue #281)."""
    summary = summary_for(
        "Service",
        {
            "apiVersion": "v1",
            "metadata": {"name": "api", "namespace": "prod", "uid": "svc-1"},
            "spec": {"selector": {"app": "api"}},
        },
        group="",
    )
    assert summary.relationships.selectors[0].target_kind == "Pod"


def test_generic_summary_relationship_facts_default_to_empty() -> None:
    """A kind with no relationship extractor must default to empty RelationshipFacts."""
    summary = GenericSummary.from_manifest("ConfigMap", {"metadata": {"name": "cfg"}})
    assert summary.relationships == RelationshipFacts()


def test_summary_for_pdb_list_item_without_api_version_uses_authoritative_version() -> None:
    """A PodDisruptionBudget LIST item omitting apiVersion must still resolve the
    v1-vs-v1beta1 empty-selector semantics correctly when the caller supplies the
    authoritative `version` (from ResourceMeta), matching how `group` is already
    threaded through instead of relying on the (often-missing) manifest apiVersion
    (issue #281)."""
    manifest: dict[str, Any] = {
        # apiVersion deliberately omitted, as with real LIST items.
        "metadata": {"name": "all", "namespace": "prod"},
        "spec": {"selector": {}},
    }
    summary_v1 = summary_for("PodDisruptionBudget", manifest, group="policy", version="v1")
    assert len(summary_v1.relationships.selectors) == 1
    selector_fact = summary_v1.relationships.selectors[0]
    assert selector_fact.relation is RelationKind.PROTECTED_BY
    assert selector_fact.empty_matches is True

    summary_v1beta1 = summary_for(
        "PodDisruptionBudget", manifest, group="policy", version="v1beta1"
    )
    assert summary_v1beta1.relationships.selectors == ()


def test_summary_for_pdb_without_version_falls_back_to_manifest_api_version() -> None:
    """Direct callers that do not supply `version` must keep deriving it from the
    manifest's `apiVersion`, unchanged from before `version` threading was added."""
    manifest: dict[str, Any] = {
        "apiVersion": "policy/v1",
        "metadata": {"name": "all", "namespace": "prod"},
        "spec": {"selector": {}},
    }
    summary = summary_for("PodDisruptionBudget", manifest)
    assert len(summary.relationships.selectors) == 1
    assert summary.relationships.selectors[0].empty_matches is True

    manifest_v1beta1 = dict(manifest, apiVersion="policy/v1beta1")
    summary_v1beta1 = summary_for("PodDisruptionBudget", manifest_v1beta1)
    assert summary_v1beta1.relationships.selectors == ()


def test_pod_summary_never_retains_secret_values() -> None:
    """PodSummary must carry relationship facts without ever leaking manifest content
    outside the metadata-only safety boundary (issue #281)."""
    summary = PodSummary.from_manifest(
        {
            "metadata": {"name": "api", "namespace": "prod", "uid": "pod-1"},
            "spec": {
                "containers": [{"name": "api"}],
                "volumes": [{"name": "s", "secret": {"secretName": "api-tls"}}],
            },
            "data": {"token": "forbidden-value"},
        }
    )
    assert summary.relationships.references[0].target.name == "api-tls"
    assert "forbidden-value" not in repr(summary)


def test_replicaset_summary_carries_relationship_facts_via_authoritative_group() -> None:
    """ReplicaSetSummary must preserve `relationships` from GenericSummary rather than
    defaulting empty, and must use the authoritative `group` kwarg (not the manifest's
    `apiVersion`, which native K8s LIST items commonly omit) (issue #281)."""
    manifest: dict[str, Any] = {
        # apiVersion deliberately omitted, as with real LIST items.
        "metadata": {
            "name": "web-6d9f88",
            "namespace": "prod",
            "uid": "rs-1",
            "ownerReferences": [
                {"apiVersion": "apps/v1", "kind": "Deployment", "name": "web", "uid": "dep-1"}
            ],
        },
        "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "web"}}},
        "status": {"replicas": 2, "readyReplicas": 2},
    }
    summary = summary_for("ReplicaSet", manifest, group="apps")
    assert isinstance(summary, ReplicaSetSummary)
    assert summary.desired == 2
    pairs = {
        (fact.relation, fact.target.kind, fact.target.name)
        for fact in summary.relationships.references
    }
    assert (RelationKind.OWNED_BY, "Deployment", "web") in pairs
    assert summary.relationships.selectors[0].relation is RelationKind.MANAGED_BY
    assert summary.relationships.selectors[0].target_kind == "Pod"


def test_endpoint_slice_summary_carries_relationship_facts_via_authoritative_group() -> None:
    """EndpointSliceSummary must preserve `relationships` from GenericSummary, threading
    the authoritative `group` kwarg through so LIST items that omit `apiVersion` still
    resolve owner references and target refs correctly (issue #281)."""
    manifest: dict[str, Any] = {
        # apiVersion deliberately omitted, as with real LIST items.
        "metadata": {
            "name": "api-abc",
            "namespace": "prod",
            "uid": "eps-1",
            "labels": {"kubernetes.io/service-name": "api"},
            "ownerReferences": [
                {"apiVersion": "v1", "kind": "Service", "name": "api", "uid": "svc-1"}
            ],
        },
        "addressType": "IPv4",
        "endpoints": [
            {
                "conditions": {"ready": True},
                "targetRef": {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "namespace": "prod",
                    "name": "api-0",
                    "uid": "pod-1",
                },
            }
        ],
    }
    summary = summary_for("EndpointSlice", manifest, group="discovery.k8s.io")
    assert isinstance(summary, EndpointSliceSummary)
    assert summary.service_name == "api"
    pairs = {
        (fact.relation, fact.target.kind, fact.target.name)
        for fact in summary.relationships.references
    }
    assert (RelationKind.OWNED_BY, "Service", "api") in pairs
    # ROUTES_TO is gated on the resolved group == "discovery.k8s.io"; this only
    # succeeds when `group` is threaded through instead of the (missing) apiVersion.
    assert (RelationKind.ROUTES_TO, "Pod", "api-0") in pairs


class _CountingDatetime(datetime):
    """`datetime` that records how often a timestamp string is parsed."""

    parses = 0

    @classmethod
    def fromisoformat(cls, date_string: str) -> datetime:  # type: ignore[override]  # counting shim over the stdlib classmethod
        _CountingDatetime.parses += 1
        return datetime.fromisoformat(date_string)


def test_age_is_not_reparsed_within_the_same_displayed_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every repaint asks all rows for their age, but "5m" is the answer for a
    whole minute. Re-parsing the same creation timestamp inside one displayed
    minute is work the answer's own validity window already settled."""
    created = "2031-03-04T00:00:00Z"
    base = datetime(2031, 3, 4, 0, 5, 0, tzinfo=UTC)
    assert format_age(created, base) == "5m"

    monkeypatch.setattr(models, "datetime", _CountingDatetime)
    _CountingDatetime.parses = 0

    assert format_age(created, base + timedelta(seconds=30)) == "5m"

    assert _CountingDatetime.parses == 0


def test_age_advances_when_the_displayed_minute_rolls_over() -> None:
    created = "2031-03-05T00:00:00Z"
    base = datetime(2031, 3, 5, 0, 5, 0, tzinfo=UTC)
    assert format_age(created, base) == "5m"

    assert format_age(created, base + timedelta(seconds=60)) == "6m"


def test_age_is_correct_when_asked_about_an_earlier_instant() -> None:
    """Panes repaint independently, so a later call can carry an earlier clock
    reading; a remembered answer must not outrun its own window backwards."""
    created = "2031-03-06T00:00:00Z"
    base = datetime(2031, 3, 6, 0, 5, 0, tzinfo=UTC)
    assert format_age(created, base) == "5m"

    assert format_age(created, base - timedelta(minutes=3)) == "2m"


def test_age_advances_across_the_hour_and_day_boundaries() -> None:
    created = "2031-03-07T00:00:00Z"
    assert format_age(created, datetime(2031, 3, 7, 0, 59, 59, tzinfo=UTC)) == "59m"
    assert format_age(created, datetime(2031, 3, 7, 1, 0, 0, tzinfo=UTC)) == "1h"
    assert format_age(created, datetime(2031, 3, 7, 23, 59, 59, tzinfo=UTC)) == "23h"
    assert format_age(created, datetime(2031, 3, 8, 0, 0, 0, tzinfo=UTC)) == "1d"
    assert format_age(created, datetime(2031, 3, 9, 0, 0, 0, tzinfo=UTC)) == "2d"


def test_distinct_creation_timestamps_keep_distinct_ages() -> None:
    older = "2031-03-10T00:00:00Z"
    newer = "2031-03-10T00:04:00Z"
    now = datetime(2031, 3, 10, 0, 5, 0, tzinfo=UTC)

    assert format_age(older, now) == "5m"
    assert format_age(newer, now) == "1m"


def test_age_rejects_empty_unparsable_and_future_timestamps() -> None:
    now = datetime(2031, 3, 11, 0, 5, 0, tzinfo=UTC)

    assert format_age("", now) == "-"
    assert format_age("not-a-timestamp", now) == "-"
    assert format_age("2031-03-11T00:06:00Z", now) == "-"


def test_age_reads_a_naive_clock_reading_as_utc() -> None:
    """A timezone-less `created` is documented as UTC, so a timezone-less
    `now` must be read the same way. Passing it to `datetime.timestamp()`
    instead resolves it against the host's local zone, which silently returns
    a different age — or "-" — depending on where the process runs."""
    created = "2031-04-01T00:00:00Z"
    aware = datetime(2031, 4, 1, 1, 0, 0, tzinfo=UTC)

    assert format_age(created, aware) == "1h"
    assert format_age(created, aware.replace(tzinfo=None)) == "1h"


def test_a_naive_clock_reading_stays_consistent_across_buckets() -> None:
    created = "2031-04-02T00:00:00Z"

    assert format_age(created, datetime(2031, 4, 2, 0, 5, 0)) == "5m"
    assert format_age(created, datetime(2031, 4, 3, 0, 0, 0)) == "1d"
    assert format_age(created, datetime(2031, 4, 1, 23, 0, 0)) == "-"


def test_an_oversized_creation_timestamp_is_not_remembered() -> None:
    """`created` is unvalidated API-server input and becomes a memo key.
    `datetime.fromisoformat` accepts an arbitrarily long fractional-second
    field, so capping entry *count* alone bounds nothing: 20,000 such keys
    would retain gigabytes. The answer is still correct, just not kept."""
    oversized = "2031-08-01T00:00:00." + "0" * 5000 + "+00:00"
    now = datetime(2031, 8, 1, 0, 5, 0, tzinfo=UTC)

    assert format_age(oversized, now) == "5m"

    assert oversized not in models._AGE_WINDOWS


def test_the_age_memo_stays_correct_when_it_reaches_its_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discarding remembered answers must cost accuracy nothing."""
    monkeypatch.setattr(models, "_MAX_AGE_WINDOWS", 2)
    now = datetime(2031, 8, 2, 0, 10, 0, tzinfo=UTC)
    stamps = [f"2031-08-02T00:0{minute}:00Z" for minute in range(5)]

    ages = [format_age(stamp, now) for stamp in stamps]

    assert ages == ["10m", "9m", "8m", "7m", "6m"]
    assert len(models._AGE_WINDOWS) <= 2
