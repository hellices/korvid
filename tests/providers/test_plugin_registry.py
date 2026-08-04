"""Tests for the provider plugin registry — selected-only entry-point loader."""

from __future__ import annotations

import importlib.metadata
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from korvid.agent.credentials import CredentialSource
from korvid.agent.provider import LLMProvider
from korvid.agent.provider_plugin import (
    PROVIDER_PLUGIN_API_VERSION,
    ProviderPlugin,
    ProviderPluginConfig,
    ProviderPluginMetadata,
)

# The module under test.
from korvid.providers.plugin_registry import (
    ProviderPluginError,
    ProviderPluginRegistry,
    normalize_provider_name,
)

# ---------------------------------------------------------------------------
# Fixture helper: build a real dist-info directory with entry_points.txt
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "provider_plugin"


def _build_dist_info(
    base_dir: Path,
    dist_name: str,
    version: str,
    entry_point_name: str,
    entry_point_value: str,
) -> Path:
    """Create a minimal dist-info directory that importlib.metadata can discover."""
    dist_info = base_dir / f"{dist_name}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)

    metadata = dist_info / "METADATA"
    metadata.write_text(
        textwrap.dedent(f"""\
            Metadata-Version: 2.1
            Name: {dist_name}
            Version: {version}
        """)
    )

    entry_points = dist_info / "entry_points.txt"
    entry_points.write_text(
        textwrap.dedent(f"""\
            [korvid.provider]
            {entry_point_name} = {entry_point_value}
        """)
    )
    return dist_info


def _discover_from_path(
    path: Path,
) -> list[tuple[importlib.metadata.EntryPoint, str]]:
    """Discover entry points from distributions in a specific path."""
    results: list[tuple[importlib.metadata.EntryPoint, str]] = []
    for dist in importlib.metadata.distributions(path=[str(path)]):
        dist_name = dist.name or "<unknown>"
        for ep in dist.entry_points:
            if ep.group == "korvid.provider":
                results.append((ep, dist_name))
    return results


@pytest.fixture
def plugin_site(tmp_path: Path) -> Any:
    """Set up a fake site directory with discoverable provider plugins.

    Builds real dist-info directories and patches discovery to use them.
    Also puts the fixtures directory on sys.path so .load() can import.
    """
    # Build dist-info for the "company-llm" plugin (valid, selected)
    _build_dist_info(
        tmp_path,
        dist_name="company_provider",
        version="1.0",
        entry_point_name="company-llm",
        entry_point_value="company_provider:CompanyProviderPlugin",
    )

    # Build dist-info for the "unselected" plugin (must never be imported)
    _build_dist_info(
        tmp_path,
        dist_name="unselected_provider",
        version="1.0",
        entry_point_name="unselected-thing",
        entry_point_value="unselected_provider:UnselectedPlugin",
    )

    # Put fixtures dir on sys.path so .load() can import the modules
    original_path = sys.path[:]
    sys.path.insert(0, str(FIXTURES_DIR))

    # Patch _discover_entry_points to use our tmp_path distributions
    with patch(
        "korvid.providers.plugin_registry._discover_entry_points",
        side_effect=lambda: _discover_from_path(tmp_path),
    ):
        yield tmp_path

    # Cleanup: restore sys.path, remove imported fixture modules from cache
    sys.path[:] = original_path
    for mod_name in list(sys.modules):
        if mod_name.startswith("company_provider") or mod_name.startswith("unselected_provider"):
            del sys.modules[mod_name]


@pytest.fixture
def registry() -> ProviderPluginRegistry:
    """Fresh (uncached) registry instance."""
    return ProviderPluginRegistry()


# ---------------------------------------------------------------------------
# normalize_provider_name
# ---------------------------------------------------------------------------


class TestNormalizeProviderName:
    def test_lowercase(self) -> None:
        assert normalize_provider_name("Company-LLM") == "company-llm"

    def test_underscores_to_hyphens(self) -> None:
        assert normalize_provider_name("my_custom_provider") == "my-custom-provider"

    def test_dots_to_hyphens(self) -> None:
        assert normalize_provider_name("org.provider.v2") == "org-provider-v2"

    def test_consecutive_separators_collapse(self) -> None:
        assert normalize_provider_name("a--b__c..d") == "a-b-c-d"

    def test_already_normalized(self) -> None:
        assert normalize_provider_name("simple") == "simple"


# ---------------------------------------------------------------------------
# Selected discovery works
# ---------------------------------------------------------------------------


class TestLoadSelected:
    def test_loads_selected_plugin(
        self, plugin_site: Any, registry: ProviderPluginRegistry
    ) -> None:
        plugin = registry.load_selected("company-llm")
        assert isinstance(plugin, ProviderPlugin)
        assert plugin.metadata.name == "company-llm"

    def test_unselected_never_imported(
        self, plugin_site: Any, registry: ProviderPluginRegistry
    ) -> None:
        """The unselected module raises on import; if the registry imports it, this fails."""
        registry.load_selected("company-llm")
        assert "unselected_provider" not in sys.modules

    def test_not_found_raises(self, plugin_site: Any, registry: ProviderPluginRegistry) -> None:
        with pytest.raises(ProviderPluginError, match="no provider plugin found"):
            registry.load_selected("nonexistent-plugin")

    def test_case_insensitive_lookup(
        self, plugin_site: Any, registry: ProviderPluginRegistry
    ) -> None:
        plugin = registry.load_selected("Company-LLM")
        assert plugin.metadata.name == "company-llm"


# ---------------------------------------------------------------------------
# Collision detection
# ---------------------------------------------------------------------------


