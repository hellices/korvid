from typing import Any

import pytest

from korvid.k8s.models import PodSummary


def _pod(spec: dict[str, Any]) -> PodSummary:
    return PodSummary.from_manifest(
        {"metadata": {"name": "sandbox", "namespace": "prod"}, "spec": spec}
    )


def test_overhead_is_added_to_requests_and_nonzero_limit_totals() -> None:
    pod = _pod(
        {
            "overhead": {"cpu": "100m", "memory": "16Mi"},
            "containers": [
                {
                    "name": "app",
                    "resources": {
                        "requests": {"cpu": "700m", "memory": "128Mi"},
                        "limits": {"cpu": "1500m", "memory": "256Mi"},
                    },
                }
            ],
        }
    )

    assert pod.cpu_request == "800m"
    assert pod.mem_request == "144Mi"
    assert pod.cpu_request_cores == pytest.approx(0.8)
    assert pod.mem_request_bytes == 144 * 2**20
    assert pod.cpu_limit == "1600m"
    assert pod.mem_limit == "272Mi"
    assert pod.cpu_limit_cores is None
    assert pod.mem_limit_bytes is None
    assert pod.container_limits[0].cpu_cores == pytest.approx(1.5)
    assert pod.container_limits[0].mem_bytes == 256 * 2**20


def test_overhead_only_resources_create_requests_but_not_limits() -> None:
    pod = _pod({"overhead": {"cpu": "10m", "memory": "1Mi"}, "containers": [{"name": "app"}]})

    assert pod.cpu_request == "10m"
    assert pod.mem_request == "1Mi"
    assert pod.cpu_request_cores == pytest.approx(0.01)
    assert pod.mem_request_bytes == 2**20
    assert pod.cpu_limit == "-"
    assert pod.mem_limit == "-"
    assert pod.cpu_limit_cores is None
    assert pod.mem_limit_bytes is None


def test_overhead_is_selected_per_resource() -> None:
    pod = _pod(
        {
            "overhead": {"memory": "3Mi"},
            "containers": [{"name": "app", "resources": {"requests": {"cpu": "100m"}}}],
        }
    )

    assert pod.cpu_request == "100m"
    assert pod.mem_request == "3Mi"
    assert pod.cpu_limit == "-"
    assert pod.mem_limit == "-"


@pytest.mark.parametrize("pod_level", [False, True])
def test_overhead_does_not_turn_explicit_zero_limits_into_caps(pod_level: bool) -> None:
    spec: dict[str, Any] = {"overhead": {"cpu": "100m", "memory": "16Mi"}}
    resources = {"limits": {"cpu": "0", "memory": "0"}}
    if pod_level:
        spec["resources"] = resources
    else:
        spec["containers"] = [{"name": "app", "resources": resources}]

    pod = _pod(spec)

    assert pod.cpu_request == "100m"
    assert pod.mem_request == "16Mi"
    assert pod.cpu_limit == "0m"
    assert pod.mem_limit == "0Mi"
    assert pod.cpu_limit_cores == (0.0 if pod_level else None)
    assert pod.mem_limit_bytes == (0 if pod_level else None)


def test_overhead_is_added_after_pod_level_resource_precedence() -> None:
    pod = _pod(
        {
            "overhead": {"cpu": "100m", "memory": "16Mi"},
            "resources": {
                "requests": {"cpu": "2", "memory": "1Gi"},
                "limits": {"cpu": "3", "memory": "2Gi"},
            },
            "containers": [
                {
                    "name": "app",
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "64Mi"},
                        "limits": {"cpu": "1500m", "memory": "128Mi"},
                    },
                }
            ],
        }
    )

    assert pod.cpu_request == "2100m"
    assert pod.mem_request == "1040Mi"
    assert pod.cpu_request_cores == pytest.approx(2.1)
    assert pod.mem_request_bytes == 1040 * 2**20
    assert pod.cpu_limit == "3100m"
    assert pod.mem_limit == "2064Mi"
    assert pod.cpu_limit_cores == pytest.approx(3.1)
    assert pod.mem_limit_bytes == 2064 * 2**20
    assert pod.container_limits[0].cpu_cores == pytest.approx(1.5)
    assert pod.container_limits[0].mem_bytes == 128 * 2**20


def test_partial_pod_level_resources_keep_the_per_resource_fallback() -> None:
    pod = _pod(
        {
            "overhead": {"cpu": "100m", "memory": "16Mi"},
            "resources": {"requests": {"cpu": "2"}, "limits": {"cpu": "4"}},
            "containers": [
                {
                    "name": "app",
                    "resources": {"requests": {"memory": "64Mi"}, "limits": {"memory": "128Mi"}},
                }
            ],
        }
    )

    assert pod.cpu_request == "2100m"
    assert pod.mem_request == "80Mi"
    assert pod.cpu_limit == "4100m"
    assert pod.mem_limit == "144Mi"
    assert pod.cpu_limit_cores == pytest.approx(4.1)
    assert pod.mem_limit_bytes is None


@pytest.mark.parametrize("bucket", ["requests", "limits"])
def test_overhead_is_added_once_after_restartable_init_peak_calculation(bucket: str) -> None:
    pod = _pod(
        {
            "overhead": {"cpu": "100m", "memory": "16Mi"},
            "containers": [
                {"name": "app", "resources": {bucket: {"cpu": "100m", "memory": "32Mi"}}}
            ],
            "initContainers": [
                {
                    "name": "before",
                    "restartPolicy": "Always",
                    "resources": {bucket: {"cpu": "100m", "memory": "16Mi"}},
                },
                {"name": "init", "resources": {bucket: {"cpu": "500m", "memory": "64Mi"}}},
                {
                    "name": "after",
                    "restartPolicy": "Always",
                    "resources": {bucket: {"cpu": "50m", "memory": "8Mi"}},
                },
            ],
        }
    )
    field = "request" if bucket == "requests" else "limit"

    assert getattr(pod, f"cpu_{field}") == "700m"
    assert getattr(pod, f"mem_{field}") == "96Mi"
    if bucket == "requests":
        assert pod.cpu_request_cores == pytest.approx(0.7)
        assert pod.mem_request_bytes == 96 * 2**20
