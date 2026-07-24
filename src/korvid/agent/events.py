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
class AgentError:
    message: str


AgentEvent = TextDelta | ToolCallStarted | ToolCallFinished | TurnComplete | AgentError
