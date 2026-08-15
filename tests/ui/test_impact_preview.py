"""Advisory blast-radius text for write approval dialogs (issue #283).

The renderer is pure and Textual-free: `ImpactSummary` in, bounded literal
lines out. These tests pin the exact line grammar (so nothing unexpected can
ever leak into an approval dialog), both bounds (per cluster-derived
fragment and per composed line), the scope/target-presence statements, and
the advisory wording.
"""

from __future__ import annotations

from korvid.core.impact import ImpactAction, ImpactItem, ImpactSummary
from korvid.core.relationships import (
    CoverageRecord,
    CoverageState,
    EdgeResolution,
    EvidencePointer,
    GraphResource,
    RelationshipEdge,
)
from korvid.k8s.relationship_facts import FactConfidence, RelationKind
from korvid.ui.impact_preview import (
    _ACTION_LABEL,
    _MAX_LINE,
    _TRUNCATION_SUFFIX,
    ADVISORY_LINE,
    IMPACT_TITLE,
    IMPACT_UNAVAILABLE_LINES,
    render_impact_lines,
)

_DEPLOY = GraphResource(group="apps", kind="Deployment", namespace="prod", name="web", uid="d-1")
_RS = GraphResource(group="apps", kind="ReplicaSet", namespace="prod", name="web-abc", uid="rs-1")
_POD = GraphResource(group="", kind="Pod", namespace="prod", name="web-abc-1", uid="pod-1")
_SECRET = GraphResource(group="", kind="Secret", namespace="prod", name="db", uid=None)

_OWNS_DEPLOY = RelationshipEdge(
    subject=_RS,
    target=_DEPLOY,
    relation=RelationKind.OWNED_BY,
    confidence=FactConfidence.DECLARED,
    evidence=EvidencePointer(resource=_RS, field="metadata.ownerReferences[0]"),
    resolution=EdgeResolution.RESOLVED,
)
_OWNS_RS = RelationshipEdge(
    subject=_POD,
    target=_RS,
    relation=RelationKind.OWNED_BY,
    confidence=FactConfidence.DECLARED,
    evidence=EvidencePointer(resource=_POD, field="metadata.ownerReferences[0]"),
    resolution=EdgeResolution.RESOLVED,
)
_FORBIDDEN_SECRETS = CoverageRecord(
    group="",
    resource="secrets",
    scope="prod",
    state=CoverageState.FORBIDDEN,
    detail="secrets is forbidden",
)


def _summary(
    *,
    direct: tuple[ImpactItem, ...] = (),
    transitive: tuple[ImpactItem, ...] = (),
    cycles: tuple[RelationshipEdge, ...] = (),
    revisits: tuple[RelationshipEdge, ...] = (),
    unresolved: tuple[RelationshipEdge, ...] = (),
    coverage: tuple[CoverageRecord, ...] = (),
    traversal_capped: bool = False,
    graph_truncated: bool = False,
    action: ImpactAction = ImpactAction.DELETE,
    target: GraphResource = _DEPLOY,
    target_present: bool = True,
    scope: str | None = "prod",
) -> ImpactSummary:
    return ImpactSummary(
        action=action,
        target=target,
        target_present=target_present,
        scope=scope,
        direct=direct,
        transitive=transitive,
        cycles=cycles,
        revisits=revisits,
        unresolved=unresolved,
        coverage=coverage,
        traversal_capped=traversal_capped,
        graph_truncated=graph_truncated,
    )


def test_render_produces_the_exact_deterministic_line_sequence() -> None:
    """Exact-match on purpose: nothing beyond identity, relation, confidence,
    evidence field, scope, and coverage state may ever reach an approval
    dialog."""
    summary = _summary(
        direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
        transitive=(ImpactItem(resource=_POD, path=(_OWNS_DEPLOY, _OWNS_RS)),),
        coverage=(_FORBIDDEN_SECRETS,),
    )
    assert render_impact_lines(summary) == (
        "graph-derived impact (advisory):",
        "  delete apps/Deployment/prod/web",
        "  known direct dependents (may be affected): 1",
        "    - apps/ReplicaSet/prod/web-abc via owned_by (declared) at metadata.ownerReferences[0]",
        "  known transitive dependents (may be affected): 1",
        "    - Pod/prod/web-abc-1 via owned_by (declared) at metadata.ownerReferences[0]"
        " -> owned_by (declared) at metadata.ownerReferences[0]",
        "  scope: prod",
        "  graph coverage: incomplete - a missing dependent here does not prove none exists",
        "    - core/secrets @prod: forbidden",
        ADVISORY_LINE,
    )


