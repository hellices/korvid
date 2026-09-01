"""The built-in agent's UI and session ownership (issue #187 / Deep Task 6).

`AgentUiController` owns everything about the embedded agent that used to
live directly on `KorvidApp`:

- the session state — session, model, settings, the configured model tier
  the wizard and `:model` rebuild from, the configurator/rebuild/disconnect
  seams, the `:ai off` disconnect marker and the follow flag;
- the turn lifecycle — the bare app-loop task, its cancellation, the
  interrupt-and-submit replacement queue, the finalization of an interrupted
  turn, and the shutdown drain;
- what the model is told about the screen, and the follow mirroring of its
  cluster reads;
- every `UIBridge` read the agent (or an MCP follow mirror) drives: evidence
  open, navigate, filter, drill, logs and describe;
- the direct, approval-gated agent write request, together with the target
  manifest/uid/ownership lookups and the write-op construction it shares
  with the proposal path.

What it deliberately does **not** own is the write security perimeter
(`WriteCoordinator` owns approval ordering, revalidation, the reservation,
the fail-closed audit and the mutation) or external write proposals, which
stay behind the `AgentProposals` port.

It reaches Textual only through `UiSurface` and the ports below, reads the
view through `ViewState`, revalidates `:ctx` through `ContextGuard`, and
never imports or holds `KorvidApp` — so the whole agent surface is exercised
without a running app. The app keeps the Textual action/message handlers as
thin delegates.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Literal, Protocol

from textual.screen import Screen

from korvid.agent.events import (
    AgentError,
    AgentEvent,
    ToolCallFinished,
    ToolCallStarted,
    TurnInterrupted,
)
from korvid.agent.install_hint import isolated_install_hint
from korvid.agent.interaction import PaneContext, ResourceIdentity
from korvid.agent.navigation import EvidenceTarget, target_for
from korvid.agent.setup import AgentConfigurator, AgentSettings
from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.errors import explain_api_error
from korvid.core.impact import ImpactAction
from korvid.core.portforward import OWNER_CHAIN_PLURALS, controller_owner
from korvid.core.resize_impact import classify_pod_resize
from korvid.core.secrets import mask_secret_manifest
from korvid.core.store import ALL_NAMESPACES, Summary
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.managed import manager_of
from korvid.k8s.models import PodSummary, manifest_uid
from korvid.k8s.relations import drill_child, owned_by
from korvid.k8s.writes import WriteOps, restart_stamp
from korvid.tools.executor import UIBridge, incarnation_of
from korvid.tools.follow import FOLLOWABLE_TOOLS, mirror_read
from korvid.ui.bridge_dispatch import BridgeDispatch
from korvid.ui.resize_impact_preview import compose_resize_impact_lines
from korvid.ui.resource_write_controller import RESTARTABLE, SCALABLE, resize_summary
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen
from korvid.ui.widgets.describe_screen import DescribeScreen, provider_footer_note
from korvid.ui.widgets.log_pane import MAX_PANELS
from korvid.ui.widgets.payload_inspector import PayloadInspectorScreen
from korvid.ui.workspace_controller import ContextGuard
from korvid.ui.workspace_state import WorkspaceState, filtered_rows
from korvid.ui.write_coordinator import WriteCoordinator, gvr_label, write_locus

if TYPE_CHECKING:
    # Annotation-only: the base TUI must not import the embedded-agent
    # session at startup (issue #73) — the composition root injects it only
    # when the [agent] extra is installed and wired.
    from korvid.agent.session import AgentSession
    from korvid.ui.agent_workspace_bridge import AgentWorkspaceBridge

logger = logging.getLogger(__name__)

#: Seconds an agent-requested approval dialog stays open before it counts as
#: a denial - an unanswered dialog must never hang the agent turn forever.
APPROVAL_TIMEOUT = 120.0

#: Upper bound on the pre-approval uid lookup: a stalled API server must
#: never leave an agent tool call (or the debug offer) pending indefinitely.
#: Best-effort inspection callers fail open; direct agent writes opt into the
#: strict path and are blocked when identity cannot be established.
UID_LOOKUP_TIMEOUT = 10.0


class TargetIdentityUnavailable(RuntimeError):
    """A direct agent write could not establish its target incarnation."""


#: `KorvidApp._get_manifest`: (kind alias, namespace, name) -> manifest.
ManifestFetcher = Callable[[str, str | None, str], Awaitable[dict[str, Any]]]

#: One validated write: (meta, namespace, op factory, operation line, audit detail).
WriteOpBuild = tuple[ResourceMeta, str | None, Callable[[str | None], Awaitable[None]], str, str]

#: A (namespace, pod, container) log target.
Triple = tuple[str, str, str]


async def _aclose(iterator: object) -> None:
    """Close an async generator the controller stopped consuming.

    Whoever abandons an `async for` owns the generator it left suspended.
    An agent turn is driven by a generator that releases the session in
    its `finally`, so dropping it without closing it would hold the turn
    open forever. Closing is best-effort: it runs the producer's cleanup,
    and any failure in that cleanup must not displace the reason we
    stopped consuming in the first place.
    """
    closer = getattr(iterator, "aclose", None)
    if closer is None:
        return
    with contextlib.suppress(BaseException):
        await closer()


class AgentPanelPort(ABC):
    """The chat panel, as the controller is allowed to drive it.

    Only what the agent session needs: visibility, the header the session's
    identity renders into, the two unconfigured-state hints, and the
    transcript operations a turn performs. Handing over the widget itself
    would hand over its whole Textual surface (and its `app`).
    """

    @abstractmethod
    def expanded(self) -> bool:
        """Whether the panel is mounted *and* visible on screen."""

    @abstractmethod
    def show(self) -> None:
        """Make the panel visible."""

    @abstractmethod
    def hide(self) -> None:
        """Hide the panel and return keyboard focus to the table."""

    @abstractmethod
    def focus_input(self) -> None:
        """Put the caret in the chat input."""

    @abstractmethod
    def enable_input(self) -> None:
        """Re-enable the chat input a setup hint may have disabled."""

    @abstractmethod
    def set_header(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        estimated: bool,
        tier: str | None = None,
    ) -> None:
        """Render the live session's model, token usage and routed tier."""

    @abstractmethod
    def show_setup_hint(self) -> None:
        """Explain that no agent is configured (`:ai` runs the wizard)."""

    @abstractmethod
    def show_reconnect_hint(self) -> None:
        """Explain that a configured agent was disconnected (`:ai off`)."""

    @abstractmethod
    def set_stop_key(self, key: str) -> None:
        """Advertise the effective interrupt key in the running-turn status."""

    @abstractmethod
    def interrupt_key(self) -> str:
        """The effective `interrupt_agent` key, resolved from the bindings."""

    @abstractmethod
    def begin_turn(self, text: str, *, echo: bool) -> None:
        """Enter the running-turn state, echoing the prompt unless replayed."""

    @abstractmethod
    def echo_user(self, text: str) -> None:
        """Show a correction typed while a turn is still running."""

    @abstractmethod
    def apply_event(self, event: AgentEvent) -> None:
        """Render one session event into the transcript."""


class AgentScreens(ABC):
    """The screen the agent may observe, must not disturb, and may fill.

    Screen *facts* rather than the stack itself: a live `Screen` carries
    `dismiss` and `app`, which is app access routed around the ports. The
    two guards here are security-relevant — an approval dialog is confirmed
    only by user keystrokes, and a describe screen the user is reading takes
    priority over anything the agent wants to show.
    """

    @abstractmethod
    def approval_dialog_active(self) -> bool:
        """Whether a write-approval dialog or write-parameter wizard is on top."""

    @abstractmethod
    def describe_screen_open(self) -> bool:
        """Whether a describe modal the user is reading is on top."""

    @abstractmethod
    def top_screen(self) -> object | None:
        """Identity token of the topmost screen, for before/after comparison."""

    @abstractmethod
    def is_stacked(self, screen: Screen[Any]) -> bool:
        """Whether *screen* is still somewhere in the stack."""

    @abstractmethod
    def dismiss_if_current(self, screen: Screen[Any]) -> None:
        """Pop *screen* when it is on top; never disturb a later screen."""

    @abstractmethod
    def selected_row_key(self) -> str | None:
        """Row key under the cursor in the focused pane, or None."""

    @abstractmethod
    def show_describe_pane(
        self,
        title: str,
        manifest: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        footer_note: str | None,
    ) -> None:
        """Show a describe view in the non-modal pane beside the chat panel."""

    @abstractmethod
    def selected_identity(self, table_id: str, kind: str) -> ResourceIdentity | None:
        """The resource under the cursor in the named pane, or None.

        Reads the pane's current selection by its widget table-id without
        changing focus.  The *kind* hint names the resource kind rendered in
        that pane so the identity can carry it even when the row key does not.
        """

    @abstractmethod
    def displayed_pane_context(self) -> DisplayedPaneContext | None:
        """The describe/log target currently shown above the resource table."""


@dataclasses.dataclass(frozen=True, slots=True)
class DisplayedPaneContext:
    """A rendered describe/log pane and the workspace pane that opened it."""

    context: PaneContext
    owner: object | None


