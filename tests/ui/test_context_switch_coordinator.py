"""Direct tests for `ContextSwitchCoordinator` — the `:ctx` transaction
(issue #36 / issue #187 Deep Task 8).

The coordinator owns everything about a runtime kube-context switch that used
to live on `KorvidApp`: the switch epoch, the in-flight claim, the context
listing/probe/swap collaborators, the picker and its completion prefetch, the
no-op and blocker refusals, the MCP quiesce, the ordered teardown, the
connection retarget with its recovery path, the atomic application of a proven
switch, and the timeline/watch/metrics resume.

Every collaborator is reached through a named port, so the whole transaction
is exercised here without a running app. The ordering these tests pin *is* the
safety property: probe before teardown, quiesce before retarget, epoch bumped
exactly once and only when a switch applies.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import pytest

from korvid.core.audit import AuditLog
from korvid.core.mcp import MCPControllerBase
from korvid.core.portforward import ForwardRecord, ForwardRegistry
from korvid.ui.context_switch_coordinator import (
    HINT_EVENTS_GROUP,
    ContextSurface,
    ContextSwitchCoordinator,
    ContextSwitchResult,
    SessionConfiguration,
)
from korvid.ui.widgets.pick_screen import PickScreen

from .test_write_coordinator import FakeUi, FakeView

# ---------------------------------------------------------------------------
# Fakes — one shared ordered log, so "what happened when" is directly readable
# ---------------------------------------------------------------------------


class Log:
    """The single ordered transcript every participant appends to."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def __call__(self, event: str) -> None:
        self.events.append(event)

    def index(self, event: str) -> int:
        return self.events.index(event)

    def has(self, event: str) -> bool:
        return event in self.events


class FakeSurface(ContextSurface):
    """The app's widget/presentation facts, recorded."""

    def __init__(self, log: Log) -> None:
        self._log = log
        self.requested: list[str] = []
        self.picker_open = False
        self.context_words: list[str] = []
        self.namespace_words: list[str] = ["team-old"]

    def request_switch(self, name: str) -> None:
        self.requested.append(name)

    def namespace_picker_open(self) -> bool:
        return self.picker_open

    def hide_describe(self) -> None:
        self._log("describe-hide")

    def set_context_words(self, names: list[str]) -> None:
        self.context_words = list(names)

    def clear_namespace_words(self) -> None:
        self.namespace_words = []
        self._log("ns-words-cleared")

    async def cancel_namespace_prefetch(self) -> None:
        self._log("ns-prefetch-cancelled")

    def start_namespace_prefetch(self) -> None:
        self._log("ns-prefetch-started")

    def cancel_worker_group(self, group: str) -> None:
        self._log(f"workers-cancelled:{group}")

    def refresh_completions(self) -> None:
        self._log("completions-refreshed")

    def refresh_status(self) -> None:
        self._log("status-refreshed")

    def resources_updated(self, kind: str) -> None:
        self._log(f"resources-updated:{kind}")


class FakeSession(SessionConfiguration):
    """Session identity, default namespace and per-cluster capabilities."""

    def __init__(self, log: Log, *, context: str | None = "ctx-a") -> None:
        self._log = log
        self.context = context
        self.namespace = "default"
        self.adopted: list[tuple[str | None, ContextSwitchResult]] = []
        self.tools: list[ContextSwitchResult] = []

    def kube_context(self) -> str | None:
        return self.context

    def adopt(self, context: str | None, result: ContextSwitchResult) -> None:
        self.context = context
        self.namespace = result.context_namespace or self.namespace
        self.adopted.append((context, result))
        self._log("adopt-config")

    def retarget_tools(self, result: ContextSwitchResult) -> None:
        self.tools.append(result)
        self._log("retarget-tools")


