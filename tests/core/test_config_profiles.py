"""Provider-neutral agent profile parsing (spec: provider-neutral model profiles)."""

from __future__ import annotations

from abc import ABC
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import yaml

from korvid.core.config import (
    AGENT_PROFILE_NAME_MAX_LENGTH,
    KEEP_MODEL_TIER,
    ConfigFileModelConnectionsWriter,
    ConnectionAuthConfig,
    ModelConnectionConfig,
    ModelConnectionsConfig,
    ModelConnectionsWriter,
    ModelTierWrite,
    is_valid_profile_name,
    load_config,
    save_model_connections,
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
      model: anthropic/claude-sonnet-4-5
      auth:
        method: environment
        key: ANTHROPIC_API_KEY
    local:
      model: ollama/llama3
      endpoint: http://localhost:11434
      auth:
        method: none
      options:
        num_ctx: 16384
        temperature: 0
""",
    )
    cfg = load_config(path)
    assert list(cfg.model_connections.profiles) == ["production", "local"]
    assert cfg.model_connections.active == "production"
    local = cfg.model_connections.profiles["local"]
    assert local.model == "ollama/llama3"
    assert local.endpoint == "http://localhost:11434"
    assert local.auth == ConnectionAuthConfig(method="none", settings={})
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
      model: openai/gpt-4o
    prod_east:
      model: openai/gpt-4o-mini
""",
    )
    cfg = load_config(path)
    active = cfg.model_connections.active_profile
    assert active is not None
    assert active.model == "openai/gpt-4o-mini"


def test_unknown_active_profile_disables_the_agent_with_a_warning(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: missing
  profiles:
    local:
      model: ollama/llama3
""",
    )
    cfg = load_config(path)
    assert cfg.model_connections.active is None
    assert cfg.model_connections.active_profile is None
    assert any("agent.active" in warning for warning in cfg.warnings)


def test_nested_option_mappings_are_copy_owned_and_immutable(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: ollama/llama3
      options:
        nested:
          depth: 1
        items: [1, 2]
""",
    )
    cfg = load_config(path)
    options = cfg.model_connections.profiles["local"].options
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
      model: openai/gpt-4o
    alpha:
      model: openai/gpt-4o-mini
    mike:
      model: ollama/llama3
""",
    )
    cfg = load_config(path)
    assert list(cfg.model_connections.profiles) == ["zulu", "alpha", "mike"]


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
      model: ollama/llama3
      options:
        blob: "{blob}"
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.profiles["local"]
    assert profile.options == {}
    assert profile.options_error is not None
    assert profile.config_error == profile.options_error
    assert any("profiles[local].options" in warning for warning in cfg.warnings)


def test_an_inline_secret_in_auth_settings_is_refused(tmp_path: Path) -> None:
    """A profile stores references; a key that looks like a secret is a bug."""
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: openai/gpt-4o
      auth:
        method: environment
        api_key: sk-inline-not-a-reference
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.profiles["local"]
    assert profile.auth.settings == {}
    assert profile.auth.settings_error is not None
    assert profile.config_error == profile.auth.settings_error
    assert any("profiles[local].auth" in warning for warning in cfg.warnings)
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
      model: openai/gpt-4o
      auth:
        method: environment
        key: OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.profiles["local"]
    assert profile.auth.settings == {"key": "OPENAI_API_KEY"}
    assert profile.config_error is None


def test_revalidating_an_already_frozen_profile_is_idempotent() -> None:
    """Rebuilding a profile from a frozen one must not fail on its tuples."""
    first = ModelConnectionConfig(model="ollama/llama3", options={"stop": ["a", "b"]})
    assert first.options["stop"] == ("a", "b")
    second = ModelConnectionConfig(model=first.model, options=first.options)
    assert second.options == first.options
    assert second.options_error is None


@pytest.mark.parametrize(
    "instance",
    [
        ConnectionAuthConfig(method="none"),
        ModelConnectionConfig(model="openai/gpt-4o"),
        ModelConnectionsConfig(),
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
      model: ollama/llama3
    "bad name":
      model: openai/gpt-4o
    {long_name}:
      model: openai/gpt-4o
""",
    )
    cfg = load_config(path)
    assert set(cfg.model_connections.profiles) == {"local"}
    assert any("invalid profile name" in warning for warning in cfg.warnings)


