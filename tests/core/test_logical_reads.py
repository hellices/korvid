"""A logical source's LIST, falling through its candidates (#409, slice S5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from korvid.core.api_versions import EVENTS, DiscoverySnapshot, observe_discovery, resolve
from korvid.core.capability import (
    ApiSource,
    Basis,
    EvidenceState,
    Observation,
    Projection,
    RunToken,
    SourceScope,
    Verb,
)
from korvid.core.evidence_reads import read_logical_list
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
)
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.evidence_requests import EvidencePage, EvidenceReader, EvidenceTarget

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
TEAM = SourceScope.namespace("team-a")
EVENT = {"metadata": {"name": "e1", "namespace": "team-a", "uid": "uid-e1"}}
FORBIDDEN = ApiStatusError(403, "Forbidden")


def _clock() -> datetime:
    return NOW


def _version(group: str, *resources: str) -> GroupVersionOutcome:
    served = tuple(ServedResource(name, frozenset({"get", "list"})) for name in resources)
    return GroupVersionOutcome(group, "v1", served)


def _snapshot(*, events_group: bool = True, core_events: bool = True) -> DiscoverySnapshot:
    core = _version("", "pods", *(("events",) if core_events else ()))
    named = (_version("events.k8s.io", "events"),) if events_group else ()
    raw = RawDiscovery(
        1,
        NOW,
        0.0,
        RootOutcome("/api", (("", "v1"),)),
        RootOutcome("/apis", tuple((outcome.group, "v1") for outcome in named)),
        (core, *named),
    )
    return observe_discovery(raw)


class FakeReader(EvidenceReader):
    """Answers each LIST with the next scripted outcome, and records which group was read."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.groups: list[str] = []
        self.limits: list[int] = []

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.groups.append(target.group)
        self.limits.append(limit)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, EvidencePage)
        return outcome

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        raise AssertionError("a logical LIST never reads one object")


def _group(observation: Observation) -> str:
    assert isinstance(observation.source, ApiSource)
    return observation.source.group


def _state(observation: Observation) -> tuple[EvidenceState, Basis]:
    return observation.state, observation.basis


def _events(snapshot: DiscoverySnapshot) -> Any:
    return resolve(EVENTS, Verb.LIST, snapshot, scope=TEAM)


async def test_the_first_candidate_that_answers_is_the_only_one_read() -> None:
    reader = FakeReader(EvidencePage((EVENT,)))

    result = await read_logical_list(reader, _events(_snapshot()), clock=_clock)

    assert reader.groups == ["events.k8s.io"]
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert _group(result.observation) == "events.k8s.io"
    assert len(result.items) == 1
    assert [_group(outcome) for outcome in result.outcomes] == ["events.k8s.io"]


async def test_a_denied_read_falls_through_to_the_next_candidate() -> None:
    reader = FakeReader(FORBIDDEN, EvidencePage((EVENT,)))

    result = await read_logical_list(reader, _events(_snapshot()), clock=_clock)

    assert reader.groups == ["events.k8s.io", ""]
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert _group(result.observation) == ""
    assert [_state(outcome) for outcome in result.outcomes] == [
        (EvidenceState.DENIED, Basis.FORBIDDEN),
        (EvidenceState.AVAILABLE, Basis.OBSERVED),
    ]


async def test_an_unsupported_read_falls_through_to_the_next_candidate() -> None:
    reader = FakeReader(ApiStatusError(406, "Not Acceptable"), EvidencePage(()))
    resolution = resolve(EVENTS, Verb.LIST, _snapshot(), scope=TEAM, projection=Projection.METADATA)

    result = await read_logical_list(reader, resolution, clock=_clock)

    assert reader.groups == ["events.k8s.io", ""]
    assert _state(result.outcomes[0]) == (
        EvidenceState.UNSUPPORTED,
        Basis.PROJECTION_UNSUPPORTED,
    )
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)


