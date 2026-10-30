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
    """`name` with a short name replaced by the requests it means.

    `cpu`, `memory`, `ephemeral-storage` and `hugepages-<size>` are quota
    aliases for their `requests.` forms.
    """
    if name.startswith("hugepages-"):
        return f"requests.{name}"
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


_BINARY = (("Ei", 60), ("Pi", 50), ("Ti", 40), ("Gi", 30), ("Mi", 20), ("Ki", 10))
_DECIMAL = (("E", 18), ("P", 15), ("T", 12), ("G", 9), ("M", 6), ("k", 3), ("", 0))
_FRACTIONS = (("m", -3), ("u", -6), ("n", -9))


def _binary_family(limit: str) -> bool:
    return limit.rstrip().endswith(tuple(suffix for suffix, _ in _BINARY))


def unit_word(resource: str) -> str:
    """What one base unit of `resource` is called."""
    canonical = canonical_resource(resource)
    if canonical.endswith(".cpu"):
        return "cores"
    if quota_group(canonical) is QuotaGroup.COUNT:
        return "objects"
    if quota_group(canonical) in (QuotaGroup.COMPUTE, QuotaGroup.STORAGE):
        return "bytes"
    return "units"


def format_quantity(value: Decimal, limit: str, resource: str) -> str:
    """`value` in `limit`'s unit family, with the largest suffix that shows it exactly.

    Binary suffixes when `limit` is written with one, decimal ones otherwise;
    never rounded. An amount no suffix shows exactly is written in base units
    with the resource's unit word.
    """
    if value == 0:
        return "0"
    family = (
        (*((s, Decimal(2) ** e) for s, e in _BINARY), ("", Decimal(1)))
        if _binary_family(limit)
        else tuple((s, Decimal(10) ** e) for s, e in (*_DECIMAL, *_FRACTIONS))
    )
    for suffix, size in family:
        scaled = value / size
        if scaled == scaled.to_integral_value():
            return f"{int(scaled)}{suffix}"
    return f"{value.normalize():f} {unit_word(resource)}"
