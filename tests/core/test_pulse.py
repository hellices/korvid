import json
from collections.abc import Sequence
from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from korvid.core.findings import Evidence, Finding, ResourceIdentity
from korvid.core.pulse import (
    PulseCoverage,
    PulseCoverageState,
    PulseItem,
    PulseModel,
    PulseRule,
    PulseSnapshot,
    PulseTarget,
)
from korvid.core.pulse_rules import DeploymentPulseRule, PodPulseRule

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


def _object(name: str = "sample", *, broken: bool = True) -> dict[str, Any]:
    return {
        "apiVersion": "example.io/v1",
        "kind": "Widget",
        "metadata": {"namespace": "production", "name": name, "uid": f"uid-{name}"},
        "status": {"broken": broken, "reason": "WidgetBroken", "message": "Current failure"},
    }


class _WidgetRule(PulseRule):
    @property
    def source(self) -> str:
        return "widgets"

    def evaluate(
        self, objects: Sequence[dict[str, Any]], observed_at: datetime
    ) -> tuple[PulseItem, ...]:
        items = []
        for resource in objects:
            if not resource["status"]["broken"]:
                continue
            metadata = resource["metadata"]
            target = PulseTarget(
                "example.io", "Widget", metadata["namespace"], metadata["name"], metadata.get("uid")
            )
            primary = ResourceIdentity(
                "Widget", metadata["namespace"], metadata["name"], metadata.get("uid", "")
            )
            message = resource["status"]["message"]
            finding = Finding(
                "widget.broken",
                "1",
                "warning",
                "high",
                primary,
                (),
                (Evidence(primary, "status.message", message),),
                message,
                ("Inspect Widget status",),
            )
            items.append(
                PulseItem(
                    f"widget:{metadata['name']}",
                    "current",
                    target,
                    resource["status"]["reason"],
                    message,
                    observed_at,
                    finding=finding,
                    source=self.source,
                )
            )
        return tuple(items)


def _event(
    uid: str = "event-one",
    *,
    occurred_at: datetime = NOW,
    count: int = 1,
    message: str = "A controller reports a problem",
    namespace: str = "production",
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Event",
        "type": "Warning",
        "metadata": {"uid": uid, "namespace": namespace, "name": uid},
        "involvedObject": {
            "apiVersion": "example.io/v1",
            "kind": "Widget",
            "namespace": namespace,
            "name": "sample",
            "uid": "uid-sample",
        },
        "reason": "UnfamiliarControllerReason",
        "message": message,
        "lastTimestamp": occurred_at.isoformat(),
        "count": count,
    }


def _coverage(snapshot: PulseSnapshot, source: str) -> PulseCoverage:
    return next(coverage for coverage in snapshot.coverage if coverage.source == source)


def _replace(
    model: PulseModel,
    objects: Sequence[dict[str, Any]],
    *,
    state: PulseCoverageState = "complete",
    observed_at: datetime = NOW,
) -> None:
    model.replace_source("widgets", objects, PulseCoverage("widgets", state, observed_at))


def test_reset_seeds_loading_coverage_without_retaining_mutable_state() -> None:
    model = PulseModel()
    model.reset(epoch=7, scope="production")
    snapshot = model.snapshot(datetime(2026, 9, 13, 12, tzinfo=UTC))

    assert snapshot.epoch == 7
    assert snapshot.scope == "production"
    assert snapshot.current == ()
    assert snapshot.recent == ()
    assert snapshot.dropped == 0
    assert [(coverage.source, coverage.state) for coverage in snapshot.coverage] == [
        ("events", "loading")
    ]


def test_arbitrary_rule_is_discovered_from_its_source() -> None:
    model = PulseModel([_WidgetRule()])
    model.reset(3, "production")
    assert _coverage(model.snapshot(NOW), "widgets").state == "loading"

    _replace(model, [_object()])

    (item,) = model.snapshot(NOW).current
    assert item.reason == "WidgetBroken"
    assert item.source == "widgets"
    assert item.finding is not None
    assert item.finding.rule_id == "widget.broken"


def test_successful_complete_observation_resolves_only_current_findings() -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])
    model.record_warning(_event(), 0, NOW)

    _replace(model, [_object(broken=False)], observed_at=NOW + timedelta(seconds=1))

    snapshot = model.snapshot(NOW + timedelta(seconds=1))
    assert snapshot.current == ()
    assert len(snapshot.recent) == 1
    assert _coverage(snapshot, "widgets").state == "complete"


@pytest.mark.parametrize("state", ["failed", "forbidden", "unavailable", "loading", "stale"])
def test_failed_or_untrusted_reads_do_not_declare_recovery(state: PulseCoverageState) -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])
    original = model.snapshot(NOW).current

    _replace(model, [], state=state, observed_at=NOW + timedelta(seconds=1))

    snapshot = model.snapshot(NOW + timedelta(seconds=1))
    assert snapshot.current == original
    assert _coverage(snapshot, "widgets").state == state


@pytest.mark.parametrize("state", ["partial", "capped"])
def test_incomplete_reads_resolve_observed_but_not_unobserved_identities(
    state: PulseCoverageState,
) -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object("observed"), _object("unobserved")])

    _replace(model, [_object("observed", broken=False), _object("new")], state=state)

    snapshot = model.snapshot(NOW)
    assert {item.target.name for item in snapshot.current} == {"unobserved", "new"}
    assert _coverage(snapshot, "widgets").state == state


def test_partial_read_recognizes_recreated_resource_without_kind_metadata() -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])
    recreated = _object(broken=False)
    recreated.pop("kind")
    recreated.pop("apiVersion")
    recreated["metadata"]["uid"] = "new-incarnation"

    _replace(model, [recreated], state="partial")

    assert model.snapshot(NOW).current == ()


@pytest.mark.parametrize("identity", [{}, {"uid": ""}, {"uid": None}, {"uid": False}])
@pytest.mark.parametrize("state", ["complete", "partial", "capped"])
def test_missing_uid_evidence_cannot_clear_verified_incarnation(
    identity: dict[str, Any], state: PulseCoverageState
) -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])
    original = model.snapshot(NOW).current
    healthy = _object(broken=False)
    healthy["metadata"].pop("uid")
    healthy["metadata"].update(identity)

    _replace(model, [healthy], state=state, observed_at=NOW + timedelta(seconds=1))

    snapshot = model.snapshot(NOW + timedelta(seconds=1))
    assert snapshot.current == original
    coverage = _coverage(snapshot, "widgets")
    assert coverage.state == ("capped" if state == "capped" else "partial")
    assert "Assessment incomplete" in coverage.detail


def test_missing_uid_still_admits_fresh_finding_without_clearing_evidence() -> None:
    model = PulseModel([_WidgetRule()])
    resource = _object()
    resource["metadata"].pop("uid")

    _replace(model, [resource])

    snapshot = model.snapshot(NOW)
    assert len(snapshot.current) == 1
    assert snapshot.current[0].reason == "WidgetBroken"
    assert snapshot.current[0].target.uid is None
    assert _coverage(snapshot, "widgets").state == "partial"


@pytest.mark.parametrize(
    "offset", [timedelta(microseconds=1), timedelta(seconds=1), timedelta(days=3650)]
)
def test_future_warning_cannot_replace_valid_recent_evidence(offset: timedelta) -> None:
    model = PulseModel(max_events=1)
    model.record_warning(_event("valid", occurred_at=NOW - timedelta(seconds=1)), 0, NOW)
    original = model.snapshot(NOW).recent
    future = _event("future", occurred_at=NOW + offset)

    model.record_warning(future, 0, NOW)

    snapshot = model.snapshot(NOW)
    assert snapshot.recent == original
    assert snapshot.dropped == 0
    coverage = _coverage(snapshot, "events")
    assert coverage.state == "partial"
    assert "future" in coverage.detail.lower()
    model.record_warning(future, 0, NOW)
    assert _coverage(model.snapshot(NOW), "events") == coverage


@pytest.mark.parametrize("offset", [timedelta(0), -timedelta(minutes=15)])
def test_recent_warning_window_includes_both_endpoints(offset: timedelta) -> None:
    model = PulseModel()

    model.record_warning(_event(occurred_at=NOW + offset), 0, NOW)

    assert len(model.snapshot(NOW).recent) == 1


