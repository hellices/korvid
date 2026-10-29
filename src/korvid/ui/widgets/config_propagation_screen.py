"""Which Pods consume a ConfigMap or Secret, and what a change means for each (#410).

`ConfigPropagationScreen` shows one `PropagationReport`: a line per evidence
source from the shared renderer, then the consumers grouped by how they
consume the object, then what the evidence does not establish. It performs
no I/O, but re-renders the evidence lines and notes against its clock while
it is open, so evidence that ages past its policy is marked stale and stops
proving that no Pod references the object. Enter on a consumer row dismisses with that `Consumer`, so the
caller can open its Pod; Escape dismisses with `None`.

Every cell is a plain `Text`, so a cluster-provided name containing markup
is shown literally.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from korvid.core.capability import EvidenceState, is_fresh
from korvid.core.config_consumers import Consumer, ConsumptionMode, ContainerCategory
from korvid.core.config_propagation import PropagationReport, declarations
from korvid.ui.evidence_text import basis_sentence, not_established, observation_line

#: The sections, in display order, with what a change means for each.
_SECTIONS: tuple[tuple[frozenset[ConsumptionMode], str], ...] = (
    (
        frozenset({ConsumptionMode.ENV_FROM, ConsumptionMode.ENV_KEY}),
        "Environment variables — read when the container starts; a change needs a "
        "rollout restart, which asks for your approval",
    ),
    (
        frozenset({ConsumptionMode.VOLUME}),
        "Volumes — updated in place by the kubelet, eventually, unless the object is "
        "immutable; whether the application rereads them is not verified",
    ),
    (
        frozenset({ConsumptionMode.SUBPATH}),
        "subPath mounts — never updated; a change needs a rollout restart, which asks "
        "for your approval",
    ),
    (
        frozenset({ConsumptionMode.UNMOUNTED}),
        "Unmounted volumes — no container mounts them, so nothing reads them",
    ),
    (
        frozenset({ConsumptionMode.IMAGE_PULL}),
        "Image pull secrets — used only when an image is pulled",
    ),
)
_COLUMNS = ("Pod", "Container", "Reference")
_IN_CONTAINER = re.compile(r"^spec\.(?:containers|initContainers|ephemeralContainers)\[\d+\]\.")
_INIT_NOTE = (
    "An init container runs to completion before the other containers start; once it "
    "has finished, a change reaches it only when the Pod restarts."
)
_EPHEMERAL_NOTE = (
    "An ephemeral container cannot be restarted, and a rollout restart creates Pods "
    "without it: to read a change to its environment or subPath mount, add a new "
    "ephemeral container."
)
#: The modes an ephemeral container cannot pick a change up through.
_READ_AT_START = frozenset(
    {ConsumptionMode.ENV_FROM, ConsumptionMode.ENV_KEY, ConsumptionMode.SUBPATH}
)


@dataclass(frozen=True, slots=True)
class Heading:
    """A section heading: the mode, then what a change means for it."""

    text: str


@dataclass(frozen=True, slots=True)
class ConsumerRow:
    """One consumer and its Pod, Container and Reference cells."""

    consumer: Consumer
    cells: tuple[str, str, str]


TableRow = Heading | ConsumerRow


def _container_cell(consumer: Consumer) -> str:
    if consumer.category is None:
        return "— (Pod)"
    name = consumer.container or "?"
    if consumer.category is ContainerCategory.CONTAINER:
        return name
    return f"{name} ({consumer.category.value})"


def _volume_text(consumer: Consumer) -> str:
    volume = f"volume {consumer.volume or '(unnamed)'}"
    if consumer.mode is ConsumptionMode.UNMOUNTED:
        return volume
    text = f"{volume} → {consumer.mount_path or '?'}"
    if consumer.sub_path is not None:
        text = f"{text} (subPath {consumer.sub_path})"
    return text


def _reference_cell(consumer: Consumer) -> str:
    relative = _IN_CONTAINER.sub("", consumer.path)
    if consumer.mode is ConsumptionMode.ENV_KEY:
        variable = relative.split(".valueFrom", 1)[0]
        text = f"{variable} {consumer.env_name or '?'} ← key {consumer.key or '?'}"
    elif consumer.mode is ConsumptionMode.ENV_FROM:
        text = relative
    elif consumer.mode is ConsumptionMode.IMAGE_PULL:
        text = consumer.path.removeprefix("spec.")
    else:
        text = _volume_text(consumer)
    return f"{text} (optional)" if consumer.optional else text


def table_rows(consumers: Sequence[Consumer]) -> list[TableRow]:
    """The consumers under their section headings; an empty section is left out."""
    rows: list[TableRow] = []
    for modes, heading in _SECTIONS:
        section = [consumer for consumer in consumers if consumer.mode in modes]
        if section:
            rows.append(Heading(heading))
            rows += (
                ConsumerRow(c, (c.pod.name, _container_cell(c), _reference_cell(c)))
                for c in section
            )
    return rows


def evidence_lines(report: PropagationReport, now: datetime) -> list[Text]:
    """The object's and the Pod list's lines, each judged by its own policy."""
    policies = {declaration.key: declaration.policy for declaration in declarations(report.target)}
    return [
        observation_line(report.object, policies["config-object"], now),
        observation_line(report.pods, policies["pods"], now),
    ]


def _object_notes(report: PropagationReport, now: datetime) -> list[Text]:
    kind = report.target.kind
    if report.object_absent:
        if not is_fresh(report.object, declarations(report.target)[0].policy, now):
            return [
                Text(f"The {kind} did not exist when it was read; it may have been created since.")
            ]
        tail = " The Pods below still reference it." if report.consumers else ""
        return [Text(f"The {kind} no longer exists.{tail}")]
    if report.recreated:
        return [
            Text(
                f"The {kind} was recreated since it was selected: only the new one's "
                "evidence is shown, and whether existing volume mounts see it is "
                "not established."
            )
        ]
    return []


def _completeness_note(report: PropagationReport, now: datetime) -> Text | None:
    target = report.target
    if report.consumers_conclusive(now):
        if report.consumers:
            return None
        return Text(
            f"No Pod in namespace {target.namespace} references this {target.kind} through "
            "envFrom, env valueFrom, configMap, secret or projected volumes, or "
            "imagePullSecrets. CSI and other volume plugins are not checked."
        )
    pods = report.pods
    stale = pods.state is EvidenceState.AVAILABLE and not is_fresh(
        pods, declarations(target)[1].policy, now
    )
    reason = "the Pod list is stale" if stale else basis_sentence(pods.basis)
    return not_established(f"whether every consumer is listed: {reason}")


def report_notes(report: PropagationReport, now: datetime) -> list[Text]:
    """What the evidence says about the object, and what it does not establish."""
    notes = _object_notes(report, now)
    if any(consumer.category is ContainerCategory.INIT for consumer in report.consumers):
        notes.append(Text(_INIT_NOTE))
    if any(
        consumer.category is ContainerCategory.EPHEMERAL and consumer.mode in _READ_AT_START
        for consumer in report.consumers
    ):
        notes.append(Text(_EPHEMERAL_NOTE))
    completeness = _completeness_note(report, now)
    if completeness is not None:
        notes.append(completeness)
    notes.append(
        not_established(
            f"whether the {report.target.kind} is immutable (only its metadata is read)"
        )
    )
    return notes


class ConfigPropagationScreen(ModalScreen[Consumer | None]):
    """One ConfigMap's or Secret's consumers, by mode, with their evidence."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=True),
        Binding("q", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    ConfigPropagationScreen {
        layout: vertical;
        background: $background;
    }
    ConfigPropagationScreen #propagation-title {
        padding: 0 1;
        text-style: bold;
    }
    ConfigPropagationScreen #propagation-evidence, ConfigPropagationScreen #propagation-notes {
        padding: 0 1;
        color: $text-muted;
    }
    ConfigPropagationScreen DataTable {
        height: 1fr;
    }
    """

    def __init__(
        self,
        report: PropagationReport,
        clock: Callable[[], datetime],
        *,
        refresh_seconds: float = 5.0,
    ) -> None:
        super().__init__()
        self.report = report
        self._clock = clock
        self._refresh_seconds = refresh_seconds
        self._rows: dict[str, Consumer] = {}

    def compose(self) -> ComposeResult:
        target = self.report.target
        yield Footer()
        yield Static(
            f"{target.kind} {target.namespace}/{target.name}",
            id="propagation-title",
            markup=False,
        )
        yield Static(id="propagation-evidence")
        yield DataTable[Text](id="propagation-table")
        yield Static(id="propagation-notes")

    def _render_text(self) -> None:
        now = self._clock()
        evidence = Text("\n").join(evidence_lines(self.report, now))
        self.query_one("#propagation-evidence", Static).update(evidence)
        notes = Text("\n").join(report_notes(self.report, now))
        self.query_one("#propagation-notes", Static).update(notes)

    def on_mount(self) -> None:
        self._render_text()
        self.set_interval(self._refresh_seconds, self._render_text)
        table: DataTable[Text] = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*_COLUMNS)
        first: int | None = None
        for index, row in enumerate(table_rows(self.report.consumers)):
            if isinstance(row, Heading):
                title, _, meaning = row.text.partition(" — ")
                table.add_row(Text(title, style="bold"), Text(""), Text(meaning), key=f"h{index}")
                continue
            key = f"r{index}"
            self._rows[key] = row.consumer
            table.add_row(*(Text(cell) for cell in row.cells), key=key)
            first = index if first is None else first
        if first is not None:
            table.move_cursor(row=first)
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a consumer row returns it; a heading does nothing."""
        consumer = self._rows.get(str(event.row_key.value))
        if consumer is not None:
            self.dismiss(consumer)

    def action_close(self) -> None:
        self.dismiss(None)
