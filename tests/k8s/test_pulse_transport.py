import asyncio
import gzip
import json
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from kubernetes_asyncio.client import ApiClient, Configuration, rest

from korvid.core.pulse_collector import PulseCollector
from korvid.k8s.client import KubeClient
from korvid.k8s.errors import KubeClientError
from korvid.k8s.pulse import PulseLimitError, PulseSource, _bounded_session, read_pulse_page


@asynccontextmanager
async def serve_http(responder: Callable[[bytes], bytes | None]) -> AsyncIterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.close_connection = True
            header = self.requestline.encode("iso-8859-1") + b"\r\n"
            response = responder(header + self.headers.as_bytes() + b"\r\n")
            if response is not None:
                self.wfile.write(response)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        await asyncio.to_thread(server.shutdown)
        await asyncio.to_thread(server.server_close)
        thread.join()


@asynccontextmanager
async def serve_body(payload: bytes, encoding: str) -> AsyncIterator[str]:
    headers = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Encoding: {encoding}\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    )
    async with serve_http(lambda _request: headers.encode() + payload) as host:
        yield host


async def test_gzip_expansion_is_bounded_before_sdk_response_buffering() -> None:
    compressed = gzip.compress(b'{"items": []}' + b" " * (6 * 1024 * 1024))
    responses: list[Any] = []
    sessions: list[Any] = []
    connectors: list[Any] = []
    async with (
        serve_body(compressed, "gzip") as host,
        ApiClient(Configuration(host=host)) as api,
    ):
        shared = api.rest_client.pool_manager
        retry_policy = getattr(shared, "_retry_connection", None)
        request = type(shared).request

        async def capture(session: Any, **kwargs: Any) -> Any:
            sessions.append(session)
            connectors.append(session.connector)
            response = await request(session, **kwargs)
            responses.append(response)
            return response

        with (
            patch.object(type(shared), "request", new=capture),
            pytest.raises(PulseLimitError, match="byte"),
        ):
            await read_pulse_page(api, PulseSource("pods", "", "v1", "pods"), None, "", 100, 262144)
        assert len(responses) == 1
        assert responses[0].closed
        assert responses[0].content.total_bytes <= len(compressed)
        assert connectors == [shared.connector]
        assert sessions[0] is not shared
        assert sessions[0].closed
        assert not shared.closed
        assert shared.auto_decompress
        assert getattr(shared, "_retry_connection", None) == retry_policy


async def test_gzip_page_remains_readable_with_real_configured_client() -> None:
    compressed = gzip.compress(b'{"items": [{"metadata": {"uid": "pod-1"}}]}')
    async with (
        serve_body(compressed, "gzip") as host,
        ApiClient(Configuration(host=host)) as api,
    ):
        page = await read_pulse_page(
            api, PulseSource("pods", "", "v1", "pods"), None, "", 100, 262144
        )
        assert page.items == ({"metadata": {"uid": "pod-1"}},)


@pytest.mark.parametrize("mode", ["redirect", "retry", "retry-without-switch"])
async def test_implicit_http_attempts_cannot_exceed_refresh_budget(mode: str) -> None:
    received: list[str] = []
    attempts: Counter[str] = Counter()

    def reply(header: bytes) -> bytes | None:
        target = header.split(b"\r\n", 1)[0].decode().split(" ")[1]
        received.append(target)
        attempts[target] += 1
        parsed = urlsplit(target)
        if parsed.path == "/ordinary":
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
        query = parse_qs(parsed.query)
        hop = int(query.get("hop", ["0"])[0])
        if mode == "redirect" and hop < 2:
            return (
                "HTTP/1.1 302 Found\r\n"
                f"Location: {parsed.path}?hop={hop + 1}\r\n"
                "Content-Length: 0\r\nConnection: close\r\n\r\n"
            ).encode()
        if mode != "redirect" and attempts[target] == 1:
            return None
        continuation = "" if mode == "redirect" or "continue" in query else "next"
        body = json.dumps({"items": [], "metadata": {"continue": continuation}}).encode()
        return (
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode() + body

    async with serve_http(reply) as host:
        configuration = Configuration(host=host)
        configuration.connection_pool_maxsize = 3
        async with ApiClient(configuration) as api:
            await _check_attempt_budget(api, mode, received, attempts)


async def _check_attempt_budget(
    api: ApiClient, mode: str, received: list[str], attempts: Counter[str]
) -> None:
    request = type(api.rest_client.pool_manager).request

    async def legacy_retry(session: Any, **kwargs: Any) -> Any:
        try:
            return await request(session, **kwargs)
        except cast(Any, rest).aiohttp.ServerDisconnectedError:
            if mode != "retry-without-switch":
                raise
            return await request(session, **kwargs)

    client = KubeClient()
    client._api = api
    sources = (
        PulseSource("pods", "", "v1", "pods"),
        PulseSource("deployments", "apps", "v1", "deployments"),
        PulseSource("events", "", "v1", "events", "type=Warning"),
    )
    with patch.object(type(api.rest_client.pool_manager), "request", new=legacy_retry):
        results = await PulseCollector(client, sources).collect("default")
    assert len(received) == 3
    assert all(count == 1 for count in attempts.values())
    assert all(result.coverage.state in {"failed", "capped"} for result in results)
    connector = api.rest_client.pool_manager.connector
    assert connector is not None
    assert not connector._acquired
    response = await api.call_api("/ordinary", "GET", _preload_content=False, _request_timeout=1)
    try:
        assert response.status == 200
        assert len(received) == 4
    finally:
        response.close()


async def test_closed_transport_cannot_create_an_unconfigured_connection() -> None:
    api = ApiClient(Configuration(host="http://127.0.0.1:1"))
    await api.close()
    with pytest.raises(KubeClientError, match="closed"):
        await read_pulse_page(api, PulseSource("pods", "", "v1", "pods"), None, "", 100, 262144)
    assert api.rest_client.pool_manager.closed


async def test_budget_guard_uses_only_cleanup_safe_trace_boundaries() -> None:
    async with (
        ApiClient(Configuration(host="http://127.0.0.1:1")) as api,
        _bounded_session(api.rest_client.pool_manager) as session,
    ):
        budget = session.trace_configs[0]
        assert len(budget.on_request_start) == 1
        assert len(budget.on_request_headers_sent) == 1
        assert len(budget.on_connection_create_start) == 0
        assert len(budget.on_connection_reuseconn) == 0
