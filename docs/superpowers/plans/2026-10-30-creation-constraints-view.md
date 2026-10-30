# Workload Creation Constraints, View (#411 Q1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the selected workload's recorded creation failures next to its namespace's ResourceQuotas and LimitRanges, through `:blocked` and the Action Palette (#525, the view half of slice Q1 of #411).

**Architecture:** `ui/widgets/creation_constraints_screen.py` renders one `CreationReport` (from #524) as pure row and note functions and a modal screen. `ui/creation_constraints_controller.py` runs `explain_creation` in a worker under a run token, as the `:consumers` controller does. A `:blocked` command and palette entry reach it through the command router; the composition root hands it the session's `EvidenceClient` and the workspace's `jump_to_object`.

**Tech Stack:** Python 3.11+, Textual, Rich `Text`, pytest with `asyncio_mode = "auto"`, mypy `--strict`, ruff, tach.

## Global Constraints

- **Design:** `docs/superpowers/specs/2026-10-30-creation-constraints-design.md`, sections "The view", "Evidence" and "Controller and lifecycle"; the presentation contract of `docs/superpowers/specs/2026-10-25-capability-aware-diagnostics-design.md`.
- **One line per source** from the shared renderer, in declaration order; the children line only for a Deployment or CronJob.
- **Attribution only as far as the message goes:** a quota by the name the message gives, flagged when the list lacks it; a LimitRange as "a LimitRange in this namespace"; anything else as "other rejection".
- **Absence only from a conclusive list:** "No ResourceQuota/LimitRange in namespace …" only when that list is conclusive; otherwise "Not established" with the reason, "the list is stale" once it ages. No recorded failure is never a conclusion.
- **Literal text:** every cell, the title and the notes are plain `Text`.
- **Late results:** dropped after a context switch, new credentials or a newer run, or when the view, scope or selection changed or a dialog opened; the context switch cancels the worker group; a failed worker is reported.
- **Palette search:** the help text must not contain the subsequence `h…e…l…m`, which a test reserves for the Helm rows.
- **Size limits:** `scripts/check_source_size.py` is not edited.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the design

- **Four generic columns.** The view's sections differ, so the table uses `Object`, `What`, `Value` and `Note` for all three, with a heading row per section, as the `:consumers` view does.
- **Counts read "once" or "N times".** The design's sketch used `12×`; ruff rejects the ambiguous multiplication sign, and words read better.
- **`elapsed_text`.** Failure ages read "last 2m ago", so `evidence_text` gains `elapsed_text`, which `age_text` now uses.
- **The fake UI surface records exclusivity.** `FakeUiSurface.exclusive_workers` lets the controller test pin `exclusive=True`, which no test checked before.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/ui/evidence_text.py` | modify | `elapsed_text` |
| `src/korvid/ui/widgets/creation_constraints_screen.py` | create | `Destination`, `title_text`, `evidence_lines`, `table_rows`, `report_notes`, `CreationConstraintsScreen` |
| `src/korvid/ui/creation_constraints_controller.py` | create | `CREATION_CONSTRAINTS_GROUP`, `CreationConstraintsController` |
| `src/korvid/ui/messages.py`, `command.py`, `command_router.py`, `action_policy.py` | modify | `:blocked`, its palette entry, routing and availability |
| `src/korvid/ui/context_switch_coordinator.py`, `worker_failures.py` | modify | cancel the group on a switch; report a failed run |
| `src/korvid/__main__.py` | modify | wire the controller |
| `tests/version_matrix/fixtures.yaml`, `test_scenarios.py`, `.github/workflows/k8s-version-matrix.yml` | modify | the live scenario and its path filters |
| `docs/tui.md`, `docs/keybindings.md`, `docs/kubernetes-support.md`, `docs/release-notes/unreleased.md` | modify | what the view shows and how to read it |

---

### Task 1: The view

Create `tests/ui/test_creation_constraints_screen.py` with:

```python
"""The workload creation constraints view (#411): rows, notes and keys."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from rich.text import Text
from textual.app import App
from textual.widgets import DataTable, Static

from korvid.core.capability import (
    ApiSource,
    Baseline,
    Basis,
    EvidenceState,
    Observation,
    SourceScope,
    Verb,
)
from korvid.core.creation_constraints import CreationReport, CreationTarget
from korvid.core.creation_failures import Failure, FailureSource, Subject, classify
from korvid.core.creation_policies import (
    LimitRangeProjection,
    LimitValue,
    QuotaEntry,
    QuotaProjection,
    ScopeExpression,
)
from korvid.core.creation_quantities import QuotaGroup, Usage
from korvid.core.creation_workloads import Counter, WorkloadProjection
from korvid.ui.widgets.creation_constraints_screen import (
    CreationConstraintsScreen,
    Destination,
    Heading,
    Row,
    evidence_lines,
    report_notes,
    table_rows,
    title_text,
)
from tests.ui.waits import screen_ready, until

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
TARGET = CreationTarget("Deployment", "shop", "api", "uid-api")
SCOPE = SourceScope.namespace("shop")
DEPLOYMENT = Subject("Deployment", "api", "uid-api")
REPLICA_SET = Subject("ReplicaSet", "api-7d9f", "uid-rs")
EXCEEDED = (
    'Error creating: pods "api-7d9f-x" is forbidden: exceeded quota: compute, '
    "requested: pods=1, used: pods=20, limited: pods=20"
)


def _source(group: str, resource: str, verb: Verb = Verb.LIST, **extra: Any) -> ApiSource:
    return ApiSource(group, resource, SCOPE, "v1", verb, **extra)


def _seen(source: ApiSource, basis: Basis = Basis.OBSERVED, uid: str | None = None) -> Observation:
    state = {
        Basis.OBSERVED: EvidenceState.AVAILABLE,
        Basis.OBJECT_ABSENT: EvidenceState.AVAILABLE,
        Basis.TRUNCATED: EvidenceState.PARTIAL,
        Basis.FORBIDDEN: EvidenceState.DENIED,
        Basis.STALE_STATUS: EvidenceState.PARTIAL,
    }[basis]
    return Observation(source, state, basis, NOW - timedelta(seconds=2), observed_uid=uid)


WORKLOAD_SOURCE = _source("apps", "deployments", Verb.GET, name="api", expected_uid="uid-api")
WORKLOAD = WorkloadProjection(
    DEPLOYMENT, (Counter("desired", "3"), Counter("created", "0")), None, None
)
QUOTA = QuotaProjection(
    "compute",
    "uid-q",
    (
        QuotaEntry("requests.cpu", QuotaGroup.COMPUTE, "2", "2", Usage.AT_LIMIT),
        QuotaEntry("pods", QuotaGroup.COUNT, "20", "20", Usage.AT_LIMIT),
        QuotaEntry("services", QuotaGroup.COUNT, "5", "1", Usage.BELOW),
    ),
    ("NotTerminating",),
    (ScopeExpression("PriorityClass", "In", ("gold", "silver")),),
)
LIMITS = LimitRangeProjection(
    "defaults",
    "uid-lr",
    (LimitValue("Container", "cpu", max="1", default="500m", default_request="100m"),),
)
FAILURE = Failure(
    REPLICA_SET, FailureSource.EVENT, classify(EXCEEDED), 12, NOW - timedelta(minutes=2)
)


def _report(**changes: Any) -> CreationReport:
    report = CreationReport(
        target=TARGET,
        workload_observation=_seen(WORKLOAD_SOURCE, uid="uid-api"),
        children_observation=_seen(_source("apps", "replicasets")),
        events_observation=_seen(_source("events.k8s.io", "events")),
        quotas_observation=_seen(_source("", "resourcequotas")),
        limit_ranges_observation=_seen(_source("", "limitranges")),
        workload=WORKLOAD,
        children=(),
        failures=(FAILURE,),
        earlier_failures=0,
        quotas=(QUOTA,),
        limit_ranges=(LIMITS,),
        baseline=Baseline(EvidenceState.AVAILABLE),
    )
    return replace(report, **changes)


def _plain(lines: list[Text]) -> list[str]:
    return [line.plain for line in lines]


def _rows(report: CreationReport) -> list[Row]:
    return [row for row in table_rows(report, NOW) if isinstance(row, Row)]


def test_the_title_names_the_workload_and_its_counters() -> None:
    assert title_text(_report()) == "Deployment shop/api   desired 3 · created 0"
    assert title_text(_report(workload=None)) == "Deployment shop/api"
    unset = replace(WORKLOAD, counters=(Counter("desired", None), Counter("created", "0")))
    assert title_text(_report(workload=unset)) == "Deployment shop/api   desired ? · created 0"


def test_each_source_has_a_line_from_the_shared_renderer_in_order() -> None:
    lines = _plain(evidence_lines(_report(), NOW))

    assert [line.split(":")[0] for line in lines] == [
        "deployments apps/v1 in namespace shop",
        "replicasets apps/v1 in namespace shop",
        "events events.k8s.io/v1 in namespace shop",
        "resourcequotas v1 in namespace shop",
        "limitranges v1 in namespace shop",
    ]
    no_children = _plain(evidence_lines(_report(children_observation=None), NOW))
    assert len(no_children) == 4


def test_sections_come_in_order_under_one_heading_each() -> None:
    headings = [row.text for row in table_rows(_report(), NOW) if isinstance(row, Heading)]

    assert headings[0].startswith("Recorded creation failures — what the controller recorded")
    assert headings[1].startswith("ResourceQuotas — current values; not a reservation")
    assert headings[2].startswith("LimitRanges — current constraints; other admission plugins")


def test_a_failure_row_says_what_recorded_it_how_often_when_and_why() -> None:
    (row, *_) = _rows(_report())

    assert row.cells == (
        "ReplicaSet api-7d9f",
        "12 times · last 2m ago",
        "quota compute — quota exceeded",
        EXCEEDED,
    )
    assert row.destination == Destination("replicasets", "shop", "api-7d9f", "uid-rs")


def test_a_condition_failure_and_an_undated_one_are_labelled() -> None:
    condition = replace(
        FAILURE, subject=DEPLOYMENT, source=FailureSource.CONDITION, count=1, at=None
    )

    (row, *_) = _rows(_report(failures=(condition,)))

    assert row.cells[:2] == ("Deployment api (condition)", "once · undated")
    assert row.destination == Destination("deployments", "shop", "api", "uid-api")


@pytest.mark.parametrize(
    ("quotas", "observation", "expected"),
    [
        ((), Basis.OBSERVED, "quota compute (no longer present) — quota exceeded"),
        ((), Basis.FORBIDDEN, "quota compute (not in the quotas read) — quota exceeded"),
    ],
)
def test_a_quota_the_message_names_but_the_list_lacks_is_said(
    quotas: tuple[QuotaProjection, ...], observation: Basis, expected: str
) -> None:
    report = _report(
        quotas=quotas, quotas_observation=_seen(_source("", "resourcequotas"), observation)
    )

    (row, *_) = _rows(report)

    assert row.cells[2] == expected


def test_limit_range_and_other_rejections_are_attributed_only_as_far_as_the_message_goes() -> None:
    limit = replace(
        FAILURE,
        rejection=classify(
            'pods "x" is forbidden: maximum cpu usage per Container is 1, but limit is 2'
        ),
    )
    webhook = replace(FAILURE, rejection=classify('admission webhook "p" denied the request: no'))

    cells = [
        row.cells[2]
        for row in _rows(_report(failures=(limit, webhook), quotas=(), limit_ranges=()))
    ]

    assert cells == [
        "a LimitRange in this namespace — above a LimitRange maximum",
        "other rejection",
    ]


def test_quota_rows_show_values_verbatim_with_markers_and_scopes() -> None:
    rows = [
        row
        for row in _rows(_report(failures=()))
        if row.destination and row.destination.kind == "resourcequotas"
    ]

    assert [row.cells for row in rows] == [
        ("compute", "compute: requests.cpu", "used 2 / hard 2", "at its limit"),
        ("", "object counts: pods", "used 20 / hard 20", "at its limit"),
        ("", "object counts: services", "used 1 / hard 5", ""),
        ("", "scopes", "NotTerminating; PriorityClass In gold, silver", ""),
    ]
    assert {row.destination for row in rows} == {
        Destination("resourcequotas", "shop", "compute", "uid-q")
    }


def test_a_stale_quota_says_its_status_is_not_yet_updated() -> None:
    stale = replace(
        QUOTA,
        entries=(QuotaEntry("pods", QuotaGroup.COUNT, "20", None, Usage.NOT_ESTABLISHED),),
        scopes=(),
        scope_selector=(),
        stale=True,
    )

    rows = [row.cells for row in _rows(_report(failures=(), quotas=(stale,), limit_ranges=()))]

    assert rows == [
        ("compute", "object counts: pods", "used ? / hard 20", "status not yet updated")
    ]


def test_limit_range_rows_list_each_constraint() -> None:
    rows = [row.cells for row in _rows(_report(failures=(), quotas=()))]

    assert rows == [("defaults", "Container cpu", "default 500m · defaultRequest 100m · max 1", "")]


def test_empty_policies_get_a_row_saying_so() -> None:
    empty_quota = replace(QUOTA, entries=(), scopes=(), scope_selector=())
    empty_range = replace(LIMITS, values=())

    rows = [
        row.cells
        for row in _rows(_report(failures=(), quotas=(empty_quota,), limit_ranges=(empty_range,)))
    ]

    assert rows == [("compute", "no hard limits", "", ""), ("defaults", "no constraints", "", "")]


def test_a_section_without_rows_is_left_out() -> None:
    headings = [
        r
        for r in table_rows(_report(failures=(), quotas=(), limit_ranges=()), NOW)
        if isinstance(r, Heading)
    ]

    assert headings == []


def test_notes_place_the_rejections_in_time_and_counters_in_context() -> None:
    notes = _plain(report_notes(_report(), NOW))

    assert (
        "The rejections were recorded at the times shown; the quotas and LimitRanges are as read now."
        in notes
    )
    assert "The counters are what the controller last reported, not a count of Pods." in notes
    assert not any(n.startswith(("No ResourceQuota", "No LimitRange")) for n in notes)


def test_no_recorded_failure_never_rules_one_out() -> None:
    available = _plain(report_notes(_report(failures=()), NOW))
    denied = _plain(
        report_notes(
            _report(
                failures=(),
                events_observation=_seen(_source("events.k8s.io", "events"), Basis.FORBIDDEN),
            ),
            NOW,
        )
    )

    assert (
        "No creation failure is recorded in the Events and conditions read. Events expire, so "
        "this does not show that none happened."
    ) in available
    assert any(
        n.startswith("Not established: whether a creation failure is recorded: your permissions")
        for n in denied
    )


def test_absent_policies_are_said_only_from_a_conclusive_list() -> None:
    empty = _plain(report_notes(_report(quotas=(), limit_ranges=()), NOW))
    truncated = _plain(
        report_notes(
            _report(
                quotas=(),
                quotas_observation=_seen(_source("", "resourcequotas"), Basis.TRUNCATED),
                limit_ranges=(),
                limit_ranges_observation=_seen(_source("", "limitranges"), Basis.FORBIDDEN),
            ),
            NOW,
        )
    )

    assert "No ResourceQuota in namespace shop." in empty
    assert "No LimitRange in namespace shop." in empty
    assert not any(
        n.startswith("No ResourceQuota") or n.startswith("No LimitRange") for n in truncated
    )
    assert (
        "Not established: whether every ResourceQuota is listed: stopped at the page limit"
        in truncated
    )
    assert any(
        n.startswith("Not established: whether every LimitRange is listed: your permissions")
        for n in truncated
    )


def test_a_stale_policy_list_no_longer_rules_anything_out() -> None:
    later = NOW + timedelta(minutes=5)

    notes = _plain(report_notes(_report(quotas=(), limit_ranges=()), later))

    assert "Not established: whether every ResourceQuota is listed: the list is stale" in notes
    assert "Not established: whether every LimitRange is listed: the list is stale" in notes


def test_an_inconclusive_child_list_is_said() -> None:
    report = _report(children_observation=_seen(_source("apps", "replicasets"), Basis.FORBIDDEN))

    notes = _plain(report_notes(report, NOW))

    assert any(
        n.startswith("Not established: whether every ReplicaSet is listed: your permissions")
        for n in notes
    )


def test_earlier_incarnations_are_counted() -> None:
    notes = _plain(report_notes(_report(earlier_failures=2), NOW))

    assert (
        "2 creation failures recorded about earlier incarnations of this Deployment or its "
        "ReplicaSets are not shown."
    ) in notes
    job = _report(
        target=CreationTarget("Job", "shop", "j"), earlier_failures=1, children_observation=None
    )
    assert (
        "1 creation failures recorded about earlier incarnations of this Job are not shown."
    ) in _plain(report_notes(job, NOW))


def test_a_gone_or_recreated_workload_is_said() -> None:
    gone = _report(
        workload=None,
        failures=(),
        workload_observation=_seen(WORKLOAD_SOURCE, Basis.OBJECT_ABSENT),
    )
    recreated = _report(workload_observation=_seen(WORKLOAD_SOURCE, uid="uid-new"))

    assert "The Deployment no longer exists." in _plain(report_notes(gone, NOW))
    assert not any("counters" in n for n in _plain(report_notes(gone, NOW)))
    assert (
        "The Deployment did not exist when it was read; it may have been created since."
        in _plain(report_notes(gone, NOW + timedelta(minutes=5)))
    )
    assert (
        "The Deployment was recreated since it was selected: only the new one's evidence is shown."
    ) in _plain(report_notes(recreated, NOW))


class _Host(App[None]):
    def __init__(self, screen: CreationConstraintsScreen) -> None:
        super().__init__()
        self.prompt = screen
        self.result: list[Destination | None] = []

    def on_mount(self) -> None:
        self.push_screen(self.prompt, self.result.append)


async def test_enter_on_a_row_returns_where_it_leads() -> None:
    app = _Host(CreationConstraintsScreen(_report(), lambda: NOW))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, CreationConstraintsScreen), label="view open")
        table = app.screen.query_one(DataTable)
        assert table.cursor_row == 1
        await pilot.press("down", "down", "enter")
        await until(pilot, lambda: bool(app.result), label="view closed")

    assert app.result == [Destination("resourcequotas", "shop", "compute", "uid-q")]


async def test_enter_on_a_heading_does_nothing_and_escape_closes() -> None:
    app = _Host(CreationConstraintsScreen(_report(), lambda: NOW))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, CreationConstraintsScreen), label="view open")
        await pilot.press("up", "enter")
        await pilot.pause()
        assert app.result == []
        await pilot.press("escape")
        await until(pilot, lambda: bool(app.result), label="view closed")

    assert app.result == [None]


async def test_cluster_text_is_shown_literally() -> None:
    hostile = replace(FAILURE, rejection=classify("[bold]boom[/bold]"))
    target = CreationTarget("Deployment", "shop", "[red]api[/red]")
    app = _Host(
        CreationConstraintsScreen(
            _report(target=target, failures=(hostile,), workload=None), lambda: NOW
        )
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, CreationConstraintsScreen), label="view open")
        title = app.screen.query_one("#creation-title", Static)
        assert "[red]api[/red]" in str(title.render())
        table = app.screen.query_one(DataTable)
        assert str(table.get_row_at(1)[3]) == "[bold]boom[/bold]"


async def test_an_open_view_ages_its_evidence_without_reading_again() -> None:
    now = [NOW]
    app = _Host(CreationConstraintsScreen(_report(quotas=()), lambda: now[0], refresh_seconds=0.05))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, CreationConstraintsScreen), label="view open")
        evidence = app.screen.query_one("#creation-evidence", Static)
        notes = app.screen.query_one("#creation-notes", Static)
        assert "No ResourceQuota in namespace shop." in str(notes.render())

        now[0] = NOW + timedelta(minutes=5)
        await until(pilot, lambda: "· stale" in str(evidence.render()), label="evidence aged")

        assert "No ResourceQuota in namespace shop." not in str(notes.render())


def test_without_the_workload_there_are_no_counters_to_explain() -> None:
    denied = _report(workload=None, workload_observation=_seen(WORKLOAD_SOURCE, Basis.FORBIDDEN))

    assert not any("counters" in n for n in _plain(report_notes(denied, NOW)))


def test_a_policy_that_could_not_be_read_in_full_never_claims_to_be_empty() -> None:
    quota = replace(QUOTA, entries=(), scopes=(), scope_selector=(), complete=False)
    partial = replace(QUOTA, name="partial", scopes=(), scope_selector=(), complete=False)
    limit_range = replace(LIMITS, values=(), complete=False)

    rows = [
        row.cells
        for row in _rows(_report(failures=(), quotas=(quota, partial), limit_ranges=(limit_range,)))
    ]

    assert ("compute", "constraints could not be read", "", "") in rows
    assert ("", "constraints could not be read", "", "") in rows
    assert ("defaults", "constraints could not be read", "", "") in rows
    assert not any(cells[1] in ("no hard limits", "no constraints") for cells in rows)


async def test_an_open_view_ages_its_table_cells_too() -> None:
    now = [NOW]
    report = _report(quotas=(), limit_ranges=())
    app = _Host(CreationConstraintsScreen(report, lambda: now[0], refresh_seconds=0.05))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, CreationConstraintsScreen), label="view open")
        table = app.screen.query_one(DataTable)
        assert str(table.get_row_at(1)[1]) == "12 times · last 2m ago"
        assert str(table.get_row_at(1)[2]) == "quota compute (no longer present) — quota exceeded"

        now[0] = NOW + timedelta(minutes=5)
        await until(
            pilot,
            lambda: str(table.get_row_at(1)[1]) == "12 times · last 7m ago",
            label="age refreshed",
        )

        assert str(table.get_row_at(1)[2]) == (
            "quota compute (not in the quotas read) — quota exceeded"
        )
        assert table.cursor_row == 1
```

Create `src/korvid/ui/widgets/creation_constraints_screen.py` with:

```python
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
from textual.widgets.data_table import ColumnKey

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
_UNREADABLE = ("", "constraints could not be read", "", "")
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
    ]
    if not quota.complete:
        cells.append(_UNREADABLE)
    elif not cells:
        cells.append(("", "no hard limits", "", ""))
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
    ]
    if not limit_range.complete:
        cells.append(_UNREADABLE)
    elif not cells:
        cells.append(("", "no constraints", "", ""))
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
        children = f" or its {target.child}s" if target.child is not None else ""
        notes.append(
            Text(
                f"{report.earlier_failures} creation failures recorded about earlier "
                f"incarnations of this {target.kind}{children} are not shown."
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
        self._columns: list[ColumnKey] = []

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
        self._refresh_rows(now)

    def _refresh_rows(self, now: datetime) -> None:
        """Re-render cells that depend on the time: ages, and whether a list still rules out."""
        if not self._columns:
            return
        table: DataTable[Text] = self.query_one(DataTable)
        for index, row in enumerate(table_rows(self.report, now)):
            if isinstance(row, Row):
                for column, cell in zip(self._columns, row.cells, strict=True):
                    table.update_cell(f"r{index}", column, Text(cell))

    def on_mount(self) -> None:
        self._render_text()
        self.set_interval(self._refresh_seconds, self._render_text)
        table: DataTable[Text] = self.query_one(DataTable)
        table.cursor_type = "row"
        self._columns = table.add_columns(*_COLUMNS)
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
```

- [ ] Add `elapsed_text` to `src/korvid/ui/evidence_text.py` (`2m ago`, `just now`, `in the future`), and make `age_text` prefix it with `observed`; test it in `tests/ui/test_evidence_text.py`.
- [ ] Run `uv run --frozen pytest -p no:tach tests/ui/test_creation_constraints_screen.py tests/ui/test_evidence_text.py -q` before (collection error) and after (PASS), then commit: `Show a workload's creation failures and namespace policies in a view`.

### Task 2: The controller

Create `tests/ui/test_creation_constraints_controller.py` with:

```python
"""The creation constraints controller: availability, runs and late results (#411)."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from typing import Any

from korvid.core.creation_constraints import CreationTarget
from korvid.core.creation_failures import Subject
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
)
from korvid.k8s.evidence_requests import EvidenceClient, EvidencePage, EvidenceTarget
from korvid.ui.action_availability import AvailabilityCode
from korvid.ui.creation_constraints_controller import (
    CREATION_CONSTRAINTS_GROUP,
    CreationConstraintsController,
)
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.creation_constraints_screen import CreationConstraintsScreen, Destination
from korvid.ui.workspace_controller import ContextGuard

from .test_session_timeline_controller import FakeUiSurface

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
DEPLOYMENTS = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
CRON_JOBS = ResourceMeta("CronJob", "cronjobs", "batch", "v1", True)
PODS = ResourceMeta("Pod", "pods", "", "v1", True)
FOREIGN = ResourceMeta("Deployment", "deployments", "vendor.example.com", "v1", True)
_SERVED = {
    ("", "v1"): ("resourcequotas", "limitranges", "events"),
    ("apps", "v1"): ("deployments", "replicasets"),
    ("batch", "v1"): ("jobs", "cronjobs"),
}
EVENT = {
    "metadata": {"name": "e", "namespace": "shop", "uid": "uid-e"},
    "reason": "FailedCreate",
    "involvedObject": {"kind": "ReplicaSet", "name": "api-7d9f", "uid": "uid-rs"},
    "message": 'pods "x" is forbidden: exceeded quota: compute, requested: pods=1',
}


class FakeView(ViewState):
    def __init__(self, kind: str = "deployments", selected: tuple[str, str] | None = None) -> None:
        self.kind = kind
        self.scope = "shop"
        self.selected = selected
        self.alias_map = {
            "deployments": DEPLOYMENTS,
            "cronjobs": CRON_JOBS,
            "pods": PODS,
            "deployments.vendor.example.com": FOREIGN,
        }

    def current_kind(self) -> str:
        return self.kind

    def canonical_kind(self, kind: str) -> str:
        return kind

    def aliases(self) -> Mapping[str, ResourceMeta]:
        return self.alias_map

    def selected_ns_name(self, *, notify: bool = True) -> tuple[str | None, str | None]:
        return self.selected if self.selected is not None else (None, None)

    def selected_uid(self, namespace: str | None, name: str) -> str | None:
        return f"uid-{name}"

    def current_scope(self) -> str:
        return self.scope

    def resources(self, kind: str, scope: str) -> list[Any]:
        return []

    def readonly(self) -> bool:
        return False

    def default_namespace(self) -> str | None:
        return "shop"

    def gvr_label(self, meta: ResourceMeta) -> str:
        return meta.plural

    def write_locus(self, namespace: str | None) -> str:
        return namespace or ""


class FakeContext(ContextGuard):
    def __init__(self) -> None:
        self.current = 1
        self.in_switch = False
        self.allowed = True

    def epoch(self) -> int:
        return self.current

    def switching(self) -> bool:
        return self.in_switch

    def reads_allowed(self) -> bool:
        return self.allowed

    def crossed(self, epoch: int) -> bool:
        return self.in_switch or epoch != self.current


class FakeClient(EvidenceClient):
    def __init__(self) -> None:
        self.generation = 3
        self.gets: list[tuple[EvidenceTarget, str]] = []
        self.lists: list[EvidenceTarget] = []

    @property
    def session_generation(self) -> int:
        return self.generation

    async def discovery_snapshot(self) -> RawDiscovery:
        outcomes = tuple(
            GroupVersionOutcome(
                group, version, tuple(ServedResource(r, frozenset({"get", "list"})) for r in names)
            )
            for (group, version), names in _SERVED.items()
        )
        groups = tuple((g, v) for g, v in _SERVED if g)
        return RawDiscovery(
            self.generation,
            NOW,
            0.0,
            RootOutcome("/api", (("", "v1"),)),
            RootOutcome("/apis", groups),
            outcomes,
        )

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.lists.append(target)
        if target.resource == "replicasets":
            owner = [{"uid": "uid-api", "controller": True}]
            rs = {
                "metadata": {
                    "name": "api-7d9f",
                    "namespace": "shop",
                    "uid": "uid-rs",
                    "ownerReferences": owner,
                }
            }
            return EvidencePage((rs,))
        if target.resource == "events":
            return EvidencePage((EVENT,))
        return EvidencePage(())

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        self.gets.append((target, name))
        return {
            "metadata": {"name": name, "namespace": "shop", "uid": f"uid-{name}"},
            "spec": {},
            "status": {},
        }


class Harness:
    def __init__(self, *, client: FakeClient | None = None, view: FakeView | None = None) -> None:
        self.ui = FakeUiSurface(close_worker_coroutines=False)
        self.view = view or FakeView(selected=("shop", "api"))
        self.context = FakeContext()
        self.client = FakeClient() if client is None else client
        self.navigations: list[tuple[str, str, str, int, str | None]] = []
        self.controller = CreationConstraintsController(
            ui=self.ui,
            view=self.view,
            context=self.context,
            client=self.client,
            navigate=self._navigate,
            clock=lambda: NOW,
        )

    async def _navigate(
        self, kind: str, namespace: str, name: str, epoch: int, uid: str | None
    ) -> None:
        self.navigations.append((kind, namespace, name, epoch, uid))

    async def run(self, index: int = -1) -> None:
        work = self.ui.workers[index]
        assert isinstance(work, Awaitable)
        await work


def _code(harness: Harness) -> AvailabilityCode | None:
    reason = harness.controller.unavailable_reason()
    return None if reason is None else reason.code


def test_it_is_available_on_a_selected_workload() -> None:
    assert _code(Harness()) is None
    assert _code(Harness(view=FakeView("cronjobs", ("shop", "nightly")))) is None


def test_it_says_why_it_is_unavailable() -> None:
    unconnected = CreationConstraintsController(
        ui=FakeUiSurface(),
        view=FakeView(selected=("shop", "api")),
        context=FakeContext(),
        client=None,
        navigate=Harness()._navigate,
    )

    reason = unconnected.unavailable_reason()
    assert reason is not None
    assert reason.code is AvailabilityCode.MISSING_CAPABILITY
    assert _code(Harness(view=FakeView("pods", ("shop", "api-1")))) is AvailabilityCode.WRONG_VIEW
    assert (
        _code(Harness(view=FakeView("deployments.vendor.example.com", ("shop", "x"))))
        is AvailabilityCode.WRONG_VIEW
    )
    assert _code(Harness(view=FakeView("deployments", None))) is AvailabilityCode.NO_SELECTION


async def test_opening_reads_the_selected_workload_and_shows_its_failures() -> None:
    harness = Harness()

    harness.controller.open_selected()
    await harness.run()

    assert harness.ui.worker_groups == [(CREATION_CONSTRAINTS_GROUP, False)]
    assert harness.ui.exclusive_workers == [True]
    (screen,) = harness.ui.screens
    assert isinstance(screen, CreationConstraintsScreen)
    assert screen.report.target == CreationTarget("Deployment", "shop", "api", "uid-api")
    assert [f.subject for f in screen.report.failures] == [
        Subject("ReplicaSet", "api-7d9f", "uid-rs")
    ]
    assert harness.client.gets == [(EvidenceTarget("apps", "v1", "deployments", "shop"), "api")]
    assert {t.resource for t in harness.client.lists} == {
        "replicasets",
        "events",
        "resourcequotas",
        "limitranges",
    }


def test_opening_where_it_is_unavailable_says_why_and_reads_nothing() -> None:
    harness = Harness(view=FakeView("pods", ("shop", "api-1")))

    harness.controller.open_selected()

    assert harness.ui.workers == []
    assert [n.severity for n in harness.ui.notifications] == ["warning"]


def test_opening_while_reads_are_held_reads_nothing() -> None:
    harness = Harness()
    harness.context.allowed = False

    harness.controller.open_selected()

    assert harness.ui.workers == []


async def test_a_newer_run_drops_an_older_result() -> None:
    harness = Harness()

    harness.controller.open_selected()
    harness.controller.open_selected()
    await harness.run(0)

    assert harness.ui.screens == []
    await harness.run(1)
    assert len(harness.ui.screens) == 1


async def test_a_context_switch_or_new_credentials_drop_the_result() -> None:
    switched = Harness()
    reauthenticated = Harness()

    switched.controller.open_selected()
    reauthenticated.controller.open_selected()
    switched.context.current = 2
    reauthenticated.client.generation = 4
    await switched.run()
    await reauthenticated.run()

    assert (switched.ui.screens, reauthenticated.ui.screens) == ([], [])


async def test_choosing_a_row_opens_its_object_in_the_run_epoch() -> None:
    harness = Harness()
    harness.controller.open_selected()
    await harness.run()

    assert harness.ui.screen_callback is not None
    harness.ui.screen_callback(None)
    assert len(harness.ui.workers) == 1
    harness.context.current = 2
    harness.ui.screen_callback(Destination("resourcequotas", "shop", "compute", "uid-q"))
    await harness.run()

    assert harness.navigations == [("resourcequotas", "shop", "compute", 1, "uid-q")]
    assert harness.ui.worker_groups[-1] == (CREATION_CONSTRAINTS_GROUP, False)


async def test_a_dialog_opened_during_the_reads_keeps_the_view_closed() -> None:
    harness = Harness()
    harness.controller.open_selected()
    harness.ui.screen_depth = lambda: 2  # type: ignore[method-assign]  # a dialog opened

    await harness.run()

    assert harness.ui.screens == []


async def test_leaving_the_view_or_the_selection_keeps_the_result_closed() -> None:
    moved = Harness()
    rescoped = Harness()
    reselected = Harness()

    for harness in (moved, rescoped, reselected):
        harness.controller.open_selected()
    moved.view.kind = "pods"
    rescoped.view.scope = "other"
    reselected.view.selected = ("shop", "other")
    for harness in (moved, rescoped, reselected):
        await harness.run()

    assert (moved.ui.screens, rescoped.ui.screens, reselected.ui.screens) == ([], [], [])


async def test_the_worker_group_is_cancelled_on_request() -> None:
    harness = Harness()

    await harness.controller.cancel_workers()

    assert harness.ui.cancelled_groups == [CREATION_CONSTRAINTS_GROUP]
```

Create `src/korvid/ui/creation_constraints_controller.py` with:

```python
"""Open the workload creation constraints view for the selected workload (#411).

One exclusive worker reads the workload, its children, the Events and the
namespace's quotas and LimitRanges through `explain_creation`, under a run
token. The view opens only while that token is current: a context switch,
new credentials or a newer run drops the result, as `RunTracker` decides. So
does another dialog that is open when the reads finish, or a change of view,
scope or selection since they started. The context-switch coordinator
cancels the worker group before it swaps the client.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from korvid.core.api_versions import observe_discovery
from korvid.core.capability import RunToken
from korvid.core.creation_constraints import CreationTarget, explain_creation
from korvid.core.creation_workloads import WORKLOAD_KINDS
from korvid.core.run_tokens import RunTracker
from korvid.k8s.evidence_requests import EvidenceClient
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.creation_constraints_screen import CreationConstraintsScreen, Destination
from korvid.ui.workspace_controller import ContextGuard

CREATION_CONSTRAINTS_GROUP = "creation-constraints"
_UNCONNECTED = UnavailableReason(
    AvailabilityCode.MISSING_CAPABILITY, "Workload creation failures need a cluster connection"
)
_WRONG_VIEW = UnavailableReason(
    AvailabilityCode.WRONG_VIEW,
    "Open Deployments, ReplicaSets, StatefulSets, DaemonSets, Jobs or CronJobs to explain "
    "creation failures",
)
_NO_SELECTION = UnavailableReason(AvailabilityCode.NO_SELECTION, "Select a workload")

#: Opens an object in its table: (view alias, namespace, name, epoch, expected UID).
ObjectNavigator = Callable[[str, str, str, int, str | None], Awaitable[None]]
#: The view, scope and (namespace, name) selection a run was started from.
_Origin = tuple[str, str, tuple[str | None, str | None]]


def _now() -> datetime:
    return datetime.now(UTC)


class CreationConstraintsController:
    """Runs the creation diagnostic and shows its result while it is current."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        view: ViewState,
        context: ContextGuard,
        client: EvidenceClient | None,
        navigate: ObjectNavigator,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._ui = ui
        self._view = view
        self._context = context
        self._client = client
        self._navigate = navigate
        self._clock = clock
        self._tracker = RunTracker(
            epoch=context.epoch,
            session_generation=lambda: client.session_generation if client is not None else 0,
            switching=context.switching,
        )

    def _selected_kind(self) -> str | None:
        meta = self._view.aliases().get(self._view.canonical_kind(self._view.current_kind()))
        workload = WORKLOAD_KINDS.get(meta.kind) if meta is not None else None
        if meta is None or workload is None or meta.group != workload.group:
            return None
        return meta.kind

    def unavailable_reason(self) -> UnavailableReason | None:
        """Why the view cannot open now, or None.

        Only for a missing connection, the wrong view or no selection: a
        source the cluster refuses never disables it, the view says so.
        """
        if self._client is None:
            return _UNCONNECTED
        if self._selected_kind() is None:
            return _WRONG_VIEW
        namespace, name = self._view.selected_ns_name(notify=False)
        if not namespace or not name:
            return _NO_SELECTION
        return None

    def open_selected(self) -> None:
        """Explain the selected workload's creation failures in a worker."""
        reason = self.unavailable_reason()
        if reason is not None:
            self._ui.notify(reason.message, severity="warning", markup=False)
            return
        kind = self._selected_kind()
        namespace, name = self._view.selected_ns_name()
        if not self._context.reads_allowed() or kind is None or not namespace or not name:
            return
        target = CreationTarget(kind, namespace, name, self._view.selected_uid(namespace, name))
        token = self._tracker.next_run()
        self._ui.run_worker(
            self._explain(target, token, self._context.epoch(), self._origin()),
            exclusive=True,
            group=CREATION_CONSTRAINTS_GROUP,
            exit_on_error=False,
        )

    def _origin(self) -> _Origin:
        """The view, scope and selected row a run explains."""
        return (
            self._view.current_kind(),
            self._view.current_scope(),
            self._view.selected_ns_name(notify=False),
        )

    async def _explain(
        self, target: CreationTarget, token: RunToken, epoch: int, origin: _Origin
    ) -> None:
        client = self._client
        if client is None:
            return
        snapshot = observe_discovery(await client.discovery_snapshot())
        report = await explain_creation(client, snapshot, target, token=token, clock=self._clock)
        # Another dialog is open, or the user has moved on, as the reads finish.
        if self._ui.screen_depth() > 1 or self._origin() != origin:
            return
        screen = CreationConstraintsScreen(report, self._clock)
        self._tracker.apply_if_current(
            token,
            lambda: self._ui.push_screen(screen, lambda chosen: self._open(chosen, epoch)),
        )

    def _open(self, destination: Destination | None, epoch: int) -> None:
        if destination is None:
            return
        self._ui.run_worker(
            self._navigate(
                destination.kind, destination.namespace, destination.name, epoch, destination.uid
            ),
            group=CREATION_CONSTRAINTS_GROUP,
            exit_on_error=False,
        )

    async def cancel_workers(self) -> None:
        """Stop a run holding the old client, before a context switch swaps it."""
        await self._ui.cancel_workers(CREATION_CONSTRAINTS_GROUP)
```

- [ ] Record `exclusive` in `FakeUiSurface.run_worker` (`tests/ui/test_session_timeline_controller.py`) as `exclusive_workers`.
- [ ] Run the controller tests before and after, then commit: `Open the creation constraints view from the selected workload`.

### Task 3: The command and the wiring

- [ ] Add tests: `test_creation_failures_reach_their_owner_once` and `test_creation_failures_without_owner_warn_unavailable` (`tests/ui/test_command_router.py`), `test_composed_command_reasons_ask_the_creation_owner_about_blocked` (`tests/ui/test_action_policy.py`), the coordinator's cancel order (`tests/ui/test_context_switch_coordinator.py`), and `tests/ui/test_worker_failures.py`.
- [ ] Add `BuiltinOperation.CREATION_FAILURES = "blocked"`; a `CommandDescriptor` with help `Pods not created for a workload: recorded failures, quotas, LimitRanges` and palette entry `Explain workload creation failures`; `CreationCommands` and `_open_creation` in the router; `creation` in `compose_command_reasons` (key `blocked`); `cancel_worker_group(CREATION_CONSTRAINTS_GROUP)` after the propagation group; the worker-failure label `Workload creation failure explanation failed`; and the controller in `__main__.py`, navigating with `jump_to_object(kind, namespace, name, epoch=epoch, expected_uid=uid)`.
- [ ] Run the router, policy, coordinator, wiring and palette tests, then commit: `Wire :blocked to the creation constraints view`.

### Task 4: End to end

Create `tests/ui/test_creation_constraints_workflow.py` with:

```python
"""`:blocked` end to end: a Deployment row, its recorded failures, and their ReplicaSet (#411)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
)
from korvid.k8s.evidence_requests import EvidenceClient, EvidencePage, EvidenceTarget
from korvid.k8s.models import GenericSummary
from korvid.ui.widgets.creation_constraints_screen import CreationConstraintsScreen
from tests.app_factory import build_test_app
from tests.ui.waits import screen_ready, until

DEPLOYMENTS = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
REPLICA_SETS = ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True)
EXCEEDED = 'pods "api-7d9f-x" is forbidden: exceeded quota: compute, requested: pods=1'
_SERVED = {
    ("", "v1"): ("resourcequotas", "limitranges", "events"),
    ("apps", "v1"): ("deployments", "replicasets"),
}


class Client(EvidenceClient):
    """Serves one Deployment whose ReplicaSet records a quota rejection."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, str]] = []

    @property
    def session_generation(self) -> int:
        return 1

    async def discovery_snapshot(self) -> RawDiscovery:
        outcomes = tuple(
            GroupVersionOutcome(
                group, version, tuple(ServedResource(r, frozenset({"get", "list"})) for r in names)
            )
            for (group, version), names in _SERVED.items()
        )
        return RawDiscovery(
            1,
            datetime.now(UTC),
            0.0,
            RootOutcome("/api", (("", "v1"),)),
            RootOutcome("/apis", (("apps", "v1"),)),
            outcomes,
        )

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.reads.append(("list", target.resource))
        if target.resource == "replicasets":
            owner = [{"uid": "uid-api", "controller": True}]
            rs = {
                "metadata": {
                    "name": "api-7d9f",
                    "namespace": "default",
                    "uid": "uid-rs",
                    "ownerReferences": owner,
                }
            }
            return EvidencePage((rs,))
        if target.resource == "events":
            event = {
                "metadata": {"name": "e", "namespace": "default", "uid": "uid-e"},
                "reason": "FailedCreate",
                "involvedObject": {"kind": "ReplicaSet", "name": "api-7d9f", "uid": "uid-rs"},
                "message": EXCEEDED,
            }
            return EvidencePage((event,))
        if target.resource == "resourcequotas":
            quota = {
                "metadata": {"name": "compute", "namespace": "default", "uid": "uid-q"},
                "spec": {"hard": {"pods": "0"}},
                "status": {"hard": {"pods": "0"}, "used": {"pods": "0"}},
            }
            return EvidencePage((quota,))
        return EvidencePage(())

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        self.reads.append(("get", target.resource))
        template = {
            "spec": {
                "containers": [{"name": "app", "env": [{"name": "T", "value": "s3cr3t-literal"}]}]
            }
        }
        return {
            "metadata": {"name": name, "namespace": "default", "uid": "uid-api"},
            "spec": {"replicas": 1, "template": template},
            "status": {},
        }


async def _source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
    if kind == "deployments":
        yield ("ADDED", GenericSummary("api", "default", "Deployment", "", uid="uid-api"))
    if kind == "replicasets":
        yield ("ADDED", GenericSummary("api-7d9f", "default", "ReplicaSet", "", uid="uid-rs"))
    await asyncio.Event().wait()


async def _command(pilot: Any, text: str) -> None:
    await pilot.press("colon", *text, "enter")


def _app(client: EvidenceClient) -> Any:
    store = ResourceStore()
    return build_test_app(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source),
        aliases=build_alias_map([PODS_META, DEPLOYMENTS, REPLICA_SETS]),
        agent_available=False,
        evidence_client=client,
    )


