"""Bounded operational relationship snapshot loader (issue #281, Task 5).

`RelationshipSnapshotLoader` is a pure async orchestrator: it LISTs a fixed
catalog of core/apps/batch/discovery/networking/policy resource kinds plus
any discovered `gateway.networking.k8s.io` resources (Gateway, `*Route`,
ReferenceGrant), follows discovered routing target kinds not already covered
by that first phase, bounds both fan-out concurrency and the total number of
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
from korvid.k8s.relationship_facts import GATEWAY_GROUP, RelationKind, is_gateway_route_kind

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
    `CoverageState.FAILED`. `detail` is the text an unavailable record
    carries, so a source that is absent for a reason other than plain
    discovery can say which.
    """

    group: str
    kind: str
    plural: str
    optional: bool = False
    detail: str = "not discovered on this cluster"


@dataclass(frozen=True, slots=True)
class GraphLoadLimits:
    """Bounds on the snapshot LIST fan-out (mirrors `GraphLimits`' caps).

    `max_target_lists` bounds the second, cross-namespace phase: a snapshot
    may follow `routes_to` references into the namespaces those references
    explicitly name, but never into more LISTs than this, no matter how
    many distinct namespaces a pathological set of routes points at.
    """

    max_concurrency: int = 4
    max_resources: int = 10_000
    max_target_lists: int = 32


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

    `*Route` membership is decided by `is_gateway_route_kind`, and
    `ReferenceGrant` has its own fact handler. A bare `Gateway` is not an
    automatic source because the phase-one extractor does not interpret its
    listener relationships; it still joins a snapshot when selected as the
    root through `_root_source`.
    """
    if meta.group != _GATEWAY_GROUP:
        return False
    return meta.kind == "ReferenceGrant" or is_gateway_route_kind(meta.group, meta.kind)


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

    The selected `root`'s own kind is included as a source too (see
    `_root_source`), so opening the graph on a discovered custom resource
    outside the fixed/Gateway catalogs still LISTs it and keeps the owner
    references every kind carries. `namespace` is accepted for interface
    symmetry with `RelationshipSnapshotLoader.load`; the catalog itself
    does not vary with it.
    """
    del namespace  # unused: the catalog is namespace independent
    selected: dict[tuple[str, str], ResourceMeta] = {}
    missing: list[GraphSourceSpec] = []

    for spec in _FIXED_SPECS:
        meta = _resolve_fixed(spec, aliases)
        if meta is None:
            missing.append(spec)
        else:
            selected[(meta.group, meta.plural)] = meta

    gateway_discovered = False
    for meta in aliases.values():
        if meta.group == _GATEWAY_GROUP:
            gateway_discovered = True
        if _is_gateway_resource(meta):
            selected[(meta.group, meta.plural)] = meta
    if not gateway_discovered:
        missing.append(_GATEWAY_MISSING_SPEC)

    root_missing = _root_source(root, aliases, selected, missing)
    if root_missing is not None:
        missing.append(root_missing)

    metas = tuple(sorted(selected.values(), key=lambda meta: (meta.group, meta.plural)))
    return metas, tuple(missing)


def _root_source(
    root: GraphResource,
    aliases: Mapping[str, ResourceMeta],
    selected: dict[tuple[str, str], ResourceMeta],
    missing: Sequence[GraphSourceSpec],
) -> GraphSourceSpec | None:
    """Add the selected root's own kind to `selected`, or say why not.

    A discovered, non-synthetic kind outside the fixed/Gateway catalogs (a
    CRD the user is looking at) is added once, keyed the same
    `(group, plural)` way as every other source, so it simply joins the
    deterministic source order and is deduplicated against a catalog entry
    that already covers it.

    When the root's kind has no listable API resource — discovery never
    reported it, or it is one of korvid's synthetic views — the caller
    records that honestly instead. No plural is invented for a kind
    discovery never described: the record names the kind itself.
    """
    if any(meta.group == root.group and meta.kind == root.kind for meta in selected.values()):
        return None
    if any(spec.group == root.group and spec.kind == root.kind for spec in missing):
        return None  # a fixed-source record already reports this exact kind
    meta = _metas_by_group_kind(aliases).get((root.group, root.kind))
    if meta is not None and not meta.synthetic:
        selected[(meta.group, meta.plural)] = meta
        return None
    detail = (
        "korvid-invented view with no API resource to list"
        if meta is not None
        else "not discovered on this cluster"
    )
    return GraphSourceSpec(root.group, root.kind, root.kind, optional=True, detail=detail)