@pytest.mark.parametrize("timestamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_utc_overflow_is_a_gap_without_aborting_following_warning(timestamp: str) -> None:
    model = PulseModel()
    invalid = _event("invalid")
    invalid["lastTimestamp"] = timestamp
    valid = _event("valid")
    valid["reason"] = "ValidAfterOverflow"

    model.replace_source(
        "events", [invalid, valid], PulseCoverage("events", "partial", NOW, "API 503: unavailable")
    )

    snapshot = model.snapshot(NOW)
    assert len(snapshot.recent) == 1
    assert snapshot.recent[0].reason == "ValidAfterOverflow"
    coverage = _coverage(snapshot, "events")
    assert coverage.state == "partial"
    assert "timestamp" in coverage.detail.lower()
    assert "API 503: unavailable" in coverage.detail


def test_older_success_cannot_replace_newer_findings_or_coverage() -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])

    _replace(model, [], observed_at=NOW - timedelta(seconds=1))

    snapshot = model.snapshot(NOW)
    assert len(snapshot.current) == 1
    assert _coverage(snapshot, "widgets").observed_at == NOW


def test_model_filters_current_objects_by_namespace() -> None:
    model = PulseModel([_WidgetRule()])
    model.reset(4, "production")
    foreign = _object("foreign")
    foreign["metadata"]["namespace"] = "development"

    _replace(model, [_object(), foreign])

    assert [item.target.name for item in model.snapshot(NOW).current] == ["sample"]


def test_warning_updates_deduplicate_by_uid_and_keep_cumulative_count() -> None:
    model = PulseModel()
    event = _event(count=3)
    model.record_warning(event, 0, NOW)
    model.record_warning(event, 0, NOW)
    updated = _event(count=8, occurred_at=NOW + timedelta(seconds=1), message="Updated evidence")
    model.record_warning(updated, 0, NOW + timedelta(seconds=2))

    (item,) = model.snapshot(NOW + timedelta(seconds=2)).recent
    assert item.count == 8
    assert item.message == "Updated evidence"
    assert item.observed_at == NOW + timedelta(seconds=1)
    assert item.category == "recent"
    assert item.finding is None


@pytest.mark.parametrize(("age", "count"), [(-1, 9), (-1, 10), (-1, 11), (0, 9), (1, 9)])
def test_older_time_or_lower_count_never_replaces_newer_evidence(age: int, count: int) -> None:
    model = PulseModel()
    model.record_warning(_event(count=10, message="Latest"), 0, NOW)
    model.record_warning(
        _event(count=count, occurred_at=NOW + timedelta(seconds=age), message="Out of order"),
        0,
        NOW + timedelta(seconds=2),
    )

    (item,) = model.snapshot(NOW + timedelta(seconds=2)).recent
    assert item.message == "Latest"
    assert item.count == 10
    assert item.observed_at == NOW


def test_new_event_api_uses_regarding_note_and_series_count() -> None:
    model = PulseModel()
    event = _event()
    event["apiVersion"] = "events.k8s.io/v1"
    event["regarding"] = event.pop("involvedObject")
    event["regarding"].pop("uid")
    event["note"] = event.pop("message")
    event["series"] = {"lastObservedTime": (NOW + timedelta(seconds=2)).isoformat(), "count": 12}
    event["eventTime"] = (NOW - timedelta(hours=1)).isoformat()
    model.record_warning(event, 0, NOW + timedelta(seconds=3))

    (item,) = model.snapshot(NOW + timedelta(seconds=3)).recent
    assert item.target == PulseTarget("example.io", "Widget", "production", "sample", None)
    assert item.reason == "UnfamiliarControllerReason"
    assert item.count == 12
    assert item.observed_at == NOW + timedelta(seconds=2)


def test_event_expiry_uses_event_time_and_does_not_resolve_current_findings() -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])
    model.record_warning(_event(occurred_at=NOW - timedelta(minutes=14)), 0, NOW)
    assert len(model.snapshot(NOW).recent) == 1

    snapshot = model.snapshot(NOW + timedelta(minutes=2))

    assert snapshot.recent == ()
    assert len(snapshot.current) == 1
    assert snapshot.dropped == 0
    assert _coverage(snapshot, "widgets").state == "stale"


def test_old_warning_received_now_is_not_fresh() -> None:
    model = PulseModel()
    model.record_warning(_event(occurred_at=NOW - timedelta(minutes=16)), 0, NOW)

    assert model.snapshot(NOW).recent == ()


@pytest.mark.parametrize("timestamp", [None, "not-a-time", "2026-09-13T12:00:00"])
def test_missing_or_invalid_event_time_surfaces_incomplete_evidence(timestamp: str | None) -> None:
    model = PulseModel()
    event = _event()
    event["lastTimestamp"] = timestamp
    model.replace_source("events", [event], PulseCoverage("events", "complete", NOW))

    snapshot = model.snapshot(NOW)
    assert snapshot.recent == ()
    assert _coverage(snapshot, "events").state == "partial"
    assert "timestamp" in _coverage(snapshot, "events").detail.lower()


def test_warning_scope_and_epoch_filter_precede_retention() -> None:
    model = PulseModel()
    model.reset(2, "production")
    model.record_warning(_event("wrong-epoch"), 1, NOW)
    model.record_warning(_event("wrong-namespace", namespace="development"), 2, NOW)
    normal = _event("normal")
    normal["type"] = "Normal"
    model.record_warning(normal, 2, NOW)
    model.record_warning(_event("accepted"), 2, NOW)

    assert len(model.snapshot(NOW).recent) == 1


def test_complete_event_reads_do_not_erase_retained_warning_history() -> None:
    model = PulseModel()
    model.record_warning(_event(), 0, NOW)

    model.replace_source("events", [], PulseCoverage("events", "complete", NOW))

    assert len(model.snapshot(NOW).recent) == 1


def test_event_count_bound_evicts_oldest_event_time_and_reports_loss() -> None:
    model = PulseModel(max_events=2)
    for name, age in (("newest", 0), ("oldest", -2), ("middle", -1)):
        model.record_warning(
            _event(name, occurred_at=NOW + timedelta(seconds=age), message=name), 0, NOW
        )

    snapshot = model.snapshot(NOW)
    assert [item.message for item in snapshot.recent] == ["newest", "middle"]
    assert snapshot.dropped == 1
    assert _coverage(snapshot, "event-buffer").state == "capped"


def test_event_byte_bound_counts_utf8_and_reports_refused_items() -> None:
    model = PulseModel(max_bytes=1000)
    for number in range(3):
        model.record_warning(_event(f"event-{number}", message="문" * 150), 0, NOW)

    snapshot = model.snapshot(NOW)
    retained_bytes = sum(
        len(json.dumps(asdict(item), default=str, ensure_ascii=False).encode())
        for item in snapshot.recent
    )
    assert retained_bytes <= 1000
    assert len(snapshot.recent) <= 1
    assert snapshot.dropped >= 2
    assert _coverage(snapshot, "event-buffer").state == "capped"


def test_single_oversized_warning_is_refused_with_explicit_loss() -> None:
    model = PulseModel(max_bytes=10)
    model.record_warning(_event(), 0, NOW)

    snapshot = model.snapshot(NOW)
    assert snapshot.recent == ()
    assert snapshot.dropped == 1
    assert _coverage(snapshot, "event-buffer").state == "capped"


def test_warning_string_clipping_is_explicit_and_follows_redaction() -> None:
    model = PulseModel()
    model.record_warning(
        _event(message='password="' + "sensitive" * 1000 + '" ' + "x" * 2000), 0, NOW
    )

    snapshot = model.snapshot(NOW)
    (item,) = snapshot.recent
    assert "sensitive" not in item.message
    assert len(item.message) <= 1024
    assert item.message.endswith("…")
    assert _coverage(snapshot, "event-buffer").state == "capped"


def test_redaction_covers_identity_reason_evidence_and_coverage_before_storage() -> None:
    model = PulseModel([_WidgetRule()])
    resource = _object()
    resource["metadata"]["name"] = "token=identity-secret\x1b\n"
    resource["metadata"]["uid"] = "password=uid-secret\x00"
    resource["status"]["reason"] = "api\x07_key=reason-secret"
    resource["status"]["message"] = "authorization: Bearer evidence-secret\r\n"
    _replace(model, [resource])
    event = _event(message="token=event-secret\x1b")
    event["involvedObject"]["name"] = "token=target-secret\x7f"
    model.record_warning(event, 0, NOW)
    model.set_coverage(
        PulseCoverage("warning-watch", "failed", NOW, "password=coverage-secret\x00")
    )

    rendered = repr(model.snapshot(NOW)) + repr(vars(model))
    for secret in (
        "identity-secret",
        "uid-secret",
        "reason-secret",
        "evidence-secret",
        "event-secret",
        "target-secret",
        "coverage-secret",
    ):
        assert secret not in rendered
    for control in ("\\x1b", "\\x00", "\\x07", "\\x7f", "\\n", "\\r"):
        assert control not in rendered


