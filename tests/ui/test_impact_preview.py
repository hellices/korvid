"""Advisory blast-radius text for write approval dialogs (issue #283).

The renderer is pure and Textual-free: `ImpactSummary` in, bounded literal
lines out. These tests pin the exact line grammar (so nothing unexpected can
ever leak into an approval dialog), both bounds (per cluster-derived
fragment and per composed line), the scope/target-presence statements, and
the advisory wording.
"""

from __future__ import annotations

import unicodedata

import pytest

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
    _COVERAGE_INCOMPLETE_LINE,
    _MAX_COVERAGE_LINES,
    _MAX_LINE,
    _MAX_TEXT,
    _MIN_NAME_BUDGET,
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
_MISSING_CONFIG = RelationshipEdge(
    subject=_POD,
    target=GraphResource(group="", kind="ConfigMap", namespace="prod", name="gone"),
    relation=RelationKind.USES_CONFIG,
    confidence=FactConfidence.DECLARED,
    evidence=EvidencePointer(resource=_POD, field="spec.volumes[0].configMap"),
    resolution=EdgeResolution.MISSING,
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
    dialog.

    The forbidden coverage record here is the reason both counts read as
    lower bounds: a source that could not be listed may hold dependents the
    walk never saw, exactly like a cap does.
    """
    summary = _summary(
        direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
        transitive=(ImpactItem(resource=_POD, path=(_OWNS_DEPLOY, _OWNS_RS)),),
        coverage=(_FORBIDDEN_SECRETS,),
    )
    assert render_impact_lines(summary) == (
        "graph-derived impact (advisory):",
        "  delete apps/Deployment/prod/web",
        ADVISORY_LINE,
        "  known direct dependents (may be affected): 1 or more",
        "    - apps/ReplicaSet/prod/web-abc via owned_by (declared) at metadata.ownerReferences[0]",
        "  known transitive dependents (may be affected): 1 or more",
        "    - Pod/prod/web-abc-1 via owned_by (declared) at metadata.ownerReferences[0]"
        " -> owned_by (declared) at metadata.ownerReferences[0]",
        "  scope: prod",
        "  graph coverage: incomplete - a missing dependent here does not prove none exists",
        "    - core/secrets @prod: forbidden",
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
    assert lines[2] == ADVISORY_LINE


def test_caps_and_cycles_are_reported_as_their_own_lines() -> None:
    """When traversal is capped, the cycle count is a lower bound: the walk
    stopped classifying edges before it could confirm there were no more, so
    "1" here would misread as exhaustive when it is only what was seen
    before the cap."""
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            cycles=(_OWNS_DEPLOY,),
            traversal_capped=True,
            graph_truncated=True,
        )
    )
    assert "  relationship cycles: 1 or more (loop edges classified, not expanded)" in lines
    assert (
        "  traversal capped: more dependents exist beyond the traversal limits"
        " and are not listed" in lines
    )
    assert (
        "  snapshot truncated: the relationship snapshot hit a resource or an edge cap,"
        " so some resources or relationships were never joined" in lines
    )


def test_cycle_count_is_exact_when_traversal_is_not_capped() -> None:
    """Without a cap, the walk classified every reachable edge, so the exact
    count stands - "or more" would understate confidence the traversal
    actually has."""
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            cycles=(_OWNS_DEPLOY,),
            traversal_capped=False,
        )
    )
    assert "  relationship cycles: 1 (loop edges classified, not expanded)" in lines


def test_revisit_count_is_a_lower_bound_when_traversal_is_capped() -> None:
    """Same reasoning as the capped cycle count: a capped walk may have
    stopped before finding every converging or parallel edge, so "1" would
    misread as the complete tally of folded-away paths."""
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            revisits=(_OWNS_RS,),
            traversal_capped=True,
        )
    )
    assert "  additional known paths: 1 or more (already-listed dependents reached again)" in lines


def test_dependent_and_unresolved_counts_are_lower_bounds_when_traversal_is_capped() -> None:
    """A capped walk stopped before it could reach every dependent, so the
    listed sections and the affected-set-bounded unresolved tally are floors,
    not totals: an exact `1` there reads as "this is all of it"."""
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            transitive=(ImpactItem(resource=_POD, path=(_OWNS_DEPLOY, _OWNS_RS)),),
            unresolved=(_MISSING_CONFIG,),
            traversal_capped=True,
        )
    )
    assert "  known direct dependents (may be affected): 1 or more" in lines
    assert "  known transitive dependents (may be affected): 1 or more" in lines
    assert "  unresolved references in the affected set: 1 or more" in lines


def test_every_count_is_a_lower_bound_when_the_snapshot_was_truncated() -> None:
    """A truncated snapshot dropped input resources or candidate edges before
    the walk began, so the walk was complete only over an incomplete graph:
    every cluster-derived count - dependents, cycles, revisits, and the
    affected-set unresolved tally - can be short by whatever the snapshot
    dropped, even though the traversal itself never hit a cap."""
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            transitive=(ImpactItem(resource=_POD, path=(_OWNS_DEPLOY, _OWNS_RS)),),
            cycles=(_OWNS_DEPLOY,),
            revisits=(_OWNS_RS,),
            unresolved=(_MISSING_CONFIG,),
            traversal_capped=False,
            graph_truncated=True,
        )
    )
    assert "  known direct dependents (may be affected): 1 or more" in lines
    assert "  known transitive dependents (may be affected): 1 or more" in lines
    assert "  relationship cycles: 1 or more (loop edges classified, not expanded)" in lines
    assert "  additional known paths: 1 or more (already-listed dependents reached again)" in lines
    assert "  unresolved references in the affected set: 1 or more" in lines


@pytest.mark.parametrize(
    "state",
    [
        CoverageState.PARTIAL,
        CoverageState.FORBIDDEN,
        CoverageState.UNAVAILABLE,
        CoverageState.FAILED,
        CoverageState.CAPPED,
    ],
)
def test_every_count_is_a_lower_bound_when_coverage_is_incomplete(state: CoverageState) -> None:
    """Incomplete coverage bounds the answer exactly like a cap does.

    A source that was forbidden, absent, failed, partially listed or capped
    was never joined into the snapshot at all, so a dependent living there
    could not be walked to - whatever `traversal_capped` and
    `graph_truncated` say. `graph coverage: incomplete` already says the
    absence of a dependent proves nothing; an exact `1` next to it would
    contradict that line by reading as the complete tally.
    """
    record = CoverageRecord(group="", resource="secrets", scope="prod", state=state)
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            transitive=(ImpactItem(resource=_POD, path=(_OWNS_DEPLOY, _OWNS_RS)),),
            cycles=(_OWNS_DEPLOY,),
            revisits=(_OWNS_RS,),
            unresolved=(_MISSING_CONFIG,),
            coverage=(record,),
            traversal_capped=False,
            graph_truncated=False,
        )
    )
    assert "  known direct dependents (may be affected): 1 or more" in lines
    assert "  known transitive dependents (may be affected): 1 or more" in lines
    assert "  relationship cycles: 1 or more (loop edges classified, not expanded)" in lines
    assert "  additional known paths: 1 or more (already-listed dependents reached again)" in lines
    assert "  unresolved references in the affected set: 1 or more" in lines
    assert _COVERAGE_INCOMPLETE_LINE in lines


def test_every_count_is_exact_when_nothing_was_capped_or_truncated() -> None:
    """The other half of the contract: with a complete walk over a complete
    snapshot - the target present, every coverage record `complete`, neither
    bound hit - the counts *are* the totals, and hedging them would
    understate what the summary actually knows."""
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            transitive=(ImpactItem(resource=_POD, path=(_OWNS_DEPLOY, _OWNS_RS)),),
            cycles=(_OWNS_DEPLOY,),
            revisits=(_OWNS_RS,),
            unresolved=(_MISSING_CONFIG,),
            coverage=(
                CoverageRecord(
                    group="apps", resource="replicasets", scope="prod", state=CoverageState.COMPLETE
                ),
            ),
            traversal_capped=False,
            graph_truncated=False,
        )
    )
    assert "  known direct dependents (may be affected): 1" in lines
    assert "  known transitive dependents (may be affected): 1" in lines
    assert "  relationship cycles: 1 (loop edges classified, not expanded)" in lines
    assert "  additional known paths: 1 (already-listed dependents reached again)" in lines
    assert "  unresolved references in the affected set: 1" in lines
    assert "  graph coverage: complete" in lines
    assert not any("or more" in line for line in lines)


def test_a_capped_section_still_reports_its_preview_overflow_exactly() -> None:
    """The header count hedges what the *traversal* may have missed; the
    `more not shown` line counts what *this preview* cut from items it
    actually holds, which is exact - hedging it would suggest the renderer
    dropped an unknown number of rows it had in hand."""
    items = tuple(
        ImpactItem(
            resource=GraphResource(group="", kind="Pod", namespace="prod", name=f"web-{index}"),
            path=(_OWNS_DEPLOY,),
        )
        for index in range(12)
    )
    lines = render_impact_lines(_summary(direct=items, traversal_capped=True))
    assert "  known direct dependents (may be affected): 12 or more" in lines
    assert "    ... 2 more not shown (preview capped)" in lines


def test_empty_sections_stay_none_in_this_snapshot_even_when_capped() -> None:
    """ "none in this snapshot" is already scoped to the snapshot and is not a
    count, so a cap has nothing to hedge: the caveat lines below say what was
    bounded, and `0 or more` would be noise."""
    lines = render_impact_lines(_summary(traversal_capped=True, graph_truncated=True))
    assert "  known direct dependents (may be affected): none in this snapshot" in lines
    assert "  known transitive dependents (may be affected): none in this snapshot" in lines
    assert not any("or more" in line for line in lines)


def test_revisited_paths_are_counted_never_expanded() -> None:
    """A dependent reached twice is one item plus a count, not two items:
    "2 dependents" when there is one would overstate the blast radius. Not
    capped here, so the count is exact rather than a lower bound."""
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            revisits=(_OWNS_RS,),
            traversal_capped=False,
        )
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
    statement about the snapshot, not about the object being deleted.

    A missing target makes the whole answer incomplete, but every section is
    empty in that case, so nothing is hedged: `none in this snapshot` stays
    as it is, and no `0 or more` is invented for a section that has no count
    at all.
    """
    lines = render_impact_lines(_summary(target_present=False))
    assert lines[2] == "  target not found in this snapshot - dependents unknown"
    # The missing-target line qualifies the identity on the action line, so
    # it stays adjacent to it; the advisory follows, still ahead of every
    # section below.
    assert lines[3] == ADVISORY_LINE
    assert "  known direct dependents (may be affected): none in this snapshot" in lines
    assert "  known transitive dependents (may be affected): none in this snapshot" in lines
    assert not any("or more" in line for line in lines)
    present = render_impact_lines(_summary())
    assert not any(line.startswith("  target not found") for line in present)


def test_coverage_records_are_bounded_with_one_exact_overflow_line() -> None:
    """More incomplete sources than the preview lists must not grow the
    dialog: the listed records stop at `_MAX_COVERAGE_LINES` and exactly one
    overflow line states how many were omitted.

    That count is exact, like every other `preview capped` line: it counts
    what this renderer cut from records the summary actually holds, not what
    the snapshot never saw.
    """
    omitted = 4
    records = tuple(
        CoverageRecord(
            group="apps" if index % 2 else "",
            resource=f"widgets-{index}",
            scope="prod",
            state=CoverageState.FORBIDDEN,
        )
        for index in range(_MAX_COVERAGE_LINES + omitted)
    )
    lines = render_impact_lines(_summary(coverage=records))
    listed = [line for line in lines if line.startswith("    - ") and "widgets-" in line]
    overflow = [line for line in lines if "more coverage records not shown" in line]
    assert _COVERAGE_INCOMPLETE_LINE in lines
    assert len(listed) == _MAX_COVERAGE_LINES
    assert listed[0] == "    - core/widgets-0 @prod: forbidden"
    assert listed[1] == "    - apps/widgets-1 @prod: forbidden"
    assert overflow == [f"    ... {omitted} more coverage records not shown (preview capped)"]
    assert max(len(line) for line in lines) <= _MAX_LINE
    assert lines[2] == ADVISORY_LINE


def test_a_single_incomplete_coverage_record_needs_no_overflow_line() -> None:
    """The boundary: exactly `_MAX_COVERAGE_LINES` records are all listed and
    nothing claims anything was cut."""
    records = tuple(
        CoverageRecord(group="", resource=f"widgets-{index}", scope="", state=CoverageState.PARTIAL)
        for index in range(_MAX_COVERAGE_LINES)
    )
    lines = render_impact_lines(_summary(coverage=records))
    assert sum(1 for line in lines if "widgets-" in line) == _MAX_COVERAGE_LINES
    assert "    - core/widgets-0: partial" in lines  # no scope, no `@` suffix
    assert not any("coverage records not shown" in line for line in lines)


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
    assert lines[2] == ADVISORY_LINE


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
        "    - Pod/prod/web-abc-1 uses_config (declared) -> ConfigMap/prod/gone-0 (missing)"
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


def test_an_inferred_cycle_edge_still_triggers_the_inferred_note_with_no_inferred_items() -> None:
    """A cycle line never carries an `[inferred]` marker of its own, but an
    inferred edge folded into `cycles` still makes an inferred hop part of
    this summary - the note must fire even when every listed dependent path
    is declared."""
    inferred_cycle_edge = RelationshipEdge(
        subject=_POD,
        target=_DEPLOY,
        relation=RelationKind.MANAGED_BY,
        confidence=FactConfidence.INFERRED,
        evidence=EvidencePointer(resource=_POD, field="spec.selector"),
        resolution=EdgeResolution.RESOLVED,
    )
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            cycles=(inferred_cycle_edge,),
        )
    )
    assert not any("[inferred]" in line for line in lines if line.startswith("    - "))
    assert "  inferred relationships are labelled and never block this write" in lines
    assert max(len(line) for line in lines) <= _MAX_LINE


