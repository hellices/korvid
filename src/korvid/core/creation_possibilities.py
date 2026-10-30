"""Which namespace policies a workload's next Pod could run into (#411 Q2).

Computed from the template's demand (`creation_demand`) and the quotas and
LimitRanges as now observed. Each row is a possibility, never a cause: quota
status is not atomic, other controllers create and delete objects
concurrently, and admission webhooks are not observed. When a policy's
applicability or a value is not established, the row says so instead of
guessing. "Nothing constrains this template" is said only when every list is
conclusive and nothing was found or left open.

A StatefulSet's claims are a separate, conditional demand: a new Pod reuses
any claim that exists, and claims are not read, so claim rows hold only "if
a new claim is created".
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from korvid.core.creation_constraints import CreationReport
from korvid.core.creation_demand import FIELD_KEYS, ContainerValues, PodDemand, pod_demand
from korvid.core.creation_policies import (
    LimitRangeProjection,
    LimitValue,
    QuotaEntry,
    QuotaProjection,
    ScopeExpression,
)
from korvid.core.creation_quantities import canonical_resource, format_quantity
from korvid.core.creation_workloads import ClaimTemplate, PodTemplate
from korvid.k8s.models import parse_quantity

_STORAGE_CLASS = ".storageclass.storage.k8s.io/"
_CLAIM_KEYS = frozenset({"requests.storage", "persistentvolumeclaims"})
_POD_COUNTS = frozenset({"pods", "count/pods"})
_NO_PRIORITY = "the Pod sets no priorityClassName, and a cluster default may apply"
_POD_LEVEL = (
    "Pod-level resources set it; whether the cluster accepts that in place of each "
    "container's is not observed"
)
_DEFAULT_CLASS = "the claim sets no storage class, and the cluster default may apply"


class Finding(StrEnum):
    """What a row says the next Pod could run into."""

    WOULD_EXCEED = "would exceed"
    REQUIRES_FIELD = "requires a field"
    AT_LIMIT = "at its limit"
    BELOW_MINIMUM = "below the minimum"
    ABOVE_MAXIMUM = "above the maximum"
    ABOVE_RATIO = "above the limit-to-request ratio"
    NOT_ESTABLISHED = "not established"


@dataclass(frozen=True, slots=True)
class Possibility:
    """One policy constraint the next Pod, or a new claim, could run into."""

    policy_kind: str
    policy: str
    uid: str | None
    subject: str
    finding: Finding
    detail: str
    conditional: bool = False


@dataclass(frozen=True, slots=True)
class Possibilities:
    """Every possible constraint, and whether the template is known to be unconstrained."""

    rows: tuple[Possibility, ...]
    nothing_constrains: bool
    defaults_applied: bool


def _parse(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    try:
        return parse_quantity(raw)
    except (ValueError, ArithmeticError):
        return None


# --- quota applicability ---------------------------------------------------


def _scope(name: str, template: PodTemplate, demand: PodDemand) -> bool | str:
    """Whether a named scope matches the Pod, or why that is not established."""
    if name == "Terminating":
        return template.active_deadline_seconds is not None
    if name == "NotTerminating":
        return template.active_deadline_seconds is None
    if name in ("BestEffort", "NotBestEffort"):
        if demand.best_effort is None:
            return "the Pod's QoS class is not established"
        return demand.best_effort is (name == "BestEffort")
    if name == "PriorityClass":
        return True if template.priority_class is not None else _NO_PRIORITY
    return f"scope {name} is not checked"


def _priority(expression: ScopeExpression, priority_class: str | None) -> bool | str:
    operator = expression.operator
    if operator not in ("In", "NotIn", "Exists", "DoesNotExist"):
        return f"operator {operator} is not checked"
    if priority_class is None:
        return _NO_PRIORITY
    if operator in ("In", "NotIn"):
        return (priority_class in expression.values) is (operator == "In")
    return operator == "Exists"


def _expression(
    expression: ScopeExpression, template: PodTemplate, demand: PodDemand
) -> bool | str:
    if expression.scope_name == "PriorityClass":
        return _priority(expression, template.priority_class)
    matched = _scope(expression.scope_name, template, demand)
    if isinstance(matched, str):
        return matched
    if expression.operator == "Exists":
        return matched
    if expression.operator == "DoesNotExist":
        return not matched
    return f"operator {expression.operator} is not checked"


def _applies(quota: QuotaProjection, template: PodTemplate, demand: PodDemand) -> bool | str:
    """Whether the quota applies to the Pod: `False` if any scope rules it out."""
    results = [_scope(name, template, demand) for name in quota.scopes]
    results += [_expression(e, template, demand) for e in quota.scope_selector]
    if any(result is False for result in results):
        return False
    return next((result for result in results if isinstance(result, str)), True)


# --- quota rows -------------------------------------------------------------


def _is_claim_key(key: str) -> bool:
    return key in _CLAIM_KEYS or _STORAGE_CLASS in key


def _exceeds(entry: QuotaEntry, amount: Decimal) -> str | None:
    """The detail of a would-exceed row, if `amount` more takes `entry` past its limit."""
    used, hard = _parse(entry.used), _parse(entry.hard)
    if used is None or hard is None or used + amount <= hard:
        return None
    remaining = hard - used
    left = format_quantity(remaining, entry.hard, entry.resource) if remaining > 0 else "none"
    return f"needs {format_quantity(amount, entry.hard, entry.resource)}; {left} remaining"


def _field_row(entry: QuotaEntry, demand: PodDemand) -> tuple[Finding, str] | None:
    key = canonical_resource(entry.resource)
    if key not in FIELD_KEYS:
        return None
    lacking = [name for name, missing in demand.missing if missing == key]
    if not lacking:
        return None
    if key.partition(".")[2] in demand.pod_level:
        return Finding.NOT_ESTABLISHED, _POD_LEVEL
    verb = "does" if len(lacking) == 1 else "do"
    return Finding.REQUIRES_FIELD, f"{', '.join(lacking)} {verb} not set it"


def _entry_row(entry: QuotaEntry, demand: PodDemand) -> tuple[Finding, str] | None:
    field = _field_row(entry, demand)
    if field is not None:
        return field
    key = canonical_resource(entry.resource)
    amount = demand.for_quota(key)
    if amount is None:
        return Finding.NOT_ESTABLISHED, "the Pod's demand for it is not established"
    if amount == 0:
        return None
    if key in _POD_COUNTS:
        exceeded = _exceeds(entry, amount) is not None
        return (Finding.AT_LIMIT, f"{entry.used} of {entry.hard} Pods exist") if exceeded else None
    detail = _exceeds(entry, amount)
    return (Finding.WOULD_EXCEED, detail) if detail is not None else None


def _quota_rows(
    quota: QuotaProjection, template: PodTemplate, demand: PodDemand
) -> Iterator[Possibility]:
    def row(subject: str, finding: Finding, detail: str) -> Possibility:
        return Possibility("ResourceQuota", quota.name, quota.uid, subject, finding, detail)

    applies = _applies(quota, template, demand)
    if applies is False:
        return
    if isinstance(applies, str):
        yield row("quota", Finding.NOT_ESTABLISHED, f"whether it applies: {applies}")
        return
    if quota.stale:
        yield row("quota", Finding.NOT_ESTABLISHED, "its status is not yet updated")
        return
    if not quota.complete:
        yield row("quota", Finding.NOT_ESTABLISHED, "its constraints could not be read")
    for entry in quota.entries:
        if _is_claim_key(entry.resource):
            continue
        found = _entry_row(entry, demand)
        if found is not None:
            yield row(entry.resource, *found)


# --- LimitRange rows ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Values:
    """One container's, or the Pod's, values for one resource."""

    owner: str
    request: Decimal | None
    limit: Decimal | None
    unknown: bool


def _values(owner: str, values: ContainerValues | PodDemand, resource: str) -> _Values:
    unknown = resource in values.unknown_requests or resource in values.unknown_limits
    return _Values(owner, values.requests.get(resource), values.limits.get(resource), unknown)


def _minimum(v: _Values, raw: str, resource: str) -> tuple[Finding, str] | None:
    bound = _parse(raw)
    if bound is None:
        return None
    if v.request is None:
        return Finding.BELOW_MINIMUM, f"{v.owner}: no request is set"
    for side, value in (("request", v.request), ("limit", v.limit)):
        if value is not None and value < bound:
            text = format_quantity(value, raw, resource)
            return Finding.BELOW_MINIMUM, f"{v.owner}: {side} {text} is below {raw}"
    return None


def _maximum(v: _Values, raw: str, resource: str) -> tuple[Finding, str] | None:
    bound = _parse(raw)
    if bound is None:
        return None
    if v.limit is None:
        return Finding.ABOVE_MAXIMUM, f"{v.owner}: no limit is set"
    for side, value in (("limit", v.limit), ("request", v.request)):
        if value is not None and value > bound:
            text = format_quantity(value, raw, resource)
            return Finding.ABOVE_MAXIMUM, f"{v.owner}: {side} {text} is above {raw}"
    return None


def _ratio(v: _Values, raw: str) -> tuple[Finding, str] | None:
    bound = _parse(raw)
    if bound is None:
        return None
    if v.request is None or v.limit is None or v.request == 0:
        return Finding.ABOVE_RATIO, f"{v.owner}: needs both a request and a limit"
    ratio = v.limit / v.request
    if ratio <= bound:
        return None
    shown = f"{round(ratio, 2).normalize():f}"
    return Finding.ABOVE_RATIO, f"{v.owner}: limit is {shown} times the request, above {raw}"


def _checks(value: LimitValue, v: _Values) -> Iterator[tuple[Finding, str]]:
    if v.unknown:
        yield Finding.NOT_ESTABLISHED, f"{v.owner}: its value is not established"
        return
    found = [
        _minimum(v, value.min, value.resource) if value.min is not None else None,
        _maximum(v, value.max, value.resource) if value.max is not None else None,
        _ratio(v, value.max_limit_request_ratio)
        if value.max_limit_request_ratio is not None
        else None,
    ]
    yield from (row for row in found if row is not None)


def _owners(value: LimitValue, demand: PodDemand) -> list[_Values]:
    if value.type == "Container":
        return [_values(c.name, c, value.resource) for c in demand.containers]
    if value.type == "Pod":
        return [_values("Pod", demand, value.resource)]
    return []


def _limit_range_rows(
    limit_range: LimitRangeProjection, demand: PodDemand
) -> Iterator[Possibility]:
    for value in limit_range.values:
        for owner in _owners(value, demand):
            for finding, detail in _checks(value, owner):
                yield Possibility(
                    "LimitRange", limit_range.name, limit_range.uid, value.resource, finding, detail
                )


# --- claims -----------------------------------------------------------------


def _claim_quota_row(entry: QuotaEntry, claim: ClaimTemplate) -> tuple[Finding, str] | None:
    key = entry.resource
    if _STORAGE_CLASS in key:
        if claim.storage_class is None:
            return Finding.NOT_ESTABLISHED, _DEFAULT_CLASS
        if key.partition(_STORAGE_CLASS)[0] != claim.storage_class:
            return None
        key = key.partition(_STORAGE_CLASS)[2]
    if key == "persistentvolumeclaims":
        detail = _exceeds(entry, Decimal(1))
        return (Finding.AT_LIMIT, f"{entry.used} of {entry.hard} claims exist") if detail else None
    storage = _parse(claim.storage)
    detail = _exceeds(entry, storage) if storage is not None else None
    return (Finding.WOULD_EXCEED, detail) if detail is not None else None


def _claim_rows(
    claims: Sequence[ClaimTemplate],
    quotas: Sequence[QuotaProjection],
    limit_ranges: Sequence[LimitRangeProjection],
) -> Iterator[Possibility]:
    for claim in claims:
        for quota in quotas:
            if quota.stale or quota.scopes or quota.scope_selector:
                continue
            for entry in quota.entries:
                found = _claim_quota_row(entry, claim) if _is_claim_key(entry.resource) else None
                if found is not None:
                    subject = f"claim {claim.name}: {entry.resource}"
                    yield Possibility("ResourceQuota", quota.name, quota.uid, subject, *found, True)
        yield from _claim_limit_rows(claim, limit_ranges)


def _claim_limit_rows(
    claim: ClaimTemplate, limit_ranges: Sequence[LimitRangeProjection]
) -> Iterator[Possibility]:
    storage = _parse(claim.storage)
    if storage is None:
        return
    for limit_range in limit_ranges:
        for value in limit_range.values:
            if value.type != "PersistentVolumeClaim" or value.resource != "storage":
                continue
            for raw, below in ((value.min, True), (value.max, False)):
                bound = _parse(raw)
                if bound is None or (storage >= bound if below else storage <= bound):
                    continue
                finding = Finding.BELOW_MINIMUM if below else Finding.ABOVE_MAXIMUM
                text = format_quantity(storage, raw or "", "storage")
                detail = f"request {text} is {'below' if below else 'above'} {raw}"
                yield Possibility(
                    "LimitRange",
                    limit_range.name,
                    limit_range.uid,
                    f"claim {claim.name}: storage",
                    finding,
                    detail,
                    True,
                )


# --- the whole ------------------------------------------------------------------


def _unconstrained(
    report: CreationReport, template: PodTemplate, rows: Sequence[Possibility], now: datetime
) -> bool:
    return (
        not rows
        and template.runtime_class is None
        and all(claim.storage_class is not None for claim in template.claims)
        and report.quotas_conclusive(now)
        and report.limit_ranges_conclusive(now)
    )


def possible_constraints(report: CreationReport, now: datetime) -> Possibilities | None:
    """The constraints the workload's next Pod could run into, or `None` without its template."""
    template = report.workload.template if report.workload is not None else None
    if template is None:
        return None
    demand = pod_demand(template, report.limit_ranges)
    rows = [row for quota in report.quotas for row in _quota_rows(quota, template, demand)]
    rows += [row for limits in report.limit_ranges for row in _limit_range_rows(limits, demand)]
    rows += _claim_rows(template.claims, report.quotas, report.limit_ranges)
    return Possibilities(
        tuple(rows), _unconstrained(report, template, rows, now), demand.defaults_applied
    )
