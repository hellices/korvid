"""Tests for Task 9: Ctrl-A agent panel wiring."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

from textual.widgets import Input

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
    from korvid.ui.widgets.agent_panel import ChatEntry

    return "\n".join(entry.raw for entry in app.query_one(AgentPanel).query(ChatEntry))


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
        assert "how are my pods?" in text
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

        async def list_models(self, settings: Any) -> list[str]:
            return []

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


async def test_model_command_swaps_model_and_saves() -> None:
    from korvid.agent.setup import AgentConfigurator, AgentSettings
    from korvid.ui.messages import UnknownCommand

    saved: list[AgentSettings] = []

    class Cfg(AgentConfigurator):
        async def begin_device_login(self) -> Any:
            raise NotImplementedError

        async def finish_device_login(self) -> None:
            raise NotImplementedError

        async def test(self, settings: Any) -> str:
            return "ok"

        async def list_models(self, settings: Any) -> list[str]:
            return []

        async def save(self, settings: AgentSettings) -> None:
            saved.append(settings)

    rebuilt: list[AgentSettings] = []
    runtime = cast("Any", StubRuntime([]))

    def rebuild(settings: AgentSettings) -> Any:
        rebuilt.append(settings)
        return runtime

    app = make_app(runtime=None, model=None, agent_configurator=Cfg(), rebuild_agent=rebuild)
    settings = AgentSettings(
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434/v1",
        model="llama3",
    )
    async with app.run_test() as pilot:
        app._apply_agent_settings(settings)
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        for _ in range(4):
            await pilot.pause()
        assert app._agent_model_name == "gpt-4o"
        assert saved
        assert saved[-1].model == "gpt-4o"
        assert rebuilt[-1].model == "gpt-4o"


async def test_model_command_without_config_does_not_crash() -> None:
    from korvid.ui.messages import UnknownCommand

    app = make_app(runtime=None, model=None)
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        app.on_unknown_command(UnknownCommand("model"))
        await pilot.pause()
        assert app._agent_model_name is None


async def test_model_command_does_not_persist_when_apply_fails() -> None:
    """If the runtime swap is refused (rebuild returns None), the new model
    must NOT be written to config.yaml — otherwise the failed change silently
    takes effect after restart."""
    from korvid.agent.setup import AgentConfigurator, AgentSettings
    from korvid.ui.messages import UnknownCommand

    saved: list[AgentSettings] = []

    class Cfg(AgentConfigurator):
        async def begin_device_login(self) -> Any:
            raise NotImplementedError

        async def finish_device_login(self) -> None:
            raise NotImplementedError

        async def test(self, settings: Any) -> str:
            return "ok"

        async def list_models(self, settings: Any) -> list[str]:
            return []

        async def save(self, settings: AgentSettings) -> None:
            saved.append(settings)

    runtime = cast("Any", StubRuntime([]))
    rebuilds: list[AgentSettings] = []

    def rebuild(settings: AgentSettings) -> Any:
        rebuilds.append(settings)
        # First call (initial apply) succeeds; the :model rebuild fails.
        return runtime if len(rebuilds) == 1 else None

    app = make_app(runtime=None, model=None, agent_configurator=Cfg(), rebuild_agent=rebuild)
    settings = AgentSettings(
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434/v1",
        model="llama3",
    )
    async with app.run_test() as pilot:
        app._apply_agent_settings(settings)
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        for _ in range(4):
            await pilot.pause()
        assert app._agent_model_name == "llama3"  # old runtime kept
        assert not saved  # and nothing was persisted


async def test_model_command_save_failure_warns_about_restart_revert() -> None:
    """If the swap succeeded but persisting failed, the user must be told the
    model is live now but will revert on restart."""
    from korvid.agent.setup import AgentConfigurator, AgentSettings
    from korvid.ui.messages import UnknownCommand

    class Cfg(AgentConfigurator):
        async def begin_device_login(self) -> Any:
            raise NotImplementedError

        async def finish_device_login(self) -> None:
            raise NotImplementedError

        async def test(self, settings: Any) -> str:
            return "ok"

        async def list_models(self, settings: Any) -> list[str]:
            return []

        async def save(self, settings: AgentSettings) -> None:
            raise RuntimeError("disk full")

    runtime = cast("Any", StubRuntime([]))
    app = make_app(
        runtime=None, model=None, agent_configurator=Cfg(), rebuild_agent=lambda s: runtime
    )
    settings = AgentSettings(
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434/v1",
        model="llama3",
    )
    async with app.run_test() as pilot:
        app._apply_agent_settings(settings)
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        for _ in range(4):
            await pilot.pause()
        assert app._agent_model_name == "gpt-4o"  # swap took effect
        notes = " ".join(str(n.message) for n in app._notifications)
        assert "disk full" in notes
        assert "revert" in notes.lower()

        # A second failed switch must not claim the app will revert to the
        # live-but-unsaved model: the in-memory snapshot is not what is on
        # disk, so the warning must not name a specific model at all.
        app.on_unknown_command(UnknownCommand("model claude-3"))
        for _ in range(4):
            await pilot.pause()
        assert app._agent_model_name == "claude-3"
        second = " ".join(
            str(n.message)
            for n in app._notifications
            if "claude-3" in str(n.message) or "save failed" in str(n.message).lower()
        )
        assert "gpt-4o" not in second  # never promise a revert target we can't know


async def test_model_command_works_after_configured_startup() -> None:
    """A runtime built from config.yaml at startup must seed _agent_settings
    so :model works without running the :ai wizard first."""
    from korvid.agent.setup import AgentConfigurator, AgentSettings
    from korvid.ui.messages import UnknownCommand

    saved: list[AgentSettings] = []

    class Cfg(AgentConfigurator):
        async def begin_device_login(self) -> Any:
            raise NotImplementedError

        async def finish_device_login(self) -> None:
            raise NotImplementedError

        async def test(self, settings: Any) -> str:
            return "ok"

        async def list_models(self, settings: Any) -> list[str]:
            return []

        async def save(self, settings: AgentSettings) -> None:
            saved.append(settings)

    runtime = cast("Any", StubRuntime([]))
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, PodSummary]]:
        yield ("ADDED", _pod("web-1"))
        while True:
            await asyncio.sleep(0.01)

    app = KorvidApp(
        config=KorvidConfig(
            namespace="default",
            agent_enabled=True,
            agent_provider="ollama",
            agent_base_url="http://localhost:11434/v1",
            agent_model="llama3",
            agent_auth_method="none",
        ),
        store=store,
        watch_manager=WatchManager(store, source),
        agent_runtime=runtime,
        agent_model_name="llama3",
        agent_configurator=Cfg(),
        rebuild_agent=lambda s: runtime,
    )
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        for _ in range(4):
            await pilot.pause()
        assert app._agent_model_name == "gpt-4o"
        assert saved
        assert saved[-1].provider == "ollama"
        assert saved[-1].model == "gpt-4o"


async def test_apply_agent_settings_notifies_on_rebuild_failure() -> None:
    from korvid.agent.setup import AgentSettings

    settings = AgentSettings(
        provider="openai-compat",
        auth_method="api_key",
        base_url="http://x/v1",
        model="m",
        api_key_env="MISSING_ENV",
    )
    app = make_app(runtime=None, model=None, rebuild_agent=lambda s: None)
    async with app.run_test() as pilot:
        app._apply_agent_settings(settings)
        await pilot.pause()
        msgs = [n.message for n in app._notifications]
        assert any("rebuild failed" in m.lower() for m in msgs)


async def test_rebuild_failure_keeps_previous_runtime_and_settings() -> None:
    from korvid.ui.messages import UnknownCommand

    old_runtime = cast("Any", StubRuntime([]))

    class Cfg2:
        async def begin_device_login(self) -> Any:
            raise NotImplementedError

        async def finish_device_login(self) -> None:
            raise NotImplementedError

        async def test(self, settings: Any) -> str:
            return "ok"

        async def list_models(self, settings: Any) -> list[str]:
            return []

        async def save(self, settings: Any) -> None:
            pass

    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, PodSummary]]:
        yield ("ADDED", _pod("web-1"))
        while True:
            await asyncio.sleep(0.01)

    app = KorvidApp(
        config=KorvidConfig(
            namespace="default",
            agent_enabled=True,
            agent_provider="ollama",
            agent_base_url="http://localhost:11434/v1",
            agent_model="llama3",
            agent_auth_method="none",
        ),
        store=store,
        watch_manager=WatchManager(store, source),
        agent_runtime=old_runtime,
        agent_model_name="llama3",
        agent_configurator=cast("Any", Cfg2()),
        rebuild_agent=lambda s: None,  # rebuild always fails
    )
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        for _ in range(4):
            await pilot.pause()
        # Transactional swap: the working runtime and settings must survive.
        assert app._agent_runtime is old_runtime
        assert app._agent_model_name == "llama3"
        assert app._agent_settings is not None
        assert app._agent_settings.model == "llama3"
        msgs = [n.message for n in app._notifications]
        assert not any("Agent model set" in m for m in msgs)  # no false success toast
        assert any("rebuild failed" in m.lower() for m in msgs)


async def test_model_switch_blocked_while_turn_running() -> None:
    from korvid.agent.setup import AgentSettings

    rebuilt: list[AgentSettings] = []
    new_runtime = cast("Any", StubRuntime([]))

    def rebuild(s: AgentSettings) -> Any:
        rebuilt.append(s)
        return new_runtime

    old_runtime = cast("Any", StubRuntime([]))
    app = make_app(runtime=old_runtime, model="llama3", rebuild_agent=rebuild)
    settings = AgentSettings(
        provider="ollama", auth_method="none", base_url="http://x/v1", model="new-model"
    )
    async with app.run_test() as pilot:
        app._agent_task = asyncio.create_task(asyncio.sleep(30))  # simulate a live turn
        try:
            app._apply_agent_settings(settings)
            await pilot.pause()
            assert not rebuilt  # swap must be blocked mid-turn
            assert app._agent_runtime is old_runtime
            msgs = [n.message for n in app._notifications]
            assert any("busy" in m.lower() for m in msgs)
        finally:
            app._agent_task.cancel()


async def test_input_reenabled_even_when_panel_closed() -> None:
    from korvid.agent.setup import AgentSettings

    new_runtime = cast("Any", StubRuntime([]))
    app = make_app(runtime=None, model=None, rebuild_agent=lambda s: new_runtime)
    settings = AgentSettings(
        provider="ollama", auth_method="none", base_url="http://x/v1", model="m"
    )
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")  # open unconfigured: hint disables input
        await pilot.press("ctrl+a")  # close panel
        app._apply_agent_settings(settings)
        await pilot.pause()
        await pilot.press("ctrl+a")  # reopen: input must be usable again
        assert app.query_one(AgentPanel).query_one("#agent-input", Input).disabled is False


async def test_model_query_requires_live_runtime() -> None:
    """`:model` must not report a model as active when the provider failed to
    build at startup (runtime None) even though config carried a model name."""
    from korvid.ui.messages import UnknownCommand

    # Startup with a config model name but no runtime (e.g. missing API key).
    app = make_app(runtime=None, model="gpt-4o")
    notices: list[str] = []
    app.notify = lambda msg, **kw: notices.append(str(msg))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("model"))
        await pilot.pause()
    assert notices, "expected a notification"
    assert "gpt-4o" not in notices[-1]
    assert "not configured" in notices[-1]


class FakeMCP:
    """Duck-typed MCPController: records lifecycle calls, scripted replies."""

    def __init__(self, start_msg: str = "MCP on :7878") -> None:
        self.calls: list[str] = []
        self.start_msg = start_msg
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> str:
        return "MCP on :7878" if self._running else "MCP off"

    async def start(self) -> str:
        self.calls.append("start")
        if not self.start_msg.startswith("ERROR"):
            self._running = True
        return self.start_msg

    async def stop(self) -> str:
        self.calls.append("stop")
        self._running = False
        return "MCP off"


async def test_mcp_command_toggles_server_and_status_bar() -> None:
    from korvid.ui.messages import UnknownCommand
    from korvid.ui.widgets.status_bar import StatusBar

    mcp = FakeMCP()
    app = make_app(StubRuntime([]), mcp=mcp)
    async with app.run_test() as pilot:
        assert "MCP off" in str(app.query_one(StatusBar).render())
        app.on_unknown_command(UnknownCommand("mcp on"))
        for _ in range(4):
            await pilot.pause()
        assert mcp.calls == ["start"]
        assert "MCP on :7878" in str(app.query_one(StatusBar).render())
        app.on_unknown_command(UnknownCommand("mcp off"))
        for _ in range(4):
            await pilot.pause()
        assert mcp.calls == ["start", "stop"]
        assert "MCP off" in str(app.query_one(StatusBar).render())


async def test_mcp_command_bare_and_bad_args_do_not_touch_server() -> None:
    from korvid.ui.messages import UnknownCommand

    mcp = FakeMCP()
    app = make_app(StubRuntime([]), mcp=mcp)
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("mcp"))
        app.on_unknown_command(UnknownCommand("mcp bogus"))
        await pilot.pause()
        assert mcp.calls == []


async def test_mcp_command_without_controller_does_not_crash() -> None:
    from korvid.ui.messages import UnknownCommand

    app = make_app(StubRuntime([]))
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("mcp on"))
        await pilot.pause()
