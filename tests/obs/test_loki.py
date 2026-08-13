"""Loki connector behaviour (issue #193).

The model may pass a substring; it may never pass a selector. These tests
pin that split, the bounds on what comes back, and the tenant header.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from korvid.obs.connector import ConnectorError, QueryLimits, QueryScope
from korvid.obs.loki import LokiConnector
from tests.obs import skeleton

SCOPE = QueryScope(namespace="prod", workload="api")

#: Nanosecond epoch for 2026-08-06T07:06:40Z. Any fixed instant does; the
#: tests assert on ordering and formatting, not on the date itself.
NS = 1786_000_000_000_000_000


def _streams(*entries: tuple[str, int, str]) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {"stream": {"pod": pod}, "values": [[str(ts), line]]} for pod, ts, line in entries
            ],
        },
    }


class Recorder:
    def __init__(self, responder: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> Any:
        """Returns a response or an awaitable one; MockTransport allows both."""
        self.requests.append(request)
        if callable(self._responder):
            return self._responder(request)
        return self._responder


def _connector(
    responder: Any, *, limits: QueryLimits | None = None, **kwargs: Any
) -> tuple[LokiConnector, Recorder]:
    recorder = Recorder(responder)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return (
        LokiConnector(
            "https://loki.example.com",
            client=client,
            limits=limits or QueryLimits(),
            **kwargs,
        ),
        recorder,
    )


def _ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


class TestRequestShape:
    async def test_the_search_goes_to_the_range_query_endpoint(self) -> None:
        connector, recorder = _connector(_ok(_streams()))
        await connector.search(scope=SCOPE)
        assert recorder.requests[0].url.path == "/loki/api/v1/query_range"

    async def test_the_scope_becomes_a_label_selector(self) -> None:
        connector, recorder = _connector(_ok(_streams()))
        await connector.search(scope=SCOPE)
        query = recorder.requests[0].url.params["query"]
        assert 'namespace="prod"' in query
        assert 'app="api"' in query

    async def test_configured_label_mappings_rename_the_scope_labels(self) -> None:
        connector, recorder = _connector(
            _ok(_streams()),
            label_mappings={"namespace": "k8s_namespace", "pod": "pod", "workload": "service"},
        )
        await connector.search(scope=SCOPE)
        query = recorder.requests[0].url.params["query"]
        assert 'k8s_namespace="prod"' in query
        assert 'service="api"' in query

    async def test_a_substring_becomes_a_line_filter_not_a_selector(self) -> None:
        connector, recorder = _connector(_ok(_streams()))
        await connector.search(scope=SCOPE, contains="OOMKilled")
        query = recorder.requests[0].url.params["query"]
        assert query.endswith('|= "OOMKilled"')

    async def test_a_hostile_substring_cannot_add_a_selector(self) -> None:
        connector, recorder = _connector(_ok(_streams()))
        await connector.search(scope=SCOPE, contains='x"} | line_format "{{.foo}}')
        query = recorder.requests[0].url.params["query"]
        assert skeleton(query) == '{app="", namespace=""} |= ""'

    async def test_the_window_becomes_the_start_and_end_parameters(self) -> None:
        connector, recorder = _connector(_ok(_streams()))
        await connector.search(scope=SCOPE, window_minutes=30)
        params = recorder.requests[0].url.params
        start, end = int(params["start"]), int(params["end"])
        assert end - start == 30 * 60 * 1_000_000_000

    async def test_the_newest_lines_are_requested_first(self) -> None:
        connector, recorder = _connector(_ok(_streams()))
        await connector.search(scope=SCOPE)
        assert recorder.requests[0].url.params["direction"] == "backward"

    async def test_the_tenant_header_is_sent_when_configured(self) -> None:
        connector, recorder = _connector(_ok(_streams()), tenant="team-a")
        await connector.search(scope=SCOPE)
        assert recorder.requests[0].headers["x-scope-orgid"] == "team-a"

    async def test_no_tenant_header_when_not_configured(self) -> None:
        connector, recorder = _connector(_ok(_streams()))
        await connector.search(scope=SCOPE)
        assert "x-scope-orgid" not in recorder.requests[0].headers


class TestLimits:
    async def test_the_requested_limit_defaults_to_the_configured_maximum(self) -> None:
        connector, recorder = _connector(_ok(_streams()), limits=QueryLimits(max_lines=37))
        await connector.search(scope=SCOPE)
        assert recorder.requests[0].url.params["limit"] == "37"

    async def test_a_smaller_limit_is_honoured(self) -> None:
        connector, recorder = _connector(_ok(_streams()), limits=QueryLimits(max_lines=200))
        await connector.search(scope=SCOPE, limit=5)
        assert recorder.requests[0].url.params["limit"] == "5"

    async def test_a_limit_over_the_maximum_never_reaches_the_backend(self) -> None:
        connector, recorder = _connector(_ok(_streams()), limits=QueryLimits(max_lines=10))
        with pytest.raises(ConnectorError, match="lines"):
            await connector.search(scope=SCOPE, limit=1000)
        assert recorder.requests == []

    async def test_an_over_long_window_never_reaches_the_backend(self) -> None:
        connector, recorder = _connector(_ok(_streams()), limits=QueryLimits(max_window_minutes=60))
        with pytest.raises(ConnectorError, match="60"):
            await connector.search(scope=SCOPE, window_minutes=600)
        assert recorder.requests == []

    async def test_a_full_page_is_reported_as_truncated(self) -> None:
        """Loki stops at the limit; more lines may exist and the model must know."""
        payload = _streams(*[(f"api-{i}", NS + i, "x") for i in range(3)])
        connector, _ = _connector(_ok(payload), limits=QueryLimits(max_lines=3))
        result = await connector.search(scope=SCOPE)
        assert result.truncated is True

    async def test_a_partial_page_is_not_reported_as_truncated(self) -> None:
        payload = _streams(("api-1", NS, "x"))
        connector, _ = _connector(_ok(payload), limits=QueryLimits(max_lines=3))
        result = await connector.search(scope=SCOPE)
        assert result.truncated is False

    async def test_lines_beyond_the_cap_are_dropped(self) -> None:
        """A backend that ignores `limit` must not widen korvid's bound."""
        payload = _streams(*[(f"api-{i}", NS + i, "x") for i in range(10)])
        connector, _ = _connector(_ok(payload), limits=QueryLimits(max_lines=4))
        result = await connector.search(scope=SCOPE)
        assert len(result.lines) == 4
        assert result.truncated is True