def test_profile_without_a_model_reference_is_dropped(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: ollama/llama3
    broken:
      endpoint: http://example.invalid
""",
    )
    cfg = load_config(path)
    assert set(cfg.model_connections.profiles) == {"local"}
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
      model: ollama/llama3
    "bad name":
      model: openai/gpt-4o
    broken:
      endpoint: http://example.invalid
""",
    )
    cfg = load_config(path)
    profiles = cfg.model_connections
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
      model: ollama/llama3
      options:
        blob: "{blob}"
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.profiles["local"]
    assert profile.options == {}
    assert profile.options_error is not None
    assert cfg.model_connections.unparsed["local"] == {
        "model": "ollama/llama3",
        "options": {"blob": blob},
    }


@pytest.mark.parametrize("name", ["prod", "prod-east", "prod_east", "a.b", "x" * 100])
def test_valid_profile_names(name: str) -> None:
    assert is_valid_profile_name(name)


@pytest.mark.parametrize("name", ["", " prod", "prod east", "prod/east", "x" * 101, "naïve"])
def test_invalid_profile_names(name: str) -> None:
    assert not is_valid_profile_name(name)


def test_profiles_config_defaults_are_empty() -> None:
    empty = ModelConnectionsConfig()
    assert empty.active is None
    assert empty.active_profile is None
    assert empty.profiles == {}
    assert empty.unparsed == {}
    assert ModelConnectionConfig(model="ollama/llama3").auth.method == "none"


# ---------------------------------------------------------------------------
# Task 3 tests: profile writer and profile parsing
# ---------------------------------------------------------------------------


def test_saving_preserves_unrelated_top_level_and_agent_keys(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
kube_context: prod
agent:
  active: default
  profiles:
    default:
      model: ollama/llama3
      endpoint: http://localhost:11434
      options:
        num_ctx: 8192
""",
    )
    cfg = load_config(path)
    save_model_connections(path, cfg.model_connections)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["kube_context"] == "prod"
    assert set(raw["agent"]) == {"active", "profiles"}
    assert raw["agent"]["active"] == "default"
    assert raw["agent"]["profiles"]["default"]["model"] == "ollama/llama3"
    assert raw["agent"]["profiles"]["default"]["options"]["num_ctx"] == 8192


def test_a_nested_option_round_trips_through_the_writer(tmp_path: Path) -> None:
    """`_freeze_config_value` produces `mappingproxy`/`tuple`; `yaml.safe_dump`
    raises `RepresenterError` on the first and normalises the second. The
    writer must thaw recursively, and the result must reload equal."""
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: openai/gpt-4o
      options:
        nested:
          depth: 1
        items: [1, 2]
""",
    )
    before = load_config(path).model_connections
    save_model_connections(path, before)
    after = load_config(path).model_connections
    nested = after.profiles["main"].options["nested"]
    assert isinstance(nested, Mapping)
    assert nested["depth"] == 1
    assert after.profiles["main"].options["items"] == (1, 2)
    assert after == before


