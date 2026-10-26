"""Choose which API version to read from discovery, never from server versions.

Slice S2 of #409. `observe_discovery` turns the k8s layer's raw discovery into
an observation of every endpoint it read. `resolve` takes a logical source's
candidates in order and, for each, finds the most preferred discovered version
korvid can parse that serves the resource with the needed verb, or says why
there is none. A negative answer needs complete discovery: a failed or partly
unreadable root or group version blocks it, so that a failure is never
reported as a missing capability.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from korvid.core.capability import (
    DEFAULT_POLICY,
    FAILURE_PRECEDENCE,
    ApiSource,
    Basis,
    Declaration,
    DiscoverySource,
    EvidenceState,
    FreshnessPolicy,
    Observation,
    Projection,
    Role,
    SourceScope,
    Verb,
)
from korvid.core.capability_classifier import ReadKind, ReadRequest, classify
from korvid.k8s.discovery_snapshot import (
    DiscoveryFailure,
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
)

#: When several parts of a group's discovery are incomplete, a blocked
#: candidate reports the first of these states that one of them is in.
_BLOCKING_ORDER = (EvidenceState.DENIED, EvidenceState.UNAVAILABLE, EvidenceState.PARTIAL)


@dataclass(frozen=True, slots=True)
class VersionEvidence:
    """What discovery established about one group version.

    Attributes:
        observation: The observation of its discovery endpoint.
        served: Each resource it serves, with that resource's verbs; empty
            unless the endpoint was read.
    """

    observation: Observation
    served: Mapping[str, frozenset[str]]


@dataclass(frozen=True, slots=True, eq=False)
class DiscoverySnapshot:
    """Observations of one session's discovery, for resolution to read.

    Attributes:
        generation: The session generation the raw snapshot belongs to.
        observed_at: When discovery was read, in UTC.
        core_root: The observation of `/api`.
        group_root: The observation of `/apis`.
        versions: Each group version read, keyed by `(group, version)`, each
            group's preferred version first.
    """

    generation: int
    observed_at: datetime
    core_root: Observation
    group_root: Observation
    versions: Mapping[tuple[str, str], VersionEvidence]

    def root(self, group: str) -> Observation:
        """The root that advertises `group`: `/api` for the core group, else `/apis`."""
        return self.group_root if group else self.core_root

    def versions_of(self, group: str) -> tuple[str, ...]:
        """The versions read for `group`, preferred first."""
        return tuple(version for name, version in self.versions if name == group)


def observe_discovery(raw: RawDiscovery) -> DiscoverySnapshot:
    """Observe every endpoint `raw` read: both roots and each group version."""
    at = raw.fetched_at
    versions = {
        (outcome.group, outcome.version): _version_evidence(outcome, at)
        for outcome in raw.group_versions
    }
    return DiscoverySnapshot(
        raw.generation,
        at,
        _root_observation(raw.core_root, DiscoverySource.core_root(), at),
        _root_observation(raw.group_root, DiscoverySource.group_root(), at),
        MappingProxyType(versions),
    )


def _root_observation(outcome: RootOutcome, source: DiscoverySource, at: datetime) -> Observation:
    if outcome.error is not None:
        return _failed(outcome.error, source, at)
    return _read(source, at, len(outcome.advertised), complete=outcome.complete)


def _version_evidence(outcome: GroupVersionOutcome, at: datetime) -> VersionEvidence:
    source = DiscoverySource.group_version(outcome.group, outcome.version)
    if outcome.error is not None:
        return VersionEvidence(_failed(outcome.error, source, at), MappingProxyType({}))
    served = {resource.name: resource.verbs for resource in outcome.resources}
    observation = _read(source, at, len(outcome.resources), complete=outcome.complete)
    return VersionEvidence(observation, MappingProxyType(served))


def _read(source: DiscoverySource, at: datetime, items: int, *, complete: bool) -> Observation:
    if complete:
        return Observation(source, EvidenceState.AVAILABLE, Basis.OBSERVED, at, items=items)
    detail = f"{source.path} had an entry korvid could not read"
    return Observation(
        source, EvidenceState.PARTIAL, Basis.MALFORMED, at, items=items, detail=detail
    )


def _failed(error: DiscoveryFailure, source: DiscoverySource, at: datetime) -> Observation:
    result = classify(error, ReadRequest(ReadKind.DISCOVERY, source))
    return Observation(source, result.state, result.basis, at, detail=result.detail)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One API resource that can supply a logical source.

    Attributes:
        group: The API group; `""` for the core group.
        resource: The resource's plural name.
        versions: The versions korvid can parse; no other version is read.
    """

    group: str
    resource: str
    versions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.resource or not self.versions or "" in self.versions:
            raise ValueError("a candidate names its resource and the versions korvid can parse")

    @property
    def name(self) -> str:
        """The resource's qualified name, such as `events.k8s.io/events`."""
        return f"{self.group}/{self.resource}" if self.group else self.resource


