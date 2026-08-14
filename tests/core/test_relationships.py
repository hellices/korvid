"""Tests for the immutable operational relationship graph (issue #281).

The graph is a pure core model: it consumes `RelationshipFacts` already
attached to list/watch summaries (Task 2) and produces an immutable,
deterministically capped graph with explicit coverage and edge resolution
states. It never retains summary status/custom data or raw manifests.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Iterator, Mapping

import pytest

from korvid.core import relationships
from korvid.core.relationships import (
    CoverageRecord,
    CoverageState,
    EdgeResolution,
    GraphInput,
    GraphLimits,
    GraphResource,
    RelationshipEdge,
    RelationshipGraph,
    build_relationship_graph,
)
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.relationship_facts import (
    FactConfidence,
    ReferenceFact,
    ReferenceGrantFact,
    RelationKind,
    RelationshipFacts,
    SelectorFact,
    TargetReference,
    extract_relationship_facts,
)
from korvid.k8s.selectors import LabelSelector, SelectorExpression


def _meta(kind: str, group: str) -> ResourceMeta:
    return ResourceMeta(kind, kind.lower() + "s", group, "v1", True)


def _ref(
    relation: str,
    group: str,
    kind: str,
    namespace: str,
    name: str,
    *,
    uid: str | None = None,
    field: str = "",
) -> ReferenceFact:
    return ReferenceFact(
        relation=RelationKind(relation),
        target=TargetReference(group=group, kind=kind, namespace=namespace, name=name, uid=uid),
        confidence=FactConfidence.DECLARED,
        field=field,
    )


def _facts(*, references: tuple[ReferenceFact, ...] = (), api_group: str = "") -> RelationshipFacts:
    return RelationshipFacts(api_group=api_group, references=references)


_EMPTY_FACTS = RelationshipFacts()


def _input(
    kind: str,
    group: str,
    namespace: str,
    name: str,
    uid: str,
    *,
    relationships: RelationshipFacts = _EMPTY_FACTS,
) -> GraphInput:
    summary = GenericSummary(
        name=name,
        namespace=namespace,
        kind=kind,
        created="",
        uid=uid,
        relationships=relationships,
    )
    return GraphInput(meta=_meta(kind, group), summary=summary)


def _complete(resource: str) -> CoverageRecord:
    return CoverageRecord("", resource, "", CoverageState.COMPLETE, "")


def _resource(graph: RelationshipGraph, kind: str, name: str) -> GraphResource:
    return next(node for node in graph.nodes if node.kind == kind and node.name == name)


def _label_selector(labels: Mapping[str, str]) -> LabelSelector:
    return LabelSelector(match_labels=tuple(sorted(labels.items())), present=True)


def _service_input(
    name: str,
    labels: Mapping[str, str] | None,
    *,
    namespace: str = "default",
) -> GraphInput:
    """A Service with a `SELECTS` `SelectorFact` (`None` labels = no selector at all)."""
    selectors: tuple[SelectorFact, ...] = ()
    if labels is not None:
        selectors = (
            SelectorFact(
                relation=RelationKind.SELECTS,
                target_group="",
                target_kind="Pod",
                selector=_label_selector(labels),
                confidence=FactConfidence.DECLARED,
                field="spec.selector",
                empty_matches=False,
                match_is_subject=False,
            ),
        )
    return _input(
        "Service",
        "",
        namespace,
        name,
        f"svc-{name}",
        relationships=RelationshipFacts(selectors=selectors),
    )


def _pod_input(
    name: str,
    *,
    labels: Mapping[str, str] | None = None,
    namespace: str = "default",
) -> GraphInput:
    summary = PodSummary(
        name=name,
        namespace=namespace,
        phase="Running",
        ready="1/1",
        restarts=0,
        node="node-a",
        uid=f"pod-{name}",
        labels=tuple(sorted((labels or {}).items())),
    )
    return GraphInput(meta=_meta("Pod", ""), summary=summary)


def _pdb_input(
    name: str,
    *,
    selector: Mapping[str, str],
    empty_matches: bool,
    namespace: str = "default",
) -> GraphInput:
    fact = SelectorFact(
        relation=RelationKind.PROTECTED_BY,
        target_group="",
        target_kind="Pod",
        selector=_label_selector(selector),
        confidence=FactConfidence.DECLARED,
        field="spec.selector",
        empty_matches=empty_matches,
        match_is_subject=True,
    )
    return _input(
        "PodDisruptionBudget",
        "policy",
        namespace,
        name,
        f"pdb-{name}",
        relationships=RelationshipFacts(selectors=(fact,)),
    )


def _http_route_input(
    name: str,
    namespace: str,
    *,
    backend_namespace: str,
    backend_name: str = "api",
) -> GraphInput:
    reference = ReferenceFact(
        relation=RelationKind.ROUTES_TO,
        target=TargetReference(
            group="", kind="Service", namespace=backend_namespace, name=backend_name
        ),
        confidence=FactConfidence.DECLARED,
        field="spec.rules[0].backendRefs[0]",
    )
    return _input(
        "HTTPRoute",
        "gateway.networking.k8s.io",
        namespace,
        name,
        f"httproute-{name}",
        relationships=RelationshipFacts(references=(reference,)),
    )


def _reference_grant_input(
    from_namespace: str,
    namespace: str,
    *,
    from_group: str = "gateway.networking.k8s.io",
    from_kind: str = "HTTPRoute",
    to_group: str = "",
    to_kind: str = "Service",
    to_name: str | None = None,
) -> GraphInput:
    grant = ReferenceGrantFact(
        from_group=from_group,
        from_kind=from_kind,
        from_namespace=from_namespace,
        to_group=to_group,
        to_kind=to_kind,
        namespace=namespace,
        field="spec",
        to_name=to_name,
    )
    return _input(
        "ReferenceGrant",
        "gateway.networking.k8s.io",
        namespace,
        f"grant-{from_namespace}-{to_name or 'all'}",
        f"grant-{from_namespace}-{to_name or 'all'}",
        relationships=RelationshipFacts(grants=(grant,)),
    )


def _owner_graph(*chain: tuple[str, str], cycle_to: str | None = None) -> RelationshipGraph:
    """Build an owned_by chain: chain[0] is the root owner, each later item
    is owned_by the previous one. When `cycle_to` names a member of the
    chain, the root additionally references that member directly (via an
    unrelated relation), producing an edge that is only discovered once the
    named member is reached breadth-first, and therefore surfaces as a cycle
    rather than a duplicate direct edge on the root."""
    inputs: list[GraphInput] = []
    for index, (kind, name) in enumerate(chain):
        references: tuple[ReferenceFact, ...] = ()
        if index > 0:
            parent_kind, parent_name = chain[index - 1]
            references += (
                _ref(
                    "owned_by",
                    "apps",
                    parent_kind,
                    "prod",
                    parent_name,
                    uid=parent_name,
                    field="metadata.ownerReferences[0]",
                ),
            )
        if index == 0 and cycle_to is not None:
            cycle_kind, cycle_name = next((kind, name) for kind, name in chain if name == cycle_to)
            references += (
                _ref(
                    "uses_config",
                    "apps",
                    cycle_kind,
                    "prod",
                    cycle_name,
                    uid=cycle_name,
                    field="spec.cycleProbe",
                ),
            )
        inputs.append(
            _input(kind, "apps", "prod", name, name, relationships=_facts(references=references))
        )
    coverage = [_complete(kind.lower() + "s") for kind, _ in chain]
    return build_relationship_graph(inputs, coverage)


def _wide_owner_graph() -> RelationshipGraph:
    deployment = _input("Deployment", "apps", "prod", "deploy-1", "deploy-1")
    rs1 = _input(
        "ReplicaSet",
        "apps",
        "prod",
        "rs-1",
        "rs-1",
        relationships=_facts(
            references=(
                _ref(
                    "owned_by",
                    "apps",
                    "Deployment",
                    "prod",
                    "deploy-1",
                    uid="deploy-1",
                    field="metadata.ownerReferences[0]",
                ),
            )
        ),
    )
    rs2 = _input(
        "ReplicaSet",
        "apps",
        "prod",
        "rs-2",
        "rs-2",
        relationships=_facts(
            references=(
                _ref(
                    "owned_by",
                    "apps",
                    "Deployment",
                    "prod",
                    "deploy-1",
                    uid="deploy-1",
                    field="metadata.ownerReferences[0]",
                ),
            )
        ),
    )
    pod1 = _input(
        "Pod",
        "",
        "prod",
        "pod-1",
        "pod-1",
        relationships=_facts(
            references=(
                _ref(
                    "owned_by",
                    "apps",
                    "ReplicaSet",
                    "prod",
                    "rs-1",
                    uid="rs-1",
                    field="metadata.ownerReferences[0]",
                ),
            )
        ),
    )
    return build_relationship_graph(
        [deployment, rs1, rs2, pod1],
        [_complete("deployments"), _complete("replicasets"), _complete("pods")],
    )


def test_owner_uid_does_not_reconnect_to_replacement_with_same_name() -> None:
    deployment = _input("Deployment", "apps", "prod", "api", "deploy-new", relationships=_facts())
    replica_set = _input(
        "ReplicaSet",
        "apps",
        "prod",
        "api-abc",
        "rs-1",
        relationships=_facts(
            references=(
                _ref(
                    "owned_by",
                    "apps",
                    "Deployment",
                    "prod",
                    "api",
                    uid="deploy-old",
                    field="metadata.ownerReferences[0]",
                ),
            )
        ),
    )
    graph = build_relationship_graph([deployment, replica_set], [_complete("deployments")])
    edge = graph.edges[0]
    assert edge.target.uid == "deploy-old"
    assert edge.resolution is EdgeResolution.MISSING
    assert "deploy-new" not in repr(edge)


def test_named_reference_resolves_current_uid() -> None:
    pod = _input(
        "Pod",
        "",
        "prod",
        "api-0",
        "pod-1",
        relationships=_facts(
            references=(
                _ref(
                    "uses_config",
                    "",
                    "ConfigMap",
                    "prod",
                    "api-config",
                    field="spec.volumes[0].configMap.name",
                ),
            )
        ),
    )
    config = _input("ConfigMap", "", "prod", "api-config", "cm-1")
    graph = build_relationship_graph([pod, config], [_complete("pods"), _complete("configmaps")])
    assert graph.edges[0].target.uid == "cm-1"
    assert graph.edges[0].resolution is EdgeResolution.RESOLVED


def test_cross_namespace_namespaced_owner_is_invalid() -> None:
    child = _input(
        "ReplicaSet",
        "apps",
        "prod",
        "api-abc",
        "rs-1",
        relationships=_facts(
            references=(
                _ref(
                    "owned_by",
                    "apps",
                    "Deployment",
                    "other",
                    "api",
                    uid="deploy-1",
                    field="metadata.ownerReferences[0]",
                ),
            )
        ),
    )
    graph = build_relationship_graph([child], [_complete("replicasets")])
    assert graph.edges[0].resolution is EdgeResolution.INVALID
    assert "cross-namespace owned_by" in graph.edges[0].explanation


def test_cluster_scoped_subject_bound_to_namespaced_target_is_resolved() -> None:
    """A cluster-scoped subject (e.g. PersistentVolume) referencing a
    namespaced target (e.g. its bound PVC) is not a cross-namespace
    violation: the invalidity rule only applies when the *subject* itself
    is namespaced and disagrees with a namespaced target."""
    persistent_volume = _input(
        "PersistentVolume",
        "",
        "",
        "pv-1",
        "pv-uid-1",
        relationships=_facts(
            references=(
                _ref(
                    "bound_to",
                    "",
                    "PersistentVolumeClaim",
                    "prod",
                    "data",
                    uid="pvc-1",
                    field="spec.claimRef",
                ),
            )
        ),
    )
    claim = _input("PersistentVolumeClaim", "", "prod", "data", "pvc-1")
    graph = build_relationship_graph(
        [persistent_volume, claim],
        [_complete("persistentvolumes"), _complete("persistentvolumeclaims")],
    )
    assert graph.edges[0].resolution is EdgeResolution.RESOLVED
    assert graph.edges[0].target.uid == "pvc-1"


def test_cross_namespace_routes_to_is_invalid_pending_reference_grant_authorization() -> None:
    """Without Task 4's ReferenceGrant-based authorization, a cross-namespace
    `routes_to` (Gateway/Ingress backend) reference defaults to invalid even
    when the target exists -- absence of a grant must never be silently
    treated as permission."""
    http_route = _input(
        "HTTPRoute",
        "gateway.networking.k8s.io",
        "prod",
        "route",
        "route-1",
        relationships=_facts(
            references=(
                _ref(
                    "routes_to",
                    "",
                    "Service",
                    "other",
                    "backend",
                    uid="svc-1",
                    field="spec.rules[0].backendRefs[0]",
                ),
            )
        ),
    )
    service = _input("Service", "", "other", "backend", "svc-1")
    graph = build_relationship_graph(
        [http_route, service], [_complete("httproutes"), _complete("services")]
    )
    assert graph.edges[0].resolution is EdgeResolution.INVALID
    assert "cross-namespace routes_to" in graph.edges[0].explanation


def test_cluster_scoped_owner_is_valid() -> None:
    pod = _input(
        "Pod",
        "",
        "prod",
        "api-0",
        "pod-1",
        relationships=_facts(
            references=(
                _ref(
                    "scheduled_on",
                    "",
                    "Node",
                    "",
                    "node-a",
                    uid="node-1",
                    field="spec.nodeName",
                ),
            )
        ),
    )
    node = _input("Node", "", "", "node-a", "node-1")
    graph = build_relationship_graph([pod, node], [_complete("pods"), _complete("nodes")])
    assert graph.edges[0].resolution is EdgeResolution.RESOLVED
    assert graph.edges[0].target.uid == "node-1"


def test_absent_named_target_is_missing() -> None:
    pod = _input(
        "Pod",
        "",
        "prod",
        "api-0",
        "pod-1",
        relationships=_facts(
            references=(
                _ref(
                    "uses_config",
                    "",
                    "ConfigMap",
                    "prod",
                    "missing-config",
                    field="spec.volumes[0].configMap.name",
                ),
            )
        ),
    )
    graph = build_relationship_graph([pod], [_complete("pods")])
    assert graph.edges[0].resolution is EdgeResolution.MISSING
    assert graph.edges[0].target.uid is None
    assert graph.edges[0].target.name == "missing-config"


def test_identical_edges_deduplicate() -> None:
    duplicate_ref = _ref(
        "uses_config",
        "",
        "ConfigMap",
        "prod",
        "api-config",
        field="spec.volumes[0].configMap.name",
    )
    pod = _input(
        "Pod",
        "",
        "prod",
        "api-0",
        "pod-1",
        relationships=_facts(references=(duplicate_ref, duplicate_ref)),
    )
    config = _input("ConfigMap", "", "prod", "api-config", "cm-1")
    graph = build_relationship_graph([pod, config], [_complete("pods"), _complete("configmaps")])
    assert len(graph.edges) == 1


def test_forbidden_coverage_keeps_graph_incomplete() -> None:
    record = CoverageRecord("", "secrets", "prod", CoverageState.FORBIDDEN, "RBAC denied")
    graph = build_relationship_graph([], [record])
    assert graph.incomplete
    assert graph.coverage == (record,)


def test_unavailable_coverage_keeps_graph_incomplete() -> None:
    record = CoverageRecord(
        "gateway.networking.k8s.io", "gateways", "", CoverageState.UNAVAILABLE, "CRD not installed"
    )
    graph = build_relationship_graph([], [record])
    assert graph.incomplete
    assert graph.coverage == (record,)


def test_caps_are_deterministic_and_visible() -> None:
    inputs = [
        _input("ConfigMap", "", "prod", "z", "uid-z"),
        _input("ConfigMap", "", "prod", "a", "uid-a"),
    ]
    graph = build_relationship_graph(
        inputs,
        [_complete("configmaps")],
        limits=GraphLimits(max_resources=1, max_edges=1),
    )
    assert [node.name for node in graph.nodes] == ["a"]
    assert graph.truncated
    assert any(record.state is CoverageState.CAPPED for record in graph.coverage)


def test_coverage_detail_is_flattened_and_bounded() -> None:
    record = CoverageRecord("", "pods", "prod", CoverageState.FAILED, "line1\nline2" + "x" * 600)
    assert "\n" not in record.detail
    assert len(record.detail) == 512


def test_graph_input_accepts_pod_summary_directly() -> None:
    pod_summary = PodSummary(
        name="api-0",
        namespace="prod",
        phase="Running",
        ready="1/1",
        restarts=0,
        node="node-a",
        uid="pod-1",
    )
    graph_input = GraphInput(meta=_meta("Pod", ""), summary=pod_summary)
    graph = build_relationship_graph([graph_input], [_complete("pods")])
    assert graph.nodes == (GraphResource("", "Pod", "prod", "api-0", "pod-1"),)


def test_dependencies_and_dependents_are_directional() -> None:
    deployment = _input("Deployment", "apps", "prod", "api", "deploy-1")
    replica_set = _input(
        "ReplicaSet",
        "apps",
        "prod",
        "api-abc",
        "rs-1",
        relationships=_facts(
            references=(
                _ref(
                    "owned_by",
                    "apps",
                    "Deployment",
                    "prod",
                    "api",
                    uid="deploy-1",
                    field="metadata.ownerReferences[0]",
                ),
            )
        ),
    )
    graph = build_relationship_graph(
        [deployment, replica_set], [_complete("deployments"), _complete("replicasets")]
    )
    deploy_resource = _resource(graph, "Deployment", "api")
    rs_resource = _resource(graph, "ReplicaSet", "api-abc")
    assert graph.dependents_of(deploy_resource) == graph.edges
    assert graph.dependencies_of(rs_resource) == graph.edges
    assert graph.dependencies_of(deploy_resource) == ()
    assert graph.dependents_of(rs_resource) == ()


def test_walk_dependents_is_breadth_first_and_cycle_safe() -> None:
    graph = _owner_graph(
        ("Deployment", "deploy-1"),
        ("ReplicaSet", "rs-1"),
        ("Pod", "pod-1"),
        cycle_to="pod-1",
    )
    root = _resource(graph, "Deployment", "deploy-1")
    result = graph.walk_dependents(root)
    assert [(edge.subject.kind, edge.target.kind) for edge in result.edges] == [
        ("ReplicaSet", "Deployment"),
        ("Pod", "ReplicaSet"),
    ]
    assert len(result.cycles) == 1
    assert root not in {edge.subject for edge in result.edges}


def test_walk_dependents_reports_depth_and_node_caps() -> None:
    graph = _wide_owner_graph()
    root = _resource(graph, "Deployment", "deploy-1")
    by_depth = graph.walk_dependents(root, max_depth=1, max_nodes=500)
    by_nodes = graph.walk_dependents(root, max_depth=5, max_nodes=1)
    assert all(edge.target == root for edge in by_depth.edges)
    assert len(by_nodes.edges) == 1
    assert by_nodes.truncated


def test_walk_dependents_not_truncated_when_leaf_is_exactly_at_depth_cap() -> None:
    """A chain whose true leaf sits exactly at max_depth has nothing left
    unexplored beyond the cap, so it must not be reported as truncated."""
    graph = _owner_graph(
        ("Deployment", "deploy-1"),
        ("ReplicaSet", "rs-1"),
        ("Pod", "pod-1"),
    )
    root = _resource(graph, "Deployment", "deploy-1")
    result = graph.walk_dependents(root, max_depth=2, max_nodes=500)
    assert [(edge.subject.kind, edge.target.kind) for edge in result.edges] == [
        ("ReplicaSet", "Deployment"),
        ("Pod", "ReplicaSet"),
    ]
    assert result.truncated is False


def test_walk_dependents_truncated_when_deeper_dependent_exists_beyond_cap() -> None:
    """Adding one more level below the depth cap must flip truncated True:
    there is now a genuine unexplored dependent beyond max_depth."""
    graph = _owner_graph(
        ("Deployment", "deploy-1"),
        ("ReplicaSet", "rs-1"),
        ("Pod", "pod-1"),
        ("Container", "c-1"),
    )
    root = _resource(graph, "Deployment", "deploy-1")
    result = graph.walk_dependents(root, max_depth=2, max_nodes=500)
    assert [(edge.subject.kind, edge.target.kind) for edge in result.edges] == [
        ("ReplicaSet", "Deployment"),
        ("Pod", "ReplicaSet"),
    ]
    assert result.truncated is True


def test_walk_dependents_cycle_only_frontier_is_not_truncated() -> None:
    """When the only edge beyond the depth cap is a cycle back into an
    already-visited resource, that alone must not report truncation --
    there is no genuine unexplored dependent, only a revisit."""
    graph = _owner_graph(
        ("Deployment", "deploy-1"),
        ("ReplicaSet", "rs-1"),
        ("Pod", "pod-1"),
        cycle_to="pod-1",
    )
    root = _resource(graph, "Deployment", "deploy-1")
    result = graph.walk_dependents(root, max_depth=2, max_nodes=500)
    assert result.truncated is False


class _CountingEdges(tuple[RelationshipEdge, ...]):
    """A `graph.edges` tuple that records how often it is scanned.

    Used to pin the traversal's *semantic* cost -- how many times it walks
    the whole edge list -- without asserting on wall-clock time, which is
    forbidden here and flaky in CI regardless.
    """

    scans: int

    def __new__(cls, edges: Iterable[RelationshipEdge]) -> _CountingEdges:
        counting = super().__new__(cls, edges)
        counting.scans = 0
        return counting

    def __iter__(self) -> Iterator[RelationshipEdge]:
        self.scans += 1
        return super().__iter__()


def _counting_graph(graph: RelationshipGraph) -> tuple[RelationshipGraph, _CountingEdges]:
    edges = _CountingEdges(graph.edges)
    return dataclasses.replace(graph, edges=edges), edges


def test_walk_dependents_scans_the_edge_list_once_per_traversal() -> None:
    """One traversal must build its dependents adjacency once, not rescan
    every edge for every visited node: a chain of N dependents previously
    cost N+ full scans of `graph.edges`, which is quadratic in a large
    snapshot (up to `max_edges = 50,000`)."""
    graph = _owner_graph(
        ("Deployment", "deploy-1"),
        ("ReplicaSet", "rs-1"),
        ("Pod", "pod-1"),
        ("Container", "c-1"),
        ("Probe", "p-1"),
    )
    counting_graph, edges = _counting_graph(graph)
    root = _resource(counting_graph, "Deployment", "deploy-1")
    result = counting_graph.walk_dependents(root)
    assert len(result.edges) == 4
    assert edges.scans == 1


def test_walk_dependents_scan_count_is_independent_of_visited_nodes() -> None:
    """The scan count must not grow with the number of dependents: a wide
    fan-out costs exactly the same single index build as a narrow one."""
    short_graph, short_edges = _counting_graph(
        _owner_graph(("Deployment", "deploy-1"), ("ReplicaSet", "rs-1"))
    )
    wide_graph, wide_edges = _counting_graph(_wide_owner_graph())
    short_graph.walk_dependents(_resource(short_graph, "Deployment", "deploy-1"))
    wide_graph.walk_dependents(_resource(wide_graph, "Deployment", "deploy-1"))
    assert wide_edges.scans == short_edges.scans


def test_walk_dependents_preserves_edge_list_order_within_a_depth() -> None:
    """Traversal order is the graph's own deterministic edge order, per
    frontier resource -- an adjacency index must not reorder siblings."""
    graph = _wide_owner_graph()
    root = _resource(graph, "Deployment", "deploy-1")
    result = graph.walk_dependents(root)
    expected = [edge for edge in graph.edges if edge.target == root]
    assert list(result.edges[: len(expected)]) == expected


def test_walk_dependents_keeps_every_parallel_edge_between_the_same_pair() -> None:
    """Two distinct relationships between the same pair of resources are
    two edges: the first is traversed, the second revisits an already
    reached resource -- an adjacency index keyed by resource must not
    collapse them into one. The revisit is not a cycle (the pair does not
    loop); see the classification tests below."""
    deployment = _input("Deployment", "apps", "prod", "api", "deploy-1")
    pod = _input(
        "Pod",
        "",
        "prod",
        "api-0",
        "pod-1",
        relationships=_facts(
            references=(
                _ref(
                    "owned_by",
                    "apps",
                    "Deployment",
                    "prod",
                    "api",
                    uid="deploy-1",
                    field="metadata.ownerReferences[0]",
                ),
                _ref(
                    "uses_config",
                    "apps",
                    "Deployment",
                    "prod",
                    "api",
                    uid="deploy-1",
                    field="spec.volumes[0].configMap",
                ),
            )
        ),
    )
    graph = build_relationship_graph(
        [deployment, pod], [_complete("deployments"), _complete("pods")]
    )
    root = _resource(graph, "Deployment", "api")
    result = graph.walk_dependents(root)
    assert len(graph.dependents_of(root)) == 2
    assert len(result.edges) == 1
    assert len(result.revisits) == 1
    assert result.cycles == ()
    assert result.edges[0].evidence.field == "metadata.ownerReferences[0]"
    assert result.revisits[0].evidence.field == "spec.volumes[0].configMap"


def test_walk_dependents_node_cap_cuts_at_the_same_edge_as_the_edge_order() -> None:
    """The node cap truncates in edge-list order, so the kept prefix is
    exactly the first `max_nodes` traversable edges."""
    graph = _wide_owner_graph()
    root = _resource(graph, "Deployment", "deploy-1")
    full = graph.walk_dependents(root, max_depth=5, max_nodes=500)
    capped = graph.walk_dependents(root, max_depth=5, max_nodes=2)
    assert capped.edges == full.edges[:2]
    assert capped.truncated is True


# --- Task 4: selector joins -------------------------------------------------


def test_service_selector_creates_declared_pod_dependencies() -> None:
    service = _service_input("api", {"app": "api"})
    api = _pod_input("api-0", labels={"app": "api"})
    worker = _pod_input("worker-0", labels={"app": "worker"})
    graph = build_relationship_graph(
        [service, api, worker], [_complete("services"), _complete("pods")]
    )
    edges = graph.dependencies_of(_resource(graph, "Service", "api"))
    assert [(edge.target.kind, edge.target.name) for edge in edges] == [("Pod", "api-0")]
    assert edges[0].relation.value == "selects"
    assert edges[0].confidence.value == "declared"


def test_duplicate_selectors_create_edges_for_each_subject() -> None:
    api = _pod_input("api-0", labels={"app": "api"})
    graph = build_relationship_graph(
        [
            _service_input("public", {"app": "api"}),
            _service_input("internal", {"app": "api"}),
            api,
        ],
        [_complete("services"), _complete("pods")],
    )
    assert [(edge.subject.name, edge.target.name) for edge in graph.edges] == [
        ("internal", "api-0"),
        ("public", "api-0"),
    ]


def test_selectors_do_not_cross_namespaces() -> None:
    graph = build_relationship_graph(
        [
            _service_input("api", {"app": "api"}, namespace="prod"),
            _pod_input("api-0", labels={"app": "api"}, namespace="other"),
        ],
        [_complete("services"), _complete("pods")],
    )
    assert graph.edges == ()


def test_policy_v1_empty_pdb_selector_matches_every_pod_in_namespace() -> None:
    graph = build_relationship_graph(
        [_pdb_input("availability", selector={}, empty_matches=True), _pod_input("api-0")],
        [_complete("poddisruptionbudgets"), _complete("pods")],
    )
    assert graph.edges[0].relation.value == "protected_by"
    assert graph.edges[0].subject.kind == "Pod"
    assert graph.edges[0].target.kind == "PodDisruptionBudget"


def test_service_absent_and_empty_selectors_create_no_edges() -> None:
    absent_service = _service_input("no-selector", None)
    empty_selector_fact = SelectorFact(
        relation=RelationKind.SELECTS,
        target_group="",
        target_kind="Pod",
        selector=LabelSelector(present=True),
        confidence=FactConfidence.DECLARED,
        field="spec.selector",
        empty_matches=False,
        match_is_subject=False,
    )
    empty_service = _input(
        "Service",
        "",
        "default",
        "empty-selector",
        "svc-empty-selector",
        relationships=RelationshipFacts(selectors=(empty_selector_fact,)),
    )
    pod = _pod_input("api-0", labels={"app": "api"})
    graph = build_relationship_graph(
        [absent_service, empty_service, pod], [_complete("services"), _complete("pods")]
    )
    assert graph.edges == ()


def test_workload_and_pdb_match_expressions() -> None:
    deployment_selector = SelectorFact(
        relation=RelationKind.MANAGED_BY,
        target_group="",
        target_kind="Pod",
        selector=LabelSelector(
            match_expressions=(SelectorExpression("tier", "In", ("web",)),), present=True
        ),
        confidence=FactConfidence.DECLARED,
        field="spec.selector",
        empty_matches=False,
        match_is_subject=True,
    )
    deployment = _input(
        "Deployment",
        "apps",
        "default",
        "web",
        "deploy-web",
        relationships=RelationshipFacts(selectors=(deployment_selector,)),
    )
    pdb_selector = SelectorFact(
        relation=RelationKind.PROTECTED_BY,
        target_group="",
        target_kind="Pod",
        selector=LabelSelector(
            match_expressions=(SelectorExpression("tier", "NotIn", ("batch",)),), present=True
        ),
        confidence=FactConfidence.DECLARED,
        field="spec.selector",
        empty_matches=False,
        match_is_subject=True,
    )
    pdb = _input(
        "PodDisruptionBudget",
        "policy",
        "default",
        "availability",
        "pdb-1",
        relationships=RelationshipFacts(selectors=(pdb_selector,)),
    )
    matching_pod = _pod_input("web-0", labels={"tier": "web"})
    other_pod = _pod_input("batch-0", labels={"tier": "batch"})
    graph = build_relationship_graph(
        [deployment, pdb, matching_pod, other_pod],
        [_complete("deployments"), _complete("poddisruptionbudgets"), _complete("pods")],
    )
    managed = [edge for edge in graph.edges if edge.relation is RelationKind.MANAGED_BY]
    protected = [edge for edge in graph.edges if edge.relation is RelationKind.PROTECTED_BY]
    assert [edge.subject.name for edge in managed] == ["web-0"]
    assert [edge.subject.name for edge in protected] == ["web-0"]


def test_unmatched_selector_creates_no_edge_without_changing_coverage() -> None:
    service = _service_input("api", {"app": "api"})
    pod = _pod_input("worker-0", labels={"app": "worker"})
    coverage = [_complete("services"), _complete("pods")]
    graph = build_relationship_graph([service, pod], coverage)
    assert graph.edges == ()
    assert graph.coverage == tuple(coverage)


# --- Task 4: EndpointSlice and routing joins --------------------------------


def test_endpoint_slice_target_ref_resolves_pod() -> None:
    pod = _pod_input("api-0", namespace="prod")
    endpoint_slice = _input(
        "EndpointSlice",
        "discovery.k8s.io",
        "prod",
        "api-abc123",
        "eps-1",
        relationships=_facts(
            references=(
                _ref(
                    "routes_to",
                    "",
                    "Pod",
                    "prod",
                    "api-0",
                    uid=pod.summary.uid,
                    field="endpoints[0].targetRef",
                ),
            )
        ),
    )
    graph = build_relationship_graph(
        [endpoint_slice, pod], [_complete("endpointslices"), _complete("pods")]
    )
    assert graph.edges[0].resolution is EdgeResolution.RESOLVED
    assert graph.edges[0].target.kind == "Pod"


def test_reference_grant_does_not_authorize_cross_namespace_endpoint_slice() -> None:
    pod = _pod_input("api-0", namespace="prod")
    endpoint_slice = _input(
        "EndpointSlice",
        "discovery.k8s.io",
        "edge",
        "api-abc123",
        "eps-1",
        relationships=_facts(
            references=(
                _ref(
                    "routes_to",
                    "",
                    "Pod",
                    "prod",
                    "api-0",
                    uid=pod.summary.uid,
                    field="endpoints[0].targetRef",
                ),
            )
        ),
    )
    grant = _reference_grant_input(
        "edge",
        "prod",
        from_group="discovery.k8s.io",
        from_kind="EndpointSlice",
        to_kind="Pod",
    )
    graph = build_relationship_graph(
        [endpoint_slice, pod, grant],
        [_complete("endpointslices"), _complete("pods"), _complete("referencegrants")],
    )
    assert graph.edges[0].resolution is EdgeResolution.INVALID


def test_ingress_backend_is_same_namespace_only() -> None:
    ingress = _input(
        "Ingress",
        "networking.k8s.io",
        "prod",
        "web",
        "ing-1",
        relationships=_facts(
            references=(
                _ref(
                    "routes_to",
                    "",
                    "Service",
                    "prod",
                    "api",
                    field="spec.rules[0].http.paths[0].backend.service",
                ),
            )
        ),
    )
    service = _service_input("api", None, namespace="prod")
    graph = build_relationship_graph(
        [ingress, service], [_complete("ingresses"), _complete("services")]
    )
    assert graph.edges[0].resolution is EdgeResolution.RESOLVED
    assert graph.edges[0].target.uid == service.summary.uid


def test_http_route_same_namespace_backend_resolves() -> None:
    route = _http_route_input("public", "prod", backend_namespace="prod")
    service = _service_input("api", None, namespace="prod")
    graph = build_relationship_graph(
        [route, service], [_complete("httproutes"), _complete("services")]
    )
    assert graph.edges[0].resolution is EdgeResolution.RESOLVED
    assert graph.edges[0].target.uid == service.summary.uid


def test_cross_namespace_route_requires_matching_reference_grant() -> None:
    route = _http_route_input("public", "edge", backend_namespace="prod")
    service = _service_input("api", None, namespace="prod")
    without_grant = build_relationship_graph(
        [route, service], [_complete("httproutes"), _complete("services")]
    )
    assert without_grant.edges[0].resolution is EdgeResolution.INVALID

    with_grant = build_relationship_graph(
        [route, service, _reference_grant_input("edge", "prod")],
        [
            _complete("httproutes"),
            _complete("services"),
            _complete("referencegrants"),
        ],
    )
    assert with_grant.edges[0].resolution is EdgeResolution.RESOLVED
    assert with_grant.edges[0].target.uid == service.summary.uid


@pytest.mark.parametrize(
    ("from_group", "from_kind", "to_kind"),
    [
        ("wrong.example", "HTTPRoute", "Service"),
        ("gateway.networking.k8s.io", "GRPCRoute", "Service"),
        ("gateway.networking.k8s.io", "HTTPRoute", "ConfigMap"),
    ],
)
def test_reference_grant_constraints_are_exact(
    from_group: str, from_kind: str, to_kind: str
) -> None:
    graph = build_relationship_graph(
        [
            _http_route_input("public", "edge", backend_namespace="prod"),
            _service_input("api", None, namespace="prod"),
            _reference_grant_input(
                "edge",
                "prod",
                from_group=from_group,
                from_kind=from_kind,
                to_kind=to_kind,
            ),
        ],
        [_complete("httproutes"), _complete("services"), _complete("referencegrants")],
    )
    assert graph.edges[0].resolution is EdgeResolution.INVALID


def test_reference_grant_named_object_does_not_authorize_a_different_name() -> None:
    """`spec.to[].name` narrows a grant to exactly one object. A grant for
    Service `payments` must never authorize a route to Service `admin` in
    the same namespace — ignoring the name silently widens every named
    grant into a namespace-wide one."""
    graph = build_relationship_graph(
        [
            _http_route_input("public", "edge", backend_namespace="prod", backend_name="admin"),
            _service_input("admin", None, namespace="prod"),
            _reference_grant_input("edge", "prod", to_name="payments"),
        ],
        [_complete("httproutes"), _complete("services"), _complete("referencegrants")],
    )
    assert graph.edges[0].resolution is EdgeResolution.INVALID


def test_reference_grant_named_object_authorizes_that_exact_name() -> None:
    graph = build_relationship_graph(
        [
            _http_route_input("public", "edge", backend_namespace="prod", backend_name="payments"),
            _service_input("payments", None, namespace="prod"),
            _reference_grant_input("edge", "prod", to_name="payments"),
        ],
        [_complete("httproutes"), _complete("services"), _complete("referencegrants")],
    )
    assert graph.edges[0].resolution is EdgeResolution.RESOLVED


def test_reference_grant_without_to_name_authorizes_every_matching_name() -> None:
    """An omitted `spec.to[].name` grants every object of that group/kind
    in the grant's namespace — the pre-existing behavior, unchanged."""
    graph = build_relationship_graph(
        [
            _http_route_input("public", "edge", backend_namespace="prod", backend_name="admin"),
            _service_input("admin", None, namespace="prod"),
            _reference_grant_input("edge", "prod", to_name=None),
        ],
        [_complete("httproutes"), _complete("services"), _complete("referencegrants")],
    )
    assert graph.edges[0].resolution is EdgeResolution.RESOLVED


