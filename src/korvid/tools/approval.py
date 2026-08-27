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

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
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


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Everything a policy may use to decide, mirroring the Textual dialog's
    own presentation fields (title, operation description, an optional
    required-name confirmation, a preview, an ownership note, and impact
    lines). A scripted/non-interactive policy is free to ignore all of it
    and decide purely from its own pre-authored sequence.
    """

    title: str
    operation: str
    require_name: str | None = None
    preview: tuple[str, ...] | None = None
    managed_note: str | None = None
    impact_lines: tuple[str, ...] | None = None


class ApprovalPolicy(ABC):
    """A composition-root-bound capability that decides approval requests.

    Trust in a decision is established by *which concrete class* a
    composition root instantiated - verified with `isinstance` in a
    composition test - never by inspecting `ApprovalDecision.decision_source`
    at the point a decision is consumed. Production
    (`korvid.ui.agent_ui_controller.AgentUiController`) binds exactly one
    `TextualApprovalPolicy`; a TUI-free eval runner binds its own, distinct
    `ScriptedApprovalPolicy`. Do not add a generic "trusted source string"
    check anywhere that consumes an `ApprovalDecision` - that is exactly the
    forgeable pattern this type replaces.
    """

    @abstractmethod
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """Decide one approval request."""


#: The `decision_source` every `ScriptedApprovalPolicy` decision carries.
#: Distinct from `TUI_KEYSTROKE_SOURCE` by construction - a scripted
#: decision can never be mistaken for one `require_tui_keystroke_source`
#: (a check specific to `TextualApprovalPolicy`'s own self-consistency, not
#: a generic gate) would accept.
SCRIPTED_POLICY_SOURCE: Final[str] = "scripted_policy"


class ScriptedApprovalPolicy(ApprovalPolicy):
    """A deterministic, pre-authored approval policy for TUI-free runs.

    No sleeps, no timers: `decide()` pops the next scripted outcome
    synchronously. An unexpected extra call (a write the script did not
    anticipate) fails closed to `DECLINE` rather than raising or silently
    approving - a fixture that expects zero writes must see every
    unrequested write refused, never crash the run.

    `interventions[i]`, if given and not `None`, runs immediately before
    step `i`'s outcome is returned when that outcome is `APPROVE`, standing
    in for a fixture's declared `dialog_intervention`: there is no dialog to
    intervene "during" in a TUI-free run, so this is the one point that
    models it deterministically.
    """

    def __init__(
        self,
        outcomes: Sequence[ApprovalOutcome],
        *,
        interventions: Sequence[Callable[[], None] | None] | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self._interventions = list(interventions) if interventions is not None else []
        self._index = 0

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        if self._index >= len(self._outcomes):
            return ApprovalDecision(ApprovalOutcome.DECLINE, SCRIPTED_POLICY_SOURCE)
        outcome = self._outcomes[self._index]
        intervention = (
            self._interventions[self._index] if self._index < len(self._interventions) else None
        )
        self._index += 1
        if intervention is not None and outcome is ApprovalOutcome.APPROVE:
            intervention()
        return ApprovalDecision(outcome, SCRIPTED_POLICY_SOURCE)