def test_raw_input_mutation_cannot_change_existing_snapshots() -> None:
    model = PulseModel([_WidgetRule()])
    resource = _object()
    event = _event()
    _replace(model, [resource])
    model.record_warning(event, 0, NOW)
    original = model.snapshot(NOW)

    resource["status"]["message"] = "Changed after collection"
    resource["metadata"]["name"] = "Changed identity"
    event["message"] = "Changed event"
    event["involvedObject"]["name"] = "Changed target"

    assert model.snapshot(NOW) == original
    attribute = "message"
    with pytest.raises(FrozenInstanceError, match="cannot assign"):
        setattr(original.recent[0], attribute, "mutated")
    assert not hasattr(original.recent[0], "__dict__")


def test_reset_clears_findings_history_coverage_and_loss_counters() -> None:
    model = PulseModel([_WidgetRule()], max_events=1)
    _replace(model, [_object()])
    model.record_warning(_event("one"), 0, NOW)
    model.record_warning(_event("two"), 0, NOW)
    model.set_coverage(PulseCoverage("warning-watch", "failed", NOW))
    assert model.snapshot(NOW).dropped == 1

    model.reset(9, "development")
    model.record_warning(_event(), 0, NOW)

    snapshot = model.snapshot(NOW)
    assert snapshot.epoch == 9
    assert snapshot.scope == "development"
    assert snapshot.current == snapshot.recent == ()
    assert snapshot.dropped == 0
    assert {coverage.source for coverage in snapshot.coverage} == {"widgets", "events"}
    assert all(coverage.state == "loading" for coverage in snapshot.coverage)


def test_successful_coverage_goes_stale_after_thirty_seconds() -> None:
    model = PulseModel()
    model.set_coverage(PulseCoverage("warning-watch", "complete", NOW))

    assert (
        _coverage(model.snapshot(NOW + timedelta(seconds=30)), "warning-watch").state == "complete"
    )
    assert _coverage(model.snapshot(NOW + timedelta(seconds=31)), "warning-watch").state == "stale"


@pytest.mark.parametrize(
    ("name", "value"),
    [("max_events", 0), ("max_events", True), ("max_bytes", -1), ("max_bytes", False)],
)
def test_invalid_buffer_bounds_are_rejected(name: str, value: int) -> None:
    limits = {"max_events": 100, "max_bytes": 131072}
    limits[name] = value
    with pytest.raises(ValueError, match=name):
        PulseModel(max_events=limits["max_events"], max_bytes=limits["max_bytes"])


def test_nonpositive_retention_is_rejected() -> None:
    with pytest.raises(ValueError, match="retention"):
        PulseModel(retention=timedelta(0))


def test_public_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        PulseCoverage("events", "complete", datetime(2026, 9, 13))
    with pytest.raises(ValueError, match="timezone-aware"):
        PulseModel().snapshot(datetime(2026, 9, 13))


def _pod_items(status: dict[str, Any]) -> tuple[PulseItem, ...]:
    resource = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"namespace": "production", "name": "api", "uid": "pod-uid"},
        "status": status,
    }
    return PodPulseRule().evaluate([resource], NOW)


def _deployment_items(
    conditions: list[dict[str, Any]],
    *,
    generation: int = 3,
    observed_generation: int | None = 3,
    replicas: int = 1,
) -> tuple[PulseItem, ...]:
    resource = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "namespace": "production",
            "name": "api",
            "uid": "deploy-uid",
            "generation": generation,
        },
        "spec": {"replicas": replicas},
        "status": {"observedGeneration": observed_generation, "conditions": conditions},
    }
    return DeploymentPulseRule().evaluate([resource], NOW)


def test_failed_pod_is_an_observation_not_a_root_cause_diagnosis() -> None:
    (item,) = _pod_items({"phase": "Failed", "reason": "Evicted", "message": "Reported by kubelet"})

    assert item.reason == "Evicted"
    assert item.message == "Reported by kubelet"
    assert item.finding is not None
    assert item.finding.rule_id == "pulse.pod.failed"
    assert Evidence(item.finding.primary, "status.phase", "Failed") in item.finding.evidence
    assert item.target.group == ""


def test_pending_unschedulable_pod_reports_current_scheduling_condition() -> None:
    (item,) = _pod_items(
        {
            "phase": "Pending",
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": "No matching nodes",
                }
            ],
        }
    )

    assert item.reason == "Unschedulable"
    assert item.finding is not None
    assert item.finding.rule_id == "pulse.pod.unschedulable"


def test_pending_pod_without_normal_initialization_remains_visible() -> None:
    items = _pod_items({"phase": "Pending"})

    assert [item.reason for item in items] == ["Pending"]


@pytest.mark.parametrize("status", ["False", "Unknown"])
def test_explicit_non_ready_active_pod_is_current(status: str) -> None:
    items = _pod_items(
        {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": status}],
        }
    )

    assert [item.reason for item in items] == ["NotReady"]


@pytest.mark.parametrize(
    "reason", ["CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "NovelRuntimeFailure"]
)
@pytest.mark.parametrize(
    "field", ["containerStatuses", "initContainerStatuses", "ephemeralContainerStatuses"]
)
def test_current_waiting_failures_include_unfamiliar_reasons(reason: str, field: str) -> None:
    (item,) = _pod_items(
        {
            "phase": "Running",
            field: [
                {
                    "name": "api",
                    "state": {"waiting": {"reason": reason, "message": "Observed failure"}},
                }
            ],
        }
    )

    assert item.reason == reason
    assert item.finding is not None
    assert item.finding.rule_id == "pulse.pod.container-waiting"


@pytest.mark.parametrize("reason", ["ContainerCreating", "PodInitializing"])
@pytest.mark.parametrize("phase", ["Pending", "Running"])
def test_normal_initialization_is_not_a_failure(reason: str, phase: str) -> None:
    items = _pod_items(
        {
            "phase": phase,
            "conditions": [{"type": "Ready", "status": "False"}],
            "containerStatuses": [{"name": "api", "state": {"waiting": {"reason": reason}}}],
        }
    )

    assert items == ()


@pytest.mark.parametrize("phase", ["Running", "Succeeded"])
def test_restart_counts_and_last_state_alone_are_not_current_failures(phase: str) -> None:
    items = _pod_items(
        {
            "phase": phase,
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [
                {
                    "name": "api",
                    "state": {"running": {}},
                    "restartCount": 99,
                    "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
                }
            ],
        }
    )

    assert items == ()


def test_succeeded_pod_is_excluded_even_if_a_container_status_looks_bad() -> None:
    assert (
        _pod_items(
            {
                "phase": "Succeeded",
                "containerStatuses": [
                    {"name": "api", "state": {"terminated": {"reason": "Error", "exitCode": 1}}}
                ],
            }
        )
        == ()
    )


@pytest.mark.parametrize(
    "termination",
    [
        {"exitCode": 1, "reason": "Error"},
        {"exitCode": 137, "reason": "OOMKilled"},
        {"exitCode": 0, "reason": "OOMKilled"},
        {"reason": "NovelTerminationFailure"},
    ],
)
def test_current_termination_failure_is_reported(termination: dict[str, Any]) -> None:
    (item,) = _pod_items(
        {
            "phase": "Running",
            "containerStatuses": [{"name": "api", "state": {"terminated": termination}}],
        }
    )

    assert item.finding is not None
    assert item.finding.rule_id == "pulse.pod.container-terminated"
    assert item.reason == termination["reason"]


def test_successful_termination_is_not_a_failure() -> None:
    assert (
        _pod_items(
            {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "init",
                        "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
                    }
                ],
            }
        )
        == ()
    )


def test_container_finding_keys_are_stable_across_status_order() -> None:
    containers = [
        {"name": name, "state": {"waiting": {"reason": "NovelFailure"}}}
        for name in ("first", "second")
    ]
    original = _pod_items({"phase": "Running", "containerStatuses": containers})
    reversed_items = _pod_items(
        {"phase": "Running", "containerStatuses": list(reversed(containers))}
    )

    assert len({item.key for item in original}) == 2
    assert {item.key for item in original} == {item.key for item in reversed_items}