def test_saving_carries_unparsed_entries_back_verbatim(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: good
  profiles:
    good:
      model: openai/gpt-4o
    broken: {}
""",
    )
    cfg = load_config(path)
    assert set(cfg.model_connections.profiles) == {"good"}
    save_model_connections(path, cfg.model_connections)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(raw["agent"]["profiles"]) == {"good", "broken"}


def test_an_explicitly_removed_profile_does_not_come_back(tmp_path: Path) -> None:
    """A profile with a rejected `options` block is in *both* `profiles`
    and `unparsed`. Dropping it from one alone lets the writer re-emit it
    from the other, and the operator can never delete it."""
    path = _write(
        tmp_path,
        """
agent:
  active: good
  profiles:
    good:
      model: openai/gpt-4o
    rejected:
      model: openai/gpt-4o
      options:
        api_key: inline-secret-value
""",
    )
    cfg = load_config(path)
    assert cfg.model_connections.profiles["rejected"].config_error is not None
    assert "rejected" in cfg.model_connections.unparsed

    pruned = replace(
        cfg.model_connections,
        profiles={k: v for k, v in cfg.model_connections.profiles.items() if k != "rejected"},
        unparsed={k: v for k, v in cfg.model_connections.unparsed.items() if k != "rejected"},
    )
    save_model_connections(path, pruned)
    assert set(load_config(path).model_connections.profiles) == {"good"}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(raw["agent"]["profiles"]) == {"good"}


def test_an_unrelated_save_keeps_a_rejected_block_the_operator_must_repair(
    tmp_path: Path,
) -> None:
    """Activating one profile must not silently strip another's raw text.

    A profile whose `auth` or `options` was rejected is in *both* halves:
    `profiles` holds the modelled remains with the offending block
    emptied, `unparsed` holds the operator's original entry. Serializing
    the modelled remains over the raw entry deletes exactly the block the
    operator has to edit — a save that "succeeded" while destroying the
    only copy of the thing it was preserving.
    """
    path = _write(
        tmp_path,
        """
agent:
  active: good
  profiles:
    good:
      model: openai/gpt-4o
    rejected:
      model: openai/gpt-4o
      auth:
        method: environment
        key: OPENAI_API_KEY
        api_key: inline-secret-value
      options:
        api_key: inline-secret-value
        num_ctx: 8192
""",
    )
    cfg = load_config(path)
    rejected = cfg.model_connections.profiles["rejected"]
    assert rejected.config_error is not None
    assert dict(rejected.options) == {}
    assert dict(rejected.auth.settings) == {}

    # An unrelated activation: only the pointer moves.
    save_model_connections(path, replace(cfg.model_connections, active="good"))

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entry = raw["agent"]["profiles"]["rejected"]
    assert entry["options"] == {"api_key": "inline-secret-value", "num_ctx": 8192}
    assert entry["auth"]["key"] == "OPENAI_API_KEY"
    assert entry["auth"]["api_key"] == "inline-secret-value"
    # Reloading still surfaces the same repairable profile.
    assert load_config(path).model_connections.profiles["rejected"].config_error is not None


def test_repairing_a_rejected_profile_replaces_the_raw_entry(tmp_path: Path) -> None:
    """The raw entry outranks the modelled one only until it is repaired.

    Once the editor hands back a profile korvid *can* model, the manager
    drops the name from `unparsed`, and that is what lets the repair
    actually reach the file instead of being overwritten by the text it
    replaces.
    """
    path = _write(
        tmp_path,
        """
agent:
  active: good
  profiles:
    good:
      model: openai/gpt-4o
    rejected:
      model: openai/gpt-4o
      options:
        api_key: inline-secret-value
""",
    )
    cfg = load_config(path)
    repaired = replace(
        cfg.model_connections,
        profiles={
            **cfg.model_connections.profiles,
            "rejected": ModelConnectionConfig(
                model="openai/gpt-4o",
                auth=ConnectionAuthConfig(method="environment", settings={"key": "OPENAI_API_KEY"}),
                options={"num_ctx": 8192},
            ),
        },
        unparsed={k: v for k, v in cfg.model_connections.unparsed.items() if k != "rejected"},
    )
    save_model_connections(path, repaired)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entry = raw["agent"]["profiles"]["rejected"]
    assert entry["options"] == {"num_ctx": 8192}
    assert entry["auth"] == {"method": "environment", "key": "OPENAI_API_KEY"}
    assert load_config(path).model_connections.profiles["rejected"].config_error is None


def test_a_pure_unparsed_name_never_loses_to_a_generated_entry(tmp_path: Path) -> None:
    """The raw half wins for a name that only exists there, too — the
    rule is one rule, not a special case for rejected blocks."""
    path = _write(
        tmp_path,
        """
agent:
  active: good
  profiles:
    good:
      model: openai/gpt-4o
    broken:
      endpoint: http://example.invalid
""",
    )
    cfg = load_config(path)
    assert set(cfg.model_connections.profiles) == {"good"}
    save_model_connections(path, cfg.model_connections)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["agent"]["profiles"]["broken"] == {"endpoint": "http://example.invalid"}


def test_an_inline_secret_is_rejected_and_recorded_on_the_profile(tmp_path: Path) -> None:
    """The secret-bearing option keeps the profile from ever being built:
    `config_error` is the field the factory refuses on."""
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: openai/gpt-4o
      options:
        api_key: inline-secret-value
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.profiles["main"]

    assert profile.config_error is not None
    assert "api_key" not in profile.options
    assert any("rejected" in w for w in cfg.warnings)


def test_a_plural_inline_secret_is_refused_and_the_profile_still_survives(
    tmp_path: Path,
) -> None:
    """`api_keys` used to pass this gate and the request gate both.

    The refusal keeps the existing semantics: the profile is still there
    with its model and endpoint, `options` is empty, `config_error` is
    set — so `:ai` can show it and the factory refuses to build it — and
    the operator gets a warning naming the segment.
    """
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: openai/gpt-4o
      endpoint: https://gateway.example/v1
      options:
        api_keys:
          - inline-secret-value
        temperature: 0.2
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.profiles["main"]

    assert profile.model == "openai/gpt-4o"
    assert profile.endpoint == "https://gateway.example/v1"
    assert dict(profile.options) == {}
    assert profile.config_error is not None
    assert "api_key" in profile.config_error
    assert any("rejected" in w for w in cfg.warnings)


def test_an_environment_profile_comes_up_enabled(tmp_path: Path) -> None:
    """The whole point of the mapping: a profile written by the wizard has
    to come up enabled, with nothing to warn about."""
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: openai/gpt-4o
      endpoint: https://api.example/v1
      auth:
        method: environment
        key: OPENAI_API_KEY
""",
    )
    cfg = load_config(path)

    assert cfg.agent_enabled is True
    assert cfg.model_connections.active_profile is not None
    assert cfg.model_connections.active_profile.auth.method == "environment"
    assert cfg.warnings == ()


