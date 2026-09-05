"""Tests for the Ctrl-A agent panel wiring over an `AgentSession`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from textual.css.query import NoMatches
from textual.widgets import Input

from korvid import __version__
from korvid.agent.events import AgentEvent, TextDelta, TurnComplete
from korvid.agent.model_policy import CapabilitySource, ModelTier
from korvid.agent.outbound import OutboundSnapshot
from korvid.agent.session import AgentSession
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.messages import AgentPromptSubmitted
from korvid.ui.widgets.agent_panel import AgentPanel
from tests.ui.agent_session_fakes import FakeSession, fake_policy
from tests.ui.test_agent_ui_controller_profiles import _StubCatalog
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


class StubSession(FakeSession):
    """Scripted stand-in for the production `AgentSession`."""

    def __init__(
        self,
        events: list[AgentEvent],
        block: bool = False,
        snapshot: OutboundSnapshot | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(events, block=block, snapshot=snapshot, **kwargs)


def make_app(
    session: AgentSession | None = None, model: str | None = "test-model", **kwargs: Any
) -> KorvidApp:
    if isinstance(session, FakeSession) and model is not None:
        session._policy = replace(
            session.policy,
            model=replace(session.policy.model, model=model),
        )
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, PodSummary]]:
        yield ("ADDED", _pod("web-1"))
        while True:
            await asyncio.sleep(0.01)

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        agent_session=session,
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
    """The wizard is open and has reached its first question.

    That question is Task 10's model search, pushed over the wizard: the
    stage machine starts by asking *what model*, so the search screen on
    top of a setup screen is the initialized state.
    """
    from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen
    from korvid.ui.widgets.model_search_screen import ModelSearchScreen

    if not any(isinstance(screen, AgentSetupScreen) for screen in app.screen_stack):
        return False
    if not isinstance(app.screen, ModelSearchScreen):
        return False
    try:
        query = app.screen.query_one("#model-query", Input)
    except NoMatches:
        return False
    return app.focused is query


async def test_ctrl_a_toggles_panel_display() -> None:
    app = make_app(StubSession([]))
    async with app.run_test() as pilot:
        panel = app.query_one(AgentPanel)
        assert panel.display is False
        await pilot.press("ctrl+a")
        assert panel.display is True
        await pilot.press("ctrl+a")
        assert panel.display is False


async def test_no_session_shows_setup_hint() -> None:
    app = make_app(session=None, model=None)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        assert "Run :ai" in _panel_text(app)
        assert app.query_one(AgentPanel).query_one("#agent-input", Input).disabled is True


async def test_prompt_drives_the_session_and_renders_the_reply() -> None:
    session = StubSession(
        [
            TextDelta(text="All pods healthy.\n"),
            TurnComplete(input_tokens=10, output_tokens=4, estimated=False),
        ]
    )
    app = make_app(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        inp.value = "how are my pods?"
        await pilot.press("enter")
        await until(pilot, lambda: session.prompts, label="agent turn started")
        assert session.prompts[0] == "how are my pods?"
        await until(
            pilot,
            lambda: "All pods healthy." in _panel_text(app),
            label="reply rendered",
        )
        assert "how are my pods?" in _panel_text(app)
        assert inp.disabled is False


async def test_second_submit_interrupts_and_replaces_running_turn() -> None:
    """Since issue #170 a submission while a turn runs is
    interrupt-and-submit: the old turn is cancelled and the new prompt
    starts a fresh turn."""
    session = StubSession([TextDelta(text="thinking")], block=True)
    app = make_app(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        panel = app.query_one(AgentPanel)
        inp = panel.query_one("#agent-input", Input)
        inp.value = "first"
        await pilot.press("enter")
        await until(pilot, lambda: bool(session.prompts), label="first agent turn running")
        panel.post_message(AgentPromptSubmitted("second"))
        await until(
            pilot,
            lambda: session.prompts == ["first", "second"],
            label="replacement turn started",
        )
        assert session.prompts == ["first", "second"]


async def test_status_bar_reflects_the_session_not_the_config_flag() -> None:
    """create_provider can return None while agent_enabled stays true —
    the status label must track the actual session."""
    from korvid.ui.widgets.status_bar import StatusBar

    app = make_app(session=None)
    app.config = KorvidConfig(namespace="default", agent_enabled=True)
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: "AI off" in str(app.query_one(StatusBar).render()),
            label="status bar shows AI off",
        )
        assert "AI off" in str(app.query_one(StatusBar).render())


async def test_status_bar_on_when_a_session_is_present() -> None:
    from korvid.ui.widgets.status_bar import StatusBar

    app = make_app(StubSession([]))
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: "AI on" in str(app.query_one(StatusBar).render()),
            label="status bar shows AI on",
        )
        assert "AI on" in str(app.query_one(StatusBar).render())


def _header_text(app: KorvidApp) -> str:
    from textual.widgets import Static

    return str(app.query_one(AgentPanel).query_one("#agent-header", Static).render())


async def test_header_shows_the_resolved_tier_and_its_provenance() -> None:
    """The header names the tier the *session* resolved, with where it
    came from — never the requested override the wizard/config carry."""
    session = StubSession(
        [], policy=fake_policy(tier=ModelTier.HIGH, route_source=CapabilitySource.USER)
    )
    app = make_app(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        await until(pilot, lambda: "high (user)" in _header_text(app), label="tier in header")
        assert "high (user)" in _header_text(app)


async def test_header_tier_survives_a_completed_turn() -> None:
    """`TurnComplete` re-renders the header from cached state; the tier
    marker must be replayed with the token counts, not dropped."""
    session = StubSession(
        [TurnComplete(input_tokens=7, output_tokens=2, estimated=False)],
        policy=fake_policy(tier=ModelTier.LOW, route_source=CapabilitySource.CATALOG),
    )
    app = make_app(session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        inp.value = "q"
        await pilot.press("enter")
        await until(pilot, lambda: "↑7" in _header_text(app), label="tokens rendered")
        assert "low (catalog)" in _header_text(app)


async def test_header_has_no_tier_marker_without_a_session() -> None:
    """No session, nothing to resolve a tier from — so the header keeps
    its default and never invents a routing decision."""
    app = make_app(session=None, model="test-model")
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        await pilot.pause()
        assert "(" not in _header_text(app)


async def test_setup_hint_not_duplicated_on_retoggle() -> None:
    app = make_app(session=None, model=None)
    async with app.run_test() as pilot:
        for _ in range(3):
            await pilot.press("ctrl+a")  # open
            await pilot.press("ctrl+a")  # close
        await pilot.press("ctrl+a")
        assert _panel_text(app).count("Run :ai") == 1


async def test_ai_command_pushes_setup_screen() -> None:
    from korvid.ui.messages import UnknownCommand

    app = make_app(
        session=None,
        model=None,
        agent_catalog=_StubCatalog(),
    )
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

    app = make_app(session=None, model=None)
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("ai"))
        await until(
            pilot,
            lambda: "Agent setup unavailable" in _notification_text(app),
            label="agent setup unavailable notification shown",
        )
        # No crash and no setup screen pushed.
        from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen

        notification = next(
            n for n in app._notifications if "Agent setup unavailable" in str(n.message)
        )
        text = str(notification.message)
        requirement = f"korvid[all,entra]=={__version__}"
        assert "Agent setup unavailable" in text
        assert "including agent" in text
        assert f"uv tool install --force '{requirement}'" in text
        assert f"pipx install --force '{requirement}'" in text
        assert notification.markup is False
        assert not isinstance(app.screen, AgentSetupScreen)


async def test_ai_payload_without_a_session_notifies_agent_is_off() -> None:
    from korvid.ui.messages import UnknownCommand
    from korvid.ui.widgets.payload_inspector import PayloadInspectorScreen

    app = make_app(session=None, model=None)
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

    app = make_app(StubSession([]))
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

    session = StubSession([], block=True, snapshot=_snapshot())
    app = make_app(session)
    async with app.run_test() as pilot:
        app.on_agent_prompt_submitted(AgentPromptSubmitted("inspect pods"))
        await until(pilot, lambda: session.prompts, label="agent turn running")
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

    app = make_app(StubSession([], snapshot=_snapshot()))
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

    session = StubSession([], policy=fake_policy(model="llama3"))
    settings = AgentSettings(
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434/v1",
        model="llama3",
    )
    app = make_app(session=None, model=None, rebuild_agent=lambda s: session)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")  # open panel: setup hint, input disabled
        app._agent_ui.apply_settings(settings)
        await until(
            pilot,
            lambda: (
                app._agent_ui.session is session
                and "AI on" in str(app.query_one(StatusBar).render())
            ),
            label="agent session rebuilt and status bar updated",
        )
        assert app._agent_ui.session is session
        assert app._agent_ui._model_name == "llama3"
        assert "AI on" in str(app.query_one(StatusBar).render())
        assert app.query_one(AgentPanel).query_one("#agent-input", Input).disabled is False


async def test_model_command_swaps_model_and_saves() -> None:
    """`:model` persists through the *profile* writer — the only path that
    writes `agent.profiles` — rather than a second, uncoordinated one."""
    from korvid.agent.setup import AgentSettings
    from korvid.core.config import ModelConnectionsConfig
    from korvid.ui.messages import UnknownCommand

    saved: list[ModelConnectionsConfig] = []
    rebuilt: list[AgentSettings] = []

    def rebuild(settings: AgentSettings) -> Any:
        rebuilt.append(settings)
        return StubSession([], policy=fake_policy(model=settings.model))

    app = make_app(
        session=None,
        model=None,
        agent_save_profiles=lambda profiles, **_kwargs: saved.append(profiles),
        rebuild_agent=rebuild,
    )
    settings = AgentSettings(
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434/v1",
        model="llama3",
        options={"tenant": "platform", "features": {"region": "apac"}},
    )
    async with app.run_test() as pilot:
        app._agent_ui.apply_settings(settings)
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        await until(
            pilot,
            lambda: app._agent_ui._model_name == "gpt-4o",
            label="model swap applied",
        )
        assert saved
        written = saved[-1]
        assert written.active is not None
        profile = written.profiles[written.active]
        assert profile.model == "ollama/gpt-4o"
        assert dict(profile.options) == {"tenant": "platform", "features": {"region": "apac"}}
        assert rebuilt[-1].model == "gpt-4o"
        assert dict(rebuilt[-1].options) == {"tenant": "platform", "features": {"region": "apac"}}


async def test_model_command_without_config_does_not_crash() -> None:
    from korvid.ui.messages import UnknownCommand

    app = make_app(session=None, model=None)
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        app.on_unknown_command(UnknownCommand("model"))
        await until(
            pilot,
            lambda: len(app._notifications) >= 2,
            label="not-configured model notifications shown",
        )
        assert app._agent_ui._model_name is None


async def test_model_command_does_not_persist_when_apply_fails() -> None:
    """If the session swap is refused (rebuild returns None), the new model
    must NOT be written to config.yaml — otherwise the failed change silently
    takes effect after restart."""
    from korvid.agent.setup import AgentSettings
    from korvid.core.config import ModelConnectionsConfig
    from korvid.ui.messages import UnknownCommand

    saved: list[ModelConnectionsConfig] = []

    session = StubSession([], policy=fake_policy(model="llama3"))
    rebuilds: list[AgentSettings] = []

    def rebuild(settings: AgentSettings) -> Any:
        rebuilds.append(settings)
        # First call (initial apply) succeeds; the :model rebuild fails.
        return session if len(rebuilds) == 1 else None

    app = make_app(
        session=None,
        model=None,
        agent_save_profiles=lambda profiles, **_kwargs: saved.append(profiles),
        rebuild_agent=rebuild,
    )
    settings = AgentSettings(
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434/v1",
        model="llama3",
    )
    async with app.run_test() as pilot:
        app._agent_ui.apply_settings(settings)
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        await until(
            pilot,
            lambda: len(rebuilds) >= 2,
            label="rebuild attempted",
        )
        assert app._agent_ui._model_name == "llama3"  # old session kept
        assert not saved  # and nothing was persisted


async def test_model_command_save_failure_warns_about_restart_revert() -> None:
    """If the swap succeeded but persisting failed, the user must be told the
    model is live now but will revert on restart."""
    from korvid.agent.setup import AgentSettings
    from korvid.core.config import ModelConnectionsConfig
    from korvid.ui.messages import UnknownCommand

    def explode(profiles: ModelConnectionsConfig, **_kwargs: Any) -> None:
        raise RuntimeError("disk full")

    app = make_app(
        session=None,
        model=None,
        agent_save_profiles=explode,
        rebuild_agent=lambda s: StubSession([], policy=fake_policy(model=s.model)),
    )
    settings = AgentSettings(
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434/v1",
        model="llama3",
    )
    async with app.run_test() as pilot:
        app._agent_ui.apply_settings(settings)
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        await until(
            pilot,
            lambda: app._agent_ui._model_name == "gpt-4o",
            label="first model swap",
        )
        assert app._agent_ui._model_name == "gpt-4o"  # swap took effect
        await pilot.pause()  # let the warning reach the notification rack
        notes = " ".join(str(n.message) for n in app._notifications)
        assert "disk full" in notes
        assert "revert" in notes.lower()

        # A second failed switch must not claim the app will revert to the
        # live-but-unsaved model: the in-memory snapshot is not what is on
        # disk, so the warning must not name a specific model at all.
        app.on_unknown_command(UnknownCommand("model claude-3"))
        await until(
            pilot,
            lambda: app._agent_ui._model_name == "claude-3",
            label="second model swap",
        )
        assert app._agent_ui._model_name == "claude-3"
        await pilot.pause()
        second = " ".join(
            str(n.message)
            for n in app._notifications
            if "claude-3" in str(n.message) or "save failed" in str(n.message).lower()
        )
        assert "gpt-4o" not in second  # never promise a revert target we can't know


async def test_model_command_works_after_configured_startup() -> None:
    """A session built from config.yaml at startup must seed _agent_settings
    so :model works without running the :ai wizard first."""
    from korvid.core.config import ModelConnectionsConfig
    from korvid.ui.messages import UnknownCommand

    saved: list[ModelConnectionsConfig] = []

    session = StubSession([], policy=fake_policy(model="llama3"))
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
        agent_session=session,
        agent_model_name="llama3",
        agent_save_profiles=lambda profiles, **_kwargs: saved.append(profiles),
        rebuild_agent=lambda s: StubSession([], policy=fake_policy(model=s.model)),
    )
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        await until(
            pilot,
            lambda: app._agent_ui._model_name == "gpt-4o",
            label="model swap from startup config",
        )
        assert saved
        written = saved[-1]
        assert written.active is not None
        profile = written.profiles[written.active]
        assert profile.model == "ollama/gpt-4o"
        assert profile.endpoint == "http://localhost:11434/v1"
        assert dict(profile.options) == {"tenant": "platform", "features": {"region": "apac"}}


async def test_model_command_recovers_a_startup_that_built_no_session() -> None:
    """A startup that degraded to "AI off" is recoverable through `:model`.

    Composing the session at startup can fail on a model the router
    refuses (a provider reporting `supports_tools=False`) while config.yaml
    still names a perfectly good provider, endpoint and auth method. The
    settings snapshot the recovery edits comes from *config*, so the fix
    is one `:model <name>` — not a full re-run of the `:ai` wizard, which
    would ask the operator to retype everything korvid already knows.
    """
    from korvid.agent.setup import AgentSettings
    from korvid.core.config import ModelConnectionsConfig
    from korvid.ui.messages import UnknownCommand

    saved: list[ModelConnectionsConfig] = []
    applied: list[AgentSettings] = []

    rebuilt = StubSession([], policy=fake_policy(model="llama3"))
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, PodSummary]]:
        yield ("ADDED", _pod("web-1"))
        while True:
            await asyncio.sleep(0.01)

    def rebuild(settings: AgentSettings) -> Any:
        applied.append(settings)
        return rebuilt

    app = KorvidApp(
        config=KorvidConfig(
            namespace="default",
            agent_enabled=True,
            agent_provider="ollama",
            agent_base_url="http://localhost:11434/v1",
            agent_model="text-only-model",
            agent_auth_method="none",
            agent_model_tier="low",
        ),
        store=store,
        watch_manager=WatchManager(store, source),
        # The composition root degraded: a provider it could not route.
        agent_session=None,
        agent_model_name=None,
        agent_save_profiles=lambda profiles, **_kwargs: saved.append(profiles),
        rebuild_agent=rebuild,
    )
    async with app.run_test() as pilot:
        assert app._agent_ui.session is None
        app.on_unknown_command(UnknownCommand("model llama3"))
        await until(
            pilot,
            lambda: app._agent_ui.session is rebuilt,
            label="degraded startup recovered by :model",
        )
        assert app._agent_ui._model_name == "llama3"
        assert saved
        written = saved[-1]
        assert written.active is not None
        profile = written.profiles[written.active]
        assert profile.model == "ollama/llama3"
        assert profile.endpoint == "http://localhost:11434/v1"
        assert applied[-1].model_tier == "low"  # the configured override survives
        assert not any("not configured" in str(n.message).lower() for n in app._notifications)


async def test_a_degraded_startup_shows_a_usable_panel_after_recovery() -> None:
    """And the panel comes back as a working agent: header rendered, input
    enabled — never the never-configured setup wipe."""
    rebuilt = StubSession(
        [],
        policy=fake_policy(tier=ModelTier.LOW, model="text-only-model"),
    )
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
            agent_model="text-only-model",
            agent_auth_method="none",
        ),
        store=store,
        watch_manager=WatchManager(store, source),
        agent_session=None,
        agent_model_name=None,
        rebuild_agent=lambda s: rebuilt,
    )
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")
        settings = app._agent_ui.settings
        assert settings is not None
        assert app._agent_ui.apply_settings(settings) is True
        await until(
            pilot,
            lambda: "text-only-model" in _header_text(app),
            label="header rendered after recovery",
        )
        inp = app.query_one(AgentPanel).query_one("#agent-input", Input)
        assert inp.disabled is False


async def test_apply_agent_settings_notifies_on_rebuild_failure() -> None:
    from korvid.agent.setup import AgentSettings

    settings = AgentSettings(
        provider="openai-compat",
        auth_method="api_key",
        base_url="http://x/v1",
        model="m",
        api_key_env="MISSING_ENV",
    )
    app = make_app(session=None, model=None, rebuild_agent=lambda s: None)
    async with app.run_test() as pilot:
        app._agent_ui.apply_settings(settings)
        await until(
            pilot,
            lambda: any("rebuild failed" in str(n.message).lower() for n in app._notifications),
            label="agent rebuild failure notification shown",
        )
        msgs = [n.message for n in app._notifications]
        assert any("rebuild failed" in m.lower() for m in msgs)


async def test_apply_agent_settings_without_rebuild_agent_shows_literal_hint() -> None:
    from korvid.agent.setup import AgentSettings

    settings = AgentSettings(
        provider="openai-compat",
        auth_method="api_key",
        base_url="http://x/v1",
        model="m",
        api_key_env="MISSING_ENV",
    )
    app = make_app(session=None, model=None)
    async with app.run_test() as pilot:
        app._agent_ui.apply_settings(settings)
        await until(
            pilot,
            lambda: any("Agent rebuild unavailable" in str(n.message) for n in app._notifications),
            label="agent rebuild unavailable notification shown",
        )
        notification = next(
            n for n in app._notifications if "Agent rebuild unavailable" in str(n.message)
        )
        text = str(notification.message)
        requirement = f"korvid[all,entra]=={__version__}"
        assert "Agent rebuild unavailable" in text
        assert "including agent" in text
        assert f"uv tool install --force '{requirement}'" in text
        assert f"pipx install --force '{requirement}'" in text
        assert notification.markup is False


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

    app = make_app(session=None, model=None, rebuild_agent=boom)
    async with app.run_test() as pilot:
        app._agent_ui.apply_settings(settings)
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


async def test_apply_agent_settings_notifies_on_install_hint_rebuild_error() -> None:
    from korvid.agent.install_hint import isolated_install_hint
    from korvid.agent.setup import AgentSettings

    settings = AgentSettings(
        provider="corp-llm",
        auth_method="api_key",
        base_url="http://x/v1",
        model="m",
    )
    requirement = f"korvid[all,entra]=={__version__}"

    def boom(s: Any) -> Any:
        raise RuntimeError(isolated_install_hint(feature="agent"))

    app = make_app(session=None, model=None, rebuild_agent=boom)
    async with app.run_test() as pilot:
        app._agent_ui.apply_settings(settings)
        await until(
            pilot,
            lambda: any("rebuild failed" in str(n.message).lower() for n in app._notifications),
            label="hint-bearing rebuild exception notification",
        )
        notification = next(
            n for n in app._notifications if "Agent rebuild failed:" in str(n.message)
        )
        text = str(notification.message)
        assert "Agent rebuild failed:" in text
        assert "including agent" in text
        assert f"uv tool install --force '{requirement}'" in text
        assert f"pipx install --force '{requirement}'" in text
        assert "pip install" not in text
        assert notification.markup is False


async def test_options_preserved_across_model_change() -> None:
    """Options seeded from config must survive a :model switch."""
    from korvid.agent.setup import AgentSettings
    from korvid.core.config import ModelConnectionsConfig
    from korvid.ui.messages import UnknownCommand

    saved: list[ModelConnectionsConfig] = []

    rebuilt: list[AgentSettings] = []
    session = StubSession([])

    def rebuild(settings: AgentSettings) -> Any:
        rebuilt.append(settings)
        return session

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
            agent_api_key_env="CORP_LLM_KEY",
            agent_options={"tenant": "platform", "features": {"region": "apac"}},
        ),
        store=store,
        watch_manager=WatchManager(store, source),
        agent_session=session,
        agent_model_name="m",
        agent_save_profiles=lambda profiles, **_kwargs: saved.append(profiles),
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
        written = saved[-1]
        assert written.active is not None
        profile = written.profiles[written.active]
        assert dict(profile.options) == {"tenant": "platform", "features": {"region": "apac"}}
        assert profile.auth.method == "environment"
        assert profile.auth.settings["key"] == "CORP_LLM_KEY"


async def test_rebuild_failure_keeps_previous_runtime_and_settings() -> None:
    from korvid.ui.messages import UnknownCommand

    old_session = StubSession([], policy=fake_policy(model="llama3"))

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
        agent_session=old_session,
        agent_model_name="llama3",
        rebuild_agent=lambda s: None,  # rebuild always fails
    )
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("model gpt-4o"))
        await until(
            pilot,
            lambda: any("rebuild failed" in str(n.message).lower() for n in app._notifications),
            label="rebuild failure notification",
        )
        # Transactional swap: the working session and settings must survive.
        assert app._agent_ui.session is old_session
        assert app._agent_ui._model_name == "llama3"
        assert app._agent_ui._settings is not None
        assert app._agent_ui._settings.model == "llama3"
        msgs = [n.message for n in app._notifications]
        assert not any("Agent model set" in m for m in msgs)  # no false success toast
        assert any("rebuild failed" in m.lower() for m in msgs)


async def test_model_switch_blocked_while_turn_running() -> None:
    from korvid.agent.setup import AgentSettings

    rebuilt: list[AgentSettings] = []
    new_runtime = StubSession([])

    def rebuild(s: AgentSettings) -> Any:
        rebuilt.append(s)
        return new_runtime

    old_session = StubSession([])
    app = make_app(session=old_session, model="llama3", rebuild_agent=rebuild)
    settings = AgentSettings(
        provider="ollama", auth_method="none", base_url="http://x/v1", model="new-model"
    )
    async with app.run_test() as pilot:
        app._agent_ui._task = asyncio.create_task(asyncio.sleep(30))  # simulate a live turn
        try:
            app._agent_ui.apply_settings(settings)
            await until(
                pilot,
                lambda: any("busy" in str(n.message).lower() for n in app._notifications),
                label="busy model-switch refusal shown",
            )
            assert not rebuilt  # swap must be blocked mid-turn
            assert app._agent_ui.session is old_session
            msgs = [n.message for n in app._notifications]
            assert any("busy" in m.lower() for m in msgs)
        finally:
            app._agent_ui._task.cancel()


async def test_input_reenabled_even_when_panel_closed() -> None:
    from korvid.agent.setup import AgentSettings

    new_runtime = StubSession([])
    app = make_app(session=None, model=None, rebuild_agent=lambda s: new_runtime)
    settings = AgentSettings(
        provider="ollama", auth_method="none", base_url="http://x/v1", model="m"
    )
    async with app.run_test() as pilot:
        await pilot.press("ctrl+a")  # open unconfigured: hint disables input
        await pilot.press("ctrl+a")  # close panel
        app._agent_ui.apply_settings(settings)
        await until(
            pilot,
            lambda: app._agent_ui.session is new_runtime,
            label="agent session rebuilt while panel closed",
        )
        await pilot.press("ctrl+a")  # reopen: input must be usable again
        assert app.query_one(AgentPanel).query_one("#agent-input", Input).disabled is False


async def test_model_query_requires_live_runtime() -> None:
    """`:model` must not report a model as active when the provider failed to
    build at startup (session None) even though config carried a model name."""
    from korvid.ui.messages import UnknownCommand

    # Startup with a config model name but no session (e.g. missing API key).
    app = make_app(session=None, model="gpt-4o")
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
    app = make_app(StubSession([]), mcp=mcp)
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
    app = make_app(StubSession([]), mcp=mcp)
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

    app = make_app(StubSession([]))
    async with app.run_test() as pilot:
        app.on_unknown_command(UnknownCommand("mcp on"))
        await until(
            pilot,
            lambda: "MCP unavailable" in _notification_text(app),
            label="mcp unavailable notification shown",
        )
        notification = next(n for n in app._notifications if "MCP unavailable" in str(n.message))
        text = str(notification.message)
        requirement = f"korvid[all,entra]=={__version__}"
        assert "MCP unavailable" in text
        assert "including mcp" in text
        assert f"uv tool install --force '{requirement}'" in text
        assert f"pipx install --force '{requirement}'" in text
        assert notification.markup is False


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
