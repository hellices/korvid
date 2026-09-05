"""The eval harness and the TUI must build the *same* provider (Task 18 Step 1).

An eval result is only evidence about the shipped product if the run went
through the shipped construction path. Every test here pins one dimension
of that equivalence: the class, the descriptor, the capabilities, the
request plan, and the trust decision. None of them talk to a network —
`create_provider_from_profile` resolves references, parameters and
capabilities from tables shipped inside the `litellm` wheel.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from korvid.__main__ import _create_provider_from_active_profile
from korvid.agent.model_policy import CapabilitySource
from korvid.agent.model_profiles import ConnectionAuthConfig, ModelConnectionConfig
from korvid.evals.__main__ import eval_profile_from_env, provider_factory_from_env
from korvid.providers.litellm_provider import LiteLLMProvider

#: A reference the shipped LiteLLM tables both route and describe, so the
#: descriptor and the capability assertions have something to be equal to.
CATALOG_REFERENCE = "openai/gpt-4o"

EVAL_ENDPOINT = "http://localhost:1234/v1"


def _eval_env(**overrides: str) -> dict[str, str]:
    env = {
        "KORVID_EVAL_BASE_URL": EVAL_ENDPOINT,
        "KORVID_EVAL_MODEL": CATALOG_REFERENCE,
    }
    env.update(overrides)
    return env


def _build(env: Mapping[str, str]) -> Any:
    return provider_factory_from_env(env)()


def test_eval_provider_is_the_shipped_transport() -> None:
    """The eval must not run against a transport the product does not ship."""
    provider = _build(_eval_env())

    assert isinstance(provider, LiteLLMProvider)


def test_eval_provider_is_fresh_per_run() -> None:
    """One factory, a new provider per repetition — no shared stream state."""
    factory = provider_factory_from_env(_eval_env())

    first = factory()
    second = factory()

    assert first is not second


def test_eval_descriptor_matches_the_app_for_an_equivalent_profile() -> None:
    """The router keys on `descriptor`; a fake provider id routes elsewhere."""
    app_provider = _create_provider_from_active_profile(
        ModelConnectionConfig(model=CATALOG_REFERENCE, endpoint=EVAL_ENDPOINT),
        None,
        None,
    )
    eval_provider = _build(_eval_env())

    assert app_provider is not None
    assert eval_provider.descriptor == app_provider.descriptor
    assert eval_provider.descriptor.provider == "openai"
    assert eval_provider.descriptor.model == "gpt-4o"


def test_eval_capabilities_match_the_app_for_an_equivalent_profile() -> None:
    """Tier routing reads capabilities; `unknown()` silently changes the arm."""
    app_provider = _create_provider_from_active_profile(
        ModelConnectionConfig(model=CATALOG_REFERENCE, endpoint=EVAL_ENDPOINT),
        None,
        None,
    )
    eval_provider = _build(_eval_env())

    assert app_provider is not None
    assert eval_provider.capabilities == app_provider.capabilities
    assert eval_provider.capabilities.supports_tools is True
    assert eval_provider.capabilities.provenance["supports_tools"] is CapabilitySource.CATALOG


def test_eval_plan_matches_the_app_for_an_equivalent_profile() -> None:
    """Same profile, same wire payload: endpoint, options and credential."""
    options = {"temperature": 0.1}
    app_provider = _create_provider_from_active_profile(
        ModelConnectionConfig(
            model=CATALOG_REFERENCE,
            endpoint=EVAL_ENDPOINT,
            auth=ConnectionAuthConfig(method="none"),
            options={**options, "timeout": 900.0},
        ),
        None,
        None,
    )
    eval_provider = _build(
        _eval_env(
            KORVID_EVAL_OPTIONS_JSON='{"temperature": 0.1}',
            KORVID_EVAL_TIMEOUT_SECONDS="900",
        )
    )

    assert app_provider is not None
    assert eval_provider._plan == app_provider._plan


def test_eval_options_json_reaches_the_request_plan() -> None:
    """A profile option the app would send must be sent by the eval too."""
    provider = _build(_eval_env(KORVID_EVAL_OPTIONS_JSON='{"temperature": 0.25}'))

    assert provider._plan.extra["temperature"] == 0.25


def test_eval_options_json_drops_parameters_the_provider_rejects() -> None:
    """Filtering is the factory's, not the eval's: `num_ctx` is not an openai param."""
    provider = _build(_eval_env(KORVID_EVAL_OPTIONS_JSON='{"num_ctx": 32768}'))

    assert "num_ctx" not in provider._plan.extra
    assert provider.capabilities.context_window_tokens == 32768
    assert provider.capabilities.provenance["context_window_tokens"] is CapabilitySource.USER


@pytest.mark.parametrize("raw", ["not json", "[]", '"text"'])
def test_eval_options_json_refuses_anything_but_an_object(raw: str) -> None:
    with pytest.raises(SystemExit, match="KORVID_EVAL_OPTIONS_JSON"):
        provider_factory_from_env(_eval_env(KORVID_EVAL_OPTIONS_JSON=raw))


def test_eval_timeout_reaches_the_litellm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """`timeout` is a named `acompletion` parameter, not an OpenAI param.

    Measured on litellm 1.98.0, `get_supported_openai_params` never lists
    it, so a timeout carried as an ordinary option is filtered off the
    call and the eval silently runs at the SDK default.
    """
    provider = _build(_eval_env(KORVID_EVAL_TIMEOUT_SECONDS="900"))

    assert provider._plan.timeout == 900.0
    kwargs = provider._plan.call_kwargs([], [], stream=False)
    assert kwargs["timeout"] == 900.0


def test_eval_ca_bundle_applies_the_operators_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`KORVID_EVAL_CA_BUNDLE` is the eval's `network.ca_bundle`."""
    from tests.providers.tls_ca import mint_ca_and_server_cert

    bundle, _cert, _key = mint_ca_and_server_cert(tmp_path)
    applied: list[str] = []
    monkeypatch.setattr(
        "korvid.providers.litellm_factory.litellm_runtime.apply_ca_bundle",
        lambda path: applied.append(path),
    )

    _build(_eval_env(KORVID_EVAL_CA_BUNDLE=str(bundle)))

    assert applied == [str(bundle)]


