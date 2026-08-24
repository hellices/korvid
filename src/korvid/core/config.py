"""Single-file configuration (design doc §5-7): ~/.config/korvid/config.yaml."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from math import isfinite
from os import chmod as os_chmod
from os import fdopen as os_fdopen
from os import fsync as os_fsync
from os import replace as os_replace
from pathlib import Path
from stat import S_IMODE
from tempfile import mkstemp
from typing import Any
from urllib.parse import urlsplit

import yaml

from korvid.k8s.columns import SOURCES, CustomColumn, parse_jsonpath
from korvid.k8s.helm import SYNTHETIC_VIEW_KINDS

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "korvid" / "config.yaml"
_MAX_AGENT_OPTIONS_DEPTH = 4
_MAX_AGENT_OPTIONS_KEYS = 64
_MAX_AGENT_OPTIONS_LIST_ITEMS = 64
_MAX_AGENT_OPTIONS_STRING_BYTES = 2048
_MAX_AGENT_OPTIONS_SERIALIZED_BYTES = 16 * 1024
_MAX_AGENT_OPTIONS_PATH_CHARS = 120
_SECRET_OPTION_KEY_SEGMENTS = (
    "secret",
    "password",
    "token",
    "api_key",
    "apikey",  # compact form of api_key (common in JSON configs)
    "authorization",
    "credential",
)

# Precompute token sequences for sliding-window matching.
# Each entry is a tuple of underscore-split tokens for the reserved segment.
_SECRET_SEGMENT_TOKEN_SEQS: tuple[tuple[str, ...], ...] = tuple(
    tuple(seg.split("_")) for seg in _SECRET_OPTION_KEY_SEGMENTS
)

_PROVIDER_SEPARATOR_RE = re.compile(r"[-_.]+")

#: The Prometheus/LogQL label-name grammar. A mapped name is interpolated
#: into a selector as an identifier, so it cannot be escaped the way a
#: value is — it has to match the grammar or be refused.
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

#: Sentinel recorded for a rejected label name, so the backend is disabled
#: rather than quietly falling back to a default that would query the
#: wrong label.
_INVALID_LABEL = "\x00invalid"


def _canonicalize_provider_name(name: str) -> str:
    """Canonicalize a provider name: lowercase, collapse [-_.] to hyphens, strip.

    Pure-stdlib mirror of ``providers.plugin_registry.normalize_provider_name``
    so ``core/`` can normalize before dispatch without importing ``providers/``
    (tach layer rules: core must not import providers).
    """
    return _PROVIDER_SEPARATOR_RE.sub("-", name.strip().lower())


@dataclass(frozen=True)
class ViewConfig:
    """Custom columns for one resource kind (issue #45)."""

    columns: tuple[CustomColumn, ...]
    #: True replaces the kind's default columns (NAME/NAMESPACE always stay);
    #: False appends after them.
    replace: bool = False


#: Scope field to backend label name, for a log shipper using the
#: conventional Kubernetes labels.
_DEFAULT_LABEL_MAPPINGS: dict[str, str] = {
    "namespace": "namespace",
    "pod": "pod",
    "workload": "app",
}

#: Scope fields a Loki label mapping may name. Closed: a mapping for an
#: unknown field would look configured and silently do nothing.
_SCOPE_FIELDS: tuple[str, ...] = ("namespace", "pod", "workload")

#: Keys that read as "turn TLS verification off". korvid has no such
#: setting, and ignoring one would leave the user believing they had
#: disabled verification when they had not.
_TLS_SWITCH_KEYS: tuple[str, ...] = (
    "insecure",
    "insecure_skip_verify",
    "skip_tls_verify",
    "tls_skip_verify",
    "verify",
    "tls_verify",
)

#: Keys that would hold a credential *value*. config.yaml is not a secret
#: store: a token belongs in an environment variable or a file, named here.
_INLINE_CREDENTIAL_KEYS: tuple[str, ...] = (
    "token",
    "password",
    "bearer_token",
    "api_key",
    "apikey",
    "credentials",
)


@dataclass(frozen=True)
class ObservabilityBackend:
    """One configured read-only observability endpoint (issue #193).

    Carries *where* the backend is and *how much* it may be asked. It
    deliberately has no field that could hold a credential value and no
    field that could weaken TLS: the credential is named indirectly
    (`token_env`/`token_file`) and trust follows `network.ca_bundle`.
    """

    url: str
    #: Environment variable holding the bearer token, read at call time.
    token_env: str | None = None
    #: File holding the bearer token, read at call time.
    token_file: str | None = None
    #: Multi-tenant header value (Loki `X-Scope-OrgID`).
    tenant: str | None = None
    timeout_seconds: float = 10.0
    default_window_minutes: int = 60
    max_window_minutes: int = 360
    max_series: int = 50
    max_lines: int = 200
    max_response_bytes: int = 1024 * 1024
    max_concurrency: int = 2
    #: Scope field to backend label name (Loki). Defaults cover the
    #: conventional Kubernetes labels a log shipper attaches.
    label_mappings: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_LABEL_MAPPINGS))
    #: Backend labels whose *values* are masked in every result, lowercased
    #: (issue #193). For fields that are sensitive by policy rather than by
    #: shape - a tenant id, a customer name - which the credential-shaped
    #: text pass cannot recognise on its own.
    mask_labels: tuple[str, ...] = ()


