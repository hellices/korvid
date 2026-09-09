"""Turn diagnostics: attribute a turn's latency without capturing its content.

`TurnDiagnosticsRecorder` is a pure state machine that a turn owns from start
to finish (design doc: "Agent Latency Diagnostics Design", §Architecture). It
records *when* things happened — provider-round and tool boundaries, on an
injected monotonic clock — and never sees a prompt, a tool argument, a tool
result, a model or provider name, or a Kubernetes identifier. The terminal
`TurnDiagnostics` snapshot it produces is safe to log and safe to render.

`TurnDiagnosticsFactory` owns the two injectable functions (clock and local
correlation-ID factory) so tests can be fully deterministic and production
code defaults to `time.monotonic` and a random locally generated ID. The ID
is available to diagnostic log handlers but is never sent to providers.
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Final

MAX_USAGE_TOKENS: Final = 1_000_000_000
"""Shared ceiling for plugin usage and optional provider token metrics."""


class TurnOutcome(Enum):
    """How a started turn ended."""

    SUCCESS = "success"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class AgentPhase(Enum):
    """What the engine is doing right now, for the status line and `AgentPhaseChanged`."""

    WAITING_FOR_MODEL = "waiting for model"
    RUNNING_TOOL = "running tool"
    COMPOSING_ANSWER = "composing answer"


class DiagnosticsStateError(RuntimeError):
    """A diagnostics method was called out of order.

    This is an internal invariant violation, not a user-facing failure: the
    engine's call sites are fixed (design doc §Engine integration), so this
    can only fire from a programming error and must never be swallowed.
    """


@dataclass(frozen=True, slots=True)
class ProviderRuntimeMetrics:
    """Normalized, numeric-only runtime metrics an adapter may report.

    Every field is optional because OpenAI-compatible providers and plugins
    may omit the event entirely (design doc §Provider metrics); none of them
    ever carries a model name, a prompt, or any other free-form text.
    """

    total_seconds: float | None = None
    load_seconds: float | None = None
    prompt_eval_seconds: float | None = None
    prompt_tokens: int | None = None
    generation_seconds: float | None = None
    generation_tokens: int | None = None


#: Event type a provider adapter may yield to report normalized, numeric-only
#: runtime metrics for the round currently streaming. It is optional: an
#: OpenAI-compatible provider or a plugin may omit it entirely, and the engine
#: and UI behave correctly on local monotonic timings alone (design doc
#: §Provider metrics).
PROVIDER_METRICS_EVENT: Final[str] = "provider_metrics"


def _metric_seconds(value: Any) -> float | None:
    """A non-negative float, or `None` for an unusable or absent value."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0.0 else None


