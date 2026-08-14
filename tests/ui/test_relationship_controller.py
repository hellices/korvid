"""Tests for the bounded relationship snapshot loader (issue #281, Task 5).

`RelationshipSnapshotLoader` is a pure async UI-layer orchestrator: it lists
a fixed catalog of core/apps/batch/networking/policy resources plus any
discovered `gateway.networking.k8s.io` resources (Gateway/*Route/
ReferenceGrant), bounds concurrency and total resource count, classifies
per-source failures into `CoverageRecord`s, and hands the collected
`GraphInput`s to the already-tested `build_relationship_graph`. It performs
no Textual operations; the app owns worker lifecycle (starting/cancelling
the load) around it.
"""

from __future__ import annotations

import asyncio

import pytest

from korvid.core.relationships import (
    CoverageState,
    GraphResource,
    SummaryLike,
)
from korvid.core.relationships import build_relationship_graph as _original_build_relationship_graph
from korvid.k8s.discovery import ResourceMeta, build_alias_map
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.ui import relationship_controller
from korvid.ui.relationship_controller import (
    GraphLoadLimits,
    RelationshipSnapshotLoader,
    graph_source_metas,
)

PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
SERVICE_META = ResourceMeta("Service", "services", "", "v1", True)
CONFIG_MAPS_META = ResourceMeta("ConfigMap", "configmaps", "", "v1", True)
SECRETS_META = ResourceMeta("Secret", "secrets", "", "v1", True)
NODES_META = ResourceMeta("Node", "nodes", "", "v1", False)
DEPLOYMENT_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
JOB_META = ResourceMeta("Job", "jobs", "batch", "v1", True)
HTTP_ROUTE_META = ResourceMeta("HTTPRoute", "httproutes", "gateway.networking.k8s.io", "v1", True)
REFERENCE_GRANT_META = ResourceMeta(
    "ReferenceGrant", "referencegrants", "gateway.networking.k8s.io", "v1beta1", True
)
#: `PersistentVolume`/`PersistentVolumeClaim` deliberately sort in opposite
#: relative order by `kind` ("PersistentVolume" < "PersistentVolumeClaim",
#: a prefix) vs by `plural` ("persistentvolumeclaims" < "persistentvolumes",
#: 'c' < 's'). This divergence is what lets a test tell apart a loader-side
#: `(group, plural)`-ordered pre-cap from delegating the cap entirely to
#: `build_relationship_graph`'s `(group, kind, ...)`-ordered one.
PV_META = ResourceMeta("PersistentVolume", "persistentvolumes", "", "v1", False)
PVC_META = ResourceMeta("PersistentVolumeClaim", "persistentvolumeclaims", "", "v1", True)


def _root(kind: str, namespace: str) -> GraphResource:
    return GraphResource(group="", kind=kind, namespace=namespace, name="root")


def _aliases(*metas: ResourceMeta) -> dict[str, ResourceMeta]:
    return build_alias_map(list(metas))


def _many_aliases() -> dict[str, ResourceMeta]:
    return _aliases(PODS_META, SERVICE_META, CONFIG_MAPS_META, SECRETS_META, NODES_META)


def _pod_summary(name: str, *, namespace: str = "prod") -> PodSummary:
    return PodSummary(
        name=name,
        namespace=namespace,
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        uid=name,
    )


def _generic_summary(
    name: str, *, namespace: str = "prod", kind: str = "ConfigMap"
) -> GenericSummary:
    return GenericSummary(name=name, namespace=namespace, kind=kind, created="", uid=name)


class _Lister:
    """Records `(group, plural, namespace)` calls; replays results/errors by key."""

    def __init__(
        self,
        *,
        results: dict[tuple[str, str], list[SummaryLike]] | None = None,
        errors: dict[tuple[str, str], Exception] | None = None,
    ) -> None:
        self._results = results or {}
        self._errors = errors or {}
        self.calls: list[tuple[str, str, str | None]] = []

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[SummaryLike]:
        key = (meta.group, meta.plural)
        self.calls.append((meta.group, meta.plural, namespace))
        if key in self._errors:
            raise self._errors[key]
        return list(self._results.get(key, []))


class _BlockingLister:
    """Blocks every call until `release_all()`; tracks peak concurrency."""

    def __init__(self) -> None:
        self._release = asyncio.Event()
        self._in_flight = 0
        self.peak_concurrency = 0

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[SummaryLike]:
        self._in_flight += 1
        self.peak_concurrency = max(self.peak_concurrency, self._in_flight)
        try:
            await self._release.wait()
        finally:
            self._in_flight -= 1
        return []

    async def wait_until_started(self, count: int) -> None:
        while self._in_flight < count:
            await asyncio.sleep(0)

    def release_all(self) -> None:
        self._release.set()


def test_graph_sources_dedupe_aliases_by_gvr() -> None:
    aliases = {
        "pods": PODS_META,
        "pod": PODS_META,
        "po": PODS_META,
        "services": SERVICE_META,
    }
    sources, _missing = graph_source_metas(_root("Pod", "prod"), "prod", aliases)
    assert [(meta.group, meta.plural) for meta in sources].count(("", "pods")) == 1


