"""The in-tree agent loop, assembled from the harness components.

`NativeAgentEngine` is `AgentRuntime`'s loop with every responsibility it
had accreted moved out: durable history and usage accounting live in
`ConversationState`, the provider boundary in `RequestGateway`, tool
routing and evidence in `ToolHarness`, and prompt composition in the
session's `PromptHarness`. What remains here is the sequencing those parts
cannot do for each other — and only that.

The order of operations in one provider round is itself a contract:

1. The system prefix is rebuilt from the request's system message plus the
   evidence table **as it stands now**, so a round never offers a reference
   that a later round minted, nor one a previous turn dropped.
2. The request is prepared — and, if it is over the boundary's ceiling,
   retried after dropping the oldest completed turn — *before* any
   iteration is opened, so the prompt estimate is measured on the exact
   payload that will cross the boundary.
3. `ConversationState.start_iteration` and `ToolHarness.begin_iteration`
   are called synchronously, before the first stream await, so a
   cancellation that lands on the very first `__anext__` still finds the
   turn's bookkeeping armed.
4. Handoff is proven by the gateway, which marks the conversation
   transmitted exactly once, so a request that really ran is charged even
   when the provider never reports usage.

Everything the model asks for is filtered before it can reach a port or
durable history: a call with no id or no name, a repeated id, unusable
arguments, and calls beyond the policy's per-iteration cap are answered or
reported but never dispatched, and only the kept prefix is stored — so the
provider protocol's one-result-per-call rule holds by construction.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from korvid.agent.conversation import ConversationBudgetError, ConversationState
from korvid.agent.engine import AgentEngine, AgentTurnRequest
from korvid.agent.events import (
    AgentError,
    AgentEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
)
from korvid.agent.model_policy import ResolvedAgentPolicy
from korvid.agent.outbound import OutboundPolicyError, OutboundRequestTooLarge
from korvid.agent.request_gateway import PreparedGatewayRequest, RequestGateway
from korvid.agent.tool_harness import ToolExecution, ToolHarness

logger = logging.getLogger(__name__)

#: Longest error message this engine will show. Provider and boundary
#: errors can quote arbitrary payload, so every message is cut to a size
#: that cannot become an exfiltration channel or flood the panel.
ERROR_CHARS = 500

#: How much of a tool result the UI row summarizes.
SUMMARY_CHARS = 120

_DISCARD_UNPAIRABLE = "discarded: the response named no tool or no call id"
_DISCARD_DUPLICATE = "discarded: duplicate tool call id"
_DISCARD_EXCESS = "discarded: too many tool calls in one response"
_BAD_ARGUMENTS = "tool arguments must be a JSON object"


@dataclass(frozen=True, slots=True)
class _Call:
    """One streamed tool call, as the provider spelled it."""

    call_id: str
    name: str
    arguments: str


@dataclass
class _Round:
    """What one provider round produced, and whether it ended the turn."""

    text: str = ""
    calls: list[_Call] = field(default_factory=list)
    discarded: list[tuple[_Call, str]] = field(default_factory=list)
    #: How many calls the policy's per-iteration cap discarded. Only these
    #: earn the one-tool-at-a-time notice: a malformed call is answered.
    excess: int = 0
    done: bool = False


class NativeAgentEngine(AgentEngine):
    """Sequence conversation, gateway and tool harness into one turn."""

    def __init__(
        self,
        *,
        conversation: ConversationState,
        gateway: RequestGateway,
        tools: ToolHarness,
    ) -> None:
        """Wire the engine to the three collaborators it drives.

        Args:
            conversation: Durable history, provenance and usage accounting.
            gateway: The single seam every provider request crosses.
            tools: Policy-aware tool routing and the evidence ledger.
        """
        self._conversation = conversation
        self._gateway = gateway
        self._tools = tools
        self._running = False
        self._closed = False
        self._gateway_closed = False
        self._interrupted = False

    # -- AgentEngine -------------------------------------------------------

    def run(self, request: AgentTurnRequest) -> AsyncIterator[AgentEvent]:
        """Start one turn. See `AgentEngine.run`.

        Raises:
            RuntimeError: The engine is closed, or a turn is already
                running — the gateway drives one provider iterator at a
                time, and a second turn would clobber it.
        """
        if self._closed:
            raise RuntimeError("the engine is closed")
        if self._running:
            raise RuntimeError("a turn is already running")
        self._running = True
        self._interrupted = False
        return self._run(request)

    def interrupt(self) -> None:
        """Ask the live turn to stop at its next boundary."""
        self._interrupted = True

    async def aclose(self) -> None:
        """Close the gateway's stream state once and refuse later turns."""
        self._closed = True
        self._running = False
        if self._gateway_closed:
            return
        self._gateway_closed = True
        await self._gateway.aclose()

    # -- the turn ----------------------------------------------------------

    async def _run(self, request: AgentTurnRequest) -> AsyncGenerator[AgentEvent, None]:
        """Drive one turn, releasing the single-flight latch on every exit."""
        try:
            async for event in self._turn(request):
                yield event
        finally:
            self._running = False

    async def _turn(self, request: AgentTurnRequest) -> AsyncGenerator[AgentEvent, None]:
        """Open the durable turn, run its rounds, and fail closed if blocked."""
        self._tools.reset_evidence(request.interaction.context_epoch)
        budget = request.policy.max_history_chars
        try:
            self._conversation.start_turn(request.prompt.user_message)
        except ConversationBudgetError:
            logger.warning("history budget: rejected a prompt that cannot fit (%d chars)", budget)
            yield AgentError(
                message=(
                    f"request too large for the history budget ({budget} chars) "
                    "— shorten the question"
                )
            )
            yield TurnComplete(input_tokens=0, output_tokens=0, estimated=False)
            return
        try:
            async for event in self._iterate(request):
                yield event
        except OutboundPolicyError as exc:
            # Fail closed: the turn that produced unsendable content is
            # rolled back whole, keeping the cost it really paid and the
            # last handoff that really happened.
            turn_in, turn_out, estimated = self._conversation.rollback_turn()
            logger.warning("turn rolled back: %s", exc.headline)
            yield AgentError(message=_bounded(f"{exc.headline}: {exc}"))
            yield TurnComplete(input_tokens=turn_in, output_tokens=turn_out, estimated=estimated)

    async def _iterate(self, request: AgentTurnRequest) -> AsyncGenerator[AgentEvent, None]:
        """Run provider rounds until one answers, ends, or the cap is hit."""
        for iteration in range(request.policy.max_iterations):
            if self._interrupted:
                return
            round_ = _Round()
            async for event in self._round(request, iteration, round_):
                yield event
            if round_.done or self._interrupted:
                return
        limit = request.policy.max_iterations
        for event in self._stop(f"iteration limit reached ({limit}) — refine the question"):
            yield event

    async def _round(
        self, request: AgentTurnRequest, iteration: int, round_: _Round
    ) -> AsyncGenerator[AgentEvent, None]:
        """Send one request, then act on exactly what came back."""
        if self._over_history_budget(request.policy, iteration):
            round_.done = True
            budget = request.policy.max_history_chars
            for event in self._stop(
                f"history budget exceeded mid-turn ({budget} chars) — turn ended early"
            ):
                yield event
            return
        prepared = self._prepare(request, iteration + 1)
        # Both counters are armed synchronously, before the first await, so
        # a cancellation on the very first event still finds them open.
        self._conversation.start_iteration(prepared.prompt_estimate)
        self._tools.begin_iteration()
        try:
            async for event in self._consume(prepared, round_):
                yield event
        except Exception as exc:  # provider transport, adapter, or protocol
            round_.done = True
            yield self._provider_error(exc)
            return
        if self._interrupted:
            return
        _apply_call_cap(round_, request.policy)
        self._conversation.append_assistant(round_.text, _stored_calls(round_.calls))
        if not round_.calls and not round_.discarded:
            round_.done = True
            yield self._complete(round_.text)
            return
        async for event in self._dispatch(round_):
            yield event
        if not round_.calls:
            # Every call in this response was unusable. Another round would
            # replay the same failure with the same history, so stop here
            # rather than burn the iteration budget on it.
            round_.done = True
            for event in self._stop("no usable tool call in the response — turn ended early"):
                yield event

    # -- one provider request ---------------------------------------------

    def _prepare(self, request: AgentTurnRequest, iteration: int) -> PreparedGatewayRequest:
        """Prepare one request, shrinking history until it fits the ceiling.

        Recovery drops whole completed turns, never parts of the current
        one: sending a smaller, still fully checked conversation is the only
        way to make an over-ceiling request fit without weakening what the
        boundary examined.

        Raises:
            OutboundPolicyError: The request cannot be prepared safely, or
                is over the ceiling with nothing left to drop.
        """
        prefix = self._system_prefix(request)
        while True:
            view = self._conversation.request_messages(prefix=prefix)
            try:
                return self._gateway.prepare(
                    view.messages,
                    request.policy.tools,
                    iteration=iteration,
                    provenance=view,
                )
            except OutboundRequestTooLarge:
                removed = self._conversation.drop_oldest_turn()
                if not removed:
                    raise
                logger.info(
                    "outbound request over the ceiling: dropped the oldest retained turn "
                    "(%d message(s))",
                    removed,
                )

    def _system_prefix(self, request: AgentTurnRequest) -> list[dict[str, Any]]:
        """The ephemeral system message for this round.

        Rebuilt per round from the ledger's current contents: the table has
        to name every reference minted so far this turn and nothing else,
        and it is never retained, so no stale reference can survive into a
        later turn.
        """
        note = self._tools.evidence.prompt_note()
        content = request.prompt.system_message
        if note:
            content = f"{content}\n\n{note}"
        return [{"role": "system", "content": content}]

    async def _consume(
        self, prepared: PreparedGatewayRequest, round_: _Round
    ) -> AsyncGenerator[AgentEvent, None]:
        """Stream one request, accumulating text, calls and usage."""
        seen: set[str] = set()
        # `RequestGateway.stream` is an async generator function; its
        # declared return type is the narrower `AsyncIterator`, so the cast
        # names what the object already is. Closing it matters: an early
        # return (an interrupt) must release the gateway's active iterator
        # rather than leave it for the garbage collector to notice.
        stream = cast(
            "AsyncGenerator[dict[str, Any], None]",
            self._gateway.stream(prepared, self._conversation.mark_transmitted),
        )
        async with contextlib.aclosing(stream) as events:
            async for event in events:
                kind = str(event.get("type", ""))
                if kind == "text_delta":
                    text = str(event.get("text", ""))
                    if text:
                        self._conversation.record_stream_text(text)
                        round_.text += text
                        yield TextDelta(text=text)
                elif kind == "tool_call":
                    self._collect_call(event, round_, seen)
                elif kind == "usage":
                    self._conversation.commit_usage(
                        _as_int(event.get("input_tokens")), _as_int(event.get("output_tokens"))
                    )
                if self._interrupted:
                    return

    def _collect_call(self, event: Mapping[str, Any], round_: _Round, seen: set[str]) -> None:
        """Validate one streamed call and file it as kept or discarded.

        A call the protocol cannot pair — no id, no name, or an id already
        used in this response — is discarded here, before it can be stored
        or dispatched: two results for one id, or a result for no id, make
        every later request invalid.
        """
        call = _Call(
            call_id=str(event.get("id", "")).strip(),
            name=str(event.get("name", "")).strip(),
            arguments=str(event.get("arguments", "")),
        )
        # Generated output, whether or not it is usable: a tool-only round
        # that is cancelled still produced these characters.
        self._conversation.record_stream_tool_call(call.name, call.arguments)
        if not call.call_id or not call.name:
            round_.discarded.append((call, _DISCARD_UNPAIRABLE))
            return
        if call.call_id in seen:
            round_.discarded.append((call, _DISCARD_DUPLICATE))
            return
        seen.add(call.call_id)
        round_.calls.append(call)

    # -- tool calls --------------------------------------------------------

    async def _dispatch(self, round_: _Round) -> AsyncGenerator[AgentEvent, None]:
        """Run the kept calls in order, then report the discarded ones.

        Strictly sequential: a round may legitimately carry several calls,
        but running them concurrently would let two writes, or a write and
        a screen action, interleave inside one approval story.
        """
        last = len(round_.calls) - 1
        for index, call in enumerate(round_.calls):
            yield ToolCallStarted(call_id=call.call_id, name=call.name, arguments=call.arguments)
            try:
                execution = await self._execute(call)
            except OutboundPolicyError:
                # A blocked result cannot be shown to the model. Close the
                # row the UI is showing, then let the turn roll back.
                yield ToolCallFinished(
                    call_id=call.call_id, name=call.name, ok=False, summary="blocked"
                )
                raise
            text = execution.outcome.text
            if round_.excess and index == last:
                text += _excess_notice(round_.excess)
            yield ToolCallFinished(
                call_id=call.call_id,
                name=call.name,
                # The producer's verdict, never the text's shape.
                ok=not execution.outcome.error,
                summary=text[:SUMMARY_CHARS],
            )
            self._conversation.append_tool_result(
                call.call_id,
                text,
                execution.outcome.redactions,
                error=execution.outcome.error,
            )
            if self._interrupted:
                return
        for call, reason in round_.discarded:
            yield ToolCallStarted(call_id=call.call_id, name=call.name, arguments=call.arguments)
            yield ToolCallFinished(call_id=call.call_id, name=call.name, ok=False, summary=reason)

    async def _execute(self, call: _Call) -> ToolExecution:
        """Route one kept call, refusing arguments no tool could accept."""
        arguments = _parse_arguments(call.arguments)
        if arguments is None:
            return self._tools.reject(call.call_id, call.name, _BAD_ARGUMENTS)
        return await self._tools.execute(call.call_id, call.name, arguments)

    # -- turn outcomes -----------------------------------------------------

    def _over_history_budget(self, policy: ResolvedAgentPolicy, iteration: int) -> bool:
        """True when a follow-up round would send more history than allowed.

        Strict-mode backstop only, and never on the first round: capped
        results alone do not bound growth (assistant text and call
        arguments are stored verbatim) and trimming never splits the turn
        in flight.
        """
        if not policy.strict_history_budget or not iteration:
            return False
        return self._conversation.history_chars > policy.max_history_chars

    def _complete(self, text: str) -> TurnComplete:
        """Close a turn that answered, reporting how it cited its evidence."""
        turn_in, turn_out, estimated = self._conversation.complete_turn()
        cited, uncited, duplicated = self._tools.evidence.check_citations(text)
        return TurnComplete(
            input_tokens=turn_in,
            output_tokens=turn_out,
            estimated=estimated,
            cited=cited,
            uncited=uncited,
            duplicated=duplicated,
        )

    def _stop(self, message: str) -> list[AgentEvent]:
        """End a turn early: one visible reason, then terminal accounting."""
        turn_in, turn_out, estimated = self._conversation.complete_turn()
        return [
            AgentError(message=message),
            TurnComplete(input_tokens=turn_in, output_tokens=turn_out, estimated=estimated),
        ]

    def _provider_error(self, exc: Exception) -> AgentError:
        """Unwind a failed round, keeping the cost the request really had.

        The round's own messages go — an assistant message whose calls can
        never be answered would break every later request — while earlier
        rounds stay. A transmitted request is charged whether or not the
        provider lived long enough to report usage; one that never reached
        the provider is charged nothing. No `TurnComplete` follows: a turn
        that failed must never be reported in the shape of one that
        succeeded.
        """
        self._conversation.abandon_iteration()
        self._conversation.complete_turn()
        logger.warning("provider stream failed: %s", type(exc).__name__)
        return AgentError(message=_bounded(str(exc) or type(exc).__name__))


