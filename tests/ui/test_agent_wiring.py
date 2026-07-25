"""Tests for Task 9: Ctrl-A agent panel wiring."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

from textual.widgets import Input, RichLog

from korvid.agent.events import AgentEvent, TextDelta, TurnComplete
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.messages import AgentPromptSubmitted
from korvid.ui.widgets.agent_panel import AgentPanel


def _pod(name: str) -> PodSummary:
    return PodSummary(
        name=name,
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        qos="-",
        containers=("main",),
    )


class StubRuntime:
    """Scripted stand-in for AgentRuntime."""

    def __init__(self, events: list[AgentEvent], block: bool = False) -> None:
        self._events = events
        self._block = block
        self.calls: list[tuple[str, str]] = []
        self.total_tokens = (0, 0)
        self.usage_estimated = False

    async def run_turn(self, user_text: str, screen_context: str) -> AsyncIterator[AgentEvent]:
        self.calls.append((user_text, screen_context))
        for ev in self._events:
            yield ev
        if self._block:
            await asyncio.Event().wait()


def make_app(runtime: Any = None, model: str | None = "test-model", **kwargs: Any) -> KorvidApp:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, PodSummary]]:
        yield ("ADDED", _pod("web-1"))
        while True:
            await asyncio.sleep(0.01)

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        agent_runtime=runtime,
        agent_model_name=model,
        **kwargs,
    )


def _panel_text(app: KorvidApp) -> str:
    log = app.query_one(AgentPanel).query_one("#agent-log", RichLog)
    return "\n".join(strip.text for strip in log.lines)


async def test_ctrl_a_toggles_panel_display() -> None:
    app = make_app(StubRuntime([]))
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        assert panel.display is False
        await pilot.press("ctrl+a")
        assert panel.display is True
        await pilot.press("ctrl+a")
        assert panel.display is False


async def test_no_runtime_shows_setup_hint() -> None:
    app = make_app(runtime=None, model=None)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        assert "Run :ai" in _panel_text(app)
        assert app.query_one(AgentPanel).query_one("#agent-input", Input).disabled is True


async def test_prompt_drives_runtime_and_renders_reply() -> None:
    runtime = StubRuntime(
        [
            TextDelta(text="All pods healthy.\n"),
            TurnComplete(input_tokens=10, output_tokens=4, estimated=False),
        ]
    )
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        inp.value = "how are my pods?"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert runtime.calls
        assert runtime.calls[0][0] == "how are my pods?"
        text = _panel_text(app)
        assert "> how are my pods?" in text
        assert "All pods healthy." in text
        assert inp.disabled is False


async def test_screen_context_includes_current_view() -> None:
    runtime = StubRuntime([TurnComplete(input_tokens=0, output_tokens=0, estimated=True)])
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        inp.value = "q"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert runtime.calls
        ctx = runtime.calls[0][1]
        assert "view=pods" in ctx
        assert "scope=default" in ctx


async def test_second_submit_ignored_while_turn_running() -> None:
    runtime = StubRuntime([TextDelta(text="thinking")], block=True)
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        panel = app.query_one(AgentPanel)
        inp = panel.query_one("#agent-input", Input)
        inp.value = "first"
        await pilot.press("enter")
        await pilot.pause()
        # Input is disabled during a turn, but simulate a direct message anyway.
        panel.post_message(AgentPromptSubmitted("second"))
        await pilot.pause()
        await pilot.pause()
        assert [c[0] for c in runtime.calls] == ["first"]


async def test_status_bar_reflects_runtime_not_config_flag() -> None:
    """create_provider can return None while agent_enabled stays true —
    the status label must track the actual runtime."""
    from korvid.ui.widgets.status_bar import StatusBar

    app = make_app(runtime=None)
    app.config = KorvidConfig(namespace="default", agent_enabled=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert "AI off" in str(app.query_one(StatusBar).render())


async def test_status_bar_on_when_runtime_present() -> None:
    from korvid.ui.widgets.status_bar import StatusBar

    app = make_app(StubRuntime([]))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        assert "AI on" in str(app.query_one(StatusBar).render())


async def test_setup_hint_not_duplicated_on_retoggle() -> None:
    app = make_app(runtime=None, model=None)
    async with app.run_test() as pilot:
        for _ in range(3):
            await pilot.press("ctrl+a")  # open
            await pilot.press("ctrl+a")  # close
        await pilot.press("ctrl+a")
        assert _panel_text(app).count("Run :ai") == 1


async def test_ai_command_pushes_setup_screen() -> None:
    from korvid.ui.messages import UnknownCommand
    from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen

    class NoopConfigurator:
        async def begin_device_login(self) -> Any:
            raise NotImplementedError

        async def finish_device_login(self) -> None:
            raise NotImplementedError

        async def test(self, settings: Any) -> str:
            return "ok"

        async def save(self, settings: Any) -> None:
            pass

    app = make_app(runtime=None, model=None, agent_configurator=NoopConfigurator())
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("ai"))
        await pilot.pause()
        assert isinstance(app.screen, AgentSetupScreen)


async def test_ai_command_without_configurator_notifies() -> None:
    from korvid.ui.messages import UnknownCommand

    app = make_app(runtime=None, model=None)
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("ai"))
        await pilot.pause()
        # No crash and no setup screen pushed.
        from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen

        assert not isinstance(app.screen, AgentSetupScreen)


async def test_apply_agent_settings_enables_agent() -> None:
    from korvid.agent.setup import AgentSettings
    from korvid.ui.widgets.status_bar import StatusBar

    runtime = cast("Any", StubRuntime([]))
    settings = AgentSettings(
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434/v1",
        model="llama3",
    )
    app = make_app(runtime=None, model=None, rebuild_agent=lambda s: runtime)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")  # open panel: setup hint, input disabled
        app._apply_agent_settings(settings)
        await pilot.pause()
        assert app._agent_runtime is runtime
        assert app._agent_model_name == "llama3"
        assert "AI on" in str(app.query_one(StatusBar).render())
        assert app.query_one(AgentPanel).query_one("#agent-input", Input).disabled is False
