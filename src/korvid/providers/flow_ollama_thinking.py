"""Native thinking for Ollama, claimed by option and declared as data.

The shared transport speaks the compatibility dialect, which drops three
things this endpoint's own protocol carries: a per-request `num_ctx` (the
shim silently truncates at the VRAM-based default), a `think` toggle for
reasoning models, and structured tool-call arguments. An operator who
needs them turns on one option; everything else about the reference —
including the host it goes to — stays exactly as it was.

The claim is that option, never the prefix: with the option off this
module is not in the path at all.
"""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from itertools import count
from typing import Any

import httpx

from korvid.agent.credentials import CredentialSource
from korvid.agent.model_policy import CapabilitySource, ModelCapabilities, ModelDescriptor
from korvid.agent.model_profiles import (
    EndpointRequirement,
    ModelConnectionConfig,
    SetupField,
    SetupFieldKind,
    SpecialFlow,
    split_reference,
)
from korvid.agent.provider import REQUEST_SENT, LLMProvider
from korvid.providers.net import make_client
from korvid.providers.static_creds import StaticHeaderSource
from korvid.providers.token_store import TokenStore

logger = logging.getLogger(__name__)

#: The prefix this flow shares with the standard transport. Sharing is
#: not claiming: nothing here runs unless `OPTION_KEY` is on.
PREFIX = "ollama"
#: The option an operator turns on to get the native protocol.
OPTION_KEY = "native_thinking"
#: The same host the standard transport uses for this prefix, so opting
#: in never silently changes *where* the request goes.
DEFAULT_ENDPOINT = "http://localhost:11434"
#: FIFO cap on remembered per-turn reasoning (keyed by tool-call id).
_MAX_THINKING_ENTRIES = 64
_SERVABLE_AUTH_METHODS = frozenset({"none", "environment", "keyring"})


class ProviderError(Exception):
    """Raised when the upstream API returns a non-2xx response."""


@dataclass(frozen=True)
class OllamaOptions:
    """Per-request tuning for the native API (profile `options.*`).

    Defaults favor tool dispatch on small local models: a 16k context
    (the server-side default can be as low as 4k), near-greedy decoding
    (the compatibility dialect forces temperature 1.0 when unset), and no
    reasoning tokens (thinking output can dwarf the response on R1-style
    models).
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
    back-compat.
    """
    base = base_url.rstrip("/")
    return base.removesuffix("/v1")


class OllamaProvider(LLMProvider):
    """LLMProvider adapter for the native `/api/chat` endpoint."""

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
        return ModelDescriptor(PREFIX, self._model)

    @property
    def capabilities(self) -> ModelCapabilities:
        """Only what the configured request options directly prove.

        `num_ctx` is the exact context window this adapter will request
        (issue #189); the native API can return multiple tool calls in one
        assistant message, so `supports_parallel_tools` is known-true.
        Neither tool support, reasoning, nor tier is inferred from the
        model tag.
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
        """Close the lazily created client and credentials (injected clients stay open)."""
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
        thinking = ""

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
                chunk: dict[str, Any] = json.loads(line)
                # The server can report a failure mid-stream with HTTP 200:
                # an {"error": ...} object after generation has started.
                # Treat it as a hard failure, not a truncated "success".
                if chunk.get("error"):
                    raise ProviderError(f"Native stream error: {chunk['error']}")
                message: dict[str, Any] = chunk.get("message") or {}
                # message.thinking is never rendered as answer text, but it
                # is accumulated so the reasoning state can be re-attached
                # to the assistant history on the next iteration (the
                # streaming contract expects thinking to be echoed back
                # with tool calls).
                thinking += str(message.get("thinking") or "")
                content: str | None = message.get("content")
                if content:
                    yield {"type": "text_delta", "text": content}
                self._collect_tool_calls(message, tool_calls)
                if chunk.get("done"):
                    usage = _usage_from_chunk(chunk)

        self._remember_thinking(thinking, tool_calls)
        for call in tool_calls:
            yield {"type": "tool_call", **call}

        if usage is not None:
            yield {"type": "usage", **usage}

        yield {"type": "done"}

    def _collect_tool_calls(self, message: dict[str, Any], acc: list[dict[str, str]]) -> None:
        """Fold native tool calls into acc, serializing object arguments exactly once.

        A server-supplied id is preserved (current releases emit one); for
        older servers that omit it, a monotonically unique `call_N` id is
        generated so the runtime's id-based tool-result correlation keeps
        working across iterations.
        """
        for call in message.get("tool_calls") or []:
            fn: dict[str, Any] = call.get("function") or {}
            arguments = fn.get("arguments")
            native_id = call.get("id")
            acc.append(
                {
                    "id": str(native_id) if native_id else f"call_{next(self._id_counter)}",
                    "name": str(fn.get("name", "")),
                    "arguments": json.dumps(arguments if isinstance(arguments, dict) else {}),
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
        """Convert the runtime's history to the native dialect.

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


