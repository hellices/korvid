import json
from pathlib import Path

import pytest

from tests.performance.profile import (
    Burst,
    load_profile,
    planned_event_count,
)


def test_aks_1k_profile_pins_live_topology_and_reuses_burst_schedule() -> None:
    """`aks-1k` is the *deterministic comparison* profile: the live topology on
    `burst-50k`'s event schedule, so a live run can be scored against the
    synthetic 1k/10k/50k baselines. The qualification run itself uses
    `aks-live-1k` (see `test_aks_live_1k_profile_matches_the_published_live_plan`).
    """
    profiles_dir = Path(__file__).with_name("profiles")

    profile = load_profile(profiles_dir / "aks-1k.json")
    burst = load_profile(profiles_dir / "burst-50k.json")

    assert profile.schema_version == 1
    assert profile.id == "aks-1k"
    assert profile.seed == 186
    assert profile.object_count == 1000
    assert profile.namespace_count == 20
    assert profile.steady_events_per_second == 200
    assert profile.duration_seconds == 30
    assert profile.bursts == (
        Burst(start_second=5, duration_seconds=1, events_per_second=1000),
        Burst(start_second=15, duration_seconds=1, events_per_second=1000),
        Burst(start_second=25, duration_seconds=1, events_per_second=1000),
    )
    assert profile.failures == ()
    assert profile.steady_events_per_second == burst.steady_events_per_second
    assert profile.duration_seconds == burst.duration_seconds
    assert profile.bursts == burst.bursts
    assert profile.failures == burst.failures


def test_aks_live_1k_profile_matches_the_published_live_plan() -> None:
    """The design doc's live sequence is 30 minutes of churn at 20 events/s with
    three 30-second bursts at 100 events/s; the live qualification profile must
    encode exactly that, otherwise three published budgets are unmeasurable."""
    profile = load_profile(Path(__file__).with_name("profiles") / "aks-live-1k.json")

    assert profile.id == "aks-live-1k"
    assert profile.seed == 186
    assert profile.object_count == 1000
    assert profile.namespace_count == 20
    assert profile.steady_events_per_second == 20
    assert profile.duration_seconds == 1800
    assert profile.bursts == (
        Burst(start_second=300, duration_seconds=30, events_per_second=100),
        Burst(start_second=900, duration_seconds=30, events_per_second=100),
        Burst(start_second=1500, duration_seconds=30, events_per_second=100),
    )
    assert profile.failures == ()
    assert planned_event_count(profile) == 43200


def test_steady_24eps_1k_profile_pins_the_published_acceptance_workload() -> None:
    """The input-latency acceptance result is published against a 1,000-Pod,
    24-events/second, 30-second steady schedule. That profile has to live in the
    repository, or the published number cannot be reproduced by anyone else. It
    is deliberately burst-free and failure-free: the cursor probe measures
    interaction under *steady* churn, and a burst or an injected failure would
    change the very workload the number is quoted against."""
    profile = load_profile(Path(__file__).with_name("profiles") / "steady-24eps-1k.json")

    assert profile.schema_version == 1
    assert profile.id == "steady-24eps-1k"
    assert profile.seed == 186
    assert profile.object_count == 1000
    assert profile.namespace_count == 20
    assert profile.steady_events_per_second == 24
    assert profile.duration_seconds == 30
    assert profile.bursts == ()
    assert profile.failures == ()
    assert planned_event_count(profile) == 720


def test_load_profile_rejects_duplicate_failure_event_positions(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-failures.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "duplicate-failures",
                "seed": 1,
                "object_count": 2,
                "namespace_count": 1,
                "steady_events_per_second": 10,
                "duration_seconds": 2,
                "bursts": [],
                "failures": [
                    {"kind": "gone", "at_event": 10},
                    {"kind": "throttled", "at_event": 10},
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="distinct at_event"):
        load_profile(path)
