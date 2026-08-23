"""Shared `AgentEngine` contract (issue #316, Task 10).

Every engine implementation drives the same loop: one composed prompt in,
typed `AgentEvent` values out, with the durable conversation, the outbound
gateway and the tool harness as its only collaborators. These cases are
written against that boundary alone — they build an engine through the
`engine_factory` fixture and then assert on emitted events, on the payloads
the provider really received, and on retained history, never on engine
internals — so a second implementation only has to supply a factory.

The behaviour pinned here is the behaviour `AgentRuntime` shipped: text
streaming, one and several tool calls, malformed / duplicate / excess calls
that must never reach a port, exact usage accounting for a provider that
reports it and honest estimates for one that does not, the exact outbound
snapshot, provider failure before and after handoff, interruption, and
orderly close.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

import pytest

from korvid.agent.conversation import INTERRUPT_MARKER
from korvid.agent.events import (
    AgentError,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
    TurnInterrupted,
)
from korvid.tools.executor import ToolOutcome

from .engine_fakes import (
    DONE,
    SYSTEM_PROMPT,
    Harness,
    RecordingExecution,
    ScriptedProvider,
    assistant_tool_calls,
    build_harness,
    make_policy,
    roles,
    system_message,
    text_delta,
    text_turn,
    tool_call,
    tool_results,
    tool_turn,
    usage,
)

EngineFactory = Callable[..., Harness]

LOGS_ARGS = '{"pod":"api-0","namespace":"prod"}'


@pytest.fixture
def engine_factory() -> EngineFactory:
    """Build one engine, wired to inspectable collaborators, per case.

    The suite only ever touches `AgentEngine` and the collaborators the
    engine was constructed with, so pointing this fixture at another
    implementation runs the same contract against it.
    """
    return build_harness


# -- a plain answer ----------------------------------------------------------


async def test_a_text_only_turn_streams_its_answer_and_completes(
    engine_factory: EngineFactory,
) -> None:
    harness = engine_factory([[text_delta("the pod is healthy"), usage(11, 3), DONE]])

    events = await harness.run()

    assert events == [
        TextDelta(text="the pod is healthy"),
        TurnComplete(input_tokens=11, output_tokens=3, estimated=False),
    ]


async def test_the_turn_starts_from_the_composed_prompt(engine_factory: EngineFactory) -> None:
    """The engine composes nothing: the user message is the prompt's own."""
    harness = engine_factory([text_turn()])

    await harness.run("why is the api pod failing?")

    call = harness.provider.calls[0]
    assert roles(call) == ["system", "user"]
    assert system_message(call) == SYSTEM_PROMPT
    assert call[1]["content"] == "why is the api pod failing?"


async def test_the_system_prompt_is_ephemeral_and_never_retained(
    engine_factory: EngineFactory,
) -> None:
    harness = engine_factory([text_turn(), text_turn("still fine")])

    await harness.run("first")
    await harness.run("second", system_message="a different contract")

    assert all(message["role"] != "system" for message in harness.conversation.messages)
    assert system_message(harness.provider.calls[1]) == "a different contract"


async def test_a_provider_that_streams_nothing_is_charged_nothing(
    engine_factory: EngineFactory,
) -> None:
    """No acknowledgement and no event: there is no evidence a request ran."""
    harness = engine_factory(provider=ScriptedProvider([[]], acknowledge=False))

    events = await harness.run()

    assert events == [TurnComplete(input_tokens=0, output_tokens=0, estimated=False)]
    assert harness.gateway.latest_outbound_payload is None
    assert harness.conversation.total_tokens == (0, 0)


# -- tool calls --------------------------------------------------------------


async def test_one_tool_call_runs_and_its_result_goes_back_to_the_model(
    engine_factory: EngineFactory,
) -> None:
    execution = RecordingExecution({"get_logs": "OOMKilled at 12:01"})
    harness = engine_factory([tool_turn(), text_turn()], execution=execution)

    events = await harness.run()

    assert [type(event) for event in events] == [
        ToolCallStarted,
        ToolCallFinished,
        TextDelta,
        TurnComplete,
    ]
    assert execution.names == ["get_logs"]
    assert execution.calls[0][1] == {"pod": "api-0", "namespace": "prod"}
    second = harness.provider.calls[1]
    assert [call["id"] for call in assistant_tool_calls(second)] == ["c1"]
    assert tool_results(second) == [
        {"role": "tool", "tool_call_id": "c1", "content": "OOMKilled at 12:01"}
    ]