@pytest.mark.parametrize(
    "condition",
    [
        {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate"},
        {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"},
    ],
)
def test_explicit_deployment_failure_needs_no_pod_evidence(condition: dict[str, Any]) -> None:
    (item,) = _deployment_items([condition])

    assert item.reason == condition["reason"]
    assert item.finding is not None
    assert item.finding.primary.kind == "Deployment"
    assert item.target.group == "apps"


@pytest.mark.parametrize("observed_generation", [None, 0, 2])
def test_stale_deployment_conditions_are_not_current_failures(
    observed_generation: int | None,
) -> None:
    assert (
        _deployment_items(
            [{"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate"}],
            observed_generation=observed_generation,
        )
        == ()
    )


@pytest.mark.parametrize("replicas", [0, 3])
def test_normal_rollout_and_scale_to_zero_do_not_create_failures(replicas: int) -> None:
    assert (
        _deployment_items(
            [
                {"type": "Progressing", "status": "True", "reason": "NewReplicaSetCreated"},
                {"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"},
                {"type": "ReplicaFailure", "status": "False"},
            ],
            replicas=replicas,
        )
        == ()
    )


def test_rules_redact_current_reason_message_and_evidence() -> None:
    (item,) = _pod_items(
        {
            "phase": "Failed",
            "reason": "token=reason-secret\x00",
            "message": "authorization: Bearer failure-secret\x1b",
        }
    )

    assert "reason-secret" not in repr(item)
    assert "failure-secret" not in repr(item)
    assert "\\x00" not in repr(item)
    assert "\\x1b" not in repr(item)


def test_missing_source_observation_time_cannot_claim_complete_coverage() -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])

    model.replace_source("widgets", [], PulseCoverage("widgets", "complete", None))

    snapshot = model.snapshot(NOW)
    assert len(snapshot.current) == 1
    assert _coverage(snapshot, "widgets").state == "partial"
    assert "timestamp" in _coverage(snapshot, "widgets").detail.lower()


def test_control_stripped_or_clipped_identity_cannot_be_used_for_navigation() -> None:
    unsafe = PulseTarget("example.io", "Widget", "production", "sample\x1b", "uid-sample")
    clipped = PulseTarget("example.io", "Widget", "production", "sample", "x" * 200)

    assert unsafe.uid is None
    assert clipped.uid is None


def test_rule_handles_malformed_readiness_evidence_without_guessing() -> None:
    assert (
        _pod_items(
            {
                "phase": "Running",
                "conditions": [None, {"type": "Ready", "status": {"unexpected": "shape"}}],
                "containerStatuses": None,
            }
        )
        == ()
    )


def test_rule_handles_malformed_waiting_reason_as_unspecified_waiting() -> None:
    items = _pod_items(
        {
            "phase": "Running",
            "containerStatuses": [
                None,
                {"name": "api", "state": {"waiting": {"reason": ["invalid"]}}},
            ],
        }
    )

    assert [item.reason for item in items] == ["ContainerWaiting"]


def test_deployment_rule_handles_malformed_condition_values() -> None:
    assert (
        _deployment_items(
            [
                {"type": ["ReplicaFailure"], "status": "True"},
                {"type": "Progressing", "status": {"value": "False"}},
            ]
        )
        == ()
    )


def test_failed_coverage_does_not_refresh_age_of_retained_findings() -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])
    _replace(model, [], state="failed", observed_at=NOW + timedelta(seconds=40))

    snapshot = model.snapshot(NOW + timedelta(seconds=41))
    assert len(snapshot.current) == 1
    assert _coverage(snapshot, "widgets").state == "stale"
    assert "failed" in _coverage(snapshot, "widgets").detail


def test_loading_retry_does_not_hide_staleness_of_retained_findings() -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])
    model.set_coverage(PulseCoverage("widgets", "loading", None))

    snapshot = model.snapshot(NOW + timedelta(seconds=31))
    assert len(snapshot.current) == 1
    assert _coverage(snapshot, "widgets").state == "stale"


def test_never_observed_source_becomes_stale_from_first_supplied_clock() -> None:
    model = PulseModel()
    assert _coverage(model.snapshot(NOW), "events").state == "loading"

    assert _coverage(model.snapshot(NOW + timedelta(seconds=31)), "events").state == "stale"


def test_refused_newer_update_still_prevents_a_backwards_update() -> None:
    model = PulseModel(max_bytes=800)
    model.record_warning(_event(count=1, message="Initial evidence"), 0, NOW)
    model.record_warning(
        _event(count=10, occurred_at=NOW + timedelta(seconds=2), message="x" * 1024),
        0,
        NOW + timedelta(seconds=2),
    )
    assert model.snapshot(NOW + timedelta(seconds=2)).dropped == 1
    model.record_warning(
        _event(count=5, occurred_at=NOW + timedelta(seconds=1), message="Late older update"),
        0,
        NOW + timedelta(seconds=3),
    )

    (item,) = model.snapshot(NOW + timedelta(seconds=3)).recent
    assert item.message == "Initial evidence"
    assert item.count == 1


@pytest.mark.parametrize("field", ["kind", "name"])
def test_missing_target_identity_cannot_be_navigated_even_with_uid(field: str) -> None:
    target = {
        "group": "example.io",
        "kind": "Widget",
        "namespace": "production",
        "name": "sample",
        "uid": "uid-sample",
    }
    target[field] = ""

    assert PulseTarget(**target).uid is None


@pytest.mark.parametrize(
    "field", ["lastTimestamp", "eventTime", "firstTimestamp", "creationTimestamp"]
)
def test_event_time_fallbacks_are_shared_with_the_existing_selector(field: str) -> None:
    model = PulseModel()
    event = _event()
    event.pop("lastTimestamp")
    if field == "creationTimestamp":
        event["metadata"][field] = NOW.isoformat()
    else:
        event[field] = NOW.isoformat()
    event["series"] = None
    model.record_warning(event, 0, NOW)

    assert model.snapshot(NOW).recent[0].observed_at == NOW


def test_event_uid_deduplication_does_not_collapse_long_distinct_identities() -> None:
    model = PulseModel()
    model.record_warning(_event("prefix" * 100 + "one"), 0, NOW)
    model.record_warning(_event("prefix" * 100 + "two"), 0, NOW)

    assert len(model.snapshot(NOW).recent) == 2


def test_finding_projection_detaches_mutable_container_inputs() -> None:
    primary = ResourceIdentity("Pod", "production", "api", "api-uid")
    related: Any = [primary]
    evidence: Any = [Evidence(primary, "status.phase", "Failed")]
    next_checks: Any = ["Inspect Pod status"]
    finding = Finding(
        "example.failed", "1", "warning", "high", primary, related, evidence, "Failed", next_checks
    )
    item = PulseItem(
        "failed",
        "current",
        PulseTarget("", "Pod", "production", "api", "api-uid"),
        "Failed",
        "Failed",
        NOW,
        finding=finding,
    )

    related.clear()
    evidence.clear()
    next_checks.clear()

    assert item.finding is not None
    assert item.finding.related == (primary,)
    assert item.finding.evidence == (Evidence(primary, "status.phase", "Failed"),)
    assert item.finding.next_checks == ("Inspect Pod status",)


def _pod_observation(name: str = "api", *, ready: object = "False") -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"namespace": "production", "name": name, "uid": f"uid-{name}"},
        "spec": {"containers": [{"name": "api"}]},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": ready}],
            "containerStatuses": [{"name": "api", "ready": False, "state": {"running": {}}}],
        },
    }


@pytest.mark.parametrize("state", ["partial", "capped"])
def test_current_retention_is_bounded_under_incomplete_identity_churn(
    state: PulseCoverageState,
) -> None:
    model = PulseModel([_WidgetRule()])
    for number in range(256):
        _replace(
            model,
            [_object(f"sample-{number:03}")],
            state=state,
            observed_at=NOW + timedelta(seconds=number * 30),
        )

    snapshot = model.snapshot(NOW + timedelta(seconds=255 * 30))
    assert len(snapshot.current) <= 200
    assert snapshot.current_dropped >= 56
    assert snapshot.dropped == 0
    coverage = _coverage(snapshot, "current-buffer:widgets")
    assert coverage.state == "capped"
    assert "retention" in coverage.detail.lower()
    assert "recovery" in coverage.detail.lower()


