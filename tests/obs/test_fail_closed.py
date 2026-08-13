"""Fail-closed edges of the connector boundary (issue #193).

Each of these is a refusal rather than a best guess. They are separated
from the happy-path connector tests because what they pin is the choice
to refuse, not the shape of an answer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from korvid.core.secrets import MASK_PLACEHOLDER
from korvid.obs.connector import ConnectorError, QueryLimits, QueryScope
from korvid.obs.credentials import resolve_token, resolve_token_async
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
                200,
                json={"status": "success", "data": {"resultType": "streams", "result": "nope"}},
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
                        200,
                        json={
                            "status": "success",
                            "data": {"resultType": "vector", "result": "nope"},
                        },
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


class TestTokenHygiene:
    """Round-1 review: a token must not be able to leak through a transport error."""

    def test_a_whitespace_only_environment_value_is_refused(self) -> None:
        """It is truthy before stripping, so it would send `Bearer ` unauthenticated."""
        with pytest.raises(ConnectorError, match="unset or empty"):
            resolve_token(
                token_env="TOK", token_file=None, source="loki", getenv={"TOK": "  \n"}.get
            )

    def test_a_non_utf8_token_file_is_an_actionable_config_error(self, tmp_path: Path) -> None:
        """`UnicodeDecodeError` is a ValueError, not an OSError."""
        path = tmp_path / "token"
        path.write_bytes(b"\xff\xfe\x00binary")
        with pytest.raises(ConnectorError, match="could not be read") as caught:
            resolve_token(token_env=None, token_file=str(path), source="loki")
        assert caught.value.kind == "config"

    @pytest.mark.parametrize("value", ["a\rb", "a\nb", "a\x00b", "a\x7fb", "tok\u00e9n"])
    def test_a_token_that_is_not_header_safe_is_refused(self, value: str) -> None:
        """An illegal header value makes httpx raise *with the value in the message*."""
        with pytest.raises(ConnectorError, match="not a valid HTTP header value") as caught:
            resolve_token(
                token_env="TOK", token_file=None, source="loki", getenv={"TOK": value}.get
            )
        assert caught.value.kind == "config"

    def test_the_refusal_does_not_echo_the_token(self) -> None:
        with pytest.raises(ConnectorError) as caught:
            resolve_token(
                token_env="TOK",
                token_file=None,
                source="loki",
                getenv={"TOK": "secret\rvalue"}.get,
            )
        assert "secret" not in str(caught.value)


class TestErrorsNeverEchoUserinfo:
    async def test_a_transport_failure_message_is_still_actionable(self) -> None:
        """A URL cannot carry userinfo any more, but the error must still say why."""

        def responder(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nodename nor servname provided", request=request)

        backend = HttpBackend(
            "https://x.example.com",
            source="prometheus",
            client=httpx.AsyncClient(transport=httpx.MockTransport(responder)),
            limits=QueryLimits(),
        )
        with pytest.raises(ConnectorError, match="nodename") as caught:
            await backend.get_json("/x", {})
        assert caught.value.kind == "network"


class TestTheTimeoutBoundsTheWholeCall:
    async def test_waiting_for_a_slot_counts_against_the_budget(self) -> None:
        """Queueing behind other calls is elapsed time the caller is waiting."""

        async def responder(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200, json={"status": "success", "data": {}})

        backend = _backend(responder, timeout_seconds=0.05, max_concurrency=1)
        with pytest.raises(ConnectorError) as caught:
            await asyncio.gather(
                backend.get_json("/x", {}),
                backend.get_json("/x", {}),
            )
        assert caught.value.kind == "timeout"

    async def test_a_trickling_response_cannot_outlast_the_budget(self) -> None:
        """httpx read timeouts bound inactivity, not total elapsed time."""

        async def trickle() -> Any:
            for _ in range(100):
                await asyncio.sleep(0.01)
                yield b"x"

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=trickle())

        backend = _backend(responder, timeout_seconds=0.05)
        with pytest.raises(ConnectorError) as caught:
            await backend.get_json("/x", {})
        assert caught.value.kind == "timeout"


class TestConfiguredLabelMasking:
    """Issue #193: configured sensitive fields are masked in the projection.

    Some label values are sensitive by policy rather than by shape — a
    tenant id, a customer name, an internal hostname. The generic
    credential-shaped pass cannot know that, so the operator names them.
    """

    async def test_a_configured_label_value_is_masked_in_a_log_result(self) -> None:
        connector = _loki(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {"stream": {"pod": "api-1", "tenant": "acme"}, "values": [["1", "x"]]}
                        ],
                    },
                },
            ),
            mask_labels=frozenset({"tenant"}),
        )
        result = await connector.search(scope=QueryScope(namespace="prod"))
        assert result.lines[0].labels["tenant"] == MASK_PLACEHOLDER
        assert result.lines[0].labels["pod"] == "api-1"

    async def test_a_configured_label_value_is_masked_in_a_metric_result(self) -> None:
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "status": "success",
                            "data": {
                                "resultType": "vector",
                                "result": [
                                    {
                                        "metric": {"pod": "api-1", "tenant": "acme"},
                                        "value": [1, "1.0"],
                                    }
                                ],
                            },
                        },
                    )
                )
            ),
            limits=QueryLimits(),
            mask_labels=frozenset({"tenant"}),
        )
        result = await connector.query(signal="cpu", scope=QueryScope(namespace="prod"))
        assert result.series[0].labels["tenant"] == MASK_PLACEHOLDER
        assert result.series[0].labels["pod"] == "api-1"

    async def test_masking_is_case_insensitive_on_the_label_name(self) -> None:
        connector = _loki(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [{"stream": {"Tenant": "acme"}, "values": [["1", "x"]]}],
                    },
                },
            ),
            mask_labels=frozenset({"tenant"}),
        )
        result = await connector.search(scope=QueryScope(namespace="prod"))
        assert result.lines[0].labels["Tenant"] == MASK_PLACEHOLDER

    async def test_nothing_configured_masks_nothing(self) -> None:
        connector = _loki(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [{"stream": {"tenant": "acme"}, "values": [["1", "x"]]}],
                    },
                },
            )
        )
        result = await connector.search(scope=QueryScope(namespace="prod"))
        assert result.lines[0].labels["tenant"] == "acme"


class TestRoundThreeFindings:
    """Advisory round-3 findings that were credible on the merits."""

    @pytest.mark.parametrize("value", [True, float("inf"), float("nan"), "10"])
    def test_a_non_finite_or_non_numeric_timeout_is_rejected_at_the_boundary(
        self, value: object
    ) -> None:
        """Config rejects these; a directly-built connector must not slip through."""
        with pytest.raises(ValueError, match="timeout_seconds"):
            QueryLimits(timeout_seconds=value)  # type: ignore[arg-type]  # the point of the test

    async def test_a_prometheus_response_of_the_wrong_result_type_is_refused(self) -> None:
        """A matrix has a list-shaped `result` and would render as "no series"."""
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "status": "success",
                            "data": {"resultType": "matrix", "result": []},
                        },
                    )
                )
            ),
            limits=QueryLimits(),
        )
        with pytest.raises(ConnectorError, match="matrix") as caught:
            await connector.query(signal="cpu", scope=QueryScope(namespace="prod"))
        assert caught.value.kind == "backend"

    async def test_a_loki_response_of_the_wrong_result_type_is_refused(self) -> None:
        connector = _loki(
            lambda request: httpx.Response(
                200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
            )
        )
        with pytest.raises(ConnectorError, match="vector") as caught:
            await connector.search(scope=QueryScope(namespace="prod"))
        assert caught.value.kind == "backend"

    @pytest.mark.parametrize("tenant", ["team\ra", "team\na", "tenant\u00e9"])
    def test_a_tenant_that_is_not_header_safe_is_refused(self, tenant: str) -> None:
        """Same leak as an unsafe token: httpx quotes an illegal header value."""
        with pytest.raises(ConnectorError, match="not a valid HTTP header value") as caught:
            _loki(lambda request: httpx.Response(200, json={}), tenant=tenant)
        assert caught.value.kind == "config"

    async def test_a_metric_result_carries_the_moment_it_describes(self) -> None:
        """Evidence with a relative window but no absolute time cannot be rechecked."""
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "status": "success",
                            "data": {
                                "resultType": "vector",
                                "result": [{"metric": {"pod": "a"}, "value": [1786000000, "1.0"]}],
                            },
                        },
                    )
                )
            ),
            limits=QueryLimits(),
        )
        result = await connector.query(signal="cpu", scope=QueryScope(namespace="prod"))
        assert result.observed_at is not None
        assert result.observed_at.endswith("Z")


class TestMaskingCoversTheProvenance:
    """A masked label's value must not reappear in the scope or the query."""

    async def test_a_masked_scope_value_is_not_echoed_in_the_scope_or_query(self) -> None:
        connector = _loki(
            lambda request: httpx.Response(
                200, json={"status": "success", "data": {"resultType": "streams", "result": []}}
            ),
            label_mappings={"namespace": "namespace", "pod": "pod", "workload": "customer"},
            mask_labels=frozenset({"customer"}),
        )
        result = await connector.search(scope=QueryScope(namespace="prod", workload="acme"))
        assert "acme" not in result.query
        assert "acme" not in result.scope.describe()
        assert result.scope.namespace == "prod"

    async def test_an_unmasked_scope_value_is_still_reported(self) -> None:
        connector = _loki(
            lambda request: httpx.Response(
                200, json={"status": "success", "data": {"resultType": "streams", "result": []}}
            ),
            mask_labels=frozenset({"customer"}),
        )
        result = await connector.search(scope=QueryScope(namespace="prod", workload="api"))
        assert "api" in result.query
        assert result.scope.workload == "api"

    async def test_a_masked_prometheus_scope_value_is_not_echoed(self) -> None:
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "status": "success",
                            "data": {"resultType": "vector", "result": []},
                        },
                    )
                )
            ),
            limits=QueryLimits(),
            mask_labels=frozenset({"namespace"}),
        )
        result = await connector.query(signal="cpu", scope=QueryScope(namespace="secret-ns"))
        assert "secret-ns" not in result.query
        assert "secret-ns" not in result.scope.describe()


