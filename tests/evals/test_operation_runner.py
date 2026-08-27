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

from korvid.evals.fake_kube import builtin_aliases
from korvid.evals.operation import OperationJourney, bundled_operations_dir, load_operation_journeys
from korvid.evals.operation_journal import ActionJournal
from korvid.evals.operation_runner import ScriptedOperationBridge, approval_from_result
from korvid.evals.operation_state import StatefulFakeKubeClient
from korvid.evals.scripted import ScriptedProvider
from korvid.tools.approval import ApprovalOutcome, ScriptedApprovalPolicy
from korvid.tools.audit import AuditLog

from .operation_scripts import OPERATION_SCRIPTS


def _load(journey_id: str) -> OperationJourney:
    journeys = load_operation_journeys(bundled_operations_dir())
    return next(j for j in journeys if j.id == journey_id)


def _bridge(
    journey: OperationJourney,
    audit_path: Path,
    outcomes: Sequence[ApprovalOutcome],
    **kwargs: Any,
) -> tuple[ScriptedOperationBridge, ActionJournal, StatefulFakeKubeClient]:
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
    return bridge, journal, kube


async def test_scripted_bridge_scales_on_approve(tmp_path: Path) -> None:
    journey = _load("scale-deployment-up")
    bridge, journal, _kube = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.APPROVE])
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
    bridge, journal, _kube = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.DECLINE])
    result = await bridge.agent_request_write(
        "scale", journey.target.kind, journey.target.name, journey.target.namespace, replicas=5
    )
    assert approval_from_result(result) == "denied"
    assert result.startswith("denied:")
    events = [event.approval for event in journal.events if event.event == "approval_observed"]
    assert events == ["denied"]


async def test_scripted_bridge_reports_expiry(tmp_path: Path) -> None:
    journey = _load("restart-approval-expired")
    bridge, journal, _kube = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.EXPIRE])
    result = await bridge.agent_request_write(
        "rollout_restart", journey.target.kind, journey.target.name, journey.target.namespace
    )
    assert approval_from_result(result) == "expired"
    assert "expired" in result
    events = [event.approval for event in journal.events if event.event == "approval_observed"]
    assert events == ["expired"]


async def test_scripted_bridge_rejects_unsupported_action(tmp_path: Path) -> None:
    journey = _load("edit-unsupported")
    bridge, _journal, _kube = _bridge(journey, tmp_path / "audit.jsonl", [])
    result = await bridge.agent_request_write(
        "edit", journey.target.kind, journey.target.name, journey.target.namespace
    )
    assert result.startswith("ERROR:")


async def test_scripted_bridge_rejects_unknown_kind(tmp_path: Path) -> None:
    journey = _load("scale-deployment-up")
    bridge, _journal, _kube = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.APPROVE])
    result = await bridge.agent_request_write(
        "scale", "NotAKind", journey.target.name, journey.target.namespace, replicas=5
    )
    assert result.startswith("ERROR: unknown kind")


async def test_scripted_bridge_rejects_negative_replicas_before_approval(tmp_path: Path) -> None:
    """A malformed replicas argument must fail the same way production's
    `AgentUiController._scale_op` fails it: before permission checks,
    manifest fetch, approval, audit, or mutation - an empty (fail-closed)
    approval script proves `decide()` was never reached, since an actual
    call would instead return a `'denied: ...'` result."""
    journey = _load("scale-deployment-up")
    audit_path = tmp_path / "audit.jsonl"
    bridge, journal, _kube = _bridge(journey, audit_path, [])
    result = await bridge.agent_request_write(
        "scale", journey.target.kind, journey.target.name, journey.target.namespace, replicas=-1
    )
    assert result == "ERROR: scale requires a 'replicas' argument >= 0"
    assert not audit_path.exists(), "a rejected request must never audit an intent"
    assert not journal.has("write_target_bound")
    assert not journal.has("approval_observed")


