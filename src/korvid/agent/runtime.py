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
from korvid.agent.tools import READ_TOOLS, UI_TOOL_NAMES, cap_result

SYSTEM_PROMPT = (
    "You are korvid's Kubernetes diagnostic agent, embedded in a live TUI the "
    "user is looking at right now. "
    "Use tools to inspect cluster state, cite evidence from tool results, "
    "and never guess resource state. "
    "You have no write tools yet: when the user asks you to modify cluster "
    "state (scale, edit, delete, restart, apply), say write actions are not "
    "yet enabled in this agent and give the exact kubectl command they can "
    "run themselves instead."
)

# Appended only when the runtime is armed with the UI-control tools, so the
# model is never told about capabilities the provider was not offered.
UI_DRIVE_PROMPT = (
    "You can also drive the TUI itself: navigate (switch the resource view), "
    "set_filter (narrow the visible rows), open_logs (show a pod's live logs "
    "on screen), and open_describe (show a resource's manifest and events). "
    "Prefer showing evidence on screen with these tools while you narrate — "
    "for example, when you find a failing pod, open its logs or describe view "
    "so the user sees exactly what you see. These screen tools change nothing "
    "in the cluster. Keep your text concise; the screen carries the detail."
)

# History is trimmed to the most recent turns to bound token cost; a turn
# begins at a "user" message, so trimming never splits assistant/tool pairs.
MAX_HISTORY_TURNS = 8
# Character budget for retained history (~30k tokens at 4 chars/token).
# Turn count alone does not bound request size: one turn can hold up to
# max_iterations tool results of MAX_RESULT_CHARS each.
MAX_HISTORY_CHARS = 120_000


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


def _message_chars(message: dict[str, Any]) -> int:
    """Approximate a message's context cost: content plus tool-call arguments."""
    n = len(str(message.get("content") or ""))
    for tc in message.get("tool_calls") or []:
        n += len(str((tc.get("function") or {}).get("arguments") or ""))
    return n


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
        tools: list[dict[str, Any]] | None = None,
        max_iterations: int = 15,
        max_history_chars: int = MAX_HISTORY_CHARS,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._tools = tools if tools is not None else READ_TOOLS
        prompt = SYSTEM_PROMPT
        if any(t.get("function", {}).get("name") in UI_TOOL_NAMES for t in self._tools):
            prompt = f"{prompt} {UI_DRIVE_PROMPT}"
        self._max_iterations = max_iterations
        self._max_history_chars = max_history_chars
        self._messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
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
        """Keep the system prompt plus at most MAX_HISTORY_TURNS-1 recent turns,
        then drop oldest complete turns until within the character budget."""
        user_indices = [i for i, m in enumerate(self._messages) if m.get("role") == "user"]
        if len(user_indices) >= MAX_HISTORY_TURNS:
            cut = user_indices[-(MAX_HISTORY_TURNS - 1)]
            self._messages = [self._messages[0], *self._messages[cut:]]
        # Turn count alone does not bound request size (tool results are
        # capped per-result, not per-turn) — enforce a character budget,
        # always retaining at least the most recent complete turn.
        while sum(_message_chars(m) for m in self._messages) > self._max_history_chars:
            user_indices = [i for i, m in enumerate(self._messages) if m.get("role") == "user"]
            if len(user_indices) <= 1:
                break
            self._messages = [self._messages[0], *self._messages[user_indices[1] :]]

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
                    # Same ingest cap as ToolExecutor — a huge exception
                    # message must not bypass the limit into history.
                    result = cap_result(f"ERROR: {exc}")
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
        # Token counts are exact only when EVERY iteration reported usage;
        # one missing iteration makes the whole turn an estimate.
        usage_missing = False
        for _ in range(self._max_iterations):
            state = _StreamState()
            try:
                stream = self._provider.complete(self._messages, self._tools)
                async for event in self._consume_stream(stream, state):
                    yield event
            except Exception as exc:
                # Tokens spent in earlier iterations (and the partial stream)
                # are real cost — account for them before bailing out.
                self._total_in += turn_in + state.in_tok
                self._total_out += turn_out + state.out_tok
                if usage_missing or not state.has_usage:
                    self._estimated = True
                yield AgentError(message=str(exc))
                return

            if not state.has_usage:
                state.out_tok = len(state.text) // 4
            turn_in += state.in_tok
            turn_out += state.out_tok
            usage_missing = usage_missing or not state.has_usage

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
                self._estimated = self._estimated or usage_missing
                yield TurnComplete(
                    input_tokens=turn_in,
                    output_tokens=turn_out,
                    estimated=usage_missing,
                )
                return

            async for event in self._dispatch_tools(state.tool_calls):
                yield event

        self._total_in += turn_in
        self._total_out += turn_out
        self._estimated = self._estimated or usage_missing
        yield AgentError(
            message=(f"iteration limit reached ({self._max_iterations}) — refine the question")
        )
        yield TurnComplete(input_tokens=turn_in, output_tokens=turn_out, estimated=usage_missing)
