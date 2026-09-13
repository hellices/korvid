import asyncio
import gzip
import importlib
import json
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlsplit

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from korvid.k8s.client import KubeClient
from korvid.k8s.errors import ApiStatusError, KubeClientError


class ChunkStream:
    def __init__(self, payload: bytes, failure: BaseException | None = None) -> None:
        self.payload = payload
        self.position = 0
        self.requests: list[int] = []
        self.failure = failure

    async def read(self, size: int) -> bytes:
        self.requests.append(size)
        if self.failure is not None:
            raise self.failure
        chunk = self.payload[self.position : self.position + min(size, 17)]
        self.position += len(chunk)
        return chunk

    def at_eof(self) -> bool:
        return self.position == len(self.payload)


class Response:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.status = status
        self.content = ChunkStream(payload)
        self.headers: dict[str, str] = {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def pulse(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    assert hasattr(KubeClient, "read_pulse_page"), "bounded Pulse reads are missing"
    module = importlib.import_module("korvid.k8s.pulse")
    monkeypatch.setattr(module, "_bounded_session", lambda shared: shared)
    return module


def client_with(response: Response) -> tuple[KubeClient, AsyncMock]:
    client = KubeClient()
    api = AsyncMock()
    api.configuration.host = "https://cluster.test"
    api.default_headers = {"User-Agent": "korvid-test"}
    api.cookie = None
    api.rest_client.proxy = None
    api.rest_client.proxy_headers = None
    api.rest_client.server_hostname = None
    api.rest_client.pool_manager.request.return_value = response
    api.rest_client.pool_manager.__aenter__.return_value = api.rest_client.pool_manager
    api.rest_client.pool_manager.__aexit__.return_value = False
    client._api = api
    return client, api


async def test_bounded_page_uses_scoped_path_and_opaque_continuation(pulse: ModuleType) -> None:
    response = Response(
        json.dumps(
            {"items": [{"metadata": {"uid": "pod-1"}}], "metadata": {"continue": "next"}}
        ).encode()
    )
    client, api = client_with(response)
    source = pulse.PulseSource("pods", "", "v1", "pods")
    page = await client.read_pulse_page(source, "team/a", "token?&", 100, 262144)
    assert page.items == ({"metadata": {"uid": "pod-1"}},)
    assert page.continuation == "next"
    assert response.closed
    request = api.rest_client.pool_manager.request.await_args.kwargs
    assert request["method"] == "GET"
    assert urlsplit(request["url"]).path == "/api/v1/namespaces/team%2Fa/pods"
    assert parse_qsl(urlsplit(request["url"]).query) == [
        ("limit", "100"),
        ("continue", "token?&"),
    ]
    assert request["auto_decompress"] is False
    assert request["allow_redirects"] is False
    assert request["read_bufsize"] == 16384
    assert request["headers"]["Accept-Encoding"] == "identity"


async def test_warning_source_applies_selector_without_scoping_all_namespaces(
    pulse: ModuleType,
) -> None:
    response = Response(b'{"items": []}')
    client, api = client_with(response)
    source = pulse.PulseSource("events", "", "v1", "events", "type=Warning")
    await client.read_pulse_page(source, None, "", 100, 262144)
    url = urlsplit(api.rest_client.pool_manager.request.await_args.kwargs["url"])
    assert url.path == "/api/v1/events"
    assert ("fieldSelector", "type=Warning") in parse_qsl(url.query)


async def test_page_byte_cap_stops_stream_before_parsing_and_closes(pulse: ModuleType) -> None:
    response = Response(b'{"items": [' + b" " * 2000)
    client, _api = client_with(response)
    with pytest.raises(pulse.PulseLimitError, match="byte"):
        await client.read_pulse_page(
            pulse.PulseSource("pods", "", "v1", "pods"), None, "", 100, 128
        )
    assert response.content.position <= 128
    assert response.closed


async def test_exact_byte_budget_accepts_complete_json(pulse: ModuleType) -> None:
    payload = b'{"items": []}'
    response = Response(payload)
    client, _api = client_with(response)
    page = await client.read_pulse_page(
        pulse.PulseSource("pods", "", "v1", "pods"), None, "", 100, len(payload)
    )
    assert page.items == ()
    assert response.closed


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b'{"items": null}',
        b'{"items": [3]}',
        b'{"items": [], "metadata": {"continue": 4}}',
    ],
)
async def test_malformed_pages_are_not_empty_success(pulse: ModuleType, payload: bytes) -> None:
    response = Response(payload)
    client, _api = client_with(response)
    with pytest.raises(KubeClientError, match="invalid"):
        await client.read_pulse_page(
            pulse.PulseSource("pods", "", "v1", "pods"), None, "", 100, 1024
        )
    assert response.closed


@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 503])
async def test_http_failure_is_normalized_without_reading_sensitive_body(
    pulse: ModuleType, status: int
) -> None:
    response = Response(b"token=top-secret", status)
    client, _api = client_with(response)
    with pytest.raises(ApiStatusError, match=f"API {status}") as failure:
        await client.read_pulse_page(
            pulse.PulseSource("pods", "", "v1", "pods"), None, "", 100, 1024
        )
    assert failure.value.body == ""
    assert response.content.position == 0
    assert response.closed


