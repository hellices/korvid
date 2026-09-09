"""Typed events the agent engine yields to the UI (design §6.1 panel contents)."""

from __future__ import annotations

from dataclasses import dataclass

from korvid.agent.diagnostics import AgentPhase, TurnDiagnostics


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolCallFinished:
    call_id: str
    name: str
    ok: bool
    summary: str


@dataclass(frozen=True)
class AgentPhaseChanged:
    """The engine crossed into a new observable phase of the turn (issue #319).

    Carries only what the status line needs: the phase, the one-based
    provider round it belongs to, and — for `RUNNING_TOOL` — the registry
    tool name. It never carries a prompt, tool arguments, a tool result, or
    any provider payload, so the panel can render it verbatim. Emitted only
    when the turn is recording diagnostics; a turn without a recorder yields
    none and the panel falls back to its plain spinner.
    """

    phase: AgentPhase
    round_number: int
    #: The registry tool name for `RUNNING_TOOL`; `None` for model phases.
    tool: str | None = None


@dataclass(frozen=True)
class TurnComplete:
    input_tokens: int
    output_tokens: int
    estimated: bool
    #: References the answer cited that the ledger actually minted.
    cited: tuple[str, ...] = ()
    #: References the answer cited that resolve to nothing (issue #192).
    #: Reported, never edited out: removing an unsupported citation would
    #: also remove the evidence that the claim was unsourced.
    uncited: tuple[str, ...] = ()
    #: References the answer cited more than once. Repetition is not extra
    #: support, and collapsing it silently would make a duplicated
    #: citation look like a single clean one.
    duplicated: tuple[str, ...] = ()
    #: The terminal latency-diagnostics snapshot for this turn (issue #319),
    #: or `None` when the turn was not recording diagnostics. Early stops and
    #: fail-closed rollbacks retain terminal accounting here with a FAILED
    #: outcome; inspect the snapshot rather than assuming success.
    diagnostics: TurnDiagnostics | None = None


@dataclass(frozen=True)
class TurnInterrupted:
    """Terminal outcome of a user-interrupted turn (issue #170).

    Carries the usage committed for the partial turn so the panel's token
    header stays honest; the runtime has already repaired model history
    (bounded, marked partial note - never a completed-looking answer).
    """

    input_tokens: int
    output_tokens: int
    estimated: bool
    #: The terminal latency-diagnostics snapshot for this turn (issue #319),
    #: or `None` when the turn was not recording diagnostics. An interrupted
    #: turn's snapshot reports `TurnOutcome.INTERRUPTED` — never a
    #: successful-looking outcome.
    diagnostics: TurnDiagnostics | None = None


@dataclass(frozen=True)
class AgentError:
    message: str
    #: The terminal latency-diagnostics snapshot when this error *ends* the
    #: turn (a provider failure, issue #319), or `None` when the error is
    #: recoverable and a `TurnComplete` follows it — a recoverable error
    #: never carries a second snapshot. A terminal error's snapshot reports
    #: `TurnOutcome.FAILED`.
    diagnostics: TurnDiagnostics | None = None


AgentEvent = (
    TextDelta
    | ToolCallStarted
    | ToolCallFinished
    | AgentPhaseChanged
    | TurnComplete
    | TurnInterrupted
    | AgentError
)
