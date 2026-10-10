"""Read-only modal for one Deployment operation outcome tracker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from korvid.core.deployment_outcome import DeploymentRestartIntent, DeploymentScaleIntent
from korvid.ui.deployment_outcome_controller import DeploymentTrackerSnapshot

OutcomeScreenVerb = Literal["goto", "events", "describe", "logs", "stop", "refresh"]
OutcomeScreenResult = tuple[OutcomeScreenVerb, str, str, str, str]

_COLUMNS = ("POD", "PHASE", "REASON", "UID", "MESSAGE")


@dataclass(frozen=True, slots=True)
class _PodTarget:
    namespace: str
    name: str
    uid: str


class DeploymentOutcomeScreen(ModalScreen[OutcomeScreenResult | None]):
    """Render accepted-write and convergence evidence as separate facts."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=True),
        Binding("q", "close", "Close", show=False),
        Binding("x", "stop", "Stop", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("enter", "pod_goto", "Pod", show=True),
        Binding("e", "pod_events", "Events", show=True),
        Binding("d", "pod_describe", "Describe", show=True),
        Binding("l", "pod_logs", "Logs", show=True),
    ]

    DEFAULT_CSS = """
    DeploymentOutcomeScreen {
        align: center middle;
        background: $background 80%;
    }
    DeploymentOutcomeScreen > VerticalScroll {
        width: 92%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $surface;
        padding: 0 1;
    }
    DeploymentOutcomeScreen #deployment-outcome-title {
        text-style: bold;
    }
    DeploymentOutcomeScreen #deployment-outcome-status {
        color: $warning;
    }
    DeploymentOutcomeScreen #deployment-outcome-summary {
        margin-top: 1;
    }
    DeploymentOutcomeScreen #deployment-outcome-evidence {
        color: $text-muted;
    }
    DeploymentOutcomeScreen #deployment-outcome-hint {
        color: $text-muted;
    }
    DeploymentOutcomeScreen DataTable {
        height: auto;
        max-height: 14;
        margin-top: 1;
    }
    """

    def __init__(self, snapshot: DeploymentTrackerSnapshot) -> None:
        super().__init__()
        self.snapshot = snapshot
        self._targets: dict[str, _PodTarget] = {}

    def compose(self) -> ComposeResult:
        target = self.snapshot.intent.target
        with VerticalScroll():
            yield Footer()
            yield Static(
                f"Deployment outcome: {target.namespace}/{target.name}",
                id="deployment-outcome-title",
                markup=False,
            )
            yield Static(self._status(), id="deployment-outcome-status", markup=False)
            yield Static(
                self.snapshot.outcome.summary,
                id="deployment-outcome-summary",
                markup=False,
            )
            yield Static(
                "\n".join(self.snapshot.outcome.evidence) or "No controller evidence yet",
                id="deployment-outcome-evidence",
                markup=False,
            )
            yield DataTable[str | Text](id="deployment-outcome-pods")
            yield Static(
                "Enter: Pod · e: events · d: describe · l: logs · "
                "r: refresh · x: stop · Esc: close without stopping",
                id="deployment-outcome-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*_COLUMNS)
        for index, pod in enumerate(self.snapshot.outcome.pods):
            row_key = f"pod-{index}"
            self._targets[row_key] = _PodTarget(pod.namespace, pod.name, pod.uid)
            table.add_row(
                Text(pod.name),
                Text(pod.phase or "-"),
                Text(pod.reason or "-"),
                Text(pod.uid or "-"),
                Text(pod.message or "-"),
                key=row_key,
            )
        table.focus()

    def _status(self) -> str:
        intent = self.snapshot.intent
        if isinstance(intent, DeploymentScaleIntent):
            request = f"scale to {intent.replicas}"
        elif isinstance(intent, DeploymentRestartIntent):
            request = f"restart marker {intent.restarted_at}"
        else:
            request = "operation"
        partial = " · partial evidence" if self.snapshot.outcome.partial_evidence else ""
        return (
            f"API request accepted: {request} at {self.snapshot.accepted_at}\n"
            f"Deployment convergence: {self.snapshot.outcome.phase.value} "
            f"(reads={self.snapshot.attempts}, "
            f"elapsed={self.snapshot.elapsed_seconds:.1f}s){partial}"
        )

    def action_close(self) -> None:
        self.dismiss(None)

    def action_stop(self) -> None:
        self.dismiss(("stop", self.snapshot.tracker_id, "", "", ""))

    def action_refresh(self) -> None:
        self.dismiss(("refresh", self.snapshot.tracker_id, "", "", ""))

    def _selected_target(self) -> _PodTarget | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            self.query_one("#deployment-outcome-hint", Static).update(
                "No affected Pod evidence is available"
            )
            return None
        row_key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        target = self._targets.get(row_key)
        if target is None or not target.uid:
            self.query_one("#deployment-outcome-hint", Static).update(
                "Pod identity is unavailable; navigation is disabled"
            )
            return None
        return target

    def _dismiss_pod(self, verb: OutcomeScreenVerb) -> None:
        target = self._selected_target()
        if target is None:
            return
        self._dismiss_target(verb, target)

    def _dismiss_target(self, verb: OutcomeScreenVerb, target: _PodTarget) -> None:
        self.dismiss(
            (
                verb,
                self.snapshot.tracker_id,
                target.namespace,
                target.name,
                target.uid,
            )
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        target = self._targets.get(str(event.row_key.value or ""))
        if target is None or not target.uid:
            self.query_one("#deployment-outcome-hint", Static).update(
                "Pod identity is unavailable; navigation is disabled"
            )
            return
        self._dismiss_target("goto", target)

    def action_pod_goto(self) -> None:
        self._dismiss_pod("goto")

    def action_pod_events(self) -> None:
        self._dismiss_pod("events")

    def action_pod_describe(self) -> None:
        self._dismiss_pod("describe")

    def action_pod_logs(self) -> None:
        self._dismiss_pod("logs")
