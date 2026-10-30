"""Why a workload's Pods are not being created: recorded failures and namespace policies (#411).

`CreationConstraintsScreen` shows one `CreationReport`: the workload and its
counters, a line per evidence source from the shared renderer, then the
recorded creation failures, the namespace's ResourceQuotas and its
LimitRanges, and what the evidence does not establish. It performs no I/O,
but re-renders the evidence lines and notes against its clock while it is
open, so a policy list that ages past its freshness policy stops ruling
anything out. Enter on a row dismisses with the `Destination` it leads to;
Escape dismisses with `None`.

Every cell is a plain `Text`, so cluster-provided names and messages are
shown literally.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from korvid.core.capability import Basis, EvidenceState, Observation, conclusive, is_fresh
from korvid.core.creation_constraints import CreationReport, declarations
from korvid.core.creation_failures import Failure, FailureSource, RejectionKind
from korvid.core.creation_policies import LimitRangeProjection, LimitValue, QuotaProjection
from korvid.core.creation_quantities import Usage
from korvid.core.creation_workloads import WORKLOAD_KINDS
from korvid.ui.evidence_text import (
    basis_sentence,
    elapsed_text,
    not_established,
    observation_line,
)

_COLUMNS = ("Object", "What", "Value", "Note")
_FAILURES = "Recorded creation failures — what the controller recorded, when it was recorded"
_QUOTAS = "ResourceQuotas — current values; not a reservation, and the next create may still differ"
_LIMIT_RANGES = "LimitRanges — current constraints; other admission plugins can also change a Pod"
_QUOTA_KINDS = frozenset(
    {
        RejectionKind.QUOTA_EXCEEDED,
        RejectionKind.QUOTA_REQUIRES_FIELD,
        RejectionKind.QUOTA_STATUS_UNKNOWN,
    }
)
_LIMIT_KINDS = frozenset(
    {RejectionKind.LIMIT_MINIMUM, RejectionKind.LIMIT_MAXIMUM, RejectionKind.LIMIT_RATIO}
)
#: Each LimitRange constraint and its field name, in display order.
_LIMIT_FIELDS: tuple[tuple[str, Callable[[LimitValue], str | None]], ...] = (
    ("default", lambda value: value.default),
    ("defaultRequest", lambda value: value.default_request),
    ("min", lambda value: value.min),
    ("max", lambda value: value.max),
    ("maxLimitRequestRatio", lambda value: value.max_limit_request_ratio),
)


@dataclass(frozen=True, slots=True)
class Destination:
    """The object a row leads to: a view alias, its namespace and name, and its UID."""

    kind: str
    namespace: str
    name: str
    uid: str | None = None


@dataclass(frozen=True, slots=True)
class Heading:
    """A section heading: what the section shows, then how to read it."""

    text: str


@dataclass(frozen=True, slots=True)
class Row:
    """One row's four cells, and where Enter on it leads."""

    cells: tuple[str, str, str, str]
    destination: Destination | None = None


TableRow = Heading | Row


def title_text(report: CreationReport) -> str:
    """The workload, and the counters its controller last reported."""
    target = report.target
    title = f"{target.kind} {target.namespace}/{target.name}"
    if report.workload is None:
        return title
    counters = " · ".join(f"{c.label} {c.value or '?'}" for c in report.workload.counters)
    return f"{title}   {counters}"


def _observations(report: CreationReport) -> dict[str, Observation | None]:
    return {
        "workload": report.workload_observation,
        "children": report.children_observation,
        "events": report.events_observation,
        "quotas": report.quotas_observation,
        "limit-ranges": report.limit_ranges_observation,
    }


def evidence_lines(report: CreationReport, now: datetime) -> list[Text]:
    """Each source's line, in reading order, judged by its own policy."""
    observations = _observations(report)
    lines: list[Text] = []
    for declaration in declarations(report.target):
        observation = observations[declaration.key]
        if observation is not None:
            lines.append(observation_line(observation, declaration.policy, now))
    return lines


def _quota_text(name: str, report: CreationReport, now: datetime) -> str:
    if any(quota.name == name for quota in report.quotas):
        return f"quota {name}"
    if report.quotas_conclusive(now):
        return f"quota {name} (no longer present)"
    return f"quota {name} (not in the quotas read)"


def _rejection_text(failure: Failure, report: CreationReport, now: datetime) -> str:
    rejection = failure.rejection
    kinds = "; ".join(kind.value for kind in rejection.kinds)
    targets = [_quota_text(name, report, now) for name in rejection.quotas]
    if any(kind in _LIMIT_KINDS for kind in rejection.kinds):
        targets.append("a LimitRange in this namespace")
    return f"{', '.join(targets)} — {kinds}" if targets else kinds


