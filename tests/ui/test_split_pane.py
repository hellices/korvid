"""2-pane split workspace (issue #48).

`ctrl+w v` splits the workspace into two side-by-side resource tables;
each pane holds an independent view (kind/namespace/filter). `ctrl+w w`
moves focus between panes - the focused pane receives `:` commands,
filters and keybindings. `ctrl+w q` closes the focused pane back to the
single view. Layout is session-only.
"""

from korvid.ui.widgets.resource_table import ResourceTable

from .test_app import _deploy, _pod, make_app
from .waits import until


async def _first_render(app, pilot) -> None:  # type: ignore[no-untyped-def]  # test helper
    await until(
        pilot,
        lambda: app.query_one("#pane-0", ResourceTable).row_count > 0,
        label="first table render",
    )


async def _split(app, pilot) -> None:  # type: ignore[no-untyped-def]  # test helper
    await pilot.press("ctrl+w", "v")
    await until(pilot, lambda: len(app.query(ResourceTable)) == 2, label="second pane mounted")


async def _type_command(pilot, text: str) -> None:  # type: ignore[no-untyped-def]  # test helper
    await pilot.press("colon")
    for ch in text:
        await pilot.press(ch if ch != " " else "space")
    await pilot.press("enter")


async def test_split_mounts_second_pane_with_same_view() -> None:
    """`ctrl+w v` clones the focused view: same kind, same rows."""
    app = make_app([_pod("api-1"), _pod("api-2")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        second = app.query_one("#pane-1", ResourceTable)
        await until(pilot, lambda: second.row_count == 2, label="clone rendered")
        assert second.row_count == 2


async def test_new_pane_is_focused_and_receives_commands() -> None:
    """After the split, `:deploy` applies to the new pane only - the
    original keeps showing pods."""
    app = make_app([_pod("api-1")], extra_data={"deployments": [_deploy("web")]})
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await _type_command(pilot, "deploy")
        second = app.query_one("#pane-1", ResourceTable)
        await until(
            pilot,
            lambda: app.current_kind == "deployments" and second.row_count == 1,
            label="pane 2 switched to deployments",
        )
        first = app.query_one("#pane-0", ResourceTable)
        assert first.row_count == 1  # api-1 pod untouched
        assert app._workspace.panes[0].kind == "pods"


async def test_focus_switch_routes_commands_to_other_pane() -> None:
    """`ctrl+w w` returns focus to pane 1; the next command changes pane 1
    while pane 2's view survives."""
    app = make_app([_pod("api-1")], extra_data={"deployments": [_deploy("web")]})
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await _type_command(pilot, "deploy")
        await until(pilot, lambda: app.current_kind == "deployments", label="pane 2 on deploys")
        await pilot.press("ctrl+w", "w")
        await until(pilot, lambda: app.current_kind == "pods", label="focus back on pane 1")
        assert app._workspace.focused_index == 0
        await _type_command(pilot, "deploy all")
        await until(
            pilot,
            lambda: app._workspace.panes[0].kind == "deployments",
            label="pane 1 switched",
        )
        assert app._workspace.panes[1].kind == "deployments"
        assert app._workspace.panes[1].scope == "default"  # pane 2 untouched by pane 1 nav
        assert app.current_scope == "*"


async def test_filter_applies_to_focused_pane_only() -> None:
    app = make_app([_pod("api-1"), _pod("checkout-2")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        second = app.query_one("#pane-1", ResourceTable)
        await until(pilot, lambda: second.row_count == 2, label="clone rendered")
        await pilot.press("slash")
        for ch in "check":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(pilot, lambda: second.row_count == 1, label="pane 2 filtered")
        first = app.query_one("#pane-0", ResourceTable)
        assert first.row_count == 2  # pane 1 sees the unfiltered rows


async def test_close_returns_to_single_view_keeping_other_pane() -> None:
    """`ctrl+w q` on the focused (second) pane: back to one table showing
    the surviving pane's view."""
    app = make_app([_pod("api-1")], extra_data={"deployments": [_deploy("web")]})
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await _type_command(pilot, "deploy")
        await until(pilot, lambda: app.current_kind == "deployments", label="pane 2 on deploys")
        await pilot.press("ctrl+w", "q")
        await until(pilot, lambda: len(app.query(ResourceTable)) == 1, label="back to one pane")
        assert app.current_kind == "pods"  # surviving pane 1 view
        assert len(app._workspace.panes) == 1


async def test_closing_first_pane_moves_second_into_place() -> None:
    app = make_app([_pod("api-1")], extra_data={"deployments": [_deploy("web")]})
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await _type_command(pilot, "deploy")
        await until(pilot, lambda: app.current_kind == "deployments", label="pane 2 on deploys")
        await pilot.press("ctrl+w", "w")  # focus pane 1 (pods)
        await until(pilot, lambda: app.current_kind == "pods", label="focus on pane 1")
        await pilot.press("ctrl+w", "q")  # close pane 1
        await until(pilot, lambda: len(app.query(ResourceTable)) == 1, label="back to one pane")
        assert app.current_kind == "deployments"  # pane 2's view took over
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="deployments rendered")


async def test_shared_watch_survives_closing_a_clone_pane() -> None:
    """Both panes on (pods, default): closing one must not stop the watch
    the surviving pane depends on."""
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await pilot.press("ctrl+w", "q")
        await until(pilot, lambda: len(app.query(ResourceTable)) == 1, label="back to one pane")
        assert ("pods", "default") in app.watch_manager.active


async def test_diverged_pane_watch_stops_on_close() -> None:
    """The closed pane's watch stops when no other pane uses it."""
    app = make_app([_pod("api-1")], extra_data={"deployments": [_deploy("web")]})
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await _type_command(pilot, "deploy")
        await until(pilot, lambda: app.current_kind == "deployments", label="pane 2 on deploys")
        await pilot.press("ctrl+w", "q")
        await until(
            pilot,
            lambda: ("deployments", "default") not in app.watch_manager.active,
            label="orphan watch stopped",
        )
        assert ("pods", "default") in app.watch_manager.active


async def test_navigating_away_keeps_watch_other_pane_uses() -> None:
    """Pane 2 (clone of pods) navigates to deployments: the pods watch that
    pane 1 still shows must keep running."""
    app = make_app([_pod("api-1")], extra_data={"deployments": [_deploy("web")]})
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await _type_command(pilot, "deploy")
        await until(pilot, lambda: app.current_kind == "deployments", label="pane 2 on deploys")
        assert ("pods", "default") in app.watch_manager.active
        assert ("deployments", "default") in app.watch_manager.active


async def test_split_is_capped_at_two_panes() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await pilot.press("ctrl+w", "v")  # second split attempt
        await pilot.pause()
        assert len(app.query(ResourceTable)) == 2


async def test_close_in_single_pane_is_a_no_op() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await pilot.press("ctrl+w", "q")
        await pilot.pause()
        assert len(app.query(ResourceTable)) == 1
        assert app.current_kind == "pods"


async def test_chord_prefix_swallows_unrelated_key() -> None:
    """`ctrl+w` then an unmapped chord key does nothing - and must not
    leak the key into normal handling (e.g. `q` must not quit)."""
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await pilot.press("ctrl+w", "x")
        await pilot.pause()
        assert len(app.query(ResourceTable)) == 1
        await pilot.press("ctrl+w", "q")  # q right after prefix: close, not quit
        await pilot.pause()
        assert app.is_running


async def test_agent_screen_context_reports_focused_and_other_pane() -> None:
    app = make_app([_pod("api-1")], extra_data={"deployments": [_deploy("web")]})
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await _type_command(pilot, "deploy")
        await until(pilot, lambda: app.current_kind == "deployments", label="pane 2 on deploys")
        context = app._screen_context()
        assert "view=deployments" in context
        assert "other_pane=pods" in context


async def test_single_pane_context_has_no_other_pane_summary() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        assert "other_pane" not in app._screen_context()


async def test_split_clones_active_filter() -> None:
    """Splitting promises a clone of the focused view: an active filter
    carries into the new pane, then evolves independently."""
    app = make_app([_pod("api-1"), _pod("checkout-2")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await pilot.press("slash")
        for ch in "check":
            await pilot.press(ch)
        await pilot.press("enter")
        first = app.query_one("#pane-0", ResourceTable)
        await until(pilot, lambda: first.row_count == 1, label="pane 1 filtered")
        await _split(app, pilot)
        assert app._workspace.panes[1].filter_pattern == "check"
        second = app.query_one("#pane-1", ResourceTable)
        await until(pilot, lambda: second.row_count == 1, label="clone starts filtered")


async def test_split_clones_drill_stack_independently() -> None:
    """The clone starts at the source pane's drill-down position, but the
    stacks are independent - popping one must not pop the other."""
    from korvid.ui.navigation import DrillLevel

    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        level = DrillLevel("deployments", "web", "default", "dep-1", "pods")
        app._drill.push(level)
        await _split(app, pilot)
        assert (
            app._workspace.panes[1].drill.breadcrumb() == app._workspace.panes[0].drill.breadcrumb()
        )
        app._workspace.panes[1].drill.pop()
        assert app._workspace.panes[0].drill.active  # source stack untouched


async def test_closing_first_pane_keeps_survivors_cursor() -> None:
    """Closing pane 1 must not repaint pane 2's view into a stale widget -
    the survivor keeps its cursor/scroll state."""
    app = make_app([_pod("api-1"), _pod("api-2"), _pod("api-3")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        second = app.query_one("#pane-1", ResourceTable)
        await until(pilot, lambda: second.row_count == 3, label="clone rendered")
        await pilot.press("down")  # cursor to row 1 in the focused clone
        assert second.cursor_row == 1
        await pilot.press("ctrl+w", "w")  # focus pane 1
        await pilot.press("ctrl+w", "q")  # close pane 1
        await until(pilot, lambda: len(app.query(ResourceTable)) == 1, label="single pane")
        assert app.query_one(ResourceTable).cursor_row == 1


async def test_navigation_lands_in_initiating_pane_after_focus_switch() -> None:
    """A navigation must write kind/scope into the pane that initiated it,
    even when pane focus moves during one of its awaits (regression:
    focused-pane delegation resolved per property access)."""
    from korvid.ui.messages import NavigateCommand

    app = make_app([_pod("api-1")], extra_data={"deployments": [_deploy("web")]})
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)  # focused: pane 2
        # _navigate_locked only closes the log pane when the initiating pane
        # owns it - intercept the ownership-close seam so it actually runs.
        flipped = False

        async def flip_focus_mid_navigation(pane: object) -> None:
            nonlocal flipped
            flipped = True
            app._workspace.focus_index(0)  # the user switches panes during the await

        app._logs.close_if_owned_by = flip_focus_mid_navigation  # type: ignore[method-assign]  # test seam
        await app.on_navigate_command(NavigateCommand("deployments", None))
        assert flipped  # the focus switch really happened mid-navigation
        assert app._workspace.panes[1].kind == "deployments"  # initiating pane transitioned
        assert app._workspace.panes[0].kind == "pods"  # newly focused pane untouched


async def test_metrics_polling_follows_pod_pane_not_just_focused() -> None:
    """Pane 1 stays on pods while pane 2 navigates to deployments: the
    metrics poller must keep polling for the pod pane's scope."""
    from korvid.k8s.metrics import MetricsPoller, PodMetrics

    calls: list[str | None] = []

    async def fetch(namespace: str | None) -> list[PodMetrics]:
        calls.append(namespace)
        return []

    app = make_app(
        [_pod("api-1")],
        extra_data={"deployments": [_deploy("web")]},
        metrics=MetricsPoller(fetch, interval=0.05),
    )
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await _type_command(pilot, "deploy")
        await until(pilot, lambda: app.current_kind == "deployments", label="pane 2 on deploys")
        calls.clear()
        await until(pilot, lambda: "default" in calls, label="poller still serves the pod pane")
        assert "default" in calls


async def test_two_pod_panes_in_different_scopes_poll_cluster_wide() -> None:
    """Two pod panes in different namespaces: a single-scope poll would
    blank one pane's numbers - poll cluster-wide instead."""
    from korvid.k8s.metrics import MetricsPoller, PodMetrics

    calls: list[str | None] = []

    async def fetch(namespace: str | None) -> list[PodMetrics]:
        calls.append(namespace)
        return []

    app = make_app(
        [_pod("api-1")],
        namespaces=["default", "kube-system"],
        metrics=MetricsPoller(fetch, interval=0.05),
    )
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await _type_command(pilot, "ns kube-system")
        await until(pilot, lambda: app.current_scope == "kube-system", label="pane 2 rescoped")
        calls.clear()
        await until(pilot, lambda: None in calls, label="cluster-wide metrics poll")
        assert None in calls


async def test_chord_does_not_arm_while_an_input_is_focused() -> None:
    """`ctrl+w` while the command bar is open must not arm the chord: the
    Input consumes the second key, which would leave the pending flag set
    and silently swallow the next key after the bar closes."""
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await pilot.press("colon")
        await pilot.press("ctrl+w")
        assert app._workspace.chord_pending is False
        await pilot.press("escape")  # close the bar
        # The next key must reach its normal binding (open the filter bar),
        # not be swallowed by a stranded chord prefix.
        from korvid.ui.widgets.filter_bar import FilterBar

        await pilot.press("slash")
        await until(pilot, lambda: app.query_one(FilterBar).display, label="filter bar opened")
        assert app.query_one(FilterBar).display


async def test_split_serializes_with_navigation_lock() -> None:
    """Splitting mutates the pane list and starts a watch - it must hold
    the same lock as navigation so a concurrent `:view`/`:ns` transition
    never interleaves with the pane-list snapshot."""
    import asyncio

    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await app._nav_lock.acquire()
        try:
            task = asyncio.create_task(app._split_pane())
            # Yield until the task reaches (and blocks on) the nav lock -
            # deterministic, unlike a wall-clock delay.
            for _ in range(10):
                await asyncio.sleep(0)
            assert not task.done()
            assert len(app._workspace.panes) == 1  # blocked behind the nav lock
        finally:
            app._nav_lock.release()
        await task
        await until(pilot, lambda: len(app.query(ResourceTable)) == 2, label="split completed")
        assert len(app._workspace.panes) == 2
        # Flush the new pane's render/highlight messages before teardown.
        second = app.query(ResourceTable).last()
        await until(pilot, lambda: second.row_count == 1, label="clone rendered")
        await pilot.pause()


async def test_filtering_one_pane_does_not_reset_other_panes_cursor() -> None:
    """A view-state change (filter) in one pane must not repaint the other
    pane showing the same kind - `show()` clears and rebuilds, which would
    reset its cursor/scroll."""
    app = make_app([_pod("api-1"), _pod("api-2"), _pod("api-3")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        second = app.query_one("#pane-1", ResourceTable)
        await until(pilot, lambda: second.row_count == 3, label="clone rendered")
        await pilot.press("down")  # cursor to row 1 in the focused clone
        assert second.cursor_row == 1
        await pilot.press("ctrl+w", "w")  # focus pane 1
        await pilot.press("slash")
        for ch in "api":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: app._workspace.panes[0].filter_pattern == "api",
            label="pane 1 filter applied",
        )
        assert second.cursor_row == 1  # pane 2 untouched by pane 1's filter


async def test_split_hides_single_pane_empty_state() -> None:
    """The single-pane empty-state overlay must not linger over a split."""
    from textual.widgets import Static

    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await pilot.press("slash")
        for ch in "zzz":
            await pilot.press(ch)
        await pilot.press("enter")
        empty = app.query_one("#empty-state", Static)
        await until(pilot, lambda: empty.display, label="empty state shown")
        await _split(app, pilot)
        assert not empty.display


async def test_close_restores_empty_state_for_empty_survivor() -> None:
    """Closing back to a single empty pane must show the usual guidance,
    not a silent blank table."""
    from textual.widgets import Static

    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await pilot.press("slash")  # filter the focused clone to zero rows
        for ch in "zzz":
            await pilot.press(ch)
        await pilot.press("enter")
        second = app.query_one("#pane-1", ResourceTable)
        await until(pilot, lambda: second.row_count == 0, label="clone filtered empty")
        await pilot.press("ctrl+w", "w")  # focus pane 1
        await pilot.press("ctrl+w", "q")  # close pane 1; empty clone survives
        await until(pilot, lambda: len(app.query(ResourceTable)) == 1, label="single pane")
        empty = app.query_one("#empty-state", Static)
        await until(pilot, lambda: empty.display, label="empty-state guidance restored")
        assert "zzz" in str(empty.render())


async def test_focused_pane_indicator_survives_input_focus() -> None:
    """The accent border must mark the command-routing target even while an
    Input (command bar) owns keyboard focus - `:focus` alone drops the
    indicator exactly when the user is choosing where a command goes."""
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        first = app.query_one("#pane-0", ResourceTable)
        second = app.query_one("#pane-1", ResourceTable)
        assert second.has_class("focused-pane")
        assert not first.has_class("focused-pane")
        await pilot.press("colon")  # command bar takes keyboard focus
        assert second.has_class("focused-pane")  # routed pane stays marked
        await pilot.press("escape")
        await pilot.press("ctrl+w", "w")
        assert first.has_class("focused-pane")
        assert not second.has_class("focused-pane")
        await pilot.press("ctrl+w", "q")  # close pane 0; survivor is single
        await until(pilot, lambda: len(app.query(ResourceTable)) == 1, label="single pane")
        assert not second.has_class("focused-pane")
        await pilot.pause()


async def test_highlight_in_non_focused_pane_does_not_drive_hint() -> None:
    """A cursor/highlight event in the non-focused pane (e.g. from its own
    re-render) must not rewrite the hint strip, which reflects the focused
    pane's selection."""
    from korvid.k8s.models import ContainerTrouble, PodSummary
    from korvid.ui.widgets.hint_strip import HintStrip

    crash = ContainerTrouble(
        container="app",
        reason="CrashLoopBackOff",
        message="back-off restarting failed container",
        exit_code=137,
        exit_reason="OOMKilled",
        restarts=3,
    )
    bad = PodSummary(
        name="a-bad",
        namespace="default",
        phase="CrashLoopBackOff",
        ready="0/1",
        restarts=3,
        node=None,
        uid="uid-bad",
        trouble=(crash,),
    )
    app = make_app([bad, _pod("b-ok")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        second = app.query_one("#pane-1", ResourceTable)
        await until(pilot, lambda: second.row_count == 2, label="clone rendered")
        strip = app.query_one(HintStrip)
        # Focused clone's cursor sits on the trouble pod (row 0, sorted).
        await until(pilot, lambda: strip.display, label="hint for focused pane's selection")
        # Move the non-focused pane's cursor to the healthy row: its
        # highlight must not clear the focused pane's hint.
        app.query_one("#pane-0", ResourceTable).move_cursor(row=1)
        await pilot.pause()
        assert strip.display is True


async def test_focus_change_disarms_pending_chord() -> None:
    """An armed `ctrl+w` must not survive a focus change (e.g. a mouse
    click into another widget): the second key would never reach the chord
    handler, leaving the flag set to swallow the next table keypress."""
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await pilot.press("ctrl+w")
        assert app._workspace.chord_pending is True
        app.query_one("#pane-0", ResourceTable).focus()  # simulate a click
        await pilot.pause()
        assert app._workspace.chord_pending is False
        await pilot.press("v")  # must not be chord-swallowed into a split
        await pilot.pause()
        assert len(app.query(ResourceTable)) == 2


async def test_concurrent_closes_do_not_underflow_pane_list() -> None:
    """Two racing closes both pass the outer guard; the loser must re-check
    under the nav lock instead of popping an already-single pane list."""
    import asyncio

    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        await app._nav_lock.acquire()
        try:
            first = asyncio.create_task(app._close_focused_pane())
            second = asyncio.create_task(app._close_focused_pane())
            # Yield until both tasks pass the outer guard and block on the lock.
            for _ in range(10):
                await asyncio.sleep(0)
        finally:
            app._nav_lock.release()
        await first
        await second  # without the re-check this raises IndexError
        await until(pilot, lambda: len(app.query(ResourceTable)) == 1, label="single pane")
        assert len(app._workspace.panes) == 1
        await pilot.pause()


async def test_other_pane_navigation_keeps_log_stream() -> None:
    """The primary split workflow - watch one pane while tailing logs from
    the other - requires that only the log-owning pane's navigation closes
    the log pane; another pane's `:view`/`:ns` must leave it streaming."""
    from korvid.ui.widgets.log_pane import LogPane

    app = make_app([_pod("api-1")], extra_data={"deployments": [_deploy("web")]})
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await app._logs.open_pane("default", [("api-1", "app")])  # pane 1 owns the logs
        assert app.query_one(LogPane).display
        await _split(app, pilot)  # focus moves to pane 2
        await _type_command(pilot, "deploy")
        await until(pilot, lambda: app.current_kind == "deployments", label="pane 2 navigated")
        assert app.query_one(LogPane).display  # pane 1's logs survived
        await pilot.press("ctrl+w", "w")  # focus back to the owning pane
        await _type_command(pilot, "deploy")
        await until(
            pilot,
            lambda: not app.query_one(LogPane).display,
            label="owner navigation closes logs",
        )


async def test_closing_log_owner_pane_closes_log_stream() -> None:
    """Closing the pane that opened the logs must not leave an orphaned
    stream pinned to the survivor's view."""
    from korvid.ui.widgets.log_pane import LogPane

    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)  # focus: pane 2
        await app._logs.open_pane("default", [("api-1", "app")])  # pane 2 owns the logs
        assert app.query_one(LogPane).display
        await pilot.press("ctrl+w", "q")  # close pane 2 (the owner)
        await until(pilot, lambda: len(app.query(ResourceTable)) == 1, label="single pane")
        assert not app.query_one(LogPane).display


async def test_sorting_applies_to_focused_pane_only() -> None:
    """`:sort` is view state: it must reorder only the focused pane and
    leave the other pane's order alone (a shared per-kind sort would also
    rebuild - and reset the cursor of - the other pane)."""
    app = make_app([_pod("b-2"), _pod("a-1"), _pod("c-3")])
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        second = app.query_one("#pane-1", ResourceTable)
        await until(pilot, lambda: second.row_count == 3, label="clone rendered")
        first = app.query_one("#pane-0", ResourceTable)
        before = [first.get_row_at(i)[0] for i in range(3)]
        await _type_command(pilot, "sort name")
        await until(
            pilot,
            lambda: second.get_row_at(0)[0] == "a-1",
            label="focused pane sorted ascending",
        )
        assert [first.get_row_at(i)[0] for i in range(3)] == before
        assert app._workspace.panes[0].sorts == {}  # pane 1 carries no sort state


async def test_pane_focus_switch_reevaluates_hint() -> None:
    """`ctrl+w w` re-targets command routing: the hint strip must follow the
    newly focused pane's selection, not keep showing the old pane's warning
    (e.g. trouble pod focused, then switching into a deployments pane)."""
    from korvid.k8s.models import ContainerTrouble, PodSummary
    from korvid.ui.widgets.hint_strip import HintStrip

    crash = ContainerTrouble(
        container="app",
        reason="CrashLoopBackOff",
        message="back-off restarting failed container",
        exit_code=137,
        exit_reason="OOMKilled",
        restarts=3,
    )
    bad = PodSummary(
        name="a-bad",
        namespace="default",
        phase="CrashLoopBackOff",
        ready="0/1",
        restarts=3,
        node=None,
        uid="uid-bad",
        trouble=(crash,),
    )
    app = make_app([bad], extra_data={"deployments": [_deploy("web")]})
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)
        second = app.query_one("#pane-1", ResourceTable)
        await until(pilot, lambda: second.row_count == 1, label="clone rendered")
        strip = app.query_one(HintStrip)
        await until(pilot, lambda: strip.display, label="hint for trouble pod")
        await _type_command(pilot, "deploy")  # focused pane now shows deployments
        await until(pilot, lambda: app.current_kind == "deployments", label="deploy view")
        await until(pilot, lambda: not strip.display, label="hint cleared on non-pod view")
        # Switch back to the pods pane: the warning must return without
        # requiring a cursor move.
        await pilot.press("ctrl+w", "w")
        await until(pilot, lambda: strip.display, label="hint restored for pods pane")
        # And back to the deployments pane: the pods warning must not linger.
        await pilot.press("ctrl+w", "w")
        await until(pilot, lambda: not strip.display, label="hint cleared for deploy pane")


async def test_navigation_queued_behind_lock_lands_in_initiating_pane() -> None:
    """A navigation that queues behind the nav lock must still land in the
    pane that initiated it: capturing the pane only after acquiring the
    lock would clear pane A's drill stack (drill_op binds early) while
    navigating whichever pane got focused in the meantime."""
    import asyncio

    from korvid.ui.messages import NavigateCommand
    from korvid.ui.navigation import DrillLevel

    app = make_app([_pod("api-1")], extra_data={"deployments": [_deploy("web")]})
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)  # focused: pane index 1
        app._workspace.panes[1].drill.push(
            DrillLevel("deployments", "web", "default", "dep-1", "pods")
        )
        release = asyncio.Event()

        async def hold() -> None:
            async with app._nav_lock:
                await release.wait()

        holder = asyncio.create_task(hold())
        for _ in range(10):
            await asyncio.sleep(0)
        nav = asyncio.create_task(app.on_navigate_command(NavigateCommand("deployments", None)))
        for _ in range(10):
            await asyncio.sleep(0)
        app._workspace.focus_index(0)  # the user flips focus while nav waits for the lock
        release.set()
        await nav
        await holder
        assert app._workspace.panes[1].kind == "deployments"  # initiating pane transitioned
        assert not app._workspace.panes[1].drill.active  # and its stack was the one cleared
        assert app._workspace.panes[0].kind == "pods"  # newly focused pane untouched


async def test_drill_pop_queued_behind_lock_pops_initiating_pane() -> None:
    """Escape in pane A that queues behind the nav lock must pop pane A's
    drill stack even when focus moves to pane B before the lock frees."""
    import asyncio

    from korvid.ui.navigation import DrillLevel

    app = make_app([_pod("api-1")], extra_data={"deployments": [_deploy("web")]})
    async with app.run_test() as pilot:
        await _first_render(app, pilot)
        await _split(app, pilot)  # focused: pane index 1
        for pane in app._workspace.panes:
            pane.drill.push(DrillLevel("deployments", "web", "default", "dep-1", "pods"))
            pane.kind = "pods"
        release = asyncio.Event()

        async def hold() -> None:
            async with app._nav_lock:
                await release.wait()

        holder = asyncio.create_task(hold())
        for _ in range(10):
            await asyncio.sleep(0)
        pop = asyncio.create_task(app._pop_drill())
        for _ in range(10):
            await asyncio.sleep(0)
        app._workspace.focus_index(0)  # focus flips while the pop waits for the lock
        release.set()
        assert await pop is True
        await holder
        assert not app._workspace.panes[1].drill.active  # initiating pane popped
        assert app._workspace.panes[0].drill.active  # other pane's stack untouched
