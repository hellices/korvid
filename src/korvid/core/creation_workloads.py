"""Project a workload and its children for creation diagnostics (#411).

A workload arrives whole, and its Pod template carries literal environment
values, commands and images. The projection keeps only identity, the
counters the view's header shows, a `FailedCreate` `ReplicaFailure`
condition, and the template's demand, so nothing else leaves the run:

- each container's and init container's name, requests and limits, in
  declaration order, and whether an init container is a sidecar;
- the Pod's `priorityClassName`, `activeDeadlineSeconds`, `runtimeClassName`
  and Pod-level requests and limits;
- a StatefulSet's claim templates: name, storage request and storage class,
  `None` when omitted and `""` when explicitly empty.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from korvid.core.creation_failures import Failure, Subject, condition_failure


@dataclass(frozen=True, slots=True)
class WorkloadKind:
    """A workload kind the view opens on, and the kind of child that records its Pods' failures."""

    kind: str
    group: str
    version: str
    resource: str
    child: str | None = None


WORKLOAD_KINDS: Mapping[str, WorkloadKind] = {
    kind.kind: kind
    for kind in (
        WorkloadKind("Deployment", "apps", "v1", "deployments", "ReplicaSet"),
        WorkloadKind("ReplicaSet", "apps", "v1", "replicasets"),
        WorkloadKind("StatefulSet", "apps", "v1", "statefulsets"),
        WorkloadKind("DaemonSet", "apps", "v1", "daemonsets"),
        WorkloadKind("Job", "batch", "v1", "jobs"),
        WorkloadKind("CronJob", "batch", "v1", "cronjobs", "Job"),
    )
}

#: A resource and its quantity, as written.
Quantities = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Counter:
    """One header counter, from the workload's spec or status; `None` when not set."""

    label: str
    value: str | None


@dataclass(frozen=True, slots=True)
class ContainerDemand:
    """One container's requests and limits; `category` is container, init or sidecar."""

    name: str
    category: str
    requests: Quantities
    limits: Quantities


@dataclass(frozen=True, slots=True)
class ClaimTemplate:
    """One StatefulSet claim template: storage class `None` when omitted, `""` when empty."""

    name: str
    storage: str | None
    storage_class: str | None


