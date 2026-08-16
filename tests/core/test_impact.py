"""Deterministic blast-radius summaries over one relationship snapshot (#283).

`summarize_impact` is pure: one immutable graph plus one proposed action in,
one immutable `ImpactSummary` out. These tests pin the closed action/relation
semantics on the resource pairs those relations really occur between, the
direct/transitive split, deterministic paths, cycle versus revisit
classification (including parity with `RelationshipGraph.walk_dependents`),
both caps, a target the snapshot never saw, the recorded scope, and the
bounded unresolved-reference warning - whose policy every action has to
choose explicitly, exhaustively keyed by `ACTION_UNRESOLVED_RELATIONS`.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from korvid.core.impact import (
    ACTION_RELATIONS,
    ACTION_UNRESOLVED_RELATIONS,
    ImpactAction,
    ImpactLimits,
    summarize_impact,
)
from korvid.core.relationships import (
    CoverageRecord,
    CoverageState,
    EdgeResolution,
    EvidencePointer,
    GraphResource,
    RelationshipEdge,
    RelationshipGraph,
)
from korvid.k8s.relationship_facts import FactConfidence, RelationKind

_COMPLETE_COVERAGE = (
    CoverageRecord(group="", resource="pods", scope="prod", state=CoverageState.COMPLETE),
)


def _res(
    kind: str,
    name: str,
    *,
    group: str = "",
    namespace: str = "prod",
    uid: str | None = None,
) -> GraphResource:
    return GraphResource(group=group, kind=kind, namespace=namespace, name=name, uid=uid)


def _edge(
    subject: GraphResource,
    target: GraphResource,
    relation: RelationKind,
    *,
    confidence: FactConfidence = FactConfidence.DECLARED,
    resolution: EdgeResolution = EdgeResolution.RESOLVED,
    field: str = "metadata.ownerReferences[0]",
    evidence_resource: GraphResource | None = None,
) -> RelationshipEdge:
    """One edge, with the graph's own dependent -> dependency direction.

    `evidence_resource` defaults to the subject, which is what every
    reference-derived fact produces; selector-derived facts (`managed_by`,
    `protected_by`) keep the *declaring* object as evidence while the
    matched object becomes the subject, so those cases pass it explicitly.
    """
    return RelationshipEdge(
        subject=subject,
        target=target,
        relation=relation,
        confidence=confidence,
        evidence=EvidencePointer(resource=evidence_resource or subject, field=field),
        resolution=resolution,
    )


def _graph(
    *edges: RelationshipEdge,
    coverage: tuple[CoverageRecord, ...] = _COMPLETE_COVERAGE,
    truncated: bool = False,
    extra_nodes: tuple[GraphResource, ...] = (),
) -> RelationshipGraph:
    nodes: list[GraphResource] = []
    for resource in (*extra_nodes, *(r for edge in edges for r in (edge.subject, edge.target))):
        if resource not in nodes:
            nodes.append(resource)
    return RelationshipGraph(
        nodes=tuple(nodes), edges=tuple(edges), coverage=coverage, truncated=truncated
    )


#: One realistic edge per relation a delete may follow, built from the
#: resource pair `korvid.k8s.relationship_facts` actually produces it
#: between. A synthetic pair (every relation between the same Pod and
#: ConfigMap) would pass while proving nothing about the semantics.
_DELETE_CASES = [
    pytest.param(
        _edge(
            _res("ReplicaSet", "web-abc", group="apps", uid="rs-1"),
            _res("Deployment", "web", group="apps", uid="deploy-1"),
            RelationKind.OWNED_BY,
        ),
        id="owned_by-replicaset-owned-by-deployment",
    ),
    pytest.param(
        _edge(
            _res("Pod", "web-abc-1", uid="pod-1"),
            _res("Deployment", "web", group="apps", uid="deploy-1"),
            RelationKind.MANAGED_BY,
            field="spec.selector",
            evidence_resource=_res("Deployment", "web", group="apps", uid="deploy-1"),
        ),
        id="managed_by-pod-managed-by-deployment",
    ),
    pytest.param(
        _edge(
            _res("Ingress", "web", group="networking.k8s.io", uid="ing-1"),
            _res("Service", "web", uid="svc-1"),
            RelationKind.ROUTES_TO,
            field="spec.rules[0].http.paths[0].backend.service",
        ),
        id="routes_to-ingress-routes-to-service",
    ),
    pytest.param(
        _edge(
            _res("EndpointSlice", "web-xyz", group="discovery.k8s.io", uid="eps-1"),
            _res("Pod", "web-abc-1", uid="pod-1"),
            RelationKind.ROUTES_TO,
            confidence=FactConfidence.OBSERVED,
            field="endpoints[0].targetRef",
        ),
        id="routes_to-endpointslice-routes-to-pod",
    ),
    pytest.param(
        _edge(
            _res("Pod", "web-abc-1", uid="pod-1"),
            _res("PersistentVolumeClaim", "data", uid="pvc-1"),
            RelationKind.USES_VOLUME,
            field="spec.volumes[0].persistentVolumeClaim",
        ),
        id="uses_volume-pod-uses-claim",
    ),
    pytest.param(
        _edge(
            _res("Deployment", "web", group="apps", uid="deploy-1"),
            _res("ConfigMap", "app-config", uid="cm-1"),
            RelationKind.USES_CONFIG,
            field="spec.template.spec.volumes[0].configMap",
        ),
        id="uses_config-workload-uses-configmap",
    ),
    pytest.param(
        _edge(
            _res("Pod", "web-abc-1", uid="pod-1"),
            _res("PodDisruptionBudget", "web", group="policy", uid="pdb-1"),
            RelationKind.PROTECTED_BY,
            field="spec.selector",
            evidence_resource=_res("PodDisruptionBudget", "web", group="policy", uid="pdb-1"),
        ),
        id="protected_by-pod-protected-by-pdb",
    ),
    pytest.param(
        _edge(
            _res("Pod", "web-abc-1", uid="pod-1"),
            _res("Node", "worker-1", namespace="", uid="node-1"),
            RelationKind.SCHEDULED_ON,
            confidence=FactConfidence.OBSERVED,
            field="spec.nodeName",
        ),
        id="scheduled_on-pod-scheduled-on-node",
    ),
    pytest.param(
        _edge(
            _res("PersistentVolumeClaim", "data", uid="pvc-1"),
            _res("PersistentVolume", "pv-data", namespace="", uid="pv-1"),
            RelationKind.BOUND_TO,
            field="spec.volumeName",
        ),
        id="bound_to-claim-bound-to-volume",
    ),
]


def test_only_supported_writes_carry_action_semantics() -> None:
    assert [action.value for action in ImpactAction] == [
        "delete",
        "rollout_restart",
        "scale_down",
    ]
    assert set(ACTION_RELATIONS) == set(ImpactAction)
    assert ACTION_RELATIONS[ImpactAction.SCALE_DOWN] == frozenset(
        {
            RelationKind.OWNED_BY,
            RelationKind.MANAGED_BY,
            RelationKind.SELECTS,
            RelationKind.ROUTES_TO,
        }
    )
    assert RelationKind.SELECTS not in ACTION_RELATIONS[ImpactAction.DELETE]
    assert RelationKind.SELECTS not in ACTION_RELATIONS[ImpactAction.ROLLOUT_RESTART]
    assert {
        cast(RelationshipEdge, edge).relation for param in _DELETE_CASES for edge in param.values
    } == ACTION_RELATIONS[ImpactAction.DELETE]


def test_every_action_chooses_its_unresolved_reference_policy() -> None:
    """The unresolved-warning policy is keyed by action, never opted into.

    `ACTION_UNRESOLVED_RELATIONS` is exhaustive over `ImpactAction` on
    purpose: `None` means "warn about a dangling reference of any relation",
    a frozenset means "warn only about these". A future action that forgets
    to choose is absent from the mapping, so `summarize_impact` raises
    rather than inheriting whichever policy happened to be the default -
    and this assertion fails before that code ever runs.

    The scale-down half is asserted with `is`, not `==`: the policy is
    "reuse the action's own walked set", so the two mappings must hold the
    *same* frozenset. An equal-but-separate literal would satisfy `==` while
    reopening exactly the drift the shared object rules out - a relation
    added to the walk but not to the warning, or the reverse.
    """
    assert set(ACTION_UNRESOLVED_RELATIONS) == set(ImpactAction)
    assert ACTION_UNRESOLVED_RELATIONS[ImpactAction.DELETE] is None
    assert ACTION_UNRESOLVED_RELATIONS[ImpactAction.ROLLOUT_RESTART] is None
    assert (
        ACTION_UNRESOLVED_RELATIONS[ImpactAction.SCALE_DOWN]
        is ACTION_RELATIONS[ImpactAction.SCALE_DOWN]
    )


@pytest.mark.parametrize("action", list(ImpactAction))
def test_every_action_summarizes_without_a_missing_policy(action: ImpactAction) -> None:
    """Both closed mappings are indexed for real, for every action.

    An action present in `ACTION_RELATIONS` but missing from
    `ACTION_UNRESOLVED_RELATIONS` (or the reverse) raises `KeyError` here
    instead of silently producing an advisory nobody chose the semantics
    for.
    """
    pod = _res("Pod", "web-1")
    missing_config = _res("ConfigMap", "gone")
    graph = _graph(
        _edge(
            pod,
            missing_config,
            RelationKind.USES_CONFIG,
            resolution=EdgeResolution.MISSING,
            field="spec.volumes[0].configMap",
        )
    )
    summary = summarize_impact(graph, action, pod)
    assert summary.action is action


@pytest.mark.parametrize("edge", _DELETE_CASES)
def test_delete_follows_every_supported_relation(edge: RelationshipEdge) -> None:
    """Deleting the dependency reports the dependent that declared it."""
    summary = summarize_impact(_graph(edge), ImpactAction.DELETE, edge.target)
    assert [item.resource for item in summary.direct] == [edge.subject]
    assert summary.transitive == ()
    assert summary.target_present is True
    assert summary.incomplete is False


def test_delete_never_claims_a_selecting_service_fails() -> None:
    """Deleting one selected Pod must not claim the Service selecting it breaks."""
    pod = _res("Pod", "web-1")
    service = _res("Service", "web")
    summary = summarize_impact(
        _graph(_edge(service, pod, RelationKind.SELECTS, field="spec.selector")),
        ImpactAction.DELETE,
        pod,
    )
    assert summary.direct == ()
    assert summary.transitive == ()
    assert summary.unresolved == ()


@pytest.mark.parametrize("relation", [RelationKind.OWNED_BY, RelationKind.MANAGED_BY])
def test_rollout_restart_follows_owner_and_manager_relations(relation: RelationKind) -> None:
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    pod = _res("Pod", "web-abc-1", uid="pod-1")
    summary = summarize_impact(
        _graph(_edge(pod, deployment, relation)), ImpactAction.ROLLOUT_RESTART, deployment
    )
    assert [item.resource for item in summary.direct] == [pod]


@pytest.mark.parametrize(
    "relation",
    [
        RelationKind.SELECTS,
        RelationKind.ROUTES_TO,
        RelationKind.USES_VOLUME,
        RelationKind.USES_CONFIG,
        RelationKind.PROTECTED_BY,
        RelationKind.SCHEDULED_ON,
        RelationKind.BOUND_TO,
    ],
)
def test_rollout_restart_ignores_every_relation_outside_its_closed_set(
    relation: RelationKind,
) -> None:
    """Counterfactual on purpose: no extractor emits a `scheduled_on` or
    `bound_to` edge *into* a Deployment today. The closed set must hold
    anyway - if a CRD fact, a future extractor, or a hand-built summary ever
    offers one, a restart must still claim nothing about it."""
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    other = _res("Service", "web", uid="svc-1")
    summary = summarize_impact(
        _graph(_edge(other, deployment, relation, field="spec.selector")),
        ImpactAction.ROLLOUT_RESTART,
        deployment,
    )
    assert summary.direct == ()
    assert summary.transitive == ()


@pytest.mark.parametrize(
    "relation",
    [
        RelationKind.OWNED_BY,
        RelationKind.MANAGED_BY,
        RelationKind.SELECTS,
        RelationKind.ROUTES_TO,
    ],
)
def test_scale_down_follows_every_relation_in_its_closed_set(relation: RelationKind) -> None:
    workload = _res("Deployment", "web", group="apps", uid="deploy-1")
    dependent = _res("Pod", "web-abc-1", uid="pod-1")
    summary = summarize_impact(
        _graph(_edge(dependent, workload, relation, field="spec.selector")),
        ImpactAction.SCALE_DOWN,
        workload,
    )
    assert [item.resource for item in summary.direct] == [dependent]


@pytest.mark.parametrize(
    "relation",
    [
        RelationKind.USES_VOLUME,
        RelationKind.USES_CONFIG,
        RelationKind.PROTECTED_BY,
        RelationKind.SCHEDULED_ON,
        RelationKind.BOUND_TO,
    ],
)
def test_scale_down_ignores_every_relation_outside_its_closed_set(
    relation: RelationKind,
) -> None:
    workload = _res("Deployment", "web", group="apps", uid="deploy-1")
    other = _res("PodDisruptionBudget", "web", group="policy", uid="other-1")
    summary = summarize_impact(
        _graph(_edge(other, workload, relation, field="spec.selector")),
        ImpactAction.SCALE_DOWN,
        workload,
    )
    assert summary.direct == ()
    assert summary.transitive == ()


def test_direct_and_transitive_dependents_stay_separate_with_their_paths() -> None:
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    replicaset = _res("ReplicaSet", "web-abc", group="apps", uid="rs-1")
    pod = _res("Pod", "web-abc-1", uid="pod-1")
    graph = _graph(
        _edge(replicaset, deployment, RelationKind.OWNED_BY),
        _edge(pod, replicaset, RelationKind.OWNED_BY),
    )
    summary = summarize_impact(graph, ImpactAction.DELETE, deployment)
    assert [item.resource for item in summary.direct] == [replicaset]
    assert [item.resource for item in summary.transitive] == [pod]
    assert len(summary.direct[0].path) == 1
    assert summary.transitive[0].path[0].target == deployment
    assert summary.transitive[0].path[-1].subject == pod


def test_unresolved_edges_are_never_traversed_as_dependents() -> None:
    configmap = _res("ConfigMap", "app-config")
    pod = _res("Pod", "web-1")
    graph = _graph(
        _edge(
            pod,
            configmap,
            RelationKind.USES_CONFIG,
            resolution=EdgeResolution.MISSING,
            field="spec.volumes[0].configMap",
        )
    )
    summary = summarize_impact(graph, ImpactAction.DELETE, configmap)
    assert summary.direct == ()
    assert summary.unresolved == ()  # its subject is neither the target nor impacted


def test_unresolved_references_are_limited_to_the_affected_set() -> None:
    configmap = _res("ConfigMap", "app-config")
    pod = _res("Pod", "web-1")
    unrelated = _res("Pod", "unrelated-9")
    missing_claim = _res("PersistentVolumeClaim", "data", uid="pvc-gone")
    graph = _graph(
        _edge(pod, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap"),
        _edge(
            pod,
            missing_claim,
            RelationKind.USES_VOLUME,
            resolution=EdgeResolution.MISSING,
            field="spec.volumes[1].persistentVolumeClaim",
        ),
        _edge(
            unrelated,
            missing_claim,
            RelationKind.USES_VOLUME,
            resolution=EdgeResolution.MISSING,
            field="spec.volumes[0].persistentVolumeClaim",
        ),
    )
    summary = summarize_impact(graph, ImpactAction.DELETE, configmap)
    assert [item.resource for item in summary.direct] == [pod]
    assert [(edge.subject, edge.target) for edge in summary.unresolved] == [(pod, missing_claim)]


def test_unresolved_reference_declared_by_the_target_itself_is_reported() -> None:
    pod = _res("Pod", "web-1")
    missing_config = _res("ConfigMap", "gone")
    graph = _graph(
        _edge(
            pod,
            missing_config,
            RelationKind.USES_CONFIG,
            resolution=EdgeResolution.MISSING,
            field="spec.volumes[0].configMap",
        )
    )
    summary = summarize_impact(graph, ImpactAction.DELETE, pod)
    assert summary.direct == ()
    assert [edge.target for edge in summary.unresolved] == [missing_config]


#: The relations a scale-down deliberately excludes from its closed set,
#: each on the resource pair `korvid.k8s.relationship_facts` really produces
#: it between (see `_SCALE_DOWN_RELATIONS`). A dangling reference of one of
#: these describes what a *remaining* Pod still holds, not something the
#: scale-down itself changes. Pinned exhaustive by
#: `test_the_excluded_unresolved_cases_cover_every_relation_a_scale_down_omits`.
_SCALE_DOWN_EXCLUDED_UNRESOLVED = [
    pytest.param(
        RelationKind.USES_VOLUME,
        _res("PersistentVolumeClaim", "data", uid="pvc-gone"),
        "spec.volumes[0].persistentVolumeClaim",
        id="uses_volume-pod-mounts-a-missing-claim",
    ),
    pytest.param(
        RelationKind.USES_CONFIG,
        _res("ConfigMap", "app-config"),
        "spec.volumes[1].configMap",
        id="uses_config-pod-mounts-a-missing-configmap",
    ),
    pytest.param(
        RelationKind.PROTECTED_BY,
        _res("PodDisruptionBudget", "web", group="policy"),
        "spec.selector",
        id="protected_by-pod-names-a-missing-pdb",
    ),
    pytest.param(
        RelationKind.SCHEDULED_ON,
        _res("Node", "worker-1", namespace=""),
        "spec.nodeName",
        id="scheduled_on-pod-names-a-missing-node",
    ),
    pytest.param(
        RelationKind.BOUND_TO,
        _res("PersistentVolume", "pv-data", namespace=""),
        "spec.volumeName",
        id="bound_to-pod-names-a-missing-volume",
    ),
]


def test_the_excluded_unresolved_cases_cover_every_relation_a_scale_down_omits() -> None:
    """The excluded-case list is derived from the closed set, not hand-kept.

    A `RelationKind` added later is either in
    `ACTION_RELATIONS[ImpactAction.SCALE_DOWN]` - and then its unresolved
    behaviour is pinned by the included-relation test below - or outside it,
    and then this assertion fails until a case for it exists here. Neither
    way can a new relation quietly escape the scale-down's warning policy.
    """
    assert {
        cast(RelationKind, param.values[0]) for param in _SCALE_DOWN_EXCLUDED_UNRESOLVED
    } == set(RelationKind) - ACTION_RELATIONS[ImpactAction.SCALE_DOWN]


@pytest.mark.parametrize(("relation", "missing", "field"), _SCALE_DOWN_EXCLUDED_UNRESOLVED)
def test_scale_down_never_warns_about_an_unresolved_relation_it_excludes(
    relation: RelationKind, missing: GraphResource, field: str
) -> None:
    """A scale-down's unresolved warning obeys the same closed set its walk
    does.

    Scaling a workload down does not detach a mounted volume or ConfigMap,
    evict a Pod past its PDB, move a Pod off its node, or unbind a claim -
    which is exactly why those relations are absent from
    `ACTION_RELATIONS[ImpactAction.SCALE_DOWN]`. A *dangling* reference of
    one of them says nothing more about the scale-down than a resolved one
    would, so warning about it would smuggle back in, as a warning, the
    claim the closed set refuses to make.
    """
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    pod = _res("Pod", "web-abc-1", uid="pod-1")
    graph = _graph(
        _edge(pod, deployment, RelationKind.MANAGED_BY, field="spec.selector"),
        _edge(pod, missing, relation, resolution=EdgeResolution.MISSING, field=field),
    )
    summary = summarize_impact(graph, ImpactAction.SCALE_DOWN, deployment)
    assert [item.resource for item in summary.direct] == [pod]
    assert summary.unresolved == ()


@pytest.mark.parametrize("action", [ImpactAction.DELETE, ImpactAction.ROLLOUT_RESTART])
@pytest.mark.parametrize(("relation", "missing", "field"), _SCALE_DOWN_EXCLUDED_UNRESOLVED)
def test_delete_and_restart_still_warn_about_every_unresolved_relation(
    action: ImpactAction, relation: RelationKind, missing: GraphResource, field: str
) -> None:
    """The relation-blind warning is scale-down-specific narrowing only.

    `ACTION_UNRESOLVED_RELATIONS` maps both of these actions to `None`, and
    that is not an accident of the excluded relations: a delete removes the
    object those references were resolved against, and a rollout restart
    recreates the Pod that has to satisfy them again - a Pod that mounts a
    ConfigMap which no longer exists will not come back - so for both, a
    dangling reference of *any* relation inside the affected set is a real
    reason the action may not land the way the reader expects. Narrowing
    them to their own walk relations would drop exactly that warning.
    """
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    pod = _res("Pod", "web-abc-1", uid="pod-1")
    graph = _graph(
        _edge(pod, deployment, RelationKind.MANAGED_BY, field="spec.selector"),
        _edge(pod, missing, relation, resolution=EdgeResolution.MISSING, field=field),
    )
    summary = summarize_impact(graph, action, deployment)
    assert [item.resource for item in summary.direct] == [pod]
    assert [(edge.relation, edge.target) for edge in summary.unresolved] == [(relation, missing)]


def test_scale_down_warns_about_an_unresolved_relation_inside_its_closed_set() -> None:
    """The narrowing is by relation, not a blanket silence.

    An EndpointSlice whose `targetRef` names a Pod the snapshot never saw is
    a dangling `routes_to` - a relation a scale-down *does* follow - held by
    a resource the scale-down affects, so it is still reported.
    """
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    pod = _res("Pod", "web-abc-1", uid="pod-1")
    slice_ = _res("EndpointSlice", "web-xyz", group="discovery.k8s.io", uid="eps-1")
    missing_pod = _res("Pod", "web-abc-2")
    graph = _graph(
        _edge(pod, deployment, RelationKind.MANAGED_BY, field="spec.selector"),
        _edge(
            slice_,
            pod,
            RelationKind.ROUTES_TO,
            confidence=FactConfidence.OBSERVED,
            field="endpoints[0].targetRef",
        ),
        _edge(
            slice_,
            missing_pod,
            RelationKind.ROUTES_TO,
            resolution=EdgeResolution.MISSING,
            confidence=FactConfidence.OBSERVED,
            field="endpoints[1].targetRef",
        ),
    )
    summary = summarize_impact(graph, ImpactAction.SCALE_DOWN, deployment)
    assert [item.resource for item in summary.transitive] == [slice_]
    assert [(edge.relation, edge.target) for edge in summary.unresolved] == [
        (RelationKind.ROUTES_TO, missing_pod)
    ]


def test_scale_down_warns_about_an_unresolved_owner_reference() -> None:
    """The included half of the policy is not only `routes_to`.

    A Pod whose `metadata.ownerReferences[0]` names a ReplicaSet the
    snapshot never saw holds a dangling `owned_by` - a relation
    `ACTION_UNRESOLVED_RELATIONS[ImpactAction.SCALE_DOWN]` contains - and the
    Pod is inside the affected set, so the warning clears both bounds and is
    reported.
    """
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    pod = _res("Pod", "web-abc-1", uid="pod-1")
    missing_replicaset = _res("ReplicaSet", "web-abc", group="apps", uid="rs-gone")
    graph = _graph(
        _edge(pod, deployment, RelationKind.MANAGED_BY, field="spec.selector"),
        _edge(
            pod,
            missing_replicaset,
            RelationKind.OWNED_BY,
            resolution=EdgeResolution.MISSING,
            field="metadata.ownerReferences[0]",
        ),
    )
    summary = summarize_impact(graph, ImpactAction.SCALE_DOWN, deployment)
    assert [item.resource for item in summary.direct] == [pod]
    assert [(edge.relation, edge.target) for edge in summary.unresolved] == [
        (RelationKind.OWNED_BY, missing_replicaset)
    ]


def test_a_genuine_loop_is_classified_as_a_cycle_and_walked_once() -> None:
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    replicaset = _res("ReplicaSet", "web-abc", group="apps", uid="rs-1")
    graph = _graph(
        _edge(replicaset, deployment, RelationKind.OWNED_BY),
        _edge(deployment, replicaset, RelationKind.OWNED_BY),
    )
    summary = summarize_impact(graph, ImpactAction.DELETE, deployment)
    assert [item.resource for item in summary.direct] == [replicaset]
    assert summary.transitive == ()
    assert [(edge.subject, edge.target) for edge in summary.cycles] == [(deployment, replicaset)]
    assert summary.revisits == ()
    assert summary.traversal_capped is False


def test_a_parallel_non_looping_edge_is_a_revisit_not_a_cycle_or_a_duplicate() -> None:
    configmap = _res("ConfigMap", "app-config", uid="cm-1")
    pod = _res("Pod", "web-1", uid="pod-1")
    first = _edge(pod, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap")
    second = _edge(
        pod,
        configmap,
        RelationKind.USES_CONFIG,
        field="spec.containers[0].envFrom[0].configMapRef",
    )
    summary = summarize_impact(_graph(first, second), ImpactAction.DELETE, configmap)
    assert [item.resource for item in summary.direct] == [pod]
    assert summary.cycles == ()
    assert summary.revisits == (second,)
    assert summary.traversal_capped is False


def test_the_first_graph_ordered_path_wins_and_repeats_identically() -> None:
    """Two routes reach the EndpointSlice; breadth-first order picks the
    first-reached dependent's path, the other becomes a counted revisit, and
    a second call returns the same value."""
    configmap = _res("ConfigMap", "app-config", uid="cm-1")
    pod_a = _res("Pod", "a-1", uid="pod-a")
    pod_b = _res("Pod", "b-1", uid="pod-b")
    slice_ = _res("EndpointSlice", "web-xyz", group="discovery.k8s.io", uid="eps-1")
    graph = _graph(
        _edge(pod_a, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap"),
        _edge(pod_b, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap"),
        _edge(
            slice_,
            pod_b,
            RelationKind.ROUTES_TO,
            confidence=FactConfidence.OBSERVED,
            field="endpoints[1].targetRef",
        ),
        _edge(
            slice_,
            pod_a,
            RelationKind.ROUTES_TO,
            confidence=FactConfidence.OBSERVED,
            field="endpoints[0].targetRef",
        ),
    )
    summary = summarize_impact(graph, ImpactAction.DELETE, configmap)
    assert [item.resource for item in summary.direct] == [pod_a, pod_b]
    assert [item.resource for item in summary.transitive] == [slice_]
    # `pod_a` entered the frontier first, so its route is the reported path
    # and `pod_b`'s is the revisit - regardless of the graph's edge order.
    assert summary.transitive[0].path[0].subject == pod_a
    assert summary.transitive[0].path[-1].evidence.field == "endpoints[0].targetRef"
    assert [edge.evidence.field for edge in summary.revisits] == ["endpoints[1].targetRef"]
    assert summarize_impact(graph, ImpactAction.DELETE, configmap) == summary


def test_cycle_and_revisit_classification_matches_the_graph_walk() -> None:
    """The impact walk is a deliberate second traversal (closed action
    filtering, full paths, affected-set filtering, its own much smaller
    caps). On edges the action filter leaves untouched it must still
    classify exactly like `RelationshipGraph.walk_dependents`: a genuine
    loop is a cycle, a converging or parallel repeat is a revisit, and each
    dependent is reached once, in the same breadth-first order."""
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    replicaset = _res("ReplicaSet", "web-abc", group="apps", uid="rs-1")
    pod = _res("Pod", "web-abc-1", uid="pod-1")
    slice_ = _res("EndpointSlice", "web-xyz", group="discovery.k8s.io", uid="eps-1")
    graph = _graph(
        _edge(replicaset, deployment, RelationKind.OWNED_BY),
        _edge(pod, deployment, RelationKind.OWNED_BY, field="metadata.ownerReferences[1]"),
        # Reached first straight from the Deployment above, so the walk
        # through the ReplicaSet converges instead of adding a second item.
        _edge(pod, replicaset, RelationKind.OWNED_BY),
        _edge(
            slice_,
            pod,
            RelationKind.ROUTES_TO,
            confidence=FactConfidence.OBSERVED,
            field="endpoints[0].targetRef",
        ),
        # A corrupted owner loop back into the traversal root.
        _edge(deployment, pod, RelationKind.OWNED_BY, field="metadata.ownerReferences[2]"),
    )
    summary = summarize_impact(
        graph, ImpactAction.DELETE, deployment, limits=ImpactLimits(max_depth=5, max_nodes=500)
    )
    walk = graph.walk_dependents(deployment, max_depth=5, max_nodes=500)
    assert summary.cycles == walk.cycles
    assert summary.revisits == walk.revisits
    assert [item.resource for item in (*summary.direct, *summary.transitive)] == [
        edge.subject for edge in walk.edges
    ]
    assert summary.traversal_capped is walk.truncated


def test_depth_cap_stops_the_walk_and_is_reported() -> None:
    root = _res("ConfigMap", "app-config")
    edges: list[RelationshipEdge] = []
    previous = root
    for index in range(5):
        current = _res("Pod", f"hop-{index}", uid=f"pod-{index}")
        edges.append(_edge(current, previous, RelationKind.OWNED_BY))
        previous = current
    summary = summarize_impact(
        _graph(*edges),
        ImpactAction.DELETE,
        root,
        limits=ImpactLimits(max_depth=2, max_nodes=50),
    )
    assert [item.resource.name for item in summary.direct] == ["hop-0"]
    assert [item.resource.name for item in summary.transitive] == ["hop-1"]
    assert summary.traversal_capped is True
    assert summary.incomplete is True


def test_node_cap_stops_the_walk_and_is_reported() -> None:
    configmap = _res("ConfigMap", "app-config")
    edges = [
        _edge(
            _res("Pod", f"web-{index}", uid=f"pod-{index}"),
            configmap,
            RelationKind.USES_CONFIG,
            field="spec.volumes[0].configMap",
        )
        for index in range(4)
    ]
    summary = summarize_impact(
        _graph(*edges),
        ImpactAction.DELETE,
        configmap,
        limits=ImpactLimits(max_depth=3, max_nodes=2),
    )
    assert [item.resource.name for item in summary.direct] == ["web-0", "web-1"]
    assert summary.traversal_capped is True
    assert summary.incomplete is True


def test_inferred_edges_are_labelled_and_still_listed() -> None:
    configmap = _res("ConfigMap", "app-config")
    pod = _res("Pod", "web-1")
    declared = summarize_impact(
        _graph(_edge(pod, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap")),
        ImpactAction.DELETE,
        configmap,
    )
    inferred = summarize_impact(
        _graph(
            _edge(
                pod,
                configmap,
                RelationKind.USES_CONFIG,
                confidence=FactConfidence.INFERRED,
                field="spec.volumes[0].configMap",
            )
        ),
        ImpactAction.DELETE,
        configmap,
    )
    assert declared.direct[0].inferred is False
    assert inferred.direct[0].inferred is True
    assert inferred.direct[0].resource == pod


def test_incomplete_is_false_only_for_a_complete_uncapped_snapshot() -> None:
    configmap = _res("ConfigMap", "app-config")
    pod = _res("Pod", "web-1")
    edge = _edge(pod, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap")
    complete = summarize_impact(_graph(edge), ImpactAction.DELETE, configmap)
    forbidden = summarize_impact(
        _graph(
            edge,
            coverage=(
                CoverageRecord(
                    group="",
                    resource="secrets",
                    scope="prod",
                    state=CoverageState.FORBIDDEN,
                    detail="secrets is forbidden",
                ),
            ),
        ),
        ImpactAction.DELETE,
        configmap,
    )
    truncated = summarize_impact(_graph(edge, truncated=True), ImpactAction.DELETE, configmap)
    assert complete.incomplete is False
    assert forbidden.incomplete is True
    assert truncated.incomplete is True
    assert truncated.graph_truncated is True


def test_a_target_missing_from_the_snapshot_is_reported_instead_of_answered() -> None:
    """A row deleted and recreated under the same name carries a new UID, so
    the identity the write targets is simply not in this graph. Reporting
    "no dependents" for it would be a lie by omission."""
    live = _res("ConfigMap", "app-config", uid="cm-1")
    replaced = _res("ConfigMap", "app-config", uid="cm-0")
    pod = _res("Pod", "web-1", uid="pod-1")
    graph = _graph(_edge(pod, live, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap"))
    stale = summarize_impact(graph, ImpactAction.DELETE, replaced)
    assert stale.target_present is False
    assert stale.direct == ()
    assert stale.transitive == ()
    assert stale.incomplete is True
    assert summarize_impact(graph, ImpactAction.DELETE, live).target_present is True


def test_the_snapshot_scope_is_recorded_verbatim() -> None:
    """The scope is the caller's, never inferred from the graph: only the
    caller knows whether it listed one namespace or all of them."""
    configmap = _res("ConfigMap", "app-config", uid="cm-1")
    graph = _graph(extra_nodes=(configmap,))
    assert summarize_impact(graph, ImpactAction.DELETE, configmap, scope="prod").scope == "prod"
    assert summarize_impact(graph, ImpactAction.DELETE, configmap).scope is None
    assert summarize_impact(graph, ImpactAction.DELETE, configmap).target_present is True


def test_summary_items_and_limits_are_immutable() -> None:
    configmap = _res("ConfigMap", "app-config")
    pod = _res("Pod", "web-1")
    summary = summarize_impact(
        _graph(_edge(pod, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap")),
        ImpactAction.DELETE,
        configmap,
    )
    with pytest.raises(FrozenInstanceError, match="cannot assign to field"):
        summary.traversal_capped = True  # type: ignore[misc]  # frozen by design
    with pytest.raises(FrozenInstanceError, match="cannot assign to field"):
        summary.direct[0].resource = configmap  # type: ignore[misc]  # frozen by design
    with pytest.raises(FrozenInstanceError, match="cannot assign to field"):
        ImpactLimits().max_nodes = 1  # type: ignore[misc]  # frozen by design


def test_the_impact_model_imports_no_textual() -> None:
    """`korvid.core` is Textual-free (AGENTS.md layer rules)."""
    probe = (
        "import sys\n"
        "import korvid.core.impact  # noqa: F401\n"
        "leaked = [m for m in sys.modules if m == 'textual' or m.startswith('textual.')]\n"
        "if leaked:\n"
        "    raise SystemExit(f'core.impact leaked Textual: {leaked}')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
