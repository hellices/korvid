"""Custom column extraction (issue #45): label/annotation/jsonpath sources."""

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


class TestParseJsonpath:
    def test_dotted_path(self) -> None:
        assert parse_jsonpath(".spec.replicas") == ("spec", "replicas")

    def test_index_segments(self) -> None:
        assert parse_jsonpath(".spec.containers[0].image") == ("spec", "containers", 0, "image")

    def test_leading_dot_required(self) -> None:
        with pytest.raises(ValueError, match="must start with"):
            parse_jsonpath("spec.replicas")

    def test_empty_segment_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_jsonpath(".spec..replicas")

    def test_unclosed_index_rejected(self) -> None:
        with pytest.raises(ValueError, match="index"):
            parse_jsonpath(".spec.containers[0.image")

    def test_non_numeric_index_rejected(self) -> None:
        with pytest.raises(ValueError, match="index"):
            parse_jsonpath(".spec.containers[abc].image")

    def test_blank_expression_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_jsonpath("")


class TestEvaluate:
    def test_label_present(self) -> None:
        col = CustomColumn("TEAM", "label", "team")
        assert evaluate(col, _MANIFEST) == "payments"

    def test_label_missing_renders_none(self) -> None:
        col = CustomColumn("TIER", "label", "tier")
        assert evaluate(col, _MANIFEST) == "<none>"

    def test_annotation_present(self) -> None:
        col = CustomColumn("OWNER", "annotation", "owner")
        assert evaluate(col, _MANIFEST) == "alice"

    def test_annotation_missing_renders_none(self) -> None:
        col = CustomColumn("X", "annotation", "does-not-exist")
        assert evaluate(col, _MANIFEST) == "<none>"

    def test_jsonpath_scalar(self) -> None:
        col = CustomColumn("IMAGE", "jsonpath", ".spec.containers[0].image")
        assert evaluate(col, _MANIFEST) == "ghcr.io/acme/api:1.2.3"

    def test_jsonpath_integer(self) -> None:
        col = CustomColumn("REPLICAS", "jsonpath", ".spec.replicas")
        assert evaluate(col, _MANIFEST) == "3"

    def test_jsonpath_bool_lowercase(self) -> None:
        """Booleans render as YAML/JSON style true/false, not Python True."""
        col = CustomColumn("PAUSED", "jsonpath", ".spec.paused")
        assert evaluate(col, _MANIFEST) == "false"

    def test_jsonpath_missing_key_renders_none(self) -> None:
        col = CustomColumn("X", "jsonpath", ".spec.nodeName")
        assert evaluate(col, _MANIFEST) == "<none>"

    def test_jsonpath_index_out_of_range_renders_none(self) -> None:
        col = CustomColumn("X", "jsonpath", ".spec.containers[9].image")
        assert evaluate(col, _MANIFEST) == "<none>"

    def test_jsonpath_traversing_scalar_renders_none(self) -> None:
        """Indexing into a scalar is a missing value, not a crash."""
        col = CustomColumn("X", "jsonpath", ".spec.replicas.count")
        assert evaluate(col, _MANIFEST) == "<none>"

    def test_jsonpath_null_value_renders_none(self) -> None:
        col = CustomColumn("X", "jsonpath", ".spec.suspend")
        manifest = {"spec": {"suspend": None}}
        assert evaluate(col, manifest) == "<none>"

    def test_jsonpath_composite_renders_compact_json(self) -> None:
        col = CustomColumn("C", "jsonpath", ".spec.containers[1]")
        assert evaluate(col, _MANIFEST) == '{"name": "sidecar", "image": "envoy:1.30"}'

    def test_unknown_source_renders_err(self) -> None:
        col = CustomColumn("X", "bogus", "team")
        assert evaluate(col, _MANIFEST) == "<err>"


def test_evaluate_all_preserves_column_order() -> None:
    cols = (
        CustomColumn("TEAM", "label", "team"),
        CustomColumn("IMAGE", "jsonpath", ".spec.containers[0].image"),
    )
    assert evaluate_all(cols, _MANIFEST) == ("payments", "ghcr.io/acme/api:1.2.3")