def _metric_count(value: Any) -> int | None:
    """A non-negative int, or `None` for an unusable or absent value."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_USAGE_TOKENS else None


def provider_metrics_from_event(event: Mapping[str, Any]) -> ProviderRuntimeMetrics:
    """Build normalized runtime metrics from a provider's metrics event.

    The single, provider-neutral place that turns a `PROVIDER_METRICS_EVENT`
    dict into a `ProviderRuntimeMetrics`. Every field is read defensively:
    an absent, negative, or non-numeric value becomes `None` rather than a
    fabricated measurement, so a partly filled event never poisons the
    round it is attached to. The event carries only numbers — no model or
    provider name, no free-form text — so nothing here can smuggle content
    into the diagnostic snapshot.
    """
    return ProviderRuntimeMetrics(
        total_seconds=_metric_seconds(event.get("total_seconds")),
        load_seconds=_metric_seconds(event.get("load_seconds")),
        prompt_eval_seconds=_metric_seconds(event.get("prompt_eval_seconds")),
        prompt_tokens=_metric_count(event.get("prompt_tokens")),
        generation_seconds=_metric_seconds(event.get("generation_seconds")),
        generation_tokens=_metric_count(event.get("generation_tokens")),
    )


def provider_metrics_are_empty(metrics: ProviderRuntimeMetrics) -> bool:
    """True when a metrics record carries no usable numeric field.

    An event that parsed to all-`None` reports nothing, so the engine can
    skip attaching it rather than recording an empty provider round.
    """
    return all(getattr(metrics, field.name) is None for field in fields(metrics))


@dataclass(frozen=True, slots=True)
class ProviderRoundDiagnostics:
    """Timings for one provider round: prepare, handoff, first event, total.

    `round_number` is one-based and counts provider rounds within the turn
    (a multi-round tool-using turn has more than one). `provider_metrics` is
    `None` whenever the adapter did not report normalized runtime metrics for
    this round.

    Sub-step fields (`prepare_seconds`, `handoff_seconds`,
    `first_event_seconds`) are `None` when the corresponding boundary never
    occurred — for example, a round that failed before `prepare_finished()`
    was called never produced a real prepare duration. `total_seconds` is
    always the actual monotonic elapsed time from round start to close.

    The `*_at_seconds` fields are offsets from round start. Request start
    means dispatch was attempted; acknowledgement is the later moment the
    gateway observed proof of acceptance, not a socket-write timestamp.
    First event includes reasoning/usage; first content is nonempty text or
    a tool call. A boundary that never occurred stays `None`.
    """

    round_number: int
    prepare_seconds: float | None
    handoff_seconds: float | None
    first_event_seconds: float | None
    total_seconds: float
    provider_metrics: ProviderRuntimeMetrics | None = None
    request_started_at_seconds: float | None = None
    request_acknowledged_at_seconds: float | None = None
    first_event_at_seconds: float | None = None
    first_content_at_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ToolDiagnostics:
    """Timing and outcome for one tool execution.

    `sequence` is one-based and counts tool executions within the turn.
    `name` is the registry tool name only — never the arguments or result.
    """

    sequence: int
    name: str
    seconds: float
    ok: bool


@dataclass(frozen=True, slots=True)
class TurnDiagnostics:
    """The immutable, terminal diagnostic snapshot for one started turn."""

    correlation_id: str
    outcome: TurnOutcome
    total_seconds: float
    rounds: tuple[ProviderRoundDiagnostics, ...]
    tools: tuple[ToolDiagnostics, ...]


@dataclass(slots=True)
class _ActiveRound:
    """Mutable bookkeeping for the round currently in progress."""

    round_number: int
    start: float
    prepare_start: float | None = None
    prepare_end: float | None = None
    handoff: float | None = None
    request_start: float | None = None
    first_event: float | None = None
    first_content: float | None = None
    provider_metrics: ProviderRuntimeMetrics | None = None


@dataclass(slots=True)
class _ActiveTool:
    """Mutable bookkeeping for the tool currently executing."""

    sequence: int
    name: str
    start: float


class TurnDiagnosticsRecorder:
    """Turn-scoped recorder: one instance per started turn, created by the factory.

    Every public method other than `finalize` corresponds to exactly one
    engine integration boundary (design doc §Engine integration): round and
    tool start/finish, provider metrics, and terminal finalization. Calling a
    method out of the expected order raises `DiagnosticsStateError` instead
    of silently producing a plausible-looking summary.
    """

    def __init__(self, correlation_id: str, clock: Callable[[], float]) -> None:
        self._correlation_id = correlation_id
        self._clock = clock
        self._start = clock()
        self._rounds: list[ProviderRoundDiagnostics] = []
        self._tools: list[ToolDiagnostics] = []
        self._active_round: _ActiveRound | None = None
        self._active_tool: _ActiveTool | None = None
        self._snapshot: TurnDiagnostics | None = None

    @property
    def correlation_id(self) -> str:
        return self._correlation_id

    @property
    def snapshot(self) -> TurnDiagnostics | None:
        """The finalized record, including before its terminal event is delivered."""
        return self._snapshot

    def begin_round(self) -> None:
        """Start a new provider round. Rejects overlapping rounds."""
        self._require_not_finalized()
        if self._active_round is not None:
            raise DiagnosticsStateError(
                "cannot begin a round while another round is already active"
            )
        self._active_round = _ActiveRound(round_number=len(self._rounds) + 1, start=self._clock())

    def prepare_started(self) -> None:
        """Mark the start of request preparation for the active round."""
        round_ = self._require_active_round()
        if round_.prepare_start is not None:
            raise DiagnosticsStateError("prepare already started for the active round")
        round_.prepare_start = self._clock()

    def prepare_finished(self) -> None:
        """Mark the end of request preparation for the active round."""
        round_ = self._require_active_round()
        if round_.prepare_start is None:
            raise DiagnosticsStateError("prepare must start before it can finish")
        if round_.prepare_end is not None:
            raise DiagnosticsStateError("prepare already finished for the active round")
        round_.prepare_end = self._clock()

    def request_handed_off(self) -> None:
        """Record when the gateway observes proof of transport acceptance."""
        round_ = self._require_active_round()
        if round_.prepare_end is None:
            raise DiagnosticsStateError("request handed off before prepare finished")
        if round_.handoff is not None:
            raise DiagnosticsStateError("request already handed off for the active round")
        round_.handoff = self._clock()

    def request_started(self) -> None:
        """Mark dispatch attempted, without claiming the provider received it."""
        round_ = self._require_active_round()
        if round_.prepare_end is None:
            raise DiagnosticsStateError("request started before prepare finished")
        if round_.request_start is not None:
            raise DiagnosticsStateError("request already started for the active round")
        round_.request_start = self._clock()

    def first_model_event(self) -> None:
        """Mark the first streamed model event for the active round."""
        round_ = self._require_active_round()
        if round_.handoff is None:
            raise DiagnosticsStateError("first model event before request handoff")
        if round_.first_event is not None:
            raise DiagnosticsStateError("first model event already recorded for the active round")
        round_.first_event = self._clock()

    def first_content_event(self) -> None:
        """Mark the first nonempty text or tool-call event, excluding reasoning."""
        round_ = self._require_active_round()
        if round_.first_event is None:
            raise DiagnosticsStateError("first content before first model event")
        if round_.first_content is not None:
            raise DiagnosticsStateError("first content already recorded for the active round")
        round_.first_content = self._clock()

    def record_provider_metrics(self, metrics: ProviderRuntimeMetrics) -> None:
        """Attach normalized provider runtime metrics to the active round."""
        round_ = self._require_active_round()
        if round_.provider_metrics is not None:
            raise DiagnosticsStateError("provider metrics already recorded for the active round")
        round_.provider_metrics = metrics

    def end_round(self) -> None:
        """Finish the active round and append its timing record."""
        round_ = self._require_active_round()
        now = self._clock()
        self._rounds.append(self._close_round(round_, at=now))
        self._active_round = None

    def begin_tool(self, name: str) -> None:
        """Start a tool execution. Rejects overlapping tool executions."""
        self._require_not_finalized()
        if self._active_tool is not None:
            raise DiagnosticsStateError("cannot begin a tool while another tool is already active")
        self._active_tool = _ActiveTool(
            sequence=len(self._tools) + 1, name=name, start=self._clock()
        )

    def end_tool(self, *, ok: bool) -> None:
        """Finish the active tool execution and append its timing record."""
        self._require_not_finalized()
        if self._active_tool is None:
            raise DiagnosticsStateError("no active tool to end")
        tool = self._active_tool
        now = self._clock()
        self._tools.append(
            ToolDiagnostics(sequence=tool.sequence, name=tool.name, seconds=now - tool.start, ok=ok)
        )
        self._active_tool = None

    def finalize(self, outcome: TurnOutcome) -> TurnDiagnostics:
        """Finalize the turn, closing any active round or tool at this instant.

        Called at most once per turn (design doc §Interruption,
        §Error handling: provider failure, contained tool failure, and user
        interruption all reach this exactly once). A still-active round or
        tool is closed here rather than raising, because provider failure
        and interruption are expected to catch work mid-flight.
        """
        self._require_not_finalized()
        now = self._clock()
        if self._active_round is not None:
            self._rounds.append(self._close_round(self._active_round, at=now))
            self._active_round = None
        if self._active_tool is not None:
            tool = self._active_tool
            self._tools.append(
                ToolDiagnostics(
                    sequence=tool.sequence, name=tool.name, seconds=now - tool.start, ok=False
                )
            )
            self._active_tool = None
        self._snapshot = TurnDiagnostics(
            correlation_id=self._correlation_id,
            outcome=outcome,
            total_seconds=now - self._start,
            rounds=tuple(self._rounds),
            tools=tuple(self._tools),
        )
        return self._snapshot

    def _close_round(self, round_: _ActiveRound, *, at: float) -> ProviderRoundDiagnostics:
        """Compute the terminal record for a round.

        Sub-steps that never completed are recorded as `None` so callers can
        distinguish "boundary occurred with 0 duration" from "boundary never
        occurred". `total_seconds` is always the actual monotonic elapsed time
        from round start to the closing instant.

        A prepare boundary is present only when both `prepare_started()` and
        `prepare_finished()` were called. A handoff boundary is present only
        when `request_handed_off()` was called. A first-event boundary is
        present only when `first_model_event()` was called.
        """
        prepare_seconds: float | None = None
        if round_.prepare_start is not None and round_.prepare_end is not None:
            prepare_seconds = round_.prepare_end - round_.prepare_start

        handoff_seconds: float | None = None
        if round_.prepare_end is not None and round_.handoff is not None:
            handoff_seconds = round_.handoff - round_.prepare_end

        first_event_seconds: float | None = None
        if round_.handoff is not None and round_.first_event is not None:
            first_event_seconds = round_.first_event - round_.handoff

        return ProviderRoundDiagnostics(
            round_number=round_.round_number,
            prepare_seconds=prepare_seconds,
            handoff_seconds=handoff_seconds,
            first_event_seconds=first_event_seconds,
            total_seconds=at - round_.start,
            provider_metrics=round_.provider_metrics,
            request_started_at_seconds=_offset(round_.start, round_.request_start),
            request_acknowledged_at_seconds=_offset(round_.start, round_.handoff),
            first_event_at_seconds=_offset(round_.start, round_.first_event),
            first_content_at_seconds=_offset(round_.start, round_.first_content),
        )

    def _require_not_finalized(self) -> None:
        if self._snapshot is not None:
            raise DiagnosticsStateError("turn diagnostics were already finalized")

    def _require_active_round(self) -> _ActiveRound:
        self._require_not_finalized()
        if self._active_round is None:
            raise DiagnosticsStateError("no active round")
        return self._active_round


class TurnDiagnosticsFactory:
    """Creates one `TurnDiagnosticsRecorder` per turn from injected collaborators.

    Production code uses the defaults (`time.monotonic`, a random local hex
    ID); tests inject deterministic functions so recorded durations follow
    from call order alone, never from real elapsed time.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory

    def create(self) -> TurnDiagnosticsRecorder:
        """Start a new recorder with a fresh local correlation ID."""
        return TurnDiagnosticsRecorder(correlation_id=self._id_factory(), clock=self._clock)


