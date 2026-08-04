"""Provider plugin registry — selected-only entry-point loader.

Discovers third-party LLM provider plugins via
`importlib.metadata.distributions()` scanning for the
``korvid.provider`` entry-point group, but only calls `.load()` on the
*selected* entry point. Unselected plugins are never imported—this
keeps startup fast and avoids side-effects from unused packages.
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
from typing import TYPE_CHECKING

from korvid.agent.provider import LLMProvider
from korvid.agent.provider_plugin import (
    PROVIDER_PLUGIN_API_VERSION,
    ProviderPlugin,
    ValidatedPluginProvider,
)

if TYPE_CHECKING:
    from korvid.agent.credentials import CredentialSource
    from korvid.agent.provider_plugin import ProviderPluginConfig

logger = logging.getLogger(__name__)

_SEPARATOR_RE = re.compile(r"[-_.]+")
_MAX_ERROR_LENGTH = 200
_MAX_LABEL_LENGTH = 60
_ENTRY_POINT_GROUP: str = "korvid.provider"
_ALLOWED_AUTH_METHODS: frozenset[str] = frozenset({"none", "api_key", "entra"})


class ProviderPluginError(Exception):
    """Raised when provider plugin discovery, loading, or validation fails."""


def normalize_provider_name(name: str) -> str:
    """Normalize a provider name: lowercase, collapse separators to hyphens."""
    return _SEPARATOR_RE.sub("-", name.strip().lower())


def _bounded(label: str, *, max_length: int = _MAX_LABEL_LENGTH) -> str:
    """Truncate an externally-influenced label to a safe length."""
    if len(label) <= max_length:
        return label
    return label[:max_length] + "..."


def _bounded_error(msg: str) -> ProviderPluginError:
    """Create a ProviderPluginError with a message bounded to _MAX_ERROR_LENGTH."""
    if len(msg) > _MAX_ERROR_LENGTH:
        msg = msg[: _MAX_ERROR_LENGTH - 3] + "..."
    return ProviderPluginError(msg)


def _discover_entry_points() -> list[tuple[importlib.metadata.EntryPoint, str]]:
    """Discover all korvid.provider entry points across installed distributions.

    Returns a list of (entry_point, distribution_name) tuples.
    Isolated for test patching.
    """
    results: list[tuple[importlib.metadata.EntryPoint, str]] = []
    for dist in importlib.metadata.distributions():
        dist_name = dist.name or "<unknown>"
        for ep in dist.entry_points:
            if ep.group == _ENTRY_POINT_GROUP:
                results.append((ep, dist_name))
    return results


def _load_entry_point(ep: importlib.metadata.EntryPoint) -> object:
    """Load an entry point — isolated for test patching."""
    return ep.load()


def _validate_auth_methods(
    auth_methods: tuple[str, ...],
    normalized: str,
) -> None:
    """Validate auth_methods tuple contents, allowlist, and duplicates."""
    for method in auth_methods:
        if not isinstance(method, str):
            raise _bounded_error(
                f"provider plugin {_bounded(normalized)!r}: auth_methods must contain only strings"
            )
    invalid = set(auth_methods) - _ALLOWED_AUTH_METHODS
    if invalid:
        raise _bounded_error(
            f"provider plugin {_bounded(normalized)!r}: "
            f"auth_methods contains disallowed values: "
            f"{', '.join(sorted(invalid))}"
        )
    if len(auth_methods) != len(set(auth_methods)):
        raise _bounded_error(
            f"provider plugin {_bounded(normalized)!r}: auth_methods contains duplicate entries"
        )


class ProviderPluginRegistry:
    """Registry that discovers and loads provider plugins by entry point.

    Only the *selected* plugin's entry point is loaded; all others
    remain as metadata-only references.
    """

    def __init__(self) -> None:
        self._cache: dict[str, ProviderPlugin] = {}

    def _resolve_entry_point(
        self,
        normalized: str,
        safe_name: str,
    ) -> tuple[importlib.metadata.EntryPoint, str]:
        """Find exactly one entry point for *normalized*, or raise."""
        all_eps = _discover_entry_points()

        candidates: dict[str, list[tuple[importlib.metadata.EntryPoint, str]]] = {}
        for ep, dist_name in all_eps:
            ep_normalized = normalize_provider_name(ep.name)
            candidates.setdefault(ep_normalized, []).append((ep, dist_name))

        if normalized not in candidates:
            raise _bounded_error(f"no provider plugin found for {safe_name!r}")

        matches = candidates[normalized]
        if len(matches) > 1:
            dist_names = sorted(_bounded(dist) for _, dist in matches)
            raise _bounded_error(
                f"provider name {safe_name!r} is registered by multiple "
                f"distributions: {', '.join(dist_names)}"
            )
        return matches[0]

    @staticmethod
    def _validate_metadata(
        plugin: ProviderPlugin,
        normalized: str,
        safe_name: str,
    ) -> None:
        """Validate plugin metadata: api_version, name match, auth_methods."""
        try:
            meta = plugin.metadata
        except Exception:
            raise _bounded_error(
                f"provider plugin {safe_name!r} raised while accessing metadata"
            ) from None

        if meta.api_version != PROVIDER_PLUGIN_API_VERSION:
            raise _bounded_error(
                f"provider plugin {safe_name!r} has api_version "
                f"{meta.api_version}, expected {PROVIDER_PLUGIN_API_VERSION}"
            )

        meta_normalized = normalize_provider_name(meta.name)
        if meta_normalized != normalized:
            raise _bounded_error(
                f"provider plugin {safe_name!r}: metadata.name does not match entry-point name"
            )

        _validate_auth_methods(meta.auth_methods, normalized)

    def load_selected(self, name: str) -> ProviderPlugin:
        """Load and validate the provider plugin matching *name*.

        Args:
            name: Provider name (normalized for comparison).

        Returns:
            A validated ProviderPlugin instance (cached).

        Raises:
            ProviderPluginError: If discovery, loading, or validation fails.
        """
        normalized = normalize_provider_name(name)
        if normalized in self._cache:
            return self._cache[normalized]

        safe_name = _bounded(normalized)
        ep, dist_name = self._resolve_entry_point(normalized, safe_name)
        safe_dist = _bounded(dist_name)

        # Load only the selected entry point
        try:
            loaded = _load_entry_point(ep)
        except Exception:
            raise _bounded_error(
                f"failed to load provider plugin {safe_name!r} from {safe_dist}"
            ) from None

        # Validate: must be a ProviderPlugin subclass (class, not instance)
        if not (isinstance(loaded, type) and issubclass(loaded, ProviderPlugin)):
            raise _bounded_error(
                f"entry point {safe_name!r} from {safe_dist} must be a "
                f"ProviderPlugin subclass, got {type(loaded).__name__}"
            )

        # Instantiate
        try:
            plugin = loaded()
        except Exception:
            raise _bounded_error(
                f"failed to instantiate provider plugin {safe_name!r} from {safe_dist}"
            ) from None

        self._validate_metadata(plugin, normalized, safe_name)

        self._cache[normalized] = plugin
        return plugin

    def create(
        self,
        name: str,
        config: ProviderPluginConfig,
        credentials: CredentialSource | None,
    ) -> LLMProvider:
        """Create an LLMProvider from a loaded plugin, wrapped in validation.

        Args:
            name: Provider name (must have been loaded via load_selected).
            config: Plugin configuration.
            credentials: Optional credential source.

        Returns:
            A ValidatedPluginProvider wrapping the plugin's provider.

        Raises:
            ProviderPluginError: If the plugin is not loaded, auth mismatch,
                or factory failure.
        """
        normalized = normalize_provider_name(name)
        safe_name = _bounded(normalized)
        plugin = self._cache.get(normalized)
        if plugin is None:
            raise _bounded_error(
                f"provider plugin {safe_name!r} not loaded — call load_selected() first"
            )

        meta = plugin.metadata

        # Validate auth method against plugin's declared auth_methods
        if config.auth_method and config.auth_method not in meta.auth_methods:
            raise _bounded_error(
                f"provider plugin {safe_name!r} does not support "
                f"auth method {_bounded(config.auth_method)!r}; "
                f"supported: {', '.join(sorted(meta.auth_methods))}"
            )

        # Call the plugin factory — translate ALL exceptions
        try:
            provider = plugin.create(config, credentials)
        except Exception as exc:
            exc_type = type(exc).__name__
            raise _bounded_error(
                f"provider plugin {safe_name!r} factory failed: {exc_type}"
            ) from None

        # Validate return type before wrapping
        if not isinstance(provider, LLMProvider):
            raise _bounded_error(
                f"provider plugin {safe_name!r} must return an LLMProvider instance"
            )

        # Wrap in ValidatedPluginProvider for event contract enforcement
        return ValidatedPluginProvider(provider)
