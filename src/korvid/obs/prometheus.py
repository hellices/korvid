"""The Prometheus connector (issue #193).

Asks one instant query per call, built from the signal catalogue in
`query`. An instant query over a range selector returns one aggregate per
series rather than a matrix of points, which is what keeps a 6-hour
window affordable for both the backend and the model's context.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

import httpx

from korvid.obs.connector import (
    ConnectorError,
    MetricResult,
    MetricsConnector,
    QueryLimits,
    QueryScope,
    Series,
    resolve_window,
)
from korvid.obs.http import HttpBackend
from korvid.obs.query import build_metric_query, build_selector, metric_unit

SOURCE = "prometheus"


def scope_matchers(scope: QueryScope) -> tuple[dict[str, str], dict[str, str]]:
    """The exact and regex label matchers for `scope`.

    A workload is matched as a pod-name prefix because that is the only
    relationship visible in cAdvisor labels without a second lookup; a pod
    is matched exactly, because guessing there would silently widen the
    answer.
    """
    exact = {"namespace": scope.namespace}
    regex: dict[str, str] = {}
    if scope.pod:
        exact["pod"] = scope.pod
    elif scope.workload:
        regex["pod"] = f"{scope.workload}-"
    return exact, regex


class PrometheusConnector(MetricsConnector):
    """Bounded, read-only metric signals from a Prometheus-compatible API."""

    source = SOURCE

    def __init__(
        self,
        url: str,
        *,
        client: httpx.AsyncClient,
        limits: QueryLimits,
        token_env: str | None = None,
        token_file: str | None = None,
    ) -> None:
        self._http = HttpBackend(
            url,
            source=SOURCE,
            client=client,
            limits=limits,
            token_env=token_env,
            token_file=token_file,
        )

    @property
    def max_concurrency(self) -> int:
        return self._http.max_concurrency

    async def aclose(self) -> None:
        await self._http.aclose()

    async def query(
        self, *, signal: str, scope: QueryScope, window_minutes: object = None
    ) -> MetricResult:
        """One catalogue signal over one Kubernetes scope.

        Raises:
            ConnectorError: for an unknown signal, an over-long window, an
                unusable credential, or any transport/backend failure.
        """
        window = resolve_window(window_minutes, self._http.limits)
        unit = metric_unit(signal)
        exact, regex = scope_matchers(scope)
        query = build_metric_query(signal, build_selector(exact, regex), window)
        payload = await self._http.get_json("/api/v1/query", {"query": query})
        data = self._http.require_success(payload)
        series, truncated = self._parse(data)
        return MetricResult(
            source=SOURCE,
            endpoint=self._http.endpoint,
            signal=signal,
            scope=scope,
            window_minutes=window,
            query=query,
            unit=unit,
            series=series,
            truncated=truncated,
        )

    def _parse(self, data: Mapping[str, Any]) -> tuple[tuple[Series, ...], bool]:
        rows = data.get("result")
        if not isinstance(rows, list):
            raise ConnectorError(
                "backend", f"{self._http.endpoint} returned an unexpected result shape"
            )
        cap = self._http.limits.max_series
        parsed: list[Series] = []
        for row in rows:
            entry = _series(row)
            if entry is None:
                continue
            if len(parsed) == cap:
                return tuple(parsed), True
            parsed.append(entry)
        return tuple(parsed), False


def _series(row: Any) -> Series | None:
    """One vector sample, or None when it is not one korvid can report.

    A single unparsable sample drops out rather than failing the query:
    the answer is still true about the series that did parse, and the
    result already carries the honesty it needs elsewhere (`truncated`).
    """
    if not isinstance(row, Mapping):
        return None
    sample = row.get("value")
    if not isinstance(sample, list) or len(sample) != 2:
        return None
    try:
        value = float(sample[1])
    except (TypeError, ValueError):
        return None
    if not isfinite(value):
        return None
    metric = row.get("metric")
    labels = {str(k): str(v) for k, v in metric.items()} if isinstance(metric, Mapping) else {}
    return Series(labels=labels, value=value)
