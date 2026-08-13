"""`query_metrics` / `search_logs` dispatch (issue #193).

The executor is where a model's arguments meet a connector. These tests
pin argument validation, the absent-backend case, and the rule that a
connector failure becomes an error result rather than an exception.
"""

from __future__ import annotations

from typing import Any

import pytest

from korvid.obs.connector import (
    ConnectorError,
    LogLine,
    LogResult,
    LogsConnector,
    MetricResult,
    MetricsConnector,
    QueryScope,
    Series,
)
from korvid.tools.executor import ToolExecutor


class FakeMetrics(MetricsConnector):
    source = "prometheus"

    def __init__(self, error: ConnectorError | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error
        self.closed = False

    async def query(
        self, *, signal: str, scope: QueryScope, window_minutes: object = None
    ) -> MetricResult:
        self.calls.append({"signal": signal, "scope": scope, "window_minutes": window_minutes})
        if self._error is not None:
            raise self._error
        return MetricResult(
            source="prometheus",
            endpoint="prom.example.com",
            signal=signal,
            scope=scope,
            window_minutes=30,
            query="sum(rate(x[30m]))",
            unit="cores",
            series=(Series(labels={"pod": "api-1"}, value=0.5),),
        )

    async def aclose(self) -> None:
        self.closed = True


class FakeLogs(LogsConnector):
    source = "loki"

    def __init__(self, error: ConnectorError | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error
        self.closed = False

    async def search(
        self,
        *,
        scope: QueryScope,
        window_minutes: object = None,
        contains: str | None = None,
        limit: object = None,
    ) -> LogResult:
        self.calls.append(
            {
                "scope": scope,
                "window_minutes": window_minutes,
                "contains": contains,
                "limit": limit,
            }
        )
        if self._error is not None:
            raise self._error
        return LogResult(
            source="loki",
            endpoint="loki.example.com",
            scope=scope,
            window_minutes=15,
            query='{namespace="prod"}',
            lines=(LogLine(timestamp="2026-08-14T00:00:00Z", labels={}, line="boom"),),
        )

    async def aclose(self) -> None:
        self.closed = True


def _executor(
    *, metrics: MetricsConnector | None = None, logs: LogsConnector | None = None
) -> ToolExecutor:
    return ToolExecutor(object(), {}, None, metrics=metrics, logs=logs)  # type: ignore[arg-type]  # kube unused


class TestMetrics:
    async def test_the_scope_reaches_the_connector(self) -> None:
        connector = FakeMetrics()
        result = await _executor(metrics=connector).execute(
            "query_metrics",
            {"signal": "cpu", "namespace": "prod", "workload": "api", "window_minutes": 30},
        )
        call = connector.calls[0]
        assert call["signal"] == "cpu"
        assert call["scope"] == QueryScope(namespace="prod", workload="api")
        assert call["window_minutes"] == 30
        assert "prom.example.com" in result

    async def test_the_rendered_result_reports_its_bounds(self) -> None:
        result = await _executor(metrics=FakeMetrics()).execute(
            "query_metrics", {"signal": "cpu", "namespace": "prod"}
        )
        assert "truncated: no" in result
        assert "window: 30m" in result

    async def test_an_absent_backend_is_an_error_not_a_crash(self) -> None:
        result = await _executor().execute("query_metrics", {"signal": "cpu", "namespace": "n"})
        assert result.startswith("ERROR:")
        assert "no metrics backend is configured" in result
        assert "observability.prometheus.url" in result

    @pytest.mark.parametrize(
        "args",
        [
            {"namespace": "prod"},
            {"signal": "cpu"},
            {"signal": 7, "namespace": "prod"},
            {"signal": "cpu", "namespace": 7},
            {"signal": "cpu", "namespace": "prod", "workload": 7},
            {"signal": "cpu", "namespace": "prod", "pod": []},
        ],
    )
    async def test_a_wrong_typed_argument_never_reaches_the_connector(
        self, args: dict[str, Any]
    ) -> None:
        connector = FakeMetrics()
        result = await _executor(metrics=connector).execute("query_metrics", args)
        assert result.startswith("ERROR:")
        assert connector.calls == []

    async def test_a_connector_failure_becomes_a_marked_error_result(self) -> None:
        connector = FakeMetrics(ConnectorError("permission", "prom.example.com refused"))
        outcome = await _executor(metrics=connector).execute_recorded(
            "query_metrics", {"signal": "cpu", "namespace": "prod"}
        )
        assert outcome.error is True
        assert "refused" in outcome.text

    async def test_the_failure_kind_reaches_the_reader(self) -> None:
        """ "Unreachable" and "forbidden" need different responses."""
        connector = FakeMetrics(ConnectorError("network", "prom.example.com is unreachable"))
        result = await _executor(metrics=connector).execute(
            "query_metrics", {"signal": "cpu", "namespace": "prod"}
        )
        assert "network" in result
        assert "unreachable" in result


class TestLogs:
    async def test_the_scope_and_filter_reach_the_connector(self) -> None:
        connector = FakeLogs()
        await _executor(logs=connector).execute(
            "search_logs",
            {"namespace": "prod", "pod": "api-1", "contains": "boom", "limit": 20},
        )
        call = connector.calls[0]
        assert call["scope"] == QueryScope(namespace="prod", pod="api-1")
        assert call["contains"] == "boom"
        assert call["limit"] == 20

    async def test_an_absent_backend_is_an_error_not_a_crash(self) -> None:
        result = await _executor().execute("search_logs", {"namespace": "prod"})
        assert result.startswith("ERROR:")
        assert "no logs backend is configured" in result
        assert "observability.loki.url" in result

    @pytest.mark.parametrize(
        "args",
        [
            {},
            {"namespace": 7},
            {"namespace": "prod", "contains": 7},
            {"namespace": "prod", "workload": {}},
        ],
    )
    async def test_a_wrong_typed_argument_never_reaches_the_connector(
        self, args: dict[str, Any]
    ) -> None:
        connector = FakeLogs()
        result = await _executor(logs=connector).execute("search_logs", args)
        assert result.startswith("ERROR:")
        assert connector.calls == []

    async def test_a_connector_failure_becomes_a_marked_error_result(self) -> None:
        connector = FakeLogs(ConnectorError("timeout", "loki.example.com did not answer"))
        outcome = await _executor(logs=connector).execute_recorded(
            "search_logs", {"namespace": "prod"}
        )
        assert outcome.error is True
        assert "did not answer" in outcome.text

    async def test_the_lines_are_rendered_with_their_timestamps(self) -> None:
        result = await _executor(logs=FakeLogs()).execute("search_logs", {"namespace": "prod"})
        assert "2026-08-14T00:00:00Z" in result
        assert "boom" in result


class TestIndependence:
    async def test_a_metrics_backend_does_not_enable_the_logs_tool(self) -> None:
        result = await _executor(metrics=FakeMetrics()).execute(
            "search_logs", {"namespace": "prod"}
        )
        assert result.startswith("ERROR:")

    async def test_a_logs_backend_does_not_enable_the_metrics_tool(self) -> None:
        result = await _executor(logs=FakeLogs()).execute(
            "query_metrics", {"signal": "cpu", "namespace": "prod"}
        )
        assert result.startswith("ERROR:")
