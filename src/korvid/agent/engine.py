"""The engine boundary: one composed prompt in, typed agent events out.

`AgentEngine` is the seam between a *session* — which owns the screen, the
prompt harness, the interaction bridge and the interrupt story — and the
*loop* that turns one composed prompt into an answer. Everything the loop
needs arrives in a single frozen `AgentTurnRequest`; everything it produces
leaves as `AgentEvent` values on one async iterator. Nothing else crosses:
an engine holds no provider, no UI bridge, no cluster client, so a second
implementation (a hosted agent framework, an evaluation stub) can be
swapped in without the session learning what it is built from.

The contract is deliberately small and is pinned, implementation-agnostic,
by `tests/agent/test_engine_contract.py`:

- `run` is a **synchronous** call returning an async iterator, so an engine
  that is closed or already driving a turn rejects the second caller
  immediately rather than at the first `__anext__` — a turn that cannot
  start must never look like a turn that started and produced nothing.
  The single-flight claim itself is taken when iteration *starts*, so an
  iterator that is created and never driven leaves nothing latched.
- `interrupt` signals a live turn to stop at its next boundary. It is
  advisory: cancelling the driving task remains the hard interrupt, and
  repairing the conversation after one is the session's job.
- `aclose` releases whatever the engine holds open and stops the live
  turn: after it returns, no further event and no further history is
  produced by the turn it closed. It is idempotent, a closed engine
  refuses to run another turn, and repairing the conversation the closed
  turn left mid-flight is — as with any interrupt — the session's job.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from korvid.agent.events import AgentEvent
from korvid.agent.interaction import InteractionContext
from korvid.agent.model_policy import ResolvedAgentPolicy
from korvid.agent.prompt_harness import ComposedPrompt


@dataclass(frozen=True, slots=True)
class AgentTurnRequest:
    """Everything one turn needs, decided before the turn starts.

    Frozen and self-contained: an engine reads the request and never reaches
    back to the session for more, so what a turn ran with is exactly what
    was handed to it.

    Attributes:
        prompt: The already composed system and user messages. The system
            message is *static for this turn*: an engine sends it on
            every round, so it must not carry the turn's evidence table
            or anything else that changes between rounds. The engine is
            the single source of the per-round evidence table it appends.
        policy: The resolved model policy — tool surface, iteration and
            call caps, history budget — for this turn.
        interaction: The workspace snapshot this turn was asked in. The
            engine reads its context epoch to scope evidence; the screen
            content itself is the prompt harness's business.
    """

    prompt: ComposedPrompt
    policy: ResolvedAgentPolicy
    interaction: InteractionContext


class AgentEngine(ABC):
    """Drive one turn at a time and report it as typed events."""

    @abstractmethod
    def run(self, request: AgentTurnRequest) -> AsyncIterator[AgentEvent]:
        """Start one turn and return the iterator of its events.

        Synchronous by design: rejection is immediate and unambiguous.

        Args:
            request: The composed prompt, policy and workspace snapshot.

        Returns:
            An async iterator of this turn's events. A terminal event —
            `TurnComplete` or `AgentError` — ends every turn that was not
            interrupted.

        Raises:
            RuntimeError: The engine is closed, or a turn is already
                running.
        """

    @abstractmethod
    def interrupt(self) -> None:
        """Ask the live turn to stop at its next boundary.

        Inert when no turn is running, so a stray keystroke cannot poison
        the next one.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release what this engine holds open, stopping a live turn.

        Idempotent. After it returns the closed turn emits no further
        event and appends no further history; the conversation it left
        mid-flight is the session's to repair, exactly as after any other
        interrupt.
        """
