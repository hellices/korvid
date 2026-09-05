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
from types import MappingProxyType
from typing import Any, TypedDict, cast
from urllib.parse import urlsplit, urlunsplit

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


#: Profile names are operator-defined identifiers, never normalized:
#: `prod-east` and `prod_east` are distinct keys so a mistyped selector can
#: never silently activate a different connection.
AGENT_PROFILE_NAME_MAX_LENGTH: int = 100
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def is_valid_profile_name(name: str) -> bool:
    """Whether *name* is a usable `agent.profiles` key."""
    return (
        type(name) is str
        and 0 < len(name) <= AGENT_PROFILE_NAME_MAX_LENGTH
        and _PROFILE_NAME_RE.match(name) is not None
    )


def _freeze_config_value(value: object) -> object:
    """Recursively copy-own a parsed value: mappings become read-only proxies,
    sequences become tuples, scalars pass through."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_config_value(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_config_value(item) for item in value)
    return value


def _freeze_config_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return cast("Mapping[str, object]", _freeze_config_value(dict(value)))


def _validated_config_mapping(
    value: Mapping[str, object], *, root: str
) -> tuple[Mapping[str, object], str | None]:
    """Bound-check *value*, then freeze it.

    Validation runs on the *raw* mapping, before freezing, so the size,
    depth and secret-key rules see the values a human wrote rather than
    the proxies and tuples the freeze produces. On rejection the mapping
    collapses to empty and the reason travels with it — a profile that
    silently kept half its options would be worse than one that visibly
    has none.

    *root* is the short path name the message uses (`options` or `auth`);
    the parser prefixes it with the profile name when it warns.

    Returns:
        The frozen mapping and `None`, or an empty mapping and the
        rejection reason. The reason never quotes a value.
    """
    sanitized, error = _parse_bounded_options(value, root=root)
    if error is not None:
        return MappingProxyType({}), error
    return _freeze_config_mapping(sanitized), None


@dataclass(frozen=True)
class ConnectionAuthConfig:
    """How a profile authenticates, as bounded copy-owned configuration.

    Core does not interpret provider-specific methods: `method` is one of
    the five common ids (`none`, `environment`, `keyring`,
    `provider-default`, `device-login`) and `settings` carries the
    method-specific *references* (never secret values) an adapter
    descriptor validates.
    """

    method: str = "none"
    settings: Mapping[str, object] = field(default_factory=dict)
    #: Why `settings` was emptied, or None. Not an `__init__` argument and
    #: not compared: two configs that differ only in *why* a rejected
    #: mapping is empty are the same configuration.
    settings_error: str | None = field(default=None, init=False, compare=False)

    # A frozen dataclass would otherwise be hashable, but `settings` is a
    # `MappingProxyType` over a dict — hashing this would raise from deep
    # inside `hash(tuple(...))` at some unrelated call site instead of here.
    __hash__ = None  # type: ignore[assignment]  # frozen but genuinely unhashable

    def __post_init__(self) -> None:
        settings, error = _validated_config_mapping(self.settings, root="auth")
        object.__setattr__(self, "settings", settings)
        object.__setattr__(self, "settings_error", error)


@dataclass(frozen=True)
class ModelConnectionConfig:
    """One named model connection."""

    model: str
    endpoint: str | None = None
    auth: ConnectionAuthConfig = field(default_factory=ConnectionAuthConfig)
    options: Mapping[str, object] = field(default_factory=dict)
    #: Why `options` was emptied, or None. See `ConnectionAuthConfig.settings_error`.
    options_error: str | None = field(default=None, init=False, compare=False)

    __hash__ = None  # type: ignore[assignment]  # frozen but genuinely unhashable

    def __post_init__(self) -> None:
        options, error = _validated_config_mapping(self.options, root="options")
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "options_error", error)

    @property
    def config_error(self) -> str | None:
        """The first reason this profile cannot be trusted, or None.

        Anything that builds a provider from a profile checks this and
        refuses rather than connecting with silently discarded settings.
        """
        return self.options_error or self.auth.settings_error


@dataclass(frozen=True)
class ModelConnectionsConfig:
    """The configured connection collection and which one is active.

    `profiles` preserves the order the entries appeared in the file. That
    order is the operator's, and it is what the wizard's profile list and
    the `:model` picker render.

    `unparsed` is the escape hatch that keeps a save honest: it maps the
    file key of every entry korvid could **not** fully model — an invalid
    name, a non-mapping, a missing `model:`, or a profile whose `options`
    or `auth` block was rejected — to that entry's raw YAML value. Nothing
    in the runtime reads it: it is not consulted by `active_profile`, by
    the wizard's list, by `:model`, or by any provider construction. Its
    only consumer is `save_model_connections` (Task 3), which writes those
    values back verbatim so saving one profile cannot delete another the
    operator still has to repair. The values are the objects `yaml.safe_load`
    already built for this same file, held opaquely and never interpreted,
    so retaining them costs nothing the loader had not already allocated.
    """

    active: str | None = None
    profiles: Mapping[str, ModelConnectionConfig] = field(default_factory=dict)
    #: Raw, unmodelled `agent.profiles` entries keyed by file key. Opaque;
    #: never read by the runtime. Not compared: two configurations that
    #: differ only in text korvid refused to interpret are the same
    #: configuration as far as the agent is concerned.
    unparsed: Mapping[str, object] = field(default_factory=dict, compare=False)

    __hash__ = None  # type: ignore[assignment]  # frozen but genuinely unhashable

    def __post_init__(self) -> None:
        object.__setattr__(self, "profiles", MappingProxyType(dict(self.profiles)))
        object.__setattr__(self, "unparsed", MappingProxyType(dict(self.unparsed)))

    @property
    def active_profile(self) -> ModelConnectionConfig | None:
        """The active profile, or None when unset or unknown.

        Only `profiles` is consulted — an `unparsed` entry can never
        become the active connection.
        """
        if self.active is None:
            return None
        return self.profiles.get(self.active)


@dataclass(frozen=True)
class KorvidConfig:
    kube_context: str | None = None
    namespace: str | None = None
    #: UI-only namespace shortcuts (issue #108): bound to keys `1`-`9` in
    #: order. Purely local navigation state — never an authorization list.
    favorite_namespaces: tuple[str, ...] = ()
    #: Named model connection profiles (`agent.active` / `agent.profiles`).
    #: The single source of truth for provider configuration; the legacy
    #: scalars below are derived from `model_connections.active_profile`
    #: during the compatibility cycle and are removed with it.
    model_connections: ModelConnectionsConfig = field(default_factory=ModelConnectionsConfig)
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
    agent_ollama_num_predict: int | None = None
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
    warnings: list[str] = []
    model_connections = _resolve_model_connections(agent_raw, warnings)
    if "profiles" in agent_raw:
        # New profile format: derive scalars from the active profile.
        scalars = _derive_legacy_scalars(model_connections, warnings)
        agent_enabled = scalars["agent_enabled"]
        agent_provider = scalars["agent_provider"]
        agent_base_url = scalars["agent_base_url"]
        agent_model = scalars["agent_model"]
        agent_api_key_env = scalars["agent_api_key_env"]
        agent_auth_method = scalars["agent_auth_method"]
        agent_options = scalars["agent_options"]
        agent_options_error = scalars["agent_options_error"]
    else:
        # Legacy format: read scalars directly from agent_raw.
        provider_raw: str | None = agent_raw.get("provider")
        # Canonicalize early: github_copilot, GitHub.Copilot etc. all become
        # github-copilot so auth-method defaults and the composition root's
        # OAuth token lookup match without case/separator awareness.
        agent_provider = (
            _canonicalize_provider_name(provider_raw) if isinstance(provider_raw, str) else None
        )
        # Auto-activation: provider present -> on, unless explicitly disabled (§6.3).
        agent_enabled = bool(agent_provider) and agent_raw.get("enabled", True) is not False
        agent_api_key_env = _opt_str(agent_raw.get("api_key_env"))
        auth_value = agent_raw.get("auth")
        auth_raw: dict[str, Any] = auth_value if isinstance(auth_value, dict) else {}
        agent_options, agent_options_error = (
            _parse_agent_options(agent_raw["options"]) if "options" in agent_raw else ({}, None)
        )
        agent_auth_method = _opt_str(auth_raw.get("method"))
        if agent_auth_method is None and agent_provider:
            # Back-compat: configs written before agent.auth existed.
            agent_auth_method = _legacy_auth_method(agent_raw, agent_provider)
        agent_base_url = _opt_str(agent_raw.get("base_url"))
        agent_model = _opt_str(agent_raw.get("model"))
        if agent_options_error is not None:
            warnings.append(agent_options_error)
    ollama_raw = _legacy_ollama_raw(agent_raw)
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
    warnings.extend(view_warnings)
    model_tier = (
        _parse_model_tier(agent_raw.get("model_tier")) if "model_tier" in agent_raw else None
    )
    agent_rules, rules_warnings = _parse_agent_rules(agent_raw.get("rules"))
    warnings.extend(rules_warnings)
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
        model_connections=model_connections,
        agent_enabled=agent_enabled,
        agent_provider=agent_provider,
        agent_base_url=agent_base_url,
        agent_model=agent_model,
        agent_api_key_env=agent_api_key_env,
        agent_auth_method=agent_auth_method,
        agent_options=agent_options,
        agent_options_error=agent_options_error,
        agent_model_tier=model_tier,
        agent_rules=agent_rules,
        agent_ollama_num_ctx=_parse_num_ctx(ollama_raw.get("num_ctx")),
        agent_ollama_temperature=_parse_temperature(ollama_raw.get("temperature")),
        agent_ollama_seed=_parse_seed(ollama_raw.get("seed")),
        agent_ollama_think=ollama_raw.get("think") is True,
        agent_ollama_keep_alive=_parse_keep_alive(ollama_raw.get("keep_alive")),
        agent_ollama_num_predict=_parse_num_predict(ollama_raw.get("num_predict"), warnings),
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


#: Agent-level keys the legacy shape owned. `save_model_connections` removes
#: them once it has written the new shape, so the first successful save
#: upgrades the file rather than leaving two shapes to disagree.
#: `enabled` is included: `active: null` is the new off switch.
LEGACY_AGENT_KEYS: tuple[str, ...] = (
    "provider",
    "model",
    "base_url",
    "api_key_env",
    "auth",
    "ollama",
    "options",
    "enabled",
)


def _thaw_config_value(value: object) -> object:
    """Undo `_freeze_config_value` recursively for serialization.

    `yaml.safe_dump` has no representer for `mappingproxy` and raises
    `RepresenterError`; tuples happen to serialize (SafeRepresenter maps
    `tuple` to `represent_list`) but round-trip back as lists anyway, so
    both are converted here rather than relying on that.
    """
    if isinstance(value, Mapping):
        return {str(key): _thaw_config_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw_config_value(item) for item in value]
    return value


def _profile_to_raw(profile: ModelConnectionConfig) -> dict[str, Any]:
    entry: dict[str, Any] = {"model": profile.model}
    if profile.endpoint is not None:
        entry["endpoint"] = profile.endpoint
    auth: dict[str, Any] = {"method": profile.auth.method}
    auth.update(cast("dict[str, Any]", _thaw_config_value(profile.auth.settings)))
    entry["auth"] = auth
    options = cast("dict[str, Any]", _thaw_config_value(profile.options))
    if options:
        entry["options"] = options
    return entry


def save_model_connections(path: Path, profiles: ModelConnectionsConfig) -> None:
    """Write `agent.active`/`agent.profiles`, preserving everything else.

    Read-modify-write: unrelated top-level keys, unrelated `agent.*` keys
    and every `unparsed` entry survive. Only the keys in
    `LEGACY_AGENT_KEYS` are removed, and only after the new shape is in
    place.
    """
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    agent_value = raw.get("agent")
    agent: dict[str, Any] = dict(agent_value) if isinstance(agent_value, dict) else {}
    written: dict[str, Any] = {
        name: _profile_to_raw(profile) for name, profile in profiles.profiles.items()
    }
    for name, entry in profiles.unparsed.items():
        if name not in written:
            written[name] = _thaw_config_value(entry)
    agent["active"] = profiles.active
    agent["profiles"] = written
    for key in LEGACY_AGENT_KEYS:
        agent.pop(key, None)
    raw["agent"] = agent
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, yaml.safe_dump(raw, sort_keys=False))


#: Provider prefixes the *interim* legacy transport cannot serve. Between
#: this task and Task 15 the running transport is still the legacy one,
#: which speaks only bearer-token OpenAI-compatible HTTP, Azure and
#: Ollama. Anything else must disable the agent visibly rather than be
#: silently routed through a bearer-token client — sending an
#: `Authorization: Bearer` to a vendor that expects its own header is a
#: credential leak, not a degraded experience. Deleted in Task 18.
_PREFIXES_WITHOUT_LEGACY_TRANSPORT: frozenset[str] = frozenset(
    {"anthropic", "bedrock", "gemini", "vertex_ai", "cohere", "mistral", "groq", "xai"}
)


def _legacy_azure_base_url(profile: ModelConnectionConfig) -> str | None:
    """Rebuild the deployment-scoped URL the legacy transport needs."""
    if profile.endpoint is None:
        return None
    deployment = profile.options.get("azure_deployment")
    if not isinstance(deployment, str) or not deployment:
        return profile.endpoint
    return f"{profile.endpoint.rstrip('/')}/openai/deployments/{deployment}"


@dataclass(frozen=True, slots=True)
class LegacyTransportProjection:
    """One profile as the scalars the *interim* legacy transport speaks.

    Temporary, and deleted with the transport in Task 18. It exists as a
    public value so startup, the `:ai` wizard's connection probe and a
    profile switch all reach the transport through one projection — a
    second, parallel one is how an Azure deployment path or an unsupported
    provider prefix ends up handled one way at boot and another way at
    runtime.
    """

    provider: str
    auth_method: str
    base_url: str | None
    model: str
    api_key_env: str | None
    options: dict[str, object]


def project_legacy_transport(
    profile: ModelConnectionConfig,
) -> tuple[LegacyTransportProjection | None, str | None]:
    """Project *profile* onto the legacy transport, or say why it cannot be.

    Returns `(projection, None)` when the transport can serve the profile
    and `(None, reason)` when it cannot. It refuses rather than guesses:
    routing a provider the legacy client cannot speak through a
    bearer-token HTTP call would send `Authorization: Bearer` to a vendor
    that expects its own header, which is a credential leak rather than a
    degraded experience.

    Args:
        profile: The connection to project.

    Returns:
        The projection, or `None` paired with a human-readable refusal.
    """
    if profile.config_error is not None:
        return None, f"the profile was rejected: {profile.config_error}"
    sep = MODEL_REFERENCE_SEPARATOR
    if sep not in profile.model:
        return None, f"model {profile.model!r} has no provider prefix"
    prefix, tag = profile.model.split(sep, 1)
    if prefix in _PREFIXES_WITHOUT_LEGACY_TRANSPORT:
        return None, f"the {prefix!r} provider needs the new transport (Task 15)"
    api_key_env: str | None = None
    if profile.auth.method == "environment":
        key_val = profile.auth.settings.get("key")
        if isinstance(key_val, str):
            api_key_env = key_val
    return (
        LegacyTransportProjection(
            provider=prefix,
            auth_method=profile.auth.method,
            base_url=_legacy_azure_base_url(profile) if prefix == "azure" else profile.endpoint,
            model=tag,
            api_key_env=api_key_env,
            options=cast("dict[str, object]", _thaw_config_value(profile.options)),
        ),
        None,
    )


class _LegacyScalars(TypedDict):
    agent_enabled: bool
    agent_provider: str | None
    agent_base_url: str | None
    agent_model: str | None
    agent_api_key_env: str | None
    agent_auth_method: str | None
    agent_options: dict[str, object]
    agent_options_error: str | None


def _empty_legacy_scalars() -> _LegacyScalars:
    return _LegacyScalars(
        agent_enabled=False,
        agent_provider=None,
        agent_base_url=None,
        agent_model=None,
        agent_api_key_env=None,
        agent_auth_method=None,
        agent_options={},
        agent_options_error=None,
    )


def _derive_legacy_scalars(profiles: ModelConnectionsConfig, warnings: list[str]) -> _LegacyScalars:
    """Project the active profile onto the pre-profile scalar fields.

    Temporary. It exists only so commit groups 1-3 stay buildable while
    the transport is still the legacy one, and Task 18 deletes it. The
    projection itself is `project_legacy_transport`, shared with every
    runtime path that has to reach the same transport; this wrapper adds
    only the config-file context a startup warning needs.
    """
    profile = profiles.active_profile
    if profile is None:
        return _empty_legacy_scalars()
    projection, refusal = project_legacy_transport(profile)
    if projection is None:
        warnings.append(f"agent.profiles.{profiles.active}: {refusal} — the agent is disabled")
        return _empty_legacy_scalars()
    return _LegacyScalars(
        agent_enabled=True,
        agent_provider=projection.provider,
        agent_base_url=projection.base_url,
        agent_model=projection.model,
        agent_api_key_env=projection.api_key_env,
        agent_auth_method=projection.auth_method,
        agent_options=projection.options,
        agent_options_error=None,
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
    (returns None), matching an absent key. String case and surrounding
    whitespace are normalized; any other value must mean `low` or `high` —
    legacy `full`/`small`, `auto`, and typos are hard errors (unlike the old
    `agent.profile`, which silently fell back).
    """
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("low", "high"):
            return normalized
    raise ConfigMigrationError(
        f"agent.model_tier must be absent, null, 'low', or 'high' (got {value!r})."
    )


