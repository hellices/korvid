"""Transactional conversation state for the agent loop (design §6.1).

`ConversationState` is the single owner of an agent turn's durable model
history — the user, assistant, and tool messages the provider must see —
together with the redaction provenance the outbound boundary cannot
re-derive from the text. It is a pure state machine: it knows nothing of
the provider, the outbound policy, the prompt composition, or tool
dispatch policy. Those belong to the engine that drives it.

Two invariants shape the design.

*Ephemeral prompt text.* The system prompt and the per-request evidence
table are not history. They change as the environment is retargeted and
as reads land, so retaining them would let a later recomposition mutate a
record of what was already sent. They are supplied to
`request_messages` and prepended to a private deep copy, so retained
history is never touched by them.

*Transactional turns.* A turn is a sequence of iterations, each one a
provider round that may append an assistant message and its paired tool
results. `start_turn` and `start_iteration` return frozen checkpoints
carrying the indices and usage baselines a rollback needs.
`finalize_interrupt` uses the active iteration's checkpoint to unwind a
cancelled turn to a protocol-valid state exactly once.
"""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from korvid.agent.events import TurnInterrupted
from korvid.core.redaction import RedactionRecord

#: History is trimmed to the most recent turns to bound token cost. A turn
#: begins at a "user" message, so trimming on a turn boundary never splits
#: an assistant tool call from its result.
MAX_HISTORY_TURNS = 8

#: Bound for the marked partial text an interrupted turn records: enough
#: for the model to understand what it was saying, small enough that
#: interrupting cannot bloat history.
INTERRUPT_PARTIAL_CHARS = 2_000

#: The exact suffix an interrupted assistant note ends with. A partial is
#: never stored raw — that would replay as a completed answer — so every
#: interrupt marker carries this suffix and nothing that looks finished.
INTERRUPT_MARKER = "[response interrupted]"


class ConversationBudgetError(Exception):
    """A turn's prompt cannot fit the history budget by itself.

    Raised by `start_turn` in strict mode when, after trimming every
    droppable predecessor, the new prompt's own turn still exceeds the
    character budget. The request would go over the wire oversized on its
    very first iteration, so it is rejected — and the offending prompt is
    dropped so it cannot poison later turns — rather than sent.
    """


def _message_chars(message: Mapping[str, Any]) -> int:
    """Approximate a message's context cost: content plus tool-call arguments."""
    total = len(str(message.get("content") or ""))
    for call in message.get("tool_calls") or []:
        total += len(str((call.get("function") or {}).get("arguments") or ""))
    return total


@dataclass(frozen=True, slots=True)
class TurnCheckpoint:
    """Immutable rollback baseline captured when a turn begins.

    `base_index` is the position of the turn's user message; the usage
    fields are the cumulative totals before the turn, so a caller can tell
    exactly what this turn added.
    """

    base_index: int
    total_in: int
    total_out: int
    estimated: bool


@dataclass(frozen=True, slots=True)
class IterationCheckpoint:
    """Immutable rollback baseline captured when an iteration begins.

    `base_index` is the position the iteration's first appended message
    would take, so truncating there unwinds everything the iteration added.
    The turn-usage fields are the running turn totals before the iteration,
    kept so a rollback never loses cost already committed by earlier
    iterations.
    """

    base_index: int
    turn_in: int
    turn_out: int
    turn_estimated: bool


@dataclass(frozen=True, slots=True)
class RequestView:
    """A private, deep-copied request payload plus its provenance.

    `messages` is safe for the caller to mutate — it shares nothing with
    retained history. `ingress` maps a message's index in `messages` to the
    redactions applied to it before it entered history, and `tool_errors`
    holds the indices of tool results the producer declared failures.
    Both are projected onto these exact indices, including the ephemeral
    prefix, so the outbound boundary can address them positionally.
    """

    messages: list[dict[str, Any]]
    ingress: dict[int, tuple[RedactionRecord, ...]]
    tool_errors: frozenset[int]


@dataclass(frozen=True, slots=True)
class _Provenance:
    """What the boundary must know about a message its text cannot say.

    The redactions already applied to it on the way in, and — for a tool
    result — whether the producer declared it a failure. Neither is
    recoverable from the text: a redaction that removed its evidence leaves
    nothing to rediscover, and an ``ERROR: ...`` string is indistinguishable
    from a document that happens to say the same thing.
    """

    message: dict[str, Any]
    records: tuple[RedactionRecord, ...]
    error: bool