class TestSecretsAreScrubbedFromEverythingThatComesBack:
    """Round-5 review: masking must not depend on which path the call took.

    A backend can echo the bearer token or the selector in an error, a
    label or a log line, and a failure never reached the projection at
    all. Both are scrubbed at the transport boundary now, so the success
    and failure paths cannot diverge.
    """

    async def test_an_echoed_bearer_token_never_reaches_the_result(self) -> None:
        """Opaque token, no `token=` syntax: the pattern pass cannot see it."""
        connector = _loki(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {
                                "stream": {"pod": "a"},
                                "values": [["1", "upstream said Zm9vYmFyc2VjcmV0dmFsdWU"]],
                            }
                        ],
                    },
                },
            ),
            token_env="TOK",
        )
        import os

        os.environ["TOK"] = "Zm9vYmFyc2VjcmV0dmFsdWU"
        try:
            result = await connector.search(scope=QueryScope(namespace="prod"))
        finally:
            del os.environ["TOK"]
        assert "Zm9vYmFyc2VjcmV0dmFsdWU" not in result.lines[0].line

    async def test_an_echoed_token_never_reaches_a_backend_error(self) -> None:
        connector = _loki(
            lambda request: httpx.Response(
                200,
                json={"status": "error", "error": "bad request: Zm9vYmFyc2VjcmV0dmFsdWU"},
            ),
            token_env="TOK",
        )
        import os

        os.environ["TOK"] = "Zm9vYmFyc2VjcmV0dmFsdWU"
        try:
            with pytest.raises(ConnectorError) as caught:
                await connector.search(scope=QueryScope(namespace="prod"))
        finally:
            del os.environ["TOK"]
        assert "Zm9vYmFyc2VjcmV0dmFsdWU" not in str(caught.value)

    async def test_a_backend_error_echoing_the_selector_masks_the_policy_value(self) -> None:
        connector = _loki(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "error",
                    "error": 'parse error in {customer="acme", namespace="prod"}',
                },
            ),
            label_mappings={"namespace": "namespace", "pod": "pod", "workload": "customer"},
            mask_labels=frozenset({"customer"}),
        )
        with pytest.raises(ConnectorError) as caught:
            await connector.search(scope=QueryScope(namespace="prod", workload="acme"))
        assert "acme" not in str(caught.value)

    async def test_a_prometheus_error_echoing_the_selector_masks_the_policy_value(self) -> None:
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "status": "error",
                            "error": 'parse error in {namespace="secret-ns"}',
                        },
                    )
                )
            ),
            limits=QueryLimits(),
            mask_labels=frozenset({"namespace"}),
        )
        with pytest.raises(ConnectorError) as caught:
            await connector.query(signal="cpu", scope=QueryScope(namespace="secret-ns"))
        assert "secret-ns" not in str(caught.value)

    async def test_an_escaped_form_in_the_echo_is_masked_too(self) -> None:
        """The query embeds the *escaped* value, so that is what comes back."""
        connector = _loki(
            lambda request: httpx.Response(
                200,
                json={"status": "error", "error": 'parse error near "ac\\"me"'},
            ),
            label_mappings={"namespace": "namespace", "pod": "pod", "workload": "customer"},
            mask_labels=frozenset({"customer"}),
        )
        with pytest.raises(ConnectorError) as caught:
            await connector.search(scope=QueryScope(namespace="prod", workload='ac"me'))
        assert 'ac\\"me' not in str(caught.value)

    async def test_an_unmasked_value_still_appears_in_a_backend_error(self) -> None:
        """Scrubbing that ate the diagnostic would make the error useless."""
        connector = _loki(
            lambda request: httpx.Response(
                200, json={"status": "error", "error": 'parse error in {namespace="prod"}'}
            )
        )
        with pytest.raises(ConnectorError, match="prod"):
            await connector.search(scope=QueryScope(namespace="prod"))