class ConfigMigrationError(ValueError):
    """A config key was removed and the file needs a one-time hand edit.

    Raised for `agent.profile`/`agent.prompts` (replaced by
    `agent.model_tier`/`agent.rules`) and for an invalid `agent.model_tier`
    value. The message is always a single line so it reads cleanly as a
    `SystemExit` at startup — never let it grow an embedded newline.
    """


@dataclass(frozen=True)
class KorvidConfig:
    kube_context: str | None = None
    namespace: str | None = None
    #: UI-only namespace shortcuts (issue #108): bound to keys `1`-`9` in
    #: order. Purely local navigation state — never an authorization list.
    favorite_namespaces: tuple[str, ...] = ()
    agent_enabled: bool = False
    agent_provider: str | None = None
    agent_base_url: str | None = None
    agent_model: str | None = None
    agent_api_key_env: str | None = None
    agent_auth_method: str | None = None
    agent_options: dict[str, object] = field(default_factory=dict)
    agent_options_error: str | None = None
    #: Explicit model-capability tier override (`agent.model_tier`): `low` or
    #: `high`, or `None` for automatic routing. Replaces the removed
    #: `agent.profile` key — see `ConfigMigrationError`. It is consumed by
    #: `korvid.agent.model_policy.ModelRouter`, which resolves it (together
    #: with provider-reported and shipped-catalog capabilities) into the
    #: `ResolvedAgentPolicy` that carries the session's tool surface,
    #: budgets, and prompt pack.
    agent_model_tier: str | None = None
    #: Additive house rules (`agent.rules`): short, plain-language
    #: instructions appended to the agent's system context (replaces the
    #: removed `agent.prompts` system/append/tool_descriptions overrides).
    #: Each entry is a non-blank string of at most 1000 characters; at most
    #: 16 entries are kept (excess and invalid entries are dropped with a
    #: warning, never a hard failure). Composed as an additive layer by
    #: `korvid.agent.prompt_harness.PromptHarness`, which never lets a rule
    #: widen what the safety contract above it granted.
    agent_rules: tuple[str, ...] = ()
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
    timeline_max_entries: int = 500
    timeline_max_bytes: int = 262144
    readonly: bool = False
    #: Contexts (kubeconfig names or fnmatch globs, issue #83) where every
    #: write demands typing the context name and the status bar shows a red
    #: protected marker. Re-evaluated on every `:ctx` switch.
    protected_contexts: tuple[str, ...] = ()
    #: `agent.disable_in_protected` (issue #83): refuse agent prompts entirely
    #: while a protected context is active.
    agent_disable_in_protected: bool = False
    #: `agent.follow`: mirror the built-in agent's successful cluster reads
    #: on screen (like MCP follow, issue #153, but for the in-app chat).
    #: Small local models rarely volunteer the UI tools, so this defaults
    #: on; runtime toggle: `:ai follow on|off`.
    agent_follow: bool = True
    mcp_enabled: bool = False
    mcp_port: int = 7878
    #: `mcp.write_proposals` (issue #110): expose the external write-proposal
    #: tools over MCP. Off by default; the tools only queue proposals — every
    #: mutation still requires explicit approval inside the TUI.
    mcp_write_proposals: bool = False
    #: `mcp.follow` (issue #153): start with MCP follow mode on — external
    #: cluster reads arriving over MCP are mirrored in the TUI. Runtime
    #: toggle: `:mcp follow on|off`.
    mcp_follow: bool = False
    #: kubectl debug image overrides (issue #52): air-gapped / private registry.
    #: `debug_images is None` means unconfigured; an explicit empty mapping is
    #: a deliberate restriction (only default/custom images are offered).
    debug_default_image: str | None = None
    debug_images: dict[str, str] | None = None
    #: node shell overrides (issue #46): the `kubectl debug node/` image
    #: (air-gapped clusters) and the namespace the debug pod is created in
    #: (clusters whose default namespace blocks privileged pods via PSA).
    node_shell_image: str | None = None
    node_shell_namespace: str | None = None
    #: Custom table columns per resource kind (issue #45), keyed by the
    #: plural kind name as used in `:` navigation (e.g. "pods").
    views: dict[str, ViewConfig] = field(default_factory=dict)
    #: `ui.topbar` (issue #142): "expanded" starts the top bar with the full
    #: grouped legend; anything else (or unset) starts collapsed. The
    #: runtime toggle persists the choice back through save_topbar_state.
    ui_topbar_expanded: bool = False
    #: `integrations.telepresence` kill-switch (issue #159): False disables
    #: detection, the status panel and the install hint entirely. On by
    #: default - detection is one `shutil.which` at startup; with the
    #: client absent, the install-hint probe adds one API GET per context
    #: (until a hint has been shown).
    telepresence_enabled: bool = True
    #: `network.ca_bundle` (issue #168): default trust bundle for
    #: korvid-owned agent HTTPS clients (OpenAI-compatible, native Ollama,
    #: and the :ai wizard's connection test). Standard environment behavior
    #: (SSL_CERT_FILE, proxy variables) applies when unset. There is no
    #: insecure mode: an unloadable bundle fails startup actionably.
    network_ca_bundle: str | None = None
    #: `observability.prometheus` / `observability.loki` (issue #193):
    #: bounded read-only investigation backends. None means not
    #: configured — the matching tools are absent, not failing.
    observability_prometheus: ObservabilityBackend | None = None
    observability_loki: ObservabilityBackend | None = None
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
    provider_raw: str | None = agent_raw.get("provider")
    # Canonicalize early: github_copilot, GitHub.Copilot etc. all become
    # github-copilot so auth-method defaults and the composition root's
    # OAuth token lookup match without case/separator awareness.
    provider: str | None = (
        _canonicalize_provider_name(provider_raw) if isinstance(provider_raw, str) else None
    )
    # Auto-activation: provider present -> on, unless explicitly disabled (§6.3).
    enabled = bool(provider) and agent_raw.get("enabled", True) is not False
    api_key_env = _opt_str(agent_raw.get("api_key_env"))
    auth_value = agent_raw.get("auth")
    auth_raw: dict[str, Any] = auth_value if isinstance(auth_value, dict) else {}
    agent_options, agent_options_error = (
        _parse_agent_options(agent_raw["options"]) if "options" in agent_raw else ({}, None)
    )
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
    node_shell_value = raw.get("node_shell")
    node_shell_raw: dict[str, Any] = node_shell_value if isinstance(node_shell_value, dict) else {}
    ui_value = raw.get("ui")
    ui_raw: dict[str, Any] = ui_value if isinstance(ui_value, dict) else {}
    integrations_value = raw.get("integrations")
    integrations_raw: dict[str, Any] = (
        integrations_value if isinstance(integrations_value, dict) else {}
    )
    network_value = raw.get("network")
    network_raw: dict[str, Any] = network_value if isinstance(network_value, dict) else {}
    timeline_value = raw.get("timeline")
    timeline_raw: dict[str, Any] = timeline_value if isinstance(timeline_value, dict) else {}
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
    if "profile" in agent_raw:
        raise ConfigMigrationError(
            "agent.profile was removed; use agent.model_tier instead (absent/low/high)."
        )
    if "prompts" in agent_raw:
        raise ConfigMigrationError(
            "agent.prompts was removed; use agent.rules instead (a list of short house rules)."
        )
    views, view_warnings = _parse_views(raw.get("views"))
    warnings = list(view_warnings)
    model_tier = (
        _parse_model_tier(agent_raw.get("model_tier")) if "model_tier" in agent_raw else None
    )
    agent_rules, rules_warnings = _parse_agent_rules(agent_raw.get("rules"))
    warnings.extend(rules_warnings)
    if agent_options_error is not None:
        warnings.append(agent_options_error)
    if "namespaces" in raw:
        warnings.append(
            "namespaces: no longer controls the namespace picker or watches"
            " (issue #108) — Kubernetes authorization is owned by the API"
            " server. Use `namespace:` for the startup namespace and"
            " `favorite_namespaces:` for UI-only shortcuts on keys 1-9."
        )
    favorites, favorite_warnings = _parse_favorite_namespaces(raw.get("favorite_namespaces"))
    warnings.extend(favorite_warnings)
    observability_value = raw.get("observability")
    observability_raw: dict[str, Any] = (
        observability_value if isinstance(observability_value, dict) else {}
    )
    prometheus, prometheus_warnings = _parse_observability_backend(
        observability_raw.get("prometheus"), "observability.prometheus"
    )
    warnings.extend(prometheus_warnings)
    loki, loki_warnings = _parse_observability_backend(
        observability_raw.get("loki"), "observability.loki"
    )
    warnings.extend(loki_warnings)
    return KorvidConfig(
        kube_context=raw.get("kube_context"),
        namespace=raw.get("namespace"),
        favorite_namespaces=favorites,
        agent_enabled=enabled,
        agent_provider=provider,
        agent_base_url=_opt_str(agent_raw.get("base_url")),
        agent_model=_opt_str(agent_raw.get("model")),
        agent_api_key_env=api_key_env,
        agent_auth_method=auth_method,
        agent_options=agent_options,
        agent_options_error=agent_options_error,
        agent_model_tier=model_tier,
        agent_rules=agent_rules,
        agent_ollama_num_ctx=_parse_num_ctx(ollama_raw.get("num_ctx")),
        agent_ollama_temperature=_parse_temperature(ollama_raw.get("temperature")),
        agent_ollama_seed=_parse_seed(ollama_raw.get("seed")),
        agent_ollama_think=ollama_raw.get("think") is True,
        agent_ollama_keep_alive=_parse_keep_alive(ollama_raw.get("keep_alive")),
        keybindings=dict(raw.get("keybindings") or {}),
        log_buffer_lines=_parse_buffer_lines(raw.get("log_buffer_lines")),
        log_wrap=logs_raw.get("wrap") is True,
        log_timestamps=logs_raw.get("timestamps") is True,
        timeline_max_entries=_mapping_positive_int(
            timeline_raw,
            "max_entries",
            KorvidConfig.timeline_max_entries,
            "timeline",
            warnings,
        ),
        timeline_max_bytes=_mapping_positive_int(
            timeline_raw,
            "max_bytes",
            KorvidConfig.timeline_max_bytes,
            "timeline",
            warnings,
        ),
        readonly=raw.get("readonly") is True,
        protected_contexts=_parse_protected_contexts(raw.get("protected_contexts")),
        agent_disable_in_protected=agent_raw.get("disable_in_protected") is True,
        agent_follow=agent_raw.get("follow") is not False,
        mcp_enabled=mcp_raw.get("enabled") is True,
        mcp_port=_parse_port(mcp_raw.get("port")),
        mcp_write_proposals=mcp_raw.get("write_proposals") is True,
        telepresence_enabled=integrations_raw.get("telepresence") is not False,
        network_ca_bundle=_opt_str(network_raw.get("ca_bundle")),
        observability_prometheus=prometheus,
        observability_loki=loki,
        mcp_follow=mcp_raw.get("follow") is True,
        debug_default_image=_opt_str(debug_raw.get("default_image")),
        debug_images=debug_images,
        node_shell_image=_opt_str(node_shell_raw.get("image")),
        node_shell_namespace=_opt_str(node_shell_raw.get("namespace")),
        views=views,
        ui_topbar_expanded=ui_raw.get("topbar") == "expanded",
        warnings=tuple(warnings),
    )