def test_empty_sections_and_complete_coverage_are_stated_explicitly() -> None:
    lines = render_impact_lines(
        _summary(
            coverage=(
                CoverageRecord(
                    group="", resource="pods", scope="prod", state=CoverageState.COMPLETE
                ),
            ),
            action=ImpactAction.ROLLOUT_RESTART,
        )
    )
    assert lines[0] == IMPACT_TITLE
    assert lines[1] == "  rollout restart apps/Deployment/prod/web"
    assert "  known direct dependents (may be affected): none in this snapshot" in lines
    assert "  known transitive dependents (may be affected): none in this snapshot" in lines
    assert "  graph coverage: complete" in lines
    assert lines[-1] == ADVISORY_LINE


def test_caps_and_cycles_are_reported_as_their_own_lines() -> None:
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            cycles=(_OWNS_DEPLOY,),
            traversal_capped=True,
            graph_truncated=True,
        )
    )
    assert "  relationship cycles: 1 (loop edges classified, not expanded)" in lines
    assert (
        "  traversal capped: more dependents exist beyond the traversal limits"
        " and are not listed" in lines
    )
    assert (
        "  snapshot truncated: the relationship snapshot hit an input cap, so some"
        " resources were never joined" in lines
    )


def test_revisited_paths_are_counted_never_expanded() -> None:
    """A dependent reached twice is one item plus a count, not two items:
    "2 dependents" when there is one would overstate the blast radius."""
    lines = render_impact_lines(
        _summary(direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),), revisits=(_OWNS_RS,))
    )
    assert "  known direct dependents (may be affected): 1" in lines
    assert "  additional known paths: 1 (already-listed dependents reached again)" in lines
    assert sum(1 for line in lines if line.startswith("    - apps/ReplicaSet/")) == 1


def test_the_snapshot_scope_is_always_stated_even_when_coverage_is_complete() -> None:
    """`complete` is only ever complete *within* the listed scope: a
    namespaced snapshot must not read as a cluster-wide answer."""
    namespaced = render_impact_lines(_summary(scope="prod"))
    cluster_wide = render_impact_lines(_summary(scope=None))
    assert "  scope: prod" in namespaced
    assert "  graph coverage: complete" in namespaced
    assert "  scope: all namespaces" in cluster_wide
    assert namespaced.index("  scope: prod") < namespaced.index("  graph coverage: complete")


def test_a_target_missing_from_the_snapshot_says_dependents_are_unknown() -> None:
    """Without the target in the graph, "none in this snapshot" below is a
    statement about the snapshot, not about the object being deleted."""
    lines = render_impact_lines(_summary(target_present=False))
    assert lines[2] == "  target not found in this snapshot - dependents unknown"
    assert "  known direct dependents (may be affected): none in this snapshot" in lines
    present = render_impact_lines(_summary())
    assert not any(line.startswith("  target not found") for line in present)


def test_every_composed_line_stays_within_the_total_line_bound() -> None:
    """Per-fragment caps do not bound a line that concatenates an identity
    and three hops; the modal is 70 columns wide, so the composed line is
    capped too - and a capped line says so, rather than reading as a
    complete claim that happens to stop mid-path."""
    hostile = GraphResource(group="", kind="Pod", namespace="prod", name="w" * 400)
    edge = RelationshipEdge(
        subject=hostile,
        target=_DEPLOY,
        relation=RelationKind.OWNED_BY,
        confidence=FactConfidence.DECLARED,
        evidence=EvidencePointer(resource=hostile, field="f" * 400),
        resolution=EdgeResolution.RESOLVED,
    )
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=hostile, path=(edge, edge, edge)),),
            unresolved=(edge,),
            scope="n" * 400,
        )
    )
    assert max(len(line) for line in lines) <= _MAX_LINE
    capped = [line for line in lines if len(line) == _MAX_LINE]
    assert capped
    assert all(line.endswith(_TRUNCATION_SUFFIX) for line in capped)
    assert lines[-1] == ADVISORY_LINE