@pytest.mark.parametrize("state", ["complete", "partial", "capped"])
def test_stale_observed_identity_cannot_resolve_current_finding(state: PulseCoverageState) -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])
    original = model.snapshot(NOW + timedelta(seconds=60)).current

    _replace(model, [_object(broken=False)], state=state, observed_at=NOW + timedelta(seconds=10))

    snapshot = model.snapshot(NOW + timedelta(seconds=60))
    assert snapshot.current == original
    assert _coverage(snapshot, "widgets").state == "stale"


def test_stale_complete_empty_snapshot_is_not_recovery() -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])
    original = model.snapshot(NOW + timedelta(seconds=60)).current

    _replace(model, [], observed_at=NOW + timedelta(seconds=10))

    snapshot = model.snapshot(NOW + timedelta(seconds=60))
    assert snapshot.current == original
    assert _coverage(snapshot, "widgets").state == "stale"


@pytest.mark.parametrize("state", ["complete", "partial", "capped"])
@pytest.mark.parametrize("ready", [{"unexpected": "shape"}, ["False"], None])
def test_unassessable_readiness_never_resolves_current_finding(
    state: PulseCoverageState,
    ready: object,
) -> None:
    model = PulseModel([PodPulseRule()])
    model.replace_source("pods", [_pod_observation()], PulseCoverage("pods", "complete", NOW))
    original = model.snapshot(NOW).current

    model.replace_source(
        "pods",
        [_pod_observation(ready=ready)],
        PulseCoverage("pods", state, NOW + timedelta(seconds=1)),
    )

    snapshot = model.snapshot(NOW + timedelta(seconds=1))
    assert snapshot.current == original
    coverage = _coverage(snapshot, "pods")
    assert coverage.state == ("capped" if state == "capped" else "partial")
    assert "assess" in coverage.detail.lower()


@pytest.mark.parametrize("malformed", [{}, {"metadata": None}, {"status": {"phase": "Running"}}])
def test_complete_list_with_unidentified_malformed_input_does_not_imply_absence(
    malformed: dict[str, Any],
) -> None:
    model = PulseModel([PodPulseRule()])
    model.reset(1, "production")
    model.replace_source("pods", [_pod_observation()], PulseCoverage("pods", "complete", NOW))
    original = model.snapshot(NOW).current

    model.replace_source(
        "pods",
        [malformed],
        PulseCoverage("pods", "complete", NOW + timedelta(seconds=1)),
    )

    snapshot = model.snapshot(NOW + timedelta(seconds=1))
    assert snapshot.current == original
    assert _coverage(snapshot, "pods").state == "partial"


@pytest.mark.parametrize("reason", ["ContainerCreating", "PodInitializing"])
def test_ephemeral_startup_does_not_suppress_application_not_ready(reason: str) -> None:
    pod = _pod_observation()
    baseline = PodPulseRule().evaluate([pod], NOW)
    pod["status"]["ephemeralContainerStatuses"] = [
        {"name": "debugger", "state": {"waiting": {"reason": reason}}}
    ]

    items = PodPulseRule().evaluate([pod], NOW)

    assert [item.reason for item in baseline] == ["NotReady"]
    assert items == baseline


@pytest.mark.parametrize("state", ["failed", "forbidden", "unavailable", "loading"])
def test_repeated_failures_before_first_success_do_not_refresh_staleness(
    state: PulseCoverageState,
) -> None:
    model = PulseModel([_WidgetRule()])
    model.snapshot(NOW)
    for seconds in (0, 10, 20, 30, 40):
        model.set_coverage(
            PulseCoverage("widgets", state, NOW + timedelta(seconds=seconds), "Connection refused")
        )

    coverage = _coverage(model.snapshot(NOW + timedelta(seconds=41)), "widgets")
    assert coverage.state == "stale"
    assert state in coverage.detail
    assert "Connection refused" in coverage.detail


def test_first_failed_observation_anchors_clock_before_any_snapshot() -> None:
    model = PulseModel()
    model.set_coverage(PulseCoverage("events", "failed", NOW, "Original failure"))
    model.set_coverage(
        PulseCoverage("events", "failed", NOW + timedelta(seconds=40), "Retry failed")
    )

    coverage = _coverage(model.snapshot(NOW + timedelta(seconds=41)), "events")
    assert coverage.state == "stale"
    assert "Retry failed" in coverage.detail


@pytest.mark.parametrize(
    ("kind", "api_version"),
    [
        ("Node", "v1"),
        ("CustomResourceDefinition", "apiextensions.k8s.io/v1"),
    ],
)
@pytest.mark.parametrize("reference_namespace", [None, ""])
@pytest.mark.parametrize("reference_field", ["involvedObject", "regarding"])
@pytest.mark.parametrize("via_list", [False, True])
def test_namespaced_event_preserves_cluster_target_identity(
    kind: str,
    api_version: str,
    reference_namespace: str | None,
    reference_field: str,
    via_list: bool,
) -> None:
    model = PulseModel()
    model.reset(5, "default")
    event = _event(namespace="default")
    event.pop("involvedObject")
    reference: dict[str, Any] = {
        "apiVersion": api_version,
        "kind": kind,
        "name": "cluster-resource",
        "uid": "exact-target-uid",
    }
    if reference_namespace is not None:
        reference["namespace"] = reference_namespace
    event[reference_field] = reference

    if via_list:
        model.replace_source("events", [event], PulseCoverage("events", "complete", NOW))
    else:
        model.record_warning(event, 5, NOW)

    snapshot = model.snapshot(NOW)
    assert len(snapshot.recent) == 1
    assert snapshot.recent[0].target.namespace == ""
    assert snapshot.recent[0].target.uid == "exact-target-uid"
    assert snapshot.recent[0].target.kind == kind
    assert {coverage.source for coverage in snapshot.coverage} == {"events"}


def test_event_filter_uses_storage_namespace_not_referenced_namespace() -> None:
    model = PulseModel()
    model.reset(2, "production")
    event = _event(namespace="production")
    event["metadata"]["namespace"] = "development"

    model.record_warning(event, 2, NOW)

    assert model.snapshot(NOW).recent == ()


def test_missing_event_namespace_falls_back_to_explicit_reference_scope() -> None:
    model = PulseModel()
    model.reset(2, "production")
    event = _event()
    event["metadata"].pop("namespace")

    model.record_warning(event, 2, NOW)

    assert len(model.snapshot(NOW).recent) == 1


def test_explicit_empty_event_namespace_does_not_borrow_reference_scope() -> None:
    model = PulseModel()
    model.reset(2, "production")
    event = _event()
    event["metadata"]["namespace"] = ""

    model.record_warning(event, 2, NOW)

    assert model.snapshot(NOW).recent == ()


def _serialized_item_bytes(item: PulseItem) -> int:
    return len(json.dumps(asdict(item), ensure_ascii=False, default=str).encode("utf-8"))


def test_default_current_byte_limit_counts_serialized_utf8_findings() -> None:
    model = PulseModel([_WidgetRule()])
    objects = [_object(f"sample-{number:03}") for number in range(100)]
    for resource in objects:
        resource["status"]["message"] = "증" * 1000

    _replace(model, objects)

    snapshot = model.snapshot(NOW)
    assert sum(_serialized_item_bytes(item) for item in snapshot.current) <= 262144
    assert 0 < len(snapshot.current) < 100
    assert snapshot.current_dropped == 100 - len(snapshot.current)
    assert snapshot.dropped == 0
    assert _coverage(snapshot, "current-buffer:widgets").state == "capped"


@pytest.mark.parametrize("reverse", [False, True])
def test_current_count_limits_are_deterministic_and_isolated_per_source(reverse: bool) -> None:
    model = PulseModel([_WidgetRule(), PodPulseRule()], max_current_findings=1)
    widgets = [_object("first"), _object("second")]
    pods = [_pod_observation("first"), _pod_observation("second")]
    _replace(model, list(reversed(widgets)) if reverse else widgets)
    model.replace_source(
        "pods", list(reversed(pods)) if reverse else pods, PulseCoverage("pods", "complete", NOW)
    )

    snapshot = model.snapshot(NOW)
    assert [(item.source, item.target.name) for item in snapshot.current] == [
        ("pods", "first"),
        ("widgets", "first"),
    ]
    assert snapshot.current_dropped == 2
    for source in ("pods", "widgets"):
        coverage = _coverage(snapshot, f"current-buffer:{source}")
        assert coverage.state == "capped"
        assert "1" in coverage.detail
        assert "retention" in coverage.detail.lower()
        assert "not recovery" in coverage.detail.lower()


