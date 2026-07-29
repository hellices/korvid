"""Immutable external write proposals and their state machine (issue #110).

An external MCP caller may *propose* a cluster write; it can never execute
one. A proposal is an immutable record — retrying or changing an operation
creates a new proposal — and only a fresh TUI keystroke moves it through
`begin_execution`. The store owns every bound an untrusted caller must not
control: per-session and global pending caps, argument and preview size
limits, and the TTL.

Thread safety: the MCP server runs on its own thread while approval and
execution run on the app thread, so every store operation holds one lock.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

ProposalState = Literal[
    "pending", "approved", "denied", "expired", "cancelled", "failed", "executed"
]

#: States a proposal can never leave.
TERMINAL_STATES: frozenset[ProposalState] = frozenset(
    {"denied", "expired", "cancelled", "failed", "executed"}
)


class ProposalLimitError(Exception):
    """The per-session or global pending cap is exhausted."""


class ProposalTooLargeError(Exception):
    """The proposal's arguments exceed the size bound."""


@dataclass(frozen=True)
class WriteProposal:
    """One immutable external write proposal.

    `client_name`/`client_version` are caller-supplied metadata (MCP
    `clientInfo`) — never authenticated identity. The `id` is high-entropy
    and doubles as the status-visibility capability: possession of the id
    is the defined policy for querying its outcome.
    """

    id: str
    action: str
    group: str
    version: str
    kind: str
    namespace: str | None
    name: str
    arguments_json: str
    uid: str | None
    context: str | None
    context_epoch: int
    summary: str
    preview: tuple[str, ...]
    session_id: str
    client_name: str
    client_version: str
    created_at: float
    expires_at: float