def test_optional_gateway_routes_are_selected_when_discovered() -> None:
    aliases = _aliases(HTTP_ROUTE_META, REFERENCE_GRANT_META)
    sources, _missing = graph_source_metas(_root("Ingress", "prod"), "prod", aliases)
    assert HTTP_ROUTE_META in sources
    assert REFERENCE_GRANT_META in sources


def test_source_order_is_group_plural_sorted() -> None:
    aliases = _aliases(SERVICE_META, PODS_META, DEPLOYMENT_META, JOB_META)
    sources, _missing = graph_source_metas(_root("Pod", "prod"), "prod", aliases)
    assert list(sources) == [PODS_META, SERVICE_META, DEPLOYMENT_META, JOB_META]


def test_missing_fixed_source_is_recorded_as_unavailable() -> None:
    _sources, missing = graph_source_metas(_root("Pod", "prod"), "prod", _aliases(PODS_META))
    fixed_missing = [spec for spec in missing if spec.group != "gateway.networking.k8s.io"]
    assert any(spec.plural == "services" for spec in fixed_missing)


async def test_loader_classifies_forbidden_without_failing_other_sources() -> None:
    lister = _Lister(
        results={("", "pods"): [_pod_summary("api-0")]},
        errors={("", "secrets"): ApiStatusError(403, "Forbidden")},
    )
    graph = await RelationshipSnapshotLoader(lister).load(
        _root("Pod", "prod"), "prod", _aliases(PODS_META, SECRETS_META)
    )
    assert any(record.state is CoverageState.COMPLETE for record in graph.coverage)
    assert any(record.state is CoverageState.FORBIDDEN for record in graph.coverage)
    assert graph.incomplete


async def test_missing_gateway_discovery_is_visible_as_unavailable() -> None:
    graph = await RelationshipSnapshotLoader(_Lister()).load(
        _root("Pod", "prod"), "prod", _aliases(PODS_META)
    )
    record = next(item for item in graph.coverage if item.group == "gateway.networking.k8s.io")
    assert record.resource == "*"
    assert record.state is CoverageState.UNAVAILABLE


async def test_loader_uses_namespace_only_for_namespaced_sources() -> None:
    lister = _Lister()
    await RelationshipSnapshotLoader(lister).load(
        _root("Pod", "prod"), "prod", _aliases(PODS_META, NODES_META)
    )
    assert lister.calls == [
        ("", "nodes", None),
        ("", "pods", "prod"),
    ]


async def test_all_namespaces_root_lists_namespaced_sources_with_none() -> None:
    lister = _Lister()
    await RelationshipSnapshotLoader(lister).load(
        _root("Pod", ""), None, _aliases(PODS_META, NODES_META)
    )
    assert lister.calls == [
        ("", "nodes", None),
        ("", "pods", None),
    ]


async def test_loader_respects_concurrency_limit() -> None:
    lister = _BlockingLister()
    task = asyncio.create_task(
        RelationshipSnapshotLoader(lister, limits=GraphLoadLimits(max_concurrency=2)).load(
            _root("Pod", "prod"), "prod", _many_aliases()
        )
    )
    await lister.wait_until_started(2)
    assert lister.peak_concurrency == 2
    lister.release_all()
    await task


async def test_resource_cap_is_visible_and_deterministic() -> None:
    lister = _Lister(
        results={
            ("", "configmaps"): [_generic_summary(f"cfg-{index:02}") for index in range(3)],
            ("", "pods"): [_pod_summary("api-0")],
        }
    )
    graph = await RelationshipSnapshotLoader(lister, limits=GraphLoadLimits(max_resources=2)).load(
        _root("Pod", "prod"), "prod", _aliases(CONFIG_MAPS_META, PODS_META)
    )
    assert [node.name for node in graph.nodes] == ["cfg-00", "cfg-01"]
    assert any(record.state is CoverageState.CAPPED for record in graph.coverage)


async def test_unexpected_api_failure_is_flattened_as_failed() -> None:
    lister = _Lister(errors={("", "pods"): OSError("connection reset")})
    graph = await RelationshipSnapshotLoader(lister).load(
        _root("Pod", "prod"), "prod", _aliases(PODS_META)
    )
    record = next(item for item in graph.coverage if item.resource == "pods")
    assert record.state is CoverageState.FAILED