async def test_an_unavailable_read_does_not_fall_through() -> None:
    reader = FakeReader(ApiStatusError(503, "Service Unavailable"), EvidencePage(()))

    result = await read_logical_list(reader, _events(_snapshot()), clock=_clock)

    assert reader.groups == ["events.k8s.io"]
    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.SERVER_ERROR)


async def test_a_partial_read_does_not_fall_through() -> None:
    truncated = [EvidencePage((EVENT,), "p2"), EvidencePage((EVENT,), "p3")]
    reader = FakeReader(*truncated, EvidencePage(()))

    result = await read_logical_list(reader, _events(_snapshot()), clock=_clock)

    assert reader.groups == ["events.k8s.io", "events.k8s.io"]
    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.TRUNCATED)
    assert len(result.items) == 2


async def test_a_candidate_discovery_did_not_resolve_is_skipped_and_kept() -> None:
    reader = FakeReader(EvidencePage((EVENT,)))

    result = await read_logical_list(reader, _events(_snapshot(events_group=False)), clock=_clock)

    assert reader.groups == [""]
    assert [_state(outcome) for outcome in result.outcomes] == [
        (EvidenceState.UNSUPPORTED, Basis.NOT_SERVED),
        (EvidenceState.AVAILABLE, Basis.OBSERVED),
    ]
    assert _group(result.observation) == ""


async def test_when_no_candidate_answers_the_strongest_failure_is_reported() -> None:
    reader = FakeReader(FORBIDDEN)

    result = await read_logical_list(reader, _events(_snapshot(core_events=False)), clock=_clock)

    assert reader.groups == ["events.k8s.io"]
    assert result.items == ()
    assert _state(result.observation) == (EvidenceState.DENIED, Basis.FORBIDDEN)
    assert [_state(outcome) for outcome in result.outcomes] == [
        (EvidenceState.DENIED, Basis.FORBIDDEN),
        (EvidenceState.UNSUPPORTED, Basis.NOT_SERVED),
    ]


async def test_a_failure_after_a_fall_through_reports_the_strongest_failure() -> None:
    reader = FakeReader(FORBIDDEN, ApiStatusError(503, "Service Unavailable"))

    result = await read_logical_list(reader, _events(_snapshot()), clock=_clock)

    assert reader.groups == ["events.k8s.io", ""]
    assert result.items == ()
    assert _state(result.observation) == (EvidenceState.DENIED, Basis.FORBIDDEN)
    assert [_state(outcome) for outcome in result.outcomes] == [
        (EvidenceState.DENIED, Basis.FORBIDDEN),
        (EvidenceState.UNAVAILABLE, Basis.SERVER_ERROR),
    ]


async def test_a_skipped_candidate_carries_the_run_token_and_keeps_its_time() -> None:
    token = RunToken(3, 2, 1)
    later = datetime(2026, 10, 28, 9, 5, tzinfo=UTC)

    result = await read_logical_list(
        FakeReader(),
        _events(_snapshot(events_group=False, core_events=False)),
        token=token,
        clock=lambda: later,
    )

    assert result.observation.token == token
    assert [outcome.token for outcome in result.outcomes] == [token, token]
    assert [outcome.observed_at for outcome in result.outcomes] == [NOW, NOW]


async def test_when_no_candidate_resolves_nothing_is_read() -> None:
    reader = FakeReader()

    result = await read_logical_list(
        reader, _events(_snapshot(events_group=False, core_events=False)), clock=_clock
    )

    assert reader.groups == []
    assert _state(result.observation) == (EvidenceState.UNSUPPORTED, Basis.NOT_SERVED)
    assert len(result.outcomes) == 2


async def test_every_read_gets_the_budgets_given() -> None:
    reader = FakeReader(FORBIDDEN, EvidencePage(()))

    result = await read_logical_list(
        reader, _events(_snapshot()), page_size=10, max_pages=1, clock=_clock
    )

    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert reader.limits == [10, 10]
