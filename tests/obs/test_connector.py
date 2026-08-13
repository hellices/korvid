"""The observability connector boundary (issue #193).

These tests pin the parts of the boundary that are policy rather than
transport: what a query may ask for, what it may cost, and what the
rendered answer is required to say about itself.
"""

from __future__ import annotations

import pytest

from korvid.obs.connector import (
    ConnectorError,
    LogLine,
    LogResult,
    MetricResult,
    QueryLimits,
    QueryScope,
    Series,
    render_logs,
    render_metrics,
    resolve_limit,
    resolve_window,
)


def _scope() -> QueryScope:
    return QueryScope(namespace="prod", workload="api")


class TestQueryLimits:
    def test_defaults_are_bounded(self) -> None:
        limits = QueryLimits()
        assert limits.max_window_minutes > 0
        assert limits.max_series > 0
        assert limits.max_lines > 0
        assert limits.max_response_bytes > 0
        assert limits.max_concurrency > 0
        assert limits.timeout_seconds > 0

    @pytest.mark.parametrize(
        "field",
        [
            "max_window_minutes",
            "max_series",
            "max_lines",
            "max_response_bytes",
            "max_concurrency",
        ],
    )
    def test_a_non_positive_limit_is_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            QueryLimits(**{field: 0})

    def test_a_non_positive_timeout_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timeout_seconds"):
            QueryLimits(timeout_seconds=0.0)


class TestResolveWindow:
    def test_an_unset_window_uses_the_default(self) -> None:
        limits = QueryLimits(default_window_minutes=15)
        assert resolve_window(None, limits) == 15

    def test_a_window_within_the_maximum_is_kept(self) -> None:
        assert resolve_window(30, QueryLimits(max_window_minutes=360)) == 30

    def test_a_window_over_the_maximum_is_refused_not_clamped(self) -> None:
        """Silently shrinking the window would answer a different question."""
        with pytest.raises(ConnectorError, match="360") as caught:
            resolve_window(400, QueryLimits(max_window_minutes=360))
        assert caught.value.kind == "limit"

    @pytest.mark.parametrize("value", [0, -5])
    def test_a_non_positive_window_is_refused(self, value: int) -> None:
        with pytest.raises(ConnectorError, match="minutes"):
            resolve_window(value, QueryLimits())

    @pytest.mark.parametrize("value", [1.5, "30", True])
    def test_a_non_integer_window_is_refused(self, value: object) -> None:
        with pytest.raises(ConnectorError, match="minutes"):
            resolve_window(value, QueryLimits())


class TestResolveLimit:
    def test_an_unset_limit_uses_the_maximum(self) -> None:
        assert resolve_limit(None, maximum=200, label="lines") == 200

    def test_a_limit_within_the_maximum_is_kept(self) -> None:
        assert resolve_limit(50, maximum=200, label="lines") == 50

    def test_a_limit_over_the_maximum_is_refused(self) -> None:
        with pytest.raises(ConnectorError, match="200") as caught:
            resolve_limit(500, maximum=200, label="lines")
        assert caught.value.kind == "limit"

    @pytest.mark.parametrize("value", [0, -1, 2.5, "10", False])
    def test_a_non_positive_or_non_integer_limit_is_refused(self, value: object) -> None:
        with pytest.raises(ConnectorError, match="lines"):
            resolve_limit(value, maximum=200, label="lines")


class TestConnectorError:
    def test_the_kind_is_carried_separately_from_the_message(self) -> None:
        error = ConnectorError("auth", "credentials rejected by the endpoint")
        assert error.kind == "auth"
        assert str(error) == "credentials rejected by the endpoint"

    def test_an_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown connector error kind"):
            ConnectorError("plausible", "nope")


class TestRenderMetrics:
    def _result(self, *, truncated: bool = False, series: int = 1) -> MetricResult:
        return MetricResult(
            source="prometheus",
            endpoint="prom.example.com",
            signal="cpu",
            scope=_scope(),
            window_minutes=30,
            query='sum by (pod) (rate(x{namespace="prod"}[30m]))',
            unit="cores",
            series=tuple(Series(labels={"pod": f"api-{i}"}, value=float(i)) for i in range(series)),
            truncated=truncated,
        )

    def test_the_header_states_source_scope_window_and_query(self) -> None:
        text = render_metrics(self._result())
        assert "source: prometheus" in text
        assert "endpoint: prom.example.com" in text.splitlines()
        assert "namespace=prod" in text
        assert "workload=api" in text
        assert "window: 30m" in text
        assert 'sum by (pod) (rate(x{namespace="prod"}[30m]))' in text

    def test_a_complete_result_says_so(self) -> None:
        assert "truncated: no" in render_metrics(self._result())

    def test_a_truncated_result_says_so(self) -> None:
        """A capped answer that reads as complete is a wrong answer."""
        text = render_metrics(self._result(truncated=True, series=3))
        assert "truncated: yes" in text

    def test_each_series_carries_its_labels_and_value(self) -> None:
        text = render_metrics(self._result(series=2))
        assert "pod=api-0" in text
        assert "pod=api-1" in text
        assert "cores" in text

    def test_an_empty_result_is_stated_not_implied(self) -> None:
        """No rows must not read as a screenful the model failed to notice."""
        text = render_metrics(self._result(series=0))
        assert "no series matched" in text


