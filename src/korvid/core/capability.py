"""The evidence vocabulary for capability-aware diagnostics (#409).

A diagnostic reads evidence that a cluster may not serve, may refuse to this
principal, or may return only in part. This module names what korvid knows
about each source a diagnostic reads, an `EvidenceState` and the `Basis` for
it, and combines those facts so that absence counts as proof only when it is.

It performs no I/O. `korvid.core.capability_classifier` turns a failed read
into this vocabulary. The design is
`docs/superpowers/specs/2026-10-25-capability-aware-diagnostics-design.md`.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType


class EvidenceState(StrEnum):
    """What korvid knows about one evidence source.

    - `UNKNOWN`: not observed yet, or a read is in flight.
    - `AVAILABLE`: the source answered completely at its scope.
    - `PARTIAL`: some evidence, known to be incomplete.
    - `UNSUPPORTED`: evidence establishes that the source or operation is not
      served, or not configured, here.
    - `DENIED`: the server or provider refused this principal.
    - `UNAVAILABLE`: could not be determined.
    """

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class Basis(StrEnum):
    """Why an `EvidenceState` holds; `ALLOWED_BASES` lists the bases of each state."""

    NOT_STARTED = "not_started"
    IN_FLIGHT = "in_flight"
    OBSERVED = "observed"
    OBJECT_ABSENT = "object_absent"
    NO_DATA = "no_data"
    TRUNCATED = "truncated"
    DEADLINE = "deadline"
    TOO_LARGE = "too_large"
    CONTINUATION_EXPIRED = "continuation_expired"
    SUBSCOPE_FAILED = "subscope_failed"
    STALE_STATUS = "stale_status"
    NOT_SERVED = "not_served"
    OPERATION_UNSUPPORTED = "operation_unsupported"
    NO_KNOWN_VERSION = "no_known_version"
    PROJECTION_UNSUPPORTED = "projection_unsupported"
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    FEATURE_DISABLED = "feature_disabled"
    FORBIDDEN = "forbidden"
    UNAUTHENTICATED = "unauthenticated"
    DISCOVERY_FORBIDDEN = "discovery_forbidden"
    THROTTLED = "throttled"
    SERVER_ERROR = "server_error"
    UNEXPECTED_STATUS = "unexpected_status"
    TRANSPORT = "transport"
    MALFORMED = "malformed"
    DISCOVERY_FAILED = "discovery_failed"
    DISCOVERY_STALE = "discovery_stale"


_DENIED_BASES = frozenset({Basis.FORBIDDEN, Basis.UNAUTHENTICATED, Basis.DISCOVERY_FORBIDDEN})
_UNAVAILABLE_BASES = frozenset(
    {
        Basis.THROTTLED,
        Basis.SERVER_ERROR,
        Basis.UNEXPECTED_STATUS,
        Basis.TRANSPORT,
        Basis.MALFORMED,
        Basis.DEADLINE,
        Basis.TOO_LARGE,
        Basis.DISCOVERY_FAILED,
        Basis.DISCOVERY_STALE,
    }
)
#: A read that fails after at least one accepted page keeps what it read and
#: is `PARTIAL` with the failure's own basis. Discovery reads have no pages, so
#: the two bases only they produce never appear here. `DISCOVERY_STALE` does:
#: it is what a LIST gets when a 404 or 405 contradicts discovery.
_FAILURE_AFTER_A_PAGE = frozenset(
    (_DENIED_BASES | _UNAVAILABLE_BASES | {Basis.PROJECTION_UNSUPPORTED})
    - {Basis.DISCOVERY_FORBIDDEN, Basis.DISCOVERY_FAILED}
)

#: The bases each state allows. An observation with any other pairing is
#: refused, so a failure can never be recorded as a missing capability.
ALLOWED_BASES: Mapping[EvidenceState, frozenset[Basis]] = MappingProxyType(
    {
        EvidenceState.UNKNOWN: frozenset({Basis.NOT_STARTED, Basis.IN_FLIGHT}),
        EvidenceState.AVAILABLE: frozenset({Basis.OBSERVED, Basis.OBJECT_ABSENT, Basis.NO_DATA}),
        EvidenceState.PARTIAL: frozenset(
            {
                Basis.TRUNCATED,
                Basis.DEADLINE,
                Basis.TOO_LARGE,
                Basis.CONTINUATION_EXPIRED,
                Basis.SUBSCOPE_FAILED,
                Basis.STALE_STATUS,
            }
        )
        | _FAILURE_AFTER_A_PAGE,
        EvidenceState.UNSUPPORTED: frozenset(
            {
                Basis.NOT_SERVED,
                Basis.OPERATION_UNSUPPORTED,
                Basis.NO_KNOWN_VERSION,
                Basis.PROJECTION_UNSUPPORTED,
                Basis.PROVIDER_NOT_CONFIGURED,
                Basis.FEATURE_DISABLED,
            }
        ),
        EvidenceState.DENIED: _DENIED_BASES,
        EvidenceState.UNAVAILABLE: _UNAVAILABLE_BASES,
    }
)


class ScopeKind(StrEnum):
    """Where a source's evidence is read."""

    CLUSTER = "cluster"
    NAMESPACE = "namespace"
    NODE = "node"
    PROVIDER = "provider"