def test_an_inferred_revisit_edge_still_triggers_the_inferred_note_with_no_inferred_items() -> None:
    """Same guarantee for a revisit: the dependent it points at was already
    listed via a declared path, so no item line ever carries the marker,
    but the revisited edge itself is inferred and must still surface the
    warning."""
    inferred_revisit_edge = RelationshipEdge(
        subject=_POD,
        target=_RS,
        relation=RelationKind.MANAGED_BY,
        confidence=FactConfidence.INFERRED,
        evidence=EvidencePointer(resource=_POD, field="spec.selector"),
        resolution=EdgeResolution.RESOLVED,
    )
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            revisits=(inferred_revisit_edge,),
        )
    )
    assert not any("[inferred]" in line for line in lines if line.startswith("    - "))
    assert "  inferred relationships are labelled and never block this write" in lines
    assert max(len(line) for line in lines) <= _MAX_LINE


def test_an_inferred_unresolved_edge_is_individually_identifiable_by_its_confidence() -> None:
    """An unresolved reference is rendered by its own line grammar, which
    never includes the `[inferred]` marker, but its confidence - unlike a
    cycle's or a revisit's - is shown right on that line, so an inferred
    unresolved edge is identifiable on its own, not only through the
    aggregate note below."""
    inferred_unresolved_edge = RelationshipEdge(
        subject=_POD,
        target=GraphResource(group="", kind="ConfigMap", namespace="prod", name="gone"),
        relation=RelationKind.USES_CONFIG,
        confidence=FactConfidence.INFERRED,
        evidence=EvidencePointer(resource=_POD, field="spec.volumes[0].configMap"),
        resolution=EdgeResolution.MISSING,
    )
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            unresolved=(inferred_unresolved_edge,),
        )
    )
    assert not any("[inferred]" in line for line in lines if line.startswith("    - "))
    assert (
        "    - Pod/prod/web-abc-1 uses_config (inferred) -> ConfigMap/prod/gone (missing)"
        " at spec.volumes[0].configMap" in lines
    )
    assert "  inferred relationships are labelled and never block this write" in lines
    assert max(len(line) for line in lines) <= _MAX_LINE


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