@pytest.mark.parametrize(
    "failure", [asyncio.CancelledError(), TimeoutError(), OSError("token=secret")]
)
async def test_interrupted_body_always_closes(pulse: ModuleType, failure: BaseException) -> None:
    response = Response(b'{"items": []}')
    response.content.failure = failure
    client, _api = client_with(response)
    expected = (
        asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else KubeClientError
    )
    with pytest.raises(expected, match=r"^$" if expected is asyncio.CancelledError else "Pulse"):
        await client.read_pulse_page(
            pulse.PulseSource("pods", "", "v1", "pods"), None, "", 100, 1024
        )
    assert response.closed


async def test_api_exception_is_normalized(pulse: ModuleType) -> None:
    client, api = client_with(Response(b""))
    api.rest_client.pool_manager.request.side_effect = ApiException(
        status=403, reason="token=secret"
    )
    with pytest.raises(ApiStatusError, match="API 403") as failure:
        await client.read_pulse_page(
            pulse.PulseSource("pods", "", "v1", "pods"), None, "", 100, 1024
        )
    assert "secret" not in str(failure.value)


async def test_server_ignoring_row_limit_is_explicitly_capped(pulse: ModuleType) -> None:
    response = Response(json.dumps({"items": [{}, {}, {}]}).encode())
    client, _api = client_with(response)
    with pytest.raises(pulse.PulseLimitError, match="object"):
        await client.read_pulse_page(pulse.PulseSource("pods", "", "v1", "pods"), None, "", 2, 1024)
    assert response.closed


@pytest.mark.parametrize(("limit", "max_bytes"), [(0, 100), (100, 0), (True, 100), (100, False)])
async def test_invalid_budgets_are_rejected_before_request(
    pulse: ModuleType, limit: Any, max_bytes: Any
) -> None:
    client, api = client_with(Response(b""))
    with pytest.raises(ValueError, match="positive integer"):
        await client.read_pulse_page(
            pulse.PulseSource("pods", "", "v1", "pods"), None, "", limit, max_bytes
        )
    assert api.rest_client.pool_manager.request.await_count == 0


async def test_request_preserves_auth_refresh_cookie_and_configured_transport(
    pulse: ModuleType,
) -> None:
    client, api = client_with(Response(b'{"items": []}'))
    api.cookie = "session=cookie"
    api.rest_client.proxy = "https://proxy.test"
    api.rest_client.proxy_headers = {"Proxy-Authorization": "opaque"}
    api.rest_client.server_hostname = "cluster.internal"

    async def refresh(
        headers: dict[str, str], query: list[tuple[str, str]], auth: list[str]
    ) -> None:
        assert auth == ["BearerToken"]
        headers["Authorization"] = "Bearer refreshed"
        query.append(("credential", "opaque?&"))

    api.update_params_for_auth.side_effect = refresh
    await client.read_pulse_page(pulse.PulseSource("pods", "", "v1", "pods"), None, "", 100, 1024)
    request = api.rest_client.pool_manager.request.await_args.kwargs
    assert request["headers"]["Authorization"] == "Bearer refreshed"
    assert request["headers"]["User-Agent"] == "korvid-test"
    assert request["headers"]["Cookie"] == "session=cookie"
    assert ("credential", "opaque?&") in parse_qsl(urlsplit(request["url"]).query)
    assert request["proxy"] == "https://proxy.test"
    assert request["proxy_headers"] == {"Proxy-Authorization": "opaque"}
    assert request["server_hostname"] == "cluster.internal"


@pytest.mark.parametrize(
    ("encoding", "payload"),
    [
        ("gzip", b"not compressed"),
        ("gzip", gzip.compress(b'{"items": []}')[:-3]),
        ("gzip", gzip.compress(b'{"items": []}') + b"trailing"),
        ("br", b"unsupported"),
    ],
)
async def test_invalid_compression_fails_closed_and_closes_response(
    pulse: ModuleType, encoding: str, payload: bytes
) -> None:
    response = Response(payload)
    response.headers["Content-Encoding"] = encoding
    client, _api = client_with(response)
    with pytest.raises(KubeClientError, match="Pulse returned"):
        await client.read_pulse_page(
            pulse.PulseSource("pods", "", "v1", "pods"), None, "", 100, 1024
        )
    assert response.closed


async def test_exact_decoded_gzip_budget_is_accepted(pulse: ModuleType) -> None:
    payload = b'{"items": []}' + b" " * 100
    response = Response(gzip.compress(payload))
    response.headers["Content-Encoding"] = "gzip"
    client, _api = client_with(response)
    page = await client.read_pulse_page(
        pulse.PulseSource("pods", "", "v1", "pods"), None, "", 100, len(payload)
    )
    assert page.items == ()
    assert response.closed


async def test_unconfigured_host_is_a_normalized_failure(pulse: ModuleType) -> None:
    client, api = client_with(Response(b'{"items": []}'))
    api.configuration.host = None
    with pytest.raises(KubeClientError, match="not configured"):
        await client.read_pulse_page(
            pulse.PulseSource("pods", "", "v1", "pods"), None, "", 100, 1024
        )
    assert api.rest_client.pool_manager.request.await_count == 0