async def test_several_tool_calls_run_one_at_a_time_in_the_order_given(
    engine_factory: EngineFactory,
) -> None:
    """A multi-call response is permitted, but never runs concurrently."""
    execution = RecordingExecution()
    harness = engine_factory(
        [
            [
                tool_call("c1", "get_logs", LOGS_ARGS),
                tool_call("c2", "get_events", '{"kind":"pods","name":"api-0"}'),
                DONE,
            ],
            text_turn(),
        ],
        policy=make_policy(tool_names=("get_logs", "get_events")),
        execution=execution,
    )

    events = await harness.run()

    assert execution.names == ["get_logs", "get_events"]
    assert execution.max_concurrent == 1
    started = [event.call_id for event in events if isinstance(event, ToolCallStarted)]
    assert started == ["c1", "c2"]
    second = harness.provider.calls[1]
    assert [call["id"] for call in assistant_tool_calls(second)] == ["c1", "c2"]
    assert [result["tool_call_id"] for result in tool_results(second)] == ["c1", "c2"]


async def test_the_producer_verdict_decides_whether_a_call_failed(
    engine_factory: EngineFactory,
) -> None:
    """A result that merely quotes `ERROR:` is a result; the producer says."""
    execution = RecordingExecution(
        {"get_logs": ToolOutcome(text="ERROR: connection refused (from the log)", error=False)}
    )
    harness = engine_factory([tool_turn(), text_turn()], execution=execution)

    events = await harness.run()

    finished = next(event for event in events if isinstance(event, ToolCallFinished))
    assert finished.ok is True


async def test_a_declared_failure_is_reported_and_the_turn_continues(
    engine_factory: EngineFactory,
) -> None:
    execution = RecordingExecution(
        {"get_logs": ToolOutcome(text="ERROR: pod not found", error=True)}
    )
    harness = engine_factory([tool_turn(), text_turn()], execution=execution)

    events = await harness.run()

    finished = next(event for event in events if isinstance(event, ToolCallFinished))
    assert finished.ok is False
    assert len(harness.provider.calls) == 2
    assert isinstance(events[-1], TurnComplete)


# -- calls that must never reach a port --------------------------------------


@pytest.mark.parametrize(
    "arguments",
    ["not json at all", "[1, 2]", '"just text"', "{"],
    ids=["invalid", "list", "string", "truncated"],
)
async def test_malformed_arguments_never_reach_a_port(
    engine_factory: EngineFactory, arguments: str
) -> None:
    execution = RecordingExecution()
    harness = engine_factory(
        [[tool_call("c1", "get_logs", arguments), DONE], text_turn()], execution=execution
    )

    events = await harness.run()

    assert execution.calls == []
    finished = next(event for event in events if isinstance(event, ToolCallFinished))
    assert finished.ok is False
    assert finished.summary.startswith("ERROR:")
    # The model is told, so it can correct itself on the next round.
    assert tool_results(harness.provider.calls[1])[0]["tool_call_id"] == "c1"


async def test_a_duplicate_call_id_is_discarded_before_dispatch(
    engine_factory: EngineFactory,
) -> None:
    """Two results for one id cannot pair, so the repeat never runs."""
    execution = RecordingExecution()
    harness = engine_factory(
        [
            [tool_call("c1", "get_logs", LOGS_ARGS), tool_call("c1", "get_logs", LOGS_ARGS), DONE],
            text_turn(),
        ],
        execution=execution,
    )

    events = await harness.run()

    assert execution.names == ["get_logs"]
    second = harness.provider.calls[1]
    assert len(assistant_tool_calls(second)) == 1
    assert len(tool_results(second)) == 1
    discarded = [event for event in events if isinstance(event, ToolCallFinished) and not event.ok]
    assert [event.summary for event in discarded] == ["discarded: duplicate tool call id"]


@pytest.mark.parametrize(
    ("call_id", "name"),
    [("", "get_logs"), ("c1", ""), ("   ", "get_logs")],
    ids=["no-id", "no-name", "blank-id"],
)
async def test_a_call_the_protocol_cannot_pair_ends_the_turn_without_dispatch(
    engine_factory: EngineFactory, call_id: str, name: str
) -> None:
    execution = RecordingExecution()
    harness = engine_factory(
        [[tool_call(call_id, name, LOGS_ARGS), DONE], text_turn()], execution=execution
    )

    events = await harness.run()

    assert execution.calls == []
    assert len(harness.provider.calls) == 1
    assert any(isinstance(event, AgentError) for event in events)
    assert isinstance(events[-1], TurnComplete)
    assert not harness.conversation.has_unmatched_tool_calls
    assert not any(message.get("tool_calls") for message in harness.conversation.messages)
    assert all(message.get("role") != "tool" for message in harness.conversation.messages)


