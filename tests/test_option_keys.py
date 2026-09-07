"""The one vocabulary both option boundaries judge a key name by.

Two gates have to agree on what a credential-shaped key looks like:
`core/config.py` refuses one in a profile's `options`, and
`providers/litellm_request.py` drops one on the way to `acompletion`. They
carried a copy of the vocabulary each, and the copies drifted — the plural
spellings `api_keys`, `secrets` and `passwords` were in neither, so they
passed the config gate *and* the request gate and reached the vendor as an
unknown request-body field.

These tests are written against the shared module and then again against
both gates, because a shared module nobody calls would fix nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from korvid.core.config import load_config
from korvid.option_keys import (
    key_segments,
    matched_credential_segment,
    names_a_credential,
    normalized_segments,
)
from korvid.providers.litellm_request import build_plan

# ---------------------------------------------------------------------------
# The vocabulary itself
# ---------------------------------------------------------------------------

#: Key names that carry a credential. Every plural here is a spelling that
#: passed both gates before this module existed.
CREDENTIAL_KEYS: Final[tuple[str, ...]] = (
    # singular, as both gates already refused
    "api_key",
    "apikey",
    "access_key",
    "secret",
    "password",
    "credential",
    "authorization",
    "token",
    "access_token",
    "azure_ad_token",
    "client_secret",
    "aws_secret_access_key",
    "vertex_credentials",
    # plural — the divergence this vocabulary exists to close
    "api_keys",
    "apikeys",
    "access_keys",
    "secrets",
    "passwords",
    "credentials",
    "tokens",
    "access_tokens",
    "azure_ad_tokens",
    "client_secrets",
    # case and separator spellings of the same names
    "apiKey",
    "apiKeys",
    "APIKey",
    "API_KEYS",
    "api-key",
    "api-keys",
    "api.key",
    "clientSecret",
    "clientSecrets",
    "accessToken",
    "accessTokens",
    "Authorization",
    "PASSWORD",
    "my_apikey",
    "client_api_key",
    "my_api_key_rotation",
)

#: Names that must survive both gates. Every one is either a real model
#: parameter (`get_supported_openai_params` reports the first four for
#: korvid's providers on litellm 1.98.0) or a name that merely *contains*
#: a credential word by accident.
BENIGN_KEYS: Final[tuple[str, ...]] = (
    "max_tokens",
    "max_completion_tokens",
    "prompt_cache_key",
    "temperature",
    "token_count",
    "context_window_tokens",
    "input_tokens",
    "output_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "tokens_per_minute",
    "num_tokens",
    "token_limit",
    "monkey",
    "monkeys",
    "client_key",
    "clientKey",
    "keychain",
    "api_version",
    "apiVersion",
    "modelName",
    "baseURL",
    "maxRetries",
    "seed",
)


@pytest.mark.parametrize("key", CREDENTIAL_KEYS)
def test_a_credential_name_is_recognized_in_every_spelling(key: str) -> None:
    """Singular or plural, camelCase or hyphenated, it is the same name."""
    assert names_a_credential(key) is True
    assert matched_credential_segment(key) is not None


@pytest.mark.parametrize("key", BENIGN_KEYS)
def test_a_model_parameter_is_never_mistaken_for_a_credential(key: str) -> None:
    """`token` is also the LLM unit of text. Every parameter that counts
    one has to keep working, or the rule costs the operator the settings
    it exists to protect."""
    assert names_a_credential(key) is False
    assert matched_credential_segment(key) is None


def test_the_named_segment_is_the_one_an_operator_can_act_on() -> None:
    """`core/config.py` puts this string in the refusal, so it has to name
    the offending word rather than the whole key."""
    assert matched_credential_segment("db_password") == "password"
    assert matched_credential_segment("access_token") == "token"
    assert matched_credential_segment("client_api_key") == "api_key"
    assert matched_credential_segment("my_apikey") == "apikey"
    assert matched_credential_segment("API_KEYS") == "api_key"


def test_keys_are_split_the_same_way_on_both_sides() -> None:
    """One tokenizer, so a spelling can never mean two things."""
    assert key_segments("apiKey") == ("api", "key")
    assert key_segments("APIKey") == ("api", "key")
    assert key_segments("api-key") == ("api", "key")
    assert key_segments("MAX_TOKENS") == ("max", "tokens")


def test_the_matching_form_folds_plurals_and_leaves_short_words_alone() -> None:
    """The form a vocabulary is looked up in.

    `providers/litellm_request.py` matches LiteLLM's control words by it,
    so `fallbacks` has to reduce to `fallback` — and `pass`, `class` and
    anything under four letters must survive untouched, or the folding
    would invent words the operator never wrote.
    """
    assert normalized_segments("fallbacks") == ("fallback",)
    assert normalized_segments("success_callbacks") == ("success", "callback")
    assert normalized_segments("mockResponse") == ("mock", "response")
    assert normalized_segments("gas") == ("gas",)
    assert normalized_segments("class_pass") == ("class", "pass")


# ---------------------------------------------------------------------------
# ...and both gates judge by it
# ---------------------------------------------------------------------------


def _profile_options(tmp_path: Path, key: str) -> tuple[dict[str, object], str | None]:
    """`agent.profiles.main.options` as `load_config` accepts it."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "agent:\n  active: main\n  profiles:\n    main:\n"
        f"      model: openai/gpt-4o\n      options:\n        {key}: leaked-value\n",
        encoding="utf-8",
    )
    profile = load_config(path).model_connections.profiles["main"]
    return dict(profile.options), profile.config_error