@dataclass(frozen=True, slots=True)
class LogicalSource:
    """One kind of evidence, and the candidates that can supply it, in order.

    The first candidate that resolves is read. The next is tried only when
    that read is denied or unsupported: RBAC can allow one API and deny
    another that serves the same evidence.
    """

    key: str
    candidates: tuple[Candidate, ...]
    policy: FreshnessPolicy = DEFAULT_POLICY

    def __post_init__(self) -> None:
        if not self.key or not self.candidates:
            raise ValueError("a logical source has a key and at least one candidate")

    def declaration(self, *, required: bool, role: Role) -> Declaration:
        """How a diagnostic declares this source, with its freshness policy."""
        return Declaration(self.key, required, role, self.policy)


#: Events, from `events.k8s.io/v1` first and then core `v1`; RBAC treats the
#: two groups as separate resources. Old events expire, so the source can
#: confirm a recorded event but never show that none happened.
EVENTS = LogicalSource(
    "events",
    (Candidate("events.k8s.io", "events", ("v1",)), Candidate("", "events", ("v1",))),
    FreshnessPolicy(retention_limited=True),
)


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    """How one candidate resolved: a source to read, or why there is none.

    `source` is resolved exactly when there is no `observation`; otherwise it
    is the unresolved source the observation is about.
    """

    candidate: Candidate
    source: ApiSource
    observation: Observation | None = None

    def __post_init__(self) -> None:
        if self.source.resolved != (self.observation is None):
            raise ValueError("a candidate is resolved exactly when nothing stopped it")


@dataclass(frozen=True, slots=True)
class Resolution:
    """Every candidate's outcome, in the logical source's order."""

    logical_source: LogicalSource
    outcomes: tuple[CandidateOutcome, ...]

    @property
    def sources(self) -> tuple[ApiSource, ...]:
        """The resolved sources, in candidate order.

        Read the first. Each later one is a fallback, read only when the read
        before it is `DENIED` or `UNSUPPORTED` (#487).
        """
        return tuple(outcome.source for outcome in self.outcomes if outcome.observation is None)

    @property
    def failure(self) -> Observation | None:
        """When no candidate resolved, the outcome the baseline precedence picks.

        Every candidate's own outcome stays in `outcomes`.
        """
        failures = [o.observation for o in self.outcomes if o.observation is not None]
        if len(failures) < len(self.outcomes):
            return None
        return min(failures, key=lambda failure: FAILURE_PRECEDENCE.index(failure.state))


