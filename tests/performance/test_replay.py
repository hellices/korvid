"""Real-app replay harness tests (Task 5, issue #186).

Drives the production KorvidApp/WatchManager/ResourceStore/ResourceTable
stack with a synthetic WorkloadProfile and asserts digest correctness,
update accounting, and API telemetry.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from collections.abc import AsyncIterator
from time import monotonic
from typing import Any, cast

import pytest

from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.models import PodSummary
from korvid.ui.messages import ResourcesUpdated
from korvid.ui.widgets.resource_table import ResourceTable, _cells_equal
from tests.performance import replay as replay_module
from tests.performance.manifests import TICK_LABEL
from tests.performance.metrics import BenchmarkRecorder, RunManifest, UpdateLatencyKind
from tests.performance.pacing import sample_paced_schedule
from tests.performance.profile import Burst, FailureInjection, WorkloadProfile
from tests.performance.replay import (
    MeasuredKorvidApp,
    ReplayAborted,
    ReplayOptions,
    build_manifest,
    measure_cursor_input,
    resolve_korvid_sha,
    run_replay,
)
from tests.performance.workload import apply_events, initial_pods, scheduled_events, summary_digest
from tests.ui.test_app import _pod, make_app
from tests.ui.test_table_diff_update import _plain_refreshes, _spy_refresh
from tests.ui.waits import WaitTimeout, until


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


def _tick_labelled_pod(name: str, tick: str) -> PodSummary:
    """A Pod differing from `_pod(name)` only in an unrendered metadata label.

    Mirrors the live churn driver exactly: it patches
    `korvid.dev/performance-tick` and nothing else, and no Pod column renders
    labels (they feed the client-side `-l` filter only).
    """
    return dataclasses.replace(_pod(name), labels=((TICK_LABEL, tick),))


async def test_measure_cursor_input_returns_when_cursor_row_changes() -> None:
    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        table.focus()

        elapsed = await measure_cursor_input(pilot, table, "down")

        assert elapsed >= 0.0
        assert table.cursor_row == 1


async def test_measure_cursor_input_ignores_unrelated_cursor_row_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        table.focus()
        driver = pilot.app._driver
        assert driver is not None

        def swallow_send(_event: object) -> None:
            return None

        monkeypatch.setattr(driver, "send_message", swallow_send)

        async def unrelated_cursor_move() -> None:
            await asyncio.sleep(0)
            table.move_cursor(row=2)

        move_task = asyncio.create_task(unrelated_cursor_move())

        with pytest.raises(WaitTimeout, match=r"down.*row 0.*0\.01s"):
            await measure_cursor_input(pilot, table, "down", timeout=0.01)

        await move_task
        assert table.cursor_row == 2


async def test_measure_cursor_input_times_out_when_a_valid_move_is_not_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        table.focus()
        driver = pilot.app._driver
        assert driver is not None

        def swallow_send(_event: object) -> None:
            return None

        monkeypatch.setattr(driver, "send_message", swallow_send)

        with pytest.raises(WaitTimeout, match=r"down.*row 0.*0\.01s"):
            await measure_cursor_input(pilot, table, "down", timeout=0.01)


@pytest.mark.parametrize(
    ("key", "start_row", "expected_row"),
    [("up", 0, -1), ("down", 1, 2)],
)
async def test_measure_cursor_input_rejects_out_of_bounds_expected_rows(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    start_row: int,
    expected_row: int,
) -> None:
    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        table.focus()
        table.move_cursor(row=start_row)
        driver = pilot.app._driver
        assert driver is not None

        def fail_send(_event: object) -> None:
            pytest.fail("measure_cursor_input should reject impossible rows before sending input")

        monkeypatch.setattr(driver, "send_message", fail_send)

        message = (
            f"cursor input measurement key {key!r} from start row {start_row} "
            f"expected row {expected_row} outside valid range 0..1"
        )
        with pytest.raises(ValueError, match=re.escape(message)):
            await measure_cursor_input(pilot, table, key)


async def test_measure_cursor_input_rejects_unsupported_keys() -> None:
    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        table.focus()

        with pytest.raises(
            ValueError, match="cursor input measurement supports only 'down' and 'up'"
        ):
            await measure_cursor_input(pilot, table, "left")


def test_replay_options_default_the_input_probe_knobs() -> None:
    assert ReplayOptions().input_ack_timeout == 5.0
    assert ReplayOptions(input_ack_timeout=0.25).input_ack_timeout == 0.25
    assert ReplayOptions().input_sample_pairs == 25
    assert ReplayOptions(input_sample_pairs=3).input_sample_pairs == 3


async def test_replay_passes_the_configured_input_ack_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cursor probe's bound is a run knob, not a constant buried in the
    harness: a slower cluster or a deliberately short abort budget must reach
    `measure_cursor_input` from the same options object the run was given."""
    schedule = sample_paced_schedule(monkeypatch, pairs=3)
    timeouts: list[float] = []
    original = replay_module.measure_cursor_input

    async def spy(*args: object, **kwargs: object) -> float:
        timeouts.append(cast(float, kwargs["timeout"]))
        return await original(*args, **kwargs)  # type: ignore[arg-type]  # test spy

    monkeypatch.setattr(replay_module, "measure_cursor_input", spy)
    profile = WorkloadProfile(
        schema_version=1,
        id="test-input-ack",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=20,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )

    report = await run_replay(profile, schedule.options(input_ack_timeout=2.5))

    assert timeouts == [2.5] * 6
    assert report.input_latency.count == 6