LOGGER_NAME: Final[str] = "korvid.agent.diagnostics"
"""Name of the logger finalization logs one structured record through."""


def _offset(start: float, boundary: float | None) -> float | None:
    return None if boundary is None else boundary - start


def _format_token_count(count: int) -> str:
    """Format a token count compactly: thousands as one-decimal `k`, else plain."""
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def format_diagnostics(summary: TurnDiagnostics) -> str:
    """Render one compact, human-readable diagnostic line for a terminal turn.

    This is the single interpretation of a `TurnDiagnostics` snapshot shared
    by the transcript, structured logs, and any other consumer (design doc
    §UI Integration): no caller recomputes durations or aggregates rounds
    itself. Only numeric timings and counts are rendered — never a provider
    or model name, a tool argument, or a tool result.
    """
    round_count = len(summary.rounds)
    model_seconds = sum(r.total_seconds for r in summary.rounds)
    parts = [
        f"{round_count} model round{'s' if round_count != 1 else ''}",
        f"model {model_seconds:.1f}s",
    ]

    if summary.tools:
        tool_seconds = sum(t.seconds for t in summary.tools)
        parts.append(f"tools {tool_seconds:.1f}s")

    parts.extend(_format_provider_metrics(summary.rounds))
    return " · ".join(parts)


