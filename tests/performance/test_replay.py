"""Real-app replay harness tests (Task 5, issue #186).

Drives the production KorvidApp/WatchManager/ResourceStore/ResourceTable
stack with a synthetic WorkloadProfile and asserts digest correctness,
update accounting, and API telemetry.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.performance.profile import FailureInjection, WorkloadProfile
from tests.performance.replay import ReplayOptions, run_replay
from tests.performance.workload import apply_events, initial_pods, scheduled_events, summary_digest


async def test_replay_uses_real_app_and_reaches_expected_digest() -> None:
    profile = WorkloadProfile(
        schema_version=1,
        id="test",
        seed=186,
        object_count=100,
        namespace_count=10,
        steady_events_per_second=10,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )
    report = await run_replay(profile, ReplayOptions(time_scale=0))
    events = scheduled_events(profile)
    oracle = summary_digest(apply_events(initial_pods(profile), events))
    assert report.object_count == 100
    assert report.expected_digest == oracle
    assert report.final_digest == oracle
    assert report.dropped_updates == 0
    assert report.rendered_updates == 110
    assert report.input_latency.count > 0
    assert report.churn_started_before_input
    assert report.api.operations["list"] == 1
    assert report.api.operations["watch_open"] == 1
    assert report.api.operations.get("get", 0) == 0


async def test_replay_time_scale_1_uses_relative_inter_event_delays() -> None:
    """time_scale=1 must use inter-event delays, not absolute offsets.

    Virtual-time seam: `monotonic_fn` returns a shared virtual clock and
    `async_sleep` advances that clock then yields via `asyncio.sleep(0)`,
    so the test completes in ~0 s of wall time regardless of profile length.

    Sensitivity: with 60 events at 20 eps over 3 s, correct sleeps sum exactly
    to the final 2.95 s offset. The historical absolute-offset bug sums every
    offset instead, while omitted sleeps sum to zero; both fail deterministically.
    """
    profile = WorkloadProfile(
        schema_version=1,
        id="test-ts1",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=20,
        duration_seconds=3,
        bursts=(),
        failures=(),
    )
    virtual_time: list[float] = [0.0]
    sleep_delays: list[float] = []

    def virtual_monotonic() -> float:
        return virtual_time[0]

    async def virtual_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        virtual_time[0] += delay
        await asyncio.sleep(0)  # yield to event loop without real wall time

    report = await run_replay(
        profile,
        ReplayOptions(time_scale=1, monotonic_fn=virtual_monotonic, async_sleep=virtual_sleep),
    )
    assert sum(sleep_delays) == pytest.approx(scheduled_events(profile)[-1].offset_seconds)
    assert report.dropped_updates == 0
    assert report.object_count == 20
    assert report.expected_digest == report.final_digest


async def test_replay_gone_reconnects_and_digest_matches() -> None:
    """gone at event 5 triggers one reconnect/re-LIST; final digest drops stale rows."""
    profile = WorkloadProfile(
        schema_version=1,
        id="test-gone",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=20,
        duration_seconds=2,
        bursts=(),
        failures=(FailureInjection(kind="gone", at_event=5),),
    )
    report = await run_replay(profile, ReplayOptions(time_scale=0))
    events = scheduled_events(profile)
    # The gone failure event itself is not applied as a watch event; filter it
    # from the apply_events oracle so it matches the actual replay outcome.
    hard_failure_seqs = {f.at_event for f in profile.failures if f.kind != "slow"}
    applied = tuple(e for e in events if e.sequence not in hard_failure_seqs)
    oracle = summary_digest(apply_events(initial_pods(profile), applied))
    assert report.expected_digest == oracle
    assert report.final_digest == oracle
    assert report.dropped_updates == 0
    assert report.churn_started_before_input
    assert report.api.operations["list"] == 2
    assert report.api.operations["watch_open"] == 2
    assert report.api.reconnects == 1
    assert report.api.relists == 1


async def test_replay_gone_reconnects_with_time_scale_1() -> None:
    """HTTP 410 reconnect with time_scale=1 must use elapsed-based delay, not absolute offsets.

    Virtual-time seam: same `monotonic_fn` / `async_sleep` pattern as
    `test_replay_time_scale_1_uses_relative_inter_event_delays`.  The shared
    virtual clock is never reset across reconnect generations, so gen=1 events
    correctly see the accumulated elapsed time from gen=0.

    Sensitivity: correct sleeps sum exactly to the final 4.95 s offset, and the
    first post-410 sleep remains one 0.05 s tick. Resetting the replay origin on
    reconnect produces a 0.25 s first reconnect sleep and 5.15 s total; omitted
    sleeps produce zero. Both fail deterministically.
    """
    profile = WorkloadProfile(
        schema_version=1,
        id="test-gone-ts1",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=20,
        duration_seconds=5,
        bursts=(),
        failures=(FailureInjection(kind="gone", at_event=5),),
    )
    virtual_time: list[float] = [0.0]
    sleep_delays: list[float] = []

    def virtual_monotonic() -> float:
        return virtual_time[0]

    async def virtual_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        virtual_time[0] += delay
        await asyncio.sleep(0)  # yield to event loop without real wall time

    report = await run_replay(
        profile,
        ReplayOptions(time_scale=1, monotonic_fn=virtual_monotonic, async_sleep=virtual_sleep),
    )

    events = scheduled_events(profile)
    failure_sequence = profile.failures[0].at_event
    first_post_reconnect_delay = (
        events[failure_sequence].offset_seconds - events[failure_sequence - 1].offset_seconds
    )
    assert sum(sleep_delays) == pytest.approx(events[-1].offset_seconds)
    assert sleep_delays[failure_sequence - 1] == pytest.approx(first_post_reconnect_delay)
    assert report.expected_digest == report.final_digest
    assert report.dropped_updates == 0
    assert report.api.operations["list"] == 2
    assert report.api.operations["watch_open"] == 2
    assert report.api.reconnects == 1
    assert report.api.relists == 1
