"""App-level integration for the read-only session timeline (issue #282,
Task 4): the `T` binding, opening without I/O (unlike the relationship
graph, `SessionTimeline.snapshot` is already in-memory, so there is no
loader worker to race), reuse of the existing `_jump_to_object` navigation
path, and the context-epoch guard that discards a goto dismissed after a
stale context switch.

Tasks 1-3 already cover the timeline's own bounds/eviction (Task 1) and the
app's producer wiring (Task 3); this module proves the integration
boundary Task 4 owns: the `T` keybinding, capturing the resource toggle
from the selected row, and translating a dismissed `TimelineGotoResult`
back into a real navigation.
"""

from __future__ import annotations

from textual.widgets import DataTable

from korvid.core.session_timeline import SessionTimeline
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.session_timeline_screen import SessionTimelineScreen

from .test_app import _deploy, _pod, make_app
from .waits import until


async def test_timeline_binding_opens_without_agent_and_enter_reuses_navigation() -> None:
    timeline = SessionTimeline(max_entries=16, max_bytes=8192)
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
    app = make_app(
        [_pod("web")], extra_data={"deployments": [_deploy("api")]}, session_timeline=timeline
    )
    # Timeline works fully independently of the AI agent — no agent
    # collaborator is wired here at all.
    assert app._rebuild_agent is None
    async with app.run_test() as pilot:
        await until(
            pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pods visible"
        )
        # The pod's own initial watch delta (issue #282 Task 3's producer
        # wiring) is recorded on its own timing, independent of the write
        # entry appended above - wait for it before opening the modal,
        # since the modal renders once at mount and never self-refreshes.
        await until(
            pilot,
            lambda: len(timeline.snapshot(epoch=0, source=None, resource=None).entries) == 2,
            label="write + watch entries recorded",
        )
        await pilot.press("T")
        await until(
            pilot, lambda: isinstance(app.screen, SessionTimelineScreen), label="timeline open"
        )
        table = app.screen.query_one(DataTable)
        # The write entry is always the row furthest down (lowest
        # sequence, since it was appended before the app - and its watch -
        # ever started), so moving to the last row stays deterministic
        # regardless of exactly when the watch delta landed.
        assert table.row_count == 2
        table.move_cursor(row=table.row_count - 1)
        await pilot.press("enter")
        await until(
            pilot, lambda: app.current_kind == "deployments", label="navigated to deployment view"
        )
        table = app.query_one(ResourceTable)
        assert any(str(row.key.value) == "default/api" for row in table.ordered_rows)
        assert not isinstance(app.screen, SessionTimelineScreen)


async def test_timeline_unavailable_without_a_session_timeline() -> None:
    """No `SessionTimeline` injected: `T` warns instead of opening a screen
    over an empty/absent feature."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod loaded")
        await pilot.press("T")
        await pilot.pause()
        assert not isinstance(app.screen, SessionTimelineScreen)
        assert any("Timeline unavailable in this session" in n.message for n in app._notifications)


async def test_r_toggle_pins_the_resource_selected_when_timeline_opened() -> None:
    """The resource toggle is captured from the table row selected at the
    moment `T` was pressed — not re-read afterwards. No entries are
    appended manually here: the harness's own producer wiring (issue #282
    Task 3) already records one watch delta per seeded pod, so asserting
    against those keeps the scenario honest about what a real session
    would actually see."""
    timeline = SessionTimeline(max_entries=16, max_bytes=8192)
    app = make_app([_pod("web"), _pod("other")], session_timeline=timeline)
    async with app.run_test() as pilot:
        await until(
            pilot, lambda: app.query_one(ResourceTable).row_count == 2, label="pods visible"
        )
        # Wait for both watch deltas to land on the timeline itself before
        # opening the modal: it renders once at mount and never
        # self-refreshes, so the row count it captures depends on what
        # arrived beforehand, not on anything discovered afterwards.
        await until(
            pilot,
            lambda: len(timeline.snapshot(epoch=0, source=None, resource=None).entries) == 2,
            label="both watch deltas recorded",
        )
        app.query_one(ResourceTable).move_cursor(row=0)  # "web" row
        await pilot.press("T")
        await until(
            pilot, lambda: isinstance(app.screen, SessionTimelineScreen), label="timeline open"
        )
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2
        await pilot.press("r")
        await until(pilot, lambda: table.row_count == 1, label="pinned to the selected pod")


async def test_context_epoch_guard_discards_stale_modal_navigation() -> None:
    """A context switch that lands (bumping `_ctx_epoch`) while the modal
    was open must discard a goto dismissed after it — the same invariant
    `_jump_to_object`'s own epoch guard already enforces for every other
    goto-style navigation (relationship graph, hierarchy tree)."""
    timeline = SessionTimeline(max_entries=16, max_bytes=8192)
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
    app = make_app(
        [_pod("web")], extra_data={"deployments": [_deploy("api")]}, session_timeline=timeline
    )
    async with app.run_test() as pilot:
        await until(
            pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pods visible"
        )
        await pilot.press("T")
        await until(
            pilot, lambda: isinstance(app.screen, SessionTimelineScreen), label="timeline open"
        )
        app._ctx_epoch += 1  # simulate a context switch that landed while open
        await pilot.press("enter")
        await pilot.pause()
        assert app.current_kind != "deployments"
        assert any("timeline navigation cancelled" in n.message for n in app._notifications)


async def test_timeline_navigation_failure_renders_event_name_literally() -> None:
    app = make_app([_pod("web")], session_timeline=SessionTimeline(8, 4096))
    app._workspace_ctl._jump_poll_attempts = 1  # shrink the give-up window for the test
    async with app.run_test() as pilot:
        await until(
            pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pods visible"
        )
        await app._workspace_ctl.jump_to_object("pods", "default", "[/bold]", epoch=app._ctx_epoch)
        await until(
            pilot,
            lambda: any("is not visible" in item.message for item in app._notifications),
            label="missing object notified",
        )
        notification = next(item for item in app._notifications if "is not visible" in item.message)
        assert "[/bold]" in notification.message
        assert notification.markup is False
