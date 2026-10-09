"""Pure Deployment operation outcome predicates.

The accepted write and the later controller observation are deliberately
separate. This module performs no I/O and never treats missing evidence as a
zero value or a successful rollout.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from korvid.core.redaction import RedactionRecord, redact_text
from korvid.k8s.deployment_outcomes import RawDeploymentOutcomeSnapshot


class DeploymentOutcomePhase(StrEnum):
    """Lifecycle phase for one accepted Deployment operation."""

    ACCEPTED = "accepted"
    OBSERVING = "observing"
    COMPLETED = "completed"
    STALLED = "stalled"
    SUPERSEDED = "superseded"
    REPLACED = "replaced"
    INCOMPLETE = "incomplete"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class DeploymentOperationTarget:
    """Stable identity captured for an accepted Deployment operation."""

    epoch: int
    cluster_id: str
    namespace: str
    name: str
    uid: str


@dataclass(frozen=True, slots=True)
class DeploymentScaleIntent:
    """Accepted request to set a Deployment's replica count."""

    target: DeploymentOperationTarget
    replicas: int


@dataclass(frozen=True, slots=True)
class DeploymentRestartIntent:
    """Accepted request carrying the exact rollout-restart annotation."""

    target: DeploymentOperationTarget
    restarted_at: str


DeploymentOperationIntent = DeploymentScaleIntent | DeploymentRestartIntent


@dataclass(frozen=True, slots=True)
class DeploymentCondition:
    """One bounded controller condition relevant to rollout progress."""

    type: str
    status: str
    reason: str
    message: str


@dataclass(frozen=True, slots=True)
class DeploymentPodEvidence:
    """Identity-safe evidence from one non-ready Pod."""

    namespace: str
    name: str
    uid: str
    phase: str
    reason: str
    message: str


@dataclass(frozen=True, slots=True)
class DeploymentObservation:
    """One authoritative, normalized Deployment observation."""

    uid: str
    generation: int | None
    observed_generation: int | None
    desired_replicas: int | None
    current_replicas: int | None
    updated_replicas: int | None
    ready_replicas: int | None
    available_replicas: int | None
    unavailable_replicas: int | None
    restart_stamp: str | None
    conditions: tuple[DeploymentCondition, ...] = ()
    pods: tuple[DeploymentPodEvidence, ...] = ()
    partial_evidence: bool = False


@dataclass(frozen=True, slots=True)
class DeploymentOutcome:
    """Evaluated status and bounded evidence for one operation."""

    phase: DeploymentOutcomePhase
    summary: str
    evidence: tuple[str, ...] = ()
    pods: tuple[DeploymentPodEvidence, ...] = ()
    partial_evidence: bool = False


