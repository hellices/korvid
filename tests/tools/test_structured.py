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


# --- Identity survives every rung of the ladder (round 10) ----------------


def _identity_last_crd() -> dict[str, object]:
    """A CRD whose identity keys are last in insertion order.

    Nothing requires `apiVersion` to come first — an API server returns
    what the resource declares, and a converted or patched object can
    carry extension fields ahead of its own identity.
    """
    document: dict[str, object] = {f"extensionField{index:02d}": "y" * 300 for index in range(40)}
    document["apiVersion"] = "example.com/v1"
    document["kind"] = "CompositeApp"
    document["metadata"] = {
        "labels": {f"team-{index}": "z" * 50 for index in range(30)},
        "name": "app-0",
        "namespace": "prod",
    }
    return document


@pytest.mark.parametrize("limit", [400, 800, 2_000, 6_000])
def test_a_shrunk_document_still_says_what_it_is(limit: int) -> None:
    """Reduction keeps whichever entries come first, and the ladder stops
    at the first rung that fits — so a document whose identity sorts last
    was returned as an anonymous pile of extension fields, never reaching
    the identity fallback (PR #197 review)."""
    text = dump_bounded_yaml(_identity_last_crd(), limit)
    loaded = yaml.safe_load(text)

    assert len(text) <= limit
    assert loaded["apiVersion"] == "example.com/v1"
    assert loaded["kind"] == "CompositeApp"


@pytest.mark.parametrize("limit", [400, 800, 2_000, 6_000])
def test_a_shrunk_document_still_says_which_object_it_is(limit: int) -> None:
    """`metadata` has the same problem one level down: labels ahead of
    `name` left a manifest that named its kind but not its object."""
    text = dump_bounded_yaml(_identity_last_crd(), limit)
    metadata = yaml.safe_load(text)["metadata"]

    assert metadata["name"] == "app-0"
    assert metadata["namespace"] == "prod"


def test_identity_survives_in_a_nested_object() -> None:
    """A manifest that embeds another object (a template, an owner) keeps
    the inner identity too."""
    document = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "prod"},
        "spec": {
            "template": {
                **{f"field{index:02d}": "y" * 200 for index in range(20)},
                "kind": "PodTemplate",
                "metadata": {
                    "labels": {f"l{index}": "z" * 40 for index in range(20)},
                    "name": "web-template",
                },
            }
        },
    }

    loaded = yaml.safe_load(dump_bounded_yaml(document, 1_500))

    assert loaded["kind"] == "Deployment"
    assert loaded["spec"]["template"]["kind"] == "PodTemplate"
    assert loaded["spec"]["template"]["metadata"]["name"] == "web-template"


def test_a_list_entry_keeps_the_name_that_identifies_it() -> None:
    """Container and env lists identify their entries by `name`; losing it
    turns evidence into an anonymous blob."""
    document = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "api-0"},
        "spec": {
            "containers": [
                {
                    **{f"field{index:02d}": "y" * 150 for index in range(10)},
                    "name": f"container-{index}",
                }
                for index in range(5)
            ]
        },
    }

    loaded = yaml.safe_load(dump_bounded_yaml(document, 3_200))

    assert loaded["spec"]["containers"][0]["name"] == "container-0"


def test_reduction_stays_deterministic() -> None:
    document = _identity_last_crd()

    assert dump_bounded_yaml(document, 800) == dump_bounded_yaml(dict(document), 800)