def _owned_by(subject: GraphResource, field: str) -> RelationshipEdge:
    """One declared `owned_by` hop from `subject`, with `field` as evidence."""
    return RelationshipEdge(
        subject=subject,
        target=_DEPLOY,
        relation=RelationKind.OWNED_BY,
        confidence=FactConfidence.DECLARED,
        evidence=EvidencePointer(resource=subject, field=field),
        resolution=EdgeResolution.RESOLVED,
    )


def _identities(lines: tuple[str, ...], prefix: str) -> list[str]:
    """The identity fragment of every item line starting with `prefix`."""
    return [line[len("    - ") :].split(" via ")[0] for line in lines if line.startswith(prefix)]


def _dependent_lines(*resources: GraphResource) -> tuple[str, ...]:
    """Render one direct-dependent item line per resource, one declared hop each."""
    return render_impact_lines(
        _summary(
            direct=tuple(
                ImpactItem(
                    resource=resource,
                    path=(_owned_by(resource, "metadata.ownerReferences[0]"),),
                )
                for resource in resources
            )
        )
    )


def test_two_long_identities_sharing_a_prefix_stay_distinguishable_when_truncated() -> None:
    """The per-fragment `_MAX_TEXT` cap must *say* it cut, and keep the tail.

    Two dependents whose legal DNS names share a long prefix render as two
    identities bounded at the same width. Without a visible marker both
    lines read as complete identities, and an approver comparing them cannot
    tell a full name from a silently shortened one - the dangerous reading,
    since the rest of the name is exactly what distinguishes the two
    objects. A prefix-only cut is worse still: it drops precisely the
    generated suffix (`-blue` / `-green`, a ReplicaSet hash, a pod
    suffix) that names differing late are told apart by. The budget keeps a
    head *and* a tail of the name and marks what it removed in between.
    """
    shared = "checkout-api-" + "a" * 80 + "-" + "x" * 150
    blue = GraphResource(group="", kind="Pod", namespace="prod", name=f"{shared}-blue")
    green = GraphResource(group="", kind="Pod", namespace="prod", name=f"{shared}-green")
    lines = _dependent_lines(blue, green)
    identities = _identities(lines, "    - Pod/prod/checkout-api-")
    assert len(identities) == 2
    for identity, resource in zip(identities, (blue, green), strict=True):
        assert len(identity) <= _MAX_TEXT
        assert _TRUNCATION_SUFFIX in identity
        assert _TRUNCATION_SUFFIX * 2 not in identity
        full = f"Pod/prod/{resource.name}"
        head, tail = identity.split(_TRUNCATION_SUFFIX, 1)
        assert full.startswith(head)
        assert full.endswith(tail)
        assert full not in identity  # the middle the budget dropped is really gone
    assert identities[0] != identities[1]
    assert identities[0].endswith("-blue")
    assert identities[1].endswith("-green")
    # A short identity is never marked: the marker means "cut", nothing else.
    assert "  delete apps/Deployment/prod/web" in lines