class FakeWorkspace:
    """`nav_lock` plus the workspace halves of quiesce and resume."""

    def __init__(self, log: Log, view: FakeView, session: FakeSession) -> None:
        self._log = log
        self._view = view
        self._session = session
        self._lock = asyncio.Lock()

    @property
    def nav_lock(self) -> asyncio.Lock:
        return self._lock

    async def quiesce_for_context_switch(self) -> None:
        self._log("workspace-quiesce")

    def reset_view_after_switch(self) -> None:
        self._view.kind = "pods"
        self._view.scope = self._session.namespace
        self._log("workspace-reset")

    async def sync_metrics_poller(self) -> None:
        self._log("metrics-sync")


class FakeWatches:
    def __init__(self, log: Log) -> None:
        self._log = log

    async def stop_all(self) -> None:
        self._log("watch-stop-all")

    async def start(self, kind: str, scope: str) -> None:
        self._log(f"watch-start:{kind}/{scope}")


class FakeStore:
    def __init__(self, log: Log) -> None:
        self._log = log

    def clear_all(self) -> None:
        self._log("store-clear")


class FakeLogs:
    def __init__(self, log: Log) -> None:
        self._log = log

    async def close(self) -> None:
        self._log("logs-close")


class FakeHints:
    def __init__(self, log: Log) -> None:
        self._log = log

    def teardown(self) -> None:
        self._log("hints-teardown")


class FakeTimeline:
    """Records the phases and the Warning-feed lifecycle."""

    def __init__(self, log: Log) -> None:
        self._log = log
        self.phases: list[tuple[int, str, str]] = []

    def record_context_switch(
        self,
        *,
        epoch: int,
        phase: Literal["started", "completed", "failed"],
        from_context: str | None,
        to_context: str | None,
        note: str = "",
    ) -> None:
        self.phases.append((epoch, phase, note))
        self._log(f"timeline:{phase}@{epoch}")

    def start_warning_watch(self) -> None:
        self._log("timeline-feed-start")

    async def stop(self) -> None:
        self._log("timeline-stop")


class FakeProposals:
    def __init__(self, log: Log) -> None:
        self._log = log
        self.reasons: list[str] = []

    async def expire_all(self, reason: str) -> None:
        self.reasons.append(reason)
        self._log("proposals-expired")


class FakeForwards:
    def __init__(self, log: Log, *, stopped: int = 0) -> None:
        self._log = log
        self._stopped = stopped

    async def teardown(self, registry: ForwardRegistry) -> list[ForwardRecord]:
        self._log("forwards-teardown")
        return [_forward_record(port) for port in range(self._stopped)]

    async def flush_audits(self) -> None:
        self._log("forwards-flush")

    def reopen(self) -> None:
        self._log("forwards-reopen")


class RecordingRegistry(ForwardRegistry):
    """A real registry whose retarget is observable."""

    def __init__(self, log: Log) -> None:
        super().__init__(context="ctx-a")
        self._log = log
        self.retargets: list[str | None] = []

    def retarget(self, context: str | None) -> None:
        self.retargets.append(context)
        self._log(f"registry-retarget:{context}")
        super().retarget(context)


class FakeWrites:
    def __init__(self, log: Log, *, active: int = 0) -> None:
        self._log = log
        self.active = active
        self.protected: list[str | None] = []

    def active_writes(self) -> int:
        return self.active

    def set_protected_context(self, name: str | None) -> None:
        self.protected.append(name)
        self._log(f"protected-context:{name}")


class FakeAgent:
    def __init__(self, log: Log, *, busy: bool = False) -> None:
        self._log = log
        self._busy = busy
        self.notes: list[str] = []

    @property
    def busy(self) -> bool:
        return self._busy

    def note_context_switch(self, note: str) -> None:
        self.notes.append(note)
        self._log("agent-note")


