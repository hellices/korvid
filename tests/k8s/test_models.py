from typing import Any

from korvid.k8s.models import PodSummary

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
