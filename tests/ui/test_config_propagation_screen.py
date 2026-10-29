"""The configuration propagation view (#410): rows, notes and keys."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rich.text import Text
from textual.app import App
from textual.widgets import DataTable, Static

from korvid.core.capability import (
    ApiSource,
    Baseline,
    Basis,
    EvidenceState,
    Observation,
    Projection,
    SourceScope,
    Verb,
)
from korvid.core.config_consumers import (
    Consumer,
    ConsumptionMode,
    ContainerCategory,
    PodIdentity,
)
from korvid.core.config_propagation import ConfigTarget, PropagationReport
from korvid.ui.widgets.config_propagation_screen import (
    ConfigPropagationScreen,
    ConsumerRow,
    Heading,
    evidence_lines,
    report_notes,
    table_rows,
)

from .waits import screen_ready, until

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
TARGET = ConfigTarget("ConfigMap", "team-a", "settings", "uid-settings")
SCOPE = SourceScope.namespace("team-a")
OBJECT = ApiSource(
    "",
    "configmaps",
    SCOPE,
    "v1",
    Verb.GET,
    projection=Projection.METADATA,
    name="settings",
    expected_uid="uid-settings",
)
PODS = ApiSource("", "pods", SCOPE, "v1", Verb.LIST)
POD = PodIdentity("team-a", "api-1", "uid-api-1", "ReplicaSet", "api-7d9f")


def _seen(
    source: ApiSource, basis: Basis = Basis.OBSERVED, observed_uid: str | None = None
) -> Observation:
    state = {
        Basis.OBSERVED: EvidenceState.AVAILABLE,
        Basis.OBJECT_ABSENT: EvidenceState.AVAILABLE,
        Basis.TRUNCATED: EvidenceState.PARTIAL,
        Basis.FORBIDDEN: EvidenceState.DENIED,
    }[basis]
    return Observation(source, state, basis, NOW - timedelta(seconds=2), observed_uid=observed_uid)


def _report(
    *consumers: Consumer,
    object_basis: Basis = Basis.OBSERVED,
    pods_basis: Basis = Basis.OBSERVED,
    observed_uid: str | None = "uid-settings",
) -> PropagationReport:
    uid = observed_uid if object_basis is Basis.OBSERVED else None
    return PropagationReport(
        TARGET,
        _seen(OBJECT, object_basis, uid),
        _seen(PODS, pods_basis),
        consumers,
        Baseline(EvidenceState.AVAILABLE),
    )


ENV = Consumer(
    POD,
    ConsumptionMode.ENV_FROM,
    "spec.containers[0].envFrom[0].configMapRef",
    False,
    "app",
    ContainerCategory.CONTAINER,
)
KEY = Consumer(
    POD,
    ConsumptionMode.ENV_KEY,
    "spec.containers[0].env[1].valueFrom.configMapKeyRef",
    True,
    "app",
    ContainerCategory.CONTAINER,
    env_name="LOG_LEVEL",
    key="log-level",
)
VOLUME = Consumer(
    POD,
    ConsumptionMode.VOLUME,
    "spec.volumes[0].configMap",
    False,
    "app",
    ContainerCategory.CONTAINER,
    volume="config",
    mount_path="/etc/app",
)
SUBPATH = Consumer(
    POD,
    ConsumptionMode.SUBPATH,
    "spec.volumes[0].configMap",
    False,
    "setup",
    ContainerCategory.INIT,
    volume="config",
    mount_path="/etc/app.yaml",
    sub_path="app.yaml",
)
UNMOUNTED = Consumer(POD, ConsumptionMode.UNMOUNTED, "spec.volumes[1].configMap", volume="spare")


def _plain(lines: list[Text]) -> list[str]:
    return [line.plain for line in lines]


def test_rows_are_grouped_by_mode_under_one_heading_each() -> None:
    rows = table_rows((VOLUME, ENV, SUBPATH, KEY))

    assert [type(row).__name__ for row in rows] == [
        "Heading",
        "ConsumerRow",
        "ConsumerRow",
        "Heading",
        "ConsumerRow",
        "Heading",
        "ConsumerRow",
    ]
    headings = [row.text for row in rows if isinstance(row, Heading)]
    assert headings[0].startswith("Environment variables — read when the container starts")
    assert headings[1].startswith("Volumes — updated in place by the kubelet, eventually")
    assert "unless the object is immutable" in headings[1]
    assert headings[2].startswith("subPath mounts — never updated")
    assert [row.consumer for row in rows if isinstance(row, ConsumerRow)] == [
        ENV,
        KEY,
        VOLUME,
        SUBPATH,
    ]


def test_a_row_names_the_pod_container_and_reference_but_no_value() -> None:
    rows = {
        row.consumer: row.cells
        for row in table_rows((KEY, VOLUME, SUBPATH, UNMOUNTED))
        if isinstance(row, ConsumerRow)
    }

    assert rows[KEY] == ("api-1", "app", "env[1] LOG_LEVEL ← key log-level (optional)")
    assert rows[VOLUME] == ("api-1", "app", "volume config → /etc/app")
    assert rows[SUBPATH] == (
        "api-1",
        "setup (init container)",
        "volume config → /etc/app.yaml (subPath app.yaml)",
    )
    assert rows[UNMOUNTED] == ("api-1", "— (Pod)", "volume spare")


def test_the_evidence_lines_come_from_the_shared_renderer() -> None:
    lines = _plain(evidence_lines(_report(ENV), NOW))

    assert lines == [
        "configmaps v1 in namespace team-a: available — read · observed 2s ago",
        "pods v1 in namespace team-a: available — read · observed 2s ago",
    ]


def test_no_consumer_is_said_only_when_the_pod_list_is_conclusive() -> None:
    complete = _plain(report_notes(_report(), NOW))
    truncated = _plain(report_notes(_report(pods_basis=Basis.TRUNCATED), NOW))
    denied = _plain(report_notes(_report(pods_basis=Basis.FORBIDDEN), NOW))

    assert any(
        n.startswith("No Pod in namespace team-a references this ConfigMap") for n in complete
    )
    (none,) = [n for n in complete if n.startswith("No Pod")]
    assert "configMap, secret or projected volumes" in none
    assert "CSI and other volume plugins are not checked" in none
    assert not any(n.startswith("No Pod") for n in truncated + denied)
    assert (
        "Not established: whether every consumer is listed: stopped at the page limit" in truncated
    )
    assert (
        "Not established: whether every consumer is listed: your permissions do not allow this read"
    ) in denied


def test_immutability_is_always_not_established() -> None:
    notes = _plain(report_notes(_report(ENV), NOW))

    assert (
        "Not established: whether the ConfigMap is immutable (only its metadata is read)" in notes
    )


def test_a_gone_or_recreated_object_is_said() -> None:
    gone = _plain(report_notes(_report(ENV, object_basis=Basis.OBJECT_ABSENT), NOW))
    recreated = _plain(report_notes(_report(VOLUME, observed_uid="uid-new"), NOW))

    assert any("no longer exists" in n for n in gone)
    assert any("was recreated" in n and "volume mounts" in n for n in recreated)
    assert not any("was recreated" in n for n in _plain(report_notes(_report(VOLUME), NOW)))


def test_an_absence_that_has_aged_is_no_longer_called_a_deletion() -> None:
    report = _report(ENV, object_basis=Basis.OBJECT_ABSENT)

    fresh = _plain(report_notes(report, NOW))
    aged = _plain(report_notes(report, NOW + timedelta(minutes=5)))

    assert any("no longer exists" in n for n in fresh)
    assert not any("no longer exists" in n for n in aged)
    assert any(
        "did not exist when it was read" in n and "may have been created since" in n for n in aged
    )


def test_an_ephemeral_container_row_explains_a_restart_will_not_help() -> None:
    debugger = Consumer(
        POD,
        ConsumptionMode.ENV_FROM,
        "spec.ephemeralContainers[0].envFrom[0].configMapRef",
        False,
        "debugger",
        ContainerCategory.EPHEMERAL,
    )

    mounted = Consumer(
        POD,
        ConsumptionMode.VOLUME,
        "spec.volumes[0].configMap",
        False,
        "debugger",
        ContainerCategory.EPHEMERAL,
        volume="config",
        mount_path="/etc/app",
    )

    with_ephemeral = _plain(report_notes(_report(debugger), NOW))
    without = _plain(report_notes(_report(ENV), NOW)) + _plain(report_notes(_report(mounted), NOW))

    (note,) = [n for n in with_ephemeral if n.startswith("An ephemeral container")]
    assert "cannot be restarted" in note
    assert "add a new ephemeral container" in note
    assert not any(n.startswith("An ephemeral container") for n in without)


def test_an_init_container_row_explains_when_it_runs() -> None:
    with_init = _plain(report_notes(_report(SUBPATH), NOW))
    without = _plain(report_notes(_report(ENV), NOW))

    assert any(n.startswith("An init container runs to completion") for n in with_init)
    assert not any(n.startswith("An init container") for n in without)


class _Host(App[None]):
    def __init__(self, screen: ConfigPropagationScreen) -> None:
        super().__init__()
        self.prompt = screen
        self.result: list[Consumer | None] = []

    def on_mount(self) -> None:
        self.push_screen(self.prompt, self.result.append)


async def test_enter_on_a_consumer_row_returns_that_consumer() -> None:
    app = _Host(ConfigPropagationScreen(_report(ENV, VOLUME), lambda: NOW))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, ConfigPropagationScreen), label="view open")
        table = app.screen.query_one(DataTable)
        assert table.cursor_row == 1
        await pilot.press("down", "down", "enter")
        await until(pilot, lambda: bool(app.result), label="view closed")

    assert app.result == [VOLUME]


async def test_enter_on_a_heading_does_nothing_and_escape_closes() -> None:
    app = _Host(ConfigPropagationScreen(_report(ENV), lambda: NOW))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, ConfigPropagationScreen), label="view open")
        await pilot.press("up", "enter")
        await pilot.pause()
        assert app.result == []
        await pilot.press("escape")
        await until(pilot, lambda: bool(app.result), label="view closed")

    assert app.result == [None]


async def test_the_title_shows_cluster_names_literally() -> None:
    hostile = ConfigTarget("ConfigMap", "team-a", "[bold]settings[/bold]")
    report = PropagationReport(
        hostile,
        _seen(OBJECT, observed_uid="uid-settings"),
        _seen(PODS),
        (),
        Baseline(EvidenceState.AVAILABLE),
    )
    app = _Host(ConfigPropagationScreen(report, lambda: NOW))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, ConfigPropagationScreen), label="view open")
        title = app.screen.query_one("#propagation-title", Static)
        assert "[bold]settings[/bold]" in str(title.render())


def test_a_list_that_has_aged_past_its_policy_no_longer_proves_absence() -> None:
    later = NOW + timedelta(minutes=5)

    notes = _plain(report_notes(_report(), later))

    assert not any(n.startswith("No Pod") for n in notes)
    assert "Not established: whether every consumer is listed: the Pod list is stale" in notes


async def test_an_open_view_ages_its_evidence_without_reading_again() -> None:
    now = [NOW]
    app = _Host(ConfigPropagationScreen(_report(), lambda: now[0], refresh_seconds=0.05))
    async with app.run_test() as pilot:
        await until(pilot, lambda: screen_ready(app, ConfigPropagationScreen), label="view open")
        evidence = app.screen.query_one("#propagation-evidence", Static)
        notes = app.screen.query_one("#propagation-notes", Static)
        assert "stale" not in str(evidence.render())
        assert "No Pod in namespace team-a" in str(notes.render())

        now[0] = NOW + timedelta(minutes=5)
        await until(pilot, lambda: "· stale" in str(evidence.render()), label="evidence aged")

        assert "observed 5m ago" in str(evidence.render())
        assert "No Pod in namespace team-a" not in str(notes.render())
        assert "the Pod list is stale" in str(notes.render())
