"""The evidence vocabulary (#409): states, bases, sources and observations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from korvid.core.capability import (
    ALLOWED_BASES,
    DEFAULT_MAX_AGE,
    ApiSource,
    Basis,
    Declaration,
    DiscoveryEndpoint,
    DiscoverySource,
    EvidenceState,
    FreshnessPolicy,
    Observation,
    Projection,
    ProviderSource,
    Role,
    RunToken,
    ScopeKind,
    SourceScope,
    Verb,
    conclusive,
    downgrade_stale_status,
    is_fresh,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TEAM = SourceScope.namespace("team-a")
PODS = ApiSource("", "pods", TEAM)
CONFIG = ApiSource(
    "",
    "configmaps",
    TEAM,
    "v1",
    Verb.GET,
    projection=Projection.METADATA,
    name="app",
    expected_uid="uid-1",
)

#: The design's table of the bases each state allows, copied here so that a
#: change to either side fails this test.
EXPECTED_BASES = {
    EvidenceState.UNKNOWN: {Basis.NOT_STARTED, Basis.IN_FLIGHT},
    EvidenceState.AVAILABLE: {Basis.OBSERVED, Basis.OBJECT_ABSENT, Basis.NO_DATA},
    EvidenceState.PARTIAL: {
        Basis.TRUNCATED,
        Basis.DEADLINE,
        Basis.TOO_LARGE,
        Basis.CONTINUATION_EXPIRED,
        Basis.SUBSCOPE_FAILED,
        Basis.STALE_STATUS,
        # A read failure after an accepted page keeps its own basis.
        Basis.FORBIDDEN,
        Basis.UNAUTHENTICATED,
        Basis.THROTTLED,
        Basis.SERVER_ERROR,
        Basis.UNEXPECTED_STATUS,
        Basis.TRANSPORT,
        Basis.MALFORMED,
        Basis.DISCOVERY_STALE,
        Basis.PROJECTION_UNSUPPORTED,
    },
    EvidenceState.UNSUPPORTED: {
        Basis.NOT_SERVED,
        Basis.OPERATION_UNSUPPORTED,
        Basis.NO_KNOWN_VERSION,
        Basis.PROJECTION_UNSUPPORTED,
        Basis.PROVIDER_NOT_CONFIGURED,
        Basis.FEATURE_DISABLED,
    },
    EvidenceState.DENIED: {Basis.FORBIDDEN, Basis.UNAUTHENTICATED, Basis.DISCOVERY_FORBIDDEN},
    EvidenceState.UNAVAILABLE: {
        Basis.THROTTLED,
        Basis.SERVER_ERROR,
        Basis.UNEXPECTED_STATUS,
        Basis.TRANSPORT,
        Basis.MALFORMED,
        Basis.DEADLINE,
        Basis.TOO_LARGE,
        Basis.DISCOVERY_FAILED,
        Basis.DISCOVERY_STALE,
    },
}
ALLOWED_PAIRS = [
    (state, basis) for state, bases in EXPECTED_BASES.items() for basis in sorted(bases)
]
REFUSED_PAIRS = [
    (state, basis)
    for state in EvidenceState
    for basis in Basis
    if basis not in EXPECTED_BASES[state]
]


def _observation(
    state: EvidenceState = EvidenceState.AVAILABLE,
    basis: Basis = Basis.OBSERVED,
    *,
    at: datetime = NOW,
    source: ApiSource = PODS,
    **fields: Any,
) -> Observation:
    observed_at = None if state is EvidenceState.UNKNOWN else at
    return Observation(source, state, basis, observed_at, **fields)


def test_the_allowed_bases_match_the_design() -> None:
    assert {state: set(bases) for state, bases in ALLOWED_BASES.items()} == EXPECTED_BASES


@pytest.mark.parametrize(("state", "basis"), ALLOWED_PAIRS)
def test_an_observation_accepts_each_allowed_basis(state: EvidenceState, basis: Basis) -> None:
    assert _observation(state, basis).basis is basis


@pytest.mark.parametrize(("state", "basis"), REFUSED_PAIRS)
def test_an_observation_refuses_every_other_basis(state: EvidenceState, basis: Basis) -> None:
    with pytest.raises(ValueError, match="cannot rest on"):
        _observation(state, basis)


def test_an_unknown_observation_has_no_time() -> None:
    with pytest.raises(ValueError, match="time exactly when"):
        Observation(PODS, EvidenceState.UNKNOWN, Basis.NOT_STARTED, NOW)


def test_a_known_observation_needs_a_time() -> None:
    with pytest.raises(ValueError, match="time exactly when"):
        Observation(PODS, EvidenceState.AVAILABLE, Basis.OBSERVED)


@pytest.mark.parametrize(
    "observed_at",
    [
        pytest.param(datetime(2026, 10, 25, 12, 0), id="naive"),
        pytest.param(datetime(2026, 10, 25, 21, 0, tzinfo=timezone(timedelta(hours=9))), id="kst"),
    ],
)
def test_observation_times_are_utc(observed_at: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        Observation(PODS, EvidenceState.AVAILABLE, Basis.OBSERVED, observed_at)


@pytest.mark.parametrize("items", [-1, True])
def test_an_item_count_is_a_non_negative_integer(items: int) -> None:
    with pytest.raises(ValueError, match="item count"):
        _observation(items=items)


def test_a_long_detail_is_shortened_to_200_characters() -> None:
    observation = _observation(detail="x" * 500)

    assert len(observation.detail) == 200
    assert observation.detail.endswith("…")


def test_a_different_uid_marks_a_recreated_object() -> None:
    assert _observation(source=CONFIG, observed_uid="uid-2").recreated
    assert not _observation(source=CONFIG, observed_uid="uid-1").recreated
    assert not _observation(source=CONFIG).recreated
    assert not _observation(observed_uid="uid-2").recreated


def test_an_observation_carries_the_token_its_read_started_under() -> None:
    token = RunToken(epoch=3, session_generation=1, run_generation=7)

    assert _observation(token=token).token == token
    assert RunToken(epoch=3, session_generation=1, run_generation=8) != token


@pytest.mark.parametrize(
    ("state", "basis", "expected"),
    [
        (EvidenceState.AVAILABLE, Basis.OBSERVED, True),
        (EvidenceState.AVAILABLE, Basis.OBJECT_ABSENT, True),
        (EvidenceState.AVAILABLE, Basis.NO_DATA, False),
        (EvidenceState.PARTIAL, Basis.TRUNCATED, False),
        (EvidenceState.DENIED, Basis.FORBIDDEN, False),
        (EvidenceState.UNKNOWN, Basis.NOT_STARTED, False),
    ],
)
def test_only_observed_available_evidence_is_conclusive(
    state: EvidenceState, basis: Basis, expected: bool
) -> None:
    assert conclusive(_observation(state, basis), FreshnessPolicy(), NOW) is expected


def test_evidence_older_than_the_maximum_age_is_not_conclusive() -> None:
    policy = FreshnessPolicy()

    assert conclusive(_observation(at=NOW - DEFAULT_MAX_AGE), policy, NOW)
    assert not conclusive(
        _observation(at=NOW - DEFAULT_MAX_AGE - timedelta(seconds=1)), policy, NOW
    )


def test_each_source_keeps_its_own_maximum_age() -> None:
    observation = _observation(at=NOW - timedelta(minutes=2))

    assert not conclusive(observation, FreshnessPolicy(), NOW)
    assert conclusive(observation, FreshnessPolicy(max_age=timedelta(minutes=5)), NOW)


def test_fresh_events_are_never_conclusive_about_absence() -> None:
    events = ApiSource("events.k8s.io", "events", TEAM)
    observation = _observation(source=events)
    retention = FreshnessPolicy(retention_limited=True)

    assert is_fresh(observation, retention, NOW)
    assert not conclusive(observation, retention, NOW)


def test_a_stale_status_downgrade_is_partial_and_never_conclusive() -> None:
    observation = downgrade_stale_status(_observation(), "status.hard lags spec.hard")

    assert (observation.state, observation.basis) == (EvidenceState.PARTIAL, Basis.STALE_STATUS)
    assert observation.detail == "status.hard lags spec.hard"
    assert not conclusive(observation, FreshnessPolicy(), NOW)


def test_only_available_evidence_can_be_downgraded() -> None:
    with pytest.raises(ValueError, match="only available evidence"):
        downgrade_stale_status(_observation(EvidenceState.PARTIAL, Basis.TRUNCATED), "lags")


@pytest.mark.parametrize("max_age", [timedelta(0), timedelta(seconds=-1)])
def test_a_maximum_age_is_positive(max_age: timedelta) -> None:
    with pytest.raises(ValueError, match="positive"):
        FreshnessPolicy(max_age=max_age)


def test_a_retention_limited_source_can_never_rule_anything_out() -> None:
    with pytest.raises(ValueError, match="never rule one out"):
        Declaration(
            "events",
            required=True,
            role=Role.RULES_OUT,
            policy=FreshnessPolicy(retention_limited=True),
        )


def test_a_retention_limited_source_can_confirm() -> None:
    declaration = Declaration(
        "events", required=True, role=Role.CONFIRMS, policy=FreshnessPolicy(retention_limited=True)
    )

    assert declaration.policy.retention_limited


def test_a_declaration_has_a_key() -> None:
    with pytest.raises(ValueError, match="has a key"):
        Declaration("", required=True, role=Role.RULES_OUT)


@pytest.mark.parametrize(
    ("scope", "kind", "name"),
    [
        (SourceScope.cluster(), ScopeKind.CLUSTER, ""),
        (SourceScope.namespace("team-a"), ScopeKind.NAMESPACE, "team-a"),
        (SourceScope.node("node-1"), ScopeKind.NODE, "node-1"),
        (SourceScope.provider("prometheus"), ScopeKind.PROVIDER, "prometheus"),
    ],
)
def test_scope_constructors(scope: SourceScope, kind: ScopeKind, name: str) -> None:
    assert (scope.kind, scope.name) == (kind, name)


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        (ScopeKind.CLUSTER, "everything"),
        (ScopeKind.NAMESPACE, ""),
        (ScopeKind.NODE, ""),
        (ScopeKind.PROVIDER, ""),
    ],
)
def test_only_a_cluster_scope_is_unnamed(kind: ScopeKind, name: str) -> None:
    with pytest.raises(ValueError, match="cluster scope has no name"):
        SourceScope(kind, name)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"resource": ""}, "names a resource"),
        ({"version": "v1"}, "both a version and a verb"),
        ({"verb": Verb.LIST}, "both a version and a verb"),
        ({"version": "v1", "verb": Verb.LIST, "name": "app"}, "LIST source names no object"),
        ({"version": "v1", "verb": Verb.GET}, "GET source names its object"),
        ({"expected_uid": "uid-1"}, "expected UID needs an object name"),
    ],
)
def test_api_sources_are_well_formed(fields: dict[str, Any], message: str) -> None:
    arguments: dict[str, Any] = {"group": "", "resource": "configmaps", "scope": TEAM, **fields}

    with pytest.raises(ValueError, match=message):
        ApiSource(**arguments)


def test_a_resolved_source_names_its_version_and_verb() -> None:
    assert not PODS.resolved
    assert ApiSource("", "pods", TEAM, "v1", Verb.LIST).resolved


@pytest.mark.parametrize(
    ("source", "path"),
    [
        (DiscoverySource.core_root(), "/api"),
        (DiscoverySource.group_root(), "/apis"),
        (DiscoverySource.group_version("", "v1"), "/api/v1"),
        (DiscoverySource.group_version("apps", "v1"), "/apis/apps/v1"),
    ],
)
def test_a_discovery_source_names_one_cluster_wide_endpoint(
    source: DiscoverySource, path: str
) -> None:
    assert source.path == path
    assert source.scope == SourceScope.cluster()


@pytest.mark.parametrize(
    "fields",
    [
        {"endpoint": DiscoveryEndpoint.GROUP_VERSION},
        {"endpoint": DiscoveryEndpoint.CORE_ROOT, "version": "v1"},
        {"endpoint": DiscoveryEndpoint.GROUP_ROOT, "group": "apps"},
    ],
)
def test_discovery_sources_are_well_formed(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="discovery"):
        DiscoverySource(**fields)


def test_a_provider_source_names_its_provider_and_query() -> None:
    with pytest.raises(ValueError, match="provider and query kind"):
        ProviderSource("", "series", SourceScope.provider("prometheus"))
