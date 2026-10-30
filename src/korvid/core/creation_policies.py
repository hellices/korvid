"""Project the namespace's ResourceQuotas and LimitRanges (#411).

Only what the view shows and Q2 compares is kept: identities, quantities as
the cluster wrote them, quota scopes and scope selectors, and whether a
quota's status is current. Quota status is filled by the quota controller
after `spec.hard` changes, so a quota whose status is missing, lags its spec
or lacks a used amount is stale, and none of its resources is called at its
limit.
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


def _identity(item: Mapping[str, Any]) -> tuple[str, str | None] | None:
    metadata = _mapping(item.get("metadata"))
    name = _text(metadata.get("name"))
    return None if name is None else (name, _text(metadata.get("uid")))


def _stale(hard: Mapping[str, str], status: Mapping[str, Any]) -> bool:
    """Whether the quota controller has not yet caught up with `hard`."""
    status_hard, used = _quantities(status.get("hard")), _quantities(status.get("used"))
    if status_hard.keys() != hard.keys() or not status_hard.keys() <= used.keys():
        return True
    return not all(same_quantity(hard[name], status_hard[name]) for name in hard)


def _scope_selector(spec: Mapping[str, Any]) -> tuple[ScopeExpression, ...]:
    expressions = _mapping(spec.get("scopeSelector")).get("matchExpressions")
    selected: list[ScopeExpression] = []
    for raw in expressions if isinstance(expressions, list) else []:
        expression = _mapping(raw)
        scope, operator = _text(expression.get("scopeName")), _text(expression.get("operator"))
        if scope is None or operator is None:
            continue
        values = expression.get("values")
        texts = tuple(v for v in values if isinstance(v, str)) if isinstance(values, list) else ()
        selected.append(ScopeExpression(scope, operator, texts))
    return tuple(selected)


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
    scopes = spec.get("scopes")
    return QuotaProjection(
        identity[0],
        identity[1],
        tuple(entries),
        tuple(s for s in scopes if isinstance(s, str)) if isinstance(scopes, list) else (),
        _scope_selector(spec),
        stale,
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


def _limit_range(item: Mapping[str, Any]) -> LimitRangeProjection | None:
    identity = _identity(item)
    if identity is None:
        return None
    limits = _mapping(item.get("spec")).get("limits")
    values = (
        value
        for limit in (limits if isinstance(limits, list) else [])
        if isinstance(limit, Mapping)
        for value in _limit_values(limit)
    )
    return LimitRangeProjection(identity[0], identity[1], tuple(values))


def project_limit_ranges(items: Iterable[object]) -> tuple[LimitRangeProjection, ...]:
    """Each named LimitRange in `items`, ordered by name; anything else is skipped."""
    ranges = (_limit_range(item) for item in items if isinstance(item, Mapping))
    return tuple(sorted((r for r in ranges if r is not None), key=lambda r: r.name))
