"""Tests for model capability routing and policy resolution (Task 3)."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from korvid.agent.model_catalog import MODEL_CATALOG_VERSION, get_catalog_entry
from korvid.agent.model_policy import (
    CapabilitySource,
    ModelCapabilities,
    ModelCatalogEntry,
    ModelDescriptor,
    ModelRouter,
    ModelRoutingError,
    ModelTier,
    PolicyEnvironment,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def capabilities(
    *,
    recommended_tier: ModelTier | None = None,
    supports_tools: bool | None = None,
    supports_parallel_tools: bool | None = None,
) -> ModelCapabilities:
    return ModelCapabilities(
        recommended_tier=recommended_tier,
        supports_tools=supports_tools,
        supports_parallel_tools=supports_parallel_tools,
    )


def environment(
    *,
    readonly: bool = False,
    resize_supported: bool = False,
    observability_backends: frozenset[str] = frozenset(),
) -> PolicyEnvironment:
    return PolicyEnvironment(
        readonly=readonly,
        resize_supported=resize_supported,
        observability_backends=observability_backends,
    )


def router(
    provider_tier: ModelTier | None = None,
    catalog_tier: ModelTier | None = None,
) -> ModelRouter:
    """Build a router with an optional catalog entry for 'test/model'."""
    entry = (
        ModelCatalogEntry(
            provider="test",
            model="model",
            capabilities=ModelCapabilities(recommended_tier=catalog_tier),
        )
        if catalog_tier is not None
        else None
    )
    return ModelRouter(catalog_entries=[entry] if entry else [])


# ---------------------------------------------------------------------------
# Step 1 — routing precedence table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "provider_tier", "catalog_tier", "expected_tier", "source"),
    [
        ("high", ModelTier.LOW, ModelTier.LOW, ModelTier.HIGH, CapabilitySource.USER),
        (None, ModelTier.HIGH, ModelTier.LOW, ModelTier.HIGH, CapabilitySource.PROVIDER),
        (None, None, ModelTier.HIGH, ModelTier.HIGH, CapabilitySource.CATALOG),
        (None, None, None, ModelTier.LOW, CapabilitySource.FALLBACK),
    ],
)
def test_routing_precedence(
    override: str | None,
    provider_tier: ModelTier | None,
    catalog_tier: ModelTier | None,
    expected_tier: ModelTier,
    source: CapabilitySource,
) -> None:
    policy = router(provider_tier, catalog_tier).resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(recommended_tier=provider_tier),
        explicit_tier=override,
        environment=environment(),
    )

    assert policy.tier is expected_tier
    assert policy.route_source is source


# ---------------------------------------------------------------------------
# supports_tools=False raises
# ---------------------------------------------------------------------------


def test_explicit_no_tool_support_raises() -> None:
    r = router()
    with pytest.raises(ModelRoutingError, match="explicitly reports no tool support"):
        r.resolve(
            descriptor=ModelDescriptor("ollama", "notools"),
            provider_capabilities=capabilities(supports_tools=False),
            explicit_tier=None,
            environment=environment(),
        )


# ---------------------------------------------------------------------------
# unknown vs false
# ---------------------------------------------------------------------------


def test_unknown_supports_tools_does_not_raise() -> None:
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("ollama", "unknown-model"),
        provider_capabilities=capabilities(supports_tools=None),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy is not None


# ---------------------------------------------------------------------------
# parallel permission
# ---------------------------------------------------------------------------


def test_high_tier_without_parallel_tools_denies_parallel() -> None:
    r = router(provider_tier=ModelTier.HIGH)
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(
            recommended_tier=ModelTier.HIGH, supports_parallel_tools=False
        ),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.supports_parallel_tools is False


def test_high_tier_with_parallel_tools_allows_parallel() -> None:
    r = router(provider_tier=ModelTier.HIGH)
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(
            recommended_tier=ModelTier.HIGH, supports_parallel_tools=True
        ),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.supports_parallel_tools is True


def test_unknown_parallel_tools_resolves_false() -> None:
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(supports_parallel_tools=None),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.supports_parallel_tools is False


# ---------------------------------------------------------------------------
# deep immutability
# ---------------------------------------------------------------------------


def test_resolved_policy_is_immutable() -> None:
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(),
        explicit_tier=None,
        environment=environment(),
    )
    with pytest.raises(AttributeError):
        policy.tier = ModelTier.HIGH  # type: ignore[misc]


def test_tool_schemas_are_deeply_frozen() -> None:
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(),
        explicit_tier=None,
        environment=environment(),
    )
    assert isinstance(policy.tool_schemas, MappingProxyType)


# ---------------------------------------------------------------------------
# Exact catalog matching
# ---------------------------------------------------------------------------


def test_catalog_matches_exact_ollama_model() -> None:
    entry = get_catalog_entry("ollama", "qwen3:8b")
    assert entry is not None
    assert entry.capabilities.recommended_tier is ModelTier.LOW


def test_catalog_does_not_match_unknown_model() -> None:
    entry = get_catalog_entry("ollama", "qwen3:72b")
    assert entry is None


def test_catalog_does_not_match_substring_provider() -> None:
    entry = get_catalog_entry("ollama-mirror", "qwen3:8b")
    assert entry is None


def test_catalog_version() -> None:
    assert MODEL_CATALOG_VERSION == 1


# ---------------------------------------------------------------------------
# Model switch re-resolution
# ---------------------------------------------------------------------------


def test_second_resolve_uses_fresh_state() -> None:
    r = router()
    p1 = r.resolve(
        descriptor=ModelDescriptor("test", "m1"),
        provider_capabilities=capabilities(recommended_tier=ModelTier.LOW),
        explicit_tier=None,
        environment=environment(),
    )
    p2 = r.resolve(
        descriptor=ModelDescriptor("test", "m2"),
        provider_capabilities=capabilities(recommended_tier=ModelTier.HIGH),
        explicit_tier=None,
        environment=environment(),
    )
    assert p1.tier is ModelTier.LOW
    assert p2.tier is ModelTier.HIGH


# ---------------------------------------------------------------------------
# ModelCapabilities.unknown()
# ---------------------------------------------------------------------------


def test_capabilities_unknown_factory() -> None:
    caps = ModelCapabilities.unknown()
    assert caps.recommended_tier is None
    assert caps.supports_tools is None
    assert caps.supports_parallel_tools is None


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_provenance_is_immutable() -> None:
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(recommended_tier=ModelTier.HIGH),
        explicit_tier=None,
        environment=environment(),
    )
    assert isinstance(policy.provenance, MappingProxyType)


def test_catalog_used_for_ollama_qwen3_8b() -> None:
    from korvid.agent.model_catalog import MODEL_CATALOG
    from korvid.agent.model_policy import ModelRouter

    r = ModelRouter(catalog_entries=MODEL_CATALOG)
    policy = r.resolve(
        descriptor=ModelDescriptor("ollama", "qwen3:8b"),
        provider_capabilities=capabilities(),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.tier is ModelTier.LOW
    assert policy.route_source is CapabilitySource.CATALOG


# ---------------------------------------------------------------------------
# Catalog version persists on the route
# ---------------------------------------------------------------------------


def test_catalog_version_on_resolved_policy() -> None:
    from korvid.agent.model_catalog import MODEL_CATALOG, MODEL_CATALOG_VERSION
    from korvid.agent.model_policy import ModelRouter

    r = ModelRouter(catalog_entries=MODEL_CATALOG)
    policy = r.resolve(
        descriptor=ModelDescriptor("ollama", "qwen3:8b"),
        provider_capabilities=capabilities(),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.catalog_version == MODEL_CATALOG_VERSION
