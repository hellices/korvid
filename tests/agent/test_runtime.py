from collections.abc import AsyncIterator
from typing import Any

from korvid.agent.events import AgentError, TextDelta, ToolCallFinished, TurnComplete
from korvid.agent.runtime import MAX_HISTORY_TURNS, AgentRuntime


class ScriptedProvider:
    """Each call to complete() pops the next scripted event list."""

    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        self.turns = turns
        self.calls: list[list[dict[str, Any]]] = []

    @property
    def name(self) -> str:
        return "scripted"

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, stream: bool = True
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append([dict(m) for m in messages])
        for ev in self.turns.pop(0):
            yield ev


class EchoExecutor:
    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        return f"result-of-{name}"


async def collect(runtime: AgentRuntime, text: str) -> list[Any]:
    return [e async for e in runtime.run_turn(text, "view=pods ns=default")]


async def test_text_only_turn() -> None:
    p = ScriptedProvider([[{"type": "text_delta", "text": "hi"}, {"type": "done"}]])
    events = await collect(AgentRuntime(p, EchoExecutor()), "hello")
    assert events[0] == TextDelta(text="hi")
    assert isinstance(events[-1], TurnComplete)
    # system + user message present
    assert p.calls[0][0]["role"] == "system"
    assert "view=pods" in p.calls[0][1]["content"]


async def test_tool_call_roundtrip() -> None:
    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    events = await collect(AgentRuntime(p, EchoExecutor()), "logs?")
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["ToolCallStarted", "ToolCallFinished", "TextDelta", "TurnComplete"]
    # second provider call saw the tool result message
    roles = [m["role"] for m in p.calls[1]]
    assert roles[-1] == "tool"
    assert p.calls[1][-1]["content"] == "result-of-get_logs"


async def test_iteration_cap() -> None:
    turn = [{"type": "tool_call", "id": "c", "name": "t", "arguments": "{}"}, {"type": "done"}]
    p = ScriptedProvider([list(turn) for _ in range(20)])
    events = await collect(AgentRuntime(p, EchoExecutor(), max_iterations=3), "loop")
    errs = [e for e in events if isinstance(e, AgentError)]
    assert errs
    assert "iteration limit" in errs[0].message
    assert len(p.calls) == 3


async def test_provider_error_surfaces() -> None:
    class BadProvider(ScriptedProvider):
        async def complete(self, messages, tools, *, stream=True):  # type: ignore[no-untyped-def]
            raise RuntimeError("api down")
            yield  # pragma: no cover

    events = await collect(AgentRuntime(BadProvider([]), EchoExecutor()), "x")
    assert isinstance(events[0], AgentError)


async def test_history_persists_across_turns() -> None:
    p = ScriptedProvider(
        [
            [{"type": "text_delta", "text": "a"}, {"type": "done"}],
            [{"type": "text_delta", "text": "b"}, {"type": "done"}],
        ]
    )
    rt = AgentRuntime(p, EchoExecutor())
    await collect(rt, "first")
    await collect(rt, "second")
    contents = [m.get("content", "") for m in p.calls[1]]
    assert any("first" in c for c in contents)
    assert any(m["role"] == "assistant" for m in p.calls[1])


async def test_usage_accumulates() -> None:
    p = ScriptedProvider(
        [
            [
                {"type": "text_delta", "text": "x"},
                {"type": "usage", "input_tokens": 100, "output_tokens": 7},
                {"type": "done"},
            ]
        ]
    )
    rt = AgentRuntime(p, EchoExecutor())
    events = await collect(rt, "q")
    tc = next(e for e in events if isinstance(e, TurnComplete))
    assert (tc.input_tokens, tc.output_tokens, tc.estimated) == (100, 7, False)
    assert rt.total_tokens == (100, 7)


