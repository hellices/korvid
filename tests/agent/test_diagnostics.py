"""State-machine and formatting tests for `korvid.agent.diagnostics` (Task 1).

Every test uses a deterministic, list-backed clock (integer ticks, one call
per recorder method) so expected durations are computed from call order
alone — no test ever sleeps or asserts real elapsed time.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import logging
from collections.abc import Callable, Iterator

import pytest

from korvid.agent.diagnostics import (
    AgentPhase,
    DiagnosticsStateError,
    ProviderRoundDiagnostics,
    ProviderRuntimeMetrics,
    ToolDiagnostics,
    TurnDiagnostics,
    TurnDiagnosticsFactory,
    TurnDiagnosticsRecorder,
    TurnOutcome,
    diagnostic_log_fields,
    format_diagnostics,
    log_diagnostics,
    provider_metrics_are_empty,
    provider_metrics_from_event,
)


def _ticking_clock(*, start: float = 0.0, step: float = 1.0) -> Callable[[], float]:
    """Return a clock yielding start, start+step, start+2*step, ... on each call."""
    counter: Iterator[float] = itertools.count(start, step)
    return lambda: next(counter)


def _recorder(correlation_id: str) -> TurnDiagnosticsRecorder:
    factory = TurnDiagnosticsFactory(clock=_ticking_clock(), id_factory=lambda: correlation_id)
    return factory.create()


# ---------------------------------------------------------------------------
# Recorder state machine
# ---------------------------------------------------------------------------


def test_one_round_completion_records_expected_timings() -> None:
    recorder = _recorder("corr-1")  # tick 0.0 consumed for turn start

    recorder.begin_round()  # 1.0
    recorder.prepare_started()  # 2.0
    recorder.prepare_finished()  # 3.0
    recorder.request_handed_off()  # 4.0
    recorder.first_model_event()  # 5.0
    recorder.end_round()  # 6.0

    summary = recorder.finalize(TurnOutcome.SUCCESS)  # 7.0

    assert summary == TurnDiagnostics(
        correlation_id="corr-1",
        outcome=TurnOutcome.SUCCESS,
        total_seconds=7.0,
        rounds=(
            ProviderRoundDiagnostics(
                round_number=1,
                prepare_seconds=1.0,
                handoff_seconds=1.0,
                first_event_seconds=1.0,
                total_seconds=5.0,
                provider_metrics=None,
                request_acknowledged_at_seconds=3.0,
                first_event_at_seconds=4.0,
            ),
        ),
        tools=(),
    )


def test_multi_round_and_tool_ordering() -> None:
    recorder = _recorder("corr-2")  # 0.0

    recorder.begin_round()  # 1.0
    recorder.prepare_started()  # 2.0
    recorder.prepare_finished()  # 3.0
    recorder.request_handed_off()  # 4.0
    recorder.first_model_event()  # 5.0
    recorder.end_round()  # 6.0

    recorder.begin_tool("list_pods")  # 7.0
    recorder.end_tool(ok=True)  # 8.0

    recorder.begin_round()  # 9.0
    recorder.prepare_started()  # 10.0
    recorder.prepare_finished()  # 11.0
    recorder.request_handed_off()  # 12.0
    recorder.first_model_event()  # 13.0
    recorder.end_round()  # 14.0

    summary = recorder.finalize(TurnOutcome.SUCCESS)  # 15.0

    assert summary.correlation_id == "corr-2"
    assert summary.outcome is TurnOutcome.SUCCESS
    assert summary.total_seconds == 15.0
    assert [r.round_number for r in summary.rounds] == [1, 2]
    assert summary.rounds[0] == ProviderRoundDiagnostics(
        round_number=1,
        prepare_seconds=1.0,
        handoff_seconds=1.0,
        first_event_seconds=1.0,
        total_seconds=5.0,
        provider_metrics=None,
        request_acknowledged_at_seconds=3.0,
        first_event_at_seconds=4.0,
    )
    assert summary.rounds[1] == ProviderRoundDiagnostics(
        round_number=2,
        prepare_seconds=1.0,
        handoff_seconds=1.0,
        first_event_seconds=1.0,
        total_seconds=5.0,
        provider_metrics=None,
        request_acknowledged_at_seconds=3.0,
        first_event_at_seconds=4.0,
    )
    assert summary.tools == (ToolDiagnostics(sequence=1, name="list_pods", seconds=1.0, ok=True),)


def test_provider_metrics_are_attached_to_the_active_round() -> None:
    recorder = _recorder("corr-3")  # 0.0
    recorder.begin_round()  # 1.0
    recorder.prepare_started()  # 2.0
    recorder.prepare_finished()  # 3.0
    recorder.request_handed_off()  # 4.0
    recorder.first_model_event()  # 5.0

    metrics = ProviderRuntimeMetrics(
        total_seconds=4.2,
        load_seconds=0.1,
        prompt_eval_seconds=3.0,
        prompt_tokens=200,
        generation_seconds=1.1,
        generation_tokens=40,
    )
    recorder.record_provider_metrics(metrics)

    recorder.end_round()  # 6.0
    summary = recorder.finalize(TurnOutcome.SUCCESS)  # 7.0

    assert summary.rounds[0].provider_metrics == metrics


def test_provider_metrics_without_active_round_are_rejected() -> None:
    recorder = _recorder("corr-4")
    metrics = ProviderRuntimeMetrics(prompt_tokens=1)
    with pytest.raises(DiagnosticsStateError, match="no active round"):
        recorder.record_provider_metrics(metrics)


def test_provider_metrics_recorded_twice_for_one_round_are_rejected() -> None:
    recorder = _recorder("corr-5")
    recorder.begin_round()
    recorder.record_provider_metrics(ProviderRuntimeMetrics(prompt_tokens=1))
    with pytest.raises(DiagnosticsStateError, match="already recorded"):
        recorder.record_provider_metrics(ProviderRuntimeMetrics(prompt_tokens=2))


def test_failed_finalization_closes_an_active_round() -> None:
    recorder = _recorder("corr-6")  # 0.0
    recorder.begin_round()  # 1.0
    recorder.prepare_started()  # 2.0
    recorder.prepare_finished()  # 3.0
    recorder.request_handed_off()  # 4.0
    # Provider failed before it produced any streamed event: no
    # first_model_event() call before the turn is finalized as failed.

    summary = recorder.finalize(TurnOutcome.FAILED)  # 5.0

    assert summary.outcome is TurnOutcome.FAILED
    assert summary.total_seconds == 5.0
    assert summary.rounds == (
        ProviderRoundDiagnostics(
            round_number=1,
            prepare_seconds=1.0,
            handoff_seconds=1.0,
            # first_model_event never happened: None, not a misleading 0.0.
            first_event_seconds=None,
            total_seconds=4.0,
            provider_metrics=None,
            request_acknowledged_at_seconds=3.0,
        ),
    )
    assert summary.tools == ()


def test_begin_round_only_failure_records_none_for_all_sub_steps() -> None:
    """A round that starts but fails before any prepare/handoff/first-event
    must not fabricate 0.0 durations for boundaries that never occurred."""
    recorder = _recorder("corr-begin-only")  # 0.0
    recorder.begin_round()  # 1.0
    # Engine fails immediately — no prepare, no handoff, no first event.

    summary = recorder.finalize(TurnOutcome.FAILED)  # 2.0

    assert len(summary.rounds) == 1
    round_ = summary.rounds[0]
    assert round_.prepare_seconds is None
    assert round_.handoff_seconds is None
    assert round_.first_event_seconds is None
    assert round_.total_seconds == 1.0  # actual elapsed time is preserved


def test_finalize_during_incomplete_prepare_records_none_prepare_and_real_total() -> None:
    """A round where `prepare_started` was called but `prepare_finished` was
    never called must record `prepare_seconds=None` — the prepare boundary did
    not complete, so no duration exists — while `total_seconds` is still the
    real elapsed time from round start to finalization."""
    recorder = _recorder("corr-prepare-incomplete")  # 0.0
    recorder.begin_round()  # 1.0
    recorder.prepare_started()  # 2.0
    # Engine fails before prepare_finished() — only prepare_start was recorded.

    summary = recorder.finalize(TurnOutcome.FAILED)  # 3.0

    assert len(summary.rounds) == 1
    round_ = summary.rounds[0]
    assert round_.prepare_seconds is None  # prepare never completed
    assert round_.handoff_seconds is None
    assert round_.first_event_seconds is None
    assert round_.total_seconds == 2.0  # actual elapsed time is preserved


def test_prepare_only_failure_records_none_for_absent_sub_steps() -> None:
    """A round that completes prepare but fails before handoff must record
    None for handoff_seconds and first_event_seconds."""
    recorder = _recorder("corr-prepare-only")  # 0.0
    recorder.begin_round()  # 1.0
    recorder.prepare_started()  # 2.0
    recorder.prepare_finished()  # 3.0
    # Engine fails after prepare — handoff never happens.

    summary = recorder.finalize(TurnOutcome.FAILED)  # 4.0

    assert len(summary.rounds) == 1
    round_ = summary.rounds[0]
    assert round_.prepare_seconds == 1.0  # prepare did occur
    assert round_.handoff_seconds is None  # handoff never occurred
    assert round_.first_event_seconds is None  # first event never occurred
    assert round_.total_seconds == 3.0  # actual elapsed time is preserved


def test_handoff_without_first_event_records_none_for_first_event_seconds() -> None:
    """A round where the request was handed off but no model event was ever
    received must record None for first_event_seconds, not a misleading 0.0."""
    recorder = _recorder("corr-handoff-no-event")  # 0.0
    recorder.begin_round()  # 1.0
    recorder.prepare_started()  # 2.0
    recorder.prepare_finished()  # 3.0
    recorder.request_handed_off()  # 4.0
    # Provider is never heard from — finalize without first_model_event().

    summary = recorder.finalize(TurnOutcome.FAILED)  # 5.0

    assert len(summary.rounds) == 1
    round_ = summary.rounds[0]
    assert round_.prepare_seconds == 1.0
    assert round_.handoff_seconds == 1.0
    assert round_.first_event_seconds is None  # never received a model event
    assert round_.total_seconds == 4.0


def test_interrupted_finalization_closes_an_active_tool_as_unsuccessful() -> None:
    recorder = _recorder("corr-7")  # 0.0
    recorder.begin_round()  # 1.0
    recorder.prepare_started()  # 2.0
    recorder.prepare_finished()  # 3.0
    recorder.request_handed_off()  # 4.0
    recorder.first_model_event()  # 5.0
    recorder.end_round()  # 6.0

    recorder.begin_tool("scale_deployment")  # 7.0
    summary = recorder.finalize(TurnOutcome.INTERRUPTED)  # 8.0

    assert summary.outcome is TurnOutcome.INTERRUPTED
    assert summary.total_seconds == 8.0
    assert len(summary.rounds) == 1
    assert summary.tools == (
        ToolDiagnostics(sequence=1, name="scale_deployment", seconds=1.0, ok=False),
    )


def test_double_finalization_is_rejected() -> None:
    recorder = _recorder("corr-8")
    recorder.finalize(TurnOutcome.SUCCESS)
    with pytest.raises(DiagnosticsStateError, match="already finalized"):
        recorder.finalize(TurnOutcome.SUCCESS)


def test_operations_after_finalize_are_rejected() -> None:
    recorder = _recorder("corr-9")
    recorder.finalize(TurnOutcome.SUCCESS)
    with pytest.raises(DiagnosticsStateError, match="already finalized"):
        recorder.begin_round()
    with pytest.raises(DiagnosticsStateError, match="already finalized"):
        recorder.begin_tool("x")


def test_begin_round_while_a_round_is_active_is_rejected() -> None:
    recorder = _recorder("corr-10")
    recorder.begin_round()
    with pytest.raises(DiagnosticsStateError, match="already active"):
        recorder.begin_round()


def test_end_round_without_begin_round_is_rejected() -> None:
    recorder = _recorder("corr-11")
    with pytest.raises(DiagnosticsStateError, match="no active round"):
        recorder.end_round()


def test_prepare_finished_before_prepare_started_is_rejected() -> None:
    recorder = _recorder("corr-12")
    recorder.begin_round()
    with pytest.raises(DiagnosticsStateError, match="prepare must start"):
        recorder.prepare_finished()


def test_request_handed_off_before_prepare_finished_is_rejected() -> None:
    recorder = _recorder("corr-13")
    recorder.begin_round()
    recorder.prepare_started()
    with pytest.raises(DiagnosticsStateError, match="prepare finished"):
        recorder.request_handed_off()


def test_first_model_event_before_request_handed_off_is_rejected() -> None:
    recorder = _recorder("corr-21")
    recorder.begin_round()
    recorder.prepare_started()
    recorder.prepare_finished()
    with pytest.raises(DiagnosticsStateError, match="request handoff"):
        recorder.first_model_event()


def test_begin_tool_while_a_tool_is_active_is_rejected() -> None:
    recorder = _recorder("corr-14")
    recorder.begin_tool("a")
    with pytest.raises(DiagnosticsStateError, match="already active"):
        recorder.begin_tool("b")


def test_end_tool_without_begin_tool_is_rejected() -> None:
    recorder = _recorder("corr-15")
    with pytest.raises(DiagnosticsStateError, match="no active tool"):
        recorder.end_tool(ok=True)


def test_prepare_started_called_twice_is_rejected() -> None:
    recorder = _recorder("corr-16")
    recorder.begin_round()
    recorder.prepare_started()
    with pytest.raises(DiagnosticsStateError, match="already started"):
        recorder.prepare_started()


def test_prepare_finished_called_twice_is_rejected() -> None:
    recorder = _recorder("corr-17")
    recorder.begin_round()
    recorder.prepare_started()
    recorder.prepare_finished()
    with pytest.raises(DiagnosticsStateError, match="already finished"):
        recorder.prepare_finished()


def test_request_handed_off_called_twice_is_rejected() -> None:
    recorder = _recorder("corr-18")
    recorder.begin_round()
    recorder.prepare_started()
    recorder.prepare_finished()
    recorder.request_handed_off()
    with pytest.raises(DiagnosticsStateError, match="already handed off"):
        recorder.request_handed_off()


def test_first_model_event_called_twice_is_rejected() -> None:
    recorder = _recorder("corr-19")
    recorder.begin_round()
    recorder.prepare_started()
    recorder.prepare_finished()
    recorder.request_handed_off()
    recorder.first_model_event()
    with pytest.raises(DiagnosticsStateError, match="already recorded"):
        recorder.first_model_event()


def test_recorder_correlation_id_property_matches_the_factory_id() -> None:
    factory = TurnDiagnosticsFactory(clock=_ticking_clock(), id_factory=lambda: "corr-20")
    recorder = factory.create()
    assert recorder.correlation_id == "corr-20"


def test_timeline_offsets_distinguish_each_observed_boundary() -> None:
    recorder = _recorder("timeline")
    recorder.begin_round()
    recorder.prepare_started()
    recorder.prepare_finished()
    recorder.request_started()
    recorder.request_handed_off()
    recorder.first_model_event()
    recorder.first_content_event()
    recorder.end_round()
    summary = recorder.finalize(TurnOutcome.SUCCESS)

    round_ = summary.rounds[0]
    assert round_.request_started_at_seconds == 3.0
    assert round_.request_acknowledged_at_seconds == 4.0
    assert round_.first_event_at_seconds == 5.0
    assert round_.first_content_at_seconds == 6.0
    fields = diagnostic_log_fields(summary)
    assert fields["round_1_request_started_at_seconds"] == 3.0
    assert fields["round_1_request_acknowledged_at_seconds"] == 4.0
    assert fields["round_1_first_event_at_seconds"] == 5.0
    assert fields["round_1_first_content_at_seconds"] == 6.0


# ---------------------------------------------------------------------------
# Immutable snapshot contents (design doc: no prompts, args, results, names)
# ---------------------------------------------------------------------------

_ALLOWED_TURN_FIELDS = {"correlation_id", "outcome", "total_seconds", "rounds", "tools"}
_ALLOWED_ROUND_FIELDS = {
    "round_number",
    "prepare_seconds",
    "handoff_seconds",
    "first_event_seconds",
    "request_started_at_seconds",
    "request_acknowledged_at_seconds",
    "first_event_at_seconds",
    "first_content_at_seconds",
    "total_seconds",
    "provider_metrics",
}
_ALLOWED_TOOL_FIELDS = {"sequence", "name", "seconds", "ok"}
_ALLOWED_METRICS_FIELDS = {
    "total_seconds",
    "load_seconds",
    "prompt_eval_seconds",
    "prompt_tokens",
    "generation_seconds",
    "generation_tokens",
}


def test_snapshot_dataclasses_expose_only_allowlisted_fields() -> None:
    assert {f.name for f in dataclasses.fields(TurnDiagnostics)} == _ALLOWED_TURN_FIELDS
    assert {f.name for f in dataclasses.fields(ProviderRoundDiagnostics)} == _ALLOWED_ROUND_FIELDS
    assert {f.name for f in dataclasses.fields(ToolDiagnostics)} == _ALLOWED_TOOL_FIELDS
    assert {f.name for f in dataclasses.fields(ProviderRuntimeMetrics)} == _ALLOWED_METRICS_FIELDS


def test_snapshot_dataclasses_are_frozen_and_slotted() -> None:
    for cls in (TurnDiagnostics, ProviderRoundDiagnostics, ToolDiagnostics, ProviderRuntimeMetrics):
        params = dataclasses.fields(cls)
        assert params, f"{cls} should declare fields"
        instance_dict = getattr(cls, "__dict__", {})
        assert "__slots__" in instance_dict, f"{cls} must be slotted"


def test_turn_diagnostics_is_immutable() -> None:
    summary = _recorder("corr-16").finalize(TurnOutcome.SUCCESS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        summary.outcome = TurnOutcome.FAILED  # type: ignore[misc]  # verifying frozen


def test_agent_phase_enum_members() -> None:
    assert {phase.value for phase in AgentPhase} == {
        "waiting for model",
        "running tool",
        "composing answer",
    }


# ---------------------------------------------------------------------------
# format_diagnostics
# ---------------------------------------------------------------------------


def _turn(
    *,
    rounds: tuple[ProviderRoundDiagnostics, ...],
    tools: tuple[ToolDiagnostics, ...] = (),
) -> TurnDiagnostics:
    return TurnDiagnostics(
        correlation_id="corr",
        outcome=TurnOutcome.SUCCESS,
        total_seconds=sum(r.total_seconds for r in rounds),
        rounds=rounds,
        tools=tools,
    )


def test_format_diagnostics_without_provider_metrics() -> None:
    summary = _turn(
        rounds=(
            ProviderRoundDiagnostics(1, 1.0, 1.0, 1.0, 100.0, None),
            ProviderRoundDiagnostics(2, 1.0, 1.0, 1.0, 42.0, None),
        ),
        tools=(ToolDiagnostics(sequence=1, name="list_pods", seconds=0.3, ok=True),),
    )

    assert format_diagnostics(summary) == "2 model rounds · model 142.0s · tools 0.3s"


def test_format_diagnostics_with_provider_metrics_matches_design_doc_example() -> None:
    metrics_a = ProviderRuntimeMetrics(
        total_seconds=60.0,
        prompt_eval_seconds=50.0,
        prompt_tokens=1800,
        generation_seconds=5.0,
        generation_tokens=20,
    )
    metrics_b = ProviderRuntimeMetrics(
        total_seconds=39.4,
        prompt_eval_seconds=38.5,
        prompt_tokens=1000,
        generation_seconds=5.9,
        generation_tokens=33,
    )
    summary = _turn(
        rounds=(
            ProviderRoundDiagnostics(1, 1.0, 1.0, 1.0, 100.0, metrics_a),
            ProviderRoundDiagnostics(2, 1.0, 1.0, 1.0, 42.0, metrics_b),
        ),
        tools=(ToolDiagnostics(sequence=1, name="list_pods", seconds=0.3, ok=True),),
    )

    assert format_diagnostics(summary) == (
        "2 model rounds · model 142.0s · tools 0.3s · ↑2.8k ↓53 tok · "
        "prompt 88.5s · generate 10.9s · other wait 42.6s"
    )


def test_format_diagnostics_one_metered_one_unmetered_round_other_wait_ignores_unmetered() -> None:
    """When one round has provider metrics and one does not, `other wait`
    must compare only the metered round's local wall time against its
    provider total.  The unmetered round's wall time must not inflate the
    `other wait` figure."""
    metered_metrics = ProviderRuntimeMetrics(
        total_seconds=30.0,
        prompt_eval_seconds=25.0,
        prompt_tokens=500,
        generation_seconds=5.0,
        generation_tokens=10,
    )
    summary = _turn(
        rounds=(
            ProviderRoundDiagnostics(1, 1.0, 1.0, 1.0, 50.0, metered_metrics),
            ProviderRoundDiagnostics(2, 1.0, 1.0, 1.0, 30.0, None),
        ),
    )

    # metered local: 50s, provider total: 30s → other_wait = 20s (not 50s)
    assert format_diagnostics(summary) == (
        "2 model rounds · model 80.0s · ↑500 ↓10 tok · "
        "prompt 25.0s · generate 5.0s · other wait 20.0s"
    )


def test_format_diagnostics_metrics_without_total_seconds_excluded_from_other_wait() -> None:
    """A `ProviderRuntimeMetrics` that omits `total_seconds` (= None) is
    treated as unmetered for the purpose of `other wait` — the round's local
    wall time is not included in the comparison."""
    partial_metrics = ProviderRuntimeMetrics(
        total_seconds=None,  # provider did not report total
        prompt_eval_seconds=10.0,
        prompt_tokens=300,
        generation_seconds=2.0,
        generation_tokens=15,
    )
    summary = _turn(
        rounds=(ProviderRoundDiagnostics(1, 1.0, 1.0, 1.0, 20.0, partial_metrics),),
    )

    # No provider total means other wait is unknown, not a measured zero.
    assert format_diagnostics(summary) == (
        "1 model round · model 20.0s · ↑300 ↓15 tok · prompt 10.0s · generate 2.0s"
    )


def test_other_wait_clamps_each_round_before_aggregating() -> None:
    summary = _turn(
        rounds=(
            ProviderRoundDiagnostics(
                1, 0.0, 0.0, 0.0, 5.0, ProviderRuntimeMetrics(total_seconds=10.0)
            ),
            ProviderRoundDiagnostics(
                2, 0.0, 0.0, 0.0, 20.0, ProviderRuntimeMetrics(total_seconds=10.0)
            ),
        )
    )
    assert format_diagnostics(summary) == "2 model rounds · model 25.0s · other wait 10.0s"


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        (ProviderRuntimeMetrics(prompt_tokens=3), "↑3 tok"),
        (ProviderRuntimeMetrics(generation_seconds=0.0), "generate 0.0s"),
        (ProviderRuntimeMetrics(total_seconds=4.0), "other wait 6.0s"),
        (ProviderRuntimeMetrics(load_seconds=2.0), "load 2.0s"),
    ],
)
def test_partial_metrics_render_only_reported_measurements(
    metrics: ProviderRuntimeMetrics, expected: str
) -> None:
    summary = _turn(rounds=(ProviderRoundDiagnostics(1, 1.0, 1.0, 1.0, 10.0, metrics),))
    assert format_diagnostics(summary) == f"1 model round · model 10.0s · {expected}"


def test_format_diagnostics_without_tools_omits_tools_segment() -> None:
    summary = _turn(rounds=(ProviderRoundDiagnostics(1, 1.0, 1.0, 1.0, 10.0, None),))

    assert format_diagnostics(summary) == "1 model round · model 10.0s"


def test_plain_log_handler_receives_correlated_structured_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    summary = _recorder("log-correlation").finalize(TurnOutcome.FAILED)
    with caplog.at_level(logging.INFO, logger="korvid.agent.diagnostics"):
        log_diagnostics(summary)
    payload = json.loads(caplog.records[-1].getMessage())
    assert payload == diagnostic_log_fields(summary)
    assert payload["correlation_id"] == "log-correlation"
    assert payload["outcome"] == "failed"


def test_format_diagnostics_with_no_rounds() -> None:
    summary = _turn(rounds=())

    assert format_diagnostics(summary) == "0 model rounds · model 0.0s"


def test_format_diagnostics_large_token_counts() -> None:
    metrics = ProviderRuntimeMetrics(
        total_seconds=1.0,
        prompt_eval_seconds=0.5,
        prompt_tokens=15_000,
        generation_seconds=0.5,
        generation_tokens=999,
    )
    summary = _turn(rounds=(ProviderRoundDiagnostics(1, 1.0, 1.0, 1.0, 1.0, metrics),))

    assert format_diagnostics(summary) == (
        "1 model round · model 1.0s · ↑15.0k ↓999 tok · "
        "prompt 0.5s · generate 0.5s · other wait 0.0s"
    )


# ---------------------------------------------------------------------------
# diagnostic_log_fields allowlist
# ---------------------------------------------------------------------------


def test_diagnostic_log_fields_allowlist_has_no_free_form_content() -> None:
    metrics = ProviderRuntimeMetrics(
        total_seconds=4.2,
        load_seconds=0.1,
        prompt_eval_seconds=3.0,
        prompt_tokens=200,
        generation_seconds=1.1,
        generation_tokens=40,
    )
    summary = _turn(
        rounds=(ProviderRoundDiagnostics(1, 1.0, 1.0, 1.0, 4.2, metrics),),
        tools=(ToolDiagnostics(sequence=1, name="list_pods", seconds=0.3, ok=True),),
    )

    fields = diagnostic_log_fields(summary)

    assert fields["correlation_id"] == "corr"
    assert fields["outcome"] == "success"
    assert fields["total_seconds"] == 4.2
    assert fields["round_count"] == 1
    assert fields["tool_count"] == 1
    assert fields["round_1_prepare_seconds"] == 1.0
    assert fields["round_1_provider_prompt_tokens"] == 200
    assert fields["tool_1_name"] == "list_pods"
    assert fields["tool_1_ok"] is True
    # Every value is a plain scalar the logging module can format safely —
    # never a dataclass, mapping, or other object requiring serialization.
    for value in fields.values():
        assert isinstance(value, (str, int, float, bool)) or value is None


# ---------------------------------------------------------------------------
# provider_metrics_from_event (provider-neutral parsing)
# ---------------------------------------------------------------------------


def test_provider_metrics_from_event_reads_every_numeric_field() -> None:
    metrics = provider_metrics_from_event(
        {
            "type": "provider_metrics",
            "total_seconds": 4.2,
            "load_seconds": 0.1,
            "prompt_eval_seconds": 3.0,
            "prompt_tokens": 200,
            "generation_seconds": 1.1,
            "generation_tokens": 40,
        }
    )

    assert metrics == ProviderRuntimeMetrics(
        total_seconds=4.2,
        load_seconds=0.1,
        prompt_eval_seconds=3.0,
        prompt_tokens=200,
        generation_seconds=1.1,
        generation_tokens=40,
    )


def test_provider_metrics_from_event_omits_absent_fields() -> None:
    metrics = provider_metrics_from_event(
        {"type": "provider_metrics", "prompt_tokens": 12, "generation_tokens": 3}
    )

    assert metrics == ProviderRuntimeMetrics(prompt_tokens=12, generation_tokens=3)
    assert metrics.total_seconds is None


def test_provider_metrics_from_event_rejects_negative_and_non_numeric() -> None:
    metrics = provider_metrics_from_event(
        {
            "type": "provider_metrics",
            "total_seconds": -1.0,  # negative duration is not a measurement
            "prompt_eval_seconds": "slow",  # non-numeric
            "prompt_tokens": -5,  # negative count
            "generation_tokens": True,  # bool is not a count
            "generation_seconds": 2.0,  # the one valid field survives
        }
    )

    assert metrics == ProviderRuntimeMetrics(generation_seconds=2.0)


@pytest.mark.parametrize(
    "value",
    [float("inf"), float("-inf"), float("nan"), 10**400],
    ids=["infinity", "negative-infinity", "nan", "overflow"],
)
def test_provider_metrics_reject_nonfinite_seconds_and_keep_strict_json(value: float | int) -> None:
    metrics = provider_metrics_from_event(
        {
            "total_seconds": value,
            "load_seconds": value,
            "prompt_eval_seconds": value,
            "generation_seconds": value,
            "generation_tokens": 20,
        }
    )
    assert metrics == ProviderRuntimeMetrics(generation_tokens=20)
    summary = _turn(rounds=(ProviderRoundDiagnostics(1, 0.0, 0.0, 0.0, 1.0, metrics),))
    assert (
        json.loads(json.dumps(diagnostic_log_fields(summary), allow_nan=False))[
            "round_1_provider_total_seconds"
        ]
        is None
    )


def test_provider_metrics_are_empty_detects_all_none() -> None:
    assert provider_metrics_are_empty(ProviderRuntimeMetrics())
    assert provider_metrics_are_empty(provider_metrics_from_event({"type": "provider_metrics"}))
    assert not provider_metrics_are_empty(ProviderRuntimeMetrics(prompt_tokens=0))


@pytest.mark.parametrize("count", [1_000_000_001, 10**400], ids=["over-limit", "huge-integer"])
def test_oversized_metric_counts_cannot_break_terminal_summary(count: int) -> None:
    metrics = provider_metrics_from_event(
        {"prompt_tokens": count, "generation_tokens": count, "total_seconds": 1.0}
    )
    assert metrics == ProviderRuntimeMetrics(total_seconds=1.0)
    summary = _turn(rounds=(ProviderRoundDiagnostics(1, 0.0, 0.0, 0.0, 1.0, metrics),))
    assert format_diagnostics(summary) == "1 model round · model 1.0s · other wait 0.0s"


def test_metric_counts_at_the_plugin_usage_ceiling_are_retained() -> None:
    metrics = provider_metrics_from_event({"prompt_tokens": 1_000_000_000, "generation_tokens": 0})
    assert metrics.prompt_tokens == 1_000_000_000
    assert metrics.generation_tokens == 0
