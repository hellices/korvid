"""Bounded operational relationship snapshot loader (issue #281, Task 5).

`RelationshipSnapshotLoader` is a pure async orchestrator: it LISTs a fixed
catalog of core/apps/batch/discovery/networking/policy resource kinds plus
any discovered `gateway.networking.k8s.io` resources (Gateway, `*Route`,
ReferenceGrant), bounds both fan-out concurrency and the total number of
resources fed into the graph, classifies per-source failures into
`CoverageRecord`s, and hands the collected `GraphInput`s to
`build_relationship_graph` (Task 4). It performs no Textual operations —
the app owns worker lifecycle (starting, tracking, and cancelling the load)
around it, exactly like `WatchManager` does for the watch streams `core/`
already owns.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from korvid.core.relationships import (
    CoverageRecord,
    CoverageState,
    GraphInput,
    GraphLimits,
    GraphResource,
    RelationshipGraph,
    SummaryLike,
    build_relationship_graph,
)
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError

#: Gateway API resources are an optional cluster feature discovered at
#: runtime (their plural/version are not fixed), unlike the always-probed
#: core/apps/batch/networking/policy catalog below.
_GATEWAY_GROUP = "gateway.networking.k8s.io"


@dataclass(frozen=True, slots=True)
class GraphSourceSpec:
    """One resource kind fed into a relationship snapshot.

    `optional` marks a source whose absence from the cluster (a missing
    CRD, not an RBAC denial) is expected and unremarkable — a 404/405 LIST
    response is reported as `CoverageState.UNAVAILABLE` rather than
    `CoverageState.FAILED`.
    """

    group: str
    kind: str
    plural: str
    optional: bool = False


@dataclass(frozen=True, slots=True)
class GraphLoadLimits:
    """Bounds on the snapshot LIST fan-out (mirrors `GraphLimits`' caps)."""

    max_concurrency: int = 4
    max_resources: int = 10_000


#: The fixed, always-probed source catalog (issue #281 design doc §Task 5).
_FIXED_SPECS: tuple[GraphSourceSpec, ...] = (
    GraphSourceSpec("", "Pod", "pods"),
    GraphSourceSpec("", "Service", "services"),
    GraphSourceSpec("", "ConfigMap", "configmaps"),
    GraphSourceSpec("", "Secret", "secrets"),
    GraphSourceSpec("", "PersistentVolumeClaim", "persistentvolumeclaims"),
    GraphSourceSpec("", "PersistentVolume", "persistentvolumes"),
    GraphSourceSpec("", "Node", "nodes"),
    GraphSourceSpec("apps", "Deployment", "deployments"),
    GraphSourceSpec("apps", "ReplicaSet", "replicasets"),
    GraphSourceSpec("apps", "StatefulSet", "statefulsets"),
    GraphSourceSpec("apps", "DaemonSet", "daemonsets"),
    GraphSourceSpec("batch", "Job", "jobs"),
    GraphSourceSpec("batch", "CronJob", "cronjobs"),
    GraphSourceSpec("discovery.k8s.io", "EndpointSlice", "endpointslices"),
    GraphSourceSpec("networking.k8s.io", "Ingress", "ingresses"),
    GraphSourceSpec("policy", "PodDisruptionBudget", "poddisruptionbudgets"),
)

#: Frozen singleton so the default argument below is a name lookup, not a
#: call (ruff B008), while still behaving as `limits=GraphLoadLimits()`.
_DEFAULT_LOAD_LIMITS = GraphLoadLimits()

#: Synthetic spec standing in for "no Gateway API resource was discovered at
#: all" — reported as a single grouped unavailable record rather than one
#: record per possible `*Route` kind, since the exact set of Gateway API
#: kinds a cluster supports is provider-defined, not enumerable here.
_GATEWAY_MISSING_SPEC = GraphSourceSpec(_GATEWAY_GROUP, "*", "*", optional=True)


def _is_gateway_resource(meta: ResourceMeta) -> bool:
    return meta.group == _GATEWAY_GROUP and (
        meta.kind == "Gateway" or meta.kind == "ReferenceGrant" or meta.kind.endswith("Route")
    )


def _resolve_fixed(
    spec: GraphSourceSpec, aliases: Mapping[str, ResourceMeta]
) -> ResourceMeta | None:
    meta = aliases.get(spec.plural)
    if meta is not None and meta.group == spec.group and meta.kind == spec.kind:
        return meta
    return None


def graph_source_metas(
    root: GraphResource,
    namespace: str | None,
    aliases: Mapping[str, ResourceMeta],
) -> tuple[tuple[ResourceMeta, ...], tuple[GraphSourceSpec, ...]]:
    """Select this snapshot's LIST sources from discovered `aliases`.

    Returns `(metas, missing_specs)`: `metas` is the deterministically
    `(group, plural)`-sorted set of resource kinds to LIST; `missing_specs`
    is every fixed source not found in `aliases` plus, when no Gateway API
    resource was discovered at all, the single grouped
    `gateway.networking.k8s.io/*` spec — both destined to become
    `CoverageState.UNAVAILABLE` records before any LIST is attempted.

    `root` and `namespace` are accepted for interface symmetry with
    `RelationshipSnapshotLoader.load`; the source catalog itself is fixed
    and does not vary with the currently viewed resource or namespace.
    """
    del root, namespace  # unused: the catalog is root/namespace independent
    selected: dict[tuple[str, str], ResourceMeta] = {}
    missing: list[GraphSourceSpec] = []

    for spec in _FIXED_SPECS:
        meta = _resolve_fixed(spec, aliases)
        if meta is None:
            missing.append(spec)
        else:
            selected[(meta.group, meta.plural)] = meta

    gateway_found = False
    for meta in aliases.values():
        if _is_gateway_resource(meta):
            selected[(meta.group, meta.plural)] = meta
            gateway_found = True
    if not gateway_found:
        missing.append(_GATEWAY_MISSING_SPEC)

    metas = tuple(sorted(selected.values(), key=lambda meta: (meta.group, meta.plural)))
    return metas, tuple(missing)


def _missing_coverage(spec: GraphSourceSpec) -> CoverageRecord:
    return CoverageRecord(
        group=spec.group,
        resource=spec.plural,
        scope="",
        state=CoverageState.UNAVAILABLE,
        detail="not discovered on this cluster",
    )


def _api_error_coverage(meta: ResourceMeta, scope: str, exc: ApiStatusError) -> CoverageRecord:
    if exc.status == 403:
        state = CoverageState.FORBIDDEN
    elif exc.status in (404, 405) and meta.group == _GATEWAY_GROUP:
        state = CoverageState.UNAVAILABLE
    else:
        state = CoverageState.FAILED
    return CoverageRecord(
        group=meta.group, resource=meta.plural, scope=scope, state=state, detail=str(exc)
    )


class Lister(Protocol):
    """The slice of the k8s read surface the loader needs (see `reads.py`)."""

    async def list_objects(
        self, meta: ResourceMeta, namespace: str | None
    ) -> Sequence[SummaryLike]: ...


class RelationshipSnapshotLoader:
    """Loads one bounded `RelationshipGraph` snapshot; owns no worker state.

    Every call to `load` is independent and side-effect free beyond the
    `Lister` calls it awaits: nothing here starts a Textual worker,
    schedules a timer, or retains state across calls. The caller (the app)
    decides when to call it, whether to cancel it, and how often to repeat
    it — the same separation `WatchManager` draws around its watch loops.
    """

    def __init__(self, lister: Lister, *, limits: GraphLoadLimits = _DEFAULT_LOAD_LIMITS) -> None:
        self._lister = lister
        self._limits = limits

    async def load(
        self,
        root: GraphResource,
        namespace: str | None,
        aliases: Mapping[str, ResourceMeta],
    ) -> RelationshipGraph:
        """Return one immutable, bounded `RelationshipGraph` snapshot."""
        metas, missing_specs = graph_source_metas(root, namespace, aliases)
        coverage: list[CoverageRecord] = [_missing_coverage(spec) for spec in missing_specs]
        semaphore = asyncio.Semaphore(self._limits.max_concurrency)

        results = await asyncio.gather(*(self._fetch(meta, namespace, semaphore) for meta in metas))

        inputs: list[GraphInput] = []
        for meta, summaries, record in results:
            coverage.append(record)
            if summaries is not None:
                inputs.extend(GraphInput(meta=meta, summary=summary) for summary in summaries)

        limits = GraphLimits(max_resources=self._limits.max_resources)
        return build_relationship_graph(inputs, coverage, limits)

    async def _fetch(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        semaphore: asyncio.Semaphore,
    ) -> tuple[ResourceMeta, list[SummaryLike] | None, CoverageRecord]:
        list_namespace = namespace if meta.namespaced else None
        scope = list_namespace or ""
        async with semaphore:
            try:
                summaries = await self._lister.list_objects(meta, list_namespace)
            except ApiStatusError as exc:
                return meta, None, _api_error_coverage(meta, scope, exc)
            except Exception as exc:  # declared network/transport failures -> failed
                record = CoverageRecord(
                    group=meta.group,
                    resource=meta.plural,
                    scope=scope,
                    state=CoverageState.FAILED,
                    detail=str(exc),
                )
                return meta, None, record
        record = CoverageRecord(
            group=meta.group, resource=meta.plural, scope=scope, state=CoverageState.COMPLETE
        )
        return meta, list(summaries), record
