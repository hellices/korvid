"""Native Ollama provider — POST /api/chat with NDJSON streaming (issue #72).

Unlike the OpenAI-compatibility shim, the native API accepts per-request
`options.num_ctx` (the shim silently truncates at the VRAM-based default),
a `think` toggle for reasoning models, `keep_alive` passthrough, and returns
tool-call arguments as structured objects instead of string fragments.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from itertools import count
from typing import Any

import httpx

from korvid.agent.credentials import CredentialSource
from korvid.agent.model_policy import CapabilitySource, ModelCapabilities, ModelDescriptor
from korvid.agent.provider import REQUEST_SENT, LLMProvider
from korvid.providers.errors import ProviderError
from korvid.providers.net import make_client
from korvid.providers.stream_limits import (
    MAX_REASONING_BYTES,
    MAX_TOOL_ARGUMENTS_BYTES,
    MAX_TOOL_CALLS,
    BoundedTextAccumulator,
    require_count,
)

logger = logging.getLogger(__name__)

#: FIFO cap on remembered per-turn reasoning (keyed by tool-call id).
_MAX_THINKING_ENTRIES = 64


@dataclass(frozen=True)
class OllamaOptions:
    """Per-request tuning for the native Ollama API (config: `agent.ollama.*`).

    Defaults favor tool dispatch on small local models: a 16k context
    (the server-side default can be as low as 4k), near-greedy decoding
    (the OpenAI shim forces temperature 1.0 when unset), and no reasoning
    tokens (thinking output can dwarf the response on R1-style models).
    """

    num_ctx: int = 16384
    temperature: float = 0.0
    seed: int | None = None
    think: bool = False
    keep_alive: str | int | None = None
    num_predict: int | None = None


def normalize_base_url(base_url: str) -> str:
    """Native API root from a configured base URL.

    Shim-era configs point at `http://host:11434/v1`; the native endpoints
    live at the server root, so a trailing `/v1` is stripped for
    back-compat when `provider: ollama` routes natively.
    """
    base = base_url.rstrip("/")
    return base.removesuffix("/v1")


class OllamaProvider(LLMProvider):
    """LLMProvider adapter for Ollama's native `/api/chat` endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        credentials: CredentialSource | None = None,
        client: httpx.AsyncClient | None = None,
        *,
        options: OllamaOptions | None = None,
        ca_bundle: str | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._base_url = normalize_base_url(base_url)
        self._model = model
        self._credentials = credentials
        self._client = client  # injected or lazily created on first call
        self._owns_client = client is None
        self._ca_bundle = ca_bundle
        self._timeout_seconds = timeout_seconds
        self._options = options or OllamaOptions()
        # Monotonic counter for generated tool-call ids: ids must stay
        # unique across completions within one agent conversation.
        self._id_counter = count()
        # Reasoning text of past assistant turns, keyed by tool-call id, so
        # it can be re-attached to history (the runtime only stores content
        # and tool calls). Bounded FIFO to keep memory flat.
        self._thinking_by_call_id: OrderedDict[str, str] = OrderedDict()

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("ollama", self._model)

    @property
    def capabilities(self) -> ModelCapabilities:
        """Only what the configured request options directly prove.

        `num_ctx` is the exact context window this adapter will request
        (issue #189); Ollama's native API can return multiple tool calls in
        one assistant message, so `supports_parallel_tools` is known-true.
        Neither tool support, reasoning, nor tier is inferred from the model
        tag.
        """
        return ModelCapabilities(
            context_window_tokens=self._options.num_ctx,
            supports_parallel_tools=True,
            provenance={
                "context_window_tokens": CapabilitySource.PROVIDER,
                "supports_parallel_tools": CapabilitySource.PROVIDER,
            },
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # A generous read timeout: a cold start (model load after
            # keep_alive expiry) can take well over a minute to the first
            # token on large local models.
            self._client = make_client(
                self._ca_bundle,
                timeout=httpx.Timeout(
                    self._timeout_seconds,
                    connect=min(10.0, self._timeout_seconds),
                ),
            )
        return self._client

    async def aclose(self) -> None:
        """Close the lazily created HTTP client and credentials (injected clients stay open)."""
        try:
            if self._owns_client and self._client is not None:
                await self._client.aclose()
                self._client = None
        finally:
            if self._credentials is not None:
                await self._credentials.aclose()

    async def _headers(self) -> dict[str, str]:
        if self._credentials is not None:
            return await self._credentials.headers()
        return {}

    def _payload(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        opts = self._options
        request_options: dict[str, Any] = {
            "num_ctx": opts.num_ctx,
            "temperature": opts.temperature,
        }
        if opts.seed is not None:
            request_options["seed"] = opts.seed
        if opts.num_predict is not None:
            request_options["num_predict"] = opts.num_predict
        payload: dict[str, Any] = {
            "model": self._model,
            # Already adapted by prepare_messages *before* the outbound
            # policy ran, so what ships is exactly what was sanitized,
            # snapshotted and size-checked.
            "messages": messages,
            "stream": True,
            "think": opts.think,
            "options": request_options,
        }
        if opts.keep_alive is not None:
            payload["keep_alive"] = opts.keep_alive
        if tools:
            payload["tools"] = tools
        return payload

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield completion events as an async generator.

        The `stream` parameter is part of the LLMProvider signature but has
        no effect here: the native request always streams NDJSON and the
        events yielded are identical either way.
        """
        client = self._get_client()
        tool_calls: list[dict[str, str]] = []
        usage: dict[str, int] | None = None
        thinking = BoundedTextAccumulator(max_bytes=MAX_REASONING_BYTES, label="reasoning")
        terminated = False

        async with client.stream(
            "POST",
            f"{self._base_url}/api/chat",
            json=self._payload(messages, tools),
            headers=await self._headers(),
        ) as resp:
            # The request is on the wire: headers came back, so whatever
            # the status says, this provider has the payload (PR #197).
            yield {"type": REQUEST_SENT}
            if resp.status_code >= 300:
                await resp.aread()
                raise ProviderError(f"Upstream returned HTTP {resp.status_code}: {resp.text}")

            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                chunk = _load_ndjson_chunk(line)
                # Ollama can report a failure mid-stream with HTTP 200: an
                # {"error": ...} object after generation has started. Treat
                # it as a hard failure instead of a truncated "success".
                if chunk.get("error"):
                    raise ProviderError(f"Ollama stream error: {chunk['error']}")
                if chunk.get("done") is True:
                    usage = _usage_from_chunk(chunk)
                    terminated = True
                    break
                message: dict[str, Any] = chunk.get("message") or {}
                # message.thinking is never rendered as answer text, but it is
                # accumulated so the reasoning state can be re-attached to the
                # assistant history on the next iteration (Ollama's streaming
                # contract expects thinking to be echoed back with tool calls).
                thinking.append(str(message.get("thinking") or ""))
                content: str | None = message.get("content")
                if content:
                    yield {"type": "text_delta", "text": content}
                self._collect_tool_calls(message, tool_calls)

        if not terminated:
            raise ProviderError("Ollama stream ended without done: true")

        self._remember_thinking(thinking.value, tool_calls)
        for call in tool_calls:
            yield {"type": "tool_call", **call}

        if usage is not None:
            yield {"type": "usage", **usage}

        yield {"type": "done"}

    def _collect_tool_calls(self, message: dict[str, Any], acc: list[dict[str, str]]) -> None:
        """Fold native tool calls into acc, serializing object arguments exactly once.

        A server-supplied id is preserved (current Ollama releases emit one);
        for older servers that omit it, a monotonically unique `call_N` id is
        generated so the runtime's id-based tool-result correlation keeps
        working across iterations.
        """
        for call in message.get("tool_calls") or []:
            require_count(len(acc), max_count=MAX_TOOL_CALLS, label="tool calls")
            fn: dict[str, Any] = call.get("function") or {}
            arguments = fn.get("arguments")
            native_id = call.get("id")
            serialized_arguments = BoundedTextAccumulator(
                max_bytes=MAX_TOOL_ARGUMENTS_BYTES,
                label="tool call arguments",
            )
            # Ollama hands us one parsed arguments object, so this cap rejects
            # oversized serialized output after JSON parsing rather than
            # bounding fragment accumulation like the OpenAI stream does.
            serialized_arguments.append(
                json.dumps(arguments if isinstance(arguments, dict) else {})
            )
            acc.append(
                {
                    "id": str(native_id) if native_id else f"call_{next(self._id_counter)}",
                    "name": str(fn.get("name", "")),
                    "arguments": serialized_arguments.value,
                }
            )

    def _remember_thinking(self, thinking: str, tool_calls: list[dict[str, str]]) -> None:
        """Key this turn's reasoning by its tool-call ids for history rebuilds.

        Only turns that issue tool calls come back through the history, so
        thinking is stored per call id. The FIFO cap keeps memory flat over
        long conversations.
        """
        if not thinking:
            return
        for call in tool_calls:
            self._thinking_by_call_id[call["id"]] = thinking
        while len(self._thinking_by_call_id) > _MAX_THINKING_ENTRIES:
            self._thinking_by_call_id.popitem(last=False)

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert runtime history (OpenAI-shaped) for the native API.

        - Assistant tool-call arguments are stored as JSON strings by the
          runtime; the native API requires objects, so they are parsed back
          (a parse failure is defensive only and degrades to an empty
          object).
        - `function.index` is reconstructed from the preserved call order so
          parallel calls keep their distinct ordering in model templates.
        - Tool-result messages carry only `tool_call_id`; native history
          identifies the executed function by `tool_name`, recovered from
          the matching assistant call.
        - Reasoning text recorded for this turn's tool calls is re-attached
          as `thinking` so R1-style models keep their reasoning state across
          tool iterations. It is model-authored text about tool results, so
          it is added here — ahead of the outbound policy — and reaches the
          wire only after redaction.
        """
        converted: list[dict[str, Any]] = []
        call_names: dict[str, str] = {}
        for message in messages:
            if message.get("role") == "tool":
                name = call_names.get(str(message.get("tool_call_id", "")))
                converted.append({**message, "tool_name": name} if name else message)
                continue
            calls = message.get("tool_calls")
            if not calls:
                converted.append(message)
                continue
            new_calls = [
                {
                    **call,
                    "function": {
                        **fn,
                        "index": index,
                        "arguments": _parse_arguments(fn.get("arguments")),
                    },
                }
                for index, call in enumerate(calls)
                for fn in [call.get("function") or {}]
            ]
            for call in new_calls:
                if call.get("id"):
                    call_names[str(call["id"])] = str(call["function"].get("name", ""))
            new_message = {**message, "tool_calls": new_calls}
            thinking = next(
                (
                    self._thinking_by_call_id[str(call["id"])]
                    for call in new_calls
                    if str(call.get("id", "")) in self._thinking_by_call_id
                ),
                None,
            )
            if thinking:
                new_message["thinking"] = thinking
            converted.append(new_message)
        return converted


def _load_ndjson_chunk(line: str) -> dict[str, Any]:
    """Parse one NDJSON chunk into a typed provider error."""
    try:
        chunk: dict[str, Any] = json.loads(line)
        return chunk
    except json.JSONDecodeError as exc:
        raise ProviderError("Ollama stream yielded invalid JSON payload") from exc


def _usage_from_chunk(chunk: dict[str, Any]) -> dict[str, int] | None:
    """Map prompt_eval_count/eval_count to the runtime's usage event.

    Emitted only when both counts are present — defaulting a missing count
    to 0 would make an incomplete report look exact (the runtime treats any
    usage event as authoritative).
    """
    if "prompt_eval_count" not in chunk or "eval_count" not in chunk:
        return None
    return {
        "input_tokens": int(chunk["prompt_eval_count"]),
        "output_tokens": int(chunk["eval_count"]),
    }


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    logger.debug("unparsable tool-call arguments dropped: %r", raw)
    return {}
