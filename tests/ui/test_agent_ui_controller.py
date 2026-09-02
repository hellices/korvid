"""Direct tests for `AgentUiController` — the built-in agent's UI ownership
(issue #187 / Deep Task 6).

The controller owns the agent's session state (session, settings, model
tier, model, disconnect marker, follow flag), the turn task lifecycle including
interrupt-and-submit, the screen context the model is told about, and every
`UIBridge` read the agent (or an MCP follow mirror) drives. It reaches
Textual only through `UiSurface` and the named ports below, so all of that is
exercised here without a running app.

Proposal persistence, review and execution are *not* here: they stay behind
the `AgentProposals` port for Deep Task 7, and the tests pin that the
controller only ever delegates to it.

`WriteCoordinator` is constructed for real, so "an agent write cannot bypass
the perimeter" is something these tests observe rather than a claim.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from textual.screen import Screen

from korvid.agent.events import (
    AgentError,
    AgentEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
    TurnInterrupted,
)
from korvid.agent.evidence import EvidenceLedger
from korvid.agent.interaction import ResourceIdentity
from korvid.agent.model_policy import CapabilitySource, ModelTier
from korvid.agent.session import AgentSession
from korvid.agent.setup import AgentSettings
from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import PodSummary
from korvid.k8s.writes import WriteOps
from korvid.ui.agent_ui_controller import (
    AgentPanelPort,
    AgentProposals,
    AgentScreens,
    AgentToolUIBridge,
    AgentUiController,
    DisplayedPaneContext,
)
from korvid.ui.bridge_dispatch import AppContextDispatch
from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.workspace_state import WorkspaceState
from korvid.ui.write_coordinator import WriteCoordinator

from .agent_session_fakes import FakeSession, fake_policy
from .test_write_coordinator import BrokenAudit, FakeContext, FakeTimeline, FakeUi, FakeView

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))

_ALIASES = {"pods": _PODS_META, "deployments": _DEPLOY_META}


async def settle(times: int = 8) -> None:
    """Let queued callbacks and freshly spawned tasks take their next step."""
    for _ in range(times):
        await asyncio.sleep(0)


def _pod(name: str, namespace: str = "default") -> PodSummary:
    return PodSummary(
        name=name,
        namespace=namespace,
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        qos="-",
        containers=("app",),
    )


_POD_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": "web-1", "namespace": "default", "uid": "uid-1"},
    "spec": {"containers": [{"name": "app"}]},
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePanel(AgentPanelPort):
    """Records every panel operation the controller performs."""

    def __init__(self, *, mounted: bool = True) -> None:
        self.mounted = mounted
        self.visible = False
        self.calls: list[str] = []
        self.events: list[AgentEvent] = []
        self.turns: list[tuple[str, bool]] = []
        self.echoed: list[str] = []
        self.headers: list[str] = []
        self.tiers: list[str | None] = []
        #: The absolute totals each `set_header` painted, in order.
        self.header_totals: list[tuple[int, int]] = []
        self.stop_key = ""
        self.input_enabled = True
        self.focused = 0

    def expanded(self) -> bool:
        return self.mounted and self.visible

    def show(self) -> None:
        self.visible = True
        self.calls.append("show")

    def hide(self) -> None:
        self.visible = False
        self.calls.append("hide")

    def focus_input(self) -> None:
        self.focused += 1

    def enable_input(self) -> None:
        self.input_enabled = True
        self.calls.append("enable_input")

    def set_header(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        estimated: bool,
        tier: str | None = None,
    ) -> None:
        self.headers.append(model)
        self.tiers.append(tier)
        self.header_totals.append((input_tokens, output_tokens))

    def show_setup_hint(self) -> None:
        self.calls.append("setup_hint")

    def show_reconnect_hint(self) -> None:
        self.calls.append("reconnect_hint")

    def set_stop_key(self, key: str) -> None:
        self.stop_key = key

    def interrupt_key(self) -> str:
        return "ctrl+x"

    def begin_turn(self, text: str, *, echo: bool) -> None:
        self.turns.append((text, echo))

    def echo_user(self, text: str) -> None:
        self.echoed.append(text)

    def apply_event(self, event: AgentEvent) -> None:
        self.events.append(event)


class FakeScreens(AgentScreens):
    """Screen-stack facts, dismissals, and the non-modal describe pane."""

    def __init__(self) -> None:
        self.approval_open = False
        self.describe_open = False
        self.top: object | None = None
        self.stacked: list[Screen[Any]] = []
        self.dismissed: list[Screen[Any]] = []
        self.panes: list[tuple[str, dict[str, Any], str | None]] = []
        self.row: str | None = "default/web-1"

    def approval_dialog_active(self) -> bool:
        return self.approval_open

    def describe_screen_open(self) -> bool:
        return self.describe_open

    def top_screen(self) -> object | None:
        return self.top

    def is_stacked(self, screen: Screen[Any]) -> bool:
        return screen in self.stacked

    def dismiss_if_current(self, screen: Screen[Any]) -> None:
        self.dismissed.append(screen)
        if screen in self.stacked:
            self.stacked.remove(screen)

    def selected_row_key(self) -> str | None:
        return self.row

    def show_describe_pane(
        self,
        title: str,
        manifest: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        footer_note: str | None,
    ) -> None:
        self.panes.append((title, manifest, footer_note))

    def selected_identity(self, table_id: str, kind: str) -> ResourceIdentity | None:
        return None

    def displayed_pane_context(self) -> DisplayedPaneContext | None:
        return None


class FakeNavigation:
    """The workspace transitions the agent's read tools drive."""

    def __init__(self, *, workspace: WorkspaceState, drill_error: str | None = None) -> None:
        self.workspace = workspace
        self.drill_error = drill_error
        self.navigations: list[tuple[str | None, str | None]] = []
        self.filters: list[str] = []
        self.drills: list[tuple[str, str]] = []
        self.navigate_error: Exception | None = None

    async def navigate_command(self, view: str | None, namespace: str | None) -> None:
        if self.navigate_error is not None:
            raise self.navigate_error
        self.navigations.append((view, namespace))
        if view is not None:
            self.workspace.current_kind = view
        if namespace is not None:
            self.workspace.current_scope = namespace

    def set_filter(self, pattern: str) -> None:
        self.filters.append(pattern)
        self.workspace.filter_pattern = pattern

    def clear_filter(self) -> None:
        self.filters.append("")
        self.workspace.filter_pattern = ""

    async def drill_into(self, namespace: str, name: str) -> str | None:
        self.drills.append((namespace, name))
        return self.drill_error


class FakeLogs:
    """The log-pane surface an agent log open drives."""

    def __init__(self) -> None:
        self.pane_gen = 0
        self.cancelled = 0
        self.opened: list[tuple[str, list[tuple[str, str, str]]]] = []
        self.on_cancel: Any = None

    async def cancel_tasks(self) -> None:
        self.cancelled += 1
        if self.on_cancel is not None:
            self.on_cancel()

    async def open_agent_logs(self, namespace: str, triples: list[tuple[str, str, str]]) -> None:
        self.opened.append((namespace, list(triples)))


