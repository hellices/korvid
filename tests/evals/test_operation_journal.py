"""Ordering, actor attribution, and redaction rules of the action journal."""

from __future__ import annotations

import pytest

from korvid.evals.operation_journal import (
    JOURNAL_DETAIL_KEYS,
    JOURNAL_RESULTS,
    ActionJournal,
    JournalTarget,
    summarize,
)

_TARGET = JournalTarget(
    context="eval",
    namespace="shop-a",
    group="apps",
    kind="Deployment",
    plural="deployments",
    name="checkout-a",
    uid="deployment-checkout-a",
)


def test_events_are_numbered_in_append_order() -> None:
    journal = ActionJournal()
    journal.append(event="goal_received", actor="fixture_actor")
    journal.append(event="target_resolved", actor="app_internal", target=_TARGET)
    assert [event.sequence for event in journal.events] == [1, 2]
    assert [event.event for event in journal.events] == ["goal_received", "target_resolved"]


def test_the_journal_is_append_only() -> None:
    journal = ActionJournal()
    journal.append(event="goal_received", actor="fixture_actor")
    snapshot = journal.events
    journal.append(event="target_resolved", actor="app_internal", target=_TARGET)
    assert isinstance(snapshot, tuple)
    assert len(snapshot) == 1
    assert len(journal.events) == 2
    assert journal.events[0] == snapshot[0]


def test_checkpoints_report_only_lifecycle_events_in_order() -> None:
    journal = ActionJournal()
    journal.append(event="goal_received", actor="fixture_actor")
    journal.append(event="tool_call", actor="model_tool", action="get_resource")
    journal.append(event="precondition_read", actor="model_tool", credit=True)
    journal.append(event="dialog_observed", actor="approval_driver")
    assert journal.checkpoints() == ("goal_received", "precondition_read")
    assert journal.has("tool_call") is True
    assert journal.count("precondition_read") == 1


def test_state_mappings_reject_secret_payload_paths() -> None:
    journal = ActionJournal()
    with pytest.raises(ValueError, match="journal state must not carry secret payloads"):
        journal.append(
            event="mutation_finished",
            actor="write_ops",
            target=_TARGET,
            post_state={"data.password": "hunter2"},
        )


def test_a_secret_target_may_not_carry_state() -> None:
    journal = ActionJournal()
    secret = JournalTarget(
        context="eval",
        namespace="shop-a",
        group="",
        kind="Secret",
        plural="secrets",
        name="db",
        uid="secret-db",
    )
    with pytest.raises(ValueError, match="Secret state is never journaled"):
        journal.append(
            event="mutation_finished", actor="write_ops", target=secret, post_state={"type": "x"}
        )


def test_the_payload_is_json_ready_and_carries_every_field() -> None:
    journal = ActionJournal()
    journal.append(
        event="mutation_finished",
        actor="write_ops",
        action="scale",
        target=_TARGET,
        approval="approved",
        pre_state={"spec.replicas": 2},
        post_state={"spec.replicas": 3},
        result="success",
        detail=summarize(action="scale", replicas=3),
    )
    entry = journal.payload()[0]
    assert entry["sequence"] == 1
    assert entry["actor"] == "write_ops"
    assert entry["target"]["uid"] == "deployment-checkout-a"
    assert entry["pre_state"] == {"spec.replicas": 2}
    assert entry["post_state"] == {"spec.replicas": 3}
    assert entry["result"] == "success"
    assert entry["detail"] == "action=scale replicas=3"
    assert entry["credit"] is False


def test_a_raw_tool_result_may_not_be_journaled() -> None:
    """`run.journal` is published as a campaign artifact, so a `result`
    field is a status token from a closed vocabulary — never model or API
    prose that could carry a payload the masking pipeline removed."""

    journal = ActionJournal()
    with pytest.raises(ValueError, match="journal result must be an allowlisted status"):
        journal.append(
            event="approval_reported",
            actor="model_tool",
            result="ERROR: scale deployments.apps/checkout-a failed: conflict",
        )


def test_raw_tool_arguments_may_not_be_journaled() -> None:
    journal = ActionJournal()
    with pytest.raises(ValueError, match="journal detail must be an allowlisted"):
        journal.append(
            event="tool_call",
            actor="model_tool",
            action="get_resource",
            detail='{"kind": "deployments", "name": "checkout-a"}',
        )


def test_the_result_and_detail_vocabularies_are_pinned() -> None:
    assert "success" in JOURNAL_RESULTS
    assert "" in JOURNAL_RESULTS
    assert tuple(sorted(JOURNAL_RESULTS)) == JOURNAL_RESULTS
    assert set(JOURNAL_DETAIL_KEYS) >= {
        "action",
        "kind",
        "name",
        "namespace",
        "replicas",
        "status",
        "tool",
        "uid",
    }
    assert "arguments" not in JOURNAL_DETAIL_KEYS
    assert "answer" not in JOURNAL_DETAIL_KEYS


def test_only_a_model_tool_event_may_claim_read_credit() -> None:
    journal = ActionJournal()
    with pytest.raises(ValueError, match="only model_tool events may earn read credit"):
        journal.append(event="postcondition_read", actor="grader", credit=True)
