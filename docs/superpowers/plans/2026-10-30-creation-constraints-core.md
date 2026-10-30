# Workload Creation Constraints, Core (#411 Q1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read a workload's recorded creation failures next to its namespace's ResourceQuotas and LimitRanges, in pure Python under `core/` (#523, the core half of slice Q1 of #411).

**Architecture:** Four projection modules turn whole API objects into what the view may keep:
- quantities: grouping and exact comparison;
- quotas and LimitRanges;
- failures, from Events and conditions, with the message classifier;
- workloads and their children.

`core/creation_constraints.py` resolves and reads every source through the #409 readers, under one run token, and returns a `CreationReport` with a baseline. No UI changes; the view is Q1 view.

**Tech Stack:** Python 3.11+, pytest with `asyncio_mode = "auto"`, mypy `--strict`, ruff, tach.

## Global Constraints

- **Design:** `docs/superpowers/specs/2026-10-30-creation-constraints-design.md`, sections "Reads", "Projections", "Classifying a recorded rejection", "Quantities" and "Evidence".
- **No literal leaves the run:** templates are projected before the report is built; a `repr` test checks for literal environment values, commands, images, annotations and volume sources.
- **Exact quantities:** shown verbatim; compared only as `Decimal` through `parse_quantity`; an unparsable value is not established.
- **UID matching:** a failure is about a subject only when its UID matches; a kind-and-name match with another, nonempty UID is counted as an earlier incarnation, and one without a UID is ignored.
- **`ReplicaFailure`:** only `status: "True"` with `reason: FailedCreate`.
- **Stale quota status:** missing status, `status.hard` differing from `spec.hard`, or a `status.used` missing a `status.hard` key; downgrade only an `AVAILABLE` list.
- **Code rules:** no `assert` in `src/`; every `pytest.raises` takes `match=`; maximum complexity 10; Google-style docstrings.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the design

- **A denied workload read still matches Events.** The design does not say whom failures are about when the workload cannot be read. The run uses the selected row's UID then, so a user without `get` on the workload still sees its recorded failures. It does not do so for an absent workload: the view will say it no longer exists.
- **Failure order is by time, not by source.** Conditions and Events are merged newest first, undated last.
- **A policy-authored denial is never classified further.** An admission webhook's or a ValidatingAdmissionPolicy's text is its author's own and can contain a quota or LimitRanger phrase. A message containing `admission webhook "…" denied the request` or `ValidatingAdmissionPolicy '…' with binding '…' denied request` is `OTHER`, and names no quota.
- **An event without a UID is ignored.** It neither matches the workload nor shows an earlier incarnation; only a nonempty, different UID counts as earlier.
- **Whatever the projection cannot read makes its list inconclusive.** An `AVAILABLE` list becomes `PARTIAL`/`MALFORMED` when its projection drops or cannot fully read an item:
  - a quota or LimitRange without a name;
  - a quota without a readable `spec`, a constraint that is not a quantity, a scope that is not text, a `scopeSelector` that is not a map, or a scope expression without a name, an operator or text values;
  - a LimitRange without a readable `spec.limits` (it is required), or a limit that is not a map of quantities with a text `type`;
  - a child the workload controls that has no name or UID.

  A list that is already partial keeps its own basis.
- **An empty quota with no status is stale**, like any quota with no status.
- **An empty selected UID is none.** A summary without a UID carries `""`, so `CreationTarget` normalises it to `None`, and the workload is read without an expected incarnation.
- **A quantity decimal arithmetic cannot represent** (`decimal.InvalidOperation`, for example a huge exponent) is not established, like an unparsable one.
- **The ratio phrase is anchored at a word's start**, so a long unbroken message is classified in linear time.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/k8s/events.py` | modify | `deprecatedLastTimestamp` and `deprecatedFirstTimestamp` in the time precedence |
| `src/korvid/core/creation_quantities.py` | create | `QuotaGroup`, `Usage`, `canonical_resource`, `quota_group`, `usage`, `same_quantity` |
| `src/korvid/core/creation_policies.py` | create | `QuotaProjection`, `LimitRangeProjection`, `project_quotas`, `project_limit_ranges` |
| `src/korvid/core/creation_failures.py` | create | `classify`, `event_failures`, `condition_failure` |
| `src/korvid/core/creation_workloads.py` | create | `WORKLOAD_KINDS`, `project_workload`, `project_children` |
| `src/korvid/core/creation_constraints.py` | create | `CreationTarget`, `declarations`, `CreationReport`, `explain_creation` |
| `tests/k8s/test_events.py` | modify | the deprecated timestamps |
| `tests/core/test_creation_*.py` | create | one test module per source module |
| `tests/test_optional_extras.py` | modify | the new modules import without extras |

---

### Task 1: Date legacy events

**Files:** Modify `src/korvid/k8s/events.py`, `tests/k8s/test_events.py`.

- [ ] **Step 1:** Add tests for these cases: a legacy event dated by `deprecatedLastTimestamp`; `lastTimestamp` beating its deprecated form; `deprecatedLastTimestamp` beating `eventTime`; `deprecatedFirstTimestamp` beating `creationTimestamp`.
- [ ] **Step 2:** Run `uv run --frozen pytest -p no:tach tests/k8s/test_events.py -q`. Expected: 3 FAIL.
- [ ] **Step 3:** Insert `event.get("deprecatedLastTimestamp")` after `lastTimestamp` and `event.get("deprecatedFirstTimestamp")` after `firstTimestamp` in `select_event_timestamp`, and update the docstrings.
- [ ] **Step 4:** Run the tests again. Expected: PASS.
- [ ] **Step 5:** Commit: `Date legacy events by their deprecated timestamps`.

### Task 2: Quantities

Create `tests/core/test_creation_quantities.py` with:

```python
"""Tests for grouping and comparing ResourceQuota quantities (#411)."""

from __future__ import annotations

import pytest

from korvid.core.creation_quantities import (
    QuotaGroup,
    Usage,
    canonical_resource,
    quota_group,
    same_quantity,
    usage,
)


@pytest.mark.parametrize(
    ("name", "canonical"),
    [
        ("cpu", "requests.cpu"),
        ("memory", "requests.memory"),
        ("ephemeral-storage", "requests.ephemeral-storage"),
        ("requests.cpu", "requests.cpu"),
        ("limits.memory", "limits.memory"),
        ("pods", "pods"),
    ],
)
def test_short_compute_names_mean_their_requests(name: str, canonical: str) -> None:
    assert canonical_resource(name) == canonical


@pytest.mark.parametrize(
    ("name", "group"),
    [
        ("requests.cpu", QuotaGroup.COMPUTE),
        ("cpu", QuotaGroup.COMPUTE),
        ("limits.cpu", QuotaGroup.COMPUTE),
        ("memory", QuotaGroup.COMPUTE),
        ("limits.memory", QuotaGroup.COMPUTE),
        ("requests.hugepages-2Mi", QuotaGroup.COMPUTE),
        ("hugepages-1Gi", QuotaGroup.COMPUTE),
        ("requests.storage", QuotaGroup.STORAGE),
        ("gold.storageclass.storage.k8s.io/requests.storage", QuotaGroup.STORAGE),
        ("ephemeral-storage", QuotaGroup.STORAGE),
        ("limits.ephemeral-storage", QuotaGroup.STORAGE),
        ("pods", QuotaGroup.COUNT),
        ("count/pods", QuotaGroup.COUNT),
        ("count/deployments.apps", QuotaGroup.COUNT),
        ("persistentvolumeclaims", QuotaGroup.COUNT),
        ("gold.storageclass.storage.k8s.io/persistentvolumeclaims", QuotaGroup.COUNT),
        ("services.loadbalancers", QuotaGroup.COUNT),
        ("services.nodeports", QuotaGroup.COUNT),
        ("configmaps", QuotaGroup.COUNT),
        ("secrets", QuotaGroup.COUNT),
        ("replicationcontrollers", QuotaGroup.COUNT),
        ("resourcequotas", QuotaGroup.COUNT),
        ("services", QuotaGroup.COUNT),
        ("requests.nvidia.com/gpu", QuotaGroup.OTHER),
        ("something-new", QuotaGroup.OTHER),
    ],
)
def test_each_quota_resource_has_one_group(name: str, group: QuotaGroup) -> None:
    assert quota_group(name) is group


@pytest.mark.parametrize(
    ("used", "hard", "expected"),
    [
        ("2", "2", Usage.AT_LIMIT),
        ("2000m", "2", Usage.AT_LIMIT),
        ("3", "2", Usage.AT_LIMIT),
        ("1999m", "2", Usage.BELOW),
        ("0.5", "500m", Usage.AT_LIMIT),
        ("1Gi", "1073741824", Usage.AT_LIMIT),
        ("1G", "1Gi", Usage.BELOW),
        ("12e3", "12k", Usage.AT_LIMIT),
        ("9", "10", Usage.BELOW),
        ("lots", "10", Usage.NOT_ESTABLISHED),
        ("1", "", Usage.NOT_ESTABLISHED),
        (None, "10", Usage.NOT_ESTABLISHED),
    ],
)
def test_usage_is_compared_as_exact_quantities(
    used: str | None, hard: str, expected: Usage
) -> None:
    assert usage(used, hard) is expected


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("1Gi", "1024Mi", True),
        ("1", "1000m", True),
        ("1G", "1Gi", False),
        ("10", "nonsense", None),
    ],
)
def test_two_quantities_are_the_same_only_by_value(
    left: str, right: str, expected: bool | None
) -> None:
    assert same_quantity(left, right) is expected


def test_a_quantity_too_large_for_decimal_arithmetic_is_not_established() -> None:
    huge = "1e999999999999999999999999"

    assert usage(huge, "10") is Usage.NOT_ESTABLISHED
    assert same_quantity(huge, "10") is None
```

Create `src/korvid/core/creation_quantities.py` with:

```python
"""Group and compare ResourceQuota quantities exactly (#411).

Values are shown as the cluster wrote them; only comparisons parse them,
into exact decimals with `parse_quantity`, never as strings or floats. A
value that does not parse makes its comparison not established.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum, StrEnum

from korvid.k8s.models import parse_quantity

#: Short quota names and the requests they mean.
_SHORT_NAMES = {
    "cpu": "requests.cpu",
    "memory": "requests.memory",
    "ephemeral-storage": "requests.ephemeral-storage",
}
_COMPUTE = frozenset({"requests.cpu", "limits.cpu", "requests.memory", "limits.memory"})
_STORAGE = frozenset({"requests.storage", "requests.ephemeral-storage", "limits.ephemeral-storage"})
_COUNTS = frozenset(
    {
        "pods",
        "persistentvolumeclaims",
        "services",
        "services.loadbalancers",
        "services.nodeports",
        "configmaps",
        "secrets",
        "replicationcontrollers",
        "resourcequotas",
    }
)
_STORAGE_CLASS = ".storageclass.storage.k8s.io/"


class QuotaGroup(StrEnum):
    """What a quota resource limits, in display order."""

    COMPUTE = "compute"
    STORAGE = "storage"
    COUNT = "object counts"
    OTHER = "other"


class Usage(Enum):
    """How a resource's used amount stands against its hard limit."""

    AT_LIMIT = "at-limit"
    BELOW = "below"
    NOT_ESTABLISHED = "not-established"


def canonical_resource(name: str) -> str:
    """`name` with a short compute name replaced by the requests it means."""
    return _SHORT_NAMES.get(name, name)


def _is_hugepages(name: str) -> bool:
    return name.removeprefix("requests.").removeprefix("limits.").startswith("hugepages-")


def quota_group(name: str) -> QuotaGroup:
    """The group a quota resource is shown under."""
    canonical = canonical_resource(name)
    if canonical in _COMPUTE or _is_hugepages(canonical):
        return QuotaGroup.COMPUTE
    if canonical in _STORAGE or canonical.endswith(f"{_STORAGE_CLASS}requests.storage"):
        return QuotaGroup.STORAGE
    if (
        canonical in _COUNTS
        or canonical.startswith("count/")
        or canonical.endswith(f"{_STORAGE_CLASS}persistentvolumeclaims")
    ):
        return QuotaGroup.COUNT
    return QuotaGroup.OTHER


def _parsed(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return parse_quantity(value)
    except (ValueError, ArithmeticError):  # decimal.InvalidOperation for an absurd exponent
        return None


def usage(used: str | None, hard: str) -> Usage:
    """Whether `used` has reached `hard`, compared as exact quantities."""
    used_value, hard_value = _parsed(used), _parsed(hard)
    if used_value is None or hard_value is None:
        return Usage.NOT_ESTABLISHED
    return Usage.AT_LIMIT if used_value >= hard_value else Usage.BELOW


def same_quantity(left: str, right: str) -> bool | None:
    """Whether two quantities are equal in value, or `None` if either does not parse."""
    left_value, right_value = _parsed(left), _parsed(right)
    if left_value is None or right_value is None:
        return None
    return left_value == right_value
```