def test_a_long_group_kind_and_namespace_never_push_the_name_out_of_the_label() -> None:
    """The name is the last thing a bounded identity may lose.

    A CRD can carry a long group, a long kind and a long namespace; joined
    ahead of the name they can exceed `_MAX_TEXT` on their own, so a
    prefix-only cut renders `<group>/<kind>/<names...` - an approval line
    naming a *type* and no object at all, identical for every object of that
    type. `_MIN_NAME_BUDGET` is reserved for the name regardless of what
    precedes it, and spent on a head and a tail, so two objects differing
    near the start and two differing near the end all stay distinct.
    """
    group = "platform-workloads-" + "g" * 70 + ".internal.example.com"
    kind = "CheckoutServiceDeploymentBinding" + "K" * 40
    namespace = "team-" + "n" * 70 + "-shard-7"
    assert len(f"{group}/{kind}/{namespace}") > _MAX_TEXT
    base = "checkout-api-" + "c" * 80
    names = (f"{base}-blue", f"{base}-green", f"blue-{base}", f"green-{base}")
    resources = tuple(
        GraphResource(group=group, kind=kind, namespace=namespace, name=name) for name in names
    )
    lines = _dependent_lines(*resources)
    identities = _identities(lines, "    - platform-workloads-")
    assert len(identities) == 4
    assert len(set(identities)) == 4  # differing near the start *or* near the end
    for identity, name in zip(identities, names, strict=True):
        assert len(identity) <= _MAX_TEXT
        assert _TRUNCATION_SUFFIX in identity
        assert identity.startswith("platform-workloads-")  # the qualifier head survives
        assert "-shard-7/" in identity  # ...and so does the namespace tail
        keeps = identity.split("-shard-7/", 1)[1]
        assert len(keeps.replace(_TRUNCATION_SUFFIX, "")) >= _MIN_NAME_BUDGET - 8
        assert keeps.startswith(name[:10])
        assert keeps.endswith(name[-10:])
    assert max(len(line) for line in lines) <= _MAX_LINE


