"""Deterministic, advisory blast-radius summaries (issue #283).

`summarize_impact` is a pure function over one immutable `RelationshipGraph`
snapshot and one proposed destructive action. It answers a deliberately
bounded question - "which resources korvid already observed depend on this
one, through relationships whose action semantics are explicitly tested?" -
and reports everything it could not answer: a target the snapshot never
saw, unresolved references inside the affected set, relationship loops, the
namespace scope the snapshot covered, that snapshot's coverage, and both
traversal caps.

Direction is fixed by `korvid.core.relationships`: an edge is always
dependent -> dependency, so this traversal walks edges *backwards* (into
`edge.target`) and every resource it reaches is a known dependent of the
action's target.

This is deliberately a *second* traversal rather than an extension of
`RelationshipGraph.walk_dependents`, which stays the graph view's own
unfiltered walk. Four requirements here are specific to a write dialog and
would distort that shared API if pushed into it:

- edges are filtered by the closed per-action relation set *before* the
  walk, so no action can claim a relationship whose semantics are untested;
- every dependent carries the full edge path that reached it, not just the
  tree edge, so a dialog can show the chain it is claiming;
- unresolved references are filtered by the affected set the walk produced,
  and by whichever relation policy the action chose - every action picks one
  explicitly, there is no default, and the pick is only ever
  "relation-blind" or "the very set this action walks" (see
  `ACTION_UNRESOLVED_RELATIONS`);
- the caps (`ImpactLimits`) are an order of magnitude smaller than the
  graph view's and must stay independent of them - an approval dialog is
  not a graph screen.

What the two walks must agree on is pinned by
`tests/core/test_impact.py::test_cycle_and_revisit_classification_matches_the_graph_walk`:
on edges the action filter leaves untouched, a genuine loop is a cycle and a
converging or parallel repeat is a revisit, in both.

The result is advisory. It never claims a dependent will fail, never blocks
a write, and cannot approve, execute, or reserve one: this module holds no
cluster client, imports nothing from `korvid.ui`, and returns immutable
values only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import StrEnum

from korvid.core.relationships import (
    CoverageRecord,
    CoverageState,
    EdgeResolution,
    GraphResource,
    RelationshipEdge,
    RelationshipGraph,
)
from korvid.k8s.relationship_facts import FactConfidence, RelationKind


class ImpactAction(StrEnum):
    """The destructive actions with explicit, tested relationship semantics.

    Closed on purpose: an action without a tested semantic mapping gets no
    impact claim at all rather than a plausible-looking guess.
    """

    DELETE = "delete"
    ROLLOUT_RESTART = "rollout_restart"
    SCALE_DOWN = "scale_down"
    POD_RESIZE = "pod_resize"
    CORDON_NODE = "cordon_node"
    UNCORDON_NODE = "uncordon_node"
    DRAIN_NODE = "drain_node"


#: Relations a delete may follow in reverse (target -> its dependents).
#:
#: `SELECTS` is deliberately absent. A Service selecting many Pods does not
#: fail because one selected Pod is deleted, so following it would make
#: every Pod delete read as catastrophic. Every relation listed here has a
#: dedicated test in `tests/core/test_impact.py`.
_DELETE_RELATIONS: frozenset[RelationKind] = frozenset(
    {
        RelationKind.OWNED_BY,
        RelationKind.MANAGED_BY,
        RelationKind.ROUTES_TO,
        RelationKind.USES_VOLUME,
        RelationKind.USES_CONFIG,
        RelationKind.PROTECTED_BY,
        RelationKind.SCHEDULED_ON,
        RelationKind.BOUND_TO,
    }
)

#: A rolling restart replaces pods of the restarted workload; only the
#: ownership/management chain below it is affected. Nothing else - a mounted
#: ConfigMap, a routing backend, a PDB - is destroyed or detached by it.
_ROLLOUT_RESTART_RELATIONS: frozenset[RelationKind] = frozenset(
    {RelationKind.OWNED_BY, RelationKind.MANAGED_BY}
)

#: Scaling a workload down replaces or removes some of its Pods, so the
#: ownership/management chain below it is affected the same way a rollout
#: restart's is. `ROUTES_TO` is also followed here, same as for delete
#: (see `_DELETE_RELATIONS` above), so a routing backend still shows up in
#: the impact path. `SELECTS` is the scale-down-specific addition - unlike
#: delete, which deliberately omits it (a Service selecting many Pods should
#: not read as catastrophic just because one selected Pod is deleted),
#: scaling down is the one action where a selector pointing at a shrinking
#: replica set is a conservative, known dependent worth listing for the
#: reader to check. Together `SELECTS` and `ROUTES_TO` compose the full
#: managed_by -> selects -> routes_to scale path. This still never asserts
#: that the Service loses an endpoint or that traffic actually fails; it
#: only says the relationship exists and was observed. PDB, volume, config,
#: node, and binding relations are excluded because scaling down does not
#: detach a mounted volume or ConfigMap, evict a Pod past its PDB, move a
#: Pod off its node, or unbind a claim - those relations describe what a
#: *remaining* Pod still holds, not something the scale-down itself changes.
#: For the same reason a scale-down's unresolved-reference warning is
#: filtered by this set as well - `ACTION_UNRESOLVED_RELATIONS` maps
#: `SCALE_DOWN` to *this very frozenset*, deliberately and by reference. The
#: two policies are not independently authored sets that happen to agree
#: today: a relation this action refuses to walk is a relation it refuses to
#: claim anything about, warning included, so the walk's closed set and the
#: warning's closed set must be the same object. Editing this set therefore
#: moves both halves of the action's semantics at once, which is the point -
#: adding a relation here that the warning kept out (or dropping one the
#: warning kept in) is exactly the divergence the coupling makes
#: unexpressible.
_SCALE_DOWN_RELATIONS: frozenset[RelationKind] = frozenset(
    {
        RelationKind.OWNED_BY,
        RelationKind.MANAGED_BY,
        RelationKind.SELECTS,
        RelationKind.ROUTES_TO,
    }
)

#: Resizing a Pod keeps the Pod object, its membership, its references, and
#: its placement intact, so every one of the nine relation kinds is excluded.
#: That closed set is intentionally empty: the action carries no graph impact
#: semantics yet, so later integration can thread the enum through without
#: accidentally claiming a dependent set.
_POD_RESIZE_RELATIONS: frozenset[RelationKind] = frozenset()

#: Cordoning a node only marks it unschedulable; it does not affect existing
#: Pod placements or dependencies. This action carries no graph impact
#: semantics - the set is deliberately empty.
_NODE_SCHEDULING_TOGGLE_RELATIONS: frozenset[RelationKind] = frozenset()

#: Draining a node evicts Pods that are scheduled on it. We follow only the
#: SCHEDULED_ON relationship to find those Pods. PROTECTED_BY is deliberately
#: absent because the current dependent walk cannot reach a PDB from a Node
#: through that edge direction and DrainPlan owns blocker state.
_DRAIN_NODE_RELATIONS: frozenset[RelationKind] = frozenset({RelationKind.SCHEDULED_ON})

ACTION_RELATIONS: Mapping[ImpactAction, frozenset[RelationKind]] = {
    ImpactAction.DELETE: _DELETE_RELATIONS,
    ImpactAction.ROLLOUT_RESTART: _ROLLOUT_RESTART_RELATIONS,
    ImpactAction.SCALE_DOWN: _SCALE_DOWN_RELATIONS,
    ImpactAction.POD_RESIZE: _POD_RESIZE_RELATIONS,
    ImpactAction.CORDON_NODE: _NODE_SCHEDULING_TOGGLE_RELATIONS,
    ImpactAction.UNCORDON_NODE: _NODE_SCHEDULING_TOGGLE_RELATIONS,
    ImpactAction.DRAIN_NODE: _DRAIN_NODE_RELATIONS,
}

#: Which relations each action's unresolved-reference warning may report,
#: always additionally bounded by the affected set the walk produced.
#:
#: `None` means "any relation"; a frozenset means "only these". Keyed by
#: *every* `ImpactAction` on purpose: an action added to `ACTION_RELATIONS`
#: and forgotten here raises `KeyError` in `_unresolved_edges` instead of
#: inheriting a permissive default, so choosing an unresolved policy is as
#: mandatory as choosing a relation set. `_unresolved_edges` documents why
#: each action has the policy it has, and
#: `tests/core/test_impact.py::test_every_action_chooses_its_unresolved_reference_policy`
#: pins the exhaustiveness.
#:
#: The choice this mapping exists to record is *relation-blind `None` versus
#: the action's own walked set* - not "which relations shall the warning
#: list". So `SCALE_DOWN` reuses `_SCALE_DOWN_RELATIONS` by reference rather
#: than restating its four members: a second literal would be a second place
#: to edit and would let the walk and the warning drift into claiming
#: different things about the same action. Delete and rollout restart pick
#: the other option, `None`, which is a real decision and not an omission -
#: see `_unresolved_edges`.
ACTION_UNRESOLVED_RELATIONS: Mapping[ImpactAction, frozenset[RelationKind] | None] = {
    ImpactAction.DELETE: None,
    ImpactAction.ROLLOUT_RESTART: None,
    ImpactAction.SCALE_DOWN: _SCALE_DOWN_RELATIONS,
    ImpactAction.POD_RESIZE: _POD_RESIZE_RELATIONS,
    ImpactAction.CORDON_NODE: _NODE_SCHEDULING_TOGGLE_RELATIONS,
    ImpactAction.UNCORDON_NODE: _NODE_SCHEDULING_TOGGLE_RELATIONS,
    ImpactAction.DRAIN_NODE: _DRAIN_NODE_RELATIONS,
}


@dataclass(frozen=True, slots=True)
class ImpactLimits:
    """Traversal caps. Smaller than the graph screen's own caps on purpose:
    an approval dialog must stay readable in one screenful."""

    max_depth: int = 3
    max_nodes: int = 50


@dataclass(frozen=True, slots=True)
class ImpactItem:
    """One known dependent plus the deterministic path that reached it."""

    resource: GraphResource
    path: tuple[RelationshipEdge, ...]

    @property
    def inferred(self) -> bool:
        """True when any hop was derived by a heuristic rather than read.

        Labelling exists so an inferred hop can be shown and discounted by
        the reader; it never blocks a write.
        """
        return any(edge.confidence is FactConfidence.INFERRED for edge in self.path)


@dataclass(frozen=True, slots=True)
class ImpactSummary:
    """One immutable, advisory blast-radius answer.

    `target_present` is False when the exact identity the action targets
    (UID included) is not a node in this snapshot - the object was replaced
    since the row was watched, or was never listed. `scope` is the
    namespace the snapshot covered, `None` meaning every namespace; it is
    the caller's value, recorded verbatim, so a namespaced snapshot can
    never be rendered as a cluster-wide answer.
    """

    action: ImpactAction
    target: GraphResource
    target_present: bool
    scope: str | None
    direct: tuple[ImpactItem, ...]
    transitive: tuple[ImpactItem, ...]
    cycles: tuple[RelationshipEdge, ...]
    revisits: tuple[RelationshipEdge, ...]
    unresolved: tuple[RelationshipEdge, ...]
    coverage: tuple[CoverageRecord, ...]
    traversal_capped: bool
    graph_truncated: bool

    @property
    def incomplete(self) -> bool:
        """True when this answer cannot be read as exhaustive.

        A target the snapshot never saw, incomplete coverage, a truncated
        snapshot, or a traversal cap all mean the same thing for a reader:
        the absence of a dependent here is not proof that none exists.
        """
        return (
            not self.target_present
            or self.traversal_capped
            or self.graph_truncated
            or any(record.state is not CoverageState.COMPLETE for record in self.coverage)
        )


#: Frozen singleton so the keyword default below is a name lookup, not a
#: call (ruff B008), while still behaving as `limits=ImpactLimits()`.
_DEFAULT_LIMITS = ImpactLimits()

#: `(items, cycles, revisits, capped)` - one traversal's whole answer.
_WalkResult = tuple[
    tuple[ImpactItem, ...], tuple[RelationshipEdge, ...], tuple[RelationshipEdge, ...], bool
]


def summarize_impact(
    graph: RelationshipGraph,
    action: ImpactAction,
    target: GraphResource,
    *,
    scope: str | None = None,
    limits: ImpactLimits = _DEFAULT_LIMITS,
) -> ImpactSummary:
    """Summarize which observed resources depend on `target` for `action`.

    Args:
        graph: One immutable relationship snapshot.
        action: The proposed destructive action.
        target: The exact resource identity (group/kind/namespace/name/uid)
            the action will run against.
        scope: The namespace the snapshot covered, or None when it covered
            every namespace. Recorded, never inferred: only the caller
            knows what it asked the loader for.
        limits: Traversal caps; both are reported when reached.

    Returns:
        An immutable `ImpactSummary`. Only resolved edges whose relation has
        tested semantics for `action` are traversed; membership of `target`
        in `graph.nodes` is checked by full identity, so a same-named
        replacement is reported as a missing target rather than summarized
        as if it were the object on screen. `unresolved` follows the policy
        `ACTION_UNRESOLVED_RELATIONS` records for `action` - every relation
        for a delete or a rollout restart, the action's own closed set for a
        scale-down - always bounded by the affected set.
    """
    relations = ACTION_RELATIONS[action]
    index = _dependents_index(graph.edges, relations)
    items, cycles, revisits, capped = _walk(index, target, limits)
    affected = {target, *(item.resource for item in items)}
    unresolved = _unresolved_edges(graph.edges, action, affected)
    return ImpactSummary(
        action=action,
        target=target,
        target_present=target in graph.nodes,
        scope=scope,
        direct=tuple(item for item in items if len(item.path) == 1),
        transitive=tuple(item for item in items if len(item.path) > 1),
        cycles=cycles,
        revisits=revisits,
        unresolved=unresolved,
        coverage=graph.coverage,
        traversal_capped=capped,
        graph_truncated=graph.truncated,
    )


def _unresolved_edges(
    edges: Sequence[RelationshipEdge],
    action: ImpactAction,
    affected: AbstractSet[GraphResource],
) -> tuple[RelationshipEdge, ...]:
    """The dangling references this action's advisory may warn about.

    The policy is read from `ACTION_UNRESOLVED_RELATIONS`, which is keyed by
    every `ImpactAction`: there is no default and no membership fallback, so
    an action that never chose one raises `KeyError` here rather than
    quietly inheriting the more permissive half. The choice is binary -
    relation-blind `None`, or the action's *own* closed set, the same
    frozenset its walk is filtered by - and never a third, separately
    authored list of relations. Both policies are additionally bounded by
    the affected set.

    - `DELETE` and `ROLLOUT_RESTART` map to `None` - warn about a dangling
      reference of *any* relation. A delete removes the object those
      references were resolved against, and a restart recreates the Pod that
      has to satisfy them again, so a mounted ConfigMap that no longer
      exists is a real reason the action may not land the way the reader
      expects (the restarted Pod will not come back). Narrowing those to the
      walk's own relations would drop exactly the warning that matters.
    - `SCALE_DOWN` maps to its own closed set (`_SCALE_DOWN_RELATIONS`, by
      reference - not a copy of its members). Scaling down does not detach a
      mounted volume or ConfigMap, evict a Pod past its PDB, move a Pod off
      its node, or unbind a claim - which is why those relations are
      excluded from the walk in the first place. A *dangling* one of them
      says nothing more about the scale-down than a resolved one would, so
      warning about it would reintroduce, as a warning, precisely the claim
      the closed set refuses to make. Sharing the frozenset is what makes
      that guarantee structural: the set an action may walk and the set it
      may warn about cannot be edited apart.

    Keeping the choice in a mapping rather than at the call site means an
    action added to `ACTION_RELATIONS` also has to decide this, and both
    halves of its semantics stay in one module.
    """
    relations = ACTION_UNRESOLVED_RELATIONS[action]
    return tuple(
        edge
        for edge in edges
        if edge.resolution is not EdgeResolution.RESOLVED
        and edge.subject in affected
        and (relations is None or edge.relation in relations)
    )


def _dependents_index(
    edges: Sequence[RelationshipEdge], relations: frozenset[RelationKind]
) -> dict[GraphResource, list[RelationshipEdge]]:
    """Group in-scope resolved edges by the resource they depend on.

    Indexed once per summary, so one traversal costs a single pass over the
    graph's edges no matter how many resources it reaches. The graph's own
    deterministic edge order is preserved inside each bucket.
    """
    index: dict[GraphResource, list[RelationshipEdge]] = {}
    for edge in edges:
        if edge.resolution is not EdgeResolution.RESOLVED or edge.relation not in relations:
            continue
        index.setdefault(edge.target, []).append(edge)
    return index


def _walk(
    index: Mapping[GraphResource, Sequence[RelationshipEdge]],
    target: GraphResource,
    limits: ImpactLimits,
) -> _WalkResult:
    """Breadth-first walk of dependents, excluding `target` itself.

    Each resource is reached once, by the first path breadth-first order
    offers, so the reported path is deterministic. An edge into an
    already-reached resource is classified rather than traversed: a cycle
    when the dependent is an ancestor of the resource it depends on along
    the path taken to get there, and otherwise a revisit - a converging or
    parallel repeat that is a real relationship but adds no new impacted
    resource. This mirrors `RelationshipGraph.walk_dependents`'s
    classification exactly; only the filtering, the retained paths, and the
    caps differ (see the module docstring).
    """
    paths: dict[GraphResource, tuple[RelationshipEdge, ...]] = {target: ()}
    items: list[ImpactItem] = []
    cycles: list[RelationshipEdge] = []
    revisits: list[RelationshipEdge] = []
    frontier = [target]
    depth = 0
    capped = False

    while frontier and depth < limits.max_depth and not capped:
        depth += 1
        next_frontier: list[GraphResource] = []
        for current in frontier:
            for edge in index.get(current, ()):
                if edge.subject in paths:
                    bucket = cycles if _is_ancestor(edge.subject, current, paths) else revisits
                    bucket.append(edge)
                    continue
                if len(items) >= limits.max_nodes:
                    capped = True
                    break
                path = (*paths[current], edge)
                paths[edge.subject] = path
                items.append(ImpactItem(resource=edge.subject, path=path))
                next_frontier.append(edge.subject)
            if capped:
                break
        frontier = next_frontier

    if frontier and not capped:
        depth_cycles, depth_revisits, capped = _classify_depth_frontier(index, frontier, paths)
        cycles.extend(depth_cycles)
        revisits.extend(depth_revisits)
    return tuple(items), tuple(cycles), tuple(revisits), capped


def _classify_depth_frontier(
    index: Mapping[GraphResource, Sequence[RelationshipEdge]],
    frontier: Sequence[GraphResource],
    paths: Mapping[GraphResource, tuple[RelationshipEdge, ...]],
) -> tuple[tuple[RelationshipEdge, ...], tuple[RelationshipEdge, ...], bool]:
    """Classify what sits just past the depth cap without traversing it.

    A loop or a second path back into an already-reached resource is a
    cycle or a revisit, not hidden work; only a genuinely unreached
    dependent means the cap truncated a real answer.
    """
    cycles: list[RelationshipEdge] = []
    revisits: list[RelationshipEdge] = []
    capped = False
    for current in frontier:
        for edge in index.get(current, ()):
            if edge.subject not in paths:
                capped = True
                continue
            bucket = cycles if _is_ancestor(edge.subject, current, paths) else revisits
            bucket.append(edge)
    return tuple(cycles), tuple(revisits), capped


def _is_ancestor(
    candidate: GraphResource,
    node: GraphResource,
    paths: Mapping[GraphResource, tuple[RelationshipEdge, ...]],
) -> bool:
    """True when `candidate` lies on the path that reached `node`.

    `node` itself counts: a resource depending on itself is a genuine
    one-step loop. Costs at most the depth cap, never the graph's size.
    """
    path = paths[node]
    if not path:
        return candidate == node  # `node` is the traversal root
    if candidate == path[0].target:
        return True
    return any(edge.subject == candidate for edge in path)