def _failure_row(failure: Failure, report: CreationReport, now: datetime) -> Row:
    subject = failure.subject
    label = f"{subject.kind} {subject.name}"
    if failure.source is FailureSource.CONDITION:
        label = f"{label} (condition)"
    when = "undated" if failure.at is None else f"last {elapsed_text(failure.at, now)}"
    destination = Destination(
        WORKLOAD_KINDS[subject.kind].resource, report.target.namespace, subject.name, subject.uid
    )
    times = "once" if failure.count == 1 else f"{failure.count} times"
    cells = (label, f"{times} · {when}", _rejection_text(failure, report, now))
    return Row((*cells, failure.rejection.message), destination)


def _scopes_text(quota: QuotaProjection) -> str:
    parts = list(quota.scopes)
    parts += [
        f"{e.scope_name} {e.operator}" + (f" {', '.join(e.values)}" if e.values else "")
        for e in quota.scope_selector
    ]
    return "; ".join(parts)


def _marker(quota: QuotaProjection, usage: Usage) -> str:
    if quota.stale:
        return "status not yet updated"
    return "at its limit" if usage is Usage.AT_LIMIT else ""


def _quota_rows(quota: QuotaProjection, namespace: str) -> list[Row]:
    destination = Destination("resourcequotas", namespace, quota.name, quota.uid)
    cells: list[tuple[str, str, str, str]] = [
        (
            "",
            f"{entry.group.value}: {entry.resource}",
            f"used {entry.used or '?'} / hard {entry.hard}",
            _marker(quota, entry.usage),
        )
        for entry in quota.entries
    ] or [("", "no hard limits", "", "")]
    scopes = _scopes_text(quota)
    if scopes:
        cells.append(("", "scopes", scopes, ""))
    cells[0] = (quota.name, *cells[0][1:])
    return [Row(row, destination) for row in cells]


def _limit_text(value: LimitValue) -> str:
    return " · ".join(
        f"{name} {quantity}"
        for name, read in _LIMIT_FIELDS
        if (quantity := read(value)) is not None
    )


def _limit_range_rows(limit_range: LimitRangeProjection, namespace: str) -> list[Row]:
    destination = Destination("limitranges", namespace, limit_range.name, limit_range.uid)
    cells: list[tuple[str, str, str, str]] = [
        ("", f"{value.type} {value.resource}", _limit_text(value), "")
        for value in limit_range.values
    ] or [("", "no constraints", "", "")]
    cells[0] = (limit_range.name, *cells[0][1:])
    return [Row(row, destination) for row in cells]


def table_rows(report: CreationReport, now: datetime) -> list[TableRow]:
    """The failures, quotas and LimitRanges under their headings; an empty section is left out."""
    namespace = report.target.namespace
    sections: list[tuple[str, list[Row]]] = [
        (_FAILURES, [_failure_row(f, report, now) for f in report.failures]),
        (_QUOTAS, [row for q in report.quotas for row in _quota_rows(q, namespace)]),
        (
            _LIMIT_RANGES,
            [row for r in report.limit_ranges for row in _limit_range_rows(r, namespace)],
        ),
    ]
    rows: list[TableRow] = []
    for heading, section in sections:
        if section:
            rows.append(Heading(heading))
            rows += section
    return rows


def _workload_notes(report: CreationReport, now: datetime) -> list[Text]:
    kind = report.target.kind
    observation = report.workload_observation
    if observation.basis is Basis.OBJECT_ABSENT:
        if not is_fresh(observation, declarations(report.target)[0].policy, now):
            return [
                Text(f"The {kind} did not exist when it was read; it may have been created since.")
            ]
        return [Text(f"The {kind} no longer exists.")]
    notes: list[Text] = []
    if report.recreated:
        notes.append(
            Text(
                f"The {kind} was recreated since it was selected: only the new one's "
                "evidence is shown."
            )
        )
    if report.workload is not None:
        notes.append(
            Text("The counters are what the controller last reported, not a count of Pods.")
        )
    return notes