@dataclass(frozen=True, slots=True)
class SourceScope:
    """The scope a source is read at: the cluster, or one namespace, node or provider."""

    kind: ScopeKind
    name: str = ""

    def __post_init__(self) -> None:
        if (self.kind is ScopeKind.CLUSTER) == bool(self.name):
            raise ValueError("a cluster scope has no name, and every other scope has one")

    @classmethod
    def cluster(cls) -> SourceScope:
        """The whole cluster."""
        return cls(ScopeKind.CLUSTER)

    @classmethod
    def namespace(cls, name: str) -> SourceScope:
        """One namespace."""
        return cls(ScopeKind.NAMESPACE, name)

    @classmethod
    def node(cls, name: str) -> SourceScope:
        """One node."""
        return cls(ScopeKind.NODE, name)

    @classmethod
    def provider(cls, provider_id: str) -> SourceScope:
        """One configured provider, such as a metrics backend."""
        return cls(ScopeKind.PROVIDER, provider_id)


class Verb(StrEnum):
    """The read operation a source is resolved for."""

    LIST = "list"
    GET = "get"


class Projection(StrEnum):
    """Whether a read returns whole objects or only their metadata."""

    OBJECT = "object"
    METADATA = "metadata"


@dataclass(frozen=True, slots=True)
class ApiSource:
    """One Kubernetes API resource, read at one scope.

    `version` and `verb` stay `None` until discovery resolves the source, and a
    resolved source names both. A GET names its object, and may carry the UID
    the caller expects that object to have.
    """

    group: str
    resource: str
    scope: SourceScope
    version: str | None = None
    verb: Verb | None = None
    subresource: str = ""
    projection: Projection = Projection.OBJECT
    name: str = ""
    expected_uid: str | None = None

    def __post_init__(self) -> None:
        if not self.resource:
            raise ValueError("an API source names a resource")
        if (self.version is None) != (self.verb is None):
            raise ValueError("a resolved API source names both a version and a verb")
        if self.verb is Verb.LIST and self.name:
            raise ValueError("a LIST source names no object")
        if self.verb is Verb.GET and not self.name:
            raise ValueError("a GET source names its object")
        if self.expected_uid is not None and not self.name:
            raise ValueError("an expected UID needs an object name")
        if self.expected_uid == "":
            raise ValueError("an expected UID is never empty")

    @property
    def resolved(self) -> bool:
        """Whether discovery has chosen this source's version and verb."""
        return self.version is not None


class DiscoveryEndpoint(StrEnum):
    """Which discovery document a `DiscoverySource` names."""

    CORE_ROOT = "core_root"
    GROUP_ROOT = "group_root"
    GROUP_VERSION = "group_version"


