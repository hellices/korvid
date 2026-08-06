import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.performance.profile import (
    Burst,
    load_profile,
    planned_event_count,
    validate_profile,
)


def _write(tmp_path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "schema_version": 1,
        "id": "smoke",
        "seed": 186,
        "object_count": 1000,
        "namespace_count": 20,
        "steady_events_per_second": 20,
        "duration_seconds": 5,
        "bursts": [{"start_second": 2, "duration_seconds": 1, "events_per_second": 100}],
        "failures": [{"kind": "gone", "at_event": 75}],
    }
    payload.update(overrides)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload))
    return path


def test_load_profile_is_strict_and_typed(tmp_path: Path) -> None:
    profile = load_profile(_write(tmp_path))
    assert profile.id == "smoke"
    assert profile.object_count == 1000
    assert profile.bursts[0].events_per_second == 100
    assert profile.failures[0].kind == "gone"
    assert planned_event_count(profile) == 180


def test_profile_rejects_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"unknown keys.*extra"):
        load_profile(_write(tmp_path, extra=True))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2, "schema_version must be 1"),
        ("object_count", 0, "object_count must be a positive integer"),
        ("namespace_count", 21, "object_count must be divisible by namespace_count"),
        ("steady_events_per_second", -1, "steady_events_per_second"),
        ("duration_seconds", 0, "duration_seconds must be a positive integer"),
    ],
)
def test_profile_rejects_invalid_values(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_profile(_write(tmp_path, **{field: value}))


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


def test_validate_profile_rejects_burst_outside_shortened_duration(tmp_path: Path) -> None:
    """`--duration` shortens a loaded profile with `dataclasses.replace`, which
    bypasses `load_profile`'s burst containment check. `validate_profile` is the
    shared re-check both paths call, so the shortened profile is rejected with a
    clear operational message instead of failing later inside the generator."""
    profile = load_profile(_write(tmp_path))
    shortened = replace(profile, duration_seconds=2)

    with pytest.raises(ValueError, match="falls outside duration_seconds"):
        validate_profile(shortened)


def test_validate_profile_rejects_failure_beyond_shortened_planned_events(
    tmp_path: Path,
) -> None:
    profile = load_profile(_write(tmp_path, bursts=[], failures=[{"kind": "gone", "at_event": 75}]))
    shortened = replace(profile, duration_seconds=1)

    with pytest.raises(ValueError, match="failure at_event exceeds planned event count"):
        validate_profile(shortened)


def test_validate_profile_accepts_a_duration_that_still_contains_every_burst(
    tmp_path: Path,
) -> None:
    profile = load_profile(_write(tmp_path, failures=[]))

    validate_profile(replace(profile, duration_seconds=3))

    assert planned_event_count(replace(profile, duration_seconds=3)) == 140


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


@pytest.mark.parametrize(
    "kind", ["gone", "throttled", "forbidden", "slow", "metrics_unavailable", "slow_logs"]
)
def test_load_profile_accepts_every_versioned_failure_kind(tmp_path: Path, kind: str) -> None:
    profile = load_profile(_write(tmp_path, failures=[{"kind": kind, "at_event": 10}]))
    assert profile.failures[0].kind == kind


def test_load_profile_rejects_unknown_failure_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        load_profile(_write(tmp_path, failures=[{"kind": "meltdown", "at_event": 10}]))


def test_load_profile_rejects_duplicate_failure_event_positions(tmp_path: Path) -> None:
    """`run_replay` indexes failures by `at_event`, so a duplicate position
    silently drops every failure but one — while the profile hash and the
    report still claim both were injected."""
    with pytest.raises(ValueError, match="at_event"):
        load_profile(
            _write(
                tmp_path,
                failures=[
                    {"kind": "gone", "at_event": 10},
                    {"kind": "throttled", "at_event": 10},
                ],
            )
        )
