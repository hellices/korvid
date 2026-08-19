"""Tests for Task 9: Ctrl-A agent panel wiring."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

from textual.css.query import NoMatches
from textual.widgets import Input, OptionList

from korvid import __version__
from korvid.agent.events import AgentEvent, TextDelta, TurnComplete
from korvid.agent.outbound import OutboundSnapshot
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.messages import AgentPromptSubmitted
from korvid.ui.widgets.agent_panel import AgentPanel
from tests.ui.waits import until


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

    def __init__(
        self,
        events: list[AgentEvent],
        block: bool = False,
        snapshot: OutboundSnapshot | None = None,
    ) -> None:
        self._events = events
        self._block = block
        self.calls: list[tuple[str, str]] = []
        self.total_tokens = (0, 0)
        self.usage_estimated = False
        self.latest_outbound_payload = snapshot

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


def _snapshot() -> OutboundSnapshot:
    return OutboundSnapshot(
        model="ollama",
        iteration=1,
        payload_json='{"messages":[],"tools":[]}',
        redactions=(),
    )


def _notification_text(app: KorvidApp) -> str:
    return " ".join(str(notification.message) for notification in app._notifications)


def _agent_setup_screen_initialized(app: KorvidApp) -> bool:
    from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen

    if not isinstance(app.screen, AgentSetupScreen):
        return False
    try:
        provider_list = app.screen.query_one("#setup-provider", OptionList)
        auth_list = app.screen.query_one("#setup-auth", OptionList)
    except NoMatches:
        return False
    return (
        provider_list.highlighted == 0
        and auth_list.display is False
        and app.focused is provider_list
    )


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
        await until(pilot, lambda: runtime.calls, label="agent turn started")
        assert runtime.calls[0][0] == "how are my pods?"
        await until(
            pilot,
            lambda: "All pods healthy." in _panel_text(app),
            label="reply rendered",
        )
        assert "how are my pods?" in _panel_text(app)
        assert inp.disabled is False


async def test_screen_context_includes_current_view() -> None:
    runtime = StubRuntime([TurnComplete(input_tokens=0, output_tokens=0, estimated=True)])
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        inp.value = "q"
        await pilot.press("enter")
        await until(pilot, lambda: runtime.calls, label="agent turn started")
        ctx = runtime.calls[0][1]
        assert "view=pods" in ctx
        assert "scope=default" in ctx


async def test_screen_context_splits_selected_namespace_from_name() -> None:
    """Row keys are 'namespace/name' composites; fed verbatim as
    `selected=` they teach the model to paste the whole string as a pod
    name (observed: get_resource name='default/otel-…' -> 404). The
    context must hand the model the two fields it actually needs."""
    runtime = StubRuntime([TurnComplete(input_tokens=0, output_tokens=0, estimated=True)])
    app = make_app(runtime)
    async with app.run_test() as pilot:
        # Wait for the watch to land the row: on an empty table the context
        # reads `selected=-` and this test would pass without exercising
        # the split (review on #172).
        await until(
            pilot,
            lambda: (
                "selected=web-1" in app._screen_context()
                or "selected=default/web-1" in app._screen_context()
            ),
            label="pod row selected",
        )
        await pilot.press("ctrl+a")
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        inp.value = "q"
        await pilot.press("enter")
        await until(pilot, lambda: runtime.calls, label="agent turn started")
        ctx = runtime.calls[0][1]
        assert "selected=web-1" in ctx
        assert "selected_ns=default" in ctx
        assert "selected=default/web-1" not in ctx


async def test_second_submit_interrupts_and_replaces_running_turn() -> None:
    """Since issue #170 a submission while a turn runs is
    interrupt-and-submit: the old turn is cancelled and the new prompt
    starts a fresh turn."""
    runtime = StubRuntime([TextDelta(text="thinking")], block=True)
    app = make_app(runtime)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        panel = app.query_one(AgentPanel)
        inp = panel.query_one("#agent-input", Input)
        inp.value = "first"
        await pilot.press("enter")
        await until(pilot, lambda: bool(runtime.calls), label="first agent turn running")
        panel.post_message(AgentPromptSubmitted("second"))
        await until(
            pilot,
            lambda: [c[0] for c in runtime.calls] == ["first", "second"],
            label="replacement turn started",
        )
        assert [c[0] for c in runtime.calls] == ["first", "second"]


async def test_status_bar_reflects_runtime_not_config_flag() -> None:
    """create_provider can return None while agent_enabled stays true —
    the status label must track the actual runtime."""
    from korvid.ui.widgets.status_bar import StatusBar

    app = make_app(runtime=None)
    app.config = KorvidConfig(namespace="default", agent_enabled=True)
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: "AI off" in str(app.query_one(StatusBar).render()),
            label="status bar shows AI off",
        )
        assert "AI off" in str(app.query_one(StatusBar).render())


async def test_status_bar_on_when_runtime_present() -> None:
    from korvid.ui.widgets.status_bar import StatusBar

    app = make_app(StubRuntime([]))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: "AI on" in str(app.query_one(StatusBar).render()),
            label="status bar shows AI on",
        )
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
        await until(
            pilot,
            lambda: _agent_setup_screen_initialized(app),
            label="agent setup provider list initialized",
        )
        assert _agent_setup_screen_initialized(app)


async def test_ai_command_without_configurator_notifies() -> None:
    from korvid.ui.messages import UnknownCommand

    app = make_app(runtime=None, model=None)
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("ai"))
        await until(
            pilot,
            lambda: "Agent setup unavailable" in _notification_text(app),
            label="agent setup unavailable notification shown",
        )
        # No crash and no setup screen pushed.
        from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen

        text = _notification_text(app)
        requirement = f"korvid[agent]=={__version__}"
        assert "Agent setup unavailable" in text
        assert f"uv tool install --force '{requirement}'" in text
        assert f"pipx install --force '{requirement}'" in text
        assert not isinstance(app.screen, AgentSetupScreen)


async def test_ai_payload_without_runtime_notifies_agent_is_off() -> None:
    from korvid.ui.messages import UnknownCommand
    from korvid.ui.widgets.payload_inspector import PayloadInspectorScreen

    app = make_app(runtime=None, model=None)
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("ai payload"))
        await until(
            pilot,
            lambda: "Agent is off" in _notification_text(app),
            label="agent-off payload notification",
        )
        assert not isinstance(app.screen, PayloadInspectorScreen)


async def test_ai_payload_without_snapshot_notifies_nothing_was_sent() -> None:
    from korvid.ui.messages import UnknownCommand
    from korvid.ui.widgets.payload_inspector import PayloadInspectorScreen

    app = make_app(StubRuntime([]))
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("ai payload"))
        await until(
            pilot,
            lambda: "No provider payload has been sent" in _notification_text(app),
            label="missing payload notification",
        )
        assert not isinstance(app.screen, PayloadInspectorScreen)


async def test_ai_payload_refuses_transient_snapshot_during_busy_turn() -> None:
    from korvid.ui.messages import UnknownCommand
    from korvid.ui.widgets.payload_inspector import PayloadInspectorScreen

    runtime = StubRuntime([], block=True, snapshot=_snapshot())
    app = make_app(runtime)
    async with app.run_test() as pilot:
        app.on_agent_prompt_submitted(AgentPromptSubmitted("inspect pods"))
        await until(pilot, lambda: runtime.calls, label="agent turn running")
        app.on_unknown_command(UnknownCommand("ai payload"))
        await until(
            pilot,
            lambda: "busy" in _notification_text(app).lower(),
            label="busy payload refusal",
        )
        assert not isinstance(app.screen, PayloadInspectorScreen)


async def test_ai_payload_opens_inspector_for_idle_snapshot() -> None:
    from korvid.ui.messages import UnknownCommand
    from korvid.ui.widgets.payload_inspector import PayloadInspectorScreen

    app = make_app(StubRuntime([], snapshot=_snapshot()))
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("ai payload"))
        await until(
            pilot,
            lambda: isinstance(app.screen, PayloadInspectorScreen),
            label="payload inspector open",
        )
        assert isinstance(app.screen, PayloadInspectorScreen)


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
        await until(
            pilot,
            lambda: (
                app._agent_runtime is runtime and "AI on" in str(app.query_one(StatusBar).render())
            ),
            label="agent runtime rebuilt and status bar updated",
        )
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
        options={"tenant": "platform", "features": {"region": "apac"}},
    )
    async with app.run_test() as pilot:
        app._apply_agent_settings(settings)
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        await until(
            pilot,
            lambda: app._agent_model_name == "gpt-4o",
            label="model swap applied",
        )
        assert saved
        assert saved[-1].model == "gpt-4o"
        assert dict(saved[-1].options) == {"tenant": "platform", "features": {"region": "apac"}}
        assert rebuilt[-1].model == "gpt-4o"
        assert dict(rebuilt[-1].options) == {"tenant": "platform", "features": {"region": "apac"}}


async def test_model_command_without_config_does_not_crash() -> None:
    from korvid.ui.messages import UnknownCommand

    app = make_app(runtime=None, model=None)
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        app.on_unknown_command(UnknownCommand("model"))
        await until(
            pilot,
            lambda: len(app._notifications) >= 2,
            label="not-configured model notifications shown",
        )
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
        await until(
            pilot,
            lambda: len(rebuilds) >= 2,
            label="rebuild attempted",
        )
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
        await until(
            pilot,
            lambda: app._agent_model_name == "gpt-4o",
            label="first model swap",
        )
        assert app._agent_model_name == "gpt-4o"  # swap took effect
        notes = " ".join(str(n.message) for n in app._notifications)
        assert "disk full" in notes
        assert "revert" in notes.lower()

        # A second failed switch must not claim the app will revert to the
        # live-but-unsaved model: the in-memory snapshot is not what is on
        # disk, so the warning must not name a specific model at all.
        app.on_unknown_command(UnknownCommand("model claude-3"))
        await until(
            pilot,
            lambda: app._agent_model_name == "claude-3",
            label="second model swap",
        )
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
            agent_options={"tenant": "platform", "features": {"region": "apac"}},
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
        await until(
            pilot,
            lambda: app._agent_model_name == "gpt-4o",
            label="model swap from startup config",
        )
        assert saved
        assert saved[-1].provider == "ollama"
        assert saved[-1].model == "gpt-4o"
        assert dict(saved[-1].options) == {"tenant": "platform", "features": {"region": "apac"}}


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
        await until(
            pilot,
            lambda: any("rebuild failed" in str(n.message).lower() for n in app._notifications),
            label="agent rebuild failure notification shown",
        )
        msgs = [n.message for n in app._notifications]
        assert any("rebuild failed" in m.lower() for m in msgs)


async def test_apply_agent_settings_notifies_on_plugin_error() -> None:
    """ProviderPluginError raised by rebuild_agent must surface via the
    existing error notification path (rebuild failure), not crash the app."""
    from korvid.agent.setup import AgentSettings
    from korvid.providers.plugin_registry import ProviderPluginError

    settings = AgentSettings(
        provider="corp-llm",
        auth_method="api_key",
        base_url="http://x/v1",
        model="m",
    )

    def boom(s: Any) -> Any:
        raise ProviderPluginError("plugin auth mismatch")

    app = make_app(runtime=None, model=None, rebuild_agent=boom)
    async with app.run_test() as pilot:
        app._apply_agent_settings(settings)
        await until(
            pilot,
            lambda: any(
                "rebuild failed" in m.lower() or "plugin" in m.lower()
                for m in (n.message for n in app._notifications)
            ),
            label="plugin error notification",
        )
        msgs = [n.message for n in app._notifications]
        assert any("rebuild failed" in m.lower() or "plugin" in m.lower() for m in msgs)


async def test_options_preserved_across_model_change() -> None:
    """Options seeded from config must survive a :model switch."""
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

    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, PodSummary]]:
        yield ("ADDED", _pod("web-1"))
        while True:
            await asyncio.sleep(0.01)

    app = KorvidApp(
        config=KorvidConfig(
            namespace="default",
            agent_enabled=True,
            agent_provider="corp-llm",
            agent_base_url="http://x/v1",
            agent_model="m",
            agent_auth_method="api_key",
            agent_options={"tenant": "platform", "features": {"region": "apac"}},
        ),
        store=store,
        watch_manager=WatchManager(store, source),
        agent_runtime=runtime,
        agent_model_name="m",
        agent_configurator=Cfg(),
        rebuild_agent=rebuild,
    )
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        await until(
            pilot,
            lambda: rebuilt,
            label="rebuild triggered",
        )
        assert dict(rebuilt[-1].options) == {"tenant": "platform", "features": {"region": "apac"}}
        assert saved
        assert dict(saved[-1].options) == {"tenant": "platform", "features": {"region": "apac"}}


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
        await until(
            pilot,
            lambda: any("rebuild failed" in str(n.message).lower() for n in app._notifications),
            label="rebuild failure notification",
        )
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
            await until(
                pilot,
                lambda: any("busy" in str(n.message).lower() for n in app._notifications),
                label="busy model-switch refusal shown",
            )
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
        await until(
            pilot,
            lambda: app._agent_runtime is new_runtime,
            label="agent runtime rebuilt while panel closed",
        )
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
    async with app.run_test():
        app.on_unknown_command(UnknownCommand("model"))
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
        await until(
            pilot,
            lambda: mcp.calls == ["start"],
            label="MCP started",
        )
        assert "MCP on :7878" in str(app.query_one(StatusBar).render())
        app.on_unknown_command(UnknownCommand("mcp off"))
        await until(
            pilot,
            lambda: mcp.calls == ["start", "stop"],
            label="MCP stopped",
        )
        assert "MCP off" in str(app.query_one(StatusBar).render())


async def test_mcp_command_bare_and_bad_args_do_not_touch_server() -> None:
    from korvid.ui.messages import UnknownCommand

    mcp = FakeMCP()
    app = make_app(StubRuntime([]), mcp=mcp)
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("mcp"))
        app.on_unknown_command(UnknownCommand("mcp bogus"))
        await until(
            pilot,
            lambda: len(app._notifications) == 2,
            label="mcp usage notifications shown",
        )
        assert mcp.calls == []


async def test_mcp_command_without_controller_does_not_crash() -> None:
    from korvid.ui.messages import UnknownCommand

    app = make_app(StubRuntime([]))
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("mcp on"))
        await until(
            pilot,
            lambda: "MCP unavailable" in _notification_text(app),
            label="mcp unavailable notification shown",
        )
        text = _notification_text(app)
        requirement = f"korvid[mcp]=={__version__}"
        assert "MCP unavailable" in text
        assert f"uv tool install --force '{requirement}'" in text
        assert f"pipx install --force '{requirement}'" in text


async def test_agent_unavailable_mounts_no_panel_and_hides_the_binding() -> None:
    """A base install (no [agent] extra) shows no agent surface at all
    (issue #73): no panel in the DOM and Ctrl-A does nothing."""
    app = make_app(agent_available=False)
    async with app.run_test() as pilot:
        assert not app.query(AgentPanel)
        assert app.check_action("toggle_agent", ()) is False
        await pilot.press("ctrl+a")  # must be a no-op, not a NoMatches crash
        assert not app.query(AgentPanel)


async def test_agent_unavailable_makes_ai_and_model_unknown_commands() -> None:
    """`:ai` / `:model` are not registered without the [agent] extra —
    they fall through to the unknown-command message (issue #73)."""
    app = make_app(agent_available=False)
    async with app.run_test():
        msgs: list[str] = []
        app.notify = lambda msg, **kw: msgs.append(str(msg))  # type: ignore[method-assign]
        from korvid.ui.messages import UnknownCommand

        app.on_unknown_command(UnknownCommand("ai"))
        app.on_unknown_command(UnknownCommand("model"))
        assert len(msgs) == 2
        assert all("Unknown resource or command" in m for m in msgs)