@dataclass(frozen=True, slots=True)
class DiscoverySource:
    """One discovery endpoint: `/api`, `/apis`, or one group version.

    The core group version is `/api/v1`; any other is `/apis/<group>/<version>`.
    Discovery is always read cluster-wide.
    """

    endpoint: DiscoveryEndpoint
    group: str = ""
    version: str = ""

    def __post_init__(self) -> None:
        is_root = self.endpoint is not DiscoveryEndpoint.GROUP_VERSION
        if is_root and (self.group or self.version):
            raise ValueError("a discovery root names no group or version")
        if not is_root and not self.version:
            raise ValueError("a discovery group version names its version")

    @classmethod
    def core_root(cls) -> DiscoverySource:
        """The core root, `/api`."""
        return cls(DiscoveryEndpoint.CORE_ROOT)

    @classmethod
    def group_root(cls) -> DiscoverySource:
        """The named-group root, `/apis`."""
        return cls(DiscoveryEndpoint.GROUP_ROOT)

    @classmethod
    def group_version(cls, group: str, version: str) -> DiscoverySource:
        """One group version; the core group is `""`."""
        return cls(DiscoveryEndpoint.GROUP_VERSION, group, version)

    @property
    def path(self) -> str:
        """The endpoint's request path."""
        if self.endpoint is DiscoveryEndpoint.CORE_ROOT:
            return "/api"
        if self.endpoint is DiscoveryEndpoint.GROUP_ROOT:
            return "/apis"
        return f"/apis/{self.group}/{self.version}" if self.group else f"/api/{self.version}"

    @property
    def scope(self) -> SourceScope:
        """Discovery is read cluster-wide."""
        return SourceScope.cluster()


@dataclass(frozen=True, slots=True)
class ProviderSource:
    """One query to a configured provider, such as a metrics backend.

    Its scope is what the query covers: the provider as a whole, the cluster,
    or one namespace or node, such as the CPU usage of one namespace's pods.
    A provider-wide scope names the queried provider itself.
    Vocabulary only for now: no collector reads one until a diagnostic needs it.
    """

    provider_id: str
    query_kind: str
    scope: SourceScope

    def __post_init__(self) -> None:
        if not self.provider_id or not self.query_kind:
            raise ValueError("a provider source names its provider and query kind")
        if self.scope.kind is ScopeKind.PROVIDER and self.scope.name != self.provider_id:
            raise ValueError("a provider-wide query is scoped to its own provider")


Source = ApiSource | DiscoverySource | ProviderSource


class Role(StrEnum):
    """What a diagnostic may conclude from a source it declares.

    - `RULES_OUT`: it may conclude something from the source's absence, for
      example "no quota constrains this workload".
    - `CONFIRMS`: only what the source contains matters, for example a
      recorded rejection event.
    """

    RULES_OUT = "rules_out"
    CONFIRMS = "confirms"


#: How long an observation stays fresh unless its source says otherwise:
#: Pulse's staleness window.
DEFAULT_MAX_AGE = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    """How long a source's observations stay fresh, and whether its entries expire.

    A retention-limited source, such as Events, drops old entries: it can
    confirm a recorded fact but never show that one is absent.
    """

    max_age: timedelta = DEFAULT_MAX_AGE
    retention_limited: bool = False

    def __post_init__(self) -> None:
        if self.max_age <= timedelta(0):
            raise ValueError("a maximum age is positive")


#: The policy of a source that declares no other.
DEFAULT_POLICY = FreshnessPolicy()


@dataclass(frozen=True, slots=True)
class Declaration:
    """One source a diagnostic reads, under the key its observation is kept at."""

    key: str
    required: bool
    role: Role
    policy: FreshnessPolicy = DEFAULT_POLICY

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("a declaration has a key")
        if self.role is Role.RULES_OUT and self.policy.retention_limited:
            raise ValueError("a retention-limited source can confirm a fact but never rule one out")