class TestEveryEncodedFormOfASecretIsCovered:
    """Round-6 review: a value reaches the query in more than one encoding.

    A workload becomes a *regex* matcher, so `api.v1` travels as
    `api\\.v1`; a value containing a quote travels string-escaped. Masking
    the raw form alone leaves the form that was actually sent.
    """

    async def test_a_regex_escaped_workload_is_masked_in_the_query(self) -> None:
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={"status": "success", "data": {"resultType": "vector", "result": []}},
                    )
                )
            ),
            limits=QueryLimits(),
            mask_labels=frozenset({"pod"}),
        )
        result = await connector.query(
            signal="cpu", scope=QueryScope(namespace="prod", workload="api.v1")
        )
        assert "api.v1" not in result.query
        assert "api\\.v1" not in result.query

    async def test_a_regex_escaped_workload_is_masked_in_a_backend_error(self) -> None:
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={"status": "error", "error": 'parse error near "api\\.v1.*"'},
                    )
                )
            ),
            limits=QueryLimits(),
            mask_labels=frozenset({"pod"}),
        )
        with pytest.raises(ConnectorError) as caught:
            await connector.query(
                signal="cpu", scope=QueryScope(namespace="prod", workload="api.v1")
            )
        assert "api\\.v1" not in str(caught.value)

    async def test_a_string_escaped_scope_value_is_masked_in_a_loki_query(self) -> None:
        connector = _loki(
            lambda request: httpx.Response(
                200, json={"status": "success", "data": {"resultType": "streams", "result": []}}
            ),
            label_mappings={"namespace": "namespace", "pod": "pod", "workload": "customer"},
            mask_labels=frozenset({"customer"}),
        )
        result = await connector.search(scope=QueryScope(namespace="prod", workload='ac"me'))
        assert 'ac\\"me' not in result.query
        assert 'ac"me' not in result.query


