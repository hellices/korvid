from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace

from korvid.k8s.models import PodSummary
from tests.performance.profile import WorkloadProfile, planned_event_count


@dataclass(frozen=True)
class ScheduledEvent:
    sequence: int
    offset_seconds: float
    event_type: str
    summary: PodSummary


def initial_pods(profile: WorkloadProfile) -> tuple[PodSummary, ...]:
    return tuple(
        PodSummary(
            name=f"pod-{index:06d}",
            namespace=f"bench-{index % profile.namespace_count:04d}",
            phase="Running",
            ready="1/1",
            restarts=0,
            node=f"node-{index % 5:02d}",
        )
        for index in range(profile.object_count)
    )


def _rate_at(profile: WorkloadProfile, second: float) -> int:
    for burst in profile.bursts:
        if burst.start_second <= second < burst.start_second + burst.duration_seconds:
            return burst.events_per_second
    return profile.steady_events_per_second


def scheduled_events(profile: WorkloadProfile) -> tuple[ScheduledEvent, ...]:
    rng = random.Random(profile.seed)
    current = list(initial_pods(profile))
    result: list[ScheduledEvent] = []
    sequence = 0
    for second in range(profile.duration_seconds):
        rate = _rate_at(profile, float(second))
        if rate <= 0:
            continue
        for tick in range(rate):
            sequence += 1
            index = rng.randrange(profile.object_count)
            old = current[index]
            updated = replace(
                old,
                phase="Pending" if old.phase == "Running" else "Running",
                ready="0/1" if old.ready == "1/1" else "1/1",
                restarts=old.restarts + 1,
            )
            current[index] = updated
            result.append(
                ScheduledEvent(
                    sequence=sequence,
                    offset_seconds=second + tick / rate,
                    event_type="MODIFIED",
                    summary=updated,
                )
            )
    assert len(result) == planned_event_count(profile)
    return tuple(result)


def _hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def summary_digest(summaries: Iterable[PodSummary]) -> str:
    ordered = sorted(summaries, key=lambda pod: (pod.namespace, pod.name))
    return _hash([asdict(pod) for pod in ordered])


def event_digest(events: Iterable[ScheduledEvent]) -> str:
    return _hash([asdict(event) for event in events])


def apply_events(
    initial: Iterable[PodSummary], events: Iterable[ScheduledEvent]
) -> tuple[PodSummary, ...]:
    current = {f"{pod.namespace}/{pod.name}": pod for pod in initial}
    for event in events:
        key = f"{event.summary.namespace}/{event.summary.name}"
        if event.event_type == "DELETED":
            current.pop(key, None)
        else:
            current[key] = event.summary
    return tuple(sorted(current.values(), key=lambda pod: (pod.namespace, pod.name)))
