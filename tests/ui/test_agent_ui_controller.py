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
from korvid.agent.interaction import ResourceIdentity
from korvid.agent.session import AgentSession
from korvid.agent.setup import AgentSettings
from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.k8s.discovery import ResourceMeta
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


# ---------------------------------------------------------------------------
# Turn lifecycle: start, interrupt, replacement drain, finalization
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Header: the resolved tier, not the requested one
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Direct agent writes: the perimeter is the only path
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Proposals stay behind the port
# ---------------------------------------------------------------------------


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
