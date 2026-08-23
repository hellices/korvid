"""Transactional conversation state contract (Task 7).

`ConversationState` owns only durable user/assistant/tool history plus the
redaction provenance the outbound boundary cannot re-derive. System and
evidence prompt text is ephemeral: it is supplied to `request_messages`
and prepended per request, so it never enters retained history and a
recomposition cannot mutate what was already stored.

These tests pin the behavior that used to live inside `AgentRuntime`
(history trimming, strict/loose budgets, provenance projection,
protocol pairing, phase validation, usage accounting, and one-shot
interruption) as a standalone state machine, independent of the provider,
the outbound policy, and any prompt/tool policy.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import FrozenInstanceError

import pytest

from korvid.agent.conversation import (
    INTERRUPT_MARKER,
    MAX_HISTORY_TURNS,
    ConversationBudgetError,
    ConversationState,
    IterationCheckpoint,
    TurnCheckpoint,
)
from korvid.agent.events import TurnInterrupted
from korvid.core.redaction import RedactionRecord

#: The two policy budgets the current tiers use, exercised directly rather
#: than via the retired profile fixtures.
STRICT_BUDGET = 24_000
LOOSE_BUDGET = 120_000


def _text_turn(
    convo: ConversationState,
    content: str,
    answer: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    usage: bool = True,
) -> None:
    """Drive one complete text-only turn through the state machine."""
    convo.start_turn(content)
    convo.start_iteration()
    convo.record_stream_text(answer)
    if usage:
        convo.commit_usage(input_tokens, output_tokens)
    convo.append_assistant(answer)
    convo.complete_turn()


def _tool_turn(
    convo: ConversationState,
    content: str,
    *,
    call_id: str = "c1",
    name: str = "get_logs",
    result: str = "ok",
    result_records: Sequence[RedactionRecord] = (),
    result_error: bool = False,
) -> None:
    """Drive one turn that makes a single tool call, then answers."""
    convo.start_turn(content)
    convo.start_iteration()
    convo.record_stream_text("")
    convo.commit_usage(10, 2)
    convo.append_assistant("", [{"id": call_id, "name": name, "arguments": "{}"}])
    convo.append_tool_result(call_id, result, result_records, error=result_error)
    convo.start_iteration()
    convo.record_stream_text("done")
    convo.commit_usage(5, 1)
    convo.append_assistant("done")
    convo.complete_turn()


# --------------------------------------------------------------------------
# History and budget
# --------------------------------------------------------------------------


def test_history_persists_across_turns() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    _text_turn(convo, "first", "a")
    _text_turn(convo, "second", "b")
    roles = [m["role"] for m in convo.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    joined = json.dumps(convo.messages)
    assert "first" in joined
    assert "second" in joined


def test_oldest_complete_turn_is_trimmed_by_turn_cap() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    for i in range(12):
        _text_turn(convo, f"question-{i}", "ok")
    user_messages = [m for m in convo.messages if m["role"] == "user"]
    assert len(user_messages) <= MAX_HISTORY_TURNS
    joined = json.dumps(convo.messages)
    assert "question-11" in joined  # newest kept
    assert "question-0" not in joined  # oldest dropped
    # A turn boundary is always a user message, so trimming never orphans a
    # tool result or an assistant tool call.
    assert convo.has_unmatched_tool_calls is False


def test_char_budget_drops_oldest_turn_but_keeps_the_newest() -> None:
    convo = ConversationState(max_history_chars=STRICT_BUDGET)
    for i in range(4):
        _text_turn(convo, "x" * 13_000 + f" mark-{i}", "ok")
    joined = json.dumps(convo.messages)
    assert "mark-3" in joined  # newest complete turn always retained
    assert "mark-0" not in joined  # oldest dropped to satisfy the budget
    user_messages = [m for m in convo.messages if m["role"] == "user"]
    assert len(user_messages) >= 1


def test_request_messages_prepends_prompt_without_mutating_history() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("hello")
    before = json.dumps(convo.messages)
    convo.start_iteration()
    view = convo.request_messages(prefix=[{"role": "system", "content": "SYSTEM PROMPT"}])
    assert view.messages[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert view.messages[1]["role"] == "user"
    # The ephemeral prefix never enters retained history.
    assert all(m["role"] != "system" for m in convo.messages)
    assert json.dumps(convo.messages) == before


def test_request_messages_returns_deep_copies() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("hello")
    convo.start_iteration()
    view = convo.request_messages()
    view.messages[0]["content"] = "TAMPERED"
    view.messages.append({"role": "user", "content": "injected"})
    assert convo.messages[0]["content"] != "TAMPERED"
    assert len(convo.messages) == 1


def test_provenance_projects_onto_the_prepared_indices() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("hello", records=[RedactionRecord("screen", "masked")])
    convo.start_iteration()
    convo.record_stream_text("")
    convo.commit_usage(1, 1)
    convo.append_assistant("", [{"id": "c1", "name": "get_logs", "arguments": "{}"}])
    convo.append_tool_result(
        "c1",
        "masked result",
        [RedactionRecord("data.password", "secret")],
        error=True,
    )
    view = convo.request_messages(prefix=[{"role": "system", "content": "SYS"}])
    # messages: [system, user, assistant, tool]
    assert view.messages[1]["role"] == "user"
    assert view.messages[3]["role"] == "tool"
    assert view.ingress[1] == (RedactionRecord("screen", "masked"),)
    assert view.ingress[3] == (RedactionRecord("data.password", "secret"),)
    assert view.tool_errors == frozenset({3})


def test_provenance_survives_trimming_by_index() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    _tool_turn(
        convo,
        "first",
        call_id="a1",
        result="first result",
        result_records=[RedactionRecord("first.pw", "secret")],
    )
    _tool_turn(
        convo,
        "second",
        call_id="b1",
        result="second result",
        result_records=[RedactionRecord("second.pw", "secret")],
    )
    convo.start_turn("third")
    removed = convo.drop_oldest_turn()
    assert removed > 0
    view = convo.request_messages()
    surviving = {
        index: records
        for index, records in view.ingress.items()
        if any(r.path == "second.pw" for r in records)
    }
    assert surviving  # the second turn's redaction record still resolves
    # The dropped turn's record no longer projects onto any index.
    assert all(all(r.path != "first.pw" for r in records) for records in view.ingress.values())
    # Every projected index addresses a message that really carries it.
    for index, records in view.ingress.items():
        assert view.messages[index].get("content") is not None
        assert records


def test_strict_preflight_rejects_a_prompt_that_cannot_fit() -> None:
    convo = ConversationState(max_history_chars=STRICT_BUDGET, strict_history_budget=True)
    with pytest.raises(ConversationBudgetError, match="history budget"):
        convo.start_turn("x" * (STRICT_BUDGET * 2))
    # The rejected prompt was dropped, so a normal follow-up starts cleanly.
    assert convo.messages == []
    checkpoint = convo.start_turn("short")
    assert isinstance(checkpoint, TurnCheckpoint)
    assert any(m["content"] == "short" for m in convo.messages)


def test_strict_preflight_drops_an_oversized_previous_turn() -> None:
    convo = ConversationState(max_history_chars=STRICT_BUDGET, strict_history_budget=True)
    _text_turn(convo, "x" * 22_000 + " old", "y" * 5_000)
    convo.start_turn("fresh question")
    joined = json.dumps(convo.messages)
    assert "old" not in joined  # the oversized predecessor was trimmed away
    assert "fresh question" in joined


def test_loose_mode_keeps_an_oversized_newest_turn() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    # Loose mode never rejects: the newest turn is retained even when it is
    # larger than the budget; recovery is the caller's via drop_oldest_turn.
    checkpoint = convo.start_turn("z" * (LOOSE_BUDGET * 2))
    assert isinstance(checkpoint, TurnCheckpoint)
    assert any(len(str(m.get("content"))) > LOOSE_BUDGET for m in convo.messages)


def test_drop_oldest_turn_never_drops_the_in_flight_turn() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    _text_turn(convo, "old question", "old answer")
    convo.start_turn("current question")
    removed = convo.drop_oldest_turn()
    assert removed > 0
    joined = json.dumps(convo.messages)
    assert "old question" not in joined
    assert "current question" in joined
    # Nothing older remains: the current prompt is never a drop candidate.
    assert convo.drop_oldest_turn() == 0
    assert "current question" in json.dumps(convo.messages)


# --------------------------------------------------------------------------
# Usage accounting
# --------------------------------------------------------------------------


def test_usage_accumulates_across_iterations_and_turns() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration()
    convo.record_stream_text("")
    convo.commit_usage(50, 5)
    convo.append_assistant("", [{"id": "c1", "name": "get_logs", "arguments": "{}"}])
    convo.append_tool_result("c1", "ok")
    convo.start_iteration()
    convo.record_stream_text("answer")
    convo.commit_usage(70, 9)
    convo.append_assistant("answer")
    turn_in, turn_out, estimated = convo.complete_turn()
    assert (turn_in, turn_out, estimated) == (120, 14, False)
    assert convo.total_tokens == (120, 14)
    assert convo.usage_estimated is False


def test_missing_usage_marks_the_turn_estimated() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("a diagnosis")  # transmitted, but no usage event
    convo.append_assistant("a diagnosis")
    turn_in, turn_out, estimated = convo.complete_turn()
    assert estimated is True
    assert turn_in > 0  # the transmitted prompt is charged, never zero
    assert turn_out > 0
    assert convo.usage_estimated is True


def test_usage_estimated_is_sticky() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    _text_turn(convo, "first", "hi", usage=False)  # missing usage
    assert convo.usage_estimated is True
    _text_turn(convo, "second", "hi", input_tokens=5, output_tokens=2)
    assert convo.usage_estimated is True  # earlier estimate stays an estimate


# --------------------------------------------------------------------------
# Protocol pairing
# --------------------------------------------------------------------------


def test_tool_calls_and_results_stay_paired() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("logs?")
    convo.start_iteration()
    convo.record_stream_text("")
    convo.commit_usage(1, 1)
    convo.append_assistant("", [{"id": "c1", "name": "get_logs", "arguments": "{}"}])
    assert convo.has_unmatched_tool_calls is True  # awaiting a result
    convo.append_tool_result("c1", "ok")
    assert convo.has_unmatched_tool_calls is False
    assistant = next(m for m in convo.messages if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["name"] == "get_logs"


def test_completing_a_turn_with_a_dangling_tool_call_is_rejected() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("logs?")
    convo.start_iteration()
    convo.record_stream_text("")
    convo.commit_usage(1, 1)
    convo.append_assistant("", [{"id": "c1", "name": "get_logs", "arguments": "{}"}])
    with pytest.raises(RuntimeError, match="unmatched"):
        convo.complete_turn()


# --------------------------------------------------------------------------
# Phase validation
# --------------------------------------------------------------------------


def test_start_turn_rejects_a_second_active_turn() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("first")
    with pytest.raises(RuntimeError, match="already active"):
        convo.start_turn("second")


def test_start_iteration_requires_an_active_turn() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    with pytest.raises(RuntimeError, match="no active turn"):
        convo.start_iteration()


def test_record_stream_text_requires_an_open_iteration() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    with pytest.raises(RuntimeError, match="no open iteration"):
        convo.record_stream_text("x")


def test_request_messages_requires_an_active_turn() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    with pytest.raises(RuntimeError, match="no active turn"):
        convo.request_messages()


def test_append_tool_result_requires_a_pending_call() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration()
    convo.record_stream_text("done")
    convo.append_assistant("done")  # no tool calls
    with pytest.raises(RuntimeError, match="no pending tool call"):
        convo.append_tool_result("c1", "ok")


def test_checkpoints_are_frozen_values() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    turn = convo.start_turn("q")
    iteration = convo.start_iteration(prompt_estimate=12)
    assert isinstance(turn, TurnCheckpoint)
    assert isinstance(iteration, IterationCheckpoint)
    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        turn.base_index = 5  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        iteration.base_index = 5  # type: ignore[misc]


# --------------------------------------------------------------------------
# Interruption
# --------------------------------------------------------------------------


def _marker(convo: ConversationState) -> str:
    return str(convo.messages[-1]["content"])


def test_interrupt_before_provider_handoff() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration(prompt_estimate=40)
    event = convo.finalize_interrupt()
    assert isinstance(event, TurnInterrupted)
    assert event.input_tokens == 0  # nothing was transmitted
    assert event.output_tokens == 0
    assert event.estimated is False
    assert _marker(convo).endswith("[response interrupted]")
    assert convo.has_unmatched_tool_calls is False


def test_interrupt_during_streamed_text_keeps_a_bounded_marked_partial() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("thinking about the outage")
    event = convo.finalize_interrupt()
    assert isinstance(event, TurnInterrupted)
    marker = _marker(convo)
    assert marker.endswith("[response interrupted]")
    assert "thinking about the outage" in marker  # bounded partial retained
    assert marker != "thinking about the outage"  # never a completed-looking answer
    assert event.estimated is True  # transmitted without usage → estimated
    assert event.input_tokens > 0


def test_interrupt_partial_is_capped() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("x" * 10_000)
    convo.finalize_interrupt()
    marker = _marker(convo)
    assert marker.endswith("[response interrupted]")
    assert len(marker) <= 2_100


def test_interrupt_during_partial_tool_arguments_leaves_no_unmatched_calls() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("logs?")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("")  # a tool-call stream started but never completed
    event = convo.finalize_interrupt()
    assert isinstance(event, TurnInterrupted)
    assert convo.has_unmatched_tool_calls is False
    for message in convo.messages:
        assert not message.get("tool_calls")
        assert message.get("role") != "tool"
    assert _marker(convo).endswith("[response interrupted]")


def test_interrupt_after_a_tool_result_rolls_back_the_active_iteration() -> None:
    """A completed prior iteration survives; the interrupted one unwinds
    whole, so no assistant tool call is left without its result."""
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("logs?")
    convo.start_iteration()
    convo.record_stream_text("")
    convo.commit_usage(50, 5)
    convo.append_assistant("", [{"id": "c1", "name": "get_logs", "arguments": "{}"}])
    convo.append_tool_result("c1", "result-one")
    # A second iteration begins and is cancelled mid-stream.
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("still working")
    convo.finalize_interrupt()
    roles = [m["role"] for m in convo.messages]
    assert "tool" in roles  # iteration one's completed result kept
    call_ids = {tc["id"] for m in convo.messages for tc in (m.get("tool_calls") or [])}
    result_ids = {m["tool_call_id"] for m in convo.messages if m["role"] == "tool"}
    assert call_ids == result_ids  # every call paired with a result
    assert convo.has_unmatched_tool_calls is False


def test_usage_reported_before_interruption_is_committed() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("partial")
    convo.commit_usage(70, 9)  # provider reported real usage before the stall
    event = convo.finalize_interrupt()
    assert event.input_tokens == 70
    assert event.output_tokens == 9
    assert event.estimated is False
    assert convo.total_tokens == (70, 9)


def test_committed_iteration_usage_is_not_double_counted_on_interrupt() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration()
    convo.record_stream_text("")
    convo.commit_usage(50, 5)
    convo.append_assistant("", [{"id": "c1", "name": "get_logs", "arguments": "{}"}])
    convo.append_tool_result("c1", "ok")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("partial")
    convo.commit_usage(70, 9)
    event = convo.finalize_interrupt()
    assert event.input_tokens == 120  # 50 committed + 70 in flight, counted once
    assert event.output_tokens == 14
    assert convo.total_tokens == (120, 14)


def test_finalize_interrupt_is_one_shot() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("partial")
    convo.commit_usage(70, 9)
    convo.finalize_interrupt()
    history = json.dumps(convo.messages)
    totals = convo.total_tokens
    with pytest.raises(RuntimeError, match="already"):
        convo.finalize_interrupt()
    assert json.dumps(convo.messages) == history  # no duplicated marker
    assert convo.total_tokens == totals  # no duplicated usage


def test_finalize_interrupt_requires_an_active_turn() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    with pytest.raises(RuntimeError, match="no active turn"):
        convo.finalize_interrupt()


def test_a_turn_runs_cleanly_after_an_interrupt() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("first")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("partial")
    convo.finalize_interrupt()
    # A fresh turn starts and completes without leaking the prior state.
    _text_turn(convo, "second", "answer")
    assert convo.messages[-1]["content"] == "answer"
    assert convo.has_unmatched_tool_calls is False


# -- the engine's own mechanisms (Task 10) ------------------------------------


def test_mark_transmitted_settles_a_request_that_streamed_nothing() -> None:
    """Handoff proof, not stream content, is what makes a request cost."""
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration(prompt_estimate=40)
    convo.mark_transmitted()
    convo.append_assistant("")
    assert convo.complete_turn() == (40, 0, True)


def test_mark_transmitted_requires_an_open_iteration() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    with pytest.raises(RuntimeError, match="no open iteration"):
        convo.mark_transmitted()


def test_a_streamed_tool_call_is_output_even_when_no_text_streamed() -> None:
    """A tool-only iteration generated the call it emitted (issue #316)."""
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_tool_call("get_logs", '{"pod":"api-0"}')
    event = convo.finalize_interrupt()
    assert event.input_tokens == 40
    assert event.output_tokens == (len("get_logs") + len('{"pod":"api-0"}')) // 4
    assert event.estimated is True


def test_a_streamed_tool_call_never_contaminates_the_partial_answer() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_tool_call("get_logs", '{"pod":"api-0"}')
    convo.finalize_interrupt()
    assert convo.messages[-1]["content"] == INTERRUPT_MARKER


def test_recording_a_streamed_tool_call_requires_an_open_iteration() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    with pytest.raises(RuntimeError, match="no open iteration"):
        convo.record_stream_tool_call("get_logs", "{}")


def test_abandon_iteration_unwinds_only_what_that_iteration_appended() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    _tool_turn(convo, "q")
    convo.start_turn("second")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("partial")
    convo.abandon_iteration()
    assert [message["role"] for message in convo.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert convo.has_unmatched_tool_calls is False
    assert convo.complete_turn() == (40, 1, True)


def test_abandon_iteration_charges_nothing_for_a_request_never_transmitted() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration(prompt_estimate=40)
    convo.abandon_iteration()
    assert convo.complete_turn() == (0, 0, False)


def test_abandon_iteration_requires_an_open_iteration() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    with pytest.raises(RuntimeError, match="no open iteration"):
        convo.abandon_iteration()


def test_rollback_turn_drops_the_turn_and_keeps_the_cost_it_paid() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    _text_turn(convo, "first", "answer", input_tokens=5, output_tokens=1)
    convo.start_turn("second")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("")
    convo.commit_usage(10, 2)
    convo.append_assistant("", [{"id": "c1", "name": "get_logs", "arguments": "{}"}])
    assert convo.rollback_turn() == (10, 2, False)
    assert [message["content"] for message in convo.messages] == ["first", "answer"]
    assert convo.has_unmatched_tool_calls is False
    assert convo.total_tokens == (15, 3)


def test_rollback_turn_counts_an_iteration_that_never_landed() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("q")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("partial")
    assert convo.rollback_turn() == (40, 1, True)
    assert convo.messages == []
    assert convo.usage_estimated is True


def test_rollback_turn_requires_an_active_turn() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    with pytest.raises(RuntimeError, match="no active turn"):
        convo.rollback_turn()


def test_a_turn_runs_cleanly_after_a_rollback() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("first")
    convo.start_iteration(prompt_estimate=40)
    convo.record_stream_text("partial")
    convo.rollback_turn()
    _text_turn(convo, "second", "answer")
    assert [message["content"] for message in convo.messages] == ["second", "answer"]


def test_history_chars_reports_what_the_budget_is_measured_against() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    assert convo.history_chars == 0
    _text_turn(convo, "question", "answer")
    assert convo.history_chars >= len("question") + len("answer")


# --------------------------------------------------------------------------
# Call-id identity across iterations and turns
# --------------------------------------------------------------------------


def test_retained_tool_call_ids_names_every_call_history_still_holds() -> None:
    """The caller cannot re-issue an id retained history already spent."""
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    _tool_turn(convo, "first", call_id="c1")
    convo.start_turn("second")
    convo.start_iteration()
    convo.append_assistant("", [{"id": "c2", "name": "get_logs", "arguments": "{}"}])

    assert convo.retained_tool_call_ids == frozenset({"c1", "c2"})


def test_retained_tool_call_ids_is_a_copy_owned_snapshot() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    _tool_turn(convo, "first", call_id="c1")

    snapshot = convo.retained_tool_call_ids
    _tool_turn(convo, "second", call_id="c2")

    assert isinstance(snapshot, frozenset)
    assert snapshot == frozenset({"c1"})
    assert convo.retained_tool_call_ids == frozenset({"c1", "c2"})


def test_retained_tool_call_ids_forgets_a_dropped_turn() -> None:
    """An id only stays spent while the message carrying it is retained."""
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    _tool_turn(convo, "first", call_id="c1")
    convo.start_turn("second")

    assert convo.drop_oldest_turn() > 0
    assert convo.retained_tool_call_ids == frozenset()


def test_a_repeated_call_id_cannot_cancel_an_unanswered_one() -> None:
    """Two calls sharing an id need two results; sets would hide the second."""
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("logs?")
    convo.start_iteration()
    convo.append_assistant(
        "",
        [
            {"id": "c1", "name": "get_logs", "arguments": "{}"},
            {"id": "c1", "name": "get_logs", "arguments": "{}"},
        ],
    )
    convo.append_tool_result("c1", "ok")

    assert convo.has_unmatched_tool_calls is True
    with pytest.raises(RuntimeError, match="unmatched"):
        convo.complete_turn()


def test_two_calls_sharing_an_id_take_two_results() -> None:
    """Pairing is per call, not per distinct id: two calls, two results."""
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("logs?")
    convo.start_iteration()
    convo.append_assistant(
        "",
        [
            {"id": "c1", "name": "get_logs", "arguments": "{}"},
            {"id": "c1", "name": "get_logs", "arguments": "{}"},
        ],
    )
    convo.append_tool_result("c1", "first")
    convo.append_tool_result("c1", "second")

    assert convo.has_unmatched_tool_calls is False
    assert convo.complete_turn() == (0, 0, False)


# --- turn-active reporting (task 11 decides whether to finalize) -------------


def test_turn_active_is_false_before_any_turn() -> None:
    assert ConversationState(max_history_chars=LOOSE_BUDGET).turn_active is False


def test_turn_active_is_true_between_start_and_complete() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)

    convo.start_turn("why?")

    assert convo.turn_active is True


def test_a_completed_turn_is_no_longer_active() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    _text_turn(convo, "why?", "because")

    assert convo.turn_active is False


def test_an_abandoned_turn_stays_active_until_it_is_finalized() -> None:
    """The session reads this to decide whether a stopped turn needs repair."""
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("why?")
    convo.start_iteration()
    convo.record_stream_text("thinking")

    assert convo.turn_active is True
    convo.finalize_interrupt()
    assert convo.turn_active is False


def test_a_rolled_back_turn_is_no_longer_active() -> None:
    convo = ConversationState(max_history_chars=LOOSE_BUDGET)
    convo.start_turn("why?")
    convo.start_iteration()

    convo.rollback_turn()

    assert convo.turn_active is False
