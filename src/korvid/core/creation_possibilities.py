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
from korvid.core.creation_quantities import canonical_resource, format_quantity, unit_word
from korvid.core.creation_workloads import ClaimTemplate, PodTemplate
from korvid.k8s.models import parse_quantity

_STORAGE_CLASS = ".storageclass.storage.k8s.io/"
_CLAIM_KEYS = frozenset(
    {"requests.storage", "persistentvolumeclaims", "count/persistentvolumeclaims"}
)
_CLAIM_COUNTS = frozenset({"persistentvolumeclaims", "count/persistentvolumeclaims"})
_POD_COUNTS = frozenset({"pods", "count/pods"})
_NO_PRIORITY = "the Pod sets no priorityClassName, and a cluster default may apply"
_POD_LEVEL = (
    "Pod-level resources are set; whether the cluster accepts them in place of each "
    "container's is not observed"
)
_UNSEEN_DEFAULT = "a default from a LimitRange that could not be read"
_DEFAULT_CLASS = "the claim sets no storage class, and the cluster default may apply"
_UNREADABLE_USAGE = "its used or hard amount could not be read"
_UNKNOWN_CLAIM = "the claim's storage request is not established"
_UNSEEN_FIELD = "a LimitRange that could not be read may set it"
_UNSEEN_GROWTH = "a default from a LimitRange that could not be read may add to the Pod's demand"


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


def _over(entry: QuotaEntry, amount: Decimal) -> bool | None:
    """Whether `amount` more takes `entry` past its limit; `None` if a value does not parse."""
    used, hard = _parse(entry.used), _parse(entry.hard)
    if used is None or hard is None:
        return None
    return used + amount > hard


def _need(entry: QuotaEntry, amount: Decimal, *, at_least: bool = False) -> str:
    """What the would-exceed row says: what is needed, and what remains."""
    used, hard = _parse(entry.used), _parse(entry.hard)
    remaining = (hard or Decimal(0)) - (used or Decimal(0))
    left = format_quantity(remaining, entry.hard, entry.resource) if remaining > 0 else "none"
    needed = format_quantity(amount, entry.hard, entry.resource)
    return f"needs {'at least ' if at_least else ''}{needed}; {left} remaining"


def _field_row(entry: QuotaEntry, demand: PodDemand) -> tuple[Finding, str] | None:
    key = canonical_resource(entry.resource)
    if key not in FIELD_KEYS:
        return None
    lacking = [name for name, missing in demand.missing if missing == key]
    if not lacking:
        return None
    # With PodLevelResources, Pod-level CPU or memory skips every per-container check.
    if demand.pod_level & {"cpu", "memory"}:
        return Finding.NOT_ESTABLISHED, _POD_LEVEL
    if not demand.defaults_known:
        return Finding.NOT_ESTABLISHED, _UNSEEN_FIELD
    verb = "does" if len(lacking) == 1 else "do"
    return Finding.REQUIRES_FIELD, f"{', '.join(lacking)} {verb} not set it"


def _entry_row(entry: QuotaEntry, demand: PodDemand) -> tuple[Finding, str] | None:
    key = canonical_resource(entry.resource)
    amount = demand.for_quota(key)
    if amount is None:
        return Finding.NOT_ESTABLISHED, "the Pod's demand for it is not established"
    grows = demand.may_grow(key)
    over = _over(entry, amount) if amount else False
    field = _field_row(entry, demand)
    if field is not None and not (over and grows):
        # A field the Pod lacks rejects it whatever the amounts; only an excess
        # already certain without the unread defaults says more.
        return field
    if over is None:
        return Finding.NOT_ESTABLISHED, _UNREADABLE_USAGE
    if key in _POD_COUNTS:
        return (Finding.AT_LIMIT, f"{entry.used} of {entry.hard} Pods exist") if over else None
    if over:
        return Finding.WOULD_EXCEED, _need(entry, amount, at_least=grows)
    return (Finding.NOT_ESTABLISHED, _UNSEEN_GROWTH) if grows else None


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
    """One container's, or the Pod's, values for one resource.

    `request_grows` and `limit_grows` say a LimitRange default korvid could
    not read may still set or raise that side.
    """

    owner: str
    request: Decimal | None
    limit: Decimal | None
    unknown: bool
    request_grows: bool = False
    limit_grows: bool = False

    @property
    def unseen(self) -> tuple[Finding, str]:
        verb = "change" if self.owner == "Pod" else "set"
        return Finding.NOT_ESTABLISHED, f"{self.owner}: {_UNSEEN_DEFAULT} may {verb} it"