def test_eval_refuses_an_unloadable_ca_bundle(tmp_path: Path) -> None:
    """A bundle that will not load is refused, exactly as `network.ca_bundle` is."""
    missing = tmp_path / "absent.pem"

    with pytest.raises(SystemExit, match="could not be loaded"):
        provider_factory_from_env(_eval_env(KORVID_EVAL_CA_BUNDLE=str(missing)))


def test_eval_reads_the_credential_from_the_named_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`KORVID_EVAL_API_KEY_ENV` names a variable, so no secret is stored."""
    monkeypatch.setenv("EVAL_TOKEN", "sk-from-named-variable")

    provider = _build(_eval_env(KORVID_EVAL_API_KEY_ENV="EVAL_TOKEN"))

    assert provider._plan.api_key == "sk-from-named-variable"


def test_the_eval_profile_stores_the_variable_name_and_not_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVAL_TOKEN", "sk-from-named-variable")

    profile = eval_profile_from_env(_eval_env(KORVID_EVAL_API_KEY_ENV="EVAL_TOKEN"))

    assert profile.auth.method == "environment"
    assert profile.auth.settings["key"] == "EVAL_TOKEN"
    assert "sk-from-named-variable" not in repr(profile)


def test_eval_refuses_when_the_named_credential_variable_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EVAL_TOKEN", raising=False)

    with pytest.raises(SystemExit, match="EVAL_TOKEN"):
        provider_factory_from_env(_eval_env(KORVID_EVAL_API_KEY_ENV="EVAL_TOKEN"))


def test_legacy_api_key_is_read_by_name_and_never_stored(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The deprecated variable still works, and the profile holds its *name*."""
    monkeypatch.setenv("KORVID_EVAL_API_KEY", "sk-legacy")
    env = _eval_env(KORVID_EVAL_API_KEY="sk-legacy")

    profile = eval_profile_from_env(env)
    provider = _build(env)

    assert provider._plan.api_key == "sk-legacy"
    assert profile.auth.settings["key"] == "KORVID_EVAL_API_KEY"
    assert "sk-legacy" not in repr(profile)
    assert "deprecated" in capsys.readouterr().err


def test_legacy_provider_prefix_is_applied_without_a_vendor_branch() -> None:
    """`KORVID_EVAL_PROVIDER` is a reference prefix, not a transport switch."""
    provider = _build(
        {
            "KORVID_EVAL_PROVIDER": "ollama",
            "KORVID_EVAL_BASE_URL": "http://localhost:11434/v1",
            "KORVID_EVAL_MODEL": "qwen3:8b",
        }
    )

    assert isinstance(provider, LiteLLMProvider)
    assert provider.descriptor.provider == "ollama"
    assert provider.descriptor.model == "qwen3:8b"


def test_canonical_reference_wins_over_the_legacy_prefix() -> None:
    """A `provider/model` reference is already complete; nothing is prepended."""
    provider = _build(_eval_env(KORVID_EVAL_PROVIDER="ollama", KORVID_EVAL_MODEL="openai/gpt-4o"))

    assert provider.descriptor.provider == "openai"


def test_eval_refuses_a_reference_litellm_cannot_route() -> None:
    """A bad reference fails at configuration, not at the first live request."""
    with pytest.raises(SystemExit, match="not-a-real-vendor/whatever"):
        provider_factory_from_env(_eval_env(KORVID_EVAL_MODEL="not-a-real-vendor/whatever"))


def test_eval_refusal_is_actionable_and_never_returns_none() -> None:
    """A refused profile must exit with the reason, not hand back `None`."""
    with pytest.raises(SystemExit) as excinfo:
        provider_factory_from_env(_eval_env(KORVID_EVAL_MODEL="bare-unmapped-model"))

    message = str(excinfo.value)
    assert "bare-unmapped-model" in message
    assert "litellm cannot dispatch" in message
