from collections.abc import AsyncIterator
from typing import Any

from korvid.agent.events import AgentError, TextDelta, ToolCallFinished, TurnComplete
from korvid.agent.runtime import MAX_HISTORY_TURNS, NO_WRITE_PROMPT, AgentRuntime


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


async def test_tool_call_with_non_mapping_arguments_is_rejected() -> None:
    """Valid JSON that is not an argument mapping never reaches the executor."""
    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "[]"},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    events = await collect(AgentRuntime(p, EchoExecutor()), "logs?")
    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert not finished.ok
    assert finished.summary == "ERROR: bad arguments"


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
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
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


async def test_provider_error_estimates_streamed_text_without_usage() -> None:
    """A stream that dies after emitting text but before its usage event
    still cost output tokens — the exception path must apply the same
    estimate as the normal path, not record zero."""

    class DiesMidStream(ScriptedProvider):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "text_delta", "text": "x" * 40}
            raise RuntimeError("connection dropped")

    runtime = AgentRuntime(DiesMidStream([]), EchoExecutor())
    events = await collect(runtime, "q")
    assert any(isinstance(e, AgentError) for e in events)
    in_tok, out_tok = runtime.total_tokens
    assert out_tok == 10
    assert in_tok > 0  # the prompt was really sent — estimated, not zero
    assert runtime.usage_estimated is True


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


async def test_history_trimmed_by_char_budget() -> None:
    """A few huge turns must not blow the request size even under the turn cap."""
    big = "x" * 4000
    p = ScriptedProvider(
        [[{"type": "text_delta", "text": big}, {"type": "done"}] for _ in range(4)]
    )
    rt = AgentRuntime(p, EchoExecutor(), max_history_chars=10_000)
    for i in range(4):
        await collect(rt, f"question-{i} {big}")
    # The last provider call must fit the budget…
    last_call_chars = sum(len(str(m.get("content") or "")) for m in p.calls[-1])
    assert last_call_chars <= 10_000 + len(big) + 100  # budget + newest user msg
    # …and the newest turn is always retained.
    assert any("question-3" in str(m.get("content") or "") for m in p.calls[-1])
    assert not any("question-0" in str(m.get("content") or "") for m in p.calls[-1])


async def test_executor_exception_result_is_capped() -> None:
    """A defensive fallback with a huge message must respect the ingest cap."""
    from korvid.agent.tools import MAX_RESULT_CHARS

    class LoudExecutor:
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            raise RuntimeError("x" * (MAX_RESULT_CHARS * 2))

    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "t", "arguments": "{}"},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "ok"}, {"type": "done"}],
        ]
    )
    rt = AgentRuntime(p, LoudExecutor())
    await collect(rt, "q")
    tool_msg = next(m for m in p.calls[-1] if m.get("role") == "tool")
    assert len(tool_msg["content"]) <= MAX_RESULT_CHARS + 50
    assert tool_msg["content"].startswith("ERROR:")


async def test_runtime_passes_injected_tools_to_provider() -> None:
    """Slice 3: composition root injects READ_TOOLS + UI_TOOLS."""

    seen: list[list[dict[str, Any]]] = []

    class ToolSpyProvider(ScriptedProvider):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            seen.append(tools)
            async for ev in super().complete(messages, tools, stream=stream):
                yield ev

    custom = [{"type": "function", "function": {"name": "navigate", "parameters": {}}}]
    p = ToolSpyProvider([[{"type": "text_delta", "text": "ok"}, {"type": "done"}]])
    await collect(AgentRuntime(p, EchoExecutor(), tools=custom), "go")
    assert seen == [custom]


async def test_runtime_defaults_to_read_tools() -> None:
    from korvid.agent.tools import READ_TOOLS

    seen: list[list[dict[str, Any]]] = []

    class ToolSpyProvider(ScriptedProvider):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            seen.append(tools)
            async for ev in super().complete(messages, tools, stream=stream):
                yield ev

    p = ToolSpyProvider([[{"type": "text_delta", "text": "ok"}, {"type": "done"}]])
    await collect(AgentRuntime(p, EchoExecutor()), "go")
    assert seen == [READ_TOOLS]


async def test_system_prompt_omits_ui_driving_without_ui_tools() -> None:
    """Read-only runtimes must not advertise UI tools the provider never saw."""
    p = ScriptedProvider([[{"type": "text_delta", "text": "ok"}, {"type": "done"}]])
    await collect(AgentRuntime(p, EchoExecutor()), "go")
    system = p.calls[0][0]["content"]
    assert "navigate" not in system
    assert "open_logs" not in system


