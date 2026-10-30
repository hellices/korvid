# Workload Creation Possibilities, Core (#411 Q2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute which ResourceQuotas and LimitRanges a workload's next Pod, or a StatefulSet's new claim, could run into, in pure Python (#527, the core half of slice Q2 of #411).

**Architecture:** Three pieces:
- `core/creation_quantities.py` gains `format_quantity` for computed amounts;
- `core/creation_demand.py` turns the projected template into one Pod's demand, after defaulting;
- `core/creation_possibilities.py` decides each quota's applicability and checks quotas, LimitRanges and claims against that demand.

The view shows the result in the next slice.

**Tech Stack:** Python 3.11+, `decimal.Decimal`, pytest, mypy `--strict`, ruff, tach.

## Global Constraints

- **Design:** `docs/superpowers/specs/2026-10-30-creation-constraints-design.md`, sections "Quantities" (computed amounts) and "Possible constraints (P2)".
- **A possibility, never a cause:** every row is phrased as what could happen; nothing says the next create will fail or succeed.
- **Not established, not guessed:** these leave their rows not established:
  - conflicting defaults, an unparsable value, a RuntimeClass overhead;
  - a missing `priorityClassName`, an unknown scope or operator, an unknown QoS class;
  - a Pod-level value that may supply a required field;
  - an omitted storage class.
- **Exact arithmetic:** `Decimal` only; computed amounts in the limit's unit family, never rounded.
- **Code rules:** no `assert` in `src/`; every `pytest.raises` takes `match=`; maximum complexity 10; Google-style docstrings.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the design

- **Core and view are separate pull requests,** as for Q1.
- **LimitRange checks follow the LimitRanger's rules.**
  - `min` needs a request, which with any limit must not fall below it.
  - `max` needs a limit, which with any request must not exceed it.
  - `maxLimitRequestRatio` needs both a request and a limit, each nonzero.
  - A value checked against a bound is shown in that bound's unit family.
- **Claims are checked only against unscoped quotas.** Quota scopes match Pods, so a scoped quota's storage keys are not claim constraints.
- **LimitRanges korvid could not all read leave defaults open.** When the LimitRange list is not conclusive, or a LimitRange is incomplete, an unseen default may still fill what a container leaves unset. Then:
  - a missing CPU or memory field is not established;
  - BestEffort is not established;
  - no default korvid did read is applied, since one it did not read may override it;
  - a demand an unseen default could raise is not established unless it already exceeds the limit, in which case the row says "needs at least";
  - a LimitRange check a default could change is not established, but a value already above `max`, or a ratio only a growing limit could reach, is still reported.
- **A Pod-level limit stands in for a missing Pod-level request,** as the API defaults it, before any LimitRange check.
- **Any Pod-level CPU or memory leaves every required field open.** With Pod-level resources the cluster may skip the per-container checks altogether, which korvid does not observe.
- **Unreadable values are never satisfied.** An unparsable `used` or `hard`, LimitRange bound or claim request gives a not-established row, never silence.
- **QoS ignores zero values and Pod-level values that do not parse,** as Kubernetes does, and unprefixed `hugepages-<size>` quota keys charge the request.
- **Ratios are explained exactly:** "limit X is more than N times the request Y", never a rounded ratio.
- **A sidecar's own peak is not a candidate.** Apps plus sidecars already include it, so only regular init containers, with the sidecars started before them, compete with that total.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/core/creation_quantities.py` | modify | `format_quantity`, `unit_word` |
| `src/korvid/core/creation_demand.py` | create | `ContainerValues`, `PodDemand`, `pod_demand`, `FIELD_KEYS` |
| `src/korvid/core/creation_possibilities.py` | create | `Finding`, `Possibility`, `Possibilities`, `possible_constraints` |
| `tests/core/test_creation_quantities.py` | modify | formatting and unit words |
| `tests/core/test_creation_demand.py`, `tests/core/test_creation_possibilities.py` | create | the demand and the possibilities |
| `tests/test_optional_extras.py` | modify | the new modules import without extras |

---

### Task 1: Computed amounts

