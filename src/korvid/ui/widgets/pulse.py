"""Ambient attention summary and explicitly refreshed, identity-bearing detail."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from korvid.core.pulse import PulseCoverage, PulseItem, PulseSnapshot, PulseTarget


@dataclass(frozen=True, slots=True)
class PulseGoto:
    """The observation frame and exact resource selected by the operator."""

    epoch: int
    scope: str | None
    target: PulseTarget


def summary_text(snapshot: PulseSnapshot) -> str:
    """Summarize observations without claiming overall cluster health."""
    gaps = sum(
        coverage.state != "complete"
        and not (coverage.source == "warning-watch" and coverage.state == "partial")
        for coverage in snapshot.coverage
    )
    current = f"{len(snapshot.current)} current" if snapshot.current else "no matched problems"
    return f"Pulse · {current} · {len(snapshot.recent)} recent · {gaps} gaps · :pulse"


class PulseSummary(Static):
    """One non-focusable line; events never become popups or moving selections."""

    DEFAULT_CSS = """
    PulseSummary {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $surface;
    }
    """

    def __init__(self) -> None:
        super().__init__("Pulse · observing… · :pulse", id="pulse-summary", markup=False)

    def show_snapshot(self, snapshot: PulseSnapshot) -> None:
        """Replace the summary without changing focus or the resource table."""
        self.update(Text(summary_text(snapshot)))


def _target_label(target: PulseTarget) -> str:
    kind = f"{target.kind}.{target.group}" if target.group else target.kind
    return "/".join(part for part in (kind, target.namespace, target.name) if part)


def _coverage_label(coverage: PulseCoverage) -> str:
    observed = (
        coverage.observed_at.isoformat(timespec="seconds")
        if coverage.observed_at
        else "not observed"
    )
    return f"{coverage.source}: {coverage.state} · {observed} · {coverage.detail}"


class PulseScreen(ModalScreen[PulseGoto | None]):
    """A frozen set of rows; only an explicit refresh can replace its ordering."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
        Binding("r", "refresh_rows", "Refresh rows"),
    ]

    DEFAULT_CSS = """
    PulseScreen { layout: vertical; background: $background; }
    PulseScreen #pulse-title { height: 1; padding: 0 1; text-style: bold; }
    PulseScreen #pulse-scope { height: auto; max-height: 3; padding: 0 1; color: $text-muted; }
    PulseScreen DataTable { height: 1fr; }
    PulseScreen #pulse-details { height: 5; padding: 0 1; border-top: solid $primary; }
    PulseScreen #pulse-evidence { height: auto; }
    PulseScreen #pulse-status { height: 2; padding: 0 1; color: $warning; }
    """

    def __init__(
        self,
        snapshot: PulseSnapshot,
        *,
        current: Callable[[], PulseSnapshot],
        refresh: Callable[[], None],
        target_problem: Callable[[PulseTarget], str | None],
    ) -> None:
        super().__init__()
        self._snapshot = snapshot
        self._current = current
        self._refresh_inputs = refresh
        self._target_problem = target_problem
        self._items: dict[str, PulseItem] = {}
        self._coverage: dict[str, PulseCoverage] = {}
        self._row_keys: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("Pulse / Problems", id="pulse-title", markup=False)
        yield Static(id="pulse-scope", markup=False)
        yield DataTable(cursor_type="row", id="pulse-table")
        with VerticalScroll(id="pulse-details"):
            yield Static(id="pulse-evidence", markup=False)
        yield Static(
            "Enter: resource · r: apply latest rows / refresh reads · Tab: evidence",
            id="pulse-status",
            markup=False,
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("SOURCE", "RESOURCE / COVERAGE", "OBSERVED EVIDENCE")
        self._render_rows(None)
        self.query_one(DataTable).focus()

    def note_update(self, snapshot: PulseSnapshot) -> None:
        """Signal new data without replacing rows, selection, or focus."""
        if self.is_mounted and snapshot != self._snapshot:
            self.query_one("#pulse-status", Static).update(
                "New data available — r to update rows · Enter: resource · Esc: close"
            )

    def _add_row(self, key: str, source: str, target: str, evidence: str) -> None:
        self._row_keys.append(key)
        self.query_one(DataTable).add_row(
            Text(source), Text(target[:48]), Text(evidence[:72]), key=key
        )

    def _render_rows(self, selected: str | None) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self._items.clear()
        self._coverage.clear()
        self._row_keys.clear()
        scope = self._snapshot.scope or "all namespaces"
        self.query_one("#pulse-scope", Static).update(
            Text(
                f"Snapshots: {scope} · live Warning feed: context-wide, best effort\n"
                "Current evidence ≠ recent events. Gaps/caps are not a healthy-cluster claim."
            )
        )
        for category, title, items in (
            ("current", "Current problems", self._snapshot.current),
            ("recent", "Recent warnings (not current state)", self._snapshot.recent),
        ):
            self._add_row(category, "", f"{title}: {len(items)}", "")
            for item in items:
                key = f"{category}:{item.key}"
                self._items[key] = item
                self._add_row(
                    key, item.source, _target_label(item.target), f"{item.reason} x{item.count}"
                )
        self._add_row("coverage", "", "Coverage / observation status", "")
        for coverage in self._snapshot.coverage:
            key = f"coverage:{coverage.source}"
            self._coverage[key] = coverage
            self._add_row(key, coverage.source, coverage.state, coverage.detail)
        row = self._row_keys.index(selected) if selected in self._row_keys else 0
        if selected is None and self._snapshot.current:
            row = 1
        table.move_cursor(row=row)

    def action_refresh_rows(self) -> None:
        """Apply buffered data and request fresh reads, preserving target identity."""
        table = self.query_one(DataTable)
        selected = self._row_keys[table.cursor_row] if self._row_keys else None
        self._snapshot = self._current()
        self._render_rows(selected)
        self._refresh_inputs()
        self.query_one("#pulse-status", Static).update(
            "Rows updated; fresh reads requested · r applies newer data when available"
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = str(event.row_key.value)
        item = self._items.get(key)
        if item is not None:
            evidence = f"{item.reason}: {item.message}"
            if item.finding is not None:
                evidence += "\n" + "; ".join(
                    f"{entry.field}={entry.value}" for entry in item.finding.evidence
                )
            text = (
                f"{_target_label(item.target)} · UID: {item.target.uid or 'missing'}\n"
                f"{item.category} · {item.source} · observed {item.observed_at.isoformat(timespec='seconds')}\n"
                f"{evidence}"
            )
        elif key in self._coverage:
            text = _coverage_label(self._coverage[key])
        else:
            text = "Choose an evidence row. Recent events are not proof of an active problem."
        self.query_one("#pulse-evidence", Static).update(Text(text))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        item = self._items.get(str(event.row_key.value))
        if item is None:
            return
        problem = self._target_problem(item.target)
        if problem:
            self.query_one("#pulse-status", Static).update(Text(problem))
            return
        self.dismiss(PulseGoto(self._snapshot.epoch, self._snapshot.scope, item.target))

    def action_close(self) -> None:
        """Restore the workspace without changing its current selection."""
        self.dismiss(None)