def _int(value: object, default: int) -> int:
    """An integer option, or the adapter's default.

    `bool` is excluded on purpose: it is an `int` subclass, so a `true`
    left in a config would otherwise become a context window of 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        if value is not None:
            logger.warning("ignoring unusable option value %r; using %r", value, default)
        return default
    return value


def _bool(key: str, value: object, default: bool) -> bool:
    """A boolean option, or the adapter's default.

    Only a real `bool` decides. A hand-written `think: "false"` is a
    *string*, and a truthiness test would read it as on — the opposite of
    what the line says — so anything that is not a boolean falls back to
    *default* and names the key it came from.

    `bool` is not widened to `int` on purpose: `1` and `0` are not this
    toggle's vocabulary, and accepting them would make `think: 2` mean
    something.
    """
    if isinstance(value, bool):
        return value
    if value is not None:
        logger.warning("ignoring unusable %r option value %r; using %r", key, value, default)
    return default


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        logger.warning("ignoring unusable option value %r", value)
        return None
    return value


def _float(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        if value is not None:
            logger.warning("ignoring unusable option value %r; using %r", value, default)
        return default
    return float(value)


def _keep_alive(value: object) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int):
        logger.warning("ignoring unusable keep_alive value %r", value)
        return None
    return value


def _options_from(profile_options: Mapping[str, object]) -> OllamaOptions:
    """Read the profile's options, refusing values the wire cannot carry.

    An unusable value falls back to the adapter's default rather than
    being shipped: a `"8192"` sent verbatim would land in
    `context_window_tokens` as text and make the model's own limit
    unknowable.

    `think` defaults to `False`, the same default the pre-profile parser
    substituted (`agent.ollama.think` was read as `raw.get("think") is
    True`). Turning the *transport* on is not consent to reasoning
    output, which can dwarf the answer on R1-style models, so only an
    explicit boolean `true` asks for it.
    """
    return OllamaOptions(
        num_ctx=_int(profile_options.get("num_ctx"), 16384),
        temperature=_float(profile_options.get("temperature"), 0.0),
        seed=_optional_int(profile_options.get("seed")),
        think=_bool("think", profile_options.get("think"), False),
        keep_alive=_keep_alive(profile_options.get("keep_alive")),
        num_predict=_optional_int(profile_options.get("num_predict")),
    )


def _credentials_for(profile: ModelConnectionConfig) -> CredentialSource | None:
    """Build the credential source, or raise `LookupError` to refuse.

    `None` is a real answer here — a local server usually needs no
    credential at all — so a refusal cannot be expressed by returning it.
    """
    method = profile.auth.method
    if method == "none":
        return None
    name = str(profile.auth.settings.get("key") or "")
    if not name:
        raise LookupError(f"auth method {method!r} names no credential")
    if method == "environment":
        value = os.environ.get(name)
        if not value:
            raise LookupError(f"environment variable {name} is not set")
        return StaticHeaderSource(value)
    stored = TokenStore().load(name)
    if not stored:
        raise LookupError(f"no stored credential named {name}")
    return StaticHeaderSource(stored)


def build_provider(profile: ModelConnectionConfig) -> LLMProvider | None:
    """Build the native transport for a profile that opted in.

    Returns `None` for anything this flow cannot serve. The factory turns
    that into a disabled agent with the reason it already logged, which is
    the same refusal every other unbuildable profile gets.
    """
    _prefix, tag = split_reference(profile.model)
    if not tag:
        logger.warning("%r names no model tag", profile.model)
        return None
    if profile.auth.method not in _SERVABLE_AUTH_METHODS:
        logger.warning(
            "the native route cannot serve auth method %r for %r",
            profile.auth.method,
            profile.model,
        )
        return None
    try:
        credentials = _credentials_for(profile)
    except LookupError as exc:
        logger.warning("credential unavailable for %r: %s", profile.model, exc)
        return None
    ca_bundle = profile.options.get("ca_bundle")
    return OllamaProvider(
        base_url=profile.endpoint or DEFAULT_ENDPOINT,
        model=tag,
        credentials=credentials,
        options=_options_from(profile.options),
        ca_bundle=str(ca_bundle) if ca_bundle else None,
    )


def ollama_thinking_flow() -> SpecialFlow:
    """Declare the flow the entry point publishes.

    `auth_methods` is deliberately empty: a declared list *replaces* the
    catalog's generic one for every reference the prefix resolves to,
    including the ones this flow does not serve, so declaring one here
    would narrow the choices for ordinary routed profiles.
    """
    return SpecialFlow(
        prefix=PREFIX,
        display_name="Ollama (native)",
        auth_methods=(),
        option_fields=(
            SetupField(
                key=OPTION_KEY,
                label="Use the native API (thinking, num_ctx)",
                kind=SetupFieldKind.BOOLEAN,
                default="false",
                help_text=(
                    "Sends requests to /api/chat instead of the compatibility "
                    "endpoint, so the model's reasoning and the configured "
                    "context window are carried."
                ),
            ),
        ),
        endpoint=EndpointRequirement.OPTIONAL,
        claims_option=OPTION_KEY,
        build_provider=build_provider,
    )


def korvid_special_flows() -> tuple[SpecialFlow, ...]:
    """The extension point's contract: what this module contributes."""
    return (ollama_thinking_flow(),)