def test_current_retention_prefers_newest_evidence_and_bounds_identity_state() -> None:
    model = PulseModel([_WidgetRule()], max_current_findings=2)
    for number in range(50):
        _replace(
            model,
            [_object(f"sample-{number:03}")],
            state="partial",
            observed_at=NOW + timedelta(seconds=number),
        )

    snapshot = model.snapshot(NOW + timedelta(seconds=49))
    assert [item.target.name for item in snapshot.current] == ["sample-048", "sample-049"]
    assert snapshot.current_dropped == 48
    assert "sample-000" not in repr(vars(model))
    assert "sample-047" not in repr(vars(model))


def test_current_byte_limit_accepts_exact_boundary_and_refuses_oversized_items() -> None:
    resource = _object()
    resource["status"]["message"] = "경고" * 30
    (item,) = _WidgetRule().evaluate([resource], NOW)
    encoded_bytes = _serialized_item_bytes(item)
    exact = PulseModel([_WidgetRule()], max_current_bytes=encoded_bytes)
    too_small = PulseModel([_WidgetRule()], max_current_bytes=encoded_bytes - 1)

    _replace(exact, [resource])
    _replace(too_small, [resource])

    assert exact.snapshot(NOW).current == (item,)
    assert exact.snapshot(NOW).current_dropped == 0
    refused = too_small.snapshot(NOW)
    assert refused.current == ()
    assert refused.current_dropped == 1
    assert refused.dropped == 0
    assert _coverage(refused, "current-buffer:widgets").state == "capped"


def test_current_byte_limit_is_per_source_and_can_fill_around_an_oversized_item() -> None:
    small = _object("small")
    (item,) = _WidgetRule().evaluate([small], NOW)
    large = _object("large")
    large["status"]["message"] = "x" * 1000
    pod = _pod_observation()
    pod["status"] = {"phase": "Failed"}
    (pod_item,) = PodPulseRule().evaluate([pod], NOW)
    model = PulseModel(
        [_WidgetRule(), PodPulseRule()],
        max_current_bytes=max(_serialized_item_bytes(item), _serialized_item_bytes(pod_item)),
    )
    _replace(model, [large, small])
    model.replace_source("pods", [pod], PulseCoverage("pods", "complete", NOW))

    snapshot = model.snapshot(NOW)
    assert {entry.source for entry in snapshot.current} == {"pods", "widgets"}
    assert "large" not in {entry.target.name for entry in snapshot.current}
    assert snapshot.current_dropped == 1


def test_current_losses_accumulate_until_reset_without_changing_warning_drops() -> None:
    model = PulseModel([_WidgetRule()], max_current_findings=1, max_events=1)
    _replace(model, [_object("first"), _object("second")])
    _replace(model, [_object("third")], state="partial", observed_at=NOW + timedelta(seconds=1))
    model.record_warning(_event("one"), 0, NOW + timedelta(seconds=1))
    model.record_warning(_event("two"), 0, NOW + timedelta(seconds=1))
    _replace(model, [], observed_at=NOW + timedelta(seconds=2))

    snapshot = model.snapshot(NOW + timedelta(seconds=2))
    assert snapshot.current == ()
    assert snapshot.current_dropped == 2
    assert snapshot.dropped == 1
    assert _coverage(snapshot, "widgets").state == "complete"
    assert _coverage(snapshot, "current-buffer:widgets").state == "capped"

    model.reset(1, "production")

    reset = model.snapshot(NOW)
    assert reset.current == reset.recent == ()
    assert reset.current_dropped == reset.dropped == 0
    assert {coverage.source for coverage in reset.coverage} == {"widgets", "events"}
    _replace(model, [_object()])
    assert len(model.snapshot(NOW).current) == 1


@pytest.mark.parametrize("name", ["max_current_findings", "max_current_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_current_buffer_bounds_are_rejected(name: str, value: Any) -> None:
    with pytest.raises(ValueError, match=name):
        PulseModel(**{name: value})


def test_snapshot_constructor_preserves_existing_positional_dropped_argument() -> None:
    snapshot = PulseSnapshot(0, None, (), (), (), 7)

    assert snapshot.dropped == 7
    assert snapshot.current_dropped == 0


class _AssessedWidgetRule(_WidgetRule):
    def can_clear(self, resource: dict[str, Any]) -> bool:
        return isinstance(resource["status"].get("broken"), bool)


@pytest.mark.parametrize("state", ["complete", "partial", "capped"])
def test_generic_rule_assessment_retains_unknown_and_unobserved_findings(
    state: PulseCoverageState,
) -> None:
    model = PulseModel([_AssessedWidgetRule()])
    _replace(model, [_object("unknown"), _object("unseen"), _object("healthy")])
    unknown = _object("unknown")
    unknown["status"]["broken"] = None

    _replace(model, [unknown, _object("healthy", broken=False), _object("new")], state=state)

    snapshot = model.snapshot(NOW)
    assert {item.target.name for item in snapshot.current} == {"unknown", "unseen", "new"}
    coverage = _coverage(snapshot, "widgets")
    assert coverage.state == ("capped" if state == "capped" else "partial")
    assert "assess" in coverage.detail.lower()


@pytest.mark.parametrize("reverse", [False, True])
def test_every_rule_for_a_source_must_allow_destructive_assessment(reverse: bool) -> None:
    rules = [_WidgetRule(), _AssessedWidgetRule()]
    model = PulseModel(list(reversed(rules)) if reverse else rules)
    _replace(model, [_object()])
    unknown = _object()
    unknown["status"]["broken"] = None

    _replace(model, [unknown])

    assert len(model.snapshot(NOW).current) == 1
    assert _coverage(model.snapshot(NOW), "widgets").state == "partial"


def test_evaluate_only_custom_rules_remain_assessable_by_default() -> None:
    assert _WidgetRule().can_clear(_object()) is True


@pytest.mark.parametrize(("age", "resolved"), [(30, True), (31, False)])
def test_current_reconciliation_freshness_boundary(age: int, resolved: bool) -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])
    now = NOW + timedelta(seconds=10 + age)
    model.snapshot(now)

    _replace(model, [], observed_at=NOW + timedelta(seconds=10))

    assert (model.snapshot(now).current == ()) is resolved


def test_stale_source_can_merge_positive_evidence_without_implying_recovery() -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object("old")])
    model.snapshot(NOW + timedelta(seconds=60))

    _replace(model, [_object("new")], observed_at=NOW + timedelta(seconds=10))

    snapshot = model.snapshot(NOW + timedelta(seconds=60))
    assert {item.target.name for item in snapshot.current} == {"old", "new"}
    assert _coverage(snapshot, "widgets").state == "stale"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phase", {"value": "Running"}),
        ("phase", "UnfamiliarPhase"),
        ("conditions", None),
        ("conditions", []),
        ("conditions", [None]),
        ("conditions", [{"type": "Ready", "status": "True"}, {"type": "Ready", "status": "False"}]),
        ("containerStatuses", None),
        ("containerStatuses", []),
        ("containerStatuses", [None]),
        ("containerStatuses", [{"name": "api"}]),
        ("containerStatuses", [{"name": "api", "state": {}}]),
        ("containerStatuses", [{"name": "api", "state": {"running": "invalid"}}]),
        ("initContainerStatuses", [{"name": "init", "state": {"terminated": {"exitCode": "0"}}}]),
        ("ephemeralContainerStatuses", {"name": "debug"}),
    ],
)
def test_malformed_pod_status_does_not_erase_prior_evidence(field: str, value: object) -> None:
    model = PulseModel([PodPulseRule()])
    model.replace_source("pods", [_pod_observation()], PulseCoverage("pods", "complete", NOW))
    original = model.snapshot(NOW).current[0]
    pod = _pod_observation(ready="True")
    pod["status"][field] = value

    model.replace_source("pods", [pod], PulseCoverage("pods", "complete", NOW))

    snapshot = model.snapshot(NOW)
    assert original in snapshot.current
    assert _coverage(snapshot, "pods").state == "partial"


