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
