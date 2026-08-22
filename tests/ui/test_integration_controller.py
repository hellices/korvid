"""Direct tests for `IntegrationController` (issue #187 / Deep Task 9).

The controller owns the two optional integrations the command bar reaches:

- `:mcp` — the server's live on/off toggle with its proposal sweeps, the
  `follow` mirror flag, and the follow-up sweep after a teardown that
  outran its bounded stop;
- `:tp` — the read-only telepresence status panel and the one-shot
  traffic-manager install hint with its epoch-bound, re-runnable probe.

Both are state the app used to hold. None of it needs a running app: the
Textual surface, the `:ctx` guard, the proposal store and the navigation
lock all arrive as injected interfaces.
"""

from __future__ import annotations

import asyncio
from typing import Any

from korvid.core.mcp import MCPControllerBase
from korvid.k8s.telepresence import (
    ActiveIntercept,
    TelepresenceError,
    TelepresenceStatus,
)
from korvid.ui.integration_controller import IntegrationController
from korvid.ui.widgets.telepresence_screen import TelepresenceScreen

from .test_write_coordinator import FakeContext, FakeUi


class FakeMCP(MCPControllerBase):
    """Lifecycle double that records the order of its transitions."""

    def __init__(self, *, running: bool = False, lingering: bool = False) -> None:
        self.is_on = running
        self.calls: list[str] = []
        self.lingering = lingering
        self.task: asyncio.Task[None] | None = None
        self.start_message = "MCP on"

    @property
    def running(self) -> bool:
        return self.is_on

    def status(self) -> str:
        return "MCP on" if self.is_on else "MCP off"

    async def start(self) -> str:
        self.calls.append("start")
        if self.is_on:
            return self.status()
        self.is_on = True
        return self.start_message

    async def stop(self) -> str:
        self.calls.append("stop")
        if self.lingering:
            # A bounded stop that timed out: the run is still dying.
            return "MCP stopping"
        self.is_on = False
        return "MCP off"

    async def shutdown(self) -> None:
        self.is_on = False

    def pending_task(self) -> asyncio.Task[None] | None:
        return self.task


