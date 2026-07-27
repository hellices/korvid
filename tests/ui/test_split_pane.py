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
        assert app._panes[0].kind == "pods"


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
        assert app._focused_pane == 0
        await _type_command(pilot, "deploy all")
        await until(
            pilot,
            lambda: app._panes[0].kind == "deployments",
            label="pane 1 switched",
        )
        assert app._panes[1].kind == "deployments"
        assert app._panes[1].scope == "default"  # pane 2 untouched by pane 1 nav
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
        assert len(app._panes) == 1


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
        assert app._panes[1].filter_pattern == "check"
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
        assert app._panes[1].drill.breadcrumb() == app._panes[0].drill.breadcrumb()
        app._panes[1].drill.pop()
        assert app._panes[0].drill.active  # source stack untouched


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

        async def flip_focus_mid_navigation() -> None:
            app._focused_pane = 0  # the user switches panes during the await

        app._close_log_pane = flip_focus_mid_navigation  # type: ignore[method-assign]  # test seam
        await app.on_navigate_command(NavigateCommand("deployments", None))
        assert app._panes[1].kind == "deployments"  # initiating pane transitioned
        assert app._panes[0].kind == "pods"  # newly focused pane untouched


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
        assert app._pane_chord_pending is False
        await pilot.press("escape")  # close the bar
        await pilot.press("d")  # must reach describe, not be chord-swallowed
        await pilot.pause()
        assert len(app.query(ResourceTable)) == 1


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
            await pilot.pause(0.1)
            assert len(app._panes) == 1  # blocked behind the nav lock
        finally:
            app._nav_lock.release()
        await task
        await until(pilot, lambda: len(app.query(ResourceTable)) == 2, label="split completed")
        assert len(app._panes) == 2
        # Flush the new pane's render/highlight messages before teardown.
        second = app.query(ResourceTable).last()
        await until(pilot, lambda: second.row_count == 1, label="clone rendered")
        await pilot.pause()