def test_unresolved_references_are_listed_and_bounded() -> None:
    unresolved = tuple(
        RelationshipEdge(
            subject=_POD,
            target=GraphResource(
                group="", kind="ConfigMap", namespace="prod", name=f"gone-{index}"
            ),
            relation=RelationKind.USES_CONFIG,
            confidence=FactConfidence.DECLARED,
            evidence=EvidencePointer(resource=_POD, field=f"spec.volumes[{index}].configMap"),
            resolution=EdgeResolution.MISSING,
        )
        for index in range(7)
    )
    lines = render_impact_lines(_summary(unresolved=unresolved))
    assert "  unresolved references in the affected set: 7" in lines
    assert (
        "    - Pod/prod/web-abc-1 uses_config -> ConfigMap/prod/gone-0 (missing)"
        " at spec.volumes[0].configMap" in lines
    )
    assert "    ... 2 more not shown (preview capped)" in lines
    assert sum(1 for line in lines if line.startswith("    - Pod/prod/web-abc-1 uses_config")) == 5


def test_dependent_lists_are_bounded_with_an_explicit_more_line() -> None:
    items = tuple(
        ImpactItem(
            resource=GraphResource(group="", kind="Pod", namespace="prod", name=f"web-{index}"),
            path=(_OWNS_DEPLOY,),
        )
        for index in range(12)
    )
    lines = render_impact_lines(_summary(direct=items))
    assert "  known direct dependents (may be affected): 12" in lines
    assert sum(1 for line in lines if line.startswith("    - Pod/prod/web-")) == 10
    assert "    ... 2 more not shown (preview capped)" in lines


def test_inferred_items_are_labelled_and_declared_never_blocks() -> None:
    inferred_edge = RelationshipEdge(
        subject=_POD,
        target=_DEPLOY,
        relation=RelationKind.MANAGED_BY,
        confidence=FactConfidence.INFERRED,
        evidence=EvidencePointer(resource=_POD, field="spec.selector"),
        resolution=EdgeResolution.RESOLVED,
    )
    lines = render_impact_lines(
        _summary(direct=(ImpactItem(resource=_POD, path=(inferred_edge,)),))
    )
    assert "    - Pod/prod/web-abc-1 via managed_by (inferred) at spec.selector [inferred]" in lines
    assert "  inferred relationships are labelled and never block this write" in lines