class TestUserinfoIsRefusedAtTheTransportBoundary:
    @pytest.mark.parametrize(
        "url",
        [
            "https://user:hunter2@x.example.com",
            "https://user@x.example.com",
        ],
    )
    def test_a_url_carrying_a_credential_is_refused(self, url: str) -> None:
        """The HTTP client turns userinfo into a Basic `Authorization` header."""
        with pytest.raises(ConnectorError, match="token_env") as caught:
            HttpBackend(
                url,
                source="prometheus",
                client=httpx.AsyncClient(),
                limits=QueryLimits(),
            )
        assert caught.value.kind == "config"

    def test_the_refusal_does_not_echo_the_credential(self) -> None:
        with pytest.raises(ConnectorError) as caught:
            HttpBackend(
                "https://user:hunter2@x.example.com",
                source="prometheus",
                client=httpx.AsyncClient(),
                limits=QueryLimits(),
            )
        assert "hunter2" not in str(caught.value)

    def test_a_plain_url_is_accepted(self) -> None:
        backend = HttpBackend(
            "https://x.example.com",
            source="prometheus",
            client=httpx.AsyncClient(),
            limits=QueryLimits(),
        )
        assert backend.endpoint == "x.example.com"


class TestScrubbingNeverRewritesTheEnvelope:
    """Round-7 review: a secret is content, so it must not touch structure.

    A one-character bearer token is legal. Blind substring replacement
    over the body turned `"status": "success"` into nonsense, so every
    successful request became a backend failure — and the same class of
    corruption could hide a real one.
    """

    async def test_a_one_character_token_does_not_break_a_successful_answer(self) -> None:
        connector = _loki(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {"stream": {"pod": "api-s1"}, "values": [["1", "s is everywhere"]]}
                        ],
                    },
                },
            ),
            token_env="TOK",
        )
        import os

        os.environ["TOK"] = "s"
        try:
            result = await connector.search(scope=QueryScope(namespace="prod"))
        finally:
            del os.environ["TOK"]
        assert len(result.lines) == 1

    async def test_the_token_is_still_scrubbed_from_the_content(self) -> None:
        connector = _loki(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {
                                "stream": {"pod": "a"},
                                "values": [["1", "leaked Zm9vYmFyc2VjcmV0"]],
                            }
                        ],
                    },
                },
            ),
            token_env="TOK",
        )
        import os

        os.environ["TOK"] = "Zm9vYmFyc2VjcmV0"
        try:
            result = await connector.search(scope=QueryScope(namespace="prod"))
        finally:
            del os.environ["TOK"]
        assert "Zm9vYmFyc2VjcmV0" not in result.lines[0].line

    async def test_a_secret_matching_a_structural_field_leaves_the_result_type_alone(
        self,
    ) -> None:
        """`resultType` is korvid's contract with the backend, not content."""
        connector = _loki(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"resultType": "streams", "result": []},
                },
            ),
            token_env="TOK",
        )
        import os

        os.environ["TOK"] = "streams"
        try:
            result = await connector.search(scope=QueryScope(namespace="prod"))
        finally:
            del os.environ["TOK"]
        assert result.lines == ()
        assert result.truncated is False

    async def test_a_one_character_token_does_not_break_a_prometheus_answer(self) -> None:
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "status": "success",
                            "data": {
                                "resultType": "vector",
                                "result": [
                                    {"metric": {"pod": "api-s"}, "value": [1786000000, "1.0"]}
                                ],
                            },
                        },
                    )
                )
            ),
            limits=QueryLimits(),
            token_env="TOK",
        )
        import os

        os.environ["TOK"] = "s"
        try:
            result = await connector.query(signal="cpu", scope=QueryScope(namespace="prod"))
        finally:
            del os.environ["TOK"]
        assert len(result.series) == 1
        assert result.series[0].value == pytest.approx(1.0)