def test_a_profile_never_carries_the_secret_itself(tmp_path: Path) -> None:
    """`auth.key` is a variable *name*. Nothing in the config layer reads
    the environment, so no round-trip can move a secret value into the
    file."""
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: openai/gpt-4o
      endpoint: https://api.example/v1
      auth:
        method: environment
        key: OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.profiles["main"]

    assert profile.auth.settings["key"] == "OPENAI_API_KEY"
    assert "sk-" not in repr(profile)
    save_model_connections(path, cfg.model_connections)
    assert "sk-" not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The model tier travels with the profiles it was chosen for
# ---------------------------------------------------------------------------


def test_the_writer_leaves_a_tier_it_was_not_asked_about_alone(tmp_path: Path) -> None:
    """Editing profiles is not a tier decision.

    The profile manager and `:model` never ask about the tier, so their
    saves must not be able to drop an override the operator chose in the
    wizard — the bug a separate tier writer produces every time the two
    writes disagree.
    """
    path = _write(
        tmp_path,
        """
agent:
  model_tier: high
  active: main
  profiles:
    main:
      model: openai/gpt-4o
""",
    )
    cfg = load_config(path)
    assert cfg.agent_model_tier == "high"
    save_model_connections(path, cfg.model_connections)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["agent"]["model_tier"] == "high"
    assert load_config(path).agent_model_tier == "high"


def test_the_writer_persists_a_tier_in_the_same_write_as_the_profiles(tmp_path: Path) -> None:
    """One file write carries both, so a crash can never leave a profile
    set persisted with a tier that was never written (or vice versa)."""
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: openai/gpt-4o
""",
    )
    cfg = load_config(path)
    save_model_connections(path, cfg.model_connections, model_tier="high")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["agent"]["model_tier"] == "high"
    assert raw["agent"]["active"] == "main"
    reloaded = load_config(path)
    assert reloaded.agent_model_tier == "high"
    assert reloaded.model_connections == cfg.model_connections


def test_choosing_automatic_clears_a_stale_tier(tmp_path: Path) -> None:
    """Automatic is `None`, and it must actually remove the old override —
    otherwise reopening the wizard resets it to a tier nobody chose."""
    path = _write(
        tmp_path,
        """
agent:
  model_tier: low
  active: main
  profiles:
    main:
      model: openai/gpt-4o
