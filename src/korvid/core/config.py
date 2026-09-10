"""Single-file configuration (design doc §5-7): ~/.config/korvid/config.yaml."""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
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
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit

import yaml

from korvid.k8s.columns import SOURCES, CustomColumn, parse_jsonpath
from korvid.k8s.helm import SYNTHETIC_VIEW_KINDS
from korvid.option_keys import matched_credential_segment

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "korvid" / "config.yaml"
_MAX_AGENT_OPTIONS_DEPTH = 4
_MAX_AGENT_OPTIONS_KEYS = 64
_MAX_AGENT_OPTIONS_LIST_ITEMS = 64
_MAX_AGENT_OPTIONS_STRING_BYTES = 2048
_MAX_AGENT_OPTIONS_SERIALIZED_BYTES = 16 * 1024
_MAX_AGENT_OPTIONS_PATH_CHARS = 120

#: The Prometheus/LogQL label-name grammar. A mapped name is interpolated
#: into a selector as an identifier, so it cannot be escaped the way a
#: value is — it has to match the grammar or be refused.
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

#: Sentinel recorded for a rejected label name, so the backend is disabled
#: rather than quietly falling back to a default that would query the
#: wrong label.
_INVALID_LABEL = "\x00invalid"


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


class ConfigError(ValueError):
    """A configuration key or value is not accepted.

    Raised for unknown top-level or `agent` keys, and for an invalid
    `agent.model_tier` value. The message is always a single line so it
    reads cleanly as a `SystemExit` at startup — never let it grow an
    embedded newline.
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
    values back verbatim — and *in preference to* the modelled profile of
    the same name, because that pairing means the raw text holds the block
    korvid emptied — so saving one profile cannot delete another the
    operator still has to repair. The values are the objects `yaml.safe_load`
    already built for this same file, held opaquely and never interpreted,
    so retaining them costs nothing the loader had not already allocated.

    The *keys* are opaque for the same reason. `yaml.safe_load` builds
    integer, boolean, float, null and date keys as readily as strings, and
    `1:` is not the profile named `"1"`: recording it as one would rename
    the operator's entry, collide with a real `"1"` profile (whose
    modelled half the raw one then outranks on write), and hand the next
    load a key it accepts as a valid profile name — korvid promoting text
    it refused into a runtime connection by itself. So the file's own key
    is kept, and `profiles` stays string-only.
    """

    active: str | None = None
    profiles: Mapping[str, ModelConnectionConfig] = field(default_factory=dict)
    #: Raw, unmodelled `agent.profiles` entries under the file's own key —
    #: which YAML does not promise is a string. Opaque; never read by the
    #: runtime. Not compared: two configurations that differ only in text
    #: korvid refused to interpret are the same configuration as far as
    #: the agent is concerned.
    unparsed: Mapping[object, object] = field(default_factory=dict, compare=False)

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

    @property
    def names(self) -> frozenset[str]:
        """Every profile *name* this set occupies, modelled or not.

        What a generated name must not collide with, and what a screen can
        list: the modelled profiles plus the string keys of `unparsed`. A
        non-string `unparsed` key names nothing — no operator can type it
        and no generator can produce it — so it cannot collide, and
        folding it in with `str()` would only reintroduce the confusion
        between `1` and `"1"` that keeping the file's key avoids.
        """
        return frozenset(self.profiles) | frozenset(
            name for name in self.unparsed if isinstance(name, str)
        )


