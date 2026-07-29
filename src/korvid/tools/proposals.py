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

#: Stored reason (and hook argument) for proposals the lazy TTL sweep expires.
_TTL_EXPIRY_REASON = "proposal expired before review"


class ProposalLimitError(Exception):
    """The per-session or global pending cap is exhausted."""


class ProposalClosedError(Exception):
    """The store no longer accepts submissions (the TUI session ended)."""


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

    Terminal records stay pollable for `terminal_retention` seconds (and at
    most `max_terminal` records, oldest evicted first) so an authorized but
    untrusted caller cannot grow memory without bound by churning proposals;
    after the window `get` reports the id as unknown.
    """

    def __init__(
        self,
        *,
        ttl: float = 600.0,
        max_pending_per_session: int = 3,
        max_pending_total: int = 10,
        max_argument_chars: int = 8192,
        max_preview_lines: int = 100,
        terminal_retention: float = 600.0,
        max_terminal: int = 50,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max_per_session = max_pending_per_session
        self._max_total = max_pending_total
        self._max_argument_chars = max_argument_chars
        self._max_preview_lines = max_preview_lines
        self._terminal_retention = terminal_retention
        self._max_terminal = max_terminal
        self._clock = clock
        self._lock = threading.Lock()
        self._proposals: dict[str, WriteProposal] = {}
        self._states: dict[str, tuple[ProposalState, str]] = {}
        self._order: list[str] = []
        self._resolved_at: dict[str, float] = {}
        self._subscribers: list[Callable[[], None]] = []
        self._on_expired: Callable[[WriteProposal, str], None] | None = None
        self._closed = False

    def subscribe(self, callback: Callable[[], None]) -> None:
        """Register a callback fired after every submit or state change.

        Callbacks run outside the lock and may be invoked from any thread;
        UI subscribers must marshal onto their own event loop.
        """
        with self._lock:
            self._subscribers.append(callback)

    def set_on_expired(self, callback: Callable[[WriteProposal, str], None]) -> None:
        """Hook fired once per proposal the lazy TTL sweep expires.

        Runs outside the lock and may fire from any thread — the korvid app
        marshals it onto the UI loop to audit the outcome. `expire_all` does
        NOT fire it: that path returns its proposals to the caller directly.
        Set once at wiring time, before the store is shared across threads.
        """
        self._on_expired = callback

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
            ProposalClosedError: the store was closed (shutdown in progress).
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
        proposal: WriteProposal | None = None
        error: Exception | None = None
        with self._lock:
            swept = self._sweep_expired(now)
            # Errors are collected, not raised, inside the lock: a raise here
            # would skip _after_sweep and permanently drop the notification
            # and expiry hook for anything the sweep just expired.
            if self._closed:
                error = ProposalClosedError(
                    "the proposal store is closed (the TUI session is shutting down)"
                )
            else:
                error = self._cap_error(session_id)
            if error is None:
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
        self._after_sweep(swept)
        if error is not None:
            raise error
        if proposal is None:  # pragma: no cover — error is None implies proposal was built
            raise RuntimeError("submit built neither a proposal nor an error")
        self._notify()
        return proposal

    def _cap_error(self, session_id: str) -> ProposalLimitError | None:
        """The pending-cap violation a submit would hit, if any (lock held)."""
        pending = self._iter_pending()
        if sum(p.session_id == session_id for p in pending) >= self._max_per_session:
            return ProposalLimitError(
                f"session already has {self._max_per_session} pending proposals"
            )
        if len(pending) >= self._max_total:
            return ProposalLimitError(f"global pending proposal limit of {self._max_total} reached")
        return None

    def close(self) -> None:
        """Refuse all future submissions (shutdown): any in-flight MCP call
        that lands after the final expiry sweep gets an error instead of
        queueing a proposal nobody will ever review, expire, or audit."""
        with self._lock:
            self._closed = True

    @property
    def max_argument_chars(self) -> int:
        """Serialized-argument size bound, exposed so intake can reject an
        oversized payload before doing any cluster I/O (the store itself
        still rechecks atomically at submit)."""
        return self._max_argument_chars

    def get(self, proposal_id: str) -> tuple[WriteProposal, ProposalState, str] | None:
        """The proposal plus its current state, or None for an unknown id."""
        found: tuple[WriteProposal, ProposalState, str] | None = None
        with self._lock:
            swept = self._sweep_expired(self._clock())
            proposal = self._proposals.get(proposal_id)
            if proposal is not None:
                state, reason = self._states[proposal_id]
                found = (proposal, state, reason)
        self._after_sweep(swept)
        return found

    def pending(self) -> list[WriteProposal]:
        """Pending proposals in submission order (expired ones swept first)."""
        with self._lock:
            swept = self._sweep_expired(self._clock())
            result = list(self._iter_pending())
        self._after_sweep(swept)
        return result

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

    def expire_all(self, *, reason: str) -> list[WriteProposal]:
        """Expire every pending proposal (context switch, shutdown, ...).

        Returns the proposals that were expired so the caller can audit each
        terminal outcome.
        """
        expired: list[WriteProposal] = []
        with self._lock:
            now = self._clock()
            for proposal in list(self._iter_pending()):
                self._states[proposal.id] = ("expired", reason)
                self._resolved_at[proposal.id] = now
                expired.append(proposal)
        if expired:
            self._notify()
        return expired

    def _transition(
        self, proposal_id: str, *, from_state: ProposalState, to: tuple[ProposalState, str]
    ) -> bool:
        with self._lock:
            now = self._clock()
            swept = self._sweep_expired(now)
            current = self._states.get(proposal_id)
            changed = current is not None and current[0] == from_state
            if changed:
                self._states[proposal_id] = to
                if to[0] in TERMINAL_STATES:
                    self._resolved_at[proposal_id] = now
        self._after_sweep(swept)
        return changed

    def _iter_pending(self) -> list[WriteProposal]:
        return [self._proposals[pid] for pid in self._order if self._states[pid][0] == "pending"]

    def _sweep_expired(self, now: float) -> list[WriteProposal]:
        # Lazy TTL: only pending proposals expire — an approved proposal is
        # mid-execution and must reach executed/failed. Returns the newly
        # expired proposals so the caller can notify and fire the expiry
        # hook after releasing the lock.
        swept: list[WriteProposal] = []
        for pid in self._order:
            if self._states[pid][0] == "pending" and self._proposals[pid].expires_at <= now:
                self._states[pid] = ("expired", _TTL_EXPIRY_REASON)
                self._resolved_at[pid] = now
                swept.append(self._proposals[pid])
        # Bounded terminal retention: keep resolved records pollable for a
        # defined window, then drop them so proposal churn cannot grow
        # memory without bound.
        terminal = [pid for pid in self._order if self._states[pid][0] in TERMINAL_STATES]
        evict = {
            pid for pid in terminal if self._resolved_at[pid] + self._terminal_retention <= now
        }
        # Cap eviction drops the oldest *resolved* records first (not the
        # oldest submitted): a just-resolved outcome must stay pollable.
        keep = sorted(
            (pid for pid in terminal if pid not in evict),
            key=lambda pid: self._resolved_at[pid],
        )
        if len(keep) > self._max_terminal:
            evict.update(keep[: len(keep) - self._max_terminal])
        if evict:
            self._order = [pid for pid in self._order if pid not in evict]
            for pid in evict:
                del self._proposals[pid]
                del self._states[pid]
                del self._resolved_at[pid]
        return swept

    def _after_sweep(self, swept: list[WriteProposal]) -> None:
        """Lazy TTL expiry is a real state change: tell subscribers (so a
        pending-count indicator refreshes) and fire the expiry hook (so the
        app can audit each outcome), both outside the lock."""
        if not swept:
            return
        hook = self._on_expired
        if hook is not None:
            for proposal in swept:
                hook(proposal, _TTL_EXPIRY_REASON)
        self._notify()

    def _notify(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            callback()
