import json
from typing import Any

import httpx
import pytest

from korvid.providers.ollama import OllamaOptions, OllamaProvider
from korvid.providers.openai_compat import ProviderError
from korvid.providers.static_creds import StaticHeaderSource


def _ndjson(*chunks: dict[str, Any]) -> str:
    return "".join(json.dumps(c) + "\n" for c in chunks)


def _done(**counts: int) -> dict[str, Any]:
    return {"done": True, "message": {"role": "assistant", "content": ""}, **counts}


def _provider(
    body: str,
    status: int = 200,
    capture: dict[str, Any] | None = None,
    *,
    base_url: str = "http://x:11434",
    options: OllamaOptions | None = None,
    credentials: StaticHeaderSource | None = None,
) -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["url"] = str(request.url)
            capture["json"] = json.loads(request.content)
            capture["headers"] = dict(request.headers)
        return httpx.Response(status, text=body, headers={"content-type": "application/x-ndjson"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaProvider(
        base_url=base_url,
        model="m1",
        credentials=credentials,
        client=client,
        options=options or OllamaOptions(),
    )


async def _events(
    provider: OllamaProvider, messages: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    msgs = messages if messages is not None else [{"role": "user", "content": "hi"}]
    return [e async for e in provider.complete(msgs, [])]


async def test_streams_text_deltas_and_done() -> None:
    body = _ndjson(
        {"message": {"role": "assistant", "content": "Wor"}, "done": False},
        {"message": {"role": "assistant", "content": "ld"}, "done": False},
        _done(),
    )
    events = await _events(_provider(body))
    assert {"type": "text_delta", "text": "Wor"} in events
    assert {"type": "text_delta", "text": "ld"} in events
    assert events[-1] == {"type": "done"}


async def test_structured_tool_calls_serialized_once() -> None:
    body = _ndjson(
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "get_logs", "arguments": {"pod": "web-1"}}},
                    {"function": {"name": "get_events", "arguments": {"ns": "prod"}}},
                ],
            },
            "done": False,
        },
        _done(),
    )
    events = await _events(_provider(body))
    calls = [e for e in events if e["type"] == "tool_call"]
    assert calls == [
        {"type": "tool_call", "id": "call_0", "name": "get_logs", "arguments": '{"pod": "web-1"}'},
        {"type": "tool_call", "id": "call_1", "name": "get_events", "arguments": '{"ns": "prod"}'},
    ]


async def test_reports_usage_from_eval_counts() -> None:
    body = _ndjson(_done(prompt_eval_count=12, eval_count=7))
    events = await _events(_provider(body))
    assert {"type": "usage", "input_tokens": 12, "output_tokens": 7} in events


async def test_usage_with_partial_counts_is_not_emitted() -> None:
    body = _ndjson(_done(eval_count=7))
    events = await _events(_provider(body))
    assert not [e for e in events if e["type"] == "usage"]


async def test_request_carries_options_think_and_keep_alive() -> None:
    capture: dict[str, Any] = {}
    options = OllamaOptions(num_ctx=8192, temperature=0.5, seed=42, think=True, keep_alive="10m")
    provider = _provider(_ndjson(_done()), capture=capture, options=options)
    await _events(provider)
    payload = capture["json"]
    assert payload["model"] == "m1"
    assert payload["stream"] is True
    assert payload["think"] is True
    assert payload["keep_alive"] == "10m"
    assert payload["options"] == {"num_ctx": 8192, "temperature": 0.5, "seed": 42}


async def test_default_options_omit_seed_and_keep_alive() -> None:
    capture: dict[str, Any] = {}
    provider = _provider(_ndjson(_done()), capture=capture)
    await _events(provider)
    payload = capture["json"]
    assert payload["think"] is False
    assert "keep_alive" not in payload
    assert payload["options"] == {"num_ctx": 16384, "temperature": 0.0}


async def test_posts_to_native_chat_endpoint() -> None:
    capture: dict[str, Any] = {}
    provider = _provider(_ndjson(_done()), capture=capture)
    await _events(provider)
    assert capture["url"] == "http://x:11434/api/chat"


async def test_shim_era_v1_base_url_is_normalized() -> None:
    capture: dict[str, Any] = {}
    provider = _provider(_ndjson(_done()), capture=capture, base_url="http://x:11434/v1/")
    await _events(provider)
    assert capture["url"] == "http://x:11434/api/chat"


async def test_assistant_tool_call_arguments_converted_to_objects() -> None:
    capture: dict[str, Any] = {}
    provider = _provider(_ndjson(_done()), capture=capture)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "get_logs", "arguments": '{"pod": "web-1"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "ok"},
    ]
    await _events(provider, messages)
    sent = capture["json"]["messages"]
    assert sent[1]["tool_calls"][0]["function"]["arguments"] == {"pod": "web-1"}
    assert sent[2] == {"role": "tool", "tool_call_id": "call_0", "content": "ok"}


async def test_unparsable_assistant_arguments_fall_back_to_empty_object() -> None:
    capture: dict[str, Any] = {}
    provider = _provider(_ndjson(_done()), capture=capture)
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "t", "arguments": "{broken"}}],
        },
    ]
    await _events(provider, messages)
    assert capture["json"]["messages"][0]["tool_calls"][0]["function"]["arguments"] == {}


async def test_thinking_field_is_not_yielded_as_text() -> None:
    body = _ndjson(
        {"message": {"role": "assistant", "content": "", "thinking": "let me see"}, "done": False},
        {"message": {"role": "assistant", "content": "answer"}, "done": False},
        _done(),
    )
    events = await _events(_provider(body))
    texts = [e["text"] for e in events if e["type"] == "text_delta"]
    assert texts == ["answer"]


async def test_tools_forwarded_and_auth_header_sent() -> None:
    capture: dict[str, Any] = {}
    provider = _provider(
        _ndjson(_done()), capture=capture, credentials=StaticHeaderSource("sk-test")
    )
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    events = [e async for e in provider.complete([{"role": "user", "content": "hi"}], tools)]
    assert events[-1] == {"type": "done"}
    assert capture["json"]["tools"] == tools
    assert capture["headers"]["authorization"] == "Bearer sk-test"


async def test_non_2xx_raises_provider_error() -> None:
    provider = _provider("model not found", status=404)
    with pytest.raises(ProviderError, match="HTTP 404"):
        await _events(provider)


async def test_aclose_closes_owned_client_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_ndjson(_done()))

    injected = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaProvider(base_url="http://x:11434", model="m1", client=injected)
    await provider.aclose()
    assert not injected.is_closed

    owned = OllamaProvider(base_url="http://x:11434", model="m1")
    client = owned._get_client()
    await owned.aclose()
    assert client.is_closed


def test_name_is_the_model() -> None:
    provider = OllamaProvider(base_url="http://x:11434", model="qwen3:8b")
    assert provider.name == "qwen3:8b"
