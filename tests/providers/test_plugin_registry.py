"""Tests for the provider plugin registry — selected-only entry-point loader."""

from __future__ import annotations

import importlib.metadata
import sys
import textwrap
from collections.abc import AsyncIterator
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
    def test_wrong_api_version(self, plugin_site: Any) -> None:
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

    def test_huge_int_api_version_bounded_no_stringify(self, plugin_site: Any) -> None:
        """A 5000-digit int api_version must raise ProviderPluginError with
        bounded message, never stringify the value (would blow up or be huge)."""
        huge_int = 10**5000  # Construct without parsing to avoid str-digit limit

        class _HugeVersionPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=huge_int,
                    name="company-llm",
                    display_name="Huge",
                    auth_methods=("api_key",),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_HugeVersionPlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="api_version") as exc_info:
                reg.load_selected("company-llm")
            msg = str(exc_info.value)
            # Must not contain the raw huge int representation
            assert len(msg) <= 200
            # Must not have raised ValueError from int-to-str conversion
            assert "999" not in msg  # no digits from the huge int

    def test_metadata_name_mismatch(self, plugin_site: Any) -> None:
        """Plugin whose metadata.name doesn't match the entry-point name is rejected."""

        class _WrongNamePlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="totally-different",
                    display_name="Wrong",
                    auth_methods=("api_key",),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_WrongNamePlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match=r"metadata\.name") as exc_info:
                reg.load_selected("company-llm")
            assert "totally-different" not in str(exc_info.value)

    def test_auth_methods_outside_allowlist_rejected(self, plugin_site: Any) -> None:
        """auth_methods containing a value outside the API-v1 allowlist is rejected."""

        class _BadAuthPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name="BadAuth",
                    auth_methods=("api_key", "oauth"),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_BadAuthPlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="auth_methods"):
                reg.load_selected("company-llm")

    def test_auth_methods_secret_huge_values_not_in_error(self, plugin_site: Any) -> None:
        """Disallowed auth_methods with secret/huge values must NOT appear in
        the error message and must not cause a pre-truncation blowup."""
        secret_method = "SECRET_BEARER_TOKEN_" + "x" * 5000

        class _SecretAuthPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name="SecretAuth",
                    auth_methods=("api_key", secret_method),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_SecretAuthPlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="disallowed") as exc_info:
                reg.load_selected("company-llm")
            msg = str(exc_info.value)
            assert "SECRET_BEARER_TOKEN" not in msg
            assert len(msg) <= 200

    def test_auth_methods_duplicate_rejected(self, plugin_site: Any) -> None:
        """Duplicate entries in auth_methods are rejected."""

        class _DuplicateAuthPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name="DupAuth",
                    auth_methods=("api_key", "api_key"),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_DuplicateAuthPlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="duplicate"):
                reg.load_selected("company-llm")

    def test_auth_methods_non_string_rejected(self, plugin_site: Any) -> None:
        """Non-string entries in auth_methods are rejected."""

        class _NonStrAuthPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name="NonStr",
                    auth_methods=("api_key", 42),  # type: ignore[arg-type]  # deliberate bad type
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_NonStrAuthPlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="auth_methods"):
                reg.load_selected("company-llm")

    def test_auth_method_mismatch_at_create(self, plugin_site: Any) -> None:
        """Config's auth_method not in metadata's auth_methods is rejected at create()."""

        class _EntraOnlyPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name="EntraOnly",
                    auth_methods=("entra",),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_EntraOnlyPlugin,
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

    def test_malformed_metadata_raises_bounded_with_secret(self, plugin_site: Any) -> None:
        """metadata property raising with a secret payload produces bounded error."""

        class _ExplodingMetaPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                raise ValueError("SUPER_SECRET_KEY_abc123xyz" * 10)

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_ExplodingMetaPlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="metadata") as exc_info:
                reg.load_selected("company-llm")
            msg = str(exc_info.value)
            assert "SUPER_SECRET_KEY" not in msg
            assert len(msg) <= 200


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


