import json
import os
from pathlib import Path

import pytest
import yaml

from korvid.core.config import (
    ConfigMigrationError,
    KorvidConfig,
    load_config,
    save_agent_config,
)
from tests.platforms import POSIX


def _load_agent_options_config(tmp_path: Path, options: object) -> KorvidConfig:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"agent": {"options": options}}, sort_keys=False))
    return load_config(path)


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg == KorvidConfig()
    assert cfg.agent_enabled is False  # no provider -> agent off


def test_load_from_yaml(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text(
        "kube_context: prod\n"
        "namespace: default\n"
        "agent:\n  provider: anthropic\n"
        "keybindings:\n  quit: q\n"
    )
    cfg = load_config(f)
    assert cfg.kube_context == "prod"
    assert cfg.namespace == "default"
    assert cfg.agent_provider == "anthropic"
    assert cfg.agent_enabled is True  # provider present -> auto-enabled
    assert cfg.keybindings == {"quit": "q"}


def test_timeline_config_defaults(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("")

    cfg = load_config(cfg_file)

    assert cfg.timeline_max_entries == 500
    assert cfg.timeline_max_bytes == 262144


def test_timeline_config_parses_nested_values(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("timeline:\n  max_entries: 32\n  max_bytes: 8192\n")

    cfg = load_config(cfg_file)

    assert cfg.timeline_max_entries == 32
    assert cfg.timeline_max_bytes == 8192


def test_timeline_config_invalid_values_warn_and_fallback(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("timeline:\n  max_entries: 0\n  max_bytes: nope\n")

    cfg = load_config(cfg_file)

    assert cfg.timeline_max_entries == 500
    assert cfg.timeline_max_bytes == 262144
    assert any("timeline.max_entries" in warning for warning in cfg.warnings)
    assert any("timeline.max_bytes" in warning for warning in cfg.warnings)


def test_explicit_agent_off_wins(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("agent:\n  provider: anthropic\n  enabled: false\n")
    cfg = load_config(f)
    assert cfg.agent_enabled is False  # explicit off switch (design doc §6.3-4)


def test_agent_follow_defaults_on_and_only_explicit_false_disables(tmp_path: Path) -> None:
    """`agent.follow` mirrors the agent's cluster reads on screen; it is on
    by default (small models rarely volunteer the UI tools) and only a
    literal `false` disables it."""
    assert KorvidConfig().agent_follow is True
    f = tmp_path / "config.yaml"
    f.write_text("agent:\n  provider: anthropic\n  follow: false\n")
    assert load_config(f).agent_follow is False
    f.write_text("agent:\n  provider: anthropic\n  follow: banana\n")
    assert load_config(f).agent_follow is True


def test_readonly_defaults_false_and_loads_from_yaml(tmp_path: Path) -> None:
    assert KorvidConfig().readonly is False
    f = tmp_path / "config.yaml"
    f.write_text("readonly: true\n")
    assert load_config(f).readonly is True
    f.write_text("readonly: banana\n")  # only a literal true enables it
    assert load_config(f).readonly is False


# ---------------------------------------------------------------------------
# log_buffer_lines
# ---------------------------------------------------------------------------


def test_log_buffer_lines_default_is_5000() -> None:
    assert KorvidConfig().log_buffer_lines == 5000


def test_log_buffer_lines_from_yaml(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("log_buffer_lines: 20000\n")
    assert load_config(cfg_file).log_buffer_lines == 20000


def test_log_buffer_lines_invalid_falls_back(tmp_path: Path) -> None:
    for bad in ("log_buffer_lines: banana\n", "log_buffer_lines: -3\n", "log_buffer_lines: 0\n"):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(bad)
        assert load_config(cfg_file).log_buffer_lines == 5000


def test_log_buffer_lines_bool_falls_back(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("log_buffer_lines: true\n")
    assert load_config(cfg_file).log_buffer_lines == 5000


def test_agent_provider_settings_parsed(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent:\n  provider: openai-compat\n  base_url: http://localhost:11434/v1\n"
        "  model: llama3\n  api_key_env: MY_KEY\n"
    )
    cfg = load_config(p)
    assert cfg.agent_provider == "openai-compat"
    assert cfg.agent_base_url == "http://localhost:11434/v1"
    assert cfg.agent_model == "llama3"
    assert cfg.agent_api_key_env == "MY_KEY"


def test_agent_settings_default_none(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("namespace: default\n")
    cfg = load_config(p)
    assert cfg.agent_base_url is None
    assert cfg.agent_model is None
    assert cfg.agent_api_key_env is None


def test_auth_method_parsed(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: github-copilot\n  auth:\n    method: device-login\n")
    cfg = load_config(p)
    assert cfg.agent_auth_method == "device-login"


def test_auth_method_backcompat_api_key(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: openai-compat\n  api_key_env: K\n")
    assert load_config(p).agent_auth_method == "api_key"


def test_auth_method_backcompat_none(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: ollama\n")
    assert load_config(p).agent_auth_method == "none"


def test_save_agent_config_preserves_other_keys(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("namespace: prod\nlog_buffer_lines: 9000\n")
    save_agent_config(
        p,
        provider="github-copilot",
        auth_method="device-login",
        base_url="https://api.githubcopilot.com",
        model="gpt-4o",
        api_key_env=None,
    )
    cfg = load_config(p)
    assert cfg.namespace == "prod"
    assert cfg.log_buffer_lines == 9000
    assert cfg.agent_provider == "github-copilot"
    assert cfg.agent_auth_method == "device-login"


def test_agent_options_parses_valid_nested_data(tmp_path: Path) -> None:
    cfg = _load_agent_options_config(
        tmp_path,
        {
            "tenant": "platform",
            "enabled": True,
            "retries": 3,
            "temperature": 0.5,
            "headers": {"x_scope": "cluster-readonly", "mode": None},
            "fallbacks": ["gpt-4o-mini", {"label": "backup", "weight": 1}],
        },
    )
    assert cfg.agent_options == {
        "tenant": "platform",
        "enabled": True,
        "retries": 3,
        "temperature": 0.5,
        "headers": {"x_scope": "cluster-readonly", "mode": None},
        "fallbacks": ["gpt-4o-mini", {"label": "backup", "weight": 1}],
    }
    assert cfg.agent_options_error is None


def test_agent_options_depth_limit_is_exact(tmp_path: Path) -> None:
    valid = _load_agent_options_config(
        tmp_path,
        {"l1": {"l2": {"l3": {"l4": "ok"}}}},
    )
    assert valid.agent_options_error is None
    assert valid.agent_options["l1"] == {"l2": {"l3": {"l4": "ok"}}}

    invalid = _load_agent_options_config(
        tmp_path,
        {"l1": {"l2": {"l3": {"l4": {"l5": "boom"}}}}},
    )
    assert invalid.agent_options == {}
    assert invalid.agent_options_error is not None
    assert "max depth 4" in invalid.agent_options_error


def test_agent_options_mixed_container_depth_limit_is_exact(tmp_path: Path) -> None:
    valid = _load_agent_options_config(
        tmp_path,
        {"l1": [{"l3": ["ok"]}]},
    )
    assert valid.agent_options_error is None
    assert valid.agent_options["l1"] == [{"l3": ["ok"]}]

    invalid = _load_agent_options_config(
        tmp_path,
        {"l1": [{"l3": [{"l5": "boom"}]}]},
    )
    assert invalid.agent_options == {}
    assert invalid.agent_options_error is not None
    assert "max depth 4" in invalid.agent_options_error


def test_agent_options_mapping_key_limit_is_exact(tmp_path: Path) -> None:
    valid = _load_agent_options_config(tmp_path, {f"k{i}": i for i in range(64)})
    assert valid.agent_options_error is None
    assert len(valid.agent_options) == 64

    invalid = _load_agent_options_config(tmp_path, {f"k{i}": i for i in range(65)})
    assert invalid.agent_options == {}
    assert invalid.agent_options_error is not None
    assert "64 mapping keys" in invalid.agent_options_error


def test_agent_options_list_item_limit_is_exact(tmp_path: Path) -> None:
    valid = _load_agent_options_config(tmp_path, {"models": list(range(64))})
    assert valid.agent_options_error is None
    assert valid.agent_options["models"] == list(range(64))

    invalid = _load_agent_options_config(tmp_path, {"models": list(range(65))})
    assert invalid.agent_options == {}
    assert invalid.agent_options_error is not None
    assert "64 list items" in invalid.agent_options_error


def test_agent_options_string_limit_is_exact(tmp_path: Path) -> None:
    valid = _load_agent_options_config(tmp_path, {"prompt": "x" * 2048})
    assert valid.agent_options_error is None
    assert valid.agent_options["prompt"] == "x" * 2048

    invalid = _load_agent_options_config(tmp_path, {"prompt": "x" * 2049})
    assert invalid.agent_options == {}
    assert invalid.agent_options_error is not None
    assert "2048 bytes" in invalid.agent_options_error


def test_agent_options_serialized_budget_limit_is_exact(tmp_path: Path) -> None:
    valid_options = {f"k{i}": "x" * 2048 for i in range(7)} | {"k7": "x" * 1983}
    assert (
        len(
            json.dumps(
                valid_options,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        == 16 * 1024
    )
    valid = _load_agent_options_config(tmp_path, valid_options)
    assert valid.agent_options_error is None

    invalid = _load_agent_options_config(tmp_path, valid_options | {"k7": "x" * 1984})
    assert invalid.agent_options == {}
    assert invalid.agent_options_error is not None
    assert "16384 bytes" in invalid.agent_options_error


def test_agent_options_rejects_nonfinite_float(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("agent:\n  options:\n    temperature: .nan\n")
    cfg = load_config(path)
    assert cfg.agent_options == {}
    assert cfg.agent_options_error is not None
    assert "finite float" in cfg.agent_options_error


def test_agent_options_rejects_non_string_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("agent:\n  options:\n    1: one\n")
    cfg = load_config(path)
    assert cfg.agent_options == {}
    assert cfg.agent_options_error is not None
    assert "string keys" in cfg.agent_options_error


def test_agent_options_rejects_unsupported_objects(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("agent:\n  options:\n    launched: 2026-08-04\n")
    cfg = load_config(path)
    assert cfg.agent_options == {}
    assert cfg.agent_options_error is not None
    assert "date" in cfg.agent_options_error


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("secret", "secret"),
        ("db_password", "password"),
        ("access_token", "token"),
        ("api-key", "api_key"),
        ("Authorization", "authorization"),
        ("credential", "credential"),
        # Compound underscore keys
        ("client_api_key", "api_key"),
        ("my_api_key_rotation", "api_key"),
        # CamelCase JSON-style keys (finding round 5)
        ("apiKey", "api_key"),
        ("clientSecret", "secret"),
        ("accessToken", "token"),
        ("APIKey", "api_key"),
        ("clientAPIKey", "api_key"),
        # Compact lowercase form (finding round 6)
        ("apikey", "apikey"),
        ("my_apikey", "apikey"),
    ],
)
def test_agent_options_rejects_secret_key_segments(tmp_path: Path, key: str, expected: str) -> None:
    cfg = _load_agent_options_config(tmp_path, {key: "plain-text-secret"})
    assert cfg.agent_options == {}
    assert cfg.agent_options_error is not None
    assert expected in cfg.agent_options_error


@pytest.mark.parametrize(
    "key",
    [
        "clientKey",
        "apiVersion",
        "timeout",
        "modelName",
        "baseURL",
        "maxRetries",
    ],
)
def test_agent_options_accepts_non_secret_camel_case_keys(tmp_path: Path, key: str) -> None:
    """Non-secret CamelCase keys must pass through without false positives."""
    cfg = _load_agent_options_config(tmp_path, {key: "v"})
    assert cfg.agent_options_error is None
    assert key in cfg.agent_options


def test_agent_options_accepts_non_secret_compound_keys(tmp_path: Path) -> None:
    """Compound keys that do NOT contain a reserved segment must pass."""
    cfg = _load_agent_options_config(
        tmp_path, {"client_key": "v", "api_version": "v2", "timeout": "30"}
    )
    assert cfg.agent_options_error is None
    assert "client_key" in cfg.agent_options
    assert "api_version" in cfg.agent_options


def test_agent_options_non_ascii_macron_password_rejected_as_non_ascii(tmp_path: Path) -> None:
    """Non-ASCII 'pāssword' (macron a) is now rejected at the ASCII gate
    before secret detection — this is the intended v1 policy (finding #9)."""
    cfg = _load_agent_options_config(tmp_path, {"pāssword": "plain-text-secret"})
    assert cfg.agent_options == {}
    assert cfg.agent_options_error is not None
    assert "ASCII" in cfg.agent_options_error


def test_agent_options_key_byte_limit_exact(tmp_path: Path) -> None:
    """Finding #3: mapping keys must respect the 2048 UTF-8 byte limit."""
    key_at_limit = "k" * 2048
    valid = _load_agent_options_config(tmp_path, {key_at_limit: "v"})
    assert valid.agent_options_error is None
    assert key_at_limit in valid.agent_options

    key_over_limit = "k" * 2049
    invalid = _load_agent_options_config(tmp_path, {key_over_limit: "v"})
    assert invalid.agent_options == {}
    assert invalid.agent_options_error is not None
    assert "2048 bytes" in invalid.agent_options_error


def test_pathological_separator_heavy_key_accepted(tmp_path: Path) -> None:
    """A 2KiB key with many separators (producing many parts) must be
    accepted without quadratic blowup. This tests the bounded sliding-window
    algorithm does not materialize O(n^2) subsequences."""
    # 2048 bytes, alternating single-char tokens with separators: a_b_c_d_...
    # produces ~1024 parts — pathological for O(n^2) but O(n) for sliding window.
    key = "_".join("x" for _ in range(1024))  # "x_x_x_..." = 2047 chars
    assert len(key.encode("utf-8")) <= 2048
    cfg = _load_agent_options_config(tmp_path, {key: "v"})
    assert cfg.agent_options_error is None
    assert key in cfg.agent_options


def test_agent_options_rejects_non_ascii_keys(tmp_path: Path) -> None:
    """Finding #9: non-ASCII option keys are rejected before normalization."""
    # Cyrillic confusable: U+043E instead of Latin 'o'
    cfg = _load_agent_options_config(tmp_path, {"t\u043eken": "val"})
    assert cfg.agent_options == {}
    assert cfg.agent_options_error is not None
    assert "ASCII" in cfg.agent_options_error


def test_agent_options_rejects_greek_lookalike_key(tmp_path: Path) -> None:
    """Finding #9: Greek lookalike option key is rejected."""
    # Greek U+03B1 in key
    cfg = _load_agent_options_config(tmp_path, {"\u03b1pi_key": "val"})
    assert cfg.agent_options == {}
    assert cfg.agent_options_error is not None
    assert "ASCII" in cfg.agent_options_error


def test_agent_options_accepts_ascii_keys(tmp_path: Path) -> None:
    """Finding #9: pure ASCII keys are still accepted."""
    cfg = _load_agent_options_config(tmp_path, {"tenant": "platform", "region_code": "apac"})
    assert cfg.agent_options_error is None
    assert cfg.agent_options == {"tenant": "platform", "region_code": "apac"}


def test_agent_options_non_ascii_values_accepted(tmp_path: Path) -> None:
    """Finding #9: non-ASCII restriction must NOT affect values."""
    cfg = _load_agent_options_config(tmp_path, {"greeting": "こんにちは"})
    assert cfg.agent_options_error is None
    assert cfg.agent_options == {"greeting": "こんにちは"}


def test_save_agent_config_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "c.yaml"
    save_agent_config(
        p,
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434/v1",
        model="llama3",
        api_key_env=None,
    )
    assert load_config(p).agent_provider == "ollama"


def test_save_agent_config_preserves_agent_extension_keys(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: ollama\n  custom_note: keepme\n  model: llama3\n")
    save_agent_config(
        p,
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434/v1",
        model="llama3",
        api_key_env=None,
    )
    import yaml

    raw = yaml.safe_load(p.read_text())
    assert raw["agent"]["custom_note"] == "keepme"  # unrelated agent key kept


def test_save_agent_config_preserves_agent_options(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "agent": {
                    "provider": "custom-provider",
                    "model": "cluster-brain",
                    "options": {
                        "tenant": "platform",
                        "scopes": ["read", "write"],
                        "nested": {"region": "apac"},
                    },
                }
            },
            sort_keys=False,
        )
    )
    save_agent_config(
        p,
        provider="custom-provider",
        auth_method="none",
        base_url="https://llm.internal/v1",
        model="cluster-brain-v2",
        api_key_env=None,
    )
    raw = yaml.safe_load(p.read_text())
    assert raw["agent"]["options"] == {
        "tenant": "platform",
        "scopes": ["read", "write"],
        "nested": {"region": "apac"},
    }
    cfg = load_config(p)
    assert cfg.agent_options == raw["agent"]["options"]
    assert cfg.agent_options_error is None


def test_save_agent_config_drops_stale_optional_fields(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        "agent:\n  provider: openai-compat\n  base_url: https://x/v1\n"
        "  model: m\n  api_key_env: K\n"
    )
    save_agent_config(
        p,
        provider="github-copilot",
        auth_method="device-login",
        base_url=None,
        model="gpt-4o",
        api_key_env=None,
    )
    import yaml

    agent = yaml.safe_load(p.read_text())["agent"]
    assert "base_url" not in agent
    assert "api_key_env" not in agent


def test_scalar_auth_value_does_not_crash(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: ollama\n  model: llama3\n  auth: none\n")
    cfg = load_config(p)  # must not raise AttributeError
    assert cfg.agent_auth_method == "none"


def test_backcompat_github_copilot_defaults_to_device_login(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: github-copilot\n  model: gpt-4o\n")
    cfg = load_config(p)
    assert cfg.agent_auth_method == "device-login"


def test_save_agent_config_clears_explicit_disable(tmp_path: Path) -> None:
    """Completing the wizard is a user-confirmed enable: a stale
    `agent.enabled: false` must not silently override it after restart."""
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: ollama\n  enabled: false\n  model: llama3\n")
    save_agent_config(
        p,
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434/v1",
        model="llama3",
        api_key_env=None,
    )
    import yaml

    assert "enabled" not in yaml.safe_load(p.read_text())["agent"]


def test_save_agent_config_interrupted_write_preserves_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure mid-write (disk full / crash) must not truncate the user's
    config: the previous file content has to survive intact."""
    import korvid.core.config as cfg_mod

    p = tmp_path / "c.yaml"
    p.write_text("keybindings:\n  q: quit\nagent:\n  provider: ollama\n  model: llama3\n")

    def failing_fsync(fd: int) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(cfg_mod, "os_fsync", failing_fsync)
        with pytest.raises(OSError, match="disk full"):
            save_agent_config(
                p,
                provider="openai",
                auth_method="api-key",
                base_url=None,
                model="gpt-4o",
                api_key_env="OPENAI_API_KEY",
            )
    cfg = load_config(p)  # must still parse as the pre-save configuration
    assert cfg.keybindings == {"q": "quit"}
    assert cfg.agent_provider == "ollama"


def test_save_agent_config_preserves_restrictive_file_mode(tmp_path: Path) -> None:
    """Atomic replacement must not widen an existing 0600 config to the
    umask-derived default, exposing preserved values.

    On POSIX we verify the effective stat mode. On Windows/NTFS, Python's
    POSIX-mode emulation does not enforce real file permissions; the code
    calls os.chmod(tmp, mode) before os.replace — we verify via spy that
    the code *requests* the restrictive mode.
    """
    import stat

    import korvid.core.config as cfg_mod

    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: ollama\n  model: llama3\n")
    os.chmod(p, 0o600)

    if POSIX:
        save_agent_config(
            p,
            provider="ollama",
            auth_method="none",
            base_url=None,
            model="llama3",
            api_key_env=None,
        )
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    else:
        # Windows: stat mode doesn't reflect POSIX bits; spy on os.chmod
        # to prove the code requests 0o600.
        chmod_calls: list[tuple[object, int]] = []
        real_chmod = os.chmod

        def spy_chmod(path: Path, mode: int) -> None:
            chmod_calls.append((path, mode))
            real_chmod(path, mode)

        from unittest.mock import patch

        with patch.object(cfg_mod, "os_chmod", spy_chmod):
            save_agent_config(
                p,
                provider="ollama",
                auth_method="none",
                base_url=None,
                model="llama3",
                api_key_env=None,
            )
        assert any(mode == 0o600 for _, mode in chmod_calls)


def test_save_agent_config_preserves_auth_extension_keys(tmp_path: Path) -> None:
    """The read-modify-write contract applies to nested `agent.auth` too:
    only `method` is managed, unrelated auth keys must survive."""
    import yaml

    p = tmp_path / "c.yaml"
    p.write_text(
        "agent:\n  provider: ollama\n  model: llama3\n  auth:\n    method: none\n    tenant_id: contoso\n"
    )
    save_agent_config(
        p,
        provider="ollama",
        auth_method="api-key",
        base_url=None,
        model="llama3",
        api_key_env="MY_KEY",
    )
    auth = yaml.safe_load(p.read_text())["agent"]["auth"]
    assert auth["method"] == "api-key"
    assert auth["tenant_id"] == "contoso"


def test_save_agent_config_fsyncs_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Data must reach disk before the rename, or a power loss can leave an
    empty/old file behind (ext4 delayed allocation)."""
    import korvid.core.config as cfg_mod

    calls: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def spy_fsync(fd: int) -> None:
        # Windows' fsync (_commit) requires a writable handle: syncing an
        # O_RDONLY fd raises there, so the implementation must sync the fd
        # it wrote through. fcntl itself is POSIX-only, so guard the check.
        if POSIX:
            import fcntl

            assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
        calls.append("fsync")
        real_fsync(fd)

    def spy_replace(src: object, dst: object) -> None:
        calls.append("replace")
        real_replace(src, dst)  # type: ignore[arg-type]  # spy forwards Path args

    with monkeypatch.context() as m:
        m.setattr(cfg_mod, "os_fsync", spy_fsync)
        m.setattr(cfg_mod, "os_replace", spy_replace)
        save_agent_config(
            tmp_path / "c.yaml",
            provider="ollama",
            auth_method="none",
            base_url=None,
            model="llama3",
            api_key_env=None,
        )
    assert calls == ["fsync", "replace"]


def test_save_agent_config_does_not_clobber_foreign_tmp(tmp_path: Path) -> None:
    """The temp file name must be unique so two concurrent processes cannot
    race on (and delete) each other's temp file."""
    p = tmp_path / "c.yaml"
    foreign = tmp_path / "c.yaml.tmp"
    foreign.write_text("owned by another process")
    save_agent_config(
        p,
        provider="ollama",
        auth_method="none",
        base_url=None,
        model="llama3",
        api_key_env=None,
    )
    assert foreign.read_text() == "owned by another process"


def test_mcp_defaults_off(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.mcp_enabled is False
    assert cfg.mcp_port == 7878


def test_mcp_write_proposals_default_off(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("mcp:\n  enabled: true\n")
    cfg = load_config(f)
    assert cfg.mcp_write_proposals is False


def test_mcp_follow_default_off(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("mcp:\n  enabled: true\n")
    cfg = load_config(f)
    assert cfg.mcp_follow is False


def test_mcp_follow_requires_literal_true(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text('mcp:\n  follow: "yes"\n')
    cfg = load_config(f)
    assert cfg.mcp_follow is False


def test_mcp_follow_enabled(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("mcp:\n  follow: true\n")
    cfg = load_config(f)
    assert cfg.mcp_follow is True


def test_mcp_write_proposals_requires_literal_true(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text('mcp:\n  write_proposals: "yes"\n')
    cfg = load_config(f)
    assert cfg.mcp_write_proposals is False


def test_mcp_write_proposals_enabled(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("mcp:\n  enabled: true\n  write_proposals: true\n")
    cfg = load_config(f)
    assert cfg.mcp_write_proposals is True


def test_mcp_loaded_from_yaml(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("mcp:\n  enabled: true\n  port: 9000\n")
    cfg = load_config(f)
    assert cfg.mcp_enabled is True
    assert cfg.mcp_port == 9000


def test_mcp_section_tolerates_scalars_and_bad_port(tmp_path: Path) -> None:
    """User-edited configs can hold scalars where mappings are expected and
    junk where numbers are expected - fall back to safe defaults."""
    f = tmp_path / "config.yaml"
    f.write_text("mcp: yes\n")
    cfg = load_config(f)
    assert cfg.mcp_enabled is False
    assert cfg.mcp_port == 7878
    f.write_text("mcp:\n  enabled: true\n  port: not-a-port\n")
    cfg = load_config(f)
    assert cfg.mcp_enabled is True
    assert cfg.mcp_port == 7878


def test_mcp_port_rejects_fractional_and_infinite_floats(tmp_path: Path) -> None:
    """int() would truncate 7878.9 and raise OverflowError on .inf - both
    must fall back to the default instead."""
    for raw in ("7878.9", ".inf", ".nan"):
        path = tmp_path / f"cfg-{raw.strip('.')}.yaml"
        path.write_text(f"mcp:\n  enabled: true\n  port: {raw}\n")
        config = load_config(path)
        assert config.mcp_port == 7878


# ---------------------------------------------------------------------------
# logs section (issue #43): wrap / timestamps display defaults
# ---------------------------------------------------------------------------


def test_log_display_defaults_are_off() -> None:
    cfg = KorvidConfig()
    assert cfg.log_wrap is False
    assert cfg.log_timestamps is False


def test_log_display_from_yaml(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("logs:\n  wrap: true\n  timestamps: true\n")
    cfg = load_config(cfg_file)
    assert cfg.log_wrap is True
    assert cfg.log_timestamps is True


def test_log_display_non_bool_falls_back(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("logs:\n  wrap: banana\n  timestamps: 1\n")
    cfg = load_config(cfg_file)
    assert cfg.log_wrap is False
    assert cfg.log_timestamps is False


def test_logs_scalar_section_tolerated(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("logs: nonsense\n")
    cfg = load_config(cfg_file)
    assert cfg.log_wrap is False
    assert cfg.log_timestamps is False


# ---------------------------------------------------------------------------
# debug section (issue #52): debug image defaults for air-gapped clusters
# ---------------------------------------------------------------------------


def test_debug_defaults() -> None:
    cfg = KorvidConfig()
    assert cfg.debug_default_image is None
    assert cfg.debug_images is None  # unconfigured, not "configured empty"


def test_debug_from_yaml(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "debug:\n"
        "  default_image: registry.corp.local/tools/busybox:1.36\n"
        "  images:\n"
        "    jvm: registry.corp.local/tools/debug-jvm:latest\n"
        "    python: registry.corp.local/tools/debug-python:latest\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.debug_default_image == "registry.corp.local/tools/busybox:1.36"
    assert cfg.debug_images == {
        "jvm": "registry.corp.local/tools/debug-jvm:latest",
        "python": "registry.corp.local/tools/debug-python:latest",
    }


def test_debug_scalar_section_tolerated(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("debug: nonsense\n")
    cfg = load_config(cfg_file)
    assert cfg.debug_default_image is None
    assert cfg.debug_images is None


def test_debug_explicit_empty_images_mapping_preserved(tmp_path: Path) -> None:
    # `debug.images: {}` is a deliberate restriction (offer nothing public),
    # distinct from the key being absent entirely.
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("debug:\n  images: {}\n")
    cfg = load_config(cfg_file)
    assert cfg.debug_images == {}


def test_debug_malformed_images_value_fails_closed(tmp_path: Path) -> None:
    # A present but non-mapping debug.images is still a restriction attempt:
    # fail closed to an empty restricted mapping instead of silently
    # re-enabling public zero-config images.
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("debug:\n  images: [jvm, python]\n")
    assert load_config(cfg_file).debug_images == {}
    cfg_file.write_text("debug:\n  images: nonsense\n")
    assert load_config(cfg_file).debug_images == {}


def test_debug_non_string_entries_dropped(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "debug:\n  default_image: 7\n  images:\n    jvm: [a, b]\n    python: img:1\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.debug_default_image is None
    assert cfg.debug_images == {"python": "img:1"}


def test_node_shell_defaults() -> None:
    cfg = KorvidConfig()
    assert cfg.node_shell_image is None
    assert cfg.node_shell_namespace is None


def test_node_shell_from_yaml(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("node_shell:\n  image: registry.local/toolkit:1\n  namespace: debug-ns\n")
    cfg = load_config(f)
    assert cfg.node_shell_image == "registry.local/toolkit:1"
    assert cfg.node_shell_namespace == "debug-ns"


def test_node_shell_scalar_section_tolerated(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("node_shell: yes\n")
    cfg = load_config(f)
    assert cfg.node_shell_image is None
    assert cfg.node_shell_namespace is None


def test_node_shell_non_string_values_ignored(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("node_shell:\n  image: 3\n  namespace: ''\n")
    cfg = load_config(f)
    assert cfg.node_shell_image is None
    assert cfg.node_shell_namespace is None


def test_ollama_defaults_when_unconfigured(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n")
    cfg = load_config(p)
    assert cfg.agent_ollama_num_ctx == 16384
    assert cfg.agent_ollama_temperature == 0.0
    assert cfg.agent_ollama_seed is None
    assert cfg.agent_ollama_think is False
    assert cfg.agent_ollama_keep_alive is None


def test_ollama_options_parsed(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent:\n  provider: ollama\n  ollama:\n"
        "    num_ctx: 32768\n    temperature: 0.7\n    seed: 42\n"
        "    think: true\n    keep_alive: 10m\n"
    )
    cfg = load_config(p)
    assert cfg.agent_ollama_num_ctx == 32768
    assert cfg.agent_ollama_temperature == 0.7
    assert cfg.agent_ollama_seed == 42
    assert cfg.agent_ollama_think is True
    assert cfg.agent_ollama_keep_alive == "10m"


def test_ollama_invalid_values_fall_back(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent:\n  provider: ollama\n  ollama:\n"
        "    num_ctx: -5\n    temperature: hot\n    seed: [1]\n"
        "    think: yes please\n    keep_alive: {}\n"
    )
    cfg = load_config(p)
    assert cfg.agent_ollama_num_ctx == 16384
    assert cfg.agent_ollama_temperature == 0.0
    assert cfg.agent_ollama_seed is None
    assert cfg.agent_ollama_think is False
    assert cfg.agent_ollama_keep_alive is None


def test_ollama_keep_alive_accepts_integer_seconds(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  ollama:\n    keep_alive: 300\n")
    cfg = load_config(p)
    assert cfg.agent_ollama_keep_alive == 300


def test_ollama_non_mapping_section_is_ignored(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  ollama: not-a-mapping\n")
    cfg = load_config(p)
    assert cfg.agent_ollama_num_ctx == 16384
    assert cfg.agent_ollama_think is False


def test_ollama_inf_and_overflow_values_fall_back(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    huge = str(10**400)
    p.write_text(
        f"agent:\n  provider: ollama\n  ollama:\n    num_ctx: .inf\n    temperature: {huge}\n"
        "    seed: .inf\n"
    )
    cfg = load_config(p)
    assert cfg.agent_ollama_num_ctx == 16384
    assert cfg.agent_ollama_temperature == 0.0
    assert cfg.agent_ollama_seed is None


def test_ollama_seed_zero_is_valid(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  ollama:\n    seed: 0\n")
    cfg = load_config(p)
    assert cfg.agent_ollama_seed == 0


def test_ollama_negative_seed_falls_back(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  ollama:\n    seed: -1\n")
    cfg = load_config(p)
    assert cfg.agent_ollama_seed is None


def test_ollama_num_predict_default_is_none(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n")
    cfg = load_config(p)
    assert cfg.agent_ollama_num_predict is None


def test_ollama_num_predict_parsed(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  ollama:\n    num_predict: 192\n")
    cfg = load_config(p)
    assert cfg.agent_ollama_num_predict == 192


def test_ollama_num_predict_invalid_falls_back(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  ollama:\n    num_predict: -10\n")
    cfg = load_config(p)
    assert cfg.agent_ollama_num_predict is None
    assert any("num_predict" in w for w in cfg.warnings), cfg.warnings


def test_ollama_num_predict_zero_falls_back_with_warning(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  ollama:\n    num_predict: 0\n")
    cfg = load_config(p)
    assert cfg.agent_ollama_num_predict is None
    assert any("num_predict" in w for w in cfg.warnings), cfg.warnings


def test_ollama_num_predict_bool_falls_back_with_warning(tmp_path: Path) -> None:
    """`true`/`false` are Python `bool`, a stealth `int` subclass — a YAML
    author who typos a boolean here must not silently get `num_predict=1`."""
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  ollama:\n    num_predict: true\n")
    cfg = load_config(p)
    assert cfg.agent_ollama_num_predict is None
    assert any("num_predict" in w for w in cfg.warnings), cfg.warnings


def test_ollama_num_predict_float_falls_back_with_warning(tmp_path: Path) -> None:
    """Unlike `num_ctx`'s permissive parser, `num_predict` must reject a
    fractional value outright instead of silently truncating it — even
    when the fraction is small enough that truncation would look sane."""
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  ollama:\n    num_predict: 1.9\n")
    cfg = load_config(p)
    assert cfg.agent_ollama_num_predict is None
    assert any("num_predict" in w for w in cfg.warnings), cfg.warnings


def test_ollama_num_predict_numeric_string_falls_back_with_warning(tmp_path: Path) -> None:
    """`num_ctx` accepts a numeric string for legacy compatibility;
    `num_predict` is a new, stricter contract and must not."""
    p = tmp_path / "config.yaml"
    p.write_text('agent:\n  provider: ollama\n  ollama:\n    num_predict: "192"\n')
    cfg = load_config(p)
    assert cfg.agent_ollama_num_predict is None
    assert any("num_predict" in w for w in cfg.warnings), cfg.warnings


def test_ollama_num_predict_valid_value_has_no_warning(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  ollama:\n    num_predict: 192\n")
    cfg = load_config(p)
    assert cfg.agent_ollama_num_predict == 192
    assert not any("num_predict" in w for w in cfg.warnings), cfg.warnings


def test_ollama_num_ctx_still_accepts_a_numeric_string(tmp_path: Path) -> None:
    """`num_ctx` keeps its existing permissive (`_parse_positive_int`)
    behavior unchanged — only `num_predict` gets the stricter parser."""
    p = tmp_path / "config.yaml"
    p.write_text('agent:\n  provider: ollama\n  ollama:\n    num_ctx: "8192"\n')
    cfg = load_config(p)
    assert cfg.agent_ollama_num_ctx == 8192


# ---------------------------------------------------------------------------
# namespace scope (issue #108): legacy `namespaces:` is a migration warning;
# `favorite_namespaces:` is a UI-only shortcut list bound to keys 1-9.
# ---------------------------------------------------------------------------


def test_legacy_namespaces_key_emits_migration_warning(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("namespaces:\n  - team-a\n  - team-b\n")
    cfg = load_config(p)
    assert not hasattr(cfg, "namespaces")
    assert any("namespaces" in w and "favorite_namespaces" in w for w in cfg.warnings)


def test_favorite_namespaces_parsed(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("favorite_namespaces:\n  - team-a\n  - team-b\n")
    cfg = load_config(p)
    assert cfg.favorite_namespaces == ("team-a", "team-b")


def test_favorite_namespaces_default_empty(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("namespace: default\n")
    cfg = load_config(p)
    assert cfg.favorite_namespaces == ()
    assert cfg.warnings == ()


def test_favorite_namespaces_non_list_ignored(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("favorite_namespaces: oops\n")
    cfg = load_config(p)
    assert cfg.favorite_namespaces == ()


def test_favorite_namespaces_non_string_entries_skipped(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text('favorite_namespaces:\n  - team-a\n  - 5\n  - ""\n  - null\n')
    cfg = load_config(p)
    assert cfg.favorite_namespaces == ("team-a",)


def test_favorite_namespaces_capped_at_nine_with_warning(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    names = "\n".join(f"  - ns-{i}" for i in range(11))
    p.write_text(f"favorite_namespaces:\n{names}\n")
    cfg = load_config(p)
    assert cfg.favorite_namespaces == tuple(f"ns-{i}" for i in range(9))
    assert any("favorite_namespaces" in w for w in cfg.warnings)


# ---------------------------------------------------------------------------
# views: custom columns (issue #45)
# ---------------------------------------------------------------------------


def test_views_parses_all_three_sources(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  pods:
    columns:
      - name: TEAM
        label: team
      - name: OWNER
        annotation: owner
      - name: IMAGE
        jsonpath: .spec.containers[0].image
"""
    )
    config = load_config(cfg)
    view = config.views["pods"]
    assert [(c.name, c.source, c.expr) for c in view.columns] == [
        ("TEAM", "label", "team"),
        ("OWNER", "annotation", "owner"),
        ("IMAGE", "jsonpath", ".spec.containers[0].image"),
    ]
    assert view.replace is False


def test_views_replace_flag(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  deployments:
    replace: true
    columns:
      - name: TEAM
        label: team
"""
    )
    config = load_config(cfg)
    assert config.views["deployments"].replace is True


def test_views_invalid_jsonpath_drops_column_with_warning(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  pods:
    columns:
      - name: BAD
        jsonpath: "spec[oops"
      - name: TEAM
        label: team
"""
    )
    config = load_config(cfg)
    assert [c.name for c in config.views["pods"].columns] == ["TEAM"]
    assert any("BAD" in warning for warning in config.warnings)


def test_views_column_without_exactly_one_source_dropped(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  pods:
    columns:
      - name: NOSOURCE
      - name: TWOSOURCES
        label: team
        annotation: owner
      - name: TEAM
        label: team
"""
    )
    config = load_config(cfg)
    assert [c.name for c in config.views["pods"].columns] == ["TEAM"]
    assert len(config.warnings) == 2


def test_views_column_without_name_dropped(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  pods:
    columns:
      - label: team
"""
    )
    config = load_config(cfg)
    assert "pods" not in config.views
    assert len(config.warnings) == 1


def test_views_non_mapping_entries_ignored(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  pods: "oops"
  deployments:
    columns:
      - name: TEAM
        label: team
"""
    )
    config = load_config(cfg)
    assert set(config.views) == {"deployments"}


def test_views_absent_means_empty(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("namespace: default\n")
    config = load_config(cfg)
    assert config.views == {}
    assert config.warnings == ()


def test_views_duplicate_column_names_case_insensitive_dropped(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  pods:
    columns:
      - name: TEAM
        label: team
      - name: team
        annotation: team
"""
    )
    config = load_config(cfg)
    assert [c.name for c in config.views["pods"].columns] == ["TEAM"]
    assert any("duplicate" in w for w in config.warnings)


def test_views_synthetic_helm_kinds_rejected_with_warning(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  helmreleases:
    columns:
      - name: TEAM
        label: team
  helmrevisions:
    columns:
      - name: TEAM
        label: team
"""
    )
    config = load_config(cfg)
    assert config.views == {}
    assert len(config.warnings) == 2


def test_views_builtin_colliding_names_dropped_with_warning(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  pods:
    columns:
      - name: CPU
        label: team
      - name: TEAM
        label: team
"""
    )
    config = load_config(cfg)
    assert [c.name for c in config.views["pods"].columns] == ["TEAM"]
    assert any("built-in" in w for w in config.warnings)


def test_views_whitespace_column_name_dropped_with_warning(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  pods:
    columns:
      - name: APP VERSION
        label: version
      - name: TEAM
        label: team
"""
    )
    config = load_config(cfg)
    assert [c.name for c in config.views["pods"].columns] == ["TEAM"]
    assert any("single token" in w for w in config.warnings)


def test_views_non_list_columns_warned(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  pods:
    columns: {name: TEAM, label: team}
"""
    )
    config = load_config(cfg)
    assert "pods" not in config.views
    assert any("must be a list" in w for w in config.warnings)


def test_views_secrets_rejected_with_warning(tmp_path: Path) -> None:
    """Security invariant: Secret values only render through the masking
    pipeline — custom columns evaluate raw manifests, so the kind is banned."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  secrets:
    columns:
      - name: TOKEN
        jsonpath: .data.token
"""
    )
    config = load_config(cfg)
    assert config.views == {}
    assert any("secrets" in w and "masking" in w for w in config.warnings)


def test_views_non_mapping_view_warned(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
views:
  pods: []
"""
    )
    config = load_config(cfg)
    assert config.views == {}
    assert any("pods" in w and "mapping" in w for w in config.warnings)


def test_views_non_mapping_top_level_warned(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("views: []\n")
    config = load_config(cfg)
    assert config.views == {}
    assert any(w.startswith("views") and "mapping" in w for w in config.warnings)


# --- agent.model_tier / agent.rules (replaces agent.profile / agent.prompts) -


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_the_removed_profile_key_is_actionable(tmp_path: Path) -> None:
    """`agent.profile` was replaced by `agent.model_tier`: reloading an old
    config must fail with a one-line, actionable message rather than
    silently reinterpreting or ignoring the key."""
    path = write_config(tmp_path, "agent:\n  profile: small\n")

    with pytest.raises(
        ConfigMigrationError,
        match=r"agent\.profile was removed.*agent\.model_tier",
    ):
        load_config(path)


def test_the_removed_profile_key_is_actionable_even_when_agent_disabled(
    tmp_path: Path,
) -> None:
    """The migration error must fire regardless of whether the agent block
    would otherwise enable the agent — a stale `profile` key is a mistake
    the user needs to see either way."""
    path = write_config(tmp_path, "agent:\n  enabled: false\n  profile: full\n")

    with pytest.raises(ConfigMigrationError, match=r"agent\.profile was removed"):
        load_config(path)


def test_removed_agent_prompts_is_actionable(tmp_path: Path) -> None:
    """`agent.prompts` (system/append/tool_descriptions replacement) was
    removed entirely in favor of `agent.rules`; it must not be silently
    ignored."""
    path = write_config(tmp_path, "agent:\n  prompts:\n    system: You are terse.\n")

    with pytest.raises(
        ConfigMigrationError,
        match=r"agent\.prompts was removed.*agent\.rules",
    ):
        load_config(path)


def test_model_tier_and_additive_rules_load(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        "agent:\n  model_tier: low\n  rules:\n    - Prefer workload owner evidence.\n",
    )

    config = load_config(path)

    assert config.agent_model_tier == "low"
    assert config.agent_rules == ("Prefer workload owner evidence.",)


def test_model_tier_high_is_parsed(tmp_path: Path) -> None:
    path = write_config(tmp_path, "agent:\n  provider: ollama\n  model_tier: high\n")
    assert load_config(path).agent_model_tier == "high"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Low", "low"), ("low ", "low"), (" HIGH ", "high")],
)
def test_model_tier_normalizes_case_and_whitespace(
    tmp_path: Path, value: str, expected: str
) -> None:
    path = write_config(
        tmp_path,
        f"agent:\n  provider: ollama\n  model_tier: {value!r}\n",
    )

    assert load_config(path).agent_model_tier == expected


def test_model_tier_unset_is_none(tmp_path: Path) -> None:
    """Omitting `model_tier` means automatic routing — distinguishable from
    an explicit choice."""
    path = write_config(tmp_path, "agent:\n  provider: ollama\n")
    assert load_config(path).agent_model_tier is None


def test_model_tier_null_is_treated_as_unset(tmp_path: Path) -> None:
    """`model_tier: null` is the YAML idiom for "not set", so it must mean
    automatic routing rather than an error."""
    path = write_config(tmp_path, "agent:\n  provider: ollama\n  model_tier: null\n")
    assert load_config(path).agent_model_tier is None


@pytest.mark.parametrize("bad_value", ["full", "small", "auto", "medium"])
def test_model_tier_rejects_legacy_and_unknown_values(tmp_path: Path, bad_value: str) -> None:
    """Only `low`/`high` (or absent) are accepted — legacy `full`/`small`,
    `auto`, and typos must fail actionably instead of silently falling back
    (unlike the old `agent.profile` behavior)."""
    path = write_config(tmp_path, f"agent:\n  provider: ollama\n  model_tier: {bad_value!r}\n")

    with pytest.raises(
        ConfigMigrationError,
        match=r"agent\.model_tier must be .*low.*high",
    ):
        load_config(path)


def test_agent_rules_default_is_empty(tmp_path: Path) -> None:
    path = write_config(tmp_path, "agent:\n  provider: ollama\n")
    assert load_config(path).agent_rules == ()


def test_agent_rules_drops_blank_entries_with_a_warning(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        "agent:\n  rules:\n    - Prefer owner evidence.\n    - '   '\n",
    )
    cfg = load_config(path)
    assert cfg.agent_rules == ("Prefer owner evidence.",)
    assert any("agent.rules" in w for w in cfg.warnings), cfg.warnings


def test_agent_rules_drops_entries_over_max_length(tmp_path: Path) -> None:
    too_long = "x" * 1001
    path = write_config(tmp_path, f"agent:\n  rules:\n    - {too_long}\n")
    cfg = load_config(path)
    assert cfg.agent_rules == ()
    assert any("1000" in w or "1,000" in w for w in cfg.warnings), cfg.warnings


def test_agent_rules_caps_at_sixteen_entries(tmp_path: Path) -> None:
    lines = "\n".join(f"    - rule {i}" for i in range(20))
    path = write_config(tmp_path, f"agent:\n  rules:\n{lines}\n")
    cfg = load_config(path)
    assert len(cfg.agent_rules) == 16
    assert cfg.agent_rules[0] == "rule 0"
    assert any("16" in w for w in cfg.warnings), cfg.warnings


def test_agent_rules_non_list_value_warns_and_is_empty(tmp_path: Path) -> None:
    path = write_config(tmp_path, "agent:\n  rules: not-a-list\n")
    cfg = load_config(path)
    assert cfg.agent_rules == ()
    assert any("agent.rules" in w for w in cfg.warnings), cfg.warnings


def test_save_agent_config_writes_an_explicit_low_tier(tmp_path: Path) -> None:
    """An explicit tier override must survive a restart."""
    p = tmp_path / "c.yaml"
    save_agent_config(
        p,
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434",
        model="qwen3:8b",
        api_key_env=None,
        model_tier="low",
    )
    assert load_config(p).agent_model_tier == "low"
    assert "model_tier: low" in p.read_text()


def test_save_agent_config_writes_an_explicit_high_tier(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: ollama\n  model: qwen3:8b\n  model_tier: low\n")
    save_agent_config(
        p,
        provider="openai-compat",
        auth_method="api_key",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        model_tier="high",
    )
    assert load_config(p).agent_model_tier == "high"
    assert "model_tier: high" in p.read_text()


def test_save_agent_config_removes_tier_for_automatic_routing(tmp_path: Path) -> None:
    """Saving with `model_tier=None` (Automatic) must pop any previously
    persisted override — reopening the wizard and choosing Automatic must
    actually clear it, not leave a stale explicit choice in place."""
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: ollama\n  model: qwen3:8b\n  model_tier: low\n")
    save_agent_config(
        p,
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434",
        model="qwen3:8b",
        api_key_env=None,
        model_tier=None,
    )
    assert load_config(p).agent_model_tier is None
    assert "model_tier" not in p.read_text()


def test_save_agent_config_preserves_unrelated_keys_and_rules(tmp_path: Path) -> None:
    """`save_agent_config` only manages tier/provider/model/auth/base_url/
    api_key_env — `agent.rules` and any other unrelated keys must survive
    the read-modify-write untouched."""
    p = tmp_path / "c.yaml"
    p.write_text(
        "namespace: prod\n"
        "agent:\n"
        "  provider: ollama\n"
        "  model: qwen3:8b\n"
        "  follow: false\n"
        "  rules:\n"
        "    - Prefer workload owner evidence.\n"
    )
    save_agent_config(
        p,
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434",
        model="qwen3:14b",
        api_key_env=None,
        model_tier="high",
    )
    cfg = load_config(p)
    assert cfg.namespace == "prod"
    assert cfg.agent_follow is False
    assert cfg.agent_rules == ("Prefer workload owner evidence.",)
    assert cfg.agent_model_tier == "high"
    assert cfg.agent_model == "qwen3:14b"


# ---------------------------------------------------------------------------
# Protected contexts (issue #83)
# ---------------------------------------------------------------------------


def test_protected_contexts_default_empty(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("namespace: default\n")
    config = load_config(cfg_file)
    assert config.protected_contexts == ()
    assert config.agent_disable_in_protected is False


def test_protected_contexts_parsed(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("protected_contexts:\n  - prod-*\n  - staging-eu\n")
    config = load_config(cfg_file)
    assert config.protected_contexts == ("prod-*", "staging-eu")


def test_protected_contexts_non_list_ignored(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("protected_contexts: prod\n")
    config = load_config(cfg_file)
    assert config.protected_contexts == ()


def test_protected_contexts_non_string_entries_dropped(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("protected_contexts:\n  - prod-*\n  - 42\n  - ''\n")
    config = load_config(cfg_file)
    assert config.protected_contexts == ("prod-*",)


def test_agent_disable_in_protected_parsed(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("agent:\n  provider: ollama\n  disable_in_protected: true\n")
    config = load_config(cfg_file)
    assert config.agent_disable_in_protected is True


def test_context_is_protected_glob_match() -> None:
    from korvid.core.config import context_is_protected

    assert context_is_protected("prod-us-east", ("prod-*",)) is True
    assert context_is_protected("staging", ("prod-*",)) is False
    assert context_is_protected("prod", ("prod",)) is True


def test_context_is_protected_none_or_empty() -> None:
    from korvid.core.config import context_is_protected

    assert context_is_protected(None, ("prod-*",)) is False
    assert context_is_protected("prod-a", ()) is False


def test_telepresence_integration_defaults_on(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.telepresence_enabled is True


def test_telepresence_kill_switch(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("integrations:\n  telepresence: off\n")
    cfg = load_config(f)
    assert cfg.telepresence_enabled is False


def test_telepresence_non_boolean_stays_on(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text('integrations:\n  telepresence: "banana"\n')
    cfg = load_config(f)
    assert cfg.telepresence_enabled is True


def test_network_ca_bundle_parses(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("network:\n  ca_bundle: /etc/korvid/company-ca.pem\n")
    cfg = load_config(cfg_path)
    assert cfg.network_ca_bundle == "/etc/korvid/company-ca.pem"


def test_network_ca_bundle_defaults_to_none(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("namespace: default\n")
    assert load_config(cfg_path).network_ca_bundle is None


def test_network_section_tolerates_non_mapping(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("network: nonsense\n")
    assert load_config(cfg_path).network_ca_bundle is None


# ---------------------------------------------------------------------------
# Finding #5: Provider name canonicalization at config load
# ---------------------------------------------------------------------------


def test_provider_name_canonicalized_at_load(tmp_path: Path) -> None:
    """Config load must canonicalize provider names so github_copilot
    receives device-login default and compositions root loads OAuth."""
    f = tmp_path / "config.yaml"
    f.write_text("agent:\n  provider: github_copilot\n  model: gpt-4o\n")
    cfg = load_config(f)
    assert cfg.agent_provider == "github-copilot"
    assert cfg.agent_auth_method == "device-login"


def test_provider_name_case_variant_canonicalized(tmp_path: Path) -> None:
    """Mixed case and dot separators are canonicalized."""
    f = tmp_path / "config.yaml"
    f.write_text("agent:\n  provider: GitHub.Copilot\n  model: m\n")
    cfg = load_config(f)
    assert cfg.agent_provider == "github-copilot"
    assert cfg.agent_auth_method == "device-login"


def test_canonicalize_provider_name_parity() -> None:
    """The core _canonicalize_provider_name must produce the same output
    as providers.plugin_registry.normalize_provider_name for
    representative built-in and plugin names."""
    from korvid.core.config import _canonicalize_provider_name
    from korvid.providers.plugin_registry import normalize_provider_name

    names = [
        "github-copilot",
        "GitHub_Copilot",
        "openai_compat",
        "OpenAI.Compat",
        "OLLAMA",
        "Company_LLM",
        "  azure  ",
        "my--custom..provider",
    ]
    for name in names:
        assert _canonicalize_provider_name(name) == normalize_provider_name(name), (
            f"parity failed for {name!r}"
        )


def test_bounded_options_accept_a_previously_frozen_tuple_value() -> None:
    from korvid.core.config import _parse_bounded_options

    parsed, error = _parse_bounded_options({"stop": ("a", "b")}, root="options")
    assert error is None
    assert parsed == {"stop": ["a", "b"]}


def test_bounded_options_refuse_an_inline_secret_key() -> None:
    from korvid.core.config import _parse_bounded_options

    parsed, error = _parse_bounded_options({"api_key": "sk-inline"}, root="auth")
    assert parsed == {}
    assert error is not None
    assert "sk-inline" not in error


def test_bounded_options_name_the_caller_s_root_in_limit_messages() -> None:
    from korvid.core.config import _parse_bounded_options

    _parsed, error = _parse_bounded_options(
        {str(index): index for index in range(65)}, root="agent.profiles[local].options"
    )
    assert error is not None
    assert error.startswith("agent.profiles[local].options")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({1: "x"}, id="non-string-key"),
        pytest.param({str(index): index for index in range(65)}, id="mapping-keys"),
        pytest.param({"stop": list(range(65))}, id="list-items"),
    ],
)
def test_no_bounded_options_message_hardcodes_the_agent_options_root(
    value: dict[object, object],
) -> None:
    """The three messages that used to spell `agent.options` by hand.

    Each of them is reachable only through a different branch, so one
    parametrized case per branch is what proves the constant is gone
    rather than moved.
    """
    from korvid.core.config import _parse_bounded_options

    _parsed, error = _parse_bounded_options(value, root="agent.profiles[local].auth")
    assert error is not None
    assert error.startswith("agent.profiles[local].auth")
    assert "agent.options" not in error
