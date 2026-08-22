"""MCP follow mode UI (issue #153): the `:mcp follow` toggle, the status-bar
badge, the activity note surface, and the approval-dialog mirror guard."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import PODS_META
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen

from .test_proposals_ui import FakeMCP
from .waits import until

_ALIASES = {"pods": PODS_META}


def make_app(
    *,
    config: KorvidConfig | None = None,
    mcp: FakeMCP | None = None,
) -> KorvidApp:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if False:  # pragma: no cover - makes this an async generator
            yield ("ADDED", None)
        while True:
            await asyncio.sleep(0.01)

    async def list_namespaces() -> list[str]:
        return ["default"]

    return KorvidApp(
        config=config or KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=dict(_ALIASES),
        mcp=mcp if mcp is not None else FakeMCP(running=True),
    )


async def _command(app: KorvidApp, pilot: object, text: str) -> None:
    app._handle_mcp_command(text.split()[1:])  # same entry the command bar uses


# ---------------------------------------------------------------------------
# :mcp follow toggle
# ---------------------------------------------------------------------------


async def test_follow_starts_from_config() -> None:
    app = make_app(config=KorvidConfig(namespace="default", mcp_follow=True))
    async with app.run_test():
        assert app.mcp_follow_enabled is True
    app2 = make_app()
    async with app2.run_test():
        assert app2.mcp_follow_enabled is False


async def test_mcp_follow_command_toggles_and_reports() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _command(app, pilot, "mcp follow on")
        await until(pilot, lambda: app.mcp_follow_enabled, label="follow on")
        await until(
            pilot,
            lambda: any("follow" in n.message.lower() for n in app._notifications),
            label="toggle reported",
        )
        await _command(app, pilot, "mcp follow off")
        await until(pilot, lambda: not app.mcp_follow_enabled, label="follow off")


async def test_bare_mcp_follow_toggles() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _command(app, pilot, "mcp follow")
        await until(pilot, lambda: app.mcp_follow_enabled, label="follow toggled on")


async def test_bare_mcp_status_reports_follow_state() -> None:
    app = make_app(config=KorvidConfig(namespace="default", mcp_follow=True))
    async with app.run_test() as pilot:
        await _command(app, pilot, "mcp")
        await until(
            pilot,
            lambda: any("follow on" in n.message for n in app._notifications),
            label="status names the follow state",
        )


# ---------------------------------------------------------------------------
# status-bar badge
# ---------------------------------------------------------------------------


async def test_status_bar_shows_the_follow_badge_while_active() -> None:
    from korvid.ui.widgets.status_bar import StatusBar

    app = make_app(config=KorvidConfig(namespace="default", mcp_follow=True))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: "follow" in str(app.query_one(StatusBar).render()),
            label="follow badge on the status bar",
        )


async def test_no_follow_badge_when_the_server_is_off() -> None:
    """Follow only matters while the server runs: a badge on a stopped
    server would advertise mirroring that cannot happen."""
    from korvid.ui.widgets.status_bar import StatusBar

    app = make_app(
        config=KorvidConfig(namespace="default", mcp_follow=True),
        mcp=FakeMCP(running=False),
    )
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: "ns:default" in str(app.query_one(StatusBar).render()),
            label="status bar rendered",
        )
        assert "follow" not in str(app.query_one(StatusBar).render())


# ---------------------------------------------------------------------------
# activity notes
# ---------------------------------------------------------------------------


async def test_note_mcp_activity_shows_a_transient_toast() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        app.note_mcp_activity("copilot: get_logs api-1 (ns prod)")
        await until(
            pilot,
            lambda: any("get_logs api-1" in n.message for n in app._notifications),
            label="activity toast shown",
        )


# ---------------------------------------------------------------------------
# approval-dialog mirror guard (security invariant)
# ---------------------------------------------------------------------------


async def test_describe_refuses_while_an_approval_dialog_is_up() -> None:
    """A mirrored (or agent-driven) describe must never steal keystroke
    focus from an approval dialog: approvals are confirmed only by user
    keystrokes, and a screen swap mid-approval could redirect them."""
    app = make_app()
    async with app.run_test() as pilot:
        app.push_screen(ConfirmScreen("Scale deploy prod/api", "scale", preview=["1 -> 3"]))
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="approval dialog up",
        )
        result = await app._agent_ui.agent_open_describe("pods", "api-1", "default")
        assert result.startswith("ERROR:")
        assert "approval" in result
        assert isinstance(app.screen, ConfirmScreen)  # the dialog kept focus


async def test_navigate_and_logs_refuse_while_an_approval_dialog_is_up() -> None:
    """Same 'user is deciding' rule as describe: a mirrored navigate would
    swap the view under the dialog, and a mirrored log open would tear down
    the log stream the user was watching beneath it."""
    app = make_app()
    async with app.run_test() as pilot:
        app.push_screen(ConfirmScreen("Scale deploy prod/api", "scale", preview=["1 -> 3"]))
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="approval dialog up",
        )
        nav = await app._agent_ui.agent_navigate("pods")
        assert nav.startswith("ERROR:")
        assert "approval" in nav
        logs = await app._agent_ui.agent_open_logs("api-1", "default")
        assert logs.startswith("ERROR:")
        assert "approval" in logs
        assert isinstance(app.screen, ConfirmScreen)  # the dialog kept focus


async def test_activity_toast_renders_without_rich_markup() -> None:
    """Args arrive from the MCP caller: `[red]...[/]` in a pod name must
    show literally, not restyle the toast."""
    app = make_app()
    async with app.run_test() as pilot:
        app.note_mcp_activity("mcp: get_logs [bold red]FAKE APPROVAL[/] (ns d)")
        await until(
            pilot,
            lambda: any("FAKE APPROVAL" in n.message for n in app._notifications),
            label="toast recorded",
        )
        note = next(n for n in app._notifications if "FAKE APPROVAL" in n.message)
        assert note.markup is False


async def test_guard_covers_every_write_flow_modal() -> None:
    """ResizePrompt / OperatorInstallPrompt / HelmInstallPrompt feed write
    confirmation just like ReplicasPrompt: a followed describe must not
    push over any of them."""
    from korvid.ui.widgets.resize_prompt import ResizePrompt

    app = make_app()
    async with app.run_test() as pilot:
        await app.push_screen(
            ResizePrompt(
                "pods/api-1",
                containers=[("main", {"requests": {"cpu": "100m", "memory": "64Mi"}})],
            )
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, ResizePrompt),
            label="resize prompt up",
        )
        result = await app._agent_ui.agent_open_describe("pods", "api-1", "default")
        assert result.startswith("ERROR:")
        assert "approval" in result
        assert isinstance(app.screen, ResizePrompt)


async def test_logs_recheck_the_approval_guard_after_the_pod_lookup() -> None:
    """The pre-check can go stale during the awaited pod/container lookup:
    an approval dialog opening in that window must still stop the log-pane
    teardown."""
    app = make_app()
    async with app.run_test() as pilot:
        app._stream_logs = lambda *a, **k: None  # type: ignore[assignment]  # gate opener; never reached

        async def lookup(namespace: str, pod: str) -> list[tuple[str, str, str]]:
            # the dialog opens while the lookup is in flight
            app.push_screen(ConfirmScreen("Scale deploy prod/api", "scale", preview=["1 -> 3"]))
            await asyncio.sleep(0)
            return [(namespace, pod, "main")]

        app._agent_ui._pod_triples = lookup  # type: ignore[method-assign]  # test seam
        result = await app._agent_ui.agent_open_logs("api-1", "default")
        assert result.startswith("ERROR:")
        assert "approval" in result
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)  # dialog untouched
