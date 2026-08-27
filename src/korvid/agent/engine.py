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
- **The consumer owns the iterator it starts.** Once a turn's iterator has
  yielded its first event it holds the engine's single-flight claim, and
  only finishing it — exhausting it, or calling `aclose()` on it, or
  `aclose()` on the engine — gives that claim back. Abandoning a started
  iterator mid-turn leaves the engine claimed until the garbage collector
  happens to finalize the generator, which is not a schedule anything may
  depend on. An iterator that was never driven owes nothing and may simply
  be dropped. Task 11's `AgentSession` is the consumer that owns this: it
  drives one turn to its terminal event, or closes it.
- `interrupt` signals a live turn to stop at its next boundary. It is
  advisory: cancelling the driving task remains the hard interrupt, and
  repairing the conversation after one is the session's job. It applies
  only to a turn that is *running* — an interrupt raised while the engine
  is idle, including between `run` and the first `__anext__`, is
  discarded rather than inherited by the next turn.
- `aclose` releases whatever the engine holds open and stops the live
  turn: after it returns, no further event and no further history is
  produced by the turn it closed. It is idempotent, a closed engine
  refuses to run another turn, and repairing the conversation the closed
  turn left mid-flight is — as with any interrupt — the session's job.
  An implementation may cancel the task driving a turn it cannot stop
  otherwise (one parked in a provider await, where no boundary check can
  run and no `aclose` on a running generator is legal); it never cancels
  the caller's own task, so `aclose()` from inside the turn's own loop
  ends that turn at its next resumption instead.
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

        The caller owns what it starts. A turn's iterator claims the
        engine on its first event and releases it when the turn ends, so
        a consumer that starts one must **exhaust it or `aclose()` it**
        (closing the engine does the same); an iterator that is never
        driven claims nothing and may be dropped. Task 11's
        `AgentSession` is that consumer.

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
        the next one. "Running" means *iterating*: the window between
        `run` returning an iterator and that iterator's first event
        belongs to no turn, and an interrupt landing in it is discarded
        with every other idle-engine interrupt.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release what this engine holds open, stopping a live turn.

        Idempotent. After it returns the closed turn emits no further
        event and appends no further history; the conversation it left
        mid-flight is the session's to repair, exactly as after any other
        interrupt.

        Stopping may require cancelling the task that drives the turn —
        a turn parked in a provider await reaches no boundary check, and
        a running async generator cannot be closed. An implementation
        that does so never cancels the *calling* task, so `aclose()` from
        inside a turn's own loop ends that turn at its next resumption
        rather than raising into it. The engine never creates the driving
        task; cancelling one is the same hard interrupt the UI performs.
        """