@dataclass(frozen=True)
class KorvidConfig:
    kube_context: str | None = None
    namespace: str | None = None
    #: UI-only namespace shortcuts (issue #108): bound to keys `1`-`9` in
    #: order. Purely local navigation state — never an authorization list.
    favorite_namespaces: tuple[str, ...] = ()
    #: Named model connection profiles (`agent.active` / `agent.profiles`).
    #: The single source of truth for provider configuration.
    model_connections: ModelConnectionsConfig = field(default_factory=ModelConnectionsConfig)
    #: Whether an agent can be built at all: exactly "a profile is active".
    #: `agent.active: null` is the off state.
    agent_enabled: bool = False
    #: Explicit model-capability tier override (`agent.model_tier`): `low` or
    #: `high`, or `None` for automatic routing. It is consumed by
    #: `korvid.agent.model_policy.ModelRouter`, which resolves it (together
    #: with provider-reported and shipped-catalog capabilities) into the
    #: `ResolvedAgentPolicy` that carries the session's tool surface,
    #: budgets, and prompt pack.
    agent_model_tier: str | None = None
    #: Additive house rules (`agent.rules`): short, plain-language
    #: instructions appended to the agent's system context.
    #: Each entry is a non-blank string of at most 1000 characters; at most
    #: 16 entries are kept (excess and invalid entries are dropped with a
    #: warning, never a hard failure). Composed as an additive layer by
    #: `korvid.agent.prompt_harness.PromptHarness`, which never lets a rule
    #: widen what the safety contract above it granted.
    agent_rules: tuple[str, ...] = ()
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
    #: `agent.model_search.models_dev`: whether this installation has an
    #: optional models.dev metadata source at all. `True` (the default)
    #: only means the source exists — it is contacted solely by the setup
    #: UI's explicit "refresh model metadata" action, never at startup and
    #: never on a routing call. `False` is the permanent kill switch an
    #: air-gapped deployment sets: the composition root then builds no
    #: source, so there is nothing left that *could* reach the network,
    #: and the refresh action reports itself disabled.
    agent_model_search_models_dev: bool = True
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


#: Top-level keys `load_config` reads. Any key not in this set is unsupported.
_SUPPORTED_ROOT_KEYS: frozenset[str] = frozenset(
    {
        "agent",
        "mcp",
        "logs",
        "debug",
        "node_shell",
        "ui",
        "integrations",
        "network",
        "timeline",
        "views",
        "favorite_namespaces",
        "kube_context",
        "namespace",
        "readonly",
        "protected_contexts",
        "keybindings",
        "log_buffer_lines",
        "observability",
    }
)

#: `agent:` sub-keys `load_config` reads. Any key not in this set is unsupported.
_SUPPORTED_AGENT_KEYS: frozenset[str] = frozenset(
    {
        "active",
        "profiles",
        "model_tier",
        "rules",
        "model_search",
        "disable_in_protected",
        "follow",
    }
)


def _check_unknown_root_keys(raw: dict[str, Any]) -> None:
    """Raise `ConfigError` for any unsupported top-level key."""
    for key in raw:
        if key not in _SUPPORTED_ROOT_KEYS:
            raise ConfigError(f"unsupported config key: {key!r}")


def _check_unknown_agent_keys(agent_raw: dict[str, Any]) -> None:
    """Raise `ConfigError` for any unsupported `agent:` key."""
    for key in agent_raw:
        if key not in _SUPPORTED_AGENT_KEYS:
            raise ConfigError(f"unsupported agent key: {key!r}")


