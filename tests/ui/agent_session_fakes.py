"""Shared `AgentSession` fakes for the UI tests (issue #316 task 12).

The production TUI owns exactly one agent object — an `AgentSession` — so
every UI test drives that exact ABC rather than a duck-typed stand-in: a
fake free to drift from the contract is a test that keeps passing against
a session the app can no longer run.

`FakeSession` is scripted rather than scripted-and-clever: it replays a
fixed event list, optionally parks until a gate opens, and records what
the controller asked of it (prompts, interrupts, finalizations, closes,
retargets, iterator releases). Everything the UI reads off a live session
— tokens, the outbound snapshot, the evidence ledger, the resolved policy
— is a plain attribute a test can set.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Sequence

from korvid.agent.events import AgentEvent, TurnComplete, TurnInterrupted
from korvid.agent.evidence import EvidenceLedger
from korvid.agent.interaction import ClusterFacts
from korvid.agent.model_policy import (
    CapabilitySource,
    ModelCapabilities,
    ModelDescriptor,
    ModelTier,
    ResolvedAgentPolicy,
)
from korvid.agent.outbound import OutboundSnapshot
from korvid.agent.session import AgentSession
from korvid.tools.registry import TOOLS_BY_NAME


def fake_policy(
    *,
    tier: ModelTier = ModelTier.LOW,
    route_source: CapabilitySource = CapabilitySource.CATALOG,
    tool_names: Sequence[str] = ("get_logs",),
    model: str = "m-1",
) -> ResolvedAgentPolicy:
    """A resolved policy with exactly the tier/provenance a test asserts on."""
    return ResolvedAgentPolicy(
        model=ModelDescriptor("test", model),
        capabilities=ModelCapabilities.unknown(),
        tier=tier,
        route_source=route_source,
        prompt_pack_id="low-korvid-operator",
        prompt_overlay_ids=(),
        tools=tuple(copy.deepcopy(TOOLS_BY_NAME[name].schema) for name in tool_names),
        max_iterations=6,
        max_history_chars=24_000,
        max_result_chars=3_000,
        max_tool_calls_per_iteration=1,
        allow_parallel_tool_calls=False,
        strict_history_budget=True,
        catalog_version=1,
    )


def fake_snapshot(model: str = "test") -> OutboundSnapshot:
    """A minimal outbound snapshot, for the `:ai payload` inspector."""
    return OutboundSnapshot(
        model=model,
        iteration=1,
        payload_json='{"messages":[],"tools":[]}',
        redactions=(),
    )


class FakeSession(AgentSession):
    """A scripted `AgentSession`: the exact ABC, no more, no less.

    Args:
        events: Replayed in order on every turn.
        block: Park forever after the script (a turn only a stop ends).
        gate: Park after the script until this event is set.
        policy: What `policy` reports; defaults to a low/catalog route.
        snapshot: What `latest_outbound_payload` reports.
        evidence: The ledger citations resolve against.
        tokens: What `total_tokens` reports.
        estimated: What `usage_estimated` reports.
        run_error: Raised synchronously by `run_turn` instead of starting.
        turn_error: Raised from inside the turn after the script.
    """

    def __init__(
        self,
        events: Sequence[AgentEvent] = (),
        *,
        block: bool = False,
        gate: asyncio.Event | None = None,
        policy: ResolvedAgentPolicy | None = None,
        snapshot: OutboundSnapshot | None = None,
        evidence: EvidenceLedger | None = None,
        tokens: tuple[int, int] = (0, 0),
        estimated: bool = False,
        run_error: Exception | None = None,
        turn_error: BaseException | None = None,
    ) -> None:
        self.events = list(events)
        self._block = block
        self._gate = gate
        self._policy = policy if policy is not None else fake_policy()
        self._snapshot = snapshot
        self._evidence = evidence if evidence is not None else EvidenceLedger()
        self._tokens = tokens
        self._estimated = estimated
        self._run_error = run_error
        self._turn_error = turn_error
        self._pending = False
        #: Everything the controller did, in the order it did it.
        self.prompts: list[str] = []
        self.interrupts = 0
        self.finalized = 0
        self.closed = 0
        self.iterators_released = 0
        self.retargets: list[tuple[ResolvedAgentPolicy, ClusterFacts]] = []
        #: Set while an iterator is mid-cleanup, so a test can land a
        #: cancellation exactly there.
        self.releasing = asyncio.Event()
        #: Awaited during iterator cleanup when set, to hold it open.
        self.release_gate: asyncio.Event | None = None

    # -- AgentSession ------------------------------------------------------

    def run_turn(self, user_text: str) -> AsyncIterator[AgentEvent]:
        if self._run_error is not None:
            raise self._run_error
        self.prompts.append(user_text)
        return self._drive()

    async def _drive(self) -> AsyncIterator[AgentEvent]:
        # A script that ends on `TurnComplete` and parks nowhere afterwards
        # models a turn the engine really completed. The real engine closes
        # the conversation turn *before* it emits that terminal event, so
        # from there on there is nothing left to finalize — even if the
        # consumer raises on the event and never resumes this generator.
        last = len(self.events) - 1
        completes = (
            self._turn_error is None
            and self._gate is None
            and not self._block
            and bool(self.events)
            and isinstance(self.events[last], TurnComplete)
        )
        finished = False
        try:
            for index, event in enumerate(self.events):
                if completes and index == last:
                    finished = True
                if isinstance(event, TurnComplete):
                    self._tokens = (
                        self._tokens[0] + event.input_tokens,
                        self._tokens[1] + event.output_tokens,
                    )
                    self._estimated = self._estimated or event.estimated
                yield event
            if self._turn_error is not None:
                raise self._turn_error
            if self._gate is not None:
                await self._gate.wait()
            elif self._block:
                await asyncio.Event().wait()
            finished = True
        finally:
            self.releasing.set()
            if self.release_gate is not None:
                await self.release_gate.wait()
            self.iterators_released += 1
            if not finished:
                self._pending = True

    def interrupt(self) -> None:
        self.interrupts += 1

    def finalize_interrupt(self) -> TurnInterrupted:
        if not self._pending:
            raise RuntimeError("no interrupted turn to finalize")
        self._pending = False
        self.finalized += 1
        event = TurnInterrupted(input_tokens=3, output_tokens=1, estimated=True)
        self._tokens = (
            self._tokens[0] + event.input_tokens,
            self._tokens[1] + event.output_tokens,
        )
        self._estimated = self._estimated or event.estimated
        return event

    def retarget(self, policy: ResolvedAgentPolicy, cluster: ClusterFacts) -> None:
        self.retargets.append((policy, cluster))
        self._policy = policy

    async def aclose(self) -> None:
        self.closed += 1
        self._pending = False

    @property
    def total_tokens(self) -> tuple[int, int]:
        return self._tokens

    @total_tokens.setter
    def total_tokens(self, value: tuple[int, int]) -> None:
        self._tokens = value

    @property
    def usage_estimated(self) -> bool:
        return self._estimated

    @property
    def latest_outbound_payload(self) -> OutboundSnapshot | None:
        return self._snapshot

    @latest_outbound_payload.setter
    def latest_outbound_payload(self, value: OutboundSnapshot | None) -> None:
        self._snapshot = value

    @property
    def evidence(self) -> EvidenceLedger:
        return self._evidence

    @property
    def policy(self) -> ResolvedAgentPolicy:
        return self._policy

    @property
    def finalization_pending(self) -> bool:
        return self._pending
