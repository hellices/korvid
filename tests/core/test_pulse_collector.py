import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest

from korvid.core.pulse import PulseCoverage, PulseModel
from korvid.core.pulse_rules import PodPulseRule
from korvid.k8s.errors import ApiStatusError, KubeClientError
from korvid.k8s.pulse import PulseLimitError, PulsePage, PulseReader, PulseSource

NOW = datetime(2026, 9, 13, tzinfo=UTC)
PODS = PulseSource("pods", "", "v1", "pods")
EVENTS = PulseSource("events", "", "v1", "events", "type=Warning")
PAGE_FAILURES = (
    ApiStatusError(403, "token=secret"),
    ApiStatusError(404, "missing"),
    ApiStatusError(503, "offline"),
    KubeClientError("token=secret"),
    TimeoutError(),
    PulseLimitError("byte cap"),
)


class Reader(PulseReader):
    def __init__(self, pages: list[PulsePage | Exception]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, str | None, str, int, int]] = []
        self.active = 0
        self.peak = 0
        self.block = False
        self.entered = asyncio.Event()
        self.released = asyncio.Event()

    async def read_pulse_page(
        self,
        source: PulseSource,
        namespace: str | None,
        continuation: str,
        limit: int,
        max_bytes: int,
    ) -> PulsePage:
        self.calls.append((source.key, namespace, continuation, limit, max_bytes))
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.entered.set()
        try:
            if self.block:
                await self.released.wait()
            await asyncio.sleep(0)
            result = self.pages.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            self.active -= 1


def collector_module() -> ModuleType:
    return importlib.import_module("korvid.core.pulse_collector")


def pod_observation(name: str, *, recovered: bool = False) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"namespace": "team-a", "name": name, "uid": f"uid-{name}"},
        "spec": {"containers": [{"name": "worker"}]},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True" if recovered else "False"}],
            "containerStatuses": [
                {
                    "name": "worker",
                    "ready": recovered,
                    "state": (
                        {"running": {}}
                        if recovered
                        else {"waiting": {"reason": "CrashLoopBackOff"}}
                    ),
                }
            ],
        },
    }


async def test_scoped_sources_are_serial_and_have_complete_coverage() -> None:
    reader = Reader([PulsePage(({},), "next"), PulsePage(({},)), PulsePage(())])
    collector = collector_module().PulseCollector(reader, (PODS, EVENTS), clock=lambda: NOW)
    result = await collector.collect("team-a")
    assert [item.source for item in result] == ["pods", "events"]
    assert result[0].objects == ({}, {})
    assert all(item.coverage.state == "complete" for item in result)
    assert all(item.coverage.observed_at == NOW for item in result)
    assert reader.calls == [
        ("pods", "team-a", "", 100, 262144),
        ("pods", "team-a", "next", 100, 262144),
        ("events", "team-a", "", 100, 262144),
    ]
    assert reader.peak == 1


async def test_page_cap_keeps_partial_objects_and_does_not_follow_more() -> None:
    reader = Reader(
        [
            PulsePage(tuple({} for _index in range(100)), "one"),
            PulsePage(tuple({} for _index in range(100)), "two"),
        ]
    )
    result = await collector_module().PulseCollector(reader, (PODS,)).collect(None)
    assert len(result[0].objects) == 200
    assert result[0].coverage.state == "capped"
    assert len(reader.calls) == 2


async def test_repeated_continuation_is_not_success() -> None:
    reader = Reader([PulsePage(({},), "same"), PulsePage(({},), "same")])
    result = await collector_module().PulseCollector(reader, (PODS,)).collect(None)
    assert result[0].coverage.state == "partial"
    assert "repeated" in result[0].coverage.detail
    assert len(reader.calls) == 2


@pytest.mark.parametrize(
    ("failure", "state"),
    [
        (ApiStatusError(403, "token=secret"), "forbidden"),
        (ApiStatusError(404, "missing"), "unavailable"),
        (ApiStatusError(503, "offline"), "failed"),
        (KubeClientError("token=secret"), "failed"),
        (PulseLimitError("byte cap"), "capped"),
    ],
)
async def test_failure_does_not_hide_other_sources_or_expose_error_body(
    failure: Exception, state: str
) -> None:
    reader = Reader([failure, PulsePage(())])
    result = await collector_module().PulseCollector(reader, (PODS, EVENTS)).collect(None)
    assert result[0].coverage.state == state
    assert "secret" not in result[0].coverage.detail
    assert result[1].coverage.state == "complete"


async def test_source_deadline_cancels_request_and_records_failure() -> None:
    reader = Reader([PulsePage(())])
    reader.block = True
    collector = collector_module().PulseCollector(reader, (PODS,), timeout_seconds=0.01)
    result = await collector.collect(None)
    assert result[0].coverage.state == "failed"
    assert "deadline" in result[0].coverage.detail
    assert reader.active == 0


async def test_cancellation_propagates_and_releases_collector() -> None:
    reader = Reader([PulsePage(())])
    reader.block = True
    collector = collector_module().PulseCollector(reader, (PODS,))
    task = asyncio.create_task(collector.collect(None))
    await reader.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError, match=r"^$"):
        await task
    assert reader.active == 0
    reader.block = False
    assert (await collector.collect(None))[0].coverage.state == "complete"


async def test_concurrent_collect_calls_never_overlap_reads() -> None:
    reader = Reader([PulsePage(()) for _index in range(4)])
    collector = collector_module().PulseCollector(reader, (PODS, EVENTS))
    result = await asyncio.gather(collector.collect("one"), collector.collect("two"))
    assert len(result) == 2
    assert reader.peak == 1
    assert [call[1] for call in reader.calls] == ["one", "one", "two", "two"]