- [ ] Run `uv run --frozen pytest -p no:tach tests/core/test_creation_quantities.py -q` before (collection error) and after (PASS), then commit: `Group and compare ResourceQuota quantities exactly`.

### Task 3: Quotas and LimitRanges

Create `tests/core/test_creation_policies.py` with:

```python
"""Tests for projecting ResourceQuotas and LimitRanges (#411)."""

from __future__ import annotations

from typing import Any

import pytest

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


def test_an_unreadable_scope_expression_is_dropped_and_the_quota_incomplete() -> None:
    quota = _quota(
        "gold",
        {"pods": "5"},
        scopeSelector={
            "matchExpressions": [
                {"scopeName": "PriorityClass"},
                {"operator": "Exists"},
                {"scopeName": "PriorityClass", "operator": "NotIn", "values": ["low", 3]},
                {"scopeName": "PriorityClass", "operator": "In", "values": "gold"},
                {"scopeName": "PriorityClass", "operator": "Exists"},
            ]
        },
    )

    (projected,) = project_quotas([quota])

    assert projected.scope_selector == (ScopeExpression("PriorityClass", "Exists", ()),)
    assert not projected.complete


def test_a_quota_with_no_hard_limits_and_no_status_is_stale() -> None:
    assert project_quotas([_quota("empty", {})])[0].stale


def test_a_well_formed_quota_and_limit_range_are_complete() -> None:
    quota = _quota(
        "ok",
        {"pods": "1"},
        status={"hard": {"pods": "1"}, "used": {"pods": "0"}},
        scopes=["BestEffort"],
    )
    limit_range = {
        "metadata": {"name": "lr", "namespace": "shop", "uid": "u"},
        "spec": {"limits": [{"type": "Container", "max": {"cpu": "1"}}]},
    }

    assert project_quotas([quota])[0].complete
    assert project_limit_ranges([limit_range])[0].complete


@pytest.mark.parametrize(
    "spec",
    [
        {"hard": {"pods": "1", "services": None}},
        {"hard": "x"},
        {"hard": {"pods": "1"}, "scopes": ["BestEffort", 3]},
        {"hard": {"pods": "1"}, "scopes": "BestEffort"},
        {
            "hard": {"pods": "1"},
            "scopeSelector": {"matchExpressions": [{"scopeName": "PriorityClass"}]},
        },
        {"hard": {"pods": "1"}, "scopeSelector": {"matchExpressions": "x"}},
        {"hard": {"pods": "1"}, "scopeSelector": "x"},
        "not-a-spec",
        None,
    ],
    ids=[
        "null-value",
        "hard-not-a-map",
        "scope-not-text",
        "scopes-not-a-list",
        "expression-without-operator",
        "expressions-not-a-list",
        "selector-not-a-map",
        "spec-not-a-map",
        "no-spec",
    ],
)
def test_a_quota_with_an_unreadable_constraint_is_incomplete(spec: object) -> None:
    item = {"metadata": {"name": "q", "namespace": "shop", "uid": "u"}, "spec": spec}

    assert not project_quotas([item])[0].complete


@pytest.mark.parametrize(
    "spec",
    [
        {"limits": "x"},
        {"limits": ["x"]},
        {"limits": [{"type": "Container", "max": {"cpu": None}}]},
        {"limits": [{"type": "Container", "max": "1"}]},
        {"limits": [{"max": {"cpu": "1"}}]},
        {"limits": [{"type": 3, "max": {"cpu": "1"}}]},
        {},
        "not-a-spec",
        None,
    ],
    ids=[
        "limits-not-a-list",
        "limit-not-a-map",
        "null-value",
        "field-not-a-map",
        "no-type",
        "type-not-text",
        "no-limits",
        "spec-not-a-map",
        "no-spec",
    ],
)
def test_a_limit_range_with_an_unreadable_constraint_is_incomplete(spec: object) -> None:
    item = {"metadata": {"name": "lr", "namespace": "shop", "uid": "u"}, "spec": spec}

    assert not project_limit_ranges([item])[0].complete
```

Create `src/korvid/core/creation_policies.py` with:

```python
"""Project the namespace's ResourceQuotas and LimitRanges (#411).

Only what the view shows and Q2 compares is kept: identities, quantities as
the cluster wrote them, quota scopes and scope selectors, and whether a
quota's status is current. Quota status is filled by the quota controller
after `spec.hard` changes, so a quota whose status is missing, lags its spec
or lacks a used amount is stale, and none of its resources is called at its
limit. A policy with a constraint the projection cannot read is kept, but
marked incomplete, so a list holding it cannot rule anything out.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from korvid.core.creation_quantities import (
    QuotaGroup,
    Usage,
    quota_group,
    same_quantity,
    usage,
)

_GROUP_ORDER = {group: index for index, group in enumerate(QuotaGroup)}
_LIMIT_FIELDS = ("max", "min", "default", "defaultRequest", "maxLimitRequestRatio")


@dataclass(frozen=True, slots=True)
class QuotaEntry:
    """One resource a quota limits: `hard` from its spec, `used` from its status."""

    resource: str
    group: QuotaGroup
    hard: str
    used: str | None
    usage: Usage


@dataclass(frozen=True, slots=True)
class ScopeExpression:
    """One `scopeSelector.matchExpressions` entry."""

    scope_name: str
    operator: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuotaProjection:
    """One ResourceQuota, as the view shows it."""

    name: str
    uid: str | None
    entries: tuple[QuotaEntry, ...]
    scopes: tuple[str, ...] = ()
    scope_selector: tuple[ScopeExpression, ...] = ()
    stale: bool = False
    complete: bool = True


@dataclass(frozen=True, slots=True)
class LimitValue:
    """One LimitRange constraint on one resource of one type, verbatim."""

    type: str
    resource: str
    max: str | None = None
    min: str | None = None
    default: str | None = None
    default_request: str | None = None
    max_limit_request_ratio: str | None = None


@dataclass(frozen=True, slots=True)
class LimitRangeProjection:
    """One LimitRange, as the view shows it."""

    name: str
    uid: str | None
    values: tuple[LimitValue, ...]
    complete: bool = True


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _quantity(value: object) -> str | None:
    """A quantity as written; JSON may carry a whole number as a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return _text(value)


def _quantities(value: object) -> dict[str, str]:
    return {
        name: quantity
        for name, raw in _mapping(value).items()
        if isinstance(name, str) and (quantity := _quantity(raw)) is not None
    }


def _readable_quantities(value: object) -> bool:
    """Whether every entry of an optional quantity map can be read."""
    if value is None:
        return True
    return isinstance(value, Mapping) and len(_quantities(value)) == len(value)


def _identity(item: Mapping[str, Any]) -> tuple[str, str | None] | None:
    metadata = _mapping(item.get("metadata"))
    name = _text(metadata.get("name"))
    return None if name is None else (name, _text(metadata.get("uid")))


def _stale(hard: Mapping[str, str], status: Mapping[str, Any]) -> bool:
    """Whether the quota controller has not yet caught up with `hard`."""
    if not status:
        return True
    status_hard, used = _quantities(status.get("hard")), _quantities(status.get("used"))
    if status_hard.keys() != hard.keys() or not status_hard.keys() <= used.keys():
        return True
    return not all(same_quantity(hard[name], status_hard[name]) for name in hard)


def _expression(raw: object) -> ScopeExpression | None:
    expression = _mapping(raw)
    scope, operator = _text(expression.get("scopeName")), _text(expression.get("operator"))
    values = expression.get("values", [])
    if scope is None or operator is None or not isinstance(values, list):
        return None
    texts = tuple(v for v in values if isinstance(v, str))
    return ScopeExpression(scope, operator, texts) if len(texts) == len(values) else None


def _scope_selector(spec: Mapping[str, Any]) -> tuple[tuple[ScopeExpression, ...], bool]:
    """The selector's expressions, and whether every one of them could be read."""
    selector = spec.get("scopeSelector", {})
    if not isinstance(selector, Mapping):
        return (), False
    expressions = selector.get("matchExpressions", [])
    if not isinstance(expressions, list):
        return (), False
    selected = [_expression(raw) for raw in expressions]
    kept = tuple(e for e in selected if e is not None)
    return kept, len(kept) == len(selected)


def _scopes(spec: Mapping[str, Any]) -> tuple[tuple[str, ...], bool]:
    scopes = spec.get("scopes", [])
    if not isinstance(scopes, list):
        return (), False
    kept = tuple(s for s in scopes if isinstance(s, str))
    return kept, len(kept) == len(scopes)


def _quota(item: Mapping[str, Any]) -> QuotaProjection | None:
    identity = _identity(item)
    if identity is None:
        return None
    spec, status = _mapping(item.get("spec")), _mapping(item.get("status"))
    hard = _quantities(spec.get("hard"))
    stale = _stale(hard, status)
    used = _quantities(status.get("used"))
    entries = sorted(
        (
            QuotaEntry(
                name,
                quota_group(name),
                limit,
                used.get(name),
                Usage.NOT_ESTABLISHED if stale else usage(used.get(name), limit),
            )
            for name, limit in hard.items()
        ),
        key=lambda entry: (_GROUP_ORDER[entry.group], entry.resource),
    )
    scopes, scopes_read = _scopes(spec)
    selector, selector_read = _scope_selector(spec)
    return QuotaProjection(
        identity[0],
        identity[1],
        tuple(entries),
        scopes,
        selector,
        stale,
        isinstance(item.get("spec"), Mapping)
        and _readable_quantities(spec.get("hard"))
        and scopes_read
        and selector_read,
    )


def project_quotas(items: Iterable[object]) -> tuple[QuotaProjection, ...]:
    """Each named quota in `items`, ordered by name; anything else is skipped."""
    quotas = (_quota(item) for item in items if isinstance(item, Mapping))
    return tuple(sorted((q for q in quotas if q is not None), key=lambda q: q.name))


def _limit_values(limit: Mapping[str, Any]) -> Iterable[LimitValue]:
    kind = _text(limit.get("type")) or "?"
    fields = {field: _quantities(limit.get(field)) for field in _LIMIT_FIELDS}
    for resource in sorted({name for values in fields.values() for name in values}):
        yield LimitValue(
            kind,
            resource,
            fields["max"].get(resource),
            fields["min"].get(resource),
            fields["default"].get(resource),
            fields["defaultRequest"].get(resource),
            fields["maxLimitRequestRatio"].get(resource),
        )


def _readable_limit(limit: object) -> bool:
    """Whether a limit says what it applies to and every constraint is a quantity."""
    return (
        isinstance(limit, Mapping)
        and _text(limit.get("type")) is not None
        and all(_readable_quantities(limit.get(field)) for field in _LIMIT_FIELDS)
    )


def _limit_range(item: Mapping[str, Any]) -> LimitRangeProjection | None:
    identity = _identity(item)
    if identity is None:
        return None
    # `spec.limits` is required: a LimitRange without it could not be read.
    limits = _mapping(item.get("spec")).get("limits")
    entries = limits if isinstance(limits, list) else []
    values = (
        value for limit in entries if isinstance(limit, Mapping) for value in _limit_values(limit)
    )
    complete = isinstance(limits, list) and all(_readable_limit(limit) for limit in entries)
    return LimitRangeProjection(identity[0], identity[1], tuple(values), complete)


def project_limit_ranges(items: Iterable[object]) -> tuple[LimitRangeProjection, ...]:
    """Each named LimitRange in `items`, ordered by name; anything else is skipped."""
    ranges = (_limit_range(item) for item in items if isinstance(item, Mapping))
    return tuple(sorted((r for r in ranges if r is not None), key=lambda r: r.name))
```

