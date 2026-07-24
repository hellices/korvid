import json
from typing import Any

import httpx
import pytest

from korvid.providers.openai_compat import OpenAICompatProvider, ProviderError


def _sse(*chunks: dict[str, Any]) -> str:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return body + "data: [DONE]\n\n"


def _provider(
    body: str, status: int = 200, capture: dict[str, Any] | None = None
) -> OpenAICompatProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["json"] = json.loads(request.content)
            capture["headers"] = dict(request.headers)
        return httpx.Response(status, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatProvider(
        base_url="http://x/v1", model="m1", api_key="sk-test", client=client
    )


async def test_streams_text_deltas_and_done() -> None:
    body = _sse(
        {"choices": [{"delta": {"content": "Wor"}}]},
        {"choices": [{"delta": {"content": "ld"}}]},
    )
    events = [e async for e in _provider(body).complete([{"role": "user", "content": "hi"}], [])]
    assert {"type": "text_delta", "text": "Wor"} in events
    assert {"type": "text_delta", "text": "ld"} in events
    assert events[-1] == {"type": "done"}


async def test_accumulates_tool_call_fragments() -> None:
    body = _sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "function": {"name": "get_logs", "arguments": '{"po'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'd": "a"}'}}]}}
            ]
        },
    )
    events = [e async for e in _provider(body).complete([], [{"type": "function"}])]
    calls = [e for e in events if e["type"] == "tool_call"]
    assert calls == [
        {"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": '{"pod": "a"}'}
    ]


async def test_reports_usage_when_present() -> None:
    body = _sse(
        {"choices": [{"delta": {"content": "x"}}]},
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
    )
    events = [e async for e in _provider(body).complete([], [])]
    assert {"type": "usage", "input_tokens": 12, "output_tokens": 3} in events


async def test_sends_auth_header_and_tools() -> None:
    cap: dict[str, Any] = {}
    body = _sse({"choices": [{"delta": {"content": "x"}}]})
    tools = [{"type": "function", "function": {"name": "t"}}]
    _ = [
        e
        async for e in _provider(body, capture=cap).complete(
            [{"role": "user", "content": "q"}], tools
        )
    ]
    assert cap["headers"]["authorization"] == "Bearer sk-test"
    assert cap["json"]["tools"] == tools
    assert cap["json"]["stream"] is True
    assert cap["json"]["stream_options"] == {"include_usage": True}


async def test_non_2xx_raises_provider_error() -> None:
    with pytest.raises(ProviderError, match="HTTP 401"):
        _ = [e async for e in _provider("nope", status=401).complete([], [])]


async def test_usage_with_partial_keys_defaults_to_zero() -> None:
    body = _sse(
        {"choices": [{"delta": {"content": "x"}}]},
        {"choices": [], "usage": {"total_tokens": 9}},
    )
    events = [e async for e in _provider(body).complete([], [])]
    assert {"type": "usage", "input_tokens": 0, "output_tokens": 0} in events


async def test_aclose_closes_owned_client_only() -> None:
    owned = OpenAICompatProvider(base_url="http://x/v1", model="m1")
    inner = owned._get_client()
    await owned.aclose()
    assert inner.is_closed

    injected_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    injected = OpenAICompatProvider(base_url="http://x/v1", model="m1", client=injected_client)
    await injected.aclose()
    assert not injected_client.is_closed
    await injected_client.aclose()


async def test_no_auth_header_without_api_key() -> None:
    cap: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["headers"] = dict(request.headers)
        body = _sse({"choices": [{"delta": {"content": "x"}}]})
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatProvider(base_url="http://x/v1", model="m1", client=client)
    _ = [e async for e in provider.complete([], [])]
    assert "authorization" not in cap["headers"]


async def test_azure_style_api_key_header() -> None:
    cap: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["headers"] = dict(request.headers)
        body = _sse({"choices": [{"delta": {"content": "x"}}]})
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatProvider(
        base_url="http://x/openai/v1",
        model="m1",
        api_key="azure-key",
        client=client,
        auth_header="api-key",
    )
    _ = [e async for e in provider.complete([], [])]
    assert cap["headers"]["api-key"] == "azure-key"
    assert "authorization" not in cap["headers"]
