"""AgentRuntime: the agentic tool-use loop (design §6.1)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from korvid.agent.events import (
    AgentError,
    AgentEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
    TurnInterrupted,
)
from korvid.agent.outbound import (
    OutboundPolicy,
    OutboundPolicyError,
    OutboundRequestTooLarge,
    OutboundSnapshot,
    PreparedOutbound,
    ToolResultBlockedError,
    provider_prepared_messages,
    request_char_budget,
    sanitize_screen_context,
    sanitize_tool_result,
)
from korvid.agent.prompts import compose_system_prompt
from korvid.core.redaction import RedactionRecord, merge_records, rebase
from korvid.tools.executor import (
    READ_TOOLS,
    ToolOutcome,
    ToolResultBlocked,
    cap_result,
)

logger = logging.getLogger(__name__)

# History is trimmed to the most recent turns to bound token cost; a turn
# begins at a "user" message, so trimming never splits assistant/tool pairs.
MAX_HISTORY_TURNS = 8
# Character budget for retained history (~30k tokens at 4 chars/token).
# Turn count alone does not bound request size: one turn can hold up to
# max_iterations tool results of MAX_RESULT_CHARS each.
MAX_HISTORY_CHARS = 120_000

#: Bound for the marked partial text an interrupted turn records (issue
#: #170): enough for the model to understand what it was saying, small
#: enough that interrupting cannot bloat history.
INTERRUPT_PARTIAL_CHARS = 2_000


class _Provider(Protocol):
    @property
    def name(self) -> str: ...

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]: ...


class _Executor(Protocol):
    async def execute(self, name: str, arguments: dict[str, Any]) -> str: ...


@dataclass(frozen=True, slots=True)
class _IngressRecords:
    """Redactions applied to one message, tied to that message."""

    message: dict[str, Any]
    records: tuple[RedactionRecord, ...]


@runtime_checkable
class _RecordingExecutor(Protocol):
    """An executor that also reports what it redacted while producing a result.

    Optional: the string `execute` remains the contract every executor
    must satisfy, so a fake or a third-party executor keeps working and
    simply contributes no producer records.
    """

    async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome: ...


def _message_chars(message: dict[str, Any]) -> int:
    """Approximate a message's context cost: content plus tool-call arguments."""
    n = len(str(message.get("content") or ""))
    for tc in message.get("tool_calls") or []:
        n += len(str((tc.get("function") or {}).get("arguments") or ""))
    return n


def _stream_output_chars(state: _StreamState) -> int:
    """Approximate one iteration's generated output: streamed text plus the
    structured tool-call payloads — a tool-only iteration is not free."""
    n = len(state.text)
    for tc in state.tool_calls:
        n += len(tc["name"]) + len(tc["arguments"])
    return n


@dataclass
class _StreamState:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    in_tok: int = 0
    out_tok: int = 0
    has_usage: bool = False


