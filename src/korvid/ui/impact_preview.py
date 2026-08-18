"""Bounded, literal text for the advisory blast-radius section (issue #283).

Pure and Textual-free on purpose: `ImpactSummary` in, `tuple[str, ...]` out,
so the exact wording an approval dialog shows is testable without a Pilot and
cannot drift between the dialog and the tests.

Every heading is machine-defined here; the only cluster-derived text that
reaches a line is a resource identity, a relation/confidence/resolution enum
value, an evidence pointer (the resource an edge's evidence was read from,
together with its field path - an `EvidencePointer` names both, and a
selector-derived `managed_by`/`protected_by` edge's evidence resource is the
Deployment/PDB that declared the selector, not the Pod it matched), and a
namespace/coverage scope - each flattened of control characters and
length-capped, with the composed line capped again at `_MAX_LINE` because
one line concatenates several of them - reserving room for the ` [inferred]`
marker and marking every cut, so neither a capped fragment nor a capped line
ever reads as a complete claim.

Each hop's evidence resource and field are bounded on their own before the
line is composed - the per-fragment cap alone does not bound a path line,
because up to three rendered hops are concatenated onto it. A hop deep in a
long path can therefore still be the part visibly cut by the `_MAX_LINE`
cap even though neither of its fragments was near its own bound on its own.
This is an accepted trade-off, not a gap: the dialog is a 70-column modal,
so a line an approver cannot read at a glance is worse than one that
visibly says it was shortened, and the `... ` truncation mark makes that
plain wherever the cut fell. The ` [inferred]` marker's width is reserved
ahead of the line cap for the same reason, so it always survives - the one
piece of the line that must never be the part silently dropped.

Nothing is formatted as Rich markup: `ConfirmScreen` appends these lines to
a `rich.text.Text`, so a resource named `[bold red]web[/]` renders
literally.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from korvid.core.impact import ImpactAction, ImpactItem, ImpactSummary
from korvid.core.relationships import CoverageState, GraphResource, RelationshipEdge
from korvid.k8s.relationship_facts import FactConfidence

IMPACT_TITLE = "graph-derived impact (advisory):"

#: Rendered directly under the action line - ahead of every count, path,
#: unresolved reference and coverage row - because it frames all of them:
#: the summary describes known relationships, not a prediction, and it never
#: gates the approval the user asked for. Below a body that can run to the
#: preview's caps, the one line saying so is the one most likely scrolled
#: past.
ADVISORY_LINE = (
    "  advisory only: known relationships from one bounded snapshot - not a prediction of"
    " failure, no replacement for the server dry-run, and never a block on approval."
)

#: What the app renders when the snapshot could not be loaded at all (a
#: timeout or an unexpected failure). Static text: an exception message can
#: embed a response body, which must never reach the dialog.
IMPACT_UNAVAILABLE_LINES: tuple[str, ...] = (
    IMPACT_TITLE,
    "  impact unavailable; approval remains available",
)

_DIRECT_TITLE = "known direct dependents (may be affected)"
_TRANSITIVE_TITLE = "known transitive dependents (may be affected)"
_COVERAGE_COMPLETE_LINE = "  graph coverage: complete"
_COVERAGE_INCOMPLETE_LINE = (
    "  graph coverage: incomplete - a missing dependent here does not prove none exists"
)
_TRAVERSAL_CAPPED_LINE = (
    "  traversal capped: more dependents exist beyond the traversal limits and are not listed"
)
_SNAPSHOT_TRUNCATED_LINE = (
    "  snapshot truncated: the relationship snapshot hit a resource or an edge cap, so some"
    " resources or relationships were never joined"
)
_INFERRED_NOTE_LINE = "  inferred relationships are labelled and never block this write"
_TARGET_MISSING_LINE = "  target not found in this snapshot - dependents unknown"
_ALL_NAMESPACES_LABEL = "all namespaces"

_ACTION_LABEL = {
    ImpactAction.DELETE: "delete",
    ImpactAction.ROLLOUT_RESTART: "rollout restart",
    ImpactAction.SCALE_DOWN: "scale down",
    ImpactAction.POD_RESIZE: "pod resize",
    ImpactAction.CORDON_NODE: "cordon",
    ImpactAction.UNCORDON_NODE: "uncordon",
    ImpactAction.DRAIN_NODE: "drain",
}

#: Machine-defined limitation notes shown only for `ImpactAction.SCALE_DOWN`.
#: A controller-driven scale-down is not routed through the Eviction API, so
#: PodDisruptionBudgets never see it and cannot gate it - and an
#: autoscaler's own targeting/reconciliation loop is likewise outside what
#: this snapshot evaluates. Both are static facts about what the walk does
#: not check, not something derived from the cluster, so they render
#: unconditionally whenever the action is a scale-down.
_SCALE_DOWN_PDB_LINE = (
    "  controller scale-down is not an Eviction API request; PodDisruptionBudgets do not gate it"
)
_SCALE_DOWN_HPA_LINE = "  HorizontalPodAutoscaler targeting and reconciliation are not evaluated"
#: Only relevant when the target itself is an `apps/StatefulSet`: a
#: Deployment scale down has no PVC retention policy to leave unchecked,
#: and neither has a custom resource that merely spells its kind the same
#: way in a group of its own - `persistentVolumeClaimRetentionPolicy` is a
#: field of the `apps` API, so the group is part of what selects the line.
_SCALE_DOWN_STS_PVC_GROUP = "apps"
_SCALE_DOWN_STS_PVC_KIND = "StatefulSet"
_SCALE_DOWN_STS_PVC_LINE = "  StatefulSet PVC retention policy is not evaluated"

#: Hard output bounds: an approval dialog must stay reviewable, and a
#: pathological cluster must not be able to grow it.
_MAX_ITEM_LINES = 10
_MAX_UNRESOLVED_LINES = 5
_MAX_COVERAGE_LINES = 5
_MAX_PATH_HOPS = 3
_MAX_TEXT = 120
#: Reserved for the resource *name* inside `_MAX_TEXT`, whatever precedes
#: it. `group/kind/namespace` can exceed the fragment bound on its own (a
#: CRD group, a long kind, a long namespace), and a label that spends the
#: bound left to right renders a type with no object in it - the same text
#: for every object of that type. Half of this goes to the head and half to
#: the tail of the name, which is enough for both the workload prefix an
#: approver reads and the generated suffix (`-blue`, a ReplicaSet hash, a
#: pod suffix) that late-differing names are told apart by.
_MIN_NAME_BUDGET = 48
#: Total bound per composed line. `_MAX_TEXT` bounds each cluster-derived
#: *fragment*, but an item line concatenates an identity and up to three
#: hops; the dialog body is 70 columns wide, so a line that would wrap into
#: a screenful on its own is truncated here instead.
_MAX_LINE = 240
#: Shown in place of what either cap cut (`_MAX_TEXT` on one fragment,
#: `_MAX_LINE` on the composed line), so shortened text reads as shortened
#: rather than as a complete claim that happens to stop mid-name or
#: mid-path.
_TRUNCATION_SUFFIX = "..."

_ASCII_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: Unicode general categories that carry no glyph of their own but change
#: how everything around them renders, and that a cluster can therefore use
#: to make an approval line say something other than what it contains:
#: `Cc` (C0/C1 controls, including NEL), `Cf` (bidi overrides U+202A-202E,
#: directional isolates U+2066-2069, zero-width joiners and marks), `Cs`
#: (lone surrogates, which no terminal can encode), and `Zl`/`Zp` (the
#: line and paragraph separators - line breaks by another name).
#: Everything else, including non-Latin scripts and emoji, is ordinary text
#: and passes through untouched.
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def render_impact_lines(summary: ImpactSummary) -> tuple[str, ...]:
    """Render one `ImpactSummary` as bounded, literal preview lines."""
    capped = _counts_are_lower_bounds(summary)
    lines = [
        IMPACT_TITLE,
        f"  {_ACTION_LABEL[summary.action]} {_resource_label(summary.target)}",
    ]
    if not summary.target_present:
        # Directly under the action line: every count below is about the
        # snapshot, and without the target in it they say nothing about
        # the object the user is about to act on. It qualifies that
        # identity, so it stays adjacent to it and the advisory follows.
        lines.append(_TARGET_MISSING_LINE)
    # Before the sections, not after them: the hedge applies to every count
    # below it, and the body between here and the end can run to the
    # preview's caps.
    lines.append(ADVISORY_LINE)
    lines.extend(_action_note_lines(summary.action, summary.target.group, summary.target.kind))
    lines.extend(_section(_DIRECT_TITLE, summary.direct, capped=capped))
    lines.extend(_section(_TRANSITIVE_TITLE, summary.transitive, capped=capped))
    lines.extend(_inferred_lines(summary))
    lines.extend(_cycle_lines(summary, capped=capped))
    lines.extend(_revisit_lines(summary, capped=capped))
    lines.extend(_unresolved_lines(summary, capped=capped))
    lines.append(_scope_line(summary))
    lines.extend(_coverage_lines(summary))
    lines.extend(_cap_lines(summary))
    return tuple(_bounded(line) for line in lines)


def render_unavailable_lines(action: ImpactAction, group: str, kind: str) -> tuple[str, ...]:
    """Render the advisory for a snapshot that could not be produced.

    The generic advisory says only that the graph-derived part is missing
    and that approval is unaffected; a scale-down's limitation lines are
    not graph-derived at all. `_action_note_lines` states what a controller
    scale-down never routes through (the Eviction API, and so a
    PodDisruptionBudget), what this walk never evaluates (an HPA's own
    targeting and reconciliation), and - for an `apps/StatefulSet` only -
    the PVC retention policy that decides what happens to the removed
    replicas' claims. Every one of those is true whether or not a single object was
    read, so dropping them on a timeout or a loader failure would take
    correct information away from the approver exactly where korvid has
    least to offer.

    `group` and `kind` are the target's type and are only ever *compared*,
    never rendered, so the output stays entirely machine-defined - the
    reason the fail-open path exists is that cluster-derived text (an
    exception message carrying a response body) must not reach the dialog.
    The lines are capped exactly as the available rendering caps them, so
    both paths share one bound as well as one wording.

    Delete and rollout restart have no such static limitation, so for them
    this is `IMPACT_UNAVAILABLE_LINES` unchanged.
    """
    lines = [*IMPACT_UNAVAILABLE_LINES, *_action_note_lines(action, group, kind)]
    return tuple(_bounded(line) for line in lines)


def _bounded(line: str, *, marker: str = "") -> str:
    """Cap one composed line at `_MAX_LINE`, keeping `marker` and showing the cut.

    `marker` is machine-defined text whose meaning must survive the cap (the
    ` [inferred]` label): it is the last thing on an item line, so capping
    the composed line afterwards would drop exactly the word that says a hop
    was guessed and leave a heuristic chain reading like a declared one. Its
    width is reserved instead, and what was removed is marked, so a line that
    stops mid-path cannot be read as a complete one.
    """
    return _truncate(line, _MAX_LINE - len(marker)) + marker


def _truncate(text: str, limit: int) -> str:
    """Cut `text` to `limit`, marking the cut with `_TRUNCATION_SUFFIX`.

    The one place either cap drops text, so a cut always looks the same
    wherever it happened. Trailing dots are removed before the suffix is
    appended: a fragment inside this text may already have been marked, and
    `.....` reads as data rather than as one truncation mark.
    """
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATION_SUFFIX)].rstrip(".") + _TRUNCATION_SUFFIX


def _action_note_lines(action: ImpactAction, group: str, kind: str) -> list[str]:
    """Static limitations that only a scale-down leaves unchecked.

    Machine-defined, not cluster-derived: nothing here depends on what the
    snapshot found, only on the action and the target's type, so it renders
    identically for every scale-down of a given type - including the one
    whose snapshot never arrived (`render_unavailable_lines`).

    The type is the *pair*, never the kind alone: a kind is unique only
    within its group, and a CRD may name its own kind `StatefulSet`. Only
    `apps/StatefulSet` has a `persistentVolumeClaimRetentionPolicy`, so
    matching on the kind alone would state a policy the lookalike has never
    had - a claim about the cluster rather than a fact about the walk.
    Group and kind *select* a line, they never become one, so nothing
    cluster-controlled reaches the output.
    """
    if action is not ImpactAction.SCALE_DOWN:
        return []
    lines = [_SCALE_DOWN_PDB_LINE, _SCALE_DOWN_HPA_LINE]
    if (group, kind) == (_SCALE_DOWN_STS_PVC_GROUP, _SCALE_DOWN_STS_PVC_KIND):
        lines.append(_SCALE_DOWN_STS_PVC_LINE)
    return lines


def _section(title: str, items: Sequence[ImpactItem], *, capped: bool) -> list[str]:
    """One dependent section: an explicit count, bounded rows, an overflow
    note. "none in this snapshot" is information - distinct from a section
    that was omitted, and already scoped to the snapshot, so a cap has
    nothing to hedge there.

    The header count is a lower bound whenever the answer could not be
    exhaustive; the overflow note stays exact, because it counts what *this
    preview* cut from items the summary actually holds, not what the walk
    never found. It also names what it counted: unresolved references and
    coverage records overflow at the same indent with the same phrasing, and
    an unqualified `... N more not shown` between them would belong to
    whichever section the reader guesses.
    """
    if not items:
        return [f"  {title}: none in this snapshot"]
    lines = [f"  {title}: {_count_label(len(items), capped=capped)}"]
    lines.extend(_item_line(item) for item in items[:_MAX_ITEM_LINES])
    if len(items) > _MAX_ITEM_LINES:
        omitted = len(items) - _MAX_ITEM_LINES
        lines.append(f"    ... {omitted} more dependents not shown (preview capped)")
    return lines


def _item_line(item: ImpactItem) -> str:
    hops = " -> ".join(_hop(edge) for edge in item.path[:_MAX_PATH_HOPS])
    if len(item.path) > _MAX_PATH_HOPS:
        hops = f"{hops} -> ... {len(item.path) - _MAX_PATH_HOPS} more hops"
    marker = " [inferred]" if item.inferred else ""
    return _bounded(f"    - {_resource_label(item.resource)} via {hops}", marker=marker)


def _hop(edge: RelationshipEdge) -> str:
    """One traversal step: relation, confidence, and *where the evidence
    came from* - `EvidencePointer.resource: EvidencePointer.field`.

    A selector-derived `managed_by`/`protected_by` edge's evidence resource
    is the Deployment/PDB that declared `spec.selector`, not the Pod that
    matched it (`subject`, rendered separately by the caller); naming only
    the field left that identity out and made every selector-matched hop
    read as if the Pod's own `spec.selector` had been read.
    """
    return (
        f"{edge.relation.value} ({edge.confidence.value}) at "
        f"{_resource_label(edge.evidence.resource)}: {_safe(edge.evidence.field)}"
    )


def _inferred_lines(summary: ImpactSummary) -> list[str]:
    """Whether any edge anywhere in this summary was heuristically derived.

    Checked across every edge collection the summary carries - not just the
    listed dependent paths - because a cycle, a revisit, or an unresolved
    reference renders its own line regardless of confidence, and an inferred
    edge folded into one of those must still surface the same warning an
    inferred dependent path gets.
    """
    listed_inferred = any(item.inferred for item in (*summary.direct, *summary.transitive))
    aggregate_inferred = any(
        edge.confidence is FactConfidence.INFERRED
        for edge in (*summary.cycles, *summary.revisits, *summary.unresolved)
    )
    if listed_inferred or aggregate_inferred:
        return [_INFERRED_NOTE_LINE]
    return []


def _counts_are_lower_bounds(summary: ImpactSummary) -> bool:
    """Whether every cluster-derived count in this summary may be short.

    This is `ImpactSummary.incomplete` - deliberately the same predicate the
    summary already uses for "this answer cannot be read as exhaustive",
    because every way of being incomplete produces the same reading problem.
    A capped traversal stopped walking before it could reach every dependent,
    cycle or revisit. A truncated snapshot dropped input resources or
    candidate edges at the graph's own caps, so the walk was exhaustive only
    over a graph that was already missing parts. Incomplete coverage means a
    whole source was never listed - forbidden, absent, failed, partial or
    capped - so a dependent living there could not be reached either. And a
    target the snapshot never saw makes every count a statement about the
    snapshot rather than about the object. Any of them leaves an exact `N`
    reading as "this is all of it", which is exactly what none of these
    cases knows.

    A missing target hedges nothing in practice: with no target node the
    walk produces no items, so every section renders `none in this snapshot`
    - a statement, not a count - and no `0 or more` is invented.
    """
    return summary.incomplete


def _count_label(count: int, *, capped: bool) -> str:
    """Render a cluster-derived count, marked as a lower bound when capped.

    `capped` is `_counts_are_lower_bounds` - the whole-answer predicate,
    never one flag on its own: the caveat lines below the counts say *which*
    bound was hit, while the count itself only needs to say that it is a
    floor. "N or more" says so; the exact count would misread as exhaustive.
    """
    return f"{count} or more" if capped else str(count)


def _cycle_lines(summary: ImpactSummary, *, capped: bool) -> list[str]:
    if not summary.cycles:
        return []
    count = _count_label(len(summary.cycles), capped=capped)
    return [f"  relationship cycles: {count} (loop edges classified, not expanded)"]


def _revisit_lines(summary: ImpactSummary, *, capped: bool) -> list[str]:
    """Converging or parallel edges into an already-listed dependent.

    Counted, never expanded: each dependent is listed once with the first
    path that reached it, and this line says how many further known paths
    the summary folded away - so "1 dependent" cannot be misread as "only
    one relationship". When the answer could not be exhaustive, that count
    is a lower bound rather than the exact tally.
    """
    if not summary.revisits:
        return []
    count = _count_label(len(summary.revisits), capped=capped)
    return [f"  additional known paths: {count} (already-listed dependents reached again)"]


def _scope_line(summary: ImpactSummary) -> str:
    """The namespace this snapshot covered, always stated.

    `graph coverage: complete` means complete *within this scope*; a
    namespaced snapshot that never listed another namespace must not read
    as a cluster-wide answer.
    """
    scope = _ALL_NAMESPACES_LABEL if summary.scope is None else _safe(summary.scope)
    return f"  scope: {scope}"


def _unresolved_lines(summary: ImpactSummary, *, capped: bool) -> list[str]:
    """Dangling references held by the affected set.

    The set they are bounded by is the one the traversal produced, so this
    count inherits the same floor semantics as the dependent sections: an
    unreached dependent's dangling references were never in scope to count.
    """
    if not summary.unresolved:
        return []
    count = _count_label(len(summary.unresolved), capped=capped)
    lines = [f"  unresolved references in the affected set: {count}"]
    lines.extend(_unresolved_line(edge) for edge in summary.unresolved[:_MAX_UNRESOLVED_LINES])
    if len(summary.unresolved) > _MAX_UNRESOLVED_LINES:
        omitted = len(summary.unresolved) - _MAX_UNRESOLVED_LINES
        lines.append(f"    ... {omitted} more unresolved references not shown (preview capped)")
    return lines


def _unresolved_line(edge: RelationshipEdge) -> str:
    """One dangling reference, with its own confidence next to its relation.

    A cycle or a revisit only ever gets the aggregate `_INFERRED_NOTE_LINE`
    because those are counted, never individually listed; an unresolved
    reference *is* individually listed, so its confidence goes right after
    the relation - matching `_hop`'s `relation (confidence) at resource:
    field` grammar - rather than folding an inferred one into that same
    generic note with no way to tell which listed reference was heuristic.
    The evidence resource is named for the same reason `_hop` names it: it
    can differ from both `subject` and `target`.
    """
    return (
        f"    - {_resource_label(edge.subject)} {edge.relation.value}"
        f" ({edge.confidence.value}) -> {_resource_label(edge.target)}"
        f" ({edge.resolution.value}) at {_resource_label(edge.evidence.resource)}:"
        f" {_safe(edge.evidence.field)}"
    )


def _coverage_lines(summary: ImpactSummary) -> list[str]:
    incomplete = [
        record for record in summary.coverage if record.state is not CoverageState.COMPLETE
    ]
    if not incomplete:
        return [_COVERAGE_COMPLETE_LINE]
    lines = [_COVERAGE_INCOMPLETE_LINE]
    for record in incomplete[:_MAX_COVERAGE_LINES]:
        scope = f" @{_safe(record.scope)}" if record.scope else ""
        target = f"{_safe(record.group or 'core')}/{_safe(record.resource)}"
        lines.append(f"    - {target}{scope}: {record.state.value}")
    if len(incomplete) > _MAX_COVERAGE_LINES:
        omitted = len(incomplete) - _MAX_COVERAGE_LINES
        lines.append(f"    ... {omitted} more coverage records not shown (preview capped)")
    return lines


def _cap_lines(summary: ImpactSummary) -> list[str]:
    lines: list[str] = []
    if summary.traversal_capped:
        lines.append(_TRAVERSAL_CAPPED_LINE)
    if summary.graph_truncated:
        lines.append(_SNAPSHOT_TRUNCATED_LINE)
    return lines


def _resource_label(resource: GraphResource) -> str:
    """`group/kind/namespace/name`, blank parts dropped (the graph screen's
    own convention), flattened and bounded by an explicit budget.

    A label under `_MAX_TEXT` is returned verbatim; only an over-long one is
    reshaped, so ordinary identities render byte for byte as before.

    Over the bound, the parts are not equal. `group/kind/namespace` says
    *what kind of thing* is affected; `name` says *which one*, and it is the
    only part an approver can match against the object they asked to write
    to. Spending the bound left to right - which is what capping the joined
    string does - drops the name first, so a long-group CRD renders one
    identical line for every object of that type. The name is given
    `_MIN_NAME_BUDGET` characters no matter what precedes it (more when the
    qualifier is short, never more than it needs), the qualifier gets the
    rest, and both are cut in the middle rather than at the end, so a head
    and a tail of each survive with the removal marked in between.
    """
    qualifier = "/".join(
        _flatten(part) for part in (resource.group, resource.kind, resource.namespace) if part
    )
    name = _flatten(resource.name)
    label = "/".join(part for part in (qualifier, name) if part)
    if len(label) <= _MAX_TEXT:
        return label
    if not name:  # a malformed reference: nothing to protect, bound the rest
        return _elide(qualifier, _MAX_TEXT)
    if not qualifier:
        return _elide(name, _MAX_TEXT)
    name_budget = min(len(name), max(_MIN_NAME_BUDGET, _MAX_TEXT - 1 - len(qualifier)))
    return f"{_elide(qualifier, _MAX_TEXT - 1 - name_budget)}/{_elide(name, name_budget)}"


def _elide(text: str, limit: int) -> str:
    """Cut the *middle* of `text` to `limit`, marking the cut.

    The counterpart to `_truncate` for text whose end identifies it as much
    as its start: a generated resource name differing only in its suffix
    survives a middle cut and is lost entirely to an end cut. The split is
    deterministic - the head takes the odd character - so the same input
    always renders the same label. Dots adjacent to the cut are removed for
    the reason `_truncate` removes them: `.....` reads as data rather than
    as one mark.
    """
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_SUFFIX):
        return _TRUNCATION_SUFFIX[: max(limit, 0)]
    available = limit - len(_TRUNCATION_SUFFIX)
    head_length = available - available // 2
    tail = text[len(text) - available // 2 :].lstrip(".") if available // 2 else ""
    return f"{text[:head_length].rstrip('.')}{_TRUNCATION_SUFFIX}{tail}"


def _flatten(text: str) -> str:
    """Replace every non-rendering control/format character with a space.

    Category-based rather than a codepoint range: C0 and DEL are only the
    ASCII part of the problem. A `Cf` character such as U+202E
    (RIGHT-TO-LEFT OVERRIDE) or U+2066 (LEFT-TO-RIGHT ISOLATE) reverses the
    visual order of everything after it, so an unflattened name could make
    an approval dialog display an identity - or an evidence path - that is
    not the one the write targets.

    One space per character, never a deletion: the replacement is
    length-preserving, so a hidden character shows up as a gap instead of
    silently collapsing two different identities into one that looks
    identical.
    """
    if text.isascii():
        return _ASCII_CONTROL_CHARS.sub(" ", text)
    return "".join(
        " " if unicodedata.category(char) in _CONTROL_CATEGORIES else char for char in text
    )


def _safe(text: str) -> str:
    """Flatten control characters (including newlines/tabs, C1, and the bidi
    and zero-width format characters) and cap length, so cluster-controlled
    text can neither break or reorder the dialog layout nor grow the preview
    unboundedly.

    A cut is marked with `_TRUNCATION_SUFFIX` for the same reason the
    composed-line cap marks its own: a shortened resource identity or
    evidence path that stops silently reads as the whole name or the whole
    field path, and two long identities sharing a prefix would render as one
    apparently complete - and apparently identical - claim.
    """
    return _truncate(_flatten(text), _MAX_TEXT)
