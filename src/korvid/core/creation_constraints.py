"""Explain why a workload's Pods are not being created (#411).

One run reads, in the workload's namespace and under one run token:

- `workload`: the workload, by name, with the selected row's UID;
- `children`: a Deployment's ReplicaSets or a CronJob's Jobs, whose own
  failures are their Pods' failures;
- `events`: the namespace's Events, filtered to `FailedCreate` about the
  workload or its children, matched by UID;
- `quotas` and `limit-ranges`: the namespace's ResourceQuotas and
  LimitRanges.

Every object is projected at once, so no template value leaves the run.
Events can confirm a recorded failure but never rule one out; "no quota" or
"no LimitRange" may be concluded only from a conclusive list.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from korvid.core.api_versions import (
    EVENTS,
    Candidate,
    DiscoverySnapshot,
    LogicalSource,
    Resolution,
    resolve,
)
from korvid.core.capability import (
    Baseline,
    Basis,
    Declaration,
    EvidenceSet,
    EvidenceState,
    Observation,
    Role,
    RunToken,
    SourceScope,
    Verb,
    conclusive,
    downgrade_stale_status,
)
from korvid.core.creation_failures import Failure, Subject, event_failures
from korvid.core.creation_policies import (
    LimitRangeProjection,
    QuotaProjection,
    project_limit_ranges,
    project_quotas,
)
from korvid.core.creation_workloads import (
    WORKLOAD_KINDS,
    ChildProjection,
    WorkloadProjection,
    project_children,
    project_workload,
)
from korvid.core.evidence_reads import (
    LIST_PAGE_SIZE,
    MAX_BYTES,
    MAX_DEADLINE,
    MAX_PAGES,
    read_logical_list,
    read_object,
)
from korvid.k8s.evidence_requests import EvidenceReader

QUOTAS = LogicalSource("quotas", (Candidate("", "resourcequotas", ("v1",)),))
LIMIT_RANGES = LogicalSource("limit-ranges", (Candidate("", "limitranges", ("v1",)),))
_CHILD_RESOURCES = {"ReplicaSet": ("apps", "replicasets"), "Job": ("batch", "jobs")}


def _workload_source(kind: str) -> LogicalSource:
    workload = WORKLOAD_KINDS[kind]
    candidate = Candidate(workload.group, workload.resource, (workload.version,))
    return LogicalSource("workload", (candidate,))


def _children_source(child: str) -> LogicalSource:
    group, resource = _CHILD_RESOURCES[child]
    return LogicalSource("children", (Candidate(group, resource, ("v1",)),))


@dataclass(frozen=True, slots=True)
class CreationTarget:
    """The selected workload, with the UID the selection showed; an empty UID is none."""

    kind: str
    namespace: str
    name: str
    uid: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in WORKLOAD_KINDS or not self.namespace or not self.name:
            raise ValueError("a target is a named workload of a supported kind, in a namespace")
        if not self.uid:
            object.__setattr__(self, "uid", None)

    @property
    def child(self) -> str | None:
        """The kind of child that records this workload's Pods' failures, if any."""
        return WORKLOAD_KINDS[self.kind].child


def declarations(target: CreationTarget) -> tuple[Declaration, ...]:
    """The sources a run reads, all required.

    The workload, its children and the Events `CONFIRMS`: only what they
    show matters. The quota and LimitRange lists `RULES_OUT`: "none in this
    namespace" rests on their completeness.
    """
    sources = [_workload_source(target.kind).declaration(required=True, role=Role.CONFIRMS)]
    if target.child is not None:
        children = _children_source(target.child)
        sources.append(children.declaration(required=True, role=Role.CONFIRMS))
    sources += [
        EVENTS.declaration(required=True, role=Role.CONFIRMS),
        QUOTAS.declaration(required=True, role=Role.RULES_OUT),
        LIMIT_RANGES.declaration(required=True, role=Role.RULES_OUT),
    ]
    return tuple(sources)


@dataclass(frozen=True, slots=True)
class CreationReport:
    """What one run established about the workload and its namespace's policies."""

    target: CreationTarget
    workload_observation: Observation
    children_observation: Observation | None
    events_observation: Observation
    quotas_observation: Observation
    limit_ranges_observation: Observation
    workload: WorkloadProjection | None
    children: tuple[ChildProjection, ...]
    failures: tuple[Failure, ...]
    earlier_failures: int
    quotas: tuple[QuotaProjection, ...]
    limit_ranges: tuple[LimitRangeProjection, ...]
    baseline: Baseline

    @property
    def recreated(self) -> bool:
        """Whether the workload read is another incarnation than the one selected."""
        return self.workload_observation.recreated

    def quotas_conclusive(self, now: datetime) -> bool:
        """Whether `quotas` is every quota in the namespace, so their absence is a fact."""
        return conclusive(self.quotas_observation, QUOTAS.policy, now)

    def limit_ranges_conclusive(self, now: datetime) -> bool:
        """Whether `limit_ranges` is every LimitRange in the namespace."""
        return conclusive(self.limit_ranges_observation, LIMIT_RANGES.policy, now)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _Lists:
    items: tuple[dict[str, Any], ...]
    observation: Observation


