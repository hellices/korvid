from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from tests.performance.manifests import build_seed_manifests


def _labels(manifest: dict[str, object]) -> dict[str, str]:
    metadata = manifest["metadata"]
    assert isinstance(metadata, dict)
    labels = metadata["labels"]
    assert isinstance(labels, dict)
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items())
    return labels


def _namespace_name(manifest: dict[str, object]) -> str:
    metadata = manifest["metadata"]
    assert isinstance(metadata, dict)
    name = metadata["name"]
    assert isinstance(name, str)
    return name


def _pod_names(manifests: tuple[dict[str, object], ...]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for manifest in manifests:
        metadata = manifest["metadata"]
        assert isinstance(metadata, dict)
        namespace = metadata["namespace"]
        name = metadata["name"]
        assert isinstance(namespace, str)
        assert isinstance(name, str)
        rows.append((namespace, name))
    return rows


def _pod_spec(manifest: dict[str, object]) -> dict[str, Any]:
    spec = manifest["spec"]
    assert isinstance(spec, dict)
    return spec


def test_build_seed_manifests_returns_namespaces_then_pods_in_stable_order() -> None:
    manifests = build_seed_manifests(
        run_id="aks186",
        namespace_count=2,
        pods_per_namespace=3,
        node_selector="korvid.dev/pool=perftest",
    )

    namespaces = manifests[:2]
    pods = manifests[2:]

    assert [manifest["kind"] for manifest in namespaces] == ["Namespace", "Namespace"]
    assert [_namespace_name(manifest) for manifest in namespaces] == [
        "korvid-perf-aks186-0",
        "korvid-perf-aks186-1",
    ]
    assert _pod_names(pods) == [
        ("korvid-perf-aks186-0", "bench-0"),
        ("korvid-perf-aks186-1", "bench-0"),
        ("korvid-perf-aks186-0", "bench-1"),
        ("korvid-perf-aks186-1", "bench-1"),
        ("korvid-perf-aks186-0", "bench-2"),
        ("korvid-perf-aks186-1", "bench-2"),
    ]
    assert all(
        _labels(manifest)
        == {
            "app.kubernetes.io/managed-by": "korvid-performance",
            "korvid.dev/performance-run": "aks186",
        }
        for manifest in manifests
    )
    assert _pod_spec(pods[0])["nodeSelector"] == {"korvid.dev/pool": "perftest"}
    assert _pod_spec(pods[0])["tolerations"] == [
        {
            "key": "korvid.dev/performance",
            "operator": "Equal",
            "value": "true",
            "effect": "NoSchedule",
        }
    ]
    containers = _pod_spec(pods[0])["containers"]
    assert containers == [
        {
            "name": "bench",
            "image": "registry.k8s.io/pause:3.10",
            "resources": {
                "requests": {"cpu": "5m", "memory": "16Mi"},
            },
        }
    ]


def test_build_seed_manifests_spreads_standard_live_profile_evenly() -> None:
    manifests = build_seed_manifests(
        run_id="aks186",
        namespace_count=20,
        pods_per_namespace=50,
        node_selector="korvid.dev/pool=perftest",
    )

    pods = manifests[20:]
    counts = Counter(namespace for namespace, _name in _pod_names(pods))

    assert len(manifests) == 1020
    assert len(counts) == 20
    assert set(counts.values()) == {50}
    assert _pod_names(pods[:4]) == [
        ("korvid-perf-aks186-0", "bench-0"),
        ("korvid-perf-aks186-1", "bench-0"),
        ("korvid-perf-aks186-2", "bench-0"),
        ("korvid-perf-aks186-3", "bench-0"),
    ]
    assert _pod_names(pods[-4:]) == [
        ("korvid-perf-aks186-16", "bench-49"),
        ("korvid-perf-aks186-17", "bench-49"),
        ("korvid-perf-aks186-18", "bench-49"),
        ("korvid-perf-aks186-19", "bench-49"),
    ]


@pytest.mark.parametrize(
    "run_id",
    ["", "Aks186", "-aks186", "aks186-", "aks_186", "a" * 49],
)
def test_build_seed_manifests_rejects_invalid_run_ids(run_id: str) -> None:
    with pytest.raises(
        ValueError, match="run_id must be 1-48 lowercase letters, digits, or hyphens"
    ):
        build_seed_manifests(
            run_id=run_id,
            namespace_count=1,
            pods_per_namespace=1,
            node_selector="korvid.dev/pool=perftest",
        )


@pytest.mark.parametrize("selector", ["", "pool", "=perftest", "pool=", "a=b=c", "pool = perftest"])
def test_build_seed_manifests_rejects_malformed_node_selectors(selector: str) -> None:
    with pytest.raises(
        ValueError, match="node_selector must be exactly one non-empty key=value pair"
    ):
        build_seed_manifests(
            run_id="aks186",
            namespace_count=1,
            pods_per_namespace=1,
            node_selector=selector,
        )


@pytest.mark.parametrize(
    ("namespace_count", "pods_per_namespace", "message"),
    [
        (0, 1, "namespace_count must be a positive integer"),
        (1, 0, "pods_per_namespace must be a positive integer"),
    ],
)
def test_build_seed_manifests_rejects_non_positive_counts(
    namespace_count: int,
    pods_per_namespace: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_seed_manifests(
            run_id="aks186",
            namespace_count=namespace_count,
            pods_per_namespace=pods_per_namespace,
            node_selector="korvid.dev/pool=perftest",
        )