async def test_system_prompt_advertises_ui_driving_with_ui_tools() -> None:
    from korvid.agent.tools import READ_TOOLS, UI_TOOLS

    p = ScriptedProvider([[{"type": "text_delta", "text": "ok"}, {"type": "done"}]])
    await collect(AgentRuntime(p, EchoExecutor(), tools=READ_TOOLS + UI_TOOLS), "go")
    system = p.calls[0][0]["content"]
    for tool in ("navigate", "set_filter", "open_logs", "open_describe", "drill_down"):
        assert tool in system


def test_system_prompt_redirects_write_requests_to_kubectl() -> None:
    """Without write tools armed, instead of a bare refusal the agent must
    offer the exact kubectl command the user can run."""
    assert "kubectl" in NO_WRITE_PROMPT
    assert "write" in NO_WRITE_PROMPT.lower() or "modify" in NO_WRITE_PROMPT.lower()


async def test_cluster_context_appended_to_system_prompt() -> None:
    """A detected-provider note lands in the system message (issue #30)."""
    p = ScriptedProvider([[{"type": "text_delta", "text": "hi"}, {"type": "done"}]])
    rt = AgentRuntime(p, EchoExecutor(), cluster_context="The cluster runs on Azure (AKS).")
    await collect(rt, "hello")
    system = p.calls[0][0]
    assert system["role"] == "system"
    assert "The cluster runs on Azure (AKS)." in system["content"]


async def test_no_cluster_context_leaves_prompt_unchanged() -> None:
    p = ScriptedProvider([[{"type": "text_delta", "text": "hi"}, {"type": "done"}]])
    rt = AgentRuntime(p, EchoExecutor())
    await collect(rt, "hello")
    assert "cluster runs on" not in p.calls[0][0]["content"]


async def test_missing_usage_estimates_prompt_tokens_too() -> None:
    """A provider that omits usage must not record zero input tokens: the
    prompt was really sent, so both directions get the heuristic estimate."""
    no_usage: list[dict[str, Any]] = [
        {"type": "text_delta", "text": "a diagnosis long enough to estimate"},
        {"type": "done"},
    ]
    runtime = AgentRuntime(ScriptedProvider([no_usage]), EchoExecutor())
    _ = await collect(runtime, "a question long enough to estimate")
    in_tok, out_tok = runtime.total_tokens
    assert in_tok > 0
    assert out_tok > 0
    assert runtime.usage_estimated is True


async def test_missing_usage_estimates_include_tool_schemas_and_payloads() -> None:
    """Prompt estimates must cover the transmitted tool schemas, and output
    estimates the generated tool-call payload — a tool-only iteration is
    not free just because the provider omitted usage."""
    import json

    from korvid.agent.tools import READ_TOOLS

    args = json.dumps({"pod": "checkout-1", "namespace": "shop", "container": "app"})
    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": args},
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(p, EchoExecutor())
    _ = await collect(runtime, "logs?")
    in_tok, out_tok = runtime.total_tokens
    # The tool-call name + JSON arguments dominate the tiny "done" text.
    assert out_tok >= (len("get_logs") + len(args)) // 4
    # A content-only estimate is far below the serialized schema cost that
    # every request really transmits.
    assert in_tok > len(json.dumps(READ_TOOLS)) // 4
    assert runtime.usage_estimated is True


async def test_provider_error_after_tool_call_estimates_output() -> None:
    """A stream that dies after emitting only a tool call (no text) still
    generated output — its payload is charged, not recorded as zero."""

    class DiesAfterToolCall(ScriptedProvider):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {
                "type": "tool_call",
                "id": "c1",
                "name": "get_logs",
                "arguments": '{"pod": "checkout-1", "namespace": "shop"}',
            }
            raise RuntimeError("connection dropped")

    runtime = AgentRuntime(DiesAfterToolCall([]), EchoExecutor())
    events = await collect(runtime, "q")
    assert any(isinstance(e, AgentError) for e in events)
    in_tok, out_tok = runtime.total_tokens
    assert out_tok > 0
    assert in_tok > 0
    assert runtime.usage_estimated is True


async def test_runtime_accepts_profile_prompt_overrides() -> None:
    """A capability profile replaces the role statement and the UI-drive
    instruction; the conditional write/no-write clause still applies."""
    from korvid.agent.tools import UI_TOOLS

    open_logs = next(t for t in UI_TOOLS if t["function"]["name"] == "open_logs")
    p = ScriptedProvider([[{"type": "text_delta", "text": "hi"}, {"type": "done"}]])
    runtime = AgentRuntime(
        p,
        EchoExecutor(),
        tools=[open_logs],
        system_prompt="CUSTOM ROLE.",
        ui_prompt="CUSTOM UI RULES.",
    )
    _ = await collect(runtime, "q")
    system = p.calls[0][0]
    assert system["role"] == "system"
    assert system["content"].startswith("CUSTOM ROLE.")
    assert "CUSTOM UI RULES." in system["content"]
    assert NO_WRITE_PROMPT in system["content"]
