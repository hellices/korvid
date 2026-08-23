"""Ordering, actor attribution, and redaction rules of the action journal."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, cast

import pytest

from korvid.evals.operation_journal import (
    JOURNAL_ACTORS,
    JOURNAL_DETAIL_KEYS,
    JOURNAL_RESULTS,
    ActionJournal,
    JournalTarget,
    summarize,
    summarize_action,
    summarize_arguments,
    summarize_untrusted,
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


def test_event_state_cannot_be_mutated_after_append() -> None:
    journal = ActionJournal()
    event = journal.append(
        event="mutation_started",
        actor="write_ops",
        target=_TARGET,
        pre_state={"spec.replicas": 2},
    )
    mutable_view = cast(MutableMapping[str, Any], event.pre_state)
    with pytest.raises(TypeError, match="does not support item assignment"):
        mutable_view["spec.replicas"] = 99
    assert journal.payload()[0]["pre_state"] == {"spec.replicas": 2}


def test_an_unknown_actor_is_rejected() -> None:
    journal = ActionJournal()
    with pytest.raises(ValueError, match="unknown journal actor"):
        journal.append(event="goal_received", actor="model")


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


def test_state_mappings_reject_non_scalar_values() -> None:
    journal = ActionJournal()
    with pytest.raises(ValueError, match="journal state values must be scalars"):
        journal.append(
            event="mutation_finished",
            actor="write_ops",
            target=_TARGET,
            post_state={"spec.template": {"metadata": {}}},
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_state_mappings_reject_non_finite_values(value: float) -> None:
    journal = ActionJournal()
    with pytest.raises(ValueError, match="journal state values must be finite"):
        journal.append(
            event="mutation_finished",
            actor="write_ops",
            target=_TARGET,
            post_state={"spec.replicas": value},
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


def test_summarize_untrusted_keeps_bounded_fields_and_reports_zero_drops() -> None:
    assert (
        summarize_untrusted(
            tool="get_resource",
            checkpoint="precondition_read",
            count=1,
        )
        == "tool=get_resource checkpoint=precondition_read count=1 dropped=0"
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


def test_summarize_rejects_a_key_outside_the_allowlist() -> None:
    with pytest.raises(ValueError, match="journal detail key is not allowlisted"):
        summarize(prompt="scale checkout-a")


def test_summarize_rejects_empty_summary_values() -> None:
    with pytest.raises(ValueError, match="journal detail value is not a bounded summary token"):
        summarize(kind="")


def test_summarize_still_strips_quotes_for_trusted_fields() -> None:
    assert (
        summarize(kind='"deployments"', name='"checkout-a"') == "kind=deployments name=checkout-a"
    )


def test_summarize_untrusted_drops_hostile_and_reserved_fields_without_raising() -> None:
    detail = summarize_untrusted(
        kind='"deployments"',
        name="checkout-a",
        namespace='"shop-a"',
        count=1,
        tool="get_resource",
        dropped="7",
        note="whatever the model wanted to say",
        status=False,
        chars=float("inf"),
        resource={"uid": "x"},
    )
    assert detail == "name=checkout-a count=1 tool=get_resource dropped=7"
    ActionJournal().append(event="tool_call", actor="model_tool", detail=detail)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "scale resource",
        "mañana",
        "n" * 121,
        "secret/password!",
        'de"lete_resource',
        'sc"ale_resource',
    ],
)
def test_summarize_action_is_total_and_bounded(value: str) -> None:
    assert summarize_action(value) == "unknown_tool"


def test_summarize_action_preserves_a_bounded_token() -> None:
    assert summarize_action("scale_resource") == "scale_resource"


def test_summarize_arguments_keeps_only_allowlisted_keys_and_counts_the_rest() -> None:
    detail = summarize_arguments(
        "scale_resource",
        {
            "kind": "deployments",
            "name": "checkout-a",
            "namespace": "shop-a",
            "replicas": 3,
            "tool": "shadow",
            "note": "whatever the model wanted to say",
        },
    )
    assert detail == (
        "tool=scale_resource kind=redacted name=redacted namespace=redacted replicas=3 dropped=2"
    )
    ActionJournal().append(event="tool_call", actor="model_tool", detail=detail)


def test_summarize_arguments_redacts_untrusted_string_values() -> None:
    credential = "ghp_" + "a" * 36

    detail = summarize_arguments(
        "scale_resource",
        {
            "kind": "deployments",
            "name": credential,
            "namespace": "shop-a",
            "replicas": 3,
        },
    )

    assert credential not in detail
    assert detail == (
        "tool=scale_resource kind=redacted name=redacted namespace=redacted replicas=3 dropped=0"
    )


def test_summarize_arguments_drops_bool_and_empty_values() -> None:
    detail = summarize_arguments(
        "scale_resource",
        {
            "kind": "",
            "name": "checkout-a",
            "replicas": 3,
            "status": False,
        },
    )
    assert detail == "tool=scale_resource name=redacted replicas=3 dropped=2"


def test_summarize_arguments_counts_reserved_tool_and_dropped_keys() -> None:
    detail = summarize_arguments(
        "delete resource",
        {
            "kind": "deployments",
            "name": "checkout-a",
            "tool": "shadow",
            "dropped": "7",
            "note": "whatever the model wanted to say",
        },
    )
    assert detail == "kind=redacted name=redacted dropped=4"


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        (
            'de"lete_resource',
            {
                "kind": "deployments",
                "name": '"checkout-a"',
                "namespace": '"shop-a"',
            },
            "kind=redacted dropped=3",
        ),
        (
            'sc"ale_resource',
            {
                "kind": "deployments",
                "name": '"checkout-a"',
                "namespace": '"shop-a"',
                "replicas": 3,
            },
            "kind=redacted replicas=3 dropped=3",
        ),
    ],
)
def test_summarize_arguments_rejects_quoted_raw_tokens(
    tool: str, arguments: dict[str, object], expected: str
) -> None:
    detail = summarize_arguments(tool, arguments)
    assert detail == expected
    assert "tool=" not in detail
    ActionJournal().append(event="tool_call", actor="model_tool", detail=detail)


@pytest.mark.parametrize(
    "tool",
    [
        "",
        "scale resource",
        "mañana",
        "n" * 121,
    ],
)
def test_summarize_arguments_drops_invalid_tool_names(tool: str) -> None:
    detail = summarize_arguments(
        tool,
        {
            "kind": "deployments",
            "name": "checkout-a",
        },
    )
    assert detail == "kind=redacted name=redacted dropped=1"
    assert "tool=" not in detail
    ActionJournal().append(event="tool_call", actor="model_tool", detail=detail)


def test_append_rejects_a_non_bounded_action() -> None:
    journal = ActionJournal()
    with pytest.raises(ValueError, match="journal action must be a bounded summary token"):
        journal.append(event="tool_call", actor="model_tool", action="delete resource")


@pytest.mark.parametrize(
    "namespace",
    [
        "shop-a ",
        "shop-a(canary)",
        "n" * 121,
        "shop-a,shop-b",
    ],
)
def test_summarize_arguments_drops_invalid_namespace_tokens(namespace: str) -> None:
    detail = summarize_arguments(
        "delete_resource",
        {
            "kind": "deployments",
            "name": "checkout-a",
            "namespace": namespace,
        },
    )
    assert detail == "tool=delete_resource kind=redacted name=redacted dropped=1"
    assert namespace not in detail
    ActionJournal().append(event="tool_call", actor="model_tool", detail=detail)


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


def test_the_actor_vocabulary_is_pinned() -> None:
    assert JOURNAL_ACTORS == (
        "model_tool",
        "app_internal",
        "approval_driver",
        "fixture_actor",
        "audit",
        "write_ops",
        "grader",
    )


def test_only_a_model_tool_event_may_claim_read_credit() -> None:
    journal = ActionJournal()
    with pytest.raises(ValueError, match="only model_tool events may earn read credit"):
        journal.append(event="postcondition_read", actor="grader", credit=True)