@pytest.mark.parametrize("state", ["complete", "partial", "capped"])
def test_missing_container_evidence_does_not_resolve_a_current_container_failure(
    state: PulseCoverageState,
) -> None:
    model = PulseModel([PodPulseRule()])
    broken = _pod_observation()
    broken["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "CrashLoopBackOff"}}
    model.replace_source("pods", [broken], PulseCoverage("pods", "complete", NOW))
    original = model.snapshot(NOW).current
    incomplete = _pod_observation(ready="True")
    incomplete["status"].pop("containerStatuses")

    model.replace_source("pods", [incomplete], PulseCoverage("pods", state, NOW))

    snapshot = model.snapshot(NOW)
    assert snapshot.current == original
    assert _coverage(snapshot, "pods").state == ("capped" if state == "capped" else "partial")


def test_unassessable_pod_can_still_add_an_explicit_positive_finding() -> None:
    model = PulseModel([PodPulseRule()])
    model.replace_source("pods", [_pod_observation()], PulseCoverage("pods", "complete", NOW))
    broken = _pod_observation(ready=None)
    broken["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "NewFailure"}}

    model.replace_source("pods", [broken], PulseCoverage("pods", "complete", NOW))

    snapshot = model.snapshot(NOW)
    assert {item.reason for item in snapshot.current} == {"NotReady", "NewFailure"}
    assert _coverage(snapshot, "pods").state == "partial"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", None),
        ("name", ""),
        ("name", "\napi"),
        ("namespace", None),
        ("namespace", ["production"]),
        ("uid", {"value": "uid-api"}),
    ],
)
def test_unassessable_metadata_does_not_turn_complete_input_into_recovery(
    field: str,
    value: object,
) -> None:
    model = PulseModel([PodPulseRule()])
    model.reset(1, "production")
    model.replace_source("pods", [_pod_observation()], PulseCoverage("pods", "complete", NOW))
    original = model.snapshot(NOW).current
    pod = _pod_observation(ready="True")
    pod["metadata"][field] = value

    model.replace_source("pods", [pod], PulseCoverage("pods", "complete", NOW))

    snapshot = model.snapshot(NOW)
    assert snapshot.current == original
    assert _coverage(snapshot, "pods").state == "partial"


def _deployment_observation() -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "namespace": "production",
            "name": "api",
            "uid": "deploy-uid",
            "generation": 3,
        },
        "status": {
            "observedGeneration": 3,
            "conditions": [{"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate"}],
        },
    }


@pytest.mark.parametrize(
    "status",
    [
        None,
        {},
        {"observedGeneration": 2, "conditions": []},
        {"observedGeneration": 3},
        {"observedGeneration": 3, "conditions": None},
        {
            "observedGeneration": 3,
            "conditions": [{"type": "ReplicaFailure", "status": {"value": "False"}}],
        },
        {"observedGeneration": 3, "conditions": [{"type": "ReplicaFailure", "status": "Unknown"}]},
        {"observedGeneration": 3, "conditions": [{"type": ["ReplicaFailure"], "status": "False"}]},
    ],
)
@pytest.mark.parametrize("state", ["complete", "partial", "capped"])
def test_unassessable_deployment_does_not_resolve_a_known_current_failure(
    status: object,
    state: PulseCoverageState,
) -> None:
    model = PulseModel([DeploymentPulseRule()])
    deployment = _deployment_observation()
    model.replace_source("deployments", [deployment], PulseCoverage("deployments", "complete", NOW))
    original = model.snapshot(NOW).current
    deployment["status"] = status

    model.replace_source("deployments", [deployment], PulseCoverage("deployments", state, NOW))

    snapshot = model.snapshot(NOW)
    assert snapshot.current == original
    assert _coverage(snapshot, "deployments").state == (
        "capped" if state == "capped" else "partial"
    )


def test_assessable_deployment_generation_can_clear_a_prior_failure() -> None:
    model = PulseModel([DeploymentPulseRule()])
    deployment = _deployment_observation()
    model.replace_source("deployments", [deployment], PulseCoverage("deployments", "complete", NOW))
    deployment["status"]["conditions"] = []

    model.replace_source("deployments", [deployment], PulseCoverage("deployments", "partial", NOW))

    assert model.snapshot(NOW).current == ()
    assert _coverage(model.snapshot(NOW), "deployments").state == "partial"


def test_malformed_foreign_namespace_does_not_hide_an_unassessable_observation() -> None:
    model = PulseModel([PodPulseRule()])
    model.reset(1, "production")
    model.replace_source("pods", [_pod_observation()], PulseCoverage("pods", "complete", NOW))
    incomplete = _pod_observation(ready="True")
    incomplete["metadata"]["namespace"] = "\x1bforeign"

    model.replace_source("pods", [incomplete], PulseCoverage("pods", "complete", NOW))

    snapshot = model.snapshot(NOW)
    assert len(snapshot.current) == 1
    assert _coverage(snapshot, "pods").state == "partial"


def test_failure_clock_prevents_delayed_complete_reconciliation_without_a_snapshot() -> None:
    model = PulseModel([_WidgetRule()])
    _replace(model, [_object()])
    model.set_coverage(PulseCoverage("widgets", "failed", NOW + timedelta(seconds=60)))

    _replace(model, [], observed_at=NOW + timedelta(seconds=10))

    assert len(model.snapshot(NOW + timedelta(seconds=60)).current) == 1


def test_first_observation_clock_is_shared_across_never_successful_sources() -> None:
    model = PulseModel([_WidgetRule()])
    model.set_coverage(PulseCoverage("events", "failed", NOW))
    model.set_coverage(PulseCoverage("widgets", "failed", NOW + timedelta(seconds=40)))

    snapshot = model.snapshot(NOW + timedelta(seconds=41))
    assert _coverage(snapshot, "events").state == "stale"
    assert _coverage(snapshot, "widgets").state == "stale"


def test_first_success_reanchors_source_staleness_and_reset_discards_prior_clock() -> None:
    model = PulseModel([_WidgetRule()])
    model.set_coverage(PulseCoverage("widgets", "failed", NOW))
    _replace(model, [_object()], observed_at=NOW + timedelta(seconds=40))

    assert _coverage(model.snapshot(NOW + timedelta(seconds=70)), "widgets").state == "complete"
    assert _coverage(model.snapshot(NOW + timedelta(seconds=71)), "widgets").state == "stale"

    model.reset(2, "production")
    model.set_coverage(PulseCoverage("widgets", "failed", NOW))
    assert _coverage(model.snapshot(NOW + timedelta(seconds=30)), "widgets").state == "failed"
    assert _coverage(model.snapshot(NOW + timedelta(seconds=31)), "widgets").state == "stale"


@pytest.mark.parametrize("reason", ["ContainerCreating", "PodInitializing"])
def test_ephemeral_startup_cannot_resolve_an_existing_current_not_ready_finding(
    reason: str,
) -> None:
    model = PulseModel([PodPulseRule()])
    model.replace_source("pods", [_pod_observation()], PulseCoverage("pods", "complete", NOW))
    pod = _pod_observation()
    pod["spec"]["ephemeralContainers"] = [{"name": "debug"}]
    pod["status"]["ephemeralContainerStatuses"] = [
        {"name": "debug", "state": {"waiting": {"reason": reason}}}
    ]

    model.replace_source(
        "pods", [pod], PulseCoverage("pods", "complete", NOW + timedelta(seconds=1))
    )

    snapshot = model.snapshot(NOW + timedelta(seconds=1))
    assert [item.reason for item in snapshot.current] == ["NotReady"]
    assert _coverage(snapshot, "pods").state == "complete"


_DECLARED_CONTAINER_GROUPS = (
    ("containers", "containerStatuses"),
    ("initContainers", "initContainerStatuses"),
    ("ephemeralContainers", "ephemeralContainerStatuses"),
)


def _pod_with_declared_worker(
    spec_field: str, status_field: str, *, broken: bool = True
) -> dict[str, Any]:
    pod = _pod_observation(ready="False" if broken else "True")
    pod["spec"] = {"containers": [{"name": "api"}]}
    pod["spec"].setdefault(spec_field, []).append({"name": "worker"})
    pod["status"]["containerStatuses"][0]["ready"] = True
    worker: dict[str, Any] = {"name": "worker", "state": {"running": {}}}
    if spec_field != "containers":
        worker["state"] = {
            "terminated": {
                "exitCode": 1 if broken else 0,
                "reason": "Error" if broken else "Completed",
            }
        }
    elif broken:
        worker["state"] = {"waiting": {"reason": "CrashLoopBackOff"}}
    pod["status"].setdefault(status_field, []).append(worker)
    return pod


