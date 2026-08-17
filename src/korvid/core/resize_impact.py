from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import DecimalException
from typing import Any

from korvid.k8s.models import parse_quantity

ResizeResourceChanges = Mapping[str, Mapping[str, Mapping[str, str]]]


@dataclass(frozen=True, slots=True)
class ResizeImpactContext:
    cpu_changed: bool
    memory_request_changed: bool
    memory_limit_changed: bool
    restart_required: bool
    cpu_restart_required: bool
    memory_restart_required: bool
    restart_policy_unknown: bool
    all_changed_resources_not_required: bool
    memory_limit_decreased: bool
    memory_limit_decrease_not_required: bool
    memory_limit_assessment_unknown: bool


def classify_pod_resize(
    manifest: Mapping[str, object], changes: ResizeResourceChanges
) -> ResizeImpactContext:
    containers = _containers_by_name(manifest)
    applied_containers = _applied_containers_by_name(manifest)
    changed_resources = [
        (name, resource)
        for name, sections in changes.items()
        for values in sections.values()
        for resource in values
        if resource in {"cpu", "memory"}
    ]
    policies = [
        _restart_policy(containers.get(name), resource) for name, resource in changed_resources
    ]
    memory_decreased, memory_decrease_not_required, memory_unknown = _memory_limit_impact(
        containers, applied_containers, changes
    )
    policy_unknown = any(policy is None for policy in policies)
    restart_required = any(policy == "RestartContainer" for policy in policies)
    cpu_restart_required = any(
        resource == "cpu" and policy == "RestartContainer"
        for (_, resource), policy in zip(changed_resources, policies, strict=True)
    )
    memory_restart_required = any(
        resource == "memory" and policy == "RestartContainer"
        for (_, resource), policy in zip(changed_resources, policies, strict=True)
    )
    return ResizeImpactContext(
        cpu_changed=any(resource == "cpu" for _, resource in changed_resources),
        memory_request_changed=any(
            "memory" in sections.get("requests", {}) for sections in changes.values()
        ),
        memory_limit_changed=any(
            "memory" in sections.get("limits", {}) for sections in changes.values()
        ),
        restart_required=restart_required,
        cpu_restart_required=cpu_restart_required,
        memory_restart_required=memory_restart_required,
        restart_policy_unknown=policy_unknown,
        all_changed_resources_not_required=bool(policies)
        and not restart_required
        and not policy_unknown,
        memory_limit_decreased=memory_decreased,
        memory_limit_decrease_not_required=memory_decrease_not_required,
        memory_limit_assessment_unknown=memory_unknown,
    )


def _memory_limit_impact(
    containers: Mapping[str, Mapping[str, Any]],
    applied_containers: Mapping[str, Mapping[str, Any]],
    changes: ResizeResourceChanges,
) -> tuple[bool, bool, bool]:
    decreased_any = False
    decrease_not_required = False
    unknown = False
    for name, sections in changes.items():
        desired = sections.get("limits", {}).get("memory")
        if desired is None:
            continue
        container = containers.get(name)
        current_known, current = _applied_limit_state(applied_containers.get(name), "memory")
        if not current_known:
            spec_known, spec_current = _limit_state(container, "memory")
            if spec_known:
                current_known, current = spec_known, spec_current
        policy = _restart_policy(container, "memory")
        if not current_known:
            unknown = True
            continue
        try:
            desired_value = parse_quantity(desired)
            decreased = current is None or desired_value < parse_quantity(current)
        except (DecimalException, ValueError):
            unknown = True
            continue
        decreased_any = decreased_any or decreased
        decrease_not_required = decrease_not_required or (decreased and policy == "NotRequired")
        unknown = unknown or (decreased and policy is None)
    return decreased_any, decrease_not_required, unknown


def _containers_by_name(manifest: Mapping[str, object]) -> dict[str, Mapping[str, Any]]:
    spec = manifest.get("spec")
    if not isinstance(spec, Mapping):
        return {}
    return _named_mappings(spec.get("containers"))


def _applied_containers_by_name(
    manifest: Mapping[str, object],
) -> dict[str, Mapping[str, Any]]:
    status = manifest.get("status")
    if not isinstance(status, Mapping):
        return {}
    return _named_mappings(status.get("containerStatuses"))


def _named_mappings(raw: object) -> dict[str, Mapping[str, Any]]:
    if not isinstance(raw, list):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str):
            result[name] = item
    return result


def _restart_policy(container: Mapping[str, Any] | None, resource: str) -> str | None:
    if container is None:
        return None
    raw = container.get("resizePolicy")
    if raw is None:
        return "NotRequired"
    if not isinstance(raw, list):
        return None
    malformed = False
    for item in raw:
        if not isinstance(item, Mapping):
            malformed = True
            continue
        resource_name = item.get("resourceName")
        policy = item.get("restartPolicy")
        if (
            not isinstance(resource_name, str)
            or not isinstance(policy, str)
            or policy not in {"NotRequired", "RestartContainer"}
        ):
            malformed = True
            continue
        if resource_name != resource:
            continue
        return policy
    if malformed:
        return None
    # Kubernetes falls through to NotRequired when no per-resource policy exists.
    return "NotRequired"


def _limit_state(container: Mapping[str, Any] | None, resource: str) -> tuple[bool, str | None]:
    if container is None:
        return False, None
    resources = container.get("resources")
    if resources is None:
        return True, None
    if not isinstance(resources, Mapping):
        return False, None
    limits = resources.get("limits")
    if limits is None:
        return True, None
    if not isinstance(limits, Mapping):
        return False, None
    value = limits.get(resource)
    if value is None:
        return True, None
    return (True, value) if isinstance(value, str) else (False, None)


def _applied_limit_state(
    container: Mapping[str, Any] | None, resource: str
) -> tuple[bool, str | None]:
    if container is None:
        return False, None
    resources = container.get("resources")
    if not isinstance(resources, Mapping) or not resources:
        return False, None
    return _limit_state(container, resource)
