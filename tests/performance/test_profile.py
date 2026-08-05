import json
from pathlib import Path

import pytest

from tests.performance.profile import Burst, load_profile, planned_event_count


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