async def test_a_deployments_failures_lead_to_their_replica_set() -> None:
    client = Client()
    app = _app(client)
    async with app.run_test() as pilot:
        await _command(pilot, "deployments")
        await until(
            pilot,
            lambda: app._view.selected_ns_name(notify=False) == ("default", "api"),
            label="Deployment selected",
        )

        await _command(pilot, "blocked")
        await until(pilot, lambda: screen_ready(app, CreationConstraintsScreen), label="view open")
        screen = app.screen
        assert isinstance(screen, CreationConstraintsScreen)
        (failure,) = screen.report.failures
        assert failure.rejection.quotas == ("compute",)
        assert "s3cr3t-literal" not in repr(screen.report)
        assert sorted(client.reads) == [
            ("get", "deployments"),
            ("list", "events"),
            ("list", "limitranges"),
            ("list", "replicasets"),
            ("list", "resourcequotas"),
        ]

        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "replicasets", label="ReplicaSet view")
        await until(
            pilot,
            lambda: app._view.selected_ns_name(notify=False) == ("default", "api-7d9f"),
            label="ReplicaSet selected",
        )


async def test_blocked_on_another_view_says_what_to_open() -> None:
    app = _app(Client())
    async with app.run_test() as pilot:
        await _command(pilot, "blocked")
        await until(
            pilot,
            lambda: any("Open Deployments, ReplicaSets" in n.message for n in app._notifications),
            label="reason shown",
        )
        assert not isinstance(app.screen, CreationConstraintsScreen)


