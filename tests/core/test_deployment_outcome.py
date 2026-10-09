from korvid.core.deployment_outcome import (
    DeploymentCondition,
    DeploymentObservation,
    DeploymentOperationTarget,
    DeploymentOutcomePhase,
    DeploymentPodEvidence,
    DeploymentRestartIntent,
    DeploymentScaleIntent,
    evaluate_deployment_outcome,
)


def _target() -> DeploymentOperationTarget:
    return DeploymentOperationTarget(
        epoch=3,
        cluster_id="context-a|https://cluster.example",
        namespace="default",
        name="web",
        uid="deploy-uid",
    )


def _scale(replicas: int = 3) -> DeploymentScaleIntent:
    return DeploymentScaleIntent(target=_target(), replicas=replicas)


def _restart(stamp: str = "2026-10-09T12:00:00+00:00") -> DeploymentRestartIntent:
    return DeploymentRestartIntent(target=_target(), restarted_at=stamp)


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


def test_same_name_replacement_never_completes() -> None:
    outcome = evaluate_deployment_outcome(
        _scale(),
        _observation(uid="replacement-uid"),
    )

    assert outcome.phase is DeploymentOutcomePhase.REPLACED
    assert "replaced" in outcome.summary.lower()


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


def test_partial_evidence_is_retained_on_observing_outcome() -> None:
    outcome = evaluate_deployment_outcome(
        _scale(),
        _observation(ready=1, available=1, partial_evidence=True),
    )

    assert outcome.phase is DeploymentOutcomePhase.OBSERVING
    assert outcome.partial_evidence is True
