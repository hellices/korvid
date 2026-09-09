from korvid.agent.diagnostics import (
    AgentPhase,
    TurnDiagnostics,
    TurnOutcome,
)
from korvid.agent.events import (
    AgentError,
    AgentPhaseChanged,
    TextDelta,
    ToolCallFinished,
    TurnComplete,
    TurnInterrupted,
)


def test_events_are_frozen_and_typed() -> None:
    d = TextDelta(text="hi")
    assert d.text == "hi"
    t = TurnComplete(input_tokens=10, output_tokens=5, estimated=True)
    assert t.estimated is True
    f = ToolCallFinished(call_id="c1", name="get_logs", ok=False, summary="boom")
    assert not f.ok
    assert AgentError(message="x").message == "x"


def test_terminal_events_default_to_no_diagnostics() -> None:
    """A turn that is not recording diagnostics leaves the snapshot None, so
    existing constructions and equality stay unchanged (issue #319)."""
    assert TurnComplete(input_tokens=1, output_tokens=2, estimated=False).diagnostics is None
    assert TurnInterrupted(input_tokens=1, output_tokens=2, estimated=False).diagnostics is None
    assert AgentError(message="boom").diagnostics is None


def test_terminal_events_carry_the_diagnostic_snapshot() -> None:
    snapshot = TurnDiagnostics(
        correlation_id="corr",
        outcome=TurnOutcome.SUCCESS,
        total_seconds=1.0,
        rounds=(),
        tools=(),
    )
    event = TurnComplete(input_tokens=1, output_tokens=2, estimated=False, diagnostics=snapshot)
    assert event.diagnostics is snapshot


def test_phase_changed_carries_only_phase_round_and_tool() -> None:
    model_phase = AgentPhaseChanged(phase=AgentPhase.WAITING_FOR_MODEL, round_number=1)
    assert model_phase.tool is None
    tool_phase = AgentPhaseChanged(phase=AgentPhase.RUNNING_TOOL, round_number=1, tool="get_logs")
    assert tool_phase.tool == "get_logs"
    assert tool_phase.round_number == 1