def _container_values(c: ContainerValues, resource: str, defaults_known: bool) -> _Values:
    unknown = resource in c.unknown_requests or resource in c.unknown_limits
    request, limit = c.requests.get(resource), c.limits.get(resource)
    return _Values(
        c.name,
        request,
        limit,
        unknown,
        not defaults_known and request is None and not unknown,
        not defaults_known and limit is None and not unknown,
    )


def _pod_values(demand: PodDemand, resource: str) -> _Values:
    unknown = resource in demand.unknown_requests or resource in demand.unknown_limits
    return _Values(
        "Pod",
        demand.requests.get(resource),
        demand.limits.get(resource),
        unknown,
        demand.may_grow(f"requests.{resource}"),
        demand.may_grow(f"limits.{resource}"),
    )


def _unreadable_bound(v: _Values, name: str) -> tuple[Finding, str]:
    return Finding.NOT_ESTABLISHED, f"{v.owner}: its {name} bound could not be read"


def _shown(value: Decimal, resource: str) -> str:
    """A container's value in its resource's own unit family."""
    return format_quantity(value, "1Ki" if unit_word(resource) == "bytes" else "1", resource)


def _minimum(v: _Values, raw: str, resource: str) -> tuple[Finding, str] | None:
    """A value below `min` is provable only if no unread default could raise it."""
    bound = _parse(raw)
    if bound is None:
        return _unreadable_bound(v, "min")
    if v.request is None:
        return (
            v.unseen
            if v.request_grows
            else (Finding.BELOW_MINIMUM, f"{v.owner}: no request is set")
        )
    for side, value, grows in (
        ("request", v.request, v.request_grows),
        ("limit", v.limit, v.limit_grows),
    ):
        if value is not None and value < bound:
            text = format_quantity(value, raw, resource)
            return (
                v.unseen
                if grows
                else (Finding.BELOW_MINIMUM, f"{v.owner}: {side} {text} is below {raw}")
            )
    return None


def _maximum(v: _Values, raw: str, resource: str) -> tuple[Finding, str] | None:
    """A value above `max` is provable, since a default can only add; a value below is not."""
    bound = _parse(raw)
    if bound is None:
        return _unreadable_bound(v, "max")
    if v.limit is None:
        return v.unseen if v.limit_grows else (Finding.ABOVE_MAXIMUM, f"{v.owner}: no limit is set")
    for side, value in (("limit", v.limit), ("request", v.request)):
        if value is not None and value > bound:
            text = format_quantity(value, raw, resource)
            return Finding.ABOVE_MAXIMUM, f"{v.owner}: {side} {text} is above {raw}"
    # A request can grow only where its limit is unset, so `limit_grows` covers both.
    return v.unseen if v.limit_grows else None