def _stored_calls(calls: list[_Call]) -> list[dict[str, str]]:
    """The kept calls in the shape durable history stores them."""
    return [{"id": call.call_id, "name": call.name, "arguments": call.arguments} for call in calls]


def _apply_call_cap(round_: _Round, policy: ResolvedAgentPolicy) -> None:
    """Keep only the policy's allowed prefix of calls; discard the rest.

    Excess calls are dropped entirely — not stored, not answered — because
    retaining their arguments would let a parallel-call-happy model grow
    history past the budget mid-turn, and trimming never drops the newest
    turn. The model learns the rule from a fixed notice on the last kept
    result instead.
    """
    limit = policy.max_tool_calls_per_iteration
    if limit is None or len(round_.calls) <= limit:
        return
    excess = round_.calls[limit:]
    round_.calls = round_.calls[:limit]
    round_.excess = len(excess)
    round_.discarded.extend((call, _DISCARD_EXCESS) for call in excess)


def _excess_notice(count: int) -> str:
    """The fixed, evidence-free notice appended to the last kept result."""
    return (
        f"\n\nNOTE: {count} extra tool call(s) in this response were discarded "
        "— call one tool at a time and wait for its result."
    )


def _parse_arguments(arguments: str) -> dict[str, Any] | None:
    """Parse a call's arguments, or None when they are not a JSON object.

    Only an object can be a tool's keyword arguments. A list, a bare
    scalar, or anything unparseable is refused here, before a port sees it.
    """
    text = arguments.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): value for key, value in parsed.items()}


def _as_int(value: Any) -> int:
    """A usage count as a non-negative int; anything unusable counts zero."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _bounded(message: str) -> str:
    """Cut an error to a size that cannot flood the panel or leak a payload."""
    text = " ".join(message.split())
    if len(text) <= ERROR_CHARS:
        return text
    return f"{text[:ERROR_CHARS]}…"