def test_reference_grant_wrong_from_namespace_stays_invalid() -> None:
    """A grant whose `from` namespace does not name the actual subject's
    namespace must not authorize the cross-namespace route, even though
    every other field (group/kind, target namespace) matches exactly."""
    graph = build_relationship_graph(
        [
            _http_route_input("public", "edge", backend_namespace="prod"),
            _service_input("api", None, namespace="prod"),
            _reference_grant_input("other-edge", "prod"),
        ],
        [_complete("httproutes"), _complete("services"), _complete("referencegrants")],
    )
    assert graph.edges[0].resolution is EdgeResolution.INVALID


def test_reference_grant_living_in_wrong_namespace_does_not_authorize() -> None:
    """A `ReferenceGrant` object whose own namespace differs from the
    backend's target namespace must not authorize the route, even when its
    `from`/`to` fields would otherwise match exactly: a grant only ever
    authorizes references *into* the namespace it lives in."""
    graph = build_relationship_graph(
        [
            _http_route_input("public", "edge", backend_namespace="prod"),
            _service_input("api", None, namespace="prod"),
            _reference_grant_input("edge", "staging"),
        ],
        [_complete("httproutes"), _complete("services"), _complete("referencegrants")],
    )
    assert graph.edges[0].resolution is EdgeResolution.INVALID