def _observability_url(value: Any, label: str, warnings: list[str]) -> str | None:
    """A usable endpoint URL, or None with the reason.

    Parsed rather than prefix-matched: `https://user:pw@` starts with
    `https://` and names no host at all, so a prefix check would accept it
    and leave the connector with nothing but the raw string — credential
    included — to name in a message.
    """
    url = _opt_str(value)
    if url is None:
        warnings.append(f"{label}: `url` is required — the backend is disabled")
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError:
        warnings.append(f"{label}.url: is not a usable URL — the backend is disabled")
        return None
    if parsed.scheme not in ("http", "https"):
        warnings.append(
            f"{label}.url: must be an http:// or https:// URL — the backend is disabled"
        )
        return None
    if not host:
        # Deliberately does not echo the URL: a hostname-less authority is
        # most often `scheme://user:password@`, and this warning is shown
        # on screen.
        warnings.append(f"{label}.url: names no host — the backend is disabled")
        return None
    if parsed.query or parsed.fragment or "?" in url or "#" in url:
        # The API path is appended to this, so a query string would
        # swallow it and every request would target the wrong endpoint.
        warnings.append(
            f"{label}.url: must be an origin with an optional base path, not a"
            f" query string or fragment — the backend is disabled"
        )
        return None
    if "@" in parsed.netloc:
        # `https://user:pw@host` is an inline credential wearing a URL's
        # clothes: the HTTP client sends it as Basic auth. Rejected for
        # the same reason a `token:` key is (issue #193, PR #280 review).
        warnings.append(
            f"{label}.url: must not carry a username or password — use `token_env`"
            f" (environment variable name) or `token_file` (path)."
            f" The backend is disabled."
        )
        return None
    return url