class FakeProposals(AgentProposals):
    """The Deep Task 7 reserve: records delegations, performs nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        self.calls.append(("submit", (action, kind, name, namespace, session_id)))
        return "proposal p-1 is pending user review in the TUI"

    async def get_write_proposal(self, proposal_id: str) -> str:
        self.calls.append(("get", (proposal_id,)))
        return f"proposal {proposal_id}: pending"

    async def cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        self.calls.append(("cancel", (proposal_id, session_id)))
        return f"proposal {proposal_id} cancelled"


class RecordingOps(WriteOps):
    """WriteOps fake: records awaited mutations and previews."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.previews: list[str] = []

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.calls.append(("delete", meta.plural, namespace, name, uid))

    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        self.calls.append(("scale", meta.plural, namespace, name, replicas, uid))

    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.calls.append(("rollout_restart", meta.plural, namespace, name, uid))

    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        self.calls.append(("replace", meta.plural, namespace, name, uid))

    async def preview_delete(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> list[str] | None:
        self.previews.append("delete")
        return ["- pods/web-1"]


class ScriptedSession(FakeSession):
    """An `AgentSession` replaying a fixed event script."""

    def __init__(
        self,
        events: list[AgentEvent] | None = None,
        *,
        gate: asyncio.Event | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(events or [], gate=gate, tokens=(1, 2), **kwargs)


class StrictSession(ScriptedSession):
    """A scripted session that enforces the live idle contract.

    `DefaultAgentSession.run_turn` refuses a turn while the previous one
    still holds the session — the turn is given back only when its
    generator is driven to the end *or* closed. A fake that lets the next
    turn start regardless would hide exactly the failure this pins: a
    consumer that raises mid-stream abandons the generator suspended at
    its yield, and every later turn is then refused for the life of the
    session.
    """

    def __init__(
        self,
        events: list[AgentEvent] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(events, **kwargs)
        self.started = 0

    def run_turn(self, user_text: str) -> AsyncIterator[AgentEvent]:
        if self.started != self.iterators_released or self.finalization_pending:
            raise RuntimeError("a turn is already running")
        self.started += 1
        return super().run_turn(user_text)


class ExplodingPanel(FakePanel):
    """A panel that fails while rendering one streamed event.

    Models the real failure mode: a widget operation raising deep inside
    `apply_event` (a torn-down node, a broken markup render) while the
    session's generator is suspended at the yield that produced it.
    """

    def __init__(self, *, on: type[AgentEvent], error: Exception | None = None) -> None:
        super().__init__()
        self._on = on
        self._error = error if error is not None else RuntimeError("panel exploded")
        self.exploded = 0

    def apply_event(self, event: AgentEvent) -> None:
        if isinstance(event, self._on):
            self.exploded += 1
            raise self._error
        super().apply_event(event)


class Env:
    """An `AgentUiController` plus every fake it was built from."""

    def __init__(
        self,
        *,
        tmp_path: Path,
        session: AgentSession | None = None,
        available: bool = True,
        audit: str = "working",
        manifests: dict[tuple[str, str | None, str], dict[str, Any]] | None = None,
        manifest_error: BaseException | None = None,
        rows: list[Any] | None = None,
        config: KorvidConfig | None = None,
        ops: RecordingOps | None = None,
        follow_bridge: Any = None,
        configurator: Any = None,
        rebuild: Any = None,
        disconnect: Any = None,
        with_manifest: bool = True,
        with_logs: bool = True,
        panel: FakePanel | None = None,
    ) -> None:
        self.ui = FakeUi()
        self.panel = panel if panel is not None else FakePanel()
        self.screens = FakeScreens()
        self.context = FakeContext()
        self.timeline = FakeTimeline()
        self.workspace = WorkspaceState("pods", "default")
        self.navigation = FakeNavigation(workspace=self.workspace)
        self.logs = FakeLogs()
        self.proposals = FakeProposals()
        self.view = FakeView(aliases=_ALIASES, rows=rows if rows is not None else [_pod("web-1")])
        self.config = config if config is not None else KorvidConfig(namespace="default")
        self.audit_path = tmp_path / "audit.jsonl"
        self.audit: AuditLog | None
        if audit == "working":
            self.audit = AuditLog(self.audit_path, context="test")
        elif audit == "broken":
            self.audit = BrokenAudit(self.audit_path, context="test")
        else:
            self.audit = None
        self.ops = ops
        self.manifests = manifests if manifests is not None else {}
        self.manifest_error = manifest_error
        self.manifest_calls: list[tuple[str, str | None, str]] = []
        self.events_fetched: list[tuple[str, str]] = []
        self.dispatch = AppContextDispatch()
        self.dispatch.activate()
        self.writes = WriteCoordinator(
            ui=self.ui,
            view=self.view,
            context=self.context,
            audit=lambda: self.audit,
            timeline=self.timeline,
            check_permission=lambda: None,
            relationship_loader=lambda: None,
            focused_pane=lambda: self.workspace.focused,
            canonical_meta_kind=lambda meta: meta.plural,
        )
        self.controller = AgentUiController(
            panel=self.panel,
            screens=self.screens,
            ui=self.ui,
            view=self.view,
            context=self.context,
            writes=self.writes,
            workspace=self.workspace,
            navigation=self.navigation,
            logs=self.logs,
            proposals=self.proposals,
            dispatch=self.dispatch,
            config=lambda: self.config,
            get_manifest=lambda: self._manifest if with_manifest else None,
            get_events=lambda: self,
            stream_logs=lambda: self._stream if with_logs else None,
            pod_containers=self._pod_containers,
            write_ops=lambda: self.ops,
            audit=lambda: self.audit,
            pod_resize_supported=lambda: True,
            provider_hint=lambda: None,
            follow_bridge=lambda: follow_bridge,
            session=session,
            model_name="m-1",
            configurator=configurator,
            rebuild=rebuild,
            disconnect=disconnect,
            available=available,
        )

    async def _manifest(self, kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        self.manifest_calls.append((kind, namespace, name))
        if self.manifest_error is not None:
            raise self.manifest_error
        found = self.manifests.get((kind, namespace, name))
        return found if found is not None else _POD_MANIFEST

    async def fetch(self, namespace: str, name: str) -> list[dict[str, Any]]:
        self.events_fetched.append((namespace, name))
        return []

    def _pod_containers(self, namespace: str, name: str) -> tuple[str, ...]:
        return ("app",)

    async def _stream(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - never run
        raise NotImplementedError

    async def approve(self) -> None:
        """Answer the pending approval dialog with the user's `y`."""
        await asyncio.sleep(0)
        self.ui.answer(True)

    async def close(self) -> None:
        await self.controller.shutdown()
        await self.dispatch.shutdown()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path=tmp_path)


# ---------------------------------------------------------------------------
# Session absent / disconnected / setup transitions
# ---------------------------------------------------------------------------


async def test_toggling_the_panel_without_a_session_shows_the_setup_hint(env: Env) -> None:
    env.controller.toggle_panel()
    assert env.panel.visible is True
    assert env.panel.calls[-1] == "setup_hint"


async def test_toggling_the_panel_after_disconnect_shows_the_reconnect_hint(
    tmp_path: Path,
) -> None:
    session = ScriptedSession()
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.handle_command(["off"])
    assert env.controller.session is None
    env.controller.toggle_panel()
    assert env.panel.calls[-1] == "reconnect_hint"
    assert "setup_hint" not in env.panel.calls


async def test_ai_off_is_refused_while_a_turn_runs(tmp_path: Path) -> None:
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("why is web-1 sad?")
    await asyncio.sleep(0)
    env.controller.handle_command(["off"])
    assert env.controller.session is session
    assert any("busy" in message for message in env.ui.messages())
    gate.set()
    await env.close()


async def test_toggling_a_configured_panel_sets_the_header_and_focuses_the_input(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path=tmp_path, session=ScriptedSession())
    env.controller.toggle_panel()
    assert env.panel.headers == ["m-1"]
    assert env.panel.focused == 1
    env.controller.toggle_panel()
    assert env.panel.visible is False


async def test_header_uses_the_session_resolved_model_name(tmp_path: Path) -> None:
    session = ScriptedSession(policy=fake_policy(model="plugin-resolved-model"))
    env = Env(tmp_path=tmp_path, session=session)

    env.controller.toggle_panel()

    assert env.panel.headers == ["plugin-resolved-model"]


async def test_agent_setup_without_a_configurator_reports_the_install_hint(env: Env) -> None:
    env.controller.handle_command([])
    assert env.ui.screens == []
    assert any("Agent setup unavailable" in message for message in env.ui.messages())


async def test_agent_commands_reject_unknown_or_trailing_arguments(tmp_path: Path) -> None:
    session = ScriptedSession()
    env = Env(tmp_path=tmp_path, session=session)
    initial_follow = env.controller.follow_enabled
    env.controller.handle_command(["off", "extra"])
    env.controller.handle_command(["follow", "off", "extra"])
    env.controller.handle_command(["payload", "extra"])
    env.controller.handle_command(["sideways"])
    assert env.controller.session is session
    assert env.controller.follow_enabled is initial_follow
    assert sum("Usage: :ai" in message for message in env.ui.messages()) == 4


async def test_model_command_rejects_trailing_arguments(env: Env) -> None:
    env.controller.handle_model_command(["m-2", "extra"])
    assert env.ui.workers == []
    assert "Usage: :model [name]" in env.ui.messages()


async def test_applying_settings_swaps_the_session_and_the_configured_tier(
    tmp_path: Path,
) -> None:
    fresh = ScriptedSession(policy=fake_policy(model="m-2"))
    env = Env(tmp_path=tmp_path, rebuild=lambda settings: fresh)
    settings = AgentSettings(
        provider="ollama", auth_method="none", base_url=None, model="m-2", model_tier="low"
    )
    assert env.controller.apply_settings(settings) is True
    assert env.controller.session is fresh
    assert env.controller.model_name == "m-2"
    assert env.controller.configured_model_tier == "low"


async def test_a_failed_rebuild_keeps_the_previous_session(tmp_path: Path) -> None:
    previous = ScriptedSession()
    env = Env(tmp_path=tmp_path, session=previous, rebuild=lambda settings: None)
    settings = AgentSettings(
        provider="ollama", auth_method="none", base_url=None, model="m-2", model_tier="high"
    )
    assert env.controller.apply_settings(settings) is False
    assert env.controller.session is previous


# ---------------------------------------------------------------------------
# Degraded startup: configured on disk, but no session was composed
# ---------------------------------------------------------------------------


_DEGRADED_CONFIG = KorvidConfig(
    namespace="default",
    agent_enabled=True,
    agent_provider="ollama",
    agent_auth_method="none",
    agent_base_url="http://localhost:11434/v1",
    agent_model="llama3",
    agent_model_tier="low",
)


async def test_settings_are_seeded_from_config_even_without_a_session(tmp_path: Path) -> None:
    """What the operator configured is a fact about *config*, not about
    whether the composition root managed to build a session from it.

    A startup that degrades — a model the router refuses for reporting no
    tool support, a provider that failed to build — leaves the agent off
    with the same config.yaml on disk. Seeding the snapshot only when a
    session exists makes that state unrecoverable except by re-running the
    whole `:ai` wizard.
    """
    env = Env(tmp_path=tmp_path, session=None, config=_DEGRADED_CONFIG)
    settings = env.controller.settings
    assert settings is not None
    assert settings.provider == "ollama"
    assert settings.model == "llama3"
    assert settings.auth_method == "none"
    assert settings.base_url == "http://localhost:11434/v1"
    assert settings.model_tier == "low"


async def test_an_unconfigured_agent_seeds_no_settings(env: Env) -> None:
    """The seed is config's, not an invention: with nothing configured
    `:model` must still say "run :ai first" rather than rebuild a blank."""
    assert env.controller.settings is None


async def test_model_recovers_a_degraded_startup_without_the_wizard(tmp_path: Path) -> None:
    """`:model <name>` is the whole recovery: the seeded snapshot names the
    provider, so only the model has to change."""

    class _Configurator:
        def __init__(self) -> None:
            self.saved: list[AgentSettings] = []

        async def save(self, settings: AgentSettings) -> None:
            self.saved.append(settings)

    fresh = ScriptedSession(policy=fake_policy(model="llama3.2"))
    configurator = _Configurator()
    env = Env(
        tmp_path=tmp_path,
        session=None,
        config=_DEGRADED_CONFIG,
        configurator=configurator,
        rebuild=lambda settings: fresh,
    )
    env.controller.handle_model_command(["llama3.2"])
    await asyncio.gather(*env.ui.workers)
    assert env.controller.session is fresh
    assert env.controller.model_name == "llama3.2"
    assert configurator.saved
    assert configurator.saved[-1].provider == "ollama"
    assert configurator.saved[-1].model == "llama3.2"
    await env.close()


async def test_a_degraded_startup_reconnects_into_the_configured_state(tmp_path: Path) -> None:
    """After the recovery the panel is a working agent again: the input is
    enabled and the header renders, never the setup hint."""
    fresh = ScriptedSession(policy=fake_policy(model="llama3"))
    env = Env(
        tmp_path=tmp_path,
        session=None,
        config=_DEGRADED_CONFIG,
        rebuild=lambda settings: fresh,
    )
    env.panel.visible = True
    settings = env.controller.settings
    assert settings is not None
    assert env.controller.apply_settings(settings) is True
    assert env.controller.session is fresh
    assert "setup_hint" not in env.panel.calls
    assert env.panel.headers[-1] == "llama3"
    await env.close()


async def test_the_setup_wizard_opens_on_the_configured_snapshot(tmp_path: Path) -> None:
    """`:ai` after a degraded startup must prefill what is on disk instead
    of asking for every answer again."""

    class _Configurator:
        async def save(self, settings: AgentSettings) -> None:  # pragma: no cover - unused
            raise NotImplementedError

    env = Env(
        tmp_path=tmp_path,
        session=None,
        config=_DEGRADED_CONFIG,
        configurator=_Configurator(),
    )
    env.controller.handle_command([])
    screen, _callback = env.ui.screens[-1]
    # The screen stack is typed as plain `Screen`s; the prefill under test
    # is the setup screen's own state, so the type is narrowed first.
    assert isinstance(screen, AgentSetupScreen)
    current = screen._current_settings
    assert current is not None
    assert current.model == "llama3"


# ---------------------------------------------------------------------------
# Turn lifecycle: start, interrupt, replacement drain, finalization
# ---------------------------------------------------------------------------


async def test_a_prompt_starts_a_turn_and_streams_events_into_the_panel(tmp_path: Path) -> None:
    session = ScriptedSession([TextDelta(text="hello")])
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("hi")
    await env.controller.wait_for_turn()
    assert session.prompts == ["hi"]
    assert env.panel.turns == [("hi", True)]
    assert [type(event) for event in env.panel.events] == [TextDelta]


async def test_a_prompt_is_refused_while_a_context_switch_runs(tmp_path: Path) -> None:
    session = ScriptedSession()
    env = Env(tmp_path=tmp_path, session=session)
    env.context.is_switching = True
    env.controller.submit_prompt("hi")
    assert session.prompts == []
    assert any("context switch" in message for message in env.ui.messages())


async def test_a_prompt_is_refused_in_a_protected_context(tmp_path: Path) -> None:
    env = Env(
        tmp_path=tmp_path,
        session=ScriptedSession(),
        config=KorvidConfig(namespace="default", agent_disable_in_protected=True),
    )
    env.writes.set_protected_context("prod")
    env.controller.submit_prompt("hi")
    assert env.panel.turns == []
    assert any("protected context" in message for message in env.ui.messages())


async def test_a_second_prompt_interrupts_the_running_turn_and_replaces_it(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    await asyncio.sleep(0)
    env.controller.submit_prompt("second")
    assert env.panel.echoed == ["second"]
    await env.controller.wait_for_turn()
    await settle()
    assert session.prompts == ["first", "second"]
    assert session.finalized == 1
    gate.set()
    await env.close()


async def test_only_the_latest_correction_is_queued(tmp_path: Path) -> None:
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    await asyncio.sleep(0)
    env.controller.submit_prompt("second")
    env.controller.submit_prompt("third")
    await env.controller.wait_for_turn()
    await settle()
    assert session.prompts == ["first", "third"]
    gate.set()
    await env.close()


async def test_an_explicit_stop_discards_the_queued_replacement(tmp_path: Path) -> None:
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    await asyncio.sleep(0)
    env.controller.submit_prompt("second")
    env.controller.interrupt()
    await env.controller.wait_for_turn()
    await settle()
    assert session.prompts == ["first"]
    gate.set()
    await env.close()


async def test_an_interrupted_turn_closes_the_generator_before_finalizing(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    await asyncio.sleep(0)
    env.controller.interrupt()
    await env.controller.wait_for_turn()
    assert session.iterators_released == 1
    assert session.finalized == 1
    gate.set()
    await env.close()


async def test_a_turn_cancelled_before_it_ran_settles_without_finalizing(
    tmp_path: Path,
) -> None:
    """The done callback must settle a task whose coroutine never started —
    its own CancelledError handler never ran. The session has nothing to
    repair (it was never asked to run), so `finalize_interrupt` — which
    raises without a pending turn — must not be called; the panel still
    leaves its running state."""
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    env.controller.interrupt()  # before the first step of the coroutine
    await env.controller.wait_for_turn()
    await settle()
    assert session.prompts == []
    assert session.finalized == 0
    assert any(isinstance(event, TurnInterrupted) for event in env.panel.events)
    gate.set()
    await env.close()


async def test_a_turn_cancelled_before_it_ran_reports_no_usage_for_itself(
    tmp_path: Path,
) -> None:
    """The fallback interrupt is a *per-turn* delta, and a turn that never
    ran spent nothing.

    The panel adds an interrupt event's numbers to the totals it already
    shows, so minting the fallback from `session.total_tokens` would add
    the whole conversation's usage a second time on every pre-start
    cancel. `estimated` is still the session's own answer: the totals the
    header keeps showing may well be estimates.
    """
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate, estimated=True)
    session.total_tokens = (120, 45)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    env.controller.interrupt()  # before the first step of the coroutine
    await env.controller.wait_for_turn()
    await settle()
    interrupted = [event for event in env.panel.events if isinstance(event, TurnInterrupted)]
    assert interrupted == [TurnInterrupted(input_tokens=0, output_tokens=0, estimated=True)]
    gate.set()
    await env.close()


async def test_a_stop_signals_the_session_before_cancelling_the_task(
    tmp_path: Path,
) -> None:
    """`interrupt()` is cooperative: the session is told first, so it can
    wind the turn down itself rather than only learning about the stop
    through a `CancelledError` it cannot attribute."""
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    await asyncio.sleep(0)
    env.controller.interrupt()
    assert session.interrupts == 1
    await env.controller.wait_for_turn()
    gate.set()
    await env.close()


async def test_interrupt_and_submit_signals_the_session_before_cancelling(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    await asyncio.sleep(0)
    env.controller.submit_prompt("second")
    assert session.interrupts == 1
    await env.controller.wait_for_turn()
    await settle()
    gate.set()
    await env.close()


async def test_an_idle_stop_never_signals_the_session(tmp_path: Path) -> None:
    session = ScriptedSession()
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.interrupt()
    assert session.interrupts == 0
    await env.close()


async def test_a_session_error_is_reported_in_the_panel(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, session=ScriptedSession(turn_error=RuntimeError("boom")))
    env.controller.submit_prompt("hi")
    await env.controller.wait_for_turn()
    assert any(isinstance(event, AgentError) for event in env.panel.events)


async def test_a_provider_error_repaints_failed_turn_token_totals(tmp_path: Path) -> None:
    session = ScriptedSession([AgentError(message="provider failed")])
    session.total_tokens = (140, 22)
    env = Env(tmp_path=tmp_path, session=session)
    env.panel.header_totals.clear()

    env.controller.submit_prompt("hi")
    await env.controller.wait_for_turn()

    assert env.panel.header_totals[-1] == (140, 22)


# ---------------------------------------------------------------------------
# A consumer that raises: the turn is still the session's until it is closed
# ---------------------------------------------------------------------------


async def test_a_panel_failure_mid_stream_releases_the_turn_before_reporting_it(
    tmp_path: Path,
) -> None:
    """A raising consumer leaves the generator suspended at its yield.

    `async for` propagates a body failure without touching the iterator,
    so nothing has given the turn back at that point: the session still
    counts it as running and its history is still mid-turn. The
    controller must close the generator first (its `finally` releases the
    turn), then finalize the history the abandoned turn left open, and
    only then report the failure — the same order the cancellation path
    already uses.
    """
    panel = ExplodingPanel(on=TextDelta)
    session = StrictSession([TextDelta(text="partial")])
    env = Env(tmp_path=tmp_path, session=session, panel=panel)
    env.controller.submit_prompt("hi")
    await env.controller.wait_for_turn()
    await settle()
    assert panel.exploded == 1
    assert session.iterators_released == 1  # the generator was closed, not abandoned
    assert session.finalized == 1  # the mid-flight history was repaired
    assert session.finalization_pending is False
    assert any(isinstance(event, AgentError) for event in env.panel.events)
    await env.close()


async def test_a_panel_failure_leaves_the_session_ready_for_the_next_turn(
    tmp_path: Path,
) -> None:
    """The failure costs one turn, not the session.

    A session whose turn was never released refuses every later one for
    the rest of its life, so the user's next prompt would die with "a
    turn is already running" and the agent would be dead until `:ai`
    rebuilt it.
    """
    panel = ExplodingPanel(on=TextDelta)
    session = StrictSession([TextDelta(text="partial")])
    env = Env(tmp_path=tmp_path, session=session, panel=panel)
    env.controller.submit_prompt("first")
    await env.controller.wait_for_turn()
    await settle()

    panel.calls.clear()
    env.controller.submit_prompt("second")
    await env.controller.wait_for_turn()
    await settle()
    assert session.prompts == ["first", "second"]
    assert session.iterators_released == 2
    assert not any(
        "a turn is already running" in str(getattr(event, "message", ""))
        for event in env.panel.events
    )
    await env.close()


async def test_a_panel_failure_repaints_the_header_from_the_session_totals(
    tmp_path: Path,
) -> None:
    """A consumer failure still costs tokens, and the header must say so.

    The session commits whatever the interrupted turn already spent to
    its own totals, but the panel adds *deltas* from turn events — and
    this path reports an `AgentError`, which carries no usage. Without a
    repaint the header keeps showing the pre-turn number until some later
    turn happens to refresh it, so the user is billed for tokens the UI
    never admits to. The repaint is absolute (`set_header` takes totals,
    not a delta), so it cannot double-count what the panel already added.
    """
    panel = ExplodingPanel(on=TextDelta)
    session = StrictSession([TextDelta(text="partial")])
    session.total_tokens = (140, 22)
    env = Env(tmp_path=tmp_path, session=session, panel=panel)
    panel.header_totals.clear()
    env.controller.submit_prompt("hi")
    await env.controller.wait_for_turn()
    await settle()

    assert session.finalized == 1
    assert panel.header_totals[-1] == (143, 23)
    assert not any(isinstance(event, TurnInterrupted) for event in panel.events)
    assert any(isinstance(event, AgentError) for event in panel.events)
    await env.close()


async def test_a_failing_header_repaint_still_reports_the_original_failure(
    tmp_path: Path,
) -> None:
    """The repaint is cosmetic; the error report settles the panel.

    A panel that is already failing is exactly where `set_header` can fail
    too. If that second failure escaped, it would replace the one the user
    needs to see *and* skip the `AgentError` — the only event that takes
    the panel out of its running state — so the next prompt would be
    refused for the rest of the session.
    """

    class _HeaderExplodingPanel(ExplodingPanel):
        def set_header(
            self,
            model: str,
            input_tokens: int,
            output_tokens: int,
            *,
            estimated: bool,
            tier: str | None = None,
        ) -> None:
            raise RuntimeError("header widget is gone")

    panel = _HeaderExplodingPanel(on=TextDelta)
    session = StrictSession([TextDelta(text="partial")])
    env = Env(tmp_path=tmp_path, session=session, panel=panel)
    env.controller.submit_prompt("hi")
    await env.controller.wait_for_turn()
    await settle()

    assert session.finalized == 1
    errors = [event for event in panel.events if isinstance(event, AgentError)]
    assert [event.message for event in errors] == ["panel exploded"]
    await env.close()


async def test_a_panel_failure_after_the_turn_ended_needs_no_finalization(
    tmp_path: Path,
) -> None:
    """Nothing to repair, nothing repaired.

    When the consumer fails on a terminal event the generator has already
    finished and given the turn back, so `finalize_interrupt` — which
    raises without a pending turn — must not be called at all.
    """
    panel = ExplodingPanel(on=TurnComplete)
    session = StrictSession(
        [TurnComplete(input_tokens=1, output_tokens=2, estimated=False)],
    )
    env = Env(tmp_path=tmp_path, session=session, panel=panel)
    env.controller.submit_prompt("hi")
    await env.controller.wait_for_turn()
    await settle()
    assert panel.exploded == 1
    assert session.finalized == 0
    assert any(isinstance(event, AgentError) for event in env.panel.events)
    await env.close()


# ---------------------------------------------------------------------------
# Shutdown: cancellation and reaping
# ---------------------------------------------------------------------------


async def test_shutdown_closes_the_session_exactly_once(tmp_path: Path) -> None:
    """The controller owns the session's lifetime while the app runs: one
    close, after the in-flight turn has been cancelled and drained."""
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    await asyncio.sleep(0)
    await env.controller.shutdown()
    assert session.closed == 1
    assert session.iterators_released == 1
    await env.controller.shutdown()  # idempotent: teardown may repeat it
    assert session.closed == 1
    await env.dispatch.shutdown()


async def test_shutdown_without_a_session_is_inert(env: Env) -> None:
    await env.controller.shutdown()
    assert env.controller.session is None
    await env.dispatch.shutdown()


async def test_shutdown_cancels_and_reaps_the_running_turn(tmp_path: Path) -> None:
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    await asyncio.sleep(0)
    task = env.controller.turn_task
    assert task is not None
    await env.controller.shutdown()
    assert task.done()
    await env.dispatch.shutdown()


async def test_shutdown_starts_no_queued_replacement(tmp_path: Path) -> None:
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    await asyncio.sleep(0)
    env.controller.submit_prompt("second")
    await env.controller.shutdown()
    await settle()
    assert session.prompts == ["first"]
    await env.dispatch.shutdown()


async def test_shutdown_does_not_touch_the_torn_down_transcript(tmp_path: Path) -> None:
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    await asyncio.sleep(0)
    before = len(env.panel.events)
    await env.controller.shutdown()
    assert len(env.panel.events) == before
    await env.dispatch.shutdown()


async def test_begin_shutdown_stops_a_replacement_that_drains_before_shutdown(
    tmp_path: Path,
) -> None:
    """Teardown awaits other subsystems before it reaches `shutdown()`; a
    turn cancelled by an interrupt-and-submit can settle in that window.
    `begin_shutdown` marks the session down synchronously, so the drain
    starts no replacement (review of Deep Task 6)."""
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    await asyncio.sleep(0)
    task = env.controller.turn_task
    env.controller.submit_prompt("second")  # queues the replacement, cancels
    env.controller.begin_shutdown()
    await settle()  # the cancelled turn settles here, before shutdown() runs
    assert session.prompts == ["first"]
    assert env.controller.turn_task is task
    await env.controller.shutdown()
    await env.dispatch.shutdown()


async def test_begin_shutdown_is_idempotent(tmp_path: Path) -> None:
    """Teardown marks the session down, and `shutdown()` marks it again
    defensively — a repeat must be inert, never reset the flag."""
    gate = asyncio.Event()
    session = ScriptedSession(gate=gate)
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("first")
    await asyncio.sleep(0)
    env.controller.begin_shutdown()
    env.controller.begin_shutdown()
    assert env.controller._shutting_down is True
    await env.controller.shutdown()
    assert env.controller._shutting_down is True
    await env.dispatch.shutdown()


# ---------------------------------------------------------------------------
# Session hand-off: no prose, typed retarget only
# ---------------------------------------------------------------------------


async def test_the_controller_hands_the_session_only_the_user_text(tmp_path: Path) -> None:
    """The workspace snapshot reaches the session through the typed
    `AgentUiBridge`, never as prose appended to the prompt: the controller
    has no screen-context string to build and no note to inject."""
    session = ScriptedSession()
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.submit_prompt("what is wrong with web-1?")
    await env.controller.wait_for_turn()
    assert session.prompts == ["what is wrong with web-1?"]
    assert not hasattr(env.controller, "screen_context")
    assert not hasattr(env.controller, "note_context_switch")


# ---------------------------------------------------------------------------
# Follow mode
# ---------------------------------------------------------------------------


class RecordingBridge:
    """A UIBridge that records the mirrored calls instead of driving the UI."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        self.calls.append(("navigate", (view, namespace)))
        return "ok"

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        self.calls.append(("describe", (kind, name, namespace)))
        return "ok"


async def test_a_successful_read_is_mirrored_through_the_configured_bridge(
    tmp_path: Path,
) -> None:
    bridge = RecordingBridge()
    session = ScriptedSession(
        [
            ToolCallStarted(
                call_id="c1",
                name="get_resource",
                arguments='{"kind": "pods", "name": "web-1", "namespace": "default"}',
            ),
            ToolCallFinished(call_id="c1", name="get_resource", ok=True, summary=""),
        ]
    )
    env = Env(tmp_path=tmp_path, session=session, follow_bridge=bridge)
    env.controller.submit_prompt("what is wrong with web-1?")
    await env.controller.wait_for_turn()
    assert bridge.calls == [("describe", ("pods", "web-1", "default"))]


async def test_a_failed_read_is_not_mirrored(tmp_path: Path) -> None:
    bridge = RecordingBridge()
    session = ScriptedSession(
        [
            ToolCallStarted(call_id="c1", name="get_resource", arguments='{"kind": "pods"}'),
            ToolCallFinished(call_id="c1", name="get_resource", ok=False, summary=""),
        ]
    )
    env = Env(tmp_path=tmp_path, session=session, follow_bridge=bridge)
    env.controller.submit_prompt("what is wrong?")
    await env.controller.wait_for_turn()
    assert bridge.calls == []


async def test_follow_off_disables_mirroring(tmp_path: Path) -> None:
    bridge = RecordingBridge()
    session = ScriptedSession(
        [
            ToolCallStarted(
                call_id="c1",
                name="get_resource",
                arguments='{"kind": "pods", "name": "web-1", "namespace": "default"}',
            ),
            ToolCallFinished(call_id="c1", name="get_resource", ok=True, summary=""),
        ]
    )
    env = Env(tmp_path=tmp_path, session=session, follow_bridge=bridge)
    env.controller.handle_command(["follow", "off"])
    assert env.controller.follow_enabled is False
    env.controller.submit_prompt("what is wrong?")
    await env.controller.wait_for_turn()
    assert bridge.calls == []


async def test_malformed_tool_arguments_do_not_break_the_turn(tmp_path: Path) -> None:
    bridge = RecordingBridge()
    session = ScriptedSession(
        [
            ToolCallStarted(call_id="c1", name="get_resource", arguments="{broken"),
            ToolCallFinished(call_id="c1", name="get_resource", ok=True, summary=""),
        ]
    )
    env = Env(tmp_path=tmp_path, session=session, follow_bridge=bridge)
    env.controller.submit_prompt("show web-1")
    await env.controller.wait_for_turn()
    assert bridge.calls == []


# ---------------------------------------------------------------------------
# Bridge reads: navigate / filter / drill / logs / describe
# ---------------------------------------------------------------------------


async def test_navigate_switches_the_view_and_reports_the_row_count(env: Env) -> None:
    out = await env.controller.agent_navigate("deployments")
    assert not out.startswith("ERROR:")
    assert env.navigation.navigations == [("deployments", None)]


async def test_navigate_refuses_while_an_approval_dialog_is_open(env: Env) -> None:
    env.screens.approval_open = True
    out = await env.controller.agent_navigate("deployments")
    assert out.startswith("ERROR: an approval dialog is open")
    assert env.navigation.navigations == []


async def test_navigate_refuses_under_a_describe_screen(env: Env) -> None:
    env.screens.describe_open = True
    out = await env.controller.agent_navigate("deployments")
    assert out.startswith("ERROR: a describe screen is open")


async def test_navigate_rejects_an_unknown_view(env: Env) -> None:
    out = await env.controller.agent_navigate("wombats")
    assert out.startswith("ERROR: unknown view")


async def test_set_filter_and_clearing_it_report_the_change(env: Env) -> None:
    applied = await env.controller.agent_set_filter("web")
    assert "web" in applied
    cleared = await env.controller.agent_set_filter("")
    assert cleared == "filter cleared"
    assert env.navigation.filters == ["web", ""]


async def test_drill_down_refuses_under_a_describe_screen(env: Env) -> None:
    env.screens.describe_open = True
    out = await env.controller.agent_drill_down("api")
    assert out.startswith("ERROR: a describe screen is open")
    assert env.navigation.drills == []


async def test_drill_down_reports_an_unknown_row(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, rows=[])
    env.workspace.current_kind = "deployments"
    env.view.kind = "deployments"
    out = await env.controller.agent_drill_down("api")
    assert out.startswith("ERROR: no deployments named")


async def test_open_logs_streams_the_resolved_containers(env: Env) -> None:
    out = await env.controller.agent_open_logs("web-1", "default")
    assert not out.startswith("ERROR:")
    assert env.logs.opened == [("default", [("default", "web-1", "app")])]
    assert env.logs.cancelled == 1


async def test_open_logs_refuses_while_an_approval_dialog_is_open(env: Env) -> None:
    env.screens.approval_open = True
    out = await env.controller.agent_open_logs("web-1", "default")
    assert out.startswith("ERROR: an approval dialog is open")
    assert env.logs.cancelled == 0


async def test_open_logs_rejects_an_unknown_container_before_tearing_down_streams(
    env: Env,
) -> None:
    out = await env.controller.agent_open_logs("web-1", "default", container="nope")
    assert out.startswith("ERROR: container 'nope' not found")
    assert env.logs.cancelled == 0


async def test_open_logs_aborts_when_the_pane_changed_during_the_cancel(env: Env) -> None:
    def bump() -> None:
        env.logs.pane_gen += 1

    env.logs.on_cancel = bump
    out = await env.controller.agent_open_logs("web-1", "default")
    assert out.startswith("ERROR: the log pane changed")
    assert env.logs.opened == []


async def test_open_describe_shows_the_pane_when_the_panel_is_expanded(env: Env) -> None:
    env.panel.mounted = True
    env.panel.visible = True
    out = await env.controller.agent_open_describe("pods", "web-1", "default")
    assert not out.startswith("ERROR:")
    assert [title for title, _manifest, _footer in env.screens.panes] == ["pods/default/web-1"]
    assert env.ui.screens == []


async def test_open_describe_pushes_a_modal_when_the_panel_is_collapsed(env: Env) -> None:
    out = await env.controller.agent_open_describe("pods", "web-1", "default")
    assert not out.startswith("ERROR:")
    assert len(env.ui.screens) == 1
    assert env.screens.panes == []


async def test_open_describe_aborts_when_the_screen_changed_during_the_fetch(env: Env) -> None:
    async def moving_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        env.screens.top = object()
        return _POD_MANIFEST

    controller: Any = env.controller  # the seam is patched, not typed
    controller._get_manifest = lambda: moving_manifest
    out = await env.controller.agent_open_describe("pods", "web-1", "default")
    assert out.startswith("ERROR: the screen changed")


async def test_open_describe_reports_a_missing_object(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, manifest_error=ApiStatusError(404, "NotFound", "nope"))
    out = await env.controller.agent_open_describe("pods", "gone", "default")
    assert out.startswith("ERROR:")
    assert "gone" in out or "pods" in out


async def test_a_secret_describe_is_masked_before_it_reaches_the_screen(tmp_path: Path) -> None:
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "db", "namespace": "default"},
        "data": {"password": "c3VwZXJzZWNyZXQ="},
    }
    env = Env(tmp_path=tmp_path, manifests={("secrets", "default", "db"): secret})
    env.view._aliases["secrets"] = ResourceMeta("Secret", "secrets", "", "v1", True)
    env.panel.mounted = True
    env.panel.visible = True
    out = await env.controller.agent_open_describe("secrets", "db", "default")
    assert not out.startswith("ERROR:")
    _title, shown, _footer = env.screens.panes[0]
    assert shown["data"]["password"] != "c3VwZXJzZWNyZXQ="


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


async def test_open_evidence_without_a_session_is_an_error(env: Env) -> None:
    out = await env.controller.open_evidence("E1")
    assert out.startswith("ERROR: the agent is not configured")


async def test_open_evidence_rejects_a_reference_korvid_never_minted(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, session=ScriptedSession())
    out = await env.controller.open_evidence("E9")
    assert out == "ERROR: E9 is not evidence from this turn"


async def test_open_evidence_resolves_through_the_session_ledger(tmp_path: Path) -> None:
    """The UI reads evidence only through the session's ledger — the
    session owns it, the controller never keeps its own copy."""
    manifest = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "web-1"}}
    ledger = EvidenceLedger()
    ledger.start_turn()
    ref = ledger.record(
        "get_resource",
        {"kind": "pods", "name": "web-1", "namespace": "default"},
        "kind: Pod\nmetadata:\n  name: web-1\n",
    )
    assert ref is not None
    env = Env(
        tmp_path=tmp_path,
        session=ScriptedSession(evidence=ledger),
        manifests={("pods", "default", "web-1"): manifest},
    )
    env.panel.mounted = True
    env.panel.visible = True
    out = await env.controller.open_evidence(ref)
    assert not out.startswith("ERROR:")
    assert [title for title, _m, _f in env.screens.panes] == ["pods/default/web-1"]


# ---------------------------------------------------------------------------
# Header: the resolved tier, not the requested one
# ---------------------------------------------------------------------------


async def test_the_header_carries_the_resolved_tier_and_its_provenance(
    tmp_path: Path,
) -> None:
    session = ScriptedSession(
        policy=fake_policy(tier=ModelTier.HIGH, route_source=CapabilitySource.USER)
    )
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.toggle_panel()
    assert env.panel.tiers[-1] == "high (user)"


async def test_the_header_reports_a_fallback_route_honestly(tmp_path: Path) -> None:
    session = ScriptedSession(
        policy=fake_policy(tier=ModelTier.LOW, route_source=CapabilitySource.FALLBACK)
    )
    env = Env(tmp_path=tmp_path, session=session)
    env.controller.toggle_panel()
    assert env.panel.tiers[-1] == "low (fallback)"


async def test_the_header_has_no_tier_without_a_session(env: Env) -> None:
    """No session, no header at all — and therefore no invented tier. The
    unconfigured panel shows the setup hint instead."""
    env.controller.toggle_panel()
    assert env.panel.tiers == []
    assert "setup_hint" in env.panel.calls


async def test_a_rebuild_refreshes_the_header_tier(tmp_path: Path) -> None:
    """`:model`/setup swap the session; the header must show the tier the
    *new* session resolved, not the one the old one had."""
    fresh = ScriptedSession(
        policy=fake_policy(tier=ModelTier.HIGH, route_source=CapabilitySource.CATALOG)
    )
    env = Env(
        tmp_path=tmp_path,
        session=ScriptedSession(policy=fake_policy(tier=ModelTier.LOW)),
        rebuild=lambda settings: fresh,
    )
    env.panel.visible = True  # the header only renders while the panel is up
    settings = AgentSettings(
        provider="ollama", auth_method="none", base_url=None, model="m-2", model_tier="high"
    )
    assert env.controller.apply_settings(settings) is True
    assert env.panel.tiers[-1] == "high (catalog)"


# ---------------------------------------------------------------------------
# Direct agent writes: the perimeter is the only path
# ---------------------------------------------------------------------------


async def test_an_agent_write_is_approved_by_the_user_and_audited(tmp_path: Path) -> None:
    ops = RecordingOps()
    env = Env(tmp_path=tmp_path, ops=ops)
    env.panel.mounted = True
    env.panel.visible = True
    request = asyncio.ensure_future(
        env.controller.agent_request_write("delete", "pods", "web-1", "default")
    )
    await env.ui.wait_for_screens()
    assert isinstance(env.ui.screens[-1][0], ConfirmScreen)
    env.ui.answer(True)
    out = await request
    assert out.startswith("approved and executed")
    assert ops.calls == [("delete", "pods", "default", "web-1", "uid-1")]
    assert env.audit_path.read_text().count("delete") >= 1


async def test_a_declined_agent_write_never_mutates(tmp_path: Path) -> None:
    ops = RecordingOps()
    env = Env(tmp_path=tmp_path, ops=ops)
    env.panel.mounted = True
    env.panel.visible = True
    request = asyncio.ensure_future(
        env.controller.agent_request_write("delete", "pods", "web-1", "default")
    )
    await env.ui.wait_for_screens()
    env.ui.answer(False)
    out = await request
    assert out.startswith("denied")
    assert ops.calls == []


async def test_can_surface_approval_requires_no_inline_input(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, session=ScriptedSession())
    env.panel.mounted = True
    env.panel.visible = True
    env.ui.inline_active = True

    assert env.controller.can_surface_approval() is False

    env.ui.inline_active = False
    assert env.controller.can_surface_approval() is True


async def test_agent_write_waits_for_inline_input_focus_to_clear(tmp_path: Path) -> None:
    ops = RecordingOps()
    env = Env(tmp_path=tmp_path, ops=ops)
    env.panel.mounted = True
    env.panel.visible = True
    env.ui.inline_active = True

    request = asyncio.ensure_future(
        env.controller.agent_request_write("delete", "pods", "web-1", "default")
    )
    await settle()
    assert env.ui.screens == []
    assert env.ui.messages() == [
        "Agent write approval pending - leave the active input using Tab/Esc to review"
    ]

    env.ui.inline_active = False
    await env.ui.wait_for_screens()
    assert isinstance(env.ui.screens[-1][0], ConfirmScreen)
    env.ui.answer(False)
    out = await request
    assert out.startswith("denied")
    assert ops.calls == []


async def test_agent_write_keeps_collapsed_panel_pending_message(tmp_path: Path) -> None:
    ops = RecordingOps()
    env = Env(tmp_path=tmp_path, ops=ops)

    request = asyncio.ensure_future(
        env.controller.agent_request_write("delete", "pods", "web-1", "default")
    )
    await settle()
    assert env.ui.screens == []
    assert env.ui.messages() == [
        "Agent write approval pending - open the agent panel (Ctrl-A) to review"
    ]

    env.panel.visible = True
    await env.ui.wait_for_screens()
    assert isinstance(env.ui.screens[-1][0], ConfirmScreen)
    env.ui.answer(False)
    out = await request
    assert out.startswith("denied")
    assert ops.calls == []


async def test_an_agent_write_is_blocked_when_the_audit_sink_is_broken(tmp_path: Path) -> None:
    ops = RecordingOps()
    env = Env(tmp_path=tmp_path, ops=ops, audit="broken")
    env.panel.mounted = True
    env.panel.visible = True
    request = asyncio.ensure_future(
        env.controller.agent_request_write("delete", "pods", "web-1", "default")
    )
    await env.ui.wait_for_screens()
    env.ui.answer(True)
    out = await request
    assert out.startswith("ERROR:")
    assert ops.calls == []


async def test_an_agent_write_is_refused_in_read_only_mode(tmp_path: Path) -> None:
    ops = RecordingOps()
    env = Env(tmp_path=tmp_path, ops=ops)
    env.view._readonly = True
    out = await env.controller.agent_request_write("delete", "pods", "web-1", "default")
    assert out == "ERROR: read-only mode - cluster writes are disabled"
    assert env.ui.screens == []


async def test_an_agent_write_without_an_audit_log_is_refused(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, ops=RecordingOps(), audit="none")
    out = await env.controller.agent_request_write("delete", "pods", "web-1", "default")
    assert out == "ERROR: writes disabled - no audit log configured"


async def test_a_synthetic_view_kind_can_never_be_written(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, ops=RecordingOps())
    env.view._aliases["helmreleases"] = ResourceMeta(
        "HelmRelease", "helmreleases", "", "v1", True, synthetic=True
    )
    out = await env.controller.agent_request_write("delete", "helmreleases", "web", "default")
    assert "read-only korvid view" in out


async def test_an_unanswered_agent_write_expires_and_dismisses_its_dialog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = RecordingOps()
    env = Env(tmp_path=tmp_path, ops=ops)
    env.panel.mounted = True
    env.panel.visible = True
    monkeypatch.setattr("korvid.ui.agent_ui_controller.APPROVAL_TIMEOUT", 0.05)
    out = await env.controller.agent_request_write("delete", "pods", "web-1", "default")
    assert out.startswith("not approved")
    assert ops.calls == []


async def test_an_approval_never_surfaces_over_another_dialog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 6.1: an approval must not stack where a stray keystroke approves."""
    ops = RecordingOps()
    env = Env(tmp_path=tmp_path, ops=ops)
    env.panel.mounted = True
    env.panel.visible = True
    env.ui.depth = 2  # another screen is already on top
    monkeypatch.setattr("korvid.ui.agent_ui_controller.APPROVAL_TIMEOUT", 0.05)
    out = await env.controller.agent_request_write("delete", "pods", "web-1", "default")
    assert out.startswith("not approved")
    assert env.ui.screens == []


async def test_an_interrupted_agent_write_dismisses_its_dialog(tmp_path: Path) -> None:
    ops = RecordingOps()
    env = Env(tmp_path=tmp_path, ops=ops)
    env.panel.mounted = True
    env.panel.visible = True
    request = asyncio.ensure_future(
        env.controller.agent_request_write("delete", "pods", "web-1", "default")
    )
    await env.ui.wait_for_screens()
    screen = env.ui.screens[-1][0]
    env.screens.stacked.append(screen)
    request.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await request
    assert screen in env.screens.dismissed
    assert ops.calls == []


# ---------------------------------------------------------------------------
# Proposals stay behind the port
# ---------------------------------------------------------------------------


async def test_proposal_calls_are_delegated_to_the_proposal_port(env: Env) -> None:
    await env.controller.submit_write_proposal("delete", "pods", "web-1", "default")
    await env.controller.get_write_proposal("p-1")
    await env.controller.cancel_write_proposal("p-1", session_id="s")
    assert [name for name, _ in env.proposals.calls] == ["submit", "get", "cancel"]


def test_the_controller_module_imports_nothing_from_the_app_module() -> None:
    """The module must never import `korvid.ui.app` or name `KorvidApp` in code.

    An import/name check, and no more than that: some ports the app binds at
    session *are* app-backed adapters (`AppAgentPanel`, `AppAgentScreens`,
    `AppUiSurface`, …). What this pins is that the controller depends on
    the port interfaces, never on the app module or an app type it names
    itself — prose in docstrings is free to explain where it came from.
    """
    import ast

    module_path = Path(__file__).parents[2] / "src" / "korvid" / "ui" / "agent_ui_controller.py"
    source = module_path.read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    referenced: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.extend(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            referenced.append(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.append(node.attr)
    assert not any(name.endswith("KorvidApp") or name == "korvid.ui.app" for name in imported)
    assert "KorvidApp" not in referenced


# ---------------------------------------------------------------------------
# AppUIBridge dispatch
# ---------------------------------------------------------------------------


async def test_app_dispatch_serializes_concurrent_ui_operations() -> None:
    dispatch = AppContextDispatch()
    dispatch.activate()
    gate = asyncio.Event()
    first_started = asyncio.Event()
    active = 0
    max_active = 0

    async def operation(*, first: bool) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if first:
            first_started.set()
        await gate.wait()
        active -= 1
        return "ok"

    first = asyncio.create_task(dispatch.run(operation(first=True)))
    await first_started.wait()
    second = asyncio.create_task(dispatch.run(operation(first=False)))
    for _ in range(3):
        await asyncio.sleep(0)

    assert max_active == 1

    gate.set()
    results = await asyncio.gather(first, second)
    assert list(results) == ["ok", "ok"]
    await dispatch.shutdown()


async def test_cancelled_queued_dispatch_closes_its_unstarted_coroutine() -> None:
    dispatch = AppContextDispatch()
    dispatch.activate()
    gate = asyncio.Event()
    first_started = asyncio.Event()

    async def blocked() -> str:
        first_started.set()
        await gate.wait()
        return "ok"

    first = asyncio.create_task(dispatch.run(blocked()))
    await first_started.wait()
    queued_coro = blocked()
    queued = asyncio.create_task(dispatch.run(queued_coro))
    await asyncio.sleep(0)
    queued.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await queued

    assert inspect.getcoroutinestate(queued_coro) == inspect.CORO_CLOSED

    gate.set()
    assert await first == "ok"
    await dispatch.shutdown()


async def test_the_bridge_refuses_dispatch_before_the_app_is_mounted(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path)
    dispatch = AppContextDispatch()  # never activated: pre-mount
    bridge = AgentToolUIBridge(env.controller, dispatch)
    out = await bridge.agent_navigate("deployments")
    assert out.startswith("ERROR: UI not ready")
    assert env.navigation.navigations == []


async def test_the_bridge_refuses_dispatch_after_shutdown(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path)
    bridge = AgentToolUIBridge(env.controller, env.dispatch)
    await env.dispatch.shutdown()
    out = await bridge.agent_navigate("deployments")
    assert out.startswith("ERROR: UI not ready")


async def test_the_bridge_runs_the_operation_on_the_app_context(tmp_path: Path) -> None:
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="foreign")
    marker.set("app")
    env = Env(tmp_path=tmp_path)
    dispatch = AppContextDispatch()
    dispatch.activate()  # captures this (app) context
    seen: list[str] = []

    async def probe() -> str:
        seen.append(marker.get())
        return "ok"

    async def foreign() -> str:
        marker.set("foreign")
        return await dispatch.run(probe())

    result = await asyncio.create_task(foreign(), context=contextvars.Context())
    assert result == "ok"
    assert seen == ["app"]
    await dispatch.shutdown()
    await env.close()


async def test_shutdown_cancels_an_in_flight_dispatch(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path)
    started = asyncio.Event()

    async def slow() -> str:
        started.set()
        await asyncio.sleep(10)
        return "never"

    call = asyncio.ensure_future(env.dispatch.run(slow()))
    await started.wait()
    await env.dispatch.shutdown()
    with contextlib.suppress(asyncio.CancelledError):
        await call
    assert call.done()


async def test_the_bridge_delegates_every_ui_tool_to_the_controller(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path)
    bridge = AgentToolUIBridge(env.controller, env.dispatch)
    assert await bridge.agent_set_filter("web") == "filter set to 'web' on the pods view"
    assert (await bridge.agent_get_write_proposal("p-1")).startswith("proposal p-1")
    await env.close()