class ProposalStore:
    """Bounded, thread-safe store owning the proposal state machine.

    Transitions: `pending → approved → executed | failed` via
    `begin_execution`/`finish_execution` (approval claims exactly once),
    and `pending → denied | expired | cancelled` via `resolve`,
    `expire_all`, `cancel`, or the lazy TTL sweep. An approved proposal
    never expires mid-execution.
    """

    def __init__(
        self,
        *,
        ttl: float = 600.0,
        max_pending_per_session: int = 3,
        max_pending_total: int = 10,
        max_argument_chars: int = 8192,
        max_preview_lines: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max_per_session = max_pending_per_session
        self._max_total = max_pending_total
        self._max_argument_chars = max_argument_chars
        self._max_preview_lines = max_preview_lines
        self._clock = clock
        self._lock = threading.Lock()
        self._proposals: dict[str, WriteProposal] = {}
        self._states: dict[str, tuple[ProposalState, str]] = {}
        self._order: list[str] = []
        self._subscribers: list[Callable[[], None]] = []

    def subscribe(self, callback: Callable[[], None]) -> None:
        """Register a callback fired after every submit or state change.

        Callbacks run outside the lock and may be invoked from any thread;
        UI subscribers must marshal onto their own event loop.
        """
        with self._lock:
            self._subscribers.append(callback)

    def submit(
        self,
        *,
        action: str,
        group: str,
        version: str,
        kind: str,
        namespace: str | None,
        name: str,
        arguments_json: str,
        uid: str | None,
        context: str | None,
        context_epoch: int,
        summary: str,
        preview: tuple[str, ...],
        session_id: str,
        client_name: str,
        client_version: str,
    ) -> WriteProposal:
        """Create a pending proposal, enforcing every caller-facing bound.

        Raises:
            ProposalTooLargeError: `arguments_json` exceeds the size bound.
            ProposalLimitError: the session or global pending cap is full.
        """
        if len(arguments_json) > self._max_argument_chars:
            raise ProposalTooLargeError(
                f"proposal arguments exceed {self._max_argument_chars} characters"
            )
        if len(preview) > self._max_preview_lines:
            omitted = len(preview) - self._max_preview_lines
            preview = (
                *preview[: self._max_preview_lines],
                f"... preview truncated ({omitted} more lines)",
            )
        now = self._clock()
        with self._lock:
            self._sweep_expired(now)
            pending = self._iter_pending()
            if sum(p.session_id == session_id for p in pending) >= self._max_per_session:
                raise ProposalLimitError(
                    f"session already has {self._max_per_session} pending proposals"
                )
            if len(pending) >= self._max_total:
                raise ProposalLimitError(
                    f"global pending proposal limit of {self._max_total} reached"
                )
            proposal = WriteProposal(
                id=secrets.token_urlsafe(32),
                action=action,
                group=group,
                version=version,
                kind=kind,
                namespace=namespace,
                name=name,
                arguments_json=arguments_json,
                uid=uid,
                context=context,
                context_epoch=context_epoch,
                summary=summary,
                preview=preview,
                session_id=session_id,
                client_name=client_name,
                client_version=client_version,
                created_at=now,
                expires_at=now + self._ttl,
            )
            self._proposals[proposal.id] = proposal
            self._states[proposal.id] = ("pending", "")
            self._order.append(proposal.id)
        self._notify()
        return proposal

    def get(self, proposal_id: str) -> tuple[WriteProposal, ProposalState, str] | None:
        """The proposal plus its current state, or None for an unknown id."""
        with self._lock:
            self._sweep_expired(self._clock())
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                return None
            state, reason = self._states[proposal_id]
            return proposal, state, reason

    def pending(self) -> list[WriteProposal]:
        """Pending proposals in submission order (expired ones swept first)."""
        with self._lock:
            self._sweep_expired(self._clock())
            return list(self._iter_pending())

    def resolve(self, proposal_id: str, state: ProposalState, *, reason: str = "") -> bool:
        """Move a pending proposal to a terminal state exactly once."""
        if state not in TERMINAL_STATES:
            raise ValueError(f"resolve() only accepts terminal states, got {state!r}")
        changed = self._transition(proposal_id, from_state="pending", to=(state, reason))
        if changed:
            self._notify()
        return changed

    def begin_execution(self, proposal_id: str) -> bool:
        """Claim a pending proposal for execution; exactly one claim wins."""
        changed = self._transition(
            proposal_id, from_state="pending", to=("approved", "approved by user")
        )
        if changed:
            self._notify()
        return changed

    def finish_execution(self, proposal_id: str, *, executed: bool, reason: str = "") -> bool:
        """Record the terminal outcome of a claimed proposal."""
        state: ProposalState = "executed" if executed else "failed"
        changed = self._transition(proposal_id, from_state="approved", to=(state, reason))
        if changed:
            self._notify()
        return changed

    def cancel(self, proposal_id: str, *, session_id: str) -> bool:
        """Caller-cancel a pending proposal; only the submitting session may."""
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None or proposal.session_id != session_id:
                return False
        changed = self._transition(
            proposal_id, from_state="pending", to=("cancelled", "cancelled by caller")
        )
        if changed:
            self._notify()
        return changed

    def expire_all(self, *, reason: str) -> int:
        """Expire every pending proposal (context switch, shutdown, ...)."""
        count = 0
        with self._lock:
            for proposal in list(self._iter_pending()):
                self._states[proposal.id] = ("expired", reason)
                count += 1
        if count:
            self._notify()
        return count

    def _transition(
        self, proposal_id: str, *, from_state: ProposalState, to: tuple[ProposalState, str]
    ) -> bool:
        with self._lock:
            self._sweep_expired(self._clock())
            current = self._states.get(proposal_id)
            if current is None or current[0] != from_state:
                return False
            self._states[proposal_id] = to
            return True

    def _iter_pending(self) -> list[WriteProposal]:
        return [self._proposals[pid] for pid in self._order if self._states[pid][0] == "pending"]

    def _sweep_expired(self, now: float) -> None:
        # Lazy TTL: only pending proposals expire — an approved proposal is
        # mid-execution and must reach executed/failed.
        for pid in self._order:
            if self._states[pid][0] == "pending" and self._proposals[pid].expires_at <= now:
                self._states[pid] = ("expired", "proposal expired before review")

    def _notify(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            callback()