def load_config(path: Path | None = None) -> KorvidConfig:
    """Load config; missing file means zero-config defaults."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.is_file():
        return KorvidConfig()
    loaded = yaml.safe_load(cfg_path.read_text())
    if loaded is None:
        raw: dict[str, Any] = {}
    elif isinstance(loaded, dict):
        raw = loaded
    else:
        raise ConfigError(f"config root must be a mapping (got {type(loaded).__name__})")
    _check_unknown_root_keys(raw)
    agent_value = raw.get("agent")
    # User-edited configs can hold scalars where mappings are expected;
    # treat anything that is not a mapping as absent instead of crashing.
    agent_raw: dict[str, Any] = agent_value if isinstance(agent_value, dict) else {}
    _check_unknown_agent_keys(agent_raw)
    warnings: list[str] = []
    model_connections = _parse_model_connections(agent_raw, warnings)
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
    views, view_warnings = _parse_views(raw.get("views"))
    warnings.extend(view_warnings)
    model_tier = (
        _parse_model_tier(agent_raw.get("model_tier")) if "model_tier" in agent_raw else None
    )
    agent_rules, rules_warnings = _parse_agent_rules(agent_raw.get("rules"))
    warnings.extend(rules_warnings)
    models_dev = _parse_models_dev(agent_raw, warnings)
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
        agent_enabled=model_connections.active_profile is not None,
        agent_model_tier=model_tier,
        agent_rules=agent_rules,
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
        agent_model_search_models_dev=models_dev,
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


def _parse_models_dev(agent_raw: Mapping[str, Any], warnings: list[str]) -> bool:
    """Parse `agent.model_search.models_dev` — the enrichment kill switch.

    Strict, not truthy: only `true` and `false` are read as themselves.
    Everything else present fails **closed**, the same rule `debug.images`
    already uses, and for the same reason — a present value korvid cannot
    interpret is still an operator trying to restrict something, and a
    quoting slip (`models_dev: 'false'`) must not silently re-enable an
    outbound fetch in an air-gapped deployment. The failure is loud: the
    warning names the key and says what was assumed.

    A `model_search` block that is not a mapping names no key at all, so
    there is no restriction to honour: it warns and keeps the default.
    """
    default = KorvidConfig.agent_model_search_models_dev
    if "model_search" not in agent_raw:
        return default
    block = agent_raw["model_search"]
    if not isinstance(block, dict):
        warnings.append(
            "agent.model_search: must be a mapping — ignored, model metadata"
            " enrichment stays available"
        )
        return default
    if "models_dev" not in block:
        return default
    value = block["models_dev"]
    if value is True or value is False:
        return value
    warnings.append(
        "agent.model_search.models_dev: must be true or false — treating"
        f" {value!r} as false, so no model metadata is fetched"
    )
    return False


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


#: The whole `agent.model_tier` vocabulary. Absent/null means automatic.
_MODEL_TIERS: frozenset[str] = frozenset({"low", "high"})


class KeepModelTier(Enum):
    """The "don't touch `agent.model_tier`" argument to the profile writer.

    A sentinel rather than `None` because `None` is a real, meaningful tier
    value — Automatic — and a save that means "clear the override" must be
    distinguishable from a save that never asked about the tier at all.
    """

    KEEP = auto()


#: The default for `save_model_connections(..., model_tier=...)`.
KEEP_MODEL_TIER: Final = KeepModelTier.KEEP

#: What a caller may hand the profile writer for the tier: an override, the
#: Automatic clear (`None`), or "leave it alone".
ModelTierWrite = str | None | KeepModelTier


class ModelConnectionsWriter(ABC):
    """The single seam that persists profiles — and the tier with them.

    Injected into the UI by the composition root so the screens never learn
    a config path, and shaped so a caller that has no opinion about the
    tier physically cannot overwrite one.

    An `abc.ABC` rather than a `Protocol` because this crosses a layer
    boundary (AGENTS.md): the UI depends on it, `core` owns it, and the
    dependency is nominal — an implementation declares that it is one, so
    a signature that drifts is caught at the implementation rather than at
    whichever call site a checker happens to reach first.
    """

    @abstractmethod
    def __call__(
        self,
        profiles: ModelConnectionsConfig,
        *,
        model_tier: ModelTierWrite = KEEP_MODEL_TIER,
    ) -> None:
        """Write `profiles`, and `model_tier` when it is not the sentinel."""


class ConfigFileModelConnectionsWriter(ModelConnectionsWriter):
    """`save_model_connections` bound to one file.

    The path is chosen once, at the composition root, and travels no
    further: what the screens hold is a writer, so no UI code is in a
    position to name a file korvid writes to.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def __call__(
        self,
        profiles: ModelConnectionsConfig,
        *,
        model_tier: ModelTierWrite = KEEP_MODEL_TIER,
    ) -> None:
        """Write `profiles` to the bound path.

        Failures propagate: a caller that has already applied the profile
        to the live session has to tell the operator the change reverts on
        restart.
        """
        save_model_connections(self._path, profiles, model_tier=model_tier)


def _thaw_config_value(value: object) -> object:
    """Undo `_freeze_config_value` recursively for serialization.

    `yaml.safe_dump` has no representer for `mappingproxy` and raises
    `RepresenterError`; tuples happen to serialize (SafeRepresenter maps
    `tuple` to `represent_list`) but round-trip back as lists anyway, so
    both are converted here rather than relying on that.

    Keys are passed through untouched. A modelled block's keys are
    already strings — the bounded validator refuses anything else — and a
    raw `unparsed` entry is the operator's own text, which this must hand
    back exactly as `yaml.safe_load` built it.
    """
    if isinstance(value, Mapping):
        return {key: _thaw_config_value(item) for key, item in value.items()}
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


