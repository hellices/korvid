"""The in-tree agent loop, assembled from the harness components.

`NativeAgentEngine` is korvid's one agent loop. Every responsibility a
loop tends to accrete lives outside it: durable history and usage
accounting in `ConversationState`, the provider boundary in
`RequestGateway`, tool routing and evidence in `ToolHarness`, and prompt
composition in the session's `PromptHarness`. What remains here is the
sequencing those parts cannot do for each other — and only that.

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
provider protocol's one-result-per-call rule holds by construction. The
same rule survives a port that fails: a collaborator raising something
nothing expected is contained as *that call's* error result, named by
exception type only, so the turn keeps a valid history and can still
finish. Cancellation is never contained — it is the hard interrupt.
"""

from __future__ import annotations

import asyncio
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

#: Longest exception class name shown for a failed provider request. A
#: class name is code, but a dynamically built one need not be.
TYPE_NAME_CHARS = 60

#: How much of a tool result the UI row summarizes.
SUMMARY_CHARS = 120

_DISCARD_UNPAIRABLE = "discarded: the response named no tool or no call id"
_DISCARD_DUPLICATE = "discarded: duplicate tool call id"
_DISCARD_EXCESS = "discarded: too many tool calls in one response"
_BAD_ARGUMENTS = "tool arguments must be a JSON object"


class ProviderResponseLimitError(RuntimeError):
    """A provider stream exceeded the resolved turn response budget."""


@dataclass(frozen=True, slots=True)
class _Call:
    """One streamed tool call, as the provider spelled it."""

    call_id: str
    name: str
    arguments: str


