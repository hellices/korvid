"""Tests for projecting ResourceQuotas and LimitRanges (#411)."""

from __future__ import annotations

from typing import Any

from korvid.core.creation_policies import (
    LimitValue,
    QuotaEntry,
    ScopeExpression,
    project_limit_ranges,
    project_quotas,
)
from korvid.core.creation_quantities import QuotaGroup, Usage


def _quota(
    name: str,
    hard: dict[str, Any],
    *,
    status: dict[str, Any] | None = None,
    **spec: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "metadata": {"name": name, "namespace": "shop", "uid": f"uid-{name}"},
        "spec": {"hard": hard, **spec},
    }
    if status is not None:
        item["status"] = status
    return item


def test_a_quota_keeps_its_values_verbatim_grouped_and_compared() -> None:
    quota = _quota(
        "compute",
        {"requests.cpu": "2", "limits.memory": "8Gi", "pods": "20", "requests.storage": "1Ti"},
        status={
            "hard": {
                "requests.cpu": "2000m",
                "limits.memory": "8Gi",
                "pods": "20",
                "requests.storage": "1Ti",
            },
            "used": {
                "requests.cpu": "2",
                "limits.memory": "3Gi",
                "pods": "10",
                "requests.storage": "0",
            },
        },
    )

    (projected,) = project_quotas([quota])

    assert (projected.name, projected.uid, projected.stale) == ("compute", "uid-compute", False)
    assert projected.entries == (
        QuotaEntry("limits.memory", QuotaGroup.COMPUTE, "8Gi", "3Gi", Usage.BELOW),
        QuotaEntry("requests.cpu", QuotaGroup.COMPUTE, "2", "2", Usage.AT_LIMIT),
        QuotaEntry("requests.storage", QuotaGroup.STORAGE, "1Ti", "0", Usage.BELOW),
        QuotaEntry("pods", QuotaGroup.COUNT, "20", "10", Usage.BELOW),
    )


def test_scopes_and_scope_selectors_are_kept() -> None:
    quota = _quota(
        "gold",
        {"pods": "5"},
        status={"hard": {"pods": "5"}, "used": {"pods": "1"}},
        scopes=["NotTerminating", "BestEffort"],
        scopeSelector={
            "matchExpressions": [
                {"scopeName": "PriorityClass", "operator": "In", "values": ["gold", "silver"]},
                {"scopeName": "PriorityClass", "operator": "Exists"},
            ]
        },
    )

    (projected,) = project_quotas([quota])

    assert projected.scopes == ("NotTerminating", "BestEffort")
    assert projected.scope_selector == (
        ScopeExpression("PriorityClass", "In", ("gold", "silver")),
        ScopeExpression("PriorityClass", "Exists", ()),
    )


def test_a_quota_without_status_is_stale_and_nothing_is_at_its_limit() -> None:
    (projected,) = project_quotas([_quota("new", {"pods": "0"})])

    assert projected.stale
    assert projected.entries == (
        QuotaEntry("pods", QuotaGroup.COUNT, "0", None, Usage.NOT_ESTABLISHED),
    )


def test_a_status_hard_that_lags_spec_hard_is_stale() -> None:
    quota = _quota(
        "compute",
        {"pods": "10", "requests.cpu": "4"},
        status={
            "hard": {"pods": "10", "requests.cpu": "2"},
            "used": {"pods": "10", "requests.cpu": "1"},
        },
    )

    (projected,) = project_quotas([quota])

    assert projected.stale
    assert {entry.usage for entry in projected.entries} == {Usage.NOT_ESTABLISHED}


def test_a_status_hard_missing_a_resource_is_stale() -> None:
    quota = _quota(
        "compute",
        {"pods": "10", "services": "2"},
        status={"hard": {"pods": "10"}, "used": {"pods": "1"}},
    )

    assert project_quotas([quota])[0].stale


def test_matching_hard_maps_with_a_missing_used_entry_are_stale() -> None:
    quota = _quota(
        "compute",
        {"pods": "10", "services": "2"},
        status={"hard": {"pods": "10", "services": "2"}, "used": {"pods": "1"}},
    )

    assert project_quotas([quota])[0].stale


def test_an_unparsable_hard_value_is_kept_and_its_usage_not_established() -> None:
    quota = _quota(
        "odd",
        {"pods": "many"},
        status={"hard": {"pods": "many"}, "used": {"pods": "1"}},
    )

    (projected,) = project_quotas([quota])

    assert projected.entries == (
        QuotaEntry("pods", QuotaGroup.COUNT, "many", "1", Usage.NOT_ESTABLISHED),
    )


def test_malformed_quotas_and_entries_are_skipped() -> None:
    items: list[Any] = [
        "not-a-mapping",
        {"metadata": {"namespace": "shop"}, "spec": {"hard": {"pods": "1"}}},
        _quota(
            "ok",
            {"pods": "1", "services": None, "secrets": True},
            status={"hard": {"pods": "1"}, "used": {"pods": "0"}},
        ),
    ]

    (projected,) = project_quotas(items)

    assert [entry.resource for entry in projected.entries] == ["pods"]


def test_quotas_are_ordered_by_name() -> None:
    names = [q.name for q in project_quotas([_quota("b", {}), _quota("a", {})])]

    assert names == ["a", "b"]


def test_a_limit_range_keeps_each_constraint_verbatim() -> None:
    item = {
        "metadata": {"name": "defaults", "namespace": "shop", "uid": "uid-defaults"},
        "spec": {
            "limits": [
                {
                    "type": "Container",
                    "default": {"cpu": "500m", "memory": "512Mi"},
                    "defaultRequest": {"cpu": "100m"},
                    "max": {"cpu": "1", "memory": "1Gi"},
                    "min": {"memory": "64Mi"},
                    "maxLimitRequestRatio": {"cpu": "4"},
                },
                {"type": "PersistentVolumeClaim", "max": {"storage": "10Gi"}},
            ]
        },
    }

    (projected,) = project_limit_ranges([item])

    assert (projected.name, projected.uid) == ("defaults", "uid-defaults")
    assert projected.values == (
        LimitValue(
            "Container",
            "cpu",
            max="1",
            default="500m",
            default_request="100m",
            max_limit_request_ratio="4",
        ),
        LimitValue("Container", "memory", max="1Gi", min="64Mi", default="512Mi"),
        LimitValue("PersistentVolumeClaim", "storage", max="10Gi"),
    )


def test_a_limit_range_without_limits_has_no_values() -> None:
    item = {"metadata": {"name": "empty", "namespace": "shop", "uid": "u"}, "spec": {"limits": "x"}}

    assert project_limit_ranges([item])[0].values == ()


def test_a_whole_number_written_as_a_number_is_kept_as_text() -> None:
    quota = _quota("counts", {"pods": 10}, status={"hard": {"pods": 10}, "used": {"pods": 4}})

    (projected,) = project_quotas([quota])

    assert projected.entries == (QuotaEntry("pods", QuotaGroup.COUNT, "10", "4", Usage.BELOW),)


def test_a_scope_expression_without_a_name_or_operator_is_skipped() -> None:
    quota = _quota(
        "gold",
        {"pods": "5"},
        scopeSelector={
            "matchExpressions": [
                {"scopeName": "PriorityClass"},
                {"operator": "Exists"},
                {"scopeName": "PriorityClass", "operator": "NotIn", "values": ["low", 3]},
            ]
        },
    )

    assert project_quotas([quota])[0].scope_selector == (
        ScopeExpression("PriorityClass", "NotIn", ("low",)),
    )
