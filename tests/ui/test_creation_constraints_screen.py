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
    assert row.destination == Destination("apps", "replicasets", "shop", "api-7d9f", "uid-rs")


def test_a_condition_failure_and_an_undated_one_are_labelled() -> None:
    condition = replace(
        FAILURE, subject=DEPLOYMENT, source=FailureSource.CONDITION, count=1, at=None
    )

    (row, *_) = _rows(_report(failures=(condition,)))

    assert row.cells[:2] == ("Deployment api (condition)", "once · undated")
    assert row.destination == Destination("apps", "deployments", "shop", "api", "uid-api")


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
        if row.destination and row.destination.resource == "resourcequotas"
    ]

    assert [row.cells for row in rows] == [
        ("compute", "compute: requests.cpu", "used 2 / hard 2", "at its limit"),
        ("", "object counts: pods", "used 20 / hard 20", "at its limit"),
        ("", "object counts: services", "used 1 / hard 5", ""),
        ("", "scopes", "NotTerminating; PriorityClass In gold, silver", ""),
    ]
    assert {row.destination for row in rows} == {
        Destination("", "resourcequotas", "shop", "compute", "uid-q")
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

    assert app.result == [Destination("", "resourcequotas", "shop", "compute", "uid-q")]


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


async def test_a_multiline_message_is_shown_in_full() -> None:
    multiline = replace(FAILURE, rejection=classify("first line\nsecond line\nthird line"))
    app = _Host(
        CreationConstraintsScreen(
            _report(failures=(multiline,), quotas=(), limit_ranges=()), lambda: NOW
        )
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, CreationConstraintsScreen), label="view open")
        table = app.screen.query_one(DataTable)
        row_key = table.coordinate_to_cell_key((1, 0)).row_key
        assert table.rows[row_key].height == 3
