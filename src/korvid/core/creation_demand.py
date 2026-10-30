"""What one new Pod of a workload asks for, as admission will see it (#411 Q2).

The demand is computed from the projected template the way Kubernetes
computes a Pod's effective request, after the defaulting admission applies:

- a container that sets a limit but no request gets the limit as its request;
- LimitRange `Container` defaults then fill what is still missing:
  `defaultRequest` a request, `default` a limit. When LimitRanges give
  different defaults for the same resource, Kubernetes does not define which
  applies, so that resource is not established;
- app containers and sidecars are summed, and each other init container
  counts only if it alone, with the sidecars started before it, needs more;
- Pod-level `resources` override per resource, separately for requests and
  limits;
- a `runtimeClassName` can add an overhead korvid does not read, so CPU and
  memory are not established.

A value that does not parse leaves its resource not established. Other
admission plugins can still change a Pod; this is the template plus the
configured defaults, nothing more.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from korvid.core.creation_policies import LimitRangeProjection
from korvid.core.creation_quantities import canonical_resource
from korvid.core.creation_workloads import ContainerDemand, PodTemplate, Quantities
from korvid.k8s.models import parse_quantity

#: The quota keys that require every container to specify the field.
FIELD_KEYS = ("requests.cpu", "limits.cpu", "requests.memory", "limits.memory")
_COMPUTE = frozenset({"cpu", "memory"})
_POD_COUNTS = frozenset({"pods", "count/pods"})


@dataclass(frozen=True, slots=True)
class ContainerValues:
    """One container's requests and limits after defaulting, and what is not established."""

    name: str
    requests: Mapping[str, Decimal]
    limits: Mapping[str, Decimal]
    unknown_requests: frozenset[str] = frozenset()
    unknown_limits: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PodDemand:
    """One new Pod's requests and limits per resource, and what is not established.

    `missing` lists `(container, quota key)` for each CPU or memory field a
    container still lacks after defaults. `pod_level` names the resources the
    Pod-level `resources` set. `best_effort` is the Pod's QoS class being
    BestEffort, or `None` when that depends on a value not established.
    """

    requests: Mapping[str, Decimal]
    limits: Mapping[str, Decimal]
    unknown_requests: frozenset[str] = frozenset()
    unknown_limits: frozenset[str] = frozenset()
    missing: tuple[tuple[str, str], ...] = ()
    pod_level: frozenset[str] = frozenset()
    best_effort: bool | None = True
    defaults_applied: bool = False
    containers: tuple[ContainerValues, ...] = ()
    defaults_known: bool = True

    def for_quota(self, key: str) -> Decimal | None:
        """What the Pod charges against quota resource `key`; `None` when not established.

        Zero means the Pod does not consume it, so the quota does not check it.
        """
        canonical = canonical_resource(key)
        if canonical in _POD_COUNTS:
            return Decimal(1)
        side, _, resource = canonical.partition(".")
        if side == "requests":
            values, unknown = self.requests, self.unknown_requests
        elif side == "limits":
            values, unknown = self.limits, self.unknown_limits
        else:
            return Decimal(0)
        if resource in unknown:
            return None
        return values.get(resource, Decimal(0))

    def may_grow(self, key: str) -> bool:
        """Whether a LimitRange default korvid could not read may add to `key`'s demand.

        Only when the LimitRanges were not all read, and some container does
        not set the resource itself.
        """
        side, _, resource = canonical_resource(key).partition(".")
        if self.defaults_known or side not in ("requests", "limits") or resource in self.pod_level:
            return False
        for container in self.containers:
            values = container.requests if side == "requests" else container.limits
            unknown = container.unknown_requests if side == "requests" else container.unknown_limits
            if resource not in values and resource not in unknown:
                return True
        return False


@dataclass(slots=True)
class _Side:
    """One side (requests or limits) of one container, as admission completes it."""

    values: dict[str, Decimal] = field(default_factory=dict)
    unknown: set[str] = field(default_factory=set)

    def has(self, resource: str) -> bool:
        return resource in self.values or resource in self.unknown


@dataclass(slots=True)
class _Defaults:
    """The LimitRange `Container` defaults for one side, and the resources they disagree on."""

    values: dict[str, Decimal] = field(default_factory=dict)
    conflicting: set[str] = field(default_factory=set)

    def add(self, resource: str, raw: str | None) -> None:
        if raw is None or resource in self.conflicting:
            return
        value = _parse(raw)
        if value is None or self.values.get(resource, value) != value:
            self.conflicting.add(resource)
            self.values.pop(resource, None)
            return
        self.values[resource] = value


def _parse(raw: str) -> Decimal | None:
    try:
        return parse_quantity(raw)
    except (ValueError, ArithmeticError):
        return None


def _side(quantities: Quantities) -> _Side:
    side = _Side()
    for resource, raw in quantities:
        value = _parse(raw)
        if value is None:
            side.unknown.add(resource)
        else:
            side.values[resource] = value
    return side


def _defaults(limit_ranges: Iterable[LimitRangeProjection]) -> tuple[_Defaults, _Defaults]:
    requests, limits = _Defaults(), _Defaults()
    for limit_range in limit_ranges:
        for value in limit_range.values:
            if value.type == "Container":
                requests.add(value.resource, value.default_request)
                limits.add(value.resource, value.default)
    return requests, limits