class WorkspaceOps(Protocol):
    """The workspace transitions the agent's read tools drive.

    Structural, and satisfied by `WorkspaceController`: navigation, the
    filter, and the drill-down are the only view changes an agent tool may
    cause, and each is the very transition the human keybinding performs.
    """

    async def navigate_command(self, view: str | None, namespace: str | None) -> None:
        """Switch the focused pane's view and/or scope."""

    def set_filter(self, pattern: str) -> None:
        """Apply a filter to the focused pane."""

    def clear_filter(self) -> None:
        """Drop the focused pane's filter."""

    async def drill_into(self, namespace: str, name: str) -> str | None:
        """Drill into a row; None on success, else the reason."""


class AgentLogOps(Protocol):
    """The log-pane lifecycle an agent log open drives (structural).

    Satisfied by `LogController`. `pane_gen` is the generation counter the
    flow re-reads after every await: a user pane change during container
    resolution takes priority over the agent's request.
    """

    @property
    def pane_gen(self) -> int:
        """Monotonic log-pane generation; bumped by every pane change."""

    async def cancel_tasks(self) -> None:
        """Stop the streams currently feeding the pane."""

    async def open_agent_logs(self, namespace: str, triples: list[Triple]) -> None:
        """Open the pane on exactly these (namespace, pod, container) triples."""


class AgentProposals(ABC):
    """External MCP write proposals, reached only through this port.

    Proposal persistence, review and execution are not the agent session's
    business: they are a user-reviewed inbox with their own store, TTL and
    audit path. The controller forwards the three tool calls and knows
    nothing else about them.
    """

    @abstractmethod
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
        """Queue an immutable proposal for user review; returns its id."""

    @abstractmethod
    async def get_write_proposal(self, proposal_id: str) -> str:
        """Terminal-outcome lookup for a proposal."""

    @abstractmethod
    async def cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        """Caller-initiated cancel (distinct from a user deny)."""


