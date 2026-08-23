"""`NativeAgentEngine`: the in-tree loop's own guarantees (issue #316, Task 10).

`test_engine_contract` pins what any engine must do. This module pins what
*this* engine is built from, and the fail-closed behaviour only its
collaborators can show: it drives a `ConversationState`, a `RequestGateway`
and a `ToolHarness` and nothing else, so every request crosses the outbound
boundary and every tool call crosses the harness.

The cases here are the ones the v1 runtime's security suite proved and this
engine has to keep proving: a result the boundary will not vouch for rolls
its turn back without leaking the document or leaving an unanswered call; an
over-ceiling request recovers by shrinking history rather than by sending
less-checked content; an unarmed tool never reaches a port; a cancellation
leaves history valid and never re-runs a write.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from typing import Any

import pytest

from korvid.agent.conversation import INTERRUPT_MARKER
from korvid.agent.events import (
    AgentError,
    AgentEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
)
from korvid.agent.interaction import Navigate, UiAction, UiActionResult
from korvid.agent.native_engine import NativeAgentEngine
from korvid.agent.request_gateway import PreparedGatewayRequest, RequestGateway
from korvid.core.secrets import MASK_PLACEHOLDER
from korvid.tools.executor import ToolResultBlocked

from .engine_fakes import (
    DONE,
    Harness,
    RecordingBridge,
    RecordingExecution,
    build_harness,
    make_policy,
    text_delta,
    text_turn,
    tool_call,
    tool_results,
    tool_turn,
    usage,
)

LOGS_ARGS = '{"pod":"api-0","namespace":"prod"}'
DELETE_ARGS = '{"kind":"pod","name":"api-0","namespace":"prod"}'
SECRET_LOG = "starting up with password: hunter2-in-the-logs"
#: A document the structured pass cannot parse — and that quotes a secret,
#: so a refusal that echoed its input would leak one.
SECRET_BLOB = "an unstructured dump carrying hunter2-in-the-logs"


class CountingGateway(RequestGateway):
    """A real gateway that also counts how often it was closed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.closes = 0

    async def aclose(self) -> None:
        self.closes += 1
        await super().aclose()


def _errors(events: list[AgentEvent]) -> list[AgentError]:
    return [event for event in events if isinstance(event, AgentError)]


def _blocked_producer_harness() -> Harness:
    """A read whose *producer* refuses to vouch for its own result."""
    execution = RecordingExecution({"get_logs": ToolResultBlocked("secret could not be masked")})
    return build_harness(
        [[tool_call("c1", "get_logs", LOGS_ARGS), usage(10, 2), DONE], text_turn()],
        execution=execution,
    )


# -- construction ------------------------------------------------------------


def test_the_engine_is_built_from_exactly_three_collaborators() -> None:
    """No provider, bridge, kube client or write path is reachable from here."""
    parameters = inspect.signature(NativeAgentEngine.__init__).parameters
    assert set(parameters) - {"self"} == {"conversation", "gateway", "tools"}
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for name, parameter in parameters.items()
        if name != "self"
    )


# -- a result the boundary will not vouch for --------------------------------


async def test_a_result_the_producer_cannot_vouch_for_stops_the_turn() -> None:
    harness = _blocked_producer_harness()

    events = await harness.run()

    error = _errors(events)[-1]
    assert "the turn stopped before its next provider request" in error.message
    assert isinstance(events[-1], TurnComplete)
    assert len(harness.provider.calls) == 1


async def test_a_blocked_turn_leaves_no_unanswered_tool_call() -> None:
    harness = _blocked_producer_harness()

    events = await harness.run()

    assert not harness.conversation.has_unmatched_tool_calls
    assert harness.conversation.messages == []
    started = [event for event in events if isinstance(event, ToolCallStarted)]
    finished = [event for event in events if isinstance(event, ToolCallFinished)]
    assert [event.call_id for event in started] == ["c1"]
    assert [(event.call_id, event.ok) for event in finished] == [("c1", False)]