async def _list(
    reader: EvidenceReader,
    resolution: Resolution,
    token: RunToken | None,
    clock: Callable[[], datetime],
    *,
    maximum: bool = False,
) -> _Lists:
    budgets: dict[str, Any] = {}
    if maximum:
        budgets = {
            "page_size": LIST_PAGE_SIZE,
            "max_pages": MAX_PAGES,
            "deadline": MAX_DEADLINE,
            "max_bytes": MAX_BYTES,
        }
    evidence = await read_logical_list(reader, resolution, token=token, clock=clock, **budgets)
    return _Lists(evidence.items, evidence.observation)


async def _get(
    reader: EvidenceReader,
    resolution: Resolution,
    token: RunToken | None,
    clock: Callable[[], datetime],
) -> tuple[dict[str, Any] | None, Observation]:
    if not resolution.sources:
        observation = resolution.outcomes[0].observation
        if observation is None:
            raise ValueError("an unresolved resolution records why")
        return None, replace(observation, token=token)
    evidence = await read_object(reader, resolution.sources[0], token=token, clock=clock)
    return evidence.item, evidence.observation


async def _no_children() -> None:
    return None


def _downgrade_unreadable(observation: Observation, unreadable: int, what: str) -> Observation:
    """An available list with items the projection could not read in full is `PARTIAL`.

    Otherwise a list of only unnamed policies, or a policy whose constraints
    were dropped, would rule out what it could not show. A list already
    partial keeps its own, already inconclusive, basis.
    """
    if not unreadable or observation.state is not EvidenceState.AVAILABLE:
        return observation
    detail = f"{unreadable} {what} could not be read in full"
    return replace(observation, state=EvidenceState.PARTIAL, basis=Basis.MALFORMED, detail=detail)


def _unreadable(read: int, projections: tuple[QuotaProjection | LimitRangeProjection, ...]) -> int:
    """How many listed policies were dropped, or kept with a constraint that could not be read."""
    return read - len(projections) + sum(not p.complete for p in projections)


def _downgrade_stale(observation: Observation, quotas: tuple[QuotaProjection, ...]) -> Observation:
    """An available quota list whose status lags is `PARTIAL`/`STALE_STATUS`.

    A list already partial keeps its own, already inconclusive, basis.
    """
    stale = [quota.name for quota in quotas if quota.stale]
    if not stale or observation.state is not EvidenceState.AVAILABLE:
        return observation
    return downgrade_stale_status(observation, f"quota status not yet updated: {', '.join(stale)}")


def _subjects(
    target: CreationTarget,
    workload: WorkloadProjection | None,
    observation: Observation,
) -> tuple[Subject, ...]:
    """Whom failures can be about: the workload read, or the one selected if it was not read."""
    if workload is not None:
        return (workload.subject,)
    if observation.basis is Basis.OBJECT_ABSENT or target.uid is None:
        return ()
    return (Subject(target.kind, target.name, target.uid),)