@dataclass(frozen=True, slots=True)
class PodTemplate:
    """What a new Pod of the workload asks for."""

    containers: tuple[ContainerDemand, ...]
    init_containers: tuple[ContainerDemand, ...]
    priority_class: str | None = None
    active_deadline_seconds: int | None = None
    runtime_class: str | None = None
    pod_requests: Quantities = ()
    pod_limits: Quantities = ()
    claims: tuple[ClaimTemplate, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkloadProjection:
    """The selected workload, as the creation diagnostic keeps it."""

    subject: Subject
    counters: tuple[Counter, ...]
    failure: Failure | None
    template: PodTemplate | None


@dataclass(frozen=True, slots=True)
class ChildProjection:
    """A ReplicaSet or Job the workload controls."""

    subject: Subject
    failure: Failure | None = None
    created_at: datetime | None = None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> str | None:
    number = _integer(value)
    return None if number is None else str(number)


def _quantity(value: object) -> str | None:
    return _number(value) if not isinstance(value, str) else _text(value)


def _quantities(value: object) -> Quantities:
    return tuple(
        sorted(
            (name, quantity)
            for name, raw in _mapping(value).items()
            if isinstance(name, str) and (quantity := _quantity(raw)) is not None
        )
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _subject(kind: str, item: Mapping[str, Any]) -> Subject | None:
    metadata = _mapping(item.get("metadata"))
    name, uid = _text(metadata.get("name")), _text(metadata.get("uid"))
    return None if name is None or uid is None else Subject(kind, name, uid)


def _replicas(spec: Mapping[str, Any], status: Mapping[str, Any]) -> tuple[Counter, ...]:
    created = _number(status.get("replicas")) or "0"
    return (Counter("desired", _number(spec.get("replicas"))), Counter("created", created))


def _counters(kind: str, spec: Mapping[str, Any], status: Mapping[str, Any]) -> tuple[Counter, ...]:
    if kind == "DaemonSet":
        return (
            Counter("desired", _number(status.get("desiredNumberScheduled"))),
            Counter("created", _number(status.get("currentNumberScheduled")) or "0"),
        )
    if kind == "Job":
        return (
            Counter("parallelism", _number(spec.get("parallelism"))),
            Counter("active", _number(status.get("active")) or "0"),
        )
    if kind == "CronJob":
        active = status.get("active")
        return (
            Counter("suspended", "yes" if spec.get("suspend") is True else "no"),
            Counter("active", str(len(active) if isinstance(active, list) else 0)),
            Counter("last schedule", _text(status.get("lastScheduleTime"))),
        )
    return _replicas(spec, status)


def _containers(entries: object, init: bool) -> tuple[ContainerDemand, ...]:
    demands: list[ContainerDemand] = []
    for raw in entries if isinstance(entries, list) else []:
        entry = _mapping(raw)
        name = _text(entry.get("name"))
        if name is None:
            continue
        category = "container"
        if init:
            category = "sidecar" if entry.get("restartPolicy") == "Always" else "init"
        resources = _mapping(entry.get("resources"))
        demands.append(
            ContainerDemand(
                name,
                category,
                _quantities(resources.get("requests")),
                _quantities(resources.get("limits")),
            )
        )
    return tuple(demands)


def _claims(templates: object) -> tuple[ClaimTemplate, ...]:
    claims: list[ClaimTemplate] = []
    for raw in templates if isinstance(templates, list) else []:
        template = _mapping(raw)
        name = _text(_mapping(template.get("metadata")).get("name"))
        if name is None:
            continue
        spec = _mapping(template.get("spec"))
        storage_class = spec.get("storageClassName")
        storage = _mapping(_mapping(spec.get("resources")).get("requests")).get("storage")
        claims.append(
            ClaimTemplate(
                name,
                _quantity(storage),
                storage_class if isinstance(storage_class, str) else None,
            )
        )
    return tuple(claims)


def _pod_spec(kind: str, spec: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if kind == "CronJob":
        spec = _mapping(_mapping(spec.get("jobTemplate")).get("spec"))
    template = spec.get("template")
    return _mapping(_mapping(template).get("spec")) if isinstance(template, Mapping) else None


def _template(kind: str, spec: Mapping[str, Any]) -> PodTemplate | None:
    pod = _pod_spec(kind, spec)
    if pod is None:
        return None
    resources = _mapping(pod.get("resources"))
    return PodTemplate(
        _containers(pod.get("containers"), init=False),
        _containers(pod.get("initContainers"), init=True),
        _text(pod.get("priorityClassName")),
        _integer(pod.get("activeDeadlineSeconds")),
        _text(pod.get("runtimeClassName")),
        _quantities(resources.get("requests")),
        _quantities(resources.get("limits")),
        _claims(spec.get("volumeClaimTemplates")) if kind == "StatefulSet" else (),
    )


def project_workload(kind: str, item: Mapping[str, Any]) -> WorkloadProjection | None:
    """The workload `item` of `kind`, or `None` without a name and UID.

    Raises:
        ValueError: if `kind` is not one of `WORKLOAD_KINDS`.
    """
    if kind not in WORKLOAD_KINDS:
        raise ValueError(f"{kind!r} is not a workload kind the creation diagnostic reads")
    subject = _subject(kind, item)
    if subject is None:
        return None
    spec, status = _mapping(item.get("spec")), _mapping(item.get("status"))
    return WorkloadProjection(
        subject,
        _counters(kind, spec, status),
        condition_failure(subject, status),
        _template(kind, spec),
    )


def _controller_uid(item: Mapping[str, Any]) -> str | None:
    owners = _mapping(item.get("metadata")).get("ownerReferences")
    for raw in owners if isinstance(owners, list) else []:
        owner = _mapping(raw)
        if owner.get("controller") is True:
            return _text(owner.get("uid"))
    return None


def _newest_first(child: ChildProjection) -> tuple[bool, float]:
    at = child.created_at
    return (at is None, -at.timestamp() if at is not None else 0.0)


def project_children(
    kind: str, items: Iterable[object], parent_uid: str
) -> tuple[ChildProjection, ...]:
    """The `kind` objects in `items` whose controller is `parent_uid`, newest first."""
    children: list[ChildProjection] = []
    for item in items:
        if not isinstance(item, Mapping) or _controller_uid(item) != parent_uid:
            continue
        subject = _subject(kind, item)
        if subject is None:
            continue
        created = _timestamp(_mapping(item.get("metadata")).get("creationTimestamp"))
        failure = condition_failure(subject, _mapping(item.get("status")))
        children.append(ChildProjection(subject, failure, created))
    return tuple(sorted(children, key=_newest_first))