def resolve(
    logical_source: LogicalSource,
    verb: Verb,
    snapshot: DiscoverySnapshot,
    *,
    scope: SourceScope,
    projection: Projection = Projection.OBJECT,
    name: str = "",
    expected_uid: str | None = None,
) -> Resolution:
    """Resolve each of `logical_source`'s candidates for a `verb` read at `scope`.

    For each candidate, in order, the first of these applies:

    1. The most preferred version korvid can parse that discovery read and
       that serves the resource with `verb` is the resolved source. A broken
       sibling version does not block it.
    2. If the candidate's root, or any version of its group, was not fully
       read, the candidate is `DENIED` or `UNAVAILABLE`: a negative answer
       would need what korvid could not read.
    3. If a version korvid can parse serves it without `verb`, it is
       `UNSUPPORTED`/`OPERATION_UNSUPPORTED`.
    4. If only versions korvid cannot parse serve it, it is
       `UNSUPPORTED`/`NO_KNOWN_VERSION`.
    5. Otherwise it is `UNSUPPORTED`/`NOT_SERVED`.
    """
    outcomes = []
    for candidate in logical_source.candidates:
        unresolved = ApiSource(
            candidate.group,
            candidate.resource,
            scope,
            projection=projection,
            name=name,
            expected_uid=expected_uid,
        )
        outcomes.append(_resolve(candidate, verb, snapshot, unresolved))
    return Resolution(logical_source, tuple(outcomes))


def _resolve(
    candidate: Candidate, verb: Verb, snapshot: DiscoverySnapshot, unresolved: ApiSource
) -> CandidateOutcome:
    version = _known_version(candidate, verb, snapshot)
    if version is not None:
        resolved = dataclasses.replace(unresolved, version=version, verb=verb)
        return CandidateOutcome(candidate, resolved)
    blocking = _incomplete_part(candidate.group, snapshot)
    if blocking is not None:
        return CandidateOutcome(candidate, unresolved, _blocked(unresolved, blocking))
    unsupported = _unsupported(candidate, verb, snapshot, unresolved)
    return CandidateOutcome(candidate, unresolved, unsupported)


def _known_version(candidate: Candidate, verb: Verb, snapshot: DiscoverySnapshot) -> str | None:
    for version in snapshot.versions_of(candidate.group):
        served = snapshot.versions[(candidate.group, version)].served
        if version in candidate.versions and verb in served.get(candidate.resource, ()):
            return version
    return None


def _incomplete_part(group: str, snapshot: DiscoverySnapshot) -> Observation | None:
    parts = [snapshot.root(group)]
    parts += [
        snapshot.versions[(group, version)].observation for version in snapshot.versions_of(group)
    ]
    incomplete = [part for part in parts if part.state is not EvidenceState.AVAILABLE]
    if not incomplete:
        return None
    return min(incomplete, key=lambda part: _BLOCKING_ORDER.index(part.state))


def _blocked(unresolved: ApiSource, part: Observation) -> Observation:
    if part.state is EvidenceState.PARTIAL:
        state, basis = EvidenceState.UNAVAILABLE, Basis.DISCOVERY_FAILED
    else:
        state, basis = part.state, part.basis
    path = part.source.path if isinstance(part.source, DiscoverySource) else "discovery"
    detail = f"discovery of {path} is incomplete"
    return Observation(unresolved, state, basis, part.observed_at, detail=detail)


def _unsupported(
    candidate: Candidate, verb: Verb, snapshot: DiscoverySnapshot, unresolved: ApiSource
) -> Observation:
    serving = [
        version
        for version in snapshot.versions_of(candidate.group)
        if candidate.resource in snapshot.versions[(candidate.group, version)].served
    ]
    if any(version in candidate.versions for version in serving):
        basis, detail = Basis.OPERATION_UNSUPPORTED, f"{candidate.name} does not support {verb}"
    elif serving:
        basis = Basis.NO_KNOWN_VERSION
        detail = f"{candidate.name} is served only in versions korvid cannot read"
    else:
        basis, detail = Basis.NOT_SERVED, f"{candidate.name} is not served"
    at = snapshot.observed_at
    return Observation(unresolved, EvidenceState.UNSUPPORTED, basis, at, detail=detail)