async def test_usage_accumulates_across_tool_iterations() -> None:
    """Usage from intermediate (tool-calling) iterations must count toward the turn."""
    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "t", "arguments": "{}"},
                {"type": "usage", "input_tokens": 50, "output_tokens": 5},
                {"type": "done"},
            ],
            [
                {"type": "text_delta", "text": "answer"},
                {"type": "usage", "input_tokens": 70, "output_tokens": 9},
                {"type": "done"},
            ],
        ]
    )
    rt = AgentRuntime(p, EchoExecutor())
    events = await collect(rt, "q")
    tc = next(e for e in events if isinstance(e, TurnComplete))
    assert (tc.input_tokens, tc.output_tokens, tc.estimated) == (120, 14, False)
    assert rt.total_tokens == (120, 14)


class RaisingExecutor:
    async def execute(self, name: str, arguments: dict[str, object]) -> str:
        raise RuntimeError("boom")


async def test_executor_exception_becomes_error_result() -> None:
    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_manifest", "arguments": "{}"},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "ok"}, {"type": "done"}],
        ]
    )
    rt = AgentRuntime(p, RaisingExecutor())
    events = await collect(rt, "q")
    fin = next(e for e in events if isinstance(e, ToolCallFinished))
    assert fin.ok is False
    assert "boom" in fin.summary
    assert not any(isinstance(e, AgentError) for e in events)


async def test_history_trimmed_to_recent_turns() -> None:
    text_turn = [{"type": "text_delta", "text": "ok"}, {"type": "done"}]
    p = ScriptedProvider([list(text_turn) for _ in range(12)])
    runtime = AgentRuntime(p, EchoExecutor())
    for i in range(12):
        _ = await collect(runtime, f"question {i}")
    last_call = p.calls[-1]
    user_msgs = [m for m in last_call if m["role"] == "user"]
    assert len(user_msgs) <= MAX_HISTORY_TURNS
    assert last_call[0]["role"] == "system"
    # oldest turns dropped, newest kept
    assert "question 0" not in str(last_call)
    assert "question 11" in str(last_call)


async def test_provider_error_still_accounts_usage() -> None:
    tool_turn: list[dict[str, Any]] = [
        {"type": "usage", "input_tokens": 40, "output_tokens": 7},
        {"type": "tool_call", "id": "c", "name": "t", "arguments": "{}"},
        {"type": "done"},
    ]

    class FailsSecondCall(ScriptedProvider):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            self.calls.append([dict(m) for m in messages])
            if len(self.calls) > 1:
                raise RuntimeError("api down")
            for ev in tool_turn:
                yield ev

    runtime = AgentRuntime(FailsSecondCall([]), EchoExecutor())
    events = await collect(runtime, "q")
    assert any(isinstance(e, AgentError) for e in events)
    assert runtime.total_tokens == (40, 7)


async def test_usage_estimated_is_sticky() -> None:
    no_usage: list[dict[str, Any]] = [{"type": "text_delta", "text": "hi"}, {"type": "done"}]
    with_usage: list[dict[str, Any]] = [
        {"type": "text_delta", "text": "hi"},
        {"type": "usage", "input_tokens": 5, "output_tokens": 2},
        {"type": "done"},
    ]
    runtime = AgentRuntime(ScriptedProvider([no_usage, with_usage]), EchoExecutor())
    assert runtime.usage_estimated is False
    _ = await collect(runtime, "first")
    assert runtime.usage_estimated is True
    _ = await collect(runtime, "second")
    assert runtime.usage_estimated is True  # sticky: earlier totals remain estimates


async def test_usage_missing_in_any_iteration_marks_estimated() -> None:
    """Exactness requires usage from EVERY iteration, not just one."""
    p = ScriptedProvider(
        [
            [
                # tool-calling iteration omits usage …
                {"type": "tool_call", "id": "c1", "name": "t", "arguments": "{}"},
                {"type": "done"},
            ],
            [
                # … final iteration reports it — turn is still an estimate.
                {"type": "text_delta", "text": "answer"},
                {"type": "usage", "input_tokens": 70, "output_tokens": 9},
                {"type": "done"},
            ],
        ]
    )
    rt = AgentRuntime(p, EchoExecutor())
    events = await collect(rt, "q")
    tc = next(e for e in events if isinstance(e, TurnComplete))
    assert tc.estimated is True
    assert rt.usage_estimated is True