async def test_a_blocked_turn_keeps_the_cost_it_really_paid() -> None:
    harness = _blocked_producer_harness()

    events = await harness.run()

    assert events[-1] == TurnComplete(input_tokens=10, output_tokens=2, estimated=False)
    assert harness.conversation.total_tokens == (10, 2)


async def test_a_blocked_turn_keeps_the_last_real_handoff_on_display() -> None:
    harness = _blocked_producer_harness()

    await harness.run()

    snapshot = harness.gateway.latest_outbound_payload
    assert snapshot is not None
    assert snapshot.iteration == 1


async def test_the_session_still_works_after_a_blocked_turn() -> None:
    harness = _blocked_producer_harness()

    await harness.run("first")
    events = await harness.run("second")

    assert isinstance(events[-1], TurnComplete)
    assert [message["role"] for message in harness.provider.calls[-1]] == ["system", "user"]
    assert harness.provider.calls[-1][1]["content"] == "second"


async def test_a_result_the_boundary_cannot_sanitize_stops_the_turn() -> None:
    """A structured tool whose result is not a document is refused, not sent."""
    execution = RecordingExecution({"get_resource": "OOMKilled at 12:01"})
    harness = build_harness(
        [[tool_call("c1", "get_resource", '{"kind":"pod","name":"api-0"}'), DONE], text_turn()],
        policy=make_policy(tool_names=("get_resource",)),
        execution=execution,
    )

    events = await harness.run()

    error = _errors(events)[-1]
    assert "outbound policy blocked the provider request" in error.message
    assert len(harness.provider.calls) == 1
    assert harness.conversation.messages == []
    assert isinstance(events[-1], TurnComplete)


async def test_a_blocked_turn_never_shows_the_document_it_refused() -> None:
    execution = RecordingExecution({"get_resource": SECRET_BLOB})
    harness = build_harness(
        [[tool_call("c1", "get_resource", '{"kind":"secret","name":"api"}'), DONE], text_turn()],
        policy=make_policy(tool_names=("get_resource",)),
        execution=execution,
    )

    events = await harness.run()

    assert _errors(events)
    assert "hunter2" not in json.dumps([str(event) for event in events])
    assert "hunter2" not in json.dumps(harness.conversation.messages)


# -- history budget recovery -------------------------------------------------


async def test_an_over_ceiling_request_drops_the_oldest_turn_and_retries() -> None:
    """Recovery shrinks the conversation; it never sends less-checked content."""
    harness = build_harness(
        [text_turn(), text_turn("second"), text_turn("third")], max_request_chars=1_600
    )

    await harness.run("first question " + "x" * 400)
    await harness.run("second question " + "y" * 400)
    events = await harness.run("third question")

    assert isinstance(events[-1], TurnComplete)
    assert len(harness.provider.calls) == 3
    last = json.dumps(harness.provider.calls[-1])
    assert "first question" not in last
    assert "second question" in last
    assert "third question" in last


async def test_a_request_that_cannot_shrink_ends_the_turn_cleanly() -> None:
    harness = build_harness([text_turn()], max_request_chars=200)

    events = await harness.run("a question far too large to send " + "z" * 400)

    error = _errors(events)[-1]
    assert "outbound policy blocked the provider request" in error.message
    assert harness.provider.calls == []
    assert harness.conversation.messages == []
    assert isinstance(events[-1], TurnComplete)


async def test_a_prompt_that_cannot_fit_the_history_budget_is_refused() -> None:
    harness = build_harness(
        [text_turn()], policy=make_policy(max_history_chars=200, strict_history_budget=True)
    )

    events = await harness.run("q" * 400)

    error = _errors(events)[-1]
    assert "request too large for the history budget (200 chars)" in error.message
    assert harness.provider.calls == []
    assert harness.conversation.messages == []
    assert events[-1] == TurnComplete(input_tokens=0, output_tokens=0, estimated=False)


async def test_a_strict_turn_that_outgrows_its_budget_ends_early() -> None:
    execution = RecordingExecution({"get_logs": "L" * 2_000})
    harness = build_harness(
        [tool_turn(), tool_turn(call_id="c2"), text_turn()],
        policy=make_policy(max_history_chars=1_000, strict_history_budget=True),
        execution=execution,
    )

    events = await harness.run()

    error = _errors(events)[-1]
    assert "history budget exceeded mid-turn (1000 chars)" in error.message
    assert isinstance(events[-1], TurnComplete)
    assert len(harness.provider.calls) == 1