""",
    )
    cfg = load_config(path)
    save_model_connections(path, cfg.model_connections, model_tier=None)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "model_tier" not in raw["agent"]
    assert load_config(path).agent_model_tier is None


def test_writing_a_tier_keeps_unrelated_agent_and_top_level_keys(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
kube_context: prod
agent:
  rules: keep-me
  active: main
  profiles:
    main:
      model: openai/gpt-4o
""",
    )
    cfg = load_config(path)
    save_model_connections(path, cfg.model_connections, model_tier="low")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["kube_context"] == "prod"
    assert raw["agent"]["rules"] == "keep-me"
    assert raw["agent"]["model_tier"] == "low"


def test_an_invalid_tier_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """The tier vocabulary is `low`/`high`/absent. A writer that accepted
    anything else would persist a file `load_config` then rejects."""
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: openai/gpt-4o
""",
    )
    before = path.read_text(encoding="utf-8")
    cfg = load_config(path)
    with pytest.raises(ValueError, match="model_tier"):
        save_model_connections(path, cfg.model_connections, model_tier="medium")
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("written", "raw"),
    [
        ("environment", "environment"),
        ("[environment]", ["environment"]),
        ("42", 42),
        ("true", True),
    ],
)
def test_a_present_non_mapping_auth_block_is_refused_not_read_as_absent(
    tmp_path: Path, written: str, raw: object
) -> None:
    """`auth: environment` is an instruction, not an absent block.

    Reading it as absent would build the connection with method `none`
    while the file says a credential is in play — a silent downgrade to
    unauthenticated. It goes through the same bounded validator a bad
    `auth` mapping goes through, so it reaches `config_error` too.
    """
    path = _write(
        tmp_path,
        f"""
agent:
  active: local
  profiles:
    local:
      model: openai/gpt-4o
      auth: {written}
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.profiles["local"]
    assert profile.auth.method == "none"
    assert profile.auth.settings == {}
    assert profile.auth.settings_error is not None
    assert profile.config_error == profile.auth.settings_error
    assert any("profiles[local].auth" in warning for warning in cfg.warnings)
    # Kept verbatim: the refused block is the one thing the operator edits.
    assert cfg.model_connections.unparsed["local"] == {"model": "openai/gpt-4o", "auth": raw}


@pytest.mark.parametrize(
    ("written", "raw"),
    [
        ("num_ctx", "num_ctx"),
        ("[num_ctx]", ["num_ctx"]),
        ("8192", 8192),
        ("false", False),
    ],
)
def test_a_present_non_mapping_options_block_is_refused_not_dropped(
    tmp_path: Path, written: str, raw: object
) -> None:
    """A scalar `options:` is settings korvid cannot apply; dropping it
    silently would connect with a configuration the file does not describe."""
    path = _write(
        tmp_path,
        f"""
agent:
  active: local
  profiles:
    local:
      model: openai/gpt-4o
      options: {written}
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.profiles["local"]
    assert profile.options == {}
    assert profile.options_error is not None
    assert profile.config_error == profile.options_error
    assert any("profiles[local].options" in warning for warning in cfg.warnings)
    assert cfg.model_connections.unparsed["local"] == {"model": "openai/gpt-4o", "options": raw}


@pytest.mark.parametrize(
    "block",
    ["", "\n      auth:", "\n      auth: null", "\n      options:", "\n      options: null"],
)
def test_an_absent_or_null_block_defaults_safely_and_is_never_unparsed(
    tmp_path: Path, block: str
) -> None:
    """`null` is the YAML spelling of "not set" — the reading
    `agent.model_tier` already gives it — so it defaults exactly like an
    absent key: no error, and nothing to repair."""
    path = _write(
        tmp_path,
        f"""
agent:
  active: local
  profiles:
    local:
      model: openai/gpt-4o{block}
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.profiles["local"]
    assert profile.auth == ConnectionAuthConfig(method="none", settings={})
    assert profile.options == {}
    assert profile.config_error is None
    assert "local" not in cfg.model_connections.unparsed
    assert not any("profiles[local]" in warning for warning in cfg.warnings)


def test_a_refused_scalar_block_survives_an_unrelated_save(tmp_path: Path) -> None:
    """The raw half outranks the modelled one for a scalar block too:
    activating another profile must not delete the line to be repaired."""
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    good:
      model: openai/gpt-4o
    local:
      model: openai/gpt-4o
      auth: environment
""",
    )
    cfg = load_config(path)
    save_model_connections(path, replace(cfg.model_connections, active="good"))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["agent"]["profiles"]["local"]["auth"] == "environment"
    assert load_config(path).model_connections.profiles["local"].config_error is not None


def test_a_non_string_profile_key_keeps_the_identity_the_file_gave_it(tmp_path: Path) -> None:
    """`1:` is a YAML integer key, not the profile named `"1"`.

    Recording it as `"1"` would rename the operator's entry: the next
    save writes a quoted key, and the load after that accepts it as a
    perfectly valid profile name — korvid promoting text it refused into
    a runtime profile, by itself.
    """
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: openai/gpt-4o
    1:
      model: openai/gpt-4o-mini
""",
    )
    profiles = load_config(path).model_connections
    assert set(profiles.profiles) == {"local"}
    assert profiles.unparsed[1] == {"model": "openai/gpt-4o-mini"}
    assert "1" not in profiles.unparsed

    save_model_connections(path, profiles)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["agent"]["profiles"][1] == {"model": "openai/gpt-4o-mini"}
    assert "1" not in raw["agent"]["profiles"]

    reloaded = load_config(path).model_connections
    assert set(reloaded.profiles) == {"local"}
    assert reloaded.unparsed[1] == {"model": "openai/gpt-4o-mini"}