async def test_excess_calls_are_discarded_and_the_notice_rides_the_last_kept_result(
    engine_factory: EngineFactory,
) -> None:
    execution = RecordingExecution()
    harness = engine_factory(
        [
            [
                tool_call("c1", "get_logs", LOGS_ARGS),
                tool_call("c2", "get_events", '{"kind":"pods","name":"api-0"}'),
                DONE,
            ],
            text_turn(),
        ],
        policy=make_policy(tool_names=("get_logs", "get_events"), max_tool_calls=1),
        execution=execution,
    )

    events = await harness.run()

    assert execution.names == ["get_logs"]
    second = harness.provider.calls[1]
    assert [call["id"] for call in assistant_tool_calls(second)] == ["c1"]
    results = tool_results(second)
    assert len(results) == 1
    assert "one tool at a time" in str(results[0]["content"])
    # The discarded call's arguments never entered durable history.
    assert "get_events" not in json.dumps(second)
    last = [event for event in events if isinstance(event, ToolCallFinished)][-1]
    assert last.call_id == "c2"
    assert last.ok is False
    assert last.summary == "discarded: too many tool calls in one response"


# -- iteration and citation --------------------------------------------------


async def test_the_iteration_limit_ends_the_turn_with_a_visible_error(
    engine_factory: EngineFactory,
) -> None:
    harness = engine_factory(
        [tool_turn(call_id="c1"), tool_turn(call_id="c2"), tool_turn(call_id="c3")],
        policy=make_policy(max_iterations=2),
    )

    events = await harness.run()

    error = next(event for event in events if isinstance(event, AgentError))
    assert "iteration limit reached (2)" in error.message
    assert isinstance(events[-1], TurnComplete)
    assert len(harness.provider.calls) == 2


async def test_every_round_restates_the_evidence_the_ledger_really_minted(
    engine_factory: EngineFactory,
) -> None:
    harness = engine_factory([tool_turn(), text_turn()])

    await harness.run()

    first, second = harness.provider.calls
    assert "[E1]" not in system_message(first)
    assert "[E1]" in system_message(second)


async def test_a_new_turn_never_offers_the_previous_turns_references(
    engine_factory: EngineFactory,
) -> None:
    harness = engine_factory([tool_turn(), text_turn(), text_turn("still fine")])

    await harness.run("first")
    await harness.run("second")

    assert "[E1]" not in system_message(harness.provider.calls[2])


async def test_the_final_answer_reports_its_citations(engine_factory: EngineFactory) -> None:
    harness = engine_factory(
        [tool_turn(), [text_delta("logs show OOM [E1]; see [E1] and [E4]"), DONE]]
    )

    events = await harness.run()

    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    assert complete.cited == ("E1",)
    assert complete.duplicated == ("E1",)
    assert complete.uncited == ("E4",)


# -- usage and the exact payload --------------------------------------------


async def test_reported_usage_is_summed_exactly_across_iterations(
    engine_factory: EngineFactory,
) -> None:
    harness = engine_factory(
        [
            [tool_call("c1", "get_logs", LOGS_ARGS), usage(10, 2), DONE],
            [text_delta("done"), usage(20, 5), DONE],
        ]
    )

    events = await harness.run()

    assert events[-1] == TurnComplete(input_tokens=30, output_tokens=7, estimated=False)


async def test_a_turn_without_reported_usage_is_estimated_from_the_exact_payload(
    engine_factory: EngineFactory,
) -> None:
    harness = engine_factory([[text_delta("hello there"), DONE]])

    events = await harness.run()

    snapshot = harness.gateway.latest_outbound_payload
    assert snapshot is not None
    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    assert complete.estimated is True
    assert complete.input_tokens == len(snapshot.payload_json) // 4
    assert complete.output_tokens == len("hello there") // 4


async def test_the_latest_snapshot_is_the_exact_payload_of_the_last_request(
    engine_factory: EngineFactory,
) -> None:
    harness = engine_factory([tool_turn(), text_turn()])

    await harness.run("why is the api pod failing?")

    snapshot = harness.gateway.latest_outbound_payload
    assert snapshot is not None
    assert snapshot.iteration == 2
    assert snapshot.model == "qwen3:8b"
    assert json.loads(snapshot.payload_json)["messages"] == harness.provider.calls[1]


# -- provider failures -------------------------------------------------------


async def test_a_failure_before_handoff_reports_the_error_and_charges_nothing(
    engine_factory: EngineFactory,
) -> None:
    provider = ScriptedProvider([[RuntimeError("no route to host")]], acknowledge=False)
    harness = engine_factory(provider=provider)

    events = await harness.run()

    error = events[-1]
    assert isinstance(error, AgentError)
    assert "no route to host" in error.message
    assert harness.conversation.total_tokens == (0, 0)
    assert harness.conversation.usage_estimated is False
    assert harness.gateway.latest_outbound_payload is None


