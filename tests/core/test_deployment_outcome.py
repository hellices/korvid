from korvid.core.deployment_outcome import (
    DeploymentCondition,
    DeploymentObservation,
    DeploymentOperationTarget,
    DeploymentOutcomePhase,
    DeploymentPodEvidence,
    DeploymentRestartIntent,
    DeploymentScaleIntent,
    evaluate_deployment_outcome,
    normalize_deployment_observation,
)
from korvid.k8s.deployment_outcomes import RawDeploymentOutcomeSnapshot


def _target() -> DeploymentOperationTarget:
    return DeploymentOperationTarget(
        epoch=3,
        cluster_id="context-a|https://cluster.example",
        namespace="default",
        name="web",
        uid="deploy-uid",
    )


def _scale(replicas: int = 3, generation: int = 7) -> DeploymentScaleIntent:
    return DeploymentScaleIntent(
        target=_target(),
        replicas=replicas,
        generation=generation,
    )


def _restart(
    stamp: str = "2026-10-09T12:00:00+00:00",
    generation: int = 7,
) -> DeploymentRestartIntent:
    return DeploymentRestartIntent(
        target=_target(),
        restarted_at=stamp,
        generation=generation,
    )


def _observation(
    *,
    uid: str = "deploy-uid",
    generation: int | None = 7,
    observed_generation: int | None = 7,
    desired: int | None = 3,
    current: int | None = 3,
    updated: int | None = 3,
    ready: int | None = 3,
    available: int | None = 3,
    unavailable: int | None = 0,
    restart_stamp: str | None = None,
    conditions: tuple[DeploymentCondition, ...] = (),
    pods: tuple[DeploymentPodEvidence, ...] = (),
    partial_evidence: bool = False,
    pod_evidence_ambiguous: bool = False,
) -> DeploymentObservation:
    return DeploymentObservation(
        uid=uid,
        generation=generation,
        observed_generation=observed_generation,
        desired_replicas=desired,
        current_replicas=current,
        updated_replicas=updated,
        ready_replicas=ready,
        available_replicas=available,
        unavailable_replicas=unavailable,
        restart_stamp=restart_stamp,
        conditions=conditions,
        pods=pods,
        partial_evidence=partial_evidence,
        pod_evidence_ambiguous=pod_evidence_ambiguous,
    )


def test_scale_requires_current_observed_generation() -> None:
    outcome = evaluate_deployment_outcome(
        _scale(),
        _observation(observed_generation=6),
    )

    assert outcome.phase is DeploymentOutcomePhase.OBSERVING
    assert "generation" in outcome.summary.lower()


def test_scale_requires_every_replica_count() -> None:
    outcome = evaluate_deployment_outcome(
        _scale(),
        _observation(available=None),
    )

    assert outcome.phase is DeploymentOutcomePhase.OBSERVING
    assert "replica" in outcome.summary.lower()


def test_scale_to_zero_completes_without_defaulting_missing_fields() -> None:
    complete = evaluate_deployment_outcome(
        _scale(0),
        _observation(
            desired=0,
            current=0,
            updated=0,
            ready=0,
            available=0,
            unavailable=0,
        ),
    )
    incomplete = evaluate_deployment_outcome(
        _scale(0),
        _observation(
            desired=0,
            current=None,
            updated=0,
            ready=0,
            available=0,
            unavailable=0,
        ),
    )

    assert complete.phase is DeploymentOutcomePhase.COMPLETED
    assert incomplete.phase is DeploymentOutcomePhase.OBSERVING


def test_scale_with_later_generation_is_superseded() -> None:
    outcome = evaluate_deployment_outcome(
        _scale(3, generation=7),
        _observation(generation=8, observed_generation=8),
    )

    assert outcome.phase is DeploymentOutcomePhase.SUPERSEDED


def test_scale_is_superseded_when_live_target_changes() -> None:
    outcome = evaluate_deployment_outcome(
        _scale(3),
        _observation(desired=5),
    )

    assert outcome.phase is DeploymentOutcomePhase.SUPERSEDED
    assert "5" in outcome.summary


def test_restart_requires_exact_marker_and_current_generation() -> None:
    intent = _restart()
    outcome = evaluate_deployment_outcome(
        intent,
        _observation(restart_stamp=intent.restarted_at),
    )

    assert outcome.phase is DeploymentOutcomePhase.COMPLETED
    assert "converged" in outcome.summary.lower()


def test_restart_with_later_marker_is_superseded() -> None:
    outcome = evaluate_deployment_outcome(
        _restart("accepted"),
        _observation(restart_stamp="later"),
    )

    assert outcome.phase is DeploymentOutcomePhase.SUPERSEDED
    assert "restart" in outcome.summary.lower()


def test_restart_with_later_generation_is_superseded() -> None:
    outcome = evaluate_deployment_outcome(
        _restart("accepted", generation=7),
        _observation(generation=8, observed_generation=8, restart_stamp="accepted"),
    )

    assert outcome.phase is DeploymentOutcomePhase.SUPERSEDED


