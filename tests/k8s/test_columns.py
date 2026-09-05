"""Custom column extraction (issue #45): label/annotation/jsonpath sources.

`tests/k8s/test_client.py` only exercises the `label` source through a real
`KubeClient` (never `annotation` or a successful `jsonpath` evaluation), and
`tests/core/test_config.py::test_views_invalid_jsonpath_drops_column_with_warning`
only checks that one malformed expression drops its column with a warning —
neither pins `parse_jsonpath`'s own `ValueError` contract or `evaluate`'s
rendering/never-raises contract. This file retains the ordered multi-column
evaluation contract, the parse-caching perf contract, and one minimal,
representative test per distinct branch of those two contracts that has no
equivalent boundary elsewhere.
"""

from __future__ import annotations

import pytest

from korvid.k8s.columns import CustomColumn, evaluate, evaluate_all, parse_jsonpath

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


@pytest.mark.parametrize(
    "expr",
    [
        "",  # blank expression
        "spec.replicas",  # missing the required leading dot
        ".spec..replicas",  # empty segment between dots
        ".spec.containers[0.image",  # unclosed/malformed index
        ".spec.containers[abc].image",  # non-numeric index
        ".spec.containers[-1].image",  # negative index
    ],
)
def test_parse_jsonpath_rejects_malformed_expressions(expr: str) -> None:
    """`parse_jsonpath` is a public function with a documented `Raises:
    ValueError` contract; nothing else in the suite calls it directly with a
    malformed expression, so its own error contract was left unverified."""
    with pytest.raises(ValueError, match="jsonpath"):
        parse_jsonpath(expr)


def test_evaluate_reads_annotation_and_flags_unknown_source() -> None:
    """`annotation` is a distinct branch from `label` (untested elsewhere),
    and an unrecognized `source` renders `<err>` rather than crashing."""
    assert evaluate(CustomColumn("OWNER", "annotation", "owner"), _MANIFEST) == "alice"
    assert evaluate(CustomColumn("X", "bogus", "team"), _MANIFEST) == "<err>"


def test_evaluate_renders_bool_and_composite_jsonpath_values() -> None:
    """`_render`'s bool and dict/list branches produce YAML/JSON-style
    strings, not Python's `str()` — a distinct rendering rule from the
    plain-scalar case already covered by `test_evaluate_all_preserves_column_order`."""
    assert evaluate(CustomColumn("PAUSED", "jsonpath", ".spec.paused"), _MANIFEST) == "false"
    assert (
        evaluate(CustomColumn("C", "jsonpath", ".spec.containers[1]"), _MANIFEST)
        == '{"name": "sidecar", "image": "envoy:1.30"}'
    )


def test_evaluate_renders_missing_values_as_none() -> None:
    assert evaluate(CustomColumn("TIER", "label", "tier"), _MANIFEST) == "<none>"
    assert evaluate(CustomColumn("X", "jsonpath", ".spec.nodeName"), _MANIFEST) == "<none>"
    assert (
        evaluate(CustomColumn("X", "jsonpath", ".spec.containers[9].image"), _MANIFEST) == "<none>"
    )


def test_evaluate_never_raises_for_invalid_jsonpath() -> None:
    """`evaluate`'s docstring promises it never raises; a malformed jsonpath
    expression must render `<err>` via the same path a bad column config
    hits, not propagate `parse_jsonpath`'s `ValueError`."""
    col = CustomColumn("X", "jsonpath", "not-a-jsonpath")
    assert evaluate(col, _MANIFEST) == "<err>"
