"""Tests for litellm_request: request-plan construction.

RED → GREEN sequence as described in task-13-brief.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

import pytest

from korvid.providers.litellm_request import (
    OMIT_API_KEY,
    RESERVED_CALL_ARGUMENTS,
    RequestPlan,
    ResolvedApiKey,
    build_plan,
)
from korvid.providers.litellm_settings import KEYLESS_API_KEY_SENTINEL

_NO_OPTIONS: Final[Mapping[str, object]] = MappingProxyType({})


def _plan(
    *,
    model: str = "openai/gpt-4o",
    api_key: ResolvedApiKey = "k",
    base_url: str | None = None,
    options: Mapping[str, object] = _NO_OPTIONS,
    supported: Sequence[str] = (),
) -> RequestPlan:
    """Convenience wrapper with safe defaults."""
    return build_plan(
        model=model,
        api_key=api_key,
        base_url=base_url,
        options=options,
        supported=supported,
    )


def test_option_keys_are_filtered_to_what_the_provider_accepts() -> None:
    """An unsupported parameter is a 400 from the vendor. Dropping it
    locally is better than a failed request the operator cannot explain."""
    plan = build_plan(
        model="anthropic/claude-sonnet-4-5",
        api_key="k",
        base_url=None,
        options={"temperature": 0.2, "num_ctx": 8192},
        supported=["temperature", "max_tokens"],
    )
    assert plan.extra == {"temperature": 0.2}


def test_korvid_owned_option_keys_never_reach_the_wire() -> None:
    """`native_thinking` selects a transport; it is not a model
    parameter. Leaking it would be a vendor-side 400."""
    plan = _plan(options={"native_thinking": True}, supported=["native_thinking"])
    assert "native_thinking" not in plan.call_kwargs([], [], stream=True)


def test_the_argument_names_match_litellms_actual_signature() -> None:
    """`base_url` and `api_version` are named parameters of acompletion;
    `api_base` is only reachable through **kwargs. Verified against
    1.98.0 by inspecting the signature."""
    kwargs = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url="https://h/v1",
        options={},
        supported=[],
    ).call_kwargs([{"role": "user", "content": "hi"}], [], stream=True)
    assert kwargs["base_url"] == "https://h/v1"
    assert "api_base" not in kwargs


def test_streaming_requests_ask_for_usage() -> None:
    """LiteLLM passes provider usage through verbatim only when it
    arrives on a choices-free chunk, which requires include_usage."""
    kwargs = _plan().call_kwargs([], [], stream=True)
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}


def test_a_non_streaming_request_omits_stream_options() -> None:
    kwargs = _plan().call_kwargs([], [], stream=False)
    assert kwargs["stream"] is False
    assert "stream_options" not in kwargs


def test_an_empty_tool_list_is_omitted_rather_than_sent_empty() -> None:
    """Several providers reject `tools: []`."""
    assert "tools" not in _plan().call_kwargs([], [], stream=True)


def test_the_key_is_passed_explicitly_so_no_ambient_key_can_be_used() -> None:
    """Passing api_key=None would let the SDK fall back to
    OPENAI_API_KEY. A profile that asked for no credential must send
    none, not whichever key happens to be exported."""
    kwargs = build_plan(
        model="openai/gpt-4o",
        api_key=None,
        base_url="http://localhost:8000/v1",
        options={},
        supported=[],
    ).call_kwargs([], [], stream=True)
    assert kwargs["api_key"] == KEYLESS_API_KEY_SENTINEL


def test_provider_default_auth_passes_no_api_key_argument_at_all() -> None:
    """`provider-default` means "use the vendor SDK's own credential
    chain". An explicit argument - `None` or a sentinel - stops that chain
    being consulted, so the only correct behaviour is absence.

    The assertion is unconditional on purpose: `"api_key" not in kwargs or
    plan.api_key is None` would pass for *any* implementation that leaves
    the key out **or** sets it to None, which is exactly the bug.
    """
    plan = build_plan(
        model="bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
        api_key=OMIT_API_KEY,
        base_url=None,
        options={},
        supported=[],
    )
    kwargs = plan.call_kwargs([], [], stream=True)
    assert "api_key" not in kwargs
    assert plan.api_key is OMIT_API_KEY


def test_the_omit_sentinel_is_distinguishable_from_no_credential() -> None:
    """Collapsing the two states is the defect this sentinel prevents."""
    keyless = build_plan(
        model="openai/gpt-4o",
        api_key=None,
        base_url="http://localhost:8000/v1",
        options={},
        supported=[],
    )
    delegated = build_plan(
        model="openai/gpt-4o",
        api_key=OMIT_API_KEY,
        base_url=None,
        options={},
        supported=[],
    )
    assert keyless.api_key is not delegated.api_key
    assert "api_key" in keyless.call_kwargs([], [], stream=True)
    assert "api_key" not in delegated.call_kwargs([], [], stream=True)


def test_the_plan_is_frozen_so_a_snapshot_cannot_drift_from_the_wire() -> None:
    with pytest.raises(AttributeError, match="cannot assign"):
        build_plan(
            model="openai/gpt-4o",
            api_key="k",
            base_url=None,
            options={},
            supported=[],
        ).model = "other"  # type: ignore[misc]


def test_options_are_deep_copied_out_of_the_frozen_profile_mapping() -> None:
    """Profile options are MappingProxy-wrapped; litellm may mutate what
    it is handed."""
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url=None,
        options={"extra_headers": {"x": "1"}},
        supported=["extra_headers"],
    )
    kwargs = plan.call_kwargs([], [], stream=True)
    kwargs["extra_headers"]["x"] = "2"
    assert plan.extra["extra_headers"] == {"x": "1"}


def test_tools_are_included_when_non_empty() -> None:
    """Non-empty tool list is forwarded to the kwargs."""
    tool = {"type": "function", "function": {"name": "foo"}}
    kwargs = _plan().call_kwargs([], [tool], stream=True)
    assert kwargs["tools"] == [tool]


def test_api_version_lifted_from_options_into_named_parameter() -> None:
    """api_version is a named acompletion param, not an extra."""
    plan = build_plan(
        model="azure/gpt-4o",
        api_key="k",
        base_url="https://my.openai.azure.com",
        options={"api_version": "2024-02-01"},
        supported=["api_version"],
    )
    assert plan.api_version == "2024-02-01"
    assert "api_version" not in plan.extra
    kwargs = plan.call_kwargs([], [], stream=True)
    assert kwargs["api_version"] == "2024-02-01"


def test_timeout_lifted_from_options_into_named_parameter() -> None:
    """`timeout` is a named acompletion param, and never an OpenAI param.

    Measured on litellm 1.98.0: `get_supported_openai_params` lists it for
    no provider, so leaving it among the extras means the allowlist filter
    drops it and a slow local model silently runs at the SDK default.
    """
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url="http://localhost:1234/v1",
        options={"timeout": 900},
        supported=["temperature"],
    )
    assert plan.timeout == 900.0
    assert "timeout" not in plan.extra
    kwargs = plan.call_kwargs([], [], stream=True)
    assert kwargs["timeout"] == 900.0


@pytest.mark.parametrize("value", [0, -1, "900", True, float("nan"), float("inf")])
def test_a_timeout_that_is_not_a_positive_number_is_dropped(value: object) -> None:
    """An unusable timeout is not sent — the SDK default is the honest answer."""
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url="http://localhost:1234/v1",
        options={"timeout": value},
        supported=[],
    )
    assert plan.timeout is None
    assert "timeout" not in plan.extra
    assert "timeout" not in plan.call_kwargs([], [], stream=True)


def test_empty_supported_keeps_all_options() -> None:
    """Empty `supported` means lookup failed — keep everything rather than
    silently drop operator settings."""
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url=None,
        options={"temperature": 0.5, "max_tokens": 1024},
        supported=[],
    )
    assert plan.extra == {"temperature": 0.5, "max_tokens": 1024}


def test_a_profile_option_cannot_turn_verification_off() -> None:
    """`ssl_verify: false` in a profile's options is a request to stop
    verifying certificates.

    Two things go wrong if it is treated as a model parameter. LiteLLM's
    own httpx handlers read it and would honour it, and — measured on
    1.98.0 — anything left in the call kwargs that the provider does not
    consume is forwarded into the *request body*, so the value reaches
    the vendor as an unknown field. Trust is korvid's transport
    decision, so the key is owned and dropped even where the provider
    reports it as supported.
    """
    plan = _plan(options={"ssl_verify": False}, supported=["ssl_verify"])
    assert "ssl_verify" not in plan.call_kwargs([], [], stream=True)
    assert plan.extra == {}


def test_a_declared_credential_is_snapshotted_onto_the_plan() -> None:
    """The plan owns its credential parameters.

    They are not deep-copied — they carry a live callable the transport
    invokes per request, and a copy of a callable is the wrong object —
    but the mapping is snapshotted, so the declaration that supplied it
    cannot rewrite the parameters of a plan already in use.
    """
    supplied: dict[str, object] = {"token_provider": "callable"}
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="sk-test",
        base_url=None,
        options={},
        supported=(),
        credential=supplied,
    )
    supplied["token_provider"] = "replaced"

    assert plan.call_kwargs([], [], stream=True)["token_provider"] == "callable"
    with pytest.raises(TypeError, match="does not support item assignment"):
        plan.credential["token_provider"] = "replaced"  # type: ignore[index]  # frozen by design


# ---------------------------------------------------------------------------
# The frozen-config boundary: `load_config` hands out MappingProxyType and
# tuples, and `acompletion` has to be handed plain mutable JSON structures.
# ---------------------------------------------------------------------------


def test_a_frozen_nested_option_mapping_survives_into_the_call() -> None:
    """`load_config` freezes every nested mapping into a `MappingProxyType`.

    `copy.deepcopy` cannot copy one — it falls through to `__reduce_ex__`
    and raises `TypeError: cannot pickle 'mappingproxy' object` — so a
    profile with any nested option mapping used to take down the whole
    request rather than send it.
    """
    options = MappingProxyType(
        {"extra_headers": MappingProxyType({"x-team": "platform", "x-env": "prod"})}
    )
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url=None,
        options=options,
        supported=["extra_headers"],
    )
    kwargs = plan.call_kwargs([], [], stream=True)
    assert kwargs["extra_headers"] == {"x-team": "platform", "x-env": "prod"}
    assert type(kwargs["extra_headers"]) is dict


def test_a_frozen_list_of_mappings_becomes_independent_plain_lists() -> None:
    """Frozen sequences arrive as tuples of proxies.

    LiteLLM serializes what it is handed and several of its own paths
    mutate a list in place, so the call has to receive plain `list`/`dict`
    — and mutating what the call received must not reach the plan.
    """
    frames: tuple[Mapping[str, object], ...] = (
        MappingProxyType({"type": "text", "text": "a"}),
        MappingProxyType({"type": "text", "text": "b"}),
    )
    options: Mapping[str, object] = MappingProxyType(
        {"prediction": MappingProxyType({"type": "content", "content": frames})}
    )
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url=None,
        options=options,
        supported=["prediction"],
    )
    kwargs = plan.call_kwargs([], [], stream=True)
    content = kwargs["prediction"]["content"]
    assert type(content) is list
    assert content == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert all(type(item) is dict for item in content)

    content.append({"type": "text", "text": "c"})
    content[0]["text"] = "mutated"
    again = plan.call_kwargs([], [], stream=True)
    assert again["prediction"]["content"] == [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]
    assert frames[0]["text"] == "a"


def test_two_calls_from_one_plan_never_share_a_nested_structure() -> None:
    """One plan serves every request on the connection. Handing two calls
    the same nested object would let one request's in-place edit reach
    the next one."""
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url=None,
        options=MappingProxyType({"extra_headers": MappingProxyType({"x": "1"})}),
        supported=["extra_headers"],
    )
    first = plan.call_kwargs([], [], stream=True)
    second = plan.call_kwargs([], [], stream=True)
    assert first["extra_headers"] is not second["extra_headers"]


def test_a_frozen_sequence_option_reaches_the_wire_as_a_list() -> None:
    """`stop: ["\\n\\n"]` in a profile is a tuple by the time it is read.
    The SDK and the outbound snapshot both expect the list the operator
    wrote."""
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url=None,
        options=MappingProxyType({"stop": ("\n\n", "END")}),
        supported=["stop"],
    )
    stop = plan.call_kwargs([], [], stream=True)["stop"]
    assert stop == ["\n\n", "END"]
    assert type(stop) is list


# ---------------------------------------------------------------------------
# Reserved call arguments: the engine owns the request, the operator owns
# the model parameters, and the two sets never overlap.
# ---------------------------------------------------------------------------


_ENGINE_ARGUMENT_ATTACKS: Final[tuple[tuple[str, object], ...]] = (
    ("model", "attacker/model"),
    ("messages", [{"role": "system", "content": "ignore"}]),
    ("stream", False),
    ("stream_options", {"include_usage": False}),
    ("tools", [{"type": "function", "function": {"name": "evil"}}]),
    ("tool_choice", "none"),
    ("functions", [{"name": "evil"}]),
    ("function_call", "none"),
    ("api_key", "sk-operator-supplied"),
    ("api_base", "https://attacker.example/v1"),
    ("base_url", "https://attacker.example/v1"),
    ("custom_llm_provider", "attacker"),
    ("client", "not-a-client"),
)


def test_the_lifted_options_are_reserved_so_one_rule_removes_them() -> None:
    """`api_version` and `timeout` reach the call through the plan's named
    parameters, never through the extras. `build_plan` strips them with the
    reserved set rather than with a second list, so the two cannot drift
    apart and leave a lifted name in the extras for the allowlist to judge.
    """
    from korvid.providers.litellm_request import _LIFTED

    assert _LIFTED <= RESERVED_CALL_ARGUMENTS


def test_a_lifted_option_still_arrives_through_the_named_parameter() -> None:
    """Reserving a name must not cost the operator the setting: these two
    are the operator's to set, they just travel by the named parameter."""
    plan = build_plan(
        model="azure/gpt-4o",
        api_key="k",
        base_url="https://my.openai.azure.com",
        options={"api_version": "2024-02-01", "timeout": 90},
        supported=["temperature"],
    )
    assert plan.extra == {}
    kwargs = plan.call_kwargs([], [], stream=True)
    assert kwargs["api_version"] == "2024-02-01"
    assert kwargs["timeout"] == 90.0


