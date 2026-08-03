"""Agent turn interruption (issue #170): runtime semantics.

The app cancels the turn task; `finalize_interrupt` then repairs state
deterministically - the in-flight iteration's partial machinery never
enters model history, completed prior iterations stay, usage already
reported is committed, and a `TurnInterrupted` terminal event carries
the accounting to the panel.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from korvid.agent.events import TurnComplete, TurnInterrupted
from korvid.agent.runtime import AgentRuntime

from .test_runtime import EchoExecutor, ScriptedProvider


class StalledProvider:
    """Streams a little text, then stalls until cancelled."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    @property
    def name(self) -> str:
        return "stalled"

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, stream: bool = True
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {"type": "text_delta", "text": "thinking about"}
            await asyncio.Event().wait()  # stalls forever
        finally:
            self.closed.set()  # cancellation must unwind the stream promptly


class BlockingExecutor:
    """First call blocks until released; records what ran."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append(name)
        self.entered.set()
        await self.release.wait()
        return f"result-of-{name}"


async def _drive_until_cancelled(runtime: AgentRuntime, text: str) -> asyncio.Task[list[Any]]:
    async def drive() -> list[Any]:
        return [e async for e in runtime.run_turn(text, "view=pods")]

    return asyncio.create_task(drive())


async def test_interrupt_during_a_stalled_stream_closes_it_promptly() -> None:
    provider = StalledProvider()
    runtime = AgentRuntime(provider, EchoExecutor())
    task = await _drive_until_cancelled(runtime, "what is wrong?")
    await asyncio.sleep(0.05)  # let the stream start and stall
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.wait_for(provider.closed.wait(), timeout=5)


async def test_finalize_marks_partial_text_and_never_commits_it_as_complete() -> None:
    provider = StalledProvider()
    runtime = AgentRuntime(provider, EchoExecutor())
    task = await _drive_until_cancelled(runtime, "what is wrong?")
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    event = runtime.finalize_interrupt()
    assert isinstance(event, TurnInterrupted)
    last = runtime._messages[-1]
    assert last["role"] == "assistant"
    assert "interrupted" in str(last["content"])  # clearly marked
    assert "thinking about" in str(last["content"])  # bounded partial retained
    # the raw partial alone (a completed-looking answer) is never stored
    assert last["content"] != "thinking about"


async def test_interrupt_during_a_tool_call_leaves_history_consistent() -> None:
    """Cancelled mid-dispatch: the in-flight iteration (assistant msg with
    tool_calls + partial results) is truncated - no orphaned tool_call
    without its result, so the next request stays protocol-valid."""
    provider = ScriptedProvider(
        [
            [
                {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"},
                {"type": "done"},
            ],
        ]
    )
    executor = BlockingExecutor()
    runtime = AgentRuntime(provider, executor)
    task = await _drive_until_cancelled(runtime, "logs?")
    await asyncio.wait_for(executor.entered.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    runtime.finalize_interrupt()
    for message in runtime._messages:
        assert not message.get("tool_calls"), "incomplete tool call leaked into history"
        assert message.get("role") != "tool", "orphan tool result leaked into history"


async def test_completed_iterations_survive_the_interrupt() -> None:
    """Interrupted during iteration 2's stream: iteration 1's completed
    tool exchange stays in history (real work, valid pairs)."""
    stall = asyncio.Event()

    class TwoIterationProvider:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def name(self) -> str:
            return "two"

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            self.calls += 1
            if self.calls == 1:
                yield {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"}
                return
            stall.set()
            await asyncio.Event().wait()
            yield {"type": "done"}  # pragma: no cover - never reached

    runtime = AgentRuntime(TwoIterationProvider(), EchoExecutor())
    task = await _drive_until_cancelled(runtime, "logs?")
    await asyncio.wait_for(stall.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    runtime.finalize_interrupt()
    roles = [m["role"] for m in runtime._messages]
    assert "tool" in roles  # iteration 1's completed result kept
    tool_call_ids = {tc["id"] for m in runtime._messages for tc in (m.get("tool_calls") or [])}
    tool_result_ids = {m["tool_call_id"] for m in runtime._messages if m.get("role") == "tool"}
    assert tool_call_ids == tool_result_ids  # every call paired with a result


async def test_usage_reported_before_interruption_is_committed() -> None:
    """Provider-reported usage from the interrupted stream counts; the
    totals never lose real spend."""

    class UsageThenStall:
        @property
        def name(self) -> str:
            return "usage"

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "usage", "input_tokens": 70, "output_tokens": 9}
            yield {"type": "text_delta", "text": "partial"}
            await asyncio.Event().wait()
            yield {"type": "done"}  # pragma: no cover

    runtime = AgentRuntime(UsageThenStall(), EchoExecutor())
    task = await _drive_until_cancelled(runtime, "hi")
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    event = runtime.finalize_interrupt()
    assert event.input_tokens == 70
    assert event.output_tokens == 9
    assert event.estimated is False  # the provider reported real usage
    assert runtime.total_tokens == (70, 9)


async def test_partial_without_usage_is_estimated() -> None:
    provider = StalledProvider()
    runtime = AgentRuntime(provider, EchoExecutor())
    task = await _drive_until_cancelled(runtime, "hi")
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    event = runtime.finalize_interrupt()
    assert event.estimated is True
    assert event.input_tokens > 0  # the transmitted prompt is real cost


async def test_next_turn_runs_cleanly_after_an_interrupt() -> None:
    provider = StalledProvider()
    runtime = AgentRuntime(provider, EchoExecutor())
    task = await _drive_until_cancelled(runtime, "first")
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    runtime.finalize_interrupt()
    # swap the provider for a clean scripted answer
    runtime._provider = ScriptedProvider(
        [[{"type": "text_delta", "text": "answer"}, {"type": "done"}]]
    )
    events = [e async for e in runtime.run_turn("second", "view=pods")]
    assert isinstance(events[-1], TurnComplete)


async def test_finalize_without_an_active_turn_is_inert() -> None:
    runtime = AgentRuntime(
        ScriptedProvider([[{"type": "text_delta", "text": "hi"}, {"type": "done"}]]),
        EchoExecutor(),
    )
    events = [e async for e in runtime.run_turn("hello", "view=pods")]
    assert isinstance(events[-1], TurnComplete)
    before = list(runtime._messages)
    event = runtime.finalize_interrupt()
    assert event == TurnInterrupted(input_tokens=0, output_tokens=0, estimated=False)
    assert runtime._messages == before  # a completed turn is not repaired