class TestParsing:
    async def test_timestamps_become_readable_utc(self) -> None:
        connector, _ = _connector(_ok(_streams(("api-1", NS, "boom"))))
        result = await connector.search(scope=SCOPE)
        assert result.lines[0].timestamp.endswith("Z")
        assert result.lines[0].timestamp.startswith("20")

    async def test_lines_are_returned_oldest_first(self) -> None:
        payload = _streams(("api-1", NS + 2, "third"), ("api-1", NS, "first"))
        connector, _ = _connector(_ok(payload))
        result = await connector.search(scope=SCOPE)
        assert [line.line for line in result.lines] == ["first", "third"]

    async def test_the_stream_labels_ride_with_each_line(self) -> None:
        connector, _ = _connector(_ok(_streams(("api-1", NS, "boom"))))
        result = await connector.search(scope=SCOPE)
        assert result.lines[0].labels["pod"] == "api-1"

    async def test_a_malformed_entry_is_skipped(self) -> None:
        payload = {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [{"stream": {}, "values": [["notanumber", "x"], ["1", "ok"]]}],
            },
        }
        connector, _ = _connector(_ok(payload))
        result = await connector.search(scope=SCOPE)
        assert [line.line for line in result.lines] == ["ok"]

    async def test_the_result_reports_its_own_provenance(self) -> None:
        connector, _ = _connector(_ok(_streams(("api-1", NS, "boom"))))
        result = await connector.search(scope=SCOPE, window_minutes=45)
        assert result.source == "loki"
        assert result.endpoint == "loki.example.com"
        assert result.window_minutes == 45
        assert 'namespace="prod"' in result.query


class TestErrors:
    async def test_a_rejected_credential_is_an_auth_error(self) -> None:
        connector, _ = _connector(httpx.Response(401, text="denied"))
        with pytest.raises(ConnectorError) as caught:
            await connector.search(scope=SCOPE)
        assert caught.value.kind == "auth"

    async def test_a_loki_level_error_is_surfaced(self) -> None:
        connector, _ = _connector(_ok({"status": "error", "error": "parse error at line 1"}))
        with pytest.raises(ConnectorError, match="parse error") as caught:
            await connector.search(scope=SCOPE)
        assert caught.value.kind == "backend"

    async def test_closing_the_connector_closes_the_client(self) -> None:
        recorder = Recorder(_ok(_streams()))
        client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
        connector = LokiConnector("https://loki.example.com", client=client, limits=QueryLimits())
        await connector.aclose()
        assert client.is_closed


class TestRoundOneReviewFindings:
    async def test_colliding_label_mappings_are_refused_at_construction(self) -> None:
        """Config rejects this; a directly-built connector must not slip through.

        Two scope fields on one label leaves a single matcher, so the
        namespace constraint disappears and the search covers the cluster.
        """
        with pytest.raises(ConnectorError, match="app") as caught:
            _connector(
                _ok(_streams()),
                label_mappings={"namespace": "app", "pod": "pod", "workload": "app"},
            )
        assert caught.value.kind == "config"

    async def test_an_out_of_range_timestamp_is_skipped_not_fatal(self) -> None:
        """A syntactically valid integer can still be outside datetime's range."""
        payload = {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"pod": "api-1"},
                        "values": [[str(10**30), "hostile"], [str(NS), "ok"]],
                    }
                ],
            },
        }
        connector, _ = _connector(_ok(payload))
        result = await connector.search(scope=SCOPE)
        assert [line.line for line in result.lines] == ["ok"]

    async def test_a_full_raw_page_is_truncated_even_when_an_entry_is_unusable(self) -> None:
        """Loki applies `limit` to raw entries, so a dropped one still means more exist."""
        payload = {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"pod": "api-1"},
                        "values": [["nonsense", "dropped"], [str(NS), "a"], [str(NS + 1), "b"]],
                    }
                ],
            },
        }
        connector, _ = _connector(_ok(payload), limits=QueryLimits(max_lines=3))
        result = await connector.search(scope=SCOPE)
        assert len(result.lines) == 2
        assert result.truncated is True

    async def test_a_partial_raw_page_is_still_not_truncated(self) -> None:
        payload = {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {"stream": {"pod": "api-1"}, "values": [["nonsense", "dropped"]]},
                ],
            },
        }
        connector, _ = _connector(_ok(payload), limits=QueryLimits(max_lines=3))
        result = await connector.search(scope=SCOPE)
        assert result.truncated is False
