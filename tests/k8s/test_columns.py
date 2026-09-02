"""Custom column extraction (issue #45): label/annotation/jsonpath sources.

Individual label/annotation/jsonpath parsing permutations are exercised at
the stronger Kubernetes-translation boundary in `tests/k8s/test_client.py`
(label + jsonpath evaluation through `KubeClient`) and at the config-load
boundary in `tests/core/test_config.py::test_views_invalid_jsonpath_drops_column_with_warning`
(invalid jsonpath syntax rejection). This file retains only the ordered
multi-column evaluation contract and the parse-caching perf contract, which
have no equivalent elsewhere.
"""

from __future__ import annotations

from korvid.k8s.columns import CustomColumn, evaluate_all, parse_jsonpath

_MANIFEST = {
    "metadata": {
        "name": "api-1",
        "labels": {"team": "payments", "app": "api"},
        "annotations": {"owner": "alice"},
    },
    "spec": {
        "containers": [
            {"name": "app", "image": "ghcr.io/acme/api:1.2.3"},
            {"name": "sidecar", "image": "envoy:1.30"},
        ],
        "replicas": 3,
        "paused": False,
    },
}


def test_evaluate_all_preserves_column_order() -> None:
    cols = (
        CustomColumn("TEAM", "label", "team"),
        CustomColumn("IMAGE", "jsonpath", ".spec.containers[0].image"),
    )
    assert evaluate_all(cols, _MANIFEST) == ("payments", "ghcr.io/acme/api:1.2.3")


def test_parse_jsonpath_caches_compiled_segments() -> None:
    """Hot path: watch fan-out evaluates per event — parsing must not repeat."""
    assert parse_jsonpath(".spec.containers[0].image") is parse_jsonpath(
        ".spec.containers[0].image"
    )