def _estimate_missing_usage(state: _StreamState, prompt_estimate: int) -> None:
    """Fill in token estimates when the provider omitted usage — totals must
    never read as zero for a request that was really transmitted."""
    if not state.has_usage:
        state.in_tok = prompt_estimate
        state.out_tok = _stream_output_chars(state) // 4


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
        max_result_chars: int | None = None,
        max_tool_calls_per_iteration: int | None = None,
        strict_history_budget: bool = False,
        max_request_chars: int | None = None,
        cluster_context: str | None = None,
        system_prompt: str | None = None,
        ui_prompt: str | None = None,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._tools = tools if tools is not None else READ_TOOLS
        self._latest_outbound_payload: OutboundSnapshot | None = None
        # The serialized tool schemas ride along on every request; they are
        # part of the prompt cost when a provider omits usage.
        self._tools_chars = len(json.dumps(self._tools))
        # The outbound ceiling is a safety net above the history budget,
        # not a second copy of it: the same conversation serializes larger
        # than its message characters (envelopes, escaping, tool schemas),
        # so equating the two blocks conversations the history budget
        # already accepted. Overridable for tests and future tuning.
        self._max_request_chars = max_request_chars
        self._outbound = self._build_policy(max_history_chars)
        # Remembered for retarget(): a `:ctx` switch recomposes the system
        # prompt and must keep the active profile's role statement and
        # UI-drive instruction (issue #71), not reset them to the defaults.
        self._system_prompt_override = system_prompt
        self._ui_prompt_override = ui_prompt
        prompt = compose_system_prompt(
            self._tools,
            cluster_context,
            system_prompt=system_prompt,
            ui_prompt=ui_prompt,
        )
        self._max_iterations = max_iterations
        self._max_history_chars = max_history_chars
        # Optional per-result cap below the executor's own ingest limit —
        # history trimming never removes the sole most-recent turn, so the
        # small profile (issue #71) sizes this so one full turn of results
        # fits inside its retained-history budget.
        self._max_result_chars = max_result_chars
        # The small profile's size bound assumes one result per iteration;
        # prompt text alone does not enforce that, so extra parallel calls
        # in one response are discarded at dispatch (issue #71).
        self._max_tool_calls_per_iteration = max_tool_calls_per_iteration
        # Opt-in hard bound (small profile): budget checked mid-turn and
        # oversized completed turns dropped at trim time. Off by default so
        # the full profile keeps the pre-profile runtime behavior exactly.
        self._strict_history_budget = strict_history_budget
        self._messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
        # Redactions applied to screen text and tool results *before* they
        # entered history, keyed by the exact sanitized content they were
        # applied to. The outbound policy re-derives what it can still
        # see, but a redaction that removed its evidence rather than
        # masking it leaves nothing to re-derive, so the inspector would
        # show a payload that looks untouched. Keyed by the identity of
        # the message it describes: content is not an identifier, and two
        # messages that sanitize to the same text are still two messages
        # — one of which may never have been redacted at all.
        self._ingress_records: dict[int, _IngressRecords] = {}
        self._total_in = 0
        self._total_out = 0
        self._estimated = False
        # Interruption bookkeeping (issue #170): run_turn keeps these
        # current so finalize_interrupt can repair state after the driving
        # task was cancelled at an arbitrary await point.
        self._turn_active = False
        self._iteration_base = 0
        self._live_state: _StreamState | None = None
        self._live_prompt_estimate = 0
        self._turn_in = 0
        self._turn_out = 0
        self._turn_usage_missing = False
        # Index of the first message of the turn in flight; kept current
        # so a rollback deletes exactly that turn even after recovery
        # trimming shifted everything down.
        self._turn_base = len(self._messages)

    def _build_policy(self, max_history_chars: int) -> OutboundPolicy:
        limit = self._max_request_chars
        if limit is None:
            limit = request_char_budget(
                max_history_chars=max_history_chars,
                tools_chars=self._tools_chars,
            )
        return OutboundPolicy(max_request_chars=limit)

    def retarget(self, *, tools: list[dict[str, Any]], cluster_context: str | None) -> None:
        """Re-arm the runtime for a new cluster (issue #36, `:ctx`).

        Conversation history survives — the system prompt is recomposed in
        place so later turns describe the new environment (cloud provider
        note) and the new capability-gated tool set (e.g. ``resize_pod``),
        instead of the cluster the runtime was originally built against.
        """
        self._tools = tools
        # Keep the omitted-usage estimate honest for the new tool set.
        self._tools_chars = len(json.dumps(self._tools))
        # A different tool surface is a different per-request overhead.
        self._outbound = self._build_policy(self._max_history_chars)
        self._messages[0] = {
            "role": "system",
            "content": compose_system_prompt(
                tools,
                cluster_context,
                system_prompt=self._system_prompt_override,
                ui_prompt=self._ui_prompt_override,
            ),
        }

    @property
    def total_tokens(self) -> tuple[int, int]:
        """Cumulative (input, output) token counts across all completed turns."""
        return (self._total_in, self._total_out)

    @property
    def usage_estimated(self) -> bool:
        """True if any counted turn lacked provider usage (totals are estimates)."""
        return self._estimated

    @property
    def latest_outbound_payload(self) -> OutboundSnapshot | None:
        """The exact redacted payload of the latest request handed to the provider.

        It survives a later blocked or rolled-back turn: such a turn sends
        nothing, so it has no payload of its own, and erasing the previous
        one would destroy the record of what actually left the machine.
        Only a newly prepared request replaces it.
        """
        return self._latest_outbound_payload

    def _remember_ingress(self, message: dict[str, Any], records: list[RedactionRecord]) -> None:
        """Keep the redactions applied to one message's content on the way in.

        The entry holds the message itself, which both identifies it and
        keeps it alive: an id whose object had been freed could be handed
        to a later allocation and silently adopt someone else's records.
        It retains nothing history does not already hold — the content is
        the sanitized text that is in the payload.
        """
        if records:
            self._ingress_records[id(message)] = _IngressRecords(message, tuple(records))

    def _ingress_by_index(self) -> dict[int, tuple[RedactionRecord, ...]]:
        """Project the store onto the positions the policy will see.

        The policy works on a list, so identity has to become position
        exactly once, here, against the same history the request is built
        from.
        """
        projected: dict[int, tuple[RedactionRecord, ...]] = {}
        for index, message in enumerate(self._messages):
            entry = self._ingress_records.get(id(message))
            if entry is not None and entry.message is message:
                projected[index] = entry.records
        return projected

    async def _execute_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[str, tuple[RedactionRecord, ...]]:
        """Run one tool, taking its producer redaction trail when it offers one.

        Producer-side redaction happens before the size bound, so it is
        the only pass that can report what it *removed* rather than
        masked. Executors that satisfy only the string contract simply
        contribute nothing here.
        """
        executor = self._executor
        if isinstance(executor, _RecordingExecutor):
            try:
                outcome = await executor.execute_recorded(name, arguments)
            except ToolResultBlocked as exc:
                # Not an answer the model can react to: the redactor could
                # not promise this document holds no credentials, so the
                # turn stops here rather than sending another request with
                # an unvetted result in history (PR #197 review). Reusing
                # the outbound block means one rollback path — history
                # truncated to the turn base, records purged, the last
                # successful snapshot left standing.
                raise ToolResultBlockedError(str(exc)) from exc
            return outcome.text, outcome.redactions
        return await executor.execute(name, arguments), ()

    def _truncate_history(self, start: int) -> None:
        """Drop history from `start` on, and the records that described it.

        Every path in the inventory has to resolve against the payload
        the user is shown, so records outlive their message by exactly
        nothing.
        """
        del self._messages[start:]
        self._forget_dropped_ingress_records()

    def _forget_dropped_ingress_records(self) -> None:
        """Drop records for content no longer in history.

        Their message is gone from the payload, so reporting them would
        name a path nobody can find.
        """
        if not self._ingress_records:
            return
        live = {id(message) for message in self._messages}
        self._ingress_records = {
            identity: entry for identity, entry in self._ingress_records.items() if identity in live
        }

    def _trim_history(self) -> None:
        """Keep the system prompt plus at most MAX_HISTORY_TURNS-1 recent turns,
        then drop oldest complete turns until within the character budget."""
        before = len(self._messages)
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
        if len(self._messages) < before:
            removed = before - len(self._messages)
            self._shift_turn_bases(removed)
            self._forget_dropped_ingress_records()
            # Dropped context makes the agent "forget" earlier exchanges;
            # leave a trace so such reports are debuggable.
            logger.info(
                "trimmed agent history: dropped %d message(s), %d retained (budget %d chars)",
                removed,
                len(self._messages),
                self._max_history_chars,
            )

    def _shift_turn_bases(self, removed: int) -> None:
        """Keep the in-flight turn's indices on the same messages.

        Trimming and recovery both drop history from *in front of* the
        current turn; without this the rollback slice would point past the
        turn it must delete and leave a rejected prompt in history.
        """
        self._turn_base = max(1, self._turn_base - removed)
        self._iteration_base = max(1, self._iteration_base - removed)

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
        """Execute the kept tool calls; yield Started/Finished events; append results.

        Excess parallel calls (beyond `max_tool_calls_per_iteration`) are
        discarded entirely: they are never executed and never stored — not
        even a refusal message — because retaining their arguments would
        let a parallel-call-happy model grow history past the profile
        budget mid-turn (trimming never drops the newest turn). The
        assistant message stores only the kept calls (see `run_turn`), so
        the provider protocol stays valid with one tool message per kept
        call. The model learns the rule from a fixed-size notice appended
        to the last kept result; the UI still sees a Finished(ok=False)
        event per discarded call.
        """
        call_limit = self._max_tool_calls_per_iteration
        kept = tool_calls if call_limit is None else tool_calls[:call_limit]
        excess = [] if call_limit is None else tool_calls[call_limit:]
        for index, tc in enumerate(kept):
            call_id = str(tc["id"])
            name = str(tc["name"])
            arguments = str(tc["arguments"])
            yield ToolCallStarted(call_id=call_id, name=name, arguments=arguments)
            produced: tuple[RedactionRecord, ...] = ()
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                result = "ERROR: bad arguments"
            else:
                if not isinstance(parsed, dict):
                    # The executor contract takes an argument mapping; valid
                    # JSON of any other shape is equally bad arguments.
                    result = "ERROR: bad arguments"
                else:
                    try:
                        result, produced = await self._execute_tool(name, parsed)
                    except OutboundPolicyError:
                        # A blocked result is not reportable to the model:
                        # let it reach run_turn's rollback.
                        yield ToolCallFinished(
                            call_id=call_id, name=name, ok=False, summary="blocked"
                        )
                        raise
                    except Exception as exc:  # defensive: executor contract is never-raise
                        # Same ingest cap as ToolExecutor — a huge exception
                        # message must not bypass the limit into history.
                        result = cap_result(f"ERROR: {exc}")
            ingress: list[RedactionRecord] = []
            if self._max_result_chars is not None:
                # Head+tail compaction, not a prefix cut: reports place
                # their evidence (events, log excerpts) last by design.
                # Structured results are shrunk structurally instead —
                # `sanitize_tool_result` redacts the document first and
                # bounds the redacted document, so it stays parseable.
                result = sanitize_tool_result(
                    name, result, max_chars=self._max_result_chars, records=ingress
                )
            else:
                result = sanitize_tool_result(name, result, records=ingress)
            if excess and index == len(kept) - 1:
                # Appended after compaction, without re-compacting: the
                # notice is a fixed-size constant carrying no evidence, so
                # what precedes it stays byte-identical to the compacted
                # result (the eval recorder captures exactly that content,
                # so grading sees only model-visible evidence); the stored
                # size bound relaxes only by the notice's constant length.
                result += (
                    f"\n\nNOTE: {len(excess)} extra tool call(s) in this response "
                    "were discarded — call one tool at a time and wait for its result."
                )
            yield ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=not result.startswith("ERROR:"),
                summary=result[:120],
            )
            tool_message = {"role": "tool", "tool_call_id": call_id, "content": result}
            self._messages.append(tool_message)
            # The producer's trail and this pass's are two views of one
            # document, re-rooted onto the same origin so a redaction both
            # of them saw is not counted twice.
            self._remember_ingress(
                tool_message,
                merge_records(ingress, [rebase(item, "tool_result") for item in produced]),
            )
        for tc in excess:
            summary = "discarded: too many tool calls in one response"
            yield ToolCallStarted(
                call_id=str(tc["id"]), name=str(tc["name"]), arguments=str(tc["arguments"])
            )
            yield ToolCallFinished(
                call_id=str(tc["id"]), name=str(tc["name"]), ok=False, summary=summary
            )

    def _over_history_budget(self, iteration: int) -> bool:
        """True when a follow-up iteration would send a request over budget.

        Strict-mode in-turn backstop (off by default — the full profile
        keeps its original behavior of enforcing the budget only across
        turns): capped tool results alone do not bound history growth —
        assistant text and kept-call arguments are stored verbatim, and
        trimming never splits the current turn. Never fires on the first
        iteration — the strict pre-flight has already trimmed and, if
        needed, rejected that request; the overshoot is at
        most one iteration's model output, which the provider's own output
        limit bounds.
        """
        if not self._strict_history_budget or not iteration:
            return False
        return sum(_message_chars(m) for m in self._messages) > self._max_history_chars

    def _assistant_message(self, state: _StreamState) -> dict[str, Any]:
        """The stored assistant message: text plus only the kept tool calls.

        Excess parallel calls (arguments included) must not enter history,
        and the provider protocol needs exactly one tool message per stored
        call — `_dispatch_tools` keeps the same prefix.
        """
        message: dict[str, Any] = {"role": "assistant", "content": state.text}
        if state.tool_calls:
            limit = self._max_tool_calls_per_iteration
            stored = state.tool_calls if limit is None else state.tool_calls[:limit]
            message["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in stored
            ]
        return message

    def _strict_preflight_over_budget(self) -> bool:
        """Strict mode: bring history within budget after the new user
        message was appended, and report a prompt that cannot fit.

        Trimming runs again here because the pre-append trim cannot see
        the new user message: a retained turn just below the cap plus the
        new prompt would otherwise go over the wire on iteration zero.
        Once appended, the previous turn is no longer the newest, so the
        normal trim drops it; True means even that was not enough — the
        request cannot fit by itself and must be rejected, not sent.
        """
        if not self._strict_history_budget:
            return False
        self._trim_history()
        return sum(_message_chars(m) for m in self._messages) > self._max_history_chars

    def _drop_oldest_retained_turn(self) -> int:
        """Drop the oldest retained turn; return how many messages went.

        Recovery only: used when a prepared request is over the outbound
        ceiling. The turn in flight is never a candidate — the current
        prompt is what the request is *for*, so it is reported as too
        large, never silently removed from the payload the model sees.
        """
        user_indices = [i for i, m in enumerate(self._messages) if m.get("role") == "user"]
        if len(user_indices) <= 1:
            return 0
        start, cut = user_indices[0], user_indices[1]
        del self._messages[start:cut]
        return cut - start

    def _prepare_request(self, iteration: int) -> PreparedOutbound:
        """Prepare one request, recovering from an over-ceiling payload.

        An oversized request is recoverable: dropping the oldest retained
        turn shrinks the same conversation until it fits, so a long
        session keeps working instead of blocking every prompt from here
        on. When nothing older is left to drop, the error propagates and
        the turn is rejected with a message the user can act on.
        """
        while True:
            self._forget_dropped_ingress_records()
            try:
                return self._outbound.prepare(
                    self._provider.name,
                    provider_prepared_messages(self._provider, self._messages),
                    self._tools,
                    iteration=iteration,
                    ingress=self._ingress_by_index(),
                )
            except OutboundRequestTooLarge:
                removed = self._drop_oldest_retained_turn()
                if not removed:
                    raise
                self._shift_turn_bases(removed)
                logger.info(
                    "outbound request over the ceiling: dropped the oldest retained turn "
                    "(%d message(s), %d retained)",
                    removed,
                    len(self._messages),
                )

    def _rollback_policy_block(
        self,
        turn_in: int,
        turn_out: int,
        usage_missing: bool,
        error: OutboundPolicyError,
    ) -> tuple[AgentError, TurnComplete]:
        """Drop a blocked turn while retaining cost from completed iterations."""
        self._turn_active = False
        self._live_state = None
        self._truncate_history(self._turn_base)
        self._total_in += turn_in
        self._total_out += turn_out
        self._estimated = self._estimated or usage_missing
        return (
            AgentError(message=f"{error.headline}: {error}"),
            TurnComplete(
                input_tokens=turn_in,
                output_tokens=turn_out,
                estimated=usage_missing,
            ),
        )

    def _provider_error_event(
        self,
        state: _StreamState,
        prompt_estimate: int,
        turn_in: int,
        turn_out: int,
        usage_missing: bool,
        error: Exception,
    ) -> AgentError:
        """Commit real provider cost and return the safe terminal error event."""
        self._turn_active = False
        if not state.has_usage and (state.text or state.tool_calls):
            state.in_tok = prompt_estimate
            state.out_tok = _stream_output_chars(state) // 4
        self._total_in += turn_in + state.in_tok
        self._total_out += turn_out + state.out_tok
        if usage_missing or not state.has_usage:
            self._estimated = True
        return AgentError(message=str(error) or type(error).__name__)

    def finalize_interrupt(self) -> TurnInterrupted:
        """Repair state after the driving task cancelled a turn (issue #170).

        The generator was cancelled at an arbitrary await point, so the
        in-flight iteration may have left a partial footprint. Repairs:

        - truncate everything the in-flight iteration appended (an
          assistant message whose tool_calls lack results would break the
          provider protocol; completed prior iterations stay),
        - record a bounded, clearly marked interrupted assistant note
          (with capped partial text when any streamed) - never the raw
          partial, which would replay as a completed answer,
        - commit usage: provider-reported counts exactly; a partial
          stream without usage is estimated the same way the error path
          estimates (the transmitted prompt was real cost).

        Inert when no turn is active (a completed turn is never repaired).
        """
        if not self._turn_active:
            return TurnInterrupted(input_tokens=0, output_tokens=0, estimated=False)
        self._turn_active = False
        self._truncate_history(self._iteration_base)
        state = self._live_state
        self._live_state = None
        partial = state.text if state is not None else ""
        if partial:
            note = (
                partial[:INTERRUPT_PARTIAL_CHARS]
                + "\n\n[response interrupted by the user before completion]"
            )
        else:
            note = "[interrupted by the user]"
        self._messages.append({"role": "assistant", "content": note})
        in_flight_in = 0
        in_flight_out = 0
        estimated = self._turn_usage_missing
        if state is not None:
            if state.has_usage:
                in_flight_in = state.in_tok
                in_flight_out = state.out_tok
            elif state.text or state.tool_calls:
                # Same rule as the stream-error path: output before dying
                # means the prompt was really transmitted.
                in_flight_in = self._live_prompt_estimate
                in_flight_out = _stream_output_chars(state) // 4
                estimated = True
        total_in = self._turn_in + in_flight_in
        total_out = self._turn_out + in_flight_out
        self._total_in += total_in
        self._total_out += total_out
        self._estimated = self._estimated or estimated
        return TurnInterrupted(input_tokens=total_in, output_tokens=total_out, estimated=estimated)

    async def run_turn(
        self,
        user_text: str,
        screen_context: str,
    ) -> AsyncIterator[AgentEvent]:
        """Async generator: run one conversation turn, yielding events until done."""
        self._trim_history()
        self._turn_base = len(self._messages)
        turn_in = 0
        turn_out = 0
        # Token counts are exact only when EVERY iteration reported usage;
        # one missing iteration makes the whole turn an estimate.
        usage_missing = False
        try:
            ingress: list[RedactionRecord] = []
            safe_screen_context = sanitize_screen_context(screen_context, ingress)
            content = (
                "[screen context: untrusted evidence]\n"
                f"{safe_screen_context}\n"
                "[end screen context]\n\n"
                f"{user_text}"
            )
            user_message = {"role": "user", "content": content}
            self._messages.append(user_message)
            self._remember_ingress(user_message, ingress)
            if self._strict_preflight_over_budget():
                # Drop the unfittable prompt so it cannot poison later turns.
                self._truncate_history(self._turn_base)
                logger.warning(
                    "strict history budget: rejected a prompt that cannot fit by itself "
                    "(budget %d chars)",
                    self._max_history_chars,
                )
                yield AgentError(
                    message=(
                        f"request too large for the history budget "
                        f"({self._max_history_chars} chars) — shorten the question"
                    )
                )
                yield TurnComplete(input_tokens=0, output_tokens=0, estimated=False)
                return

            # Arm interruption bookkeeping (issue #170): from here on a
            # cancellation is repairable by finalize_interrupt.
            self._turn_active = True
            self._iteration_base = len(self._messages)
            self._live_state = None
            self._live_prompt_estimate = 0
            self._turn_in = 0
            self._turn_out = 0
            self._turn_usage_missing = False
            for iteration in range(self._max_iterations):
                if self._over_history_budget(iteration):
                    self._turn_active = False
                    self._total_in += turn_in
                    self._total_out += turn_out
                    self._estimated = self._estimated or usage_missing
                    yield AgentError(
                        message=(
                            f"history budget exceeded mid-turn "
                            f"({self._max_history_chars} chars) — turn ended early"
                        )
                    )
                    yield TurnComplete(
                        input_tokens=turn_in,
                        output_tokens=turn_out,
                        estimated=usage_missing,
                    )
                    return
                state = _StreamState()
                self._iteration_base = len(self._messages)
                self._live_state = state
                # Replaced only once a request exists to replace it with:
                # preparation can refuse, and the previous handoff is still
                # the latest thing this session sent.
                prepared = self._prepare_request(iteration + 1)
                self._latest_outbound_payload = prepared.snapshot
                # Estimate of the prompt this iteration sends — used only when
                # the provider omits usage, so token totals never read as zero
                # input for a request that was really transmitted. Measured on
                # the canonical payload that was actually handed over: history
                # accounting cannot see what preparation dropped to fit, nor
                # what a provider dialect hook added inside the boundary.
                prompt_estimate = len(prepared.snapshot.payload_json) // 4
                self._live_prompt_estimate = prompt_estimate
                try:
                    stream = self._provider.complete(prepared.messages, prepared.tools)
                    async for event in self._consume_stream(stream, state):
                        yield event
                except Exception as exc:
                    # Tokens spent in earlier iterations (and the partial stream)
                    # are real cost — account for them before bailing out. A
                    # stream that produced output before dying definitely had
                    # its prompt processed, so apply the same estimates as the
                    # normal path; one that died before yielding anything gets
                    # no speculative charge.
                    yield self._provider_error_event(
                        state,
                        prompt_estimate,
                        turn_in,
                        turn_out,
                        usage_missing,
                        exc,
                    )
                    return

                _estimate_missing_usage(state, prompt_estimate)
                turn_in += state.in_tok
                turn_out += state.out_tok
                usage_missing = usage_missing or not state.has_usage
                # This iteration's stream is complete: its usage is committed
                # and its state must no longer be double-counted on interrupt.
                self._turn_in = turn_in
                self._turn_out = turn_out
                self._turn_usage_missing = usage_missing
                self._live_state = None

                assistant_msg = self._assistant_message(state)
                self._messages.append(assistant_msg)

                if not state.tool_calls:
                    self._turn_active = False
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

            self._turn_active = False
            self._total_in += turn_in
            self._total_out += turn_out
            self._estimated = self._estimated or usage_missing
            yield AgentError(
                message=(f"iteration limit reached ({self._max_iterations}) — refine the question")
            )
            yield TurnComplete(
                input_tokens=turn_in,
                output_tokens=turn_out,
                estimated=usage_missing,
            )
        except OutboundPolicyError as exc:
            error, complete = self._rollback_policy_block(
                turn_in,
                turn_out,
                usage_missing,
                exc,
            )
            yield error
            yield complete