async def test_scripted_bridge_rejects_missing_replicas_before_approval(tmp_path: Path) -> None:
    journey = _load("scale-deployment-up")
    audit_path = tmp_path / "audit.jsonl"
    bridge, journal, _kube = _bridge(journey, audit_path, [])
    result = await bridge.agent_request_write(
        "scale", journey.target.kind, journey.target.name, journey.target.namespace, replicas=None
    )
    assert result == "ERROR: scale requires a 'replicas' argument >= 0"
    assert not audit_path.exists()
    assert not journal.has("write_target_bound")


async def test_scripted_bridge_rejects_scale_on_a_non_scalable_kind(tmp_path: Path) -> None:
    """DaemonSet is RESTARTABLE but not SCALABLE - the same identity
    production's `_scale_op` rejects via `validate_scale_request`."""
    journey = _load("restart-daemonset")
    audit_path = tmp_path / "audit.jsonl"
    bridge, journal, _kube = _bridge(journey, audit_path, [])
    result = await bridge.agent_request_write(
        "scale", journey.target.kind, journey.target.name, journey.target.namespace, replicas=3
    )
    assert result == "ERROR: scale does not apply to daemonsets.apps"
    assert not audit_path.exists()
    assert not journal.has("write_target_bound")


async def test_scripted_bridge_rejects_restart_on_a_non_restartable_kind(tmp_path: Path) -> None:
    """ReplicaSet is SCALABLE but not RESTARTABLE - validation runs before
    the manifest fetch, so no fixture object need actually exist."""
    journey = _load("scale-deployment-up")
    audit_path = tmp_path / "audit.jsonl"
    bridge, journal, _kube = _bridge(journey, audit_path, [])
    result = await bridge.agent_request_write(
        "rollout_restart", "ReplicaSet", journey.target.name, journey.target.namespace
    )
    assert result == "ERROR: rollout restart does not apply to replicasets.apps"
    assert not audit_path.exists()
    assert not journal.has("write_target_bound")


async def test_scripted_bridge_reports_missing_target(tmp_path: Path) -> None:
    journey = _load("scale-deployment-up")
    bridge, _journal, _kube = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.APPROVE])
    result = await bridge.agent_request_write(
        "scale", journey.target.kind, "does-not-exist", journey.target.namespace, replicas=5
    )
    assert result.startswith("ERROR:")
    assert "not found" in result


async def test_scripted_bridge_honors_permission_denial(tmp_path: Path) -> None:
    journey = _load("scale-rbac-denied")
    bridge, journal, _kube = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.APPROVE])
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
        bridge, _journal, _kube = _bridge(
            journey, tmp_path / f"audit-{path_suffix}.jsonl", [outcome]
        )
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


async def test_journaling_executor_records_precondition_read(tmp_path: Path) -> None:
    """The ported executor journals a target-resolving read and credits it."""
    from korvid.evals.operation_runner import _OperationJournalingExecutor
    from korvid.tools.executor import ToolExecutor

    journey = _load("scale-deployment-up")
    bridge, journal, kube = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.APPROVE])
    executor = _OperationJournalingExecutor(
        ToolExecutor(kube, builtin_aliases(), ui=bridge),
        journal,
        journey,
        max_result_chars=20000,
    )
    result = await executor.execute(
        "get_resource",
        {
            "kind": journey.target.kind,
            "name": journey.target.name,
            "namespace": journey.target.namespace,
        },
    )
    events = {event.event for event in journal.events}
    assert "target_resolved" in events
    assert "precondition_read" in events
    assert result


async def test_journaling_executor_records_write_requested_and_reported(
    tmp_path: Path,
) -> None:
    from korvid.evals.operation_runner import _OperationJournalingExecutor
    from korvid.tools.executor import ToolExecutor

    journey = _load("scale-deployment-up")
    bridge, journal, kube = _bridge(journey, tmp_path / "audit.jsonl", [ApprovalOutcome.APPROVE])
    executor = _OperationJournalingExecutor(
        ToolExecutor(kube, builtin_aliases(), ui=bridge),
        journal,
        journey,
        max_result_chars=20000,
    )
    result = await executor.execute(
        "scale_resource",
        {
            "kind": journey.target.kind,
            "name": journey.target.name,
            "namespace": journey.target.namespace,
            "replicas": 5,
        },
    )
    events = [event.event for event in journal.events]
    assert "write_requested" in events
    assert "approval_reported" in events
    assert approval_from_result(result) == "approved" or result.startswith("approved and executed")