@dataclass(frozen=True, slots=True)
class RunToken:
    """The context epoch, session generation and run generation a read started under.

    A result is applied only while all three still match the current ones.
    """

    epoch: int
    session_generation: int
    run_generation: int


_DETAIL_LIMIT = 200
_CONCLUSIVE_BASES = frozenset({Basis.OBSERVED, Basis.OBJECT_ABSENT})


def _expected_uid(source: Source) -> str | None:
    return source.expected_uid if isinstance(source, ApiSource) else None


@dataclass(frozen=True, slots=True)
class Observation:
    """What one read established about one source.

    Attributes:
        source: The source that answered.
        state: What korvid knows about the source.
        basis: Why that state holds; one of the state's `ALLOWED_BASES`.
        observed_at: When, in UTC. `None` exactly while the state is `UNKNOWN`.
        items: How many objects the read returned.
        observed_uid: The UID a GET actually returned. Required when the
            object was observed and the source expects a UID, so that the
            observation's incarnation is always known.
        detail: Fixed template text and resource identities, never response
            bodies or field values. Text over 200 characters is shortened.
        token: The run token the read started under.
    """

    source: Source
    state: EvidenceState
    basis: Basis
    observed_at: datetime | None = None
    items: int = 0
    observed_uid: str | None = None
    detail: str = ""
    token: RunToken | None = None

    def __post_init__(self) -> None:
        if self.basis not in ALLOWED_BASES[self.state]:
            raise ValueError(f"{self.state} evidence cannot rest on {self.basis}")
        if (self.state is EvidenceState.UNKNOWN) != (self.observed_at is None):
            raise ValueError("an observation has a time exactly when its state is known")
        if self.observed_at is not None and self.observed_at.utcoffset() != timedelta(0):
            raise ValueError("observation times are UTC")
        if isinstance(self.items, bool) or not isinstance(self.items, int) or self.items < 0:
            raise ValueError("an item count is a non-negative integer")
        if (
            self.basis is Basis.OBSERVED
            and self.observed_uid is None
            and _expected_uid(self.source) is not None
        ):
            raise ValueError("an observed object with an expected UID records the UID it saw")
        if len(self.detail) > _DETAIL_LIMIT:
            object.__setattr__(self, "detail", self.detail[: _DETAIL_LIMIT - 1] + "…")

    @property
    def recreated(self) -> bool:
        """Whether a GET found a different incarnation than the one expected.

        A recreated object never inherits the old one's evidence (#410).
        """
        expected = _expected_uid(self.source)
        if expected is None or self.observed_uid is None:
            return False
        return self.observed_uid != expected


def is_fresh(observation: Observation, policy: FreshnessPolicy, now: datetime) -> bool:
    """Whether the observation is no older than the policy's maximum age.

    An observation timed after `now` is not fresh: a clock that disagrees with
    itself proves nothing.
    """
    observed_at = observation.observed_at
    return observed_at is not None and timedelta(0) <= now - observed_at <= policy.max_age


def conclusive(observation: Observation, policy: FreshnessPolicy, now: datetime) -> bool:
    """Whether a diagnostic may treat absence in this observation as a fact.

    That takes complete, observed evidence (`NO_DATA` never counts: a provider
    that returns no series has not shown that a value is zero) about the
    expected incarnation, read recently enough, from a source whose entries do
    not expire.
    """
    return (
        observation.state is EvidenceState.AVAILABLE
        and observation.basis in _CONCLUSIVE_BASES
        and not observation.recreated
        and not policy.retention_limited
        and is_fresh(observation, policy, now)
    )


def downgrade_stale_status(observation: Observation, detail: str) -> Observation:
    """Mark available evidence whose content shows it lags as `PARTIAL`/`STALE_STATUS`.

    Read time is not semantic freshness: a ResourceQuota whose `status.hard`
    disagrees with its `spec.hard` was read just now but describes an older
    state.

    Raises:
        ValueError: if the observation is not `AVAILABLE`.
    """
    if observation.state is not EvidenceState.AVAILABLE:
        raise ValueError("only available evidence can be downgraded as stale")
    return dataclasses.replace(
        observation, state=EvidenceState.PARTIAL, basis=Basis.STALE_STATUS, detail=detail
    )