- [ ] Add tests to `tests/core/test_creation_quantities.py`:
  - `1.5` cores against `2` is `1500m`;
  - `1.5Gi` against `8Gi` is `1536Mi`;
  - `0.5` bytes is `0.5 bytes`;
  - `3000` against `10k` is `3k`;
  - negatives and zero;
  - plus `unit_word` per resource kind.
- [ ] Implement `format_quantity(value, limit, resource)`: try the largest suffix of the limit's family that shows the value as an integer, else base units with the unit word. Commit: `Format computed quota amounts exactly in their limit's unit family`.

### Task 2: The demand

Create `tests/core/test_creation_demand.py` with:

```python
"""Tests for a workload template's demand as one new Pod (#411 Q2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from korvid.core.creation_demand import ContainerValues, pod_demand
from korvid.core.creation_policies import LimitRangeProjection, LimitValue
from korvid.core.creation_workloads import ContainerDemand, PodTemplate


def _container(
    name: str,
    category: str = "container",
    *,
    requests: dict[str, str] | None = None,
    limits: dict[str, str] | None = None,
) -> ContainerDemand:
    return ContainerDemand(
        name,
        category,
        tuple(sorted((requests or {}).items())),
        tuple(sorted((limits or {}).items())),
    )


def _template(
    *containers: ContainerDemand, init: tuple[ContainerDemand, ...] = (), **extra: object
) -> PodTemplate:
    return PodTemplate(containers, init, **extra)  # type: ignore[arg-type]  # test-only keyword fields


def _limits(name: str, *values: LimitValue) -> LimitRangeProjection:
    return LimitRangeProjection(name, f"uid-{name}", values)


def test_requests_and_limits_are_summed_over_app_containers() -> None:
    demand = pod_demand(
        _template(
            _container("a", requests={"cpu": "250m", "memory": "64Mi"}, limits={"memory": "128Mi"}),
            _container("b", requests={"cpu": "0.5"}, limits={"cpu": "1"}),
        ),
        (),
    )

    assert demand.requests == {"cpu": Decimal("0.75"), "memory": Decimal(64 * 2**20)}
    assert demand.limits == {"cpu": Decimal(1), "memory": Decimal(128 * 2**20)}


def test_a_limit_stands_in_for_a_missing_request() -> None:
    demand = pod_demand(_template(_container("a", limits={"cpu": "1"})), ())

    assert demand.requests == {"cpu": Decimal(1)}


def test_limit_range_defaults_fill_only_what_is_missing() -> None:
    defaults = _limits(
        "defaults",
        LimitValue("Container", "cpu", default="500m", default_request="100m"),
        LimitValue("Container", "memory", default="256Mi", default_request="128Mi"),
        LimitValue("Pod", "cpu", max="4"),
    )

    demand = pod_demand(
        _template(
            _container("a", requests={"cpu": "200m"}), _container("b", limits={"memory": "64Mi"})
        ),
        (defaults,),
    )

    assert demand.requests == {"cpu": Decimal("0.3"), "memory": Decimal(192 * 2**20)}
    assert demand.limits == {"cpu": Decimal(1), "memory": Decimal(320 * 2**20)}
    assert demand.defaults_applied


def test_conflicting_defaults_leave_the_resource_not_established() -> None:
    first = _limits("a", LimitValue("Container", "cpu", default="1", default_request="500m"))
    second = _limits("b", LimitValue("Container", "cpu", default="2", default_request="500m"))
    same = _limits("c", LimitValue("Container", "memory", default="1Gi", default_request="1Gi"))
    also = _limits("d", LimitValue("Container", "memory", default="1024Mi", default_request="1Gi"))

    demand = pod_demand(_template(_container("a")), (first, second, same, also))

    assert demand.requests == {"cpu": Decimal("0.5"), "memory": Decimal(2**30)}
    assert "cpu" in demand.unknown_limits
    assert "cpu" not in demand.limits
    assert demand.limits == {"memory": Decimal(2**30)}


def test_a_container_that_sets_the_resource_needs_no_default() -> None:
    first = _limits("a", LimitValue("Container", "cpu", default="1"))
    second = _limits("b", LimitValue("Container", "cpu", default="2"))

    demand = pod_demand(_template(_container("a", limits={"cpu": "3"})), (first, second))

    assert (demand.limits, demand.unknown_limits) == ({"cpu": Decimal(3)}, frozenset())


def test_init_containers_and_sidecars_count_as_kubernetes_counts_them() -> None:
    template = _template(
        _container("app", requests={"cpu": "100m"}),
        init=(
            _container("migrate", "init", requests={"cpu": "1"}),
            _container("proxy", "sidecar", requests={"cpu": "200m"}),
            _container("warm", "init", requests={"cpu": "250m"}),
        ),
    )

    demand = pod_demand(template, ())

    # apps + sidecars = 300m; migrate alone = 1; warm with the proxy before it = 450m.
    assert demand.requests == {"cpu": Decimal(1)}


def test_sidecars_add_to_the_apps_when_the_init_containers_are_small() -> None:
    template = _template(
        _container("app", requests={"cpu": "500m"}),
        init=(
            _container("proxy", "sidecar", requests={"cpu": "200m"}),
            _container("warm", "init", requests={"cpu": "250m"}),
        ),
    )

    # apps + sidecar = 700m; warm with the proxy before it = 450m.
    assert pod_demand(template, ()).requests == {"cpu": Decimal("0.7")}


def test_pod_level_resources_override_one_resource_and_one_side_only() -> None:
    template = _template(
        _container("a", requests={"cpu": "2", "memory": "1Gi"}, limits={"cpu": "4"}),
        pod_requests=(("cpu", "1"),),
    )

    demand = pod_demand(template, ())

    assert demand.requests == {"cpu": Decimal(1), "memory": Decimal(2**30)}
    assert demand.limits == {"cpu": Decimal(4)}
    assert demand.pod_level == frozenset({"cpu"})


def test_a_runtime_class_leaves_cpu_and_memory_not_established() -> None:
    template = _template(
        _container("a", requests={"cpu": "1", "memory": "1Gi", "ephemeral-storage": "1Gi"}),
        runtime_class="kata",
    )

    demand = pod_demand(template, ())

    assert demand.requests == {"ephemeral-storage": Decimal(2**30)}
    assert {"cpu", "memory"} <= demand.unknown_requests
    assert {"cpu", "memory"} <= demand.unknown_limits


def test_an_unparsable_value_leaves_its_resource_not_established() -> None:
    demand = pod_demand(_template(_container("a", requests={"cpu": "lots", "memory": "1Gi"})), ())

    assert demand.requests == {"memory": Decimal(2**30)}
    assert demand.unknown_requests == frozenset({"cpu"})


def test_missing_cpu_and_memory_fields_are_kept_per_container_after_defaults() -> None:
    defaults = _limits(
        "d", LimitValue("Container", "memory", default="1Gi", default_request="512Mi")
    )
    template = _template(
        _container("app", requests={"cpu": "1"}, limits={"cpu": "1"}),
        init=(_container("setup", "init"),),
    )

    demand = pod_demand(template, (defaults,))

    assert demand.missing == (
        ("setup", "requests.cpu"),
        ("setup", "limits.cpu"),
    )


@pytest.mark.parametrize(
    ("containers", "pod_requests", "limit_ranges", "expected"),
    [
        ((_container("a"),), (), (), True),
        ((_container("a", requests={"cpu": "1"}),), (), (), False),
        ((_container("a"),), (("memory", "1Gi"),), (), False),
        ((_container("a", requests={"ephemeral-storage": "1Gi"}),), (), (), True),
        (
            (_container("a"),),
            (),
            (_limits("d", LimitValue("Container", "cpu", default_request="1")),),
            False,
        ),
        (
            (_container("a"),),
            (),
            (
                _limits("x", LimitValue("Container", "cpu", default_request="1")),
                _limits("y", LimitValue("Container", "cpu", default_request="2")),
            ),
            None,
        ),
    ],
    ids=[
        "nothing",
        "container-request",
        "pod-level",
        "only-ephemeral",
        "default",
        "conflicting-default",
    ],
)
def test_best_effort_follows_the_pods_qos_class(
    containers: tuple[ContainerDemand, ...],
    pod_requests: tuple[tuple[str, str], ...],
    limit_ranges: tuple[LimitRangeProjection, ...],
    expected: bool | None,
) -> None:
    template = _template(*containers, pod_requests=pod_requests)

    assert pod_demand(template, limit_ranges).best_effort is expected


@pytest.mark.parametrize(
    ("key", "amount"),
    [
        ("requests.cpu", Decimal("0.5")),
        ("cpu", Decimal("0.5")),
        ("limits.memory", Decimal(2**30)),
        ("memory", Decimal(2**30)),
        ("pods", Decimal(1)),
        ("count/pods", Decimal(1)),
        ("services", Decimal(0)),
        ("requests.nvidia.com/gpu", Decimal(2)),
        ("limits.cpu", None),
    ],
)
def test_the_amount_a_quota_key_charges(key: str, amount: Decimal | None) -> None:
    template = _template(
        _container(
            "a",
            requests={"cpu": "500m", "nvidia.com/gpu": "2"},
            limits={"memory": "1Gi", "cpu": "x"},
        ),
    )
    demand = pod_demand(template, ())

    assert demand.for_quota(key) == amount


def test_one_containers_unknown_value_leaves_the_pods_unknown() -> None:
    demand = pod_demand(
        _template(_container("a", requests={"cpu": "1"}), _container("b", requests={"cpu": "x"})),
        (),
    )

    assert (demand.requests, demand.unknown_requests) == ({}, frozenset({"cpu"}))


def test_an_unparsable_limit_without_a_request_leaves_both_not_established() -> None:
    demand = pod_demand(_template(_container("a", limits={"cpu": "x"})), ())

    assert (demand.unknown_requests, demand.unknown_limits) == (
        frozenset({"cpu"}),
        frozenset({"cpu"}),
    )
    assert demand.missing == (("a", "requests.memory"), ("a", "limits.memory"))


def test_only_container_limits_are_defaults() -> None:
    pod_limits = _limits("p", LimitValue("Pod", "cpu", default="1", default_request="1"))

    demand = pod_demand(_template(_container("a")), (pod_limits,))

    assert (demand.requests, demand.limits, demand.defaults_applied) == ({}, {}, False)


def test_a_pod_level_value_replaces_a_containers_unknown_one() -> None:
    template = _template(
        _container("a", requests={"cpu": "x"}),
        pod_requests=(("cpu", "1"), ("memory", "lots")),
    )

    demand = pod_demand(template, ())

    assert demand.requests == {"cpu": Decimal(1)}
    assert demand.unknown_requests == frozenset({"memory"})


def test_an_init_container_runs_alongside_the_sidecars_started_before_it() -> None:
    template = _template(
        _container("app", requests={"cpu": "100m"}),
        init=(
            _container("proxy", "sidecar", requests={"cpu": "200m"}),
            _container("warm", "init", requests={"cpu": "400m"}),
            _container("late", "sidecar", requests={"cpu": "50m"}),
        ),
    )

    # apps + sidecars = 350m; warm with only the proxy before it = 600m.
    assert pod_demand(template, ()).requests == {"cpu": Decimal("0.6")}


def test_each_containers_values_after_defaults_are_kept_in_order() -> None:
    defaults = _limits("d", LimitValue("Container", "cpu", default="1", default_request="100m"))
    template = _template(
        _container("app", requests={"memory": "x"}),
        init=(_container("setup", "init", limits={"cpu": "2"}),),
    )

    demand = pod_demand(template, (defaults,))

    assert demand.containers == (
        ContainerValues(
            "app", {"cpu": Decimal("0.1")}, {"cpu": Decimal(1)}, frozenset({"memory"}), frozenset()
        ),
        ContainerValues(
            "setup", {"cpu": Decimal(2)}, {"cpu": Decimal(2)}, frozenset(), frozenset()
        ),
    )


def test_zero_values_do_not_change_the_qos_class() -> None:
    zero = _container("a", requests={"cpu": "0"}, limits={"memory": "0"})

    assert pod_demand(_template(zero), ()).best_effort is True


def test_an_unknown_pod_level_value_leaves_the_qos_class_unknown() -> None:
    template = _template(_container("a"), pod_requests=(("cpu", "lots"),))

    assert pod_demand(template, ()).best_effort is None


def test_an_unprefixed_hugepages_key_charges_the_request() -> None:
    demand = pod_demand(_template(_container("a", requests={"hugepages-2Mi": "4Mi"})), ())

    assert demand.for_quota("hugepages-2Mi") == Decimal(4 * 2**20)


def test_defaults_not_read_leave_qos_and_missing_values_open() -> None:
    bare = pod_demand(_template(_container("a")), (), defaults_known=False)
    full = pod_demand(
        _template(_container("a", requests={"cpu": "1"}, limits={"cpu": "1"})),
        (),
        defaults_known=False,
    )

    assert bare.best_effort is None
    assert bare.may_grow("requests.cpu")
    assert not full.may_grow("requests.cpu")
    assert not full.may_grow("pods")
    assert not pod_demand(_template(_container("a")), ()).may_grow("requests.cpu")


def test_a_value_set_at_pod_level_or_set_unreadably_cannot_grow() -> None:
    pod_level = pod_demand(
        _template(_container("a"), pod_requests=(("cpu", "1"),)), (), defaults_known=False
    )
    unreadable = pod_demand(
        _template(_container("a", requests={"cpu": "x"})), (), defaults_known=False
    )

    assert not pod_level.may_grow("requests.cpu")
    assert not unreadable.may_grow("requests.cpu")


def test_defaults_seen_from_an_incomplete_list_are_not_applied() -> None:
    defaults = _limits("d", LimitValue("Container", "cpu", default="1", default_request="100m"))

    demand = pod_demand(_template(_container("a")), (defaults,), defaults_known=False)

    assert (demand.requests, demand.limits, demand.defaults_applied) == ({}, {}, False)
    assert demand.may_grow("requests.cpu")


def test_a_pod_level_limit_stands_in_for_a_missing_pod_level_request() -> None:
    defaults = _limits("d", LimitValue("Container", "cpu", default_request="100m"))
    template = _template(_container("a"), pod_limits=(("cpu", "2"),))

    demand = pod_demand(template, (defaults,))

    assert demand.requests["cpu"] == Decimal(2)
    assert demand.pod_level == frozenset({"cpu"})


def test_a_pod_level_request_wins_over_the_pod_level_limit() -> None:
    template = _template(
        _container("a"), pod_requests=(("cpu", "500m"),), pod_limits=(("cpu", "2"),)
    )

    demand = pod_demand(template, ())

    assert (demand.requests["cpu"], demand.limits["cpu"]) == (Decimal("0.5"), Decimal(2))
```

Create `src/korvid/core/creation_demand.py` with:

```python
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
    # Defaults seen in an incomplete list may conflict with ones not read, so
    # none is applied: explicit values are a lower bound, and `may_grow` says
    # which demands could rise.
    defaults = _defaults(limit_ranges) if defaults_known else (_Defaults(), _Defaults())
    completed = [_complete(c, defaults) for c in (*template.containers, *template.init_containers)]
    containers = [container for container, _ in completed]
    apps = containers[: len(template.containers)]
    inits = containers[len(template.containers) :]
    requests, unknown_requests = _effective(apps, inits, "requests")
    limits, unknown_limits = _effective(apps, inits, "limits")
    # A Pod-level limit stands in for a missing Pod-level request, before admission.
    pod_requests = tuple({**dict(template.pod_limits), **dict(template.pod_requests)}.items())
    pod_level = _override(requests, unknown_requests, pod_requests)
    pod_level |= _override(limits, unknown_limits, template.pod_limits)
    pod_sides = [_side(pod_requests), _side(template.pod_limits)]
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
```

- [ ] Run the tests before (collection error) and after (PASS), then commit: `Compute a workload template's demand as one new Pod`.

### Task 3: The possibilities

Create `tests/core/test_creation_possibilities.py` with:

```python
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
```

Create `src/korvid/core/creation_possibilities.py` with:

```python
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
```

- [ ] Add both modules to the base-import list in `tests/test_optional_extras.py`, run the tests and `tach check`, then commit: `Compute which policies a workload's next Pod could run into`.

### Task 4: Run the full gate

- [ ] Run `make check`, or behind the mirror the same chain with the venv's tools.
- [ ] Mutation-check every comparison, guard and branch. The helper refuses to report on a failing baseline. Record equivalent mutants in the pull request.
