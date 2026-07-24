"""AgentRuntime: the agentic tool-use loop (design §6.1)."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from korvid.agent.events import (
    AgentError,
    AgentEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
)
from korvid.agent.tools import READ_TOOLS

SYSTEM_PROMPT = (
    "You are korvid's Kubernetes diagnostic agent. "
    "Use tools to inspect cluster state, cite evidence from tool results, "
    "and never guess resource state."
)

# History is trimmed to the most recent turns to bound token cost; a turn
# begins at a "user" message, so trimming never splits assistant/tool pairs.
MAX_HISTORY_TURNS = 8


class _Provider(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]: ...


class _Executor(Protocol):
    async def execute(self, name: str, arguments: dict[str, Any]) -> str: ...


@dataclass
class _StreamState:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    in_tok: int = 0
    out_tok: int = 0
    has_usage: bool = False


class AgentRuntime:
    """Drives the provider + tools loop, emitting typed AgentEvent objects."""

    def __init__(
        self,
        provider: _Provider,
        executor: _Executor,
        *,
        max_iterations: int = 15,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._max_iterations = max_iterations
        self._messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._total_in = 0
        self._total_out = 0
        self._estimated = False

    @property
    def total_tokens(self) -> tuple[int, int]:
        """Cumulative (input, output) token counts across all completed turns."""
        return (self._total_in, self._total_out)

    @property
    def usage_estimated(self) -> bool:
        """True if any counted turn lacked provider usage (totals are estimates)."""
        return self._estimated

    def _trim_history(self) -> None:
        """Keep the system prompt plus at most MAX_HISTORY_TURNS-1 recent turns."""
        user_indices = [i for i, m in enumerate(self._messages) if m.get("role") == "user"]
        if len(user_indices) < MAX_HISTORY_TURNS:
            return
        cut = user_indices[-(MAX_HISTORY_TURNS - 1)]
        self._messages = [self._messages[0], *self._messages[cut:]]

    async def _consume_stream(
        self,
        stream: AsyncIterator[dict[str, Any]],
        state: _StreamState,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Iterate provider stream, yield TextDelta events, accumulate state."""
        async for ev in stream:
            ev_type = ev.get("type", "")
            if ev_type == "text_delta":
                text = str(ev.get("text", ""))
                state.text += text
                yield TextDelta(text=text)
            elif ev_type == "tool_call":
                state.tool_calls.append(
                    {
                        "id": str(ev.get("id", "")),
                        "name": str(ev.get("name", "")),
                        "arguments": str(ev.get("arguments", "")),
                    }
                )
            elif ev_type == "usage":
                state.in_tok += int(ev.get("input_tokens", 0))
                state.out_tok += int(ev.get("output_tokens", 0))
                state.has_usage = True

    async def _dispatch_tools(
        self,
        tool_calls: list[dict[str, Any]],
    ) -> AsyncGenerator[AgentEvent, None]:
        """Execute each tool call; yield Started/Finished events; append results."""
        for tc in tool_calls:
            call_id = str(tc["id"])
            name = str(tc["name"])
            arguments = str(tc["arguments"])
            yield ToolCallStarted(call_id=call_id, name=name, arguments=arguments)
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                result = "ERROR: bad arguments"
            else:
                try:
                    result = await self._executor.execute(name, parsed)
                except Exception as exc:  # defensive: executor contract is never-raise
                    result = f"ERROR: {exc}"
            yield ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=not result.startswith("ERROR:"),
                summary=result[:120],
            )
            self._messages.append({"role": "tool", "tool_call_id": call_id, "content": result})

    async def run_turn(
        self,
        user_text: str,
        screen_context: str,
    ) -> AsyncIterator[AgentEvent]:
        """Async generator: run one conversation turn, yielding events until done."""
        self._trim_history()
        self._messages.append(
            {"role": "user", "content": f"[screen] {screen_context}\n\n{user_text}"}
        )

        turn_in = 0
        turn_out = 0
        saw_usage = False
        for _ in range(self._max_iterations):
            state = _StreamState()
            try:
                stream = self._provider.complete(self._messages, READ_TOOLS)
                async for event in self._consume_stream(stream, state):
                    yield event
            except Exception as exc:
                # Tokens spent in earlier iterations (and the partial stream)
                # are real cost — account for them before bailing out.
                self._total_in += turn_in + state.in_tok
                self._total_out += turn_out + state.out_tok
                if not (saw_usage or state.has_usage):
                    self._estimated = True
                yield AgentError(message=str(exc))
                return

            if not state.has_usage:
                state.out_tok = len(state.text) // 4
            turn_in += state.in_tok
            turn_out += state.out_tok
            saw_usage = saw_usage or state.has_usage

            assistant_msg: dict[str, Any] = {"role": "assistant", "content": state.text}
            if state.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for tc in state.tool_calls
                ]
            self._messages.append(assistant_msg)

            if not state.tool_calls:
                self._total_in += turn_in
                self._total_out += turn_out
                self._estimated = self._estimated or not saw_usage
                yield TurnComplete(
                    input_tokens=turn_in,
                    output_tokens=turn_out,
                    estimated=not saw_usage,
                )
                return

            async for event in self._dispatch_tools(state.tool_calls):
                yield event

        self._total_in += turn_in
        self._total_out += turn_out
        self._estimated = self._estimated or not saw_usage
        yield AgentError(
            message=(f"iteration limit reached ({self._max_iterations}) — refine the question")
        )
        yield TurnComplete(input_tokens=turn_in, output_tokens=turn_out, estimated=not saw_usage)