@pytest.mark.parametrize("use_injected_clock", [False, True])
async def test_replay_passes_its_monotonic_clock_to_cursor_sampling(
    monkeypatch: pytest.MonkeyPatch,
    use_injected_clock: bool,
) -> None:
    profile = WorkloadProfile(
        schema_version=1,
        id="test-input-clock",
        seed=186,
        object_count=2,
        namespace_count=1,
        steady_events_per_second=1,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )
    seen: list[object] = []

    async def fake_sample_cursor_input(
        *args: object, now: object = monotonic, **kwargs: object
    ) -> None:
        seen.append(now)

    injected_clock = (lambda: 123.0) if use_injected_clock else None
    monkeypatch.setattr(replay_module, "sample_cursor_input", fake_sample_cursor_input)

    await run_replay(
        profile,
        ReplayOptions(time_scale=0, input_sample_pairs=1, monotonic_fn=injected_clock),
    )

    assert seen == [injected_clock or monotonic]


async def test_replay_takes_the_configured_number_of_cursor_sample_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A percentile over two samples is a point observation, not a percentile.
    The pair count is configurable so the published input figure rests on a
    usable sample size, and each pair is a `down`/`up` round trip so the
    cursor ends where it started - the digest and row checks that follow must
    see an unmoved selection."""
    profile = WorkloadProfile(
        schema_version=1,
        id="test-input-pairs",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=20,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )
    schedule = sample_paced_schedule(monkeypatch, pairs=4)

    report = await run_replay(profile, schedule.options())

    assert report.input_latency.count == 8


async def test_replay_keeps_emitting_churn_events_throughout_input_sampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Input latency is only meaningful against a *moving* update stream, so
    the harness must never hold the schedule after the first event and sample
    a frozen table.

    The interleave is decided by injected seams, never by wall time: the
    schedule's inter-event sleep waits for a permit that each completed cursor
    sample releases, so the emitted-event count observed at successive samples
    keeps climbing instead of freezing at the first event.
    """
    profile = WorkloadProfile(
        schema_version=1,
        id="test-input-during-churn",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=10,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )
    pairs = 3
    total_samples = 2 * pairs
    schedule = sample_paced_schedule(monkeypatch, pairs=pairs)

    sources: list[Any] = []
    original_source = replay_module._ReplaySource

    def capturing_source(*args: Any, **kwargs: Any) -> Any:
        source = original_source(*args, **kwargs)
        sources.append(source)
        return source

    snapshots: list[int] = []
    paced_measure = replay_module.measure_cursor_input

    async def spy(*args: Any, **kwargs: Any) -> float:
        elapsed = await paced_measure(*args, **kwargs)
        snapshots.append(sources[0].emitted_events)
        return elapsed

    monkeypatch.setattr(replay_module, "_ReplaySource", capturing_source)
    monkeypatch.setattr(replay_module, "measure_cursor_input", spy)

    report = await run_replay(profile, schedule.options())

    assert len(snapshots) == total_samples
    assert report.input_latency.count == total_samples
    # The decisive claim: churn is not gated to a single event for the whole
    # probe. More than one event reaches the store while sampling is running.
    assert snapshots[-1] - snapshots[0] >= 2
    assert snapshots == sorted(snapshots)
    assert report.churn_started_before_input