def test_optional_gateway_unavailable_coverage_does_not_abort_build() -> None:
    """An unavailable Gateway API CRD must not abort building the rest of
    the graph; only `graph.incomplete` reflects the missing coverage."""
    pod = _pod_input("api-0")
    coverage = [
        _complete("pods"),
        CoverageRecord(
            "gateway.networking.k8s.io",
            "gateways",
            "",
            CoverageState.UNAVAILABLE,
            "CRD not installed",
        ),
    ]
    graph = build_relationship_graph([pod], coverage)
    assert graph.incomplete
    assert graph.nodes == (GraphResource("", "Pod", "default", "api-0", "pod-api-0"),)


def _workload_managed_by_selector(labels: Mapping[str, str]) -> SelectorFact:
    return SelectorFact(
        relation=RelationKind.MANAGED_BY,
        target_group="",
        target_kind="Pod",
        selector=_label_selector(labels),
        confidence=FactConfidence.DECLARED,
        field="spec.selector",
        empty_matches=False,
        match_is_subject=True,
    )


def test_workload_and_pdb_selector_evidence_is_declaring_resource() -> None:
    """`match_is_subject=True` (workload `MANAGED_BY`, PDB `PROTECTED_BY`)
    flips subject/target to the matched Pod, but the evidence pointer must
    always name the selector-declaring workload/PDB, never the Pod."""
    deployment = _input(
        "Deployment",
        "apps",
        "default",
        "web",
        "deploy-web",
        relationships=RelationshipFacts(selectors=(_workload_managed_by_selector({"app": "web"}),)),
    )
    pdb = _pdb_input("availability", selector={"app": "web"}, empty_matches=False)
    pod = _pod_input("web-0", labels={"app": "web"})
    graph = build_relationship_graph(
        [deployment, pdb, pod],
        [_complete("deployments"), _complete("poddisruptionbudgets"), _complete("pods")],
    )
    managed_edge = next(edge for edge in graph.edges if edge.relation is RelationKind.MANAGED_BY)
    protected_edge = next(
        edge for edge in graph.edges if edge.relation is RelationKind.PROTECTED_BY
    )
    deployment_resource = _resource(graph, "Deployment", "web")
    pdb_resource = _resource(graph, "PodDisruptionBudget", "availability")
    pod_resource = _resource(graph, "Pod", "web-0")

    assert managed_edge.subject == pod_resource
    assert managed_edge.target == deployment_resource
    assert managed_edge.evidence.resource == deployment_resource

    assert protected_edge.subject == pod_resource
    assert protected_edge.target == pdb_resource
    assert protected_edge.evidence.resource == pdb_resource


