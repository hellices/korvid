"""Tests for the immutable operational relationship graph (issue #281).

The graph is a pure core model: it consumes `RelationshipFacts` already
attached to list/watch summaries (Task 2) and produces an immutable,
deterministically capped graph with explicit coverage and edge resolution
states. It never retains summary status/custom data or raw manifests.
"""

from __future__ import annotations

from korvid.core.relationships import (
    CoverageRecord,
    CoverageState,
    EdgeResolution,
    GraphInput,
    GraphLimits,
    GraphResource,
    RelationshipGraph,
    build_relationship_graph,
)
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.relationship_facts import (
    FactConfidence,
    ReferenceFact,
    RelationKind,
    RelationshipFacts,
    TargetReference,
)


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
    assert "cross-namespace owner" in graph.edges[0].explanation


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