def test_a_numeric_key_and_its_quoted_twin_stay_two_entries(tmp_path: Path) -> None:
    """`1:` and `"1":` are different keys to YAML, so collapsing them
    loses one: the raw half outranks the modelled one on write, so the
    refused entry would be written over the valid profile."""
    path = _write(
        tmp_path,
        """
agent:
  active: "1"
  profiles:
    1:
      model: openai/gpt-4o
    "1":
      model: openai/gpt-4o-mini
""",
    )
    profiles = load_config(path).model_connections
    assert profiles.profiles["1"].model == "openai/gpt-4o-mini"
    assert profiles.unparsed[1] == {"model": "openai/gpt-4o"}

    save_model_connections(path, profiles)
    written = yaml.safe_load(path.read_text(encoding="utf-8"))["agent"]["profiles"]
    assert written[1] == {"model": "openai/gpt-4o"}
    assert written["1"]["model"] == "openai/gpt-4o-mini"

    reloaded = load_config(path).model_connections
    assert reloaded.profiles["1"].model == "openai/gpt-4o-mini"
    assert reloaded.active == "1"
    assert reloaded.unparsed[1] == {"model": "openai/gpt-4o"}


@pytest.mark.parametrize(
    ("written", "key"),
    [("true", True), ("1.5", 1.5), ("null", None), ("2026-01-01", date(2026, 1, 1))],
)
def test_every_scalar_yaml_key_survives_a_save_as_itself(
    tmp_path: Path, written: str, key: object
) -> None:
    """Bools, floats, nulls and dates are all keys `yaml.safe_load`
    produces. None of them is a profile name, and a save must not
    invent one."""
    path = _write(
        tmp_path,
        f"""
agent:
  profiles:
    {written}:
      model: openai/gpt-4o
""",
    )
    profiles = load_config(path).model_connections
    assert profiles.profiles == {}
    assert list(profiles.unparsed) == [key]

    save_model_connections(path, profiles)
    reloaded = load_config(path).model_connections
    assert reloaded.profiles == {}
    assert list(reloaded.unparsed) == [key]


def test_a_sequence_profile_key_never_reaches_korvid(tmp_path: Path) -> None:
    """The tuple-like key YAML can spell is one `safe_load` refuses to
    build, so the document fails as a document — korvid is never handed
    half a profile set to preserve."""
    path = _write(
        tmp_path,
        """
agent:
  profiles:
    ? [a, b]
    : {model: openai/gpt-4o}
""",
    )
    with pytest.raises(yaml.YAMLError, match="unhashable key"):
        load_config(path)


