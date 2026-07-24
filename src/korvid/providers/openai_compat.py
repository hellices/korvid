"""OpenAI-compatible streaming provider adapter.

Implements LLMProvider via SSE streaming over httpx.
Non-2xx responses raise ProviderError.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from korvid.agent.provider import LLMProvider


class ProviderError(Exception):
    """Raised when the upstream API returns a non-2xx response."""


class OpenAICompatProvider(LLMProvider):
    """Adapter for any OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        auth_header: str = "Authorization",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._auth_header = auth_header
        self._client = client  # injected or lazily created on first call
        self._owns_client = client is None

    @property
    def name(self) -> str:
        return self._model

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return self._client

    async def aclose(self) -> None:
        """Close the lazily created HTTP client (injected clients stay open)."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key is not None:
            if self._auth_header == "Authorization":
                headers["Authorization"] = "Bearer " + self._api_key
            else:
                # e.g. Azure OpenAI expects the raw key in an "api-key" header
                headers[self._auth_header] = self._api_key
        return headers

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield completion events as an async generator."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools

        client = self._get_client()
        async with client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            json=payload,
            headers=self._headers(),
        ) as resp:
            if resp.status_code >= 300:
                await resp.aread()
                raise ProviderError(f"Upstream returned HTTP {resp.status_code}: {resp.text}")

            # tool_calls[index] = {"id": str, "name": str, "arguments": str}
            tool_acc: dict[int, dict[str, str]] = {}
            last_usage: dict[str, int] | None = None

            async for line in resp.aiter_lines():
                # SSE permits both "data:<value>" and "data: <value>" — strip
                # at most one optional leading space from the field value.
                if not line.startswith("data:"):
                    continue
                payload_str = line[len("data:") :].removeprefix(" ")
                if payload_str == "[DONE]":
                    break

                chunk: dict[str, Any] = json.loads(payload_str)

                # Capture top-level usage (sent in the final chunk by many providers)
                raw_usage = chunk.get("usage")
                if raw_usage:
                    last_usage = raw_usage

                text = _chunk_text(chunk, tool_acc)
                if text:
                    yield {"type": "text_delta", "text": text}

        # Emit accumulated tool calls in index order
        for idx in sorted(tool_acc):
            acc = tool_acc[idx]
            yield {
                "type": "tool_call",
                "id": acc["id"],
                "name": acc["name"],
                "arguments": acc["arguments"],
            }

        if last_usage:
            yield {
                "type": "usage",
                "input_tokens": int(last_usage.get("prompt_tokens", 0)),
                "output_tokens": int(last_usage.get("completion_tokens", 0)),
            }

        yield {"type": "done"}


def _chunk_text(chunk: dict[str, Any], tool_acc: dict[int, dict[str, str]]) -> str | None:
    """Extract the text delta from one SSE chunk; fold tool-call fragments into tool_acc."""
    choices: list[dict[str, Any]] = chunk.get("choices", [])
    if not choices:
        return None

    delta: dict[str, Any] = choices[0].get("delta", {})
    for frag in delta.get("tool_calls") or []:
        idx: int = frag["index"]
        acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        if frag.get("id"):
            acc["id"] = frag["id"]
        fn: dict[str, str] = frag.get("function", {})
        if fn.get("name"):
            acc["name"] = fn["name"]
        acc["arguments"] += fn.get("arguments", "")

    content: str | None = delta.get("content")
    return content or None