def _parse_num_ctx(value: Any) -> int:
    """Coerce `agent.ollama.num_ctx` to a positive int; fall back to 16384."""
    parsed = _parse_positive_int(value)
    return parsed if parsed is not None else 16384


def _parse_positive_int(value: Any) -> int | None:
    """Coerce a value to a positive int, or None.

    Permissive on purpose (existing `num_ctx`/legacy compatibility): a
    numeric string or a value `int()` can otherwise accept is coerced
    rather than rejected. `num_predict` does *not* use this — see
    `_parse_num_predict` for that stricter contract.
    """
    if isinstance(value, bool):  # YAML `true` would silently become 1
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _parse_num_predict(value: Any, warnings: list[str]) -> int | None:
    """Coerce `agent.ollama.num_predict` to a strictly positive `int`, or None.

    Unlike `_parse_positive_int` (kept for `num_ctx`'s existing permissive
    compatibility), this rejects anything that is not *already* an actual
    positive `int`: a `bool` (a stealth `int` subclass), a `float` (even
    one that looks integral, like `2.0`, or truncates cleanly, like
    `1.9`), a numeric string, and any non-positive integer. An absent
    value is silently `None` — the provider then omits the option. A
    *provided* invalid value both resolves to `None` and appends a
    startup config warning, so a typo is surfaced instead of silently
    capping (or not capping) generation.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        warnings.append("agent.ollama.num_predict: must be a positive integer — ignoring the value")
        return None
    return value


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
    root: str = "agent.options"
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


def _parse_bounded_options(value: Any, *, root: str) -> tuple[dict[str, object], str | None]:
    """Validate *value* as a bounded, secret-free option mapping.

    *root* is the configuration path the messages name, so the same rules
    guard `agent.options`, a profile's `options` and a profile's `auth`
    settings without any of them inventing its own limits.

    Returns:
        The accepted mapping and `None`, or `{}` and a reason. The reason
        names the offending *path*, never the offending value.
    """
    if not isinstance(value, Mapping):
        return {}, f"{root} must be a mapping with string keys"
    counters = _AgentOptionCounters(root=root)
    try:
        parsed = _parse_agent_option_mapping(value, path=root, depth=1, counters=counters)
        serialized = json.dumps(
            parsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except _AgentOptionsError as exc:
        return {}, str(exc)
    except (TypeError, ValueError) as exc:
        return {}, f"{root} could not be serialized safely: {type(exc).__name__}"
    if len(serialized) > _MAX_AGENT_OPTIONS_SERIALIZED_BYTES:
        return (
            {},
            f"{root} exceeds max serialized budget {_MAX_AGENT_OPTIONS_SERIALIZED_BYTES} bytes",
        )
    return parsed, None


def _parse_agent_options(value: Any) -> tuple[dict[str, object], str | None]:
    """`agent.options`, validated. Thin wrapper over `_parse_bounded_options`."""
    return _parse_bounded_options(value, root="agent.options")


def _parse_profile_entry(
    name: str, raw: object, warnings: list[str]
) -> ModelConnectionConfig | None:
    """One `agent.profiles.<name>` entry, or None when unusable."""
    if not isinstance(raw, dict):
        warnings.append(f"agent.profiles[{name}] is not a mapping; the profile was ignored")
        return None
    model = _opt_str(raw.get("model"))
    if model is None:
        warnings.append(f"agent.profiles[{name}] has no model reference; the profile was ignored")
        return None
    auth_raw = raw.get("auth")
    auth_map: dict[str, Any] = auth_raw if isinstance(auth_raw, dict) else {}
    method = _opt_str(auth_map.get("method")) or "none"
    settings = {key: value for key, value in auth_map.items() if key != "method"}
    options_raw = raw.get("options")
    options: Mapping[str, object] = options_raw if isinstance(options_raw, dict) else {}
    profile = ModelConnectionConfig(
        model=model,
        endpoint=_opt_str(raw.get("endpoint")),
        auth=ConnectionAuthConfig(method=method, settings=settings),
        options=options,
    )
    # The dataclasses validated and (on rejection) emptied these mappings;
    # the parser is the layer that knows the profile's name, so it is the
    # layer that turns the reason into an operator-facing warning. The
    # profile is *kept* — with an empty mapping and a recorded reason — so
    # `:ai` can show it and let the operator fix it, but anything that
    # builds a provider refuses while `config_error` is set.
    if profile.options_error is not None:
        warnings.append(f"agent.profiles[{name}].options was rejected: {profile.options_error}")
    if profile.auth.settings_error is not None:
        warnings.append(f"agent.profiles[{name}].auth was rejected: {profile.auth.settings_error}")
    return profile


def _parse_model_connections(
    agent_raw: dict[str, Any], warnings: list[str]
) -> ModelConnectionsConfig:
    """Parse the `agent.active`/`agent.profiles` shape."""
    raw_profiles = agent_raw.get("profiles")
    if not isinstance(raw_profiles, dict):
        warnings.append("agent.profiles is not a mapping; no agent profile was loaded")
        return ModelConnectionsConfig()
    profiles: dict[str, ModelConnectionConfig] = {}
    unparsed: dict[str, object] = {}
    reported_invalid_name = False
    for raw_name, raw_entry in raw_profiles.items():
        name = raw_name if type(raw_name) is str else ""
        if not is_valid_profile_name(name):
            if not reported_invalid_name:
                warnings.append(
                    "agent.profiles contains an invalid profile name; the entry was ignored"
                )
                reported_invalid_name = True
            unparsed[str(raw_name)] = raw_entry
            continue
        parsed = _parse_profile_entry(name, raw_entry, warnings)
        if parsed is None:
            # korvid could not model it; keep the text so a later save
            # rewrites it untouched instead of deleting the operator's work.
            unparsed[name] = raw_entry
            continue
        if parsed.config_error is not None:
            # Kept, but with an emptied block. The rejected block is the
            # one thing the operator has to edit, so it must survive a save.
            unparsed[name] = raw_entry
        profiles[name] = parsed
    active = _opt_str(agent_raw.get("active"))
    if active is not None and active not in profiles:
        warnings.append(f"agent.active names an unknown profile {active!r}; the agent is disabled")
        active = None
    return ModelConnectionsConfig(active=active, profiles=profiles, unparsed=unparsed)


#: The in-memory profile name a legacy `agent.provider` config migrates into.
LEGACY_PROFILE_NAME: str = "default"

#: The separator between a profile's provider name and model identifier.
MODEL_REFERENCE_SEPARATOR: str = "/"

#: Legacy provider names that meant "an OpenAI-compatible endpoint".
#: `azure` is deliberately absent: Azure OpenAI authenticates with the raw
#: `api-key` header (or an Entra token) rather than a bearer token, so it
#: keeps its own `azure/` adapter instead of collapsing into `openai/`.
_LEGACY_OPENAI_COMPAT_NAMES: frozenset[str] = frozenset(
    {"openai-compat", "openai", "vllm", "github", "anthropic", "claude"}
)

#: Legacy provider names whose credential handling changed with the
#: migration and therefore warrant a one-line warning on load.
_LEGACY_REVIEW_NAMES: frozenset[str] = frozenset({"azure"})

#: Legacy `agent.ollama.*` keys carried into the migrated profile's options
#: so the writer's new shape preserves the operator's tuning.
_LEGACY_OLLAMA_KEYS: tuple[str, ...] = (
    "num_ctx",
    "temperature",
    "seed",
    "think",
    "keep_alive",
    "num_predict",
)

#: The legacy `agent.ollama.*` knobs whose pre-profile parser coerced a
#: numeric *string* to a number, mapped to the type it produced.
#: `OllamaOptions` is a plain dataclass with no validation, so a `"8192"`
#: that survived migration would be sent as a JSON string and would land
#: in `context_window_tokens` as a `str`.
_LEGACY_OLLAMA_NUMERIC_KEYS: Mapping[str, type[int] | type[float]] = MappingProxyType(
    {"num_ctx": int, "seed": int, "temperature": float}
)

#: `num_predict` is deliberately absent from the coercion table above.
#: Its pre-profile parser was the *strict* one: it refused a numeric
#: string, a fractional float, a `bool` and a non-positive value outright
#: instead of coercing them (`tests/core/test_config.py` pins all four).
#: Migration keeps that contract by dropping the key, which lands on
#: `OllamaOptions.num_predict = None` — the same effective value the old
#: fallback produced.
_LEGACY_OLLAMA_STRICT_INT_KEYS: frozenset[str] = frozenset({"num_predict"})

#: Legacy auth methods → the five common method ids.
_LEGACY_AUTH_METHODS: Mapping[str, str] = MappingProxyType(
    {
        "api_key": "environment",
        "entra": "provider-default",
        "device-login": "device-login",
        "none": "none",
    }
)


def _legacy_model_reference(provider: str, model: str) -> str:
    """`provider/model` for a legacy provider name.

    Translated at this one parser boundary: nothing downstream branches on
    a legacy provider name again.
    """
    if provider in _LEGACY_OPENAI_COMPAT_NAMES:
        return f"openai{MODEL_REFERENCE_SEPARATOR}{model}"
    return f"{provider}{MODEL_REFERENCE_SEPARATOR}{model}"


def _legacy_auth_method(agent_raw: dict[str, Any], provider: str) -> str:
    auth_value = agent_raw.get("auth")
    auth_map: dict[str, Any] = auth_value if isinstance(auth_value, dict) else {}
    legacy_method = _opt_str(auth_map.get("method"))
    api_key_env = _opt_str(agent_raw.get("api_key_env"))
    if legacy_method is None:
        if provider == "github-copilot":
            return "device-login"
        return "api_key" if api_key_env else "none"
    return legacy_method


def _legacy_auth(agent_raw: dict[str, Any], provider: str) -> ConnectionAuthConfig:
    legacy_method = _legacy_auth_method(agent_raw, provider)
    method = _LEGACY_AUTH_METHODS.get(legacy_method, legacy_method)
    api_key_env = _opt_str(agent_raw.get("api_key_env"))
    settings: dict[str, object] = {}
    if method == "environment" and api_key_env:
        settings["key"] = api_key_env
    return ConnectionAuthConfig(method=method, settings=settings)


def _legacy_ollama_raw(agent_raw: dict[str, Any]) -> dict[str, Any]:
    ollama_value = agent_raw.get("ollama")
    return ollama_value if isinstance(ollama_value, dict) else {}


def _legacy_options(
    agent_raw: dict[str, Any], provider: str, warnings: list[str]
) -> dict[str, object]:
    """Options carried into the migrated profile.

    Only `provider: ollama` had a legacy tuning block, so only `ollama`
    reads `agent.ollama.*`. Copying those keys into, say, an `openai`
    profile would invent settings the operator never wrote and that the
    adapter would then have to ignore.

    Migrated `ollama` profiles also get `native_api: True`. The legacy
    transport was `OllamaProvider`'s `/api/chat` route, which returns
    per-tool-call reasoning the OpenAI dialect cannot carry (Task 17). A
    *new* `ollama:` profile defaults to the shared route; an *existing*
    install keeps the transport it was already running, because a
    migration that silently changes the wire protocol is not "read
    without changes".

    Values are copied verbatim with one exception: the numeric knobs are
    coerced (`num_ctx`, `seed`, `temperature`) or strictly validated
    (`num_predict`), because the pre-profile parser did that and Task 17
    deletes it along with the scalars. Anything that will not coerce is
    **dropped with a warning** rather than replaced by an invented
    default — the default the old parser substituted is `OllamaOptions`'
    own field default, which a migrated profile still reaches through
    `native_api: True`, so dropping restores exactly the old effective
    value while also telling the operator which line to fix.
    """
    options: dict[str, object] = {}
    if provider == "ollama":
        options.update(_legacy_ollama_options(_legacy_ollama_raw(agent_raw), warnings))
        options["native_api"] = True
    extra = agent_raw.get("options")
    if isinstance(extra, dict):
        options.update(extra)
    return options


def _legacy_ollama_options(ollama_raw: dict[str, Any], warnings: list[str]) -> dict[str, object]:
    """The `agent.ollama.*` block as profile options. See `_legacy_options`."""
    options: dict[str, object] = {}
    for key in _LEGACY_OLLAMA_KEYS:
        if key not in ollama_raw:
            continue
        value = ollama_raw[key]
        if key in _LEGACY_OLLAMA_STRICT_INT_KEYS:
            # `bool` is an `int` subclass, so YAML `true` must not pass here.
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                warnings.append(
                    f"agent.ollama.{key}: must be a positive integer — the value was dropped"
                )
                continue
            options[key] = value
            continue
        cast_to = _LEGACY_OLLAMA_NUMERIC_KEYS.get(key)
        if cast_to is None:
            options[key] = value
            continue
        coerced = _legacy_ollama_number(key, value, cast_to, warnings)
        if coerced is not None:
            options[key] = coerced
    return options


def _legacy_ollama_number(
    key: str, value: object, cast_to: type[int] | type[float], warnings: list[str]
) -> int | float | None:
    """One permissive numeric knob, coerced the way the old parser was.

    Returns `None` for "drop it" — the caller tests `is not None` rather
    than truthiness, because `seed: 0` and `temperature: 0.0` are both
    valid values that a truthiness test would silently discard.
    """
    # `bool` is an `int` subclass, so YAML `true` would coerce to 1.
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        warnings.append(f"agent.ollama.{key}: must be a number — the value was dropped")
        return None
    try:
        coerced = cast_to(value)
    except (TypeError, ValueError, OverflowError):
        # `.inf` reaches `int()` as OverflowError, `.nan` as ValueError.
        warnings.append(f"agent.ollama.{key}: must be a number — the value was dropped")
        return None
    if isinstance(coerced, float) and not isfinite(coerced):
        # `.inf`/`.nan` survive `float()`, and the bounded validator
        # refuses them — which would reject the whole migrated profile
        # over one tuning knob the old parser quietly replaced.
        warnings.append(f"agent.ollama.{key}: must be finite — the value was dropped")
        return None
    return coerced


def _migrate_azure_endpoint(base_url: str) -> tuple[str, str | None, str | None]:
    """Reduce a legacy Azure `base_url` to the resource URL it was built from.

    The legacy transport posted to `f"{base_url}/chat/completions"`, so a
    working legacy value was already deployment- or version-scoped —
    `https://x.openai.azure.com/openai/deployments/<name>` or
    `https://x.openai.azure.com/openai/v1`. `AzureProvider` takes the
    *resource* URL and builds the `/openai/...` path itself; given the old
    value it appends rather than replaces, producing
    `.../openai/deployments/<name>/openai/chat/completions` or
    `.../openai/v1/openai/deployments/<model>/chat/completions`. Both 404.

    Everything from the first `/openai` segment onward is therefore
    dropped, and any deployment name it encoded is returned so the caller
    can preserve it rather than lose it.

    Returns:
        The resource URL, the deployment name the old URL encoded (or
        None), and a warning naming both the old and the new value (or
        None when nothing was rewritten).
    """
    split = urlsplit(base_url)
    segments = [segment for segment in split.path.split("/") if segment]
    resource = urlunsplit((split.scheme, split.netloc, "", "", ""))
    if "openai" not in segments:
        if not segments and not split.query and not split.fragment:
            return resource, None, None
        # A path korvid does not recognise: leave the value alone rather
        # than guess. The adapter will surface the failure with the real
        # URL in it, which is more useful than a silent rewrite.
        return base_url, None, None
    tail = segments[segments.index("openai") + 1 :]
    deployment = tail[1] if len(tail) >= 2 and tail[0] == "deployments" else None
    warning = (
        f"agent.base_url {base_url!r} was rewritten to {resource!r} for the azure "
        "adapter, which builds the /openai/deployments path itself"
    )
    if deployment is not None:
        warning += f"; the deployment name {deployment!r} was kept as options.azure_deployment"
    return resource, deployment, warning


def _migrate_legacy_agent(agent_raw: dict[str, Any], warnings: list[str]) -> ModelConnectionsConfig:
    """Normalize a legacy `agent.provider` config into one `default` profile."""
    provider_raw = agent_raw.get("provider")
    if not isinstance(provider_raw, str) or not provider_raw.strip():
        return ModelConnectionsConfig()
    provider = _canonicalize_provider_name(provider_raw)
    model = _opt_str(agent_raw.get("model"))
    if model is None:
        warnings.append("agent.provider is set but agent.model is missing; the agent is disabled")
        return ModelConnectionsConfig()
    endpoint = _opt_str(agent_raw.get("base_url"))
    options = _legacy_options(agent_raw, provider, warnings)
    if provider == "azure" and endpoint is not None:
        endpoint, deployment, endpoint_warning = _migrate_azure_endpoint(endpoint)
        if deployment is not None:
            options.setdefault("azure_deployment", deployment)
        if endpoint_warning is not None:
            warnings.append(endpoint_warning)
    profile = ModelConnectionConfig(
        model=_legacy_model_reference(provider, model),
        endpoint=endpoint,
        auth=_legacy_auth(agent_raw, provider),
        options=options,
    )
    if provider in _LEGACY_REVIEW_NAMES:
        # The credential *reference* survives, but where Entra was implicit
        # the method is now spelled out. Saying so beats a silent 401.
        warnings.append(
            f"agent.provider {provider!r} migrated to an {provider} profile; "
            "check auth.method (provider-default for Entra ID) in :ai"
        )
    enabled = agent_raw.get("enabled", True) is not False
    return ModelConnectionsConfig(
        active=LEGACY_PROFILE_NAME if enabled else None,
        profiles={LEGACY_PROFILE_NAME: profile},
    )


def _resolve_model_connections(
    agent_raw: dict[str, Any], warnings: list[str]
) -> ModelConnectionsConfig:
    """Route agent config to new-shape parser or legacy migration."""
    if "profiles" in agent_raw:
        if "provider" in agent_raw:
            warnings.append(
                "agent.profiles is present; the legacy agent.provider fields were ignored"
            )
        return _parse_model_connections(agent_raw, warnings)
    return _migrate_legacy_agent(agent_raw, warnings)


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
            raise _AgentOptionsError(f"{counters.root} must use string keys")
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
                f"{counters.root} exceeds max {_MAX_AGENT_OPTIONS_KEYS} mapping keys"
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
    if isinstance(value, list | tuple):
        counters.list_items += len(value)
        if counters.list_items > _MAX_AGENT_OPTIONS_LIST_ITEMS:
            raise _AgentOptionsError(
                f"{counters.root} exceeds max {_MAX_AGENT_OPTIONS_LIST_ITEMS} list items"
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