def test_stale_failure_condition_does_not_stall_new_generation() -> None:
    condition = DeploymentCondition(
        type="Progressing",
        status="False",
        reason="ProgressDeadlineExceeded",
        message="old rollout",
    )

    outcome = evaluate_deployment_outcome(
        _scale(),
        _observation(observed_generation=6, conditions=(condition,)),
    )

    assert outcome.phase is DeploymentOutcomePhase.OBSERVING


def test_same_name_replacement_never_completes() -> None:
    replacement_pod = DeploymentPodEvidence(
        namespace="default",
        name="replacement-pod",
        uid="replacement-pod-uid",
        phase="Running",
        reason="",
        message="",
    )
    outcome = evaluate_deployment_outcome(
        _scale(),
        _observation(uid="replacement-uid", pods=(replacement_pod,)),
    )

    assert outcome.phase is DeploymentOutcomePhase.REPLACED
    assert "replaced" in outcome.summary.lower()
    assert outcome.pods == ()


def test_missing_observed_uid_is_incomplete_not_replaced() -> None:
    outcome = evaluate_deployment_outcome(_scale(), _observation(uid=""))

    assert outcome.phase is DeploymentOutcomePhase.INCOMPLETE
    assert "identity" in outcome.summary.lower()


def test_progress_deadline_and_non_ready_pod_are_stalled() -> None:
    pod = DeploymentPodEvidence(
        namespace="default",
        name="web-new-1",
        uid="pod-uid",
        phase="Pending",
        reason="ImagePullBackOff",
        message="image pull failed",
    )
    condition = DeploymentCondition(
        type="Progressing",
        status="False",
        reason="ProgressDeadlineExceeded",
        message="ReplicaSet web-new exceeded its progress deadline",
    )

    outcome = evaluate_deployment_outcome(
        _scale(),
        _observation(
            current=2,
            updated=2,
            ready=1,
            available=1,
            unavailable=2,
            conditions=(condition,),
            pods=(pod,),
        ),
    )

    assert outcome.phase is DeploymentOutcomePhase.STALLED
    assert outcome.pods == (pod,)
    assert "ProgressDeadlineExceeded" in outcome.summary


def test_replica_failure_is_stalled_without_pod_evidence() -> None:
    condition = DeploymentCondition(
        type="ReplicaFailure",
        status="True",
        reason="FailedCreate",
        message="quota exceeded",
    )

    outcome = evaluate_deployment_outcome(
        _scale(),
        _observation(conditions=(condition,)),
    )

    assert outcome.phase is DeploymentOutcomePhase.STALLED
    assert "FailedCreate" in outcome.summary
    assert "ReplicaFailure=True FailedCreate: quota exceeded" in outcome.evidence


def test_partial_evidence_is_retained_on_observing_outcome() -> None:
    outcome = evaluate_deployment_outcome(
        _scale(),
        _observation(ready=1, available=1, partial_evidence=True),
    )

    assert outcome.phase is DeploymentOutcomePhase.OBSERVING
    assert outcome.partial_evidence is True


def test_partial_related_evidence_allows_authoritative_scale_completion() -> None:
    outcome = evaluate_deployment_outcome(
        _scale(),
        _observation(partial_evidence=True),
    )

    assert outcome.phase is DeploymentOutcomePhase.COMPLETED
    assert outcome.partial_evidence is True


def test_partial_related_evidence_allows_authoritative_restart_completion() -> None:
    outcome = evaluate_deployment_outcome(
        _restart("accepted"),
        _observation(restart_stamp="accepted", partial_evidence=True),
    )

    assert outcome.phase is DeploymentOutcomePhase.COMPLETED
    assert outcome.partial_evidence is True


def test_ambiguous_pod_blocker_does_not_stall_operation() -> None:
    blocker = DeploymentPodEvidence(
        namespace="default",
        name="old-pod",
        uid="old-pod-uid",
        phase="Pending",
        reason="ImagePullBackOff",
        message="old rollout",
    )

    outcome = evaluate_deployment_outcome(
        _scale(),
        _observation(
            ready=1,
            available=1,
            pods=(blocker,),
            partial_evidence=True,
            pod_evidence_ambiguous=True,
        ),
    )

    assert outcome.phase is DeploymentOutcomePhase.OBSERVING


def test_truncated_current_pod_blocker_stalls_operation() -> None:
    blocker = DeploymentPodEvidence(
        namespace="default",
        name="current-pod",
        uid="current-pod-uid",
        phase="Pending",
        reason="ImagePullBackOff",
        message="current rollout",
    )

    outcome = evaluate_deployment_outcome(
        _scale(),
        _observation(ready=1, available=1, pods=(blocker,), partial_evidence=True),
    )

    assert outcome.phase is DeploymentOutcomePhase.STALLED


def test_restart_with_removed_marker_is_superseded() -> None:
    outcome = evaluate_deployment_outcome(
        _restart("accepted"),
        _observation(restart_stamp=None),
    )

    assert outcome.phase is DeploymentOutcomePhase.SUPERSEDED