@dataclass
class _LiveIteration:
    """Mutable accumulators for the iteration a provider is streaming.

    Settled into the turn totals when the assistant message lands, or read
    by `finalize_interrupt` when a cancellation strikes first.
    """

    base_index: int
    prompt_estimate: int
    text: str = ""
    transmitted: bool = False
    in_tok: int = 0
    out_tok: int = 0
    has_usage: bool = False
    #: Set once the assistant message is appended: the streamed output is
    #: now durable history, so an interrupt must not also mark it partial.
    settled: bool = False
    #: Characters of tool calls this iteration streamed. Kept apart from
    #: `text`: they are generated output and must be charged, but they are
    #: not the assistant's partial answer and must never be replayed as
    #: one in the interrupted-turn note.
    tool_call_chars: int = 0


class ConversationState:
    """Durable conversation history with transactional turn/iteration state."""

    def __init__(self, *, max_history_chars: int, strict_history_budget: bool = False) -> None:
        """Initialise empty history.

        Args:
            max_history_chars: The retained-history character budget. Turn
                trimming and the strict pre-flight both measure against it.
            strict_history_budget: When true, `start_turn` enforces the
                budget before the first request and rejects a prompt that
                cannot fit. When false (the default), an oversized turn is
                retained and recovery is left to the caller's
                `drop_oldest_turn`.
        """
        self._max_history_chars = max_history_chars
        self._strict = strict_history_budget
        self._messages: list[dict[str, Any]] = []
        #: Provenance keyed by the identity of the message it describes:
        #: content is not an identifier, and two messages that sanitize to
        #: the same text are still two messages.
        self._provenance: dict[int, _Provenance] = {}
        self._total_in = 0
        self._total_out = 0
        self._estimated = False
        # Per-turn state, valid only while a turn is active.
        self._turn_active = False
        self._turn_base = 0
        self._turn_in = 0
        self._turn_out = 0
        self._turn_estimated = False
        self._interrupted = False
        # Per-iteration state, valid only while an iteration is open.
        self._live: _LiveIteration | None = None
        self._iteration_base = 0

    # -- properties --------------------------------------------------------

    @property
    def messages(self) -> list[dict[str, Any]]:
        """The retained model history (a shallow copy of the message list)."""
        return list(self._messages)

    @property
    def total_tokens(self) -> tuple[int, int]:
        """Cumulative (input, output) token counts across completed turns."""
        return (self._total_in, self._total_out)

    @property
    def usage_estimated(self) -> bool:
        """True if any counted turn lacked provider usage (totals are estimates)."""
        return self._estimated

    @property
    def has_unmatched_tool_calls(self) -> bool:
        """True when a stored tool call has no result, or vice versa.

        The provider protocol requires every assistant tool call to be
        answered by exactly one tool message. Any asymmetry — a call
        awaiting a result mid-turn, or an orphan left by a bad rollback —
        makes the next request invalid.

        Counted, not set-compared: pairing is per *call*, so two calls
        that (wrongly) share an id still need two results, and a set
        would let the repeat cancel the original and report a broken
        history as sound.
        """
        calls = Counter(
            str(call["id"]) for message in self._messages for call in _tool_calls(message)
        )
        results = Counter(
            str(message["tool_call_id"])
            for message in self._messages
            if message.get("role") == "tool"
        )
        return calls != results

    @property
    def retained_tool_call_ids(self) -> frozenset[str]:
        """Every call id retained history still holds, as a copy-owned set.

        An id is only reusable once the message that spent it has left
        history: while it is retained, a second call with the same id
        could not be paired with a result of its own. The caller filtering
        a provider's stream reads this rather than remembering ids itself,
        so trimming and rollback shrink the set for free.
        """
        return frozenset(
            str(call["id"]) for message in self._messages for call in _tool_calls(message)
        )

    @property
    def history_chars(self) -> int:
        """The size retained history is measured at against the budget."""
        return self._history_chars()

    @property
    def turn_active(self) -> bool:
        """True while a started turn has neither completed nor rolled back.

        A turn that a cancellation or an advisory stop abandoned stays
        active until `finalize_interrupt` closes it, which is exactly what
        `AgentSession` (issue #316 task 11) reads to decide whether the
        turn it just stopped still owes a finalization — the alternative
        is the session keeping a shadow copy of this flag and drifting
        from it.
        """
        return self._turn_active

    # -- turn lifecycle ----------------------------------------------------

    def start_turn(self, content: str, records: Sequence[RedactionRecord] = ()) -> TurnCheckpoint:
        """Begin a turn with the user's (already composed) message.

        Old turns are trimmed to make room, then the message is appended.
        In strict mode the budget is re-checked with the new message in
        place: a previous turn that no longer fits is dropped, and a prompt
        that cannot fit even alone is rejected and removed.

        Args:
            content: The user message text, composed by the caller (screen
                context wrapping, etc. are not this module's concern).
            records: Redactions applied to `content` before it arrived.

        Returns:
            A checkpoint carrying the turn's base index and the usage
            totals before it.

        Raises:
            RuntimeError: A turn is already active.
            ConversationBudgetError: Strict mode, and the prompt cannot fit
                the history budget by itself.
        """
        if self._turn_active:
            raise RuntimeError("cannot start a turn while a turn is already active")
        self._trim_history()
        message: dict[str, Any] = {"role": "user", "content": content}
        self._messages.append(message)
        self._remember(message, records, error=False)
        if self._strict:
            # The pre-append trim could not see this message; now that the
            # previous turn is no longer the newest, trimming may drop it.
            self._trim_history()
            if self._history_chars() > self._max_history_chars:
                # Even alone the prompt is too large: drop it so it cannot
                # poison later turns, and report it rather than send it.
                self._truncate(len(self._messages) - 1)
                raise ConversationBudgetError(
                    f"prompt exceeds the history budget ({self._max_history_chars} chars) by itself"
                )
        self._turn_base = len(self._messages) - 1
        self._turn_active = True
        self._interrupted = False
        self._turn_in = 0
        self._turn_out = 0
        self._turn_estimated = False
        self._live = None
        self._iteration_base = len(self._messages)
        return TurnCheckpoint(
            base_index=self._turn_base,
            total_in=self._total_in,
            total_out=self._total_out,
            estimated=self._estimated,
        )

    def complete_turn(self) -> tuple[int, int, bool]:
        """Commit the active turn's usage to the totals and end the turn.

        Returns:
            The turn's (input, output, estimated) usage.

        Raises:
            RuntimeError: No turn is active, an iteration is still open, or
                a tool call is still unanswered.
        """
        if not self._turn_active:
            raise RuntimeError("no active turn to complete")
        if self._live is not None:
            raise RuntimeError("cannot complete a turn while an iteration is open")
        if self.has_unmatched_tool_calls:
            raise RuntimeError("cannot complete a turn with an unmatched tool call")
        turn_in, turn_out, estimated = self._turn_in, self._turn_out, self._turn_estimated
        self._total_in += turn_in
        self._total_out += turn_out
        self._estimated = self._estimated or estimated
        self._end_turn()
        return (turn_in, turn_out, estimated)

    # -- iteration lifecycle ----------------------------------------------

    def start_iteration(self, prompt_estimate: int = 0) -> IterationCheckpoint:
        """Begin a provider iteration within the active turn.

        Args:
            prompt_estimate: The estimated input-token cost of the request
                this iteration will send, measured by the caller on the
                exact prepared payload. Used only to charge a transmitted
                request whose provider omitted usage, so a real request
                never reads as zero input.

        Returns:
            A checkpoint whose base index unwinds everything this iteration
            appends, carrying the turn-usage baseline.

        Raises:
            RuntimeError: No turn is active, or an iteration is already open.
        """
        if not self._turn_active:
            raise RuntimeError("no active turn to iterate")
        if self._live is not None:
            raise RuntimeError("an iteration is already open")
        base = len(self._messages)
        self._iteration_base = base
        self._live = _LiveIteration(base_index=base, prompt_estimate=prompt_estimate)
        return IterationCheckpoint(
            base_index=base,
            turn_in=self._turn_in,
            turn_out=self._turn_out,
            turn_estimated=self._turn_estimated,
        )

    def request_messages(self, *, prefix: Sequence[Mapping[str, Any]] = ()) -> RequestView:
        """Build one request's payload: `prefix` prepended to a deep copy.

        The prefix (system prompt, evidence table) is ephemeral — it is not
        retained — and the body is a deep copy, so the caller may hand the
        result to a provider dialect hook that mutates it without touching
        stored history. Provenance is projected onto the indices of the
        returned list, prefix included.

        Args:
            prefix: Ephemeral messages to prepend (typically the system
                message and evidence note).

        Returns:
            A `RequestView` the caller owns outright.

        Raises:
            RuntimeError: No turn is active.
        """
        if not self._turn_active:
            raise RuntimeError("no active turn to build a request for")
        prefix_messages = [copy.deepcopy(dict(message)) for message in prefix]
        body = [copy.deepcopy(message) for message in self._messages]
        offset = len(prefix_messages)
        ingress: dict[int, tuple[RedactionRecord, ...]] = {}
        tool_errors: set[int] = set()
        for index, message in enumerate(self._messages):
            entry = self._provenance.get(id(message))
            if entry is None or entry.message is not message:
                continue
            if entry.records:
                ingress[offset + index] = entry.records
            if entry.error:
                tool_errors.add(offset + index)
        return RequestView(
            messages=prefix_messages + body,
            ingress=ingress,
            tool_errors=frozenset(tool_errors),
        )

    def record_stream_text(self, text: str = "") -> None:
        """Record a chunk of streamed assistant text for the open iteration.

        Any call marks the request transmitted — the provider produced a
        stream event, so its prompt was really processed — even when `text`
        is empty (a tool-only or usage-only iteration is not free). The
        accumulated text is what a bounded, marked partial is cut from if
        the turn is interrupted.

        Raises:
            RuntimeError: No iteration is open.
        """
        live = self._require_iteration()
        live.transmitted = True
        live.text += text

    def mark_transmitted(self) -> None:
        """Record that this iteration's request reached the provider.

        Handoff proof, not stream content, is what makes a request cost:
        the gateway calls this the moment the provider produced anything at
        all, so a request that ran and then died is still charged its
        prompt while one that never left is charged nothing.

        Raises:
            RuntimeError: No iteration is open.
        """
        self._require_iteration().transmitted = True

    def record_stream_tool_call(self, name: str, arguments: str) -> None:
        """Record a streamed tool call as generated output.

        Counted apart from the assistant's text: a tool-only iteration
        generated real output even though nothing readable streamed, and an
        interruption must charge for it — but the call is not an answer, so
        it must never leak into the partial note a cancelled turn stores.

        Args:
            name: The tool name the provider streamed.
            arguments: The raw argument text the provider streamed.

        Raises:
            RuntimeError: No iteration is open.
        """
        live = self._require_iteration()
        live.transmitted = True
        live.tool_call_chars += len(name) + len(arguments)

    def commit_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Record provider-reported usage for the open iteration.

        Accumulates across usage events and marks the iteration transmitted
        and exactly counted, so its cost is committed as real (never
        estimated) when the assistant message lands or the turn is
        interrupted.

        Raises:
            RuntimeError: No iteration is open.
        """
        live = self._require_iteration()
        live.transmitted = True
        live.in_tok += input_tokens
        live.out_tok += output_tokens
        live.has_usage = True

    def append_assistant(self, text: str, tool_calls: Sequence[Mapping[str, Any]] = ()) -> None:
        """Append the iteration's assistant message and settle its usage.

        Only the tool calls given are stored, in OpenAI shape; the caller
        is responsible for having dropped any excess or partial calls, so
        exactly one tool result is expected per stored call. Settling folds
        this iteration's usage into the turn: reported counts exactly,
        otherwise a heuristic estimate for a transmitted request.

        Raises:
            RuntimeError: No iteration is open.
        """
        live = self._require_iteration()
        message: dict[str, Any] = {"role": "assistant", "content": text}
        stored_calls = [
            {
                "id": str(call["id"]),
                "type": "function",
                "function": {"name": str(call["name"]), "arguments": str(call["arguments"])},
            }
            for call in tool_calls
        ]
        if stored_calls:
            message["tool_calls"] = stored_calls
        self._messages.append(message)
        self._settle_iteration(live, stored_calls)

    def append_tool_result(
        self,
        call_id: str,
        content: str,
        records: Sequence[RedactionRecord] = (),
        *,
        error: bool = False,
    ) -> None:
        """Append a tool result paired to a pending assistant tool call.

        Args:
            call_id: The id of the assistant tool call this answers.
            content: The (already sanitized and bounded) result text.
            records: Redactions applied to `content` before storage.
            error: Whether the producer declared this result a failure —
                the boundary's verdict, not a guess from the text.

        Raises:
            RuntimeError: No turn is active, or there is no unanswered tool
                call for a result to pair with.
        """
        if not self._turn_active:
            raise RuntimeError("no active turn to record a tool result for")
        if not self.has_unmatched_tool_calls:
            raise RuntimeError("no pending tool call for this result")
        message: dict[str, Any] = {"role": "tool", "tool_call_id": call_id, "content": content}
        self._messages.append(message)
        self._remember(message, records, error=error)

    # -- recovery and interruption ----------------------------------------

    def abandon_iteration(self) -> None:
        """Discard the open iteration, keeping the cost it really incurred.

        For a request that failed mid-stream: everything the iteration
        appended goes — an assistant message whose tool calls can never be
        answered would invalidate every later request — while completed
        iterations of the same turn stay. A transmitted request is charged
        (reported usage exactly, otherwise the prompt estimate plus what
        streamed); one that never reached the provider is charged nothing.

        Raises:
            RuntimeError: No iteration is open.
        """
        live = self._require_iteration()
        self._truncate(live.base_index)
        in_tok, out_tok, estimated = self._in_flight_usage(live)
        self._turn_in += in_tok
        self._turn_out += out_tok
        self._turn_estimated = self._turn_estimated or estimated
        live.settled = True
        self._live = None

    def rollback_turn(self) -> tuple[int, int, bool]:
        """Drop the whole active turn, committing the cost it really paid.

        For a turn that produced something the boundary will not send: the
        history it built is removed down to (and including) the user
        message that started it, so no unanswerable tool call and no
        unsendable result can survive into the next request, while the
        tokens already spent are committed to the totals rather than
        silently forgiven.

        Returns:
            The rolled-back turn's (input, output, estimated) usage.

        Raises:
            RuntimeError: No turn is active.
        """
        if not self._turn_active:
            raise RuntimeError("no active turn to roll back")
        in_flight_in, in_flight_out, estimated = self._in_flight_usage(self._live)
        turn_in = self._turn_in + in_flight_in
        turn_out = self._turn_out + in_flight_out
        estimated = self._turn_estimated or estimated
        self._truncate(self._turn_base)
        self._total_in += turn_in
        self._total_out += turn_out
        self._estimated = self._estimated or estimated
        self._end_turn()
        return (turn_in, turn_out, estimated)

    def drop_oldest_turn(self) -> int:
        """Drop the oldest retained turn; return how many messages went.

        Recovery for an over-ceiling request: shrinking the same
        conversation lets a long session keep working. The turn in flight
        is never a candidate — the current prompt is what the request is
        *for* — so when only one turn remains nothing is dropped.

        Raises:
            RuntimeError: No turn is active.
        """
        if not self._turn_active:
            raise RuntimeError("no active turn to recover")
        user_indices = self._user_indices()
        if len(user_indices) <= 1:
            return 0
        start, cut = user_indices[0], user_indices[1]
        del self._messages[start:cut]
        removed = cut - start
        self._shift_bases(removed)
        self._forget_dropped()
        return removed

    def finalize_interrupt(self) -> TurnInterrupted:
        """Repair state after the turn's driver was cancelled.

        The active iteration may have left a partial footprint at an
        arbitrary await point, so this:

        - truncates everything the active iteration appended (an assistant
          message whose tool calls lack results would break the protocol;
          completed prior iterations stay);
        - appends a bounded assistant note ending with the interrupt
          marker — with capped partial text when any streamed, never the
          raw partial, which would replay as a finished answer;
        - commits only usage that was really transmitted: reported counts
          exactly, an acknowledged-but-usage-less stream by estimate.

        One-shot: it ends the turn, so a second call — which would only
        duplicate the marker and the usage — raises instead.

        Returns:
            The interrupted turn's committed usage.

        Raises:
            RuntimeError: No turn is active (never started, already
                completed, or already interrupted).
        """
        if self._interrupted:
            raise RuntimeError("finalize_interrupt already called for this turn")
        if not self._turn_active:
            raise RuntimeError("no active turn to interrupt")
        live = self._live
        self._truncate(self._iteration_base)
        partial = live.text if live is not None else ""
        if partial:
            note = f"{partial[:INTERRUPT_PARTIAL_CHARS]}\n\n{INTERRUPT_MARKER}"
        else:
            note = INTERRUPT_MARKER
        self._messages.append({"role": "assistant", "content": note})
        in_flight_in, in_flight_out, estimated = self._in_flight_usage(live)
        total_in = self._turn_in + in_flight_in
        total_out = self._turn_out + in_flight_out
        self._total_in += total_in
        self._total_out += total_out
        self._estimated = self._estimated or estimated
        self._interrupted = True
        self._end_turn()
        return TurnInterrupted(input_tokens=total_in, output_tokens=total_out, estimated=estimated)

    # -- internals ---------------------------------------------------------

    def _require_iteration(self) -> _LiveIteration:
        if self._live is None:
            raise RuntimeError("no open iteration")
        return self._live

    def _in_flight_usage(self, live: _LiveIteration | None) -> tuple[int, int, bool]:
        """The interrupted iteration's cost, and whether it was estimated."""
        if live is None or live.settled:
            return (0, 0, self._turn_estimated)
        if live.has_usage:
            return (live.in_tok, live.out_tok, self._turn_estimated)
        if live.transmitted:
            # The request was acknowledged, so its prompt was real cost;
            # the streamed text and calls are the only generated output we
            # can see.
            return (live.prompt_estimate, (len(live.text) + live.tool_call_chars) // 4, True)
        return (0, 0, self._turn_estimated)

    def _settle_iteration(
        self, live: _LiveIteration, stored_calls: Sequence[Mapping[str, Any]]
    ) -> None:
        """Fold a completed iteration's usage into the turn, then close it."""
        if live.has_usage:
            self._turn_in += live.in_tok
            self._turn_out += live.out_tok
        elif live.transmitted:
            self._turn_in += live.prompt_estimate
            self._turn_out += _output_estimate(live.text, stored_calls)
            self._turn_estimated = True
        live.settled = True
        self._live = None

    def _end_turn(self) -> None:
        self._turn_active = False
        self._live = None

    def _remember(
        self, message: dict[str, Any], records: Sequence[RedactionRecord], *, error: bool
    ) -> None:
        """Keep what the boundary cannot re-derive about one message.

        The entry holds the message itself, which both identifies it and
        keeps it alive: an id whose object had been freed could be handed
        to a later allocation and silently adopt someone else's records.
        Copy-owned: the record tuple is this state's, not the caller's.
        """
        if records or error:
            self._provenance[id(message)] = _Provenance(message, tuple(records), error)

    def _forget_dropped(self) -> None:
        """Drop provenance for content no longer in history."""
        if not self._provenance:
            return
        live = {id(message) for message in self._messages}
        self._provenance = {
            identity: entry for identity, entry in self._provenance.items() if identity in live
        }

    def _user_indices(self) -> list[int]:
        return [index for index, message in enumerate(self._messages) if message["role"] == "user"]

    def _history_chars(self) -> int:
        return sum(_message_chars(message) for message in self._messages)

    def _truncate(self, start: int) -> None:
        """Drop history from `start` on, and the records that described it."""
        del self._messages[start:]
        self._forget_dropped()

    def _trim_history(self) -> None:
        """Keep at most MAX_HISTORY_TURNS turns, then drop oldest complete
        turns until within the character budget, always retaining the newest."""
        before = len(self._messages)
        user_indices = self._user_indices()
        if len(user_indices) >= MAX_HISTORY_TURNS:
            cut = user_indices[-(MAX_HISTORY_TURNS - 1)]
            self._messages = self._messages[cut:]
            self._shift_bases(cut)
        while self._history_chars() > self._max_history_chars:
            user_indices = self._user_indices()
            if len(user_indices) <= 1:
                break
            cut = user_indices[1]
            self._messages = self._messages[cut:]
            self._shift_bases(cut)
        if len(self._messages) < before:
            self._forget_dropped()

    def _shift_bases(self, removed: int) -> None:
        """Keep the in-flight turn/iteration indices on the same messages.

        Trimming and recovery drop history from in front of the current
        turn; without this a rollback slice would point past the turn it
        must delete.
        """
        self._turn_base = max(0, self._turn_base - removed)
        self._iteration_base = max(0, self._iteration_base - removed)
        if self._live is not None:
            self._live.base_index = max(0, self._live.base_index - removed)


def _tool_calls(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls") or []
    return [call for call in calls if isinstance(call, dict)]


def _output_estimate(text: str, stored_calls: Sequence[Mapping[str, Any]]) -> int:
    """Approximate one iteration's generated output when usage was omitted.

    Streamed text plus the structured tool-call payloads: a tool-only
    iteration still generated the call it emitted.
    """
    total = len(text)
    for call in stored_calls:
        function = call.get("function") or {}
        total += len(str(function.get("name") or "")) + len(str(function.get("arguments") or ""))
    return total // 4
