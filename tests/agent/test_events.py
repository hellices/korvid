from korvid.agent.events import (
    AgentError,
    TextDelta,
    ToolCallFinished,
    TurnComplete,
)


def test_events_are_frozen_and_typed() -> None:
    d = TextDelta(text="hi")
    assert d.text == "hi"
    t = TurnComplete(input_tokens=10, output_tokens=5, estimated=True)
    assert t.estimated is True
    f = ToolCallFinished(call_id="c1", name="get_logs", ok=False, summary="boom")
    assert not f.ok
    assert AgentError(message="x").message == "x"
