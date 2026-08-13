"""Fail-closed edges of the connector boundary (issue #193).

Each of these is a refusal rather than a best guess. They are separated
from the happy-path connector tests because what they pin is the choice
to refuse, not the shape of an answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from korvid.obs.connector import ConnectorError, QueryLimits, QueryScope
from korvid.obs.credentials import resolve_token
from korvid.obs.http import HttpBackend, endpoint_host
from korvid.obs.loki import LokiConnector
from korvid.obs.query import build_selector


class TestCredentialSources:
    def test_two_sources_are_refused_rather_than_ranked(self) -> None:
        """Config rejects this too; a directly-built connector must not slip through."""
        with pytest.raises(ConnectorError, match="exactly one") as caught:
            resolve_token(token_env="A", token_file="/b", source="prometheus")
        assert caught.value.kind == "config"

    def test_an_empty_token_file_is_refused(self, tmp_path: Path) -> None:
        """A file of whitespace would otherwise send `Bearer `."""
        path = tmp_path / "token"
        path.write_text("   \n")
        with pytest.raises(ConnectorError, match="empty"):
            resolve_token(token_env=None, token_file=str(path), source="loki")

    def test_no_configured_source_means_no_token(self) -> None:
        assert resolve_token(token_env=None, token_file=None, source="loki") is None

    def test_the_environment_lookup_is_injectable(self) -> None:
        token = resolve_token(
            token_env="TOK", token_file=None, source="loki", getenv={"TOK": "v"}.get
        )
        assert token == "v"


class TestEndpointHost:
    def test_userinfo_is_dropped_with_the_rest_of_the_authority(self) -> None:
        assert endpoint_host("https://user:hunter2@prom.internal:9090/x") == "prom.internal"

    def test_an_unparsable_url_falls_back_to_the_whole_string(self) -> None:
        """Better a useless host than an empty one in a diagnostic."""
        assert endpoint_host("not a url") == "not a url"


def _backend(responder: Any, **kwargs: Any) -> HttpBackend:
    return HttpBackend(
        "https://x.example.com",
        source="prometheus",
        client=httpx.AsyncClient(transport=httpx.MockTransport(responder)),
        limits=QueryLimits(**kwargs),
    )


class TestResponseEnvelope:
    async def test_a_non_mapping_payload_is_refused(self) -> None:
        backend = _backend(lambda request: httpx.Response(200, json=["not", "an", "envelope"]))
        with pytest.raises(ConnectorError, match="unexpected payload") as caught:
            backend.require_success(await backend.get_json("/x", {}))
        assert caught.value.kind == "backend"

    async def test_a_missing_status_is_refused(self) -> None:
        backend = _backend(lambda request: httpx.Response(200, json={"data": {}}))
        with pytest.raises(ConnectorError, match="refused the query"):
            backend.require_success(await backend.get_json("/x", {}))

    async def test_a_success_without_data_is_refused(self) -> None:
        """A success envelope with nothing in it is not an empty result."""
        backend = _backend(lambda request: httpx.Response(200, json={"status": "success"}))
        with pytest.raises(ConnectorError, match="no result data"):
            backend.require_success(await backend.get_json("/x", {}))

    async def test_a_non_integer_content_length_does_not_bypass_the_streamed_cap(self) -> None:
        """A hostile header must not be able to skip the byte bound."""
        body = b"x" * 4096

        def responder(request: httpx.Request) -> httpx.Response:
            response = httpx.Response(200, content=body)
            response.headers["content-length"] = "not-a-number"
            return response

        backend = _backend(responder, max_response_bytes=128)
        with pytest.raises(ConnectorError, match="128-byte cap") as caught:
            await backend.get_json("/x", {})
        assert caught.value.kind == "limit"


class TestSelectorEdges:
    def test_a_korvid_owned_suffix_is_appended_verbatim(self) -> None:
        """The 5xx class is korvid's text, not the model's."""
        assert build_selector({"a": "b"}, suffix='code=~"5.."') == '{a="b", code=~"5.."}'


def _loki(responder: Any, **kwargs: Any) -> LokiConnector:
    return LokiConnector(
        "https://loki.example.com",
        client=httpx.AsyncClient(transport=httpx.MockTransport(responder)),
        limits=QueryLimits(),
        **kwargs,
    )


class TestLokiParsingEdges:
    async def test_an_unmapped_scope_field_falls_back_to_the_conventional_label(self) -> None:
        """A partial mapping must not drop a scope field entirely."""
        recorded: list[httpx.Request] = []

        def responder(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return httpx.Response(
                200, json={"status": "success", "data": {"resultType": "streams", "result": []}}
            )

        connector = _loki(responder, label_mappings={"namespace": "ns"})
        await connector.search(scope=QueryScope(namespace="prod", pod="api-1"))
        query = recorded[0].url.params["query"]
        assert 'ns="prod"' in query
        assert 'pod="api-1"' in query

    async def test_a_non_list_result_is_refused(self) -> None:
        connector = _loki(
            lambda request: httpx.Response(
                200, json={"status": "success", "data": {"result": "streams"}}
            )
        )
        with pytest.raises(ConnectorError, match="unexpected result shape") as caught:
            await connector.search(scope=QueryScope(namespace="prod"))
        assert caught.value.kind == "backend"

    @pytest.mark.parametrize(
        "result",
        [
            ["not a stream"],
            [{"stream": {"pod": "a"}, "values": "not a list"}],
            [{"stream": {"pod": "a"}, "values": [["1"]]}],
            [{"stream": {"pod": "a"}, "values": [["1", 7]]}],
            [{"stream": "not a mapping", "values": [["1", "ok"]]}],
        ],
    )
    async def test_an_unusable_entry_is_skipped_not_fatal(self, result: list[Any]) -> None:
        """One malformed stream must not lose the lines that did parse."""
        connector = _loki(
            lambda request: httpx.Response(
                200, json={"status": "success", "data": {"resultType": "streams", "result": result}}
            )
        )
        outcome = await connector.search(scope=QueryScope(namespace="prod"))
        assert all(isinstance(line.line, str) for line in outcome.lines)


class TestLimitsConsistency:
    def test_a_default_window_over_the_maximum_is_rejected(self) -> None:
        """The pair is contradictory; accepting it would silently pick one."""
        with pytest.raises(ValueError, match="default_window_minutes"):
            QueryLimits(default_window_minutes=600, max_window_minutes=60)

    async def test_each_connector_reports_its_configured_concurrency(self) -> None:
        connector = _loki(lambda request: httpx.Response(200, json={}))
        assert connector.max_concurrency == QueryLimits().max_concurrency
        await connector.aclose()


class TestPrometheusParsingEdges:
    async def test_a_non_list_result_is_refused(self) -> None:
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, json={"status": "success", "data": {"result": "vector"}}
                    )
                )
            ),
            limits=QueryLimits(),
        )
        with pytest.raises(ConnectorError, match="unexpected result shape") as caught:
            await connector.query(signal="cpu", scope=QueryScope(namespace="prod"))
        assert caught.value.kind == "backend"

    @pytest.mark.parametrize(
        ("row", "labels"),
        [
            ({"value": [1, "1.0"]}, {}),
            ({"metric": "not a mapping", "value": [1, "1.0"]}, {}),
        ],
    )
    async def test_a_sample_without_usable_labels_still_reports_its_value(
        self, row: dict[str, Any], labels: dict[str, str]
    ) -> None:
        """A value with no labels is still a true answer about the scope."""
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "status": "success",
                            "data": {"resultType": "vector", "result": [row]},
                        },
                    )
                )
            ),
            limits=QueryLimits(),
        )
        result = await connector.query(signal="cpu", scope=QueryScope(namespace="prod"))
        assert result.series[0].labels == labels
        assert result.series[0].value == pytest.approx(1.0)
