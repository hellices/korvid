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
    except ValueError:
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
