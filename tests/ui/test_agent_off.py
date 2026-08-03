"""`:ai off` disconnects the agent runtime for the session; bare `:ai`
reconnects with the kept settings (issue #167). Ctrl+A stays a pure panel
visibility toggle."""

from __future__ import annotations

from typing import Any, cast

from textual.widgets import Input

from korvid.agent.events import TextDelta, TurnComplete
from korvid.ui.messages import UnknownCommand
from korvid.ui.widgets.agent_panel import AgentPanel, ChatEntry
from korvid.ui.widgets.status_bar import StatusBar
from tests.ui.test_agent_wiring import StubRuntime, make_app

from .waits import until


def _panel_text(app: Any) -> str:
    return "\n".join(entry.raw for entry in app.query_one(AgentPanel).query(ChatEntry))


def _status(app: Any) -> str:
    return str(app.query_one(StatusBar).render())


async def test_ai_off_disconnects_the_runtime_and_updates_the_status() -> None:
    closed: list[bool] = []
    runtime = StubRuntime([TurnComplete(input_tokens=0, output_tokens=0, estimated=False)])
    app = make_app(runtime, disconnect_agent=lambda: closed.append(True))
    async with app.run_test() as pilot:
        assert "AI on" in _status(app)
        app.on_unknown_command(UnknownCommand("ai off"))
        await pilot.pause()
        assert app._agent_runtime is None
        assert "AI off" in _status(app)
        assert closed == [True]  # the provider was released, not leaked


async def test_ai_off_disables_prompt_submission_and_shows_the_hint() -> None:
    runtime = StubRuntime([TurnComplete(input_tokens=0, output_tokens=0, estimated=False)])
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        app.on_unknown_command(UnknownCommand("ai off"))
        await pilot.pause()
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        assert inp.disabled is True
        assert ":ai" in _panel_text(app)  # reconnect hint names the command
        assert not runtime.calls


async def test_ai_off_keeps_the_conversation_transcript() -> None:
    runtime = StubRuntime(
        [TextDelta(text="all good"), TurnComplete(input_tokens=1, output_tokens=1, estimated=False)]
    )
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        inp.value = "how are my pods?"
        await pilot.press("enter")
        await until(pilot, lambda: "all good" in _panel_text(app), label="turn done")
        app.on_unknown_command(UnknownCommand("ai off"))
        await pilot.pause()
        assert "all good" in _panel_text(app)  # disconnect never erases history


async def test_ai_off_is_idempotent_when_already_off() -> None:
    app = make_app(runtime=None, model=None)
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("ai off"))
        await pilot.pause()
        assert app._agent_runtime is None  # no crash, still off
        assert any("off" in n.message for n in app._notifications)


async def test_ai_off_refuses_while_a_turn_is_running() -> None:
    runtime = StubRuntime([TextDelta(text="thinking")], block=True)
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        inp.value = "q"
        await pilot.press("enter")
        await until(pilot, lambda: bool(runtime.calls), label="turn running")
        app.on_unknown_command(UnknownCommand("ai off"))
        await pilot.pause()
        assert app._agent_runtime is not None  # unchanged: never cancels midway
        assert any("busy" in n.message.lower() for n in app._notifications)


async def test_reconnect_after_off_restores_the_agent() -> None:
    from korvid.agent.setup import AgentSettings

    runtime = StubRuntime([TurnComplete(input_tokens=0, output_tokens=0, estimated=False)])
    fresh = cast("Any", StubRuntime([]))
    app = make_app(runtime, rebuild_agent=lambda s: fresh)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        app.on_unknown_command(UnknownCommand("ai off"))
        await pilot.pause()
        assert "AI off" in _status(app)
        settings = AgentSettings(
            provider="ollama",
            auth_method="none",
            base_url="http://localhost:11434/v1",
            model="llama3",
        )
        assert app._apply_agent_settings(settings) is True
        await pilot.pause()
        assert app._agent_runtime is fresh
        assert "AI on" in _status(app)
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        assert inp.disabled is False


async def test_ctrl_a_stays_a_pure_visibility_toggle() -> None:
    runtime = StubRuntime([TurnComplete(input_tokens=0, output_tokens=0, estimated=False)])
    app = make_app(runtime)
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        await pilot.press("ctrl+a")
        assert panel.display is True
        await pilot.press("ctrl+a")
        assert panel.display is False
        assert app._agent_runtime is runtime  # visibility never touches state
