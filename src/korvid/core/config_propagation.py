"""Explain how a ConfigMap's or Secret's changes reach the Pods that use it (#410).

One run reads two sources through the #409 readers, under the run token it
started with:

- `config-object`: the object's metadata, by name, to confirm it exists,
  which incarnation it is, and which resource version it has. A ConfigMap or
  Secret is never read as an object.
- `pods`: the Pods in its namespace, at the readers' maximum budgets. Each
  Pod is projected to its references at once, and only the projection
  leaves this module.

"No Pod consumes it" may be concluded only when the Pod list is conclusive.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from korvid.core.api_versions import (
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
    Observation,
    Projection,
    Role,
    RunToken,
    SourceScope,
    Verb,
    conclusive,
)
from korvid.core.config_changes import Sighting
from korvid.core.config_consumers import Consumer, consumers_of
from korvid.core.evidence_reads import (
    LIST_PAGE_SIZE,
    MAX_BYTES,
    MAX_DEADLINE,
    MAX_PAGES,
    read_list,
    read_object,
)
from korvid.k8s.evidence_requests import EvidenceReader

CONFIG_MAPS = LogicalSource("config-object", (Candidate("", "configmaps", ("v1",)),))
SECRETS = LogicalSource("config-object", (Candidate("", "secrets", ("v1",)),))
PODS = LogicalSource("pods", (Candidate("", "pods", ("v1",)),))
_OBJECT_SOURCES = {"ConfigMap": CONFIG_MAPS, "Secret": SECRETS}


@dataclass(frozen=True, slots=True)
class ConfigTarget:
    """The selected ConfigMap or Secret, with the UID the selection showed."""

    kind: str
    namespace: str
    name: str
    uid: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _OBJECT_SOURCES or not self.namespace or not self.name:
            raise ValueError("a target is a ConfigMap or a Secret, with a namespace and a name")


def declarations(target: ConfigTarget) -> tuple[Declaration, ...]:
    """The two sources a run reads, both required.

    The object `CONFIRMS`: only what it shows matters. The Pod list
    `RULES_OUT`: "no Pod consumes it" rests on its completeness.
    """
    return (
        _OBJECT_SOURCES[target.kind].declaration(required=True, role=Role.CONFIRMS),
        PODS.declaration(required=True, role=Role.RULES_OUT),
    )


@dataclass(frozen=True, slots=True)
class PropagationReport:
    """What one run established about the target and its consumers.

    `sighting` is the object's resource version, from before the reads were
    sent until the object's arrived, when the object was observed with one.
    """

    target: ConfigTarget
    object: Observation
    pods: Observation
    consumers: tuple[Consumer, ...]
    baseline: Baseline
    sighting: Sighting | None = None

    @property
    def object_absent(self) -> bool:
        """Whether the object is known to be gone: a 404 that named it."""
        return self.object.basis is Basis.OBJECT_ABSENT

    @property
    def recreated(self) -> bool:
        """Whether the object read is another incarnation than the one selected."""
        return self.object.recreated

    def consumers_conclusive(self, now: datetime) -> bool:
        """Whether `consumers` is every recognised consumer, so absence is a fact."""
        return conclusive(self.pods, PODS.policy, now)


def _now() -> datetime:
    return datetime.now(UTC)


def _unresolved(resolution: Resolution, token: RunToken | None) -> Observation:
    """Why discovery resolved no source; it carries the run's token."""
    observation = resolution.outcomes[0].observation
    if observation is None:
        raise ValueError("an unresolved resolution records why")
    return replace(observation, token=token)


async def _read_object(
    reader: EvidenceReader,
    resolution: Resolution,
    token: RunToken | None,
    clock: Callable[[], datetime],
) -> tuple[Observation, str | None]:
    """The object's observation, and its resource version if it was observed with one."""
    if not resolution.sources:
        return _unresolved(resolution, token), None
    evidence = await read_object(reader, resolution.sources[0], token=token, clock=clock)
    metadata = (evidence.item or {}).get("metadata")
    version = metadata.get("resourceVersion") if isinstance(metadata, dict) else None
    return evidence.observation, version if isinstance(version, str) and version else None


def _sighting(observation: Observation, version: str | None, sent_at: datetime) -> Sighting | None:
    """A version is read only from an object that was observed, so it has a time."""
    observed_at = observation.observed_at
    if version is None or observed_at is None:
        return None
    return Sighting(version, sent_at, observed_at)


async def _read_consumers(
    reader: EvidenceReader,
    resolution: Resolution,
    target: ConfigTarget,
    token: RunToken | None,
    clock: Callable[[], datetime],
) -> tuple[Observation, tuple[Consumer, ...]]:
    if not resolution.sources:
        return _unresolved(resolution, token), ()
    pods = await read_list(
        reader,
        resolution.sources[0],
        page_size=LIST_PAGE_SIZE,
        max_pages=MAX_PAGES,
        deadline=MAX_DEADLINE,
        max_bytes=MAX_BYTES,
        token=token,
        clock=clock,
    )
    return pods.observation, consumers_of(pods.items, target.kind, target.name)


async def explain_propagation(
    reader: EvidenceReader,
    snapshot: DiscoverySnapshot,
    target: ConfigTarget,
    *,
    token: RunToken | None = None,
    clock: Callable[[], datetime] = _now,
) -> PropagationReport:
    """Read the target's metadata and its namespace's Pods, together, and report.

    Sources are resolved from `snapshot`, so a source the cluster does not
    serve is reported, not read. A source that fails does not stop the other.
    """
    scope = SourceScope.namespace(target.namespace)
    object_resolution = resolve(
        _OBJECT_SOURCES[target.kind],
        Verb.GET,
        snapshot,
        scope=scope,
        projection=Projection.METADATA,
        name=target.name,
        expected_uid=target.uid,
    )
    pod_resolution = resolve(PODS, Verb.LIST, snapshot, scope=scope)
    sent_at = clock()
    (object_observation, version), (pod_observation, consumers) = await asyncio.gather(
        _read_object(reader, object_resolution, token, clock),
        _read_consumers(reader, pod_resolution, target, token, clock),
    )
    evidence = EvidenceSet({"config-object": object_observation, "pods": pod_observation})
    baseline = evidence.baseline(declarations(target), clock())
    return PropagationReport(
        target,
        object_observation,
        pod_observation,
        consumers,
        baseline,
        _sighting(object_observation, version, sent_at),
    )