def test_a_long_three_hop_path_keeps_its_inferred_marker_within_the_line_bound() -> None:
    """Nothing pathological here: real names and real field paths.

    A Pod reached through three hops of ordinary Kubernetes field paths
    already composes past `_MAX_LINE`, and the ` [inferred]` marker is the
    *last* thing on the line. Capping the composed line last would drop
    exactly the label that says one hop was guessed, turning a heuristic
    chain into what reads like a declared one. The marker's width is
    reserved instead, and the cut is shown.
    """
    pod = GraphResource(
        group="",
        kind="Pod",
        namespace="payments-production-eu-west-1",
        name="checkout-api-canary-7f9c8b5d64-2xk9p",
        uid="pod-9",
    )
    declared = RelationshipEdge(
        subject=pod,
        target=_DEPLOY,
        relation=RelationKind.USES_CONFIG,
        confidence=FactConfidence.DECLARED,
        evidence=EvidencePointer(
            resource=pod, field="spec.template.spec.volumes[0].projected.sources[1].configMap.name"
        ),
        resolution=EdgeResolution.RESOLVED,
    )
    inferred = RelationshipEdge(
        subject=pod,
        target=_DEPLOY,
        relation=RelationKind.MANAGED_BY,
        confidence=FactConfidence.INFERRED,
        evidence=EvidencePointer(
            resource=pod, field="spec.selector.matchLabels[app.kubernetes.io/instance]"
        ),
        resolution=EdgeResolution.RESOLVED,
    )
    last = RelationshipEdge(
        subject=pod,
        target=_DEPLOY,
        relation=RelationKind.USES_CONFIG,
        confidence=FactConfidence.DECLARED,
        evidence=EvidencePointer(
            resource=pod,
            field="spec.template.spec.initContainers[0].env[3].valueFrom.secretKeyRef.name",
        ),
        resolution=EdgeResolution.RESOLVED,
    )
    lines = render_impact_lines(
        _summary(transitive=(ImpactItem(resource=pod, path=(declared, inferred, last)),))
    )
    item = next(line for line in lines if line.startswith("    - Pod/payments-production"))
    assert len(item) == _MAX_LINE
    assert item.endswith(f"{_TRUNCATION_SUFFIX} [inferred]")
    assert item.startswith(
        "    - Pod/payments-production-eu-west-1/checkout-api-canary-7f9c8b5d64-2xk9p"
        " via uses_config (declared) at"
        " spec.template.spec.volumes[0].projected.sources[1].configMap.name"
        " -> managed_by (inferred) at"
    )
    assert "  inferred relationships are labelled and never block this write" in lines
    assert max(len(line) for line in lines) <= _MAX_LINE


def test_every_impact_action_has_a_rendered_label() -> None:
    """`_ACTION_LABEL` is indexed, not defaulted: a new `ImpactAction`
    without a label would raise a `KeyError` inside a write dialog rather
    than render an unlabelled action."""
    assert set(_ACTION_LABEL) == set(ImpactAction)


def test_cluster_text_stays_literal_and_control_characters_are_flattened() -> None:
    hostile = GraphResource(group="", kind="Pod", namespace="prod", name="[bold red]web\nrogue[/]")
    edge = RelationshipEdge(
        subject=hostile,
        target=_DEPLOY,
        relation=RelationKind.OWNED_BY,
        confidence=FactConfidence.DECLARED,
        evidence=EvidencePointer(resource=hostile, field="metadata\townerReferences[0]"),
        resolution=EdgeResolution.RESOLVED,
    )
    lines = render_impact_lines(_summary(direct=(ImpactItem(resource=hostile, path=(edge,)),)))
    body = "\n".join(lines)
    assert "[bold red]web rogue[/]" in body
    assert "metadata ownerReferences[0]" in body
    assert not any("\n" in line or "\t" in line for line in lines)


def test_no_line_claims_a_guaranteed_failure_and_the_advisory_is_always_last() -> None:
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            coverage=(_FORBIDDEN_SECRETS,),
            traversal_capped=True,
        )
    )
    body = " ".join(lines).lower()
    for claim in ("will fail", "will break", "guaranteed", "definitely", "certain to"):
        assert claim not in body
    assert "advisory" in lines[0]
    assert lines[-1] == ADVISORY_LINE
    assert "never a block on approval" in ADVISORY_LINE


def test_secret_identity_is_rendered_without_any_value_field() -> None:
    edge = RelationshipEdge(
        subject=_POD,
        target=_SECRET,
        relation=RelationKind.USES_CONFIG,
        confidence=FactConfidence.DECLARED,
        evidence=EvidencePointer(resource=_POD, field="spec.volumes[0].secret.secretName"),
        resolution=EdgeResolution.RESOLVED,
    )
    lines = render_impact_lines(
        _summary(direct=(ImpactItem(resource=_POD, path=(edge,)),), target=_SECRET)
    )
    assert lines[1] == "  delete Secret/prod/db"
    assert (
        "    - Pod/prod/web-abc-1 via uses_config (declared) at"
        " spec.volumes[0].secret.secretName" in lines
    )
    assert not any("data" in line and "=" in line for line in lines)


def test_unavailable_lines_are_static_and_keep_approval_available() -> None:
    assert IMPACT_UNAVAILABLE_LINES == (
        IMPACT_TITLE,
        "  impact unavailable; approval remains available",
    )
