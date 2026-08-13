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
        self.labels: dict[str, str] = {"pod": "api-1"}

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
            series=(Series(labels=self.labels, value=0.5),),
        )

    async def aclose(self) -> None:
        self.closed = True


class FakeLogs(LogsConnector):
    source = "loki"

    def __init__(self, error: ConnectorError | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error
        self.closed = False
        self.line = "boom"

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
            lines=(LogLine(timestamp="2026-08-14T00:00:00Z", labels={}, line=self.line),),
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
        # Whole-line equality, not a substring: the endpoint field is
        # rendered on its own line and the exact form is what a reader
        # (and a citation) relies on.
        assert "endpoint: prom.example.com" in result.splitlines()

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


class TestResultProjection:
    """Issue #193: results are projected before they leave the boundary.

    The embedded agent redacts on the way to a provider, but MCP receives
    this `ToolOutcome` directly, so the pass has to happen here.
    """

    async def test_a_credential_shaped_log_line_is_masked(self) -> None:
        connector = FakeLogs()
        connector.line = "starting with api_key=AKIAIOSFODNN7EXAMPLE and going on"
        result = await _executor(logs=connector).execute("search_logs", {"namespace": "prod"})
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    async def test_the_masking_is_recorded_so_the_user_can_see_it_happened(self) -> None:
        connector = FakeLogs()
        connector.line = "token=ghp_0123456789abcdefghijklmnopqrstuvwxyz"
        outcome = await _executor(logs=connector).execute_recorded(
            "search_logs", {"namespace": "prod"}
        )
        assert outcome.redactions

    async def test_a_credential_shaped_metric_label_is_masked(self) -> None:
        connector = FakeMetrics()
        connector.labels = {"pod": "api-1", "api_key": "AKIAIOSFODNN7EXAMPLE"}
        result = await _executor(metrics=connector).execute(
            "query_metrics", {"signal": "cpu", "namespace": "prod"}
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    async def test_an_ordinary_log_line_survives_intact(self) -> None:
        """Masking that ate the evidence would be worse than none."""
        connector = FakeLogs()
        connector.line = "OOMKilled: container api exceeded its memory limit"
        result = await _executor(logs=connector).execute("search_logs", {"namespace": "prod"})
        assert "OOMKilled: container api exceeded its memory limit" in result

    async def test_the_provenance_header_survives_masking(self) -> None:
        """A citation needs the endpoint, window and truncation status."""
        connector = FakeLogs()
        connector.line = "password=hunter2"
        result = await _executor(logs=connector).execute("search_logs", {"namespace": "prod"})
        lines = result.splitlines()
        assert "endpoint: loki.example.com" in lines
        assert "truncated: no" in lines
        assert "hunter2" not in result