def _failure_notes(report: CreationReport) -> list[Text]:
    target = report.target
    notes: list[Text] = []
    if report.earlier_failures:
        notes.append(
            Text(
                f"{report.earlier_failures} creation failures recorded about an earlier "
                f"{target.kind} named {target.name} are not shown."
            )
        )
    if report.failures:
        notes.append(
            Text(
                "The rejections were recorded at the times shown; the quotas and LimitRanges "
                "are as read now."
            )
        )
    elif report.events_observation.state is EvidenceState.AVAILABLE:
        notes.append(
            Text(
                "No creation failure is recorded in the Events and conditions read. Events "
                "expire, so this does not show that none happened."
            )
        )
    else:
        reason = basis_sentence(report.events_observation.basis)
        notes.append(not_established(f"whether a creation failure is recorded: {reason}"))
    return notes


def _listed(
    report: CreationReport, key: str, observation: Observation | None, now: datetime
) -> bool | str:
    """`True` when the list is conclusive, otherwise why it is not."""
    if observation is None:
        return True
    policy = next(d.policy for d in declarations(report.target) if d.key == key)
    if conclusive(observation, policy, now):
        return True
    if observation.state is EvidenceState.AVAILABLE and not is_fresh(observation, policy, now):
        return "the list is stale"
    return basis_sentence(observation.basis)


def _completeness_notes(report: CreationReport, now: datetime) -> list[Text]:
    namespace = report.target.namespace
    notes: list[Text] = []
    child = report.target.child
    children = _listed(report, "children", report.children_observation, now)
    if child is not None and children is not True:
        notes.append(not_established(f"whether every {child} is listed: {children}"))
    for key, observation, items, kind in (
        ("quotas", report.quotas_observation, report.quotas, "ResourceQuota"),
        ("limit-ranges", report.limit_ranges_observation, report.limit_ranges, "LimitRange"),
    ):
        listed = _listed(report, key, observation, now)
        if listed is not True:
            notes.append(not_established(f"whether every {kind} is listed: {listed}"))
        elif not items:
            notes.append(Text(f"No {kind} in namespace {namespace}."))
    return notes


def report_notes(report: CreationReport, now: datetime) -> list[Text]:
    """What the evidence says about the workload, and what it does not establish."""
    return [
        *_workload_notes(report, now),
        *_failure_notes(report),
        *_completeness_notes(report, now),
    ]


class CreationConstraintsScreen(ModalScreen[Destination | None]):
    """One workload's recorded creation failures and its namespace's policies."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=True),
        Binding("q", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    CreationConstraintsScreen {
        layout: vertical;
        background: $background;
    }
    CreationConstraintsScreen #creation-title {
        padding: 0 1;
        text-style: bold;
    }
    CreationConstraintsScreen #creation-evidence, CreationConstraintsScreen #creation-notes {
        padding: 0 1;
        color: $text-muted;
    }
    CreationConstraintsScreen DataTable {
        height: 1fr;
    }
    """

    def __init__(
        self,
        report: CreationReport,
        clock: Callable[[], datetime],
        *,
        refresh_seconds: float = 5.0,
    ) -> None:
        super().__init__()
        self.report = report
        self._clock = clock
        self._refresh_seconds = refresh_seconds
        self._rows: dict[str, Destination] = {}

    def compose(self) -> ComposeResult:
        yield Footer()
        yield Static(title_text(self.report), id="creation-title", markup=False)
        yield Static(id="creation-evidence")
        yield DataTable[Text](id="creation-table")
        yield Static(id="creation-notes")

    def _render_text(self) -> None:
        now = self._clock()
        evidence = Text("\n").join(evidence_lines(self.report, now))
        self.query_one("#creation-evidence", Static).update(evidence)
        notes = Text("\n").join(report_notes(self.report, now))
        self.query_one("#creation-notes", Static).update(notes)

    def on_mount(self) -> None:
        self._render_text()
        self.set_interval(self._refresh_seconds, self._render_text)
        table: DataTable[Text] = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*_COLUMNS)
        first: int | None = None
        for index, row in enumerate(table_rows(self.report, self._clock())):
            if isinstance(row, Heading):
                title, _, meaning = row.text.partition(" — ")
                table.add_row(Text(title, style="bold"), Text(""), Text(meaning), key=f"h{index}")
                continue
            key = f"r{index}"
            if row.destination is not None:
                self._rows[key] = row.destination
            table.add_row(*(Text(cell) for cell in row.cells), key=key)
            first = index if first is None else first
        if first is not None:
            table.move_cursor(row=first)
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row returns where it leads; a heading does nothing."""
        destination = self._rows.get(str(event.row_key.value))
        if destination is not None:
            self.dismiss(destination)

    def action_close(self) -> None:
        self.dismiss(None)
