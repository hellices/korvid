"""The Prometheus connector (issue #193).

Asks one instant query per call, built from the signal catalogue in
`query`. An instant query over a range selector returns one aggregate per
series rather than a matrix of points, which is what keeps a 6-hour
window affordable for both the backend and the model's context.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
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
    mask_in,
    masked_labels,
    resolve_window,
)
from korvid.obs.http import HttpBackend
from korvid.obs.query import build_metric_query, encoded_forms, metric_unit

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
        mask_labels: frozenset[str] = frozenset(),
    ) -> None:
        self._mask = frozenset(name.lower() for name in mask_labels)
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
        query = build_metric_query(signal, exact, regex, window_minutes=window)
        # Computed before the request: a backend that refuses the query
        # usually quotes it back, and a failure never reaches the
        # success-path projection (round-5 review).
        secrets = encoded_forms(self._masked_scope_values(scope))
        payload = await self._http.get_json("/api/v1/query", {"query": query}, secrets=secrets)
        data = self._http.require_success(payload)
        self._http.require_result_type(data, "vector")
        series, truncated, observed_at = self._parse(data)
        # The scope and the query name the values that were *asked about*,
        # which for a masked label is exactly the value the operator
        # declared sensitive.
        return MetricResult(
            source=SOURCE,
            endpoint=self._http.endpoint,
            signal=signal,
            scope=_masked_scope(scope, secrets),
            window_minutes=window,
            query=mask_in(query, secrets),
            unit=unit,
            series=series,
            truncated=truncated,
            observed_at=observed_at,
        )

    def _masked_scope_values(self, scope: QueryScope) -> tuple[str, ...]:
        """Scope values whose Prometheus label the operator marked sensitive."""
        pairs = (("namespace", scope.namespace), ("pod", scope.pod), ("pod", scope.workload))
        return tuple(value for label, value in pairs if value and label in self._mask)

    def _parse(self, data: Mapping[str, Any]) -> tuple[tuple[Series, ...], bool, str | None]:
        rows = data.get("result")
        if not isinstance(rows, list):
            raise ConnectorError(
                "backend", f"{self._http.endpoint} returned an unexpected result shape"
            )
        cap = self._http.limits.max_series
        parsed: list[Series] = []
        observed_at: str | None = None
        truncated = False
        for row in rows:
            observed_at = observed_at or _observed_at(row)
            entry = _series(row, self._mask)
            if entry is None:
                continue
            if len(parsed) == cap:
                truncated = True
                break
            parsed.append(entry)
        return tuple(parsed), truncated, observed_at


def _observed_at(row: Any) -> str | None:
    """The sample's own timestamp as UTC, or None when it is unusable.

    An instant query answers "as of when"; a window alone is relative, and
    a citation someone rechecks tomorrow needs the absolute moment.
    """
    if not isinstance(row, Mapping):
        return None
    sample = row.get("value")
    if not isinstance(sample, list) or len(sample) != 2:
        return None
    try:
        moment = datetime.fromtimestamp(float(sample[0]), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _masked_scope(scope: QueryScope, secrets: tuple[str, ...]) -> QueryScope:
    """`scope` with every configured-sensitive value replaced."""
    if not secrets:
        return scope
    return QueryScope(
        namespace=mask_in(scope.namespace, secrets),
        workload=mask_in(scope.workload, secrets) if scope.workload else scope.workload,
        pod=mask_in(scope.pod, secrets) if scope.pod else scope.pod,
    )


def _series(row: Any, mask: frozenset[str]) -> Series | None:
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
    return Series(labels=masked_labels(labels, mask), value=value)
