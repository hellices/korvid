"""Provider-neutral agent profile parsing (spec: provider-neutral model profiles)."""

from __future__ import annotations

from pathlib import Path

import pytest

from korvid.core.config import (
    AGENT_PROFILE_NAME_MAX_LENGTH,
    AgentAuthConfig,
    AgentProfileConfig,
    AgentProfilesConfig,
    is_valid_profile_name,
    load_config,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_multiple_profiles_round_trip_into_the_domain(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: production
  profiles:
    production:
      model: anthropic:claude-sonnet-4-5
      auth:
        method: environment
        key: ANTHROPIC_API_KEY
    local:
      model: ollama:llama3
      endpoint: http://localhost:11434
      auth:
        method: none
      options:
        num_ctx: 16384
        temperature: 0
""",
    )
    cfg = load_config(path)
    assert list(cfg.agent_profiles.profiles) == ["production", "local"]
    assert cfg.agent_profiles.active == "production"
    local = cfg.agent_profiles.profiles["local"]
    assert local.model == "ollama:llama3"
    assert local.endpoint == "http://localhost:11434"
    assert local.auth == AgentAuthConfig(method="none", settings={})
    assert local.options["num_ctx"] == 16384
    assert local.config_error is None


def test_active_profile_selects_the_exact_named_entry(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: prod_east
  profiles:
    prod-east:
      model: openai:gpt-4o
    prod_east:
      model: openai:gpt-4o-mini
""",
    )
    cfg = load_config(path)
    active = cfg.agent_profiles.active_profile
    assert active is not None
    assert active.model == "openai:gpt-4o-mini"


def test_unknown_active_profile_disables_the_agent_with_a_warning(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: missing
  profiles:
    local:
      model: ollama:llama3
""",
    )
    cfg = load_config(path)
    assert cfg.agent_profiles.active is None
    assert cfg.agent_profiles.active_profile is None
    assert any("agent.active" in warning for warning in cfg.warnings)


def test_nested_option_mappings_are_copy_owned_and_immutable(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
      options:
        nested:
          depth: 1
        items: [1, 2]
""",
    )
    cfg = load_config(path)
    options = cfg.agent_profiles.profiles["local"].options
    with pytest.raises(TypeError, match="does not support item assignment"):
        options["nested"]["depth"] = 2  # type: ignore[index]  # proving immutability
    assert options["items"] == (1, 2)


def test_profile_order_follows_the_file_not_an_alphabetical_sort(tmp_path: Path) -> None:
    """Insertion order is the contract the wizard and `:model` list against."""
    path = _write(
        tmp_path,
        """
agent:
  active: zulu
  profiles:
    zulu:
      model: openai:gpt-4o
    alpha:
      model: openai:gpt-4o-mini
    mike:
      model: ollama:llama3
""",
    )
    cfg = load_config(path)
    assert list(cfg.agent_profiles.profiles) == ["zulu", "alpha", "mike"]


def test_oversized_options_are_rejected_and_recorded_on_the_profile(tmp_path: Path) -> None:
    """`options` goes through the same bounded validator as `agent.options`."""
    blob = "x" * 4096
    path = _write(
        tmp_path,
        f"""
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
      options:
        blob: "{blob}"
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.profiles["local"]
    assert profile.options == {}
    assert profile.options_error is not None
    assert profile.config_error == profile.options_error
    assert any("agent.profiles[local].options" in warning for warning in cfg.warnings)


def test_an_inline_secret_in_auth_settings_is_refused(tmp_path: Path) -> None:
    """A profile stores references; a key that looks like a secret is a bug."""
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: openai:gpt-4o
      auth:
        method: environment
        api_key: sk-inline-not-a-reference
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.profiles["local"]
    assert profile.auth.settings == {}
    assert profile.auth.settings_error is not None
    assert profile.config_error == profile.auth.settings_error
    assert any("agent.profiles[local].auth" in warning for warning in cfg.warnings)
    assert not any("sk-inline-not-a-reference" in warning for warning in cfg.warnings)


def test_an_environment_reference_key_is_not_treated_as_a_secret(tmp_path: Path) -> None:
    """`key: OPENAI_API_KEY` names a variable; only the *key name* is bounded."""
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: openai:gpt-4o
      auth:
        method: environment
        key: OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.profiles["local"]
    assert profile.auth.settings == {"key": "OPENAI_API_KEY"}
    assert profile.config_error is None


def test_revalidating_an_already_frozen_profile_is_idempotent() -> None:
    """Rebuilding a profile from a frozen one must not fail on its tuples."""
    first = AgentProfileConfig(model="ollama:llama3", options={"stop": ["a", "b"]})
    assert first.options["stop"] == ("a", "b")
    second = AgentProfileConfig(model=first.model, options=first.options)
    assert second.options == first.options
    assert second.options_error is None


@pytest.mark.parametrize(
    "instance",
    [
        AgentAuthConfig(method="none"),
        AgentProfileConfig(model="openai:gpt-4o"),
        AgentProfilesConfig(),
    ],
)
def test_frozen_profile_dataclasses_are_unhashable(instance: object) -> None:
    """They hold mutable-by-identity proxies; hashing one would be a lie."""
    with pytest.raises(TypeError, match="unhashable type"):
        hash(instance)


def test_invalid_profile_names_are_dropped_with_a_warning(tmp_path: Path) -> None:
    long_name = "x" * (AGENT_PROFILE_NAME_MAX_LENGTH + 1)
    path = _write(
        tmp_path,
        f"""
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
    "bad name":
      model: openai:gpt-4o
    {long_name}:
      model: openai:gpt-4o
""",
    )
    cfg = load_config(path)
    assert set(cfg.agent_profiles.profiles) == {"local"}
    assert any("invalid profile name" in warning for warning in cfg.warnings)


def test_profile_without_a_model_reference_is_dropped(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
    broken:
      endpoint: http://example.invalid
""",
    )
    cfg = load_config(path)
    assert set(cfg.agent_profiles.profiles) == {"local"}
    assert any("broken" in warning and "model" in warning for warning in cfg.warnings)


def test_an_unmodellable_profile_is_kept_as_a_raw_mapping_but_never_reaches_the_runtime(
    tmp_path: Path,
) -> None:
    """Dropping a profile from the domain must not amount to deleting it."""
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
    "bad name":
      model: openai:gpt-4o
    broken:
      endpoint: http://example.invalid
""",
    )
    cfg = load_config(path)
    profiles = cfg.agent_profiles
    assert set(profiles.profiles) == {"local"}
    assert set(profiles.unparsed) == {"bad name", "broken"}
    assert profiles.unparsed["broken"] == {"endpoint": "http://example.invalid"}
    assert profiles.active_profile is profiles.profiles["local"]


def test_a_rejected_options_block_is_retained_verbatim_for_write_back(
    tmp_path: Path,
) -> None:
    """The operator has to be able to fix the block korvid refused to load."""
    blob = "x" * 4096
    path = _write(
        tmp_path,
        f"""
agent:
  active: local
  profiles:
    local:
      model: ollama:llama3
      options:
        blob: "{blob}"
""",
    )
    cfg = load_config(path)
    profile = cfg.agent_profiles.profiles["local"]
    assert profile.options == {}
    assert profile.options_error is not None
    assert cfg.agent_profiles.unparsed["local"] == {
        "model": "ollama:llama3",
        "options": {"blob": blob},
    }


@pytest.mark.parametrize("name", ["prod", "prod-east", "prod_east", "a.b", "x" * 100])
def test_valid_profile_names(name: str) -> None:
    assert is_valid_profile_name(name)


@pytest.mark.parametrize("name", ["", " prod", "prod east", "prod/east", "x" * 101, "naïve"])
def test_invalid_profile_names(name: str) -> None:
    assert not is_valid_profile_name(name)


def test_profiles_config_defaults_are_empty() -> None:
    empty = AgentProfilesConfig()
    assert empty.active is None
    assert empty.active_profile is None
    assert empty.profiles == {}
    assert empty.unparsed == {}
    assert AgentProfileConfig(model="ollama:llama3").auth.method == "none"