def _observability_rejections(raw: Mapping[str, Any], label: str, warnings: list[str]) -> bool:
    """Whether a key was present that must disable the backend outright.

    Both classes fail closed rather than being ignored: a user who thinks
    they turned off TLS verification, or who thinks their token is being
    read from `config.yaml`, is worse off believing it than being told no.
    """
    rejected = False
    for key in _TLS_SWITCH_KEYS:
        if key in raw:
            warnings.append(
                f"{label}.{key}: TLS verification cannot be disabled — remove the key and"
                f" configure a trust bundle with `network.ca_bundle` instead."
                f" The backend is disabled."
            )
            rejected = True
    for key in _INLINE_CREDENTIAL_KEYS:
        if key in raw:
            warnings.append(
                f"{label}.{key}: a credential value must not live in config.yaml — use"
                f" `token_env` (environment variable name) or `token_file` (path)."
                f" The backend is disabled."
            )
            rejected = True
    token_env = _opt_str(raw.get("token_env"))
    token_file = _opt_str(raw.get("token_file"))
    if token_env and token_file:
        warnings.append(
            f"{label}: set either `token_env` or `token_file`, not both —"
            f" the backend is disabled rather than guessing which credential to send."
        )
        rejected = True
    return rejected


def _mapping_positive_int(
    raw: Mapping[str, Any], key: str, default: int, label: str, warnings: list[str]
) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        warnings.append(f"{label}.{key}: must be a positive integer — using the default {default}")
        return default
    return value


def _observability_timeout(raw: Mapping[str, Any], label: str, warnings: list[str]) -> float:
    default = ObservabilityBackend.timeout_seconds
    if "timeout_seconds" not in raw:
        return default
    value = raw["timeout_seconds"]
    # `isfinite` matters: YAML `.inf` parses to a float that is greater
    # than zero, and would mean the bounded-query contract has no bound.
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not isfinite(value)
        or value <= 0
    ):
        warnings.append(
            f"{label}.timeout_seconds: must be a positive finite number"
            f" — using the default {default}"
        )
        return default
    return float(value)