def _missing_coverage(spec: GraphSourceSpec) -> CoverageRecord:
    return CoverageRecord(
        group=spec.group,
        resource=spec.plural,
        scope="",
        state=CoverageState.UNAVAILABLE,
        detail=spec.detail,
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


_FetchedSource = tuple[ResourceMeta, list[SummaryLike], str]


def _is_root_summary(meta: ResourceMeta, summary: SummaryLike, root: GraphResource) -> bool:
    return (
        meta.group == root.group
        and meta.kind == root.kind
        and summary.namespace == root.namespace
        and summary.name == root.name
        and (root.uid is None or summary.uid == root.uid)
    )


def _ordered_input_buckets(
    fetched: Sequence[_FetchedSource],
    root: GraphResource,
) -> list[_FetchedSource]:
    """Sort each source and put the selected root's source first."""
    buckets: list[_FetchedSource] = []
    for meta, summaries, scope in fetched:
        ordered = sorted(summaries, key=_summary_sort_key)
        root_index = next(
            (
                index
                for index, summary in enumerate(ordered)
                if _is_root_summary(meta, summary, root)
            ),
            None,
        )
        if root_index is not None:
            ordered.insert(0, ordered.pop(root_index))
        buckets.append((meta, ordered, scope))
    buckets.sort(key=lambda bucket: bucket[0].group != root.group or bucket[0].kind != root.kind)
    return buckets


def _cap_inputs(
    fetched: Sequence[_FetchedSource],
    max_resources: int,
    root: GraphResource,
) -> tuple[list[GraphInput], list[CoverageRecord]]:
    """Bound inputs while preserving the root and sharing space across sources."""
    buckets = _ordered_input_buckets(fetched, root)
    positions = [0] * len(buckets)
    inputs: list[GraphInput] = []
    limit = max(max_resources, 0)
    while len(inputs) < limit:
        added = False
        for index, (meta, summaries, _scope) in enumerate(buckets):
            if positions[index] >= len(summaries):
                continue
            inputs.append(GraphInput(meta=meta, summary=summaries[positions[index]]))
            positions[index] += 1
            added = True
            if len(inputs) >= limit:
                break
        if not added:
            break

    coverage: list[CoverageRecord] = []
    for position, (meta, summaries, scope) in zip(positions, buckets, strict=True):
        dropped = len(summaries) - position
        if dropped == 0:
            continue
        coverage.append(
            CoverageRecord(
                group=meta.group,
                resource=meta.plural,
                scope=scope,
                state=CoverageState.CAPPED,
                detail=f"{dropped} input resource(s) dropped at the {limit}-resource cap",
            )
        )
    return inputs, coverage


class Lister(Protocol):
    """The slice of the k8s read surface the loader needs (see `reads.py`)."""

    async def list_objects(
        self, meta: ResourceMeta, namespace: str | None
    ) -> Sequence[SummaryLike]: ...


#: The kind every cross-namespace `routes_to` follow-up also LISTs: only a
#: Gateway API `ReferenceGrant` *in the target namespace* can authorize such
#: a reference, so loading the backend without it would default-deny an
#: authorized route.
_REFERENCE_GRANT_KIND = "ReferenceGrant"


def _metas_by_group_kind(
    aliases: Mapping[str, ResourceMeta],
) -> dict[tuple[str, str], ResourceMeta]:
    """Index discovered resources by `(group, kind)` (aliases collapse)."""
    return {(meta.group, meta.kind): meta for meta in aliases.values()}


def _cross_namespace_route_targets(
    fetched: Sequence[_FetchedSource],
) -> dict[str, set[tuple[str, str]]]:
    """The `(group, kind)`s each namespace is referenced for from outside it.

    A `routes_to` fact is cross-namespace when it explicitly names a
    namespace other than its own subject's — the same test the graph uses
    to decide an edge needs `ReferenceGrant` authorization. This is derived
    from the facts alone, so it is meaningful whether or not the snapshot
    goes on to issue follow-up LISTs.
    """
    targets: dict[str, set[tuple[str, str]]] = {}
    for meta, summaries, _scope in fetched:
        for summary in summaries:
            if not is_gateway_route_kind(meta.group, meta.kind):
                continue
            for fact in summary.relationships.references:
                target = fact.target
                if fact.relation is not RelationKind.ROUTES_TO:
                    continue
                if not target.namespace or target.namespace == summary.namespace:
                    continue
                targets.setdefault(target.namespace, set()).add((target.group, target.kind))
    return targets


def _follow_up_route_targets(
    fetched: Sequence[_FetchedSource],
) -> dict[str, set[tuple[str, str]]]:
    """Referenced route target kinds that can affect graph resolution.

    Same-namespace targets are meaningful for every routing source. A
    cross-namespace target is followed only for a Gateway API Route, where
    `ReferenceGrant` defines whether the edge is valid. Other cross-namespace
    `ROUTES_TO` facts (notably EndpointSlice targetRefs) are already invalid
    and cannot become resolvable by loading the named object.
    """
    targets: dict[str, set[tuple[str, str]]] = {}
    for meta, summaries, _scope in fetched:
        for summary in summaries:
            gateway_route = is_gateway_route_kind(meta.group, meta.kind)
            for fact in summary.relationships.references:
                target = fact.target
                if fact.relation is not RelationKind.ROUTES_TO:
                    continue
                target_namespace = target.namespace or summary.namespace
                if not target_namespace:
                    continue
                if target_namespace != summary.namespace and not gateway_route:
                    continue
                targets.setdefault(target_namespace, set()).add((target.group, target.kind))
    return targets


def _listed_in_first_phase(
    group_kind: tuple[str, str],
    target_namespace: str,
    namespace: str | None,
    first_phase: Sequence[ResourceMeta],
) -> bool:
    if namespace is not None and target_namespace != namespace:
        return False
    return any((meta.group, meta.kind) == group_kind and meta.namespaced for meta in first_phase)


def _target_namespace_requests(
    fetched: Sequence[_FetchedSource],
    namespace: str | None,
    aliases: Mapping[str, ResourceMeta],
    first_phase: Sequence[ResourceMeta],
) -> list[tuple[ResourceMeta, str]]:
    """The deterministic second-phase `(meta, namespace)` LISTs to run.

    This LISTs each referenced discovered kind not already covered by the
    first-phase catalog. Cross-namespace Gateway Route targets also require
    `ReferenceGrant` in the target namespace. Requests are sorted by
    `(namespace, group, plural)` and deduplicated, so repeated references
    still cost one LIST per resource.

    A kind that discovery never reported is skipped rather than guessed at:
    an API resource absent from discovery has no objects to list. So is a
    `synthetic` kind — a korvid-invented view (the helm browser and friends)
    shares the alias map but has no API endpoint at all, exactly as
    `_selected_relationship_root` already refuses one as a graph root.
    """
    targets = _follow_up_route_targets(fetched)
    grant_targets = _cross_namespace_route_targets(fetched)
    by_group_kind = _metas_by_group_kind(aliases)
    requests: list[tuple[ResourceMeta, str]] = []
    for target_namespace in sorted(targets):
        wanted = set(targets[target_namespace])
        if target_namespace in grant_targets:
            wanted.add((_GATEWAY_GROUP, _REFERENCE_GRANT_KIND))
        metas: dict[tuple[str, str], ResourceMeta] = {}
        for group_kind in wanted:
            if _listed_in_first_phase(group_kind, target_namespace, namespace, first_phase):
                continue
            meta = by_group_kind.get(group_kind)
            if meta is not None and meta.namespaced and not meta.synthetic:
                metas[(meta.group, meta.plural)] = meta
        requests.extend((meta, target_namespace) for _key, meta in sorted(metas.items()))
    return requests


def _unavailable_target_coverage(
    fetched: Sequence[_FetchedSource],
    namespace: str | None,
    aliases: Mapping[str, ResourceMeta],
    first_phase: Sequence[ResourceMeta],
    limit: int,
) -> list[CoverageRecord]:
    """Bounded records for referenced kinds discovery cannot safely LIST."""
    by_group_kind = _metas_by_group_kind(aliases)
    gaps: set[tuple[str, str, str, str]] = set()
    for target_namespace, group_kinds in _follow_up_route_targets(fetched).items():
        for group, kind in group_kinds:
            if _listed_in_first_phase((group, kind), target_namespace, namespace, first_phase):
                continue
            meta = by_group_kind.get((group, kind))
            if meta is not None and meta.namespaced and not meta.synthetic:
                continue
            if meta is None:
                detail = "referenced kind was not discovered; target could not be listed"
            elif meta.synthetic:
                detail = "referenced kind is synthetic and has no Kubernetes LIST endpoint"
            else:
                detail = "referenced kind is cluster-scoped but the reference names a namespace"
            gaps.add((target_namespace, group, kind, detail))

    ordered = sorted(gaps)
    bounded = max(limit, 0)
    records = [
        CoverageRecord(
            group=group,
            resource=kind,
            scope=scope,
            state=CoverageState.UNAVAILABLE,
            detail=detail,
        )
        for scope, group, kind, detail in ordered[:bounded]
    ]
    if len(ordered) > bounded:
        records.append(
            CoverageRecord(
                group="",
                resource="*",
                scope="",
                state=CoverageState.CAPPED,
                detail=(
                    f"{len(ordered) - bounded} unavailable target coverage record(s) "
                    f"dropped at the {bounded}-target-list cap"
                ),
            )
        )
    return records


def _cap_target_requests(
    requests: Sequence[tuple[ResourceMeta, str]], max_target_lists: int
) -> tuple[list[tuple[ResourceMeta, str]], CoverageRecord | None]:
    """Truncate follow-up LISTs to `max_target_lists`, visibly."""
    limit = max(max_target_lists, 0)
    if len(requests) <= limit:
        return list(requests), None
    dropped = len(requests) - limit
    record = CoverageRecord(
        group="",
        resource="*",
        scope="",
        state=CoverageState.CAPPED,
        detail=f"{dropped} target-namespace LIST(s) dropped at the {limit}-target-list cap",
    )
    return list(requests[:limit]), record


def _target_partial_coverage(
    requests: Sequence[tuple[ResourceMeta, str]],
) -> list[CoverageRecord]:
    """Record that follow-ups cover selected kinds, not whole namespaces."""
    return [
        CoverageRecord(
            group="",
            resource="*",
            scope=scope,
            state=CoverageState.PARTIAL,
            detail="target follow-up listed only referenced kinds in this namespace",
        )
        for scope in sorted({scope for _meta, scope in requests})
    ]


def _grant_gap_record(scope: str) -> CoverageRecord:
    return CoverageRecord(
        group=_GATEWAY_GROUP,
        resource=_REFERENCE_GRANT_KIND,
        scope=scope,
        state=CoverageState.UNAVAILABLE,
        detail=(
            "ReferenceGrant is not discovered on this cluster; cross-namespace "
            "references stay denied with no authorization source to read"
        ),
    )


def _missing_grant_coverage(
    fetched: Sequence[_FetchedSource],
    namespace: str | None,
    requests: Sequence[tuple[ResourceMeta, str]],
    aliases: Mapping[str, ResourceMeta],
) -> list[CoverageRecord]:
    """Records for the namespaces whose authorization source is absent.

    A cluster can serve Gateway Routes without the `ReferenceGrant` kind
    being discovered at all. Those routes still load and their
    cross-namespace edges still default-deny — but with no grant source to
    read, that denial is not evidence, so the gap is recorded instead of
    the snapshot reporting complete coverage.

    A namespaced snapshot reports one record per target namespace it
    actually loaded (deduplicated and sorted, so the records stay bounded
    by the follow-up cap and never describe a namespace this snapshot did
    not read). An all-namespaces snapshot issues no follow-ups at all, yet
    has exactly the same gap — it is reported once, cluster-wide, whenever
    any `routes_to` fact crossed a namespace, never once per route or per
    target namespace.

    Nothing is reported when the kind *is* discovered (an empty LIST is a
    read that happened) or when no reference ever crossed a namespace.
    """
    if _metas_by_group_kind(aliases).get((_GATEWAY_GROUP, _REFERENCE_GRANT_KIND)) is not None:
        return []
    if namespace is None:
        if not _cross_namespace_route_targets(fetched):
            return []
        return [_grant_gap_record("")]
    target_namespaces = _cross_namespace_route_targets(fetched)
    requested_scopes = {target_namespace for _meta, target_namespace in requests}
    return [
        _grant_gap_record(target_namespace)
        for target_namespace in sorted(target_namespaces.keys() & requested_scopes)
    ]


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


def _join_results(
    results: Sequence[_FetchResult], coverage: list[CoverageRecord]
) -> list[_FetchedSource]:
    """Record every LIST's coverage; keep the ones that returned summaries."""
    fetched: list[_FetchedSource] = []
    for meta, summaries, record in results:
        coverage.append(record)
        if summaries is not None:
            fetched.append((meta, summaries, record.scope))
    return fetched


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
        """Return one immutable, bounded `RelationshipGraph` snapshot.

        Sources are listed in two phases. The first LISTs the fixed catalog
        in the requested namespace (cluster-scoped kinds cluster-wide). The
        second follows `routes_to` references to discovered kinds not already
        listed there. A cross-namespace Gateway Route also LISTs
        `ReferenceGrant` in the target namespace, where both the backend and
        the grant that authorizes it live.
        """
        metas, missing_specs = graph_source_metas(root, namespace, aliases)
        coverage: list[CoverageRecord] = [_missing_coverage(spec) for spec in missing_specs]
        semaphore = asyncio.Semaphore(self._limits.max_concurrency)

        results = await self._gather([(meta, namespace) for meta in metas], semaphore)
        fetched = _join_results(results, coverage)

        requests, target_cap_record = _cap_target_requests(
            _target_namespace_requests(fetched, namespace, aliases, metas),
            self._limits.max_target_lists,
        )
        if target_cap_record is not None:
            coverage.append(target_cap_record)
        coverage.extend(
            _unavailable_target_coverage(
                fetched,
                namespace,
                aliases,
                metas,
                self._limits.max_target_lists,
            )
        )
        coverage.extend(_missing_grant_coverage(fetched, namespace, requests, aliases))
        if requests:
            coverage.extend(_target_partial_coverage(requests))
            target_results = await self._gather(list(requests), semaphore)
            fetched.extend(_join_results(target_results, coverage))

        inputs, cap_records = _cap_inputs(fetched, self._limits.max_resources, root)
        coverage.extend(cap_records)

        # The fair, root-prioritized cap already ran above; `GraphLimits` here
        # only carries the configured `max_resources` through into the
        # resulting graph's metadata — `len(inputs) <= max_resources`
        # always holds by construction, so this never triggers a second,
        # differently-ordered cap inside `build_relationship_graph`.
        limits = GraphLimits(max_resources=self._limits.max_resources)
        return build_relationship_graph(inputs, coverage, limits)

    async def _gather(
        self,
        requests: Sequence[tuple[ResourceMeta, str | None]],
        semaphore: asyncio.Semaphore,
    ) -> list[_FetchResult]:
        """Run one phase's LISTs concurrently, in request order.

        Explicit tasks (rather than bare coroutines) so a source failure
        `_fetch` deliberately does not classify — or a cancellation of
        this load — leaves no sibling LIST running against a client the
        caller may be about to close. `gather` still returns results in
        request order, so the join stays deterministic.
        """
        tasks = [
            asyncio.create_task(self._fetch(meta, request_namespace, semaphore))
            for meta, request_namespace in requests
        ]
        try:
            return list(await asyncio.gather(*tasks))
        finally:
            await _cancel_pending(tasks)

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
