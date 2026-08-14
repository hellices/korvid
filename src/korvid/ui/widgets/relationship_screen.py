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

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from korvid.core.relationships import (
    CoverageState,
    EdgeResolution,
    GraphResource,
    RelationshipEdge,
    RelationshipGraph,
)

#: DIRECTION | RELATION | RESOURCE | CONFIDENCE | STATE | EVIDENCE
_COLUMNS = ("DIRECTION", "RELATION", "RESOURCE", "CONFIDENCE", "STATE", "EVIDENCE")

#: `("goto", group, kind, namespace, name)` — the dismissed navigation target.
GotoResult = tuple[str, str, str, str, str]

_IDLE_STATUS = "Enter: navigate a resolved row · d: expand dependents · c: coverage detail"


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

    def _render_coverage(self) -> None:
        lines = ["Coverage: incomplete" if self.graph.incomplete else "Coverage: complete"]
        for record in self.graph.coverage:
            if record.state is CoverageState.COMPLETE:
                continue
            line = f"{record.group or 'core'}/{record.resource}: {record.state.value}"
            if self._coverage_detailed and record.detail:
                line = f"{line} — {record.detail}"
            lines.append(line)
        self.query_one("#relationship-coverage", Static).update("\n".join(lines))

    def _render_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self._targets = {}
        index = self._add_section(
            table, 0, "Dependencies", self.graph.dependencies_of(self.root), _target_of
        )
        index = self._add_section(
            table, index, "Dependents", self.graph.dependents_of(self.root), _subject_of
        )
        if self._expanded:
            self._add_expansion(table, index)

    def _add_section(
        self,
        table: DataTable[str | Text],
        index: int,
        label: str,
        edges: tuple[RelationshipEdge, ...],
        other_of: Callable[[RelationshipEdge], GraphResource],
    ) -> int:
        table.add_row(label, "", "", "", "", "", key=f"row-{index}")
        index += 1
        for edge in edges:
            index = self._add_edge_row(table, index, "", edge, other_of(edge))
        return index

    def _add_edge_row(
        self,
        table: DataTable[str | Text],
        index: int,
        direction: str,
        edge: RelationshipEdge,
        resource: GraphResource,
    ) -> int:
        key = f"row-{index}"
        table.add_row(
            direction,
            edge.relation.value,
            Text(_resource_label(resource)),
            edge.confidence.value,
            edge.resolution.value,
            Text(edge.evidence.field),
            key=key,
        )
        resolvable = edge.resolution is EdgeResolution.RESOLVED
        self._targets[key] = _RowTarget(
            resource=resource, resolvable=resolvable, reason=edge.resolution.value
        )
        return index + 1

    def _add_expansion(self, table: DataTable[str | Text], index: int) -> int:
        """Bounded dependent expansion (the `d` toggle).

        Reuses `RelationshipGraph.walk_dependents` for the actual bounded
        BFS (cap/cycle logic lives there, tested independently); this only
        adds the *additional* rows beyond the direct dependents already
        shown — deeper edges (tagged with their BFS depth), cycle edges,
        and a capped marker when the node cap stopped traversal early.
        """
        result = self.graph.walk_dependents(self.root)
        depth_of: dict[GraphResource, int] = {self.root: 0}
        for edge in result.edges:
            depth = depth_of.get(edge.target, 0) + 1
            depth_of[edge.subject] = depth
            if depth <= 1:
                continue  # already rendered by the direct Dependents section
            index = self._add_edge_row(
                table, index, f"Dependents (depth {depth})", edge, edge.subject
            )
        for edge in result.cycles:
            key = f"row-{index}"
            table.add_row(
                "Dependents (cycle)",
                edge.relation.value,
                Text(_resource_label(edge.subject)),
                edge.confidence.value,
                "cycle",
                Text(edge.evidence.field),
                key=key,
            )
            index += 1
        if result.truncated:
            table.add_row(
                "Dependents (capped)",
                "-",
                "-",
                "-",
                "capped",
                Text(f"expansion capped at {self.graph.limits.max_nodes} node(s)"),
                key=f"row-{index}",
            )
            index += 1
        return index

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


def _target_of(edge: RelationshipEdge) -> GraphResource:
    return edge.target


def _subject_of(edge: RelationshipEdge) -> GraphResource:
    return edge.subject
