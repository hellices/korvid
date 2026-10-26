"""API version resolution from discovery, never from server versions (#409, S2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core.api_versions import (
    EVENTS,
    Candidate,
    CandidateOutcome,
    DiscoverySnapshot,
    LogicalSource,
    Resolution,
    observe_discovery,
    resolve,
)
from korvid.core.capability import (
    ApiSource,
    Basis,
    DiscoverySource,
    EvidenceState,
    Projection,
    Role,
    SourceScope,
    Verb,
)
from korvid.k8s.discovery_snapshot import (
    DiscoveryFailure,
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
    fetch_discovery,
)
from korvid.k8s.errors import ApiStatusError, KubeMalformedError, KubeTimeoutError

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
TEAM = SourceScope.namespace("team-a")
FORBIDDEN = ApiStatusError(403, "Forbidden")
SERVER_ERROR = ApiStatusError(503, "Service Unavailable")

WIDGETS = LogicalSource("widgets", (Candidate("example.io", "widgets", ("v1", "v1beta1")),))
GADGETS = LogicalSource("gadgets", (Candidate("example.io", "gadgets", ("v1",)),))
CONFIGMAPS = LogicalSource("configmaps", (Candidate("", "configmaps", ("v1",)),))
SECRETS = LogicalSource("secrets", (Candidate("", "secrets", ("v1",)),))


def _served(name: str, *verbs: str) -> ServedResource:
    return ServedResource(name, frozenset(verbs or ("get", "list", "watch")))


def _version(
    group: str,
    version: str,
    *resources: ServedResource,
    complete: bool = True,
    error: DiscoveryFailure | None = None,
) -> GroupVersionOutcome:
    complete = complete and error is None
    return GroupVersionOutcome(group, version, resources, complete=complete, error=error)


CORE = _version("", "v1", _served("pods"), _served("configmaps"), _served("events"))


def _snapshot(
    *named: GroupVersionOutcome,
    core: GroupVersionOutcome = CORE,
    core_root: RootOutcome | None = None,
    group_root: RootOutcome | None = None,
) -> DiscoverySnapshot:
    advertised = tuple(dict.fromkeys((outcome.group, outcome.version) for outcome in named))
    raw = RawDiscovery(
        7,
        NOW,
        0.0,
        core_root or RootOutcome("/api", (("", "v1"),)),
        group_root or RootOutcome("/apis", advertised),
        (core, *named),
    )
    return observe_discovery(raw)


def _resolve(
    source: LogicalSource, snapshot: DiscoverySnapshot, verb: Verb = Verb.LIST, **target: Any
) -> Resolution:
    return resolve(source, verb, snapshot, scope=TEAM, **target)


def _failure(resolution: Resolution) -> tuple[EvidenceState, Basis]:
    (outcome,) = resolution.outcomes
    assert outcome.observation is not None
    return outcome.observation.state, outcome.observation.basis


def _versions(resolution: Resolution) -> list[tuple[str, str | None]]:
    return [(source.group, source.version) for source in resolution.sources]


def test_every_endpoint_read_is_observed_at_its_own_source() -> None:
    snapshot = _snapshot(_version("apps", "v1", _served("deployments")))
    versions = snapshot.versions

    assert snapshot.generation == 7
    assert snapshot.core_root.source == DiscoverySource.core_root()
    assert snapshot.group_root.source == DiscoverySource.group_root()
    assert {key: evidence.observation.source for key, evidence in versions.items()} == {
        ("", "v1"): DiscoverySource.group_version("", "v1"),
        ("apps", "v1"): DiscoverySource.group_version("apps", "v1"),
    }
    observations = [
        snapshot.core_root,
        snapshot.group_root,
        *(evidence.observation for evidence in versions.values()),
    ]
    assert {(item.state, item.basis, item.observed_at) for item in observations} == {
        (EvidenceState.AVAILABLE, Basis.OBSERVED, NOW)
    }


def test_each_document_counts_the_entries_it_returned() -> None:
    snapshot = _snapshot(
        _version("apps", "v1", _served("deployments"), _served("replicasets")),
        _version("batch", "v1", _served("jobs")),
    )
    versions = snapshot.versions

    assert (snapshot.core_root.items, snapshot.group_root.items) == (1, 2)
    assert {key: evidence.observation.items for key, evidence in versions.items()} == {
        ("", "v1"): 3,
        ("apps", "v1"): 2,
        ("batch", "v1"): 1,
    }


@pytest.mark.parametrize(
    ("error", "state", "basis"),
    [
        (ApiStatusError(401, "Unauthorized"), EvidenceState.DENIED, Basis.UNAUTHENTICATED),
        (FORBIDDEN, EvidenceState.DENIED, Basis.DISCOVERY_FORBIDDEN),
        (ApiStatusError(429, "Too Many Requests"), EvidenceState.UNAVAILABLE, Basis.THROTTLED),
        (SERVER_ERROR, EvidenceState.UNAVAILABLE, Basis.DISCOVERY_FAILED),
        (KubeTimeoutError("timed out"), EvidenceState.UNAVAILABLE, Basis.DISCOVERY_FAILED),
        (KubeMalformedError("malformed"), EvidenceState.UNAVAILABLE, Basis.DISCOVERY_FAILED),
    ],
)
def test_a_failed_discovery_request_is_classified(
    error: DiscoveryFailure, state: EvidenceState, basis: Basis
) -> None:
    snapshot = _snapshot(
        _version("apps", "v1", error=error),
        core_root=RootOutcome("/api", complete=False, error=error),
    )
    apps = snapshot.versions[("apps", "v1")]

    for observation in (snapshot.core_root, apps.observation):
        assert (observation.state, observation.basis) == (state, basis)
    assert apps.served == {}


def test_a_document_with_an_unreadable_entry_is_partial() -> None:
    snapshot = _snapshot(
        _version("apps", "v1", _served("deployments"), complete=False),
        group_root=RootOutcome("/apis", (("apps", "v1"),), complete=False),
    )

    for observation in (snapshot.group_root, snapshot.versions[("apps", "v1")].observation):
        assert (observation.state, observation.basis, observation.items) == (
            EvidenceState.PARTIAL,
            Basis.MALFORMED,
            1,
        )


def test_the_most_preferred_version_is_chosen() -> None:
    snapshot = _snapshot(
        _version("example.io", "v1", _served("widgets")),
        _version("example.io", "v1beta1", _served("widgets")),
    )

    assert _resolve(WIDGETS, snapshot).sources == (
        ApiSource("example.io", "widgets", TEAM, "v1", Verb.LIST),
    )


def test_a_non_preferred_version_is_chosen_when_only_it_serves_the_resource() -> None:
    snapshot = _snapshot(
        _version("example.io", "v1", _served("gadgets")),
        _version("example.io", "v1beta1", _served("widgets")),
    )

    assert _versions(_resolve(WIDGETS, snapshot)) == [("example.io", "v1beta1")]


def test_a_version_korvid_cannot_parse_is_never_chosen() -> None:
    snapshot = _snapshot(
        _version("example.io", "v2", _served("widgets")),
        _version("example.io", "v1", _served("widgets")),
    )

    assert _versions(_resolve(WIDGETS, snapshot)) == [("example.io", "v1")]


@pytest.mark.parametrize(
    ("served", "basis"),
    [
        pytest.param(
            _version("example.io", "v1", _served("widgets", "get")),
            Basis.OPERATION_UNSUPPORTED,
            id="verb-missing",
        ),
        pytest.param(
            _version("example.io", "v2", _served("widgets")),
            Basis.NO_KNOWN_VERSION,
            id="unknown-version-only",
        ),
        pytest.param(
            _version("example.io", "v1", _served("gadgets")), Basis.NOT_SERVED, id="not-served"
        ),
    ],
)
def test_complete_discovery_proves_a_negative_answer(
    served: GroupVersionOutcome, basis: Basis
) -> None:
    assert _failure(_resolve(WIDGETS, _snapshot(served))) == (EvidenceState.UNSUPPORTED, basis)


def test_an_unadvertised_group_is_not_served() -> None:
    assert _failure(_resolve(WIDGETS, _snapshot())) == (
        EvidenceState.UNSUPPORTED,
        Basis.NOT_SERVED,
    )


@pytest.mark.parametrize(
    ("source", "snapshot", "detail"),
    [
        pytest.param(
            WIDGETS,
            _snapshot(_version("example.io", "v1", _served("widgets", "get"))),
            "example.io/widgets does not support list",
            id="verb-missing",
        ),
        pytest.param(
            WIDGETS,
            _snapshot(_version("example.io", "v2", _served("widgets"))),
            "example.io/widgets is served only in versions korvid cannot read",
            id="unknown-version-only",
        ),
        pytest.param(
            CONFIGMAPS,
            _snapshot(core=_version("", "v1", _served("pods"))),
            "configmaps is not served",
            id="core-not-served",
        ),
        pytest.param(
            WIDGETS,
            _snapshot(_version("example.io", "v1", error=SERVER_ERROR)),
            "discovery of /apis/example.io/v1 is incomplete",
            id="version-failed",
        ),
        pytest.param(
            CONFIGMAPS,
            _snapshot(
                core=_version("", "v1", _served("pods")),
                core_root=RootOutcome("/api", complete=False, error=FORBIDDEN),
            ),
            "discovery of /api is incomplete",
            id="core-root-failed",
        ),
    ],
)
def test_each_failure_says_what_stopped_the_candidate(
    source: LogicalSource, snapshot: DiscoverySnapshot, detail: str
) -> None:
    (outcome,) = _resolve(source, snapshot).outcomes

    assert outcome.observation is not None
    assert outcome.observation.detail == detail


@pytest.mark.parametrize(
    ("verbs", "verb", "resolves"),
    [
        (("get",), Verb.GET, True),
        (("get",), Verb.LIST, False),
        (("list",), Verb.LIST, True),
        (("list",), Verb.GET, False),
    ],
)
def test_a_resource_resolves_only_for_the_verbs_it_advertises(
    verbs: tuple[str, ...], verb: Verb, resolves: bool
) -> None:
    snapshot = _snapshot(_version("example.io", "v1", _served("widgets", *verbs)))
    name = "app" if verb is Verb.GET else ""

    assert bool(_resolve(WIDGETS, snapshot, verb, name=name).sources) is resolves


def test_a_known_version_resolves_beside_a_failed_sibling() -> None:
    snapshot = _snapshot(
        _version("example.io", "v1", error=SERVER_ERROR),
        _version("example.io", "v1beta1", _served("widgets")),
    )

    assert _versions(_resolve(WIDGETS, snapshot)) == [("example.io", "v1beta1")]


@pytest.mark.parametrize(
    "sibling",
    [
        pytest.param(_version("example.io", "v1beta1"), id="not-served"),
        pytest.param(
            _version("example.io", "v1beta1", _served("widgets", "get")), id="verb-missing"
        ),
        pytest.param(_version("example.io", "v2", _served("widgets")), id="unknown-version-only"),
    ],
)
@pytest.mark.parametrize(
    ("error", "state", "basis"),
    [
        (FORBIDDEN, EvidenceState.DENIED, Basis.DISCOVERY_FORBIDDEN),
        (ApiStatusError(401, "Unauthorized"), EvidenceState.DENIED, Basis.UNAUTHENTICATED),
        (SERVER_ERROR, EvidenceState.UNAVAILABLE, Basis.DISCOVERY_FAILED),
        (ApiStatusError(429, "Too Many Requests"), EvidenceState.UNAVAILABLE, Basis.THROTTLED),
    ],
)
def test_a_failed_version_of_the_group_blocks_every_negative_answer(
    sibling: GroupVersionOutcome, error: DiscoveryFailure, state: EvidenceState, basis: Basis
) -> None:
    snapshot = _snapshot(_version("example.io", "v1", error=error), sibling)

    assert _failure(_resolve(WIDGETS, snapshot)) == (state, basis)


def test_a_denied_part_of_discovery_outranks_an_unavailable_one() -> None:
    snapshot = _snapshot(
        _version("example.io", "v1", error=SERVER_ERROR),
        _version("example.io", "v1beta1", error=FORBIDDEN),
    )

    assert _failure(_resolve(WIDGETS, snapshot)) == (
        EvidenceState.DENIED,
        Basis.DISCOVERY_FORBIDDEN,
    )


def test_an_unreadable_entry_blocks_negative_answers_but_not_positive_ones() -> None:
    snapshot = _snapshot(_version("example.io", "v1", _served("widgets"), complete=False))

    assert _resolve(WIDGETS, snapshot).sources
    assert _failure(_resolve(GADGETS, snapshot)) == (
        EvidenceState.UNAVAILABLE,
        Basis.DISCOVERY_FAILED,
    )


def test_an_unreadable_root_entry_blocks_every_named_negative_answer() -> None:
    snapshot = _snapshot(
        _version("example.io", "v1", _served("widgets")),
        group_root=RootOutcome("/apis", (("example.io", "v1"),), complete=False),
    )

    assert _resolve(WIDGETS, snapshot).sources
    assert _failure(_resolve(GADGETS, snapshot)) == (
        EvidenceState.UNAVAILABLE,
        Basis.DISCOVERY_FAILED,
    )


EXAMPLE_GROUP = {
    "name": "example.io",
    "preferredVersion": {"groupVersion": "example.io/v1", "version": "v1"},
    "versions": [{"groupVersion": "example.io/v1", "version": "v1"}],
}
WIDGET_ENTRY = {"name": "widgets", "kind": "Widget", "namespaced": True}
#: Discovery a server answered in full: widgets are served, but cannot be listed.
SERVED_DOCUMENTS: dict[str, dict[str, Any]] = {
    "/api": {"versions": ["v1"]},
    "/api/v1": {"resources": []},
    "/apis": {"groups": [EXAMPLE_GROUP]},
    "/apis/example.io/v1": {"resources": [{**WIDGET_ENTRY, "verbs": ["get"]}]},
}


async def _resolve_documents(changes: dict[str, dict[str, Any]]) -> tuple[EvidenceState, Basis]:
    """Read discovery from `SERVED_DOCUMENTS` with `changes`, then resolve widgets."""
    documents = {**SERVED_DOCUMENTS, **changes}

    async def request(path: str) -> dict[str, Any]:
        return documents[path]

    raw = await fetch_discovery(request, generation=1, timeout=1.0, concurrency=2)
    return _failure(_resolve(WIDGETS, observe_discovery(raw)))


async def test_discovery_read_in_full_can_answer_unsupported() -> None:
    assert await _resolve_documents({}) == (
        EvidenceState.UNSUPPORTED,
        Basis.OPERATION_UNSUPPORTED,
    )


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"/apis": {}}, id="no-group-list"),
        pytest.param(
            {"/apis": {"groups": [{**EXAMPLE_GROUP, "versions": [{"version": "v1"}]}]}},
            id="no-group-version",
        ),
        pytest.param(
            {"/apis": {"groups": [{**EXAMPLE_GROUP, "versions": None}]}}, id="no-version-list"
        ),
        pytest.param({"/apis/example.io/v1": {}}, id="no-resource-list"),
        pytest.param({"/apis/example.io/v1": {"resources": None}}, id="null-resource-list"),
        pytest.param({"/apis/example.io/v1": {"resources": [WIDGET_ENTRY]}}, id="no-verbs"),
        pytest.param(
            {"/apis/example.io/v1": {"resources": [{**WIDGET_ENTRY, "verbs": None}]}},
            id="null-verbs",
        ),
    ],
)
async def test_discovery_not_read_in_full_never_answers_unsupported(
    changes: dict[str, dict[str, Any]],
) -> None:
    assert await _resolve_documents(changes) == (
        EvidenceState.UNAVAILABLE,
        Basis.DISCOVERY_FAILED,
    )


@pytest.mark.parametrize(
    ("error", "state"),
    [(FORBIDDEN, EvidenceState.DENIED), (SERVER_ERROR, EvidenceState.UNAVAILABLE)],
)
def test_a_failed_core_root_blocks_only_negative_core_answers(
    error: ApiStatusError, state: EvidenceState
) -> None:
    snapshot = _snapshot(core_root=RootOutcome("/api", complete=False, error=error))

    assert _failure(_resolve(SECRETS, snapshot))[0] is state
    assert _resolve(CONFIGMAPS, snapshot).sources
    assert _failure(_resolve(WIDGETS, snapshot)) == (
        EvidenceState.UNSUPPORTED,
        Basis.NOT_SERVED,
    )


def test_a_failed_group_root_blocks_only_named_groups() -> None:
    snapshot = _snapshot(group_root=RootOutcome("/apis", complete=False, error=FORBIDDEN))

    assert _failure(_resolve(WIDGETS, snapshot)) == (
        EvidenceState.DENIED,
        Basis.DISCOVERY_FORBIDDEN,
    )
    assert _failure(_resolve(SECRETS, snapshot)) == (EvidenceState.UNSUPPORTED, Basis.NOT_SERVED)
    assert _resolve(CONFIGMAPS, snapshot).sources


def test_a_refresh_that_drops_the_verb_is_operation_unsupported() -> None:
    before = _snapshot(_version("example.io", "v1", _served("widgets", "get", "list")))
    after = _snapshot(_version("example.io", "v1", _served("widgets", "get")))

    assert _resolve(WIDGETS, before).sources
    assert _failure(_resolve(WIDGETS, after)) == (
        EvidenceState.UNSUPPORTED,
        Basis.OPERATION_UNSUPPORTED,
    )


def test_events_resolve_through_both_groups_in_order() -> None:
    resolution = _resolve(EVENTS, _snapshot(_version("events.k8s.io", "v1", _served("events"))))

    assert _versions(resolution) == [("events.k8s.io", "v1"), ("", "v1")]
    assert resolution.failure is None


def test_events_fall_back_to_core_when_the_events_group_is_absent() -> None:
    resolution = _resolve(EVENTS, _snapshot())
    first = resolution.outcomes[0].observation

    assert _versions(resolution) == [("", "v1")]
    assert resolution.failure is None
    assert first is not None
    assert first.basis is Basis.NOT_SERVED


def test_events_fall_back_to_core_when_the_events_group_is_denied() -> None:
    resolution = _resolve(EVENTS, _snapshot(_version("events.k8s.io", "v1", error=FORBIDDEN)))
    first = resolution.outcomes[0].observation

    assert _versions(resolution) == [("", "v1")]
    assert resolution.failure is None
    assert first is not None
    assert first.state is EvidenceState.DENIED


def test_with_neither_group_serving_events_the_strongest_failure_is_reported() -> None:
    snapshot = _snapshot(
        _version("events.k8s.io", "v1", error=FORBIDDEN), core=_version("", "v1", _served("pods"))
    )
    resolution = _resolve(EVENTS, snapshot)

    assert resolution.sources == ()
    assert resolution.failure is not None
    assert (resolution.failure.state, resolution.failure.basis) == (
        EvidenceState.DENIED,
        Basis.DISCOVERY_FORBIDDEN,
    )
    assert [
        outcome.observation.basis for outcome in resolution.outcomes if outcome.observation
    ] == [
        Basis.DISCOVERY_FORBIDDEN,
        Basis.NOT_SERVED,
    ]


def test_a_resolved_source_carries_the_read_target() -> None:
    resolution = resolve(
        CONFIGMAPS,
        Verb.GET,
        _snapshot(),
        scope=TEAM,
        projection=Projection.METADATA,
        name="app",
        expected_uid="uid-1",
    )

    assert resolution.sources == (
        ApiSource(
            "",
            "configmaps",
            TEAM,
            "v1",
            Verb.GET,
            projection=Projection.METADATA,
            name="app",
            expected_uid="uid-1",
        ),
    )


def test_an_unresolved_candidate_is_observed_at_its_unresolved_source() -> None:
    (outcome,) = _resolve(SECRETS, _snapshot()).outcomes

    assert outcome.source == ApiSource("", "secrets", TEAM)
    assert outcome.observation is not None
    assert (outcome.observation.source, outcome.observation.observed_at) == (outcome.source, NOW)


def test_events_can_confirm_but_never_rule_out() -> None:
    assert EVENTS.declaration(required=True, role=Role.CONFIRMS).policy.retention_limited
    with pytest.raises(ValueError, match="never rule one out"):
        EVENTS.declaration(required=True, role=Role.RULES_OUT)


@pytest.mark.parametrize(
    ("resource", "versions"), [("", ("v1",)), ("widgets", ()), ("widgets", ("v1", ""))]
)
def test_a_candidate_names_its_resource_and_versions(
    resource: str, versions: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError, match="candidate names its resource"):
        Candidate("example.io", resource, versions)


@pytest.mark.parametrize(
    ("key", "candidates"), [("", (Candidate("", "pods", ("v1",)),)), ("pods", ())]
)
def test_a_logical_source_has_a_key_and_candidates(
    key: str, candidates: tuple[Candidate, ...]
) -> None:
    with pytest.raises(ValueError, match="key and at least one candidate"):
        LogicalSource(key, candidates)


def test_a_candidate_outcome_is_resolved_exactly_when_nothing_stopped_it() -> None:
    with pytest.raises(ValueError, match="resolved exactly when"):
        CandidateOutcome(Candidate("", "pods", ("v1",)), ApiSource("", "pods", TEAM))