class FakeTelepresence:
    """CLI double: canned status/intercepts, call recording."""

    def __init__(
        self,
        status: TelepresenceStatus | None = None,
        intercepts: list[ActiveIntercept] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.status_result = status or TelepresenceStatus(
            connected=False, user_running=False, root_running=False
        )
        self.intercepts_result = intercepts or []
        self.error = error
        self.calls: list[str] = []

    async def status(self) -> TelepresenceStatus:
        self.calls.append("status")
        if self.error is not None:
            raise self.error
        return self.status_result

    async def list_intercepts(self, daemon: str | None = None) -> list[ActiveIntercept]:
        self.calls.append("list")
        return self.intercepts_result


class FakeProposals:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def expire_all(self, reason: str) -> None:
        self.reasons.append(reason)


class FakeSerializer:
    """Holds the same `:ctx` navigation lock the switch transaction takes."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def nav_lock(self) -> asyncio.Lock:
        return self._lock


class Harness:
    def __init__(
        self,
        *,
        mcp: FakeMCP | None = None,
        telepresence: FakeTelepresence | None = None,
        probe: Any = None,
        telepresence_enabled: bool = True,
        follow: bool = False,
    ) -> None:
        self.ui = FakeUi()
        self.context = FakeContext()
        self.proposals = FakeProposals()
        self.serializer = FakeSerializer()
        self.mcp = mcp
        self.telepresence = telepresence
        self.status_refreshes = 0
        self.controller = IntegrationController(
            ui=self.ui,
            context=self.context,
            proposals=self.proposals,
            serializer=self.serializer,
            mcp=lambda: mcp,
            telepresence=telepresence,  # type: ignore[arg-type]  # duck-typed fake
            probe_traffic_manager=probe,
            telepresence_enabled=lambda: telepresence_enabled,
            follow_enabled=follow,
            refresh_status=self._refresh_status,
        )

    def _refresh_status(self) -> None:
        self.status_refreshes += 1


# ---------------------------------------------------------------------------
# :mcp follow
# ---------------------------------------------------------------------------


async def test_follow_is_seeded_from_the_session_setting() -> None:
    assert Harness(follow=True).controller.follow_enabled is True
    assert Harness(follow=False).controller.follow_enabled is False


async def test_follow_on_and_off_toggle_and_report() -> None:
    h = Harness(mcp=FakeMCP(running=True))
    h.controller.handle_mcp_command(["follow", "on"])
    assert h.controller.follow_enabled is True
    assert any("mirrored on screen" in message for message in h.ui.messages())
    h.controller.handle_mcp_command(["follow", "off"])
    assert h.controller.follow_enabled is False
    assert h.status_refreshes == 2


async def test_bare_follow_flips_the_flag() -> None:
    h = Harness(mcp=FakeMCP(running=True))
    h.controller.handle_mcp_command(["follow"])
    assert h.controller.follow_enabled is True


async def test_follow_rejects_an_unknown_argument() -> None:
    h = Harness(mcp=FakeMCP(running=True))
    h.controller.handle_mcp_command(["follow", "sideways"])
    assert h.controller.follow_enabled is False
    assert "Usage: :mcp follow [on|off]" in h.ui.messages()


async def test_mcp_state_commands_reject_trailing_arguments() -> None:
    mcp = FakeMCP(running=True)
    h = Harness(mcp=mcp)
    h.controller.handle_mcp_command(["off", "extra"])
    h.controller.handle_mcp_command(["follow", "on", "extra"])
    await h.ui.settle()
    assert mcp.running is True
    assert h.controller.follow_enabled is False
    assert h.proposals.reasons == []
    assert sum("Usage: :mcp" in message for message in h.ui.messages()) == 2


async def test_activity_notes_never_raise_into_the_caller() -> None:
    h = Harness()

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("toast failed")

    h.ui.notify = _boom  # type: ignore[method-assign]
    h.controller.note_activity("copilot: get_logs api-1")  # must not raise


# ---------------------------------------------------------------------------
# :mcp on/off
# ---------------------------------------------------------------------------


async def test_mcp_without_the_extra_explains_the_absence() -> None:
    h = Harness(mcp=None)
    h.controller.handle_mcp_command([])
    assert any("MCP unavailable" in message for message in h.ui.messages())


async def test_bare_mcp_reports_status_and_follow() -> None:
    h = Harness(mcp=FakeMCP(running=True))
    h.controller.handle_mcp_command([])
    assert "MCP on · follow off" in h.ui.messages()


async def test_mcp_rejects_an_unknown_action() -> None:
    h = Harness(mcp=FakeMCP(running=True))
    h.controller.handle_mcp_command(["sideways"])
    assert "Usage: :mcp [on|off] | :mcp follow [on|off]" in h.ui.messages()


async def test_mcp_toggle_is_refused_during_a_context_switch() -> None:
    mcp = FakeMCP(running=False)
    h = Harness(mcp=mcp)
    h.context.is_switching = True
    h.controller.handle_mcp_command(["on"])
    await h.ui.settle()
    assert mcp.calls == []
    assert any("context switch is in progress" in message for message in h.ui.messages())


async def test_mcp_on_sweeps_stale_proposals_before_the_endpoint_goes_live() -> None:
    """A pending proposal predates the run about to start: its capability
    token came from an older, ended run, so it must be expired *before*
    start() returns and new callers can submit."""
    mcp = FakeMCP(running=False)
    h = Harness(mcp=mcp)
    h.controller.handle_mcp_command(["on"])
    await h.ui.settle()
    assert h.proposals.reasons == ["the MCP server was restarted"]
    assert mcp.calls == ["start"]


async def test_mcp_on_while_already_running_keeps_pending_work() -> None:
    mcp = FakeMCP(running=True)
    h = Harness(mcp=mcp)
    h.controller.handle_mcp_command(["on"])
    await h.ui.settle()
    assert h.proposals.reasons == []


async def test_mcp_off_expires_every_pending_proposal() -> None:
    mcp = FakeMCP(running=True)
    h = Harness(mcp=mcp)
    h.controller.handle_mcp_command(["off"])
    await h.ui.settle()
    assert h.proposals.reasons == ["the MCP server was stopped"]
    assert h.status_refreshes == 1


async def test_mcp_off_when_already_stopped_expires_nothing() -> None:
    mcp = FakeMCP(running=False)
    h = Harness(mcp=mcp)
    h.controller.handle_mcp_command(["off"])
    await h.ui.settle()
    assert h.proposals.reasons == []


async def test_a_toggle_that_lost_the_race_to_a_switch_is_refused() -> None:
    """The pre-check passed, but the switch claimed the transaction before
    the worker took the lock: the re-check inside it must refuse."""
    mcp = FakeMCP(running=False)
    h = Harness(mcp=mcp)
    async with h.serializer.nav_lock:
        h.controller.handle_mcp_command(["on"])
        await asyncio.sleep(0)
        h.context.is_switching = True
    await h.ui.settle()
    assert mcp.calls == []
    assert any("context switch is in progress" in message for message in h.ui.messages())


async def test_a_dragged_out_teardown_gets_a_follow_up_sweep() -> None:
    """`stop()`'s bounded wait can return while the old run is still dying;
    an in-flight proposal on that run may land after the stop-time sweep."""
    mcp = FakeMCP(running=True, lingering=True)
    done = asyncio.Event()

    async def _dying() -> None:
        await done.wait()
        mcp.is_on = False

    mcp.task = asyncio.ensure_future(_dying())
    h = Harness(mcp=mcp)
    h.controller.handle_mcp_command(["off"])
    await asyncio.sleep(0)
    done.set()
    await h.ui.settle()
    assert h.proposals.reasons == [
        "the MCP server was stopped",
        "the MCP server was stopped",
    ]


async def test_no_follow_up_sweep_when_a_fresh_run_came_up() -> None:
    """A restart that won the race owns the pending proposals: its own start
    transition already swept the old run's stragglers."""
    mcp = FakeMCP(running=True, lingering=True)
    done = asyncio.Event()

    async def _dying() -> None:
        await done.wait()

    mcp.task = asyncio.ensure_future(_dying())
    h = Harness(mcp=mcp)
    h.controller.handle_mcp_command(["off"])
    await asyncio.sleep(0)
    done.set()  # the old run ends, but a racing `:mcp on` left it running
    await h.ui.settle()
    assert h.proposals.reasons == ["the MCP server was stopped"]


# ---------------------------------------------------------------------------
# :tp
# ---------------------------------------------------------------------------


async def test_tp_without_the_binary_explains_the_absence() -> None:
    h = Harness(telepresence=None)
    h.controller.handle_telepresence_command()
    await h.ui.settle()
    assert h.ui.screens == []
    assert any("telepresence not available" in message for message in h.ui.messages())


async def test_tp_opens_the_status_panel() -> None:
    tp = FakeTelepresence()
    h = Harness(telepresence=tp)
    h.controller.handle_telepresence_command()
    await h.ui.settle()
    assert isinstance(h.ui.screens[-1][0], TelepresenceScreen)
    assert tp.calls == ["status"]  # not connected: intercepts never queried


async def test_tp_queries_intercepts_when_connected() -> None:
    tp = FakeTelepresence(
        status=TelepresenceStatus(connected=True, user_running=True, root_running=True),
        intercepts=[ActiveIntercept(workload="web", namespace="prod")],
    )
    h = Harness(telepresence=tp)
    h.controller.handle_telepresence_command()
    await h.ui.settle()
    assert tp.calls == ["status", "list"]


async def test_tp_cli_failure_is_a_literal_toast_not_a_panel() -> None:
    h = Harness(telepresence=FakeTelepresence(error=TelepresenceError("connector refused")))
    h.controller.handle_telepresence_command()
    await h.ui.settle()
    assert h.ui.screens == []
    assert any("connector refused" in message for message in h.ui.messages())


# ---------------------------------------------------------------------------
# traffic-manager install hint
# ---------------------------------------------------------------------------


async def _probe(result: bool) -> bool:
    return result


async def test_no_hint_when_the_client_is_installed() -> None:
    h = Harness(telepresence=FakeTelepresence(), probe=lambda: _probe(True))
    await h.controller.maybe_hint_telepresence()
    assert h.ui.messages() == []


async def test_no_hint_when_the_integration_is_switched_off() -> None:
    h = Harness(probe=lambda: _probe(True), telepresence_enabled=False)
    await h.controller.maybe_hint_telepresence()
    assert h.ui.messages() == []


async def test_no_hint_without_a_probe() -> None:
    h = Harness(probe=None)
    await h.controller.maybe_hint_telepresence()
    assert h.ui.messages() == []


async def test_hint_shows_once_per_session() -> None:
    h = Harness(probe=lambda: _probe(True))
    await h.controller.maybe_hint_telepresence()
    await h.controller.maybe_hint_telepresence()
    assert sum("traffic-manager detected" in m for m in h.ui.messages()) == 1


async def test_no_hint_without_a_traffic_manager_and_it_stays_re_runnable() -> None:
    h = Harness(probe=lambda: _probe(False))
    await h.controller.maybe_hint_telepresence()
    assert h.ui.messages() == []
    h.controller._probe_traffic_manager = lambda: _probe(True)  # a later `:ctx` target
    await h.controller.maybe_hint_telepresence()
    assert any("traffic-manager detected" in m for m in h.ui.messages())


async def test_a_failing_probe_means_no_hint() -> None:
    async def _boom() -> bool:
        raise RuntimeError("forbidden")

    h = Harness(probe=_boom)
    await h.controller.maybe_hint_telepresence()
    assert h.ui.messages() == []


async def test_an_answer_for_a_left_context_is_discarded() -> None:
    async def _crossing() -> bool:
        h.context.value += 1
        return True

    h = Harness(probe=lambda: _crossing())
    await h.controller.maybe_hint_telepresence()
    assert h.ui.messages() == []


async def test_a_reprobe_requested_mid_probe_is_queued_not_lost() -> None:
    gate = asyncio.Event()
    answers = [False, True]

    async def _gated() -> bool:
        await gate.wait()
        return answers.pop(0)

    h = Harness(probe=lambda: _gated())
    first = asyncio.ensure_future(h.controller.maybe_hint_telepresence())
    await asyncio.sleep(0)
    await h.controller.maybe_hint_telepresence()  # the `:ctx` re-probe request
    gate.set()
    await first
    await h.ui.settle()
    assert any("traffic-manager detected" in m for m in h.ui.messages())
