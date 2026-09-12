"""Prometheus connector transport behaviour (issue #193).

Every test drives a deterministic in-process transport: what matters here
is what korvid sends, what it refuses to send, and what it does with an
answer that is too big, too slow, or hostile.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
import pytest

from korvid.obs.connector import ConnectorError, QueryLimits, QueryScope, render_metrics
from korvid.obs.http import Answer
from korvid.obs.prometheus import PrometheusConnector, _series


def _vector(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"pod": pod}, "value": [1786_000_000, value]} for pod, value in pairs
            ],
        },
    }


class Recorder:
    """Captures every request the connector makes."""

    def __init__(self, responder: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> Any:
        """Returns a response or an awaitable one; MockTransport allows both."""
        self.requests.append(request)
        if callable(self._responder):
            return self._responder(request)
        return self._responder


def _client(responder: Any) -> tuple[httpx.AsyncClient, Recorder]:
    recorder = Recorder(responder)
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder)), recorder


def _connector(
    responder: Any, *, limits: QueryLimits | None = None, **kwargs: Any
) -> tuple[PrometheusConnector, Recorder]:
    client, recorder = _client(responder)
    return (
        PrometheusConnector(
            "https://prom.example.com",
            client=client,
            limits=limits or QueryLimits(),
            **kwargs,
        ),
        recorder,
    )


def _ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


SCOPE = QueryScope(namespace="prod", workload="api")


class TestRequestShape:
    async def test_the_query_is_sent_to_the_instant_query_endpoint(self) -> None:
        connector, recorder = _connector(_ok(_vector(("api-1", "0.5"))))
        await connector.query(signal="cpu", scope=SCOPE, window_minutes=30)
        assert recorder.requests[0].url.path == "/api/v1/query"

    async def test_a_base_path_in_the_url_is_preserved(self) -> None:
        client, recorder = _client(_ok(_vector()))
        connector = PrometheusConnector(
            "https://prom.example.com/prometheus", client=client, limits=QueryLimits()
        )
        await connector.query(signal="cpu", scope=SCOPE)
        assert recorder.requests[0].url.path == "/prometheus/api/v1/query"

    async def test_the_scope_becomes_label_matchers(self) -> None:
        connector, recorder = _connector(_ok(_vector()))
        await connector.query(signal="cpu", scope=SCOPE, window_minutes=30)
        query = recorder.requests[0].url.params["query"]
        assert 'namespace="prod"' in query
        assert 'pod=~"api' in query

    async def test_a_pod_scope_matches_exactly_not_by_prefix(self) -> None:
        """A workload is a pod-name prefix; a pod is the pod."""
        connector, recorder = _connector(_ok(_vector()))
        await connector.query(signal="cpu", scope=QueryScope(namespace="prod", pod="api-1-abc"))
        query = recorder.requests[0].url.params["query"]
        assert 'pod="api-1-abc"' in query

    async def test_the_window_becomes_the_range_selector(self) -> None:
        connector, recorder = _connector(_ok(_vector()))
        await connector.query(signal="cpu", scope=SCOPE, window_minutes=7)
        assert "[7m]" in recorder.requests[0].url.params["query"]

    async def test_the_result_reports_the_query_it_ran(self) -> None:
        connector, _ = _connector(_ok(_vector(("api-1", "0.5"))))
        result = await connector.query(signal="cpu", scope=SCOPE, window_minutes=30)
        assert 'namespace="prod"' in result.query
        assert result.window_minutes == 30
        assert result.source == "prometheus"

    async def test_the_endpoint_is_reported_as_a_host_without_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROM_TOKEN", "secret")
        connector, _ = _connector(_ok(_vector()), token_env="PROM_TOKEN")
        result = await connector.query(signal="cpu", scope=SCOPE)
        assert result.endpoint == "prom.example.com"

    async def test_a_url_carrying_a_credential_is_refused_outright(self) -> None:
        """It would be sent as Basic auth — an inline credential, not a URL."""
        client, _ = _client(_ok(_vector()))
        with pytest.raises(ConnectorError, match="token_env") as caught:
            PrometheusConnector(
                "https://user:hunter2@prom.example.com", client=client, limits=QueryLimits()
            )
        assert "hunter2" not in str(caught.value)


class TestCredentials:
    async def test_no_authorization_header_without_a_configured_source(self) -> None:
        connector, recorder = _connector(_ok(_vector()))
        await connector.query(signal="cpu", scope=SCOPE)
        assert "authorization" not in recorder.requests[0].headers

    async def test_the_token_is_read_from_the_environment_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connector, recorder = _connector(_ok(_vector()), token_env="PROM_TOKEN")
        monkeypatch.setenv("PROM_TOKEN", "secret-one")
        await connector.query(signal="cpu", scope=SCOPE)
        monkeypatch.setenv("PROM_TOKEN", "secret-two")
        await connector.query(signal="cpu", scope=SCOPE)
        assert recorder.requests[0].headers["authorization"] == "Bearer secret-one"
        assert recorder.requests[1].headers["authorization"] == "Bearer secret-two"

    async def test_a_token_file_is_read_and_stripped(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "token"
        path.write_text("file-token\n")
        connector, recorder = _connector(_ok(_vector()), token_file=str(path))
        await connector.query(signal="cpu", scope=SCOPE)
        assert recorder.requests[0].headers["authorization"] == "Bearer file-token"

    async def test_a_missing_environment_variable_is_a_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PROM_TOKEN", raising=False)
        connector, recorder = _connector(_ok(_vector()), token_env="PROM_TOKEN")
        with pytest.raises(ConnectorError, match="PROM_TOKEN") as caught:
            await connector.query(signal="cpu", scope=SCOPE)
        assert caught.value.kind == "config"
        assert recorder.requests == []

    async def test_a_missing_token_file_is_a_config_error(self, tmp_path: Any) -> None:
        connector, _ = _connector(_ok(_vector()), token_file=str(tmp_path / "absent"))
        with pytest.raises(ConnectorError, match="absent") as caught:
            await connector.query(signal="cpu", scope=SCOPE)
        assert caught.value.kind == "config"

    async def test_no_error_message_contains_the_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROM_TOKEN", "super-secret-value")
        connector, _ = _connector(httpx.Response(401, text="denied"), token_env="PROM_TOKEN")
        with pytest.raises(ConnectorError) as caught:
            await connector.query(signal="cpu", scope=SCOPE)
        assert "super-secret-value" not in str(caught.value)


class TestLimitsAreEnforcedBeforeTheRequest:
    async def test_an_over_long_window_never_reaches_the_backend(self) -> None:
        connector, recorder = _connector(_ok(_vector()), limits=QueryLimits(max_window_minutes=60))
        with pytest.raises(ConnectorError, match="60"):
            await connector.query(signal="cpu", scope=SCOPE, window_minutes=600)
        assert recorder.requests == []

    async def test_an_unknown_signal_never_reaches_the_backend(self) -> None:
        connector, recorder = _connector(_ok(_vector()))
        with pytest.raises(ConnectorError, match="unknown signal"):
            await connector.query(signal="everything", scope=SCOPE)
        assert recorder.requests == []

    async def test_an_empty_namespace_never_reaches_the_backend(self) -> None:
        connector, recorder = _connector(_ok(_vector()))
        with pytest.raises(ConnectorError, match="namespace"):
            await connector.query(signal="cpu", scope=QueryScope(namespace=""))
        assert recorder.requests == []


class TestResponseBounds:
    async def test_malformed_samples_after_the_series_cap_are_counted(self) -> None:
        payload = _vector(("kept", "1.5"), ("over-cap", "2"))
        payload["data"]["result"][0]["value"][0] = "not-a-timestamp"
        payload["data"]["result"].extend(
            ["junk", {"metric": {}}, {"metric": {}, "value": [1786_000_000, "NaN"]}]
        )
        connector, _ = _connector(_ok(payload), limits=QueryLimits(max_series=1))

        result = await connector.query(signal="cpu", scope=SCOPE)

        assert [(series.labels["pod"], series.value) for series in result.series] == [("kept", 1.5)]
        assert result.truncated is True
        assert result.omitted_entries == 3
        assert result.observed_at is None
        assert "omitted unusable entries: 3" in render_metrics(result)

    async def test_series_beyond_the_cap_are_dropped_and_reported(self) -> None:
        payload = _vector(*[(f"api-{i}", "1") for i in range(10)])
        connector, _ = _connector(_ok(payload), limits=QueryLimits(max_series=3))
        result = await connector.query(signal="cpu", scope=SCOPE)
        assert len(result.series) == 3
        assert result.truncated is True

    async def test_a_result_within_the_cap_is_not_marked_truncated(self) -> None:
        connector, _ = _connector(_ok(_vector(("api-1", "1"))), limits=QueryLimits(max_series=3))
        result = await connector.query(signal="cpu", scope=SCOPE)
        assert result.truncated is False

    async def test_a_body_over_the_byte_cap_is_refused(self) -> None:
        payload = _vector(*[(f"api-{i}", "1") for i in range(500)])
        connector, _ = _connector(_ok(payload), limits=QueryLimits(max_response_bytes=256))
        with pytest.raises(ConnectorError, match="bytes") as caught:
            await connector.query(signal="cpu", scope=SCOPE)
        assert caught.value.kind == "limit"

    async def test_a_declared_content_length_over_the_cap_is_refused(self) -> None:
        """The cap must not depend on the body actually arriving."""
        body = json.dumps(_vector(*[(f"api-{i}", "1") for i in range(500)])).encode()
        connector, _ = _connector(
            httpx.Response(200, content=body, headers={"content-type": "application/json"}),
            limits=QueryLimits(max_response_bytes=128),
        )
        with pytest.raises(ConnectorError, match="bytes"):
            await connector.query(signal="cpu", scope=SCOPE)

    async def test_concurrent_queries_stay_within_the_configured_bound(self) -> None:
        in_flight = 0
        peak = 0

        async def responder(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            # Yield so every admitted request is in flight at once: without
            # this the loop serialises the handlers and any bound passes.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            in_flight -= 1
            return _ok(_vector())

        connector, _ = _connector(responder, limits=QueryLimits(max_concurrency=2))
        await asyncio.gather(
            *(connector.query(signal="cpu", scope=SCOPE) for _ in range(6)),
        )
        assert peak == 2

    async def test_without_a_bound_the_probe_would_see_every_request_in_flight(self) -> None:
        """Pins the probe itself: it can distinguish bounded from unbounded."""
        in_flight = 0
        peak = 0

        async def responder(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            in_flight -= 1
            return _ok(_vector())

        connector, _ = _connector(responder, limits=QueryLimits(max_concurrency=6))
        await asyncio.gather(
            *(connector.query(signal="cpu", scope=SCOPE) for _ in range(6)),
        )
        assert peak == 6


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (401, "auth"),
            (403, "permission"),
            (404, "config"),
            (429, "backend"),
            (500, "backend"),
            (503, "backend"),
        ],
    )
    async def test_status_codes_map_to_distinguishable_kinds(self, status: int, kind: str) -> None:
        connector, _ = _connector(httpx.Response(status, text="nope"))
        with pytest.raises(ConnectorError) as caught:
            await connector.query(signal="cpu", scope=SCOPE)
        assert caught.value.kind == kind

    async def test_every_error_names_the_endpoint_host(self) -> None:
        connector, _ = _connector(httpx.Response(500, text="nope"))
        with pytest.raises(ConnectorError, match=re.escape("prom.example.com")):
            await connector.query(signal="cpu", scope=SCOPE)

    async def test_a_timeout_is_reported_as_a_timeout(self) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        connector, _ = _connector(responder)
        with pytest.raises(ConnectorError) as caught:
            await connector.query(signal="cpu", scope=SCOPE)
        assert caught.value.kind == "timeout"

    async def test_a_transport_failure_is_reported_as_a_network_error(self) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        connector, _ = _connector(responder)
        with pytest.raises(ConnectorError) as caught:
            await connector.query(signal="cpu", scope=SCOPE)
        assert caught.value.kind == "network"

    async def test_a_non_json_body_is_a_backend_error(self) -> None:
        connector, _ = _connector(httpx.Response(200, text="<html>hello</html>"))
        with pytest.raises(ConnectorError) as caught:
            await connector.query(signal="cpu", scope=SCOPE)
        assert caught.value.kind == "backend"

    async def test_a_prometheus_level_error_is_surfaced(self) -> None:
        connector, _ = _connector(
            _ok({"status": "error", "errorType": "bad_data", "error": "parse error"})
        )
        with pytest.raises(ConnectorError, match="parse error") as caught:
            await connector.query(signal="cpu", scope=SCOPE)
        assert caught.value.kind == "backend"


class TestParsing:
    @pytest.mark.parametrize(
        "label_fields",
        [
            pytest.param({}, id="missing"),
            pytest.param({"metric": None}, id="null-container"),
            pytest.param({"metric": []}, id="list-container"),
            pytest.param({"metric": "invalid"}, id="string-container"),
            pytest.param({"metric": 42}, id="numeric-container"),
            pytest.param({"metric": {"pod": None}}, id="null-value"),
            pytest.param({"metric": {"pod": 42}}, id="numeric-value"),
            pytest.param({"metric": {"pod": True}}, id="boolean-value"),
            pytest.param({"metric": {"pod": []}}, id="list-value"),
            pytest.param({"metric": {"pod": {}}}, id="mapping-value"),
        ],
    )
    @pytest.mark.parametrize("include_valid", [False, True])
    async def test_invalid_metric_labels_are_omitted_without_losing_valid_siblings(
        self, label_fields: dict[str, Any], include_valid: bool
    ) -> None:
        payload = _vector(*([("api-first", "1.5"), ("api-last", "2.5")] if include_valid else []))
        payload["data"]["result"].insert(1, {"value": [1786_000_060, "2.0"], **label_fields})
        connector, _ = _connector(_ok(payload))

        result = await connector.query(signal="cpu", scope=SCOPE)
        rendered = render_metrics(result)

        assert [(series.labels, series.value) for series in result.series] == (
            [({"pod": "api-first"}, 1.5), ({"pod": "api-last"}, 2.5)] if include_valid else []
        )
        assert result.observed_at == ("2026-08-06T07:06:40Z" if include_valid else None)
        assert result.omitted_entries == 1
        assert result.truncated is True
        assert "omitted unusable entries: 1" in rendered
        assert "truncated: yes" in rendered
        assert "no series matched" not in rendered
        if not include_valid:
            assert "no usable series remained" in rendered

    @pytest.mark.parametrize("key", [7, False, None])
    def test_non_string_metric_keys_are_rejected(self, key: Any) -> None:
        row = {"metric": {key: "api"}, "value": [1786_000_000, "1.5"]}

        assert _series(row, frozenset(), Answer(payload={})) is None

    async def test_an_unlabelled_series_with_an_empty_metric_is_complete(self) -> None:
        payload = _vector(("api", "1.5"))
        payload["data"]["result"][0]["metric"] = {}
        connector, _ = _connector(_ok(payload))

        result = await connector.query(signal="cpu", scope=SCOPE)

        assert [(series.labels, series.value) for series in result.series] == [({}, 1.5)]
        assert result.omitted_entries == 0
        assert result.truncated is False

    @pytest.mark.parametrize(
        "overflow_value",
        [pytest.param(10**400, id="positive"), pytest.param(-(10**400), id="negative")],
    )
    @pytest.mark.parametrize("include_valid", [False, True])
    async def test_numeric_overflow_omits_only_the_unusable_sample(
        self, overflow_value: int, include_valid: bool
    ) -> None:
        payload = _vector(*([("api-first", "1.5"), ("api-last", "2.5")] if include_valid else []))
        payload["data"]["result"].insert(
            1,
            {"metric": {"pod": "api-overflow"}, "value": [1786_000_060, overflow_value]},
        )
        connector, _ = _connector(_ok(payload))

        result = await connector.query(signal="cpu", scope=SCOPE)
        rendered = render_metrics(result)

        assert [(series.labels, series.value) for series in result.series] == (
            [({"pod": "api-first"}, 1.5), ({"pod": "api-last"}, 2.5)] if include_valid else []
        )
        assert result.observed_at == ("2026-08-06T07:06:40Z" if include_valid else None)
        assert result.omitted_entries == 1
        assert result.truncated is True
        assert "omitted unusable entries: 1" in rendered
        assert "truncated: yes" in rendered
        assert "api-overflow" not in rendered
        assert "no series matched" not in rendered
        if not include_valid:
            assert "no usable series remained" in rendered

    @pytest.mark.parametrize("include_valid", [False, True])
    async def test_unusable_samples_are_reported_as_incomplete(self, include_valid: bool) -> None:
        payload = _vector(*([("api-good", "1.5")] if include_valid else []))
        payload["data"]["result"].extend(
            ["junk", {"metric": {}}, {"metric": {}, "value": [1786_000_000, "NaN"]}]
        )
        connector, _ = _connector(_ok(payload))

        result = await connector.query(signal="cpu", scope=SCOPE)
        rendered = render_metrics(result)

        assert [series.value for series in result.series] == ([1.5] if include_valid else [])
        assert result.truncated is True
        assert result.omitted_entries == 3
        assert "omitted unusable entries: 3" in rendered
        assert "truncated: yes" in rendered
        assert "no series matched" not in rendered
        if not include_valid:
            assert "no usable series remained" in rendered
            assert result.observed_at is None

    async def test_a_genuinely_empty_vector_is_complete(self) -> None:
        connector, _ = _connector(_ok(_vector()))

        result = await connector.query(signal="cpu", scope=SCOPE)

        assert result.series == ()
        assert result.omitted_entries == 0
        assert result.truncated is False
        assert "no series matched" in render_metrics(result)

    async def test_labels_and_values_are_carried_through(self) -> None:
        connector, _ = _connector(_ok(_vector(("api-1", "0.25"))))
        result = await connector.query(signal="cpu", scope=SCOPE)
        assert result.series[0].labels["pod"] == "api-1"
        assert result.series[0].value == pytest.approx(0.25)

    async def test_a_non_numeric_sample_is_skipped_rather_than_failing_the_query(self) -> None:
        connector, _ = _connector(_ok(_vector(("api-1", "NaN"), ("api-2", "1.5"))))
        result = await connector.query(signal="cpu", scope=SCOPE)
        assert [s.labels["pod"] for s in result.series] == ["api-2"]

    async def test_a_malformed_entry_is_skipped(self) -> None:
        payload = {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}}, "junk"]},
        }
        connector, _ = _connector(_ok(payload))
        result = await connector.query(signal="cpu", scope=SCOPE)
        assert result.series == ()

    async def test_the_unit_comes_from_the_catalogue(self) -> None:
        connector, _ = _connector(_ok(_vector(("api-1", "1"))))
        result = await connector.query(signal="memory", scope=SCOPE)
        assert result.unit == "bytes"


class TestLifecycle:
    async def test_closing_the_connector_closes_the_client(self) -> None:
        client, _ = _client(_ok(_vector()))
        connector = PrometheusConnector(
            "https://prom.example.com", client=client, limits=QueryLimits()
        )
        await connector.aclose()
        assert client.is_closed
