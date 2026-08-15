"""Bounded, literal text for the advisory blast-radius section (issue #283).

Pure and Textual-free on purpose: `ImpactSummary` in, `tuple[str, ...]` out,
so the exact wording an approval dialog shows is testable without a Pilot and
cannot drift between the dialog and the tests.

Every heading is machine-defined here; the only cluster-derived text that
reaches a line is a resource identity, a relation/confidence/resolution enum
value, an evidence field path, and a namespace/coverage scope - each
flattened of control characters and length-capped, with the composed line
capped again at `_MAX_LINE` because one line concatenates several of them -
reserving room for the ` [inferred]` marker and marking every cut, so
neither a capped fragment nor a capped line ever reads as a complete claim.
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

#: Always the last line: the summary describes known relationships, not a
#: prediction, and it never gates the approval the user asked for.
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
    "  snapshot truncated: the relationship snapshot hit an input cap, so some resources"
    " were never joined"
)
_INFERRED_NOTE_LINE = "  inferred relationships are labelled and never block this write"
_TARGET_MISSING_LINE = "  target not found in this snapshot - dependents unknown"
_ALL_NAMESPACES_LABEL = "all namespaces"

_ACTION_LABEL = {
    ImpactAction.DELETE: "delete",
    ImpactAction.ROLLOUT_RESTART: "rollout restart",
}

#: Hard output bounds: an approval dialog must stay reviewable, and a
#: pathological cluster must not be able to grow it.
_MAX_ITEM_LINES = 10
_MAX_UNRESOLVED_LINES = 5
_MAX_COVERAGE_LINES = 5
_MAX_PATH_HOPS = 3
_MAX_TEXT = 120
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
    lines = [
        IMPACT_TITLE,
        f"  {_ACTION_LABEL[summary.action]} {_resource_label(summary.target)}",
    ]
    if not summary.target_present:
        # Directly under the action line: every count below is about the
        # snapshot, and without the target in it they say nothing about
        # the object the user is about to act on.
        lines.append(_TARGET_MISSING_LINE)
    lines.extend(_section(_DIRECT_TITLE, summary.direct))
    lines.extend(_section(_TRANSITIVE_TITLE, summary.transitive))
    lines.extend(_inferred_lines(summary))
    lines.extend(_cycle_lines(summary))
    lines.extend(_revisit_lines(summary))
    lines.extend(_unresolved_lines(summary))
    lines.append(_scope_line(summary))
    lines.extend(_coverage_lines(summary))
    lines.extend(_cap_lines(summary))
    lines.append(ADVISORY_LINE)
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


def _section(title: str, items: Sequence[ImpactItem]) -> list[str]:
    """One dependent section: an explicit count, bounded rows, an overflow
    note. "none in this snapshot" is information - distinct from a section
    that was omitted."""
    if not items:
        return [f"  {title}: none in this snapshot"]
    lines = [f"  {title}: {len(items)}"]
    lines.extend(_item_line(item) for item in items[:_MAX_ITEM_LINES])
    if len(items) > _MAX_ITEM_LINES:
        lines.append(f"    ... {len(items) - _MAX_ITEM_LINES} more not shown (preview capped)")
    return lines


def _item_line(item: ImpactItem) -> str:
    hops = " -> ".join(_hop(edge) for edge in item.path[:_MAX_PATH_HOPS])
    if len(item.path) > _MAX_PATH_HOPS:
        hops = f"{hops} -> ... {len(item.path) - _MAX_PATH_HOPS} more hops"
    marker = " [inferred]" if item.inferred else ""
    return _bounded(f"    - {_resource_label(item.resource)} via {hops}", marker=marker)


def _hop(edge: RelationshipEdge) -> str:
    return f"{edge.relation.value} ({edge.confidence.value}) at {_safe(edge.evidence.field)}"


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


def _count_label(count: int, *, capped: bool) -> str:
    """Render a cluster-derived count, marked as a lower bound when capped.

    A capped traversal stops classifying edges once it hits its limits, so
    any count folded out of that walk - a cycle, a revisit - may be an
    undercount, not the true total. "N or more" says so; the exact count
    would misread as exhaustive.
    """
    return f"{count} or more" if capped else str(count)


def _cycle_lines(summary: ImpactSummary) -> list[str]:
    if not summary.cycles:
        return []
    count = _count_label(len(summary.cycles), capped=summary.traversal_capped)
    return [f"  relationship cycles: {count} (loop edges classified, not expanded)"]


def _revisit_lines(summary: ImpactSummary) -> list[str]:
    """Converging or parallel edges into an already-listed dependent.

    Counted, never expanded: each dependent is listed once with the first
    path that reached it, and this line says how many further known paths
    the summary folded away - so "1 dependent" cannot be misread as "only
    one relationship". When traversal was capped, that count is a lower
    bound rather than the exact tally - the walk may have stopped before
    finding every revisit.
    """
    if not summary.revisits:
        return []
    count = _count_label(len(summary.revisits), capped=summary.traversal_capped)
    return [f"  additional known paths: {count} (already-listed dependents reached again)"]


def _scope_line(summary: ImpactSummary) -> str:
    """The namespace this snapshot covered, always stated.

    `graph coverage: complete` means complete *within this scope*; a
    namespaced snapshot that never listed another namespace must not read
    as a cluster-wide answer.
    """
    scope = _ALL_NAMESPACES_LABEL if summary.scope is None else _safe(summary.scope)
    return f"  scope: {scope}"


def _unresolved_lines(summary: ImpactSummary) -> list[str]:
    if not summary.unresolved:
        return []
    lines = [f"  unresolved references in the affected set: {len(summary.unresolved)}"]
    lines.extend(_unresolved_line(edge) for edge in summary.unresolved[:_MAX_UNRESOLVED_LINES])
    if len(summary.unresolved) > _MAX_UNRESOLVED_LINES:
        omitted = len(summary.unresolved) - _MAX_UNRESOLVED_LINES
        lines.append(f"    ... {omitted} more not shown (preview capped)")
    return lines


def _unresolved_line(edge: RelationshipEdge) -> str:
    """One dangling reference, with its own confidence next to its relation.

    A cycle or a revisit only ever gets the aggregate `_INFERRED_NOTE_LINE`
    because those are counted, never individually listed; an unresolved
    reference *is* individually listed, so its confidence goes right after
    the relation - matching `_hop`'s `relation (confidence) at field`
    grammar - rather than folding an inferred one into that same generic
    note with no way to tell which listed reference was heuristic.
    """
    return (
        f"    - {_resource_label(edge.subject)} {edge.relation.value}"
        f" ({edge.confidence.value}) -> {_resource_label(edge.target)}"
        f" ({edge.resolution.value}) at {_safe(edge.evidence.field)}"
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
    own convention), flattened and capped."""
    parts = (resource.group, resource.kind, resource.namespace, resource.name)
    return _safe("/".join(part for part in parts if part))


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
