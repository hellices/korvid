from __future__ import annotations

from collections.abc import Callable

import pytest

from korvid.agent.model_profiles import ModelEntrySource
from korvid.providers.endpoint_discovery import EndpointDiscovery

pytestmark = pytest.mark.anyio

httpx = pytest.importorskip("httpx")


def _factory(handler: Callable[[httpx.Request], httpx.Response]) -> Callable[[], httpx.AsyncClient]:
    def make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return make


def _routes(
    mapping: dict[str, httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler that dispatches by URL path."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if path in mapping:
            return mapping[path]
        return httpx.Response(404)

    handler.captured = captured  # type: ignore[attr-defined]
    return handler


async def test_openai_shaped_response_becomes_model_entry() -> None:
    """An OpenAI-compat `{"data": [{"id": "m"}]}` becomes one `ModelEntry`."""
    handler = _routes(
        {
            "/v1/models": httpx.Response(
                200,
                json={"data": [{"id": "m"}]},
                headers={"content-type": "application/json"},
            ),
        }
    )
    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=None, prefix="openai"
    )
    assert len(entries) == 1
    assert entries[0].reference == "openai/m"
    assert entries[0].source is ModelEntrySource.ENDPOINT


async def test_ollama_shaped_response_keeps_colon_in_tag() -> None:
    """An Ollama `{"models": [{"name": "qwen3:8b"}]}` becomes `{prefix}/qwen3:8b`.

    The colon must survive — it is the Ollama tag separator and is valid
    in the model portion of a reference.
    """
    handler = _routes(
        {
            "/v1/models": httpx.Response(404),
            "/api/tags": httpx.Response(
                200,
                json={"models": [{"name": "qwen3:8b"}]},
                headers={"content-type": "application/json"},
            ),
        }
    )
    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:11434", api_key=None, prefix="ollama"
    )
    assert len(entries) == 1
    assert entries[0].reference == "ollama/qwen3:8b"
    assert entries[0].source is ModelEntrySource.ENDPOINT


async def test_404_on_v1_models_falls_through_to_api_tags() -> None:
    handler = _routes(
        {
            "/v1/models": httpx.Response(404),
            "/api/tags": httpx.Response(
                200,
                json={"models": [{"name": "llama3"}]},
                headers={"content-type": "application/json"},
            ),
        }
    )
    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=None, prefix="ollama"
    )
    assert len(entries) == 1
    assert entries[0].reference == "ollama/llama3"


async def test_both_endpoints_failing_returns_empty() -> None:
    handler = _routes(
        {
            "/v1/models": httpx.Response(500),
            "/api/tags": httpx.Response(500),
        }
    )
    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=None, prefix="custom"
    )
    assert entries == ()


async def test_connection_error_returns_empty_and_does_not_raise() -> None:
    """Network failure is silent — type the name yourself is a better outcome."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    discovery = EndpointDiscovery(client_factory=_factory(boom))
    entries = await discovery.list_models(
        base_url="http://localhost:9999", api_key=None, prefix="custom"
    )
    assert entries == ()


async def test_oversized_body_returns_empty() -> None:
    """A body exceeding the 2 MiB ceiling is discarded silently."""
    oversized = b"x" * (2 * 1024 * 1024 + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized, headers={"content-type": "application/json"})

    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=None, prefix="openai"
    )
    assert entries == ()


async def test_more_than_500_entries_are_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": f"model-{i}"} for i in range(600)]},
            headers={"content-type": "application/json"},
        )

    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=None, prefix="openai"
    )
    assert len(entries) == 500


async def test_authorization_header_present_when_key_supplied() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-4o"}]},
            headers={"content-type": "application/json"},
        )

    discovery = EndpointDiscovery(client_factory=_factory(handler))
    await discovery.list_models(base_url="http://host:8080", api_key="sk-test", prefix="openai")
    assert seen
    assert seen[0].headers.get("authorization") == "Bearer sk-test"


async def test_authorization_header_absent_when_no_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-4o"}]},
            headers={"content-type": "application/json"},
        )

    discovery = EndpointDiscovery(client_factory=_factory(handler))
    await discovery.list_models(base_url="http://host:8080", api_key=None, prefix="openai")
    assert seen
    assert "authorization" not in seen[0].headers
