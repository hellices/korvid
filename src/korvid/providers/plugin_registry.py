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

from korvid.agent.provider_plugin import (
    PROVIDER_PLUGIN_API_VERSION,
    ProviderPlugin,
    ValidatedPluginProvider,
)

if TYPE_CHECKING:
    from korvid.agent.credentials import CredentialSource
    from korvid.agent.provider import LLMProvider
    from korvid.agent.provider_plugin import ProviderPluginConfig

logger = logging.getLogger(__name__)

_SEPARATOR_RE = re.compile(r"[-_.]+")
_MAX_ERROR_LENGTH = 200
_ENTRY_POINT_GROUP: str = "korvid.provider"


class ProviderPluginError(Exception):
    """Raised when provider plugin discovery, loading, or validation fails."""


def normalize_provider_name(name: str) -> str:
    """Normalize a provider name: lowercase, collapse separators to hyphens."""
    return _SEPARATOR_RE.sub("-", name.strip().lower())


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


class ProviderPluginRegistry:
    """Registry that discovers and loads provider plugins by entry point.

    Only the *selected* plugin's entry point is loaded; all others
    remain as metadata-only references.
    """

    def __init__(self) -> None:
        self._cache: dict[str, ProviderPlugin] = {}

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

        # Discover all entry points in the korvid.provider group
        all_eps = _discover_entry_points()

        # Group by normalized name to detect collisions
        candidates: dict[str, list[tuple[importlib.metadata.EntryPoint, str]]] = {}
        for ep, dist_name in all_eps:
            ep_normalized = normalize_provider_name(ep.name)
            candidates.setdefault(ep_normalized, []).append((ep, dist_name))

        if normalized not in candidates:
            raise ProviderPluginError(f"no provider plugin found for {normalized!r}")

        matches = candidates[normalized]
        if len(matches) > 1:
            # Deterministic: sort by distribution name
            dist_names = sorted(dist for _, dist in matches)
            raise ProviderPluginError(
                f"provider name {normalized!r} is registered by multiple "
                f"distributions: {', '.join(dist_names)}"
            )

        ep, dist_name = matches[0]

        # Load only the selected entry point
        try:
            loaded = _load_entry_point(ep)
        except Exception:
            msg = f"failed to load provider plugin {normalized!r} from {dist_name}"
            raise ProviderPluginError(msg) from None

        # Validate: must be a ProviderPlugin subclass (class, not instance)
        if not (isinstance(loaded, type) and issubclass(loaded, ProviderPlugin)):
            raise ProviderPluginError(
                f"entry point {normalized!r} from {dist_name} must be a "
                f"ProviderPlugin subclass, got {type(loaded).__name__}"
            )

        # Instantiate
        try:
            plugin = loaded()
        except Exception:
            raise ProviderPluginError(
                f"failed to instantiate provider plugin {normalized!r} from {dist_name}"
            ) from None

        # Validate metadata
        try:
            meta = plugin.metadata
        except Exception:
            raise ProviderPluginError(
                f"provider plugin {normalized!r} raised while accessing metadata"
            ) from None

        if meta.api_version != PROVIDER_PLUGIN_API_VERSION:
            raise ProviderPluginError(
                f"provider plugin {normalized!r} has api_version {meta.api_version}, "
                f"expected {PROVIDER_PLUGIN_API_VERSION}"
            )

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
        plugin = self._cache.get(normalized)
        if plugin is None:
            raise ProviderPluginError(
                f"provider plugin {normalized!r} not loaded — call load_selected() first"
            )

        meta = plugin.metadata

        # Validate auth method against plugin's declared auth_methods
        if config.auth_method and config.auth_method not in meta.auth_methods:
            raise ProviderPluginError(
                f"provider plugin {normalized!r} does not support auth method "
                f"{config.auth_method!r}; supported: {', '.join(sorted(meta.auth_methods))}"
            )

        # Call the plugin factory
        try:
            provider = plugin.create(config, credentials)
        except ProviderPluginError:
            raise
        except Exception as exc:
            # Bound the error message — never leak secrets from exception payloads
            exc_type = type(exc).__name__
            raise ProviderPluginError(
                f"provider plugin {normalized!r} factory failed: {exc_type}"
            ) from None

        # Wrap in ValidatedPluginProvider for event contract enforcement
        return ValidatedPluginProvider(provider)
