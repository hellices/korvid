"""Combining observations (#409): absence is proof only when it is conclusive."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from korvid.core.capability import (
    DEFAULT_MAX_AGE,
    ApiSource,
    Baseline,
    Basis,
    Declaration,
    EvidenceSet,
    EvidenceState,
    FreshnessPolicy,
    Observation,
    Role,
    ScopeKind,
    SourceScope,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TEAM = SourceScope.namespace("team-a")
STALE = DEFAULT_MAX_AGE + timedelta(seconds=1)
QUOTAS = Declaration("quotas", required=True, role=Role.RULES_OUT)
LIMITS = Declaration("limits", required=True, role=Role.RULES_OUT)
EVENTS = Declaration(
    "events", required=True, role=Role.CONFIRMS, policy=FreshnessPolicy(retention_limited=True)
)
METRICS = Declaration("metrics", required=False, role=Role.RULES_OUT)


def _seen(
    state: EvidenceState = EvidenceState.AVAILABLE,
    basis: Basis = Basis.OBSERVED,
    *,
    age: timedelta = timedelta(0),
    scope: SourceScope = TEAM,
) -> Observation:
    observed_at = None if state is EvidenceState.UNKNOWN else NOW - age
    return Observation(ApiSource("", "pods", scope), state, basis, observed_at)


DENIED = _seen(EvidenceState.DENIED, Basis.FORBIDDEN)
UNAVAILABLE = _seen(EvidenceState.UNAVAILABLE, Basis.TRANSPORT)
UNKNOWN = _seen(EvidenceState.UNKNOWN, Basis.IN_FLIGHT)
UNSUPPORTED = _seen(EvidenceState.UNSUPPORTED, Basis.NOT_SERVED)


def test_complete_evidence_is_available() -> None:
    evidence = EvidenceSet(
        {"quotas": _seen(), "limits": _seen(basis=Basis.OBJECT_ABSENT), "events": _seen()}
    )

    assert evidence.baseline((QUOTAS, LIMITS, EVENTS), NOW) == Baseline(EvidenceState.AVAILABLE)


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(_seen(), id="observed"),
        pytest.param(_seen(basis=Basis.NO_DATA), id="no-data"),
    ],
)
def test_a_confirming_source_never_needs_to_be_conclusive(events: Observation) -> None:
    evidence = EvidenceSet({"events": events})

    assert evidence.baseline((EVENTS,), NOW) == Baseline(EvidenceState.AVAILABLE)


@pytest.mark.parametrize(
    "quotas",
    [
        pytest.param(_seen(basis=Basis.NO_DATA), id="no-data"),
        pytest.param(_seen(age=STALE), id="stale"),
        pytest.param(_seen(EvidenceState.PARTIAL, Basis.TRUNCATED), id="truncated"),
    ],
)
def test_a_ruling_out_source_must_be_conclusive(quotas: Observation) -> None:
    evidence = EvidenceSet({"quotas": quotas, "events": _seen()})

    assert evidence.baseline((QUOTAS, EVENTS), NOW) == Baseline(EvidenceState.PARTIAL, ("quotas",))


def test_a_confirming_source_must_be_fresh() -> None:
    evidence = EvidenceSet({"events": _seen(age=STALE)})

    assert evidence.baseline((EVENTS,), NOW) == Baseline(EvidenceState.PARTIAL, ("events",))


def test_any_contributed_evidence_makes_an_incomplete_baseline_partial() -> None:
    evidence = EvidenceSet({"quotas": _seen(), "limits": DENIED})

    assert evidence.baseline((QUOTAS, LIMITS), NOW) == Baseline(EvidenceState.PARTIAL, ("limits",))


@pytest.mark.parametrize(
    ("quotas", "limits", "expected"),
    [
        pytest.param(UNAVAILABLE, DENIED, EvidenceState.DENIED, id="denied-over-unavailable"),
        pytest.param(UNSUPPORTED, DENIED, EvidenceState.DENIED, id="denied-over-unsupported"),
        pytest.param(
            UNKNOWN, UNAVAILABLE, EvidenceState.UNAVAILABLE, id="unavailable-over-unknown"
        ),
        pytest.param(UNSUPPORTED, UNKNOWN, EvidenceState.UNKNOWN, id="unknown-over-unsupported"),
        pytest.param(UNSUPPORTED, UNSUPPORTED, EvidenceState.UNSUPPORTED, id="unsupported"),
    ],
)
def test_without_evidence_a_failure_outranks_a_missing_capability(
    quotas: Observation, limits: Observation, expected: EvidenceState
) -> None:
    evidence = EvidenceSet({"quotas": quotas, "limits": limits})

    assert evidence.baseline((QUOTAS, LIMITS), NOW) == Baseline(expected, ("limits", "quotas"))


def test_a_source_with_no_observation_is_unknown() -> None:
    assert EvidenceSet().baseline((QUOTAS,), NOW) == Baseline(EvidenceState.UNKNOWN, ("quotas",))


@pytest.mark.parametrize(
    "metrics",
    [
        pytest.param({}, id="missing"),
        pytest.param({"metrics": DENIED}, id="denied"),
        pytest.param({"metrics": UNSUPPORTED}, id="unsupported"),
        pytest.param({"metrics": _seen(basis=Basis.NO_DATA)}, id="no-data"),
    ],
)
def test_an_optional_source_never_lowers_the_baseline(metrics: dict[str, Observation]) -> None:
    evidence = EvidenceSet({"quotas": _seen(), **metrics})

    assert evidence.baseline((QUOTAS, METRICS), NOW) == Baseline(EvidenceState.AVAILABLE)


@pytest.mark.parametrize(
    ("observations", "expected"),
    [
        pytest.param(
            {"quotas": DENIED, "limits": UNSUPPORTED, "events": UNAVAILABLE},
            Baseline(EvidenceState.DENIED, ("events", "limits", "quotas")),
            id="failures",
        ),
        pytest.param(
            {"quotas": _seen(), "limits": UNKNOWN, "events": _seen()},
            Baseline(EvidenceState.PARTIAL, ("limits",)),
            id="partial",
        ),
        pytest.param(
            {"quotas": _seen(), "limits": _seen(), "events": _seen()},
            Baseline(EvidenceState.AVAILABLE),
            id="complete",
        ),
    ],
)
def test_the_baseline_ignores_declaration_and_insertion_order(
    observations: dict[str, Observation], expected: Baseline
) -> None:
    results = {
        EvidenceSet({key: observations[key] for key in keys}).baseline(order, NOW)
        for order in itertools.permutations((QUOTAS, LIMITS, EVENTS, METRICS))
        for keys in itertools.permutations(observations)
    }

    assert results == {expected}


def test_declarations_need_distinct_keys() -> None:
    with pytest.raises(ValueError, match="own key"):
        EvidenceSet().baseline((QUOTAS, QUOTAS), NOW)


def test_a_baseline_needs_a_required_source() -> None:
    with pytest.raises(ValueError, match="at least one required source"):
        EvidenceSet().baseline((METRICS,), NOW)


def test_states_are_counted_per_scope_kind_and_never_collapsed() -> None:
    nodes = {f"kubelet-{name}": _seen(scope=SourceScope.node(name)) for name in "abc"}
    denied = {
        f"kubelet-{name}": _seen(
            EvidenceState.DENIED, Basis.FORBIDDEN, scope=SourceScope.node(name)
        )
        for name in "de"
    }
    evidence = EvidenceSet({**nodes, **denied, "quotas": _seen()})

    assert evidence.by_scope(ScopeKind.NODE) == {
        EvidenceState.AVAILABLE: 3,
        EvidenceState.DENIED: 2,
    }
    assert evidence.by_scope(ScopeKind.NAMESPACE) == {EvidenceState.AVAILABLE: 1}
    assert evidence.by_scope(ScopeKind.CLUSTER) == {}


def test_recording_an_observation_leaves_the_original_set_unchanged() -> None:
    original = EvidenceSet()
    updated = original.with_observation("quotas", _seen())

    assert original.get("quotas") is None
    assert updated.get("quotas") == _seen()
