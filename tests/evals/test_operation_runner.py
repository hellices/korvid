"""Tests for the TUI-free operation-journey runner's write bridge.

`ScriptedOperationBridge` is the only new production write seam: it must
run the same shared `run_approved_write`/`AuditLog`/`StatefulFakeWriteOps`
path a Textual run uses, gated by an injected `ApprovalPolicy` rather than
a `ConfirmScreen`, and it must never treat a bare string as authorization
(the policy's typed `ApprovalDecision` is what it inspects).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from korvid.evals.operation import OperationJourney, bundled_operations_dir, load_operation_journeys
from korvid.evals.operation_journal import ActionJournal
from korvid.evals.operation_runner import ScriptedOperationBridge, approval_from_result
from korvid.evals.operation_state import StatefulFakeKubeClient
from korvid.tools.approval import ApprovalOutcome, ScriptedApprovalPolicy
from korvid.tools.audit import AuditLog


def _load(journey_id: str) -> OperationJourney:
    journeys = load_operation_journeys(bundled_operations_dir())
    return next(j for j in journeys if j.id == journey_id)


def _bridge(
    journey: OperationJourney,
    audit_path: Path,
    outcomes: Sequence[ApprovalOutcome],
    **kwargs: Any,
) -> tuple[ScriptedOperationBridge, ActionJournal]:
    kube = StatefulFakeKubeClient(journey.cluster)
    journal = ActionJournal()
    audit = AuditLog(audit_path, context=journey.target.context)
    policy = ScriptedApprovalPolicy(outcomes)
    bridge = ScriptedOperationBridge(
        kube=kube,
        journal=journal,
        journey=journey,
        audit=audit,
        audit_path=audit_path,
        policy=policy,
        **kwargs,
    )
    return bridge, journal


async def test_scripted_bridge_scales_on_approve(tmp_path: Path) -> None:
    journey = _load("scale-deployment-up")
    bridge, journal = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.APPROVE])
    result = await bridge.agent_request_write(
        "scale",
        journey.target.kind,
        journey.target.name,
        journey.target.namespace,
        replicas=5,
    )
    assert approval_from_result(result) == "approved"
    assert result.startswith("approved and executed")
    assert journal.has("write_target_bound")
    assert journal.has("approval_observed")
    assert (tmp_path / "audit.jsonl").exists()


async def test_scripted_bridge_denies_on_decline(tmp_path: Path) -> None:
    journey = _load("scale-deployment-up")
    bridge, journal = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.DECLINE])
    result = await bridge.agent_request_write(
        "scale", journey.target.kind, journey.target.name, journey.target.namespace, replicas=5
    )
    assert approval_from_result(result) == "denied"
    assert result.startswith("denied:")
    events = [event.approval for event in journal.events if event.event == "approval_observed"]
    assert events == ["denied"]


async def test_scripted_bridge_reports_expiry(tmp_path: Path) -> None:
    journey = _load("restart-approval-expired")
    bridge, journal = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.EXPIRE])
    result = await bridge.agent_request_write(
        "rollout_restart", journey.target.kind, journey.target.name, journey.target.namespace
    )
    assert approval_from_result(result) == "expired"
    assert "expired" in result
    events = [event.approval for event in journal.events if event.event == "approval_observed"]
    assert events == ["expired"]


async def test_scripted_bridge_rejects_unsupported_action(tmp_path: Path) -> None:
    journey = _load("edit-unsupported")
    bridge, _journal = _bridge(journey, tmp_path / "audit.jsonl", [])
    result = await bridge.agent_request_write(
        "edit", journey.target.kind, journey.target.name, journey.target.namespace
    )
    assert result.startswith("ERROR:")


async def test_scripted_bridge_rejects_unknown_kind(tmp_path: Path) -> None:
    journey = _load("scale-deployment-up")
    bridge, _journal = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.APPROVE])
    result = await bridge.agent_request_write(
        "scale", "NotAKind", journey.target.name, journey.target.namespace, replicas=5
    )
    assert result.startswith("ERROR: unknown kind")


async def test_scripted_bridge_reports_missing_target(tmp_path: Path) -> None:
    journey = _load("scale-deployment-up")
    bridge, _journal = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.APPROVE])
    result = await bridge.agent_request_write(
        "scale", journey.target.kind, "does-not-exist", journey.target.namespace, replicas=5
    )
    assert result.startswith("ERROR:")
    assert "not found" in result


async def test_scripted_bridge_honors_permission_denial(tmp_path: Path) -> None:
    journey = _load("scale-rbac-denied")
    bridge, journal = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.APPROVE])
    result = await bridge.agent_request_write(
        "scale", journey.target.kind, journey.target.name, journey.target.namespace, replicas=5
    )
    assert result.startswith("ERROR: missing permission")
    assert journal.has("permission_denied")
    # A denied permission must never reach the approval policy at all.
    assert not journal.has("approval_observed")


async def test_scripted_bridge_never_treats_a_string_as_authorization(tmp_path: Path) -> None:
    """The bridge only accepts a typed `ApprovalPolicy`, never a bare token.

    A plain string has no `.decide(...)` coroutine method, so passing one
    in place of a real `ApprovalPolicy` must never be silently treated as
    an approval — it must fail loudly (an `AttributeError`) instead of
    ever reaching `"approved and executed"`.
    """
    journey = _load("scale-deployment-up")
    kube = StatefulFakeKubeClient(journey.cluster)
    journal = ActionJournal()
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(audit_path, context=journey.target.context)
    bridge = ScriptedOperationBridge(
        kube=kube,
        journal=journal,
        journey=journey,
        audit=audit,
        audit_path=audit_path,
        policy="approved",  # type: ignore[arg-type]
    )
    with pytest.raises(AttributeError):
        await bridge.agent_request_write(
            "scale",
            journey.target.kind,
            journey.target.name,
            journey.target.namespace,
            replicas=5,
        )


async def test_concurrent_bridges_do_not_leak_decisions(tmp_path: Path) -> None:
    """Two runners built with distinct scripted policies never cross-talk."""
    journey = _load("scale-deployment-up")

    async def run(outcome: ApprovalOutcome, path_suffix: str) -> str:
        bridge, _journal = _bridge(journey, tmp_path / f"audit-{path_suffix}.jsonl", [outcome])
        return await bridge.agent_request_write(
            "scale",
            journey.target.kind,
            journey.target.name,
            journey.target.namespace,
            replicas=7,
        )

    approved, denied = await asyncio.gather(
        run(ApprovalOutcome.APPROVE, "a"), run(ApprovalOutcome.DECLINE, "b")
    )
    assert approval_from_result(approved) == "approved"
    assert approval_from_result(denied) == "denied"


async def test_scripted_bridge_applies_dialog_intervention_before_approval(
    tmp_path: Path,
) -> None:
    """The fixture's declared mid-dialog replacement runs before the write."""
    journey = _load("scale-same-name-replacement")
    kube = StatefulFakeKubeClient(journey.cluster)
    journal = ActionJournal()
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(audit_path, context=journey.target.context)
    intervention = journey.dialog_intervention
    assert intervention is not None
    new_uid = intervention.replace_target.uid

    def apply() -> None:
        kube.state.replace_incarnation(
            group=journey.target.group,
            kind=journey.target.kind,
            namespace=journey.target.namespace,
            name=journey.target.name,
            uid=new_uid,
        )

    policy = ScriptedApprovalPolicy([ApprovalOutcome.APPROVE], interventions=[apply])
    bridge = ScriptedOperationBridge(
        kube=kube,
        journal=journal,
        journey=journey,
        audit=audit,
        audit_path=audit_path,
        policy=policy,
    )
    result = await bridge.agent_request_write(
        "scale",
        journey.target.kind,
        journey.target.name,
        journey.target.namespace,
        replicas=3,
    )
    assert approval_from_result(result) == "approved"
    live_uid = kube.state.uid_of(
        group=journey.target.group,
        kind=journey.target.kind,
        namespace=journey.target.namespace,
        name=journey.target.name,
    )
    assert live_uid == new_uid