class TestLongLabelBounding:
    def test_long_entry_point_name_is_bounded(self, tmp_path: Path) -> None:
        """Entry-point name exceeding _MAX_ERROR_LENGTH is truncated in error."""
        long_name = "a" * 500
        _build_dist_info(
            tmp_path,
            dist_name="long_provider",
            version="1.0",
            entry_point_name=long_name,
            entry_point_value="nonexistent_module:SomePlugin",
        )
        with patch(
            "korvid.providers.plugin_registry._discover_entry_points",
            side_effect=lambda: _discover_from_path(tmp_path),
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError) as exc_info:
                reg.load_selected(long_name)
            assert len(str(exc_info.value)) <= 200

    def test_long_distribution_name_is_bounded(self, tmp_path: Path) -> None:
        """Distribution name exceeding safe length is truncated in error."""
        long_dist = "d" * 120
        _build_dist_info(
            tmp_path,
            dist_name=long_dist,
            version="1.0",
            entry_point_name="my-plugin",
            entry_point_value="nonexistent_module:Missing",
        )
        with patch(
            "korvid.providers.plugin_registry._discover_entry_points",
            side_effect=lambda: _discover_from_path(tmp_path),
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError) as exc_info:
                reg.load_selected("my-plugin")
            assert len(str(exc_info.value)) <= 200


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
            assert len(str(exc_info.value)) <= 200

    def test_plugin_raised_provider_plugin_error_is_translated(
        self, plugin_site: Any, registry: ProviderPluginRegistry
    ) -> None:
        """A ProviderPluginError raised by plugin.create() with secrets must be translated."""

        class _PluginErrorPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name="PPE",
                    auth_methods=("api_key",),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise ProviderPluginError("SECRET_CREDENTIAL_xyz789 leaked in error")

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_PluginErrorPlugin,
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
            with pytest.raises(ProviderPluginError, match="factory failed") as exc_info:
                reg.create("company-llm", config, None)
            assert "SECRET_CREDENTIAL" not in str(exc_info.value)

    def test_non_llmprovider_factory_output_rejected(
        self, plugin_site: Any, registry: ProviderPluginRegistry
    ) -> None:
        """Plugin.create() returning a non-LLMProvider is translated to ProviderPluginError."""

        class _BadReturnPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name="BadReturn",
                    auth_methods=("none",),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                return "not-a-provider"  # type: ignore[return-value]

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_BadReturnPlugin,
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
            with pytest.raises(ProviderPluginError, match="must return an LLMProvider"):
                reg.create("company-llm", config, None)


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


# ---------------------------------------------------------------------------
# Blocker 1: Full metadata boundary validation
# ---------------------------------------------------------------------------


class TestMetadataBoundary:
    """Validate that metadata must be ProviderPluginMetadata and all fields type-checked."""

    def test_dict_metadata_rejected(self, plugin_site: Any) -> None:
        """A plugin returning a dict instead of ProviderPluginMetadata is rejected."""

        class _DictMetaPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return {"api_version": 1, "name": "company-llm"}  # type: ignore[return-value]

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_DictMetaPlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="ProviderPluginMetadata"):
                reg.load_selected("company-llm")

    def test_api_version_bool_rejected(self, plugin_site: Any) -> None:
        """api_version=True (bool is subclass of int) must be rejected."""

        class _BoolVersionPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=True,  # bool subclass of int — valid to mypy, rejected at runtime
                    name="company-llm",
                    display_name="Bool",
                    auth_methods=("api_key",),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_BoolVersionPlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="api_version"):
                reg.load_selected("company-llm")

    def test_name_empty_rejected(self, plugin_site: Any) -> None:
        """metadata.name that is empty string is rejected."""

        class _EmptyNamePlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="",
                    display_name="Empty",
                    auth_methods=("api_key",),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_EmptyNamePlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="name"):
                reg.load_selected("company-llm")

    def test_display_name_non_string_rejected(self, plugin_site: Any) -> None:
        """metadata.display_name that is not a string is rejected."""

        class _BadDisplayPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name=42,  # type: ignore[arg-type]
                    auth_methods=("api_key",),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_BadDisplayPlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="display_name"):
                reg.load_selected("company-llm")

    def test_auth_methods_list_rejected(self, plugin_site: Any) -> None:
        """metadata.auth_methods as list (not tuple) is rejected."""

        class _ListAuthPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name="List",
                    auth_methods=["api_key"],  # type: ignore[arg-type]
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_ListAuthPlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match=r"auth_methods.*tuple"):
                reg.load_selected("company-llm")

    def test_supports_generic_setup_non_bool_rejected(self, plugin_site: Any) -> None:
        """metadata.supports_generic_setup that is not bool is rejected."""

        class _BadSetupPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name="BadSetup",
                    auth_methods=("api_key",),
                    supports_generic_setup=1,  # type: ignore[arg-type]
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_BadSetupPlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="supports_generic_setup"):
                reg.load_selected("company-llm")

    def test_create_never_rereads_metadata(self, plugin_site: Any) -> None:
        """create() must use cached metadata from load_selected — not re-read the property.

        A plugin whose metadata raises on second read must still have create() succeed.
        """
        call_count = 0

        class _InlineProvider(LLMProvider):
            @property
            def name(self) -> str:
                return "inline"

            async def complete(
                self,
                messages: list[dict[str, Any]],
                tools: list[dict[str, Any]],
                *,
                stream: bool = True,
            ) -> AsyncIterator[dict[str, Any]]:
                yield {"type": "done"}

            async def aclose(self) -> None:
                pass

        class _StatefulMetaPlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                nonlocal call_count
                call_count += 1
                if call_count > 1:
                    raise RuntimeError("SECRET_KEY_LEAKED_ON_SECOND_READ")
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="company-llm",
                    display_name="Stateful",
                    auth_methods=("api_key",),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                return _InlineProvider()

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_StatefulMetaPlugin,
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
            # This MUST NOT re-read metadata — if it does, RuntimeError would leak
            provider = reg.create("company-llm", config, None)
            assert provider is not None

    def test_name_too_long_rejected(self, plugin_site: Any) -> None:
        """metadata.name exceeding bounds is rejected."""

        class _LongNamePlugin(ProviderPlugin):
            @property
            def metadata(self) -> ProviderPluginMetadata:
                return ProviderPluginMetadata(
                    api_version=PROVIDER_PLUGIN_API_VERSION,
                    name="x" * 200,
                    display_name="Long",
                    auth_methods=("api_key",),
                )

            def create(
                self, config: ProviderPluginConfig, credentials: CredentialSource | None
            ) -> LLMProvider:
                raise NotImplementedError

        with patch(
            "korvid.providers.plugin_registry._load_entry_point",
            return_value=_LongNamePlugin,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="name") as exc_info:
                reg.load_selected("company-llm")
            assert len(str(exc_info.value)) <= 200