#: When no required source contributed evidence, the baseline is the first of
#: these states that one of them is in, so a failure is never summarized as a
#: missing capability.
_FAILURE_PRECEDENCE = (
    EvidenceState.DENIED,
    EvidenceState.UNAVAILABLE,
    EvidenceState.UNKNOWN,
    EvidenceState.UNSUPPORTED,
)
_CONTRIBUTED = frozenset({EvidenceState.AVAILABLE, EvidenceState.PARTIAL})


@dataclass(frozen=True, slots=True)
class Baseline:
    """A diagnostic's overall evidence state, and what keeps it from being complete.

    `blocking` holds the sorted keys of every required source that keeps the
    baseline from `AVAILABLE`, not only the one the precedence picked.
    """

    state: EvidenceState
    blocking: tuple[str, ...] = ()


class EvidenceSet:
    """An immutable collection of observations, keyed by declaration key."""

    __slots__ = ("_observations",)

    def __init__(self, observations: Mapping[str, Observation] | None = None) -> None:
        self._observations: Mapping[str, Observation] = MappingProxyType(dict(observations or {}))

    def get(self, key: str) -> Observation | None:
        """The observation kept under `key`, if any."""
        return self._observations.get(key)

    def with_observation(self, key: str, observation: Observation) -> EvidenceSet:
        """A copy with `observation` kept under `key`."""
        return EvidenceSet({**self._observations, key: observation})

    def baseline(self, declarations: Iterable[Declaration], now: datetime) -> Baseline:
        """Combine the required sources into one state, in any declaration order.

        - `AVAILABLE` when every required `RULES_OUT` source is conclusive and
          every required `CONFIRMS` source is available and fresh.
        - `PARTIAL` when a required source still contributed evidence.
        - Otherwise the first of `DENIED`, `UNAVAILABLE`, `UNKNOWN` and
          `UNSUPPORTED` that a required source is in.

        Optional sources never lower the baseline, and a source with no
        observation is `UNKNOWN`.

        Raises:
            ValueError: if two declarations share a key, or none is required.
        """
        required = _required(declarations)
        blocking = tuple(sorted(item.key for item in required if not self._complete(item, now)))
        if not blocking:
            return Baseline(EvidenceState.AVAILABLE)
        states = {self._state(item.key) for item in required}
        if states & _CONTRIBUTED:
            return Baseline(EvidenceState.PARTIAL, blocking)
        return Baseline(next(state for state in _FAILURE_PRECEDENCE if state in states), blocking)

    def by_scope(self, kind: ScopeKind) -> Mapping[EvidenceState, int]:
        """Count the states of the observations read at one kind of scope.

        "Available on 3 of 5 nodes" is `by_scope(ScopeKind.NODE)`: a mixed
        cluster is counted, never collapsed into one answer.
        """
        return dict(
            Counter(
                observation.state
                for observation in self._observations.values()
                if observation.source.scope.kind is kind
            )
        )

    def _state(self, key: str) -> EvidenceState:
        observation = self._observations.get(key)
        return EvidenceState.UNKNOWN if observation is None else observation.state

    def _complete(self, declaration: Declaration, now: datetime) -> bool:
        observation = self._observations.get(declaration.key)
        if observation is None or observation.recreated:
            return False
        if declaration.role is Role.RULES_OUT:
            return conclusive(observation, declaration.policy, now)
        return observation.state is EvidenceState.AVAILABLE and is_fresh(
            observation, declaration.policy, now
        )


def _required(declarations: Iterable[Declaration]) -> tuple[Declaration, ...]:
    items = tuple(declarations)
    if len({item.key for item in items}) != len(items):
        raise ValueError("each declaration has its own key")
    required = tuple(item for item in items if item.required)
    if not required:
        raise ValueError("a baseline needs at least one required source")
    return required