def test_workload_and_pdb_selectors_do_not_cross_namespaces() -> None:
    """The shared candidate index restricts workload/PDB selector matches
    to the declaring resource's own namespace, same as Service selectors."""
    deployment = _input(
        "Deployment",
        "apps",
        "prod",
        "web",
        "deploy-web",
        relationships=RelationshipFacts(selectors=(_workload_managed_by_selector({"app": "web"}),)),
    )
    pdb = _pdb_input("availability", selector={"app": "web"}, empty_matches=False, namespace="prod")
    other_namespace_pod = _pod_input("web-0", labels={"app": "web"}, namespace="other")
    graph = build_relationship_graph(
        [deployment, pdb, other_namespace_pod],
        [_complete("deployments"), _complete("poddisruptionbudgets"), _complete("pods")],
    )
    assert graph.edges == ()


def _pv_input(
    name: str, *, claim: str, claim_uid: str | None, namespace: str = "prod"
) -> GraphInput:
    """A PersistentVolume whose `spec.claimRef` BOUND_TO fact carries `claim_uid`."""
    return _input(
        "PersistentVolume",
        "",
        "",
        name,
        f"pv-{name}",
        relationships=_facts(
            references=(
                _ref(
                    "bound_to",
                    "",
                    "PersistentVolumeClaim",
                    namespace,
                    claim,
                    uid=claim_uid,
                    field="spec.claimRef",
                ),
            )
        ),
    )


