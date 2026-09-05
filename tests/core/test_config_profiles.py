"""Provider-neutral agent profile parsing (spec: provider-neutral model profiles)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from korvid.core.config import (
    AGENT_PROFILE_NAME_MAX_LENGTH,
    ConnectionAuthConfig,
    ModelConnectionConfig,
    ModelConnectionsConfig,
    _legacy_model_reference,
    is_valid_profile_name,
    load_config,
    project_legacy_transport,
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


def test_the_legacy_reference_helper_joins_with_a_slash() -> None:
    assert _legacy_model_reference("ollama", "llama3") == "ollama/llama3"
    assert _legacy_model_reference("vllm", "qwen") == "openai/qwen"


def test_a_model_identifier_containing_a_colon_survives_migration(
    tmp_path: Path,
) -> None:
    """`qwen3:8b` is a real Ollama tag, so the separator must be `/`."""
    path = _write(
        tmp_path,
        """
agent:
  provider: ollama
  model: qwen3:8b
  base_url: http://localhost:11434
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.model == "ollama/qwen3:8b"
    prefix, _, tag = profile.model.partition("/")
    assert (prefix, tag) == ("ollama", "qwen3:8b")


# ---------------------------------------------------------------------------
# Task 2: Legacy agent configuration migration
# ---------------------------------------------------------------------------