#: "no raw entry for this name". A distinct object rather than `None`,
#: because `None` is a real raw entry: the file key `broken:` with no
#: value parses to it, and it must still be written back verbatim.
_NO_UNPARSED_ENTRY: Final = object()


def _tier_to_write(model_tier: ModelTierWrite) -> str | None:
    """Validate a tier bound for disk, in the vocabulary `load_config` reads.

    Raises before any file is touched: persisting `medium` would produce a
    config the next start refuses to load.
    """
    if isinstance(model_tier, str) and model_tier.strip().lower() not in _MODEL_TIERS:
        raise ValueError(
            f"model_tier must be None, 'low', or 'high' (got {model_tier!r}); "
            "pass KEEP_MODEL_TIER to leave the persisted value alone."
        )
    return model_tier.strip().lower() if isinstance(model_tier, str) else None


def save_model_connections(
    path: Path,
    profiles: ModelConnectionsConfig,
    *,
    model_tier: ModelTierWrite = KEEP_MODEL_TIER,
) -> None:
    """Write `agent.active`/`agent.profiles`, preserving everything else.

    Read-modify-write: unrelated top-level keys, unrelated `agent.*` keys
    and every `unparsed` entry survive.

    A name in `unparsed` is written from `unparsed`, even when a modelled
    profile of the same name exists — that pairing means the entry parsed
    only *partly* (a rejected `auth` or `options` block), and the raw text
    is the sole surviving copy of the block the operator has to repair.
    The two ways out are both explicit and both handled: dropping the name
    from *both* halves deletes it, and dropping it from `unparsed` alone
    lets the repaired profile be serialized over it.

    Args:
        path: The config file to rewrite.
        profiles: The profile set to persist.
        model_tier: `KEEP_MODEL_TIER` (the default) leaves `agent.model_tier`
            exactly as it is — the only correct choice for a save that never
            asked about the tier. A `str` writes that override and `None`
            removes it, both in the *same* write as the profiles, so the two
            can never disagree on disk.
    """
    tier = _tier_to_write(model_tier)
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    agent_value = raw.get("agent")
    agent: dict[str, Any] = dict(agent_value) if isinstance(agent_value, dict) else {}
    written: dict[object, Any] = {}
    for name, profile in profiles.profiles.items():
        # The raw half outranks the modelled one. A profile whose `auth`
        # or `options` was rejected lives in *both*: `profiles` holds the
        # remains with the offending block emptied, `unparsed` holds what
        # the operator wrote. Serializing the remains over the raw entry
        # would delete exactly the block that has to be repaired.
        raw_entry = profiles.unparsed.get(name, _NO_UNPARSED_ENTRY)
        written[name] = (
            _profile_to_raw(profile)
            if raw_entry is _NO_UNPARSED_ENTRY
            else _thaw_config_value(raw_entry)
        )
    for key, entry in profiles.unparsed.items():
        # `key`, not `name`: the file's key for an unmodelled entry is
        # whatever YAML built, and it is written back as that.
        if key not in written:
            written[key] = _thaw_config_value(entry)
    agent["active"] = profiles.active
    agent["profiles"] = written
    if model_tier is not KEEP_MODEL_TIER:
        # An explicit choice, including Automatic: writing it here — rather
        # than through a second writer — is what keeps the tier and the
        # profiles one atomic decision.
        if tier is not None:
            agent["model_tier"] = tier
        else:
            agent.pop("model_tier", None)
    raw["agent"] = agent
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, yaml.safe_dump(raw, sort_keys=False))


#: The auth-settings key naming the environment variable an API key lives
#: in. A *name*: nothing in this module reads the environment, so a secret
#: value can never travel from a profile into a projection or back to disk.
_AUTH_ENV_KEY_SETTING: str = "key"


def save_topbar_state(path: Path, *, expanded: bool) -> None:
    """Persist the top bar collapse/expand choice (issue #142), preserving
    unrelated keys (same read-modify-write shape as save_model_connections)."""
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
        if normalized in _MODEL_TIERS:
            return normalized
    raise ConfigError(f"agent.model_tier must be absent, null, 'low', or 'high' (got {value!r}).")


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


#: "this profile key was not written at all", as distinct from written
#: with a value korvid cannot model. `None` cannot serve: `auth:` with
#: nothing after it is a present key whose value is `None`.
_ABSENT_BLOCK: Final = object()


