"""Tests for recorded workload creation failures and their classification (#411)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core.creation_failures import (
    MESSAGE_LIMIT,
    Failure,
    FailureSource,
    Rejection,
    RejectionKind,
    Subject,
    classify,
    condition_failure,
    event_failures,
)

DEPLOYMENT = Subject("Deployment", "api", "uid-api")
REPLICA_SET = Subject("ReplicaSet", "api-7d9f", "uid-rs")
EXCEEDED = (
    'Error creating: pods "api-7d9f-x2kq" is forbidden: exceeded quota: compute, '
    "requested: requests.cpu=500m, used: requests.cpu=2, limited: requests.cpu=2"
)


@pytest.mark.parametrize(
    ("message", "kinds", "quotas"),
    [
        (EXCEEDED, (RejectionKind.QUOTA_EXCEEDED,), ("compute",)),
        (
            'pods "x" is forbidden: failed quota: compute: must specify limits.cpu for: app',
            (RejectionKind.QUOTA_REQUIRES_FIELD,),
            ("compute",),
        ),
        (
            'pods "x" is forbidden: status unknown for quota: compute, resource: pods',
            (RejectionKind.QUOTA_STATUS_UNKNOWN,),
            ("compute",),
        ),
        (
            'pods "x" is forbidden: minimum memory usage per Container is 64Mi, but request is 32Mi',
            (RejectionKind.LIMIT_MINIMUM,),
            (),
        ),
        (
            'pods "x" is forbidden: maximum cpu usage per Pod is 2, but limit is 3',
            (RejectionKind.LIMIT_MAXIMUM,),
            (),
        ),
        (
            'persistentvolumeclaims "d" is forbidden: maximum storage usage per '
            "PersistentVolumeClaim is 10Gi, but request is 20Gi",
            (RejectionKind.LIMIT_MAXIMUM,),
            (),
        ),
        (
            'pods "x" is forbidden: cpu max limit to request ratio per Container is 2, '
            "but provided ratio is 4.000000",
            (RejectionKind.LIMIT_RATIO,),
            (),
        ),
        (
            'pods "x" is forbidden: [maximum cpu usage per Container is 1, but limit is 2, '
            "minimum memory usage per Container is 64Mi, but request is 32Mi]",
            (RejectionKind.LIMIT_MAXIMUM, RejectionKind.LIMIT_MINIMUM),
            (),
        ),
        (
            'admission webhook "policy.example.com" denied the request: no latest tags',
            (RejectionKind.OTHER,),
            (),
        ),
    ],
)
def test_a_message_is_classified_only_by_fixed_phrases(
    message: str, kinds: tuple[RejectionKind, ...], quotas: tuple[str, ...]
) -> None:
    rejection = classify(message)

    assert (rejection.kinds, rejection.quotas, rejection.message) == (kinds, quotas, message)


def test_a_long_message_is_bounded() -> None:
    rejection = classify("x" * 2000)

    assert len(rejection.message) == MESSAGE_LIMIT
    assert rejection.message.endswith("…")


def _event(
    regarding: dict[str, Any], *, reason: str = "FailedCreate", **fields: Any
) -> dict[str, Any]:
    return {
        "metadata": {"name": "e", "namespace": "shop"},
        "reason": reason,
        "regarding": regarding,
        **fields,
    }


def _ref(subject: Subject, uid: str | None = None) -> dict[str, str]:
    return {"kind": subject.kind, "name": subject.name, "uid": uid or subject.uid}


def test_events_about_the_workload_or_its_children_become_failures_newest_first() -> None:
    events = [
        _event(
            _ref(REPLICA_SET),
            note=EXCEEDED,
            series={"count": 12, "lastObservedTime": "2026-10-30T09:58:00Z"},
        ),
        _event(_ref(DEPLOYMENT), note="other", eventTime="2026-10-30T09:59:00Z"),
        _event(_ref(REPLICA_SET), reason="SuccessfulCreate", note="created"),
    ]

    failures, earlier = event_failures(events, (DEPLOYMENT, REPLICA_SET))

    assert earlier == 0
    assert failures == (
        Failure(
            DEPLOYMENT,
            FailureSource.EVENT,
            classify("other"),
            1,
            datetime(2026, 10, 30, 9, 59, tzinfo=UTC),
        ),
        Failure(
            REPLICA_SET,
            FailureSource.EVENT,
            classify(EXCEEDED),
            12,
            datetime(2026, 10, 30, 9, 58, tzinfo=UTC),
        ),
    )


def test_a_core_event_is_matched_by_its_involved_object() -> None:
    event = {
        "metadata": {"name": "e"},
        "reason": "FailedCreate",
        "involvedObject": _ref(REPLICA_SET),
        "message": EXCEEDED,
        "count": 3,
        "lastTimestamp": "2026-10-30T09:00:00Z",
    }

    (failure,), _ = event_failures([event], (REPLICA_SET,))

    assert (failure.count, failure.rejection.kinds) == (3, (RejectionKind.QUOTA_EXCEEDED,))


def test_a_legacy_events_k8s_io_event_keeps_its_deprecated_count_and_time() -> None:
    event = _event(
        _ref(REPLICA_SET),
        note=EXCEEDED,
        deprecatedCount=7,
        deprecatedLastTimestamp="2026-10-30T09:30:00Z",
        metadata={"name": "e", "creationTimestamp": "2026-10-30T08:00:00Z"},
    )

    (failure,), _ = event_failures([event], (REPLICA_SET,))

    assert (failure.count, failure.at) == (7, datetime(2026, 10, 30, 9, 30, tzinfo=UTC))


def test_an_earlier_incarnations_events_are_counted_not_shown() -> None:
    events = [
        _event(_ref(DEPLOYMENT, "uid-old"), note=EXCEEDED),
        _event(_ref(REPLICA_SET, "uid-old-rs"), note=EXCEEDED),
        _event({"kind": "Deployment", "name": "other", "uid": "x"}, note=EXCEEDED),
    ]

    failures, earlier = event_failures(events, (DEPLOYMENT, REPLICA_SET))

    assert (failures, earlier) == ((), 2)


def test_an_event_without_a_subject_uid_or_with_a_bad_count_is_handled() -> None:
    events = [
        _event({"kind": "Deployment", "name": "api"}, note=EXCEEDED),
        _event(_ref(DEPLOYMENT), note=EXCEEDED, count=True, series={"count": 0}),
        "not-an-event",
    ]

    failures, earlier = event_failures(events, (DEPLOYMENT,))

    assert [(f.count, f.at) for f in failures] == [(1, None)]
    assert earlier == 1


def test_a_replica_failure_condition_counts_only_for_failed_create() -> None:
    created = {
        "conditions": [
            {
                "type": "ReplicaFailure",
                "status": "True",
                "reason": "FailedCreate",
                "message": EXCEEDED,
                "lastTransitionTime": "2026-10-30T09:00:00Z",
                "lastUpdateTime": "2026-10-30T09:40:00Z",
            }
        ]
    }

    failure = condition_failure(DEPLOYMENT, created)

    assert failure == Failure(
        DEPLOYMENT,
        FailureSource.CONDITION,
        classify(EXCEEDED),
        1,
        datetime(2026, 10, 30, 9, 40, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "condition",
    [
        {"type": "ReplicaFailure", "status": "True", "reason": "FailedDelete", "message": "m"},
        {"type": "ReplicaFailure", "status": "False", "reason": "FailedCreate", "message": "m"},
        {"type": "Progressing", "status": "True", "reason": "FailedCreate", "message": "m"},
    ],
    ids=["failed-delete", "status-false", "other-type"],
)
def test_other_conditions_are_not_creation_failures(condition: dict[str, str]) -> None:
    assert condition_failure(DEPLOYMENT, {"conditions": [condition]}) is None


def test_a_condition_without_times_is_undated() -> None:
    status = {
        "conditions": [{"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate"}]
    }

    failure = condition_failure(REPLICA_SET, status)

    assert failure is not None
    assert (failure.at, failure.rejection) == (None, Rejection((RejectionKind.OTHER,), (), ""))


def test_a_boolean_count_is_not_a_count() -> None:
    event = _event(_ref(DEPLOYMENT), note=EXCEEDED, series={"count": True}, count=4)

    (failure,), _ = event_failures([event], (DEPLOYMENT,))

    assert failure.count == 4


def test_a_time_without_a_zone_is_not_used() -> None:
    event = _event(_ref(DEPLOYMENT), note=EXCEEDED, eventTime="2026-10-30T09:00:00")

    (failure,), _ = event_failures([event], (DEPLOYMENT,))

    assert failure.at is None