def test_pv_claim_ref_uid_does_not_reconnect_to_a_recreated_claim() -> None:
    """A PV bound to a deleted PVC must not reattach to its same-named
    replacement: the claimRef UID makes the stale binding visibly missing."""
    volume = _pv_input("pv-1", claim="api-data", claim_uid="pvc-old")
    claim = _input("PersistentVolumeClaim", "", "prod", "api-data", "pvc-new")
    graph = build_relationship_graph(
        [volume, claim], [_complete("persistentvolumes"), _complete("persistentvolumeclaims")]
    )
    edge = next(edge for edge in graph.edges if edge.subject.kind == "PersistentVolume")
    assert edge.resolution is EdgeResolution.MISSING
    assert edge.target.uid == "pvc-old"
    assert "pvc-new" not in repr(edge)


def test_pv_claim_ref_uid_resolves_the_exact_bound_claim() -> None:
    """The same claimRef UID resolves to the live claim it actually names."""
    volume = _pv_input("pv-1", claim="api-data", claim_uid="pvc-1")
    claim = _input("PersistentVolumeClaim", "", "prod", "api-data", "pvc-1")
    graph = build_relationship_graph(
        [volume, claim], [_complete("persistentvolumes"), _complete("persistentvolumeclaims")]
    )
    edge = next(edge for edge in graph.edges if edge.subject.kind == "PersistentVolume")
    assert edge.resolution is EdgeResolution.RESOLVED
    assert edge.target.uid == "pvc-1"