def _format_provider_metrics(rounds: tuple[ProviderRoundDiagnostics, ...]) -> list[str]:
    metrics = [r.provider_metrics for r in rounds if r.provider_metrics is not None]
    parts: list[str] = []
    tokens: list[str] = []
    for marker, counts in (
        ("↑", [m.prompt_tokens for m in metrics if m.prompt_tokens is not None]),
        ("↓", [m.generation_tokens for m in metrics if m.generation_tokens is not None]),
    ):
        if counts:
            tokens.append(f"{marker}{_format_token_count(sum(counts))}")
    if tokens:
        parts.append(f"{' '.join(tokens)} tok")
    for label, seconds in (
        ("load", [m.load_seconds for m in metrics if m.load_seconds is not None]),
        ("prompt", [m.prompt_eval_seconds for m in metrics if m.prompt_eval_seconds is not None]),
        ("generate", [m.generation_seconds for m in metrics if m.generation_seconds is not None]),
    ):
        if seconds:
            parts.append(f"{label} {sum(seconds):.1f}s")
    metered_pairs = [
        (r.total_seconds, r.provider_metrics.total_seconds)
        for r in rounds
        if r.provider_metrics is not None and r.provider_metrics.total_seconds is not None
    ]
    if metered_pairs:
        # Only compare metered rounds; transport/adapter overhead is not pure queue time.
        other_wait = sum(max(0.0, local - provider) for local, provider in metered_pairs)
        parts.append(f"other wait {other_wait:.1f}s")
    return parts