async def test_replay_rejects_input_sampling_when_churn_finishes_early() -> None:
    """The input metric contract requires every sample to happen during active
    churn, so a cleanly finished schedule must fail rather than pad the tail
    with idle cursor moves."""
    profile = WorkloadProfile(
        schema_version=1,
        id="test-input-incomplete",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=1,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )

    with pytest.raises(
        WaitTimeout,
        match="input sampling incomplete: churn finished before all 3 cursor sample pairs completed",
    ):
        await run_replay(profile, ReplayOptions(time_scale=0, input_sample_pairs=3))


async def test_replay_rejects_a_non_positive_input_sample_pair_count() -> None:
    """Zero pairs would report an input percentile computed from no samples;
    a negative count is a programmer error, not a "skip the probe" switch."""
    profile = WorkloadProfile(
        schema_version=1,
        id="test-input-pairs-invalid",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=10,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )

    with pytest.raises(ValueError, match="input_sample_pairs must be positive"):
        await run_replay(profile, ReplayOptions(time_scale=0, input_sample_pairs=0))


@pytest.mark.parametrize(
    "timeout",
    [0.0, -1.0, float("nan"), float("inf"), float("-inf")],
    ids=["zero", "negative", "nan", "positive-infinity", "negative-infinity"],
)
async def test_replay_rejects_a_non_finite_or_non_positive_input_ack_timeout(
    monkeypatch: pytest.MonkeyPatch, timeout: float
) -> None:
    """`asyncio.timeout(inf)`/`nan` never fires, so an unacknowledged key would
    hang the run forever, contradicting the documented bounded probe; a
    non-positive bound aborts before the key can possibly be acknowledged.
    Both are harness misconfiguration and must fail before the app starts."""

    def fail_app(*args: object, **kwargs: object) -> MeasuredKorvidApp:
        pytest.fail("run_replay should reject an invalid input_ack_timeout before app startup")

    monkeypatch.setattr(replay_module, "MeasuredKorvidApp", fail_app)
    profile = WorkloadProfile(
        schema_version=1,
        id="test-input-ack-invalid",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=10,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )

    with pytest.raises(ValueError, match="input_ack_timeout must be finite and positive"):
        await run_replay(profile, ReplayOptions(time_scale=0, input_ack_timeout=timeout))


async def test_replay_rejects_one_object_input_sampling_before_app_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-row table cannot acknowledge the first `down` cursor sample, so the
    benchmark contract rejects that topology before the Textual app starts."""
    profile = WorkloadProfile(
        schema_version=1,
        id="test-one-object",
        seed=186,
        object_count=1,
        namespace_count=1,
        steady_events_per_second=0,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )

    def fail_app(*args: object, **kwargs: object) -> MeasuredKorvidApp:
        pytest.fail("run_replay should reject one-object input sampling before app startup")

    monkeypatch.setattr(replay_module, "MeasuredKorvidApp", fail_app)

    with pytest.raises(
        ValueError, match=r"performance input sampling requires object_count >= 2; got 1"
    ):
        await run_replay(profile, ReplayOptions(time_scale=0, input_sample_pairs=1))


def test_replay_scales_churn_completion_wait_with_profile_duration() -> None:
    """The churn-completion wait must cover the schedule's wall duration at the
    configured `time_scale`, plus the render-drain allowance."""
    profile = WorkloadProfile(
        schema_version=1,
        id="test-churn-wait",
        seed=186,
        object_count=2,
        namespace_count=1,
        steady_events_per_second=1,
        duration_seconds=30,
        bursts=(),
        failures=(),
    )
    timeout = replay_module.replay_churn_completion_timeout(profile, ReplayOptions(time_scale=2.0))

    assert timeout == 30 * 2.0 + replay_module._REPLAY_CHURN_COMPLETION_GRACE_SECONDS


async def test_replay_uses_real_app_and_reaches_expected_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    report = await run_replay(profile, sample_paced_schedule(monkeypatch).options())
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


async def test_replay_time_scale_1_uses_relative_inter_event_delays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """time_scale=1 must use inter-event delays, not absolute offsets.

    Virtual-time seam: `monotonic_fn` returns a shared virtual clock and
    `async_sleep` advances that clock then yields via `asyncio.sleep(0)`,
    so the test completes in ~0 s of wall time regardless of profile length.
    Each scheduled sleep additionally waits for a completed cursor sample, so
    the schedule outlives the input probe without changing a single delay.

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
    schedule = sample_paced_schedule(monkeypatch)

    report = await run_replay(profile, schedule.options())
    assert sum(schedule.delays) == pytest.approx(scheduled_events(profile)[-1].offset_seconds)
    assert report.dropped_updates == 0
    assert report.object_count == 20
    assert report.expected_digest == report.final_digest


