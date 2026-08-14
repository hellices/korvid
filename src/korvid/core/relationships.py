"""Immutable operational relationship graph model (issue #281).

This module is a pure core builder: it joins the metadata-only
`RelationshipFacts` already attached to list/watch summaries (Task 2) into an
immutable, deterministically bounded graph of `RelationshipEdge` values with
explicit `CoverageRecord` completeness and per-edge `EdgeResolution` states.

Only `name`, `namespace`, `uid`, `labels`, and `relationships` are ever read
from a summary. Summary status/custom-column data and raw manifests are
never retained in a `GraphResource` or `RelationshipEdge`.

Direction is always dependent -> dependency: a `RelationshipEdge.subject` is
the resource that declares or exhibits the relationship and
`RelationshipEdge.target` is the resource it references or matches. Reverse
(`dependents_of`) queries return the resources that depend on a given one.

Named/UID-based reference resolution only; selector- and routing-based joins
(Task 4) are out of scope here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.relationship_facts import (
    FactConfidence,
    ReferenceFact,
    RelationKind,
    RelationshipFacts,
)

#: Control characters (including DEL) flattened out of coverage detail text.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
#: Coverage detail is bounded so a pathological RBAC/API error message can
#: never grow the graph snapshot unboundedly.
_MAX_DETAIL_LENGTH = 512


class EdgeResolution(StrEnum):
    """Whether a `RelationshipEdge` target was found in the graph."""

    RESOLVED = "resolved"
    MISSING = "missing"
    INVALID = "invalid"


class CoverageState(StrEnum):
    """Completeness of one attempted resource LIST feeding the graph."""

    COMPLETE = "complete"
    FORBIDDEN = "forbidden"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    CAPPED = "capped"


@dataclass(frozen=True, slots=True)
class GraphResource:
    """A node identity: API group, kind, namespace, name, and optional UID.

    An unresolved reference target is retained with the UID (or `None`) it
    was declared with, rather than being silently reconnected to a different
    live object that happens to share its name.
    """

    group: str
    kind: str
    namespace: str
    name: str
    uid: str | None = None


@dataclass(frozen=True, slots=True)
class EvidencePointer:
    """The subject resource and JSON field path an edge was derived from."""

    resource: GraphResource
    field: str


@dataclass(frozen=True, slots=True)
class RelationshipEdge:
    """One directed, dependent-to-dependency relationship."""

    subject: GraphResource
    target: GraphResource
    relation: RelationKind
    confidence: FactConfidence
    evidence: EvidencePointer
    resolution: EdgeResolution
    explanation: str = ""


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    """Completeness of one attempted resource LIST.

    `detail` is defensively sanitized: control characters (including
    newlines) are flattened to spaces and the text is capped at
    `_MAX_DETAIL_LENGTH` characters so a pathological error message cannot
    grow the graph snapshot unboundedly or break single-line rendering.
    """

    group: str
    resource: str
    scope: str
    state: CoverageState
    detail: str = ""

    def __post_init__(self) -> None:
        flattened = _CONTROL_CHARS.sub(" ", self.detail)
        object.__setattr__(self, "detail", flattened[:_MAX_DETAIL_LENGTH])


class SummaryLike(Protocol):
    """The structural subset of a list/watch summary the graph may read.

    `GenericSummary` and `PodSummary` (and their subclasses) both satisfy
    this shape without a shared base class; the graph never imports either
    concrete type so it cannot accidentally reach into `custom`, status
    text, or any other summary field.
    """

    # Declared as read-only properties (rather than plain annotations) so
    # frozen dataclasses such as GenericSummary/PodSummary -- whose fields
    # mypy treats as read-only -- satisfy this Protocol.
    @property
    def name(self) -> str: ...
    @property
    def namespace(self) -> str: ...
    @property
    def uid(self) -> str: ...
    @property
    def labels(self) -> tuple[tuple[str, str], ...]: ...
    @property
    def relationships(self) -> RelationshipFacts: ...


@dataclass(frozen=True, slots=True)
class GraphInput:
    """One discovered resource: its API discovery metadata and its summary."""

    meta: ResourceMeta
    summary: SummaryLike


@dataclass(frozen=True, slots=True)
class GraphLimits:
    """Deterministic caps applied while building and traversing the graph."""

    max_resources: int = 10_000
    max_edges: int = 50_000
    max_depth: int = 5
    max_nodes: int = 500


@dataclass(frozen=True, slots=True)
class TraversalResult:
    """The result of a bounded breadth-first `walk_dependents` traversal."""

    edges: tuple[RelationshipEdge, ...]
    cycles: tuple[RelationshipEdge, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class RelationshipGraph:
    """An immutable snapshot: nodes, edges, coverage, limits, and caps."""

    nodes: tuple[GraphResource, ...]
    edges: tuple[RelationshipEdge, ...]
    coverage: tuple[CoverageRecord, ...]
    limits: GraphLimits = field(default_factory=GraphLimits)
    truncated: bool = False

    @property
    def incomplete(self) -> bool:
        """True when any coverage record is not `CoverageState.COMPLETE`.

        A missing target or an invalid cross-namespace reference is an edge
        resolution state, not proof that no dependency exists; only
        incomplete *coverage* makes the graph itself incomplete.
        """
        return any(record.state is not CoverageState.COMPLETE for record in self.coverage)

    def dependencies_of(self, resource: GraphResource) -> tuple[RelationshipEdge, ...]:
        """Edges declared by `resource` (what it depends on)."""
        return tuple(edge for edge in self.edges if edge.subject == resource)

    def dependents_of(self, resource: GraphResource) -> tuple[RelationshipEdge, ...]:
        """Edges that reference `resource` (what depends on it)."""
        return tuple(edge for edge in self.edges if edge.target == resource)

    def walk_dependents(
        self,
        resource: GraphResource,
        *,
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ) -> TraversalResult:
        """Breadth-first traversal of dependents, excluding `resource` itself.

        Resources are deduplicated by full `GraphResource` identity. An edge
        that returns to an already-visited resource (including `resource`
        itself) is recorded in `cycles` rather than being traversed again.
        `truncated` is set once the node cap is reached, or when the depth
        cap stops traversal while further dependents remain unexplored.
        """
        depth_limit = self.limits.max_depth if max_depth is None else max_depth
        node_limit = self.limits.max_nodes if max_nodes is None else max_nodes

        visited = {resource}
        edges: list[RelationshipEdge] = []
        cycles: list[RelationshipEdge] = []
        frontier = [resource]
        depth = 0
        truncated = False
        added_nodes = 0

        while frontier and depth < depth_limit and not truncated:
            depth += 1
            next_frontier: list[GraphResource] = []
            for current in frontier:
                for edge in self.dependents_of(current):
                    if edge.subject in visited:
                        cycles.append(edge)
                        continue
                    if added_nodes >= node_limit:
                        truncated = True
                        break
                    visited.add(edge.subject)
                    added_nodes += 1
                    edges.append(edge)
                    next_frontier.append(edge.subject)
                if truncated:
                    break
            frontier = next_frontier

        if frontier and not truncated:
            # The depth cap stopped traversal while dependents remained.
            truncated = True

        return TraversalResult(edges=tuple(edges), cycles=tuple(cycles), truncated=truncated)


def _input_sort_key(item: GraphInput) -> tuple[str, str, str, str, str]:
    return (
        item.meta.group,
        item.meta.kind,
        item.summary.namespace,
        item.summary.name,
        item.summary.uid,
    )


def _node_for(item: GraphInput) -> GraphResource:
    return GraphResource(
        group=item.meta.group,
        kind=item.meta.kind,
        namespace=item.summary.namespace,
        name=item.summary.name,
        uid=item.summary.uid or None,
    )


def _build_edge(
    subject: GraphResource,
    fact: ReferenceFact,
    by_uid: dict[tuple[str, str], GraphResource],
    by_name: dict[tuple[str, str, str, str], GraphResource],
) -> RelationshipEdge:
    target_ref = fact.target
    evidence = EvidencePointer(resource=subject, field=fact.field)

    # A Gateway backend is the sole relation Task 4 may authorize across
    # namespaces (via a ReferenceGrant); every other namespaced reference
    # that names a different namespace than its subject is invalid.
    if (
        target_ref.namespace
        and target_ref.namespace != subject.namespace
        and fact.relation is not RelationKind.ROUTES_TO
    ):
        target = GraphResource(
            group=target_ref.group,
            kind=target_ref.kind,
            namespace=target_ref.namespace,
            name=target_ref.name,
            uid=target_ref.uid,
        )
        explanation = (
            "cross-namespace owner reference: subject namespace "
            f"{subject.namespace!r} does not match target namespace "
            f"{target_ref.namespace!r}"
        )
        return RelationshipEdge(
            subject=subject,
            target=target,
            relation=fact.relation,
            confidence=fact.confidence,
            evidence=evidence,
            resolution=EdgeResolution.INVALID,
            explanation=explanation,
        )

    if target_ref.uid is not None:
        resolved = by_uid.get((target_ref.group, target_ref.uid))
        if resolved is not None:
            return RelationshipEdge(
                subject=subject,
                target=resolved,
                relation=fact.relation,
                confidence=fact.confidence,
                evidence=evidence,
                resolution=EdgeResolution.RESOLVED,
                explanation="",
            )
        stale_target = GraphResource(
            group=target_ref.group,
            kind=target_ref.kind,
            namespace=target_ref.namespace,
            name=target_ref.name,
            uid=target_ref.uid,
        )
        return RelationshipEdge(
            subject=subject,
            target=stale_target,
            relation=fact.relation,
            confidence=fact.confidence,
            evidence=evidence,
            resolution=EdgeResolution.MISSING,
            explanation=f"no observed resource has uid {target_ref.uid!r}",
        )

    resolved = by_name.get(
        (target_ref.group, target_ref.kind, target_ref.namespace, target_ref.name)
    )
    if resolved is not None:
        return RelationshipEdge(
            subject=subject,
            target=resolved,
            relation=fact.relation,
            confidence=fact.confidence,
            evidence=evidence,
            resolution=EdgeResolution.RESOLVED,
            explanation="",
        )
    named_target = GraphResource(
        group=target_ref.group,
        kind=target_ref.kind,
        namespace=target_ref.namespace,
        name=target_ref.name,
        uid=None,
    )
    return RelationshipEdge(
        subject=subject,
        target=named_target,
        relation=fact.relation,
        confidence=fact.confidence,
        evidence=evidence,
        resolution=EdgeResolution.MISSING,
        explanation=f"no observed resource named {target_ref.name!r} was found",
    )


#: Frozen singleton so the default argument below is a name lookup, not a
#: call (ruff B008), while still behaving as `limits=GraphLimits()`.
_DEFAULT_LIMITS = GraphLimits()


def build_relationship_graph(
    inputs: Sequence[GraphInput],
    coverage: Sequence[CoverageRecord],
    limits: GraphLimits = _DEFAULT_LIMITS,
) -> RelationshipGraph:
    """Build an immutable `RelationshipGraph` from discovered inputs.

    Inputs are sorted by `(group, kind, namespace, name, uid)` before caps
    are applied so which resources/edges survive a cap is deterministic
    regardless of discovery order. Only `name`, `namespace`, `uid`,
    `labels`, and `relationships` are ever read from each input's summary.
    """
    sorted_inputs = sorted(inputs, key=_input_sort_key)
    truncated = False
    extra_coverage: list[CoverageRecord] = []

    if len(sorted_inputs) > limits.max_resources:
        dropped = len(sorted_inputs) - limits.max_resources
        sorted_inputs = sorted_inputs[: limits.max_resources]
        truncated = True
        extra_coverage.append(
            CoverageRecord(
                group="",
                resource="*",
                scope="",
                state=CoverageState.CAPPED,
                detail=f"{dropped} input resource(s) dropped at the {limits.max_resources}-resource cap",
            )
        )

    nodes: list[GraphResource] = []
    by_uid: dict[tuple[str, str], GraphResource] = {}
    by_name: dict[tuple[str, str, str, str], GraphResource] = {}
    for item in sorted_inputs:
        resource = _node_for(item)
        nodes.append(resource)
        by_name[(resource.group, resource.kind, resource.namespace, resource.name)] = resource
        if resource.uid is not None:
            by_uid[(resource.group, resource.uid)] = resource

    edges: list[RelationshipEdge] = []
    seen_edges: set[RelationshipEdge] = set()
    for item, subject in zip(sorted_inputs, nodes, strict=True):
        for fact in item.summary.relationships.references:
            edge = _build_edge(subject, fact, by_uid, by_name)
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            edges.append(edge)

    if len(edges) > limits.max_edges:
        dropped_edges = len(edges) - limits.max_edges
        edges = edges[: limits.max_edges]
        truncated = True
        extra_coverage.append(
            CoverageRecord(
                group="",
                resource="*",
                scope="",
                state=CoverageState.CAPPED,
                detail=f"{dropped_edges} edge(s) dropped at the {limits.max_edges}-edge cap",
            )
        )

    return RelationshipGraph(
        nodes=tuple(nodes),
        edges=tuple(edges),
        coverage=tuple(coverage) + tuple(extra_coverage),
        limits=limits,
        truncated=truncated,
    )
