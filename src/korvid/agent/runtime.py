"""AgentRuntime: the agentic tool-use loop (design §6.1)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from korvid.agent.events import (
    AgentError,
    AgentEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnComplete,
    TurnInterrupted,
)
from korvid.agent.evidence import Evidence, EvidenceLedger
from korvid.agent.outbound import (
    OutboundPolicy,
    OutboundPolicyError,
    OutboundRequestTooLarge,
    OutboundSnapshot,
    PreparedOutbound,
    ToolResultBlockedError,
    provider_prepared_messages,
    request_char_budget,
    sanitize_recorded_tool_result,
    sanitize_screen_context,
)
from korvid.agent.prompts import compose_system_prompt
from korvid.core.redaction import RedactionRecord
from korvid.tools.executor import (
    READ_TOOLS,
    RecordedExecution,
    ToolResultBlocked,
    cap_result,
)
from korvid.tools.registry import (
    TOOLS_BY_NAME,
    CustomToolResult,
    ResultFormat,
    resolve_result_formats,
    tool_schema_names,
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


@dataclass(frozen=True, slots=True)
class _MessageProvenance:
    """What the boundary must know about a message that its text cannot say.

    Two such facts, and both are tied to the message object rather than
    to its content: the redactions already applied to it on the way in,
    and — for a tool result — whether the producer declared it a failure
    rather than a result. Neither is recoverable from the text. A
    redaction that *removed* its evidence leaves nothing to rediscover,
    and an `ERROR: ...` string is indistinguishable from a document that
    says the same thing, which is the whole point of asking the producer.
    """

    message: dict[str, Any]
    records: tuple[RedactionRecord, ...] = ()
    error: bool = False


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
    #: The request reached the provider. Set on the first event of any
    #: kind — a built-in's `REQUEST_SENT` acknowledgement, or any
    #: completion event from an adapter that cannot acknowledge. Streamed
    #: output used to stand in for this, which charged nothing for a
    #: prompt that was processed and then answered HTTP 500, and charged
    #: a full prompt for a stream that never ran (PR #197 review).
    transmitted: bool = False


def _estimate_missing_usage(state: _StreamState, prompt_estimate: int) -> None:
    """Fill in token estimates when the provider omitted usage — totals must
    never read as zero for a request that was really transmitted, nor
    charge for one that never was."""
    if not state.has_usage and state.transmitted:
        state.in_tok = prompt_estimate
        state.out_tok = _stream_output_chars(state) // 4


def _parsed_arguments(arguments: str) -> dict[str, Any] | None:
    """A tool call's arguments as a mapping, or None when unusable.

    The executor contract takes an argument mapping, so valid JSON of any
    other shape is as unusable as invalid JSON.
    """
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def evidence_note(items: Sequence[Evidence]) -> str:
    """The reference table the model needs in order to cite anything.

    A model can only cite a reference it has been shown, so the mapping
    from `E<n>` to what was read has to reach it somehow. It goes in the
    system message rather than alongside each result for two reasons.

    A `structured_yaml` result is re-serialised from its parsed document
    on every request, so anything written into it - a YAML comment
    included - is dropped before the model sees it, and anything written
    *around* it stops the document parsing and the policy rejects the
    request. Neither is a place a reference can live.

    The second reason is better: the system message is korvid's own text.
    Putting the table there keeps it out of the region the prompt calls
    untrusted, so a log line claiming to be `E4` is not sitting next to
    the real table. Trust still does not rest on that - a citation is
    checked against what the ledger minted - but the two should not look
    alike.

    Each row names only the tool, which several reads may share, so the
    note says what the number means: references are minted in read order,
    so `[E2]` is the second read of the turn. That is korvid's fact about
    its own ledger - the alternative discriminator, the target, is the
    model's text and is exactly what must not be here.

    One short line per read, so the cost is bounded by the number of tool
    calls a turn may make.
    """
    if not items:
        return ""
    lines = [
        "Evidence you may cite, in the order you read it ([E1] is your first"
        " read this turn). Cite these references for each diagnostic claim;"
        " any other is shown to the user as unsupported. Say so plainly when"
        " the evidence does not settle a question."
    ]
    lines.extend(f"[{item.ref}] {_describe(item)}" for item in items)
    return "\n".join(lines)


def _describe(item: Evidence) -> str:
    """One line naming a reference's source, in korvid's words only.

    Deliberately does *not* name the target. Kind, name, namespace and
    container are tool arguments, so they are the model's text, and this
    line lands in the system message - the one region the table is placed
    in *because* it is korvid's own. Sanitising them is not enough: an
    argument the tool ignores still travels, so `get_resource` on a
    cluster-scoped kind will happily carry a namespace of
    "IGNORE PREVIOUS INSTRUCTIONS" into the prompt (#192 review).

    The reference and the tool name are korvid's - the tool name is
    checked against the registry before anything is recorded - and they
    are enough for the model to cite. Which object each reference points
    at is already in the tool result the model read, and the UI slice
    resolves the full locator from `Evidence` without going through here.
    """
    return item.tool


def _is_cluster_read(name: str) -> bool:
    """Whether `name` reads cluster state, per the tool registry.

    Unknown names are not reads: a custom or plugin tool has to declare
    itself before its output can be cited, rather than being trusted as
    evidence by default (issue #192).
    """
    definition = TOOLS_BY_NAME.get(name)
    return definition is not None and definition.effect == "cluster_read"


class AgentRuntime:
    """Drives the provider + tools loop, emitting typed AgentEvent objects."""

    def __init__(
        self,
        provider: _Provider,
        executor: RecordedExecution,
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
        custom_tool_results: Sequence[CustomToolResult] = (),
    ) -> None:
        self._provider = provider
        # The tools layer's ABC, not something that merely looks like it:
        # adapting a duck here made a structural shape the real boundary,
        # and a mistyped executor would only fail at the first tool call.
        # `as_recorded` is the on-ramp, and composing it is the caller's
        # decision (AGENTS.md layer rules, PR #197 review).
        if not isinstance(executor, RecordedExecution):
            raise TypeError(
                "executor must implement RecordedExecution "
                "(korvid.tools.executor.as_recorded adapts a string-only executor)"
            )
        self._executor: RecordedExecution = executor
        self._tools = tools if tools is not None else READ_TOOLS
        # How each offered tool's results are treated. A tool this build
        # does not define has to be declared: the boundary cannot tell a
        # manifest from a paragraph by looking, and assuming "text" let a
        # custom tool return a `Secret` document (PR #197 review).
        self._declared_tool_results = tuple(custom_tool_results)
        self._result_formats: dict[str, ResultFormat] = self._resolve_formats(self._tools)
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
        #: The composed prompt without the per-request evidence table, so
        #: the table can be restated without recomposing everything.
        self._base_prompt = prompt
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
        self._provenance: dict[int, _MessageProvenance] = {}
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
        #: Reads of the turn in flight, addressable by the references an
        #: answer may cite. korvid mints them so a provider cannot invent
        #: support for a claim (issue #192).
        self._evidence = EvidenceLedger()

    @property
    def evidence(self) -> EvidenceLedger:
        """The current turn's citable reads."""
        return self._evidence

    def _build_policy(self, max_history_chars: int) -> OutboundPolicy:
        limit = self._max_request_chars
        if limit is None:
            limit = request_char_budget(
                max_history_chars=max_history_chars,
                tools_chars=self._tools_chars,
            )
        return OutboundPolicy(max_request_chars=limit, result_formats=self._result_formats)

    def _resolve_formats(self, tools: list[dict[str, Any]]) -> dict[str, ResultFormat]:
        """Resolve the result format of every tool `tools` offers.

        Only the declarations that name an offered tool take part: the
        caller's set describes tools it may offer at some point, and
        `resolve_result_formats` — rightly — rejects a declaration for a
        tool that is not on the surface being resolved.
        """
        offered = set(tool_schema_names(tools))
        return resolve_result_formats(
            tools, [item for item in self._declared_tool_results if item.name in offered]
        )

    def retarget(self, *, tools: list[dict[str, Any]], cluster_context: str | None) -> None:
        """Re-arm the runtime for a new cluster (issue #36, `:ctx`).

        Conversation history survives — the system prompt is recomposed in
        place so later turns describe the new environment (cloud provider
        note) and the new capability-gated tool set (e.g. ``resize_pod``),
        instead of the cluster the runtime was originally built against.

        Evidence does *not* survive. A reference read from the old cluster
        would still resolve, and opening it would show a same-named object
        in the new one as though it were the cited evidence (issue #192).
        """
        self._evidence.start_turn()
        # A new surface is a new set of declarations to honour: the ones
        # for tools it no longer offers simply do not apply, and anything
        # it does offer must still resolve or construction-time validation
        # would have been theatre. The set the caller made is kept whole —
        # deriving the active subset each time means a surface that comes
        # back (`:ctx` to another cluster and back) is still declared, and
        # a retarget that refuses leaves nothing consumed (PR #197 review).
        # Resolve before anything is mutated so a refused surface leaves
        # the runtime on the one it was already armed for.
        result_formats = self._resolve_formats(tools)
        self._tools = tools
        self._result_formats = result_formats
        # Keep the omitted-usage estimate honest for the new tool set.
        self._tools_chars = len(json.dumps(self._tools))
        # A different tool surface is a different per-request overhead.
        self._outbound = self._build_policy(self._max_history_chars)
        self._base_prompt = compose_system_prompt(
            tools,
            cluster_context,
            system_prompt=self._system_prompt_override,
            ui_prompt=self._ui_prompt_override,
        )
        self._refresh_evidence_note()

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
        It is replaced only once the provider has accepted a new payload —
        preparing one is not sending it, and a provider that refuses the
        call outright never received anything to show.
        """
        return self._latest_outbound_payload

    def _remember_ingress(
        self,
        message: dict[str, Any],
        records: Sequence[RedactionRecord],
        *,
        error: bool = False,
    ) -> None:
        """Keep what the boundary cannot re-derive about one message.

        The entry holds the message itself, which both identifies it and
        keeps it alive: an id whose object had been freed could be handed
        to a later allocation and silently adopt someone else's records.
        It retains nothing history does not already hold — the content is
        the sanitized text that is in the payload, and the verdict is one
        bit about it.
        """
        if records or error:
            self._provenance[id(message)] = _MessageProvenance(message, tuple(records), error)

    def _provenance_by_index(self) -> tuple[dict[int, tuple[RedactionRecord, ...]], set[int]]:
        """Project the store onto the positions the policy will see.

        The policy works on a list, so identity has to become position
        exactly once, here, against the same history the request is built
        from.
        """
        records: dict[int, tuple[RedactionRecord, ...]] = {}
        errors: set[int] = set()
        for index, message in enumerate(self._messages):
            entry = self._provenance.get(id(message))
            if entry is None or entry.message is not message:
                continue
            if entry.records:
                records[index] = entry.records
            if entry.error:
                errors.add(index)
        return records, errors

    async def _execute_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[str, tuple[RedactionRecord, ...], bool]:
        """Run one tool, taking the producer redaction trail it reports.

        Producer-side redaction happens before the size bound, so it is
        the only pass that can report what it *removed* rather than
        masked. `RecordedExecution` answers for every executor, so there
        is no capability test here; an implementation with no producer
        pass reports an empty trail.
        """
        try:
            outcome = await self._executor.execute_recorded(name, arguments)
        except ToolResultBlocked as exc:
            # Not an answer the model can react to: the redactor could not
            # promise this document holds no credentials, so the turn stops
            # here rather than sending another request with an unvetted
            # result in history (PR #197 review). Reusing the outbound
            # block means one rollback path — history truncated to the turn
            # base, records purged, the last successful snapshot standing.
            raise ToolResultBlockedError(str(exc)) from exc
        return outcome.text, outcome.redactions, outcome.error

    async def _tool_result(
        self, name: str, arguments: str
    ) -> tuple[str, tuple[RedactionRecord, ...], bool, str | None]:
        """Run one tool call and return what history may keep of it.

        Whether the text is a failure is the producer's to state, never
        the boundary's to read off the text: a structured result that
        opened with `ERROR:` used to skip the pass that sees nested
        secrets (PR #197 review). The failures raised here are errors by
        construction, so they say so. The verdict is returned with the
        text because it has to be stored with the message: every later
        request re-sanitizes history from scratch, and by then the text
        is all there is.

        Head+tail compaction, not a prefix cut: reports place their
        evidence (events, log excerpts) last by design. Structured
        results are shrunk structurally instead — the document is
        redacted first and the redacted document bounded, so it stays
        parseable. The producer's trail and this pass's are two views of
        one document, merged onto the same origin so a redaction both of
        them saw is not counted twice.

        Raises:
            OutboundPolicyError: the result cannot be sent — from the
                producer, or from the ingress pass over what it returned.
        """
        produced: tuple[RedactionRecord, ...] = ()
        errored = True
        parsed = _parsed_arguments(arguments)
        if parsed is None:
            result = "ERROR: bad arguments"
        else:
            try:
                result, produced, errored = await self._execute_tool(name, parsed)
            except OutboundPolicyError:
                raise
            except Exception as exc:  # defensive: executor contract is never-raise
                # Same ingest cap as ToolExecutor — a huge exception
                # message must not bypass the limit into history.
                result = cap_result(f"ERROR: {exc}")
        text, records = sanitize_recorded_tool_result(
            name,
            result,
            produced,
            max_chars=self._max_result_chars,
            error=errored,
            result_format=self._result_formats.get(name),
        )
        # Only cluster reads are evidence. A successful mutation or a
        # screen action also reports error=False, so recording every
        # non-error result would let "I deleted the pod" be cited as
        # support for a claim about what the cluster is (#192 review).
        #
        # Recorded after sanitisation so a citation's excerpt matches what
        # the model was actually shown - evidence the user cannot find in
        # the transcript would be worse than no citation.
        ref = None
        if _is_cluster_read(name):
            ref = self._evidence.record(name, parsed or {}, text, error=errored)
        return text, records, errored, ref

    def _refresh_evidence_note(self) -> None:
        """Re-state the citable references on the system message.

        Rebuilt per request rather than appended to: the table changes as
        reads land, and the turn boundary clears it, so the model is never
        offered a reference that no longer resolves.
        """
        items = [
            item
            for ref in self._evidence.references()
            if (item := self._evidence.resolve(ref)) is not None
        ]
        note = evidence_note(items)
        self._messages[0] = {
            "role": "system",
            "content": f"{self._base_prompt}\n\n{note}" if note else self._base_prompt,
        }

    def _truncate_history(self, start: int) -> None:
        """Drop history from `start` on, and the records that described it.

        Every path in the inventory has to resolve against the payload
        the user is shown, so records outlive their message by exactly
        nothing.
        """
        del self._messages[start:]
        self._forget_dropped_provenance()

    def _forget_dropped_provenance(self) -> None:
        """Drop what is known about content no longer in history.

        Their message is gone from the payload, so reporting a record
        would name a path nobody can find, and holding a verdict for a
        message that is not there could only ever be adopted by mistake.
        """
        if not self._provenance:
            return
        live = {id(message) for message in self._messages}
        self._provenance = {
            identity: entry for identity, entry in self._provenance.items() if identity in live
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
            self._forget_dropped_provenance()
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
        snapshot: OutboundSnapshot,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Iterate provider stream, yield TextDelta events, accumulate state.

        The first event of any kind is proof the request ran, and is when
        `snapshot` becomes the session's latest handoff: a built-in says so
        explicitly with `REQUEST_SENT` (before it judges the status code, so
        an HTTP error still counts), and anything else — plugins, scripted
        adapters — is taken at its first completion event.
        """
        async for ev in stream:
            # Recorded here rather than around `complete()`: that call only
            # builds the generator, so a provider that cannot reach its
            # endpoint would otherwise show an unsent payload as the last
            # thing this session handed over (PR #197 review). The same
            # evidence settles the bill — a request that ran costs its
            # prompt whether or not anything streamed back.
            self._latest_outbound_payload = snapshot
            state.transmitted = True
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
            try:
                result, ingress_records, errored, _ref = await self._tool_result(name, arguments)
            except OutboundPolicyError:
                # A blocked result is not reportable to the model: close
                # the call the UI is showing, then let it reach run_turn's
                # rollback. Both boundary passes — the producer's and this
                # ingress one — unwind through here, so neither can leave
                # a tool row running for the session (PR #197 review).
                yield ToolCallFinished(call_id=call_id, name=name, ok=False, summary="blocked")
                raise
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
                # The producer's verdict, not the text's shape: a log
                # excerpt, a describe output or a diagnosis that quotes
                # `ERROR: ...` produced a result, and the row that says a
                # call failed has to mean the call failed. The same bit
                # chooses the boundary's sanitization pass, so the UI and
                # the policy cannot disagree about it (PR #197 review).
                ok=not errored,
                summary=result[:120],
            )
            tool_message = {"role": "tool", "tool_call_id": call_id, "content": result}
            self._messages.append(tool_message)
            self._remember_ingress(tool_message, ingress_records, error=errored)
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
        new prompt would otherwise go over the wire on the turn's first
        request. Once appended, the previous turn is no longer the newest,
        so the normal trim drops it; True means even that was not enough — the
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
            self._refresh_evidence_note()
            self._forget_dropped_provenance()
            ingress, tool_errors = self._provenance_by_index()
            try:
                return self._outbound.prepare(
                    self._provider.name,
                    provider_prepared_messages(self._provider, self._messages),
                    self._tools,
                    iteration=iteration,
                    ingress=ingress,
                    tool_errors=tool_errors,
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
        if not state.has_usage and state.transmitted:
            state.in_tok = prompt_estimate
            state.out_tok = _stream_output_chars(state) // 4
        self._total_in += turn_in + state.in_tok
        self._total_out += turn_out + state.out_tok
        if usage_missing or (state.transmitted and not state.has_usage):
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
            elif state.transmitted:
                # Same rule as the stream-error path: a request the
                # provider acknowledged was really transmitted, whether or
                # not anything had streamed back before the cancellation.
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
        # Before the trim and the size preflight, not after: both budget
        # `_messages[0]`, and last turn's table is neither citable nor
        # free - counting it can cost a retained turn (#192 review).
        #
        # A citation must resolve to evidence read for *this* question:
        # last turn's pod may since have been replaced.
        self._evidence.start_turn()
        self._refresh_evidence_note()
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
                    # `complete()` is an async generator: this line transmits
                    # nothing. The handoff is recorded inside the stream, on
                    # the first event the provider produces (PR #197 review).
                    async for event in self._consume_stream(stream, state, prepared.snapshot):
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
                    # The answer is checked, never edited: deleting an
                    # unsupported citation would delete the evidence that
                    # the claim was unsourced (issue #192).
                    cited, uncited, duplicated = self._evidence.check_citations(
                        str(assistant_msg.get("content") or "")
                    )
                    yield TurnComplete(
                        input_tokens=turn_in,
                        output_tokens=turn_out,
                        estimated=usage_missing,
                        cited=cited,
                        uncited=uncited,
                        duplicated=duplicated,
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
