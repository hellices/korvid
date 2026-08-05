import json
from pathlib import Path

import pytest

from tests.performance.profile import load_profile, planned_event_count


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
