import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from korvid.agent.events import (
    AgentError,
    TextDelta,
    ToolCallFinished,
    TurnComplete,
    TurnInterrupted,
)
from korvid.agent.prompts import NO_WRITE_PROMPT
from korvid.agent.provider import REQUEST_SENT
from korvid.agent.runtime import AgentRuntime
from korvid.tools.executor import (
    MAX_RESULT_CHARS,
    READ_TOOLS,
    RecordedExecution,
)
from tests.agent.runtime_fakes import (
    EchoExecutor,
    RaisingExecutor,
    ScriptedProvider,
    collect,
)


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
    turn = [
        {"type": "tool_call", "id": "c", "name": "get_logs", "arguments": "{}"},
        {"type": "done"},
    ]
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


async def test_provider_error_with_empty_message_names_its_type() -> None:
    class EmptyMessageProvider(ScriptedProvider):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            raise TimeoutError()
            yield  # pragma: no cover

    events = await collect(
        AgentRuntime(EmptyMessageProvider([]), EchoExecutor()),
        "x",
    )
    error = next(event for event in events if isinstance(event, AgentError))
    assert error.message == "TimeoutError"


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


async def test_executor_exception_result_is_capped() -> None:
    """A defensive fallback with a huge message must respect the ingest cap."""

    class LoudExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            raise RuntimeError("x" * (MAX_RESULT_CHARS * 2))

    p = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"},
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
    from korvid.tools.executor import UI_TOOLS

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


async def test_runtime_accepts_profile_prompt_overrides() -> None:
    """A capability profile replaces the role statement and the UI-drive
    instruction; the conditional write/no-write clause still applies."""
    from korvid.tools.executor import UI_TOOLS

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


async def test_the_snapshot_is_available_before_the_first_event() -> None:
    """The handoff is the transport, not the call: a provider reading the
    inspector once it has acknowledged the request must already see it."""

    class _ObservingProvider:
        def __init__(self) -> None:
            self.runtime: AgentRuntime | None = None
            self.observed: list[Any] = []

        @property
        def name(self) -> str:
            return "observing"

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            assert self.runtime is not None
            yield {"type": REQUEST_SENT}
            self.observed.append(getattr(self.runtime, "latest_outbound_payload", None))
            yield {"type": "text_delta", "text": "ok"}
            yield {"type": "done"}

    provider = _ObservingProvider()
    runtime = AgentRuntime(provider, EchoExecutor())
    provider.runtime = runtime

    await collect(runtime, "hello")

    assert [snapshot.iteration for snapshot in provider.observed] == [1]
    assert runtime.latest_outbound_payload is provider.observed[0]


async def test_a_stream_that_dies_mid_flight_still_recorded_its_handoff() -> None:
    """The payload did leave — the failure came after. Keeping it is the
    point of the inspector."""

    class _DyingProvider:
        @property
        def name(self) -> str:
            return "dying"

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "text_delta", "text": "partial"}
            raise RuntimeError("connection reset")

    runtime = AgentRuntime(_DyingProvider(), EchoExecutor())

    await collect(runtime, "hello")

    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    assert "hello" in snapshot.payload_json


async def _cancel_mid_turn(runtime: AgentRuntime) -> TurnInterrupted:
    """Start a turn, cancel it once the provider is reached, and finalize."""

    async def drive() -> list[Any]:
        return [e async for e in runtime.run_turn("hello", "view=pods ns=default")]

    task = asyncio.create_task(drive())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return runtime.finalize_interrupt()


async def test_a_turn_cancelled_right_after_the_handoff_is_still_charged() -> None:
    """Nothing had streamed back yet, but the payload was on the wire."""

    class _AckThenHang:
        @property
        def name(self) -> str:
            return "hang"

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": REQUEST_SENT}
            await asyncio.sleep(60)

    runtime = AgentRuntime(_AckThenHang(), EchoExecutor())
    interrupted = await _cancel_mid_turn(runtime)

    snapshot = runtime.latest_outbound_payload
    assert snapshot is not None
    assert interrupted.input_tokens == len(snapshot.payload_json) // 4
    assert interrupted.estimated is True


async def test_a_turn_cancelled_before_the_handoff_is_charged_nothing() -> None:
    class _HangBeforeAck:
        @property
        def name(self) -> str:
            return "hang"

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            await asyncio.sleep(60)
            yield {"type": "done"}

    runtime = AgentRuntime(_HangBeforeAck(), EchoExecutor())
    interrupted = await _cancel_mid_turn(runtime)

    assert (interrupted.input_tokens, interrupted.output_tokens) == (0, 0)
    assert interrupted.estimated is False
    assert runtime.latest_outbound_payload is None
