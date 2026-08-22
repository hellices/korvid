"""The optional integrations' commands and state (issue #187).

`IntegrationController` owns the two things a korvid session can talk to
besides the cluster API, and the state each one keeps:

- **MCP** (issue #153): the live `:mcp on` / `:mcp off` toggle, serialized
  against the `:ctx` transaction, with the proposal sweeps that bracket a
  server run's authority; the `follow` mirror flag and its status badge;
  and the follow-up sweep for a teardown that outran its bounded stop.
- **Telepresence** (issue #159): the read-only `:tp` status panel, and the
  one-shot install hint driven by an epoch-bound, re-runnable
  traffic-manager probe.

Both are *optional*: a missing MCP extra or telepresence binary is a
reported absence here, never an error the caller must handle. Neither
integration is a cluster write, so no `WriteGate` appears; what they do
need is the `:ctx` guard, because an MCP run must not be restarted against
a client being swapped and a probe answering for a context the session has
already left must not raise a hint about it.

The proposal store is reached only through `IntegrationProposals` (the
sweeps are audited there, not here) and the switch serialization only
through `SwitchSerializer` - the same `nav_lock` the `:ctx` transaction
holds through quiesce/teardown/retarget.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Protocol

from korvid.agent.install_hint import isolated_install_hint
from korvid.core.mcp import MCPControllerBase
from korvid.k8s.telepresence import TelepresenceCLI, TelepresenceError
from korvid.ui.ui_surface import UiSurface
from korvid.ui.widgets.telepresence_screen import TelepresenceScreen
from korvid.ui.workspace_controller import ContextGuard


class IntegrationProposals(Protocol):
    """The audited expiry sweep an MCP run transition drives."""

    async def expire_all(self, reason: str) -> None: ...


class SwitchSerializer(Protocol):
    """Holder of the `:ctx` navigation lock the MCP toggle serializes on.

    The switch transaction holds it through quiesce/teardown/retarget, so
    taking it here is what keeps a toggle from starting a server against a
    client and alias map that are mid-swap.
    """

    @property
    def nav_lock(self) -> asyncio.Lock: ...


class IntegrationController:
    """Owns the MCP and telepresence commands, flows and state."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        context: ContextGuard,
        proposals: IntegrationProposals,
        serializer: SwitchSerializer,
        #: Read at call time, like `ContextSwitchCoordinator`'s own handle on
        #: it: None means the [mcp] extra is not installed and `:mcp` says so.
        mcp: Callable[[], MCPControllerBase | None],
        #: None = binary absent or kill-switched; the `:tp` panel reports that.
        telepresence: TelepresenceCLI | None,
        probe_traffic_manager: Callable[[], Awaitable[bool]] | None,
        telepresence_enabled: Callable[[], bool],
        follow_enabled: bool,
        refresh_status: Callable[[], None],
    ) -> None:
        self._ui = ui
        self._context = context
        self._proposals = proposals
        self._serializer = serializer
        self._mcp_fn = mcp
        self._telepresence = telepresence
        self._probe_traffic_manager = probe_traffic_manager
        self._telepresence_enabled = telepresence_enabled
        self._refresh_status = refresh_status
        #: MCP follow mode (issue #153): mirror external cluster reads in
        #: the TUI. Config seeds the state; `:mcp follow on|off` toggles it.
        self._follow = follow_enabled
        self._telepresence_hinted = False
        self._telepresence_probing = False
        self._telepresence_reprobe = False

    # ------------------------------------------------------------------
    # MCP
    # ------------------------------------------------------------------

    @property
    def follow_enabled(self) -> bool:
        """Current follow-mode state; read by the MCP server's wiring."""
        return self._follow

    @property
    def telepresence_available(self) -> bool:
        """Whether the telepresence CLI is wired; the help overlay documents
        `:tp` only where it can actually do something."""
        return self._telepresence is not None

    def note_activity(self, line: str) -> None:
        """Transient activity note for an external MCP read (issue #153):
        with follow off, this is the only trace an external host leaves on
        screen. Display only — never raises into the caller."""
        with contextlib.suppress(Exception):
            # markup=False: parts of the line are caller-controlled (pod and
            # namespace names from the MCP host) - Rich tags must render
            # literally, never restyle or forge toast content.
            self._ui.notify(line, title="MCP", severity="information", timeout=3, markup=False)

    def handle_mcp_command(self, args: list[str]) -> None:
        """`:mcp` shows server state; `:mcp on` / `:mcp off` toggle it live."""
        mcp = self._mcp_fn()
        if mcp is None:
            self._ui.notify(
                f"MCP unavailable — {isolated_install_hint(feature='mcp')}",
                severity="warning",
                markup=False,
            )
            return
        if not args:
            follow = "follow on" if self._follow else "follow off"
            self._ui.notify(f"{mcp.status()} · {follow}")
            return
        action = args[0].lower()
        if action == "follow":
            self.handle_follow_command(args[1:])
            return
        if action not in ("on", "off"):
            self._ui.notify("Usage: :mcp [on|off] | :mcp follow [on|off]", severity="warning")
            return
        self._toggle_server(mcp, action)

    def handle_follow_command(self, args: list[str]) -> None:
        """`:mcp follow [on|off]` (issue #153): toggle mirroring of external
        cluster reads in the TUI. Bare `:mcp follow` flips the state."""
        if args and args[0].lower() not in ("on", "off"):
            self._ui.notify("Usage: :mcp follow [on|off]", severity="warning")
            return
        self._follow = args[0].lower() == "on" if args else not self._follow
        state = "on" if self._follow else "off"
        mirrored = "mirrored on screen" if self._follow else "no longer mirrored"
        self._ui.notify(f"MCP follow {state} — external reads are {mirrored}")
        self._refresh_status()

    def _toggle_server(self, mcp: MCPControllerBase, action: str) -> None:
        """Start/stop the MCP server live (`:mcp on` / `:mcp off`)."""
        if self._context.switching():
            # The switch quiesced the server before swapping the client and
            # alias map; a toggle landing mid-swap could restart it against
            # state that is being replaced.
            self._ui.notify(
                "A context switch is in progress — try again once it completes",
                severity="warning",
            )
            return
        self._ui.run_worker(self._switch_server(mcp, action), exclusive=False)

    async def _switch_server(self, mcp: MCPControllerBase, action: str) -> None:
        """The toggle itself, serialized against the `:ctx` transaction.

        The switch holds `nav_lock` through quiesce/teardown/retarget, so
        the state is re-checked *inside* it: a toggle queued just before the
        switch claimed the transaction could otherwise start the server
        against the client/alias map mid-swap, or have its stop undone by
        the switch's restart.
        """
        async with self._serializer.nav_lock:
            if self._context.switching():
                self._ui.notify(
                    "A context switch is in progress — try again once it completes",
                    severity="warning",
                )
                return
            was_running = mcp.running
            if action == "on" and not was_running:
                # Any pending proposal predates the run about to start —
                # its capability token is from an older, ended run.
                # Sweep BEFORE the endpoint goes live: once start()
                # returns, the new run's callers may already have
                # submitted, and their work must not be expired as
                # old-run stragglers.
                await self._proposals.expire_all("the MCP server was restarted")
            msg = await (mcp.start() if action == "on" else mcp.stop())
            # A real stop invalidates every capability token handed out
            # for that run (issue #110): pending proposals from it must
            # not survive. A stop whose bounded teardown timed out
            # (`running` still True) has still ended that run's
            # authority, so it expires too; only an idempotent
            # status-preserving toggle (`:mcp on` while already
            # running) keeps pending work.
            stopped = action == "off" and was_running
            # Captured under the lock and BEFORE the audited sweep: the
            # sweep awaits audit appends, and the dying run's task can
            # finish during that wait — `running` re-read afterwards
            # would be False, skipping the follow-up sweep that catches
            # the run's last in-flight submissions. The wait below must
            # bind to *this* dying run, never to whichever run the
            # controller owns once the lock is released.
            old_task = mcp.pending_task() if stopped and mcp.running else None
            if stopped:
                await self._proposals.expire_all("the MCP server was stopped")
        self._ui.notify(msg, severity="error" if msg.startswith("ERROR") else "information")
        self._refresh_status()
        if old_task is not None:
            # The bounded stop timed out and the old run is still dying
            # in the background: wait it out, then sweep again so an
            # in-flight submission that raced the teardown cannot
            # outlive its server run.
            await self._sweep_after_teardown(mcp, old_task)

    async def _sweep_after_teardown(self, mcp: MCPControllerBase, task: asyncio.Task[None]) -> None:
        """Final proposal sweep once a dragged-out MCP teardown completes.

        `stop()`'s bounded wait can return while the old server run is still
        dying; an in-flight proposal call on that run may land *after* the
        stop-time sweep. *task* is the old run's server task, captured under
        `nav_lock` before it was released: the follow-up wait binds to that
        exact run, so a fresh server started by a racing `:mcp on` is never
        the one waited on or torn down here.
        """
        with contextlib.suppress(Exception):
            await asyncio.wait({task})
        # Serialize the sweep decision against a racing `:mcp on`: if a
        # fresh run came up while the old teardown dragged on, pending
        # proposals belong to that live run (its start transition already
        # swept old-run stragglers) and must not be expired here.
        async with self._serializer.nav_lock:
            if mcp.running:
                return
            await self._proposals.expire_all("the MCP server was stopped")

    # ------------------------------------------------------------------
    # Telepresence
    # ------------------------------------------------------------------

    def handle_telepresence_command(self) -> None:
        """`:tp` / `:telepresence` (issue #159): open the read-only status
        panel. Queries run only here, on the explicit user action - the
        telepresence CLI spawns its local user daemon to answer, so korvid
        never polls it in the background."""
        tp = self._telepresence
        if tp is None:
            self._ui.notify(
                "telepresence not available — binary not on PATH, or disabled "
                "via `integrations: {telepresence: off}`",
                severity="warning",
            )
            return
        self._ui.run_worker(self._open_panel(tp), exclusive=True, group="telepresence")

    async def _open_panel(self, tp: TelepresenceCLI) -> None:
        with self._ui.progress("querying telepresence"):
            try:
                status = await tp.status()
                intercepts = (
                    await tp.list_intercepts(daemon=status.daemon_name or None)
                    if status.connected
                    else []
                )
            except TelepresenceError as exc:
                # stderr tails are hostile input for a markup toast.
                self._ui.notify(str(exc), title="telepresence", severity="error", markup=False)
                return
        await self._ui.push_screen(TelepresenceScreen(status, intercepts))

    async def maybe_hint_telepresence(self) -> None:
        """One dim hint per session (issue #159): the cluster runs a
        traffic-manager but the local client is absent. The probe is an
        injected pure API check - never the telepresence binary; a missing
        probe, a failure or the kill-switch all silently mean no hint.

        Re-runnable until the hint actually shows: the startup cluster may
        lack a manager while a later `:ctx` target runs one. Results are
        epoch-bound - a probe answering for a context that was already left
        is discarded - and a re-probe requested while one is in flight is
        queued instead of lost.
        """
        if (
            self._telepresence is not None
            or self._telepresence_hinted
            or not self._telepresence_enabled()
            or self._probe_traffic_manager is None
        ):
            return
        if self._telepresence_probing:
            # A :ctx switch mid-probe: run again once the old probe (whose
            # answer describes the old cluster) unwinds.
            self._telepresence_reprobe = True
            return
        self._telepresence_probing = True
        epoch = self._context.epoch()
        try:
            present = await self._probe_traffic_manager()
        except Exception:
            return  # absent / forbidden / transient: all mean "no hint"
        finally:
            self._telepresence_probing = False
            if self._telepresence_reprobe:
                self._telepresence_reprobe = False
                self._ui.run_worker(self.maybe_hint_telepresence(), exclusive=False)
        if not present or self._context.epoch() != epoch:
            return  # no manager, or the answer describes a left context
        self._telepresence_hinted = True
        self._ui.notify(
            "telepresence traffic-manager detected in this cluster — install "
            "the client and restart korvid to inspect intercepts (`:tp`)",
            severity="information",
            timeout=8,
        )