def _profile_block(raw: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], str | None]:
    """Split a profile's `auth:`/`options:` value into a block and a refusal.

    Missing and present are different answers, and the difference is the
    point. A missing key — and `key: null`, the YAML spelling of "not
    set" that `agent.model_tier` already reads that way — is the operator
    saying nothing, so it defaults to an empty block with no error.

    A present value that is not a mapping is the operator saying
    something korvid cannot model: `auth: environment` is a string, not a
    block. Reading it as absent would build the connection with method
    `none` while the file says a credential is in play, and would drop an
    `options:` line without a word. So it is refused through the same
    bounded validator a bad mapping goes through — one vocabulary for
    both shapes of "this block is unusable" — and the reason reaches
    `config_error`, which every provider build refuses on. `debug.images`
    fails closed on a present non-mapping for the same reason.

    Returns:
        The mapping to model (empty when absent or refused) and the
        rejection reason, or `None` when there is nothing to refuse.
    """
    value = raw.get(key, _ABSENT_BLOCK)
    if value is _ABSENT_BLOCK or value is None:
        return {}, None
    if isinstance(value, Mapping):
        return value, None
    return {}, _parse_bounded_options(value, root=key)[1]


def _record_refusal(
    config: object, attribute: Literal["settings_error", "options_error"], reason: str | None
) -> None:
    """Record on *config* why a present block was refused before modelling.

    `_validated_config_mapping` can only refuse a mapping it was handed;
    a present `auth: environment` is a shape it never sees. The reason
    still has to reach `config_error`, so it is written to the same
    frozen field `__post_init__` computes — rather than being passed
    through `__init__`, where any caller could forge one and
    `dataclasses.replace` would carry a stale one past a repair.

    `attribute` is a `Literal` of the two fields that exist: `object.__setattr__`
    would happily invent a third from a typo, and an error nothing reads is
    the same as no error at all.
    """
    if reason is not None:
        object.__setattr__(config, attribute, reason)


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
    auth_map, auth_refusal = _profile_block(raw, "auth")
    method = _opt_str(auth_map.get("method")) or "none"
    settings = {key: value for key, value in auth_map.items() if key != "method"}
    options, options_refusal = _profile_block(raw, "options")
    auth = ConnectionAuthConfig(method=method, settings=settings)
    _record_refusal(auth, "settings_error", auth_refusal)
    profile = ModelConnectionConfig(
        model=model,
        endpoint=_opt_str(raw.get("endpoint")),
        auth=auth,
        options=options,
    )
    _record_refusal(profile, "options_error", options_refusal)
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
    if raw_profiles is not None and not isinstance(raw_profiles, dict):
        warnings.append("agent.profiles is not a mapping; no agent profile was loaded")
        return ModelConnectionsConfig()
    if not isinstance(raw_profiles, dict):
        return ModelConnectionsConfig()
    profiles: dict[str, ModelConnectionConfig] = {}
    unparsed: dict[object, object] = {}
    reported_invalid_name = False
    for raw_name, raw_entry in raw_profiles.items():
        name = raw_name if type(raw_name) is str else ""
        if not is_valid_profile_name(name):
            if not reported_invalid_name:
                warnings.append(
                    "agent.profiles contains an invalid profile name; the entry was ignored"
                )
                reported_invalid_name = True
            # Under the file's own key, not `str(raw_name)`: see
            # `ModelConnectionsConfig`. A stringified key would rename the
            # entry, collide with a real profile of that name, and load
            # back as a valid profile name the next time.
            unparsed[raw_name] = raw_entry
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


#: The default profile name used when creating a new named connection profile.
DEFAULT_PROFILE_NAME: str = "default"

#: The separator between a profile's provider name and model identifier.
MODEL_REFERENCE_SEPARATOR: str = "/"


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


def _raise_if_secret_key_segment(key: str, *, path: str) -> None:
    """Refuse an option key that would hold a credential value.

    The vocabulary lives in `korvid.option_keys` rather than here because
    `providers/litellm_request.py` drops the same names on the way to the
    wire, and two copies of it drifted: the plural spellings passed this
    gate and then passed that one too.
    """
    segment = matched_credential_segment(key)
    if segment is None:
        return
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