def _observability_label_mappings(value: Any, label: str, warnings: list[str]) -> dict[str, str]:
    mappings = dict(_DEFAULT_LABEL_MAPPINGS)
    if value is None:
        return mappings
    if not isinstance(value, Mapping):
        warnings.append(f"{label}.label_mappings: must be a mapping — using the defaults")
        return mappings
    for scope_field, backend_label in value.items():
        if scope_field not in _SCOPE_FIELDS:
            warnings.append(
                f"{label}.label_mappings.{scope_field}: unknown scope field — ignored"
                f" (known fields: {', '.join(_SCOPE_FIELDS)})"
            )
            continue
        name = _opt_str(backend_label)
        if name is None:
            warnings.append(
                f"{label}.label_mappings.{scope_field}: must be a non-empty label name — ignored"
            )
            continue
        if not _LABEL_NAME_RE.fullmatch(name):
            warnings.append(
                f"{label}.label_mappings.{scope_field}: {name!r} is not a usable label name"
                f" (a label name must match [a-zA-Z_][a-zA-Z0-9_]*) — the backend is disabled"
            )
            mappings[scope_field] = _INVALID_LABEL
            continue
        mappings[scope_field] = name
    return mappings


def _observability_mask_labels(value: Any, label: str, warnings: list[str]) -> tuple[str, ...]:
    """The label names whose values are always masked, lowercased and sorted."""
    if value is None:
        return ()
    if not isinstance(value, list):
        warnings.append(f"{label}.mask_labels: must be a list of label names — ignored")
        return ()
    names: set[str] = set()
    for entry in value:
        name = _opt_str(entry)
        if name is None:
            warnings.append(
                f"{label}.mask_labels: entries must be non-empty label names — one dropped"
            )
            continue
        names.add(name.lower())
    return tuple(sorted(names))


def _colliding_label_mapping(mappings: Mapping[str, str]) -> tuple[str, list[str]] | None:
    """The backend label two scope fields share, with the fields, or None.

    A collision is not a preference, it is a lost constraint: the
    selector is a mapping from label to value, so mapping `namespace` and
    `workload` both to `app` leaves one matcher and the search silently
    covers every namespace.
    """
    by_label: dict[str, list[str]] = {}
    for scope_field, name in mappings.items():
        by_label.setdefault(name, []).append(scope_field)
    for name, fields in sorted(by_label.items()):
        if len(fields) > 1:
            return name, sorted(fields)
    return None


def _parse_observability_backend(
    value: Any, label: str
) -> tuple[ObservabilityBackend | None, list[str]]:
    """One `observability.<backend>` section, or None with the reasons why.

    Returns:
        The backend and the warnings its section produced. A None backend
        means the tools that would use it are simply absent.
    """
    warnings: list[str] = []
    if value is None:
        return None, warnings
    if not isinstance(value, Mapping):
        warnings.append(f"{label}: must be a mapping — the backend is disabled")
        return None, warnings
    url = _observability_url(value.get("url"), label, warnings)
    rejected = _observability_rejections(value, label, warnings)
    mappings = _observability_label_mappings(value.get("label_mappings"), label, warnings)
    if _INVALID_LABEL in mappings.values():
        return None, warnings
    collision = _colliding_label_mapping(mappings)
    if collision is not None:
        name, fields = collision
        warnings.append(
            f"{label}.label_mappings: {' and '.join(fields)} both map to the label"
            f" {name!r}, which would drop one of the two constraints from every"
            f" query — the backend is disabled"
        )
        rejected = True
    if url is None or rejected:
        return None, warnings
    token_env = _opt_str(value.get("token_env"))
    token_file = _opt_str(value.get("token_file"))
    # The parsed scheme, not the spelling: URL schemes are
    # case-insensitive, so `HTTP://` is cleartext too.
    if urlsplit(url).scheme == "http" and (token_env or token_file):
        # Allowed, because a cluster-local Prometheus over http is an
        # ordinary deployment — but a bearer token on that connection
        # crosses the network in the clear, and the user should decide
        # that knowingly rather than by omission.
        warnings.append(
            f"{label}: a credential is configured for a plaintext http:// endpoint —"
            f" the token will cross the network unencrypted"
        )
    defaults = ObservabilityBackend(url=url)
    max_window = _mapping_positive_int(
        value, "max_window_minutes", defaults.max_window_minutes, label, warnings
    )
    default_window = _mapping_positive_int(
        value, "default_window_minutes", defaults.default_window_minutes, label, warnings
    )
    if default_window > max_window:
        warnings.append(
            f"{label}.default_window_minutes: {default_window} exceeds"
            f" max_window_minutes {max_window} — using {max_window}"
        )
        default_window = max_window
    return (
        ObservabilityBackend(
            url=url,
            token_env=token_env,
            token_file=token_file,
            tenant=_opt_str(value.get("tenant")),
            timeout_seconds=_observability_timeout(value, label, warnings),
            default_window_minutes=default_window,
            max_window_minutes=max_window,
            max_series=_mapping_positive_int(
                value, "max_series", defaults.max_series, label, warnings
            ),
            max_lines=_mapping_positive_int(
                value, "max_lines", defaults.max_lines, label, warnings
            ),
            max_response_bytes=_mapping_positive_int(
                value, "max_response_bytes", defaults.max_response_bytes, label, warnings
            ),
            max_concurrency=_mapping_positive_int(
                value, "max_concurrency", defaults.max_concurrency, label, warnings
            ),
            label_mappings=mappings,
            mask_labels=_observability_mask_labels(value.get("mask_labels"), label, warnings),
        ),
        warnings,
    )


