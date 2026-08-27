"""Pure unit tests for the typed write-approval decision (issue TBD).

`ApprovalDecision` is the shared, UI-independent vocabulary a write
approval resolves to: `approve`/`decline`/`dismiss`/`expire`, plus the
`decision_source` that says how the decision was obtained. The production
Textual app is the only source ever allowed to produce `tui_keystroke`,
and `require_tui_keystroke_source` is the fail-closed gate that enforces
it - checked here without importing `textual` or `korvid.ui` at all.
"""

from __future__ import annotations

import pytest

from korvid.tools.approval import (
    SCRIPTED_POLICY_SOURCE,
    TUI_KEYSTROKE_SOURCE,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalPolicy,
    ApprovalRequest,
    RejectedApprovalSourceError,
    ScriptedApprovalPolicy,
    require_tui_keystroke_source,
)


def test_outcome_values_are_stable_strings() -> None:
    assert ApprovalOutcome.APPROVE.value == "approve"
    assert ApprovalOutcome.DECLINE.value == "decline"
    assert ApprovalOutcome.DISMISS.value == "dismiss"
    assert ApprovalOutcome.EXPIRE.value == "expire"


def test_decision_is_frozen() -> None:
    decision = ApprovalDecision.approved(TUI_KEYSTROKE_SOURCE)
    with pytest.raises(AttributeError):
        decision.outcome = ApprovalOutcome.DECLINE  # type: ignore[misc]


@pytest.mark.parametrize(
    ("constructor", "outcome"),
    [
        (ApprovalDecision.approved, ApprovalOutcome.APPROVE),
        (ApprovalDecision.declined, ApprovalOutcome.DECLINE),
        (ApprovalDecision.dismissed, ApprovalOutcome.DISMISS),
        (ApprovalDecision.expired, ApprovalOutcome.EXPIRE),
    ],
)
def test_each_constructor_builds_its_outcome(constructor: object, outcome: ApprovalOutcome) -> None:
    decision = constructor("some-source")  # type: ignore[operator]
    assert decision.outcome is outcome
    assert decision.decision_source == "some-source"


def test_approval_from_the_allowed_source_passes_the_gate() -> None:
    decision = ApprovalDecision.approved(TUI_KEYSTROKE_SOURCE)
    assert require_tui_keystroke_source(decision) is decision


def test_approval_from_any_other_source_is_rejected() -> None:
    decision = ApprovalDecision.approved("mcp")
    with pytest.raises(RejectedApprovalSourceError):
        require_tui_keystroke_source(decision)


@pytest.mark.parametrize(
    "constructor",
    [ApprovalDecision.declined, ApprovalDecision.dismissed, ApprovalDecision.expired],
)
def test_non_approving_outcomes_are_never_rejected_by_source(constructor: object) -> None:
    decision = constructor("anything-at-all")  # type: ignore[operator]
    assert require_tui_keystroke_source(decision) is decision


def test_approval_request_is_frozen() -> None:
    request = ApprovalRequest(title="t", operation="scale replicas")
    with pytest.raises(AttributeError):
        request.title = "changed"  # type: ignore[misc]


def test_approval_request_defaults() -> None:
    request = ApprovalRequest(title="t", operation="op")
    assert request.require_name is None
    assert request.preview is None
    assert request.managed_note is None
    assert request.impact_lines is None


def test_approval_policy_is_abstract() -> None:
    with pytest.raises(TypeError):
        ApprovalPolicy()  # type: ignore[abstract]


async def test_scripted_policy_returns_outcomes_in_order() -> None:
    policy = ScriptedApprovalPolicy([ApprovalOutcome.APPROVE, ApprovalOutcome.DECLINE])
    request = ApprovalRequest(title="t", operation="op")
    first = await policy.decide(request)
    second = await policy.decide(request)
    assert first.outcome is ApprovalOutcome.APPROVE
    assert second.outcome is ApprovalOutcome.DECLINE


async def test_scripted_policy_source_is_never_tui_keystroke() -> None:
    policy = ScriptedApprovalPolicy([ApprovalOutcome.APPROVE])
    decision = await policy.decide(ApprovalRequest(title="t", operation="op"))
    assert decision.decision_source == SCRIPTED_POLICY_SOURCE
    assert decision.decision_source != TUI_KEYSTROKE_SOURCE


async def test_scripted_policy_fails_closed_when_exhausted() -> None:
    policy = ScriptedApprovalPolicy([ApprovalOutcome.APPROVE])
    await policy.decide(ApprovalRequest(title="t", operation="op"))
    decision = await policy.decide(ApprovalRequest(title="t", operation="op"))
    assert decision.outcome is ApprovalOutcome.DECLINE


async def test_scripted_policy_runs_intervention_before_approve() -> None:
    calls: list[str] = []
    policy = ScriptedApprovalPolicy(
        [ApprovalOutcome.APPROVE],
        interventions=[lambda: calls.append("intervened")],
    )
    decision = await policy.decide(ApprovalRequest(title="t", operation="op"))
    assert calls == ["intervened"]
    assert decision.outcome is ApprovalOutcome.APPROVE


async def test_scripted_policy_does_not_run_intervention_on_decline() -> None:
    calls: list[str] = []
    policy = ScriptedApprovalPolicy(
        [ApprovalOutcome.DECLINE],
        interventions=[lambda: calls.append("intervened")],
    )
    await policy.decide(ApprovalRequest(title="t", operation="op"))
    assert calls == []