# -- the tool surface --------------------------------------------------------


async def test_an_unarmed_tool_never_reaches_a_port() -> None:
    execution = RecordingExecution()
    bridge = RecordingBridge()
    harness = build_harness(
        [[tool_call("c1", "delete_resource", DELETE_ARGS), DONE], text_turn()],
        execution=execution,
        bridge=bridge,
    )

    events = await harness.run()

    assert execution.calls == []
    assert bridge.actions == []
    finished = next(event for event in events if isinstance(event, ToolCallFinished))
    assert finished.ok is False
    assert "not armed" in finished.summary
    assert "delete_resource" not in json.dumps(harness.provider.tool_surfaces[0])


async def test_a_screen_tool_goes_to_the_bridge_and_not_to_the_cluster() -> None:
    execution = RecordingExecution()
    bridge = RecordingBridge()
    harness = build_harness(
        [[tool_call("c1", "navigate", '{"view":"pods","namespace":"prod"}'), DONE], text_turn()],
        policy=make_policy(tool_names=("navigate",)),
        execution=execution,
        bridge=bridge,
    )

    events = await harness.run()

    assert bridge.actions == [Navigate(view="pods", namespace="prod")]
    assert execution.calls == []
    assert isinstance(events[-1], TurnComplete)


async def test_every_request_re_sanitizes_the_results_it_carries() -> None:
    """History is re-checked on the way out, not trusted because it is stored."""
    execution = RecordingExecution({"get_logs": SECRET_LOG})
    harness = build_harness([tool_turn(), text_turn()], execution=execution)

    await harness.run()

    result = tool_results(harness.provider.calls[1])[0]
    assert "hunter2-in-the-logs" not in str(result["content"])
    assert MASK_PLACEHOLDER in str(result["content"])


async def test_the_evidence_table_only_names_reads_that_happened() -> None:
    harness = build_harness([tool_turn(), text_turn()])

    await harness.run()

    note = str(harness.provider.calls[1][0]["content"])
    assert "[E1] get_logs" in note
    assert "[E2]" not in note
    assert harness.tools.evidence.references() == ("E1",)


# -- failures nothing expected -----------------------------------------------