def diagnostic_log_fields(summary: TurnDiagnostics) -> dict[str, str | int | float | bool | None]:
    """Return the explicit allowlisted fields for structured logging.

    This is the single place that decides what may reach the
    `korvid.agent.diagnostics` logger's `extra` fields (design doc
    §Structured Logging): only numeric timings, the local correlation ID,
    the outcome, round numbers, tool names, and success booleans. There is
    no generic object serialization here, so a caller can never smuggle an
    arbitrary object — prompt, tool argument, tool result, or credential —
    into a log record through this function.
    """
    fields: dict[str, str | int | float | bool | None] = {
        "correlation_id": summary.correlation_id,
        "outcome": summary.outcome.value,
        "total_seconds": summary.total_seconds,
        "round_count": len(summary.rounds),
        "tool_count": len(summary.tools),
    }
    for round_ in summary.rounds:
        prefix = f"round_{round_.round_number}"
        fields[f"{prefix}_prepare_seconds"] = round_.prepare_seconds
        fields[f"{prefix}_handoff_seconds"] = round_.handoff_seconds
        fields[f"{prefix}_first_event_seconds"] = round_.first_event_seconds
        fields[f"{prefix}_request_started_at_seconds"] = round_.request_started_at_seconds
        fields[f"{prefix}_request_acknowledged_at_seconds"] = round_.request_acknowledged_at_seconds
        fields[f"{prefix}_first_event_at_seconds"] = round_.first_event_at_seconds
        fields[f"{prefix}_first_content_at_seconds"] = round_.first_content_at_seconds
        fields[f"{prefix}_total_seconds"] = round_.total_seconds
        metrics = round_.provider_metrics
        if metrics is not None:
            fields[f"{prefix}_provider_total_seconds"] = metrics.total_seconds
            fields[f"{prefix}_provider_load_seconds"] = metrics.load_seconds
            fields[f"{prefix}_provider_prompt_eval_seconds"] = metrics.prompt_eval_seconds
            fields[f"{prefix}_provider_prompt_tokens"] = metrics.prompt_tokens
            fields[f"{prefix}_provider_generation_seconds"] = metrics.generation_seconds
            fields[f"{prefix}_provider_generation_tokens"] = metrics.generation_tokens
    for tool in summary.tools:
        prefix = f"tool_{tool.sequence}"
        fields[f"{prefix}_name"] = tool.name
        fields[f"{prefix}_seconds"] = tool.seconds
        fields[f"{prefix}_ok"] = tool.ok
    return fields


def log_diagnostics(summary: TurnDiagnostics) -> None:
    """Log one finalized turn's diagnostic snapshot, exactly once.

    Serialize only the explicit scalar allowlist, so ordinary file handlers
    retain correlation IDs and individual timings without a custom formatter.
    Structured handlers also receive the same fields as record attributes.
    """
    logger = logging.getLogger(LOGGER_NAME)
    fields = diagnostic_log_fields(summary)
    logger.info("%s", json.dumps(fields, sort_keys=True), extra=fields)
