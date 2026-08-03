"""Typed events yielded by AgentRuntime to the UI (design §6.1 panel contents)."""

from __future__ import annotations

from dataclasses import dataclass


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
class TurnComplete:
    input_tokens: int
    output_tokens: int
    estimated: bool


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


@dataclass(frozen=True)
class AgentError:
    message: str


AgentEvent = (
    TextDelta | ToolCallStarted | ToolCallFinished | TurnComplete | TurnInterrupted | AgentError
)
