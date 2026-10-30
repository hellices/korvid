"""Tests for grouping and comparing ResourceQuota quantities (#411)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from korvid.core.creation_quantities import (
    QuotaGroup,
    Usage,
    canonical_resource,
    format_quantity,
    quota_group,
    same_quantity,
    unit_word,
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


@pytest.mark.parametrize(
    ("value", "limit", "resource", "text"),
    [
        ("1.5", "2", "requests.cpu", "1500m"),
        ("0.5", "2", "requests.cpu", "500m"),
        ("2", "4", "requests.cpu", "2"),
        ("0", "4", "requests.cpu", "0"),
        ("-0.5", "2", "requests.cpu", "-500m"),
        ("1.0000000001", "2", "requests.cpu", "1.0000000001 cores"),
        ("1073741824", "8Gi", "limits.memory", "1Gi"),
        ("1610612736", "8Gi", "limits.memory", "1536Mi"),
        ("100", "1Gi", "limits.memory", "100"),
        ("0.5", "1Gi", "limits.memory", "0.5 bytes"),
        ("3000", "10k", "pods", "3k"),
        ("1000000", "8G", "requests.storage", "1M"),
        ("7", "10", "count/pods", "7"),
    ],
)
def test_a_computed_amount_is_shown_exactly_in_its_limits_unit_family(
    value: str, limit: str, resource: str, text: str
) -> None:
    assert format_quantity(Decimal(value), limit, resource) == text


@pytest.mark.parametrize(
    ("resource", "word"),
    [
        ("cpu", "cores"),
        ("limits.cpu", "cores"),
        ("memory", "bytes"),
        ("requests.storage", "bytes"),
        ("hugepages-2Mi", "bytes"),
        ("pods", "objects"),
        ("count/jobs.batch", "objects"),
        ("requests.nvidia.com/gpu", "units"),
    ],
)
def test_each_resource_names_its_base_unit(resource: str, word: str) -> None:
    assert unit_word(resource) == word
