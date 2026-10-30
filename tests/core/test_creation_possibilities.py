"""Tests for the possible creation constraints of a workload's next Pod (#411 Q2)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from korvid.core.capability import (
    ApiSource,
    Baseline,
    Basis,
    EvidenceState,
    Observation,
    SourceScope,
    Verb,
)
from korvid.core.creation_constraints import CreationReport, CreationTarget
from korvid.core.creation_failures import Subject
from korvid.core.creation_policies import (
    LimitRangeProjection,
    LimitValue,
    QuotaEntry,
    QuotaProjection,
    ScopeExpression,
)
from korvid.core.creation_possibilities import Finding, Possibility, possible_constraints
from korvid.core.creation_quantities import Usage, quota_group
from korvid.core.creation_workloads import (
    ClaimTemplate,
    ContainerDemand,
    PodTemplate,
    WorkloadProjection,
)

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
SCOPE = SourceScope.namespace("shop")
APP = ContainerDemand(
    "app", "container", (("cpu", "500m"), ("memory", "256Mi")), (("cpu", "1"), ("memory", "512Mi"))
)


def _seen(resource: str, basis: Basis = Basis.OBSERVED) -> Observation:
    state = EvidenceState.AVAILABLE if basis is Basis.OBSERVED else EvidenceState.PARTIAL
    return Observation(ApiSource("", resource, SCOPE, "v1", Verb.LIST), state, basis, NOW)


def _quota(name: str, entries: dict[str, tuple[str, str]], **fields: Any) -> QuotaProjection:
    rows = tuple(
        QuotaEntry(key, quota_group(key), hard, used, Usage.BELOW)
        for key, (used, hard) in entries.items()
    )
    return QuotaProjection(name, f"uid-{name}", rows, **fields)


def _report(
    template: PodTemplate | None = None,
    *,
    quotas: tuple[QuotaProjection, ...] = (),
    limit_ranges: tuple[LimitRangeProjection, ...] = (),
    kind: str = "Deployment",
    quotas_basis: Basis = Basis.OBSERVED,
    limits_basis: Basis = Basis.OBSERVED,
) -> CreationReport:
    workload = None
    if template is not None:
        workload = WorkloadProjection(Subject(kind, "api", "uid-api"), (), None, template)
    observation = Observation(
        ApiSource("apps", "deployments", SCOPE, "v1", Verb.GET, name="api"),
        EvidenceState.AVAILABLE,
        Basis.OBSERVED,
        NOW,
        observed_uid="uid-api",
    )
    return CreationReport(
        CreationTarget(kind, "shop", "api"),
        observation,
        None,
        _seen("events"),
        _seen("resourcequotas", quotas_basis),
        _seen("limitranges", limits_basis),
        workload,
        (),
        (),
        0,
        quotas,
        limit_ranges,
        Baseline(EvidenceState.AVAILABLE),
    )


def _template(*containers: ContainerDemand, **fields: Any) -> PodTemplate:
    return PodTemplate(containers or (APP,), (), **fields)


def _findings(report: CreationReport) -> list[tuple[str, Finding, str]]:
    possibilities = possible_constraints(report, NOW)
    assert possibilities is not None
    return [(p.subject, p.finding, p.detail) for p in possibilities.rows]


def test_without_a_template_there_is_nothing_to_compute() -> None:
    assert possible_constraints(_report(None), NOW) is None


def test_a_clean_template_in_a_conclusive_namespace_is_unconstrained() -> None:
    quota = _quota("compute", {"requests.cpu": ("1", "4"), "pods": ("3", "10")})

    possibilities = possible_constraints(_report(_template(), quotas=(quota,)), NOW)

    assert possibilities is not None
    assert (possibilities.rows, possibilities.nothing_constrains) == ((), True)


@pytest.mark.parametrize(
    ("used", "hard", "expected"),
    [
        ("1.5", "2", []),
        ("1.6", "2", [("requests.cpu", Finding.WOULD_EXCEED, "needs 500m; 400m remaining")]),
        ("2", "2", [("requests.cpu", Finding.WOULD_EXCEED, "needs 500m; none remaining")]),
    ],
    ids=["exactly-fits", "just-over", "at-the-limit"],
)
def test_a_resource_the_pod_would_take_past_the_limit_would_exceed(
    used: str, hard: str, expected: list[tuple[str, Finding, str]]
) -> None:
    quota = _quota("compute", {"requests.cpu": (used, hard)})

    assert _findings(_report(_template(), quotas=(quota,))) == expected


def test_memory_remaining_is_shown_in_the_limits_unit_family() -> None:
    quota = _quota("compute", {"limits.memory": ("7.75Gi", "8Gi")})

    assert _findings(_report(_template(), quotas=(quota,))) == [
        ("limits.memory", Finding.WOULD_EXCEED, "needs 512Mi; 256Mi remaining")
    ]


def test_a_pod_count_at_its_limit_is_said() -> None:
    quota = _quota(
        "objects", {"pods": ("10", "10"), "count/pods": ("3", "10"), "services": ("5", "5")}
    )

    assert _findings(_report(_template(), quotas=(quota,))) == [
        ("pods", Finding.AT_LIMIT, "10 of 10 Pods exist")
    ]


def test_a_container_without_a_tracked_field_requires_it() -> None:
    bare = ContainerDemand("sidecar", "container", (), ())
    quota = _quota("compute", {"limits.cpu": ("0", "8"), "requests.hugepages-2Mi": ("0", "1Gi")})

    assert _findings(_report(_template(APP, bare), quotas=(quota,))) == [
        ("limits.cpu", Finding.REQUIRES_FIELD, "sidecar does not set it")
    ]


def test_a_pod_level_value_may_supply_a_required_field() -> None:
    bare = ContainerDemand("app", "container", (), ())
    quota = _quota("compute", {"limits.cpu": ("0", "8")})

    rows = _findings(_report(_template(bare, pod_limits=(("cpu", "2"),)), quotas=(quota,)))

    assert rows == [
        (
            "limits.cpu",
            Finding.NOT_ESTABLISHED,
            "Pod-level resources are set; whether the cluster accepts them in place of each "
            "container's is not observed",
        )
    ]


def test_an_unknown_demand_is_not_established_and_blocks_the_conclusion() -> None:
    quota = _quota("compute", {"requests.cpu": ("0", "8")})

    possibilities = possible_constraints(
        _report(_template(runtime_class="kata"), quotas=(quota,)), NOW
    )

    assert possibilities is not None
    assert [(p.subject, p.finding) for p in possibilities.rows] == [
        ("requests.cpu", Finding.NOT_ESTABLISHED)
    ]
    assert not possibilities.nothing_constrains


def test_a_runtime_class_alone_blocks_the_conclusion() -> None:
    possibilities = possible_constraints(_report(_template(runtime_class="kata")), NOW)

    assert possibilities is not None
    assert (possibilities.rows, possibilities.nothing_constrains) == ((), False)


def test_a_stale_or_unreadable_quota_is_not_established() -> None:
    stale = _quota("stale", {"requests.cpu": ("0", "8")}, stale=True)
    broken = _quota("broken", {}, complete=False)

    rows = _findings(_report(_template(), quotas=(stale, broken)))

    assert rows == [
        ("quota", Finding.NOT_ESTABLISHED, "its status is not yet updated"),
        ("quota", Finding.NOT_ESTABLISHED, "its constraints could not be read"),
    ]


@pytest.mark.parametrize(
    ("fields", "template_fields", "applies"),
    [
        ({"scopes": ("Terminating",)}, {"active_deadline_seconds": 60}, True),
        ({"scopes": ("Terminating",)}, {}, False),
        ({"scopes": ("NotTerminating",)}, {}, True),
        ({"scopes": ("NotTerminating",)}, {"active_deadline_seconds": 60}, False),
        ({"scopes": ("BestEffort",)}, {}, False),
        ({"scopes": ("NotBestEffort",)}, {}, True),
        ({"scopes": ("PriorityClass",)}, {"priority_class": "gold"}, True),
        (
            {"scope_selector": (ScopeExpression("PriorityClass", "In", ("gold",)),)},
            {"priority_class": "gold"},
            True,
        ),
        (
            {"scope_selector": (ScopeExpression("PriorityClass", "In", ("gold",)),)},
            {"priority_class": "low"},
            False,
        ),
        (
            {"scope_selector": (ScopeExpression("PriorityClass", "NotIn", ("gold",)),)},
            {"priority_class": "low"},
            True,
        ),
        (
            {"scope_selector": (ScopeExpression("PriorityClass", "Exists", ()),)},
            {"priority_class": "low"},
            True,
        ),
        (
            {"scope_selector": (ScopeExpression("PriorityClass", "DoesNotExist", ()),)},
            {"priority_class": "low"},
            False,
        ),
        ({"scope_selector": (ScopeExpression("BestEffort", "DoesNotExist", ()),)}, {}, True),
        ({"scope_selector": (ScopeExpression("Terminating", "Exists", ()),)}, {}, False),
    ],
)
def test_a_quota_applies_only_when_every_scope_does(
    fields: dict[str, Any], template_fields: dict[str, Any], applies: bool
) -> None:
    quota = _quota("scoped", {"pods": ("10", "10")}, **fields)

    rows = _findings(_report(_template(**template_fields), quotas=(quota,)))

    assert rows == ([("pods", Finding.AT_LIMIT, "10 of 10 Pods exist")] if applies else [])


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        (
            {"scope_selector": (ScopeExpression("PriorityClass", "In", ("gold",)),)},
            "the Pod sets no priorityClassName, and a cluster default may apply",
        ),
        (
            {"scopes": ("PriorityClass",)},
            "the Pod sets no priorityClassName, and a cluster default may apply",
        ),
        (
            {"scopes": ("CrossNamespacePodAffinity",)},
            "scope CrossNamespacePodAffinity is not checked",
        ),
        ({"scopes": ("Mystery",)}, "scope Mystery is not checked"),
        (
            {"scope_selector": (ScopeExpression("PriorityClass", "Gt", ("1",)),)},
            "operator Gt is not checked",
        ),
    ],
)
def test_an_applicability_korvid_cannot_decide_is_not_established(
    fields: dict[str, Any], reason: str
) -> None:
    quota = _quota("scoped", {"pods": ("10", "10")}, **fields)

    rows = _findings(_report(_template(), quotas=(quota,)))

    assert rows == [("quota", Finding.NOT_ESTABLISHED, f"whether it applies: {reason}")]


def test_best_effort_applicability_waits_on_an_unknown_qos() -> None:
    bare = ContainerDemand("app", "container", (), ())
    conflicting = (
        LimitRangeProjection("x", "u1", (LimitValue("Container", "cpu", default_request="1"),)),
        LimitRangeProjection("y", "u2", (LimitValue("Container", "cpu", default_request="2"),)),
    )
    quota = _quota("best", {"pods": ("10", "10")}, scopes=("BestEffort",))

    rows = _findings(_report(_template(bare), quotas=(quota,), limit_ranges=conflicting))

    assert (
        "quota",
        Finding.NOT_ESTABLISHED,
        "whether it applies: the Pod's QoS class is not established",
    ) in rows


def _range(*values: LimitValue) -> LimitRangeProjection:
    return LimitRangeProjection("limits", "uid-lr", values)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            LimitValue("Container", "cpu", min="600m"),
            (Finding.BELOW_MINIMUM, "app: request 500m is below 600m"),
        ),
        (
            LimitValue("Container", "cpu", max="800m"),
            (Finding.ABOVE_MAXIMUM, "app: limit 1 is above 800m"),
        ),
        (
            LimitValue("Container", "memory", max_limit_request_ratio="1.5"),
            (Finding.ABOVE_RATIO, "app: limit 512Mi is more than 1.5 times the request 256Mi"),
        ),
        (
            LimitValue("Container", "ephemeral-storage", min="1Gi"),
            (Finding.BELOW_MINIMUM, "app: no request is set"),
        ),
        (
            LimitValue("Container", "ephemeral-storage", max="1Gi"),
            (Finding.ABOVE_MAXIMUM, "app: no limit is set"),
        ),
        (
            LimitValue("Container", "ephemeral-storage", max_limit_request_ratio="2"),
            (Finding.ABOVE_RATIO, "app: needs both a request and a limit"),
        ),
        (
            LimitValue("Pod", "cpu", max="900m"),
            (Finding.ABOVE_MAXIMUM, "Pod: limit 1 is above 900m"),
        ),
        (
            LimitValue("Pod", "memory", min="512Mi"),
            (Finding.BELOW_MINIMUM, "Pod: request 256Mi is below 512Mi"),
        ),
    ],
)
def test_limit_range_constraints_are_checked_per_container_and_per_pod(
    value: LimitValue, expected: tuple[Finding, str]
) -> None:
    rows = _findings(_report(_template(), limit_ranges=(_range(value),)))

    assert [(finding, detail) for _, finding, detail in rows] == [expected]
    assert rows[0][0] == value.resource


def test_satisfied_limit_ranges_say_nothing() -> None:
    at_the_bounds = _range(
        LimitValue("Container", "cpu", min="500m", max="1", max_limit_request_ratio="2"),
        LimitValue("Container", "memory", max_limit_request_ratio="2"),
        LimitValue("Pod", "memory", max="1Gi"),
    )

    assert _findings(_report(_template(), limit_ranges=(at_the_bounds,))) == []


def test_an_unknown_container_value_leaves_its_limit_range_check_not_established() -> None:
    odd = ContainerDemand("app", "container", (("cpu", "x"),), (("cpu", "1"),))

    rows = _findings(
        _report(_template(odd), limit_ranges=(_range(LimitValue("Container", "cpu", min="1")),))
    )

    assert rows == [("cpu", Finding.NOT_ESTABLISHED, "app: its value is not established")]


def _stateful(*claims: ClaimTemplate) -> PodTemplate:
    return _template(claims=claims)


def test_a_new_claim_is_checked_against_storage_quotas_conditionally() -> None:
    quota = _quota(
        "storage",
        {
            "requests.storage": ("95Gi", "100Gi"),
            "persistentvolumeclaims": ("9", "10"),
            "gold.storageclass.storage.k8s.io/requests.storage": ("0", "1Ti"),
            "silver.storageclass.storage.k8s.io/requests.storage": ("10Gi", "10Gi"),
        },
    )

    possibilities = possible_constraints(
        _report(
            _stateful(ClaimTemplate("data", "10Gi", "gold")), quotas=(quota,), kind="StatefulSet"
        ),
        NOW,
    )

    assert possibilities is not None
    assert [(p.subject, p.finding, p.detail, p.conditional) for p in possibilities.rows] == [
        ("claim data: requests.storage", Finding.WOULD_EXCEED, "needs 10Gi; 5Gi remaining", True)
    ]


def test_a_claim_without_a_storage_class_leaves_per_class_quotas_not_established() -> None:
    quota = _quota(
        "storage", {"gold.storageclass.storage.k8s.io/persistentvolumeclaims": ("0", "1")}
    )
    omitted = _report(
        _stateful(ClaimTemplate("data", "1Gi", None)), quotas=(quota,), kind="StatefulSet"
    )
    empty = _report(
        _stateful(ClaimTemplate("data", "1Gi", "")), quotas=(quota,), kind="StatefulSet"
    )

    omitted_result = possible_constraints(omitted, NOW)
    empty_result = possible_constraints(empty, NOW)

    assert omitted_result is not None
    assert empty_result is not None
    assert [(p.finding, p.detail) for p in omitted_result.rows] == [
        (
            Finding.NOT_ESTABLISHED,
            "the claim sets no storage class, and the cluster default may apply",
        )
    ]
    assert not omitted_result.nothing_constrains
    assert (empty_result.rows, empty_result.nothing_constrains) == ((), True)


def test_a_claim_is_checked_against_persistent_volume_claim_limit_ranges() -> None:
    limits = _range(LimitValue("PersistentVolumeClaim", "storage", max="5Gi"))

    rows = _findings(
        _report(
            _stateful(ClaimTemplate("data", "10Gi", "gold")),
            limit_ranges=(limits,),
            kind="StatefulSet",
        )
    )

    assert rows == [("claim data: storage", Finding.ABOVE_MAXIMUM, "request 10Gi is above 5Gi")]


def test_an_inconclusive_policy_list_blocks_the_conclusion() -> None:
    possibilities = possible_constraints(_report(_template(), quotas_basis=Basis.TRUNCATED), NOW)

    assert possibilities is not None
    assert (possibilities.rows, possibilities.nothing_constrains) == ((), False)
    later = possible_constraints(_report(_template()), NOW + timedelta(minutes=5))
    assert later is not None
    assert not later.nothing_constrains


def test_each_row_names_its_policy() -> None:
    quota = _quota("compute", {"pods": ("10", "10")})

    possibilities = possible_constraints(_report(_template(), quotas=(quota,)), NOW)

    assert possibilities is not None
    assert possibilities.rows == (
        Possibility(
            "ResourceQuota",
            "compute",
            "uid-compute",
            "pods",
            Finding.AT_LIMIT,
            "10 of 10 Pods exist",
        ),
    )


def test_defaults_applied_is_reported() -> None:
    bare = ContainerDemand("app", "container", (), ())
    defaults = _range(LimitValue("Container", "cpu", default="1", default_request="1"))

    possibilities = possible_constraints(_report(_template(bare), limit_ranges=(defaults,)), NOW)

    assert possibilities is not None
    assert possibilities.defaults_applied
    assert replace(possibilities, defaults_applied=False).rows == possibilities.rows


def test_several_containers_missing_a_field_are_named_together() -> None:
    bare = (ContainerDemand("a", "container", (), ()), ContainerDemand("b", "container", (), ()))
    quota = _quota("compute", {"requests.memory": ("0", "8Gi")})

    assert _findings(_report(_template(*bare), quotas=(quota,))) == [
        ("requests.memory", Finding.REQUIRES_FIELD, "a, b do not set it")
    ]


def test_a_resource_the_pod_does_not_use_is_never_a_constraint() -> None:
    quota = _quota("extended", {"requests.nvidia.com/gpu": ("6", "5")})

    assert _findings(_report(_template(), quotas=(quota,))) == []


def test_a_zero_request_cannot_satisfy_a_ratio() -> None:
    zero = ContainerDemand("app", "container", (("cpu", "0"),), (("cpu", "1"),))
    ratio = _range(LimitValue("Container", "cpu", max_limit_request_ratio="4"))

    rows = _findings(_report(_template(zero), limit_ranges=(ratio,)))

    assert rows == [("cpu", Finding.ABOVE_RATIO, "app: needs both a request and a limit")]


def test_scoped_quotas_and_container_limits_do_not_apply_to_claims() -> None:
    scoped = _quota("scoped", {"requests.storage": ("100Gi", "100Gi")}, scopes=("NotTerminating",))
    container = _range(LimitValue("Container", "storage", max="1Gi"))

    possibilities = possible_constraints(
        _report(
            _stateful(ClaimTemplate("data", "10Gi", "gold")),
            quotas=(scoped,),
            limit_ranges=(container,),
            kind="StatefulSet",
        ),
        NOW,
    )

    assert possibilities is not None
    assert not [p for p in possibilities.rows if p.subject.startswith("claim")]


def test_a_claim_without_a_storage_class_blocks_the_conclusion_on_its_own() -> None:
    possibilities = possible_constraints(
        _report(_stateful(ClaimTemplate("data", "1Gi", None)), kind="StatefulSet"), NOW
    )

    assert possibilities is not None
    assert (possibilities.rows, possibilities.nothing_constrains) == ((), False)


def test_an_inconclusive_limit_range_list_blocks_the_conclusion() -> None:
    possibilities = possible_constraints(_report(_template(), limits_basis=Basis.TRUNCATED), NOW)

    assert possibilities is not None
    assert (possibilities.rows, possibilities.nothing_constrains) == ((), False)


def test_a_ratio_is_explained_without_rounding() -> None:
    close = ContainerDemand("app", "container", (("cpu", "1"),), (("cpu", "1.0001"),))
    ratio = _range(LimitValue("Container", "cpu", max_limit_request_ratio="1"))

    rows = _findings(_report(_template(close), limit_ranges=(ratio,)))

    assert rows == [
        ("cpu", Finding.ABOVE_RATIO, "app: limit 1000100u is more than 1 times the request 1")
    ]


def test_an_unreadable_usage_is_not_established_and_blocks_the_conclusion() -> None:
    quota = _quota("compute", {"requests.cpu": ("lots", "8")})

    possibilities = possible_constraints(_report(_template(), quotas=(quota,)), NOW)

    assert possibilities is not None
    assert [(p.subject, p.finding, p.detail) for p in possibilities.rows] == [
        ("requests.cpu", Finding.NOT_ESTABLISHED, "its used or hard amount could not be read")
    ]
    assert not possibilities.nothing_constrains


@pytest.mark.parametrize(
    ("value", "name"),
    [
        (LimitValue("Container", "cpu", max="lots"), "max"),
        (LimitValue("Container", "cpu", min="lots"), "min"),
        (LimitValue("Container", "cpu", max_limit_request_ratio="lots"), "maxLimitRequestRatio"),
    ],
)
def test_an_unreadable_limit_range_bound_is_not_established(value: LimitValue, name: str) -> None:
    rows = _findings(_report(_template(), limit_ranges=(_range(value),)))

    assert rows == [("cpu", Finding.NOT_ESTABLISHED, f"app: its {name} bound could not be read")]


def test_a_claim_count_quota_charges_one_claim() -> None:
    quota = _quota("objects", {"count/persistentvolumeclaims": ("10", "10")})

    possibilities = possible_constraints(
        _report(
            _stateful(ClaimTemplate("data", "1Gi", "gold")), quotas=(quota,), kind="StatefulSet"
        ),
        NOW,
    )

    assert possibilities is not None
    assert [(p.subject, p.finding, p.conditional) for p in possibilities.rows] == [
        ("claim data: count/persistentvolumeclaims", Finding.AT_LIMIT, True)
    ]


def test_an_unknown_claim_request_leaves_its_storage_checks_open() -> None:
    quota = _quota(
        "storage", {"requests.storage": ("0", "100Gi"), "persistentvolumeclaims": ("10", "10")}
    )
    limits = _range(LimitValue("PersistentVolumeClaim", "storage", max="5Gi", min="lots"))

    possibilities = possible_constraints(
        _report(
            _stateful(ClaimTemplate("data", None, "gold")),
            quotas=(quota,),
            limit_ranges=(limits,),
            kind="StatefulSet",
        ),
        NOW,
    )

    assert possibilities is not None
    assert [(p.subject, p.finding, p.detail) for p in possibilities.rows] == [
        (
            "claim data: requests.storage",
            Finding.NOT_ESTABLISHED,
            "the claim's storage request is not established",
        ),
        ("claim data: persistentvolumeclaims", Finding.AT_LIMIT, "10 of 10 claims exist"),
        (
            "claim data: storage",
            Finding.NOT_ESTABLISHED,
            "the claim's storage request is not established",
        ),
    ]


def test_an_unreadable_claim_limit_bound_is_not_established() -> None:
    limits = _range(LimitValue("PersistentVolumeClaim", "storage", max="lots"))

    rows = _findings(
        _report(
            _stateful(ClaimTemplate("data", "1Gi", "gold")),
            limit_ranges=(limits,),
            kind="StatefulSet",
        )
    )

    assert rows == [
        ("claim data: storage", Finding.NOT_ESTABLISHED, "its max bound could not be read")
    ]


def test_limit_ranges_not_read_leave_fields_and_growing_demand_open() -> None:
    bare = ContainerDemand("sidecar", "container", (), ())
    quota = _quota(
        "compute",
        {
            "limits.cpu": ("0", "8"),
            "requests.memory": ("8000Mi", "8Gi"),
            "requests.ephemeral-storage": ("0", "1Gi"),
        },
    )

    rows = _findings(_report(_template(APP, bare), quotas=(quota,), limits_basis=Basis.FORBIDDEN))

    assert rows == [
        ("limits.cpu", Finding.NOT_ESTABLISHED, "a LimitRange that could not be read may set it"),
        # Already over with what is known: an unseen default can only add to it.
        ("requests.memory", Finding.WOULD_EXCEED, "needs at least 256Mi; 192Mi remaining"),
        (
            "requests.ephemeral-storage",
            Finding.NOT_ESTABLISHED,
            "a default from a LimitRange that could not be read may add to the Pod's demand",
        ),
    ]


def test_an_unreadable_claim_quota_usage_is_not_established() -> None:
    quota = _quota("storage", {"requests.storage": ("lots", "100Gi")})

    rows = _findings(
        _report(
            _stateful(ClaimTemplate("data", "1Gi", "gold")), quotas=(quota,), kind="StatefulSet"
        )
    )

    assert rows == [
        (
            "claim data: requests.storage",
            Finding.NOT_ESTABLISHED,
            "its used or hard amount could not be read",
        )
    ]


def test_a_zero_limit_cannot_satisfy_a_pod_ratio() -> None:
    unlimited = ContainerDemand("a", "container", (("cpu", "1"),), ())
    zero = ContainerDemand("b", "container", (("cpu", "0"),), (("cpu", "0"),))
    ratio = _range(LimitValue("Pod", "cpu", max_limit_request_ratio="4"))

    rows = _findings(_report(_template(unlimited, zero), limit_ranges=(ratio,)))

    assert rows == [("cpu", Finding.ABOVE_RATIO, "Pod: needs both a request and a limit")]


def test_any_pod_level_cpu_or_memory_leaves_every_required_field_open() -> None:
    bare = ContainerDemand("app", "container", (), ())
    quota = _quota("compute", {"requests.memory": ("0", "8Gi")})

    rows = _findings(_report(_template(bare, pod_requests=(("cpu", "1"),)), quotas=(quota,)))

    assert rows == [
        (
            "requests.memory",
            Finding.NOT_ESTABLISHED,
            "Pod-level resources are set; whether the cluster accepts them in place of each "
            "container's is not observed",
        )
    ]


def test_limit_range_checks_wait_on_defaults_not_read_but_keep_provable_violations() -> None:
    bare = ContainerDemand("sidecar", "container", (), ())
    limits = _range(
        LimitValue("Container", "cpu", max="800m"),
        LimitValue("Pod", "cpu", min="2"),
    )

    rows = _findings(
        _report(_template(APP, bare), limit_ranges=(limits,), limits_basis=Basis.TRUNCATED)
    )

    assert rows == [
        ("cpu", Finding.ABOVE_MAXIMUM, "app: limit 1 is above 800m"),
        (
            "cpu",
            Finding.NOT_ESTABLISHED,
            "sidecar: a default from a LimitRange that could not be read may set it",
        ),
        (
            "cpu",
            Finding.NOT_ESTABLISHED,
            "Pod: a default from a LimitRange that could not be read may change it",
        ),
    ]


_SIDECAR_UNSEEN = "sidecar: a default from a LimitRange that could not be read may set it"
_POD_UNSEEN = "Pod: a default from a LimitRange that could not be read may change it"
_WIDE = ContainerDemand("app", "container", (("cpu", "100m"),), (("cpu", "1"),))


@pytest.mark.parametrize(
    ("app", "value", "detail"),
    [
        # A missing request may yet come from a default.
        (None, LimitValue("Container", "cpu", min="100m"), _SIDECAR_UNSEEN),
        (None, LimitValue("Container", "cpu", max_limit_request_ratio="2"), _SIDECAR_UNSEEN),
        # The Pod's limit is within `max` so far, but the sidecar may add to it.
        (APP, LimitValue("Pod", "cpu", max="4"), _POD_UNSEEN),
        # Over the ratio so far, but a default may raise the request.
        (_WIDE, LimitValue("Pod", "cpu", max_limit_request_ratio="4"), _POD_UNSEEN),
        # Within the ratio so far, but a default may raise the limit.
        (APP, LimitValue("Pod", "cpu", max_limit_request_ratio="4"), _POD_UNSEEN),
    ],
)
def test_a_limit_range_check_a_default_not_read_may_change_is_not_established(
    app: ContainerDemand | None, value: LimitValue, detail: str
) -> None:
    sidecar = ContainerDemand("sidecar", "container", (), ())
    containers = (sidecar,) if app is None else (app, sidecar)

    rows = _findings(
        _report(_template(*containers), limit_ranges=(_range(value),), limits_basis=Basis.TRUNCATED)
    )

    assert rows == [("cpu", Finding.NOT_ESTABLISHED, detail)]


def test_a_ratio_over_with_only_the_limit_able_to_grow_is_provable() -> None:
    # A default can only raise the Pod's limit here, so it stays over the ratio.
    request_only = ContainerDemand("sidecar", "container", (("cpu", "100m"),), ())
    ratio = LimitValue("Pod", "cpu", max_limit_request_ratio="4")

    rows = _findings(
        _report(
            _template(_WIDE, request_only),
            limit_ranges=(_range(ratio),),
            limits_basis=Basis.TRUNCATED,
        )
    )

    assert rows == [
        ("cpu", Finding.ABOVE_RATIO, "Pod: limit 1 is more than 4 times the request 200m")
    ]
