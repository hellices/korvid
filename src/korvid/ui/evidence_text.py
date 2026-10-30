"""The one place evidence observations become text (#486, S6 of #409).

Every diagnostic panel shows its sources the same way, one line each:

    <resource> <group/version> <scope>: <state> — <basis sentence> · observed <age>

Widgets call these functions and never read a `Basis` themselves, so a
denied read, an API the cluster does not serve and a server error read the
same everywhere. Cluster-provided names go into a plain `Text`, which never
parses markup.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType

from rich.text import Text

from korvid.core.capability import (
    ApiSource,
    Basis,
    DiscoverySource,
    FreshnessPolicy,
    Observation,
    ScopeKind,
    Source,
    SourceScope,
    is_fresh,
)

#: One sentence for every basis. A new `Basis` without one fails the tests.
BASIS_SENTENCES: Mapping[Basis, str] = MappingProxyType(
    {
        Basis.NOT_STARTED: "not read yet",
        Basis.IN_FLIGHT: "reading",
        Basis.OBSERVED: "read",
        Basis.OBJECT_ABSENT: "the object does not exist",
        Basis.NO_DATA: "the provider returned no data",
        Basis.TRUNCATED: "stopped at the page limit",
        Basis.DEADLINE: "stopped at the deadline",
        Basis.TOO_LARGE: "a response was too large to read",
        Basis.CONTINUATION_EXPIRED: "the list changed while it was being read",
        Basis.SUBSCOPE_FAILED: "part of the scope could not be read",
        Basis.STALE_STATUS: "its status lags behind its specification",
        Basis.NOT_SERVED: "the cluster does not serve this API",
        Basis.OPERATION_UNSUPPORTED: "the API does not allow this read",
        Basis.NO_KNOWN_VERSION: "the cluster serves only versions korvid cannot read",
        Basis.PROJECTION_UNSUPPORTED: "the API cannot return metadata only",
        Basis.PROVIDER_NOT_CONFIGURED: "no provider is configured",
        Basis.FEATURE_DISABLED: "the feature is disabled",
        Basis.FORBIDDEN: "your permissions do not allow this read",
        Basis.UNAUTHENTICATED: "the cluster did not accept your credentials",
        Basis.DISCOVERY_FORBIDDEN: "your permissions do not allow API discovery",
        Basis.THROTTLED: "the API server asked korvid to slow down",
        Basis.SERVER_ERROR: "the API server returned an error",
        Basis.UNEXPECTED_STATUS: "the API server returned an unexpected status",
        Basis.TRANSPORT: "the API server could not be reached",
        Basis.MALFORMED: "the response could not be parsed",
        Basis.DISCOVERY_FAILED: "API discovery failed",
        Basis.DISCOVERY_STALE: "API discovery is out of date",
    }
)

_UNITS = ((86400, "d"), (3600, "h"), (60, "m"), (1, "s"))


def basis_sentence(basis: Basis) -> str:
    """What `basis` means, as a sentence fragment a user can act on."""
    return BASIS_SENTENCES[basis]


def elapsed_text(at: datetime, now: datetime) -> str:
    """How long ago `at` was, in its largest whole unit, such as `2m ago`."""
    seconds = (now - at).total_seconds()
    if seconds < 0:
        return "in the future"
    for size, unit in _UNITS:
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def age_text(observed_at: datetime | None, now: datetime) -> str:
    """How long ago an observation was made, in its largest whole unit."""
    if observed_at is None:
        return "not observed"
    return f"observed {elapsed_text(observed_at, now)}"


def _scope_text(scope: SourceScope) -> str:
    if scope.kind is ScopeKind.CLUSTER:
        return "cluster-wide"
    if scope.kind is ScopeKind.NAMESPACE:
        return f"in namespace {scope.name}"
    if scope.kind is ScopeKind.NODE:
        return f"on node {scope.name}"
    return f"from provider {scope.name}"


def source_text(source: Source) -> str:
    """What a source reads, and where."""
    if isinstance(source, ApiSource):
        api = source.group or "core"
        if source.version:
            api = f"{source.group}/{source.version}" if source.group else source.version
        return f"{source.resource} {api} {_scope_text(source.scope)}"
    if isinstance(source, DiscoverySource):
        return f"discovery {source.path} {_scope_text(source.scope)}"
    return f"{source.provider_id} {source.query_kind} {_scope_text(source.scope)}"


def observation_line(observation: Observation, policy: FreshnessPolicy, now: datetime) -> Text:
    """One source's line: what it read, its state and why, and how old it is.

    `policy` is the source's own, the one its declaration and `conclusive`
    use, so the line never calls fresh what the baseline treats as stale.
    """
    line = (
        f"{source_text(observation.source)}: {observation.state.value} — "
        f"{basis_sentence(observation.basis)} · {age_text(observation.observed_at, now)}"
    )
    if observation.observed_at is not None and not is_fresh(observation, policy, now):
        line = f"{line} · stale"
    return Text(line)


def not_established(what: str) -> Text:
    """A conclusion the evidence does not support, said as such."""
    return Text(f"Not established: {what}")
