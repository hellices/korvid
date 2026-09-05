"""`:ai off` disconnects the agent session for the session; bare `:ai`
reconnects with the kept settings (issue #167). Ctrl+A stays a pure panel
visibility toggle."""

from __future__ import annotations

from typing import Any, cast

from textual.widgets import Input

from korvid.agent.events import TextDelta, TurnComplete
from korvid.ui.messages import UnknownCommand
from korvid.ui.widgets.agent_panel import AgentPanel, ChatEntry
from korvid.ui.widgets.status_bar import StatusBar
from tests.ui.test_agent_wiring import StubSession, make_app

from .waits import until


def _panel_text(app: Any) -> str:
    return "\n".join(entry.raw for entry in app.query_one(AgentPanel).query(ChatEntry))


def _status(app: Any) -> str:
    return str(app.query_one(StatusBar).render())


async def test_ai_off_disconnects_the_session_and_updates_the_status() -> None:
    closed: list[bool] = []
    session = StubSession([TurnComplete(input_tokens=0, output_tokens=0, estimated=False)])
    app = make_app(session, disconnect_agent=lambda: closed.append(True))
    async with app.run_test() as pilot:
        assert "AI on" in _status(app)
        app.on_unknown_command(UnknownCommand("ai off"))
        await pilot.pause()
        assert app._agent_ui.session is None
        assert "AI off" in _status(app)
        assert closed == [True]  # the provider was released, not leaked


async def test_ai_off_disables_prompt_submission_and_shows_the_hint() -> None:
    session = StubSession([TurnComplete(input_tokens=0, output_tokens=0, estimated=False)])
    app = make_app(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        app.on_unknown_command(UnknownCommand("ai off"))
        await pilot.pause()
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        assert inp.disabled is True
        assert ":ai" in _panel_text(app)  # reconnect hint names the command
        assert not session.prompts


async def test_ai_off_keeps_the_conversation_transcript() -> None:
    session = StubSession(
        [TextDelta(text="all good"), TurnComplete(input_tokens=1, output_tokens=1, estimated=False)]
    )
    app = make_app(session)
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
    app = make_app(session=None, model=None)
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("ai off"))
        await pilot.pause()
        assert app._agent_ui.session is None  # no crash, still off
        assert any("off" in n.message for n in app._notifications)


async def test_ai_off_refuses_while_a_turn_is_running() -> None:
    session = StubSession([TextDelta(text="thinking")], block=True)
    app = make_app(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        inp.value = "q"
        await pilot.press("enter")
        await until(pilot, lambda: bool(session.prompts), label="turn running")
        app.on_unknown_command(UnknownCommand("ai off"))
        await pilot.pause()
        assert app._agent_ui.session is not None  # unchanged: never cancels midway
        assert any("busy" in n.message.lower() for n in app._notifications)


async def test_reconnect_after_off_restores_the_agent() -> None:
    from korvid.agent.setup import AgentSettings

    session = StubSession([TurnComplete(input_tokens=0, output_tokens=0, estimated=False)])
    fresh = cast("Any", StubSession([]))
    app = make_app(session, rebuild_agent=lambda s: fresh)
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
        assert app._agent_ui.apply_settings(settings) is True
        await pilot.pause()
        assert app._agent_ui.session is fresh
        assert "AI on" in _status(app)
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        assert inp.disabled is False


async def test_ctrl_a_stays_a_pure_visibility_toggle() -> None:
    session = StubSession([TurnComplete(input_tokens=0, output_tokens=0, estimated=False)])
    app = make_app(session)
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        await pilot.press("ctrl+a")
        assert panel.display is True
        await pilot.press("ctrl+a")
        assert panel.display is False
        assert app._agent_ui.session is session  # visibility never touches state


async def test_ctrl_a_after_off_keeps_the_transcript() -> None:
    """Ctrl+A must stay a pure visibility toggle after :ai off: reopening
    the panel shows the reconnect hint without erasing the conversation
    (review on #180)."""
    session = StubSession(
        [TextDelta(text="all good"), TurnComplete(input_tokens=1, output_tokens=1, estimated=False)]
    )
    app = make_app(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        inp.value = "how are my pods?"
        await pilot.press("enter")
        await until(pilot, lambda: "all good" in _panel_text(app), label="turn done")
        app.on_unknown_command(UnknownCommand("ai off"))
        await pilot.pause()
        await pilot.press("ctrl+a")  # hide
        await pilot.press("ctrl+a")  # …and reopen
        assert "all good" in _panel_text(app)  # transcript survived the toggle
        assert ":ai" in _panel_text(app)  # reconnect hint, not the setup wipe
        await pilot.press("ctrl+a")
        await pilot.press("ctrl+a")
        assert _panel_text(app).count("run :ai to reconnect") == 1  # no hint spam


async def test_bare_ai_after_off_prefills_the_wizard() -> None:
    """The wizard opened after :ai off starts from the kept settings —
    the user reconnects by confirming, not re-entering (review on #180).

    The prefill is a profile now: the kept scalars are projected onto one
    so the wizard's stages start from the connection that was live.
    """
    from typing import Any as _Any

    from korvid.agent.setup import AgentSettings
    from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen
    from tests.ui.test_agent_ui_controller_profiles import _StubCatalog

    class NoopConfigurator:
        async def begin_device_login(self) -> _Any:
            raise NotImplementedError

        async def finish_device_login(self) -> None:
            raise NotImplementedError

        async def test(self, settings: _Any) -> str:
            return "ok"

        async def list_models(self, settings: _Any) -> list[str]:
            return []

        async def save(self, settings: _Any) -> None:
            pass

    session = StubSession([TurnComplete(input_tokens=0, output_tokens=0, estimated=False)])
    app = make_app(session, agent_configurator=NoopConfigurator(), agent_catalog=_StubCatalog())
    settings = AgentSettings(
        provider="ollama",
        auth_method="none",
        base_url="http://my-ollama:11434/v1",
        model="qwen3:8b",
    )
    app._agent_ui._settings = settings
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("ai off"))
        await pilot.pause()
        app.on_unknown_command(UnknownCommand("ai"))
        await until(
            pilot,
            lambda: any(isinstance(screen, AgentSetupScreen) for screen in app.screen_stack),
            label="setup wizard opened",
        )
        wizard = next(screen for screen in app.screen_stack if isinstance(screen, AgentSetupScreen))
        assert wizard._seed.model == "ollama/qwen3:8b"
        assert wizard._seed.endpoint == "http://my-ollama:11434/v1"
        assert wizard._seed.auth.method == "none"
        # Close the wizard before the app tears down: the stage worker is
        # parked on a pushed screen, and shutting down under it cancels a
        # wait nothing is left to answer.
        await pilot.press("escape")
        await pilot.press("escape")
        await until(
            pilot,
            lambda: not any(isinstance(screen, AgentSetupScreen) for screen in app.screen_stack),
            label="setup wizard closed",
        )