def test_old_replica_set_pod_blocker_is_not_current_rollout_evidence() -> None:
    deployment = {
        "metadata": {"uid": "deploy-uid"},
        "spec": {"replicas": 1},
        "status": {},
    }
    replica_sets = (
        {
            "metadata": {
                "uid": "old-rs",
                "annotations": {"deployment.kubernetes.io/revision": "1"},
                "ownerReferences": [
                    {"kind": "Deployment", "uid": "deploy-uid", "controller": True}
                ],
            }
        },
        {
            "metadata": {
                "uid": "new-rs",
                "annotations": {"deployment.kubernetes.io/revision": "2"},
                "ownerReferences": [
                    {"kind": "Deployment", "uid": "deploy-uid", "controller": True}
                ],
            }
        },
    )
    pods = (
        {
            "metadata": {
                "name": "old-pod",
                "namespace": "default",
                "uid": "old-pod-uid",
                "ownerReferences": [{"kind": "ReplicaSet", "uid": "old-rs", "controller": True}],
            },
            "status": {
                "phase": "Pending",
                "containerStatuses": [
                    {"state": {"waiting": {"reason": "ImagePullBackOff", "message": "old"}}}
                ],
            },
        },
    )

    observation = normalize_deployment_observation(
        RawDeploymentOutcomeSnapshot(deployment, replica_sets, pods, False)
    )

    assert observation.pods == ()


def test_current_scale_to_zero_normalizes_omitted_status_counters() -> None:
    deployment = {
        "metadata": {"uid": "deploy-uid", "generation": 7},
        "spec": {"replicas": 0},
        "status": {"observedGeneration": 7},
    }

    observation = normalize_deployment_observation(
        RawDeploymentOutcomeSnapshot(deployment, (), (), False)
    )

    assert observation.current_replicas == 0
    assert observation.updated_replicas == 0
    assert observation.ready_replicas == 0
    assert observation.available_replicas == 0


def test_current_scale_to_zero_does_not_default_malformed_counter() -> None:
    deployment = {
        "metadata": {"uid": "deploy-uid", "generation": 7},
        "spec": {"replicas": 0},
        "status": {"observedGeneration": 7, "readyReplicas": "invalid"},
    }

    observation = normalize_deployment_observation(
        RawDeploymentOutcomeSnapshot(deployment, (), (), False)
    )

    assert observation.ready_replicas is None


def test_unknown_replica_set_revision_marks_evidence_partial() -> None:
    deployment = {
        "metadata": {"uid": "deploy-uid"},
        "spec": {"replicas": 1},
        "status": {},
    }
    replica_set = {
        "metadata": {
            "uid": "ambiguous-rs",
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "controller": True}],
        }
    }

    pod = {
        "metadata": {
            "name": "ambiguous-pod",
            "namespace": "default",
            "uid": "ambiguous-pod-uid",
            "ownerReferences": [{"kind": "ReplicaSet", "uid": "ambiguous-rs", "controller": True}],
        },
        "status": {
            "phase": "Pending",
            "containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff"}}}],
        },
    }
    observation = normalize_deployment_observation(
        RawDeploymentOutcomeSnapshot(deployment, (replica_set,), (pod,), False)
    )

    assert observation.partial_evidence
    assert observation.pods == ()


def test_truncated_replica_set_page_omits_ambiguous_pod_evidence() -> None:
    deployment = {
        "metadata": {"uid": "deploy-uid"},
        "spec": {"replicas": 1},
        "status": {},
    }
    replica_set = {
        "metadata": {
            "uid": "returned-rs",
            "annotations": {"deployment.kubernetes.io/revision": "2"},
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "controller": True}],
        }
    }
    pod = {
        "metadata": {
            "name": "possibly-old-pod",
            "namespace": "default",
            "uid": "possibly-old-pod-uid",
            "ownerReferences": [{"kind": "ReplicaSet", "uid": "returned-rs", "controller": True}],
        },
        "status": {
            "phase": "Pending",
            "containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff"}}}],
        },
    }

    observation = normalize_deployment_observation(
        RawDeploymentOutcomeSnapshot(
            deployment,
            (replica_set,),
            (pod,),
            True,
            pod_ownership_ambiguous=True,
        )
    )

    assert observation.partial_evidence is True
    assert observation.pods == ()


def test_mixed_known_and_malformed_replica_set_revisions_are_partial() -> None:
    deployment = {
        "metadata": {"uid": "deploy-uid"},
        "spec": {"replicas": 1},
        "status": {},
    }
    replica_sets = tuple(
        {
            "metadata": {
                "uid": uid,
                "annotations": {"deployment.kubernetes.io/revision": revision},
                "ownerReferences": [
                    {"kind": "Deployment", "uid": "deploy-uid", "controller": True}
                ],
            }
        }
        for uid, revision in (("known-rs", "2"), ("unknown-rs", "not-an-integer"))
    )

    observation = normalize_deployment_observation(
        RawDeploymentOutcomeSnapshot(deployment, replica_sets, (), False)
    )

    assert observation.partial_evidence
    assert observation.pods == ()