async def test_replay_gone_reconnects_and_digest_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    report = await run_replay(profile, sample_paced_schedule(monkeypatch).options())
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


async def test_replay_gone_reconnects_with_time_scale_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    schedule = sample_paced_schedule(monkeypatch)

    report = await run_replay(profile, schedule.options())

    events = scheduled_events(profile)
    failure_sequence = profile.failures[0].at_event
    first_post_reconnect_delay = (
        events[failure_sequence].offset_seconds - events[failure_sequence - 1].offset_seconds
    )
    assert sum(schedule.delays) == pytest.approx(events[-1].offset_seconds)
    assert schedule.delays[failure_sequence - 1] == pytest.approx(first_post_reconnect_delay)
    assert report.expected_digest == report.final_digest
    assert report.dropped_updates == 0
    assert report.api.operations["list"] == 2
    assert report.api.operations["watch_open"] == 2
    assert report.api.reconnects == 1
    assert report.api.relists == 1


async def test_replay_throttled_reconnects_and_digest_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    report = await run_replay(profile, sample_paced_schedule(monkeypatch).options())
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


async def test_replay_slow_delays_without_dropping_or_reconnecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    schedule = sample_paced_schedule(monkeypatch)

    report = await run_replay(profile, schedule.options())
    events = scheduled_events(profile)
    oracle = summary_digest(apply_events(initial_pods(profile), events))

    assert report.expected_digest == oracle
    assert report.final_digest == oracle
    assert report.dropped_updates == 0
    assert report.api.operations["watch_open"] == 1
    assert report.api.reconnects == 0
    # The injected 1/20s stall is an *extra* sleep on top of the schedule.
    assert sum(schedule.delays) == pytest.approx(events[-1].offset_seconds + 1 / 20)


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
    """Zero-event profiles cannot publish cursor samples against an idle table."""
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

    assert scheduled_events(profile) == ()
    with pytest.raises(
        ValueError,
        match="performance input sampling requires at least one scheduled churn event",
    ):
        await run_replay(profile, ReplayOptions(time_scale=0))


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


async def test_metadata_only_event_records_a_render_sample_without_changing_cells() -> None:
    """The update-latency metric times recorder completion, not a repaint.

    The live 24 ev/s workload patches only `korvid.dev/performance-tick`, a
    label no Pod column renders. The in-place diff therefore finds no changed
    cell and requests no repaint, yet `MeasuredKorvidApp.on_resources_updated`
    still calls `record_render`, so the recorded sample spans event receipt to
    *no-op* table-diff completion. That number is real, but it is not a
    rendered-frame measurement and must never be published against the
    event-to-render budget for a metadata-only workload - which is exactly what
    `UpdateLatencyKind` records. Replay is different: its churn rewrites
    phase/ready/restarts, which are rendered cells.
    """
    recorder = BenchmarkRecorder()
    store = ResourceStore()
    pods = [_pod(f"pod-{i:02d}") for i in range(3)]

    async def source(kind: str, _scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for pod in pods if kind == "pods" else []:
            yield ("ADDED", pod)
        await asyncio.Event().wait()

    app = MeasuredKorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source, retry_delay=0.0),
        recorder=recorder,
    )
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        before = list(table.get_row("default/pod-01"))
        refreshes = _spy_refresh(table)

        recorder.record_event(1, monotonic())
        store.apply_event("pods", "default", "MODIFIED", _tick_labelled_pod("pod-01", "7"))
        await until(pilot, lambda: recorder.pending_count() == 0, label="event reached recorder")

        after = list(table.get_row("default/pod-01"))
        assert [_cells_equal(old, new) for old, new in zip(before, after, strict=True)] == [
            True
        ] * len(before)
        assert _plain_refreshes(refreshes) == []

    report = recorder.report(_manifest_for_test(), (), final_digest="d")
    assert report.render_passes == 1
    assert report.update_latency.count == 1


async def test_replay_measures_list_phase_separately_from_watch_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    report = await run_replay(profile, sample_paced_schedule(monkeypatch).options())

    # 10 scheduled watch events, and *only* those, are event-to-render samples.
    assert len(scheduled_events(profile)) == 10
    assert report.update_latency.count == 10
    # Deterministic replay churns phase/ready/restarts - rendered cells - so
    # its samples really are event-to-render and keep the key/label the
    # published 250 ms render budget is stated against.
    assert report.update_latency_kind is UpdateLatencyKind.EVENT_TO_RENDER
    assert report.rendered_updates == 10

    # The LIST-to-populated-table and startup phases are measured explicitly.
    assert report.phases.list_to_populated_table_seconds is not None
    assert report.phases.list_to_populated_table_seconds >= 0.0
    assert report.phases.process_start_to_interactive_seconds is not None
    assert report.phases.process_start_to_interactive_seconds >= 0.0


