"""The shared evidence renderer (#486, S6 of #409)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from rich.text import Text

from korvid.core.capability import (
    ApiSource,
    Basis,
    DiscoverySource,
    EvidenceState,
    FreshnessPolicy,
    Observation,
    ProviderSource,
    Source,
    SourceScope,
    Verb,
)
from korvid.ui.evidence_text import (
    age_text,
    basis_sentence,
    not_established,
    observation_line,
)

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
POLICY = FreshnessPolicy()
CONFIG_MAPS = ApiSource("", "configmaps", SourceScope.namespace("team-a"), "v1", Verb.LIST)


def _observed(source: Source, state: EvidenceState, basis: Basis, seconds: float) -> Observation:
    return Observation(source, state, basis, NOW - timedelta(seconds=seconds))


def test_every_basis_has_its_own_sentence() -> None:
    sentences = [basis_sentence(basis) for basis in Basis]

    assert all(sentences)
    assert len(set(sentences)) == len(Basis)


@pytest.mark.parametrize(
    ("basis", "words"),
    [
        (Basis.FORBIDDEN, "permissions"),
        (Basis.NOT_SERVED, "does not serve"),
        (Basis.SERVER_ERROR, "returned an error"),
        (Basis.TRUNCATED, "page limit"),
        (Basis.DEADLINE, "deadline"),
        (Basis.TOO_LARGE, "too large"),
        (Basis.CONTINUATION_EXPIRED, "changed while"),
    ],
)
def test_denied_unsupported_unavailable_and_partial_read_differently(
    basis: Basis, words: str
) -> None:
    assert words in basis_sentence(basis)


def test_an_api_line_names_the_resource_version_scope_state_basis_and_age() -> None:
    observation = _observed(CONFIG_MAPS, EvidenceState.AVAILABLE, Basis.OBSERVED, 2)

    line = observation_line(observation, POLICY, NOW)

    assert isinstance(line, Text)
    assert line.plain == "configmaps v1 in namespace team-a: available — read · observed 2s ago"


def test_a_grouped_cluster_wide_source_names_its_group() -> None:
    events = ApiSource("events.k8s.io", "events", SourceScope.cluster(), "v1", Verb.LIST)
    observation = _observed(events, EvidenceState.DENIED, Basis.FORBIDDEN, 5)

    assert observation_line(observation, POLICY, NOW).plain == (
        "events events.k8s.io/v1 cluster-wide: denied — "
        "your permissions do not allow this read · observed 5s ago"
    )


def test_an_unresolved_source_names_its_group_without_a_version() -> None:
    events = ApiSource("events.k8s.io", "events", SourceScope.namespace("a"))
    observation = _observed(events, EvidenceState.UNSUPPORTED, Basis.NOT_SERVED, 1)

    assert observation_line(observation, POLICY, NOW).plain.startswith(
        "events events.k8s.io in namespace a: unsupported — "
    )


def test_discovery_and_provider_sources_name_what_they_read() -> None:
    discovery = _observed(
        DiscoverySource.group_version("apps", "v1"),
        EvidenceState.UNAVAILABLE,
        Basis.DISCOVERY_FAILED,
        3,
    )
    provider = _observed(
        ProviderSource("prometheus", "cpu", SourceScope.node("n1")),
        EvidenceState.UNSUPPORTED,
        Basis.PROVIDER_NOT_CONFIGURED,
        3,
    )

    assert observation_line(discovery, POLICY, NOW).plain.startswith(
        "discovery /apis/apps/v1 cluster-wide: unavailable — API discovery failed"
    )
    assert observation_line(provider, POLICY, NOW).plain.startswith(
        "prometheus cpu on node n1: unsupported — no provider is configured"
    )


def test_staleness_follows_the_sources_own_policy() -> None:
    observation = _observed(CONFIG_MAPS, EvidenceState.AVAILABLE, Basis.OBSERVED, 60)
    short = FreshnessPolicy(max_age=timedelta(seconds=30))
    long = FreshnessPolicy(max_age=timedelta(minutes=5))

    assert observation_line(observation, short, NOW).plain.endswith("observed 1m ago · stale")
    assert observation_line(observation, long, NOW).plain.endswith("observed 1m ago")


def test_an_observation_never_made_is_not_called_stale() -> None:
    observation = Observation(CONFIG_MAPS, EvidenceState.UNKNOWN, Basis.NOT_STARTED)

    assert observation_line(observation, POLICY, NOW).plain == (
        "configmaps v1 in namespace team-a: unknown — not read yet · not observed"
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0.4, "observed just now"),
        (59, "observed 59s ago"),
        (61, "observed 1m ago"),
        (2 * 3600 + 5, "observed 2h ago"),
        (3 * 86400, "observed 3d ago"),
        (-5, "observed in the future"),
    ],
)
def test_ages_are_whole_units(seconds: float, text: str) -> None:
    assert age_text(NOW - timedelta(seconds=seconds), NOW) == text


def test_cluster_provided_names_are_rendered_literally() -> None:
    hostile = ApiSource("", "configmaps", SourceScope.namespace("[bold]x[/bold]"), "v1", Verb.LIST)
    observation = _observed(hostile, EvidenceState.AVAILABLE, Basis.OBSERVED, 1)

    line = observation_line(observation, POLICY, NOW)

    assert "in namespace [bold]x[/bold]:" in line.plain
    assert line.spans == []


def test_not_established_says_why() -> None:
    line = not_established("whether every consumer is listed")

    assert line.plain == "Not established: whether every consumer is listed"
    assert not_established("[red]x").plain == "Not established: [red]x"
