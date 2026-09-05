"""Tests for litellm_request: request-plan construction.

RED → GREEN sequence as described in task-13-brief.md.
"""

from __future__ import annotations

import pytest

from korvid.providers.litellm_request import OMIT_API_KEY, build_plan
from korvid.providers.litellm_settings import KEYLESS_API_KEY_SENTINEL


def _plan(**kwargs):  # type: ignore[no-untyped-def]
    """Convenience wrapper with safe defaults."""
    defaults: dict[str, object] = {
        "model": "openai/gpt-4o",
        "api_key": "k",
        "base_url": None,
        "options": {},
        "supported": [],
    }
    defaults.update(kwargs)
    return build_plan(**defaults)


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
