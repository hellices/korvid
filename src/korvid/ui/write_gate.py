"""The write-approval boundary, as a checked interface (issue #187).

Controllers do not perform approved writes themselves; they compose an
operation and hand it to the app through this gate. Naming that boundary as
an `abc.ABC` rather than a bag of `Callable[..., ...]` is the point: the
keywords that carry the security contract - `action`, `meta`, `op_factory`,
`epoch` - are then checked by mypy at every call site and in every fake,
instead of being erased by an ellipsis.

The single implementation lives on `KorvidApp`, which owns the confirm
dialog, the `_run_write` worker, and the fail-closed intent audit.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from korvid.k8s.discovery import ResourceMeta


class WriteGate(ABC):
    """Approval, revalidation, and audited execution of a cluster write."""

    @abstractmethod
    async def confirm(
        self,
        title: str,
        operation: str,
        *,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        op_factory: Callable[[], Awaitable[None]],
        detail: str = "",
        require_name: str | None = None,
        preview: list[str] | None = None,
        preview_title: str = "server dry-run preview:",
        managed_note: str | None = None,
    ) -> None:
        """Ask the user to approve `operation`, then run it if they agree.

        Takes an operation *factory*, not a coroutine: a declined dialog must
        never construct the mutation, so there is nothing to leak unawaited
        and no side effect before approval. On approval the app awaits the
        factory from its own worker, after the intent audit record persisted.
        """

    @abstractmethod
    def context_intact(
        self,
        action: str,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        *,
        phase: str = "the permission check",
        epoch: int,
    ) -> bool:
        """Whether the write may still proceed after an awaited gap.

        `epoch` is captured when the flow began; a context switch that
        started or completed during the gap invalidates it, because a
        same-named row on another cluster would otherwise pass the
        selection checks.
        """

    @abstractmethod
    async def permitted(
        self, action: str, meta: ResourceMeta, namespace: str | None, name: str
    ) -> bool:
        """SubjectAccessReview pre-check, run before the dialog is pushed.

        Advisory by design: with no checker injected this is True and the
        write still passes the approval gate and the audit. It exists so a
        missing permission is reported before a failed mutation, not instead
        of the gate.
        """

    @abstractmethod
    def run(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        op_factory: Callable[[], Awaitable[None]],
        detail: str = "",
    ) -> Coroutine[Any, Any, str]:
        """Build the coroutine for an already-approved, fail-closed write.

        Synchronous on purpose, returning an unstarted coroutine: the
        in-flight cluster write is reserved *here*, so a `:ctx` queued
        between the confirmation callback and `run_worker` starting the
        coroutine already sees it. Wrapping this in an async adapter
        reintroduces exactly that gap.

        The intent record must persist *before* the mutation; if it cannot,
        the write is blocked. Only for flows that own their own approval
        step (the operator install dialog re-checks the UID inside its
        callback) - everything else goes through `confirm`, which calls this
        internally. Returns a short outcome string.
        """

    @abstractmethod
    def reserve_write(self) -> Callable[[], None]:
        """Reserve an in-flight cluster mutation; returns the release.

        `:ctx` switching consults this count, so the reservation must be
        taken **synchronously** at the point the write coroutine is
        *constructed*, not when it starts running: a confirmation callback
        builds the coroutine and hands it to `run_worker`, which only starts
        it on a later event-loop iteration, and a `:ctx` processed in that
        gap must already see the write as in flight.

        The returned release is idempotent, so a coroutine that is closed or
        collected without ever running cannot leak a reservation and wedge
        every future `:ctx` switch.
        """

    @abstractmethod
    def audit_configured(self) -> bool:
        """Whether an audit sink exists.

        Part of the perimeter, not a detail of it: auditing is fail-closed,
        so no sink means no write may start. Controllers check this in their
        own gate so the refusal is reported in their own language.
        """

    @abstractmethod
    def epoch(self) -> int:
        """The current context epoch, captured at the start of a flow."""

    @abstractmethod
    def switching(self) -> bool:
        """Whether a context switch is in flight right now."""
