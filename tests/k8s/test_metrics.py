"""PodMetrics parsing and MetricsPoller behaviour (no real cluster)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from korvid.k8s.errors import ApiStatusError
from korvid.k8s.metrics import MetricsFetch, MetricsPoller, PodMetrics, parse_pod_metrics_list

_METRICS_LIST = {
    "kind": "PodMetricsList",
    "apiVersion": "metrics.k8s.io/v1beta1",
    "items": [
        {
            "metadata": {"name": "web-1", "namespace": "default"},
            "timestamp": "2026-01-01T00:00:00Z",
            "window": "15s",
            "containers": [
                {"name": "nginx", "usage": {"cpu": "100m", "memory": "128Mi"}},
                {"name": "sidecar", "usage": {"cpu": "50m", "memory": "64Mi"}},
            ],
        },
        {
            "metadata": {"name": "db-0", "namespace": "prod"},
            "containers": [{"name": "pg", "usage": {"cpu": "1", "memory": "1Gi"}}],
        },
    ],
}


class TestParsePodMetricsList:
    def test_sums_container_usage(self) -> None:
        metrics = parse_pod_metrics_list(_METRICS_LIST)
        assert metrics[0].name == "web-1"
        assert metrics[0].namespace == "default"
        assert metrics[0].cpu_cores == pytest.approx(0.15)
        assert metrics[0].memory_bytes == 192 * 2**20
        assert metrics[1].cpu_cores == pytest.approx(1.0)
        assert metrics[1].memory_bytes == 2**30

    def test_skips_malformed_items(self) -> None:
        data = {
            "items": [
                {"containers": []},  # no metadata.name
                {"metadata": {"name": "ok", "namespace": "ns"}, "containers": []},
                {
                    "metadata": {"name": "bad-q", "namespace": "ns"},
                    "containers": [{"usage": {"cpu": "wat", "memory": "1Mi"}}],
                },
            ]
        }
        metrics = parse_pod_metrics_list(data)
        assert [m.name for m in metrics] == ["ok"]
        assert metrics[0].cpu_cores == 0.0
        assert metrics[0].memory_bytes == 0

    def test_empty_list(self) -> None:
        assert parse_pod_metrics_list({}) == []


def _fetcher(responses: list[object]) -> tuple[list[str | None], MetricsFetch]:
    """A fetch stub that replays queued responses (lists or exceptions)."""
    calls: list[str | None] = []

    async def fetch(namespace: str | None) -> list[PodMetrics]:
        calls.append(namespace)
        result = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, list)
        return result

    return calls, fetch


_WEB = PodMetrics(name="web-1", namespace="default", cpu_cores=0.1, memory_bytes=2**20)


async def test_poller_polls_and_exposes_lookup() -> None:
    calls, fetch = _fetcher([[_WEB]])
    updates: list[bool] = []
    poller = MetricsPoller(fetch, interval=0.01, on_update=lambda: updates.append(True))
    await poller.start("default")
    try:
        for _ in range(100):
            if poller.get("default", "web-1") is not None:
                break
            await asyncio.sleep(0.01)
        assert poller.get("default", "web-1") == _WEB
        assert poller.get("default", "missing") is None
        assert poller.available
        assert calls[0] == "default"
        assert updates
    finally:
        await poller.stop()


async def test_poller_repolls_on_interval() -> None:
    calls, fetch = _fetcher([[_WEB]])
    poller = MetricsPoller(fetch, interval=0.01)
    await poller.start(None)
    try:
        for _ in range(200):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.01)
        assert len(calls) >= 3
        assert calls[0] is None  # all-namespaces scope
    finally:
        await poller.stop()


async def test_poller_unavailable_on_api_error_clears_data() -> None:
    """metrics-server absent (404) or forbidden (403): degrade to unavailable
    but keep polling - the server may be installed later."""
    calls, fetch = _fetcher([[_WEB], ApiStatusError(404, "NotFound")])
    poller = MetricsPoller(fetch, interval=0.01)
    await poller.start("default")
    try:
        for _ in range(200):
            if not poller.available and len(calls) >= 3:
                break
            await asyncio.sleep(0.01)
        assert not poller.available
        assert poller.get("default", "web-1") is None  # stale data cleared
        assert len(calls) >= 3  # still polling for late installs
    finally:
        await poller.stop()


async def test_poller_recovers_after_error() -> None:
    _calls, fetch = _fetcher([ApiStatusError(503, "Unavailable"), [_WEB]])
    poller = MetricsPoller(fetch, interval=0.01)
    await poller.start("default")
    try:
        for _ in range(200):
            if poller.available:
                break
            await asyncio.sleep(0.01)
        assert poller.available
        assert poller.get("default", "web-1") == _WEB
    finally:
        await poller.stop()


async def test_poller_unexpected_error_does_not_kill_loop() -> None:
    _calls, fetch = _fetcher([RuntimeError("boom"), [_WEB]])
    poller = MetricsPoller(fetch, interval=0.01)
    await poller.start("default")
    try:
        for _ in range(200):
            if poller.available:
                break
            await asyncio.sleep(0.01)
        assert poller.available
    finally:
        await poller.stop()


async def test_poller_restart_switches_scope_and_drops_old_data() -> None:
    calls, fetch = _fetcher([[_WEB]])
    poller = MetricsPoller(fetch, interval=0.01)
    await poller.start("default")
    try:
        for _ in range(100):
            if poller.get("default", "web-1") is not None:
                break
            await asyncio.sleep(0.01)
        await poller.start("prod")  # scope change restarts the loop
        assert poller.get("default", "web-1") is None
        for _ in range(100):
            if "prod" in calls:
                break
            await asyncio.sleep(0.01)
        assert "prod" in calls
    finally:
        await poller.stop()


async def test_poller_stop_is_idempotent_and_cancels() -> None:
    calls, fetch = _fetcher([[_WEB]])
    poller = MetricsPoller(fetch, interval=0.01)
    await poller.start("default")
    await poller.stop()
    await poller.stop()
    count = len(calls)
    await asyncio.sleep(0.05)
    assert len(calls) == count  # no polling after stop


class TestParseGuards:
    def test_skips_non_mapping_items_and_metadata(self) -> None:
        """Review round 1: a null item or non-object metadata must be skipped,
        not abort the whole poll with AttributeError."""
        data: dict[str, Any] = {
            "items": [
                None,
                "bogus",
                {"metadata": None, "containers": []},
                {"metadata": "nope", "containers": []},
                {"metadata": {"name": "ok", "namespace": "ns"}, "containers": []},
            ]
        }
        metrics = parse_pod_metrics_list(data)
        assert [m.name for m in metrics] == ["ok"]

    def test_skips_items_without_namespace(self) -> None:
        """The lookup key is (namespace, name); an item without a namespace
        can never join a pod row."""
        data = {
            "items": [
                {"metadata": {"name": "orphan"}, "containers": []},
                {"metadata": {"name": "ok", "namespace": "ns"}, "containers": []},
            ]
        }
        assert [m.name for m in parse_pod_metrics_list(data)] == ["ok"]

    def test_non_list_items_yields_empty(self) -> None:
        assert parse_pod_metrics_list({"items": "bogus"}) == []


async def test_poller_skips_notify_when_data_unchanged() -> None:
    """Identical successive polls must not re-notify (issue #12 review round 2:
    avoid a full table re-render every interval on a quiet cluster)."""
    calls, fetch = _fetcher([[_WEB]])
    updates: list[bool] = []
    poller = MetricsPoller(fetch, interval=0.01, on_update=lambda: updates.append(True))
    await poller.start("default")
    try:
        for _ in range(200):
            if len(calls) >= 4:
                break
            await asyncio.sleep(0.01)
        assert len(calls) >= 4
        assert len(updates) == 1  # first successful poll only
    finally:
        await poller.stop()


async def test_poller_notifies_again_when_data_changes() -> None:
    changed = PodMetrics(name="web-1", namespace="default", cpu_cores=0.5, memory_bytes=2**21)
    _calls, fetch = _fetcher([[_WEB], [changed]])
    updates: list[bool] = []
    poller = MetricsPoller(fetch, interval=0.01, on_update=lambda: updates.append(True))
    await poller.start("default")
    try:
        for _ in range(200):
            if len(updates) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(updates) == 2
        assert poller.get("default", "web-1") == changed
    finally:
        await poller.stop()


class TestContainerUsageParsing:
    """Review fix (PR #51 r4): per-container usage must survive parsing -
    limits are enforced per container, so severity needs the breakdown."""

    def test_parses_container_breakdown(self) -> None:
        from korvid.k8s.metrics import ContainerUsage

        data = {
            "items": [
                {
                    "metadata": {"name": "web-1", "namespace": "default"},
                    "containers": [
                        {"name": "app", "usage": {"cpu": "100m", "memory": "64Mi"}},
                        {"name": "sidecar", "usage": {"cpu": "5m", "memory": "95Mi"}},
                    ],
                }
            ]
        }
        (m,) = parse_pod_metrics_list(data)
        assert m.containers == (
            ContainerUsage(name="app", cpu_cores=0.1, memory_bytes=64 * 2**20),
            ContainerUsage(name="sidecar", cpu_cores=0.005, memory_bytes=95 * 2**20),
        )
        assert m.memory_bytes == (64 + 95) * 2**20  # totals unchanged
