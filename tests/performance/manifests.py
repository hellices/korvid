from __future__ import annotations

import re

_RUN_ID_PATTERN = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,46}[a-z0-9])?$")
_SELECTOR_KEY_PATTERN = re.compile(r"^[a-z0-9](?:[-a-z0-9./]{0,251}[a-z0-9])?$")
_SELECTOR_VALUE_PATTERN = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,61}[a-z0-9])?$")
_MANAGED_BY = "korvid-performance"
_BENCH_IMAGE = "registry.k8s.io/pause:3.10"


def _validate_positive(value: int, label: str) -> int:
    if value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validate_run_id(run_id: str) -> str:
    if _RUN_ID_PATTERN.fullmatch(run_id):
        return run_id
    raise ValueError("run_id must be 1-48 lowercase letters, digits, or hyphens")


def _parse_node_selector(node_selector: str) -> dict[str, str]:
    if node_selector.count("=") != 1:
        raise ValueError("node_selector must be exactly one non-empty key=value pair")
    key, value = node_selector.split("=", 1)
    if not key or not value:
        raise ValueError("node_selector must be exactly one non-empty key=value pair")
    if key.strip() != key or value.strip() != value:
        raise ValueError("node_selector must be exactly one non-empty key=value pair")
    if not _SELECTOR_KEY_PATTERN.fullmatch(key) or not _SELECTOR_VALUE_PATTERN.fullmatch(value):
        raise ValueError("node_selector must be exactly one non-empty key=value pair")
    return {key: value}


def _common_labels(run_id: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/managed-by": _MANAGED_BY,
        "korvid.dev/performance-run": run_id,
    }


def _namespace_name(run_id: str, namespace_index: int) -> str:
    name = f"korvid-perf-{run_id}-{namespace_index}"
    if len(name) > 63:
        raise ValueError("generated namespace name must be 63 characters or fewer")
    return name


def build_seed_manifests(
    *,
    run_id: str,
    namespace_count: int,
    pods_per_namespace: int,
    node_selector: str,
) -> tuple[dict[str, object], ...]:
    run = _validate_run_id(run_id)
    namespaces = _validate_positive(namespace_count, "namespace_count")
    pods_each = _validate_positive(pods_per_namespace, "pods_per_namespace")
    selector = _parse_node_selector(node_selector)
    labels = _common_labels(run)

    manifests: list[dict[str, object]] = []
    namespace_names = tuple(_namespace_name(run, index) for index in range(namespaces))
    for name in namespace_names:
        manifests.append(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": name,
                    "labels": dict(labels),
                },
            }
        )

    for object_index in range(namespaces * pods_each):
        namespace_index = object_index % namespaces
        pod_name = f"bench-{object_index // namespaces}"
        manifests.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": pod_name,
                    "namespace": namespace_names[namespace_index],
                    "labels": dict(labels),
                },
                "spec": {
                    "nodeSelector": dict(selector),
                    "tolerations": [
                        {
                            "key": "purpose",
                            "operator": "Equal",
                            "value": "perftest",
                            "effect": "NoSchedule",
                        }
                    ],
                    "containers": [
                        {
                            "name": "bench",
                            "image": _BENCH_IMAGE,
                            "resources": {
                                "requests": {
                                    "cpu": "5m",
                                    "memory": "16Mi",
                                }
                            },
                        }
                    ],
                    "restartPolicy": "Always",
                },
            }
        )
    return tuple(manifests)
