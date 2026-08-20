"""Mutable fake cluster state and the operation-eval WriteOps fake."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

import pytest

from korvid.evals.operation import OperationCluster
from korvid.evals.operation_journal import ActionJournal
from korvid.evals.operation_state import (
    RESTART_ANNOTATION,
    AuditRecord,
    FakeClusterState,
    StatefulFakeKubeClient,
    StatefulFakeWriteOps,
    parse_audit_records,
)
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.dryrun import diff_manifests
from korvid.k8s.errors import ApiStatusError

_DEPLOY = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_DAEMONSET = ResourceMeta("DaemonSet", "daemonsets", "apps", "v1", True, ("ds",))
_JOB = ResourceMeta("Job", "jobs", "batch", "v1", True, ("job",))
_UID = "deployment-checkout-a"

_INTENT = AuditRecord(
    action="scale",
    kind="deployments",
    group="apps",
    namespace="shop-a",
    name="checkout-a",
    outcome="intent",
    context="eval",
)


def _manifest(
    uid: str = _UID,
    replicas: int = 2,
    *,
    name: str = "checkout-a",
    namespace: str = "shop-a",
) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": uid,
            "generation": 4,
            "resourceVersion": "1001",
            "creationTimestamp": "2026-07-27T05:00:00Z",
        },
        "spec": {"replicas": replicas, "template": {"metadata": {"annotations": {}}}},
        "status": {"replicas": replicas, "readyReplicas": replicas, "availableReplicas": replicas},
    }


def _wiring(
    *objects: dict[str, Any],
    reconcile: bool = True,
    audit_intent_probe: Callable[[], tuple[AuditRecord, ...]] | None = None,
    context: str = "eval",
) -> tuple[StatefulFakeKubeClient, StatefulFakeWriteOps, ActionJournal]:
    cluster = OperationCluster(objects=tuple(objects or (_manifest(),)), reconcile_status=reconcile)
    kube = StatefulFakeKubeClient(cluster)
    journal = ActionJournal()
    writes = StatefulFakeWriteOps(
        kube.state, journal, context=context, audit_intent_probe=audit_intent_probe
    )
    return kube, writes, journal


async def test_a_scale_is_visible_through_the_shared_read_path() -> None:
    kube, writes, _journal = _wiring()
    await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=_UID)
    fetched = await kube.get_object(_DEPLOY, "shop-a", "checkout-a")
    assert fetched["spec"]["replicas"] == 3
    assert fetched["status"]["readyReplicas"] == 3
    assert fetched["metadata"]["resourceVersion"] != "1001"


async def test_reads_still_return_deep_copies() -> None:
    kube, _writes, _journal = _wiring()
    first = await kube.get_object(_DEPLOY, "shop-a", "checkout-a")
    first["spec"]["replicas"] = 99
    second = await kube.get_object(_DEPLOY, "shop-a", "checkout-a")
    assert second["spec"]["replicas"] == 2


def test_snapshot_returns_a_deep_copy_and_uid_of_tracks_presence() -> None:
    state = FakeClusterState([_manifest()], reconcile_status=True)
    snapshot = state.snapshot(
        group="apps", kind="Deployment", namespace="shop-a", name="checkout-a"
    )
    assert snapshot is not None
    snapshot["spec"]["replicas"] = 99
    assert (
        state.find(group="apps", kind="Deployment", namespace="shop-a", name="checkout-a")
        is not None
    )
    assert (
        state.uid_of(group="apps", kind="Deployment", namespace="shop-a", name="checkout-a") == _UID
    )
    live = state.find(group="apps", kind="Deployment", namespace="shop-a", name="checkout-a")
    assert live is not None
    assert live["spec"]["replicas"] == 2
    assert (
        state.snapshot(group="apps", kind="Deployment", namespace="shop-a", name="missing") is None
    )
    assert state.uid_of(group="apps", kind="Deployment", namespace="shop-a", name="missing") is None


async def test_a_scale_journals_the_mutation_boundary_with_pre_and_post_state() -> None:
    _kube, writes, journal = _wiring()
    await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=_UID)
    assert journal.checkpoints() == ("mutation_started", "mutation_finished")
    finished = journal.events[-1]
    assert finished.actor == "write_ops"
    assert finished.pre_state == {"spec.replicas": 2}
    assert finished.post_state == {"spec.replicas": 3}
    assert finished.target is not None
    assert finished.target.uid == _UID


async def test_a_write_without_a_uid_precondition_hard_fails_and_mutates_nothing() -> None:
    kube, writes, journal = _wiring()
    with pytest.raises(ApiStatusError, match="uid precondition"):
        await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=None)
    assert journal.has("write_without_uid") is True
    assert journal.has("mutation_started") is False
    fetched = await kube.get_object(_DEPLOY, "shop-a", "checkout-a")
    assert fetched["spec"]["replicas"] == 2


async def test_a_replaced_target_conflicts_instead_of_mutating() -> None:
    kube, writes, journal = _wiring()
    kube.state.replace_object(_manifest(uid="deployment-checkout-a-2", replicas=2))
    with pytest.raises(ApiStatusError, match="changed since it was approved") as excinfo:
        await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=_UID)
    assert excinfo.value.status == 409
    assert journal.has("uid_conflict") is True
    assert journal.has("mutation_started") is False
    fetched = await kube.get_object(_DEPLOY, "shop-a", "checkout-a")
    assert fetched["spec"]["replicas"] == 2


async def test_a_missing_target_write_is_journaled_as_a_wrong_target_attempt_before_404() -> None:
    _kube, writes, journal = _wiring()
    with pytest.raises(ApiStatusError, match="not found") as excinfo:
        await writes.scale_object(_DEPLOY, "shop-a", "missing", 3, uid="deployment-missing")
    assert excinfo.value.status == 404
    assert journal.has("wrong_target_write") is True
    event = next(item for item in journal.events if item.event == "wrong_target_write")
    assert event.actor == "write_ops"
    assert event.result == "refused"
    assert event.target is not None
    assert event.target.name == "missing"
    assert journal.has("mutation_started") is False


async def test_replacing_the_incarnation_swaps_the_uid_and_keeps_the_object_shape() -> None:
    """What a fixture's declarative `dialog_intervention` does: the object
    keeps its name, namespace, and spec, and becomes a different object."""
    kube, _writes, _journal = _wiring()
    replaced = kube.state.replace_incarnation(
        group="apps",
        kind="Deployment",
        namespace="shop-a",
        name="checkout-a",
        uid="deployment-checkout-a-2",
    )
    assert replaced is True
    fetched = await kube.get_object(_DEPLOY, "shop-a", "checkout-a")
    assert fetched["metadata"]["uid"] == "deployment-checkout-a-2"
    assert fetched["spec"]["replicas"] == 2
    assert fetched["metadata"]["resourceVersion"] != "1001"


def test_replacing_an_object_preserves_the_existing_store_order() -> None:
    objects = [
        _manifest(uid="deployment-catalog", name="catalog"),
        _manifest(uid=_UID, name="checkout-a"),
        _manifest(uid="deployment-payments", name="payments"),
    ]
    state = FakeClusterState(objects, reconcile_status=True)
    state.replace_object(_manifest(uid="deployment-checkout-a-2", name="checkout-a"))
    assert [manifest["metadata"]["name"] for manifest in objects] == [
        "catalog",
        "checkout-a",
        "payments",
    ]
    assert objects[1]["metadata"]["uid"] == "deployment-checkout-a-2"


def test_replacing_an_incarnation_preserves_the_existing_store_order() -> None:
    objects = [
        _manifest(uid="deployment-catalog", name="catalog"),
        _manifest(uid=_UID, name="checkout-a"),
        _manifest(uid="deployment-payments", name="payments"),
    ]
    state = FakeClusterState(objects, reconcile_status=True)
    replaced = state.replace_incarnation(
        group="apps",
        kind="Deployment",
        namespace="shop-a",
        name="checkout-a",
        uid="deployment-checkout-a-2",
    )
    assert replaced is True
    assert [manifest["metadata"]["name"] for manifest in objects] == [
        "catalog",
        "checkout-a",
        "payments",
    ]
    assert objects[1]["metadata"]["uid"] == "deployment-checkout-a-2"


def test_replacing_an_absent_object_reports_that_nothing_was_replaced() -> None:
    state = FakeClusterState([_manifest()], reconcile_status=True)
    assert (
        state.replace_incarnation(
            group="apps",
            kind="Deployment",
            namespace="shop-a",
            name="missing",
            uid="deployment-missing-2",
        )
        is False
    )


async def test_a_rollout_restart_stamps_the_template_and_advances_generation() -> None:
    kube, writes, _journal = _wiring()
    await writes.rollout_restart_with_stamp(
        _DEPLOY, "shop-a", "checkout-a", uid=_UID, restarted_at="2026-08-21T02:00:00+09:00"
    )
    fetched = await kube.get_object(_DEPLOY, "shop-a", "checkout-a")
    annotations = fetched["spec"]["template"]["metadata"]["annotations"]
    assert annotations[RESTART_ANNOTATION] == "2026-08-21T02:00:00+09:00"
    assert fetched["metadata"]["generation"] == 5
    assert fetched["status"]["observedGeneration"] == 5


async def test_scale_is_refused_for_a_kind_the_fake_does_not_support() -> None:
    _kube, writes, journal = _wiring()
    with pytest.raises(ApiStatusError, match="scale is not supported") as excinfo:
        await writes.scale_object(_DAEMONSET, "shop-a", "checkout-a", 3, uid=_UID)
    assert excinfo.value.status == 422
    assert journal.has("unsupported_write") is True


async def test_restart_is_refused_for_a_kind_the_fake_does_not_support() -> None:
    _kube, writes, journal = _wiring()
    with pytest.raises(ApiStatusError, match="rollout restart is not supported") as excinfo:
        await writes.rollout_restart(_JOB, "shop-a", "checkout-a", uid=_UID)
    assert excinfo.value.status == 422
    assert journal.has("unsupported_write") is True


async def test_delete_fails_closed_as_a_405_api_error() -> None:
    _kube, writes, journal = _wiring()
    with pytest.raises(ApiStatusError, match="operation eval fake") as excinfo:
        await writes.delete_object(_DEPLOY, "shop-a", "checkout-a", uid=_UID)
    assert excinfo.value.status == 405
    assert journal.has("unsupported_write") is True


async def test_replace_fails_closed_as_a_422_api_error() -> None:
    _kube, writes, journal = _wiring()
    with pytest.raises(ApiStatusError, match="operation eval fake") as excinfo:
        await writes.replace_object(_DEPLOY, "shop-a", "checkout-a", {}, uid=_UID)
    assert excinfo.value.status == 422
    assert journal.has("unsupported_write") is True


async def test_no_write_path_raises_not_implemented_through_the_application() -> None:
    _kube, writes, _journal = _wiring()
    for coroutine in (
        writes.create_object(_DEPLOY, "shop-a", {}),
        writes.resize_pod("shop-a", "checkout-a-1", {}, uid="pod-1"),
        writes.cordon_node("worker-1", True, uid="node-1"),
        writes.evict_pod("shop-a", "checkout-a-1", uid="pod-1"),
        writes.drain_plan("worker-1"),
    ):
        with pytest.raises(ApiStatusError, match="operation eval fake"):
            await coroutine


async def test_previews_describe_the_exact_request_that_would_execute() -> None:
    kube, writes, _journal = _wiring()
    before = await kube.get_object(_DEPLOY, "shop-a", "checkout-a")
    scale = await writes.preview_scale(_DEPLOY, "shop-a", "checkout-a", 3, uid=_UID)
    stamp = "2026-08-21T02:00:00+09:00"
    restart = await writes.preview_rollout_restart(
        _DEPLOY, "shop-a", "checkout-a", uid=_UID, restarted_at=stamp
    )
    scaled = deepcopy(before)
    scaled["spec"]["replicas"] = 3
    restarted = deepcopy(before)
    restarted["spec"]["template"]["metadata"]["annotations"][RESTART_ANNOTATION] = stamp
    assert scale == ["~ spec.replicas: 2 -> 3"]
    assert restart == diff_manifests(before, restarted)
    assert scale == diff_manifests(before, scaled)
    after = await kube.get_object(_DEPLOY, "shop-a", "checkout-a")
    assert after == before


async def test_scale_preview_uses_structural_diff_so_a_noop_is_empty() -> None:
    _kube, writes, _journal = _wiring()
    assert await writes.preview_scale(_DEPLOY, "shop-a", "checkout-a", 2, uid=_UID) == []


async def test_a_preview_without_a_uid_precondition_is_unavailable() -> None:
    _kube, writes, _journal = _wiring()
    assert await writes.preview_scale(_DEPLOY, "shop-a", "checkout-a", 3, uid=None) is None


async def test_restart_preview_is_unavailable_without_a_uid_precondition() -> None:
    _kube, writes, _journal = _wiring()
    assert await writes.preview_rollout_restart(_DEPLOY, "shop-a", "checkout-a", uid=None) is None


async def test_restart_preview_is_unavailable_when_the_target_is_missing() -> None:
    _kube, writes, _journal = _wiring()
    assert (
        await writes.preview_rollout_restart(_DEPLOY, "shop-a", "missing", uid="deployment-missing")
        is None
    )


def test_typed_path_reads_walk_quoted_annotation_segments() -> None:
    state = FakeClusterState([_manifest()], reconcile_status=True)
    state.find(group="apps", kind="Deployment", namespace="shop-a", name="checkout-a")
    found, value = state.read(
        group="apps",
        kind="Deployment",
        namespace="shop-a",
        name="checkout-a",
        path="spec.replicas",
    )
    assert (found, value) == (True, 2)
    missing = state.read(
        group="apps",
        kind="Deployment",
        namespace="shop-a",
        name="checkout-a",
        path='spec.template.metadata.annotations."kubectl.kubernetes.io/restartedAt"',
    )
    assert missing == (False, None)


async def test_status_reconciliation_is_fixture_controlled() -> None:
    kube, writes, _journal = _wiring(_manifest(), reconcile=False)
    await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=_UID)
    fetched = await kube.get_object(_DEPLOY, "shop-a", "checkout-a")
    assert fetched["spec"]["replicas"] == 3
    assert fetched["status"]["readyReplicas"] == 2


async def test_the_audit_intent_probe_is_read_immediately_before_the_mutation() -> None:
    """Fail-closed ordering, proved without subclassing the audit log."""
    reads: list[int] = []

    def probe() -> tuple[AuditRecord, ...]:
        reads.append(len(reads) + 1)
        return (_INTENT,)

    _kube, writes, journal = _wiring(audit_intent_probe=probe)
    await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=_UID)
    events = [event.event for event in journal.events]
    assert reads == [1]
    assert events.index("audit_intent_observed") < events.index("mutation_started")
    assert events.index("mutation_started") < events.index("mutation_finished")
    observed = next(e for e in journal.events if e.event == "audit_intent_observed")
    assert observed.actor == "audit"
    assert observed.result == "durable"


async def test_a_mutation_without_a_persisted_intent_is_journaled_as_missing() -> None:
    """The probe never blocks: enforcement is the production app's job, and
    the grader turns `audit_intent_missing` into a hard failure."""
    _kube, writes, journal = _wiring(audit_intent_probe=lambda: ())
    await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=_UID)
    assert journal.has("audit_intent_missing") is True
    assert journal.has("audit_intent_observed") is False
    assert journal.has("mutation_finished") is True


async def test_one_persisted_intent_cannot_satisfy_multiple_mutations() -> None:
    records = [_INTENT]

    def probe() -> tuple[AuditRecord, ...]:
        return tuple(records)

    _kube, writes, journal = _wiring(audit_intent_probe=probe)
    await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=_UID)
    await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 4, uid=_UID)
    assert [event.event for event in journal.events if event.event.startswith("audit_intent_")] == [
        "audit_intent_observed",
        "audit_intent_missing",
    ]


async def test_a_later_intent_can_satisfy_a_later_mutation_once_it_exists() -> None:
    records = [_INTENT]

    def probe() -> tuple[AuditRecord, ...]:
        return tuple(records)

    _kube, writes, journal = _wiring(audit_intent_probe=probe)
    await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=_UID)
    records.append(_INTENT)
    await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 4, uid=_UID)
    assert [event.event for event in journal.events if event.event.startswith("audit_intent_")] == [
        "audit_intent_observed",
        "audit_intent_observed",
    ]


async def test_an_intent_for_another_target_does_not_count_as_this_write_s_intent() -> None:
    other = AuditRecord(
        action="scale",
        kind="deployments",
        group="apps",
        namespace="shop-b",
        name="checkout-a",
        outcome="intent",
        context="eval",
    )
    _kube, writes, journal = _wiring(audit_intent_probe=lambda: (other,))
    await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=_UID)
    assert journal.has("audit_intent_missing") is True


async def test_an_intent_recorded_under_another_context_does_not_count() -> None:
    """Context identity is part of the journaled boundary, so a shared
    audit log (Slice B) can never lend one context's intent to another
    context's write."""
    elsewhere = AuditRecord(
        action="scale",
        kind="deployments",
        group="apps",
        namespace="shop-a",
        name="checkout-a",
        outcome="intent",
        context="production",
    )
    _kube, writes, journal = _wiring(audit_intent_probe=lambda: (elsewhere,))
    await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=_UID)
    assert journal.has("audit_intent_missing") is True
    assert journal.has("audit_intent_observed") is False