def save_agent_config(
    path: Path,
    *,
    provider: str,
    auth_method: str,
    base_url: str | None,
    model: str,
    api_key_env: str | None,
    model_tier: str | None = None,
) -> None:
    """Persist managed agent fields, preserving unrelated keys (read-modify-write)."""
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = yaml.safe_load(path.read_text()) or {}
    existing = raw.get("agent")
    agent: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    agent["provider"] = provider
    agent["model"] = model
    # An explicit low/high override is a deliberate choice and is written
    # out so it survives a restart and reopening `:ai` never resets it to
    # Automatic. Automatic (None) instead pops any previously persisted
    # override — choosing Automatic in the wizard must actually clear a
    # stale explicit tier, not leave it stuck.
    if model_tier is not None:
        agent["model_tier"] = model_tier
    else:
        agent.pop("model_tier", None)
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


def save_topbar_state(path: Path, *, expanded: bool) -> None:
    """Persist the top bar collapse/expand choice (issue #142), preserving
    unrelated keys (same read-modify-write shape as save_agent_config)."""
    raw: dict[str, Any] = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text())
        raw = loaded if isinstance(loaded, dict) else {}
    existing = raw.get("ui")
    ui: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    ui["topbar"] = "expanded" if expanded else "collapsed"
    raw["ui"] = ui
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, yaml.safe_dump(raw, sort_keys=False))


def _atomic_write_text(path: Path, text: str) -> None:
    """Unique same-directory temp file + fsync + atomic replace: an
    interrupted write can never leave truncated YAML behind (destroying
    unrelated keys), a power loss cannot leave an empty file, and concurrent
    writers cannot race on a shared temp name."""
    try:
        existing_mode = S_IMODE(path.stat().st_mode)
    except OSError:
        existing_mode = None
    # On Windows, POSIX stat mode emulation returns 0o666 for readable+writable
    # files regardless of actual ACLs; we cannot trust it as a "preserve" signal
    # and always request the restrictive 0o600.  On POSIX the real mode is
    # meaningful, so we honour it when present.
    mode = existing_mode if os.name != "nt" and existing_mode is not None else 0o600
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


def _parse_model_tier(value: Any) -> str | None:
    """Coerce a present `agent.model_tier` value.

    `null` is the YAML idiom for "not set" and means automatic routing
    (returns None), matching an absent key. Any other value must be exactly
    `low` or `high` — legacy `full`/`small`, `auto`, and typos are hard
    errors (unlike the old `agent.profile`, which silently fell back).
    """
    if value is None:
        return None
    if isinstance(value, str) and value in ("low", "high"):
        return value
    raise ConfigMigrationError(
        f"agent.model_tier must be absent, null, 'low', or 'high' (got {value!r})."
    )


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


@dataclass
class _AgentOptionCounters:
    mapping_keys: int = 0
    list_items: int = 0


class _AgentOptionsError(ValueError):
    """Raised when `agent.options` violates the published v1 bounds."""


_UNSUPPORTED_AGENT_OPTION = object()


_MAX_AGENT_RULES = 16
_MAX_AGENT_RULE_CHARS = 1000


def _parse_agent_rules(value: Any) -> tuple[tuple[str, ...], list[str]]:
    """Parse `agent.rules`: additive house-rule strings.

    Every problem is a warning, never a hard failure — a bad `agent.rules`
    entry degrades to "this one rule is dropped", not a startup crash. Each
    kept entry is stripped, non-blank, and at most `_MAX_AGENT_RULE_CHARS`
    characters; the list is capped at `_MAX_AGENT_RULES` entries (first N
    kept, in order).
    """
    warnings: list[str] = []
    if value is None:
        return (), warnings
    if not isinstance(value, list):
        warnings.append("agent.rules must be a list of strings; ignored")
        return (), warnings
    rules: list[str] = []
    dropped = 0
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            dropped += 1
            continue
        text = entry.strip()
        if len(text) > _MAX_AGENT_RULE_CHARS:
            warnings.append(
                f"agent.rules: an entry over {_MAX_AGENT_RULE_CHARS} characters was dropped"
            )
            continue
        rules.append(text)
    if dropped:
        warnings.append(f"agent.rules: {dropped} blank or non-string entr(y/ies) dropped")
    if len(rules) > _MAX_AGENT_RULES:
        warnings.append(f"agent.rules: only the first {_MAX_AGENT_RULES} entries are kept")
        rules = rules[:_MAX_AGENT_RULES]
    return tuple(rules), warnings