_POD_BLOCKERS = frozenset(
    {
        "CrashLoopBackOff",
        "CreateContainerConfigError",
        "CreateContainerError",
        "ErrImagePull",
        "ImagePullBackOff",
        "InvalidImageName",
        "PodScheduledFalse",
        "Unschedulable",
    }
)
_RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
_RELEVANT_CONDITIONS = frozenset({"Available", "Progressing", "ReplicaFailure"})
_MAX_TEXT_CHARS = 240


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _entries(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bounded_text(value: object, path: str) -> str:
    records: list[RedactionRecord] = []
    text = redact_text(str(value or ""), path, records)
    return " ".join(text.split())[:_MAX_TEXT_CHARS]


def _controller_owner_uid(manifest: Mapping[str, Any], kind: str) -> str | None:
    metadata = _mapping(manifest.get("metadata"))
    for owner in _entries(metadata.get("ownerReferences")):
        if owner.get("kind") == kind and owner.get("controller") is True and owner.get("uid"):
            return str(owner["uid"])
    return None


def _condition_entries(status: Mapping[str, Any]) -> tuple[DeploymentCondition, ...]:
    conditions: list[DeploymentCondition] = []
    for index, condition in enumerate(_entries(status.get("conditions"))):
        condition_type = str(condition.get("type") or "")
        if condition_type not in _RELEVANT_CONDITIONS:
            continue
        conditions.append(
            DeploymentCondition(
                type=condition_type,
                status=str(condition.get("status") or ""),
                reason=_bounded_text(
                    condition.get("reason"), f"deployment.status.conditions[{index}].reason"
                ),
                message=_bounded_text(
                    condition.get("message"), f"deployment.status.conditions[{index}].message"
                ),
            )
        )
    return tuple(conditions)


def _pod_problem(status: Mapping[str, Any]) -> tuple[str, str]:
    reason = _bounded_text(status.get("reason"), "pod.status.reason")
    message = _bounded_text(status.get("message"), "pod.status.message")
    if reason:
        return reason, message
    for condition in _entries(status.get("conditions")):
        if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
            return (
                _bounded_text(condition.get("reason"), "pod.status.conditions.reason")
                or "PodScheduledFalse",
                _bounded_text(condition.get("message"), "pod.status.conditions.message"),
            )
    for field in ("initContainerStatuses", "containerStatuses"):
        for container in _entries(status.get(field)):
            state = _mapping(container.get("state"))
            waiting = _mapping(state.get("waiting"))
            waiting_reason = _bounded_text(waiting.get("reason"), f"pod.status.{field}.reason")
            if waiting_reason not in {"", "ContainerCreating", "PodInitializing"}:
                return (
                    waiting_reason,
                    _bounded_text(waiting.get("message"), f"pod.status.{field}.message"),
                )
            terminated = _mapping(state.get("terminated"))
            exit_code = _integer(terminated.get("exitCode"))
            if exit_code not in {None, 0}:
                return (
                    _bounded_text(terminated.get("reason"), f"pod.status.{field}.reason")
                    or f"ExitCode{exit_code}",
                    _bounded_text(terminated.get("message"), f"pod.status.{field}.message"),
                )
    return "", ""


def _pod_evidence(
    pods: tuple[dict[str, Any], ...],
    replica_set_uids: frozenset[str],
) -> list[DeploymentPodEvidence]:
    evidence: list[DeploymentPodEvidence] = []
    for pod in pods:
        if _controller_owner_uid(pod, "ReplicaSet") not in replica_set_uids:
            continue
        metadata = _mapping(pod.get("metadata"))
        status = _mapping(pod.get("status"))
        reason, message = _pod_problem(status)
        if not reason:
            continue
        uid = str(metadata.get("uid") or "")
        name = str(metadata.get("name") or "")
        if not uid or not name:
            continue
        evidence.append(
            DeploymentPodEvidence(
                namespace=str(metadata.get("namespace") or ""),
                name=name,
                uid=uid,
                phase=str(status.get("phase") or ""),
                reason=reason,
                message=message,
            )
        )
    return sorted(evidence, key=lambda item: (item.namespace, item.name, item.uid))


def normalize_deployment_observation(
    raw: RawDeploymentOutcomeSnapshot,
    *,
    max_pod_evidence: int = 5,
) -> DeploymentObservation:
    """Normalize and redact one bounded Kubernetes snapshot."""

    if max_pod_evidence < 1:
        raise ValueError("max_pod_evidence must be positive")
    deployment = raw.deployment
    metadata = _mapping(deployment.get("metadata"))
    spec = _mapping(deployment.get("spec"))
    status = _mapping(deployment.get("status"))
    live_uid = str(metadata.get("uid") or "")
    replica_set_uids = frozenset(
        str(_mapping(item.get("metadata")).get("uid"))
        for item in raw.replica_sets
        if _controller_owner_uid(item, "Deployment") == live_uid
        and _mapping(item.get("metadata")).get("uid")
    )
    all_pod_evidence = _pod_evidence(raw.pods, replica_set_uids)
    template = _mapping(spec.get("template"))
    template_metadata = _mapping(template.get("metadata"))
    annotations = _mapping(template_metadata.get("annotations"))
    return DeploymentObservation(
        uid=live_uid,
        generation=_integer(metadata.get("generation")),
        observed_generation=_integer(status.get("observedGeneration")),
        desired_replicas=_integer(spec.get("replicas")),
        current_replicas=_integer(status.get("replicas")),
        updated_replicas=_integer(status.get("updatedReplicas")),
        ready_replicas=_integer(status.get("readyReplicas")),
        available_replicas=_integer(status.get("availableReplicas")),
        unavailable_replicas=_integer(status.get("unavailableReplicas")),
        restart_stamp=(
            str(annotations[_RESTART_ANNOTATION]) if annotations.get(_RESTART_ANNOTATION) else None
        ),
        conditions=_condition_entries(status),
        pods=tuple(all_pod_evidence[:max_pod_evidence]),
        partial_evidence=raw.partial or len(all_pod_evidence) > max_pod_evidence,
    )


def _condition_failure(
    conditions: tuple[DeploymentCondition, ...],
) -> DeploymentCondition | None:
    for condition in conditions:
        if condition.type == "ReplicaFailure" and condition.status == "True":
            return condition
        if condition.type == "Progressing" and condition.status == "False" and condition.reason:
            return condition
    return None


def _pod_failure(pods: tuple[DeploymentPodEvidence, ...]) -> DeploymentPodEvidence | None:
    return next((pod for pod in pods if pod.reason in _POD_BLOCKERS), None)


def _generation_current(observation: DeploymentObservation) -> bool:
    return (
        observation.generation is not None
        and observation.observed_generation is not None
        and observation.observed_generation >= observation.generation
    )


def _replicas_converged(observation: DeploymentObservation, desired: int) -> bool:
    required = (
        observation.current_replicas,
        observation.updated_replicas,
        observation.ready_replicas,
        observation.available_replicas,
    )
    return (
        observation.desired_replicas == desired
        and all(value == desired for value in required)
        and observation.unavailable_replicas in {None, 0}
    )


def _progress_evidence(observation: DeploymentObservation) -> tuple[str, ...]:
    return (
        f"generation {observation.observed_generation}/{observation.generation}",
        (
            "replicas "
            f"current={observation.current_replicas} "
            f"updated={observation.updated_replicas} "
            f"ready={observation.ready_replicas} "
            f"available={observation.available_replicas} "
            f"desired={observation.desired_replicas}"
        ),
    )


def _stalled_outcome(observation: DeploymentObservation) -> DeploymentOutcome | None:
    condition = _condition_failure(observation.conditions)
    pod = _pod_failure(observation.pods)
    if condition is None and pod is None:
        return None
    if condition is not None:
        detail = condition.reason or condition.type
        summary = f"Deployment rollout stalled: {detail}"
    elif pod is not None:
        summary = f"Deployment rollout stalled: Pod {pod.name} reports {pod.reason}"
    else:
        return None
    return DeploymentOutcome(
        phase=DeploymentOutcomePhase.STALLED,
        summary=summary,
        evidence=_progress_evidence(observation),
        pods=observation.pods,
        partial_evidence=observation.partial_evidence,
    )


def _observing_outcome(observation: DeploymentObservation) -> DeploymentOutcome:
    summary = (
        "Waiting for the Deployment controller to observe the current generation"
        if not _generation_current(observation)
        else "Waiting for all requested Deployment replicas to converge"
    )
    return DeploymentOutcome(
        phase=DeploymentOutcomePhase.OBSERVING,
        summary=summary,
        evidence=_progress_evidence(observation),
        pods=observation.pods,
        partial_evidence=observation.partial_evidence,
    )


def _evaluate_scale(
    intent: DeploymentScaleIntent, observation: DeploymentObservation
) -> DeploymentOutcome:
    if observation.desired_replicas is not None and observation.desired_replicas != intent.replicas:
        return DeploymentOutcome(
            phase=DeploymentOutcomePhase.SUPERSEDED,
            summary=(
                "Scale request was superseded: "
                f"live target is {observation.desired_replicas}, requested {intent.replicas}"
            ),
            evidence=_progress_evidence(observation),
            pods=observation.pods,
            partial_evidence=observation.partial_evidence,
        )
    stalled = _stalled_outcome(observation)
    if stalled is not None:
        return stalled
    if _generation_current(observation) and _replicas_converged(observation, intent.replicas):
        return DeploymentOutcome(
            phase=DeploymentOutcomePhase.COMPLETED,
            summary=f"Deployment converged at {intent.replicas} replicas",
            evidence=_progress_evidence(observation),
            partial_evidence=observation.partial_evidence,
        )
    return _observing_outcome(observation)


def _evaluate_restart(
    intent: DeploymentRestartIntent, observation: DeploymentObservation
) -> DeploymentOutcome:
    if observation.restart_stamp is not None and observation.restart_stamp != intent.restarted_at:
        return DeploymentOutcome(
            phase=DeploymentOutcomePhase.SUPERSEDED,
            summary="Rollout restart was superseded by a later restart marker",
            evidence=_progress_evidence(observation),
            pods=observation.pods,
            partial_evidence=observation.partial_evidence,
        )
    stalled = _stalled_outcome(observation)
    if stalled is not None:
        return stalled
    desired = observation.desired_replicas
    if (
        observation.restart_stamp == intent.restarted_at
        and desired is not None
        and _generation_current(observation)
        and _replicas_converged(observation, desired)
    ):
        return DeploymentOutcome(
            phase=DeploymentOutcomePhase.COMPLETED,
            summary="Deployment rollout restart converged",
            evidence=_progress_evidence(observation),
            partial_evidence=observation.partial_evidence,
        )
    return _observing_outcome(observation)


def evaluate_deployment_outcome(
    intent: DeploymentOperationIntent,
    observation: DeploymentObservation,
) -> DeploymentOutcome:
    """Evaluate one observation without inventing missing evidence."""

    if observation.uid != intent.target.uid:
        return DeploymentOutcome(
            phase=DeploymentOutcomePhase.REPLACED,
            summary="Deployment was replaced by a different object with the same name",
            evidence=(f"expected uid {intent.target.uid}", f"observed uid {observation.uid}"),
            pods=observation.pods,
            partial_evidence=observation.partial_evidence,
        )
    if isinstance(intent, DeploymentScaleIntent):
        return _evaluate_scale(intent, observation)
    return _evaluate_restart(intent, observation)
