"""The native thinking flow, declared as data on the extension point.

Migrated from the thinking half of `tests/providers/test_ollama.py`
(Task 17). The claim is the `native_thinking` *option*, not the `ollama/`
prefix: with the option off — the default — `ollama/qwen3:8b` goes
through the shared transport like everything else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from korvid.agent.model_policy import CapabilitySource, ModelDescriptor
from korvid.agent.model_profiles import (
    ConnectionAuthConfig,
    ModelConnectionConfig,
    SpecialFlow,
)
from korvid.agent.provider import (
    MAX_REASONING_CHARS,
    MAX_TOOL_ARGUMENT_CHARS,
    MAX_TOOL_CALLS_PER_RESPONSE,
    REQUEST_SENT,
    STREAM_FAILED,
    STREAM_MALFORMED,
    STREAM_TRUNCATED,
    ProviderProtocolError,
    ProviderStreamError,
    ProviderStreamLimitError,
    ProviderStreamTruncatedError,
)
from korvid.providers.flow_ollama_thinking import (
    OllamaOptions,
    OllamaProvider,
    build_provider,
    ollama_thinking_flow,
)
from korvid.providers.litellm_factory import create_provider_from_profile
from korvid.providers.special_flows import SpecialFlowRegistry


def _ndjson(*chunks: dict[str, Any]) -> str:
    return "".join(json.dumps(c) + "\n" for c in chunks)


def _done(**counts: int) -> dict[str, Any]:
    return {"done": True, "message": {"role": "assistant", "content": ""}, **counts}


def _client(
    body: str, capture: dict[str, Any] | None = None, status: int = 200
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["url"] = str(request.url)
            capture["json"] = json.loads(request.content)
            capture["headers"] = dict(request.headers)
        return httpx.Response(status, text=body, headers={"content-type": "application/x-ndjson"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _profile(
    reference: str = "ollama/qwen3:8b",
    *,
    endpoint: str | None = "http://x:11434",
    options: dict[str, object] | None = None,
    method: str = "none",
    settings: dict[str, object] | None = None,
) -> ModelConnectionConfig:
    return ModelConnectionConfig(
        model=reference,
        endpoint=endpoint,
        auth=ConnectionAuthConfig(method=method, settings=settings or {}),
        options=options if options is not None else {"native_thinking": True},
    )


async def _events(
    provider: OllamaProvider, messages: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    msgs = messages if messages is not None else [{"role": "user", "content": "hi"}]
    return [e async for e in provider.complete(msgs, [])]


def _built(
    profile: ModelConnectionConfig, capture: dict[str, Any], body: str = ""
) -> OllamaProvider:
    provider = build_provider(profile)
    assert isinstance(provider, OllamaProvider)
    provider._client = _client(body or _ndjson(_done()), capture)
    provider._owns_client = True
    return provider


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def test_the_flow_claims_an_option_not_a_prefix() -> None:
    flow = ollama_thinking_flow()
    assert isinstance(flow, SpecialFlow)
    assert flow.claims_option == "native_thinking"
    assert flow.prefix == "ollama"


def test_the_option_defaults_off_so_ollama_routes_through_litellm() -> None:
    """Parity is opt-in. The default path for ollama/* must be the same
    path every other model takes."""
    registry = SpecialFlowRegistry([ollama_thinking_flow()])
    assert registry.claim_by_option("ollama/qwen3:8b", {}) is None
    assert registry.claim_by_option("ollama/qwen3:8b", {"native_thinking": False}) is None
    assert registry.claim_by_option("ollama/qwen3:8b", {"native_thinking": "yes"}) is None


def test_the_option_on_reaches_this_flow() -> None:
    flow = ollama_thinking_flow()
    registry = SpecialFlowRegistry([flow])
    assert registry.claim_by_option("ollama/qwen3:8b", {"native_thinking": True}) is flow


def test_the_flow_offers_the_option_as_a_setup_field() -> None:
    """An operator can only opt in to a field the wizard renders."""
    keys = {field.key for field in ollama_thinking_flow().option_fields}
    assert "native_thinking" in keys


def test_the_shipped_distribution_registers_the_flow_on_the_entry_point() -> None:
    from korvid.providers.litellm_runtime import models_by_provider

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes=models_by_provider())
    claimed = registry.claim_by_option("ollama/qwen3:8b", {"native_thinking": True})
    assert claimed is not None
    assert claimed.claims_option == "native_thinking"


def test_the_installed_flow_does_not_take_the_prefix_from_the_shared_transport() -> None:
    """The entry point is named for the prefix it shares, so the registry
    can find it without loading every plugin. Sharing a prefix is not
    claiming it: with the option off the reference must still route."""
    from korvid.providers.litellm_provider import LiteLLMProvider
    from korvid.providers.litellm_runtime import models_by_provider

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes=models_by_provider())
    provider = create_provider_from_profile(_profile(options={}), flows=registry)
    assert isinstance(provider, LiteLLMProvider)


def test_the_option_on_takes_the_installed_flows_transport() -> None:
    from korvid.providers.litellm_runtime import models_by_provider

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes=models_by_provider())
    provider = create_provider_from_profile(_profile(), flows=registry)
    assert isinstance(provider, OllamaProvider)


def test_the_flow_leaves_the_generic_auth_methods_alone() -> None:
    """A declared auth list *replaces* the catalog's generic one for every
    reference the prefix resolves to, including the ones this flow does
    not serve. Sharing a prefix must not narrow them."""
    assert ollama_thinking_flow().auth_methods == ()


# ---------------------------------------------------------------------------
# Thinking — the reason this flow exists
# ---------------------------------------------------------------------------


async def test_thinking_content_is_surfaced_when_the_option_is_on() -> None:
    """`message.thinking` exists only on the native endpoint. It is kept
    for the assistant history and never yielded as answer text."""
    capture: dict[str, Any] = {}
    body = _ndjson(
        {
            "message": {"role": "assistant", "content": "", "thinking": "step 1; "},
            "done": False,
        },
        {
            "message": {
                "role": "assistant",
                "content": "",
                "thinking": "step 2",
                "tool_calls": [{"function": {"name": "get_logs", "arguments": {}}}],
            },
            "done": False,
        },
        _done(),
    )
    provider = _built(_profile(), capture, body)
    events = await _events(provider)
    call_id = next(e["id"] for e in events if e["type"] == "tool_call")

    history: list[dict[str, Any]] = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "get_logs", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": "ok"},
    ]
    provider._client = _client(_ndjson(_done()), capture)
    await _events(provider, provider.prepare_messages(history))
    assert capture["json"]["messages"][1]["thinking"] == "step 1; step 2"


async def test_thinking_field_is_not_yielded_as_text() -> None:
    body = _ndjson(
        {"message": {"role": "assistant", "content": "", "thinking": "let me see"}, "done": False},
        {"message": {"role": "assistant", "content": "answer"}, "done": False},
        _done(),
    )
    capture: dict[str, Any] = {}
    events = await _events(_built(_profile(), capture, body))
    texts = [e["text"] for e in events if e["type"] == "text_delta"]
    assert texts == ["answer"]


async def test_turning_the_transport_on_does_not_turn_thinking_on() -> None:
    """`native_thinking` chooses the *transport*; `think` chooses whether
    the model emits reasoning. The legacy adapter defaulted `think` to
    `False` — reasoning output can dwarf the answer on R1-style models —
    and opting in to the native route must not flip that for an operator
    who never asked for it."""
    capture: dict[str, Any] = {}
    await _events(_built(_profile(), capture))
    assert capture["json"]["think"] is False


async def test_an_explicit_true_asks_the_model_to_think() -> None:
    capture: dict[str, Any] = {}
    await _events(_built(_profile(options={"native_thinking": True, "think": True}), capture))
    assert capture["json"]["think"] is True


async def test_an_explicit_false_keeps_the_model_quiet() -> None:
    capture: dict[str, Any] = {}
    await _events(_built(_profile(options={"native_thinking": True, "think": False}), capture))
    assert capture["json"]["think"] is False


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("false", id="the-string-false"),
        pytest.param("true", id="the-string-true"),
        pytest.param("yes please", id="prose"),
        pytest.param(1, id="one"),
        pytest.param(0, id="zero"),
        pytest.param([], id="wrong-shape"),
    ],
)
async def test_a_non_boolean_think_never_silently_turns_thinking_on(
    value: object, caplog: pytest.LogCaptureFixture
) -> None:
    """A hand-edited `think: "false"` must not mean the opposite of what
    it reads. Only a real boolean decides; anything else falls back to the
    legacy default and names itself in the log, exactly as the other
    unusable option values in this module do."""
    capture: dict[str, Any] = {}
    profile = _profile(options={"native_thinking": True, "think": value})
    with caplog.at_level("WARNING", logger="korvid.providers.flow_ollama_thinking"):
        await _events(_built(profile, capture))
    assert capture["json"]["think"] is False
    assert any("think" in record.getMessage() for record in caplog.records)


async def test_num_ctx_still_reaches_the_native_endpoint() -> None:
    """The compatibility shim silently truncates at the VRAM default; the
    native endpoint takes the number the operator configured."""
    capture: dict[str, Any] = {}
    profile = _profile(options={"native_thinking": True, "num_ctx": 8192})
    await _events(_built(profile, capture))
    assert capture["json"]["options"]["num_ctx"] == 8192


async def test_num_ctx_falls_back_to_the_adapters_own_default() -> None:
    capture: dict[str, Any] = {}
    await _events(_built(_profile(), capture))
    assert capture["json"]["options"] == {"num_ctx": 16384, "temperature": 0.0}


async def test_request_carries_options_think_and_keep_alive() -> None:
    capture: dict[str, Any] = {}
    profile = _profile(
        options={
            "native_thinking": True,
            "num_ctx": 8192,
            "temperature": 0.5,
            "seed": 42,
            "think": True,
            "keep_alive": "10m",
            "num_predict": 192,
        }
    )
    await _events(_built(profile, capture))
    payload = capture["json"]
    assert payload["stream"] is True
    assert payload["think"] is True
    assert payload["keep_alive"] == "10m"
    assert payload["options"] == {
        "num_ctx": 8192,
        "temperature": 0.5,
        "seed": 42,
        "num_predict": 192,
    }


async def test_an_unusable_option_value_falls_back_instead_of_shipping_a_string() -> None:
    """`OllamaOptions` has no validation of its own: a `"8192"` that
    reached it would be sent as a JSON string and would land in
    `context_window_tokens` as text."""
    capture: dict[str, Any] = {}
    profile = _profile(options={"native_thinking": True, "num_ctx": "lots", "temperature": True})
    await _events(_built(profile, capture))
    assert capture["json"]["options"] == {"num_ctx": 16384, "temperature": 0.0}


async def test_posts_to_native_chat_endpoint() -> None:
    capture: dict[str, Any] = {}
    await _events(_built(_profile(), capture))
    assert capture["url"] == "http://x:11434/api/chat"


async def test_a_shim_era_endpoint_still_reaches_the_native_route() -> None:
    capture: dict[str, Any] = {}
    await _events(_built(_profile(endpoint="http://x:11434/v1/"), capture))
    assert capture["url"] == "http://x:11434/api/chat"


async def test_a_colon_tagged_model_reaches_the_native_endpoint_intact() -> None:
    """`ollama/qwen3:8b` — the tag colon must survive both the reference
    split and the native request body."""
    capture: dict[str, Any] = {}
    await _events(_built(_profile("ollama/qwen3:8b"), capture))
    assert capture["json"]["model"] == "qwen3:8b"


def test_descriptor_is_ollama_and_the_model_tag() -> None:
    provider = build_provider(_profile("ollama/qwen3:8b"))
    assert provider is not None
    assert provider.descriptor == ModelDescriptor("ollama", "qwen3:8b")


def test_capabilities_report_context_window_and_multiple_tool_calls() -> None:
    """The adapter reports only what it directly knows: the configured
    `num_ctx` and that its native response can carry multiple tool calls
    (issue #189). It must never infer tier or reasoning from the tag."""
    provider = build_provider(_profile(options={"native_thinking": True, "num_ctx": 16384}))
    assert provider is not None
    capabilities = provider.capabilities
    assert capabilities.context_window_tokens == 16_384
    assert capabilities.supports_parallel_tools is True
    assert capabilities.provenance["context_window_tokens"] is CapabilitySource.PROVIDER
    assert capabilities.supports_tools is None


async def test_non_2xx_raises_provider_error() -> None:
    provider = build_provider(_profile())
    assert isinstance(provider, OllamaProvider)
    provider._client = _client("model not found", status=404)
    with pytest.raises(ProviderStreamError, match="does not have this model"):
        await _events(provider)


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def test_a_reference_with_no_model_is_refused() -> None:
    assert build_provider(_profile("ollama/")) is None


def test_a_profile_with_no_endpoint_uses_the_conventional_local_host() -> None:
    """The same host the shared transport would have used, so opting into
    the native route never changes *where* the request goes."""
    provider = build_provider(_profile(endpoint=None))
    assert isinstance(provider, OllamaProvider)
    assert provider._base_url == "http://localhost:11434"


async def test_a_named_environment_credential_is_sent_as_a_bearer_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_GATEWAY_KEY", "sk-live")
    capture: dict[str, Any] = {}
    profile = _profile(method="environment", settings={"key": "OLLAMA_GATEWAY_KEY"})
    await _events(_built(profile, capture))
    assert capture["headers"]["authorization"] == "Bearer sk-live"


def test_an_environment_credential_that_is_not_set_disables_the_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OLLAMA_GATEWAY_KEY", raising=False)
    profile = _profile(method="environment", settings={"key": "OLLAMA_GATEWAY_KEY"})
    assert build_provider(profile) is None


@pytest.mark.parametrize("method", ["device-login", "not-a-method"])
def test_an_auth_method_this_flow_cannot_serve_is_refused(method: str) -> None:
    assert build_provider(_profile(method=method)) is None


def test_the_operators_trust_bundle_reaches_the_transport(tmp_path: Path) -> None:
    """`network.ca_bundle` is one trust decision for every korvid-owned
    HTTPS client. A flow that owns its transport owns that too."""
    bundle = tmp_path / "corporate-root.pem"
    bundle.write_text("")
    profile = _profile(options={"native_thinking": True, "ca_bundle": str(bundle)})
    provider = build_provider(profile)
    assert isinstance(provider, OllamaProvider)
    assert provider._ca_bundle == str(bundle)


def test_the_legacy_thinking_toggle_still_decides_thinking() -> None:
    """A migrated install carries `think:` from `agent.ollama.think`. It
    stays the answer for whether the model reasons out loud."""
    off = build_provider(_profile(options={"native_thinking": True, "think": False}))
    assert isinstance(off, OllamaProvider)
    assert off._options == OllamaOptions(think=False)


def test_a_migrated_native_install_keeps_the_native_transport(tmp_path: Path) -> None:
    """The migration promises an existing install keeps the wire protocol
    it was already running. That promise is only kept if the option the
    migration writes is the option this flow claims."""
    from korvid.core.config import load_config

    path = tmp_path / "korvid.yaml"
    path.write_text(
        "agent:\n"
        "  provider: ollama\n"
        "  base_url: http://localhost:11434\n"
        "  model: qwen3:8b\n"
        "  ollama:\n"
        "    think: true\n"
        "    num_ctx: 8192\n"
    )
    profile = load_config(path).model_connections.active_profile
    assert profile is not None

    registry = SpecialFlowRegistry([ollama_thinking_flow()])
    assert registry.claim_by_option(profile.model, profile.options) is not None

    provider = build_provider(profile)
    assert isinstance(provider, OllamaProvider)
    assert provider._options.num_ctx == 8192
    assert provider._options.think is True


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        pytest.param("    think: true\n", True, id="on-stays-on"),
        pytest.param("    think: false\n", False, id="off-stays-off"),
        pytest.param("", False, id="unset-stays-off"),
        pytest.param('    think: "false"\n', False, id="unusable-lands-on-the-old-default"),
    ],
)
async def test_a_migrated_install_puts_the_old_think_value_on_the_wire(
    tmp_path: Path, block: str, expected: bool
) -> None:
    """End to end from the legacy file to the request body.

    `agent.ollama.think` was read as `raw.get("think") is True`, so an
    install that never wrote the key — or wrote something that is not a
    boolean — was running with thinking *off*. Migration plus this flow
    has to reproduce that value, not a new one, because the operator did
    not ask for anything to change.
    """
    from korvid.core.config import load_config

    path = tmp_path / "korvid.yaml"
    path.write_text(
        "agent:\n"
        "  provider: ollama\n"
        "  base_url: http://localhost:11434\n"
        "  model: qwen3:8b\n"
        "  ollama:\n"
        "    num_ctx: 8192\n" + block
    )
    profile = load_config(path).model_connections.active_profile
    assert profile is not None

    capture: dict[str, Any] = {}
    await _events(_built(profile, capture))
    assert capture["json"]["think"] is expected


# ---------------------------------------------------------------------------
# The terminal marker and the cumulative bounds (issue #336)
# ---------------------------------------------------------------------------

#: A value shaped like a credential, so a test can prove a refusal did not
#: echo the server text it came from.
_SECRET_ISH = "bearer-leaked-token-value"


async def _drain(provider: OllamaProvider, seen: list[dict[str, Any]]) -> None:
    """Consume a stream into `seen` so `pytest.raises` wraps one call."""
    async for event in provider.complete([{"role": "user", "content": "hi"}], []):
        seen.append(event)


def _on(body: str, *, status: int = 200) -> OllamaProvider:
    """The native transport on a mock wire, with no builder in the way."""
    provider = build_provider(_profile())
    assert isinstance(provider, OllamaProvider)
    provider._client = _client(body, status=status)
    provider._owns_client = True
    return provider


async def test_a_stream_that_never_said_done_is_refused() -> None:
    """`done: true` is this protocol's terminal marker. Without it the
    server stopped mid-answer, whether or not the socket closed tidily."""
    seen: list[dict[str, Any]] = []
    body = _ndjson({"message": {"role": "assistant", "content": "par"}, "done": False})

    with pytest.raises(ProviderStreamTruncatedError, match="ended before") as raised:
        await _drain(_on(body), seen)

    assert raised.value.operator_message() == STREAM_TRUNCATED
    assert [e["type"] for e in seen] == [REQUEST_SENT, "text_delta"]
    assert {"type": "done"} not in seen


async def test_a_truncated_stream_emits_neither_its_calls_nor_its_usage() -> None:
    seen: list[dict[str, Any]] = []
    body = _ndjson(
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_logs", "arguments": {}}}],
            },
            "done": False,
        },
        {"prompt_eval_count": 3, "eval_count": 4, "done": False},
    )

    with pytest.raises(ProviderStreamTruncatedError):
        await _drain(_on(body), seen)

    assert [e["type"] for e in seen] == [REQUEST_SENT]


async def test_reading_stops_at_the_first_done_chunk() -> None:
    """The server may keep writing; the turn is over. Anything after the
    terminal chunk belongs to a response the runtime already closed."""
    body = _ndjson(
        {"message": {"role": "assistant", "content": "hi"}, "done": False},
        _done(prompt_eval_count=3, eval_count=4),
        {"message": {"role": "assistant", "content": " and more"}, "done": False},
        {"prompt_eval_count": 99, "eval_count": 99, "done": True},
    )

    events = await _events(_on(body))

    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["hi"]
    assert [e for e in events if e["type"] == "usage"] == [
        {"type": "usage", "input_tokens": 3, "output_tokens": 4}
    ]
    assert events[-1] == {"type": "done"}


async def test_the_final_chunks_own_content_is_still_delivered() -> None:
    """Stopping *at* the marker is not stopping *before* it: the terminal
    chunk carries the last token on some servers."""
    body = _ndjson(
        {"message": {"role": "assistant", "content": "hi"}, "done": False},
        {"message": {"role": "assistant", "content": "!"}, "done": True},
    )

    events = await _events(_on(body))

    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["hi", "!"]


async def test_a_mid_stream_server_error_is_refused_without_quoting_it() -> None:
    """The server can report a failure with HTTP 200. Its text is the
    server's, so the refusal carries a written sentence instead.

    The type and the sentence are both asserted: a failure the server
    *declared* is not the same situation as a stream that merely stopped,
    and only `STREAM_FAILED` tells the operator to go and read the
    server's own logs. Refusing it as a truncation would send them to
    "retry the request" for a fault a retry cannot fix.
    """
    seen: list[dict[str, Any]] = []
    body = _ndjson({"error": f"unauthorized: {_SECRET_ISH}"})

    with pytest.raises(ProviderProtocolError, match="reported a failure") as raised:
        await _drain(_on(body), seen)

    assert raised.value.operator_message() == STREAM_FAILED
    assert _SECRET_ISH not in str(raised.value)
    # The acknowledgement is all that may have been yielded: no text, no
    # call, no usage and no `done` follows a declared failure.
    assert [e["type"] for e in seen] == [REQUEST_SENT]


async def test_a_mid_stream_error_after_content_still_refuses_the_whole_answer() -> None:
    """Text that really streamed stays; the answer is still not a finished
    one, and the failure is the server's rather than a truncation."""
    seen: list[dict[str, Any]] = []
    body = _ndjson(
        {"message": {"role": "assistant", "content": "par"}, "done": False},
        {"error": f"overloaded: {_SECRET_ISH}"},
        {"message": {"role": "assistant", "content": "tial"}, "done": True},
    )

    with pytest.raises(ProviderProtocolError) as raised:
        await _drain(_on(body), seen)

    assert raised.value.operator_message() == STREAM_FAILED
    assert [e["type"] for e in seen] == [REQUEST_SENT, "text_delta"]
    assert {"type": "done"} not in seen


async def test_an_unreadable_line_is_refused_without_quoting_it() -> None:
    """A line this protocol cannot read is a protocol failure named as
    one: `STREAM_MALFORMED` sends the operator to check that the endpoint
    speaks Ollama's native API, which is the actual fault."""
    seen: list[dict[str, Any]] = []

    with pytest.raises(ProviderProtocolError, match="could not read") as raised:
        await _drain(_on(f"not json {_SECRET_ISH}\n"), seen)

    assert raised.value.operator_message() == STREAM_MALFORMED
    assert _SECRET_ISH not in str(raised.value)
    assert [e["type"] for e in seen] == [REQUEST_SENT]


async def test_a_line_that_is_not_an_object_is_refused_the_same_way() -> None:
    """Valid JSON is not enough: this protocol's frames are objects."""
    seen: list[dict[str, Any]] = []

    with pytest.raises(ProviderProtocolError) as raised:
        await _drain(_on(f'"{_SECRET_ISH}"\n'), seen)

    assert raised.value.operator_message() == STREAM_MALFORMED
    assert _SECRET_ISH not in str(raised.value)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
async def test_a_refused_request_never_quotes_the_servers_answer(status: int) -> None:
    seen: list[dict[str, Any]] = []

    with pytest.raises(ProviderStreamError) as raised:
        await _drain(_on(f"denied: {_SECRET_ISH}", status=status), seen)

    assert _SECRET_ISH not in str(raised.value)
    assert raised.value.operator_message() == str(raised.value)
    assert seen == [{"type": REQUEST_SENT}]


async def test_a_connection_that_fails_mid_stream_is_translated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError(f"reset while reading {_SECRET_ISH}", request=request)

    provider = build_provider(_profile())
    assert isinstance(provider, OllamaProvider)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider._owns_client = True
    seen: list[dict[str, Any]] = []

    with pytest.raises(ProviderStreamError) as raised:
        await _drain(provider, seen)

    assert _SECRET_ISH not in str(raised.value)


def _thinking(total: int, *, piece: int = 4_096) -> str:
    """Reasoning that arrives `piece` characters at a time, then finishes."""
    chunks: list[dict[str, Any]] = []
    written = 0
    while written < total:
        step = min(piece, total - written)
        chunks.append({"message": {"role": "assistant", "thinking": "t" * step}, "done": False})
        written += step
    chunks.append(
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_logs", "arguments": {}}}],
            },
            "done": True,
        }
    )
    return _ndjson(*chunks)


async def test_reasoning_that_stops_exactly_at_the_bound_is_kept() -> None:
    provider = _on(_thinking(MAX_REASONING_CHARS))

    events = await _events(provider)

    call_id = next(e["id"] for e in events if e["type"] == "tool_call")
    assert len(provider._thinking_by_call_id[call_id]) == MAX_REASONING_CHARS


async def test_reasoning_one_character_past_the_bound_is_refused() -> None:
    """Reasoning is held for the *next* request, so it is the one buffer
    the engine's per-response budget never sees."""
    seen: list[dict[str, Any]] = []

    with pytest.raises(ProviderStreamLimitError, match="limit"):
        await _drain(_on(_thinking(MAX_REASONING_CHARS + 1)), seen)

    assert [e["type"] for e in seen] == [REQUEST_SENT]


def _call(name: str = "get_logs", **arguments: Any) -> dict[str, Any]:
    return {"function": {"name": name, "arguments": arguments}}


async def test_arguments_that_stop_exactly_at_the_bound_are_kept() -> None:
    # `{"a": "xxx"}` — the serialized form is what is bounded, so the
    # padding is sized against it rather than against the raw value.
    padding = MAX_TOOL_ARGUMENT_CHARS - len(json.dumps({"a": ""}))
    body = _ndjson(
        {"message": {"role": "assistant", "tool_calls": [_call(a="x" * padding)]}, "done": True}
    )

    events = await _events(_on(body))

    call = next(e for e in events if e["type"] == "tool_call")
    assert len(call["arguments"]) == MAX_TOOL_ARGUMENT_CHARS


async def test_arguments_one_character_past_the_bound_are_refused() -> None:
    padding = MAX_TOOL_ARGUMENT_CHARS - len(json.dumps({"a": ""})) + 1
    body = _ndjson(
        {"message": {"role": "assistant", "tool_calls": [_call(a="x" * padding)]}, "done": True}
    )
    seen: list[dict[str, Any]] = []

    with pytest.raises(ProviderStreamLimitError, match="limit"):
        await _drain(_on(body), seen)

    assert [e["type"] for e in seen] == [REQUEST_SENT]


async def test_exactly_the_permitted_number_of_calls_is_kept() -> None:
    body = _ndjson(
        {
            "message": {
                "role": "assistant",
                "tool_calls": [_call() for _ in range(MAX_TOOL_CALLS_PER_RESPONSE)],
            },
            "done": True,
        }
    )

    events = await _events(_on(body))

    assert len([e for e in events if e["type"] == "tool_call"]) == MAX_TOOL_CALLS_PER_RESPONSE


async def test_one_call_too_many_stops_the_stream_before_it_grows() -> None:
    """The count is cumulative across chunks: the native protocol may
    report calls on several messages of one answer."""
    body = _ndjson(
        *(
            {"message": {"role": "assistant", "tool_calls": [_call()]}, "done": False}
            for _ in range(MAX_TOOL_CALLS_PER_RESPONSE + 1)
        ),
        _done(),
    )
    seen: list[dict[str, Any]] = []

    with pytest.raises(ProviderStreamLimitError, match="limit"):
        await _drain(_on(body), seen)

    assert [e["type"] for e in seen] == [REQUEST_SENT]
