"""Real-app replay harness tests (Task 5, issue #186).

Drives the production KorvidApp/WatchManager/ResourceStore/ResourceTable
stack with a synthetic WorkloadProfile and asserts digest correctness,
update accounting, and API telemetry.
"""

from __future__ import annotations

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

    If the delay computation uses each event's absolute offset_seconds instead
    of the elapsed time since churn started, total sleep = sum(all offsets) for
    60 events at 20 eps over 3 s ≈ 91.5 s > the 30 s until() guard, causing an
    AssertionError before any assert below is reached.

    The sleep_callback assertion is a scale-independent deterministic check:
    with the fix, total_sleep ≈ profile.duration_seconds (inter-event delays);
    with the bug, total_sleep ≈ 91.5 s (sum of absolute offsets).
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
    total_sleep: list[float] = [0.0]

    def record_sleep(delay: float) -> None:
        total_sleep[0] += delay

    report = await run_replay(profile, ReplayOptions(time_scale=1, sleep_callback=record_sleep))
    # Scale-independent check: inter-event delays sum to ≈ duration_seconds, not sum_of_offsets.
    # Bug: total_sleep ≈ 91.5 s; fix: total_sleep ≈ 3 s.
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

    With the absolute-offset reconnect bug, gen=1 events (post-reconnect) sleep
    their full absolute offset_seconds values.  For 95 events at 20 eps over
    5 s (offsets 0.30-5.00 s), the total gen=1 sleep ~251.75 s >> the 30 s
    until() guard, causing a deterministic failure even when run in isolation
    (margin 221.75 s, eliminating the pilot-overhead timing race in duration_seconds=2).

    The sleep_callback assertion provides a scale-independent deterministic check:
    with the fix, total_sleep ≈ profile.duration_seconds; with the bug, gen=1 alone
    contributes ≈ 251.75 s.
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
    total_sleep: list[float] = [0.0]

    def record_sleep(delay: float) -> None:
        total_sleep[0] += delay

    report = await run_replay(profile, ReplayOptions(time_scale=1, sleep_callback=record_sleep))

    # Scale-independent check: total inter-event delays ≈ profile.duration_seconds.
    # With the absolute-offset bug: gen=1 alone contributes ≈ 251.75 s.
    assert total_sleep[0] < profile.duration_seconds * 3
    assert report.expected_digest == report.final_digest
    assert report.dropped_updates == 0
    assert report.api.operations["list"] == 2
    assert report.api.operations["watch_open"] == 2
    assert report.api.reconnects == 1
    assert report.api.relists == 1