@pytest.mark.parametrize(("spec_field", "status_field"), _DECLARED_CONTAINER_GROUPS)
@pytest.mark.parametrize("state", ["complete", "partial", "capped"])
@pytest.mark.parametrize("omission", ["entry", "group"])
def test_missing_declared_container_evidence_cannot_clear_whole_pod(
    spec_field: str, status_field: str, state: PulseCoverageState, omission: str
) -> None:
    model = PulseModel([PodPulseRule()])
    broken = _pod_with_declared_worker(spec_field, status_field)
    model.replace_source("pods", [broken], PulseCoverage("pods", "complete", NOW))
    original = model.snapshot(NOW).current
    assert [item.reason for item in original] == [
        "CrashLoopBackOff" if spec_field == "containers" else "Error"
    ]
    incomplete = _pod_with_declared_worker(spec_field, status_field, broken=False)
    if omission == "group":
        incomplete["status"].pop(status_field)
    else:
        incomplete["status"][status_field] = [
            container
            for container in incomplete["status"][status_field]
            if container["name"] != "worker"
        ]

    model.replace_source(
        "pods", [incomplete], PulseCoverage("pods", state, NOW + timedelta(seconds=1))
    )

    snapshot = model.snapshot(NOW + timedelta(seconds=1))
    assert snapshot.current == original
    assert snapshot.current_dropped == 0
    coverage = _coverage(snapshot, "pods")
    assert coverage.state == ("capped" if state == "capped" else "partial")
    assert "assess" in coverage.detail.lower()


@pytest.mark.parametrize(("spec_field", "status_field"), _DECLARED_CONTAINER_GROUPS)
@pytest.mark.parametrize("state", ["complete", "partial", "capped"])
def test_full_matching_declared_container_evidence_can_confirm_recovery(
    spec_field: str, status_field: str, state: PulseCoverageState
) -> None:
    model = PulseModel([PodPulseRule()])
    broken = _pod_with_declared_worker(spec_field, status_field)
    model.replace_source("pods", [broken], PulseCoverage("pods", "complete", NOW))
    healthy = _pod_with_declared_worker(spec_field, status_field, broken=False)
    healthy["status"][status_field].reverse()

    model.replace_source(
        "pods", [healthy], PulseCoverage("pods", state, NOW + timedelta(seconds=1))
    )

    snapshot = model.snapshot(NOW + timedelta(seconds=1))
    assert PodPulseRule().can_clear(healthy) is True
    assert snapshot.current == ()
    assert snapshot.current_dropped == 0
    assert _coverage(snapshot, "pods").state == state


@pytest.mark.parametrize("names", [["api", "api"], ["api", "other"], ["api", "worker", "worker"]])
def test_declared_container_coverage_matches_unique_names_not_status_count(
    names: list[str],
) -> None:
    model = PulseModel([PodPulseRule()])
    broken = _pod_with_declared_worker("containers", "containerStatuses")
    model.replace_source("pods", [broken], PulseCoverage("pods", "complete", NOW))
    original = model.snapshot(NOW).current
    incomplete = _pod_with_declared_worker("containers", "containerStatuses", broken=False)
    incomplete["status"]["containerStatuses"] = [
        {"name": name, "ready": True, "state": {"running": {}}} for name in names
    ]

    model.replace_source("pods", [incomplete], PulseCoverage("pods", "complete", NOW))

    assert model.snapshot(NOW).current == original
    assert _coverage(model.snapshot(NOW), "pods").state == "partial"


def test_declared_container_evidence_cannot_be_borrowed_from_another_status_group() -> None:
    model = PulseModel([PodPulseRule()])
    broken = _pod_with_declared_worker("ephemeralContainers", "ephemeralContainerStatuses")
    model.replace_source("pods", [broken], PulseCoverage("pods", "complete", NOW))
    original = model.snapshot(NOW).current
    incomplete = _pod_with_declared_worker(
        "ephemeralContainers", "ephemeralContainerStatuses", broken=False
    )
    incomplete["status"]["containerStatuses"].extend(
        incomplete["status"].pop("ephemeralContainerStatuses")
    )

    model.replace_source("pods", [incomplete], PulseCoverage("pods", "complete", NOW))

    assert model.snapshot(NOW).current == original
    assert _coverage(model.snapshot(NOW), "pods").state == "partial"


@pytest.mark.parametrize(
    "spec",
    [
        None,
        [],
        {},
        {"containers": None},
        {"containers": []},
        {"containers": [None]},
        {"containers": [{"name": "api"}, {"name": None}]},
        {"containers": [{"name": "api"}, {"name": "worker"}, {"name": "worker"}]},
        {"containers": [{"name": "api"}, {"name": "worker"}], "ephemeralContainers": None},
    ],
)
def test_unassessable_container_declarations_do_not_prove_recovery(spec: object) -> None:
    model = PulseModel([PodPulseRule()])
    broken = _pod_with_declared_worker("containers", "containerStatuses")
    model.replace_source("pods", [broken], PulseCoverage("pods", "complete", NOW))
    original = model.snapshot(NOW).current
    incomplete = _pod_with_declared_worker("containers", "containerStatuses", broken=False)
    incomplete["spec"] = spec

    model.replace_source("pods", [incomplete], PulseCoverage("pods", "complete", NOW))

    assert model.snapshot(NOW).current == original
    assert _coverage(model.snapshot(NOW), "pods").state == "partial"


def test_missing_pod_spec_cannot_establish_complete_container_identity_coverage() -> None:
    model = PulseModel([PodPulseRule()])
    broken = _pod_with_declared_worker("containers", "containerStatuses")
    model.replace_source("pods", [broken], PulseCoverage("pods", "complete", NOW))
    original = model.snapshot(NOW).current
    incomplete = _pod_with_declared_worker("containers", "containerStatuses", broken=False)
    incomplete.pop("spec")
    incomplete["status"]["containerStatuses"] = incomplete["status"]["containerStatuses"][:1]

    model.replace_source("pods", [incomplete], PulseCoverage("pods", "complete", NOW))

    assert model.snapshot(NOW).current == original
    assert _coverage(model.snapshot(NOW), "pods").state == "partial"


@pytest.mark.parametrize(("spec_field", "status_field"), _DECLARED_CONTAINER_GROUPS)
@pytest.mark.parametrize("state", ["complete", "partial", "capped"])
def test_succeeded_pod_can_clear_even_when_declared_container_statuses_are_omitted(
    spec_field: str, status_field: str, state: PulseCoverageState
) -> None:
    model = PulseModel([PodPulseRule()])
    pod = _pod_with_declared_worker(spec_field, status_field)
    model.replace_source("pods", [pod], PulseCoverage("pods", "complete", NOW))
    pod["status"] = {"phase": "Succeeded"}

    model.replace_source("pods", [pod], PulseCoverage("pods", state, NOW + timedelta(seconds=1)))

    assert PodPulseRule().can_clear(pod) is True
    assert model.snapshot(NOW + timedelta(seconds=1)).current == ()
    assert _coverage(model.snapshot(NOW + timedelta(seconds=1)), "pods").state == state


@pytest.mark.parametrize(("spec_field", "status_field"), _DECLARED_CONTAINER_GROUPS)
def test_failed_pod_with_omitted_declared_statuses_preserves_container_evidence(
    spec_field: str, status_field: str
) -> None:
    model = PulseModel([PodPulseRule()])
    pod = _pod_with_declared_worker(spec_field, status_field)
    model.replace_source("pods", [pod], PulseCoverage("pods", "complete", NOW))
    original = model.snapshot(NOW).current[0]
    pod["status"] = {"phase": "Failed"}

    model.replace_source(
        "pods", [pod], PulseCoverage("pods", "complete", NOW + timedelta(seconds=1))
    )

    snapshot = model.snapshot(NOW + timedelta(seconds=1))
    assert original in snapshot.current
    assert {item.reason for item in snapshot.current} == {original.reason, "Failed"}
    assert _coverage(snapshot, "pods").state == "partial"


def test_fully_observed_failed_pod_replaces_obsolete_container_evidence_but_stays_failed() -> None:
    model = PulseModel([PodPulseRule()])
    pod = _pod_with_declared_worker("containers", "containerStatuses")
    model.replace_source("pods", [pod], PulseCoverage("pods", "complete", NOW))
    pod["status"] = {
        "phase": "Failed",
        "containerStatuses": [
            {"name": "api", "state": {"terminated": {"exitCode": 0, "reason": "Completed"}}},
            {"name": "worker", "state": {"terminated": {"exitCode": 1, "reason": "Error"}}},
        ],
    }

    model.replace_source(
        "pods", [pod], PulseCoverage("pods", "complete", NOW + timedelta(seconds=1))
    )

    snapshot = model.snapshot(NOW + timedelta(seconds=1))
    assert PodPulseRule().can_clear(pod) is True
    assert {item.reason for item in snapshot.current} == {"Failed", "Error"}
    assert _coverage(snapshot, "pods").state == "complete"
