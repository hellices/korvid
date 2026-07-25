from pathlib import Path

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

    raw = yaml.safe_load(p.read_text())
    assert raw["agent"]["enabled"] is False  # unrelated agent key kept


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
