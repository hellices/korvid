"""The live agent session: one screen, one engine, one turn at a time (Task 11).

`AgentSession` is the only agent component that knows a *screen* exists.
Everything below it — the engine loop, the request gateway, the tool
harness, the conversation — was built to be handed frozen inputs and to
report typed results. Something has to stand where those meet the moving
parts of a TUI (the pane the user is looking at *right now*, the context
they just switched to, the key they pressed to stop a turn) and turn that
into one immutable `AgentTurnRequest` per turn. That is this module, and
keeping it in exactly one place is what lets every other component stay a
pure function of its inputs.

The decisions this boundary makes, and why:

- **The workspace is read at the start of every turn, never cached.** A
  session that composed a snapshot taken at construction would answer
  about the pane the user left ten navigations ago. The snapshot is taken
  when the turn *actually starts* — not when `run_turn` is called — so
  what reaches the model is what was on screen when the turn began.
- **Navigation between turns is state, not speech.** It reaches the model
  as fresh typed context on the next turn and never as a synthetic
  "the user navigated to ..." transcript entry: a fabricated user message
  is durable, and would still be asserting a stale screen position long
  after the screen moved on.
- **The session hands over typed state and writes no model-facing prose.**
  When the Kubernetes context changes, `PromptHarness` — not this module —
  writes the handoff note, from the previous and current snapshots. The
  session's only job is to remember which snapshot the last *delivered*
  turn used, which is what makes the note appear exactly once: a turn that
  never reached the provider leaves the note pending for the next one.
- **`run_turn` is synchronous and returns a session-owned iterator.** A
  session that is closed, already running a turn, or still owing a
  finalization says so immediately, at the call, rather than at the first
  `__anext__` where it would look like a turn that started and produced
  nothing. The turn's claim is taken when iteration starts, so an
  iterator that is created and never driven wedges nothing.
- **The session owns the engine iterator it starts.** It exhausts it or
  closes it — the engine contract requires a consumer that does, and this
  is that consumer — and closing it releases the provider stream
  underneath, which closing a generator does not do by itself.
- **Interruption, retarget and close are each one atomic operation.** An
  advisory stop or a cancellation leaves `ConversationState` mid-turn; the
  session records that a finalization is owed, and `finalize_interrupt`
  (synchronous, one-shot) closes it. `retarget` validates everything
  before it moves anything, then swaps policy, tool surface and outbound
  boundary together. `aclose` runs once no matter how many callers ask
  for it, and no caller returns before that close has finished.

What this session deliberately does *not* own is provider and model
replacement. `retarget` swaps policy and cluster facts on the live
session — the environment-derived half of a resolved policy — but a
different model descriptor or history budget means the gateway and
conversation were built wrong for it, and those are the composition
root's to rebuild (`SessionRetargetError` says so).
"""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator

from korvid.agent.conversation import ConversationState
from korvid.agent.engine import AgentEngine, AgentTurnRequest
from korvid.agent.events import AgentEvent, TurnInterrupted
from korvid.agent.evidence import EvidenceLedger
from korvid.agent.interaction import AgentUiBridge, ClusterFacts, InteractionContext
from korvid.agent.model_policy import ResolvedAgentPolicy
from korvid.agent.outbound import OutboundSnapshot
from korvid.agent.prompt_harness import PromptHarness, PromptInputs
from korvid.agent.request_gateway import RequestGateway
from korvid.agent.tool_harness import ToolHarness


class SessionRetargetError(RuntimeError):
    """A live session cannot be retargeted onto this policy.

    Raised only for the half of a resolved policy the session does not
    own: the model descriptor and the history budget the gateway and
    conversation were constructed around. The composition root rebuilds
    the session for those (issue #316 task 12); everything the session
    *does* own is either swapped atomically or refused by the collaborator
    that validated it.
    """


