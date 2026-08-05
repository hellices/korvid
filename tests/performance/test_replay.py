"""Real-app replay harness tests (Task 5, issue #186).

Drives the production KorvidApp/WatchManager/ResourceStore/ResourceTable
stack with a synthetic WorkloadProfile and asserts digest correctness,
update accounting, and API telemetry.
"""

from __future__ import annotations

from tests.performance.profile import FailureInjection, WorkloadProfile
from tests.performance.replay import ReplayOptions, run_replay


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
    assert report.object_count == 100
    assert report.final_digest == report.expected_digest
    assert report.dropped_updates == 0
    assert report.rendered_updates == 110
    assert report.input_latency.count > 0
    assert report.api.operations["list"] == 1
    assert report.api.operations["watch_open"] == 1
    assert report.api.operations.get("get", 0) == 0


async def test_replay_gone_reconnects_and_digest_matches() -> None:
    """gone at event 5 triggers one reconnect/re-LIST; final digest drops stale rows."""
    profile = WorkloadProfile(
        schema_version=1,
        id="test-gone",
        seed=186,
        object_count=20,
        namespace_count=4,
        steady_events_per_second=3,
        duration_seconds=2,
        bursts=(),
        failures=(FailureInjection(kind="gone", at_event=5),),
    )
    report = await run_replay(profile, ReplayOptions(time_scale=0))
    assert report.final_digest == report.expected_digest
    assert report.dropped_updates == 0
    assert report.api.operations["list"] == 2
    assert report.api.operations["watch_open"] == 2
    assert report.api.reconnects == 1
    assert report.api.relists == 1