# ---------------------------------------------------------------------------
# Blocker 2: Reserved name rejection
# ---------------------------------------------------------------------------


class TestReservedNames:
    """ProviderPluginRegistry.load_selected must reject reserved built-in names."""

    @pytest.mark.parametrize(
        "name",
        [
            "openai-compat",
            "openai",
            "azure",
            "vllm",
            "github",
            "anthropic",
            "claude",
            "ollama",
            "github-copilot",
            # Variant casings / separators that normalize to reserved names
            "OpenAI_Compat",
            "GitHub_Copilot",
            "OLLAMA",
            "Azure",
        ],
    )
    def test_reserved_name_rejected_before_discovery(
        self, name: str, registry: ProviderPluginRegistry
    ) -> None:
        """Reserved built-in names are rejected without any entry-point discovery."""
        with pytest.raises(ProviderPluginError, match="reserved"):
            registry.load_selected(name)


# ---------------------------------------------------------------------------
# Finding #7: importlib.metadata discovery/enumeration failures
# ---------------------------------------------------------------------------


class TestDiscoveryFailure:
    def test_distributions_raises_translates_to_bounded_plugin_error(self) -> None:
        """If importlib.metadata.distributions() explodes, the error must be
        translated to a bounded ProviderPluginError without raw payload."""

        def boom() -> list[object]:
            raise RuntimeError("SECRET_INTERNAL_PATH=/opt/venv/lib/python" * 10)

        with patch(
            "korvid.providers.plugin_registry.importlib.metadata.distributions",
            side_effect=boom,
        ):
            reg = ProviderPluginRegistry()
            with pytest.raises(ProviderPluginError, match="discovery failed") as exc_info:
                reg.load_selected("my-plugin")
            msg = str(exc_info.value)
            assert "SECRET_INTERNAL_PATH" not in msg
            assert len(msg) <= 200

    def test_single_dist_enumeration_failure_degrades_gracefully(self) -> None:
        """A single broken distribution during enumeration must be skipped,
        not crash the whole discovery."""

        class _BrokenDist:
            @property
            def name(self) -> str:
                raise RuntimeError("SECRET_CORRUPTED_METADATA")

            @property
            def entry_points(self) -> list[object]:
                raise RuntimeError("SECRET_CORRUPTED_METADATA")

        class _GoodDist:
            name = "good-dist"

            @property
            def entry_points(self) -> list[object]:
                return []

        with patch(
            "korvid.providers.plugin_registry.importlib.metadata.distributions",
            return_value=[_BrokenDist(), _GoodDist()],
        ):
            reg = ProviderPluginRegistry()
            # No plugin found for "my-plugin", but discovery didn't crash
            with pytest.raises(ProviderPluginError, match="no provider plugin found"):
                reg.load_selected("my-plugin")