def test_deleting_a_string_profile_leaves_its_numeric_twin_alone(tmp_path: Path) -> None:
    """Deletion clears a *name* from both halves. A key that only looks
    like that name is a different entry and must stay."""
    path = _write(
        tmp_path,
        """
agent:
  active: "1"
  profiles:
    1:
      model: openai/gpt-4o
    "1":
      model: openai/gpt-4o-mini
""",
    )
    profiles = load_config(path).model_connections
    pruned = replace(
        profiles,
        active=None,
        profiles={k: v for k, v in profiles.profiles.items() if k != "1"},
        unparsed={k: v for k, v in profiles.unparsed.items() if k != "1"},
    )
    save_model_connections(path, pruned)

    written = yaml.safe_load(path.read_text(encoding="utf-8"))["agent"]["profiles"]
    assert written == {1: {"model": "openai/gpt-4o"}}


def test_a_raw_entry_keeps_its_own_nested_keys_through_a_save(tmp_path: Path) -> None:
    """`unparsed` is held opaquely, so the writer must not rewrite the
    keys *inside* it either — the operator's text is what they have to
    repair."""
    path = _write(
        tmp_path,
        """
agent:
  active: good
  profiles:
    good:
      model: openai/gpt-4o
    broken:
      model: openai/gpt-4o
      options:
        1: one
""",
    )
    profiles = load_config(path).model_connections
    assert profiles.profiles["broken"].options_error is not None

    save_model_connections(path, profiles)
    written = yaml.safe_load(path.read_text(encoding="utf-8"))["agent"]["profiles"]
    assert written["broken"]["options"] == {1: "one"}


# ---------------------------------------------------------------------------
# The writer seam the UI is injected with
# ---------------------------------------------------------------------------


def test_the_writer_seam_is_an_abstract_base_class() -> None:
    """A boundary interface is an `abc.ABC` (AGENTS.md), not a Protocol.

    `ModelConnectionsWriter` crosses core → UI, and a structural Protocol
    makes that seam invisible: anything callable enough satisfies it, so
    no implementation ever declares that it is one and a signature drift
    is only caught where a checker happens to look. The ABC is nominal —
    an implementation says so, and the composition root hands over
    something that says so.
    """
    assert issubclass(ModelConnectionsWriter, ABC)
    assert getattr(ModelConnectionsWriter.__call__, "__isabstractmethod__", False)
    with pytest.raises(TypeError, match="abstract"):
        ModelConnectionsWriter()  # type: ignore[abstract]  # the point of the test

    def structural(
        profiles: ModelConnectionsConfig, *, model_tier: ModelTierWrite = KEEP_MODEL_TIER
    ) -> None: ...  # pragma: no cover - never called

    assert not isinstance(structural, ModelConnectionsWriter)
    assert issubclass(ConfigFileModelConnectionsWriter, ModelConnectionsWriter)


def test_the_config_file_writer_persists_profiles_and_the_tier(tmp_path: Path) -> None:
    """The concrete writer is `save_model_connections` bound to one path.

    Binding the path is the whole job: the screens are handed a writer,
    never a location, so no UI code can choose a file to write to.
    """
    path = _write(
        tmp_path,
        """
kube_context: prod
agent:
  active: main
  profiles:
    main:
      model: openai/gpt-4o
""",
    )
    writer: ModelConnectionsWriter = ConfigFileModelConnectionsWriter(path)
    cfg = load_config(path)

    writer(replace(cfg.model_connections, active="main"), model_tier="high")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["kube_context"] == "prod"
    assert raw["agent"]["active"] == "main"
    assert raw["agent"]["model_tier"] == "high"


def test_the_config_file_writer_leaves_an_unasked_tier_alone(tmp_path: Path) -> None:
    """The sentinel default survives the adapter.

    A writer that forwarded `None` for "nobody asked" would clear an
    override every time the profile manager saved.
    """
    path = _write(
        tmp_path,
        """
agent:
  model_tier: high
  active: main
  profiles:
    main:
      model: openai/gpt-4o
""",
    )
    ConfigFileModelConnectionsWriter(path)(load_config(path).model_connections)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["agent"]["model_tier"] == "high"