def _ratio(v: _Values, raw: str, resource: str) -> tuple[Finding, str] | None:
    """The LimitRanger needs a nonzero request and limit, and limit / request within `raw`."""
    bound = _parse(raw)
    if bound is None:
        return _unreadable_bound(v, "maxLimitRequestRatio")
    if not v.request or not v.limit:
        if v.request_grows or v.limit_grows:
            return v.unseen
        return Finding.ABOVE_RATIO, f"{v.owner}: needs both a request and a limit"
    over = v.limit > v.request * bound
    if over and not v.request_grows:
        limit, request = _shown(v.limit, resource), _shown(v.request, resource)
        detail = f"{v.owner}: limit {limit} is more than {raw} times the request {request}"
        return Finding.ABOVE_RATIO, detail
    # Over with a growing request means a growing limit too: either way, not established.
    return v.unseen if v.limit_grows else None


def _checks(value: LimitValue, v: _Values) -> Iterator[tuple[Finding, str]]:
    if v.unknown:
        yield Finding.NOT_ESTABLISHED, f"{v.owner}: its value is not established"
        return
    found = [
        _minimum(v, value.min, value.resource) if value.min is not None else None,
        _maximum(v, value.max, value.resource) if value.max is not None else None,
        _ratio(v, value.max_limit_request_ratio, value.resource)
        if value.max_limit_request_ratio is not None
        else None,
    ]
    yield from (row for row in found if row is not None)


def _owners(value: LimitValue, demand: PodDemand) -> list[_Values]:
    if value.type == "Container":
        known = demand.defaults_known
        return [_container_values(c, value.resource, known) for c in demand.containers]
    if value.type == "Pod":
        return [_pod_values(demand, value.resource)]
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
    counted = key in _CLAIM_COUNTS
    amount = Decimal(1) if counted else _parse(claim.storage)
    if amount is None:
        return Finding.NOT_ESTABLISHED, _UNKNOWN_CLAIM
    over = _over(entry, amount)
    if over is None:
        return Finding.NOT_ESTABLISHED, _UNREADABLE_USAGE
    if not over:
        return None
    if counted:
        return Finding.AT_LIMIT, f"{entry.used} of {entry.hard} claims exist"
    return Finding.WOULD_EXCEED, _need(entry, amount)


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


def _claim_bound(storage: Decimal | None, raw: str, below: bool) -> tuple[Finding, str] | None:
    """One PersistentVolumeClaim bound checked against a claim's storage request."""
    if storage is None:
        return Finding.NOT_ESTABLISHED, _UNKNOWN_CLAIM
    bound = _parse(raw)
    if bound is None:
        return Finding.NOT_ESTABLISHED, f"its {'min' if below else 'max'} bound could not be read"
    if storage >= bound if below else storage <= bound:
        return None
    finding = Finding.BELOW_MINIMUM if below else Finding.ABOVE_MAXIMUM
    text = format_quantity(storage, raw, "storage")
    return finding, f"request {text} is {'below' if below else 'above'} {raw}"


def _claim_limit_rows(
    claim: ClaimTemplate, limit_ranges: Sequence[LimitRangeProjection]
) -> Iterator[Possibility]:
    storage = _parse(claim.storage)
    for limit_range in limit_ranges:
        for value in limit_range.values:
            if value.type != "PersistentVolumeClaim" or value.resource != "storage":
                continue
            bounds = [(raw, below) for raw, below in ((value.min, True), (value.max, False)) if raw]
            found = [_claim_bound(storage, raw, below) for raw, below in bounds]
            # An unknown request says so once per LimitRange value, not once per bound.
            rows = list(dict.fromkeys(row for row in found if row is not None))
            for finding, detail in rows:
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
    defaults_known = report.limit_ranges_conclusive(now) and all(
        limit_range.complete for limit_range in report.limit_ranges
    )
    demand = pod_demand(template, report.limit_ranges, defaults_known=defaults_known)
    rows = [row for quota in report.quotas for row in _quota_rows(quota, template, demand)]
    rows += [row for limits in report.limit_ranges for row in _limit_range_rows(limits, demand)]
    rows += _claim_rows(template.claims, report.quotas, report.limit_ranges)
    return Possibilities(
        tuple(rows), _unconstrained(report, template, rows, now), demand.defaults_applied
    )