def _parse_agent_options(value: Any) -> tuple[dict[str, object], str | None]:
    if not isinstance(value, Mapping):
        return {}, "agent.options must be a mapping with string keys"
    counters = _AgentOptionCounters()
    try:
        parsed = _parse_agent_option_mapping(
            value, path="agent.options", depth=1, counters=counters
        )
        serialized = json.dumps(
            parsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except _AgentOptionsError as exc:
        return {}, str(exc)
    except (TypeError, ValueError) as exc:
        return {}, f"agent.options could not be serialized safely: {type(exc).__name__}"
    if len(serialized) > _MAX_AGENT_OPTIONS_SERIALIZED_BYTES:
        return (
            {},
            f"agent.options exceeds max serialized budget {_MAX_AGENT_OPTIONS_SERIALIZED_BYTES} bytes",
        )
    return parsed, None


def _parse_agent_option_mapping(
    value: Mapping[object, object],
    *,
    path: str,
    depth: int,
    counters: _AgentOptionCounters,
) -> dict[str, object]:
    if depth > _MAX_AGENT_OPTIONS_DEPTH:
        raise _AgentOptionsError(
            f"{_agent_options_path(path)} exceeds max depth {_MAX_AGENT_OPTIONS_DEPTH}"
        )
    parsed: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise _AgentOptionsError("agent.options must use string keys")
        if len(key.encode("utf-8")) > _MAX_AGENT_OPTIONS_STRING_BYTES:
            raise _AgentOptionsError(
                f"{_agent_options_path(f'{path}.{key[:60]}...')} key exceeds max length "
                f"{_MAX_AGENT_OPTIONS_STRING_BYTES} bytes"
            )
        if not key.isascii():
            raise _AgentOptionsError(
                f"{_agent_options_path(f'{path}.{key[:60]}')} option keys must be ASCII-only"
            )
        _raise_if_secret_key_segment(key, path=path)
        counters.mapping_keys += 1
        if counters.mapping_keys > _MAX_AGENT_OPTIONS_KEYS:
            raise _AgentOptionsError(
                f"agent.options exceeds max {_MAX_AGENT_OPTIONS_KEYS} mapping keys"
            )
        child_path = f"{path}.{key}"
        parsed[key] = _parse_agent_option_value(
            item, path=child_path, depth=depth, counters=counters
        )
    return parsed


def _parse_agent_option_value(
    value: object,
    *,
    path: str,
    depth: int,
    counters: _AgentOptionCounters,
) -> object:
    scalar = _parse_agent_option_scalar(value, path=path)
    if scalar is not _UNSUPPORTED_AGENT_OPTION:
        return scalar
    if isinstance(value, Mapping):
        return _parse_agent_option_mapping(value, path=path, depth=depth + 1, counters=counters)
    if isinstance(value, list):
        counters.list_items += len(value)
        if counters.list_items > _MAX_AGENT_OPTIONS_LIST_ITEMS:
            raise _AgentOptionsError(
                f"agent.options exceeds max {_MAX_AGENT_OPTIONS_LIST_ITEMS} list items"
            )
        if depth + 1 > _MAX_AGENT_OPTIONS_DEPTH:
            raise _AgentOptionsError(
                f"{_agent_options_path(path)} exceeds max depth {_MAX_AGENT_OPTIONS_DEPTH}"
            )
        return [
            _parse_agent_option_value(
                item, path=f"{path}[{index}]", depth=depth + 1, counters=counters
            )
            for index, item in enumerate(value)
        ]
    raise _AgentOptionsError(
        f"{_agent_options_path(path)} must be null/bool/int/finite float/string/list/mapping, "
        f"got {type(value).__name__}"
    )


def _parse_agent_option_scalar(value: object, *, path: str) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise _AgentOptionsError(f"{_agent_options_path(path)} must be a finite float")
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > _MAX_AGENT_OPTIONS_STRING_BYTES:
            raise _AgentOptionsError(
                f"{_agent_options_path(path)} exceeds max string length "
                f"{_MAX_AGENT_OPTIONS_STRING_BYTES} bytes"
            )
        return value
    return _UNSUPPORTED_AGENT_OPTION


_CAMEL_BOUNDARY_RE = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])"  # lowerUpper: apiKey → api_Key
    r"|(?<=[A-Z])(?=[A-Z][a-z])"  # ACRONYMWord: APIKey → API_Key
)


def _raise_if_secret_key_segment(key: str, *, path: str) -> None:
    # Split ASCII CamelCase/acronym transitions BEFORE casefold so that
    # apiKey, clientSecret, accessToken, APIKey, clientAPIKey etc. are
    # correctly tokenized and matched against reserved segments.
    camel_split = _CAMEL_BOUNDARY_RE.sub("_", key)
    normalized = unicodedata.normalize("NFKD", camel_split).casefold().strip()
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    parts = [p for p in normalized.split("_") if p]
    # Bounded sliding-window comparison: for each reserved segment's token
    # sequence (max 2 tokens), slide over parts looking for a contiguous
    # match.  O(len(parts) * number_of_reserved_patterns) — no set
    # materialization of all O(n²) subsequences.
    for seg_tokens, segment in zip(
        _SECRET_SEGMENT_TOKEN_SEQS, _SECRET_OPTION_KEY_SEGMENTS, strict=True
    ):
        seg_len = len(seg_tokens)
        if seg_len == 1:
            # Single-token segment: check exact match in parts or full normalized
            if seg_tokens[0] in parts or seg_tokens[0] == normalized:
                raise _AgentOptionsError(
                    f"{_agent_options_path(f'{path}.{key}')} uses reserved "
                    f"secret-bearing key segment {segment!r}; keep secrets in "
                    f"env vars such as agent.api_key_env"
                )
        else:
            # Multi-token segment: slide a window of seg_len over parts
            for i in range(len(parts) - seg_len + 1):
                if parts[i : i + seg_len] == list(seg_tokens):
                    raise _AgentOptionsError(
                        f"{_agent_options_path(f'{path}.{key}')} uses reserved "
                        f"secret-bearing key segment {segment!r}; keep secrets in "
                        f"env vars such as agent.api_key_env"
                    )