async def test_replay_records_post_burst_drain_and_backlog_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    report = await run_replay(profile, sample_paced_schedule(monkeypatch).options())

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


async def test_replay_metrics_unavailable_keeps_resource_navigation_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    report = await run_replay(profile, sample_paced_schedule(monkeypatch).options())
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


async def test_replay_slow_logs_do_not_block_resource_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    schedule = sample_paced_schedule(monkeypatch)

    report = await run_replay(profile, schedule.options())
    events = scheduled_events(profile)
    oracle = summary_digest(apply_events(initial_pods(profile), events))

    assert report.expected_digest == oracle
    assert report.final_digest == oracle
    assert report.dropped_updates == 0
    assert report.api.reconnects == 0
    assert report.failures_injected["slow_logs"] == 1
    # A slow log stream adds no extra sleep to the resource schedule.
    assert sum(schedule.delays) == pytest.approx(events[-1].offset_seconds)


async def test_replay_does_not_re_mark_bursts_after_a_watch_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    report = await run_replay(profile, sample_paced_schedule(monkeypatch).options())

    assert report.api.reconnects == 1  # the schedule really was interrupted
    assert len(report.phases.post_burst_drain_seconds) == len(profile.bursts)


def test_the_churn_completion_wait_never_shrinks_the_original_drain_allowance() -> None:
    """Scaling the wait by the schedule must not cut the drain allowance.

    The wait opens right after the first churn event, so it has to cover the
    rest of the schedule *and* the render drain that follows it. Adding only a
    5 s constant on top of the schedule leaves that much for the drain alone,
    where the fixed wait this replaced allowed 30 s. Measured on the committed
    `burst-50k` profile the drain takes ~27.5 s, so the constant would fail a
    healthy run rather than a broken one.
    """
    large = WorkloadProfile(
        schema_version=1,
        id="test-large-drain",
        seed=186,
        object_count=50_000,
        namespace_count=20,
        steady_events_per_second=200,
        duration_seconds=30,
        bursts=(),
        failures=(),
    )

    timeout = replay_module.replay_churn_completion_timeout(large, ReplayOptions(time_scale=1.0))

    assert timeout >= large.duration_seconds + 30.0


def test_the_churn_completion_wait_keeps_the_drain_allowance_when_time_is_compressed() -> None:
    """A programmatic `time_scale` below 1 shortens the schedule but not the
    drain, which is the app's own work at full speed."""
    profile = WorkloadProfile(
        schema_version=1,
        id="test-compressed-drain",
        seed=186,
        object_count=10_000,
        namespace_count=20,
        steady_events_per_second=100,
        duration_seconds=30,
        bursts=(),
        failures=(),
    )

    timeout = replay_module.replay_churn_completion_timeout(profile, ReplayOptions(time_scale=0.0))

    assert timeout >= 30.0


async def test_an_unacknowledged_key_stops_spinning_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key that is never acknowledged must not hold the loop at zero delay
    for the whole timeout.

    The happy path settles in a handful of turns (measured: median 6), so the
    probe keeps a zero-delay spin that long to stay the tightest observer of a
    real acknowledgement. Past that the key is not coming, and continuing to
    spin would starve the churn and render work being measured alongside it —
    the failure path would then inflate the very percentile it belongs to.

    Counts the probe's own sleeps through `_ack_sleep`; patching
    `asyncio.sleep` globally would also count Textual's message pump, whose
    rate differs by platform.
    """
    delays: list[float] = []
    real_sleep = replay_module._ack_sleep

    async def recording_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(delay)

    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        table.focus()
        driver = pilot.app._driver
        assert driver is not None
        monkeypatch.setattr(driver, "send_message", lambda _event: None)
        monkeypatch.setattr(replay_module, "_ack_sleep", recording_sleep)

        with pytest.raises(WaitTimeout, match="down"):
            await measure_cursor_input(pilot, table, "down", timeout=0.2)

    spin = replay_module._ACK_SPIN_TURNS
    assert delays[:spin] == [0] * spin
    assert set(delays[spin:]) == {replay_module._ACK_BACKOFF_SECONDS}
