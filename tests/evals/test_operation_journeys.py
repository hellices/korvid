"""Deterministic operation journeys through the production approval path.

Every journey runs the real `KorvidApp`, the real `AgentRuntime`, the real
`ToolExecutor`, the real `AppUIBridge`, the real unmodified fail-closed
`AuditLog`, and a Textual pilot that presses the same keys a user would.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from korvid.evals.operation import bundled_operations_dir, load_operation_journeys
from korvid.evals.operation_state import RESTART_ANNOTATION
from korvid.evals.scripted import ScriptedProvider

from .operation_app import (
    MIN_APPROVAL_TIMEOUT,
    OperationRun,
    approval_from_result,
    run_operation_journey,
)
from .operation_scripts import OPERATION_SCRIPTS

_JOURNEYS = {journey.id: journey for journey in load_operation_journeys(bundled_operations_dir())}

POSITIVE_JOURNEYS = (
    "restart-daemonset",
    "restart-deployment",
    "scale-deployment-down",
    "scale-deployment-up",
    "scale-statefulset-down",
)


async def run_scripted_journey(
    journey_id: str, tmp_path: Path, *, approval_timeout_seconds: float = 5.0
) -> OperationRun:
    """Run one journey with its deterministic script."""
    return await run_operation_journey(
        _JOURNEYS[journey_id],
        audit_path=tmp_path / "audit.jsonl",
        provider_factory=lambda: ScriptedProvider(OPERATION_SCRIPTS[journey_id]),
        approval_timeout_seconds=approval_timeout_seconds,
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("approved and executed: scale deployments.apps/checkout-a", "approved"),
        (
            "denied: the user declined the scale request for deployments.apps/checkout-a",
            "denied",
        ),
        (
            "not approved: the request expired before the user responded"
            " (scale deployments.apps/checkout-a)",
            "expired",
        ),
        (
            "ERROR: scale deployments.apps/checkout-a failed: conflict: the target changed"
            " since it was approved - refresh and retry",
            "approved",
        ),
        (
            "ERROR: scale deployments.apps/checkout-a blocked: audit log unavailable",
            "approved",
        ),
        ("ERROR: missing permission: patch deployments/scale", "error"),
    ],
)
def test_approval_from_result_classifies_every_production_write_result(
    result: str, expected: str
) -> None:
    """The four strings `KorvidApp.agent_request_write` can return, plus the
    two fail-closed shapes `_run_write_inner` wraps in `ERROR:`. A write
    that was approved and then blocked or failed still reports `approved`:
    the *user's decision* is what the grader compares against the driver's
    observation, and the audit rules are what catch the blocked write."""
    assert approval_from_result(result) == expected


async def test_a_sub_second_approval_window_is_refused(tmp_path: Path) -> None:
    """`until` polls at 0.05s and the app re-checks its budget immediately
    after `push_screen`, so a sub-second window is a flake, not a test."""
    with pytest.raises(ValueError, match="approval_timeout_seconds must be at least"):
        await run_operation_journey(
            _JOURNEYS["scale-deployment-up"],
            audit_path=tmp_path / "audit.jsonl",
            provider_factory=lambda: ScriptedProvider(OPERATION_SCRIPTS["scale-deployment-up"]),
            approval_timeout_seconds=MIN_APPROVAL_TIMEOUT / 2,
        )


@pytest.mark.parametrize("journey_id", POSITIVE_JOURNEYS)
async def test_a_positive_journey_completes_safely(journey_id: str, tmp_path: Path) -> None:
    run = await run_scripted_journey(journey_id, tmp_path)
    assert run.grade.hard_failures == ()
    assert run.grade.safe is True
    assert run.grade.missing_checkpoints == ()
    assert run.grade.outcome == "completed"
    assert run.grade.completion is True
    assert run.grade.verification is True
    assert run.grade.quality == pytest.approx(1.0)


@pytest.mark.parametrize("journey_id", POSITIVE_JOURNEYS)
async def test_a_positive_journey_reaches_the_fixture_state(
    journey_id: str, tmp_path: Path
) -> None:
    """Provisional evidence: the fake transition happened as the fixture
    declared. Excluded from the score until Slice B calibrates it."""
    run = await run_scripted_journey(journey_id, tmp_path)
    assert run.grade.provisional_assertions != ()
    assert all(result.satisfied for result in run.grade.provisional_assertions)


async def test_the_audit_intent_is_durable_before_the_mutation(tmp_path: Path) -> None:
    """Fail-closed ordering, proved from persisted evidence.

    The audit log is the production one, unmodified: the ordering claim
    comes from the injected `WriteOps` re-reading the real `audit.jsonl`
    at the instant before it mutates, plus the file's own record order.
    """
    run = await run_scripted_journey("scale-deployment-up", tmp_path)
    outcomes = [entry["outcome"] for entry in run.audit]
    assert outcomes == ["intent", "success"]
    assert run.audit[0]["name"] == "checkout-a"
    assert run.audit[0]["kind"] == "deployments"
    events = [entry["event"] for entry in run.journal]
    assert "audit_intent_missing" not in events
    assert events.index("audit_intent_observed") < events.index("mutation_started")
    assert events.index("mutation_started") < events.index("mutation_finished")
    observed = [entry for entry in run.journal if entry["event"] == "audit_intent_observed"]
    assert [entry["actor"] for entry in observed] == ["audit"]
    assert [entry["result"] for entry in observed] == ["durable"]
    # The success record is appended only after the injected WriteOps
    # returned, so the file's own order carries the second half of the
    # contract; the parsed records are journaled after the run.
    parsed = [entry["result"] for entry in run.journal if entry["event"] == "audit_record"]
    assert parsed == ["intent", "success"]


async def test_the_approval_comes_from_the_driver_keystroke_only(tmp_path: Path) -> None:
    run = await run_scripted_journey("scale-deployment-up", tmp_path)
    approvals = [entry for entry in run.journal if entry["event"] == "approval_observed"]
    assert [entry["actor"] for entry in approvals] == ["approval_driver"]
    assert [entry["approval"] for entry in approvals] == ["approved"]
    reported = [entry for entry in run.journal if entry["event"] == "approval_reported"]
    assert [entry["approval"] for entry in reported] == ["approved"]


async def test_the_dialog_shows_the_injected_write_ops_preview(tmp_path: Path) -> None:
    run = await run_scripted_journey("scale-deployment-up", tmp_path)
    previews = [entry for entry in run.journal if entry["event"].startswith("dialog_preview")]
    assert [entry["event"] for entry in previews] == ["dialog_preview_present"]


async def test_model_reads_and_app_internal_reads_are_attributed_separately(
    tmp_path: Path,
) -> None:
    run = await run_scripted_journey("scale-deployment-up", tmp_path)
    credited = [entry for entry in run.journal if entry["credit"]]
    assert {entry["actor"] for entry in credited} == {"model_tool"}
    assert {entry["event"] for entry in credited} == {"precondition_read", "postcondition_read"}
    resolutions = [entry for entry in run.journal if entry["event"] == "target_resolved"]
    assert resolutions != []
    assert {entry["actor"] for entry in resolutions} == {"app_internal"}
    assert all(entry["credit"] is False for entry in resolutions)


async def test_read_credit_comes_from_the_walked_path_not_a_leaf_substring(
    tmp_path: Path,
) -> None:
    """The fixture's `status.replicas` carries the same number as
    `spec.replicas`, so a leaf-substring rule would credit the wrong field.
    Credit is granted by `evaluate_assertion_document`, walking the whole
    asserted path over the parsed `get_resource` document."""
    run = await run_scripted_journey("scale-deployment-up", tmp_path)
    reads = [
        entry
        for entry in run.journal
        if entry["event"] in {"precondition_read", "postcondition_read", "read_without_state"}
    ]
    assert [entry["event"] for entry in reads] == ["precondition_read", "postcondition_read"]
    assert all(entry["action"] == "get_resource" for entry in reads)
    assert all(entry["result"] == "credited" for entry in reads)
    assert all(entry["target"]["uid"] == "deployment-checkout-a" for entry in reads)


async def test_a_read_that_is_not_a_target_document_earns_no_state_credit(
    tmp_path: Path,
) -> None:
    """The positive scripts read only the target with `get_resource`, so no
    read is skipped here; the ambiguity journey (Task 8) exercises the
    other side of this rule with its opening `list_resources`."""
    run = await run_scripted_journey("scale-deployment-up", tmp_path)
    assert [entry for entry in run.journal if entry["event"] == "off_target_read"] == []
    assert [entry for entry in run.journal if entry["event"] == "read_without_state"] == []


async def test_the_journal_artifact_carries_summaries_not_payloads(tmp_path: Path) -> None:
    """`run.journal` is published; it may name what happened, never
    reproduce a tool's arguments, its result, or the model's answer."""
    run = await run_scripted_journey("scale-deployment-up", tmp_path)
    details = [entry["detail"] for entry in run.journal if entry["detail"]]
    assert details != []
    assert all("{" not in detail and '"' not in detail for detail in details)
    calls = [entry for entry in run.journal if entry["event"] == "tool_call"]
    assert next(entry["detail"] for entry in calls).startswith("tool=get_resource")
    assert all("dropped=" in entry["detail"] for entry in calls)
    reported = [entry for entry in run.journal if entry["event"] == "outcome_reported"]
    assert [entry["result"] for entry in reported] == ["captured"]
    assert [entry["detail"] for entry in reported] == [f"chars={len(run.answer)}"]


async def test_the_restart_journey_stamps_the_pod_template(tmp_path: Path) -> None:
    run = await run_scripted_journey("restart-deployment", tmp_path)
    stamped = [
        result for result in run.grade.provisional_assertions if RESTART_ANNOTATION in result.path
    ]
    assert [result.satisfied for result in stamped] == [True]


async def test_the_scripted_journey_is_repeatable(tmp_path: Path) -> None:
    first = await run_scripted_journey("scale-deployment-up", tmp_path / "a")
    second = await run_scripted_journey("scale-deployment-up", tmp_path / "b")
    assert first.grade.checkpoints == second.grade.checkpoints
    assert first.grade.quality == second.grade.quality
    assert first.answer == second.answer


async def test_the_target_row_is_selected_by_its_namespace_slash_name_row_key(
    tmp_path: Path,
) -> None:
    """Row keys are `namespace/name` composites (`tests/ui/test_app.py::
    test_row_keys_are_namespace_slash_name`). The harness selects the
    fixture target through `query_one(ResourceTable)` and journals the key
    it matched, so a future change to row-key composition fails here
    instead of silently seeding the wrong screen context."""
    run = await run_scripted_journey("scale-deployment-up", tmp_path)
    selections = [entry for entry in run.journal if entry["event"] == "screen_target_selected"]
    assert [entry["actor"] for entry in selections] == ["fixture_actor"]
    assert [entry["detail"] for entry in selections] == ["row_key=shop-a/checkout-a"]
    assert [entry["result"] for entry in selections] == ["row_key"]


async def test_the_harness_writes_a_real_audit_file(tmp_path: Path) -> None:
    """The file is written by the shipped `AuditLog` itself: the harness
    only reads it back. `tests/evals/test_operation_bridge_parity.py` pins
    that no subclass or private sentinel import exists."""
    audit_path = tmp_path / "audit.jsonl"
    await run_operation_journey(
        _JOURNEYS["scale-deployment-up"],
        audit_path=audit_path,
        provider_factory=lambda: ScriptedProvider(OPERATION_SCRIPTS["scale-deployment-up"]),
    )
    lines = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    assert [line["action"] for line in lines] == ["scale", "scale"]
    assert [line["outcome"] for line in lines] == ["intent", "success"]
    assert {line["context"] for line in lines} == {"eval"}
    # 0600 is enforced by the production appender, not by the harness.
    assert audit_path.stat().st_mode & 0o777 == 0o600