@dataclass
class _Round:
    """What one provider round produced, and whether it ended the turn."""

    text_chunks: list[str] = field(default_factory=list)
    text_chars: int = 0
    calls: list[_Call] = field(default_factory=list)
    discarded: list[tuple[_Call, str]] = field(default_factory=list)
    #: How many calls the policy's per-iteration cap discarded. Only these
    #: earn the one-tool-at-a-time notice: a malformed call is answered.
    excess: int = 0
    done: bool = False

    @property
    def text(self) -> str:
        """Assistant text joined once when the completed round needs it."""
        return "".join(self.text_chunks)

    def append_text(self, text: str) -> None:
        """Record one bounded stream chunk without quadratic concatenation."""
        self.text_chunks.append(text)
        self.text_chars += len(text)


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
        #: The task driving the live turn, recorded when iteration starts
        #: so `aclose` can stop a turn that is blocked in a provider await
        #: rather than suspended at a yield it will never be resumed from.
        self._driver: asyncio.Task[Any] | None = None

    # -- AgentEngine -------------------------------------------------------

    def run(self, request: AgentTurnRequest) -> AsyncIterator[AgentEvent]:
        """Start one turn. See `AgentEngine.run`.

        The rejection is checked here so a caller learns immediately, and
        again when iteration really starts: an iterator that is created
        and then abandoned never claimed anything, so it cannot leave the
        engine latched against the next turn. For the same reason the
        interrupt flag is *not* cleared here but in `_run`, when the turn
        actually claims the engine — clearing it here would make an
        `interrupt()` that lands in the window before the first
        `__anext__` apply to a turn that had not started, silently ending
        it before its first round.

        Raises:
            RuntimeError: The engine is closed, or a turn is already
                running — the gateway drives one provider iterator at a
                time, and a second turn would clobber it.
        """
        self._reject_if_unavailable()
        return self._run(request)

    def interrupt(self) -> None:
        """Ask the live turn to stop at its next boundary."""
        self._interrupted = True

    async def aclose(self) -> None:
        """Stop the live turn, close the gateway's stream state once.

        Closing is a hard stop, not a request: the turn is marked
        interrupted so every boundary check ends it, and a turn whose
        driver is parked in a provider await — where no boundary check
        can run and no `aclose` on a running generator is legal — has
        that driver cancelled.

        **Driver-cancel ownership.** The engine does not create the task
        that drives a turn; it cancels one only here, and only a task
        that is neither the caller's own nor already done. That is the
        same hard interrupt the UI performs, so the same rule applies
        after it: the conversation is left mid-turn, and the session
        repairs it with `ConversationState.finalize_interrupt`. Called
        from inside the turn's own loop, nothing is cancelled — the
        generator stops itself at the next resumption instead.
        """
        self._closed = True
        self._interrupted = True
        driver = self._driver
        if driver is not None and driver is not asyncio.current_task() and not driver.done():
            driver.cancel()
        if self._gateway_closed:
            return
        self._gateway_closed = True
        await self._gateway.aclose()

    # -- the turn ----------------------------------------------------------

    def _reject_if_unavailable(self) -> None:
        """Refuse a turn this engine cannot run.

        Raises:
            RuntimeError: The engine is closed, or a turn is already
                running.
        """
        if self._closed:
            raise RuntimeError("the engine is closed")
        if self._running:
            raise RuntimeError("a turn is already running")

    async def _run(self, request: AgentTurnRequest) -> AsyncGenerator[AgentEvent, None]:
        """Claim the engine for this turn and release it on every exit.

        The claim is taken here rather than in `run` so it belongs to a
        turn that actually started, and the interrupt flag is cleared with
        it, in the same synchronous step: an interrupt only ever applies
        to a turn that is running, so one raised while the engine was idle
        — including in the window between `run` and the first
        `__anext__` — is discarded here rather than inherited. `_closed`
        is re-read after every event: a close that lands while this
        generator is suspended must end the turn at that exact point,
        before another event or another history append.
        """
        self._reject_if_unavailable()
        self._running = True
        self._interrupted = False
        self._driver = asyncio.current_task()
        try:
            turn = self._turn(request)
            async with contextlib.aclosing(turn) as events:
                async for event in events:
                    yield event
                    if self._closed:
                        return
        finally:
            self._running = False
            self._driver = None

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
        except Exception as exc:
            # Nothing expected this: a collaborator outside the tool
            # boundary — the gateway, the conversation itself — raised
            # something this loop has no answer for. (A failing tool port
            # never reaches here: `_dispatch` contains it as that call's
            # own result.) The failure is the session's to render, but the
            # *state* is this engine's to leave usable — an active turn,
            # or an assistant call no result can ever pair with, would
            # make every later turn fail to start. Only `Exception`: a
            # cancellation is the hard interrupt, and the turn must stay
            # active for the session to finalize.
            logger.warning("turn rolled back after an unexpected %s", type(exc).__name__)
            with contextlib.suppress(Exception):
                self._conversation.rollback_turn()
            raise

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
            async for event in self._consume(
                prepared,
                round_,
                response_limit=request.policy.max_history_chars,
            ):
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
        self,
        prepared: PreparedGatewayRequest,
        round_: _Round,
        *,
        response_limit: int,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Stream one request, accumulating text, calls and usage."""
        # Seeded from history, not empty: an id a previous iteration or a
        # previous turn already spent is still unusable, because the call
        # that spent it is still stored and still has its own result.
        seen: set[str] = set(self._conversation.retained_tool_call_ids)
        # `RequestGateway.stream` is an async generator function; its
        # declared return type is the narrower `AsyncIterator`, so the cast
        # names what the object already is. Closing it matters: an early
        # return (an interrupt) must release the gateway's active iterator
        # rather than leave it for the garbage collector to notice.
        stream = cast(
            "AsyncGenerator[dict[str, Any], None]",
            self._gateway.stream(prepared, self._conversation.mark_transmitted),
        )
        event_count = 0
        response_chars = 0
        async with contextlib.aclosing(stream) as events:
            async for event in events:
                event_count += 1
                response_chars += _stream_event_chars(event)
                if event_count > response_limit or response_chars > response_limit:
                    raise ProviderResponseLimitError(
                        f"provider response exceeded the {response_limit}-character policy limit"
                    )
                kind = str(event.get("type", ""))
                if kind == "text_delta":
                    text = str(event.get("text", ""))
                    if text:
                        self._conversation.record_stream_text(text)
                        round_.append_text(text)
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
        spent by this response, by an earlier iteration, or by a retained
        earlier turn — is discarded here, before it can be stored or
        dispatched: two results for one id, or a result for no id, make
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
            except Exception as exc:  # executor, bridge or harness bug
                execution = self._contain(call, exc)
            text = execution.outcome.text
            if round_.excess and index == last:
                text = self._tools.cap_text(
                    text,
                    suffix=_excess_notice(round_.excess),
                )
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

    def _contain(self, call: _Call, exc: Exception) -> ToolExecution:
        """Turn a collaborator's unexpected failure into this call's result.

        The ports behind the harness — the recorded executor, the UI
        bridge — can raise anything: a torn-down screen, a client bug, a
        decoder that met a body it did not expect. That is *one call's*
        failure, not the turn's: the model is told this call failed, the
        stored call gets the one result the protocol requires it to have,
        and the turn goes on to its next round. Letting it escape instead
        would abandon a turn the model could still finish, and would leave
        the panel with a half-open tool row.

        Only `Exception`. A `CancelledError` is the hard interrupt and
        must keep unwinding to the session, which repairs the conversation
        with `ConversationState.finalize_interrupt`.

        The reason is built the way a provider failure is — the class name
        and nothing else. A tool failure routinely quotes what the call
        touched: a cluster endpoint, a rejected credential, or the very
        resource body the masking pipeline exists to redact, and this text
        is bound for the *model*, so echoing it would send unmasked
        cluster content across the boundary that is supposed to check it.
        """
        logger.warning("tool call failed: %s", type(exc).__name__)
        return self._tools.reject(call.call_id, call.name, _tool_failure_message(exc))

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
        return AgentError(message=_failure_message(exc))


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
    limit = _call_limit(policy)
    if limit is None or len(round_.calls) <= limit:
        return
    excess = round_.calls[limit:]
    round_.calls = round_.calls[:limit]
    round_.excess = len(excess)
    round_.discarded.extend((call, _DISCARD_EXCESS) for call in excess)


def _call_limit(policy: ResolvedAgentPolicy) -> int | None:
    """How many calls of one response this policy may actually run.

    `allow_parallel_tool_calls` is not a cap but a routing fact: the model
    router sets it only where the *provider itself* confirmed parallel
    tool calling on a high-tier route, so a policy without it runs one
    call per response whatever its numeric cap says (a tier may leave the
    cap unset entirely). Where it is granted, the explicit cap still
    bounds it — permission to batch is not permission to batch without
    limit.
    """
    limit = policy.max_tool_calls_per_iteration
    if policy.allow_parallel_tool_calls:
        return limit
    return 1 if limit is None else min(limit, 1)


def _excess_notice(count: int) -> str:
    """The fixed, evidence-free notice appended to the last kept result."""
    return (
        f"\n\nNOTE: {count} extra tool call(s) in this response were discarded "
        "— call one tool at a time and wait for its result."
    )


def _stream_event_chars(event: Mapping[str, Any]) -> int:
    """Character cost of one normalized provider stream event."""
    kind = str(event.get("type", ""))
    if kind == "text_delta":
        return len(str(event.get("text", "")))
    if kind == "tool_call":
        return sum(len(str(event.get(field, ""))) for field in ("id", "name", "arguments"))
    return 1


def _parse_arguments(arguments: str) -> dict[str, Any] | None:
    """Parse a call's arguments, or None when they are not a JSON object.

    Only an object can be a tool's keyword arguments. A list, a bare
    scalar, or anything unparsable is refused here, before a port sees it.
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


