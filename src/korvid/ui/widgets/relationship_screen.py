"""Keyboard-navigable operational relationship graph view (issue #281, Task 6).

`RelationshipScreen` renders one root `GraphResource`'s direct
`dependencies_of`/`dependents_of` from an already-built, immutable
`RelationshipGraph` (Task 4, loaded per Task 5) as two labelled `DataTable`
sections, plus a coverage-completeness banner. It performs no I/O and holds
no state beyond the toggled view (bounded dependent expansion, coverage
detail) — the caller owns building/loading the graph and re-opening this
screen with a fresh one when the underlying data changes (Task 7).

Navigation never parses rendered display strings: each navigable row's
target `GraphResource` is kept in a `RowKey`-keyed mapping populated at
render time, and Enter resolves through that mapping. Resource names and
evidence field paths are cluster/user-controlled — they are always wrapped
in a markup-disabled `rich.text.Text` before becoming a table cell, so a
literal `[style]...[/]` sequence in an object's name can never be
misinterpreted as Rich markup.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from korvid.core.relationships import (
    CoverageRecord,
    CoverageState,
    EdgeResolution,
    GraphResource,
    RelationshipEdge,
    RelationshipGraph,
)

#: DIRECTION | RELATION | RESOURCE | CONFIDENCE | STATE | EVIDENCE
_COLUMNS = ("DIRECTION", "RELATION", "RESOURCE", "CONFIDENCE", "STATE", "EVIDENCE")

#: Control characters (including DEL) flattened out of a rendered coverage
#: scope, mirroring what `CoverageRecord` already does to `detail`, so the
#: banner can never be broken across lines by a pathological namespace.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: `("goto", group, kind, namespace, name)` — the dismissed navigation target.
GotoResult = tuple[str, str, str, str, str]

_IDLE_STATUS = "Enter: navigate a resolved row · d: expand dependents · c: coverage detail"

#: Shown when the root itself is not among the snapshot's nodes (a stale
#: UID after delete/recreate, or a resource dropped at a source cap). Its
#: empty sections then describe the snapshot, not the cluster, and must not
#: read as "this resource has no relationships".
_ABSENT_ROOT_NOTE = (
    "This resource is not present in this snapshot (deleted, recreated with a "
    "new uid, or dropped at a source cap) - empty rows below say nothing about "
    "its real relationships."
)


def _resource_label(resource: GraphResource) -> str:
    """A readable "group/kind/namespace/name" label, blank parts dropped.

    Cluster-scoped resources (blank namespace) and resources recorded with
    no discovered API group render without an empty segment.
    """
    parts = (resource.group, resource.kind, resource.namespace, resource.name)
    return "/".join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class _RowTarget:
    """What Enter on one data row resolves to."""

    resource: GraphResource
    resolvable: bool
    reason: str


class _RowBudget:
    """A single, shared render-row counter capped at `graph.limits.max_nodes`.

    One counter is threaded through direct dependencies, direct
    dependents, expansion depths, and cycle rows so no individual category
    can exceed the graph's own node cap on its own — in particular,
    `RelationshipGraph.walk_dependents` does not bound its `cycles` list,
    so a resource with many redundant back-edges must still be capped here.
    """

    def __init__(self, capacity: int) -> None:
        self._remaining = capacity

    def take(self) -> bool:
        """Consume one row of budget; `False` once capacity is exhausted."""
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True


def _capped_reasons(
    *,
    direct_omitted: int,
    depth_omitted: int,
    cycle_omitted: int,
    truncated: bool,
    max_nodes: int,
) -> str:
    """Human-readable summary of every reason the Dependents view was capped."""
    parts: list[str] = []
    if direct_omitted:
        parts.append(f"{direct_omitted} direct dependent row(s) omitted")
    if depth_omitted:
        parts.append(f"{depth_omitted} deeper dependent row(s) omitted")
    if cycle_omitted:
        parts.append(f"{cycle_omitted} cycle row(s) omitted")
    if truncated:
        parts.append(f"traversal capped at {max_nodes} node(s)")
    return "; ".join(parts) if parts else f"capped at {max_nodes} node(s)"


class RelationshipScreen(ModalScreen[GotoResult | None]):
    """Adjacency view of one resource's direct dependencies/dependents.

    Dismisses with `("goto", group, kind, namespace, name)` when Enter is
    pressed on a resolved, navigable row, or `None` on Escape. A dependency
    row navigates to its (possibly unresolved) target; a dependent row
    navigates to the dependent resource itself — always resolvable, since a
    dependent is by construction a live node already in the graph.
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=True),
        Binding("q", "close", "Close", show=False),
        Binding("d", "toggle_expand", "Expand", show=True),
        Binding("c", "toggle_coverage", "Coverage", show=True),
    ]

    DEFAULT_CSS = """
    RelationshipScreen {
        layout: vertical;
        background: $background;
    }
    RelationshipScreen #relationship-title {
        padding: 0 1;
        text-style: bold;
    }
    RelationshipScreen #relationship-coverage {
        padding: 0 1;
        color: $warning;
    }
    RelationshipScreen #relationship-status {
        padding: 0 1;
        color: $text-muted;
    }
    RelationshipScreen DataTable {
        height: 1fr;
    }
    """

    def __init__(self, graph: RelationshipGraph, root: GraphResource) -> None:
        super().__init__()
        self.graph = graph
        self.root = root
        self._expanded = False
        self._coverage_detailed = False
        self._targets: dict[str, _RowTarget] = {}
        # Node index for `_dependent_resolvable`: a dependent row navigates
        # `edge.subject`, whose resolvability is unrelated to
        # `edge.resolution` (which only ever describes `edge.target`).
        self._known_nodes = frozenset(graph.nodes)

    def compose(self) -> ComposeResult:
        yield Footer()
        yield Static(
            f"Relationships: {_resource_label(self.root)}", id="relationship-title", markup=False
        )
        yield Static("", id="relationship-coverage", markup=False)
        yield Static(_IDLE_STATUS, id="relationship-status", markup=False)
        yield DataTable[str | Text](id="relationship-table")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*_COLUMNS)
        self._render_coverage()
        self._render_table()
        table.focus()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _coverage_line(self, record: CoverageRecord) -> str:
        """One non-`complete` coverage record as a single literal line.

        The concise form (`core/services: forbidden`) is what the banner
        always showed. Detail mode adds the record's `scope` when it has
        one, because the cross-namespace `routes_to` follow-ups list the
        same GVR in several namespaces — without the scope, two denied
        namespaces render as two identical, unactionable lines. Scope is
        flattened the way `CoverageRecord` already flattens `detail`
        (control characters, including newlines, become spaces) so it
        cannot break the single-line banner; the widget renders with
        `markup=False`, so a namespace containing markup stays literal.
        """
        target = f"{record.group or 'core'}/{record.resource}"
        if self._coverage_detailed and record.scope:
            target = f"{target} @{_CONTROL_CHARS.sub(' ', record.scope)}"
        line = f"{target}: {record.state.value}"
        if self._coverage_detailed and record.detail:
            line = f"{line} — {record.detail}"
        return line

    def _render_coverage(self) -> None:
        lines = ["Coverage: incomplete" if self.graph.incomplete else "Coverage: complete"]
        if self.root not in self._known_nodes:
            # Independent of coverage: a snapshot can be perfectly complete
            # and still not contain this exact root identity.
            lines.append(_ABSENT_ROOT_NOTE)
        for record in self.graph.coverage:
            if record.state is CoverageState.COMPLETE:
                continue
            lines.append(self._coverage_line(record))
        self.query_one("#relationship-coverage", Static).update("\n".join(lines))

    def _render_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self._targets = {}
        # One shared budget bounds every category of rendered data row
        # (direct dependencies, direct dependents, expansion depths, and
        # cycles) at `graph.limits.max_nodes` in total — not per section —
        # so no single category can silently render an unbounded table.
        budget = _RowBudget(self.graph.limits.max_nodes)
        index, deps_omitted = self._add_dependencies(table, 0, budget)
        if deps_omitted:
            index = self._add_capped_row(
                table, index, "Dependencies", f"{deps_omitted} dependency row(s) omitted"
            )
        index, direct_omitted = self._add_direct_dependents(table, index, budget)
        depth_omitted = 0
        cycle_omitted = 0
        truncated = False
        if self._expanded:
            index, depth_omitted, cycle_omitted, truncated = self._add_expansion(
                table, index, budget
            )
        if direct_omitted or depth_omitted or cycle_omitted or truncated:
            index = self._add_capped_row(
                table,
                index,
                "Dependents",
                _capped_reasons(
                    direct_omitted=direct_omitted,
                    depth_omitted=depth_omitted,
                    cycle_omitted=cycle_omitted,
                    truncated=truncated,
                    max_nodes=self.graph.limits.max_nodes,
                ),
            )

    def _add_dependencies(
        self, table: DataTable[str | Text], index: int, budget: _RowBudget
    ) -> tuple[int, int]:
        table.add_row("Dependencies", "", "", "", "", "", key=f"row-{index}")
        index += 1
        omitted = 0
        for edge in self.graph.dependencies_of(self.root):
            if not budget.take():
                omitted += 1
                continue
            resolvable = edge.resolution is EdgeResolution.RESOLVED
            index = self._add_edge_row(
                table,
                index,
                "",
                edge,
                edge.target,
                resolvable=resolvable,
                reason=edge.resolution.value,
                state=edge.resolution.value,
            )
        return index, omitted

    def _add_direct_dependents(
        self, table: DataTable[str | Text], index: int, budget: _RowBudget
    ) -> tuple[int, int]:
        table.add_row("Dependents", "", "", "", "", "", key=f"row-{index}")
        index += 1
        omitted = 0
        for edge in self.graph.dependents_of(self.root):
            if not budget.take():
                omitted += 1
                continue
            index = self._add_dependent_row(table, index, "", edge)
        return index, omitted

    def _dependent_resolvable(self, edge: RelationshipEdge) -> bool:
        """Whether the resource a Dependents row navigates to is real.

        A Dependents row always navigates `edge.subject`, never
        `edge.target` — `edge.resolution` describes only whether
        `edge.target` was found at build time and says nothing about the
        dependent itself. `edge.subject` is a genuine, discovered node by
        construction (`dependents_of` only surfaces edges whose subject was
        actually walked), verified here against the node index rather than
        assumed unconditionally true.
        """
        return edge.subject in self._known_nodes

    def _add_dependent_row(
        self,
        table: DataTable[str | Text],
        index: int,
        direction: str,
        edge: RelationshipEdge,
    ) -> int:
        """`_add_edge_row` for a Dependents-direction row (subject-navigating).

        Resolvability and state come from `_dependent_resolvable`, never
        from `edge.resolution` — see that method's docstring.
        """
        resolvable = self._dependent_resolvable(edge)
        state = "resolved" if resolvable else "not indexed"
        return self._add_edge_row(
            table,
            index,
            direction,
            edge,
            edge.subject,
            resolvable=resolvable,
            reason="" if resolvable else state,
            state=state,
        )

    def _add_edge_row(
        self,
        table: DataTable[str | Text],
        index: int,
        direction: str,
        edge: RelationshipEdge,
        resource: GraphResource,
        *,
        resolvable: bool,
        reason: str,
        state: str,
    ) -> int:
        key = f"row-{index}"
        table.add_row(
            direction,
            edge.relation.value,
            Text(_resource_label(resource)),
            edge.confidence.value,
            state,
            Text(edge.evidence.field),
            key=key,
        )
        self._targets[key] = _RowTarget(resource=resource, resolvable=resolvable, reason=reason)
        return index + 1

    def _add_capped_row(
        self, table: DataTable[str | Text], index: int, label: str, detail: str
    ) -> int:
        table.add_row(
            f"{label} (capped)", "-", "-", "-", "capped", Text(detail), key=f"row-{index}"
        )
        return index + 1

    def _bfs_depths(self, edges: tuple[RelationshipEdge, ...]) -> dict[GraphResource, int]:
        """Depth-from-root for every subject in `edges`, order-independent.

        `TraversalResult.edges` happens to already come back in true BFS
        order from `walk_dependents`'s current implementation, but relying
        on that as an implicit contract is fragile. This instead rebuilds
        a target-keyed adjacency map first (indifferent to `edges`'
        iteration order) and runs its own fresh BFS from `self.root` over
        it, so depth labels stay correct even if the traversal's internal
        ordering ever changes.
        """
        children_of: dict[GraphResource, list[GraphResource]] = defaultdict(list)
        for edge in edges:
            children_of[edge.target].append(edge.subject)
        depth_of: dict[GraphResource, int] = {self.root: 0}
        queue: deque[GraphResource] = deque([self.root])
        while queue:
            current = queue.popleft()
            for child in children_of.get(current, ()):
                if child in depth_of:
                    continue
                depth_of[child] = depth_of[current] + 1
                queue.append(child)
        return depth_of

    def _add_expansion(
        self, table: DataTable[str | Text], index: int, budget: _RowBudget
    ) -> tuple[int, int, int, bool]:
        """Bounded dependent expansion (the `d` toggle).

        Reuses `RelationshipGraph.walk_dependents` for the actual bounded
        BFS (cap logic lives there, tested independently); this renders
        the *additional* rows beyond the direct dependents already shown —
        deeper edges (tagged with an order-independent BFS depth) and
        cycle edges — sharing the same row `budget` as every other
        section, since `walk_dependents` does not itself bound `cycles`.
        Returns `(index, depth_omitted, cycle_omitted, truncated)`.
        """
        result = self.graph.walk_dependents(self.root)
        depth_of = self._bfs_depths(result.edges)
        deeper_edges = sorted(
            (edge for edge in result.edges if depth_of.get(edge.subject, 0) > 1),
            key=lambda edge: (depth_of[edge.subject], edge.evidence.field, edge.subject.name),
        )
        depth_omitted = 0
        for edge in deeper_edges:
            if not budget.take():
                depth_omitted += 1
                continue
            depth = depth_of[edge.subject]
            index = self._add_dependent_row(table, index, f"Dependents (depth {depth})", edge)
        cycle_omitted = 0
        for edge in result.cycles:
            if not budget.take():
                cycle_omitted += 1
                continue
            table.add_row(
                "Dependents (cycle)",
                edge.relation.value,
                Text(_resource_label(edge.subject)),
                edge.confidence.value,
                "cycle",
                Text(edge.evidence.field),
                key=f"row-{index}",
            )
            index += 1
        return index, depth_omitted, cycle_omitted, result.truncated

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        target = self._targets.get(event.row_key.value or "")
        if target is None:
            self.query_one("#relationship-status", Static).update(
                "No relationship recorded for this row."
            )
            return
        if not target.resolvable:
            label = _resource_label(target.resource)
            self.query_one("#relationship-status", Static).update(
                f"{label} is {target.reason} — cannot navigate"
            )
            return
        resource = target.resource
        self.dismiss(("goto", resource.group, resource.kind, resource.namespace, resource.name))

    def action_toggle_expand(self) -> None:
        self._expanded = not self._expanded
        self._render_table()

    def action_toggle_coverage(self) -> None:
        self._coverage_detailed = not self._coverage_detailed
        self._render_coverage()

    def action_close(self) -> None:
        self.dismiss(None)