@pytest.mark.parametrize(("key", "value"), _ENGINE_ARGUMENT_ATTACKS)
def test_a_profile_option_can_never_occupy_an_engine_owned_argument(
    key: str, value: object
) -> None:
    """The extras used to be merged *last*, so any of these silently
    replaced the engine's own value — re-routing the request, muting the
    tools, or turning streaming off under a streaming reader.

    Run with the capability lookup reporting every one of these names as
    supported, which is the worst case and, for the five OpenAI ones,
    the real one.
    """
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url="https://gateway.example/v1",
        options={key: value},
        supported=[key],
    )
    assert key not in plan.extra
    kwargs = plan.call_kwargs(
        [{"role": "user", "content": "hi"}],
        [{"type": "function", "function": {"name": "get_pods"}}],
        stream=True,
    )
    assert kwargs.get(key) != value


@pytest.mark.parametrize(("key", "value"), _ENGINE_ARGUMENT_ATTACKS)
def test_an_engine_owned_argument_is_owned_when_the_lookup_failed_too(
    key: str, value: object
) -> None:
    """An empty `supported` means the capability lookup failed and korvid
    forwards everything rather than dropping operator settings. That
    fallback must not become the way around the reserved policy."""
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url="https://gateway.example/v1",
        options={key: value},
        supported=(),
    )
    assert key not in plan.extra
    kwargs = plan.call_kwargs(
        [{"role": "user", "content": "hi"}],
        [{"type": "function", "function": {"name": "get_pods"}}],
        stream=True,
    )
    assert kwargs.get(key) != value