class TurnTasks(ABC):
    """Where an agent turn's task is created.

    A turn is a bare `asyncio` task rather than a Textual worker on purpose:
    the interrupt key must cancel *this* turn, the replacement must start
    from the cancelled task's done callback, and shutdown must reap it. The
    controller owns that lifecycle; this port only decides which loop the
    task is created on, so tests can observe it.
    """

    @abstractmethod
    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Create the turn's task on the app's event loop."""


class AppLoopTurnTasks(TurnTasks):
    """The production `TurnTasks`: a task on the running app loop."""

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        return asyncio.create_task(coro)


class AgentToolUIBridge(UIBridge):
    """`UIBridge` adapter over an `AgentUiController` and a dispatch surface.

    The layer-boundary interface must be an `abc.ABC` (AGENTS.md); this is
    the ui-side implementation the composition root hands to the tool
    executor. Every call is marshaled onto the app-owned execution context
    (issue #165) before it reaches the controller, which also fixes the
    downstream-task hazard: log-stream tasks spawned inside a dispatched
    call inherit the app context instead of carrying the MCP request context
    for the stream's lifetime.
    """

    def __init__(self, controller: AgentUiController, dispatch: BridgeDispatch) -> None:
        self._agent = controller
        self._dispatch = dispatch

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        return await self._dispatch.run(self._agent.agent_navigate(view, namespace))

    async def agent_set_filter(self, pattern: str) -> str:
        return await self._dispatch.run(self._agent.agent_set_filter(pattern))

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        return await self._dispatch.run(self._agent.agent_open_logs(pod, namespace, container))

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        return await self._dispatch.run(self._agent.agent_open_describe(kind, name, namespace))

    async def agent_drill_down(self, name: str) -> str:
        return await self._dispatch.run(self._agent.agent_drill_down(name))

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        return await self._dispatch.run(
            self._agent.agent_request_write(action, kind, name, namespace, replicas, resources)
        )

    async def agent_submit_write_proposal(
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
        return await self._dispatch.run(
            self._agent.submit_write_proposal(
                action,
                kind,
                name,
                namespace,
                replicas,
                resources,
                session_id=session_id,
                client_name=client_name,
                client_version=client_version,
            )
        )

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        return await self._dispatch.run(self._agent.get_write_proposal(proposal_id))

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        return await self._dispatch.run(
            self._agent.cancel_write_proposal(proposal_id, session_id=session_id)
        )


class AgentUiController:
    """Owns the agent's session, its turn tasks, and its UI bridge reads."""

    def __init__(
        self,
        *,
        panel: AgentPanelPort,
        screens: AgentScreens,
        ui: UiSurface,
        view: ViewState,
        context: ContextGuard,
        writes: WriteCoordinator,
        workspace: WorkspaceState,
        navigation: WorkspaceOps,
        logs: AgentLogOps,
        proposals: AgentProposals,
        dispatch: BridgeDispatch,
        config: Callable[[], KorvidConfig],
        #: Late-binding: a `:ctx` switch retargets the manifest/event
        #: sources, the write client and the cluster's capabilities.
        get_manifest: Callable[[], ManifestFetcher | None],
        get_events: Callable[[], Any | None],
        stream_logs: Callable[[], Any | None],
        pod_containers: Callable[[str, str], tuple[str, ...]],
        write_ops: Callable[[], WriteOps | None],
        audit: Callable[[], AuditLog | None],
        pod_resize_supported: Callable[[], bool],
        provider_hint: Callable[[], str | None],
        approval_timeout_seconds: float | None = None,
        #: Repaint the status bar's AI on/off/blocked label after a session
        #: state change; the bar itself belongs to the app's widget tree.
        refresh_status: Callable[[], None] = lambda: None,
        #: The shared serialized bridge (the composition root's proxy):
        #: follow mirrors route through it so they serialize with the
        #: agent's own UI tools and concurrent MCP UI calls. None (tests,
        #: degraded wiring) falls back to this controller's own adapter.
        follow_bridge: Callable[[], UIBridge | None] = lambda: None,
        tasks: TurnTasks | None = None,
        session: AgentSession | None = None,
        model_name: str | None = None,
        configurator: AgentConfigurator | None = None,
        rebuild: Callable[[AgentSettings], AgentSession | None] | None = None,
        disconnect: Callable[[], None] | None = None,
        available: bool = True,
    ) -> None:
        self._panel = panel
        self._screens = screens
        self._ui = ui
        self._view = view
        self._context = context
        self._writes = writes
        self._workspace = workspace
        self._navigation = navigation
        self._logs = logs
        self._proposals = proposals
        self._dispatch = dispatch
        self._config = config
        self._get_manifest = get_manifest
        self._get_events = get_events
        self._stream_logs = stream_logs
        self._pod_containers = pod_containers
        self._write_ops = write_ops
        self._audit = audit
        self._pod_resize_supported = pod_resize_supported
        self._provider_hint = provider_hint
        self._approval_timeout = (
            APPROVAL_TIMEOUT if approval_timeout_seconds is None else approval_timeout_seconds
        )
        self._refresh_status = refresh_status
        self._follow_bridge = follow_bridge
        self._tasks = tasks if tasks is not None else AppLoopTurnTasks()
        self._session = session
        #: One close from this controller, however many times teardown
        #: asks: the app's unmount path and a defensive `shutdown` both
        #: run on the way down.
        self._session_closed = False
        self._model_name = session.policy.model.model if session is not None else model_name
        self._configurator = configurator
        self._rebuild = rebuild
        #: Releases the live provider on `:ai off` (issue #167) — session
        #: state only; persisted configuration is untouched.
        self._disconnect = disconnect
        #: False when the [agent] extra is absent (issue #73): the agent
        #: panel is not mounted and :ai/:model/Ctrl-A are not offered.
        self._available = available
        settings = config()
        self._settings: AgentSettings | None = None
        #: model tier as explicitly configured (None = Automatic) — seeds
        #: the `:ai` wizard's tier step and `:model` rebuilds so an
        #: explicit low/high override survives across them.
        self._configured_tier = settings.agent_model_tier
        # config.yaml naming a provider and a model is enough to seed the
        # settings snapshot, whether or not the composition root managed to
        # build a session from it. A startup that degraded (a provider the
        # router refuses, say `supports_tools=False`) still has to be
        # recoverable with a single `:model <name>` — and reconnect and the
        # `:ai` wizard have to open on what is configured — instead of
        # asking the operator to retype a configuration korvid already has.
        if settings.agent_provider and settings.agent_model:
            self._settings = AgentSettings(
                provider=settings.agent_provider,
                auth_method=settings.agent_auth_method or "none",
                base_url=settings.agent_base_url,
                model=settings.agent_model,
                api_key_env=settings.agent_api_key_env,
                model_tier=settings.agent_model_tier,
                options=settings.agent_options,
            )
        #: Agent follow: mirror the built-in agent's cluster reads on screen
        #: — small models rarely volunteer the UI tools, so without this the
        #: screen sits idle while the agent reads "behind its back". Config
        #: seeds the state (default on); `:ai follow on|off` toggles it.
        self._follow: bool = settings.agent_follow
        self._task: asyncio.Task[None] | None = None
        #: True after :ai off (issue #167): the agent was configured and
        #: explicitly disconnected — reconnect hint, not the setup wipe.
        self._disconnected = False
        # Interrupt-and-submit (issue #170): the latest correction typed
        # while a turn runs; started once the cancelled turn is finalized.
        self._replacement: str | None = None
        self._turn_finalized = False
        self._shutting_down = False
        #: Identity of the object the last evidence open actually displayed.
        self._displayed_incarnation: str | None = None
        self._bridge = AgentToolUIBridge(self, dispatch)
        #: Lazily created typed workspace bridge (see `workspace_bridge`).
        self._workspace_bridge: AgentWorkspaceBridge | None = None

    # ------------------------------------------------------------------
    # Session state, observable but not mutable from outside
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether the [agent] extra was wired at all (issue #73)."""
        return self._available

    @property
    def session(self) -> AgentSession | None:
        """The live session — the `:ai` wizard may have replaced the initial
        one, so per-cluster retargeting (issue #36) must read it here."""
        return self._session

    @property
    def model_name(self) -> str | None:
        """Model of the live session, as the panel header shows it."""
        return self._model_name

    @property
    def configured_model_tier(self) -> str | None:
        """Explicitly configured model tier (None = Automatic), as last set
        by config.yaml or the `:ai` wizard."""
        return self._configured_tier

    @property
    def settings(self) -> AgentSettings | None:
        """The settings snapshot `:model` edits, or None when unconfigured."""
        return self._settings

    @property
    def follow_enabled(self) -> bool:
        """Whether the agent's cluster reads are mirrored on screen."""
        return self._follow

    @property
    def busy(self) -> bool:
        """Whether a turn is running right now."""
        return self._task is not None and not self._task.done()

    @property
    def turn_task(self) -> asyncio.Task[None] | None:
        """The in-flight turn's task, for shutdown assertions."""
        return self._task

    @property
    def bridge(self) -> UIBridge:
        """This controller as a serialized `UIBridge` (the follow fallback)."""
        return self._bridge

    @property
    def workspace_bridge(self) -> AgentWorkspaceBridge:
        """The typed workspace-action bridge (`AgentUiBridge`) for this session.

        Created once and cached. `timeline_cursor` is deliberately left at
        its default (always `None`) here, and that is the production
        behaviour: a cursor names *the timeline entry the user is looking
        at*, and korvid has no user-visible timeline selection to read it
        from yet. Synthesising one — "the newest entry", say — would hand
        the agent a cursor no user ever placed, so `timeline_cursor=None`
        is the honest answer until such a selection exists. The parameter
        stays on `AgentWorkspaceBridge` for the tests that exercise cursor
        handling and for the composition root to wire on the day the
        selection lands.
        """
        bridge = self._workspace_bridge
        if bridge is None:
            # Lazy: agent_workspace_bridge imports this module for the
            # controller it drives, so a module-level import here would
            # close the cycle.
            from korvid.ui.agent_workspace_bridge import AgentWorkspaceBridge

            bridge = AgentWorkspaceBridge(
                config=self._config,
                context=self._context,
                workspace=self._workspace,
                screens=self._screens,
                controller=self,
                dispatch=self._dispatch,
            )
            self._workspace_bridge = bridge
        return bridge

    def blocked_in_protected(self) -> bool:
        """`agent.disable_in_protected` (issue #83): agent turns are refused
        entirely while a protected context is active."""
        return self._writes.protected_context is not None and (
            self._config().agent_disable_in_protected
        )

    # ------------------------------------------------------------------
    # `:ai` / `:model` commands
    # ------------------------------------------------------------------

    def handle_command(self, args: list[str]) -> None:
        """`:ai` / `:agent` — wizard, payload inspector, follow, disconnect."""
        if not args:
            self._open_setup()
            return
        subcommand = args[0].lower() if args else ""
        if subcommand == "payload" and len(args) == 1:
            self._open_payload_inspector()
            return
        if subcommand == "follow":
            self._handle_follow_command(args[1:])
            return
        if subcommand == "off" and len(args) == 1:
            self._handle_off()
            return
        self._ui.notify(
            "Usage: :ai [off|payload] | :ai follow [on|off]",
            severity="warning",
        )

    def _open_payload_inspector(self) -> None:
        """Open the latest stable redacted provider payload, if available."""
        session = self._session
        if session is None:
            self._ui.notify("Agent is off", severity="warning")
            return
        if self.busy:
            self._ui.notify(
                "Agent is busy — wait for the turn to finish before inspecting its payload",
                severity="warning",
            )
            return
        snapshot = session.latest_outbound_payload
        if snapshot is None:
            self._ui.notify("No provider payload has been sent", severity="warning")
            return
        self._ui.push_screen(PayloadInspectorScreen(snapshot))

    def _handle_off(self) -> None:
        """`:ai off` (issue #167): disconnect the agent for this session.

        Keeps the configured provider/model/tier/credentials so bare
        `:ai` reconnects without re-entry; never rewrites `agent.enabled`
        or the persisted config. Refused while a turn runs — cancelling
        midway is the interrupt key's job, not a state command's.
        """
        if self._session is None:
            self._ui.notify("Agent is already off")
            return
        if self.busy:
            self._ui.notify(
                "Agent is busy — wait for the turn to finish (or stop it) before :ai off",
                severity="warning",
            )
            return
        if self._disconnect is not None:
            self._disconnect()
        self._session = None
        # Disconnected-but-configured (vs never-configured): visibility
        # toggles must show the reconnect hint, never the setup wipe.
        self._disconnected = True
        self._refresh_status()
        self._panel.show_reconnect_hint()
        self._ui.notify("Agent disconnected — run :ai to reconnect")

    def _open_setup(self) -> None:
        if self._configurator is None:
            self._ui.notify(
                f"Agent setup unavailable — {isolated_install_hint(feature='agent')}",
                severity="warning",
                markup=False,
            )
            return
        # The wizard applies the settings itself (via apply_settings) before
        # persisting, so a refused swap keeps the wizard open and unsaved.
        self._ui.push_screen(
            AgentSetupScreen(
                self._configurator,
                apply_settings=self.apply_settings,
                current_tier=self._configured_tier,
                current_settings=self._settings,
            )
        )

    def handle_model_command(self, args: list[str]) -> None:
        """`:model` shows the current model; `:model <name>` switches and persists it."""
        if len(args) > 1:
            self._ui.notify("Usage: :model [name]", severity="warning")
            return
        if not args:
            # Report only a live model: at startup config may carry a model
            # name even though provider creation failed (session is None).
            if self._session is not None and self._model_name:
                self._ui.notify(f"Agent model: {self._model_name}")
            else:
                self._ui.notify("Agent not configured — run :ai first", severity="warning")
            return
        settings = self._settings
        configurator = self._configurator
        if settings is None or configurator is None:
            self._ui.notify("Agent not configured — run :ai first", severity="warning")
            return
        new_settings = dataclasses.replace(settings, model=args[0])

        async def _switch() -> None:
            # Apply first: persistence must be conditional on a successful
            # swap, or a refused change would silently take effect on restart.
            if not self.apply_settings(new_settings):
                return  # apply_settings already notified the reason
            try:
                await configurator.save(new_settings)
            except Exception as exc:  # session is live but disk is stale
                # Do not name a revert target: after a previous failed save
                # the in-memory snapshot may itself never have been persisted.
                self._ui.notify(
                    f"Model applied, but save failed: {exc} — will revert to "
                    "the last saved model on restart",
                    severity="warning",
                )
                return
            self._ui.notify(f"Agent model set to {new_settings.model}")

        self._ui.run_worker(_switch(), exclusive=False)

    def _handle_follow_command(self, args: list[str]) -> None:
        """`:ai follow [on|off]`: toggle mirroring of the built-in agent's
        cluster reads on screen. Bare `:ai follow` flips the state."""
        if len(args) > 1 or (args and args[0].lower() not in ("on", "off")):
            self._ui.notify("Usage: :ai follow [on|off]", severity="warning")
            return
        self._follow = args[0].lower() == "on" if args else not self._follow
        state = "on" if self._follow else "off"
        self._ui.notify(
            f"Agent follow {state} — the agent's reads are "
            f"{'mirrored on screen' if self._follow else 'no longer mirrored'}"
        )

    def apply_settings(self, settings: AgentSettings) -> bool:
        """Swap in a fresh session built from the wizard's settings.

        Transactional: on any failure the previous session/settings are kept
        and False is returned; the swap is also refused while a turn is live.
        """
        if self._rebuild is None:
            self._ui.notify(
                f"Agent rebuild unavailable — {isolated_install_hint(feature='agent')}",
                severity="warning",
                markup=False,
            )
            return False
        if self.busy:
            self._ui.notify(
                "Agent is busy — wait for the current turn to finish", severity="warning"
            )
            return False
        try:
            session = self._rebuild(settings)
        except Exception as exc:
            self._ui.notify(f"Agent rebuild failed: {exc}", severity="error", markup=False)
            return False
        if session is None:
            self._ui.notify(
                "Agent rebuild failed — check configuration; keeping previous agent",
                severity="error",
            )
            return False
        self._session = session
        self._session_closed = False  # a fresh session, not the closed one
        self._disconnected = False  # reconnected (issue #167)
        self._model_name = session.policy.model.model
        self._settings = settings
        # Once applied (and persisted by the wizard) the tier is an explicit
        # choice — reopening :ai must preserve it.
        self._configured_tier = settings.model_tier
        self._refresh_status()
        # Always re-enable: the hint may have disabled the input while the
        # panel was open earlier; only focus/header rendering depends on
        # visibility.
        self._panel.enable_input()
        if self._panel.expanded():
            self._render_header(session, self._model_name)
            self._panel.focus_input()
        return True

    @staticmethod
    def _tier_label(session: AgentSession) -> str:
        """How the resolved tier is shown: the tier the session actually
        runs on, and where that decision came from.

        Deliberately the *resolved* policy rather than the requested
        override: a request the catalogue could not honour must read as
        the fallback it became, not as the choice the user typed.
        """
        policy = session.policy
        return f"{policy.tier.value} ({policy.route_source.value})"

    def _render_header(self, session: AgentSession, model: str | None) -> None:
        """Paint the panel header from the live session's own numbers."""
        in_tok, out_tok = session.total_tokens
        self._panel.set_header(
            model or "",
            in_tok,
            out_tok,
            estimated=session.usage_estimated,
            tier=self._tier_label(session),
        )

    # ------------------------------------------------------------------
    # Panel toggle and prompt submission
    # ------------------------------------------------------------------

    def toggle_panel(self) -> None:
        """Toggle the agent chat panel; show setup hint when unconfigured."""
        if not self._available:
            return
        if self._panel.expanded():
            self._panel.hide()
            return
        self._panel.show()
        if self._session is None:
            if self._disconnected:
                # Disconnected-but-configured (:ai off, issue #167): the
                # transcript must survive visibility toggles — never the
                # setup wipe meant for a never-configured agent.
                self._panel.show_reconnect_hint()
            else:
                self._panel.show_setup_hint()
            return
        if self._model_name:
            self._render_header(self._session, self._model_name)
        self._panel.focus_input()

    def submit_prompt(self, text: str) -> None:
        """A prompt the user submitted in the chat input."""
        if self.blocked_in_protected():
            # agent.disable_in_protected (issue #83): in protected contexts
            # the agent must not run at all, not merely gate its writes.
            self._ui.notify(
                f"Agent is disabled in protected context {self._writes.protected_context!r}"
                " (agent.disable_in_protected)",
                severity="warning",
            )
            return
        if self._context.switching():
            # A turn started now would run during teardown/retarget and could
            # act on the new cluster with the old cluster's screen context.
            self._ui.notify(
                "A context switch is in progress — try again once it completes",
                severity="warning",
            )
            return
        if self._session is None:
            return
        task = self._task
        if task is not None and not task.done():
            # Interrupt-and-submit (issue #170): echo the correction now,
            # remember only the latest one, and cancel the running turn —
            # the replacement starts once the cancelled task settles (a
            # done callback, so it drains even when the cancel lands
            # before the task's coroutine ever ran).
            self._panel.echo_user(text)
            self._replacement = text
            if task.cancelling() == 0:
                # Never re-inject cancellation into a task that is already
                # cancelling: a second CancelledError can interrupt the
                # cleanup itself (review on #175). The depth-one queue
                # above already holds the newest correction.
                self._signal_interrupt()
                task.cancel()
            return
        self._replacement = None  # a direct turn supersedes any queue
        self._start_turn(text, echo=True)

    def _start_turn(self, text: str, *, echo: bool) -> None:
        self._panel.set_stop_key(self._panel.interrupt_key())
        self._panel.begin_turn(text, echo=echo)
        self._turn_finalized = False
        task = self._tasks.spawn(self.run_turn(text))
        task.add_done_callback(self._drain_replacement)
        self._task = task

    def _drain_replacement(self, task: asyncio.Task[None]) -> None:
        """Start the queued interrupt-and-submit correction once the
        cancelled turn's task settles — including the race where the task
        was cancelled before its coroutine (and thus its CancelledError
        handler) ever ran. Scoped to the current owner: a stale callback
        from a superseded task must not consume the queue or start a
        second concurrent turn."""
        if task is not self._task:
            return
        if task.cancelled() and not self._turn_finalized:
            # Cancelled before the coroutine's first step: its own
            # CancelledError handler never ran, so finalize here — the
            # session is untouched (finalize is inert then) but the panel
            # must still leave its running state.
            self._finish_interrupted_turn(self._session)
        replacement, self._replacement = self._replacement, None
        if replacement is None or self._context.switching() or self._shutting_down:
            return
        self._start_turn(replacement, echo=False)

    def interrupt(self) -> None:
        """Stop the running agent turn (issue #170). No-op when idle. A
        stop while an interrupt-and-submit is already draining discards
        the queued replacement (the user changed their mind about it) and
        never re-injects cancellation into the draining task."""
        task = self._task
        if task is None or task.done():
            return
        self._replacement = None
        if task.cancelling() == 0:
            self._signal_interrupt()
            task.cancel()

    def _signal_interrupt(self) -> None:
        """Tell the session to stop before the task is cancelled.

        Order matters: cancellation arrives as an exception wherever the
        turn happens to be suspended, while `interrupt` is the session's
        own cooperative stop. Signalling first lets the turn wind down at
        a boundary it chose; cancelling first would only ever unwind it.
        Only while a turn is live — an idle stop must signal nothing.
        """
        if self._session is not None:
            self._session.interrupt()

    async def wait_for_turn(self) -> None:
        """Drain the in-flight turn: await its task, absorbing the
        `CancelledError` an interrupted turn re-raises. The drain half of
        `shutdown`, which cancels first."""
        task = self._task
        if task is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ------------------------------------------------------------------
    # The turn itself
    # ------------------------------------------------------------------

    async def run_turn(self, user_text: str) -> None:
        """One agent turn: stream the session's events into the panel and
        mirror its cluster reads when follow is on.

        The user's text is all the controller hands over. What is on
        screen is not prose assembled here any more: the session reads it
        through the workspace port at the moment it needs it, so a turn
        that outlives a navigation sees the screen it is actually on.
        """
        session = self._session
        if session is None:
            return
        # Agent follow: started cluster reads awaiting their result, keyed
        # by call id (the finish event does not carry the arguments).
        pending_reads: dict[str, tuple[str, str]] = {}
        gen = session.run_turn(user_text)
        try:
            async for event in gen:
                self._panel.apply_event(event)
                await self._maybe_follow_read(event, pending_reads)
            with contextlib.suppress(Exception):
                self._render_header(session, self._model_name)
        except asyncio.CancelledError:
            # Close the generator first: if the cancel landed between
            # yields the generator is still suspended, and finalize must
            # not race a later resume that appends to the history.
            await _aclose(gen)
            self._finish_interrupted_turn(session)
            raise
        except Exception as exc:
            # The failure is ours (a panel/follow error), not the
            # session's: the session is still suspended at its `yield`,
            # holding the turn. Abandoning it there would strand the turn
            # and every later prompt would be refused as "a turn is
            # already running". So close the generator, let the session
            # repair the half-written history if it has one to repair,
            # and only then surface the error.
            await _aclose(gen)
            if session.finalization_pending:
                session.finalize_interrupt()
                # Finalization commits whatever the abandoned turn already
                # spent to the session's totals, but the error reported
                # below carries no usage for the panel to add. Repainting
                # from the session's own absolute numbers settles the
                # header now instead of leaving it stale until some later
                # turn happens to refresh it; `set_header` takes totals,
                # not a delta, so nothing the panel already added is
                # counted twice.
                #
                # Best-effort on purpose: a panel that just failed is
                # exactly where a repaint can fail too, and that must not
                # replace the failure the user needs to see — nor skip the
                # `AgentError` below, which is the only event that takes
                # the panel out of its running state.
                with contextlib.suppress(Exception):
                    self._render_header(session, self._model_name)
            self._panel.apply_event(AgentError(message=str(exc)))

    def _finish_interrupted_turn(self, session: AgentSession | None) -> None:
        """Settle an interrupted turn: repair the conversation history and
        mark the transcript (issue #170). The queued replacement, if any, is
        drained by the task's done callback — not here, because a task
        cancelled before its coroutine first ran never reaches this code.

        Finalization is asked for only when the session says it has a turn
        to finalize: a task cancelled before its coroutine ever ran left
        the session untouched, and demanding a repair it has no record of
        would raise. The panel still leaves its running state either way —
        the transcript belongs to the UI, not to the session.

        `TurnInterrupted` carries what *this* turn spent, not what the
        session has spent so far. The panel adds each turn's usage to the
        running total it shows, so a stop with nothing to finalize reports
        a zero delta: the session's cumulative totals would be counted a
        second time and the header would double after every stop.
        """
        self._turn_finalized = True
        if session is None:
            event = TurnInterrupted(input_tokens=0, output_tokens=0, estimated=False)
        elif session.finalization_pending:
            event = session.finalize_interrupt()
        else:
            # The turn never ran, so it spent nothing. Whatever the
            # session has already committed is on the header already.
            event = TurnInterrupted(
                input_tokens=0, output_tokens=0, estimated=session.usage_estimated
            )
        if not self._shutting_down:
            self._panel.apply_event(event)

    async def _maybe_follow_read(
        self,
        event: AgentEvent,
        pending: dict[str, tuple[str, str]],
    ) -> None:
        """Mirror a successful agent cluster read on screen (agent follow).

        Small local models rarely volunteer the UI tools — they call the
        data-returning reads and answer in text while the screen sits
        idle. With follow on, each successful read is mirrored through the
        same UIBridge mapping MCP follow uses (issue #153). Best-effort:
        `mirror_read` never raises, and the bridge guards (approval
        dialogs, screens the user is reading) refuse rather than cover.
        """
        if isinstance(event, ToolCallStarted):
            if event.name in FOLLOWABLE_TOOLS:
                pending[event.call_id] = (event.name, event.arguments)
            return
        if not isinstance(event, ToolCallFinished):
            return
        started = pending.pop(event.call_id, None)
        if started is None or not event.ok or not self._follow:
            return
        name, raw_arguments = started
        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return  # small models emit broken JSON; the read still answered
        if not isinstance(arguments, dict):
            return
        await mirror_read(self._follow_bridge() or self._bridge, name, arguments)

    def begin_shutdown(self) -> None:
        """Mark the agent session down, synchronously and idempotently.

        Teardown has awaits of its own to run before it reaches the agent
        (the bridge-dispatch reap, the proposal expiry sweep). A turn that
        an interrupt-and-submit left cancelling can settle inside any of
        them, and its drain callback would then start the queued
        replacement against a screen stack that is being torn down. Marking
        the session down before the *first* await of teardown closes that
        window; `shutdown` still cancels and reaps the task.
        """
        self._shutting_down = True

    async def shutdown(self) -> None:
        """Shutdown teardown of the agent turn: mark the session shutting
        down (finalization must not touch the torn-down transcript or start
        a replacement) and let the task drain. An explicit stop may already
        have the task cancelling — re-injecting cancellation would abort
        that cleanup mid-flight.

        Teardown normally marks the session down earlier, with
        `begin_shutdown` (the app has awaits to run before it reaches the
        controller); the call here is the defensive one for callers that
        only await this.
        """
        self.begin_shutdown()
        if self._task is not None:
            if self._task.cancelling() == 0:
                self._signal_interrupt()
                self._task.cancel()
            await self.wait_for_turn()
        session = self._session
        if session is not None and not self._session_closed:
            # After the drain, never before: closing a session out from
            # under a turn that is still unwinding would tear its cleanup
            # in half. Closed at most once from here even when teardown
            # calls `shutdown` twice; the composition root's guard may
            # still close the same session again, which `aclose` absorbs.
            self._session_closed = True
            await session.aclose()

    # ------------------------------------------------------------------
    # UIBridge implementation (spec §4.1 UI Bus): the agent drives the
    # exact same handlers as user keystrokes. Every method returns a
    # confirmation or an "ERROR: …" string and never raises (executor
    # contract), and every screen change is announced via notify so the
    # user always sees what the agent did.
    # ------------------------------------------------------------------

    def _mark_action(self, summary: str) -> None:
        self._ui.notify(summary, title="agent", severity="information", timeout=3)

    async def open_evidence(self, ref: str) -> str:
        """Open the read a citation points at (issue #192).

        The reference is resolved against the ledger, never against the
        answer text: a model that writes `[E9]` cannot make korvid open
        anything, because `E9` is not something korvid minted.

        Reuses the agent's own view entry points, so a citation cannot
        reach a screen the agent itself is not allowed to open - the
        approval-dialog guard included.
        """
        session = self._session
        if session is None:
            return "ERROR: the agent is not configured in this session"
        item = session.evidence.resolve(ref)
        if item is None:
            return f"ERROR: {ref} is not evidence from this turn"
        target = target_for(item)
        if target is None:
            return f"ERROR: {ref} has no view to open ({item.tool})"
        if target.view == "list":
            # A listing with no namespace covered every namespace; `None`
            # would instead be read as "keep the pane's current scope".
            scope = ALL_NAMESPACES if target.all_namespaces else target.namespace
            return await self.agent_navigate(target.kind or "", namespace=scope)
        self._displayed_incarnation = None
        if target.view == "logs":
            opened = await self._open_evidence_logs(ref, target)
        else:
            opened = await self._open_evidence_describe(ref, target)
        replaced = self._displayed_is_a_replacement(target)
        if replaced and not opened.startswith("ERROR:"):
            # Opened anyway: the user asked to see it, and the current
            # object is usually what they need next. Saying nothing is the
            # failure - the claim was about an object that no longer
            # exists (#250).
            return (
                f"{opened} (warning: this object was replaced since the cited read —"
                " what is on screen is a new instance, not the evidence)"
            )
        return opened

    def _displayed_is_a_replacement(self, target: EvidenceTarget) -> bool:
        """Whether what was just displayed is a different instance.

        Compared against the manifest the opener actually put on screen,
        not a separate lookup: a second fetch could disagree with the
        display, which would either miss a replacement or warn about one
        the user is not looking at.

        Only ever answers yes on a positive identification. No recorded
        incarnation, or a view that showed no identifiable object, means
        "cannot tell" - a warning nobody can trust is worse than none.
        """
        if target.incarnation is None or self._displayed_incarnation is None:
            return False
        return self._displayed_incarnation != target.incarnation

    async def _open_evidence_logs(self, ref: str, target: EvidenceTarget) -> str:
        """Stream the container the cited read actually looked at."""
        if target.name is None or target.namespace is None:
            return f"ERROR: {ref} does not name a pod to stream"
        container = target.container
        if target.needs_container_resolution:
            # The read defaulted to the pod's first container. Opening
            # every container would show streams that were not the
            # evidence, and could scroll the cited one away.
            #
            # Resolved from the live manifest, not the store: the cited pod
            # is often outside the pane's kind and scope, where the store
            # lookup finds nothing and the fallback reopens everything.
            try:
                triples = await self._pod_triples(target.namespace, target.name)
            except ApiStatusError as exc:
                return (
                    f"ERROR: {explain_api_error(exc.status, exc.reason, 'pods', target.namespace)}"
                )
            container = triples[0][2] if triples and triples[0][2] else None
        return await self.agent_open_logs(target.name, target.namespace, container=container)

    async def _open_evidence_describe(self, ref: str, target: EvidenceTarget) -> str:
        """Describe the cited object, saying so when its events are absent."""
        if target.kind is None or target.name is None:  # narrowed for typing
            return f"ERROR: {ref} does not name an object to describe"
        opened = await self.agent_open_describe(target.kind, target.name, target.namespace)
        if opened.startswith("ERROR:"):
            return opened
        if target.expects_events and self._view.canonical_kind(target.kind) != "pods":
            # Describe fetches events for pods only, so the cited events
            # are not on the screen this just opened. Saying so beats the
            # user hunting for evidence that is not there.
            return (
                f"{opened} (note: this evidence includes events, and korvid"
                " shows events for pods only - the events are not shown here)"
            )
        return opened

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        if self._screens.approval_dialog_active():
            # Same "user is deciding" rule as describe: swapping the view
            # beneath an approval dialog mid-decision is disorienting.
            return (
                "ERROR: an approval dialog is open — the user is deciding; "
                "wait for their decision before changing the view"
            )
        if self._screens.describe_screen_open():
            # The user opened a describe modal and is reading it; switching
            # the table underneath while reporting 'switched' would lie about
            # what's on screen. User action takes priority.
            return (
                "ERROR: a describe screen is open — the user is reading it; "
                "ask them to close it (Esc) before changing the view"
            )
        key = view.strip().lower()
        meta = self._view.aliases().get(key)
        if meta is None:
            return f"ERROR: unknown view {view!r} — not a resource kind in this cluster"
        if namespace and namespace.strip().lower() in ("all", ALL_NAMESPACES):
            # Same mapping as the human ':view all' command path.
            namespace = ALL_NAMESPACES
        try:
            # Canonical view kind, not the bare plural: safe under alias
            # collisions (same rule as the command-bar path).
            await self._navigation.navigate_command(self._view.canonical_kind(key), namespace)
        except Exception as exc:
            return f"ERROR: {exc}"
        kind, scope = self._view.current_kind(), self._view.current_scope()
        # Report what the user actually sees: apply the same filter as the
        # table render (substring/label/regex/… — issue #44) before counting.
        rows = self._visible_rows()
        self._mark_action(f"view → {kind} ({scope})")
        suffix = " (list may still be loading)" if not rows else ""
        pattern = self._workspace.filter_pattern
        filter_note = f" (filter {pattern!r} applied)" if pattern else ""
        return f"switched to {kind} in {scope} — {len(rows)} resources{filter_note}{suffix}"

    async def agent_set_filter(self, pattern: str) -> str:
        try:
            if pattern:
                self._navigation.set_filter(pattern)
            else:
                self._navigation.clear_filter()
        except Exception as exc:
            return f"ERROR: {exc}"
        if pattern:
            self._mark_action(f"filter → {pattern!r}")
            return f"filter set to {pattern!r} on the {self._view.current_kind()} view"
        self._mark_action("filter cleared")
        return "filter cleared"

    @staticmethod
    def _log_targets(
        known: list[Triple], namespace: str, pod: str, container: str | None
    ) -> list[Triple] | str:
        """The (ns, pod, container) triples to stream, or an "ERROR: ..."."""
        if not known:
            # Validate before cancel_tasks: a hallucinated pod name
            # must not tear down the streams the user is watching.
            return f"ERROR: pod {namespace}/{pod} not found (check the name and namespace)"
        if container:
            names = [c for _, _, c in known if c]
            if names and container not in names:
                return (
                    f"ERROR: container {container!r} not found in pod "
                    f"{namespace}/{pod} (containers: {', '.join(names)})"
                )
            return [(namespace, pod, container)]
        return known

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        if self._screens.approval_dialog_active():
            # Opening logs tears down the current log stream (the one the
            # user may be watching beneath the dialog while deciding).
            return (
                "ERROR: an approval dialog is open — the user is deciding; "
                "wait for their decision before opening logs"
            )
        if self._screens.describe_screen_open():
            # Same user-priority rule as describe/navigate/drill: opening
            # logs swaps the streams beneath the modal the user is reading.
            return (
                "ERROR: a describe screen is open — the user is reading it; "
                "ask them to close it (Esc) before opening logs"
            )
        if self._stream_logs() is None:
            return "ERROR: log streaming unavailable in this session"
        pane_gen = self._logs.pane_gen
        try:
            known = await self._pod_triples(namespace, pod)
            triples = self._log_targets(known, namespace, pod, container)
            if isinstance(triples, str):
                return triples
            if pane_gen != self._logs.pane_gen:
                # The user (or another turn) changed the log pane while we were
                # resolving containers — user keystrokes take priority.
                return (
                    "ERROR: the log pane changed while resolving containers "
                    "(user action takes priority) — retry if still needed"
                )
            if self._screens.approval_dialog_active():
                # The pre-check can go stale during the awaited lookup: an
                # approval dialog that opened meanwhile still wins before
                # the destructive log-pane teardown below.
                return (
                    "ERROR: an approval dialog is open — the user is deciding; "
                    "wait for their decision before opening logs"
                )
            await self._logs.cancel_tasks()
            if pane_gen != self._logs.pane_gen:
                # Recheck after the cancel await: a user pane change landing
                # in that window still wins.
                return (
                    "ERROR: the log pane changed while preparing the streams "
                    "(user action takes priority) — retry if still needed"
                )
            await self._logs.open_agent_logs(namespace, triples)
        except Exception as exc:
            return f"ERROR: {exc}"
        target = f"{namespace}/{pod}" + (f" [{container}]" if container else "")
        self._mark_action(f"logs → {target}")
        # open_pane caps at MAX_PANELS; tell the model which subset is
        # actually visible so it never assumes every container is on screen.
        truncated = ""
        if len(triples) > MAX_PANELS:
            truncated = (
                f" (showing first {MAX_PANELS} of {len(triples)} containers; "
                f"pass 'container' to view a specific one)"
            )
        return f"log pane opened for {target} — the user can now see the live logs{truncated}"

    async def _pod_triples(self, namespace: str, pod: str) -> list[Triple]:
        """All (ns, pod, container) triples for a pod the agent targets.

        The agent may open logs for a pod outside the visible view/scope, so
        the live manifest is authoritative; the store bucket is only a
        fallback. Returns an empty list when the pod cannot be found at all.
        """
        get_manifest = self._get_manifest()
        if get_manifest is not None:
            try:
                manifest = await get_manifest("pods", namespace, pod)
            except ApiStatusError:
                # The API authoritatively rejected the target (e.g. 404 for a
                # freshly deleted pod still in the watch cache) — surface it
                # instead of falling back to stale cache data.
                raise
            except Exception:
                logger.debug("agent logs: manifest container lookup failed", exc_info=True)
            else:
                spec = manifest.get("spec") or {}
                # Init and ephemeral containers are valid log targets too
                # (the human container picker exposes init containers).
                names = [
                    c.get("name")
                    for section in ("containers", "initContainers", "ephemeralContainers")
                    for c in spec.get(section) or []
                ]
                triples = [(namespace, pod, str(n)) for n in names if n]
                if triples:
                    return triples
        containers = self._pod_containers(namespace, pod)
        if containers:
            return [(namespace, pod, ctr) for ctr in containers]
        if any(
            obj.namespace == namespace and obj.name == pod and isinstance(obj, PodSummary)
            for obj in self._rows()
        ):
            # Known pod without container info: blank container = server default.
            return [(namespace, pod, "")]
        return []

    async def agent_drill_down(self, name: str) -> str:
        if self._screens.describe_screen_open():
            # Same user-priority guard as agent_navigate: drilling would
            # change the table hidden under the modal the user is reading.
            return (
                "ERROR: a describe screen is open — the user is reading it; "
                "ask them to close it (Esc) before changing the view"
            )
        canonical = self._view.canonical_kind(self._view.current_kind())
        child = drill_child(canonical)
        if child is None:
            return (
                f"ERROR: {canonical} has no drill-down chain - "
                "drill_down works on deployments, replicasets, and helm releases"
            )
        rows = self._rows()
        drill = self._workspace.drill
        drill_uid = drill.parent_uid
        if drill_uid is not None and self._view.current_kind() == drill.child_kind:
            rows = [r for r in rows if owned_by(r, drill_uid)]
        # drill_down acts on the visible table: apply the same filter as the
        # table render so the agent cannot drill into a hidden row.
        rows = filtered_rows(rows, self._workspace.focused.resource_filter)
        matches = [r for r in rows if r.name == name]
        if not matches:
            return f"ERROR: no {canonical} named {name!r} in the current view"
        if len(matches) > 1:
            return (
                f"ERROR: multiple {canonical} named {name!r} across namespaces - "
                "navigate to one namespace first"
            )
        error = await self._navigation.drill_into(matches[0].namespace, name)
        if error is not None:
            return f"ERROR: {error}"
        self._mark_action(f"drill → {drill.breadcrumb()}")
        return (
            f"drilled into {canonical}/{name} — now showing the {child} it owns "
            f"({drill.breadcrumb()})"
        )

    def _rows(self) -> list[Summary]:
        """The focused pane's store bucket."""
        return self._view.resources(self._view.current_kind(), self._view.current_scope())

    def _visible_rows(self) -> list[Summary]:
        """The focused pane's rows, filtered the way the table renders them."""
        return filtered_rows(self._rows(), self._workspace.focused.resource_filter)

    def _describe_precheck(self, kind: str, namespace: str | None) -> ResourceMeta | str:
        """Guards + target resolution for agent_open_describe: the meta to
        describe, or an "ERROR: ..." string."""
        if self._screens.approval_dialog_active():
            # Security invariant: approval dialogs are confirmed only by
            # user keystrokes. A describe pushed on top (agent- or MCP
            # follow-driven) would steal that focus mid-approval.
            return (
                "ERROR: an approval dialog is open — the user is deciding; "
                "wait for their decision before opening screens"
            )
        if self._screens.describe_screen_open():
            # Same user-priority rule as agent_navigate/agent_drill_down
            # (and the docs/agent.md follow contract): a describe screen on
            # top is being read — covering it with another would replace
            # the content mid-read. User action takes priority.
            return (
                "ERROR: a describe screen is open — the user is reading it; "
                "ask them to close it (Esc) before opening another"
            )
        if self._get_manifest() is None:
            return "ERROR: describe unavailable in this session"
        meta = self._view.aliases().get(kind.strip().lower())
        if meta is None:
            return f"ERROR: unknown kind {kind!r} — not a resource kind in this cluster"
        if meta.namespaced and not namespace:
            return f"ERROR: kind {kind!r} is namespaced — provide the 'namespace' argument"
        return meta

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        meta = self._describe_precheck(kind, namespace)
        if isinstance(meta, str):
            return meta
        get_manifest = self._get_manifest()
        if get_manifest is None:  # re-narrowed for typing; precheck guarantees it
            return "ERROR: describe unavailable in this session"
        # Snapshot the visible state: if the user pushes a screen or navigates
        # while the fetches below are pending, abort instead of covering it.
        top_screen = self._screens.top_screen()
        view_before = (self._view.current_kind(), self._view.current_scope())
        try:
            manifest = await get_manifest(meta.plural, namespace, name)
        except ApiStatusError as exc:
            return f"ERROR: {explain_api_error(exc.status, exc.reason, meta.plural, namespace)}"
        except Exception as exc:
            return f"ERROR: {exc}"
        events: list[dict[str, Any]] = []
        # Events are name-scoped only, so restrict to pods (same rule as `d`).
        get_events = self._get_events()
        if get_events is not None and namespace and meta.plural == "pods":
            try:
                events = await get_events.fetch(namespace, name)
            except Exception:  # events are best-effort; the manifest still shows
                logger.debug("agent describe: event fetch failed", exc_info=True)
        title = f"{meta.plural}/{namespace or '-'}/{name}"
        current_top = self._screens.top_screen()
        if (
            current_top is not top_screen
            or (
                self._view.current_kind(),
                self._view.current_scope(),
            )
            != view_before
        ):
            return (
                "ERROR: the screen changed while fetching the manifest "
                "(user action takes priority) — retry if still needed"
            )
        # When the chat panel is visible, show the non-modal pane on the left
        # instead of pushing a modal: a modal becomes the active screen and
        # would keep the chat input from taking focus. Resolved outside the
        # try below so a missing widget isn't masked as a generic push error.
        share = self._panel.expanded()
        try:
            await self._show_describe(share, title, manifest, events)
        except Exception as exc:
            return f"ERROR: {exc}"
        # The identity of what is *actually* displayed, recorded after the
        # display succeeded: checking a separate fetch beforehand leaves
        # the same race it was meant to close, only narrower (#250 review).
        self._displayed_incarnation = incarnation_of(manifest)
        self._mark_action(f"describe → {title}")
        return f"describe screen opened for {title} — manifest and events are on screen"

    async def _show_describe(
        self,
        share: bool,
        title: str,
        manifest: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> None:
        """Present a describe view: non-modal pane when sharing with the chat
        panel (modal screens would steal focus from the chat input)."""
        if manifest.get("kind") == "Secret":
            # Masking pipeline (design §7): this path is agent-driven, so the
            # rendered body is LLM-adjacent — secret values must never appear.
            manifest = mask_secret_manifest(manifest)
        footer = provider_footer_note(manifest, self._provider_hint())
        if share:
            self._screens.show_describe_pane(title, manifest, events, footer_note=footer)
        else:
            await self._ui.push_screen(DescribeScreen(title, manifest, events, footer_note=footer))

    # ------------------------------------------------------------------
    # Direct agent writes — every one through `WriteCoordinator`
    # ------------------------------------------------------------------

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        """Approval-gated write requested by the agent (spec §6.2): opens the
        same ConfirmScreen as the keybindings; only the user's keystroke can
        approve it, and the outcome (executed/denied/error) flows back as the
        tool result. Every executed write is audited with an agent marker."""
        name = name.strip()  # every stage below must see the exact same target
        # One stamp per approval request (rollout restarts only): the preview
        # and the executed write send the identical patch body.
        stamp = restart_stamp()
        built = self.build_write_op(
            action, kind, name, namespace, replicas, resources, restarted_at=stamp
        )
        if isinstance(built, str):
            return built
        meta, ns, op, operation, detail = built
        if not await self._writes.permitted(action, meta, ns, name):
            verb, target = self._writes.perm_target(action, meta)
            return f"ERROR: missing permission: {verb} {target}"
        try:
            # Capture the target's manifest *before* asking for approval:
            # the executed write carries its uid as a precondition, so the
            # approval is bound to this exact object incarnation - a
            # same-named replacement created while the dialog is open gets a
            # 409, not the mutation. The lookup uses the caller's validated
            # alias, not meta.plural: alias resolution is first-wins, so a
            # plural that collides across groups could otherwise resolve to
            # a different resource than the one validated above. The same
            # snapshot feeds the ownership banner - no second round trip.
            snapshot = await self.target_manifest(
                kind.strip().lower(),
                ns,
                name,
                strict=True,
            )
        except ApiStatusError:
            return f"ERROR: {gvr_label(meta)}/{name} not found{write_locus(ns)}"
        except TargetIdentityUnavailable:
            return (
                f"ERROR: target identity unavailable for {gvr_label(meta)}/{name}"
                f"{write_locus(ns)}; write blocked"
            )
        uid = manifest_uid(snapshot) if snapshot is not None else None
        if uid is None:
            return (
                f"ERROR: target identity has no UID for {gvr_label(meta)}/{name}"
                f"{write_locus(ns)}; write blocked"
            )
        preview = await self.preview_for_action(
            action, meta, ns, name, replicas, resources, uid, stamp
        )
        note = await self.managed_note_from(snapshot, ns) if snapshot is not None else None
        require = name if action == "delete" and not meta.namespaced else None
        impact_lines = (
            await self._resize_impact(meta, ns, name, uid, snapshot, resources)
            if action == "resize" and resources
            else None
        )
        decision = await self._await_user_approval(
            f"Agent requests: {action} {gvr_label(meta)}/{name}{write_locus(ns)}",
            operation,
            require_name=require,
            preview=preview,
            managed_note=note,
            impact_lines=impact_lines,
        )
        if decision == "expired":
            return (
                f"not approved: the request expired before the user responded"
                f" ({action} {gvr_label(meta)}/{name})"
            )
        if decision != "approved":
            return f"denied: the user declined the {action} request for {gvr_label(meta)}/{name}"
        outcome = await self._writes.run_shielded(
            action, meta, ns, name, lambda: op(uid), detail=detail
        )
        if outcome != "done":
            return f"ERROR: {action} {gvr_label(meta)}/{name} {outcome}"
        self._mark_action(f"{action} → {gvr_label(meta)}/{name}")
        return f"approved and executed: {action} {gvr_label(meta)}/{name}"

    async def preview_for_action(
        self,
        action: str,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        replicas: int | None,
        resources: dict[str, dict[str, dict[str, str]]] | None,
        uid: str | None,
        restarted_at: str,
    ) -> list[str] | None:
        """Dry-run preview for an agent-requested write; None (no preview)
        for unknown actions or a scale without a validated replica count.
        The captured ``uid`` and per-approval ``restarted_at`` stamp ride
        along so the dry run replays the exact request that would execute
        on approval."""
        ops = self._write_ops()
        if ops is None:
            return None
        if action == "delete":
            return await self._writes.dry_run_preview(ops.preview_delete(meta, ns, name, uid=uid))
        if action == "scale" and replicas is not None:
            return await self._writes.dry_run_preview(
                ops.preview_scale(meta, ns, name, replicas, uid=uid)
            )
        if action == "rollout_restart":
            return await self._writes.dry_run_preview(
                ops.preview_rollout_restart(meta, ns, name, uid=uid, restarted_at=restarted_at)
            )
        if action == "resize" and resources:
            return await self._writes.dry_run_preview(
                ops.preview_resize(ns or "", name, resources, uid=uid)
            )
        return None

    async def _resize_impact(
        self,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        snapshot: dict[str, Any] | None,
        resources: dict[str, dict[str, dict[str, str]]],
    ) -> tuple[str, ...]:
        context = classify_pod_resize(snapshot or {}, resources)
        graph_lines = await self._writes.impact_preview_for_scope(
            ImpactAction.POD_RESIZE,
            meta,
            ns,
            name,
            uid,
            scope=ns if meta.namespaced else None,
        )
        return compose_resize_impact_lines(graph_lines, context)

    async def target_manifest(
        self,
        kind_alias: str,
        ns: str | None,
        name: str,
        *,
        strict: bool = False,
    ) -> dict[str, Any] | None:
        """Manifest of a write target at request time, looked up by the same
        alias the write was validated with (both resolve through the one
        aliases mapping wired in __main__, so the manifest and the mutation
        address the same resource even when plurals collide across groups).
        Raises ApiStatusError(404) when the target does not exist (the caller
        turns that into an actionable error before bothering the user with a
        dialog). Best-effort callers fail open when no manifest source is
        wired or infrastructure lookup fails. Direct agent writes pass
        `strict=True`, translating those failures to
        `TargetIdentityUnavailable` so no approval can execute against an
        unverified replacement."""
        get_manifest = self._get_manifest()
        if get_manifest is None:
            if strict:
                raise TargetIdentityUnavailable
            return None
        try:
            return await asyncio.wait_for(get_manifest(kind_alias, ns, name), UID_LOOKUP_TIMEOUT)
        except ApiStatusError as exc:
            if exc.status == 404:
                raise
            if strict:
                raise TargetIdentityUnavailable from None
            logger.warning("uid lookup for %s/%s failed; writing without precondition", ns, name)
            return None
        except TimeoutError:
            if strict:
                raise TargetIdentityUnavailable from None
            logger.warning("uid lookup for %s/%s timed out; writing without precondition", ns, name)
            return None
        except Exception:
            if strict:
                raise TargetIdentityUnavailable from None
            logger.exception("uid lookup for %s/%s failed; writing without precondition", ns, name)
            return None

    async def target_uid(self, kind_alias: str, ns: str | None, name: str) -> str | None:
        """Uid of a write target at request time — `target_manifest` with
        only the precondition extracted (same 404/fail-open semantics)."""
        manifest = await self.target_manifest(kind_alias, ns, name)
        return manifest_uid(manifest) if manifest is not None else None

    async def managed_note(self, kind_alias: str, ns: str | None, name: str) -> str | None:
        """Ownership banner text for a write dialog, or None (issue #119).

        Best-effort display support, fail-open: no manifest source, a slow
        or failed lookup, or an unmanaged target all yield None — the write
        flow is never blocked, and the target fetch plus the entire
        owner-chain walk share one `UID_LOOKUP_TIMEOUT` deadline.
        """
        get_manifest = self._get_manifest()
        if get_manifest is None:
            return None
        try:
            async with asyncio.timeout(UID_LOOKUP_TIMEOUT):
                manifest = await get_manifest(kind_alias, ns, name)
                return await self._walk_managed(manifest, ns)
        except Exception as exc:  # display support only — never blocks the write
            # An API error message can embed the response body (for a
            # Secret, its data): log the exception type, not its payload.
            logger.debug("manager lookup for %s/%s failed: %s", ns, name, type(exc).__name__)
            return None

    async def managed_note_from(self, manifest: dict[str, Any], ns: str | None) -> str | None:
        """Manager note for an already-fetched manifest; the owner-chain
        walk shares one `UID_LOOKUP_TIMEOUT` deadline and fails open like
        `managed_note`."""
        try:
            async with asyncio.timeout(UID_LOOKUP_TIMEOUT):
                return await self._walk_managed(manifest, ns)
        except Exception as exc:  # display support only — never blocks the write
            # Same payload caution as managed_note — the exception type
            # only, and nothing derived from the manifest (which may be a
            # Secret's; CodeQL py/clear-text-logging-sensitive-data).
            logger.debug("owner-chain lookup in %s failed: %s", ns, type(exc).__name__)
            return None

    async def _walk_managed(self, manifest: dict[str, Any], ns: str | None) -> str | None:
        """Walk the built-in controller chain when the object itself looks
        unmanaged: a pod owned by rs -> deploy (or job -> cronjob) reports
        the top owner's manager — helm annotations live on the top-level
        object, not on every pod it produced. Callers bound this walk with
        one shared deadline and fail open on any error."""
        found = manager_of(manifest)
        current = manifest
        for _ in range(2):
            if found is not None:
                break
            owner = controller_owner(current)
            if owner is None:
                break
            plural = OWNER_CHAIN_PLURALS.get(owner[0])
            get_manifest = self._get_manifest()
            if plural is None or get_manifest is None:
                break
            current = await get_manifest(plural, ns, owner[1])
            found = manager_of(current)
        return found.note if found is not None else None

    # ------------------------------------------------------------------
    # Write-operation construction (shared with the proposal path)
    # ------------------------------------------------------------------

    def build_write_op(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None,
        replicas: int | None,
        resources: dict[str, dict[str, dict[str, str]]] | None,
        *,
        restarted_at: str,
    ) -> WriteOpBuild | str:
        """Validate an agent write request; return (meta, ns, op, operation
        description, audit detail) or an 'ERROR: ...' string. ``restarted_at``
        is the per-approval stamp a rollout restart shares with its preview."""
        if self._view.readonly():
            return "ERROR: read-only mode - cluster writes are disabled"
        if self._audit() is None:
            # Fail-closed auditing (AGENTS.md): no audit sink means no writes.
            return "ERROR: writes disabled - no audit log configured"
        name = name.strip()
        if not name:
            # JSON Schema 'required' does not reject empty strings; an empty
            # name would build a collection path instead of one exact object.
            # (agent_request_write pre-strips: keep this for direct callers.)
            return "ERROR: 'name' must be a non-empty resource name"
        namespace = namespace.strip() or None if namespace is not None else None
        resolved = self._write_meta(kind, namespace)
        if isinstance(resolved, str):
            return resolved
        meta, ns = resolved
        if action == "delete":
            return self._delete_op(meta, ns, name)
        if action == "scale":
            return self._scale_op(meta, ns, name, replicas)
        if action == "rollout_restart":
            return self._restart_op(meta, ns, name, restarted_at)
        if action == "resize":
            return self._resize_op(meta, ns, name, resources)
        return f"ERROR: unknown write action {action!r}"

    def _write_meta(
        self, kind: str, namespace: str | None
    ) -> tuple[ResourceMeta, str | None] | str:
        """Resolve an agent write's kind to a writable (meta, ns), or an
        'ERROR: ...' string: synthetic view kinds (helm browser) are
        read-only presentations of other objects and can never be written."""
        meta = self._view.aliases().get(kind.strip().lower())
        if meta is None:
            return f"ERROR: unknown kind {kind!r} - not a resource kind in this cluster"
        if meta.synthetic:
            return f"ERROR: kind {kind!r} is a read-only korvid view - it cannot be written"
        if meta.namespaced and not namespace:
            return f"ERROR: kind {kind!r} is namespaced - provide the 'namespace' argument"
        return meta, namespace if meta.namespaced else None

    def _delete_op(self, meta: ResourceMeta, ns: str | None, name: str) -> WriteOpBuild | str:
        ops = self._write_ops()
        if ops is None:
            return "ERROR: delete unavailable in this session"
        return (
            meta,
            ns,
            lambda uid: ops.delete_object(meta, ns, name, uid=uid),
            f"DELETE {gvr_label(meta)}/{name}{write_locus(ns)}",
            "requested by agent",
        )

    def _scale_op(
        self, meta: ResourceMeta, ns: str | None, name: str, replicas: int | None
    ) -> WriteOpBuild | str:
        ops = self._write_ops()
        if ops is None:
            return "ERROR: scale unavailable in this session"
        if (meta.group, meta.plural) not in SCALABLE:
            return f"ERROR: scale does not apply to {gvr_label(meta)}"
        if replicas is None or replicas < 0:
            return "ERROR: scale requires a 'replicas' argument >= 0"
        return (
            meta,
            ns,
            lambda uid: ops.scale_object(meta, ns, name, replicas, uid=uid),
            f"PATCH {gvr_label(meta)}/{name} scale -> {replicas} replicas{write_locus(ns)}",
            f"replicas -> {replicas}; requested by agent",
        )

    def _restart_op(
        self, meta: ResourceMeta, ns: str | None, name: str, restarted_at: str
    ) -> WriteOpBuild | str:
        ops = self._write_ops()
        if ops is None:
            return "ERROR: rollout restart unavailable in this session"
        if (meta.group, meta.plural) not in RESTARTABLE:
            return f"ERROR: rollout restart does not apply to {gvr_label(meta)}"
        return (
            meta,
            ns,
            lambda uid: ops.rollout_restart_with_stamp(
                meta, ns, name, uid=uid, restarted_at=restarted_at
            ),
            f"PATCH {gvr_label(meta)}/{name} pod template (restartedAt annotation)"
            f"{write_locus(ns)}",
            "requested by agent",
        )

    def _resize_op(
        self,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        resources: dict[str, dict[str, dict[str, str]]] | None,
    ) -> WriteOpBuild | str:
        ops = self._write_ops()
        if ops is None:
            return "ERROR: resize unavailable in this session"
        if (meta.group, meta.plural) != ("", "pods"):
            return f"ERROR: resize does not apply to {gvr_label(meta)}"
        if not self._pod_resize_supported():
            return "ERROR: this cluster does not expose pods/resize (requires Kubernetes 1.35+)"
        if not resources:
            return "ERROR: resize requires a non-empty 'resources' argument"
        namespace = ns or ""
        summary = resize_summary(resources)
        return (
            meta,
            ns,
            lambda uid: ops.resize_pod(namespace, name, resources, uid=uid),
            f"PATCH pods/{name}/resize: {summary}{write_locus(ns)}",
            f"{summary}; requested by agent",
        )

    # ------------------------------------------------------------------
    # The approval dialog an agent write must pass
    # ------------------------------------------------------------------

    def can_surface_approval(self) -> bool:
        """An approval dialog may only appear when the panel is expanded AND
        no other screen is stacked on top AND no inline text input owns the
        next key: pushing it over an active dialog or command/input editor
        would let the user's next y/Enter approve an unexpected write."""
        return (
            self._panel.expanded()
            and self._ui.screen_depth() == 1
            and not self._ui.inline_input_active()
        )

    async def _wait_until_surfaceable(self, deadline: float) -> bool:
        """Poll until an approval dialog may surface (panel expanded, no other
        screen on top); False when the deadline passes first."""
        loop = asyncio.get_running_loop()
        if self.can_surface_approval():
            return True
        pending_msg = "Agent write approval pending - open the agent panel (Ctrl-A) to review"
        self._ui.notify(pending_msg, severity="warning", timeout=10)
        last_reminder = loop.time()
        while not self.can_surface_approval():
            if loop.time() >= deadline:
                return False
            if loop.time() - last_reminder >= 30:
                # The first toast fades after 10s: keep reminding so the
                # request does not silently expire.
                self._ui.notify(pending_msg, severity="warning", timeout=10)
                last_reminder = loop.time()
            await asyncio.sleep(0.05)
        return True

    async def _await_user_approval(
        self,
        title: str,
        operation: str,
        *,
        require_name: str | None = None,
        preview: list[str] | None = None,
        managed_note: str | None = None,
        impact_lines: tuple[str, ...] | None = None,
    ) -> Literal["approved", "declined", "expired"]:
        """Show a ConfirmScreen and wait for the user's decision. Only real key
        input can resolve it. While the agent panel is collapsed, or another
        screen (a user dialog, describe, picker) is on top, the request stays
        pending instead of pushing a modal (spec 6.1: approval dialogs are
        never auto-opened from the collapsed state, and never stacked over an
        active dialog where a stray keystroke could approve it); it surfaces
        when the panel is expanded with a clear screen. Pending and on-screen
        time share one deadline, so an unanswered or never-surfaced request
        resolves as "expired" (distinct from an explicit "declined", so the
        agent is never told the user declined when nobody answered) and an
        agent turn can never hang forever."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._approval_timeout
        if not await self._wait_until_surfaceable(deadline):
            return "expired"
        fut: asyncio.Future[bool] = loop.create_future()

        def _done(confirmed: bool | None) -> None:
            if not fut.done():
                fut.set_result(bool(confirmed))

        screen = self._writes.confirm_screen(
            title,
            operation,
            require_name=require_name,
            preview=preview,
            managed_note=managed_note,
            impact_lines=impact_lines,
        )
        try:
            await self._ui.push_screen(screen, _done)
            # Recheck after mounting: surfacing the dialog (or push_screen
            # itself) can consume the last of the budget, and a fixed minimum
            # here would quietly extend the expiry contract past
            # the approval timeout.
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            confirmed = await asyncio.wait_for(fut, timeout=remaining)
            return "approved" if confirmed else "declined"
        except asyncio.CancelledError:
            # Turn interrupted (issue #170): never leave an orphaned dialog
            # whose 'y' would resolve a dead future. The write cannot run.
            # push_screen is inside the guarded region so a cancel landing
            # during the mount itself still dismisses the dialog.
            self._screens.dismiss_if_current(screen)
            raise
        except TimeoutError:
            # Late keystrokes are a no-op (the future is already resolved),
            # but clear the dialog when possible so it doesn't linger.
            if self._ui.is_current_screen(screen):
                self._screens.dismiss_if_current(screen)
            elif self._screens.is_stacked(screen):
                self._ui.notify(
                    "Agent write request expired - dismiss the pending dialog with Esc",
                    severity="warning",
                )
            return "expired"

    # ------------------------------------------------------------------
    # External write proposals — delegated, never implemented here
    # ------------------------------------------------------------------

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
        """External MCP write proposal intake — see `AgentProposals`."""
        return await self._proposals.submit_write_proposal(
            action,
            kind,
            name,
            namespace,
            replicas,
            resources,
            session_id=session_id,
            client_name=client_name,
            client_version=client_version,
        )

    async def get_write_proposal(self, proposal_id: str) -> str:
        """Terminal-outcome lookup for an external write proposal."""
        return await self._proposals.get_write_proposal(proposal_id)

    async def cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        """Caller-initiated cancel; only the submitting session may cancel."""
        return await self._proposals.cancel_write_proposal(proposal_id, session_id=session_id)