_BUNDLED_JOURNEYS = load_operation_journeys(bundled_operations_dir())
_BUNDLED_IDS = [journey.id for journey in _BUNDLED_JOURNEYS]


async def _run_scripted_case(journey_id: str, tmp_path: Path) -> Any:
    from korvid.evals.operation_runner import run_operation_case

    journey = _load(journey_id)
    return await run_operation_case(
        journey,
        audit_path=tmp_path / "audit.jsonl",
        provider_factory=lambda: ScriptedProvider(OPERATION_SCRIPTS[journey_id]),
    )


@pytest.mark.parametrize("journey_id", _BUNDLED_IDS)
async def test_run_operation_case_matches_the_fixture_pack(journey_id: str, tmp_path: Path) -> None:
    """`run_operation_case` grades every bundled fixture exactly as its
    Textual-driven counterpart (`run_operation_journey`) does: same
    scripted transcript, same safety verdict, same outcome."""
    run = await _run_scripted_case(journey_id, tmp_path)
    journey = _load(journey_id)
    assert run.journey_id == journey_id
    assert run.grade.safe is True
    assert run.grade.outcome == journey.expected_outcome
    assert run.wall_time_s >= 0.0


async def test_run_operation_case_publishes_prompt_and_decisions(tmp_path: Path) -> None:
    """The published run carries prompt identity and decision provenance,
    not just the grade."""
    from korvid.tools.approval import SCRIPTED_POLICY_SOURCE

    run = await _run_scripted_case("scale-deployment-up", tmp_path)
    assert run.prompt["pack"]
    assert run.prompt["sha256"]
    assert run.decisions == ({"outcome": "approve", "decision_source": SCRIPTED_POLICY_SOURCE},)
    assert any(event["event"] == "outcome_reported" for event in run.journal)
    assert run.audit  # the scale mutation left at least one persisted audit record


async def test_run_operation_case_never_dialogs_for_a_no_write_fixture(tmp_path: Path) -> None:
    """A fixture with `expected_approval_dialogs == 0` scripts zero
    outcomes: `run.decisions` is empty, proving the policy was never
    consulted for a write the fixture never expected."""
    run = await _run_scripted_case("scale-no-op", tmp_path)
    assert run.decisions == ()
    assert run.grade.outcome == "completed"


async def test_run_operation_case_omits_decisions_when_no_write_is_requested(
    tmp_path: Path,
) -> None:
    """`decisions` must reflect only decisions the policy actually made -
    never the planned script. `scale-no-op`'s transcript never calls a
    write tool at all, so even an explicit non-empty `approval_script`
    override must publish zero decisions: `policy.decide()` was never
    invoked, so there is nothing to report."""
    from korvid.evals.operation_runner import run_operation_case

    journey = _load("scale-no-op")
    run = await run_operation_case(
        journey,
        audit_path=tmp_path / "audit.jsonl",
        provider_factory=lambda: ScriptedProvider(OPERATION_SCRIPTS["scale-no-op"]),
        approval_script=[ApprovalOutcome.APPROVE],
    )
    assert run.decisions == ()


async def test_run_operation_case_records_a_fail_closed_decline_past_the_script(
    tmp_path: Path,
) -> None:
    """A write request the script did not anticipate still gets a real
    `ApprovalDecision` back (`ScriptedApprovalPolicy` fails closed to
    `DECLINE`) - and that decision must appear in `run.decisions`, not be
    silently dropped because it falls outside the authored script."""
    from korvid.evals.operation_runner import run_operation_case
    from korvid.tools.approval import SCRIPTED_POLICY_SOURCE

    journey = _load("scale-deployment-up")
    run = await run_operation_case(
        journey,
        audit_path=tmp_path / "audit.jsonl",
        provider_factory=lambda: ScriptedProvider(OPERATION_SCRIPTS["scale-deployment-up"]),
        approval_script=[],  # exhausted before the fixture's own single write request
    )
    assert run.decisions == ({"outcome": "decline", "decision_source": SCRIPTED_POLICY_SOURCE},)
