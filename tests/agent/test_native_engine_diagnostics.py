"""Latency diagnostics woven through one native turn (issue #319).

Every test drives the real engine over scripted collaborators, attaching a
turn recorder over a deterministic clock so durations follow from call order
alone — no test sleeps or asserts a wall-clock figure. What is pinned is the
*shape* of one turn's attribution: the phase events the panel reads, the
terminal snapshot's outcome, and the per-round / per-tool records — never the
prompt, the tool arguments, or any provider payload.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from korvid.agent.diagnostics import AgentPhase, TurnOutcome, diagnostic_log_fields
from korvid.agent.events import (
    AgentError,
    AgentEvent,
    AgentPhaseChanged,
    TurnComplete,
)

from .engine_fakes import (
    DONE,
    RecordingExecution,
    ScriptedProvider,
    build_harness,
    deterministic_recorder,
    make_policy,
    provider_metrics,
    reasoning,
    text_delta,
    text_turn,
    tool_call,
    tool_turn,
)


def _phases(events: list[AgentEvent]) -> list[AgentPhaseChanged]:
    return [event for event in events if isinstance(event, AgentPhaseChanged)]


async def test_one_round_completion_reports_a_success_snapshot() -> None:
    harness = build_harness([text_turn("the pod is healthy")])
    recorder = deterministic_recorder()

    events = await harness.run(recorder=recorder)

    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    snapshot = complete.diagnostics
    assert snapshot is not None
    assert snapshot.outcome is TurnOutcome.SUCCESS
    assert snapshot.correlation_id == "corr"
    assert len(snapshot.rounds) == 1
    assert snapshot.tools == ()
    assert snapshot.total_seconds > 0
    # The one round measured prepare, handoff and first-event boundaries.
    only = snapshot.rounds[0]
    assert only.round_number == 1
    assert only.prepare_seconds is not None
    assert only.handoff_seconds is not None
    assert only.first_event_seconds is not None


async def test_one_round_emits_a_waiting_for_model_phase() -> None:
    harness = build_harness([text_turn("ok")])
    events = await harness.run(recorder=deterministic_recorder())

    phases = _phases(events)
    assert [p.phase for p in phases] == [AgentPhase.WAITING_FOR_MODEL]
    assert phases[0].round_number == 1
    assert phases[0].tool is None


async def test_turn_without_a_recorder_emits_no_phases_and_no_snapshot() -> None:
    harness = build_harness([text_turn("ok")])

    events = await harness.run()  # no recorder

    assert _phases(events) == []
    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    assert complete.diagnostics is None


async def test_tool_turn_reports_two_rounds_and_one_tool() -> None:
    harness = build_harness([tool_turn(), text_turn("done")])
    recorder = deterministic_recorder()

    events = await harness.run(recorder=recorder)

    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    snapshot = complete.diagnostics
    assert snapshot is not None
    assert snapshot.outcome is TurnOutcome.SUCCESS
    assert [r.round_number for r in snapshot.rounds] == [1, 2]
    assert len(snapshot.tools) == 1
    assert snapshot.tools[0].name == "get_logs"
    assert snapshot.tools[0].ok is True


async def test_tool_turn_phase_sequence_distinguishes_every_stage() -> None:
    harness = build_harness([tool_turn(), text_turn("done")])

    events = await harness.run(recorder=deterministic_recorder())
    phases = _phases(events)

    assert [(p.phase, p.round_number, p.tool) for p in phases] == [
        (AgentPhase.WAITING_FOR_MODEL, 1, None),
        (AgentPhase.RUNNING_TOOL, 1, "get_logs"),
        (AgentPhase.COMPOSING_ANSWER, 2, None),
    ]


async def test_provider_failure_reports_a_terminal_failed_snapshot() -> None:
    provider = ScriptedProvider([[text_delta("partial"), RuntimeError("stream died")]])
    harness = build_harness(provider=provider)

    events = await harness.run(recorder=deterministic_recorder())

    # A provider failure ends the turn with a terminal AgentError and no
    # TurnComplete: the failed turn must never look like a successful one.
    assert not any(isinstance(event, TurnComplete) for event in events)
    error = events[-1]
    assert isinstance(error, AgentError)
    snapshot = error.diagnostics
    assert snapshot is not None
    assert snapshot.outcome is TurnOutcome.FAILED
    assert len(snapshot.rounds) == 1


async def test_contained_tool_failure_keeps_the_turn_and_marks_the_tool() -> None:
    from .engine_fakes import RecordingExecution

    execution = RecordingExecution({"get_logs": RuntimeError("port exploded")})
    harness = build_harness([tool_turn(), text_turn("recovered")], execution=execution)

    events = await harness.run(recorder=deterministic_recorder())

    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    snapshot = complete.diagnostics
    assert snapshot is not None
    assert snapshot.outcome is TurnOutcome.SUCCESS  # the turn still finished
    assert len(snapshot.tools) == 1
    assert snapshot.tools[0].ok is False  # but the tool failed


async def test_interrupted_turn_leaves_the_recorder_for_the_session() -> None:
    stall = asyncio.Event()
    harness = build_harness([[text_delta("thinking"), stall]])
    recorder = deterministic_recorder()
    request = harness.request(recorder=recorder)

    async def drive() -> list[AgentEvent]:
        return [event async for event in harness.engine.run(request)]

    task = asyncio.create_task(drive())
    await asyncio.wait_for(harness.provider.stalled.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # The engine does not finalize on cancel: the recorder is still open, so
    # the session finalizes it as interrupted — a snapshot that must never
    # read as a success.
    snapshot = recorder.finalize(TurnOutcome.INTERRUPTED)
    assert snapshot.outcome is TurnOutcome.INTERRUPTED
    assert len(snapshot.rounds) == 1


async def test_provider_metrics_are_attached_to_their_round() -> None:
    metered = [
        tool_call("c1", "get_logs", '{"pod":"api-0","namespace":"prod"}'),
        provider_metrics(
            total_seconds=60.0,
            prompt_eval_seconds=50.0,
            prompt_tokens=1800,
            generation_seconds=5.0,
            generation_tokens=20,
        ),
        DONE,
    ]
    harness = build_harness([metered, text_turn("done")])

    events = await harness.run(recorder=deterministic_recorder())
    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    snapshot = complete.diagnostics
    assert snapshot is not None
    first = snapshot.rounds[0].provider_metrics
    assert first is not None
    assert first.prompt_tokens == 1800
    assert first.generation_tokens == 20
    # The composing round carried no metrics event.
    assert snapshot.rounds[1].provider_metrics is None


async def test_absent_provider_metrics_leave_rounds_unmetered() -> None:
    harness = build_harness([text_turn("plain")])

    events = await harness.run(recorder=deterministic_recorder())
    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    assert complete.diagnostics is not None
    assert complete.diagnostics.rounds[0].provider_metrics is None


@pytest.mark.parametrize("stop", ["iterations", "history", "unusable"])
async def test_early_stop_keeps_usage_but_reports_failed_diagnostics(stop: str) -> None:
    policy = make_policy(
        max_iterations=1 if stop == "iterations" else 6,
        max_history_chars=1_000 if stop == "history" else 24_000,
        strict_history_budget=stop == "history",
    )
    rounds = [tool_turn(call_id="" if stop == "unusable" else "c1")]
    harness = build_harness(
        rounds,
        policy=policy,
        execution=RecordingExecution({"get_logs": "L" * 2_000}),
    )

    events = await harness.run(recorder=deterministic_recorder())

    assert any(isinstance(event, AgentError) for event in events)
    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    assert complete.input_tokens > 0
    assert complete.diagnostics is not None
    assert complete.diagnostics.outcome is TurnOutcome.FAILED


async def test_round_distinguishes_dispatch_acknowledgement_and_content() -> None:
    harness = build_harness([[reasoning("private"), text_delta(""), text_delta("answer"), DONE]])
    events = await harness.run(recorder=deterministic_recorder())

    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    assert complete.diagnostics is not None
    round_ = complete.diagnostics.rounds[0]
    assert round_.request_started_at_seconds is not None
    assert round_.request_acknowledged_at_seconds is not None
    assert round_.first_event_at_seconds is not None
    assert round_.first_content_at_seconds is not None
    assert (
        round_.request_started_at_seconds
        < round_.request_acknowledged_at_seconds
        < round_.first_event_at_seconds
        < round_.first_content_at_seconds
        < round_.total_seconds
    )


async def test_dispatch_failure_has_no_acknowledgement_or_content() -> None:
    harness = build_harness(
        provider=ScriptedProvider([[ConnectionError("offline")]], acknowledge=False)
    )
    events = await harness.run(recorder=deterministic_recorder())

    error = events[-1]
    assert isinstance(error, AgentError)
    assert error.diagnostics is not None
    round_ = error.diagnostics.rounds[0]
    assert round_.request_started_at_seconds is not None
    assert round_.request_acknowledged_at_seconds is None
    assert round_.first_event_at_seconds is None
    assert round_.first_content_at_seconds is None
    assert harness.gateway.latest_outbound_payload is None


async def test_reasoning_only_round_does_not_fabricate_first_content() -> None:
    harness = build_harness([[reasoning("private"), DONE]])
    events = await harness.run(recorder=deterministic_recorder())

    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    assert complete.diagnostics is not None
    round_ = complete.diagnostics.rounds[0]
    assert round_.first_event_at_seconds is not None
    assert round_.first_content_at_seconds is None


async def test_diagnostics_never_retain_a_model_invented_tool_name() -> None:
    unknown = "provider-payload-private-marker"
    harness = build_harness([tool_turn(name=unknown), text_turn("recovered")])
    events = await harness.run(recorder=deterministic_recorder())

    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    assert complete.diagnostics is not None
    assert unknown not in str(diagnostic_log_fields(complete.diagnostics))
    assert all(phase.tool != unknown for phase in _phases(events))
    assert complete.diagnostics.tools[0].ok is False