async def test_page_failure_preserves_previous_page_evidence() -> None:
    reader = Reader(
        [PulsePage(({"metadata": {"name": "old"}},), "next"), ApiStatusError(403, "forbidden")]
    )
    result = await collector_module().PulseCollector(reader, (PODS,)).collect(None)
    assert result[0].objects == ({"metadata": {"name": "old"}},)
    assert result[0].coverage.state == "partial"
    assert "API 403: snapshot access denied" in result[0].coverage.detail


@pytest.mark.parametrize("failure", PAGE_FAILURES)
async def test_later_page_failure_keeps_observed_problem_in_model(failure: Exception) -> None:
    reader = Reader([PulsePage((pod_observation("seen"),), "next"), failure])
    collector = collector_module().PulseCollector(reader, (PODS,), clock=lambda: NOW)
    (result,) = await collector.collect("team-a")
    model = PulseModel([PodPulseRule()])
    model.reset(1, "team-a")

    model.replace_source(result.source, result.objects, result.coverage)

    snapshot = model.snapshot(NOW)
    assert [(item.target.name, item.reason) for item in snapshot.current] == [
        ("seen", "CrashLoopBackOff")
    ]
    coverage = next(item for item in snapshot.coverage if item.source == "pods")
    assert coverage.state == ("capped" if isinstance(failure, PulseLimitError) else "partial")
    assert coverage.detail == result.coverage.detail
    assert "secret" not in coverage.detail


@pytest.mark.parametrize("failure", PAGE_FAILURES)
async def test_later_page_failure_resolves_only_observed_recovery(failure: Exception) -> None:
    model = PulseModel([PodPulseRule()])
    model.reset(1, "team-a")
    model.replace_source(
        "pods",
        (pod_observation("seen"), pod_observation("unseen")),
        PulseCoverage("pods", "complete", NOW - timedelta(seconds=1)),
    )
    assert {item.target.name for item in model.snapshot(NOW).current} == {"seen", "unseen"}
    reader = Reader([PulsePage((pod_observation("seen", recovered=True),), "next"), failure])
    collector = collector_module().PulseCollector(reader, (PODS,), clock=lambda: NOW)
    (result,) = await collector.collect("team-a")

    model.replace_source(result.source, result.objects, result.coverage)

    snapshot = model.snapshot(NOW)
    assert [item.target.name for item in snapshot.current] == ["unseen"]
    coverage = next(item for item in snapshot.coverage if item.source == "pods")
    assert coverage.state == ("capped" if isinstance(failure, PulseLimitError) else "partial")
    assert coverage.detail == result.coverage.detail


@pytest.mark.parametrize("failure", PAGE_FAILURES)
async def test_later_page_failure_keeps_unknown_warning_observation(failure: Exception) -> None:
    event = {
        "apiVersion": "v1",
        "kind": "Event",
        "type": "Warning",
        "metadata": {"uid": "event-one", "namespace": "team-a", "name": "event-one"},
        "involvedObject": {
            "apiVersion": "example.io/v1",
            "kind": "Widget",
            "namespace": "team-a",
            "name": "sample",
            "uid": "uid-sample",
        },
        "reason": "UnfamiliarControllerReason",
        "message": "A controller reports a problem",
        "lastTimestamp": NOW.isoformat(),
    }
    reader = Reader([PulsePage((event,), "next"), failure])
    collector = collector_module().PulseCollector(reader, (EVENTS,), clock=lambda: NOW)
    (result,) = await collector.collect("team-a")
    model = PulseModel()
    model.reset(1, "team-a")

    model.replace_source(result.source, result.objects, result.coverage)

    snapshot = model.snapshot(NOW)
    assert [(item.target.kind, item.reason) for item in snapshot.recent] == [
        ("Widget", "UnfamiliarControllerReason")
    ]
    assert snapshot.coverage[0].state == (
        "capped" if isinstance(failure, PulseLimitError) else "partial"
    )
    assert snapshot.coverage[0].detail == result.coverage.detail


@pytest.mark.parametrize("failure", PAGE_FAILURES)
async def test_undated_warning_keeps_later_page_failure_explanation(failure: Exception) -> None:
    event = {
        "type": "Warning",
        "metadata": {"uid": "event-one", "namespace": "team-a", "name": "event-one"},
        "reason": "UnfamiliarControllerReason",
    }
    reader = Reader([PulsePage((event,), "next"), failure])
    collector = collector_module().PulseCollector(reader, (EVENTS,), clock=lambda: NOW)
    (result,) = await collector.collect("team-a")
    model = PulseModel()
    model.reset(1, "team-a")

    model.replace_source(result.source, result.objects, result.coverage)

    snapshot = model.snapshot(NOW)
    assert snapshot.recent == ()
    assert snapshot.coverage[0].state == (
        "capped" if isinstance(failure, PulseLimitError) else "partial"
    )
    assert result.coverage.detail in snapshot.coverage[0].detail
    assert "timestamp" in snapshot.coverage[0].detail
    model.record_warning(event, 1, NOW)
    assert model.snapshot(NOW).coverage == snapshot.coverage


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_source_deadline_is_rejected(timeout: Any) -> None:
    with pytest.raises(ValueError, match="deadline"):
        collector_module().PulseCollector(Reader([]), (PODS,), timeout_seconds=timeout)