class TestRenderLogs:
    def _result(self, *, truncated: bool = False, lines: int = 1) -> LogResult:
        return LogResult(
            source="loki",
            endpoint="loki.example.com",
            scope=_scope(),
            window_minutes=15,
            query='{namespace="prod"} |= "boom"',
            lines=tuple(
                LogLine(
                    timestamp=f"2026-08-14T00:0{i}:00Z",
                    labels={"pod": "api-1"},
                    line=f"line {i}",
                )
                for i in range(lines)
            ),
            truncated=truncated,
        )

    def test_the_header_states_source_scope_window_and_query(self) -> None:
        text = render_logs(self._result())
        assert "source: loki" in text
        assert "endpoint: loki.example.com" in text.splitlines()
        assert "namespace=prod" in text
        assert "window: 15m" in text
        assert '{namespace="prod"} |= "boom"' in text

    def test_a_truncated_result_says_so(self) -> None:
        assert "truncated: yes" in render_logs(self._result(truncated=True, lines=2))

    def test_each_line_keeps_its_timestamp_and_pod(self) -> None:
        text = render_logs(self._result(lines=2))
        assert "2026-08-14T00:00:00Z" in text
        assert "api-1" in text
        assert "line 1" in text

    def test_an_empty_result_is_stated_not_implied(self) -> None:
        assert "no log lines matched" in render_logs(self._result(lines=0))


class TestTheHeaderCannotBeForged:
    """Round-12 review: header fields are korvid's claims about the result.

    A scope value is a model-supplied tool argument. Emitted verbatim it
    can add a line to the provenance block, so a citation could carry a
    `truncated: no` the connector never made.
    """

    def _result(self, scope: QueryScope) -> LogResult:
        return LogResult(
            source="loki",
            endpoint="loki.example.com",
            scope=scope,
            window_minutes=15,
            query="{}",
            lines=(),
            truncated=True,
        )

    def test_a_newline_in_a_scope_value_cannot_add_a_header_line(self) -> None:
        text = render_logs(self._result(QueryScope(namespace="prod\ntruncated: no")))
        assert "truncated: yes" in text.splitlines()
        assert "truncated: no" not in text.splitlines()

    @pytest.mark.parametrize("char", ["\n", "\r", "\x1b", "\u2028"])
    def test_line_breaking_characters_never_reach_the_header(self, char: str) -> None:
        text = render_logs(self._result(QueryScope(namespace=f"a{char}b")))
        header = text.split("\n\n")[0]
        assert len([line for line in header.splitlines() if line.startswith("scope:")]) == 1

    def test_the_value_is_still_visible_after_flattening(self) -> None:
        """Hiding it would make the header lie about what was asked."""
        text = render_logs(self._result(QueryScope(namespace="a\nb")))
        assert "a b" in text

    def test_a_metric_label_cannot_add_a_line(self) -> None:
        result = MetricResult(
            source="prometheus",
            endpoint="p.example.com",
            signal="cpu",
            scope=QueryScope(namespace="prod"),
            window_minutes=30,
            query="q",
            unit="cores",
            series=(Series(labels={"pod": "a\ntruncated: no"}, value=1.0),),
            truncated=True,
        )
        lines = render_metrics(result).splitlines()
        assert "truncated: yes" in lines
        assert "truncated: no" not in lines


class TestTheRenderedResultFitsTheIngestBudget:
    """Round-12 review: a result cut downstream still claimed to be complete."""

    def _logs(self, count: int, length: int) -> LogResult:
        return LogResult(
            source="loki",
            endpoint="loki.example.com",
            scope=QueryScope(namespace="prod"),
            window_minutes=15,
            query="{}",
            lines=tuple(
                LogLine(timestamp="2026-08-14T00:00:00Z", labels={}, line="x" * length)
                for _ in range(count)
            ),
        )

    def test_a_result_within_the_budget_is_unchanged(self) -> None:
        text = render_logs(self._logs(2, 10), limit=4000)
        assert "truncated: no" in text.splitlines()
        assert len(text) <= 4000

    def test_dropping_entries_to_fit_reports_truncation(self) -> None:
        text = render_logs(self._logs(200, 200), limit=2000)
        assert len(text) <= 2000
        assert "truncated: yes" in text.splitlines()

    def test_one_oversized_line_is_clamped_and_reported(self) -> None:
        text = render_logs(self._logs(1, 50_000), limit=2000)
        assert len(text) <= 2000
        assert "truncated: yes" in text.splitlines()

    def test_the_header_always_survives(self) -> None:
        """Provenance is what a citation needs; entries are what it can lose."""
        text = render_logs(self._logs(200, 200), limit=400)
        assert "endpoint: loki.example.com" in text.splitlines()

    def test_metrics_are_bounded_the_same_way(self) -> None:
        result = MetricResult(
            source="prometheus",
            endpoint="p.example.com",
            signal="cpu",
            scope=QueryScope(namespace="prod"),
            window_minutes=30,
            query="q",
            unit="cores",
            series=tuple(Series(labels={"pod": "p" * 100}, value=1.0) for _ in range(200)),
        )
        text = render_metrics(result, limit=2000)
        assert len(text) <= 2000
        assert "truncated: yes" in text.splitlines()