def test_a_difference_the_budget_cannot_keep_is_still_marked_as_shortened() -> None:
    """The residual limit, stated: no bounded label can distinguish every
    pair of 250-character names. Two names differing only in the middle
    render identically - but both carry the marker, so neither line reads as
    a complete identity, and the preview never claims an exactness it does
    not have."""
    head, tail = "checkout-api-" + "a" * 90, "x" * 150
    blue = GraphResource(group="", kind="Pod", namespace="prod", name=f"{head}-blue-{tail}")
    green = GraphResource(group="", kind="Pod", namespace="prod", name=f"{head}-green-{tail}")
    identities = _identities(_dependent_lines(blue, green), "    - Pod/prod/checkout-api-")
    assert len(identities) == 2
    for identity in identities:
        assert _TRUNCATION_SUFFIX in identity
        assert len(identity) <= _MAX_TEXT


def test_a_label_with_blank_parts_drops_them_and_still_holds_the_bound() -> None:
    """`GraphResource` does not validate its fields, so an unresolved
    reference can carry a blank namespace, a blank kind, or - malformed -
    a blank name. Blank parts are dropped, never rendered as an empty
    segment or a trailing slash, and the bound still holds when the name is
    the only part there is."""
    nameless = GraphResource(group="", kind="ConfigMap", namespace="prod", name="")
    bare = GraphResource(group="", kind="", namespace="", name="n" * 400)
    long_kindless = GraphResource(group="", kind="", namespace="", name="a" * 60 + "-tail")
    identities = _identities(_dependent_lines(nameless, bare, long_kindless), "    - ")
    assert identities[0] == "ConfigMap/prod"
    assert identities[2] == "a" * 60 + "-tail"
    assert len(identities[1]) == _MAX_TEXT
    assert identities[1] == "n" * 59 + _TRUNCATION_SUFFIX + "n" * 58
    assert not any(identity.endswith("/") or "//" in identity for identity in identities)


