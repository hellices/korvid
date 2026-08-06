from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, cast

FailureKind = Literal["gone", "throttled", "forbidden", "slow", "metrics_unavailable", "slow_logs"]
_FAILURE_KINDS = frozenset(
    {"gone", "throttled", "forbidden", "slow", "metrics_unavailable", "slow_logs"}
)
_PROFILE_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "seed",
        "object_count",
        "namespace_count",
        "steady_events_per_second",
        "duration_seconds",
        "bursts",
        "failures",
    }
)
_BURST_KEYS = frozenset({"start_second", "duration_seconds", "events_per_second"})
_FAILURE_KEYS = frozenset({"kind", "at_event"})


@dataclass(frozen=True)
class Burst:
    start_second: int
    duration_seconds: int
    events_per_second: int


@dataclass(frozen=True)
class FailureInjection:
    kind: FailureKind
    at_event: int


@dataclass(frozen=True)
class WorkloadProfile:
    schema_version: int
    id: str
    seed: int
    object_count: int
    namespace_count: int
    steady_events_per_second: int
    duration_seconds: int
    bursts: tuple[Burst, ...]
    failures: tuple[FailureInjection, ...]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _unknown(raw: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown keys: {', '.join(unknown)}")


def _int(raw: dict[str, Any], key: str, *, positive: bool = False) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if positive and value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _bursts(raw: Any) -> tuple[Burst, ...]:
    if not isinstance(raw, list):
        raise ValueError("bursts must be a list")
    result: list[Burst] = []
    for index, value in enumerate(raw, 1):
        item = _mapping(value, f"burst {index}")
        _unknown(item, _BURST_KEYS, f"burst {index}")
        burst = Burst(
            start_second=_int(item, "start_second"),
            duration_seconds=_int(item, "duration_seconds", positive=True),
            events_per_second=_int(item, "events_per_second", positive=True),
        )
        result.append(burst)
    return tuple(sorted(result, key=lambda burst: burst.start_second))


def _failures(raw: Any) -> tuple[FailureInjection, ...]:
    if not isinstance(raw, list):
        raise ValueError("failures must be a list")
    result: list[FailureInjection] = []
    for index, value in enumerate(raw, 1):
        item = _mapping(value, f"failure {index}")
        _unknown(item, _FAILURE_KEYS, f"failure {index}")
        kind = item.get("kind")
        if kind not in _FAILURE_KINDS:
            raise ValueError(f"failure {index} kind must be one of {sorted(_FAILURE_KINDS)}")
        result.append(
            FailureInjection(
                kind=cast(FailureKind, kind),
                at_event=_int(item, "at_event", positive=True),
            )
        )
    return tuple(sorted(result, key=lambda failure: failure.at_event))


def load_profile(path: Path) -> WorkloadProfile:
    raw = _mapping(json.loads(path.read_text()), path.name)
    _unknown(raw, _PROFILE_KEYS, path.name)
    schema_version = _int(raw, "schema_version", positive=True)
    if schema_version != 1:
        raise ValueError("schema_version must be 1")
    profile_id = raw.get("id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("id must be a non-empty string")
    object_count = _int(raw, "object_count", positive=True)
    namespace_count = _int(raw, "namespace_count", positive=True)
    if object_count % namespace_count:
        raise ValueError("object_count must be divisible by namespace_count")
    duration = _int(raw, "duration_seconds", positive=True)
    steady = _int(raw, "steady_events_per_second")
    if steady < 0:
        raise ValueError("steady_events_per_second must be non-negative")
    profile = WorkloadProfile(
        schema_version=schema_version,
        id=profile_id,
        seed=_int(raw, "seed"),
        object_count=object_count,
        namespace_count=namespace_count,
        steady_events_per_second=steady,
        duration_seconds=duration,
        bursts=_bursts(raw.get("bursts")),
        failures=_failures(raw.get("failures")),
    )
    validate_profile(profile)
    return profile


def validate_profile(profile: WorkloadProfile) -> None:
    """Re-check every duration-dependent invariant of an assembled profile.

    `load_profile` calls this on load, and any caller that rewrites a loaded
    profile - notably the CLI's `--duration` override, which uses
    `dataclasses.replace` and therefore skips the loader entirely - must call
    it again. Without it a shortened duration can leave a burst hanging past
    the end of the run or a failure injection past the last planned event,
    which only surfaces much later as an opaque generator assertion.

    Raises:
        ValueError: a burst falls outside `duration_seconds`, two bursts
            overlap, or a failure injection is scheduled past the last
            planned event.
    """
    for burst in profile.bursts:
        if burst.start_second < 0 or burst.start_second + burst.duration_seconds > (
            profile.duration_seconds
        ):
            raise ValueError(
                f"burst at second {burst.start_second} lasting {burst.duration_seconds}s "
                f"falls outside duration_seconds={profile.duration_seconds}"
            )
    ordered = sorted(profile.bursts, key=lambda burst: burst.start_second)
    for previous, current in pairwise(ordered):
        if previous.start_second + previous.duration_seconds > current.start_second:
            raise ValueError("bursts must not overlap")
    total = planned_event_count(profile)
    if any(failure.at_event > total for failure in profile.failures):
        raise ValueError(
            f"failure at_event exceeds planned event count ({total}) for "
            f"duration_seconds={profile.duration_seconds}"
        )


def planned_event_count(profile: WorkloadProfile) -> int:
    total = profile.duration_seconds * profile.steady_events_per_second
    for burst in profile.bursts:
        total -= round(burst.duration_seconds * profile.steady_events_per_second)
        total += round(burst.duration_seconds * burst.events_per_second)
    return total


def burst_end_offsets(profile: WorkloadProfile) -> tuple[float, ...]:
    """Absolute second offsets at which each burst's window closes.

    Ordered ascending so a driver can mark post-burst backlog drain the moment
    the schedule leaves a burst, on the same time axis event offsets use.
    """
    return tuple(
        sorted(float(burst.start_second + burst.duration_seconds) for burst in profile.bursts)
    )
