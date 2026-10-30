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