def _agent_options_path(path: str) -> str:
    if len(path) <= _MAX_AGENT_OPTIONS_PATH_CHARS:
        return path
    return path[: _MAX_AGENT_OPTIONS_PATH_CHARS - 3] + "..."


def _parse_favorite_namespaces(value: Any) -> tuple[tuple[str, ...], list[str]]:
    """`favorite_namespaces:` UI shortcut list (issue #108): non-empty
    strings only, capped at the nine digit keys `1`-`9`."""
    if not isinstance(value, list):
        return (), []
    names = tuple(item for item in value if isinstance(item, str) and item)
    if len(names) > 9:
        return names[:9], [
            f"favorite_namespaces: only the first 9 entries are bound to"
            f" keys 1-9; {len(names) - 9} extra entries ignored"
        ]
    return names, []


def _parse_protected_contexts(value: Any) -> tuple[str, ...]:
    """`protected_contexts:` list (issue #83): non-empty string globs only."""
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def context_is_protected(context: str | None, patterns: tuple[str, ...]) -> bool:
    """Whether *context* matches any protected-context pattern (issue #83).

    Patterns are `fnmatch` globs (e.g. `prod-*`) or literal context names.
    An unresolvable context name (None) is never treated as protected — the
    marker exists to make known-dangerous clusters loud, not to guess.
    """
    if context is None:
        return False
    return any(fnmatchcase(context, pattern) for pattern in patterns)


def _parse_column(kind: str, entry: Any) -> tuple[CustomColumn | None, str | None]:
    """(column, warning) for one `views.<kind>.columns` item; at most one is set."""
    if not isinstance(entry, dict):
        return None, f"views.{kind}: column entries must be mappings"
    name = _opt_str(entry.get("name"))
    if name is None:
        return None, f"views.{kind}: a column is missing its `name`"
    if len(name.split()) != 1:
        # :sort splits its input on whitespace — a multi-word name could
        # never be addressed by the command it promises.
        return None, f"views.{kind}: column name {name!r} must be a single token"
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


#: Custom column names that would collide with identity/sortable built-in
#: headers: `:sort CPU` would hit the builtin branch first (never the custom
#: column) and the sort arrow would decorate two identical headers.
_RESERVED_COLUMN_NAMES = frozenset({"name", "namespace", "age", "cpu", "mem"})


def _collect_columns(kind: str, raw_columns: Any) -> tuple[list[CustomColumn], list[str]]:
    """Valid, uniquely-named columns for one view; problems become warnings.

    Case-insensitive duplicates and names shadowing built-in headers are
    dropped: both would make headers ambiguous and later columns
    unreachable for `:sort`.
    """
    columns: list[CustomColumn] = []
    warnings: list[str] = []
    seen: set[str] = set()
    if raw_columns is not None and not isinstance(raw_columns, list):
        return [], [f"views.{kind}.columns must be a list of column mappings"]
    for entry in raw_columns if isinstance(raw_columns, list) else []:
        column, warning = _parse_column(kind, entry)
        if warning is not None:
            warnings.append(warning)
        if column is None:
            continue
        if column.name.lower() in _RESERVED_COLUMN_NAMES:
            warnings.append(f"views.{kind}.{column.name}: collides with a built-in column")
        elif column.name.lower() in seen:
            warnings.append(f"views.{kind}.{column.name}: duplicate column name")
        else:
            seen.add(column.name.lower())
            columns.append(column)
    return columns, warnings


def _parse_views(value: Any) -> tuple[dict[str, ViewConfig], list[str]]:
    """`views:` custom columns (issue #45): invalid columns are dropped with
    a warning instead of failing the whole config — a typo in one column
    must not take the TUI down."""
    if value is None:
        return {}, []
    if not isinstance(value, dict):
        return {}, ["views: must be a mapping of kind names to view definitions"]
    views: dict[str, ViewConfig] = {}
    warnings: list[str] = []
    for kind, view_raw in value.items():
        if not isinstance(view_raw, dict):
            warnings.append(f"views.{kind}: a view definition must be a mapping")
            continue
        if str(kind) in SYNTHETIC_VIEW_KINDS:
            # Synthetic helm views are adapted from backing Secrets — there
            # is no manifest to evaluate custom columns against.
            warnings.append(f"views.{kind}: synthetic view kinds don't support custom columns")
            continue
        if str(kind) == "secrets":
            # Security invariant: Secret values only ever render through the
            # masking pipeline — custom columns evaluate raw manifests
            # (including last-applied-configuration), so the kind is banned.
            warnings.append(
                "views.secrets: Secret values only render through the masking "
                "pipeline — custom columns are not supported"
            )
            continue
        columns, column_warnings = _collect_columns(str(kind), view_raw.get("columns"))
        warnings.extend(column_warnings)
        if columns:
            views[str(kind)] = ViewConfig(
                columns=tuple(columns), replace=view_raw.get("replace") is True
            )
    return views, warnings