class ExplodingGateway(RequestGateway):
    """A real gateway whose first preparation fails in a way nothing expects."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.armed = True

    def prepare(self, *args: Any, **kwargs: Any) -> PreparedGatewayRequest:
        if self.armed:
            self.armed = False
            raise RuntimeError("gateway exploded")
        return super().prepare(*args, **kwargs)


class ExplodingBridge(RecordingBridge):
    """A UI bridge that fails the way a torn-down screen would."""

    async def apply(self, action: UiAction) -> UiActionResult:
        raise RuntimeError("bridge exploded")


async def test_an_unexpected_gateway_failure_leaves_the_turn_recoverable() -> None:
    harness = build_harness([text_turn("recovered")], gateway_class=ExplodingGateway)

    with pytest.raises(RuntimeError, match="gateway exploded"):
        await harness.run("first")
    events = await harness.run("second")

    assert isinstance(events[-1], TurnComplete)
    # The turn that exploded never reached the provider, and left nothing.
    assert len(harness.provider.calls) == 1
    assert [message["content"] for message in harness.conversation.messages] == [
        "second",
        "recovered",
    ]


async def test_an_unexpected_bridge_failure_leaves_the_turn_recoverable() -> None:
    bridge = ExplodingBridge()
    harness = build_harness(
        [
            [tool_call("c1", "navigate", '{"view":"pods","namespace":"prod"}'), DONE],
            text_turn("recovered"),
        ],
        policy=make_policy(tool_names=("navigate",)),
        bridge=bridge,
    )

    with pytest.raises(RuntimeError, match="bridge exploded"):
        await harness.run("first")

    assert not harness.conversation.has_unmatched_tool_calls
    assert harness.conversation.messages == []
    assert isinstance((await harness.run("second"))[-1], TurnComplete)


# -- cancellation ------------------------------------------------------------


async def _cancel_at_stall(harness: Harness) -> None:
    """Drive one turn until the provider stalls, then cancel its task."""

    async def drive() -> list[AgentEvent]:
        return [event async for event in harness.engine.run(harness.request())]

    task = asyncio.create_task(drive())
    await asyncio.wait_for(harness.provider.stalled.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_a_cancelled_turn_is_left_active_for_the_session_to_finalize() -> None:
    """Cancellation is not an unexpected error: nothing unwinds it here."""
    stall = asyncio.Event()
    harness = build_harness([[text_delta("thinking about"), stall]])

    await _cancel_at_stall(harness)

    interrupted = harness.conversation.finalize_interrupt()
    assert interrupted.input_tokens > 0
    note = str(harness.conversation.messages[-1]["content"])
    assert note.startswith("thinking about")
    assert note.endswith(INTERRUPT_MARKER)


async def test_a_tool_only_stream_that_is_cancelled_is_still_charged() -> None:
    """The call it generated is output, even though no text ever streamed."""
    stall = asyncio.Event()
    execution = RecordingExecution()
    harness = build_harness([[tool_call("c1", "get_logs", LOGS_ARGS), stall]], execution=execution)

    await _cancel_at_stall(harness)
    interrupted = harness.conversation.finalize_interrupt()

    assert execution.calls == []
    assert harness.conversation.messages[-1]["content"] == INTERRUPT_MARKER
    assert interrupted.output_tokens == (len("get_logs") + len(LOGS_ARGS)) // 4
    snapshot = harness.gateway.latest_outbound_payload
    assert snapshot is not None
    assert interrupted.input_tokens == len(snapshot.payload_json) // 4
    assert interrupted.estimated is True


async def test_a_partial_answer_is_kept_marked_not_replayed_as_finished() -> None:
    stall = asyncio.Event()
    harness = build_harness([[text_delta("the pod is"), stall]])

    await _cancel_at_stall(harness)
    harness.conversation.finalize_interrupt()

    note = str(harness.conversation.messages[-1]["content"])
    assert note.startswith("the pod is")
    assert note.endswith(INTERRUPT_MARKER)
    assert not harness.conversation.has_unmatched_tool_calls


async def test_a_write_cancelled_mid_flight_is_never_re_executed() -> None:
    gate = asyncio.Event()
    execution = RecordingExecution({"delete_resource": "deletion approved"})
    execution.gate = gate
    harness = build_harness(
        [[tool_call("c1", "delete_resource", DELETE_ARGS), DONE], text_turn("nothing further")],
        policy=make_policy(tool_names=("delete_resource",)),
        execution=execution,
    )

    async def drive() -> list[AgentEvent]:
        return [event async for event in harness.engine.run(harness.request())]

    task = asyncio.create_task(drive())
    await asyncio.wait_for(execution.entered.wait(), timeout=5)
    task.cancel()
    gate.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    harness.conversation.finalize_interrupt()

    events = await harness.run("what happened?")

    assert execution.names == ["delete_resource"]
    assert not harness.conversation.has_unmatched_tool_calls
    assert isinstance(events[-1], TurnComplete)
    assert "delete_resource" not in json.dumps(harness.provider.calls[-1])


# -- close -------------------------------------------------------------------


async def test_closing_the_engine_closes_the_gateway_once() -> None:
    harness = build_harness([text_turn()], gateway_class=CountingGateway)
    gateway = harness.gateway
    assert isinstance(gateway, CountingGateway)

    events = await harness.run()
    await harness.engine.aclose()
    await harness.engine.aclose()

    assert isinstance(events[0], TextDelta)
    assert gateway.closes == 1


async def test_a_closed_engine_refuses_to_start_another_turn() -> None:
    harness = build_harness([text_turn(), text_turn("second")])

    await harness.engine.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        harness.engine.run(harness.request())
    assert harness.provider.calls == []