def _failure_message(exc: Exception) -> str:
    """Name a failed request by its type, never by what it said.

    A transport, adapter or protocol error routinely carries the
    provider's response body: an HTTP client quotes the failing request,
    an auth error quotes the credential it was refused for, a validation
    error quotes the prompt. Truncating that is not enough — the first
    500 characters of a 401 body are exactly the part that identifies the
    key — so nothing derived from `str(exc)` reaches the panel. The class
    name is korvid's or the library's own code, not payload, and is
    bounded anyway in case a dynamically built class carries one.
    """
    return (
        f"the provider request failed ({_safe_type_name(exc)}) — its own message is "
        "withheld because provider errors can quote the request or a credential"
    )


def _tool_failure_message(exc: Exception) -> str:
    """Name a failed tool call by its type, never by what it said.

    Same rule as `_failure_message`, for the other direction: this text is
    stored as a tool result and sent to the model, and a port's exception
    can quote the resource it read, the endpoint it called or the token it
    was refused with — none of which passed the result-sanitizing pass
    this refusal replaces.
    """
    return (
        f"the tool call failed ({_safe_type_name(exc)}) — its own message is withheld "
        "because a tool failure can quote cluster data or a credential; "
        "try a different call or tell the user what could not be read"
    )


def _safe_type_name(exc: Exception) -> str:
    """An exception's class name, reduced to identifier characters."""
    name = "".join(char for char in type(exc).__name__ if char.isalnum() or char == "_")
    return name[:TYPE_NAME_CHARS] or "unknown error"