async def test_loader_never_swallows_cancellation() -> None:
    lister = _BlockingLister()
    task = asyncio.create_task(
        RelationshipSnapshotLoader(lister).load(_root("Pod", "prod"), "prod", _aliases(PODS_META))
    )
    await lister.wait_until_started(1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_loader_pre_caps_by_source_order_before_build_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap must truncate in `(group, plural)` source order *before*
    `build_relationship_graph` ever sees the inputs — not delegate to that
    function's own `(group, kind, ...)` sort, which would keep the wrong
    resource here (`PersistentVolume` sorts before `PersistentVolumeClaim`
    by kind, but after it by plural)."""
    captured_inputs: list[object] = []

    def _spy(inputs: object, coverage: object, limits: object) -> object:
        captured_inputs.extend(inputs)  # type: ignore[arg-type]  # test spy, any iterable
        return _original_build_relationship_graph(inputs, coverage, limits)  # type: ignore[arg-type]  # inputs/coverage/limits typed as object to match the patched signature

    monkeypatch.setattr(relationship_controller, "build_relationship_graph", _spy)

    lister = _Lister(
        results={
            ("", "persistentvolumes"): [
                _generic_summary("pv-a", namespace="", kind="PersistentVolume")
            ],
            ("", "persistentvolumeclaims"): [
                _generic_summary("pvc-a", namespace="prod", kind="PersistentVolumeClaim")
            ],
        }
    )
    graph = await RelationshipSnapshotLoader(lister, limits=GraphLoadLimits(max_resources=1)).load(
        _root("Pod", "prod"), "prod", _aliases(PV_META, PVC_META)
    )

    assert len(captured_inputs) == 1
    kept = captured_inputs[0]
    assert kept.meta.kind == "PersistentVolumeClaim"  # type: ignore[attr-defined]  # captured_inputs is list[object]; real elements are GraphInput
    assert [node.name for node in graph.nodes] == ["pvc-a"]
    assert any(record.state is CoverageState.CAPPED for record in graph.coverage)


async def test_unrelated_error_propagates_instead_of_becoming_failed_coverage() -> None:
    lister = _Lister(errors={("", "pods"): RuntimeError("not a network/API failure")})
    with pytest.raises(RuntimeError, match="not a network/API failure"):
        await RelationshipSnapshotLoader(lister).load(
            _root("Pod", "prod"), "prod", _aliases(PODS_META)
        )


class _SiblingCancellationLister:
    """One source blocks until cancelled; another fails once the blocked
    sibling is provably in flight.

    Lets a test assert that an unexpected error from one LIST does not
    leave its siblings running against a client the caller is about to
    close — without any wall-clock sleep to make the race deterministic.
    """

    def __init__(self, *, blocking: tuple[str, str], failing: tuple[str, str]) -> None:
        self._blocking = blocking
        self._failing = failing
        self._started = asyncio.Event()
        self.cancelled: list[str] = []

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[SummaryLike]:
        key = (meta.group, meta.plural)
        if key == self._blocking:
            self._started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.append(meta.plural)
                raise
        elif key == self._failing:
            await self._started.wait()
            raise RuntimeError("not a network/API failure")
        return []


async def test_unexpected_source_error_cancels_sibling_lists() -> None:
    lister = _SiblingCancellationLister(blocking=("", "pods"), failing=("", "secrets"))
    with pytest.raises(RuntimeError, match="not a network/API failure"):
        await RelationshipSnapshotLoader(lister).load(
            _root("Pod", "prod"), "prod", _aliases(PODS_META, SECRETS_META)
        )
    assert lister.cancelled == ["pods"]


async def test_per_source_api_errors_do_not_cancel_siblings() -> None:
    """A 403/404/network failure is normal per-source coverage, not a
    reason to abandon the rest of the snapshot: every other LIST must still
    run to completion and be classified."""
    lister = _Lister(
        results={("", "pods"): [_pod_summary("api-0")], ("", "configmaps"): []},
        errors={
            ("", "secrets"): ApiStatusError(403, "Forbidden"),
            ("", "nodes"): OSError("connection reset"),
        },
    )
    graph = await RelationshipSnapshotLoader(lister).load(
        _root("Pod", "prod"),
        "prod",
        _aliases(PODS_META, SECRETS_META, CONFIG_MAPS_META, NODES_META),
    )
    states = {
        (record.group, record.resource): record.state
        for record in graph.coverage
        if record.resource in {"pods", "secrets", "configmaps", "nodes"}
    }
    assert states[("", "pods")] is CoverageState.COMPLETE
    assert states[("", "configmaps")] is CoverageState.COMPLETE
    assert states[("", "secrets")] is CoverageState.FORBIDDEN
    assert states[("", "nodes")] is CoverageState.FAILED
    assert [node.name for node in graph.nodes] == ["api-0"]


async def test_source_results_stay_in_source_order_regardless_of_completion_order() -> None:
    """Results are joined in `(group, plural)` source order even when the
    LISTs finish in a different order — the cap and the resulting node list
    must not depend on which API call returned first."""

    class _ReversedCompletionLister:
        """Finishes `pods` only after `services` has already returned."""

        def __init__(self) -> None:
            self._services_done = asyncio.Event()

        async def list_objects(
            self, meta: ResourceMeta, namespace: str | None
        ) -> list[SummaryLike]:
            if meta.plural == "services":
                self._services_done.set()
                return [_generic_summary("svc-a", kind="Service")]
            if meta.plural == "pods":
                await self._services_done.wait()
                return [_pod_summary("api-0")]
            return []

    graph = await RelationshipSnapshotLoader(
        _ReversedCompletionLister(), limits=GraphLoadLimits(max_resources=1)
    ).load(_root("Pod", "prod"), "prod", _aliases(PODS_META, SERVICE_META))
    assert [node.name for node in graph.nodes] == ["api-0"]
