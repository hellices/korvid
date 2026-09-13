"""Initial Pulse rules: explicit current symptoms, never inferred root causes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from datetime import datetime
from typing import Any

from korvid.core.findings import Evidence, Finding, ResourceIdentity
from korvid.core.pulse import PulseItem, PulseRule, PulseTarget

_STARTING_REASONS = frozenset({"ContainerCreating", "PodInitializing"})
_CONTAINER_GROUPS = {
    "containers": "containerStatuses",
    "initContainers": "initContainerStatuses",
    "ephemeralContainers": "ephemeralContainerStatuses",
}
_CONTAINER_FIELDS = tuple(_CONTAINER_GROUPS.values())
_CONDITION_STATES = frozenset({"True", "False", "Unknown"})
_DEPLOYMENT_FAILURES = {
    ("ReplicaFailure", "True"): "pulse.deployment.replica-failure",
    ("Progressing", "False"): "pulse.deployment.progressing-failed",
}


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _entries(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(entry for entry in value if isinstance(entry, dict))


def _display(value: object) -> str:
    return str(value) if isinstance(value, (str, int, float, bool)) else ""


def _target(resource: dict[str, Any], kind: str, group: str = "") -> PulseTarget:
    metadata = _mapping(resource.get("metadata"))
    return PulseTarget(
        group,
        kind,
        metadata.get("namespace", ""),
        metadata.get("name", ""),
        metadata.get("uid"),
    )


def _item(
    target: PulseTarget,
    *,
    source: str,
    rule_id: str,
    reason: str,
    message: str,
    observed_at: datetime,
    fields: tuple[tuple[str, object], ...],
    slot: str = "",
) -> PulseItem:
    primary = ResourceIdentity(target.kind, target.namespace, target.name, target.uid or "")
    evidence = tuple(Evidence(primary, field, _display(value)) for field, value in fields)
    finding = Finding(
        rule_id,
        "1",
        "warning",
        "high",
        primary,
        (),
        evidence,
        message,
        (f"Inspect the current {target.kind} status and related Warning events.",),
    )
    identity = (
        target.group,
        target.kind,
        target.namespace,
        target.name,
        target.uid,
        rule_id,
        slot,
    )
    key = rule_id + ":" + hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()
    return PulseItem(
        key,
        "current",
        target,
        reason,
        message,
        observed_at,
        finding=finding,
        source=source,
    )


def _condition(status: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(
        (
            condition
            for condition in _entries(status.get("conditions"))
            if condition.get("type") == kind
        ),
        {},
    )


def _conditions_assessable(value: object) -> bool:
    if not isinstance(value, (list, tuple)):
        return False
    seen: set[str] = set()
    for condition in value:
        if not isinstance(condition, dict):
            return False
        kind = condition.get("type")
        state = condition.get("status")
        if not isinstance(kind, str) or not kind or kind in seen:
            return False
        if not isinstance(state, str) or state not in _CONDITION_STATES:
            return False
        seen.add(kind)
    return True


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _container_assessable(value: object) -> bool:
    container = _mapping(value)
    if not isinstance(container.get("name"), str) or not container["name"]:
        return False
    if "ready" in container and not isinstance(container["ready"], bool):
        return False
    state = _mapping(container.get("state"))
    active = [kind for kind in ("running", "waiting", "terminated") if kind in state]
    if len(active) != 1 or not isinstance(state[active[0]], dict):
        return False
    details = state[active[0]]
    if "reason" in details and not isinstance(details["reason"], str):
        return False
    if active[0] == "terminated":
        return _integer(details.get("exitCode")) and (
            "signal" not in details or _integer(details["signal"])
        )
    return True


def _container_names(value: object) -> set[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    names: set[str] = set()
    for container in value:
        if not isinstance(container, dict):
            return None
        name = container.get("name")
        if not isinstance(name, str) or not name or name in names:
            return None
        names.add(name)
    return names


def _containers_assessable(value: object, declared: object) -> bool:
    declared_names = _container_names(declared)
    return (
        declared_names is not None
        and isinstance(value, (list, tuple))
        and _container_names(value) == declared_names
        and all(_container_assessable(entry) for entry in value)
    )


def _pod_assessable(resource: dict[str, Any]) -> bool:
    status = _mapping(resource.get("status"))
    phase = status.get("phase")
    if phase == "Succeeded":
        return True
    if not isinstance(phase, str) or phase not in {"Pending", "Running", "Unknown", "Failed"}:
        return False
    if "conditions" in status and not _conditions_assessable(status["conditions"]):
        return False
    spec = _mapping(resource.get("spec"))
    if not _container_names(spec.get("containers")):
        return False
    if not all(
        _containers_assessable(status.get(status_field, ()), spec.get(spec_field, ()))
        for spec_field, status_field in _CONTAINER_GROUPS.items()
    ):
        return False
    if phase in {"Running", "Unknown"}:
        return _condition(status, "Ready").get("status") in _CONDITION_STATES
    return True


def _condition_item(
    target: PulseTarget,
    condition: dict[str, Any],
    observed_at: datetime,
    *,
    source: str,
    rule_id: str,
    fallback: str,
    fields: tuple[tuple[str, object], ...] = (),
) -> PulseItem:
    kind = _display(condition.get("type"))
    path = f"status.conditions[{kind}]"
    return _item(
        target,
        source=source,
        rule_id=rule_id,
        reason=_display(condition.get("reason")) or fallback,
        message=_display(condition.get("message"))
        or f"{target.kind} reports {kind}={condition.get('status')}.",
        observed_at=observed_at,
        fields=(
            *fields,
            (f"{path}.status", condition.get("status")),
            (f"{path}.reason", condition.get("reason")),
            (f"{path}.message", condition.get("message")),
        ),
    )


def _container_entries(status: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for field in _CONTAINER_FIELDS:
        for index, container in enumerate(_entries(status.get(field))):
            name = _display(container.get("name")) or str(index)
            yield f"status.{field}[{index}]", f"{field}:{name}", container


def _nonzero(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value != 0


def _termination_failed(termination: dict[str, Any]) -> bool:
    reason = _display(termination.get("reason"))
    return (
        reason not in {"", "Completed"}
        or _nonzero(termination.get("exitCode"))
        or _nonzero(termination.get("signal"))
    )


def _container_item(
    target: PulseTarget,
    path: str,
    slot: str,
    container: dict[str, Any],
    observed_at: datetime,
) -> PulseItem | None:
    state = _mapping(container.get("state"))
    waiting = state.get("waiting")
    if isinstance(waiting, dict) and _display(waiting.get("reason")) not in _STARTING_REASONS:
        return _item(
            target,
            source="pods",
            rule_id="pulse.pod.container-waiting",
            reason=_display(waiting.get("reason")) or "ContainerWaiting",
            message=_display(waiting.get("message")) or "A current container state is waiting.",
            observed_at=observed_at,
            slot=slot,
            fields=(
                (f"{path}.name", container.get("name")),
                (f"{path}.state.waiting.reason", waiting.get("reason")),
                (f"{path}.state.waiting.message", waiting.get("message")),
            ),
        )
    terminated = _mapping(state.get("terminated"))
    if terminated and _termination_failed(terminated):
        return _item(
            target,
            source="pods",
            rule_id="pulse.pod.container-terminated",
            reason=_display(terminated.get("reason")) or "ContainerTerminated",
            message=_display(terminated.get("message"))
            or "A current container termination reports failure.",
            observed_at=observed_at,
            slot=slot,
            fields=(
                (f"{path}.name", container.get("name")),
                (f"{path}.state.terminated.reason", terminated.get("reason")),
                (f"{path}.state.terminated.exitCode", terminated.get("exitCode")),
                (f"{path}.state.terminated.signal", terminated.get("signal")),
                (f"{path}.state.terminated.message", terminated.get("message")),
            ),
        )
    return None


def _containers_starting(status: dict[str, Any]) -> bool:
    return any(
        _display(_mapping(_mapping(container.get("state")).get("waiting")).get("reason"))
        in _STARTING_REASONS
        for field in ("containerStatuses", "initContainerStatuses")
        for container in _entries(status.get(field))
    )


def _pod_items(resource: dict[str, Any], observed_at: datetime) -> tuple[PulseItem, ...]:
    status = _mapping(resource.get("status"))
    phase = _display(status.get("phase"))
    if phase == "Succeeded":
        return ()
    target = _target(resource, "Pod")
    containers = tuple(
        item
        for path, slot, container in _container_entries(status)
        if (item := _container_item(target, path, slot, container, observed_at)) is not None
    )
    if phase == "Failed":
        return (
            _item(
                target,
                source="pods",
                rule_id="pulse.pod.failed",
                reason=_display(status.get("reason")) or "Failed",
                message=_display(status.get("message")) or "Pod phase is Failed.",
                observed_at=observed_at,
                fields=(
                    ("status.phase", phase),
                    ("status.reason", status.get("reason")),
                    ("status.message", status.get("message")),
                ),
            ),
            *containers,
        )
    scheduled = _condition(status, "PodScheduled")
    if scheduled.get("status") == "False":
        return (
            _condition_item(
                target,
                scheduled,
                observed_at,
                source="pods",
                rule_id="pulse.pod.unschedulable",
                fallback="Unschedulable",
            ),
            *containers,
        )
    if containers or _containers_starting(status):
        return containers
    return _pod_readiness(target, status, observed_at)


def _pod_readiness(
    target: PulseTarget,
    status: dict[str, Any],
    observed_at: datetime,
) -> tuple[PulseItem, ...]:
    phase = _display(status.get("phase"))
    if phase == "Pending":
        return (
            _item(
                target,
                source="pods",
                rule_id="pulse.pod.pending",
                reason="Pending",
                message=_display(status.get("message")) or "Pod phase is Pending.",
                observed_at=observed_at,
                fields=(("status.phase", phase), ("status.message", status.get("message"))),
            ),
        )
    ready = _condition(status, "Ready")
    if phase in {"Running", "Unknown"} and _display(ready.get("status")) in {"False", "Unknown"}:
        return (
            _condition_item(
                target,
                ready,
                observed_at,
                source="pods",
                rule_id="pulse.pod.not-ready",
                fallback="NotReady",
            ),
        )
    return ()


def _generation_current(resource: dict[str, Any]) -> bool:
    generation = _mapping(resource.get("metadata")).get("generation")
    observed = _mapping(resource.get("status")).get("observedGeneration")
    return (
        isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation >= 0
        and isinstance(observed, int)
        and not isinstance(observed, bool)
        and observed >= generation
    )


def _deployment_items(resource: dict[str, Any], observed_at: datetime) -> tuple[PulseItem, ...]:
    if not _generation_current(resource):
        return ()
    status = _mapping(resource.get("status"))
    target = _target(resource, "Deployment", "apps")
    items = []
    for condition in _entries(status.get("conditions")):
        rule_id = _DEPLOYMENT_FAILURES.get(
            (_display(condition.get("type")), _display(condition.get("status")))
        )
        if rule_id is not None:
            items.append(
                _condition_item(
                    target,
                    condition,
                    observed_at,
                    source="deployments",
                    rule_id=rule_id,
                    fallback=_display(condition.get("type")),
                    fields=(
                        (
                            "metadata.generation",
                            _mapping(resource.get("metadata")).get("generation"),
                        ),
                        ("status.observedGeneration", status.get("observedGeneration")),
                    ),
                )
            )
    return tuple(items)


class PodPulseRule(PulseRule):
    """Observe Pod phase, scheduling, readiness, and current container states."""

    @property
    def source(self) -> str:
        return "pods"

    def can_clear(self, resource: dict[str, Any]) -> bool:
        """Require usable current phase, condition, and container evidence."""
        return _pod_assessable(resource)

    def evaluate(
        self,
        objects: Sequence[dict[str, Any]],
        observed_at: datetime,
    ) -> tuple[PulseItem, ...]:
        """Report active symptoms, excluding completed Pods and historical state."""
        return tuple(item for resource in objects for item in _pod_items(resource, observed_at))


class DeploymentPulseRule(PulseRule):
    """Observe explicit failure conditions acknowledged for this generation."""

    @property
    def source(self) -> str:
        return "deployments"

    def can_clear(self, resource: dict[str, Any]) -> bool:
        """Require an acknowledged generation and assessable failure conditions."""
        conditions = _mapping(resource.get("status")).get("conditions")
        return (
            _generation_current(resource)
            and _conditions_assessable(conditions)
            and all(
                condition.get("status") != "Unknown"
                for condition in _entries(conditions)
                if condition.get("type") in {"ReplicaFailure", "Progressing"}
            )
        )

    def evaluate(
        self,
        objects: Sequence[dict[str, Any]],
        observed_at: datetime,
    ) -> tuple[PulseItem, ...]:
        """Report explicit Deployment failures without requiring any Pods."""
        return tuple(
            item for resource in objects for item in _deployment_items(resource, observed_at)
        )