class TestTheBaseUrlIsOnlyAnOrigin:
    """Round-8 review: the API path is appended by concatenation.

    `https://host/base?x=1` + `/api/v1/query` puts the API path inside the
    query string, so every request quietly targets the wrong endpoint.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://x.example.com/base?x=1",
            "https://x.example.com#frag",
            "https://x.example.com/base?",
            "https://x.example.com/base#",
        ],
    )
    def test_a_url_with_a_query_or_fragment_is_refused(self, url: str) -> None:
        with pytest.raises(ConnectorError, match="query string or fragment") as caught:
            HttpBackend(url, source="prometheus", client=httpx.AsyncClient(), limits=QueryLimits())
        assert caught.value.kind == "config"

    @pytest.mark.parametrize(
        "url",
        [
            "https://x.example.com",
            "https://x.example.com/",
            "https://x.example.com/prometheus",
            "http://x.example.com:9090/base/path",
        ],
    )
    def test_an_origin_with_an_optional_base_path_is_accepted(self, url: str) -> None:
        backend = HttpBackend(
            url, source="prometheus", client=httpx.AsyncClient(), limits=QueryLimits()
        )
        assert backend.endpoint == "x.example.com"


class TestRoundNineFindings:
    async def test_a_token_file_read_cannot_block_the_event_loop(self) -> None:
        """The surrounding `asyncio.timeout` cannot fire while a sync read holds the loop."""
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            for _ in range(50):
                await asyncio.sleep(0)
                ticks += 1

        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".tok", delete=False) as handle:
            handle.write("a-token")
            path = handle.name
        beat = asyncio.create_task(heartbeat())
        await resolve_token_async(token_env=None, token_file=path, source="loki")
        await beat
        assert ticks == 50

    async def test_an_oversized_token_file_is_refused_rather_than_read(
        self, tmp_path: Path
    ) -> None:
        """A device or a runaway file must not be pulled into memory."""
        path = tmp_path / "token"
        path.write_text("x" * 200_000)
        with pytest.raises(ConnectorError, match="too large") as caught:
            await resolve_token_async(token_env=None, token_file=str(path), source="loki")
        assert caught.value.kind == "config"

    @pytest.mark.parametrize("name", ["namespace\n", "app\n", "_x\n"])
    def test_a_label_name_with_a_trailing_newline_is_refused(self, name: str) -> None:
        """`$` matches before a final newline; only a full match is the grammar."""
        from korvid.obs.query import valid_label_name

        assert not valid_label_name(name)

    def test_a_scope_value_cannot_be_rewritten_by_the_range_substitution(self) -> None:
        """A namespace literally named `team-{range}` must reach the backend intact."""
        from korvid.obs.query import build_metric_query

        query = build_metric_query("cpu", {"namespace": "team-{range}"}, window_minutes=30)
        assert 'namespace="team-{range}"' in query
        assert "[30m]" in query

    def test_the_memory_signal_totals_a_pods_containers(self) -> None:
        """`max by` reports the largest container, not the pod."""
        from korvid.obs.query import build_metric_query

        query = build_metric_query("memory", {"namespace": "prod"}, window_minutes=30)
        assert query.startswith("sum by (namespace, pod)")

    @pytest.mark.parametrize("signal", ["cpu", "memory"])
    def test_container_signals_exclude_the_pod_level_series(self, signal: str) -> None:
        """cAdvisor emits a pod total with an empty container name; counting it doubles."""
        from korvid.obs.query import build_metric_query

        query = build_metric_query(signal, {"namespace": "prod"}, window_minutes=30)
        assert 'container!=""' in query

    @pytest.mark.parametrize(
        "url", ["ftp://x.example.com", "x.example.com", "https://", "https://x.example.com:bad"]
    )
    def test_the_transport_validates_the_whole_origin(self, url: str) -> None:
        with pytest.raises(ConnectorError) as caught:
            HttpBackend(url, source="prometheus", client=httpx.AsyncClient(), limits=QueryLimits())
        assert caught.value.kind == "config"


class TestRoundTenFindings:
    def test_a_short_secret_cannot_leave_a_longer_one_partly_visible(self) -> None:
        """Replacing in input order lets `a` chew the front off `abc`."""
        from korvid.obs.connector import mask_in

        masked = mask_in("abc", ("a", "abc"))
        assert "bc" not in masked

    def test_the_longest_secret_wins_regardless_of_order(self) -> None:
        from korvid.obs.connector import mask_in

        assert mask_in("abc", ("abc", "a")) == mask_in("abc", ("a", "abc"))

    def test_a_repeated_secret_is_handled_once(self) -> None:
        from korvid.obs.connector import mask_in

        assert "secret" not in mask_in("secret", ("secret", "secret"))

    async def test_a_deeply_nested_body_is_a_backend_error(self) -> None:
        """`RecursionError` is not a `ValueError`, so it escaped the contract."""
        body = b"[" * 200_000 + b"]" * 200_000
        backend = _backend(lambda request: httpx.Response(200, content=body))
        with pytest.raises(ConnectorError) as caught:
            await backend.get_json("/x", {})
        assert caught.value.kind in ("backend", "limit")

    async def test_the_observation_time_comes_from_a_row_that_was_kept(self) -> None:
        """Otherwise the result dates its series by a row it discarded."""
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "status": "success",
                            "data": {
                                "resultType": "vector",
                                "result": [
                                    {"metric": {"pod": "bad"}, "value": [1000000000, "NaN"]},
                                    {"metric": {"pod": "good"}, "value": [1786000000, "1.0"]},
                                ],
                            },
                        },
                    )
                )
            ),
            limits=QueryLimits(),
        )
        result = await connector.query(signal="cpu", scope=QueryScope(namespace="prod"))
        assert [s.labels["pod"] for s in result.series] == ["good"]
        assert result.observed_at is not None
        assert not result.observed_at.startswith("2001")


class TestRoundElevenFindings:
    def test_a_partial_mapping_that_collides_with_a_default_is_refused(self) -> None:
        """`workload -> namespace` collides only once the defaults fill in.

        `_selector` falls back to the default `namespace` mapping, so the
        namespace matcher is overwritten by the workload and the search
        covers every namespace — the collision the check exists to stop.
        """
        with pytest.raises(ConnectorError, match="namespace") as caught:
            _loki(
                lambda request: httpx.Response(200, json={}),
                label_mappings={"workload": "namespace"},
            )
        assert caught.value.kind == "config"

    async def test_a_partial_mapping_without_a_collision_still_works(self) -> None:
        recorded: list[httpx.Request] = []

        def responder(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return httpx.Response(
                200, json={"status": "success", "data": {"resultType": "streams", "result": []}}
            )

        connector = _loki(responder, label_mappings={"workload": "service"})
        await connector.search(scope=QueryScope(namespace="prod", workload="api"))
        query = recorded[0].url.params["query"]
        assert 'namespace="prod"' in query
        assert 'service="api"' in query

    @pytest.mark.parametrize("kind", ["fifo", "directory"])
    async def test_a_token_file_that_is_not_a_regular_file_is_refused(
        self, tmp_path: Path, kind: str
    ) -> None:
        """Opening a FIFO blocks forever, and a worker thread cannot be cancelled."""
        import os

        path = tmp_path / "token"
        if kind == "fifo":
            if not hasattr(os, "mkfifo"):
                pytest.skip("no FIFOs on this platform")
            os.mkfifo(path)
        else:
            path.mkdir()
        with pytest.raises(ConnectorError, match="not a regular file") as caught:
            await resolve_token_async(token_env=None, token_file=str(path), source="loki")
        assert caught.value.kind == "config"


class TestRoundFourteenFindings:
    async def test_a_numeric_token_echoed_as_a_sample_value_is_not_rendered(self) -> None:
        """A sample value is text until korvid parses it, so it is scrubbed first."""
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "status": "success",
                            "data": {
                                "resultType": "vector",
                                "result": [
                                    {"metric": {"pod": "a"}, "value": [1786000000, "123456789"]},
                                    {"metric": {"pod": "b"}, "value": [1786000000, "2.5"]},
                                ],
                            },
                        },
                    )
                )
            ),
            limits=QueryLimits(),
            token_env="TOK",
        )
        import os

        os.environ["TOK"] = "123456789"
        try:
            result = await connector.query(signal="cpu", scope=QueryScope(namespace="prod"))
        finally:
            del os.environ["TOK"]
        assert [s.labels["pod"] for s in result.series] == ["b"]

    async def test_an_ordinary_sample_value_is_untouched(self) -> None:
        from korvid.obs.prometheus import PrometheusConnector

        connector = PrometheusConnector(
            "https://p.example.com",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={
                            "status": "success",
                            "data": {
                                "resultType": "vector",
                                "result": [{"metric": {"pod": "a"}, "value": [1786000000, "0.25"]}],
                            },
                        },
                    )
                )
            ),
            limits=QueryLimits(),
        )
        result = await connector.query(signal="cpu", scope=QueryScope(namespace="prod"))
        assert result.series[0].value == pytest.approx(0.25)
