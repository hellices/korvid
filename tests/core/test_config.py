import os
from pathlib import Path

import pytest

from korvid.core.config import KorvidConfig, load_config, save_agent_config


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


def test_explicit_agent_off_wins(tmp_path: Path) -> None:
    f = tmp_path / "config.yaml"
    f.write_text("agent:\n  provider: anthropic\n  enabled: false\n")
    cfg = load_config(f)
    assert cfg.agent_enabled is False  # explicit off switch (design doc §6.3-4)


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
    umask-derived default, exposing preserved values."""
    import os
    import stat

    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: ollama\n  model: llama3\n")
    os.chmod(p, 0o600)
    save_agent_config(
        p,
        provider="ollama",
        auth_method="none",
        base_url=None,
        model="llama3",
        api_key_env=None,
    )
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


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
        if os.name != "nt":
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


# ---------------------------------------------------------------------------
# namespaces: fallback namespace list for RBAC-limited users (issue #49)
# ---------------------------------------------------------------------------


def test_namespaces_list_parsed(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("namespaces:\n  - team-a\n  - team-b\n")
    cfg = load_config(p)
    assert cfg.namespaces == ("team-a", "team-b")


def test_namespaces_default_empty(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("namespace: default\n")
    cfg = load_config(p)
    assert cfg.namespaces == ()


def test_namespaces_non_list_ignored(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("namespaces: oops\n")
    cfg = load_config(p)
    assert cfg.namespaces == ()


def test_namespaces_non_string_entries_skipped(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text('namespaces:\n  - team-a\n  - 5\n  - ""\n  - null\n')
    cfg = load_config(p)
    assert cfg.namespaces == ("team-a",)


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


def test_agent_profile_unset_is_none(tmp_path: Path) -> None:
    """Unset stays distinguishable from an explicit `profile: full` so the
    `:ai` wizard only suggests `small` for Ollama when the user never chose."""
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n")
    cfg = load_config(p)
    assert cfg.agent_profile is None


def test_agent_profile_parsed(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  profile: small\n")
    cfg = load_config(p)
    assert cfg.agent_profile == "small"


def test_agent_profile_explicit_full_is_preserved(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  profile: full\n")
    cfg = load_config(p)
    assert cfg.agent_profile == "full"


def test_agent_profile_invalid_values_fall_back_to_unset(tmp_path: Path) -> None:
    """An unknown profile must not crash startup or half-configure the
    agent — unset keeps today's runtime behavior (full)."""
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  provider: ollama\n  profile: tiny\n")
    cfg = load_config(p)
    assert cfg.agent_profile is None


def test_save_agent_config_persists_the_small_profile(tmp_path: Path) -> None:
    """The wizard's profile suggestion must survive a restart (issue #71):
    saving `small` writes agent.profile so the next start rebuilds the same
    reduced surface the user just tested."""
    p = tmp_path / "c.yaml"
    save_agent_config(
        p,
        provider="ollama",
        auth_method="none",
        base_url="http://localhost:11434",
        model="qwen3:8b",
        api_key_env=None,
        profile="small",
    )
    assert load_config(p).agent_profile == "small"


def test_save_agent_config_writes_full_explicitly(tmp_path: Path) -> None:
    """Saving `full` writes the key: after the wizard runs the profile is a
    deliberate choice, and an explicit `full` must survive so reopening
    `:ai` never re-suggests `small` over it."""
    p = tmp_path / "c.yaml"
    p.write_text("agent:\n  provider: ollama\n  model: qwen3:8b\n  profile: small\n")
    save_agent_config(
        p,
        provider="openai-compat",
        auth_method="api_key",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        profile="full",
    )
    assert load_config(p).agent_profile == "full"
    assert "profile: full" in p.read_text()