async def test_a_restart_observes_its_own_audit_intent() -> None:
    restart_intent = AuditRecord(
        action="rollout_restart",
        kind="deployments",
        group="apps",
        namespace="shop-a",
        name="checkout-a",
        outcome="intent",
        context="eval",
    )
    _kube, writes, journal = _wiring(audit_intent_probe=lambda: (restart_intent,))
    await writes.rollout_restart(_DEPLOY, "shop-a", "checkout-a", uid=_UID)
    assert journal.has("audit_intent_observed") is True


async def test_a_refused_write_never_claims_an_audit_observation() -> None:
    _kube, writes, journal = _wiring(audit_intent_probe=lambda: (_INTENT,))
    with pytest.raises(ApiStatusError, match="uid precondition"):
        await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=None)
    assert journal.has("audit_intent_observed") is False
    assert journal.has("audit_intent_missing") is False


async def test_an_unusual_context_value_does_not_break_audit_observation() -> None:
    weird_context = "eval context"
    weird_intent = AuditRecord(
        action="scale",
        kind="deployments",
        group="apps",
        namespace="shop-a",
        name="checkout-a",
        outcome="intent",
        context=weird_context,
    )
    _kube, writes, journal = _wiring(
        audit_intent_probe=lambda: (weird_intent,), context=weird_context
    )
    await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid=_UID)
    assert journal.has("audit_intent_observed") is True
    assert journal.has("mutation_finished") is True


async def test_an_unusual_uid_does_not_mask_the_conflict_api_error() -> None:
    _kube, writes, journal = _wiring()
    with pytest.raises(ApiStatusError, match="changed since it was approved") as excinfo:
        await writes.scale_object(_DEPLOY, "shop-a", "checkout-a", 3, uid="unsafe uid")
    assert excinfo.value.status == 409
    assert journal.has("uid_conflict") is True


def test_audit_records_are_parsed_and_a_torn_final_line_is_skipped() -> None:
    text = (
        '{"action": "scale", "kind": "deployments", "group": "apps", "namespace": "shop-a",'
        ' "name": "checkout-a", "outcome": "intent", "context": "eval"}\n'
        "\n"
        '{"action": "scale", "kind": "deploy'
    )
    records = parse_audit_records(text)
    assert records == (_INTENT,)


def test_parsing_an_empty_audit_file_yields_no_records() -> None:
    assert parse_audit_records("") == ()
