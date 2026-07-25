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
        import fcntl

        # Windows' fsync (_commit) requires a writable handle: syncing an
        # O_RDONLY fd raises there, so the implementation must sync the fd
        # it wrote through.
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
