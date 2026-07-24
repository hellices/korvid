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
