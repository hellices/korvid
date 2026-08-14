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
from korvid.k8s.discovery import ResourceMeta, build_alias_map
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
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
