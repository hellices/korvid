"""Real-app replay harness tests (Task 5, issue #186).

Drives the production KorvidApp/WatchManager/ResourceStore/ResourceTable
stack with a synthetic WorkloadProfile and asserts digest correctness,
update accounting, and API telemetry.
"""

from __future__ import annotations

import asyncio

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

    Sensitivity: with 60 events at 20 eps over 3 s, sum_of_offsets ~= 91.5 s.
    Under the absolute-offset bug the accumulated delay totals ~= 91.5 s >> 9 s
    (= duration_seconds x 3), so the assertion fails immediately without any
    wall-clock race or timeout dependency.
    With the correct fix, inter-event delays sum ~= 3 s < 9 s -> GREEN.
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
    total_sleep: list[float] = [0.0]

    def virtual_monotonic() -> float:
        return virtual_time[0]

    async def virtual_sleep(delay: float) -> None:
        total_sleep[0] += delay
        virtual_time[0] += delay
        await asyncio.sleep(0)  # yield to event loop without real wall time

    report = await run_replay(
        profile,
        ReplayOptions(time_scale=1, monotonic_fn=virtual_monotonic, async_sleep=virtual_sleep),
    )
    # Bug: total_sleep ≈ 91.5 s; fix: total_sleep ≈ 3 s.  Threshold = 9 s.
    assert total_sleep[0] < profile.duration_seconds * 3
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

    Sensitivity: 95 post-reconnect events (20 eps x 5 s profile minus 5 pre-gone
    events) have sum_of_offsets ~= 251.75 s.  Under the reconnect-reset bug,
    gen=1 sees elapsed=0 and accumulates full absolute offsets -> total_sleep >> 15 s
    (= duration_seconds x 3), failing deterministically without a wall-clock race.
    With the correct fix, total_sleep ~= 5 s < 15 s -> GREEN.
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
    total_sleep: list[float] = [0.0]

    def virtual_monotonic() -> float:
        return virtual_time[0]

    async def virtual_sleep(delay: float) -> None:
        total_sleep[0] += delay
        virtual_time[0] += delay
        await asyncio.sleep(0)  # yield to event loop without real wall time

    report = await run_replay(
        profile,
        ReplayOptions(time_scale=1, monotonic_fn=virtual_monotonic, async_sleep=virtual_sleep),
    )

    # Bug: gen=1 alone contributes ≈ 251.75 s; fix: total ≈ 5 s.  Threshold = 15 s.
    assert total_sleep[0] < profile.duration_seconds * 3
    assert report.expected_digest == report.final_digest
    assert report.dropped_updates == 0
    assert report.api.operations["list"] == 2
    assert report.api.operations["watch_open"] == 2
    assert report.api.reconnects == 1
    assert report.api.relists == 1