def test_the_engine_wins_even_on_a_plan_assembled_without_build_plan() -> None:
    """`build_plan` strips the reserved keys, but `RequestPlan` is a public
    dataclass a caller can build directly. `call_kwargs` therefore enforces
    the same policy rather than trusting its own constructor."""
    plan = RequestPlan(
        model="openai/gpt-4o",
        api_key="k",
        base_url="https://gateway.example/v1",
        api_version=None,
        extra=MappingProxyType(
            {
                "model": "attacker/model",
                "api_key": "sk-operator-supplied",
                "base_url": "https://attacker.example/v1",
                "tool_choice": "none",
                "stream": False,
                "temperature": 0.3,
            }
        ),
    )
    kwargs = plan.call_kwargs([{"role": "user", "content": "hi"}], [], stream=True)
    assert kwargs["model"] == "openai/gpt-4o"
    assert kwargs["api_key"] == "k"
    assert kwargs["base_url"] == "https://gateway.example/v1"
    assert kwargs["stream"] is True
    assert "tool_choice" not in kwargs
    assert kwargs["temperature"] == 0.3


def test_a_profile_cannot_mute_the_agents_tools() -> None:
    """`tool_choice` is listed as a supported parameter by essentially
    every provider (measured on litellm 1.98.0), so the allowlist filter
    does not stop it. korvid drives the tool loop, and a profile that set
    `tool_choice: none` would leave the agent unable to read the cluster
    while still reporting the tools as available."""
    plan = build_plan(
        model="anthropic/claude-sonnet-4-5",
        api_key="k",
        base_url=None,
        options={"tool_choice": "none"},
        supported=["tool_choice", "tools", "temperature"],
    )
    tool = {"type": "function", "function": {"name": "get_pods"}}
    kwargs = plan.call_kwargs([], [tool], stream=True)
    assert "tool_choice" not in kwargs
    assert kwargs["tools"] == [tool]