def test_labels_within_the_bound_are_rendered_byte_for_byte() -> None:
    """Nothing about the budget may touch an ordinary label. Every label up
    to and including `_MAX_TEXT` characters renders exactly as
    `group/kind/namespace/name` with blank parts dropped - no marker, no
    reshaping, no lost character."""
    name = "b" * (_MAX_TEXT - len("apps/Deployment/prod/"))
    exact = GraphResource(group="apps", kind="Deployment", namespace="prod", name=name)
    assert len(f"apps/Deployment/prod/{name}") == _MAX_TEXT
    cluster_scoped = GraphResource(
        group="rbac.authorization.k8s.io", kind="ClusterRole", namespace="", name="view"
    )
    lines = _dependent_lines(exact, _POD, cluster_scoped)
    assert lines[1] == "  delete apps/Deployment/prod/web"
    identities = _identities(lines, "    - ")
    assert identities == [
        f"apps/Deployment/prod/{name}",
        "Pod/prod/web-abc-1",
        "rbac.authorization.k8s.io/ClusterRole/view",
    ]
    assert not any(_TRUNCATION_SUFFIX in identity for identity in identities)


def test_a_bounded_label_stays_flat_and_literal_for_unicode_and_control_input() -> None:
    """The budget runs on flattened text, so a hostile name cannot buy width
    with characters that render as nothing. Control, bidi and zero-width
    characters are still one space each (length-preserving, so two names
    cannot collapse into one), ordinary Unicode still survives, no markup is
    interpreted, and the bound holds on both."""
    hostile_name = "[bold red]checkout\u202e" + "\u200b" * 60 + "rogue\u0085" * 20 + "\u2066tail[/]"
    hostile = GraphResource(group="", kind="Pod", namespace="prod", name=hostile_name)
    unicode_name = "配置-café-🚀-" + "書" * 200 + "-末尾"
    friendly = GraphResource(group="", kind="ConfigMap", namespace="prod", name=unicode_name)
    lines = _dependent_lines(hostile, friendly)
    identities = _identities(lines, "    - ")
    assert len(identities) == 2
    for identity in identities:
        assert len(identity) <= _MAX_TEXT
        assert _TRUNCATION_SUFFIX in identity
        assert not any(
            unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in identity
        )
    assert identities[0].startswith("Pod/prod/[bold red]checkout")
    assert identities[0].endswith("tail[/]")
    assert identities[1].startswith("ConfigMap/prod/配置-café-🚀")
    assert identities[1].endswith("-末尾")
    assert max(len(line) for line in lines) <= _MAX_LINE


