"""Single-file configuration (design doc §5-7): ~/.config/korvid/config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "korvid" / "config.yaml"


@dataclass(frozen=True)
class KorvidConfig:
    kube_context: str | None = None
    namespace: str | None = None
    agent_enabled: bool = False
    agent_provider: str | None = None
    agent_base_url: str | None = None
    agent_model: str | None = None
    agent_api_key_env: str | None = None
    agent_auth_method: str | None = None
    keybindings: dict[str, str] = field(default_factory=dict)
    log_buffer_lines: int = 5000


def load_config(path: Path | None = None) -> KorvidConfig:
    """Load config; missing file means zero-config defaults."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.is_file():
        return KorvidConfig()
    raw: dict[str, Any] = yaml.safe_load(cfg_path.read_text()) or {}
    agent_raw: dict[str, Any] = raw.get("agent") or {}
    provider: str | None = agent_raw.get("provider")
    # Auto-activation: provider present -> on, unless explicitly disabled (§6.3).
    enabled = bool(provider) and agent_raw.get("enabled", True) is not False
    api_key_env = _opt_str(agent_raw.get("api_key_env"))
    auth_raw: dict[str, Any] = agent_raw.get("auth") or {}
    auth_method = _opt_str(auth_raw.get("method"))
    if auth_method is None and provider:
        # Back-compat: configs written before agent.auth existed.
        auth_method = "api_key" if api_key_env else "none"
    return KorvidConfig(
        kube_context=raw.get("kube_context"),
        namespace=raw.get("namespace"),
        agent_enabled=enabled,
        agent_provider=provider,
        agent_base_url=_opt_str(agent_raw.get("base_url")),
        agent_model=_opt_str(agent_raw.get("model")),
        agent_api_key_env=api_key_env,
        agent_auth_method=auth_method,
        keybindings=dict(raw.get("keybindings") or {}),
        log_buffer_lines=_parse_buffer_lines(raw.get("log_buffer_lines")),
    )


def save_agent_config(
    path: Path,
    *,
    provider: str,
    auth_method: str,
    base_url: str | None,
    model: str,
    api_key_env: str | None,
) -> None:
    """Persist managed agent fields, preserving unrelated keys (read-modify-write)."""
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = yaml.safe_load(path.read_text()) or {}
    existing = raw.get("agent")
    agent: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    agent["provider"] = provider
    agent["model"] = model
    agent["auth"] = {"method": auth_method}
    if base_url:
        agent["base_url"] = base_url
    else:
        agent.pop("base_url", None)
    if api_key_env:
        agent["api_key_env"] = api_key_env
    else:
        agent.pop("api_key_env", None)
    raw["agent"] = agent
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


def _parse_buffer_lines(value: Any) -> int:
    """Coerce log_buffer_lines to a sane positive int; fall back to 5000."""
    if isinstance(value, bool):  # YAML `true` would silently become a 1-line buffer
        return 5000
    try:
        lines = int(value)
    except (TypeError, ValueError):
        return 5000
    return lines if lines > 0 else 5000


def _opt_str(value: Any) -> str | None:
    """Coerce value to string or None if empty."""
    return value if isinstance(value, str) and value else None