- [ ] Run the module's tests before and after, then commit: `Project the namespace's ResourceQuotas and LimitRanges`.

### Task 4: Failures and their classification

Create `tests/core/test_creation_failures.py` with:

```python
"""Tests for recorded workload creation failures and their classification (#411)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core.creation_failures import (
    MESSAGE_LIMIT,
    Failure,
    FailureSource,
    Rejection,
    RejectionKind,
    Subject,
    classify,
    condition_failure,
    event_failures,
)

DEPLOYMENT = Subject("Deployment", "api", "uid-api")
REPLICA_SET = Subject("ReplicaSet", "api-7d9f", "uid-rs")
EXCEEDED = (
    'Error creating: pods "api-7d9f-x2kq" is forbidden: exceeded quota: compute, '
    "requested: requests.cpu=500m, used: requests.cpu=2, limited: requests.cpu=2"
)


@pytest.mark.parametrize(
    ("message", "kinds", "quotas"),
    [
        (EXCEEDED, (RejectionKind.QUOTA_EXCEEDED,), ("compute",)),
        (
            'pods "x" is forbidden: failed quota: compute: must specify limits.cpu for: app',
            (RejectionKind.QUOTA_REQUIRES_FIELD,),
            ("compute",),
        ),
        (
            'pods "x" is forbidden: status unknown for quota: compute, resource: pods',
            (RejectionKind.QUOTA_STATUS_UNKNOWN,),
            ("compute",),
        ),
        (
            'pods "x" is forbidden: minimum memory usage per Container is 64Mi, but request is 32Mi',
            (RejectionKind.LIMIT_MINIMUM,),
            (),
        ),
        (
            'pods "x" is forbidden: maximum cpu usage per Pod is 2, but limit is 3',
            (RejectionKind.LIMIT_MAXIMUM,),
            (),
        ),
        (
            'persistentvolumeclaims "d" is forbidden: maximum storage usage per '
            "PersistentVolumeClaim is 10Gi, but request is 20Gi",
            (RejectionKind.LIMIT_MAXIMUM,),
            (),
        ),
        (
            'pods "x" is forbidden: cpu max limit to request ratio per Container is 2, '
            "but provided ratio is 4.000000",
            (RejectionKind.LIMIT_RATIO,),
            (),
        ),
        (
            'pods "x" is forbidden: [maximum cpu usage per Container is 1, but limit is 2, '
            "minimum memory usage per Container is 64Mi, but request is 32Mi]",
            (RejectionKind.LIMIT_MAXIMUM, RejectionKind.LIMIT_MINIMUM),
            (),
        ),
        (
            'admission webhook "policy.example.com" denied the request: no latest tags',
            (RejectionKind.OTHER,),
            (),
        ),
    ],
)
def test_a_message_is_classified_only_by_fixed_phrases(
    message: str, kinds: tuple[RejectionKind, ...], quotas: tuple[str, ...]
) -> None:
    rejection = classify(message)

    assert (rejection.kinds, rejection.quotas, rejection.message) == (kinds, quotas, message)


def test_a_long_message_is_bounded() -> None:
    rejection = classify("x" * 2000)

    assert len(rejection.message) == MESSAGE_LIMIT
    assert rejection.message.endswith("…")


def _event(
    regarding: dict[str, Any], *, reason: str = "FailedCreate", **fields: Any
) -> dict[str, Any]:
    return {
        "metadata": {"name": "e", "namespace": "shop"},
        "reason": reason,
        "regarding": regarding,
        **fields,
    }


def _ref(subject: Subject, uid: str | None = None) -> dict[str, str]:
    return {"kind": subject.kind, "name": subject.name, "uid": uid or subject.uid}


def test_events_about_the_workload_or_its_children_become_failures_newest_first() -> None:
    events = [
        _event(
            _ref(REPLICA_SET),
            note=EXCEEDED,
            series={"count": 12, "lastObservedTime": "2026-10-30T09:58:00Z"},
        ),
        _event(_ref(DEPLOYMENT), note="other", eventTime="2026-10-30T09:59:00Z"),
        _event(_ref(REPLICA_SET), reason="SuccessfulCreate", note="created"),
    ]

    failures, earlier = event_failures(events, (DEPLOYMENT, REPLICA_SET))

    assert earlier == 0
    assert failures == (
        Failure(
            DEPLOYMENT,
            FailureSource.EVENT,
            classify("other"),
            1,
            datetime(2026, 10, 30, 9, 59, tzinfo=UTC),
        ),
        Failure(
            REPLICA_SET,
            FailureSource.EVENT,
            classify(EXCEEDED),
            12,
            datetime(2026, 10, 30, 9, 58, tzinfo=UTC),
        ),
    )


def test_a_core_event_is_matched_by_its_involved_object() -> None:
    event = {
        "metadata": {"name": "e"},
        "reason": "FailedCreate",
        "involvedObject": _ref(REPLICA_SET),
        "message": EXCEEDED,
        "count": 3,
        "lastTimestamp": "2026-10-30T09:00:00Z",
    }

    (failure,), _ = event_failures([event], (REPLICA_SET,))

    assert (failure.count, failure.rejection.kinds) == (3, (RejectionKind.QUOTA_EXCEEDED,))


def test_a_legacy_events_k8s_io_event_keeps_its_deprecated_count_and_time() -> None:
    event = _event(
        _ref(REPLICA_SET),
        note=EXCEEDED,
        deprecatedCount=7,
        deprecatedLastTimestamp="2026-10-30T09:30:00Z",
        metadata={"name": "e", "creationTimestamp": "2026-10-30T08:00:00Z"},
    )

    (failure,), _ = event_failures([event], (REPLICA_SET,))

    assert (failure.count, failure.at) == (7, datetime(2026, 10, 30, 9, 30, tzinfo=UTC))


def test_an_earlier_incarnations_events_are_counted_not_shown() -> None:
    events = [
        _event(_ref(DEPLOYMENT, "uid-old"), note=EXCEEDED),
        _event(_ref(REPLICA_SET, "uid-old-rs"), note=EXCEEDED),
        _event({"kind": "Deployment", "name": "other", "uid": "x"}, note=EXCEEDED),
    ]

    failures, earlier = event_failures(events, (DEPLOYMENT, REPLICA_SET))

    assert (failures, earlier) == ((), 2)


def test_an_event_without_a_subject_uid_or_with_a_bad_count_is_handled() -> None:
    events = [
        _event({"kind": "Deployment", "name": "api"}, note=EXCEEDED),
        _event(_ref(DEPLOYMENT), note=EXCEEDED, count=True, series={"count": 0}),
        "not-an-event",
    ]

    failures, earlier = event_failures(events, (DEPLOYMENT,))

    assert [(f.count, f.at) for f in failures] == [(1, None)]
    assert earlier == 0


def test_a_replica_failure_condition_counts_only_for_failed_create() -> None:
    created = {
        "conditions": [
            {
                "type": "ReplicaFailure",
                "status": "True",
                "reason": "FailedCreate",
                "message": EXCEEDED,
                "lastTransitionTime": "2026-10-30T09:00:00Z",
                "lastUpdateTime": "2026-10-30T09:40:00Z",
            }
        ]
    }

    failure = condition_failure(DEPLOYMENT, created)

    assert failure == Failure(
        DEPLOYMENT,
        FailureSource.CONDITION,
        classify(EXCEEDED),
        1,
        datetime(2026, 10, 30, 9, 40, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "condition",
    [
        {"type": "ReplicaFailure", "status": "True", "reason": "FailedDelete", "message": "m"},
        {"type": "ReplicaFailure", "status": "False", "reason": "FailedCreate", "message": "m"},
        {"type": "Progressing", "status": "True", "reason": "FailedCreate", "message": "m"},
    ],
    ids=["failed-delete", "status-false", "other-type"],
)
def test_other_conditions_are_not_creation_failures(condition: dict[str, str]) -> None:
    assert condition_failure(DEPLOYMENT, {"conditions": [condition]}) is None


def test_a_condition_without_times_is_undated() -> None:
    status = {
        "conditions": [{"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate"}]
    }

    failure = condition_failure(REPLICA_SET, status)

    assert failure is not None
    assert (failure.at, failure.rejection) == (None, Rejection((RejectionKind.OTHER,), (), ""))


def test_a_boolean_count_is_not_a_count() -> None:
    event = _event(_ref(DEPLOYMENT), note=EXCEEDED, series={"count": True}, count=4)

    (failure,), _ = event_failures([event], (DEPLOYMENT,))

    assert failure.count == 4


def test_a_time_without_a_zone_is_not_used() -> None:
    event = _event(_ref(DEPLOYMENT), note=EXCEEDED, eventTime="2026-10-30T09:00:00")

    (failure,), _ = event_failures([event], (DEPLOYMENT,))

    assert failure.at is None


def test_a_webhook_denial_is_never_attributed_to_a_policy() -> None:
    message = (
        'admission webhook "policy.example.com" denied the request: exceeded quota: compute, '
        "minimum cpu usage per Container is 1"
    )

    assert classify(message) == Rejection((RejectionKind.OTHER,), (), message)


def test_a_very_long_unbroken_message_is_classified() -> None:
    message = "x" * 200_000 + " max limit to request ratio per Container is 2"

    rejection = classify(message)

    assert rejection.kinds == (RejectionKind.LIMIT_RATIO,)
    assert len(rejection.message) == MESSAGE_LIMIT


def test_only_a_different_uid_is_an_earlier_incarnation() -> None:
    events = [
        _event({"kind": "Deployment", "name": "api"}, note=EXCEEDED),
        _event({"kind": "Deployment", "name": "api", "uid": ""}, note=EXCEEDED),
        _event(_ref(DEPLOYMENT, "uid-old"), note=EXCEEDED),
    ]

    failures, earlier = event_failures(events, (DEPLOYMENT,))

    assert (failures, earlier) == ((), 1)


def test_a_validating_admission_policy_denial_is_never_attributed_to_a_policy() -> None:
    message = (
        "ValidatingAdmissionPolicy 'quota-guard' with binding 'quota-guard-binding' denied "
        "request: exceeded quota: compute, maximum cpu usage per Container is 1"
    )

    assert classify(message) == Rejection((RejectionKind.OTHER,), (), message)
```

Create `src/korvid/core/creation_failures.py` with:

```python
"""Recorded workload creation failures, and what their messages say (#411).

A failure is a `FailedCreate` event about the workload or one of its
children, matched by UID, or a `ReplicaFailure` condition whose reason is
`FailedCreate`. Its message is the API server's text, not an API field: it is
classified only by the fixed quota and LimitRanger phrasings below, and kept
verbatim, bounded, whatever it says. A quota is named only as the message
names it; a LimitRange is never named, because its messages do not say which.
A denial an admission webhook or a ValidatingAdmissionPolicy wrote is never
classified further: its text is the policy author's own, and can say
anything.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from korvid.k8s.events import select_event_timestamp

#: How many characters of a message are kept.
MESSAGE_LIMIT = 500
_FAILED_CREATE = "FailedCreate"
_PER = r"per (?:Container|Pod|PersistentVolumeClaim) is"


class RejectionKind(StrEnum):
    """What a rejection message says, by its fixed phrasing."""

    QUOTA_EXCEEDED = "quota exceeded"
    QUOTA_REQUIRES_FIELD = "quota requires a field"
    QUOTA_STATUS_UNKNOWN = "quota not yet computed"
    LIMIT_MINIMUM = "below a LimitRange minimum"
    LIMIT_MAXIMUM = "above a LimitRange maximum"
    LIMIT_RATIO = "above a LimitRange ratio"
    OTHER = "other rejection"


#: Each phrase, in the order a message is searched; quota phrases name a quota.
_PHRASES: tuple[tuple[RejectionKind, re.Pattern[str]], ...] = (
    (RejectionKind.QUOTA_EXCEEDED, re.compile(r"exceeded quota: ([^,\s]+),")),
    (RejectionKind.QUOTA_REQUIRES_FIELD, re.compile(r"failed quota: ([^:\s]+): must specify")),
    (RejectionKind.QUOTA_STATUS_UNKNOWN, re.compile(r"status unknown for quota: ([^,\s]+)")),
    (RejectionKind.LIMIT_MINIMUM, re.compile(rf"minimum \S+ usage {_PER}")),
    (RejectionKind.LIMIT_MAXIMUM, re.compile(rf"maximum \S+ usage {_PER}")),
    # Anchored at a word's start, so a long unbroken message is scanned in linear time.
    (RejectionKind.LIMIT_RATIO, re.compile(rf"(?<!\S)\S+ max limit to request ratio {_PER}")),
)
_AUTHORED_DENIAL = re.compile(
    r'admission webhook "[^"]*" denied the request'
    r"|ValidatingAdmissionPolicy '[^']*' with binding '[^']*' denied request"
)


class FailureSource(StrEnum):
    """Where a failure was recorded."""

    EVENT = "event"
    CONDITION = "condition"


@dataclass(frozen=True, slots=True)
class Subject:
    """An object a failure can be recorded on: the workload or one of its children."""

    kind: str
    name: str
    uid: str


@dataclass(frozen=True, slots=True)
class Rejection:
    """A message's classification, the quotas it names, and the message, bounded."""

    kinds: tuple[RejectionKind, ...]
    quotas: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class Failure:
    """One recorded creation failure: about what, from where, how often, and when last."""

    subject: Subject
    source: FailureSource
    rejection: Rejection
    count: int
    at: datetime | None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _bounded(message: str) -> str:
    if len(message) <= MESSAGE_LIMIT:
        return message
    return message[: MESSAGE_LIMIT - 1] + "…"


def classify(message: str) -> Rejection:
    """Classify `message` by its fixed phrasings, in the order it states them.

    A message that states none of them, or that an admission webhook or a
    ValidatingAdmissionPolicy wrote, is `OTHER`.
    """
    if _AUTHORED_DENIAL.search(message):
        return Rejection((RejectionKind.OTHER,), (), _bounded(message))
    matches = sorted(
        (match.start(), kind, match.group(1) if match.groups() else None)
        for kind, pattern in _PHRASES
        for match in pattern.finditer(message)
    )
    kinds = dict.fromkeys(kind for _, kind, _ in matches)
    quotas = dict.fromkeys(quota for _, _, quota in matches if quota is not None)
    return Rejection(tuple(kinds) or (RejectionKind.OTHER,), tuple(quotas), _bounded(message))


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _event_count(event: Mapping[str, Any]) -> int:
    for value in (
        _mapping(event.get("series")).get("count"),
        event.get("deprecatedCount"),
        event.get("count"),
    ):
        count = _count(value)
        if count is not None:
            return count
    return 1


def _event_subject(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(event.get("regarding")) or _mapping(event.get("involvedObject"))


def _newest_first(failure: Failure) -> tuple[bool, float]:
    return (failure.at is None, -failure.at.timestamp() if failure.at is not None else 0.0)


def event_failures(
    events: Iterable[object], subjects: Sequence[Subject]
) -> tuple[tuple[Failure, ...], int]:
    """The `FailedCreate` events about `subjects`, newest first, and how many are not.

    An event is about a subject only when its UID matches. One whose kind and
    name match a subject but whose UID is another is about an earlier
    incarnation: it is counted, not returned. One without a UID establishes
    neither, and is ignored.
    """
    by_uid = {subject.uid: subject for subject in subjects}
    names = {(subject.kind, subject.name) for subject in subjects}
    failures: list[Failure] = []
    earlier = 0
    for event in events:
        if not isinstance(event, Mapping) or event.get("reason") != _FAILED_CREATE:
            continue
        about = _event_subject(event)
        uid = _text(about.get("uid"))
        subject = by_uid.get(uid)
        if subject is None:
            earlier += bool(uid) and (_text(about.get("kind")), _text(about.get("name"))) in names
            continue
        message = _text(event.get("note")) or _text(event.get("message"))
        at = _timestamp(select_event_timestamp(dict(event)))
        failures.append(
            Failure(subject, FailureSource.EVENT, classify(message), _event_count(event), at)
        )
    return tuple(sorted(failures, key=_newest_first)), earlier


def condition_failure(subject: Subject, status: Mapping[str, Any]) -> Failure | None:
    """The subject's `ReplicaFailure` condition, if it records a `FailedCreate`.

    The ReplicaSet controller also sets it with `FailedDelete`, which is not
    a creation failure.
    """
    conditions = status.get("conditions")
    for raw in conditions if isinstance(conditions, list) else []:
        condition = _mapping(raw)
        if (
            condition.get("type") == "ReplicaFailure"
            and condition.get("status") == "True"
            and condition.get("reason") == _FAILED_CREATE
        ):
            at = _timestamp(condition.get("lastUpdateTime")) or _timestamp(
                condition.get("lastTransitionTime")
            )
            rejection = classify(_text(condition.get("message")))
            return Failure(subject, FailureSource.CONDITION, rejection, 1, at)
    return None
```

- [ ] Run the module's tests before and after, then commit: `Classify recorded workload creation failures`.

### Task 5: Workloads and their children

Create `tests/core/test_creation_workloads.py` with:

```python
"""Tests for projecting workloads and their children for creation diagnostics (#411)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core.creation_failures import FailureSource, Subject
from korvid.core.creation_workloads import (
    WORKLOAD_KINDS,
    ClaimTemplate,
    ContainerDemand,
    Counter,
    project_children,
    project_workload,
)

POD_SPEC: dict[str, Any] = {
    "priorityClassName": "gold",
    "activeDeadlineSeconds": 600,
    "runtimeClassName": "kata",
    "resources": {"requests": {"cpu": "1"}},
    "initContainers": [
        {
            "name": "migrate",
            "image": "registry/migrate:secret-tag",
            "resources": {"requests": {"cpu": "2"}},
        },
        {"name": "proxy", "restartPolicy": "Always", "resources": {"limits": {"memory": "64Mi"}}},
    ],
    "containers": [
        {
            "name": "app",
            "image": "registry/app:secret-tag",
            "command": ["run", "--token=s3cr3t-literal"],
            "env": [{"name": "TOKEN", "value": "s3cr3t-literal"}],
            "resources": {
                "requests": {"cpu": "500m", "memory": "128Mi"},
                "limits": {"memory": "256Mi"},
            },
        }
    ],
    "volumes": [{"name": "v", "secret": {"secretName": "creds"}}],
}


def _workload(
    kind: str, spec: dict[str, Any], status: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": "api",
            "namespace": "shop",
            "uid": "uid-api",
            "annotations": {"note": "annotation-literal"},
        },
        "spec": spec,
        "status": status or {},
    }


def test_the_six_kinds_and_their_children() -> None:
    assert {kind: (k.group, k.resource, k.child) for kind, k in WORKLOAD_KINDS.items()} == {
        "Deployment": ("apps", "deployments", "ReplicaSet"),
        "ReplicaSet": ("apps", "replicasets", None),
        "StatefulSet": ("apps", "statefulsets", None),
        "DaemonSet": ("apps", "daemonsets", None),
        "Job": ("batch", "jobs", None),
        "CronJob": ("batch", "cronjobs", "Job"),
    }


def test_a_deployment_keeps_only_its_demand_and_never_a_literal() -> None:
    item = _workload("Deployment", {"replicas": 3, "template": {"spec": POD_SPEC}}, {"replicas": 0})

    projected = project_workload("Deployment", item)

    assert projected is not None
    assert projected.subject == Subject("Deployment", "api", "uid-api")
    assert projected.counters == (Counter("desired", "3"), Counter("created", "0"))
    template = projected.template
    assert template is not None
    assert template.containers == (
        ContainerDemand(
            "app", "container", (("cpu", "500m"), ("memory", "128Mi")), (("memory", "256Mi"),)
        ),
    )
    assert template.init_containers == (
        ContainerDemand("migrate", "init", (("cpu", "2"),), ()),
        ContainerDemand("proxy", "sidecar", (), (("memory", "64Mi"),)),
    )
    assert (template.priority_class, template.active_deadline_seconds, template.runtime_class) == (
        "gold",
        600,
        "kata",
    )
    assert (template.pod_requests, template.pod_limits) == ((("cpu", "1"),), ())
    for literal in ("s3cr3t-literal", "secret-tag", "annotation-literal", "creds"):
        assert literal not in repr(projected)


@pytest.mark.parametrize(
    ("kind", "spec", "status", "counters"),
    [
        ("ReplicaSet", {"replicas": 2}, {}, (Counter("desired", "2"), Counter("created", "0"))),
        (
            "StatefulSet",
            {"replicas": 1},
            {"replicas": 1},
            (Counter("desired", "1"), Counter("created", "1")),
        ),
        (
            "DaemonSet",
            {},
            {"desiredNumberScheduled": 4, "currentNumberScheduled": 2},
            (Counter("desired", "4"), Counter("created", "2")),
        ),
        (
            "Job",
            {"parallelism": 2},
            {"active": 1},
            (Counter("parallelism", "2"), Counter("active", "1")),
        ),
        (
            "CronJob",
            {"suspend": True},
            {"active": [{"name": "a"}, {"name": "b"}], "lastScheduleTime": "2026-10-30T09:00:00Z"},
            (
                Counter("suspended", "yes"),
                Counter("active", "2"),
                Counter("last schedule", "2026-10-30T09:00:00Z"),
            ),
        ),
        ("Deployment", {}, {}, (Counter("desired", None), Counter("created", "0"))),
        (
            "CronJob",
            {},
            {},
            (Counter("suspended", "no"), Counter("active", "0"), Counter("last schedule", None)),
        ),
    ],
)
def test_each_kind_has_its_own_counters(
    kind: str, spec: dict[str, Any], status: dict[str, Any], counters: tuple[Counter, ...]
) -> None:
    projected = project_workload(kind, _workload(kind, spec, status))

    assert projected is not None
    assert projected.counters == counters


def test_a_cronjob_template_is_its_job_templates_pod_template() -> None:
    spec = {"jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "task"}]}}}}}

    projected = project_workload("CronJob", _workload("CronJob", spec))

    assert projected is not None
    assert projected.template is not None
    assert [c.name for c in projected.template.containers] == ["task"]


def test_a_statefulset_keeps_its_claim_templates_and_whether_the_class_was_omitted() -> None:
    claims = [
        {
            "metadata": {"name": "data"},
            "spec": {"storageClassName": "gold", "resources": {"requests": {"storage": "10Gi"}}},
        },
        {"metadata": {"name": "logs"}, "spec": {"resources": {"requests": {"storage": "1Gi"}}}},
        {"metadata": {"name": "scratch"}, "spec": {"storageClassName": ""}},
    ]
    spec = {"replicas": 1, "template": {"spec": {"containers": []}}, "volumeClaimTemplates": claims}

    projected = project_workload("StatefulSet", _workload("StatefulSet", spec))

    assert projected is not None
    assert projected.template is not None
    assert projected.template.claims == (
        ClaimTemplate("data", "10Gi", "gold"),
        ClaimTemplate("logs", "1Gi", None),
        ClaimTemplate("scratch", None, ""),
    )


def test_a_replica_failure_on_the_workload_is_kept() -> None:
    status = {
        "conditions": [
            {
                "type": "ReplicaFailure",
                "status": "True",
                "reason": "FailedCreate",
                "message": "exceeded quota: q, x",
            }
        ]
    }

    projected = project_workload("Deployment", _workload("Deployment", {"replicas": 1}, status))

    assert projected is not None
    assert projected.failure is not None
    assert (projected.failure.source, projected.failure.rejection.quotas) == (
        FailureSource.CONDITION,
        ("q",),
    )


@pytest.mark.parametrize(
    "item",
    [{"metadata": {"name": "api"}}, {"metadata": {"uid": "u"}}, {}],
    ids=["no-uid", "no-name", "no-metadata"],
)
def test_a_workload_without_a_name_or_uid_is_not_projected(item: dict[str, Any]) -> None:
    assert project_workload("Deployment", item) is None


def test_an_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="workload kind"):
        project_workload("Pod", _workload("Pod", {}))


def _child(
    name: str, owner_uid: str | None, *, controller: bool = True, **extra: Any
) -> dict[str, Any]:
    owners = (
        []
        if owner_uid is None
        else [{"uid": owner_uid, "controller": controller, "kind": "X", "name": "x"}]
    )
    return {
        "metadata": {
            "name": name,
            "namespace": "shop",
            "uid": f"uid-{name}",
            "ownerReferences": owners,
            **extra.pop("metadata", {}),
        },
        "spec": {
            "template": {
                "spec": {"containers": [{"env": [{"name": "T", "value": "s3cr3t-literal"}]}]}
            }
        },
        **extra,
    }


def test_only_children_the_workload_controls_are_kept() -> None:
    items = [
        _child("api-1", "uid-api"),
        _child("api-2", "uid-api", controller=False),
        _child("other-1", "uid-other"),
        _child("orphan", None),
        "not-an-object",
    ]

    children = project_children("ReplicaSet", items, "uid-api").children

    assert [c.subject for c in children] == [Subject("ReplicaSet", "api-1", "uid-api-1")]
    assert "s3cr3t-literal" not in repr(children)


def test_a_replica_sets_failed_create_condition_is_kept() -> None:
    status = {
        "conditions": [
            {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate", "message": "m"}
        ]
    }

    (child,) = project_children(
        "ReplicaSet", [_child("api-1", "uid-api", status=status)], "uid-api"
    ).children

    assert child.failure is not None
    assert child.failure.subject == child.subject


def test_jobs_are_newest_first() -> None:
    items = [
        _child("job-old", "uid-cron", metadata={"creationTimestamp": "2026-10-30T08:00:00Z"}),
        _child("job-new", "uid-cron", metadata={"creationTimestamp": "2026-10-30T09:00:00Z"}),
        _child("job-undated", "uid-cron"),
    ]

    children = project_children("Job", items, "uid-cron").children

    assert [c.subject.name for c in children] == ["job-new", "job-old", "job-undated"]
    assert children[0].created_at == datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


def test_malformed_template_fields_are_dropped_and_numbers_kept_as_text() -> None:
    pod = {
        "activeDeadlineSeconds": True,
        "containers": [
            {"resources": {"requests": {"cpu": "1"}}},
            {"name": "app", "resources": {"requests": {"cpu": 2, "memory": None}}},
        ],
    }

    projected = project_workload("Job", _workload("Job", {"template": {"spec": pod}}))

    assert projected is not None
    assert projected.template is not None
    assert projected.template.active_deadline_seconds is None
    assert projected.template.containers == (
        ContainerDemand("app", "container", (("cpu", "2"),), ()),
    )


def test_only_a_statefulset_has_claim_templates() -> None:
    spec = {
        "template": {"spec": {"containers": []}},
        "volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {}}],
    }

    projected = project_workload("Deployment", _workload("Deployment", spec))

    assert projected is not None
    assert projected.template is not None
    assert projected.template.claims == ()


def test_owned_children_without_a_name_or_uid_are_counted_as_unprojected() -> None:
    nameless = _child("x", "uid-api")
    del nameless["metadata"]["name"]
    uidless = _child("y", "uid-api")
    del uidless["metadata"]["uid"]
    unowned = _child("z", "uid-other")
    del unowned["metadata"]["name"]

    projected = project_children(
        "ReplicaSet", [_child("api-1", "uid-api"), nameless, uidless, unowned], "uid-api"
    )

    assert [c.subject.name for c in projected.children] == ["api-1"]
    assert projected.unprojected == 2
```

Create `src/korvid/core/creation_workloads.py` with:

```python
"""Project a workload and its children for creation diagnostics (#411).

A workload arrives whole, and its Pod template carries literal environment
values, commands and images. The projection keeps only identity, the
counters the view's header shows, a `FailedCreate` `ReplicaFailure`
condition, and the template's demand, so nothing else leaves the run:

- each container's and init container's name, requests and limits, in
  declaration order, and whether an init container is a sidecar;
- the Pod's `priorityClassName`, `activeDeadlineSeconds`, `runtimeClassName`
  and Pod-level requests and limits;
- a StatefulSet's claim templates: name, storage request and storage class,
  `None` when omitted and `""` when explicitly empty.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from korvid.core.creation_failures import Failure, Subject, condition_failure


@dataclass(frozen=True, slots=True)
class WorkloadKind:
    """A workload kind the view opens on, and the kind of child that records its Pods' failures."""

    kind: str
    group: str
    version: str
    resource: str
    child: str | None = None


WORKLOAD_KINDS: Mapping[str, WorkloadKind] = {
    kind.kind: kind
    for kind in (
        WorkloadKind("Deployment", "apps", "v1", "deployments", "ReplicaSet"),
        WorkloadKind("ReplicaSet", "apps", "v1", "replicasets"),
        WorkloadKind("StatefulSet", "apps", "v1", "statefulsets"),
        WorkloadKind("DaemonSet", "apps", "v1", "daemonsets"),
        WorkloadKind("Job", "batch", "v1", "jobs"),
        WorkloadKind("CronJob", "batch", "v1", "cronjobs", "Job"),
    )
}

#: A resource and its quantity, as written.
Quantities = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Counter:
    """One header counter, from the workload's spec or status; `None` when not set."""

    label: str
    value: str | None


@dataclass(frozen=True, slots=True)
class ContainerDemand:
    """One container's requests and limits; `category` is container, init or sidecar."""

    name: str
    category: str
    requests: Quantities
    limits: Quantities


@dataclass(frozen=True, slots=True)
class ClaimTemplate:
    """One StatefulSet claim template: storage class `None` when omitted, `""` when empty."""

    name: str
    storage: str | None
    storage_class: str | None


@dataclass(frozen=True, slots=True)
class PodTemplate:
    """What a new Pod of the workload asks for."""

    containers: tuple[ContainerDemand, ...]
    init_containers: tuple[ContainerDemand, ...]
    priority_class: str | None = None
    active_deadline_seconds: int | None = None
    runtime_class: str | None = None
    pod_requests: Quantities = ()
    pod_limits: Quantities = ()
    claims: tuple[ClaimTemplate, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkloadProjection:
    """The selected workload, as the creation diagnostic keeps it."""

    subject: Subject
    counters: tuple[Counter, ...]
    failure: Failure | None
    template: PodTemplate | None


@dataclass(frozen=True, slots=True)
class ChildProjection:
    """A ReplicaSet or Job the workload controls."""

    subject: Subject
    failure: Failure | None = None
    created_at: datetime | None = None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> str | None:
    number = _integer(value)
    return None if number is None else str(number)


def _quantity(value: object) -> str | None:
    return _number(value) if not isinstance(value, str) else _text(value)


def _quantities(value: object) -> Quantities:
    return tuple(
        sorted(
            (name, quantity)
            for name, raw in _mapping(value).items()
            if isinstance(name, str) and (quantity := _quantity(raw)) is not None
        )
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _subject(kind: str, item: Mapping[str, Any]) -> Subject | None:
    metadata = _mapping(item.get("metadata"))
    name, uid = _text(metadata.get("name")), _text(metadata.get("uid"))
    return None if name is None or uid is None else Subject(kind, name, uid)


def _replicas(spec: Mapping[str, Any], status: Mapping[str, Any]) -> tuple[Counter, ...]:
    created = _number(status.get("replicas")) or "0"
    return (Counter("desired", _number(spec.get("replicas"))), Counter("created", created))


def _counters(kind: str, spec: Mapping[str, Any], status: Mapping[str, Any]) -> tuple[Counter, ...]:
    if kind == "DaemonSet":
        return (
            Counter("desired", _number(status.get("desiredNumberScheduled"))),
            Counter("created", _number(status.get("currentNumberScheduled")) or "0"),
        )
    if kind == "Job":
        return (
            Counter("parallelism", _number(spec.get("parallelism"))),
            Counter("active", _number(status.get("active")) or "0"),
        )
    if kind == "CronJob":
        active = status.get("active")
        return (
            Counter("suspended", "yes" if spec.get("suspend") is True else "no"),
            Counter("active", str(len(active) if isinstance(active, list) else 0)),
            Counter("last schedule", _text(status.get("lastScheduleTime"))),
        )
    return _replicas(spec, status)


def _containers(entries: object, init: bool) -> tuple[ContainerDemand, ...]:
    demands: list[ContainerDemand] = []
    for raw in entries if isinstance(entries, list) else []:
        entry = _mapping(raw)
        name = _text(entry.get("name"))
        if name is None:
            continue
        category = "container"
        if init:
            category = "sidecar" if entry.get("restartPolicy") == "Always" else "init"
        resources = _mapping(entry.get("resources"))
        demands.append(
            ContainerDemand(
                name,
                category,
                _quantities(resources.get("requests")),
                _quantities(resources.get("limits")),
            )
        )
    return tuple(demands)


def _claims(templates: object) -> tuple[ClaimTemplate, ...]:
    claims: list[ClaimTemplate] = []
    for raw in templates if isinstance(templates, list) else []:
        template = _mapping(raw)
        name = _text(_mapping(template.get("metadata")).get("name"))
        if name is None:
            continue
        spec = _mapping(template.get("spec"))
        storage_class = spec.get("storageClassName")
        storage = _mapping(_mapping(spec.get("resources")).get("requests")).get("storage")
        claims.append(
            ClaimTemplate(
                name,
                _quantity(storage),
                storage_class if isinstance(storage_class, str) else None,
            )
        )
    return tuple(claims)


def _pod_spec(kind: str, spec: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if kind == "CronJob":
        spec = _mapping(_mapping(spec.get("jobTemplate")).get("spec"))
    template = spec.get("template")
    return _mapping(_mapping(template).get("spec")) if isinstance(template, Mapping) else None


def _template(kind: str, spec: Mapping[str, Any]) -> PodTemplate | None:
    pod = _pod_spec(kind, spec)
    if pod is None:
        return None
    resources = _mapping(pod.get("resources"))
    return PodTemplate(
        _containers(pod.get("containers"), init=False),
        _containers(pod.get("initContainers"), init=True),
        _text(pod.get("priorityClassName")),
        _integer(pod.get("activeDeadlineSeconds")),
        _text(pod.get("runtimeClassName")),
        _quantities(resources.get("requests")),
        _quantities(resources.get("limits")),
        _claims(spec.get("volumeClaimTemplates")) if kind == "StatefulSet" else (),
    )


def project_workload(kind: str, item: Mapping[str, Any]) -> WorkloadProjection | None:
    """The workload `item` of `kind`, or `None` without a name and UID.

    Raises:
        ValueError: if `kind` is not one of `WORKLOAD_KINDS`.
    """
    if kind not in WORKLOAD_KINDS:
        raise ValueError(f"{kind!r} is not a workload kind the creation diagnostic reads")
    subject = _subject(kind, item)
    if subject is None:
        return None
    spec, status = _mapping(item.get("spec")), _mapping(item.get("status"))
    return WorkloadProjection(
        subject,
        _counters(kind, spec, status),
        condition_failure(subject, status),
        _template(kind, spec),
    )


def _controller_uid(item: Mapping[str, Any]) -> str | None:
    owners = _mapping(item.get("metadata")).get("ownerReferences")
    for raw in owners if isinstance(owners, list) else []:
        owner = _mapping(raw)
        if owner.get("controller") is True:
            return _text(owner.get("uid"))
    return None


@dataclass(frozen=True, slots=True)
class ChildrenProjection:
    """The children a workload controls, and how many of them had no name or UID."""

    children: tuple[ChildProjection, ...]
    unprojected: int = 0


def _newest_first(child: ChildProjection) -> tuple[bool, float]:
    at = child.created_at
    return (at is None, -at.timestamp() if at is not None else 0.0)


def project_children(kind: str, items: Iterable[object], parent_uid: str) -> ChildrenProjection:
    """The `kind` objects in `items` whose controller is `parent_uid`, newest first.

    One the workload controls but that has no name or UID is counted, since
    its conditions and the events about it are lost.
    """
    children: list[ChildProjection] = []
    unprojected = 0
    for item in items:
        if not isinstance(item, Mapping) or _controller_uid(item) != parent_uid:
            continue
        subject = _subject(kind, item)
        if subject is None:
            unprojected += 1
            continue
        created = _timestamp(_mapping(item.get("metadata")).get("creationTimestamp"))
        failure = condition_failure(subject, _mapping(item.get("status")))
        children.append(ChildProjection(subject, failure, created))
    return ChildrenProjection(tuple(sorted(children, key=_newest_first)), unprojected)
```

- [ ] Run the module's tests before and after, then commit: `Project workloads and their children for creation diagnostics`.

### Task 6: The run

Create `tests/core/test_creation_constraints.py` with:

```python
"""Tests for reading a workload's creation failures and namespace policies (#411)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core.api_versions import DiscoverySnapshot, observe_discovery
from korvid.core.capability import Basis, EvidenceState, Observation, RunToken
from korvid.core.creation_constraints import (
    CreationTarget,
    declarations,
    explain_creation,
)
from korvid.core.creation_failures import RejectionKind, Subject
from korvid.core.evidence_reads import DEFAULT_MAX_BYTES, LIST_PAGE_SIZE, MAX_BYTES
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
)
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.evidence_requests import EvidencePage, EvidenceReader, EvidenceTarget

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
TOKEN = RunToken(4, 2, 7)
TARGET = CreationTarget("Deployment", "shop", "api", "uid-api")
EXCEEDED = (
    'Error creating: pods "api-7d9f-x" is forbidden: exceeded quota: compute, '
    "requested: pods=1, used: pods=0, limited: pods=0"
)
_SERVED = {
    ("", "v1"): ("resourcequotas", "limitranges", "events"),
    ("apps", "v1"): ("deployments", "replicasets", "statefulsets", "daemonsets"),
    ("batch", "v1"): ("jobs", "cronjobs"),
    ("events.k8s.io", "v1"): ("events",),
}


def _clock() -> datetime:
    return NOW


def _snapshot(served: dict[tuple[str, str], tuple[str, ...]] = _SERVED) -> DiscoverySnapshot:
    outcomes = tuple(
        GroupVersionOutcome(
            group, version, tuple(ServedResource(r, frozenset({"get", "list"})) for r in resources)
        )
        for (group, version), resources in served.items()
    )
    groups = tuple((g, v) for g, v in served if g)
    raw = RawDiscovery(
        1, NOW, 0.0, RootOutcome("/api", (("", "v1"),)), RootOutcome("/apis", groups), outcomes
    )
    return observe_discovery(raw)


def _status(
    code: int, reason: str, *, name: str = "", kind: str = "", group: str = ""
) -> ApiStatusError:
    details = {"name": name, "group": group, "kind": kind}
    return ApiStatusError(
        code, reason, json.dumps({"kind": "Status", "reason": reason, "details": details})
    )


def _meta(name: str, uid: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "namespace": "shop", "uid": uid, **extra}


DEPLOYMENT = {
    "metadata": _meta("api", "uid-api"),
    "spec": {
        "replicas": 3,
        "template": {
            "spec": {
                "containers": [{"name": "app", "env": [{"name": "T", "value": "s3cr3t-literal"}]}]
            }
        },
    },
    "status": {},
}
REPLICA_SET = {
    "metadata": _meta(
        "api-7d9f", "uid-rs", ownerReferences=[{"uid": "uid-api", "controller": True}]
    ),
    "spec": {"template": {"spec": {"containers": [{"env": [{"value": "s3cr3t-literal"}]}]}}},
}
FAILED_CREATE = {
    "metadata": _meta("e1", "uid-e1"),
    "reason": "FailedCreate",
    "regarding": {"kind": "ReplicaSet", "name": "api-7d9f", "uid": "uid-rs"},
    "note": EXCEEDED,
    "series": {"count": 12, "lastObservedTime": "2026-10-30T09:58:00Z"},
}
QUOTA = {
    "metadata": _meta("compute", "uid-q"),
    "spec": {"hard": {"pods": "0"}},
    "status": {"hard": {"pods": "0"}, "used": {"pods": "0"}},
}
LIMIT_RANGE = {
    "metadata": _meta("defaults", "uid-lr"),
    "spec": {"limits": [{"type": "Container", "max": {"cpu": "1"}}]},
}


class FakeReader(EvidenceReader):
    """Answers each resource from its own script, since the reads run together."""

    def __init__(self, **answers: object) -> None:
        self.answers: dict[str, object] = {
            "deployments": DEPLOYMENT,
            "replicasets": (REPLICA_SET,),
            "events": (FAILED_CREATE,),
            "resourcequotas": (QUOTA,),
            "limitranges": (LIMIT_RANGE,),
            **answers,
        }
        self.gets: list[tuple[EvidenceTarget, str, int]] = []
        self.lists: list[tuple[EvidenceTarget, int, int]] = []
        self.continuations: dict[str, list[str]] = {}

    def _answer(self, target: EvidenceTarget) -> object:
        answer = self.answers.get(
            f"{target.group}/{target.resource}", self.answers.get(target.resource)
        )
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.lists.append((target, limit, max_bytes))
        self.continuations.setdefault(target.resource, []).append(continuation)
        answer = self._answer(target)
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, EvidencePage):
            return answer
        assert isinstance(answer, tuple)
        return EvidencePage(answer)

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        self.gets.append((target, name, max_bytes))
        answer = self._answer(target)
        assert isinstance(answer, dict)
        return answer


def _truncated(item: dict[str, Any]) -> list[EvidencePage]:
    """Two pages, each with more pending: the default page cap truncates the read."""
    return [EvidencePage((item,), continuation="a"), EvidencePage((), continuation="b")]


def _state(observation: Observation | None) -> tuple[EvidenceState, Basis] | None:
    return None if observation is None else (observation.state, observation.basis)


async def _explain(
    reader: FakeReader, target: CreationTarget = TARGET, snapshot: DiscoverySnapshot | None = None
) -> Any:
    return await explain_creation(
        reader, snapshot or _snapshot(), target, token=TOKEN, clock=_clock
    )


async def test_a_deployments_failures_come_from_its_replica_sets_next_to_the_policies() -> None:
    reader = FakeReader()

    report = await _explain(reader)

    assert report.workload.subject == Subject("Deployment", "api", "uid-api")
    assert [c.subject.name for c in report.children] == ["api-7d9f"]
    (failure,) = report.failures
    assert (failure.subject.name, failure.count) == ("api-7d9f", 12)
    assert failure.rejection.kinds == (RejectionKind.QUOTA_EXCEEDED,)
    assert [q.name for q in report.quotas] == ["compute"]
    assert [r.name for r in report.limit_ranges] == ["defaults"]
    assert report.baseline.state is EvidenceState.AVAILABLE
    assert report.quotas_conclusive(NOW)
    assert report.limit_ranges_conclusive(NOW)
    assert "s3cr3t-literal" not in repr(report)


async def test_the_reads_and_their_budgets() -> None:
    reader = FakeReader()

    await _explain(reader)

    assert reader.gets == [
        (EvidenceTarget("apps", "v1", "deployments", "shop"), "api", DEFAULT_MAX_BYTES)
    ]
    assert Counter(reader.lists) == Counter(
        {
            (EvidenceTarget("apps", "v1", "replicasets", "shop"), LIST_PAGE_SIZE, MAX_BYTES): 1,
            (EvidenceTarget("events.k8s.io", "v1", "events", "shop"), LIST_PAGE_SIZE, MAX_BYTES): 1,
            (
                EvidenceTarget("", "v1", "resourcequotas", "shop"),
                LIST_PAGE_SIZE,
                DEFAULT_MAX_BYTES,
            ): 1,
            (EvidenceTarget("", "v1", "limitranges", "shop"), LIST_PAGE_SIZE, DEFAULT_MAX_BYTES): 1,
        }
    )
    assert reader.continuations == {
        "replicasets": [""],
        "events": [""],
        "resourcequotas": [""],
        "limitranges": [""],
    }


async def test_a_cronjob_reads_its_jobs_and_a_statefulset_no_children() -> None:
    cron = {"metadata": _meta("nightly", "uid-cron"), "spec": {}, "status": {}}
    job = {
        "metadata": _meta(
            "nightly-1", "uid-job", ownerReferences=[{"uid": "uid-cron", "controller": True}]
        )
    }
    reader = FakeReader(cronjobs=cron, jobs=(job,), events=())

    report = await _explain(reader, CreationTarget("CronJob", "shop", "nightly", "uid-cron"))

    assert [c.subject for c in report.children] == [Subject("Job", "nightly-1", "uid-job")]
    stateful = FakeReader(statefulsets={"metadata": _meta("db", "uid-db"), "spec": {}}, events=())
    db = await _explain(stateful, CreationTarget("StatefulSet", "shop", "db", "uid-db"))
    assert db.children == ()
    assert db.children_observation is None
    assert all(t.resource != "replicasets" for t, _, _ in stateful.lists)


async def test_a_recreated_workload_matches_events_against_its_new_uid() -> None:
    recreated = {**DEPLOYMENT, "metadata": _meta("api", "uid-new")}
    old_event = {
        **FAILED_CREATE,
        "regarding": {"kind": "Deployment", "name": "api", "uid": "uid-api"},
    }
    new_event = {
        **FAILED_CREATE,
        "regarding": {"kind": "Deployment", "name": "api", "uid": "uid-new"},
    }
    reader = FakeReader(deployments=recreated, replicasets=(), events=(old_event, new_event))

    report = await _explain(reader)

    assert report.recreated
    assert [f.subject.uid for f in report.failures] == ["uid-new"]
    assert report.earlier_failures == 1


async def test_a_denied_workload_read_still_matches_events_by_the_selected_uid() -> None:
    event = {**FAILED_CREATE, "regarding": {"kind": "Deployment", "name": "api", "uid": "uid-api"}}
    reader = FakeReader(deployments=_status(403, "Forbidden"), events=(event,))

    report = await _explain(reader)

    assert report.workload is None
    assert _state(report.workload_observation) == (EvidenceState.DENIED, Basis.FORBIDDEN)
    assert [f.subject for f in report.failures] == [Subject("Deployment", "api", "uid-api")]
    assert [c.subject.name for c in report.children] == ["api-7d9f"]
    assert report.baseline.state is not EvidenceState.AVAILABLE


async def test_an_absent_workload_has_no_failures_or_children() -> None:
    reader = FakeReader(
        deployments=_status(404, "NotFound", name="api", kind="deployments", group="apps")
    )

    report = await _explain(reader, CreationTarget("Deployment", "shop", "api"))

    assert _state(report.workload_observation) == (EvidenceState.AVAILABLE, Basis.OBJECT_ABSENT)
    assert (report.workload, report.children, report.failures) == (None, (), ())


async def test_a_replica_failure_condition_is_a_failure_next_to_the_events() -> None:
    condition = {
        "type": "ReplicaFailure",
        "status": "True",
        "reason": "FailedCreate",
        "message": EXCEEDED,
        "lastUpdateTime": "2026-10-30T09:59:00Z",
    }
    deployment = {**DEPLOYMENT, "status": {"conditions": [condition]}}

    report = await _explain(FakeReader(deployments=deployment))

    assert [(f.subject.kind, f.source.value) for f in report.failures] == [
        ("Deployment", "condition"),
        ("ReplicaSet", "event"),
    ]


async def test_a_stale_quota_downgrades_an_available_quota_list() -> None:
    stale = {**QUOTA, "status": {"hard": {"pods": "5"}, "used": {"pods": "0"}}}

    report = await _explain(FakeReader(resourcequotas=(stale,)))

    assert _state(report.quotas_observation) == (EvidenceState.PARTIAL, Basis.STALE_STATUS)
    assert "compute" in report.quotas_observation.detail
    assert not report.quotas_conclusive(NOW)


async def test_a_stale_quota_in_a_truncated_list_keeps_its_basis() -> None:
    stale = {**QUOTA, "status": {}}
    reader = FakeReader(resourcequotas=_truncated(stale))

    report = await explain_creation(reader, _snapshot(), TARGET, token=TOKEN, clock=_clock)

    assert reader.continuations["resourcequotas"] == ["", "a"]
    assert _state(report.quotas_observation) == (EvidenceState.PARTIAL, Basis.TRUNCATED)
    assert report.quotas[0].stale


async def test_an_empty_conclusive_quota_list_rules_out_quotas_but_a_denied_one_does_not() -> None:
    empty = await _explain(FakeReader(resourcequotas=()))
    denied = await _explain(FakeReader(resourcequotas=_status(403, "Forbidden")))

    assert (empty.quotas, empty.quotas_conclusive(NOW)) == ((), True)
    assert (denied.quotas, denied.quotas_conclusive(NOW)) == ((), False)


async def test_core_events_are_read_when_events_k8s_io_is_not_served() -> None:
    served = {key: value for key, value in _SERVED.items() if key[0] != "events.k8s.io"}
    core_event = {
        "metadata": _meta("e", "uid-e"),
        "reason": "FailedCreate",
        "involvedObject": {"kind": "ReplicaSet", "name": "api-7d9f", "uid": "uid-rs"},
        "message": EXCEEDED,
        "count": 2,
    }
    reader = FakeReader(events=(core_event,))

    report = await _explain(reader, snapshot=_snapshot(served))

    assert [f.count for f in report.failures] == [2]
    assert any(t.group == "" and t.resource == "events" for t, _, _ in reader.lists)


async def test_every_observation_carries_the_run_token() -> None:
    report = await _explain(FakeReader())

    observations = [
        report.workload_observation,
        report.children_observation,
        report.events_observation,
        report.quotas_observation,
        report.limit_ranges_observation,
    ]
    assert {o.token for o in observations if o is not None} == {TOKEN}


def test_declarations_name_each_source_and_its_role() -> None:
    keys = {(d.key, d.required, d.role.value) for d in declarations(TARGET)}

    assert keys == {
        ("workload", True, "confirms"),
        ("children", True, "confirms"),
        ("events", True, "confirms"),
        ("quotas", True, "rules_out"),
        ("limit-ranges", True, "rules_out"),
    }
    assert "children" not in {d.key for d in declarations(CreationTarget("Job", "shop", "j"))}


@pytest.mark.parametrize(
    ("kind", "namespace", "name"),
    [("Pod", "shop", "x"), ("Deployment", "", "x"), ("Deployment", "shop", "")],
)
def test_a_target_is_a_named_workload_in_a_namespace(kind: str, namespace: str, name: str) -> None:
    with pytest.raises(ValueError, match="workload"):
        CreationTarget(kind, namespace, name)


async def test_an_absent_workloads_old_events_are_not_its_failures() -> None:
    event = {**FAILED_CREATE, "regarding": {"kind": "Deployment", "name": "api", "uid": "uid-api"}}
    absent = _status(404, "NotFound", name="api", kind="deployments", group="apps")

    report = await _explain(FakeReader(deployments=absent, events=(event,)))

    assert report.failures == ()


async def test_a_replica_sets_own_failure_condition_is_reported() -> None:
    condition = {
        "type": "ReplicaFailure",
        "status": "True",
        "reason": "FailedCreate",
        "message": EXCEEDED,
    }
    replica_set = {**REPLICA_SET, "status": {"conditions": [condition]}}

    report = await _explain(FakeReader(replicasets=(replica_set,), events=()))

    assert [(f.subject.kind, f.source.value) for f in report.failures] == [
        ("ReplicaSet", "condition")
    ]


async def test_an_unserved_workload_resource_is_reported_with_the_token_and_not_read() -> None:
    served = {key: value for key, value in _SERVED.items() if key != ("apps", "v1")}
    reader = FakeReader()

    report = await _explain(reader, snapshot=_snapshot(served))

    assert report.workload_observation.state is EvidenceState.UNSUPPORTED
    assert report.workload_observation.token == TOKEN
    assert reader.gets == []


@pytest.mark.parametrize(
    ("resource", "item"),
    [
        ("resourcequotas", {"metadata": {"namespace": "shop"}, "spec": {"hard": {"pods": "0"}}}),
        ("limitranges", {"metadata": {"namespace": "shop"}, "spec": {"limits": []}}),
    ],
)
async def test_a_policy_the_projection_cannot_name_makes_its_list_inconclusive(
    resource: str, item: dict[str, Any]
) -> None:
    report = await _explain(FakeReader(**{resource: (item,)}))

    observation = (
        report.quotas_observation
        if resource == "resourcequotas"
        else report.limit_ranges_observation
    )
    assert _state(observation) == (EvidenceState.PARTIAL, Basis.MALFORMED)
    assert not report.quotas_conclusive(NOW) or resource != "resourcequotas"
    assert not report.limit_ranges_conclusive(NOW) or resource != "limitranges"
    assert report.baseline.state is not EvidenceState.AVAILABLE


async def test_an_unnamed_policy_in_a_truncated_list_keeps_the_lists_basis() -> None:
    unnamed = {"metadata": {"namespace": "shop"}, "spec": {"limits": []}}
    reader = FakeReader(limitranges=_truncated(unnamed))

    report = await _explain(reader)

    assert reader.continuations["limitranges"] == ["", "a"]
    assert _state(report.limit_ranges_observation) == (EvidenceState.PARTIAL, Basis.TRUNCATED)


async def test_an_owned_child_the_projection_cannot_name_makes_the_children_inconclusive() -> None:
    nameless = {
        "metadata": {
            "namespace": "shop",
            "uid": "u",
            "ownerReferences": [{"uid": "uid-api", "controller": True}],
        }
    }

    report = await _explain(FakeReader(replicasets=(REPLICA_SET, nameless)))

    assert _state(report.children_observation) == (EvidenceState.PARTIAL, Basis.MALFORMED)
    assert report.baseline.state is not EvidenceState.AVAILABLE


@pytest.mark.parametrize(
    ("resource", "item"),
    [
        ("resourcequotas", {**QUOTA, "spec": {"hard": {"pods": "0", "services": None}}}),
        ("limitranges", {**LIMIT_RANGE, "spec": {"limits": "x"}}),
    ],
)
async def test_a_policy_with_an_unreadable_constraint_makes_its_list_inconclusive(
    resource: str, item: dict[str, Any]
) -> None:
    report = await _explain(FakeReader(**{resource: (item,)}))

    observation = (
        report.quotas_observation
        if resource == "resourcequotas"
        else report.limit_ranges_observation
    )
    assert _state(observation) == (EvidenceState.PARTIAL, Basis.MALFORMED)


async def test_an_empty_selected_uid_reads_the_workload_without_an_expected_incarnation() -> None:
    reader = FakeReader()

    report = await _explain(reader, CreationTarget("Deployment", "shop", "api", ""))

    assert report.target.uid is None
    assert report.workload is not None
    assert not report.recreated
```

Create `src/korvid/core/creation_constraints.py` with:

```python
"""Explain why a workload's Pods are not being created (#411).

One run reads, in the workload's namespace and under one run token:

- `workload`: the workload, by name, with the selected row's UID;
- `children`: a Deployment's ReplicaSets or a CronJob's Jobs, whose own
  failures are their Pods' failures;
- `events`: the namespace's Events, filtered to `FailedCreate` about the
  workload or its children, matched by UID;
- `quotas` and `limit-ranges`: the namespace's ResourceQuotas and
  LimitRanges.

Every object is projected at once, so no template value leaves the run.
Events can confirm a recorded failure but never rule one out; "no quota" or
"no LimitRange" may be concluded only from a conclusive list.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from korvid.core.api_versions import (
    EVENTS,
    Candidate,
    DiscoverySnapshot,
    LogicalSource,
    Resolution,
    resolve,
)
from korvid.core.capability import (
    Baseline,
    Basis,
    Declaration,
    EvidenceSet,
    EvidenceState,
    Observation,
    Role,
    RunToken,
    SourceScope,
    Verb,
    conclusive,
    downgrade_stale_status,
)
from korvid.core.creation_failures import Failure, Subject, event_failures
from korvid.core.creation_policies import (
    LimitRangeProjection,
    QuotaProjection,
    project_limit_ranges,
    project_quotas,
)
from korvid.core.creation_workloads import (
    WORKLOAD_KINDS,
    ChildProjection,
    WorkloadProjection,
    project_children,
    project_workload,
)
from korvid.core.evidence_reads import (
    LIST_PAGE_SIZE,
    MAX_BYTES,
    MAX_DEADLINE,
    MAX_PAGES,
    read_logical_list,
    read_object,
)
from korvid.k8s.evidence_requests import EvidenceReader

QUOTAS = LogicalSource("quotas", (Candidate("", "resourcequotas", ("v1",)),))
LIMIT_RANGES = LogicalSource("limit-ranges", (Candidate("", "limitranges", ("v1",)),))
_CHILD_RESOURCES = {"ReplicaSet": ("apps", "replicasets"), "Job": ("batch", "jobs")}


def _workload_source(kind: str) -> LogicalSource:
    workload = WORKLOAD_KINDS[kind]
    candidate = Candidate(workload.group, workload.resource, (workload.version,))
    return LogicalSource("workload", (candidate,))


def _children_source(child: str) -> LogicalSource:
    group, resource = _CHILD_RESOURCES[child]
    return LogicalSource("children", (Candidate(group, resource, ("v1",)),))


@dataclass(frozen=True, slots=True)
class CreationTarget:
    """The selected workload, with the UID the selection showed; an empty UID is none."""

    kind: str
    namespace: str
    name: str
    uid: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in WORKLOAD_KINDS or not self.namespace or not self.name:
            raise ValueError("a target is a named workload of a supported kind, in a namespace")
        if not self.uid:
            object.__setattr__(self, "uid", None)

    @property
    def child(self) -> str | None:
        """The kind of child that records this workload's Pods' failures, if any."""
        return WORKLOAD_KINDS[self.kind].child


def declarations(target: CreationTarget) -> tuple[Declaration, ...]:
    """The sources a run reads, all required.

    The workload, its children and the Events `CONFIRMS`: only what they
    show matters. The quota and LimitRange lists `RULES_OUT`: "none in this
    namespace" rests on their completeness.
    """
    sources = [_workload_source(target.kind).declaration(required=True, role=Role.CONFIRMS)]
    if target.child is not None:
        children = _children_source(target.child)
        sources.append(children.declaration(required=True, role=Role.CONFIRMS))
    sources += [
        EVENTS.declaration(required=True, role=Role.CONFIRMS),
        QUOTAS.declaration(required=True, role=Role.RULES_OUT),
        LIMIT_RANGES.declaration(required=True, role=Role.RULES_OUT),
    ]
    return tuple(sources)


@dataclass(frozen=True, slots=True)
class CreationReport:
    """What one run established about the workload and its namespace's policies."""

    target: CreationTarget
    workload_observation: Observation
    children_observation: Observation | None
    events_observation: Observation
    quotas_observation: Observation
    limit_ranges_observation: Observation
    workload: WorkloadProjection | None
    children: tuple[ChildProjection, ...]
    failures: tuple[Failure, ...]
    earlier_failures: int
    quotas: tuple[QuotaProjection, ...]
    limit_ranges: tuple[LimitRangeProjection, ...]
    baseline: Baseline

    @property
    def recreated(self) -> bool:
        """Whether the workload read is another incarnation than the one selected."""
        return self.workload_observation.recreated

    def quotas_conclusive(self, now: datetime) -> bool:
        """Whether `quotas` is every quota in the namespace, so their absence is a fact."""
        return conclusive(self.quotas_observation, QUOTAS.policy, now)

    def limit_ranges_conclusive(self, now: datetime) -> bool:
        """Whether `limit_ranges` is every LimitRange in the namespace."""
        return conclusive(self.limit_ranges_observation, LIMIT_RANGES.policy, now)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _Lists:
    items: tuple[dict[str, Any], ...]
    observation: Observation


async def _list(
    reader: EvidenceReader,
    resolution: Resolution,
    token: RunToken | None,
    clock: Callable[[], datetime],
    *,
    maximum: bool = False,
) -> _Lists:
    budgets: dict[str, Any] = {}
    if maximum:
        budgets = {
            "page_size": LIST_PAGE_SIZE,
            "max_pages": MAX_PAGES,
            "deadline": MAX_DEADLINE,
            "max_bytes": MAX_BYTES,
        }
    evidence = await read_logical_list(reader, resolution, token=token, clock=clock, **budgets)
    return _Lists(evidence.items, evidence.observation)


async def _get(
    reader: EvidenceReader,
    resolution: Resolution,
    token: RunToken | None,
    clock: Callable[[], datetime],
) -> tuple[dict[str, Any] | None, Observation]:
    if not resolution.sources:
        observation = resolution.outcomes[0].observation
        if observation is None:
            raise ValueError("an unresolved resolution records why")
        return None, replace(observation, token=token)
    evidence = await read_object(reader, resolution.sources[0], token=token, clock=clock)
    return evidence.item, evidence.observation


async def _no_children() -> None:
    return None


def _downgrade_unreadable(observation: Observation, unreadable: int, what: str) -> Observation:
    """An available list with items the projection could not read in full is `PARTIAL`.

    Otherwise a list of only unnamed policies, or a policy whose constraints
    were dropped, would rule out what it could not show. A list already
    partial keeps its own, already inconclusive, basis.
    """
    if not unreadable or observation.state is not EvidenceState.AVAILABLE:
        return observation
    detail = f"{unreadable} {what} could not be read in full"
    return replace(observation, state=EvidenceState.PARTIAL, basis=Basis.MALFORMED, detail=detail)


def _unreadable(read: int, projections: tuple[QuotaProjection | LimitRangeProjection, ...]) -> int:
    """How many listed policies were dropped, or kept with a constraint that could not be read."""
    return read - len(projections) + sum(not p.complete for p in projections)


def _downgrade_stale(observation: Observation, quotas: tuple[QuotaProjection, ...]) -> Observation:
    """An available quota list whose status lags is `PARTIAL`/`STALE_STATUS`.

    A list already partial keeps its own, already inconclusive, basis.
    """
    stale = [quota.name for quota in quotas if quota.stale]
    if not stale or observation.state is not EvidenceState.AVAILABLE:
        return observation
    return downgrade_stale_status(observation, f"quota status not yet updated: {', '.join(stale)}")


def _subjects(
    target: CreationTarget,
    workload: WorkloadProjection | None,
    observation: Observation,
) -> tuple[Subject, ...]:
    """Whom failures can be about: the workload read, or the one selected if it was not read."""
    if workload is not None:
        return (workload.subject,)
    if observation.basis is Basis.OBJECT_ABSENT or target.uid is None:
        return ()
    return (Subject(target.kind, target.name, target.uid),)


def _failures(
    events: _Lists,
    subjects: tuple[Subject, ...],
    workload: WorkloadProjection | None,
    children: tuple[ChildProjection, ...],
) -> tuple[tuple[Failure, ...], int]:
    recorded, earlier = event_failures(events.items, subjects)
    conditions = [workload.failure if workload is not None else None]
    conditions += [child.failure for child in children]
    merged = [*recorded, *(failure for failure in conditions if failure is not None)]
    merged.sort(key=lambda f: (f.at is None, -f.at.timestamp() if f.at is not None else 0.0))
    return tuple(merged), earlier


async def explain_creation(
    reader: EvidenceReader,
    snapshot: DiscoverySnapshot,
    target: CreationTarget,
    *,
    token: RunToken | None = None,
    clock: Callable[[], datetime] = _now,
) -> CreationReport:
    """Read the workload, its children, the Events and the policies, together, and report.

    Sources are resolved from `snapshot`, so a source the cluster does not
    serve is reported, not read. A source that fails does not stop the others.
    """
    scope = SourceScope.namespace(target.namespace)
    workload_resolution = resolve(
        _workload_source(target.kind),
        Verb.GET,
        snapshot,
        scope=scope,
        name=target.name,
        expected_uid=target.uid,
    )
    children_read = (
        _list(
            reader,
            resolve(_children_source(target.child), Verb.LIST, snapshot, scope=scope),
            token,
            clock,
            maximum=True,
        )
        if target.child is not None
        else _no_children()
    )
    (
        (item, workload_observation),
        children_lists,
        events,
        quotas,
        limit_ranges,
    ) = await asyncio.gather(
        _get(reader, workload_resolution, token, clock),
        children_read,
        _list(
            reader, resolve(EVENTS, Verb.LIST, snapshot, scope=scope), token, clock, maximum=True
        ),
        _list(reader, resolve(QUOTAS, Verb.LIST, snapshot, scope=scope), token, clock),
        _list(reader, resolve(LIMIT_RANGES, Verb.LIST, snapshot, scope=scope), token, clock),
    )
    workload = project_workload(target.kind, item) if item is not None else None
    subjects = _subjects(target, workload, workload_observation)
    children: tuple[ChildProjection, ...] = ()
    children_observation = children_lists.observation if children_lists is not None else None
    if children_lists is not None and subjects and target.child is not None:
        projected = project_children(target.child, children_lists.items, subjects[0].uid)
        children = projected.children
        children_observation = _downgrade_unreadable(
            children_lists.observation, projected.unprojected, f"{target.child}s"
        )
    failures, earlier = _failures(
        events, subjects + tuple(child.subject for child in children), workload, children
    )
    quota_projections = project_quotas(quotas.items)
    quotas_observation = _downgrade_stale(
        _downgrade_unreadable(
            quotas.observation,
            _unreadable(len(quotas.items), quota_projections),
            "ResourceQuotas",
        ),
        quota_projections,
    )
    range_projections = project_limit_ranges(limit_ranges.items)
    limit_ranges_observation = _downgrade_unreadable(
        limit_ranges.observation,
        _unreadable(len(limit_ranges.items), range_projections),
        "LimitRanges",
    )
    observations = {
        "workload": workload_observation,
        "events": events.observation,
        "quotas": quotas_observation,
        "limit-ranges": limit_ranges_observation,
    }
    if children_observation is not None:
        observations["children"] = children_observation
    baseline = EvidenceSet(observations).baseline(declarations(target), clock())
    return CreationReport(
        target,
        workload_observation,
        children_observation,
        events.observation,
        quotas_observation,
        limit_ranges_observation,
        workload,
        children,
        failures,
        earlier,
        quota_projections,
        range_projections,
        baseline,
    )
```

- [ ] Add the five modules to `test_base_import_does_not_require_optional_extras` in `tests/test_optional_extras.py`.
- [ ] Run `uv run --frozen pytest -p no:tach tests/core/test_creation_constraints.py tests/test_optional_extras.py -q` and `uv run --frozen tach check`. Expected: PASS.
- [ ] Commit: `Read a workload's creation failures next to its namespace's policies`.

### Task 7: Run the full gate

- [ ] Run `make check`, or behind the mirror the same chain with the venv's tools: `scripts/check_source_size.py`, `ruff check`, `ruff format --check`, `mypy src/`, `tach check`, `pytest`.
- [ ] Mutation-check each new condition. Every mutation must fail a test, unless it is equivalent; record those in the pull request. The conditions are:
  - the `>=` limit comparison and the stale-status rules;
  - the bool and whole-number guards;
  - the classifier's ordering and bounds;
  - the UID matching and earlier-incarnation count;
  - the `ReplicaFailure` filter;
  - the child controller check;
  - the subject fallback;
  - the stale downgrade's `AVAILABLE` precondition.