def test_a_long_evidence_path_is_truncated_visibly_below_the_line_bound() -> None:
    """The same marker on the other cluster-derived fragment.

    A single hop off a short identity composes well inside `_MAX_LINE`, so
    the composed-line cap cannot fire here: whatever marks this cut is the
    per-fragment cap doing it. An evidence path that stops mid-field without
    a marker reads as the whole path - a claim about *where* a relationship
    was found, pointing at a field that is not the one that was read.
    """
    field = "spec.template.spec.initContainers[0].env[7].valueFrom.secretKeyRef." + "n" * 120
    lines = render_impact_lines(
        _summary(direct=(ImpactItem(resource=_POD, path=(_owned_by(_POD, field),)),))
    )
    item = next(line for line in lines if line.startswith("    - Pod/prod/web-abc-1 via"))
    assert len(item) < _MAX_LINE
    rendered = item.split(" at ", 1)[1]
    assert len(rendered) <= _MAX_TEXT
    assert rendered.endswith(_TRUNCATION_SUFFIX)
    assert not rendered.endswith(_TRUNCATION_SUFFIX * 2)
    kept = rendered[: -len(_TRUNCATION_SUFFIX)]
    assert field.startswith(kept)
    assert kept.startswith("spec.template.spec.initContainers[0].env[7].valueFrom.secretKeyRef")
    assert field not in item


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


def test_c1_bidi_and_zero_width_controls_are_flattened_ordinary_unicode_is_not() -> None:
    """C0 is not the whole control surface. C1 (U+0085 NEL), the bidi
    overrides (U+202E) and the directional isolates (U+2066..U+2069) all
    reorder or break a rendered line, and a zero-width character (U+200B)
    can hide the difference between two identities. Ordinary Unicode - a
    non-Latin name, an emoji - is text, and must survive untouched."""
    hostile = GraphResource(
        group="",
        kind="ConfigMap",
        namespace="prod",
        name="app\u202econfig\u2066rogue\u2069\u200b\u0085x",
    )
    edge = RelationshipEdge(
        subject=_POD,
        target=hostile,
        relation=RelationKind.USES_CONFIG,
        confidence=FactConfidence.DECLARED,
        evidence=EvidencePointer(resource=_POD, field="spec.volumes[0]\u009c.configMap"),
        resolution=EdgeResolution.MISSING,
    )
    lines = render_impact_lines(_summary(unresolved=(edge,)))
    body = "\n".join(lines)
    assert not any(
        unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for line in lines for ch in line
    )
    assert "ConfigMap/prod/app config rogue   x" in body
    assert "spec.volumes[0] .configMap" in body

    friendly = GraphResource(group="", kind="ConfigMap", namespace="prod", name="配置-café-🚀")
    ok_edge = RelationshipEdge(
        subject=_POD,
        target=friendly,
        relation=RelationKind.USES_CONFIG,
        confidence=FactConfidence.DECLARED,
        evidence=EvidencePointer(resource=_POD, field="spec.volumes[0].configMap"),
        resolution=EdgeResolution.MISSING,
    )
    assert "ConfigMap/prod/配置-café-🚀" in "\n".join(
        render_impact_lines(_summary(unresolved=(ok_edge,)))
    )


def test_no_line_claims_a_guaranteed_failure_and_the_advisory_leads_the_body() -> None:
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
    assert lines[2] == ADVISORY_LINE
    assert "never a block on approval" in ADVISORY_LINE


def test_the_advisory_appears_once_directly_under_the_action_line() -> None:
    """The hedge frames the counts, so it has to be read before them.

    Last-line placement put it under every dependent, unresolved-reference
    and coverage row - up to the preview's caps, a screenful the reader
    scrolls past - so the one line saying "this is not a prediction" was the
    one most likely never seen. Directly under the action line it is on
    screen with the target itself, before the first count.

    Exactly one occurrence, whatever the summary holds: a repeated advisory
    would read as two different claims about two different parts of the
    body.
    """
    long_summary = _summary(
        direct=tuple(
            ImpactItem(
                resource=GraphResource(
                    group="apps", kind="ReplicaSet", namespace="prod", name=f"web-{index}"
                ),
                path=(_OWNS_DEPLOY,),
            )
            for index in range(20)
        ),
        coverage=(_FORBIDDEN_SECRETS,),
        traversal_capped=True,
    )
    for lines in (render_impact_lines(_summary()), render_impact_lines(long_summary)):
        assert lines.count(ADVISORY_LINE) == 1
        assert lines[:3] == (IMPACT_TITLE, "  delete apps/Deployment/prod/web", ADVISORY_LINE)
        sections = [
            index for index, line in enumerate(lines) if "dependents (may be affected)" in line
        ]
        assert sections
        assert lines.index(ADVISORY_LINE) < min(sections)


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