def test_a_profile_cannot_forge_tools_onto_a_toolless_request() -> None:
    """Several providers reject `tools: []`, which is why an empty list is
    omitted. An option must not put one back."""
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url=None,
        options={"tools": [{"type": "function", "function": {"name": "evil"}}]},
        supported=["tools"],
    )
    assert "tools" not in plan.call_kwargs([], [], stream=True)


def test_a_profile_cannot_ask_a_non_streaming_call_for_usage_frames() -> None:
    """`stream_options` is only meaningful with `stream=True`; sending it
    on a blocking call is a vendor-side 400."""
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url=None,
        options={"stream_options": {"include_usage": True}},
        supported=["stream_options"],
    )
    assert "stream_options" not in plan.call_kwargs([], [], stream=False)


# ---------------------------------------------------------------------------
# Credentials: nothing an operator writes in a profile may become one.
# ---------------------------------------------------------------------------


_CREDENTIAL_SHAPED_KEYS: Final[tuple[str, ...]] = (
    "api_key",
    "apiKey",
    "APIKey",
    "apikey",
    "azure_ad_token",
    "azure_ad_token_provider",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "vertex_credentials",
    "client_secret",
    "password",
    "authorization",
    "bearer_token",
)


@pytest.mark.parametrize("key", _CREDENTIAL_SHAPED_KEYS)
def test_a_credential_shaped_option_never_reaches_the_wire(key: str) -> None:
    """An option korvid does not consume is forwarded into the *request
    body* (measured on 1.98.0, and the reason `ssl_verify` is owned), so a
    credential-shaped key is not merely an override risk: it would send
    whatever it holds to the vendor as an unknown field.

    Named after the same key segments `core/config.py` already refuses in
    a profile's options, so the two boundaries cannot disagree about what
    a credential looks like.
    """
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url="https://gateway.example/v1",
        options={key: "leaked-value"},
        supported=(),
    )
    assert key not in plan.extra
    assert "leaked-value" not in plan.call_kwargs([], [], stream=True).values()


