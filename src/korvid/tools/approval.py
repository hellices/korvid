"""The typed write-approval decision (issue TBD).

Every cluster mutation korvid performs needs one thing before it may run: a
decision that says whether the user approved it, and - just as
importantly - *how* that decision was obtained. Before this module, an
approval was a bare `bool | None` that collapsed to a two-value string the
instant it left the dialog callback (`agent_ui_controller.py`); nothing
recorded provenance, so nothing could ever assert that an approval reaching
a mutation actually came from a real dialog rather than some other caller.

`ApprovalDecision` is that provenance-carrying value: an `ApprovalOutcome`
(approve/decline/dismiss/expire) plus a `decision_source` string.
`require_tui_keystroke_source` is the fail-closed gate every production
approval passes through before it may authorize a write: only a
`decision_source` of `TUI_KEYSTROKE_SOURCE` may ever carry an `APPROVE`
outcome forward, since that is the only source the running Textual app
(`ConfirmScreen`, gated by its own `FreshKeysInput` key-freshness check) is
ever allowed to produce. Decline, dismiss, and expire carry no mutating
authority, so their source is informational only and is never rejected
here.

This module holds no reference to Textual, `korvid.ui`, or any concrete
approval surface - a decision is built by whichever caller resolved the
approval (today, only the Textual app) and handed to
`require_tui_keystroke_source` and, from there, to
`korvid.tools.write_coordinator.run_approved_write`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ApprovalOutcome(StrEnum):
    """How a write-approval request was resolved."""

    APPROVE = "approve"
    DECLINE = "decline"
    DISMISS = "dismiss"
    EXPIRE = "expire"


#: The only `decision_source` production code may ever present alongside an
#: `APPROVE` outcome: a real key event resolved by `ConfirmScreen` after it
#: was shown. `ConfirmScreen`'s own `FreshKeysInput` already discards any
#: key event timestamped before the dialog existed, so every decision this
#: source names is guaranteed to be a genuine, post-dialog user keystroke.
TUI_KEYSTROKE_SOURCE: Final[str] = "tui_keystroke"


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """A resolved write-approval request: what happened, and how."""

    outcome: ApprovalOutcome
    decision_source: str

    @classmethod
    def approved(cls, decision_source: str) -> ApprovalDecision:
        return cls(ApprovalOutcome.APPROVE, decision_source)

    @classmethod
    def declined(cls, decision_source: str) -> ApprovalDecision:
        return cls(ApprovalOutcome.DECLINE, decision_source)

    @classmethod
    def dismissed(cls, decision_source: str) -> ApprovalDecision:
        return cls(ApprovalOutcome.DISMISS, decision_source)

    @classmethod
    def expired(cls, decision_source: str) -> ApprovalDecision:
        return cls(ApprovalOutcome.EXPIRE, decision_source)


class RejectedApprovalSourceError(RuntimeError):
    """An `APPROVE` decision arrived from a source production must refuse."""


def require_tui_keystroke_source(decision: ApprovalDecision) -> ApprovalDecision:
    """Fail-closed gate: an `APPROVE` decision must be `tui_keystroke`-sourced.

    Only `APPROVE` is gated - it is the only outcome that can authorize a
    mutation. A decline, dismiss, or expire changes nothing on the cluster
    no matter where it came from, so gating them would add no safety.
    """
    if (
        decision.outcome is ApprovalOutcome.APPROVE
        and decision.decision_source != TUI_KEYSTROKE_SOURCE
    ):
        raise RejectedApprovalSourceError(
            f"approval source {decision.decision_source!r} is not allowed to authorize a write"
        )
    return decision