class AgentSession(ABC):
    """Drive turns for one live workspace, against one engine.

    The screen-facing half of the agent: the caller supplies user text and
    gets typed events, and every other input a turn needs — the workspace
    snapshot, the composed prompt, the policy — is decided here.
    """

    @abstractmethod
    def run_turn(self, user_text: str) -> AsyncIterator[AgentEvent]:
        """Start one turn and return the iterator of its events.

        Synchronous, like `AgentEngine.run` and for the same reason: a
        turn that cannot start must not look like one that started and
        produced nothing. The returned iterator is the session's; drive it
        to its terminal event or close it (`aclose` does the same).

        Args:
            user_text: What the operator typed.

        Returns:
            An async iterator of this turn's events, unchanged from the
            engine's.

        Raises:
            RuntimeError: The session is closed, a turn is already
                running, or an interrupted turn is still awaiting
                `finalize_interrupt`.
        """

    @abstractmethod
    def interrupt(self) -> None:
        """Ask the running turn to stop at its next boundary.

        Advisory and inert unless a turn really started: the window
        between `run_turn` and the first event belongs to no turn, so an
        interrupt landing in it is discarded rather than inherited.
        """

    @abstractmethod
    def finalize_interrupt(self) -> TurnInterrupted:
        """Close the conversation a stopped turn left mid-flight.

        Synchronous so a screen can call it from an event handler, and
        one-shot. Allowed only once the turn's iterator has been released.

        Returns:
            The event describing what was retained.

        Raises:
            RuntimeError: A turn is still running, or no turn is awaiting
                finalization.
        """

    @abstractmethod
    def retarget(self, policy: ResolvedAgentPolicy, cluster: ClusterFacts) -> None:
        """Install a new policy and cluster snapshot between turns.

        All of the session or none of it: the composed prompt, the armed
        tool surface and the request boundary that surface is sent across
        move together, so no later turn is run against a mixture of the
        two environments.

        Args:
            policy: The newly resolved policy for this environment.
            cluster: The cluster facts that go with it.

        Raises:
            RuntimeError: The session is closed, a turn is running, or an
                interrupted turn is awaiting finalization.
            SessionRetargetError: The policy changes something only a
                rebuilt session can change.
            ValueError: The policy is not composable, not executable, or
                not sendable. Nothing is changed in that case.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Close the session and everything it started.

        Idempotent, and one operation however many callers ask for it:
        concurrent callers await the same close, and each returns only
        once that close has finished. After it returns, no further event
        and no further history arrive from the turn it stopped, and the
        caller has nothing left to finalize.
        """

    @property
    @abstractmethod
    def total_tokens(self) -> tuple[int, int]:
        """Cumulative (input, output) token counts across completed turns."""

    @property
    @abstractmethod
    def usage_estimated(self) -> bool:
        """True if any counted turn lacked provider usage."""

    @property
    @abstractmethod
    def latest_outbound_payload(self) -> OutboundSnapshot | None:
        """What the most recent provider request actually carried."""

    @property
    @abstractmethod
    def evidence(self) -> EvidenceLedger:
        """The current turn's evidence ledger, for readers only.

        The session and its tool harness are the ledger's only writers:
        entries are minted by the reads a turn actually performed, and the
        whole ledger is dropped when a turn starts or the session is
        retargeted. A consumer — the citation UI of issue #316 task 12
        included — reads it through `EvidenceLedger.references` and
        `EvidenceLedger.resolve`, and treats a reference that no longer
        resolves as the answer it is: the read behind it belongs to a
        cluster or a turn this session has left.

        The object is live, not a copy. Holding it across turns is holding
        the session's ledger, so a reader that must outlive a turn keeps
        what it resolved, not the ledger it resolved from.
        """

    @property
    @abstractmethod
    def policy(self) -> ResolvedAgentPolicy:
        """The policy turns are currently composed and run against."""