@pytest.mark.parametrize("supported", [(), ("token_provider", "temperature")])
def test_an_option_cannot_displace_a_declared_credential_parameter(
    supported: Sequence[str],
) -> None:
    """The declared chain carries a *refreshing* callable. An option that
    occupied its parameter would swap a live credential for a string from
    a YAML file — on the lookup-failed path as well as the ordinary one."""

    def token_provider() -> str:
        return "live"

    plan = build_plan(
        model="openai/gpt-4o",
        api_key=OMIT_API_KEY,
        base_url=None,
        options={"token_provider": "from-the-profile", "temperature": 0.4},
        supported=supported,
        credential={"token_provider": token_provider},
    )
    kwargs = plan.call_kwargs([], [], stream=True)
    assert kwargs["token_provider"] is token_provider
    assert kwargs["temperature"] == 0.4
    assert "api_key" not in kwargs


def test_provider_default_stays_delegated_when_a_profile_names_a_key() -> None:
    """`provider-default` works by the argument being *absent*. An option
    that put `api_key` back would stop the vendor SDK consulting its own
    chain, which is the entire point of the method."""
    plan = build_plan(
        model="openai/gpt-4o",
        api_key=OMIT_API_KEY,
        base_url=None,
        options={"api_key": "sk-from-the-profile"},
        supported=(),
    )
    assert "api_key" not in plan.call_kwargs([], [], stream=True)


# ---------------------------------------------------------------------------
# ...and the ordinary parameters still work, which is what the policy is
# there to protect.
# ---------------------------------------------------------------------------


def test_ordinary_model_parameters_still_reach_the_wire() -> None:
    """The reserved policy is a narrow one. Everything an operator
    legitimately tunes has to survive it untouched."""
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url="https://gateway.example/v1",
        options=MappingProxyType(
            {
                "temperature": 0.2,
                "max_tokens": 4096,
                "max_completion_tokens": 2048,
                "seed": 7,
                "timeout": 120,
                "top_p": 0.9,
                "stop": ("\n\n",),
                "prompt_cache_key": "korvid",
                "extra_headers": MappingProxyType({"x-team": "platform"}),
            }
        ),
        supported=[
            "temperature",
            "max_tokens",
            "max_completion_tokens",
            "seed",
            "top_p",
            "stop",
            "prompt_cache_key",
            "extra_headers",
        ],
    )
    kwargs = plan.call_kwargs([{"role": "user", "content": "hi"}], [], stream=True)
    assert kwargs["temperature"] == 0.2
    assert kwargs["max_tokens"] == 4096
    assert kwargs["max_completion_tokens"] == 2048
    assert kwargs["seed"] == 7
    assert kwargs["timeout"] == 120.0
    assert kwargs["top_p"] == 0.9
    assert kwargs["stop"] == ["\n\n"]
    assert kwargs["prompt_cache_key"] == "korvid"
    assert kwargs["extra_headers"] == {"x-team": "platform"}
