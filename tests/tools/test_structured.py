"""Tests for structure-preserving size bounds on YAML tool results."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from korvid.tools.structured import ELISION, ERROR_PREFIX, dump_bounded_yaml, dump_yaml


def test_small_document_is_returned_unmodified() -> None:
    document = {"kind": "Pod", "metadata": {"name": "api-0"}}
    assert dump_bounded_yaml(document, 4_000) == dump_yaml(document)


@pytest.mark.parametrize("limit", [200, 1_000, 3_000, 8_000])
def test_bounded_output_is_always_parseable_yaml_within_the_limit(limit: int) -> None:
    document: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "api-0",
            "namespace": "prod",
            "annotations": {f"note-{index}": "y" * 500 for index in range(200)},
        },
        "spec": {"containers": [{"name": f"c{index}", "args": ["z" * 300]} for index in range(50)]},
    }
    text = dump_bounded_yaml(document, limit)
    assert len(text) <= limit
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    assert loaded["kind"] == "Pod"
    assert loaded["metadata"]["name"] == "api-0"


def test_deeply_nested_document_is_elided_not_cut() -> None:
    document: Any = {"leaf": "value"}
    for _ in range(60):
        document = {"nested": document}
    document = {"kind": "Deep", "spec": document}
    text = dump_bounded_yaml(document, 400)
    assert len(text) <= 400
    assert yaml.safe_load(text)["kind"] == "Deep"
    assert ELISION in text


def test_impossible_budget_degrades_to_a_parseable_identity_notice() -> None:
    document = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "api-0", "namespace": "prod"},
        "spec": {"bulk": "x" * 50_000},
    }
    text = dump_bounded_yaml(document, 10)
    loaded = yaml.safe_load(text)
    assert loaded["kind"] == "Pod"
    assert loaded["metadata"] == {"name": "api-0", "namespace": "prod"}
    assert loaded["truncated"] == ELISION
    assert "x" * 100 not in text


def test_top_level_list_stays_a_list() -> None:
    document = [{"name": f"item-{index}", "blob": "x" * 400} for index in range(100)]
    text = dump_bounded_yaml(document, 1_000)
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, list)
    assert loaded[0]["name"] == "item-0"
    assert len(text) <= 1_000


def test_a_document_never_serializes_into_the_error_marker() -> None:
    """The boundary tells an executor error from a document by its `ERROR:`
    prefix; a sorted-key dump must not be able to produce that prefix."""
    document = {"ERROR": "a CRD status field", "kind": "Widget"}

    text = dump_yaml(document)

    assert not text.startswith(ERROR_PREFIX)
    assert yaml.safe_load(text) == document
    assert yaml.safe_load(dump_bounded_yaml(document, 40)) is not None