class DefaultAgentSession(AgentSession):
    """The session the TUI runs: live bridge, live policy, one engine.

    Args:
        engine: The turn loop. The session owns every iterator it starts
            from this and closes it exactly once.
        bridge: The typed workspace bridge, read at the start of each turn.
        prompt_harness: Composes the layered prompt and owns every word
            the model reads about a context switch.
        conversation: Durable history and usage totals. Shared with the
            engine, which appends the turn as it runs.
        gateway: The provider request path. Read for `latest_outbound_payload`.
        tools: The armed tool surface. Shared with the engine, so
            `retarget` re-points this same object.
        policy: The initially resolved policy.
        cluster: The cluster facts that go with it.
        user_rules: `config.agent_rules`, composed into every turn.

    Raises:
        ValueError: `policy` (with `user_rules`) does not compose, or arms
            a tool the registry does not define. Validated eagerly, and
            statically: the constructor must not read the bridge, because
            the composition root wires a bridge proxy that has no app
            behind it yet (task 12).
    """

    def __init__(
        self,
        *,
        engine: AgentEngine,
        bridge: AgentUiBridge,
        prompt_harness: PromptHarness,
        conversation: ConversationState,
        gateway: RequestGateway,
        tools: ToolHarness,
        policy: ResolvedAgentPolicy,
        cluster: ClusterFacts,
        user_rules: tuple[str, ...] = (),
    ) -> None:
        self._engine = engine
        self._bridge = bridge
        self._prompts = prompt_harness
        self._conversation = conversation
        self._gateway = gateway
        self._tools = tools
        self._user_rules = user_rules
        self._validate(policy)
        self._policy = policy
        self._cluster = cluster

        self._closed = False
        self._closing: asyncio.Task[None] | None = None
        #: Tasks parked inside `aclose`. A close must not wait for a turn
        #: driver that is itself waiting for that close.
        self._close_waiters: set[asyncio.Task[object]] = set()
        #: Set whenever the live turn's driver joins the close, so a close
        #: already waiting on that driver stops waiting for it.
        self._driver_joined_close = asyncio.Event()
        #: The exact task a close is currently waiting for. A driver gives
        #: the turn back (`_driver` becomes None) *before* its own `finally`
        #: asks for the close, so this is the only identity by which that
        #: driver can still be recognized as the one being waited for.
        self._awaited_driver: asyncio.Task[object] | None = None
        self._turn_active = False
        self._turn_started = False
        self._awaiting_finalization = False
        self._turn_iterator: AsyncIterator[AgentEvent] | None = None
        self._driver: asyncio.Task[object] | None = None
        #: The snapshot the last *delivered* turn composed from. Only a
        #: request proven to have reached the provider moves it, so a turn
        #: that never crossed the boundary cannot swallow a pending
        #: handoff note.
        self._last_started: InteractionContext | None = None

    # -- properties --------------------------------------------------------

    @property
    def total_tokens(self) -> tuple[int, int]:
        """Cumulative (input, output) token counts across completed turns."""
        return self._conversation.total_tokens

    @property
    def usage_estimated(self) -> bool:
        """True if any counted turn lacked provider usage (totals are estimates)."""
        return self._conversation.usage_estimated

    @property
    def latest_outbound_payload(self) -> OutboundSnapshot | None:
        """What the most recent provider request actually carried."""
        return self._gateway.latest_outbound_payload

    @property
    def evidence(self) -> EvidenceLedger:
        """The current turn's evidence ledger, for readers only.

        The harness's live ledger, not a copy. See `AgentSession.evidence`
        for the reader contract: `references` and `resolve`, and a
        reference that stops resolving after a retarget is a reference to
        a cluster this session has left.
        """
        return self._tools.evidence

    @property
    def policy(self) -> ResolvedAgentPolicy:
        """The policy turns are currently composed and run against."""
        return self._policy

    # -- turns -------------------------------------------------------------

    def run_turn(self, user_text: str) -> AsyncIterator[AgentEvent]:
        """Start one turn and return the iterator of its events.

        Rejection happens here, synchronously; the workspace snapshot and
        the engine call happen when the returned iterator is first driven.

        Args:
            user_text: What the operator typed.

        Returns:
            An async iterator of this turn's events, unchanged.

        Raises:
            RuntimeError: The session is closed, a turn is already
                running, or an interrupted turn awaits finalization.
        """
        self._require_idle("start a turn")
        return self._drive(user_text)

    async def _drive(self, user_text: str) -> AsyncIterator[AgentEvent]:
        """The turn itself, claimed only once someone actually drives it."""
        self._require_idle("start a turn")
        self._turn_active = True
        self._turn_started = True
        self._driver = asyncio.current_task()
        #: What the boundary had delivered before this turn existed. The
        #: handoff is consumed against this, not against the attempt.
        delivered = self._gateway.latest_outbound_payload
        request: AgentTurnRequest | None = None
        try:
            request = self._request(user_text)
            iterator = self._engine.run(request)
            self._turn_iterator = iterator
            try:
                async for event in iterator:
                    self._commit_handoff(request, delivered)
                    yield event
            finally:
                await self._release_iterator(iterator)
        finally:
            self._commit_handoff(request, delivered)
            self._release_turn()

    def _request(self, user_text: str) -> AgentTurnRequest:
        """Snapshot the live workspace and compose this turn's request.

        Reads state; changes none. Composing is one of several things that
        can fail before a single character reaches the provider, and the
        pending handoff note is owed to a turn the *model actually saw* —
        so what was delivered is decided by `_commit_handoff`, after the
        boundary has spoken.
        """
        interaction = self._bridge.snapshot()
        prompt = self._prompts.compose(
            user_text,
            PromptInputs(
                policy=self._policy,
                interaction=interaction,
                cluster=self._cluster,
                user_rules=self._user_rules,
                previous_interaction=self._last_started,
            ),
        )
        return AgentTurnRequest(prompt=prompt, policy=self._policy, interaction=interaction)

    def _commit_handoff(
        self, request: AgentTurnRequest | None, delivered: OutboundSnapshot | None
    ) -> None:
        """Consume the one-shot context handoff, but only once it was delivered.

        The gateway moves `latest_outbound_payload` to a new snapshot
        exactly when a request is proven to have reached the provider, so
        a payload that is no longer the one this turn started with *is*
        the proof that the note crossed the boundary. Everything that can
        stop a turn before that — a prompt that will not compose, a
        history budget that refuses it, a refusal at the outbound
        boundary, a provider that failed or was cancelled before handoff —
        leaves the note pending for the next turn, because the model never
        read it.

        Synchronous and idempotent: it runs after every event and again
        when the turn unwinds, including through cancellation, where there
        is no opportunity to await anything.
        """
        if request is None:
            return
        if self._gateway.latest_outbound_payload is delivered:
            return
        self._last_started = request.interaction

    async def _release_iterator(self, iterator: AsyncIterator[AgentEvent]) -> None:
        """Release this turn's engine iterator and the stream underneath it.

        Closing an async generator unwinds that generator only: the ones
        it was itself iterating are left to the garbage collector, on no
        schedule a caller can wait for. A partially consumed turn closed
        by its caller must nonetheless leave no provider iterator open, so
        the session closes the gateway's stream state too — a no-op on
        every path where the stream already ended, and never a substitute
        for `AgentEngine.aclose`, which is what stops a *running* turn.
        """
        try:
            await _aclose(iterator)
        finally:
            await self._gateway.aclose()

    def _release_turn(self) -> None:
        """Give the session back, and record any finalization the turn owes."""
        self._turn_active = False
        self._turn_started = False
        self._turn_iterator = None
        self._driver = None
        if self._conversation.turn_active:
            self._awaiting_finalization = True

    # -- interruption ------------------------------------------------------

    def interrupt(self) -> None:
        """Ask the running turn to stop at its next boundary.

        Inert unless a turn really started, so a keystroke in the window
        between `run_turn` and the first event is discarded rather than
        inherited by the turn that is about to begin.
        """
        if self._turn_started:
            self._engine.interrupt()

    def finalize_interrupt(self) -> TurnInterrupted:
        """Close the conversation the stopped turn left mid-flight.

        Returns:
            The event describing what was retained.

        Raises:
            RuntimeError: A turn is still running, or no turn awaits
                finalization.
        """
        if self._turn_active:
            raise RuntimeError("cannot finalize an interrupt while a turn is running")
        if not self._awaiting_finalization:
            raise RuntimeError("no interrupted turn to finalize")
        self._awaiting_finalization = False
        return self._conversation.finalize_interrupt()

    # -- retarget ----------------------------------------------------------

    def retarget(self, policy: ResolvedAgentPolicy, cluster: ClusterFacts) -> None:
        """Install a new policy and cluster snapshot between turns.

        Idle-only and atomic: everything that can refuse this runs before
        anything moves, so a refused retarget leaves the previous policy
        composed, the previous surface armed, the previous outbound
        boundary in force, and the previous cluster facts standing. What
        moves after that cannot fail.

        The whole session is retargeted, not half of it. The tool surface
        and the request boundary that surface is sent across are one
        decision: a session re-armed with more tools but still bounded by
        the ceiling derived for the old ones would answer by silently
        dropping the operator's oldest turns to fit. So the gateway is
        re-armed from the same policy, in the same operation.

        Evidence is cleared immediately rather than at the next turn — a
        citation minted against the cluster we just left must not resolve
        for anyone, including a screen rendering the answer that is still
        on it. It is cleared *without* re-reading the workspace: this is
        not a turn, there may be no live screen to read (task 12 wires a
        bridge proxy), and the epoch that evidence belongs to is supplied
        by the next turn that really starts.

        History and the pending context handoff both survive: the
        conversation is the operator's, and the note about the switch is
        owed to the next *turn*, not to this call.

        Args:
            policy: The newly resolved policy for this environment.
            cluster: The cluster facts that go with it.

        Raises:
            RuntimeError: The session is closed, a turn is running, or an
                interrupted turn awaits finalization.
            SessionRetargetError: The policy changes the model descriptor
                or the history budget, which only a rebuilt session can
                change.
            ValueError: The policy does not compose, arms a tool the
                registry does not define, or offers a surface the outbound
                boundary cannot be built for.
        """
        self._require_idle("retarget")
        self._require_rebuildable_only(policy)
        self._validate(policy)
        outbound = self._gateway.prepare_policy(policy)

        self._tools.retarget(policy)
        self._gateway.install_policy(outbound)
        self._policy = policy
        self._cluster = cluster
        self._tools.clear_evidence()

    def _require_rebuildable_only(self, policy: ResolvedAgentPolicy) -> None:
        """Refuse the half of a policy the session's collaborators own.

        The gateway was built for one model descriptor and the
        conversation for one history budget; swapping either here would
        leave a session composing for a model its provider does not serve,
        or trimming history to a budget its requests no longer respect.
        """
        current = self._policy
        changed = [
            name
            for name, old, new in (
                ("model", current.model, policy.model),
                ("max_history_chars", current.max_history_chars, policy.max_history_chars),
                (
                    "strict_history_budget",
                    current.strict_history_budget,
                    policy.strict_history_budget,
                ),
            )
            if old != new
        ]
        if changed:
            raise SessionRetargetError(
                f"cannot retarget a live session onto a policy that changes "
                f"{', '.join(changed)}: rebuild the session for it"
            )

    def _validate(self, policy: ResolvedAgentPolicy) -> None:
        """Static checks only — there may be no live workspace to read yet."""
        self._prompts.validate(policy, self._user_rules)
        ToolHarness.validate_policy(policy)

    # -- close -------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the session and everything it started.

        One close, however many callers. A screen tearing down while a
        context switch is closing the same session must not be told the
        session is closed while the engine, the driver and the
        conversation are still settling, so the first caller starts the
        close and every caller — first or not — returns only when that
        one close has finished. The close runs as its own task and is
        awaited through a shield, so a caller that is cancelled abandons
        its wait and not the cleanup: the turn's driver, the provider
        iterator and the mid-flight conversation are all mid-teardown by
        then, and stopping there would leave a turn nobody can finalize.

        Idempotent. After it returns, no further event and no further
        history arrive from the turn it stopped, and the caller has
        nothing left to finalize.
        """
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.ensure_future(self._close())
        closing = self._closing
        caller = asyncio.current_task()
        driving = caller is not None and self._is_driver(caller)
        if caller is not None:
            self._close_waiters.add(caller)
            if driving:
                self._driver_joined_close.set()
        try:
            await self._wait_out(closing, caller if driving else None)
        finally:
            if caller is not None:
                self._close_waiters.discard(caller)

    def _is_driver(self, caller: asyncio.Task[object]) -> bool:
        """Is this caller the turn driver, live or being waited for?

        A driver stopped by a close unwinds its turn before its own
        `finally` closes the session, and unwinding releases the turn —
        so by the time it asks, it is no longer `_driver`. The close that
        stopped it is still waiting for exactly that task, and says so in
        `_awaited_driver`; either identity means the caller is the driver
        joining its own close, which is what makes the close stop waiting
        for it instead of deadlocking against it.
        """
        return caller is self._driver or caller is self._awaited_driver

    async def _wait_out(
        self, closing: asyncio.Task[None], driver: asyncio.Task[object] | None
    ) -> None:
        """Wait for the one close, absorbing the stop it performs on us.

        The engine stops a turn parked in a provider await by cancelling
        its driver, and it exempts the task that asked — which, now that
        the close runs as a task of its own, is no longer the task the
        engine sees. So the exemption is honored here instead: a driver
        closing its own session is being stopped by its own request, and
        answering that request with `CancelledError` would report the
        close as abandoned when it is exactly what was asked for.

        Absorbed once, and only when this close is the sole thing
        cancelling the caller: `uncancel` reports what remains, so a
        driver that someone *else* also cancelled still propagates.
        """
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            if driver is None or driver.uncancel() > 0:
                raise
            await asyncio.shield(closing)

    async def _close(self) -> None:
        """Do the closing, exactly once, whoever asked for it.

        Order matters. The engine is closed first: it is the only party
        that can stop a turn parked in a provider await, and it cancels
        the task driving that turn. Only once that driver has settled can
        the session's own generator be closed — a generator suspended in
        an await cannot be closed, and closing it while its driver still
        runs would race the last events into the caller. Finalizing the
        conversation comes last, so what is retained is everything the
        turn had actually appended by the time it really stopped.
        """
        await self._engine.aclose()
        await self._await_driver()
        await self._close_iterator()
        if self._conversation.turn_active:
            self._conversation.finalize_interrupt()
        self._awaiting_finalization = False

    async def _await_driver(self) -> None:
        """Let an externally driven turn finish unwinding before we touch it.

        The close runs as a task of its own, so the driver is never this
        task and a session closed from inside its own turn's loop has to
        be recognized some other way: by the driver being parked in
        `aclose`, either already or by joining while this wait is in
        progress. Waiting for a driver that is waiting for this close is
        the one way this can hang, and both of those are that case — the
        second only because the identity of the task being waited for is
        published here, for a driver that has already released the turn
        by the time its `finally` asks for the close.
        """
        driver = self._driver
        if driver is None or driver in self._close_waiters:
            return
        joined = asyncio.ensure_future(self._driver_joined_close.wait())
        self._awaited_driver = driver
        try:
            await asyncio.wait({driver, joined}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._awaited_driver = None
            joined.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await joined

    async def _close_iterator(self) -> None:
        """Close a turn nobody is driving (a caller that stopped at `anext`)."""
        iterator = self._turn_iterator
        if iterator is None:
            return
        self._turn_iterator = None
        with contextlib.suppress(RuntimeError):
            await _aclose(iterator)

    # -- guards ------------------------------------------------------------

    def _require_idle(self, action: str) -> None:
        if self._closed:
            raise RuntimeError(f"cannot {action}: the session is closed")
        if self._turn_active:
            raise RuntimeError(f"cannot {action}: a turn is already running")
        if self._awaiting_finalization:
            raise RuntimeError(f"cannot {action}: the interrupted turn must be finalized first")


async def _aclose(iterator: AsyncIterator[AgentEvent]) -> None:
    """Release an engine iterator, whatever kind of iterator it turned out to be.

    The engine contract promises an async *iterator*, not an async
    generator, so the close it may need is discovered rather than assumed:
    an engine backed by a plain iterator class has nothing to release and
    must not be a type error here.
    """
    if isinstance(iterator, AsyncGenerator):
        await iterator.aclose()
