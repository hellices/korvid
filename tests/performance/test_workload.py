from dataclasses import replace
from itertools import pairwise

from tests.performance.profile import WorkloadProfile
from tests.performance.workload import (
    apply_events,
    event_digest,
    initial_pods,
    scheduled_events,
    summary_digest,
)


def _profile(seed: int = 186) -> WorkloadProfile:
    return WorkloadProfile(
        schema_version=1,
        id="test",
        seed=seed,
        object_count=100,
        namespace_count=10,
        steady_events_per_second=10,
        duration_seconds=2,
        bursts=(),
        failures=(),
    )


def test_same_seed_produces_identical_hashes() -> None:
    first = _profile()
    second = replace(first)
    assert summary_digest(initial_pods(first)) == summary_digest(initial_pods(second))
    assert event_digest(scheduled_events(first)) == event_digest(scheduled_events(second))


def test_different_seed_changes_event_hash_not_initial_hash() -> None:
    first = _profile(186)
    second = _profile(187)
    assert summary_digest(initial_pods(first)) == summary_digest(initial_pods(second))
    assert event_digest(scheduled_events(first)) != event_digest(scheduled_events(second))


def test_events_are_stably_scheduled_and_change_final_digest() -> None:
    profile = _profile()
    initial = initial_pods(profile)
    events = scheduled_events(profile)
    assert len(events) == 20
    assert [event.sequence for event in events] == list(range(1, 21))
    assert all(left.offset_seconds <= right.offset_seconds for left, right in pairwise(events))
    assert summary_digest(apply_events(initial, events)) != summary_digest(initial)
