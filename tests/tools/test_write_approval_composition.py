"""Composition tests: the extraction is load-bearing, not inert code.

These prove the four things the write-approval-port slice exists to
guarantee, independent of any single unit test elsewhere:

1. only a `tui_keystroke`-sourced approval may ever authorize a write;
2. `agent_ui_controller`'s own decision-building matches that contract;
3. the fail-closed audit-before-mutation ordering holds for the pure
   orchestrator every production write now runs through;
4. an unavailable audit sink blocks the mutation outright.
"""

from __future__ import annotations

import pytest

from korvid.k8s.discovery import ResourceMeta
from korvid.tools.approval import (
    TUI_KEYSTROKE_SOURCE,
    ApprovalDecision,
    RejectedApprovalSourceError,
    require_tui_keystroke_source,
)
from korvid.tools.write_coordinator import run_approved_write
from korvid.ui.agent_ui_controller import _collapse_decision, _decision_from_confirm_screen

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))


@pytest.mark.parametrize(
    ("confirmed", "expected_outcome_name"),
    [(True, "APPROVE"), (False, "DECLINE"), (None, "DISMISS")],
)
def test_every_real_dialog_resolution_is_tui_keystroke_sourced(
    confirmed: bool | None, expected_outcome_name: str
) -> None:
    decision = _decision_from_confirm_screen(confirmed)
    assert decision.decision_source == TUI_KEYSTROKE_SOURCE
    assert decision.outcome.name == expected_outcome_name


def test_collapse_decision_matches_the_legacy_three_value_contract() -> None:
    assert _collapse_decision(_decision_from_confirm_screen(True)) == "approved"
    assert _collapse_decision(_decision_from_confirm_screen(False)) == "declined"
    assert _collapse_decision(_decision_from_confirm_screen(None)) == "declined"
    assert _collapse_decision(ApprovalDecision.expired("timeout")) == "expired"


def test_a_non_tui_keystroke_approval_is_rejected_before_it_ever_reaches_a_write() -> None:
    """A future non-Textual caller (an eval runner, an MCP tool) that ever
    builds an APPROVE decision without a real dialog resolution must be
    refused here - before `run_approved_write` is even called, since an
    unauthorized decision must never reach the mutation step at all."""
    forged = ApprovalDecision.approved("scripted-test-double")
    with pytest.raises(RejectedApprovalSourceError):
        require_tui_keystroke_source(forged)


def test_collapse_decision_itself_rejects_a_forged_non_tui_keystroke_approval() -> None:
    """`_collapse_decision` is the exact function every production approval
    passes through - the gate must fire there too, not just in isolation."""
    forged = ApprovalDecision.approved("scripted-test-double")
    with pytest.raises(RejectedApprovalSourceError):
        _collapse_decision(forged)


async def test_run_approved_write_never_mutates_before_the_intent_audit_lands() -> None:
    calls: list[str] = []

    async def audit(
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        detail: str,
        outcome: str,
    ) -> None:
        calls.append(f"audit:{outcome}")

    def notify(message: str, *, severity: str) -> None:
        calls.append(f"notify:{severity}")

    async def op_factory() -> None:
        calls.append("mutate")

    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", op_factory, "", audit=audit, notify=notify
    )
    assert outcome == "done"
    assert calls == ["audit:intent", "mutate", "audit:success", "notify:information"]


async def test_run_approved_write_blocks_the_mutation_when_the_audit_sink_is_gone() -> None:
    mutated = False

    async def audit(
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        detail: str,
        outcome: str,
    ) -> None:
        raise RuntimeError("audit log not configured")

    def notify(message: str, *, severity: str) -> None:
        pass

    async def op_factory() -> None:
        nonlocal mutated
        mutated = True

    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", op_factory, "", audit=audit, notify=notify
    )
    assert outcome == "blocked: audit log unavailable"
    assert mutated is False
