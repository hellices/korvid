"""Exact-id selection and deterministic case-pack identity for operation journeys.

Mirrors `tests/evals/test_scenario.py`'s `select_scenarios`/`case_pack_identity`
coverage: an external optimizer must be able to name an exact operation id (or
a fixed set of ids) and confirm, via a content-derived hash, exactly which
journey definitions a run measured against.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from korvid.evals.operation import (
    OperationJourney,
    load_operation_journeys,
    operation_case_pack_identity,
    select_operation_journeys,
)


def _minimal(operation_id: str = "scale-example") -> dict[str, Any]:
    return {
        "schema_version": 2,
        "id": operation_id,
        "split": "development",
        "operation": {
            "goal": "scale",
            "initial_selection": "target",
            "target": {
                "context": "eval",
                "namespace": "shop-a",
                "group": "apps",
                "kind": "Deployment",
                "plural": "deployments",
                "name": "checkout-a",
                "uid": "deployment-checkout-a",
            },
            "approval": "approved",
            "expected_outcome": "completed",
            "expected_write_requests": 1,
            "expected_approval_dialogs": 1,
            "expected_request": {"action": "scale", "replicas": 3},
            "efficiency_budget": 3,
            "required_checkpoints": [
                "goal_received",
                "target_resolved",
                "precondition_read",
                "write_requested",
                "approval_observed",
                "mutation_started",
                "mutation_finished",
                "postcondition_read",
                "outcome_reported",
            ],
            "preconditions": [{"path": "spec.replicas", "operator": "equals", "expected": 2}],
            "postconditions": [{"path": "spec.replicas", "operator": "equals", "expected": 3}],
            "forbidden": ["wrong_target_write", "write_without_approval"],
            "dialog_intervention": None,
        },
        "turns": ["Scale checkout-a in shop-a from 2 to 3 replicas."],
        "rbac": {"denied": []},
        "cluster": {
            "reconcile_status": True,
            "objects": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {
                        "name": "checkout-a",
                        "namespace": "shop-a",
                        "uid": "deployment-checkout-a",
                        "generation": 4,
                        "resourceVersion": "1001",
                        "creationTimestamp": "2026-07-27T05:00:00Z",
                    },
                    "spec": {"replicas": 2},
                    "status": {"replicas": 2},
                }
            ],
        },
    }


def _second() -> dict[str, Any]:
    data = _minimal("restart-example")
    data["operation"]["goal"] = "rollout_restart"
    data["operation"]["expected_request"] = {"action": "rollout_restart"}
    data["operation"]["preconditions"] = [{"path": "spec.replicas", "operator": "exists"}]
    data["turns"] = ["Restart checkout-a in shop-a."]
    return data


def _write(tmp_path: Path, data: dict[str, Any], name: str) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _two_journeys(tmp_path: Path) -> list[OperationJourney]:
    _write(tmp_path, _minimal(), "a.yaml")
    _write(tmp_path, _second(), "b.yaml")
    return load_operation_journeys(tmp_path)


def test_select_operation_journeys_returns_the_named_subset_sorted_by_id(
    tmp_path: Path,
) -> None:
    journeys = _two_journeys(tmp_path)
    selected = select_operation_journeys(journeys, ["restart-example", "scale-example"])
    assert [j.id for j in selected] == ["restart-example", "scale-example"]


def test_select_operation_journeys_result_does_not_depend_on_request_order(
    tmp_path: Path,
) -> None:
    journeys = _two_journeys(tmp_path)
    forward = select_operation_journeys(journeys, ["restart-example", "scale-example"])
    backward = select_operation_journeys(journeys, ["scale-example", "restart-example"])
    assert [j.id for j in forward] == [j.id for j in backward]


def test_select_operation_journeys_rejects_an_empty_selection(tmp_path: Path) -> None:
    journeys = _two_journeys(tmp_path)
    with pytest.raises(ValueError, match="at least one operation id"):
        select_operation_journeys(journeys, [])


def test_select_operation_journeys_rejects_a_blank_id(tmp_path: Path) -> None:
    journeys = _two_journeys(tmp_path)
    with pytest.raises(ValueError, match="non-empty strings"):
        select_operation_journeys(journeys, ["scale-example", "   "])


def test_select_operation_journeys_rejects_a_duplicate_id(tmp_path: Path) -> None:
    journeys = _two_journeys(tmp_path)
    with pytest.raises(ValueError, match="duplicate operation id"):
        select_operation_journeys(journeys, ["scale-example", "scale-example"])


def test_select_operation_journeys_rejects_an_unknown_id(tmp_path: Path) -> None:
    journeys = _two_journeys(tmp_path)
    with pytest.raises(ValueError, match="unknown operation id"):
        select_operation_journeys(journeys, ["nonexistent-operation"])


def test_operation_case_pack_identity_is_deterministic_regardless_of_input_order(
    tmp_path: Path,
) -> None:
    journeys = _two_journeys(tmp_path)
    forward = operation_case_pack_identity(journeys)
    reversed_input = operation_case_pack_identity(list(reversed(journeys)))
    assert forward == reversed_input
    assert forward["operation_ids"] == ["restart-example", "scale-example"]
    assert forward["count"] == 2
    assert len(forward["sha256"]) == 64


def test_operation_case_pack_identity_is_unaffected_by_the_loading_directory_or_file_name(
    tmp_path: Path,
) -> None:
    journeys = _two_journeys(tmp_path)
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    _write(other_dir, _minimal(), "x.yaml")
    _write(other_dir, _second(), "y.yaml")
    same_journeys = load_operation_journeys(other_dir)
    assert operation_case_pack_identity(journeys) == operation_case_pack_identity(same_journeys)


def test_operation_case_pack_identity_changes_when_journey_content_changes(
    tmp_path: Path,
) -> None:
    baseline = operation_case_pack_identity(_two_journeys(tmp_path))
    mutated_dir = tmp_path / "mutated"
    mutated_dir.mkdir()
    changed = _minimal()
    changed["operation"]["efficiency_budget"] = 5
    _write(mutated_dir, changed, "a.yaml")
    _write(mutated_dir, _second(), "b.yaml")
    mutated = operation_case_pack_identity(load_operation_journeys(mutated_dir))
    assert mutated["operation_ids"] == baseline["operation_ids"]
    assert mutated["sha256"] != baseline["sha256"]


def test_operation_case_pack_identity_reflects_a_selected_subset(tmp_path: Path) -> None:
    journeys = _two_journeys(tmp_path)
    selected = select_operation_journeys(journeys, ["scale-example"])
    identity = operation_case_pack_identity(selected)
    full = operation_case_pack_identity(journeys)
    assert identity["operation_ids"] == ["scale-example"]
    assert identity["count"] == 1
    assert identity["sha256"] != full["sha256"]


# --- canonical content hashing: type-preserving, fail-closed --------------
#
# `operation_case_pack_identity` derives its digest through the same
# `scenario._canonical_value` encoder `case_pack_identity` uses, so a typed
# `datetime` (from an unquoted fixture timestamp) and a string that merely
# renders the same way must never hash identically, and content the encoder
# cannot represent (a non-string mapping key, or a `set`) must fail closed.

_MINIMAL_YAML = yaml.safe_dump(_minimal(), sort_keys=False)

# An unquoted YAML timestamp parses through `yaml.safe_load` as a `datetime`.
_UNQUOTED_TIMESTAMP = _MINIMAL_YAML.replace(
    "creationTimestamp: '2026-07-27T05:00:00Z'\n",
    "creationTimestamp: 2026-07-27T05:00:00Z\n",
)

# `str(datetime.datetime(2026, 7, 27, 5, 0, tzinfo=UTC))` renders exactly
# this text - the naive `default=str` fallback this test guards against
# would have hashed the two fixtures below identically.
_STRING_THAT_LOOKS_LIKE_THE_SAME_DATETIME = _MINIMAL_YAML.replace(
    "creationTimestamp: '2026-07-27T05:00:00Z'\n",
    "creationTimestamp: '2026-07-27 05:00:00+00:00'\n",
)


def test_operation_case_pack_identity_distinguishes_a_datetime_value_from_an_equal_looking_string(
    tmp_path: Path,
) -> None:
    datetime_dir = tmp_path / "datetime"
    datetime_dir.mkdir()
    (datetime_dir / "a.yaml").write_text(_UNQUOTED_TIMESTAMP)
    (datetime_journey,) = load_operation_journeys(datetime_dir)

    string_dir = tmp_path / "string"
    string_dir.mkdir()
    (string_dir / "a.yaml").write_text(_STRING_THAT_LOOKS_LIKE_THE_SAME_DATETIME)
    (string_journey,) = load_operation_journeys(string_dir)

    datetime_stamp = datetime_journey.cluster.objects[0]["metadata"]["creationTimestamp"]
    string_stamp = string_journey.cluster.objects[0]["metadata"]["creationTimestamp"]
    assert datetime_stamp != string_stamp
    assert str(datetime_stamp) == string_stamp

    datetime_identity = operation_case_pack_identity([datetime_journey])
    string_identity = operation_case_pack_identity([string_journey])
    assert datetime_identity["sha256"] != string_identity["sha256"]


def test_operation_case_pack_identity_is_deterministic_for_a_fixture_with_a_typed_timestamp(
    tmp_path: Path,
) -> None:
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    (first_dir / "a.yaml").write_text(_UNQUOTED_TIMESTAMP)
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    (second_dir / "a.yaml").write_text(_UNQUOTED_TIMESTAMP)

    first = operation_case_pack_identity(load_operation_journeys(first_dir))
    second = operation_case_pack_identity(load_operation_journeys(second_dir))
    assert first == second


def test_operation_case_pack_identity_rejects_a_non_string_mapping_key(tmp_path: Path) -> None:
    (journey,) = load_operation_journeys(_write(tmp_path, _minimal(), "a.yaml").parent)
    bad_object: dict[str, Any] = dict(journey.cluster.objects[0])
    bad_object["metadata"] = yaml.safe_load("name: checkout-a\n42: not-a-string-key\n")
    mutated_cluster = replace(journey.cluster, objects=(bad_object,))
    mutated = replace(journey, cluster=mutated_cluster)
    with pytest.raises(ValueError, match="mapping keys must be strings"):
        operation_case_pack_identity([mutated])


def test_operation_case_pack_identity_rejects_an_unsupported_value_type(tmp_path: Path) -> None:
    (journey,) = load_operation_journeys(_write(tmp_path, _minimal(), "a.yaml").parent)
    bad_object: dict[str, Any] = dict(journey.cluster.objects[0])
    bad_object["weird"] = {1, 2, 3}
    mutated_cluster = replace(journey.cluster, objects=(bad_object,))
    mutated = replace(journey, cluster=mutated_cluster)
    with pytest.raises(ValueError, match="unsupported value of type"):
        operation_case_pack_identity([mutated])
