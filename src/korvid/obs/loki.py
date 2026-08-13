"""The Loki connector (issue #193).

Scope becomes a label selector through the configured label mappings; the
model's free text becomes a *line filter*, which cannot widen the label
scope no matter what it contains. That asymmetry is the whole safety
argument for letting free text in at all.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from korvid.obs.connector import (
    ConnectorError,
    LogLine,
    LogResult,
    LogsConnector,
    QueryLimits,
    QueryScope,
    resolve_limit,
    resolve_window,
)
from korvid.obs.http import HttpBackend
from korvid.obs.query import build_line_filter, build_selector

SOURCE = "loki"

DEFAULT_LABEL_MAPPINGS: dict[str, str] = {
    "namespace": "namespace",
    "pod": "pod",
    "workload": "app",
}

_NS_PER_MINUTE = 60 * 1_000_000_000


class LokiConnector(LogsConnector):
    """Bounded, read-only centralized log search."""

    source = SOURCE

    def __init__(
        self,
        url: str,
        *,
        client: httpx.AsyncClient,
        limits: QueryLimits,
        token_env: str | None = None,
        token_file: str | None = None,
        tenant: str | None = None,
        label_mappings: Mapping[str, str] | None = None,
    ) -> None:
        self._http = HttpBackend(
            url,
            source=SOURCE,
            client=client,
            limits=limits,
            token_env=token_env,
            token_file=token_file,
            headers={"x-scope-orgid": tenant} if tenant else None,
        )
        self._labels = dict(label_mappings or DEFAULT_LABEL_MAPPINGS)

    @property
    def max_concurrency(self) -> int:
        return self._http.max_concurrency

    async def aclose(self) -> None:
        await self._http.aclose()

    def _selector(self, scope: QueryScope) -> str:
        exact = {self._labels.get("namespace", "namespace"): scope.namespace}
        if scope.pod:
            exact[self._labels.get("pod", "pod")] = scope.pod
        elif scope.workload:
            exact[self._labels.get("workload", "app")] = scope.workload
        return build_selector(exact)

    async def search(
        self,
        *,
        scope: QueryScope,
        window_minutes: object = None,
        contains: str | None = None,
        limit: object = None,
    ) -> LogResult:
        """Lines matching one Kubernetes scope, newest page first.

        Raises:
            ConnectorError: for an over-long window or limit, an unusable
                credential, or any transport/backend failure.
        """
        window = resolve_window(window_minutes, self._http.limits)
        line_limit = resolve_limit(limit, maximum=self._http.limits.max_lines, label="lines")
        query = f"{self._selector(scope)}{build_line_filter(contains)}"
        end = time.time_ns()
        start = end - window * _NS_PER_MINUTE
        payload = await self._http.get_json(
            "/loki/api/v1/query_range",
            {
                "query": query,
                "start": str(start),
                "end": str(end),
                "limit": str(line_limit),
                "direction": "backward",
            },
        )
        data = self._http.require_success(payload)
        lines, truncated = self._parse(data, line_limit)
        return LogResult(
            source=SOURCE,
            endpoint=self._http.endpoint,
            scope=scope,
            window_minutes=window,
            query=query,
            lines=lines,
            truncated=truncated,
        )

    def _parse(self, data: Mapping[str, Any], line_limit: int) -> tuple[tuple[LogLine, ...], bool]:
        streams = data.get("result")
        if not isinstance(streams, list):
            raise ConnectorError(
                "backend", f"{self._http.endpoint} returned an unexpected result shape"
            )
        collected: list[tuple[int, LogLine]] = []
        for stream in streams:
            collected.extend(_stream_lines(stream))
        # Sorted then cut from the *newest* end: `direction=backward` asked
        # for the most recent page, so dropping the oldest overflow keeps
        # the lines the caller asked about.
        collected.sort(key=lambda item: item[0])
        truncated = len(collected) >= line_limit
        kept = collected[-line_limit:] if truncated else collected
        return tuple(line for _, line in kept), truncated


def _stream_lines(stream: Any) -> list[tuple[int, LogLine]]:
    if not isinstance(stream, Mapping):
        return []
    raw_labels = stream.get("stream")
    labels = (
        {str(k): str(v) for k, v in raw_labels.items()} if isinstance(raw_labels, Mapping) else {}
    )
    values = stream.get("values")
    if not isinstance(values, list):
        return []
    lines: list[tuple[int, LogLine]] = []
    for entry in values:
        if not isinstance(entry, list) or len(entry) != 2:
            continue
        try:
            nanos = int(entry[0])
        except (TypeError, ValueError):
            continue
        text = entry[1]
        if not isinstance(text, str):
            continue
        lines.append((nanos, LogLine(timestamp=_iso(nanos), labels=labels, line=text)))
    return lines


def _iso(nanos: int) -> str:
    """Nanosecond epoch as readable UTC, to the millisecond."""
    moment = datetime.fromtimestamp(nanos / 1_000_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
