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
from korvid.k8s.relationship_facts import GATEWAY_GROUP, is_gateway_route_kind

#: Gateway API resources are an optional cluster feature discovered at
#: runtime (their plural/version are not fixed), unlike the always-probed
#: core/apps/batch/networking/policy catalog below.
_GATEWAY_GROUP = GATEWAY_GROUP


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
    """A discovered Gateway API resource this snapshot can actually use.

    `*Route` membership is decided by `is_gateway_route_kind`, the same
    predicate the fact extractor uses to pick a route's backendRef handler,
    so no kind is ever LISTed (and reported as `complete` coverage) that
    the extractor would silently ignore.
    """
    if meta.group != _GATEWAY_GROUP:
        return False
    return (
        meta.kind == "Gateway"
        or meta.kind == "ReferenceGrant"
        or is_gateway_route_kind(meta.group, meta.kind)
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


def _summary_sort_key(summary: SummaryLike) -> tuple[str, str, str]:
    return (summary.namespace, summary.name, summary.uid)


def _cap_inputs(
    fetched: Sequence[tuple[ResourceMeta, list[SummaryLike]]],
    max_resources: int,
) -> tuple[list[GraphInput], CoverageRecord | None]:
    """Truncate `fetched` to at most `max_resources`, in the caller's
    `(group, plural)` source order, deterministically ordering each
    source's own summaries by `(namespace, name, uid)` first.

    This truncation happens before `build_relationship_graph` ever sees the
    inputs: that function's own cap sorts by `(group, kind, namespace,
    name, uid)`, which is not always equivalent to `(group, plural)` source
    order (e.g. `PersistentVolume` sorts before `PersistentVolumeClaim` by
    kind, but after it by plural) and is not the ordering the loader's
    caller requested a snapshot in.
    """
    inputs: list[GraphInput] = []
    remaining = max_resources
    dropped = 0
    for meta, summaries in fetched:
        ordered = sorted(summaries, key=_summary_sort_key)
        if remaining <= 0:
            dropped += len(ordered)
            continue
        keep, drop = ordered[:remaining], ordered[remaining:]
        inputs.extend(GraphInput(meta=meta, summary=summary) for summary in keep)
        remaining -= len(keep)
        dropped += len(drop)

    if dropped == 0:
        return inputs, None
    record = CoverageRecord(
        group="",
        resource="*",
        scope="",
        state=CoverageState.CAPPED,
        detail=f"{dropped} input resource(s) dropped at the {max_resources}-resource cap",
    )
    return inputs, record


class Lister(Protocol):
    """The slice of the k8s read surface the loader needs (see `reads.py`)."""

    async def list_objects(
        self, meta: ResourceMeta, namespace: str | None
    ) -> Sequence[SummaryLike]: ...


#: One source LIST's outcome: its meta, its summaries (None when the LIST
#: failed in a way `_fetch` classifies), and the coverage record it earned.
_FetchResult = tuple[ResourceMeta, list[SummaryLike] | None, CoverageRecord]


async def _cancel_pending(tasks: Sequence[asyncio.Task[_FetchResult]]) -> None:
    """Cancel and reap every LIST task that has not finished.

    Runs on every exit path of `load`: an unexpected error from one source
    (or cancellation of the load itself) must not leave siblings listing
    against a client that is about to be closed. `return_exceptions=True`
    means a sibling that failed in the same tick is reaped here rather than
    surfacing as a never-retrieved task exception — and, more importantly,
    never replaces the original error propagating out of `load`.
    """
    pending = [task for task in tasks if not task.done()]
    if not pending:
        return
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


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

        # Explicit tasks (rather than bare coroutines) so a source failure
        # `_fetch` deliberately does not classify — or a cancellation of
        # this load — leaves no sibling LIST running against a client the
        # caller may be about to close. `gather` still returns results in
        # source order, so the join below stays deterministic.
        tasks = [asyncio.create_task(self._fetch(meta, namespace, semaphore)) for meta in metas]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            await _cancel_pending(tasks)

        fetched: list[tuple[ResourceMeta, list[SummaryLike]]] = []
        for meta, summaries, record in results:
            coverage.append(record)
            if summaries is not None:
                fetched.append((meta, summaries))

        inputs, cap_record = _cap_inputs(fetched, self._limits.max_resources)
        if cap_record is not None:
            coverage.append(cap_record)

        # The cap already ran above (in source order); `GraphLimits` here
        # only carries the configured `max_resources` through into the
        # resulting graph's metadata — `len(inputs) <= max_resources`
        # always holds by construction, so this never triggers a second,
        # differently-ordered cap inside `build_relationship_graph`.
        limits = GraphLimits(max_resources=self._limits.max_resources)
        return build_relationship_graph(inputs, coverage, limits)

    async def _fetch(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        semaphore: asyncio.Semaphore,
    ) -> _FetchResult:
        list_namespace = namespace if meta.namespaced else None
        scope = list_namespace or ""
        async with semaphore:
            try:
                summaries = await self._lister.list_objects(meta, list_namespace)
            except ApiStatusError as exc:
                return meta, None, _api_error_coverage(meta, scope, exc)
            except OSError as exc:  # declared network/transport failures -> failed
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
