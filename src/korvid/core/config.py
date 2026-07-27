"""Single-file configuration (design doc §5-7): ~/.config/korvid/config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from os import chmod as os_chmod
from os import fdopen as os_fdopen
from os import fsync as os_fsync
from os import replace as os_replace
from pathlib import Path
from stat import S_IMODE
from tempfile import mkstemp
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
    #: Native Ollama tuning (issue #72): `agent.ollama.*` in config.yaml.
    agent_ollama_num_ctx: int = 16384
    agent_ollama_temperature: float = 0.0
    agent_ollama_seed: int | None = None
    agent_ollama_think: bool = False
    agent_ollama_keep_alive: str | int | None = None
    keybindings: dict[str, str] = field(default_factory=dict)
    log_buffer_lines: int = 5000
    log_wrap: bool = False
    log_timestamps: bool = False
    readonly: bool = False
    mcp_enabled: bool = False
    mcp_port: int = 7878
    #: kubectl debug image overrides (issue #52): air-gapped / private registry.
    #: `debug_images is None` means unconfigured; an explicit empty mapping is
    #: a deliberate restriction (only default/custom images are offered).
    debug_default_image: str | None = None
    debug_images: dict[str, str] | None = None


def load_config(path: Path | None = None) -> KorvidConfig:
    """Load config; missing file means zero-config defaults."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.is_file():
        return KorvidConfig()
    raw: dict[str, Any] = yaml.safe_load(cfg_path.read_text()) or {}
    agent_value = raw.get("agent")
    # User-edited configs can hold scalars where mappings are expected;
    # treat anything that is not a mapping as absent instead of crashing.
    agent_raw: dict[str, Any] = agent_value if isinstance(agent_value, dict) else {}
    provider: str | None = agent_raw.get("provider")
    # Auto-activation: provider present -> on, unless explicitly disabled (§6.3).
    enabled = bool(provider) and agent_raw.get("enabled", True) is not False
    api_key_env = _opt_str(agent_raw.get("api_key_env"))
    auth_value = agent_raw.get("auth")
    auth_raw: dict[str, Any] = auth_value if isinstance(auth_value, dict) else {}
    auth_method = _opt_str(auth_raw.get("method"))
    if auth_method is None and provider:
        # Back-compat: configs written before agent.auth existed.
        if provider == "github-copilot":
            auth_method = "device-login"
        else:
            auth_method = "api_key" if api_key_env else "none"
    ollama_value = agent_raw.get("ollama")
    ollama_raw: dict[str, Any] = ollama_value if isinstance(ollama_value, dict) else {}
    mcp_value = raw.get("mcp")
    mcp_raw: dict[str, Any] = mcp_value if isinstance(mcp_value, dict) else {}
    logs_value = raw.get("logs")
    logs_raw: dict[str, Any] = logs_value if isinstance(logs_value, dict) else {}
    debug_value = raw.get("debug")
    debug_raw: dict[str, Any] = debug_value if isinstance(debug_value, dict) else {}
    images_value = debug_raw.get("images")
    debug_images: dict[str, str] | None
    if "images" not in debug_raw:
        debug_images = None
    elif isinstance(images_value, dict):
        debug_images = {
            str(key): value
            for key, value in images_value.items()
            if isinstance(value, str) and value
        }
    else:
        # A present but non-mapping value is still a restriction attempt:
        # fail closed to an empty restricted mapping rather than silently
        # re-enabling public zero-config images.
        debug_images = {}
    return KorvidConfig(
        kube_context=raw.get("kube_context"),
        namespace=raw.get("namespace"),
        agent_enabled=enabled,
        agent_provider=provider,
        agent_base_url=_opt_str(agent_raw.get("base_url")),
        agent_model=_opt_str(agent_raw.get("model")),
        agent_api_key_env=api_key_env,
        agent_auth_method=auth_method,
        agent_ollama_num_ctx=_parse_num_ctx(ollama_raw.get("num_ctx")),
        agent_ollama_temperature=_parse_temperature(ollama_raw.get("temperature")),
        agent_ollama_seed=_parse_positive_int(ollama_raw.get("seed")),
        agent_ollama_think=ollama_raw.get("think") is True,
        agent_ollama_keep_alive=_parse_keep_alive(ollama_raw.get("keep_alive")),
        keybindings=dict(raw.get("keybindings") or {}),
        log_buffer_lines=_parse_buffer_lines(raw.get("log_buffer_lines")),
        log_wrap=logs_raw.get("wrap") is True,
        log_timestamps=logs_raw.get("timestamps") is True,
        readonly=raw.get("readonly") is True,
        mcp_enabled=mcp_raw.get("enabled") is True,
        mcp_port=_parse_port(mcp_raw.get("port")),
        debug_default_image=_opt_str(debug_raw.get("default_image")),
        debug_images=debug_images,
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
    # Merge into any existing auth mapping: only `method` is managed here,
    # unrelated nested keys must survive the read-modify-write.
    existing_auth = agent.get("auth")
    auth: dict[str, Any] = dict(existing_auth) if isinstance(existing_auth, dict) else {}
    auth["method"] = auth_method
    agent["auth"] = auth
    # A completed wizard/model save is a user-confirmed enable: clear any
    # stale explicit-disable switch so it cannot silently win after restart.
    agent.pop("enabled", None)
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
    _atomic_write_text(path, yaml.safe_dump(raw, sort_keys=False))


def _atomic_write_text(path: Path, text: str) -> None:
    """Unique same-directory temp file + fsync + atomic replace: an
    interrupted write can never leave truncated YAML behind (destroying
    unrelated keys), a power loss cannot leave an empty file, and concurrent
    writers cannot race on a shared temp name."""
    try:
        # Preserve an existing restrictive mode; default new files to 0600.
        mode = S_IMODE(path.stat().st_mode)
    except OSError:
        mode = 0o600
    fd, tmp_name = mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        # Write through the mkstemp fd and fsync it while still writable:
        # Windows' fsync (_commit) rejects read-only handles.
        with os_fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os_fsync(fh.fileno())
        os_chmod(tmp, mode)
        os_replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _parse_port(value: Any) -> int:
    """Coerce mcp.port to a valid TCP port; fall back to 7878."""
    if isinstance(value, bool):  # YAML `true` would silently become port 1
        return 7878
    if isinstance(value, float) and not value.is_integer():
        # Rejects fractional ports (7878.9) as well as .inf/.nan, which
        # int() would otherwise truncate or blow up on (OverflowError).
        return 7878
    try:
        port = int(value)
    except (TypeError, ValueError, OverflowError):
        return 7878
    return port if 0 < port < 65536 else 7878


def _parse_buffer_lines(value: Any) -> int:
    """Coerce log_buffer_lines to a sane positive int; fall back to 5000."""
    if isinstance(value, bool):  # YAML `true` would silently become a 1-line buffer
        return 5000
    try:
        lines = int(value)
    except (TypeError, ValueError):
        return 5000
    return lines if lines > 0 else 5000


def _parse_num_ctx(value: Any) -> int:
    """Coerce `agent.ollama.num_ctx` to a positive int; fall back to 16384."""
    parsed = _parse_positive_int(value)
    return parsed if parsed is not None else 16384


def _parse_positive_int(value: Any) -> int | None:
    """Coerce an `agent.ollama` count (num_ctx, seed) to a positive int, or None."""
    if isinstance(value, bool):  # YAML `true` would silently become 1
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_temperature(value: Any) -> float:
    """Coerce `agent.ollama.temperature` to a non-negative float; fall back to 0.0."""
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed >= 0 else 0.0


def _parse_keep_alive(value: Any) -> str | int | None:
    """`agent.ollama.keep_alive` passthrough: duration string ("10m") or integer seconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) or (isinstance(value, str) and value):
        return value
    return None


def _opt_str(value: Any) -> str | None:
    """Coerce value to string or None if empty."""
    return value if isinstance(value, str) and value else None