def test_legacy_ollama_config_becomes_the_default_profile(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  provider: ollama
  model: llama3
  base_url: http://localhost:11434
  ollama:
    num_ctx: 8192
    think: true
""",
    )
    cfg = load_config(path)
    assert cfg.model_connections.active == "default"
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.model == "ollama/llama3"
    assert profile.endpoint == "http://localhost:11434"
    assert profile.auth.method == "none"
    assert profile.options["num_ctx"] == 8192
    assert profile.options["think"] is True
    # The legacy transport was the native `/api/chat` route, and migration
    # must not silently switch an existing install to `/v1`.
    assert profile.options["native_api"] is True


def test_a_new_ollama_profile_defaults_to_the_shared_route(tmp_path: Path) -> None:
    """`native_api` is a migration artefact, not a default for new profiles.

    Only `_legacy_options` sets it. A profile written in the new shape is
    parsed verbatim, so an operator who never ran the old config gets the
    common adapter.
    """
    path = _write(
        tmp_path,
        """
agent:
  active: local
  profiles:
    local:
      model: ollama/llama3
      endpoint: http://localhost:11434
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert "native_api" not in profile.options


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param('num_ctx: "8192"', ("num_ctx", 8192), id="numeric-string-int"),
        pytest.param("num_ctx: 8192.0", ("num_ctx", 8192), id="float-to-int"),
        pytest.param('temperature: "0.2"', ("temperature", 0.2), id="numeric-string-float"),
        pytest.param("seed: 0", ("seed", 0), id="zero-seed-is-not-absent"),
        pytest.param("num_predict: 192", ("num_predict", 192), id="strict-int-kept"),
        pytest.param("keep_alive: 5m", ("keep_alive", "5m"), id="non-numeric-verbatim"),
    ],
)
def test_legacy_ollama_numbers_keep_the_old_parser_s_coercion(
    tmp_path: Path, raw: str, expected: tuple[str, object]
) -> None:
    """The pre-profile parser coerced these; Task 17 deletes it.

    `OllamaOptions` is a plain dataclass, so a surviving `"8192"` would be
    sent as a JSON string and would reach `context_window_tokens` as a
    `str`. Migration is the last place that can still fix it.
    """
    path = _write(tmp_path, f"agent:\n  provider: ollama\n  model: llama3\n  ollama:\n    {raw}\n")
    profile = load_config(path).model_connections.active_profile
    assert profile is not None
    key, value = expected
    assert profile.options[key] == value
    assert type(profile.options[key]) is type(value)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("num_ctx: nope", id="not-a-number"),
        pytest.param("num_ctx: true", id="bool-is-not-a-number"),
        pytest.param("temperature: .inf", id="non-finite"),
        pytest.param("num_ctx: [1]", id="wrong-shape"),
        pytest.param('num_predict: "192"', id="strict-int-refuses-a-numeric-string"),
        pytest.param("num_predict: 1.9", id="strict-int-refuses-a-fraction"),
        pytest.param("num_predict: 0", id="strict-int-refuses-non-positive"),
    ],
)
def test_an_uncoercible_legacy_ollama_value_is_dropped_with_a_warning(
    tmp_path: Path, raw: str
) -> None:
    """Dropped, not defaulted, and never fatal to the whole profile.

    The old parser substituted its own fallback here. That fallback is now
    the field default on `OllamaOptions`, which a migrated profile still
    reaches through `native_api: True`, so dropping the key restores the
    same effective value *and* names the line to fix. Non-finite floats
    matter especially: the bounded validator refuses them, so carrying one
    through would reject the entire migrated profile over one knob.

    `num_predict` is the odd one out: `num_ctx` accepts `"8192"` because
    its old parser did, while `num_predict`'s old parser refused a numeric
    string, a fraction, a `bool` and a non-positive value outright
    instead of coercing them (`tests/core/test_config.py` pins all four).
    Migration keeps each contract as it was rather than unifying them,
    because unifying them would change what an existing config means.
    """
    key = raw.split(":")[0]
    path = _write(tmp_path, f"agent:\n  provider: ollama\n  model: llama3\n  ollama:\n    {raw}\n")
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.config_error is None
    assert key not in profile.options
    assert any(f"agent.ollama.{key}" in warning for warning in cfg.warnings)


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai-compat", "gpt-4o-mini", "openai/gpt-4o-mini"),
        ("openai", "gpt-4o", "openai/gpt-4o"),
        ("vllm", "qwen", "openai/qwen"),
        ("anthropic", "claude-sonnet-4-5", "openai/claude-sonnet-4-5"),
        ("claude", "claude-sonnet-4-5", "openai/claude-sonnet-4-5"),
        ("azure", "gpt-4o", "azure/gpt-4o"),
        ("ollama", "llama3", "ollama/llama3"),
        ("github-copilot", "gpt-4o", "github-copilot/gpt-4o"),
        ("company-llm", "v2", "company-llm/v2"),
    ],
)
def test_legacy_provider_names_translate_to_model_references(
    tmp_path: Path, provider: str, model: str, expected: str
) -> None:
    path = _write(
        tmp_path,
        f"""
agent:
  provider: {provider}
  model: {model}
  base_url: https://example.invalid/v1
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.model == expected


def test_legacy_api_key_env_becomes_environment_auth(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  provider: openai-compat
  model: gpt-4o
  base_url: https://api.openai.com/v1
  api_key_env: OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.auth == ConnectionAuthConfig(
        method="environment", settings={"key": "OPENAI_API_KEY"}
    )


def test_legacy_entra_auth_becomes_provider_default(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  provider: azure
  model: gpt-4o
  base_url: https://example.openai.azure.com
  auth:
    method: entra
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.auth.method == "provider-default"
    assert profile.model == "azure/gpt-4o"
    assert profile.endpoint == "https://example.openai.azure.com"
    assert any("azure" in warning.lower() for warning in cfg.warnings)


def test_legacy_azure_api_key_keeps_the_azure_adapter(tmp_path: Path) -> None:
    """Azure is not an OpenAI-compatible endpoint: it must not become `openai/`.

    The `openai/` adapter would send `Authorization: ******; Azure
    OpenAI authenticates an API key with the raw `api-key` header, so a
    migration onto `openai/` would silently break every key-based Azure
    install.
    """
    path = _write(
        tmp_path,
        """
agent:
  provider: azure
  model: gpt-4o
  base_url: https://example.openai.azure.com
  api_key_env: AZURE_OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.model == "azure/gpt-4o"
    assert profile.auth == ConnectionAuthConfig(
        method="environment", settings={"key": "AZURE_OPENAI_API_KEY"}
    )


def test_legacy_azure_deployment_url_is_reduced_to_the_resource_url(tmp_path: Path) -> None:
    """The legacy client posted to `<base_url>/chat/completions`.

    So a working legacy `base_url` was deployment-scoped. `AzureProvider`
    wants the *resource* URL and appends `/openai/deployments/<model>`
    itself; handing it the old value produces
    `.../openai/deployments/my-dep/openai/chat/completions`, a 404. The
    migration therefore truncates at `/openai` and keeps the deployment
    name as an option instead of throwing it away.
    """
    path = _write(
        tmp_path,
        """
agent:
  provider: azure
  model: gpt-4o
  base_url: https://example.openai.azure.com/openai/deployments/my-dep
  api_key_env: AZURE_OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.endpoint == "https://example.openai.azure.com"
    assert profile.options["azure_deployment"] == "my-dep"
    assert any(
        "https://example.openai.azure.com/openai/deployments/my-dep" in warning
        and "was rewritten" in warning
        for warning in cfg.warnings
    )


def test_legacy_azure_v1_url_is_reduced_without_inventing_a_deployment(
    tmp_path: Path,
) -> None:
    """`.../openai/v1` is Azure's v1 surface: no deployment is encoded in it."""
    path = _write(
        tmp_path,
        """
agent:
  provider: azure
  model: gpt-4o
  base_url: https://example.openai.azure.com/openai/v1
  api_key_env: AZURE_OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.endpoint == "https://example.openai.azure.com"
    assert "azure_deployment" not in profile.options
    assert any("openai/v1" in warning for warning in cfg.warnings)


def test_a_bare_azure_resource_url_migrates_unchanged_and_silently(
    tmp_path: Path,
) -> None:
    """Nothing to rewrite means nothing to warn about."""
    path = _write(
        tmp_path,
        """
agent:
  provider: azure
  model: gpt-4o
  base_url: https://example.openai.azure.com/
  api_key_env: AZURE_OPENAI_API_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.endpoint == "https://example.openai.azure.com"
    assert not any("was rewritten" in warning for warning in cfg.warnings)


def test_a_non_azure_endpoint_is_never_rewritten(tmp_path: Path) -> None:
    """Only the `azure` adapter's endpoint changes meaning; leave the rest alone."""
    path = _write(
        tmp_path,
        """
agent:
  provider: openai-compat
  model: gpt-4o
  base_url: https://gateway.corp.invalid/openai/v1
  api_key_env: CORP_KEY
""",
    )
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.endpoint == "https://gateway.corp.invalid/openai/v1"


def test_legacy_copilot_auth_stays_device_login(tmp_path: Path) -> None:
    path = _write(tmp_path, "agent:\n  provider: github-copilot\n  model: gpt-4o\n")
    cfg = load_config(path)
    profile = cfg.model_connections.active_profile
    assert profile is not None
    assert profile.auth.method == "device-login"


def test_explicitly_disabled_legacy_agent_produces_no_active_profile(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "agent:\n  enabled: false\n  provider: ollama\n  model: llama3\n  base_url: http://x:11434\n",
    )
    cfg = load_config(path)
    assert cfg.model_connections.active is None
    assert "default" in cfg.model_connections.profiles


def test_new_shape_wins_over_legacy_with_a_warning(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
agent:
  provider: ollama
  model: llama3
  base_url: http://localhost:11434
  active: production
  profiles:
    production:
      model: openai/gpt-4o
""",
    )
    cfg = load_config(path)
    assert set(cfg.model_connections.profiles) == {"production"}
    assert any("legacy" in warning for warning in cfg.warnings)


@pytest.mark.parametrize(
    ("azure_endpoint", "expected_path"),
    [
        (
            "https://x.openai.azure.com",
            "/openai/deployments/gpt-4o/chat/completions",
        ),
        (
            "https://x.openai.azure.com/openai/deployments/my-dep",
            "/openai/deployments/my-dep/openai/chat/completions",
        ),
        (
            "https://x.openai.azure.com/openai/v1",
            "/openai/v1/openai/deployments/gpt-4o/chat/completions",
        ),
    ],
)
def test_the_azure_sdk_builds_the_url_from_the_resource_root(
    azure_endpoint: str, expected_path: str
) -> None:
    """Why `_migrate_azure_endpoint` strips the deployment segment.

    Only the first row is a working URL. The other two are what an
    operator's pre-migration `base_url` produces once the request is built
    by the SDK instead of by korvid's own string concatenation: the SDK
    treats `azure_endpoint` as the *resource root*, appends `/openai`, and
    inserts `/deployments/<model>` only when the resulting path does not
    already contain a deployment segment.
    """
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    seen: list[str] = []

    def _capture(request: Any) -> Any:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    client = openai.AzureOpenAI(
        azure_endpoint=azure_endpoint,
        api_key="not-a-real-key",
        api_version="2024-10-21",
        http_client=httpx.Client(transport=httpx.MockTransport(_capture)),
    )
    client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    assert len(seen) == 1
    assert seen[0].startswith(f"https://x.openai.azure.com{expected_path}?")
    assert "api-version=2024-10-21" in seen[0]


# ---------------------------------------------------------------------------
# Task 3 tests: profile writer and derived legacy scalars
# ---------------------------------------------------------------------------


def test_saving_writes_only_the_new_shape_and_drops_the_legacy_keys(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
kube_context: prod
agent:
  provider: ollama
  model: llama3
  base_url: http://localhost:11434
  enabled: true
  ollama:
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
    assert after.profiles["main"].options["nested"]["depth"] == 1
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


def test_derived_scalars_refuse_a_prefix_the_legacy_transport_cannot_serve(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: anthropic/claude-sonnet-4-5
""",
    )
    cfg = load_config(path)
    assert cfg.agent_enabled is False
    assert cfg.agent_provider is None
    assert any("anthropic" in w and "Task 15" in w for w in cfg.warnings)


def test_derived_scalars_reattach_the_azure_deployment_path(tmp_path: Path) -> None:
    """Group 1's transport is still the legacy string-concatenating one,
    which posts to `<base_url>/chat/completions`. The profile holds the
    resource root, so the projection has to rebuild what Task 2 stripped
    or every interim Azure request 404s."""
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: azure/gpt-4o
      endpoint: https://x.openai.azure.com
      auth:
        method: environment
        key: AZURE_OPENAI_API_KEY
      options:
        azure_deployment: my-dep
""",
    )
    cfg = load_config(path)
    assert cfg.agent_provider == "azure"
    assert cfg.agent_base_url == "https://x.openai.azure.com/openai/deployments/my-dep"


def test_a_profile_with_a_config_error_yields_no_legacy_scalars(tmp_path: Path) -> None:
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
    assert cfg.agent_enabled is False
    assert any("rejected" in w for w in cfg.warnings)


# ---------------------------------------------------------------------------
# The shared interim projection
# ---------------------------------------------------------------------------


def test_the_shared_projection_is_the_one_startup_derives_its_scalars_from(
    tmp_path: Path,
) -> None:
    """Startup and every later profile switch must reach the same scalars.

    `project_legacy_transport` is that single path: a second, parallel
    projection is how an Azure deployment URL or an unsupported prefix
    ends up handled one way at startup and another way at `:ai` time.
    """
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: azure/gpt-4o
      endpoint: https://x.openai.azure.com
      auth:
        method: environment
        key: AZURE_OPENAI_API_KEY
      options:
        azure_deployment: my-dep
""",
    )
    cfg = load_config(path)
    projection, refusal = project_legacy_transport(cfg.model_connections.profiles["main"])

    assert refusal is None
    assert projection is not None
    assert projection.provider == cfg.agent_provider
    assert projection.base_url == cfg.agent_base_url
    assert projection.model == cfg.agent_model
    assert projection.api_key_env == cfg.agent_api_key_env
    assert projection.auth_method == cfg.agent_auth_method


def test_the_shared_projection_refuses_a_prefix_the_legacy_transport_cannot_serve() -> None:
    projection, refusal = project_legacy_transport(
        ModelConnectionConfig(model="anthropic/claude-sonnet-4-5")
    )

    assert projection is None
    assert refusal is not None
    assert "anthropic" in refusal
    assert "Task 15" in refusal


def test_the_shared_projection_refuses_a_reference_without_a_provider_prefix() -> None:
    projection, refusal = project_legacy_transport(ModelConnectionConfig(model="bare"))

    assert projection is None
    assert refusal is not None
    assert "prefix" in refusal


def test_the_shared_projection_refuses_a_profile_with_a_config_error() -> None:
    broken = ModelConnectionConfig(model="openai/gpt-4o", options={"bad": object()})
    assert broken.config_error is not None  # the fixture is the precondition

    projection, refusal = project_legacy_transport(broken)

    assert projection is None
    assert refusal is not None
    assert "rejected" in refusal


# ---------------------------------------------------------------------------
# Auth methods: the common ids the transport actually speaks
# ---------------------------------------------------------------------------


def test_the_projection_translates_environment_auth_into_the_transports_api_key() -> None:
    """`environment` is the profile vocabulary; the interim transport only
    knows `api_key`. Handing it the profile spelling verbatim makes
    `build_credentials` reject it as an unknown method and disables an
    agent the operator configured correctly."""
    profile = ModelConnectionConfig(
        model="openai/gpt-4o",
        endpoint="https://api.example/v1",
        auth=ConnectionAuthConfig(method="environment", settings={"key": "OPENAI_API_KEY"}),
    )
    projection, refusal = project_legacy_transport(profile)

    assert refusal is None
    assert projection is not None
    assert projection.auth_method == "api_key"
    assert projection.api_key_env == "OPENAI_API_KEY"


def test_the_projection_refuses_environment_auth_without_a_variable_name() -> None:
    """`api_key` with no variable name is not a connection: the transport
    would raise "api_key_env is not set" from inside the provider factory
    instead of the profile being refused where the reason is known."""
    profile = ModelConnectionConfig(
        model="openai/gpt-4o",
        auth=ConnectionAuthConfig(method="environment", settings={}),
    )
    projection, refusal = project_legacy_transport(profile)

    assert projection is None
    assert refusal is not None
    assert "environment" in refusal


def test_the_projection_maps_azure_provider_default_onto_entra() -> None:
    """Azure's SDK credential chain *is* Entra ID, which the interim
    transport speaks."""
    profile = ModelConnectionConfig(
        model="azure/gpt-4o",
        endpoint="https://x.openai.azure.com",
        auth=ConnectionAuthConfig(method="provider-default"),
    )
    projection, refusal = project_legacy_transport(profile)

    assert refusal is None
    assert projection is not None
    assert projection.auth_method == "entra"
    assert projection.api_key_env is None


def test_the_projection_refuses_provider_default_for_a_non_azure_provider() -> None:
    """There is no SDK credential chain behind the bearer-token client:
    silently downgrading to an unauthenticated request would connect as
    nobody, or leak whatever `api_key_env` happened to be lying around."""
    profile = ModelConnectionConfig(
        model="openai/gpt-4o",
        endpoint="https://api.example/v1",
        auth=ConnectionAuthConfig(method="provider-default"),
    )
    projection, refusal = project_legacy_transport(profile)

    assert projection is None
    assert refusal is not None
    assert "provider-default" in refusal


def test_the_projection_keeps_none_auth_as_none() -> None:
    profile = ModelConnectionConfig(
        model="ollama/llama3", endpoint="http://localhost:11434", auth=ConnectionAuthConfig()
    )
    projection, refusal = project_legacy_transport(profile)

    assert refusal is None
    assert projection is not None
    assert projection.auth_method == "none"


def test_the_projection_allows_device_login_only_for_github_copilot() -> None:
    profile = ModelConnectionConfig(
        model="github-copilot/gpt-4o",
        auth=ConnectionAuthConfig(method="device-login"),
    )
    projection, refusal = project_legacy_transport(profile)

    assert refusal is None
    assert projection is not None
    assert projection.auth_method == "device-login"


def test_the_projection_refuses_device_login_for_any_other_provider() -> None:
    """Only the Copilot adapter has a device-login token store; any other
    provider would be built with no credential at all."""
    profile = ModelConnectionConfig(
        model="openai/gpt-4o",
        endpoint="https://api.example/v1",
        auth=ConnectionAuthConfig(method="device-login"),
    )
    projection, refusal = project_legacy_transport(profile)

    assert projection is None
    assert refusal is not None
    assert "device-login" in refusal


def test_the_projection_refuses_keyring_auth_with_a_reason() -> None:
    """The interim transport has no keyring reader. Refusing names the
    reason; passing `keyring` through names none."""
    profile = ModelConnectionConfig(
        model="openai/gpt-4o",
        endpoint="https://api.example/v1",
        auth=ConnectionAuthConfig(method="keyring"),
    )
    projection, refusal = project_legacy_transport(profile)

    assert projection is None
    assert refusal is not None
    assert "keyring" in refusal


def test_the_projection_refuses_an_auth_method_it_does_not_know() -> None:
    profile = ModelConnectionConfig(
        model="openai/gpt-4o",
        endpoint="https://api.example/v1",
        auth=ConnectionAuthConfig(method="mtls"),
    )
    projection, refusal = project_legacy_transport(profile)

    assert projection is None
    assert refusal is not None
    assert "mtls" in refusal


def test_an_environment_profile_disables_nothing_at_startup(tmp_path: Path) -> None:
    """The whole point of the mapping: a profile written by the wizard has
    to come up enabled, with the scalars the transport speaks."""
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
    assert cfg.agent_auth_method == "api_key"
    assert cfg.agent_api_key_env == "OPENAI_API_KEY"
    assert cfg.warnings == ()


def test_a_keyring_profile_disables_the_agent_with_the_keyring_reason(tmp_path: Path) -> None:
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
        method: keyring
""",
    )
    cfg = load_config(path)

    assert cfg.agent_enabled is False
    assert any("keyring" in warning for warning in cfg.warnings)


def test_a_profile_never_carries_the_secret_itself(tmp_path: Path) -> None:
    """`auth.key` is a variable *name*. Nothing in the config layer reads
    the environment, so a projection cannot move a secret value into the
    file or into a rendered profile."""
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
    projection, _refusal = project_legacy_transport(cfg.model_connections.profiles["main"])

    assert projection is not None
    assert projection.api_key_env == "OPENAI_API_KEY"
    assert "sk-" not in repr(projection)
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