class BrokenClient(Client):
    async def discovery_snapshot(self) -> RawDiscovery:
        raise RuntimeError("discovery exploded")


async def test_a_failed_run_is_reported_instead_of_vanishing() -> None:
    app = _app(BrokenClient())
    async with app.run_test() as pilot:
        await _command(pilot, "deployments")
        await until(
            pilot,
            lambda: app._view.selected_ns_name(notify=False) == ("default", "api"),
            label="Deployment selected",
        )
        await _command(pilot, "blocked")
        await until(
            pilot,
            lambda: any(
                "Workload creation failure explanation failed" in n.message
                and "discovery exploded" in n.message
                for n in app._notifications
            ),
            label="failure reported",
        )
        assert app.is_running
```

- [ ] Run it, then commit: `Cover :blocked end to end`.

### Task 5: The version matrix

- [ ] In `tests/version_matrix/fixtures.yaml`, add a `korvid-matrix-quota` namespace containing:
  - a `pods: "0"` ResourceQuota `matrix-pods`;
  - a Container LimitRange `matrix-limits`;
  - a one-replica Deployment `blocked` whose template carries `matrix-template-literal`.

  It is its own namespace, so the quota does not block the other fixtures.
- [ ] Add `test_a_quota_rejection_is_read_next_to_the_namespace_policies` to `tests/version_matrix/test_scenarios.py`. It polls `explain_creation` until a failure is recorded and the quota's status is current, up to 90 seconds.
- [ ] Add the `creation_*` modules and `k8s/events.py` to the workflow's path filters, then commit: `Add a version-matrix scenario for workload creation constraints`.

### Task 6: Document it

- [ ] Add a "Workload creation failures" section to `docs/tui.md`, the `:blocked` row to `docs/keybindings.md`, the scenario to `docs/kubernetes-support.md` and an entry to `docs/release-notes/unreleased.md`, then commit: `Document :blocked`.

### Task 7: Run the full gate

- [ ] Run `make check`, or behind the mirror the same chain with the venv's tools: `scripts/check_source_size.py`, `ruff check`, `ruff format --check`, `mypy src/`, `tach check`, `pytest`.
- [ ] Mutation-check each branch of the screen's rows and notes and the controller's guards. The mutation helper runs the unmutated tests first and refuses to report on a failing baseline.