def _failures(
    events: _Lists,
    subjects: tuple[Subject, ...],
    workload: WorkloadProjection | None,
    children: tuple[ChildProjection, ...],
) -> tuple[tuple[Failure, ...], int]:
    recorded, earlier = event_failures(events.items, subjects)
    conditions = [workload.failure if workload is not None else None]
    conditions += [child.failure for child in children]
    merged = [*recorded, *(failure for failure in conditions if failure is not None)]
    merged.sort(key=lambda f: (f.at is None, -f.at.timestamp() if f.at is not None else 0.0))
    return tuple(merged), earlier


async def explain_creation(
    reader: EvidenceReader,
    snapshot: DiscoverySnapshot,
    target: CreationTarget,
    *,
    token: RunToken | None = None,
    clock: Callable[[], datetime] = _now,
) -> CreationReport:
    """Read the workload, its children, the Events and the policies, together, and report.

    Sources are resolved from `snapshot`, so a source the cluster does not
    serve is reported, not read. A source that fails does not stop the others.
    """
    scope = SourceScope.namespace(target.namespace)
    workload_resolution = resolve(
        _workload_source(target.kind),
        Verb.GET,
        snapshot,
        scope=scope,
        name=target.name,
        expected_uid=target.uid,
    )
    children_read = (
        _list(
            reader,
            resolve(_children_source(target.child), Verb.LIST, snapshot, scope=scope),
            token,
            clock,
            maximum=True,
        )
        if target.child is not None
        else _no_children()
    )
    (
        (item, workload_observation),
        children_lists,
        events,
        quotas,
        limit_ranges,
    ) = await asyncio.gather(
        _get(reader, workload_resolution, token, clock),
        children_read,
        _list(
            reader, resolve(EVENTS, Verb.LIST, snapshot, scope=scope), token, clock, maximum=True
        ),
        _list(reader, resolve(QUOTAS, Verb.LIST, snapshot, scope=scope), token, clock),
        _list(reader, resolve(LIMIT_RANGES, Verb.LIST, snapshot, scope=scope), token, clock),
    )
    workload = project_workload(target.kind, item) if item is not None else None
    subjects = _subjects(target, workload, workload_observation)
    children: tuple[ChildProjection, ...] = ()
    children_observation = children_lists.observation if children_lists is not None else None
    if children_lists is not None and subjects and target.child is not None:
        projected = project_children(target.child, children_lists.items, subjects[0].uid)
        children = projected.children
        children_observation = _downgrade_unreadable(
            children_lists.observation, projected.unprojected, f"{target.child}s"
        )
    failures, earlier = _failures(
        events, subjects + tuple(child.subject for child in children), workload, children
    )
    quota_projections = project_quotas(quotas.items)
    quotas_observation = _downgrade_stale(
        _downgrade_unreadable(
            quotas.observation,
            _unreadable(len(quotas.items), quota_projections),
            "ResourceQuotas",
        ),
        quota_projections,
    )
    range_projections = project_limit_ranges(limit_ranges.items)
    limit_ranges_observation = _downgrade_unreadable(
        limit_ranges.observation,
        _unreadable(len(limit_ranges.items), range_projections),
        "LimitRanges",
    )
    observations = {
        "workload": workload_observation,
        "events": events.observation,
        "quotas": quotas_observation,
        "limit-ranges": limit_ranges_observation,
    }
    if children_observation is not None:
        observations["children"] = children_observation
    baseline = EvidenceSet(observations).baseline(declarations(target), clock())
    return CreationReport(
        target,
        workload_observation,
        children_observation,
        events.observation,
        quotas_observation,
        limit_ranges_observation,
        workload,
        children,
        failures,
        earlier,
        quota_projections,
        range_projections,
        baseline,
    )
