"""Runtime kube-context switching, as its own coordinator (issue #36 /
issue #187 Deep Task 8).

A `:ctx` switch retargets *the whole session*: the client, the alias table,
the audit log's attribution, the watches, the metrics poller, the log streams,
the port-forward registry, the embedded MCP server, the agent's screen
context, the proposal inbox and the session timeline all belong to one
cluster at a time. `ContextSwitchCoordinator` owns that transaction, which
used to live directly on `KorvidApp`:

- the switch epoch and the in-flight claim every other flow revalidates
  against — this class *is* the session's single `ContextGuard`;
- the kubeconfig listing, the pre-switch auth probe and the connection swap;
- the `:ctx` picker, its kubeconfig-context completion prefetch task, and
  the picker's display-label mapping (the `:ns` completion prefetch belongs
  to `WorkspaceController`, which the transaction drives directly);
- the no-op check, the pre-probe guards, and the blocker set (busy agent,
  reserved write, open dialog, open namespace picker);
- the embedded MCP quiesce, whose failure aborts the switch untouched;
- the ordered teardown of every old-cluster consumer;
- the connection retarget with its restore-the-old-context recovery;
- the atomic application of a proven switch (identity, default namespace,
  capability gates, protected-context marker, audit attribution, forward
  registry, view reset, per-context CLI wrappers, agent note);
- the timeline phases and the Warning-feed / watch / metrics resume.

The ordering below is the safety property, not an implementation detail:

1. serialize with the workspace navigation lock;
2. refuse while any blocker holds;
3. probe the target *before* anything is torn down;
4. quiesce the MCP server and expire proposals before the first fallible
   teardown await;
5. stop watches, pollers, streams and forwards, then clear the store;
6. retarget the connection;
7. bump the epoch exactly once, and only when a context is actually applied;
8. reset the workspace and restart the feeds.

Textual is reached only through `UiSurface` and the named ports below; the
coordinator never imports or holds `KorvidApp`, so the whole transaction is
exercised without a running app. The app keeps the `:ctx` message handlers as
thin delegates and implements the two app-owned boundaries (`ContextSurface`,
`SessionConfiguration`).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from korvid.core.audit import AuditLog
from korvid.core.mcp import MCPControllerBase
from korvid.core.portforward import ForwardRecord, ForwardRegistry
from korvid.k8s.helmcli import HelmCLI
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.workspace_controller import ContextGuard

logger = logging.getLogger(__name__)

#: The app worker group holding the old cluster's hint-events fetch. Cancelled
#: (not awaited) mid-teardown, exactly where the hint cache is dropped, so no
#: late result or retry can resurrect old-cluster hints.
HINT_EVENTS_GROUP = "hint-events"

#: Suffix marking the active context in the `:ctx` picker.
CURRENT_CONTEXT_SUFFIX = " (current)"


@dataclasses.dataclass(frozen=True)
class ContextSwitchResult:
    """What the composition root re-derived for the new cluster (issue #36).

    Returned by the injected ``switch_context`` callable once the connection
    is retargeted: capability gates are per-cluster facts the session must
    adopt atomically with the switch.
    """

    pod_resize_supported: bool
    provider_hint: str | None
    context_namespace: str | None
    #: helm CLI wrapper rebound to the new context (issue #31 x #36): the
    #: startup HelmCLI pins --kube-context, so keeping it across a switch
    #: would send approval-gated helm writes to the OLD cluster.
    helm: HelmCLI | None = None
    #: The new context's name when it matches `protected_contexts` (issue
    #: #83), None otherwise — the marker is re-derived on every switch.
    protected_context: str | None = None


# ---------------------------------------------------------------------------
# App-owned boundaries
# ---------------------------------------------------------------------------


class ContextSurface(ABC):
    """The app-owned widget and presentation facts a `:ctx` switch drives.

    Everything here is owned by `KorvidApp` because it is either a widget
    (the command bar's completion words, the describe pane, the inline
    namespace picker), an app worker group, or a UI-bus post. The transaction
    itself lives in this module: no method below may run any part of it.
    """

    @abstractmethod
    def request_switch(self, name: str) -> None:
        """Put a picked context on the UI bus as a `SwitchContextCommand`.

        The picker's answer is a *user command*, routed like the one the
        command bar posts, so both entry points converge on one handler.
        """

    @abstractmethod
    def namespace_picker_open(self) -> bool:
        """Whether the inline namespace picker is showing old-cluster options.

        Not a screen, so `UiSurface.screen_depth` does not see it; its
        options would survive teardown and a later selection would navigate
        the new cluster to a namespace picked from the old one.
        """

    @abstractmethod
    def hide_describe(self) -> None:
        """Dismiss the describe pane covering the table."""

    @abstractmethod
    def set_context_words(self, names: list[str]) -> None:
        """Publish the kubeconfig contexts as `:ctx` completions."""

    @abstractmethod
    def cancel_worker_group(self, group: str) -> None:
        """Cancel an app worker group *without* waiting for it to settle.

        Deliberately not `UiSurface.cancel_workers`: the hint-events worker's
        exception path re-populates the cache the very next teardown step
        clears, so the cancellation is delivered here and the cache is
        dropped before the loop can run it.
        """

    @abstractmethod
    def refresh_completions(self) -> None:
        """Rebuild the command-bar word lists after the alias table changed."""

    @abstractmethod
    def refresh_status(self) -> None:
        """Repaint the status bar and top-bar legend."""

    @abstractmethod
    def resources_updated(self, kind: str) -> None:
        """Post the UI-bus render request for *kind* on the new cluster."""


class SessionConfiguration(ABC):
    """How a proven switch is applied to the session's own configuration.

    Split from the rest of the transaction because these are the facts the
    *app* owns and every other flow reads directly (the active context, the
    session default namespace, the per-cluster capability gates, the
    context-pinned CLI wrappers) — not participants with a lifecycle.
    """

    @abstractmethod
    def kube_context(self) -> str | None:
        """The context in effect, or None for the kubeconfig default."""

    @abstractmethod
    def adopt(self, context: str | None, result: ContextSwitchResult) -> None:
        """Adopt the new cluster's identity, default namespace and capabilities.

        The target's kubeconfig namespace becomes the session default too:
        the `ns` toggle-back and the helm/operator namespace fallbacks read
        it, and jumping to the *startup* context's namespace after a switch
        would cross clusters.
        """

    @abstractmethod
    def retarget_tools(self, result: ContextSwitchResult) -> None:
        """Rebind context-pinned CLI wrappers and re-probe optional integrations."""


# ---------------------------------------------------------------------------
# Ordered lifecycle participants
# ---------------------------------------------------------------------------


class SwitchWorkspace(Protocol):
    """The workspace halves of the switch, plus the lock it serializes on.

    Satisfied by `WorkspaceController`: `:ctx`, `:mcp`, the proposal
    execution claim and every navigation share `nav_lock`, so the whole
    quiesce/retarget/resume window is inside it.
    """

    @property
    def nav_lock(self) -> asyncio.Lock: ...

    async def quiesce_for_context_switch(self) -> None: ...

    def reset_view_after_switch(self) -> None: ...

    async def sync_metrics_poller(self) -> None: ...

    def clear_namespace_words(self) -> None: ...

    async def cancel_namespace_prefetch(self) -> None: ...

    def start_namespace_prefetch(self) -> None: ...


class SwitchWatches(Protocol):
    """The watch fleet the switch stops wholesale and restarts (structural)."""

    async def stop_all(self) -> None: ...

    async def start(self, kind: str, scope: str) -> None: ...


class SwitchStore(Protocol):
    """The resource store whose old-cluster rows must not survive."""

    def clear_all(self) -> None: ...


class SwitchLogs(Protocol):
    """The log subsystem holding streams against the old connection."""

    async def close(self) -> None: ...


class SwitchHints(Protocol):
    """The hint strip's event cache and parked-cursor refresh timer."""

    def teardown(self) -> None: ...


class SwitchTimeline(Protocol):
    """Session-timeline recording plus its epoch-bound Warning feed."""

    def record_context_switch(
        self,
        *,
        epoch: int,
        phase: Literal["started", "completed", "failed"],
        from_context: str | None,
        to_context: str | None,
        note: str = "",
    ) -> None: ...

    def start_warning_watch(self) -> None: ...

    async def stop(self) -> None: ...


class SwitchProposals(Protocol):
    """The external write-proposal inbox, invalidated by a committed switch."""

    async def expire_all(self, reason: str) -> None: ...


class SwitchForwards(Protocol):
    """Port-forward session teardown, audit flush and re-open."""

    async def teardown(self, registry: ForwardRegistry) -> list[ForwardRecord]: ...

    async def flush_audits(self) -> None: ...

    def reopen(self) -> None: ...


class SwitchWrites(Protocol):
    """The write perimeter's switch-relevant reads and the protected marker.

    `active_writes` is why a switch cannot start under an in-flight mutation:
    retargeting the client beneath one would land it on the wrong cluster.
    """

    def active_writes(self) -> int: ...

    def set_protected_context(self, name: str | None) -> None: ...


class SwitchAgent(Protocol):
    """The agent session's busy flag — all the switch needs to know.

    The cluster change itself is no longer announced as prose: the
    session is retargeted with typed cluster facts by the composition
    root, and its own epoch snapshot tells the next turn what it is
    looking at.
    """

    @property
    def busy(self) -> bool: ...


class ContextSwitchCoordinator(ContextGuard):
    """Owns the `:ctx` session state and the ordered switch transaction."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        surface: ContextSurface,
        view: ViewState,
        session: SessionConfiguration,
        store: SwitchStore,
        watches: SwitchWatches,
        #: Late-bound participants: every one of them is a controller that
        #: takes *this* coordinator as its `ContextGuard`, so they cannot be
        #: constructed first. Each accessor returns the real typed
        #: collaborator - the transaction below calls it directly and never
        #: routes a step back through the app.
        workspace: Callable[[], SwitchWorkspace],
        logs: Callable[[], SwitchLogs],
        hints: Callable[[], SwitchHints],
        timeline: Callable[[], SwitchTimeline],
        proposals: Callable[[], SwitchProposals],
        forwards: Callable[[], SwitchForwards],
        registry: Callable[[], ForwardRegistry | None],
        writes: Callable[[], SwitchWrites],
        agent: Callable[[], SwitchAgent],
        mcp: Callable[[], MCPControllerBase | None],
        audit: Callable[[], AuditLog | None],
        #: `:ctx` collaborators wired by the composition root: kubeconfig
        #: listing, the pre-switch auth probe, and the connection/capability
        #: retarget. All None in builds without a cluster connection.
        list_contexts: Callable[[], tuple[list[str], str | None]] | None = None,
        probe_context: Callable[[str], Awaitable[None]] | None = None,
        switch_context: Callable[[str | None], Awaitable[ContextSwitchResult]] | None = None,
    ) -> None:
        self._ui = ui
        self._surface = surface
        self._view = view
        self._session = session
        self._store = store
        self._watches = watches
        self._workspace = workspace
        self._logs = logs
        self._hints = hints
        self._timeline = timeline
        self._proposals = proposals
        self._forwards = forwards
        self._registry = registry
        self._writes = writes
        self._agent = agent
        self._mcp = mcp
        self._audit = audit
        self._list_contexts = list_contexts
        self._probe_context = probe_context
        self._switch_context = switch_context
        #: True while a switch is probing, tearing down or retargeting;
        #: refuses concurrent switches and marks every captured epoch stale.
        self._switching = False
        #: Bumped every time a context is applied: pre-approval awaits
        #: capture it and refuse to proceed if the cluster changed under them.
        self._epoch = 0
        #: The kubeconfig context prefetch warming `:ctx` completions.
        self._prefetch_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # ContextGuard — the session's single switch-state read surface
    # ------------------------------------------------------------------

    def epoch(self) -> int:
        return self._epoch

    def switching(self) -> bool:
        return self._switching

    def reads_allowed(self) -> bool:
        """Refuse read actions that spawn cluster streams during a switch.

        Streams started mid-swap would attach to whichever cluster wins the
        switch while still labeled with the old selection (issue #84).
        Returns True when it is safe to proceed.
        """
        if self._switching:
            self._ui.notify(
                "A context switch is in progress — try again once it completes",
                severity="warning",
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Warm the `:ctx` completions off the message pump (app mount).

        A local kubeconfig read, but off-loop so a slow filesystem never
        blocks mount.
        """
        list_contexts = self._list_contexts
        if list_contexts is None:
            return

        # No error boundary here on purpose. The wired collaborator is
        # `k8s.client.list_context_names`, which is total by contract: an
        # unreadable or malformed kubeconfig yields `([], None)` rather than
        # raising (pinned by
        # `tests/k8s/test_client.py::test_list_context_names_unreadable_returns_empty`).
        # No specific exception type is documented for it, and a blanket
        # `except Exception` would swallow programming errors here — and in
        # `shutdown`, which reaps this task — instead of surfacing them.
        async def _fetch() -> None:
            names, _active = await asyncio.to_thread(list_contexts)
            self._surface.set_context_words(names)

        self._prefetch_task = asyncio.create_task(_fetch())

    async def shutdown(self) -> None:
        """Cancel and reap the completion prefetch (app unmount).

        Reaped rather than abandoned: the task publishes into a widget, and
        an unmounting app must not leave that landing behind it.
        """
        task = self._prefetch_task
        if task is None:
            return
        self._prefetch_task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ------------------------------------------------------------------
    # Entry points — the app's `:ctx` handlers delegate straight to these
    # ------------------------------------------------------------------

    def show_picker(self) -> None:
        """`:ctx` with no argument: pick a kubeconfig context from a list."""
        self._ui.run_worker(self._show_picker(), exclusive=False)

    def switch(self, name: str) -> None:
        """`:ctx <name>`: run the whole switch transaction for *name*."""
        self._ui.run_worker(self._switch_flow(name), exclusive=False)

    async def _show_picker(self) -> None:
        list_contexts = self._list_contexts
        if list_contexts is None:
            self._ui.notify("Context switching unavailable in this build", severity="warning")
            return
        names, active = await asyncio.to_thread(list_contexts)
        if not names:
            self._ui.notify("No contexts found in kubeconfig", severity="warning")
            return
        self._surface.set_context_words(names)
        # Sessions started from the kubeconfig current-context have no
        # explicit config value — fall back to what the kubeconfig reports.
        current = self._session.kube_context() or active
        # Explicit display->name mapping: decoding the label (suffix strip)
        # would corrupt a real context whose name ends in " (current)".
        labels: dict[str, str] = {}
        for name in names:
            label = f"{name}{CURRENT_CONTEXT_SUFFIX}" if name == current else name
            if label != name and (label in names or label in labels):
                label = name  # marker collides with another context's name
            labels[label] = name

        def _on_pick(choice: str | None) -> None:
            if choice is None:
                return
            self._surface.request_switch(labels.get(choice, choice))

        self._ui.push_screen(PickScreen("Switch context:", list(labels)), _on_pick)

    # ------------------------------------------------------------------
    # The transaction
    # ------------------------------------------------------------------

    async def _switch_flow(self, name: str) -> None:
        """Orchestrate a context switch: guards, auth probe, teardown, swap.

        The probe runs against a private client configuration first — on any
        failure nothing has been torn down and the old context keeps working
        (issue #36's "don't strand the user" requirement). Only a proven
        target proceeds to teardown and retarget.
        """
        if self._probe_context is None or self._switch_context is None:
            self._ui.notify("Context switching unavailable in this build", severity="warning")
            return
        # Claim before the first await: two queued SwitchContextCommands
        # must not both pass the guards and race the teardown.
        if self._switching:
            self._ui.notify("A context switch is already in progress", severity="warning")
            return
        self._switching = True
        try:
            await self._switch_locked(name)
        finally:
            self._switching = False

    async def _switch_locked(self, name: str) -> None:
        """The body of `_switch_flow`; runs with the claim held."""
        old = self._session.kube_context()
        if await self._is_noop(name, old):
            self._ui.notify(f"Already on context {name}")
            return
        if not await self._guards_pass(name):
            return
        # The switch is now committed to attempting: recorded before the
        # probe, on the epoch that is still serving this session. Anything
        # refused above never started, and inventing a `started` for it
        # would put a transition in the record that never happened.
        epoch = self._epoch
        self._timeline().record_context_switch(
            epoch=epoch, phase="started", from_context=old, to_context=name
        )
        try:
            await self._probe_context(name)  # type: ignore[misc]  # guarded by caller
        except Exception as exc:
            # Nothing was torn down and no cluster was applied, so the
            # failure belongs to the epoch that is still live.
            self._timeline().record_context_switch(
                epoch=epoch,
                phase="failed",
                from_context=old,
                to_context=name,
                note=describe_context_error(exc),
            )
            self._ui.notify(
                f"Cannot switch to context {name!r}: {describe_context_error(exc)}"
                f" — staying on {old or 'the current context'}",
                severity="error",
                timeout=10,
            )
            return
        async with self._workspace().nav_lock:
            # The probe awaited network I/O — an agent turn or a dialog may
            # have started meanwhile; re-check before anything is torn down.
            blocker = self._blocker()
            if blocker is not None:
                # A `started` with no terminal phase would read as a switch
                # still in flight; the abort is the outcome, on the old epoch.
                self._timeline().record_context_switch(
                    epoch=epoch,
                    phase="failed",
                    from_context=old,
                    to_context=name,
                    note=blocker,
                )
                self._ui.notify(blocker, severity="warning")
                return
            # Quiesce the embedded MCP server BEFORE any teardown: external
            # callers share the client and alias map being swapped, and an
            # undrainable server must abort while the old context is still
            # fully usable (watches, forwards, store all intact).
            mcp_restart = await self._quiesce_mcp()
            if mcp_restart is None:
                self._timeline().record_context_switch(
                    epoch=epoch,
                    phase="failed",
                    from_context=old,
                    to_context=name,
                    note="embedded MCP server did not stop in time",
                )
                return
            # Old-context proposals are stale the moment this committed
            # transition begins: the old MCP run (and its capability) is
            # already stopped, and both the teardown below and the retarget
            # perform fallible awaits — expire them now, not at a later
            # point that an exception could keep from ever being reached.
            await self._proposals().expire_all("kube context switched")
            await self._teardown()
            ok, applied = await self._retarget(name, old)
            if not ok:
                if mcp_restart:
                    self._ui.notify(
                        "Embedded MCP server was stopped for the switch —"
                        " restart it with :mcp on once reconnected",
                        severity="warning",
                        timeout=15,
                    )
                return
            self._resume_timeline_after_retarget(name, old, applied)
            mcp = self._mcp()
            if mcp_restart and mcp is not None:
                # Resume on the same endpoint, now serving whichever context
                # was actually applied (target, or the restored old one).
                msg = await mcp.start()
                self._ui.notify(msg, severity="error" if msg.startswith("ERROR") else "information")
            await self._watches.start(self._view.current_kind(), self._view.current_scope())
            await self._workspace().sync_metrics_poller()
        self._surface.resources_updated(self._view.current_kind())
        self._surface.refresh_status()
        self._workspace().start_namespace_prefetch()
        self._surface.refresh_completions()
        if applied == name:
            self._ui.notify(f"Switched to context {name} (ns: {self._view.current_scope()})")

    def _resume_timeline_after_retarget(
        self, name: str, old: str | None, applied: str | None
    ) -> None:
        """Close out the switch on the timeline and rebind its Warning feed.

        `completed` belongs to the epoch the switch created, so the new
        cluster's record opens with the switch that started it — but only
        when the requested target is what got applied: `_apply` also runs
        while *restoring* the old context after a failed swap, and recording
        completion there would report the target that failed as if it had
        succeeded. The feed restarts either way: teardown cancelled the old
        epoch's, and whichever context is now applied deserves one.
        """
        if applied == name:
            self._timeline().record_context_switch(
                epoch=self._epoch,
                phase="completed",
                from_context=old,
                to_context=name,
                note="all cluster state was reset",
            )
        self._timeline().start_warning_watch()

    async def _quiesce_mcp(self) -> bool | None:
        """Drain and stop the embedded MCP server ahead of a context switch.

        Returns True when a restart is owed after the switch, False when the
        server was not running, and None when the server could not be drained
        in time — the switch must then abort with nothing torn down.
        """
        mcp = self._mcp()
        if mcp is None or not mcp.running:
            return False
        pending = await mcp.shutdown()
        if pending is not None:
            # Even cancellation didn't land within its deadline: an in-flight
            # tool call could cross the context boundary if we proceeded.
            self._ui.notify(
                "Embedded MCP server did not stop in time — context"
                " switch aborted (old context untouched)",
                severity="error",
                timeout=10,
            )
            return None
        return True

    async def _is_noop(self, name: str, old: str | None) -> bool:
        """True when *name* is already the active context — explicitly, or as
        the kubeconfig's active context for sessions started without
        -c/--context (old stays None there for the recovery path)."""
        effective = old
        if effective is None and self._list_contexts is not None:
            with contextlib.suppress(Exception):
                _, effective = await asyncio.to_thread(self._list_contexts)
        return name == effective

    def _blocker(self) -> str | None:
        """Why a switch cannot proceed right now, or None when it can."""
        if self._agent().busy:
            return "Agent is busy — wait for the current turn to finish before switching contexts"
        if self._writes().active_writes():
            return (
                "A cluster write is in progress — wait for it to finish before switching contexts"
            )
        if self._ui.screen_depth() > 1:
            return "Close open dialogs before switching contexts"
        if self._surface.namespace_picker_open():
            return "Close the namespace picker before switching contexts"
        return None

    async def _guards_pass(self, name: str) -> bool:
        """Pre-probe refusals; each states why the switch cannot start now."""
        blocker = self._blocker()
        if blocker is not None:
            self._ui.notify(blocker, severity="warning")
            return False
        if self._list_contexts is not None:
            names, _ = await asyncio.to_thread(self._list_contexts)
            if names and name not in names:
                self._ui.notify(
                    f"Unknown context {name!r} — kubeconfig has: {', '.join(names)}",
                    severity="error",
                )
                return False
        return True

    async def _teardown(self) -> None:
        """Stop every consumer of the old cluster before the client swaps.

        Order matters: streams and pollers first (they hold the old
        connection), then session state that would otherwise leak old-cluster
        rows, breadcrumbs, or hints into the new one.
        """
        await self._logs().close()
        self._surface.hide_describe()
        # The workspace controller folds the split back to one pane, stops and
        # de-targets the metrics poller, clears the drill breadcrumb, cancels
        # the relationship workers holding the old client, and clears the
        # filter — the workspace-only halves of the teardown (issue #187 /
        # Deep Task 3). It runs first (under the nav lock, held by the
        # caller) so those pollers and workers release the old connection
        # before the wholesale watch stop below.
        await self._workspace().quiesce_for_context_switch()
        # An old-cluster namespace prefetch still in flight could land after
        # the new cluster's and overwrite its completions — cancel it first.
        # Both steps run on the workspace controller: it owns the prefetch
        # task and the `:ns` completion words.
        await self._workspace().cancel_namespace_prefetch()
        # Completions that already loaded are old-cluster names — drop them
        # now so they aren't offered while (or if) the new prefetch fails.
        self._workspace().clear_namespace_words()
        await self._watches.stop_all()
        registry = self._registry()
        if registry is not None:
            # Same quiesce-stop-audit sequence as app exit: in-flight
            # launches land first, stop_all runs off-loop (it polls up to
            # the grace deadline), and every stop is enqueued for audit.
            stopped = await self._forwards().teardown(registry)
            if stopped:
                self._ui.notify(f"Stopped {len(stopped)} port-forward(s) targeting the old cluster")
        # Old-cluster audit entries resolve their context only at append();
        # flush them before `_apply` re-points the audit log, or they would
        # be written as belonging to the new cluster.
        await self._forwards().flush_audits()
        self._store.clear_all()
        # The hint-events worker holds the old client and its exception path
        # re-populates the cache — cancel it (and the parked-cursor refresh
        # timer) before the cache is cleared, so no late result or retry can
        # resurrect old-cluster hints.
        self._surface.cancel_worker_group(HINT_EVENTS_GROUP)
        self._hints().teardown()
        await self._timeline().stop()

    async def _retarget(self, name: str, old: str | None) -> tuple[bool, str | None]:
        """Swap the connection to *name*; on failure fall back to *old*.

        Returns ``(ok, applied)``: ``ok`` is False only when even the
        fallback failed (the session then needs a restart — everything is
        already torn down and nothing is connected). ``applied`` is the
        context actually in effect, which may legitimately be None (the
        kubeconfig default) — that is why success is a separate flag.

        Old-context proposals were already expired by the caller (right
        after MCP quiescing), before teardown or either switch attempt.
        """
        # Captured before the first attempt: `_apply` bumps the epoch as its
        # first action, so reading it in the handler below could file a
        # failed swap under the epoch it failed to create.
        epoch = self._epoch
        try:
            result = await self._switch_context(name)  # type: ignore[misc]  # guarded by caller
            self._apply(name, old, result)
            return True, name
        except Exception as exc:
            self._timeline().record_context_switch(
                epoch=epoch,
                phase="failed",
                from_context=old,
                to_context=name,
                note=describe_context_error(exc),
            )
            self._ui.notify(
                f"Context switch to {name!r} failed mid-swap: {describe_context_error(exc)}",
                severity="error",
                timeout=10,
            )
        try:
            result = await self._switch_context(old)  # type: ignore[misc]  # guarded by caller
            self._apply(old, old, result)
            self._ui.notify(f"Restored context {old or '(kubeconfig default)'}")
            return True, old
        except Exception as exc:
            self._ui.notify(
                f"Could not restore context {old or '(kubeconfig default)'}:"
                f" {describe_context_error(exc)} — restart korvid",
                severity="error",
                timeout=15,
            )
            return False, None

    def _apply(self, name: str | None, old: str | None, result: ContextSwitchResult) -> None:
        """Adopt the new cluster's identity and re-probed capabilities.

        Synchronous by design: everything below lands in one event-loop
        slice, so no flow can observe a session half on either cluster.
        """
        self._epoch += 1
        self._session.adopt(name, result)
        self._writes().set_protected_context(result.protected_context)
        if result.protected_context is not None:
            self._ui.notify(
                f"Context {result.protected_context!r} is protected — writes"
                " require typing the context name",
                severity="warning",
                timeout=10,
            )
        audit = self._audit()
        if audit is not None:
            audit.set_context(name)
        registry = self._registry()
        if registry is not None:
            # Reopen the registry that teardown latched closed; forwards
            # started from now on target the new cluster.
            registry.retarget(name)
            self._forwards().reopen()
        # Adopt the new cluster's default view (pods in its default namespace)
        # through the workspace controller, which owns the view state and the
        # view-scoped binding refresh.
        self._workspace().reset_view_after_switch()
        self._session.retarget_tools(result)


def describe_context_error(exc: Exception) -> str:
    """One short phrase for a probe or swap failure, for user and timeline."""
    if isinstance(exc, TimeoutError):
        return "authentication check timed out"
    return str(exc) or type(exc).__name__
