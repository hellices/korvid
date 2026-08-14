"""Tests for the read-only session timeline screen (issue #282, Task 4).

`SessionTimelineScreen` renders one `SessionTimeline`'s bounded, already
in-memory snapshot as a keyboard-navigable table. It performs no I/O and
holds no state beyond the toggled filters (epoch/source/resource) — the
caller (the app) owns building the `SessionTimeline` and reopening this
screen with a fresh `current_epoch`/`resource_toggle` on every `T` press.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static

from korvid.core.session_timeline import SessionTimeline, TimelineResourceRef
from korvid.ui.widgets.session_timeline_screen import SessionTimelineScreen

from .waits import until


class HostApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: object = "unset"

    def compose(self) -> ComposeResult:
        yield Static("host")


def _all_cells(table: DataTable[object]) -> list[str]:
    return [str(cell) for index in range(table.row_count) for cell in table.get_row_at(index)]


# ---------------------------------------------------------------------------
# Rendering and filters
# ---------------------------------------------------------------------------


async def test_screen_defaults_to_current_epoch_only() -> None:
    """The default filter is `epoch=current_epoch`, `source=all`, `resource=all`."""
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    timeline.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="old",
        uid="uid-old",
        verb="ADDED",
    )
    timeline.append_watch(
        epoch=1,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="new",
        uid="uid-new",
        verb="ADDED",
    )
    timeline.append_context_switch(
        epoch=1, phase="completed", from_context="ctx-a", to_context="ctx-b"
    )
    app = HostApp()
    screen = SessionTimelineScreen(
        timeline,
        current_epoch=1,
        resource_toggle=TimelineResourceRef("pods", "Pod", "default", "new", "uid-new"),
    )
    async with app.run_test():
        await app.push_screen(screen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2  # only the two epoch=1 entries


async def test_s_cycles_source_deterministically() -> None:
    """`s` cycles all -> watch -> event -> context -> write -> all."""
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    timeline.append_watch(
        epoch=1,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="new",
        uid="uid-new",
        verb="ADDED",
    )
    timeline.append_context_switch(
        epoch=1, phase="completed", from_context="ctx-a", to_context="ctx-b"
    )
    app = HostApp()
    screen = SessionTimelineScreen(timeline, current_epoch=1, resource_toggle=None)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2
        await pilot.press("s")  # -> watch only
        await until(pilot, lambda: table.row_count == 1, label="watch-only rows")
        await pilot.press("s")  # -> event only (none recorded)
        await until(pilot, lambda: table.row_count == 0, label="event-only rows")
        await pilot.press("s")  # -> context only
        await until(pilot, lambda: table.row_count == 1, label="context-only rows")
        await pilot.press("s")  # -> write only (none recorded)
        await until(pilot, lambda: table.row_count == 0, label="write-only rows")
        await pilot.press("s")  # -> back to all
        await until(pilot, lambda: table.row_count == 2, label="all rows again")


async def test_e_toggles_current_epoch_vs_all_epochs() -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    timeline.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="old",
        uid="uid-old",
        verb="ADDED",
    )
    timeline.append_watch(
        epoch=1,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="new",
        uid="uid-new",
        verb="ADDED",
    )
    app = HostApp()
    screen = SessionTimelineScreen(timeline, current_epoch=1, resource_toggle=None)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 1  # current epoch only
        await pilot.press("e")
        await until(pilot, lambda: table.row_count == 2, label="all epochs")
        await pilot.press("e")
        await until(pilot, lambda: table.row_count == 1, label="back to current epoch")


async def test_r_toggles_the_captured_resource_filter() -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    timeline.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="a",
        uid="uid-a",
        verb="ADDED",
    )
    timeline.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="b",
        uid="uid-b",
        verb="ADDED",
    )
    app = HostApp()
    screen = SessionTimelineScreen(
        timeline,
        current_epoch=0,
        resource_toggle=TimelineResourceRef("pods", "Pod", "default", "a", "uid-a"),
    )
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2
        await pilot.press("r")
        await until(pilot, lambda: table.row_count == 1, label="pinned resource only")
        await pilot.press("r")
        await until(pilot, lambda: table.row_count == 2, label="all resources again")


async def test_r_is_inert_without_a_captured_resource() -> None:
    """No row was selected when the modal opened: `r` has nothing to pin."""
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    timeline.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="a",
        uid="uid-a",
        verb="ADDED",
    )
    app = HostApp()
    screen = SessionTimelineScreen(timeline, current_epoch=0, resource_toggle=None)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        table = app.screen.query_one(DataTable)
        await pilot.press("r")
        await pilot.pause()
        assert table.row_count == 1
        status = str(app.screen.query_one("#timeline-status", Static).render())
        assert "nothing to toggle" in status.lower()


async def test_banner_shows_stats_including_zero_evicted_and_refused() -> None:
    """Caps must never be silent: the banner always shows the counters."""
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    timeline.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="a",
        uid="uid-a",
        verb="ADDED",
    )
    app = HostApp()
    screen = SessionTimelineScreen(timeline, current_epoch=0, resource_toggle=None)
    async with app.run_test():
        await app.push_screen(screen)
        banner = str(app.screen.query_one("#timeline-banner", Static).render())
        assert "evicted=0" in banner
        assert "refused=0" in banner
        assert "stored=1" in banner


async def test_banner_reports_nonzero_evicted_and_refused() -> None:
    timeline = SessionTimeline(max_entries=1, max_bytes=4096)
    timeline.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="a",
        uid="uid-a",
        verb="ADDED",
    )
    timeline.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="b",
        uid="uid-b",
        verb="ADDED",
    )
    over_budget = SessionTimeline(max_entries=8, max_bytes=1)
    over_budget.append_watch(
        epoch=0,
        kind_alias="pods",
        display_kind="Pod",
        namespace="default",
        name="c",
        uid="uid-c",
        verb="ADDED",
    )
    app = HostApp()
    screen = SessionTimelineScreen(timeline, current_epoch=0, resource_toggle=None)
    async with app.run_test():
        await app.push_screen(screen)
        banner = str(app.screen.query_one("#timeline-banner", Static).render())
        assert "evicted=1" in banner

    app2 = HostApp()
    screen2 = SessionTimelineScreen(over_budget, current_epoch=0, resource_toggle=None)
    async with app2.run_test():
        await app2.push_screen(screen2)
        banner2 = str(app2.screen.query_one("#timeline-banner", Static).render())
        assert "refused=1" in banner2


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


async def test_enter_dismisses_goto_for_navigable_row() -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    timeline.append_write(
        epoch=0,
        action="delete",
        kind_alias="deployments",
        display_kind="Deployment",
        namespace="default",
        name="api",
        uid=None,
        outcome="success",
    )
    app = HostApp()
    screen = SessionTimelineScreen(timeline, current_epoch=0, resource_toggle=None)
    async with app.run_test() as pilot:
        await app.push_screen(screen, lambda value: setattr(app, "result", value))
        await pilot.press("enter")
        assert app.result == ("goto", "deployments", "default", "api")


async def test_enter_on_non_resource_row_is_inert_and_updates_status() -> None:
    """A context-switch entry has no resource — Enter must not navigate."""
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    timeline.append_context_switch(
        epoch=0, phase="completed", from_context="ctx-a", to_context="ctx-b"
    )
    app = HostApp()
    screen = SessionTimelineScreen(timeline, current_epoch=0, resource_toggle=None)
    async with app.run_test() as pilot:
        await app.push_screen(screen, lambda value: setattr(app, "result", value))
        await pilot.press("enter")
        assert app.result == "unset"  # never dismissed
        assert app.screen is screen
        status = str(app.screen.query_one("#timeline-status", Static).render())
        assert "no navigable resource" in status.lower()


async def test_escape_dismisses_with_none() -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    app = HostApp()
    screen = SessionTimelineScreen(timeline, current_epoch=0, resource_toggle=None)
    async with app.run_test() as pilot:
        await app.push_screen(screen, lambda value: setattr(app, "result", value))
        await pilot.press("escape")
        assert app.result is None


# ---------------------------------------------------------------------------
# Literal rendering — cluster-controlled fields must never be Rich markup
# ---------------------------------------------------------------------------


async def test_cluster_controlled_fields_render_literally() -> None:
    """`occurred_at`, `reason`/`note`, and resource identifiers can contain
    text that looks like Rich markup (an attacker-controlled Warning event
    reason, or a namespace/name); it must render as the literal string."""
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    timeline.append_warning_event(
        epoch=0,
        kind_alias="pods",
        event={
            "reason": "[red]BackOff[/]",
            "message": "[bold]pull failed[/bold]",
            "count": 1,
            "lastTimestamp": "[blue]2026-01-01T00:00:00Z[/]",
            "involvedObject": {
                "apiVersion": "v1",
                "kind": "Pod",
                "namespace": "[green]default[/]",
                "name": "[cyan]api[/]",
                "uid": "uid-1",
            },
        },
    )
    app = HostApp()
    screen = SessionTimelineScreen(timeline, current_epoch=0, resource_toggle=None)
    async with app.run_test():
        await app.push_screen(screen)
        table = app.screen.query_one(DataTable)
        cells = _all_cells(table)
        assert "[red]BackOff[/]" in "\n".join(cells)
        assert "[green]default[/]/[cyan]api[/]" in "\n".join(cells) or any(
            "[green]default[/]" in cell and "[cyan]api[/]" in cell for cell in cells
        )
        assert "[blue]2026-01-01T00:00:00Z[/]" in cells