async def test_a_failure_after_handoff_charges_the_prompt_it_really_sent(
    engine_factory: EngineFactory,
) -> None:
    provider = ScriptedProvider(
        [[text_delta("partial answer while it lasted"), RuntimeError("stream died")], text_turn()]
    )
    harness = engine_factory(provider=provider)

    events = await harness.run()

    snapshot = harness.gateway.latest_outbound_payload
    assert snapshot is not None
    assert isinstance(events[-1], AgentError)
    assert harness.conversation.total_tokens == (
        len(snapshot.payload_json) // 4,
        len("partial answer while it lasted") // 4,
    )
    assert harness.conversation.usage_estimated is True


async def test_a_failed_turn_never_looks_like_a_completed_one(
    engine_factory: EngineFactory,
) -> None:
    provider = ScriptedProvider([[text_delta("partial"), RuntimeError("stream died")]])
    harness = engine_factory(provider=provider)

    events = await harness.run()

    assert not any(isinstance(event, TurnComplete) for event in events)


async def test_a_failure_after_a_streamed_call_leaves_valid_history(
    engine_factory: EngineFactory,
) -> None:
    """A call whose iteration died is never stored: it could never be answered."""
    execution = RecordingExecution()
    provider = ScriptedProvider(
        [
            [tool_call("c1", "get_logs", LOGS_ARGS), RuntimeError("stream died")],
            text_turn("recovered"),
        ]
    )
    harness = engine_factory(provider=provider, execution=execution)

    await harness.run("first")
    events = await harness.run("second")

    assert execution.calls == []
    assert not harness.conversation.has_unmatched_tool_calls
    assert isinstance(events[-1], TurnComplete)
    assert not any(message.get("tool_calls") for message in harness.provider.calls[1])
    assert all(message.get("role") != "tool" for message in harness.provider.calls[1])


async def test_a_provider_failure_message_is_bounded(engine_factory: EngineFactory) -> None:
    provider = ScriptedProvider([[RuntimeError("boom " * 5_000)]], acknowledge=False)
    harness = engine_factory(provider=provider)

    events = await harness.run()

    error = events[-1]
    assert isinstance(error, AgentError)
    assert len(error.message) <= 600


# -- interruption and close --------------------------------------------------


async def test_interrupting_ends_the_stream_without_a_completion(
    engine_factory: EngineFactory,
) -> None:
    """The engine stops at the next boundary; the session finalizes."""
    harness = engine_factory([tool_turn(), text_turn()])

    events = []
    async for event in harness.engine.run(harness.request()):
        events.append(event)
        if isinstance(event, ToolCallFinished):
            harness.engine.interrupt()

    assert not any(isinstance(event, TurnComplete) for event in events)
    assert len(harness.provider.calls) == 1
    interrupted = harness.conversation.finalize_interrupt()
    assert isinstance(interrupted, TurnInterrupted)
    assert not harness.conversation.has_unmatched_tool_calls


async def test_interrupting_an_idle_engine_does_not_disturb_the_next_turn(
    engine_factory: EngineFactory,
) -> None:
    harness = engine_factory([text_turn()])

    harness.engine.interrupt()
    events = await harness.run()

    assert isinstance(events[-1], TurnComplete)


async def test_cancelling_the_driving_task_closes_the_provider_stream(
    engine_factory: EngineFactory,
) -> None:
    stall = asyncio.Event()
    provider = ScriptedProvider([[text_delta("thinking about"), stall]])
    harness = engine_factory(provider=provider)

    async def drive() -> list[Any]:
        return [event async for event in harness.engine.run(harness.request())]

    task = asyncio.create_task(drive())
    await asyncio.wait_for(provider.streaming.wait(), timeout=5)
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.wait_for(provider.closed_event.wait(), timeout=5)

    assert provider.closed == 1
    interrupted = harness.conversation.finalize_interrupt()
    assert isinstance(interrupted, TurnInterrupted)
    note = str(harness.conversation.messages[-1]["content"])
    assert note.startswith("thinking about")
    assert note.endswith(INTERRUPT_MARKER)


async def test_a_second_turn_cannot_start_while_one_is_live(
    engine_factory: EngineFactory,
) -> None:
    harness = engine_factory([text_turn(), text_turn("second answer")])

    stream = harness.engine.run(harness.request("first"))
    first = await anext(stream)
    with pytest.raises(RuntimeError, match="already running"):
        harness.engine.run(harness.request("second"))
    async for _event in stream:
        pass

    assert first == TextDelta(text="the pod is healthy")
    events = await harness.run("second")
    assert isinstance(events[-1], TurnComplete)


async def test_aclose_releases_the_engine_and_rejects_a_later_turn(
    engine_factory: EngineFactory,
) -> None:
    harness = engine_factory([text_turn()])

    await harness.run()
    await harness.engine.aclose()
    await harness.engine.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        harness.engine.run(harness.request())