@pytest.mark.parametrize("key", CREDENTIAL_KEYS)
def test_the_config_gate_refuses_every_credential_spelling(tmp_path: Path, key: str) -> None:
    """config.yaml is not a secret store, in any spelling."""
    options, error = _profile_options(tmp_path, key)
    assert options == {}
    assert error is not None


@pytest.mark.parametrize("key", CREDENTIAL_KEYS)
def test_the_request_gate_drops_every_credential_spelling(key: str) -> None:
    """The second gate, for a plan assembled without the first one — and
    on the lookup-failed path, where every unfiltered option is forwarded
    into the request body."""
    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url="https://gateway.example/v1",
        options={key: "leaked-value"},
        supported=(),
    )
    assert key not in plan.extra
    assert "leaked-value" not in plan.call_kwargs([], [], stream=True).values()


@pytest.mark.parametrize("key", BENIGN_KEYS)
def test_both_gates_keep_the_parameters_an_operator_really_sets(tmp_path: Path, key: str) -> None:
    """The rule is narrow on purpose: agreement is worthless if the two
    gates agree on refusing a real parameter.

    `api_version` is checked through the plan's named parameter rather
    than the extras — it is the operator's to set, it just does not
    travel among the model parameters.
    """
    options, error = _profile_options(tmp_path, key)
    assert error is None
    assert key in options

    plan = build_plan(
        model="openai/gpt-4o",
        api_key="k",
        base_url=None,
        options={key: "value"},
        supported=(key,),
    )
    if key == "api_version":
        assert plan.api_version == "value"
    else:
        assert plan.extra == {key: "value"}


def test_a_nested_credential_key_is_refused_where_the_nesting_is_parsed(
    tmp_path: Path,
) -> None:
    """The config gate walks the whole mapping, so a credential one level
    down is refused with the same vocabulary as a top-level one."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "agent:\n  active: main\n  profiles:\n    main:\n"
        "      model: openai/gpt-4o\n      options:\n"
        "        extra_headers:\n          api_keys: leaked-value\n",
        encoding="utf-8",
    )
    profile = load_config(path).model_connections.profiles["main"]
    assert profile.config_error is not None
    assert "api_key" in profile.config_error
    assert dict(profile.options) == {}