def _adversarial_selector_inputs(pdbs: int, pods: int) -> list[GraphInput]:
    """`pdbs` x `pods` candidate selector edges: every empty-selector
    `policy/v1` PDB matches every Pod in the namespace."""
    inputs: list[GraphInput] = [
        _pdb_input(f"pdb-{index:03d}", selector={}, empty_matches=True) for index in range(pdbs)
    ]
    inputs.extend(_pod_input(f"pod-{index:03d}") for index in range(pods))
    return inputs


def _selector_coverage() -> list[CoverageRecord]:
    return [_complete("poddisruptionbudgets"), _complete("pods")]


def _peak_tracking_accumulator(peaks: list[int]) -> type[relationships._BoundedEdges]:
    """A `_BoundedEdges` subclass recording its retained size after every offer."""

    class _PeakTrackingEdges(relationships._BoundedEdges):
        def offer(self, edge: RelationshipEdge) -> relationships._EdgeOffer:
            outcome = super().offer(edge)
            peaks.append(len(self))
            return outcome

    return _PeakTrackingEdges


def test_edge_cap_never_retains_more_candidates_than_max_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PDB x Pod join must never materialize every candidate edge first.

    Thousands of empty-selector PDBs joined against thousands of Pods
    produce millions of candidates; only `max_edges` of them may ever be
    retained at once, so the cap has to bound accumulation during
    generation rather than truncating a fully built list afterwards.
    """
    peaks: list[int] = []
    monkeypatch.setattr(relationships, "_BoundedEdges", _peak_tracking_accumulator(peaks))
    inputs = _adversarial_selector_inputs(pdbs=20, pods=20)
    graph = build_relationship_graph(inputs, _selector_coverage(), limits=GraphLimits(max_edges=7))
    assert len(graph.edges) == 7
    assert peaks
    assert max(peaks) <= 7


def test_edge_cap_keeps_the_lowest_ordered_edges_regardless_of_input_order() -> None:
    """The capped edge set is exactly the top of the fully ordered join,
    and does not depend on the order inputs were discovered in."""
    inputs = _adversarial_selector_inputs(pdbs=8, pods=8)
    uncapped = build_relationship_graph(inputs, _selector_coverage())
    capped = build_relationship_graph(inputs, _selector_coverage(), limits=GraphLimits(max_edges=5))
    reversed_capped = build_relationship_graph(
        list(reversed(inputs)), _selector_coverage(), limits=GraphLimits(max_edges=5)
    )
    assert capped.edges == uncapped.edges[:5]
    assert reversed_capped.edges == capped.edges


def test_edge_cap_records_capped_coverage_without_inventing_a_count() -> None:
    """The cap stays visible in coverage. The exact number of distinct
    dropped edges is unknowable under bounded memory, so the record names
    the cap that was hit rather than a fabricated count."""
    graph = build_relationship_graph(
        _adversarial_selector_inputs(pdbs=4, pods=4),
        _selector_coverage(),
        limits=GraphLimits(max_edges=3),
    )
    record = next(item for item in graph.coverage if item.state is CoverageState.CAPPED)
    assert graph.truncated
    assert "3-edge cap" in record.detail


def test_deduplicated_edges_survive_a_cap_larger_than_the_distinct_edge_count() -> None:
    """Duplicate candidate edges must not consume cap budget: the same
    fact declared twice is one edge, leaving room for the rest."""
    duplicate = _ref("uses_config", "", "ConfigMap", "prod", "api-config", field="spec.volumes[0]")
    pod = _input(
        "Pod",
        "",
        "prod",
        "api-0",
        "pod-1",
        relationships=_facts(references=(duplicate, duplicate)),
    )
    other = _input(
        "Pod",
        "",
        "prod",
        "api-1",
        "pod-2",
        relationships=_facts(references=(duplicate,)),
    )
    graph = build_relationship_graph(
        [pod, other], [_complete("pods")], limits=GraphLimits(max_edges=2)
    )
    assert len(graph.edges) == 2
    assert not graph.truncated


def test_pv_claim_ref_uid_flows_from_the_manifest_into_graph_resolution() -> None:
    """End-to-end: a PV manifest's `spec.claimRef.uid` must survive
    extraction and make a recreated same-named claim resolve as missing."""
    facts = extract_relationship_facts(
        "PersistentVolume",
        "",
        "v1",
        {
            "metadata": {"name": "pv-1"},
            "spec": {"claimRef": {"namespace": "prod", "name": "api-data", "uid": "pvc-old"}},
        },
    )
    volume = _input("PersistentVolume", "", "", "pv-1", "pv-uid", relationships=facts)
    claim = _input("PersistentVolumeClaim", "", "prod", "api-data", "pvc-new")
    graph = build_relationship_graph(
        [volume, claim], [_complete("persistentvolumes"), _complete("persistentvolumeclaims")]
    )
    edge = next(edge for edge in graph.edges if edge.subject.kind == "PersistentVolume")
    assert edge.resolution is EdgeResolution.MISSING
    assert edge.target.uid == "pvc-old"


def test_endpoint_slice_target_without_namespace_resolves_in_its_own_namespace() -> None:
    """Manifest -> graph: a same-namespace endpoint target that omits
    `targetRef.namespace` must resolve to the Pod in the slice's namespace,
    not stay missing against a blank (cluster-scoped) namespace."""
    facts = extract_relationship_facts(
        "EndpointSlice",
        "discovery.k8s.io",
        "v1",
        {
            "metadata": {"name": "api-abc", "namespace": "prod"},
            "endpoints": [{"targetRef": {"apiVersion": "v1", "kind": "Pod", "name": "api-0"}}],
        },
    )
    slice_input = _input(
        "EndpointSlice", "discovery.k8s.io", "prod", "api-abc", "eps-1", relationships=facts
    )
    pod = _pod_input("api-0", namespace="prod")
    graph = build_relationship_graph(
        [slice_input, pod], [_complete("endpointslices"), _complete("pods")]
    )
    edge = next(edge for edge in graph.edges if edge.relation is RelationKind.ROUTES_TO)
    assert edge.resolution is EdgeResolution.RESOLVED
    assert edge.target.namespace == "prod"


def _diamond_graph() -> RelationshipGraph:
    """`root` <- rs-a, rs-b; `pod-0` depends on both (a diamond, not a cycle)."""
    root = _input("Deployment", "apps", "prod", "api", "deploy-1")
    replica_sets = [
        _input(
            "ReplicaSet",
            "apps",
            "prod",
            name,
            name,
            relationships=_facts(
                references=(
                    _ref(
                        "owned_by",
                        "apps",
                        "Deployment",
                        "prod",
                        "api",
                        uid="deploy-1",
                        field="metadata.ownerReferences[0]",
                    ),
                )
            ),
        )
        for name in ("rs-a", "rs-b")
    ]
    pod = _input(
        "Pod",
        "",
        "prod",
        "pod-0",
        "pod-0",
        relationships=_facts(
            references=(
                _ref(
                    "owned_by",
                    "apps",
                    "ReplicaSet",
                    "prod",
                    "rs-a",
                    uid="rs-a",
                    field="metadata.ownerReferences[0]",
                ),
                _ref(
                    "uses_config",
                    "apps",
                    "ReplicaSet",
                    "prod",
                    "rs-b",
                    uid="rs-b",
                    field="spec.volumes[0].configMap",
                ),
            )
        ),
    )
    return build_relationship_graph(
        [root, *replica_sets, pod],
        [_complete("deployments"), _complete("replicasets"), _complete("pods")],
    )


def test_walk_dependents_does_not_call_a_diamond_join_a_cycle() -> None:
    """Two independent paths converging on one dependent is a DAG, not a
    cycle: the second path's edge revisits an already-visited resource
    without ever returning to an ancestor of itself."""
    graph = _diamond_graph()
    root = _resource(graph, "Deployment", "api")
    result = graph.walk_dependents(root)
    assert result.cycles == ()
    assert [edge.subject.name for edge in result.edges] == ["rs-a", "rs-b", "pod-0"]
    assert [edge.evidence.field for edge in result.revisits] == ["spec.volumes[0].configMap"]
    assert result.truncated is False


def test_walk_dependents_reports_parallel_edges_as_revisits_not_cycles() -> None:
    """Two distinct relationships between the same pair are two edges, but
    the second is a repeat of a resource already reached — not a loop back
    into the path that reached it."""
    deployment = _input("Deployment", "apps", "prod", "api", "deploy-1")
    pod = _input(
        "Pod",
        "",
        "prod",
        "api-0",
        "pod-1",
        relationships=_facts(
            references=(
                _ref(
                    "owned_by",
                    "apps",
                    "Deployment",
                    "prod",
                    "api",
                    uid="deploy-1",
                    field="metadata.ownerReferences[0]",
                ),
                _ref(
                    "uses_config",
                    "apps",
                    "Deployment",
                    "prod",
                    "api",
                    uid="deploy-1",
                    field="spec.volumes[0].configMap",
                ),
            )
        ),
    )
    graph = build_relationship_graph(
        [deployment, pod], [_complete("deployments"), _complete("pods")]
    )
    result = graph.walk_dependents(_resource(graph, "Deployment", "api"))
    assert [edge.evidence.field for edge in result.edges] == ["metadata.ownerReferences[0]"]
    assert [edge.evidence.field for edge in result.revisits] == ["spec.volumes[0].configMap"]
    assert result.cycles == ()


def test_walk_dependents_still_reports_a_genuine_back_edge_as_a_cycle() -> None:
    """A dependent that loops back into an ancestor of the path that
    reached it is a real cycle and must stay classified as one."""
    graph = _owner_graph(
        ("Deployment", "deploy-1"),
        ("ReplicaSet", "rs-1"),
        ("Pod", "pod-1"),
        cycle_to="pod-1",
    )
    result = graph.walk_dependents(_resource(graph, "Deployment", "deploy-1"))
    assert [edge.evidence.field for edge in result.cycles] == ["spec.cycleProbe"]
    assert result.revisits == ()


def test_walk_dependents_reports_a_self_dependency_as_a_cycle() -> None:
    """A resource that references itself loops back into the path trivially."""
    config = _input(
        "ConfigMap",
        "",
        "prod",
        "api-config",
        "cm-1",
        relationships=_facts(
            references=(
                _ref(
                    "uses_config", "", "ConfigMap", "prod", "api-config", uid="cm-1", field="spec"
                ),
            )
        ),
    )
    graph = build_relationship_graph([config], [_complete("configmaps")])
    result = graph.walk_dependents(_resource(graph, "ConfigMap", "api-config"))
    assert [edge.evidence.field for edge in result.cycles] == ["spec"]
    assert result.edges == ()
