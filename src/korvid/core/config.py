"""Single-file configuration (design doc §5-7): ~/.config/korvid/config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from os import chmod as os_chmod
from os import fdopen as os_fdopen
from os import fsync as os_fsync
from os import replace as os_replace
from pathlib import Path
from stat import S_IMODE
from tempfile import mkstemp
from typing import Any

import yaml

from korvid.k8s.columns import SOURCES, CustomColumn, parse_jsonpath
from korvid.k8s.helm import SYNTHETIC_VIEW_KINDS

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "korvid" / "config.yaml"


@dataclass(frozen=True)
class ViewConfig:
    """Custom columns for one resource kind (issue #45)."""

    columns: tuple[CustomColumn, ...]
    #: True replaces the kind's default columns (NAME/NAMESPACE always stay);
    #: False appends after them.
    replace: bool = False


@dataclass(frozen=True)
class KorvidConfig:
    kube_context: str | None = None
    namespace: str | None = None
    #: Fallback namespaces for RBAC-limited users (issue #49): used by the
    #: namespace picker and the per-namespace watch fanout when cluster-wide
    #: LIST/WATCH is forbidden.
    namespaces: tuple[str, ...] = ()
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
    #: Custom table columns per resource kind (issue #45), keyed by the
    #: plural kind name as used in `:` navigation (e.g. "pods").
    views: dict[str, ViewConfig] = field(default_factory=dict)
    #: Human-readable config problems (e.g. an invalid custom column) that
    #: the UI surfaces once at startup instead of crashing or hiding them.
    warnings: tuple[str, ...] = ()


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
    views, view_warnings = _parse_views(raw.get("views"))
    return KorvidConfig(
        kube_context=raw.get("kube_context"),
        namespace=raw.get("namespace"),
        namespaces=_parse_namespaces(raw.get("namespaces")),
        agent_enabled=enabled,
        agent_provider=provider,
        agent_base_url=_opt_str(agent_raw.get("base_url")),
        agent_model=_opt_str(agent_raw.get("model")),
        agent_api_key_env=api_key_env,
        agent_auth_method=auth_method,
        agent_ollama_num_ctx=_parse_num_ctx(ollama_raw.get("num_ctx")),
        agent_ollama_temperature=_parse_temperature(ollama_raw.get("temperature")),
        agent_ollama_seed=_parse_seed(ollama_raw.get("seed")),
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
        views=views,
        warnings=tuple(view_warnings),
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
    """Coerce `agent.ollama.num_ctx` to a positive int, or None."""
    if isinstance(value, bool):  # YAML `true` would silently become 1
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _parse_seed(value: Any) -> int | None:
    """Coerce `agent.ollama.seed` to a non-negative int, or None.

    Unlike num_ctx, `seed: 0` is a valid (reproducible) sampling seed and
    must not fall back to the server's random default.
    """
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _parse_temperature(value: Any) -> float:
    """Coerce `agent.ollama.temperature` to a non-negative float; fall back to 0.0."""
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    # Non-finite values (.inf/.nan) would serialize as invalid JSON downstream.
    return parsed if parsed >= 0 and isfinite(parsed) else 0.0


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


def _parse_namespaces(value: Any) -> tuple[str, ...]:
    """`namespaces:` fallback list (issue #49): non-empty strings only."""
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _parse_column(kind: str, entry: Any) -> tuple[CustomColumn | None, str | None]:
    """(column, warning) for one `views.<kind>.columns` item; at most one is set."""
    if not isinstance(entry, dict):
        return None, f"views.{kind}: column entries must be mappings"
    name = _opt_str(entry.get("name"))
    if name is None:
        return None, f"views.{kind}: a column is missing its `name`"
    declared = [source for source in SOURCES if _opt_str(entry.get(source)) is not None]
    if len(declared) != 1:
        return None, (
            f"views.{kind}.{name}: declare exactly one of "
            f"{', '.join(SOURCES)} (got {len(declared)})"
        )
    source = declared[0]
    expr = str(entry[source])
    if source == "jsonpath":
        try:
            parse_jsonpath(expr)
        except ValueError as exc:
            return None, f"views.{kind}.{name}: {exc}"
    return CustomColumn(name, source, expr), None


def _parse_views(value: Any) -> tuple[dict[str, ViewConfig], list[str]]:
    """`views:` custom columns (issue #45): invalid columns are dropped with
    a warning instead of failing the whole config — a typo in one column
    must not take the TUI down."""
    if not isinstance(value, dict):
        return {}, []
    views: dict[str, ViewConfig] = {}
    warnings: list[str] = []
    for kind, view_raw in value.items():
        if not isinstance(view_raw, dict):
            continue
        if str(kind) in SYNTHETIC_VIEW_KINDS:
            # Synthetic helm views are adapted from backing Secrets — there
            # is no manifest to evaluate custom columns against.
            warnings.append(f"views.{kind}: synthetic view kinds don't support custom columns")
            continue
        columns: list[CustomColumn] = []
        seen: set[str] = set()
        raw_columns = view_raw.get("columns")
        for entry in raw_columns if isinstance(raw_columns, list) else []:
            column, warning = _parse_column(str(kind), entry)
            if column is not None:
                # Case-insensitive duplicate names would make headers
                # ambiguous and later columns unreachable for :sort.
                if column.name.lower() in seen:
                    warnings.append(f"views.{kind}.{column.name}: duplicate column name")
                else:
                    seen.add(column.name.lower())
                    columns.append(column)
            if warning is not None:
                warnings.append(warning)
        if columns:
            views[str(kind)] = ViewConfig(
                columns=tuple(columns), replace=view_raw.get("replace") is True
            )
    return views, warnings
