"""Real-app replay harness tests (Task 5, issue #186).

Drives the production KorvidApp/WatchManager/ResourceStore/ResourceTable
stack with a synthetic WorkloadProfile and asserts digest correctness,
update accounting, and API telemetry.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from time import monotonic

import pytest

from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.ui.messages import ResourcesUpdated
from tests.performance.metrics import BenchmarkRecorder, RunManifest
from tests.performance.profile import Burst, FailureInjection, WorkloadProfile
from tests.performance.replay import (
    MeasuredKorvidApp,
    ReplayAborted,
    ReplayOptions,
    build_manifest,
    resolve_korvid_sha,
    run_replay,
)
from tests.performance.workload import apply_events, initial_pods, scheduled_events, summary_digest


async def _never_watch(_kind: str, _scope: str) -> AsyncIterator[tuple[str, Summary]]:
    """A watch source that never yields: the render-accounting test drives the
    app directly and must not race a background stream."""
    await asyncio.Event().wait()
    # Unreachable; present so the function is an async *generator*, which is
    # what `WatchSource` requires.
    yield ("ADDED", initial_pods(_manifest_profile())[0])


def _manifest_profile() -> WorkloadProfile:
    return WorkloadProfile(
        schema_version=1,
        id="render-accounting",
        seed=1,
        object_count=1,
        namespace_count=1,
        steady_events_per_second=0,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )


def _manifest_for_test() -> RunManifest:
    return build_manifest(_manifest_profile())


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
    assert report.rendered_updates == 10
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


async def test_replay_throttled_reconnects_and_digest_matches() -> None:
    """A 429 ends one watch connection; `WatchManager` retries, the source
    re-LISTs from its tracked state, and the run still reaches the oracle
    digest with zero drops. The throttled event itself is never delivered."""
    profile = WorkloadProfile(
        schema_version=1,
        id="test-throttled",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=20,
        duration_seconds=2,
        bursts=(),
        failures=(FailureInjection(kind="throttled", at_event=5),),
    )
    report = await run_replay(profile, ReplayOptions(time_scale=0))
    events = scheduled_events(profile)
    applied = tuple(e for e in events if e.sequence != 5)
    oracle = summary_digest(apply_events(initial_pods(profile), applied))

    assert report.expected_digest == oracle
    assert report.final_digest == oracle
    assert report.dropped_updates == 0
    assert report.api.operations["list"] == 2
    assert report.api.operations["watch_open"] == 2
    assert report.api.reconnects == 1
    assert report.api.throttles == 1
    # A 429 is not a 410: it must not be counted as a re-LIST recovery.
    assert report.api.relists == 0


async def test_replay_slow_delays_without_dropping_or_reconnecting() -> None:
    """`slow` delays one event by one steady-rate tick and still delivers it:
    no reconnect, no drop, and the stall is real extra time.

    The stall is injected at the *last* scheduled event on purpose. Mid-run the
    absolute-offset schedule silently absorbs a one-tick stall (the following
    event's delay simply shrinks by the same tick), so only a stall with no
    remaining schedule to catch up in is observable as extra virtual time:
    correct behaviour totals `last offset + one tick`, while ignoring or
    dropping the `slow` injection totals exactly `last offset`.
    """
    profile = WorkloadProfile(
        schema_version=1,
        id="test-slow",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=20,
        duration_seconds=2,
        bursts=(),
        failures=(FailureInjection(kind="slow", at_event=40),),
    )
    virtual_time: list[float] = [0.0]
    sleep_delays: list[float] = []

    def virtual_monotonic() -> float:
        return virtual_time[0]

    async def virtual_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        virtual_time[0] += delay
        await asyncio.sleep(0)

    report = await run_replay(
        profile,
        ReplayOptions(time_scale=1, monotonic_fn=virtual_monotonic, async_sleep=virtual_sleep),
    )
    events = scheduled_events(profile)
    oracle = summary_digest(apply_events(initial_pods(profile), events))

    assert report.expected_digest == oracle
    assert report.final_digest == oracle
    assert report.dropped_updates == 0
    assert report.api.operations["watch_open"] == 1
    assert report.api.reconnects == 0
    # The injected 1/20s stall is an *extra* sleep on top of the schedule.
    assert sum(sleep_delays) == pytest.approx(events[-1].offset_seconds + 1 / 20)


async def test_replay_forbidden_aborts_with_an_explicit_terminal_error() -> None:
    """403 is an authorization boundary: `WatchManager` never reconnects and
    clears the store, so the run can never complete. That must surface at once
    as a named terminal failure instead of a 30-second `until` timeout on a
    permanently empty backlog."""
    profile = WorkloadProfile(
        schema_version=1,
        id="test-forbidden",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=20,
        duration_seconds=2,
        bursts=(),
        failures=(FailureInjection(kind="forbidden", at_event=5),),
    )
    with pytest.raises(ReplayAborted, match="403"):
        await run_replay(profile, ReplayOptions(time_scale=0))


async def test_replay_churn_started_before_input_is_false_without_any_events() -> None:
    """The flag must be a real emitted-event signal: a profile that schedules
    no churn at all cannot claim churn was active during input measurement."""
    profile = WorkloadProfile(
        schema_version=1,
        id="test-no-churn",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=0,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )
    report = await run_replay(profile, ReplayOptions(time_scale=0))

    assert scheduled_events(profile) == ()
    assert not report.churn_started_before_input
    assert report.input_latency.count > 0


async def test_measured_app_counts_only_resource_update_renders() -> None:
    """`_render_table` is also called by cursor/filter/sort/split-pane paths.
    Counting those inflates `render_passes` and lets an unrelated repaint flush
    the pending-event backlog, so only store-driven renders may be recorded."""
    recorder = BenchmarkRecorder()
    app = MeasuredKorvidApp(
        config=KorvidConfig(namespace=ALL_NAMESPACES),
        store=ResourceStore(),
        watch_manager=WatchManager(ResourceStore(), _never_watch, retry_delay=0.0),
        recorder=recorder,
    )
    async with app.run_test():
        recorder.record_event(1, monotonic())
        app._render_table("pods")
        assert recorder.pending_count() == 1

        app.on_resources_updated(ResourcesUpdated("pods"))
        assert recorder.pending_count() == 0

    report = recorder.report(_manifest_for_test(), (), final_digest="d")
    assert report.render_passes == 1
    assert report.rendered_updates == 1


async def test_replay_measures_list_phase_separately_from_watch_events() -> None:
    """Initial LIST rows must not be counted as event-to-render samples; they
    are timed as a separate LIST-to-populated-table startup phase, so replay
    p95 is comparable with the live watch-only event-to-render metric."""
    profile = WorkloadProfile(
        schema_version=1,
        id="list-sep",
        seed=186,
        object_count=100,
        namespace_count=10,
        steady_events_per_second=10,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )
    report = await run_replay(profile, ReplayOptions(time_scale=0))

    # 10 scheduled watch events, and *only* those, are event-to-render samples.
    assert len(scheduled_events(profile)) == 10
    assert report.event_to_render.count == 10
    assert report.rendered_updates == 10

    # The LIST-to-populated-table and startup phases are measured explicitly.
    assert report.phases.list_to_populated_table_seconds is not None
    assert report.phases.list_to_populated_table_seconds >= 0.0
    assert report.phases.process_start_to_interactive_seconds is not None
    assert report.phases.process_start_to_interactive_seconds >= 0.0


async def test_replay_records_post_burst_drain_and_backlog_depth() -> None:
    """A burst produces a measurable backlog and a post-burst drain sample."""
    profile = WorkloadProfile(
        schema_version=1,
        id="burst-drain",
        seed=186,
        object_count=50,
        namespace_count=5,
        steady_events_per_second=5,
        duration_seconds=3,
        bursts=(Burst(start_second=1, duration_seconds=1, events_per_second=40),),
        failures=(),
    )
    report = await run_replay(profile, ReplayOptions(time_scale=0))

    assert report.phases.max_backlog_depth >= 1
    assert report.phases.post_burst_drain_seconds != ()
    assert report.phases.max_post_burst_drain_seconds is not None


def test_resolve_korvid_sha_prefers_github_sha_then_git_head() -> None:
    sha = "a" * 40
    other = "b" * 40
    assert resolve_korvid_sha(env={"GITHUB_SHA": sha}, git_head=lambda: other) == sha
    assert resolve_korvid_sha(env={}, git_head=lambda: other) == other
    # A non-immutable / missing value resolves to None rather than a fake SHA.
    assert resolve_korvid_sha(env={"GITHUB_SHA": "dev"}, git_head=lambda: None) is None


def test_build_manifest_records_resolved_sha() -> None:
    sha = "c" * 40
    manifest = build_manifest(_manifest_profile(), korvid_sha=sha)
    assert manifest.korvid_sha == sha


def test_build_manifest_marks_unresolved_offline_sha_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.performance.replay as replay_module

    monkeypatch.setattr(replay_module, "resolve_korvid_sha", lambda: None)
    manifest = build_manifest(_manifest_profile())
    assert manifest.korvid_sha == "unknown"


async def test_replay_metrics_unavailable_keeps_resource_navigation_healthy() -> None:
    """`metrics_unavailable` records evidence on the metrics read path while the
    resource watch/render reaches the oracle digest with zero drops - proving
    navigation is independent of the metrics poller."""
    profile = WorkloadProfile(
        schema_version=1,
        id="metrics-unavail",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=20,
        duration_seconds=2,
        bursts=(),
        failures=(FailureInjection(kind="metrics_unavailable", at_event=5),),
    )
    report = await run_replay(profile, ReplayOptions(time_scale=0))
    events = scheduled_events(profile)
    oracle = summary_digest(apply_events(initial_pods(profile), events))

    # The failing event is still delivered (not a hard fault), so no filtering.
    assert report.expected_digest == oracle
    assert report.final_digest == oracle
    assert report.dropped_updates == 0
    assert report.api.reconnects == 0
    assert report.failures_injected["metrics_unavailable"] == 1
    # Evidence lands on the metrics read path, never the pods path.
    assert "/apis/metrics.k8s.io/v1beta1/pods" in report.api.paths


async def test_replay_slow_logs_do_not_block_resource_progress() -> None:
    """`slow_logs` records evidence on the log read path and adds no delay to
    the resource schedule - resource watch/render progress is independent of log
    consumption."""
    profile = WorkloadProfile(
        schema_version=1,
        id="slow-logs",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=20,
        duration_seconds=2,
        bursts=(),
        failures=(FailureInjection(kind="slow_logs", at_event=40),),
    )
    virtual_time: list[float] = [0.0]
    sleep_delays: list[float] = []

    def virtual_monotonic() -> float:
        return virtual_time[0]

    async def virtual_sleep(delay: float) -> None:
        sleep_delays.append(delay)
        virtual_time[0] += delay
        await asyncio.sleep(0)

    report = await run_replay(
        profile,
        ReplayOptions(time_scale=1, monotonic_fn=virtual_monotonic, async_sleep=virtual_sleep),
    )
    events = scheduled_events(profile)
    oracle = summary_digest(apply_events(initial_pods(profile), events))

    assert report.expected_digest == oracle
    assert report.final_digest == oracle
    assert report.dropped_updates == 0
    assert report.api.reconnects == 0
    assert report.failures_injected["slow_logs"] == 1
    # A slow log stream adds no extra sleep to the resource schedule.
    assert sum(sleep_delays) == pytest.approx(events[-1].offset_seconds)


async def test_replay_does_not_re_mark_bursts_after_a_watch_reconnect() -> None:
    """A reconnect must not replay burst boundaries that already passed.

    `_ReplaySource` restarts at `_next_event_index` on a new generation. With
    the burst cursor reset to zero, the first event after a 410 re-marks every
    burst that already ended, producing duplicate and time-shifted drain
    samples for a run that had exactly one burst.
    """
    profile = WorkloadProfile(
        schema_version=1,
        id="burst-reconnect",
        seed=186,
        object_count=50,
        namespace_count=5,
        steady_events_per_second=5,
        duration_seconds=4,
        bursts=(Burst(start_second=1, duration_seconds=1, events_per_second=40),),
        # A 410 forces the watch manager to drop and re-list mid-schedule,
        # after the burst window has already closed.
        failures=(FailureInjection(kind="gone", at_event=50),),
    )

    report = await run_replay(profile, ReplayOptions(time_scale=0))

    assert report.api.reconnects == 1  # the schedule really was interrupted
    assert len(report.phases.post_burst_drain_seconds) == len(profile.bursts)