class TestCollisionDetection:
    def test_duplicate_names_error_names_both_distributions(
        self, tmp_path: Path, registry: ProviderPluginRegistry
    ) -> None:
        """Two dists providing the same normalized name must cause a deterministic error."""
        _build_dist_info(
            tmp_path,
            dist_name="provider-alpha",
            version="1.0",
            entry_point_name="my-llm",
            entry_point_value="alpha_mod:AlphaPlugin",
        )
        _build_dist_info(
            tmp_path,
            dist_name="provider-beta",
            version="2.0",
            entry_point_name="My_LLM",
            entry_point_value="beta_mod:BetaPlugin",
        )

        with patch(
            "korvid.providers.plugin_registry._discover_entry_points",
            side_effect=lambda: _discover_from_path(tmp_path),
        ):
            with pytest.raises(ProviderPluginError, match="provider-alpha") as exc_info:
                registry.load_selected("my-llm")
            # Both distributions named in the error
            assert "provider-beta" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Validation errors — bounded, non-secret messages
# ---------------------------------------------------------------------------


class TestMetadataValidation:
    def test_wrong_api_version(self, plugin_site: Any, tmp_path: Path) -> None:
        """Plugin with wrong api_version is rejected with a bounded error."""

        class _BadVersionPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=9999,
                    name="company-llm",
                    display_name="Bad",
                    auth_methods=("api_key",),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_BadVersionPlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="api_version"):
                reg.load_selected("company-llm")

    def test_auth_method_mismatch(self, plugin_site: Any, tmp_path: Path) -> None:
        """Config's auth_method not in metadata's auth_methods is rejected."""

        class _NarrowAuthPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name="Narrow",
                    auth_methods=("oauth",),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_NarrowAuthPlugin,
        ):
            reg = ProviderPluginRegistry()
            reg.load_selected("company-llm")
            config = ProviderPluginConfig(
                base_url=None,
                model=None,
                auth_method="api_key",
                api_key_env=None,
                options={},
            )
            with pytest.raises(ProviderPluginError, match=r"auth.*method"):
                reg.create("company-llm", config, None)


class TestImportFailure:
    def test_import_error_is_bounded(self, tmp_path: Path) -> None:
        """An import failure yields a bounded, non-secret error."""
        _build_dist_info(
            tmp_path,
            dist_name="broken_provider",
            version="1.0",
            entry_point_name="broken-llm",
            entry_point_value="nonexistent_module:BrokenPlugin",
        )
        with patch(
            "korvid.providers.plugin_registry._discover_entry_points",
            side_effect=lambda: _discover_from_path(tmp_path),
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="failed to load") as exc_info:
                reg.load_selected("broken-llm")
            # Error must NOT contain full traceback/secrets
            assert len(str(exc_info.value)) < 300


class TestWrongObjectType:
    def test_entry_point_not_plugin_class(self, plugin_site: Any, tmp_path: Path) -> None:
        """Entry point resolving to a non-ProviderPlugin class is rejected."""

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=str,  # Not a ProviderPlugin subclass
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="must be a ProviderPlugin"):
                reg.load_selected("company-llm")


class TestFactoryFailure:
    def test_create_raises_bounded_error(
        self, plugin_site: Any, registry: ProviderPluginRegistry
    ) -> None:
        """If plugin.create() raises, error is wrapped and bounded."""

        class _FailingPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name="Failing",
                    auth_methods=("api_key", "none"),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise RuntimeError("SECRET_TOKEN_LEAK_ATTEMPT" * 50)

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_FailingPlugin,
        ):
            reg = ProviderPluginRegistry()
            reg.load_selected("company-llm")
            config = ProviderPluginConfig(
                base_url=None,
                model=None,
                auth_method="none",
                api_key_env=None,
                options={},
            )
            with pytest.raises(ProviderPluginError, match="factory failed") as exc_info:
                reg.create("company-llm", config, None)
            # Must NOT leak the secret
            assert "SECRET_TOKEN_LEAK_ATTEMPT" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Cache — loads selected plugin once
# ---------------------------------------------------------------------------


class TestCache:
    def test_load_selected_caches(self, plugin_site: Any, registry: ProviderPluginRegistry) -> None:
        """Repeated load_selected calls return the same instance."""
        p1 = registry.load_selected("company-llm")
        p2 = registry.load_selected("company-llm")
        assert p1 is p2

    def test_cache_is_name_normalized(
        self, plugin_site: Any, registry: ProviderPluginRegistry
    ) -> None:
        """Different casings resolve to the same cached instance."""
        p1 = registry.load_selected("company-llm")
        p2 = registry.load_selected("Company_LLM")
        assert p1 is p2


# ---------------------------------------------------------------------------
# create() wraps in ValidatedPluginProvider
# ---------------------------------------------------------------------------


class TestCreate:
    def test_returns_validated_provider(
        self, plugin_site: Any, registry: ProviderPluginRegistry
    ) -> None:
        from korvid.agent.provider_plugin import ValidatedPluginProvider

        registry.load_selected("company-llm")
        config = ProviderPluginConfig(
            base_url=None,
            model=None,
            auth_method="api_key",
            api_key_env=None,
            options={},
        )
        provider = registry.create("company-llm", config, None)
        assert isinstance(provider, ValidatedPluginProvider)

    def test_create_without_load_raises(self, registry: ProviderPluginRegistry) -> None:
        config = ProviderPluginConfig(
            base_url=None,
            model=None,
            auth_method=None,
            api_key_env=None,
            options={},
        )
        with pytest.raises(ProviderPluginError, match="not loaded"):
            registry.create("nonexistent", config, None)