class FakeMCP(MCPControllerBase):
    """An embedded MCP server with a scripted drain outcome."""

    def __init__(self, log: Log, *, live: bool = True, drainable: bool = True) -> None:
        self._log = log
        self._live = live
        self._drainable = drainable
        self.start_result = "MCP on :4321"

    @property
    def running(self) -> bool:
        return self._live

    def status(self) -> str:
        return "MCP :4321" if self._live else "MCP off"

    async def start(self) -> str:
        self._live = True
        self._log("mcp-start")
        return self.start_result

    async def stop(self) -> str:
        self._live = False
        return "MCP off"

    async def shutdown(self) -> asyncio.Task[None] | None:
        self._log("mcp-shutdown")
        if not self._drainable:
            return asyncio.get_running_loop().create_future()  # type: ignore[return-value]  # a still-pending teardown
        self._live = False
        return None


class RecordingAudit(AuditLog):
    def __init__(self, log: Log, path: Path) -> None:
        super().__init__(path, context="ctx-a")
        self._log = log
        self.contexts: list[str | None] = []

    def set_context(self, context: str | None) -> None:
        self.contexts.append(context)
        self._log(f"audit-context:{context}")
        super().set_context(context)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class Env:
    """A coordinator wired to recording fakes for every port."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        contexts: tuple[str, ...] = ("ctx-a", "ctx-b"),
        active_context: str | None = "ctx-a",
        session_context: str | None = "ctx-a",
        probe_error: Exception | None = None,
        switch_error: Exception | None = None,
        restore_error: Exception | None = None,
        result: ContextSwitchResult | None = None,
        mcp: FakeMCP | None = None,
        stopped_forwards: int = 0,
        wire_collaborators: bool = True,
    ) -> None:
        self.log = Log()
        self.ui = FakeUi()
        self.view = FakeView(kind="pods", scope="default")
        self.surface = FakeSurface(self.log)
        self.session = FakeSession(self.log, context=session_context)
        self.workspace = FakeWorkspace(self.log, self.view, self.session)
        self.watches = FakeWatches(self.log)
        self.store = FakeStore(self.log)
        self.logs = FakeLogs(self.log)
        self.hints = FakeHints(self.log)
        self.timeline = FakeTimeline(self.log)
        self.proposals = FakeProposals(self.log)
        self.forwards = FakeForwards(self.log, stopped=stopped_forwards)
        self.registry: ForwardRegistry | None = RecordingRegistry(self.log)
        self.writes = FakeWrites(self.log)
        self.agent = FakeAgent(self.log)
        self.mcp = mcp
        self.audit = RecordingAudit(self.log, tmp_path / "audit.jsonl")
        self.contexts = list(contexts)
        self.active_context = active_context
        self.probe_error = probe_error
        self.switch_error = switch_error
        self.restore_error = restore_error
        self.result = result or ContextSwitchResult(
            pod_resize_supported=True, provider_hint="AKS", context_namespace="ns-b"
        )
        self.probes: list[str] = []
        self.swaps: list[str | None] = []
        self.list_calls = 0

        def list_contexts() -> tuple[list[str], str | None]:
            self.list_calls += 1
            return list(self.contexts), self.active_context

        async def probe(name: str) -> None:
            self.probes.append(name)
            self.log(f"probe:{name}")
            if self.probe_error is not None:
                raise self.probe_error

        async def swap(name: str | None) -> ContextSwitchResult:
            self.swaps.append(name)
            self.log(f"swap:{name}")
            if len(self.swaps) == 1 and self.switch_error is not None:
                raise self.switch_error
            if len(self.swaps) > 1 and self.restore_error is not None:
                raise self.restore_error
            return self.result

        self.coordinator = ContextSwitchCoordinator(
            ui=self.ui,
            surface=self.surface,
            view=self.view,
            session=self.session,
            store=self.store,
            watches=self.watches,
            workspace=lambda: self.workspace,
            logs=lambda: self.logs,
            hints=lambda: self.hints,
            timeline=lambda: self.timeline,
            proposals=lambda: self.proposals,
            forwards=lambda: self.forwards,
            registry=lambda: self.registry,
            writes=lambda: self.writes,
            agent=lambda: self.agent,
            mcp=lambda: self.mcp,
            audit=lambda: self.audit,
            list_contexts=list_contexts if wire_collaborators else None,
            probe_context=probe if wire_collaborators else None,
            switch_context=swap if wire_collaborators else None,
        )

    async def switch(self, name: str = "ctx-b") -> None:
        """Run one whole `:ctx <name>` flow to completion."""
        self.coordinator.switch(name)
        await self.ui.settle()


def _forward_record(port: int) -> ForwardRecord:
    from korvid.core.portforward import ForwardSpec

    return ForwardRecord(
        id=port,
        spec=ForwardSpec(
            kind="pods", namespace="default", name=f"p-{port}", local_port=port, remote_port=80
        ),
    )


#: Every event of a clean switch, in the only order that is safe.
_SUCCESSFUL_ORDER = [
    "timeline:started@0",
    "probe:ctx-b",
    "mcp-shutdown",
    "proposals-expired",
    "logs-close",
    "describe-hide",
    "workspace-quiesce",
    "ns-prefetch-cancelled",
    "ns-words-cleared",
    "watch-stop-all",
    "forwards-teardown",
    "forwards-flush",
    "store-clear",
    f"workers-cancelled:{HINT_EVENTS_GROUP}",
    "hints-teardown",
    "timeline-stop",
    "swap:ctx-b",
    "adopt-config",
    "protected-context:None",
    "audit-context:ctx-b",
    "registry-retarget:ctx-b",
    "forwards-reopen",
    "workspace-reset",
    "retarget-tools",
    "agent-note",
    "timeline:completed@1",
    "timeline-feed-start",
    "mcp-start",
    "watch-start:pods/ns-b",
    "metrics-sync",
    "resources-updated:pods",
    "status-refreshed",
    "ns-prefetch-started",
    "completions-refreshed",
]


# ---------------------------------------------------------------------------
# Ordering — the transaction itself
# ---------------------------------------------------------------------------


async def test_a_switch_runs_quiesce_retarget_apply_and_resume_in_one_fixed_order(
    tmp_path: Path,
) -> None:
    """The single ordering pin: MCP quiesce and proposal expiry precede any
    teardown, every old-cluster consumer stops before the swap, and the
    resume only starts once the new cluster is applied."""
    env = Env(tmp_path)
    env.mcp = FakeMCP(env.log)
    await env.switch()
    assert env.log.events == _SUCCESSFUL_ORDER


async def test_the_target_is_probed_before_anything_is_torn_down(tmp_path: Path) -> None:
    """A failed probe must leave the old context fully usable, so the probe
    runs before the first destructive step."""
    env = Env(tmp_path)
    await env.switch()
    assert env.log.index("probe:ctx-b") < env.log.index("logs-close")
    assert env.log.index("probe:ctx-b") < env.log.index("watch-stop-all")


async def test_a_failed_probe_tears_nothing_down_and_keeps_the_old_context(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, probe_error=RuntimeError("unauthorized"))
    await env.switch()
    assert env.swaps == []
    assert env.session.context == "ctx-a"
    assert not env.log.has("logs-close")
    assert not env.log.has("watch-stop-all")
    assert env.coordinator.epoch() == 0
    assert any("staying on ctx-a" in message for message in env.ui.messages())


async def test_a_probe_timeout_is_described_as_an_authentication_timeout(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, probe_error=TimeoutError())
    await env.switch()
    assert env.timeline.phases == [
        (0, "started", ""),
        (0, "failed", "authentication check timed out"),
    ]


# ---------------------------------------------------------------------------
# Epoch
# ---------------------------------------------------------------------------


async def test_a_successful_switch_increments_the_epoch_exactly_once(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.switch()
    assert env.coordinator.epoch() == 1


async def test_the_epoch_is_unchanged_when_the_probe_fails(tmp_path: Path) -> None:
    env = Env(tmp_path, probe_error=RuntimeError("nope"))
    await env.switch()
    assert env.coordinator.epoch() == 0


async def test_the_epoch_is_unchanged_when_every_swap_attempt_fails(tmp_path: Path) -> None:
    env = Env(
        tmp_path,
        switch_error=RuntimeError("target unreachable"),
        restore_error=RuntimeError("old cluster gone too"),
    )
    await env.switch()
    assert env.coordinator.epoch() == 0
    assert any("restart korvid" in message for message in env.ui.messages())


async def test_a_failed_swap_that_restores_the_old_context_bumps_the_epoch_once(
    tmp_path: Path,
) -> None:
    """The restore is itself an application of a context: it must be visible
    to every awaiting flow, so the epoch moves — but only once, and the
    switch is never recorded as completed."""
    env = Env(tmp_path, switch_error=RuntimeError("target unreachable"))
    await env.switch()
    assert env.swaps == ["ctx-b", "ctx-a"]
    assert env.coordinator.epoch() == 1
    assert [phase for _epoch, phase, _note in env.timeline.phases] == ["started", "failed"]
    assert env.session.context == "ctx-a"
    assert any("Restored context ctx-a" in message for message in env.ui.messages())


async def test_a_flow_that_awaited_through_the_switch_sees_a_crossed_epoch(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    before = env.coordinator.epoch()
    await env.switch()
    assert env.coordinator.crossed(before) is True
    assert env.coordinator.crossed(env.coordinator.epoch()) is False


async def test_everything_is_crossed_while_the_switch_is_in_flight(tmp_path: Path) -> None:
    """A flow that captured the *current* epoch is still stale mid-switch:
    the client is being swapped under it."""
    gate = asyncio.Event()
    env = Env(tmp_path)
    observed: list[tuple[bool, bool, int]] = []

    async def probe_that_waits(name: str) -> None:
        env.probes.append(name)
        observed.append(
            (
                env.coordinator.switching(),
                env.coordinator.crossed(env.coordinator.epoch()),
                env.coordinator.epoch(),
            )
        )
        await gate.wait()

    env.coordinator._probe_context = probe_that_waits
    env.coordinator.switch("ctx-b")
    await asyncio.sleep(0)
    assert observed == [(True, True, 0)]
    gate.set()
    await env.ui.settle()


# ---------------------------------------------------------------------------
# Refusals: no-ops, unknown targets, blockers, concurrency
# ---------------------------------------------------------------------------


async def test_switching_to_the_active_context_is_a_no_op(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.switch("ctx-a")
    assert env.probes == []
    assert env.timeline.phases == []
    assert env.ui.messages() == ["Already on context ctx-a"]


async def test_the_kubeconfig_default_counts_as_the_active_context(tmp_path: Path) -> None:
    """A session started without -c has no explicit context: the no-op check
    falls back to whatever the kubeconfig reports as active."""
    env = Env(tmp_path, session_context=None, active_context="ctx-a")
    await env.switch("ctx-a")
    assert env.probes == []
    assert env.ui.messages() == ["Already on context ctx-a"]


async def test_an_unknown_context_is_rejected_before_the_probe(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.switch("ctx-zzz")
    assert env.probes == []
    assert env.timeline.phases == []
    assert any("Unknown context 'ctx-zzz'" in message for message in env.ui.messages())


async def test_a_busy_agent_blocks_the_switch(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.agent = FakeAgent(env.log, busy=True)
    await env.switch()
    assert env.probes == []
    assert env.timeline.phases == []
    assert any("Agent is busy" in message for message in env.ui.messages())


async def test_a_reserved_write_blocks_the_switch(tmp_path: Path) -> None:
    """The write reservation is the whole point of `active_writes`: a switch
    that retargeted the client under an in-flight mutation would land it on
    the wrong cluster."""
    env = Env(tmp_path)
    env.writes.active = 1
    await env.switch()
    assert env.probes == []
    assert env.swaps == []
    assert any("A cluster write is in progress" in message for message in env.ui.messages())


async def test_an_open_dialog_blocks_the_switch(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.ui.depth = 2
    await env.switch()
    assert env.probes == []
    assert any("Close open dialogs" in message for message in env.ui.messages())


async def test_an_open_namespace_picker_blocks_the_switch(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.surface.picker_open = True
    await env.switch()
    assert env.probes == []
    assert any("Close the namespace picker" in message for message in env.ui.messages())


async def test_a_blocker_that_appears_during_the_probe_aborts_before_teardown(
    tmp_path: Path,
) -> None:
    """The probe awaits network I/O; a write or dialog started meanwhile must
    still stop the switch, and the aborted attempt is recorded as failed so
    the timeline never shows a switch stuck in flight."""
    gate = asyncio.Event()
    env = Env(tmp_path)

    async def probe_then_write(name: str) -> None:
        env.probes.append(name)
        await gate.wait()
        env.writes.active = 1

    env.coordinator._probe_context = probe_then_write
    env.coordinator.switch("ctx-b")
    await asyncio.sleep(0)
    gate.set()
    await env.ui.settle()
    assert env.swaps == []
    assert not env.log.has("logs-close")
    assert env.timeline.phases[-1][1] == "failed"
    assert "A cluster write is in progress" in env.timeline.phases[-1][2]


async def test_a_second_switch_is_refused_while_one_is_in_flight(tmp_path: Path) -> None:
    gate = asyncio.Event()
    env = Env(tmp_path)

    async def probe_that_waits(name: str) -> None:
        env.probes.append(name)
        await gate.wait()

    env.coordinator._probe_context = probe_that_waits
    env.coordinator.switch("ctx-b")
    await asyncio.sleep(0)
    env.coordinator.switch("ctx-b")
    await asyncio.sleep(0)
    assert env.probes == ["ctx-b"]
    assert any("already in progress" in message for message in env.ui.messages())
    gate.set()
    await env.ui.settle()


async def test_the_claim_is_released_when_the_switch_fails(tmp_path: Path) -> None:
    env = Env(tmp_path, probe_error=RuntimeError("nope"))
    await env.switch()
    assert env.coordinator.switching() is False


async def test_a_build_without_switch_collaborators_says_so(tmp_path: Path) -> None:
    env = Env(tmp_path, wire_collaborators=False)
    await env.switch()
    assert env.ui.messages() == ["Context switching unavailable in this build"]


# ---------------------------------------------------------------------------
# Read allowance
# ---------------------------------------------------------------------------


async def test_reads_are_refused_with_a_notice_while_switching(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert env.coordinator.reads_allowed() is True
    env.coordinator._switching = True
    assert env.coordinator.reads_allowed() is False
    assert any("A context switch is in progress" in message for message in env.ui.messages())


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


async def test_the_mcp_server_drains_before_teardown_and_restarts_after_the_swap(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    env.mcp = FakeMCP(env.log)
    await env.switch()
    assert env.log.index("mcp-shutdown") < env.log.index("logs-close")
    assert env.log.index("swap:ctx-b") < env.log.index("mcp-start")
    assert any("MCP on :4321" in message for message in env.ui.messages())


async def test_a_failed_mcp_restart_is_reported_as_an_error(tmp_path: Path) -> None:
    env = Env(tmp_path)
    mcp = FakeMCP(env.log)
    mcp.start_result = "ERROR: port busy"
    env.mcp = mcp
    await env.switch()
    assert ("ERROR: port busy", "error") in env.ui.notifications


async def test_an_undrainable_mcp_server_aborts_the_switch_before_teardown(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    env.mcp = FakeMCP(env.log, drainable=False)
    await env.switch()
    assert env.swaps == []
    assert not env.log.has("logs-close")
    assert env.proposals.reasons == []
    assert env.timeline.phases[-1] == (0, "failed", "embedded MCP server did not stop in time")
    assert any("context switch aborted" in message for message in env.ui.messages())


async def test_a_stopped_mcp_server_is_reported_when_the_session_cannot_reconnect(
    tmp_path: Path,
) -> None:
    env = Env(
        tmp_path,
        switch_error=RuntimeError("target unreachable"),
        restore_error=RuntimeError("old cluster gone too"),
    )
    env.mcp = FakeMCP(env.log)
    await env.switch()
    assert any("restart it with :mcp on" in message for message in env.ui.messages())


async def test_a_server_that_was_not_running_is_not_restarted(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.mcp = FakeMCP(env.log, live=False)
    await env.switch()
    assert not env.log.has("mcp-start")


# ---------------------------------------------------------------------------
# Teardown / apply details
# ---------------------------------------------------------------------------


async def test_pending_proposals_expire_as_soon_as_the_transition_commits(
    tmp_path: Path,
) -> None:
    """Both the teardown and the retarget can raise; the sweep must not sit
    behind either of them."""
    env = Env(tmp_path)
    await env.switch()
    assert env.proposals.reasons == ["kube context switched"]
    assert env.log.index("proposals-expired") < env.log.index("logs-close")


async def test_the_hint_worker_group_is_cancelled_before_the_hint_cache_is_cleared(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    await env.switch()
    assert env.log.index(f"workers-cancelled:{HINT_EVENTS_GROUP}") < env.log.index("hints-teardown")


async def test_old_cluster_namespace_completions_are_dropped_then_reprimed(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    await env.switch()
    assert env.surface.namespace_words == []
    assert env.log.index("ns-prefetch-cancelled") < env.log.index("ns-words-cleared")
    assert env.log.index("ns-words-cleared") < env.log.index("ns-prefetch-started")


async def test_stopped_forwards_are_reported_to_the_operator(tmp_path: Path) -> None:
    env = Env(tmp_path, stopped_forwards=2)
    await env.switch()
    assert any(
        "Stopped 2 port-forward(s) targeting the old cluster" in message
        for message in env.ui.messages()
    )


async def test_forward_audits_flush_before_the_audit_log_is_repointed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.switch()
    assert env.log.index("forwards-flush") < env.log.index("audit-context:ctx-b")


async def test_a_protected_target_is_adopted_and_announced(tmp_path: Path) -> None:
    env = Env(
        tmp_path,
        result=ContextSwitchResult(
            pod_resize_supported=False,
            provider_hint=None,
            context_namespace="ns-b",
            protected_context="ctx-b",
        ),
    )
    await env.switch()
    assert env.writes.protected == ["ctx-b"]
    assert any("is protected" in message for message in env.ui.messages())


async def test_the_agent_is_told_about_the_switch_once(tmp_path: Path) -> None:
    env = Env(tmp_path)
    await env.switch()
    assert env.agent.notes == [
        "kube context switched from ctx-a to ctx-b; all cluster state was reset"
    ]


async def test_restoring_the_same_context_does_not_note_a_switch(tmp_path: Path) -> None:
    env = Env(tmp_path, switch_error=RuntimeError("target unreachable"))
    await env.switch()
    assert env.agent.notes == []


async def test_the_warning_feed_restarts_even_when_the_target_swap_failed(
    tmp_path: Path,
) -> None:
    """Teardown cancelled the old epoch's feed; whichever context is applied
    deserves one."""
    env = Env(tmp_path, switch_error=RuntimeError("target unreachable"))
    await env.switch()
    assert env.log.has("timeline-feed-start")
    assert not any(phase == "completed" for _epoch, phase, _note in env.timeline.phases)


async def test_a_recovered_session_still_restarts_watches_and_metrics(tmp_path: Path) -> None:
    env = Env(tmp_path, switch_error=RuntimeError("target unreachable"))
    await env.switch()
    assert env.log.has("watch-start:pods/ns-b")
    assert env.log.has("metrics-sync")
    assert env.log.has("status-refreshed")


async def test_a_session_that_cannot_reconnect_stops_before_restarting_anything(
    tmp_path: Path,
) -> None:
    env = Env(
        tmp_path,
        switch_error=RuntimeError("target unreachable"),
        restore_error=RuntimeError("old cluster gone too"),
    )
    await env.switch()
    assert not env.log.has("timeline-feed-start")
    assert not any(event.startswith("watch-start") for event in env.log.events)
    assert not env.log.has("metrics-sync")


async def test_no_forward_registry_skips_the_forward_retarget(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.registry = None
    await env.switch()
    assert not env.log.has("forwards-teardown")
    assert not env.log.has("forwards-reopen")
    assert env.log.has("forwards-flush")


# ---------------------------------------------------------------------------
# Picker and prefetch
# ---------------------------------------------------------------------------


async def test_the_picker_marks_the_current_context_and_maps_labels_back(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    env.coordinator.show_picker()
    await env.ui.settle()
    screen, callback = env.ui.screens[-1]
    assert isinstance(screen, PickScreen)
    labels = list(screen._options)
    assert "ctx-a (current)" in labels
    assert callback is not None
    callback("ctx-a (current)")
    assert env.surface.requested == ["ctx-a"]


async def test_the_picker_seeds_the_command_bar_completions(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.coordinator.show_picker()
    await env.ui.settle()
    assert env.surface.context_words == ["ctx-a", "ctx-b"]


async def test_an_empty_kubeconfig_opens_no_picker(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.contexts = []
    env.coordinator.show_picker()
    await env.ui.settle()
    assert env.ui.screens == []
    assert any("No contexts found" in message for message in env.ui.messages())


async def test_the_completion_prefetch_is_cancelled_and_reaped_on_shutdown(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    env.coordinator.start()
    task = env.coordinator._prefetch_task
    assert task is not None
    await env.coordinator.shutdown()
    assert task.done()
    assert env.coordinator._prefetch_task is None


async def test_shutdown_without_a_prefetch_is_a_no_op(tmp_path: Path) -> None:
    env = Env(tmp_path, wire_collaborators=False)
    env.coordinator.start()
    assert env.coordinator._prefetch_task is None
    await env.coordinator.shutdown()
    assert env.coordinator._prefetch_task is None


async def test_the_prefetch_fills_the_context_completions(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.coordinator.start()
    task = env.coordinator._prefetch_task
    assert task is not None
    await task
    assert env.surface.context_words == ["ctx-a", "ctx-b"]
    await env.coordinator.shutdown()


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


async def test_the_transaction_holds_the_navigation_lock(tmp_path: Path) -> None:
    """`:mcp` toggles, writes and navigation all serialize on this lock; the
    quiesce/retarget/resume window must be inside it."""
    env = Env(tmp_path)
    held: list[bool] = []

    async def observing_quiesce() -> None:
        held.append(env.workspace.nav_lock.locked())

    env.workspace.quiesce_for_context_switch = observing_quiesce  # type: ignore[method-assign]
    await env.switch()
    assert held == [True]
    assert env.workspace.nav_lock.locked() is False


async def test_a_teardown_failure_leaves_the_claim_released(tmp_path: Path) -> None:
    """A raising participant must not wedge `:ctx` for the rest of the
    session: the claim is released even though the transaction aborted."""
    env = Env(tmp_path)

    async def exploding_quiesce() -> None:
        raise RuntimeError("teardown blew up")

    env.workspace.quiesce_for_context_switch = exploding_quiesce  # type: ignore[method-assign]
    env.coordinator.switch("ctx-b")
    with pytest.raises(RuntimeError, match="teardown blew up"):
        await env.ui.settle()
    assert env.coordinator.switching() is False
    assert env.coordinator.epoch() == 0
    assert env.proposals.reasons == ["kube context switched"]