def _fill(side: _Side, defaults: _Defaults) -> bool:
    """Fill what `side` lacks from `defaults`; whether a default was applied."""
    applied = False
    for resource in defaults.conflicting:
        if not side.has(resource):
            side.unknown.add(resource)
    for resource, value in defaults.values.items():
        if not side.has(resource):
            side.values[resource] = value
            applied = True
    return applied


@dataclass(slots=True)
class _Container:
    name: str
    category: str
    requests: _Side
    limits: _Side


def _complete(
    container: ContainerDemand, defaults: tuple[_Defaults, _Defaults]
) -> tuple[_Container, bool]:
    requests, limits = _side(container.requests), _side(container.limits)
    for resource, value in limits.values.items():
        if not requests.has(resource):
            requests.values[resource] = value
    for resource in limits.unknown:
        if not requests.has(resource):
            requests.unknown.add(resource)
    applied = _fill(requests, defaults[0])
    applied = _fill(limits, defaults[1]) or applied
    return _Container(container.name, container.category, requests, limits), applied


def _add(total: dict[str, Decimal], values: Mapping[str, Decimal]) -> dict[str, Decimal]:
    summed = dict(total)
    for resource, value in values.items():
        summed[resource] = summed.get(resource, Decimal(0)) + value
    return summed


def _maximum(left: Mapping[str, Decimal], right: Mapping[str, Decimal]) -> dict[str, Decimal]:
    return {r: max(left.get(r, Decimal(0)), right.get(r, Decimal(0))) for r in {*left, *right}}


def _effective(
    apps: Sequence[_Container], inits: Sequence[_Container], side: str
) -> tuple[dict[str, Decimal], set[str]]:
    """The Pod's effective value per resource for one side, and the resources not established."""
    unknown: set[str] = set()
    total: dict[str, Decimal] = {}
    for app in apps:
        values: _Side = getattr(app, side)
        total = _add(total, values.values)
        unknown |= values.unknown
    sidecars: dict[str, Decimal] = {}
    peak: dict[str, Decimal] = {}
    for init in inits:
        values = getattr(init, side)
        unknown |= values.unknown
        if init.category == "sidecar":
            # A sidecar runs on beside the apps, so it adds to their total.
            total = _add(total, values.values)
            sidecars = _add(sidecars, values.values)
        else:
            peak = _maximum(peak, _add(sidecars, values.values))
    effective = _maximum(total, peak)
    for resource in unknown:
        # One container's value not established leaves the Pod's not established.
        effective.pop(resource, None)
    return effective, unknown


def _override(values: dict[str, Decimal], unknown: set[str], pod: Quantities) -> frozenset[str]:
    """Apply the Pod-level values over the computed ones; the resources they set."""
    overridden = _side(pod)
    for resource, value in overridden.values.items():
        values[resource] = value
        unknown.discard(resource)
    for resource in overridden.unknown:
        values.pop(resource, None)
        unknown.add(resource)
    return frozenset(name for name, _ in pod)


def _missing(containers: Sequence[_Container]) -> tuple[tuple[str, str], ...]:
    missing: list[tuple[str, str]] = []
    for container in containers:
        for key in FIELD_KEYS:
            side, _, resource = key.partition(".")
            if not getattr(container, side).has(resource):
                missing.append((container.name, key))
    return tuple(missing)


def _best_effort(sides: Sequence[_Side]) -> bool | None:
    """BestEffort as Kubernetes computes it: no positive CPU or memory anywhere.

    `None` when a CPU or memory value is not established and none is positive.
    """
    if any(side.values.get(r, Decimal(0)) > 0 for side in sides for r in _COMPUTE):
        return False
    if any(r in side.unknown for side in sides for r in _COMPUTE):
        return None
    return True


def pod_demand(
    template: PodTemplate,
    limit_ranges: Sequence[LimitRangeProjection],
    *,
    defaults_known: bool = True,
) -> PodDemand:
    """One new Pod's demand from `template`, after defaulting and `limit_ranges`' defaults.

    `defaults_known` is whether `limit_ranges` are all the namespace's: when
    not, a default korvid could not read may still fill what a container
    leaves unset, so the QoS class is not established while it looks
    BestEffort, and `may_grow` says which demands could rise.
    """
    defaults = _defaults(limit_ranges)
    completed = [_complete(c, defaults) for c in (*template.containers, *template.init_containers)]
    containers = [container for container, _ in completed]
    apps = containers[: len(template.containers)]
    inits = containers[len(template.containers) :]
    requests, unknown_requests = _effective(apps, inits, "requests")
    limits, unknown_limits = _effective(apps, inits, "limits")
    pod_level = _override(requests, unknown_requests, template.pod_requests)
    pod_level |= _override(limits, unknown_limits, template.pod_limits)
    pod_sides = [_side(template.pod_requests), _side(template.pod_limits)]
    best_effort = _best_effort(
        [*(s for c in containers for s in (c.requests, c.limits)), *pod_sides]
    )
    if best_effort is True and not defaults_known:
        best_effort = None
    if template.runtime_class is not None:
        for values, unknown in ((requests, unknown_requests), (limits, unknown_limits)):
            for resource in _COMPUTE:
                values.pop(resource, None)
                unknown.add(resource)
    return PodDemand(
        requests,
        limits,
        frozenset(unknown_requests),
        frozenset(unknown_limits),
        _missing(containers),
        pod_level,
        best_effort,
        any(applied for _, applied in completed),
        tuple(
            ContainerValues(
                c.name,
                dict(c.requests.values),
                dict(c.limits.values),
                frozenset(c.requests.unknown),
                frozenset(c.limits.unknown),
            )
            for c in containers
        ),
        defaults_known,
    )
