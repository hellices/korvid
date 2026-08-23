"""Tests for model capability routing and policy resolution (Task 3)."""

from __future__ import annotations

import ast
import inspect
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
    context_window_tokens: int | None = None,
    supports_tools: bool | None = None,
    supports_parallel_tools: bool | None = None,
    supports_reasoning: bool | None = None,
    recommended_tier: ModelTier | None = None,
) -> ModelCapabilities:
    return ModelCapabilities(
        context_window_tokens=context_window_tokens,
        supports_tools=supports_tools,
        supports_parallel_tools=supports_parallel_tools,
        supports_reasoning=supports_reasoning,
        recommended_tier=recommended_tier,
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
    catalog_capabilities: ModelCapabilities | None = None,
) -> ModelRouter:
    """Build a router with an optional catalog entry for 'test/model'."""
    if catalog_capabilities is not None:
        entry: ModelCatalogEntry | None = ModelCatalogEntry(
            provider="test", model="model", capabilities=catalog_capabilities
        )
    elif catalog_tier is not None:
        entry = ModelCatalogEntry(
            provider="test",
            model="model",
            capabilities=ModelCapabilities(recommended_tier=catalog_tier),
        )
    else:
        entry = None
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


def test_explicit_override_does_not_rewrite_capability_provenance() -> None:
    """User override changes the route but not `capabilities.provenance`."""
    policy = router(catalog_tier=ModelTier.LOW).resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(),
        explicit_tier="high",
        environment=environment(),
    )
    assert policy.tier is ModelTier.HIGH
    assert policy.route_source is CapabilitySource.USER
    # The underlying fact still traces back to the catalog, not the user.
    assert policy.capabilities.recommended_tier is ModelTier.LOW
    assert policy.capabilities.provenance["recommended_tier"] is CapabilitySource.CATALOG


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


def test_catalog_only_no_tool_support_blocks() -> None:
    """A merged `supports_tools=False` blocks even when only the catalog said so."""
    r = router(
        catalog_capabilities=ModelCapabilities(recommended_tier=ModelTier.LOW, supports_tools=False)
    )
    with pytest.raises(ModelRoutingError, match="explicitly reports no tool support"):
        r.resolve(
            descriptor=ModelDescriptor("test", "model"),
            provider_capabilities=capabilities(),
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
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(
            recommended_tier=ModelTier.HIGH, supports_parallel_tools=False
        ),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.allow_parallel_tool_calls is False


def test_high_tier_with_parallel_tools_allows_parallel() -> None:
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(
            recommended_tier=ModelTier.HIGH, supports_parallel_tools=True
        ),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.allow_parallel_tool_calls is True


def test_high_tier_unknown_parallel_tools_resolves_false() -> None:
    """Unknown (`None`) is not permissive here: only an explicit `True` unlocks it."""
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(
            recommended_tier=ModelTier.HIGH, supports_parallel_tools=None
        ),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.allow_parallel_tool_calls is False


def test_low_tier_never_allows_parallel_even_if_reported() -> None:
    """Low tier is always sequential regardless of a `True` provider fact."""
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(
            recommended_tier=ModelTier.LOW, supports_parallel_tools=True
        ),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.allow_parallel_tool_calls is False


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


def test_tools_is_the_actual_tier_selected_schema_tuple() -> None:
    """`policy.tools` is built from the registry, not left empty."""
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(recommended_tier=ModelTier.LOW),
        explicit_tier=None,
        environment=environment(readonly=True, resize_supported=False),
    )
    assert isinstance(policy.tools, tuple)
    assert len(policy.tools) > 0
    names = {tool["function"]["name"] for tool in policy.tools}
    # low_agent + readonly: reads plus the two UI tools, no write tools.
    assert "open_logs" in names
    assert "open_describe" in names
    assert "delete_resource" not in names
    assert "navigate" not in names  # navigate is high_agent-only


def test_tools_gate_writes_and_resize_from_environment() -> None:
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(recommended_tier=ModelTier.HIGH),
        explicit_tier=None,
        environment=environment(readonly=False, resize_supported=True),
    )
    names = {tool["function"]["name"] for tool in policy.tools}
    assert "delete_resource" in names
    assert "resize_pod" in names


def test_tools_are_deeply_frozen() -> None:
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(recommended_tier=ModelTier.HIGH),
        explicit_tier=None,
        environment=environment(),
    )
    assert len(policy.tools) > 0
    for tool in policy.tools:
        assert isinstance(tool, MappingProxyType)
        assert isinstance(tool["function"], MappingProxyType)
        assert isinstance(tool["function"]["parameters"], MappingProxyType)


def test_tool_schema_nested_mutation_raises_type_error() -> None:
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(recommended_tier=ModelTier.HIGH),
        explicit_tier=None,
        environment=environment(),
    )
    schema = policy.tools[0]
    with pytest.raises(TypeError):
        schema["function"]["name"] = "mutated"  # type: ignore[index]


def test_provenance_is_immutable() -> None:
    r = router()
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(recommended_tier=ModelTier.HIGH),
        explicit_tier=None,
        environment=environment(),
    )
    assert isinstance(policy.capabilities.provenance, MappingProxyType)
    with pytest.raises(TypeError):
        policy.capabilities.provenance["recommended_tier"] = CapabilitySource.USER  # type: ignore[index]


# ---------------------------------------------------------------------------
# Exact catalog matching
# ---------------------------------------------------------------------------


def test_catalog_matches_exact_ollama_model() -> None:
    entry = get_catalog_entry("ollama", "qwen3:8b")
    assert entry is not None
    assert entry.capabilities.recommended_tier is ModelTier.LOW
    assert entry.prompt_overlay_ids == ()


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
    assert p1.model.model == "m1"
    assert p2.model.model == "m2"


# ---------------------------------------------------------------------------
# ModelCapabilities.unknown()
# ---------------------------------------------------------------------------


def test_capabilities_unknown_factory() -> None:
    caps = ModelCapabilities.unknown()
    assert caps.context_window_tokens is None
    assert caps.supports_tools is None
    assert caps.supports_parallel_tools is None
    assert caps.supports_reasoning is None
    assert caps.recommended_tier is None
    assert isinstance(caps.provenance, MappingProxyType)
    assert dict(caps.provenance) == {}


# ---------------------------------------------------------------------------
# Merged context/reasoning provenance
# ---------------------------------------------------------------------------


def test_merged_context_window_and_reasoning_prefer_provider() -> None:
    r = router(
        catalog_capabilities=ModelCapabilities(
            recommended_tier=ModelTier.LOW,
            context_window_tokens=4_000,
            supports_reasoning=False,
        )
    )
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(context_window_tokens=32_000, supports_reasoning=True),
        explicit_tier=None,
        environment=environment(),
    )
    caps = policy.capabilities
    assert caps.context_window_tokens == 32_000
    assert caps.provenance["context_window_tokens"] is CapabilitySource.PROVIDER
    assert caps.supports_reasoning is True
    assert caps.provenance["supports_reasoning"] is CapabilitySource.PROVIDER


def test_merged_context_window_and_reasoning_fall_back_to_catalog() -> None:
    r = router(
        catalog_capabilities=ModelCapabilities(
            recommended_tier=ModelTier.LOW,
            context_window_tokens=4_000,
            supports_reasoning=False,
        )
    )
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(),
        explicit_tier=None,
        environment=environment(),
    )
    caps = policy.capabilities
    assert caps.context_window_tokens == 4_000
    assert caps.provenance["context_window_tokens"] is CapabilitySource.CATALOG
    assert caps.supports_reasoning is False
    assert caps.provenance["supports_reasoning"] is CapabilitySource.CATALOG


def test_merged_context_window_unknown_when_neither_source_reports_it() -> None:
    policy = router().resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(),
        explicit_tier=None,
        environment=environment(),
    )
    caps = policy.capabilities
    assert caps.context_window_tokens is None
    assert "context_window_tokens" not in caps.provenance


def test_catalog_used_for_ollama_qwen3_8b() -> None:
    from korvid.agent.model_catalog import MODEL_CATALOG

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
    from korvid.agent.model_catalog import MODEL_CATALOG

    r = ModelRouter(catalog_entries=MODEL_CATALOG)
    policy = r.resolve(
        descriptor=ModelDescriptor("ollama", "qwen3:8b"),
        provider_capabilities=capabilities(),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.catalog_version == MODEL_CATALOG_VERSION


def test_catalog_version_is_none_without_a_catalog_match() -> None:
    policy = router().resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(recommended_tier=ModelTier.LOW),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.catalog_version is None


# ---------------------------------------------------------------------------
# Complete fields and budgets, exactly as specified
# ---------------------------------------------------------------------------


def test_low_tier_budgets_and_prompt_pack_are_exact() -> None:
    policy = router().resolve(
        descriptor=ModelDescriptor("ollama", "qwen3:8b-fresh"),
        provider_capabilities=capabilities(recommended_tier=ModelTier.LOW),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.prompt_pack_id == "low-korvid-operator"
    assert policy.max_iterations == 6
    assert policy.max_history_chars == 24_000
    assert policy.max_result_chars == 3_000
    assert policy.max_tool_calls_per_iteration == 1
    assert policy.allow_parallel_tool_calls is False
    assert policy.strict_history_budget is True


def test_high_tier_budgets_and_prompt_pack_are_exact() -> None:
    policy = router().resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(
            recommended_tier=ModelTier.HIGH, supports_parallel_tools=True
        ),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.prompt_pack_id == "high-korvid-operator"
    assert policy.max_iterations == 15
    assert policy.max_history_chars == 120_000
    assert policy.max_result_chars is None
    assert policy.max_tool_calls_per_iteration is None
    assert policy.allow_parallel_tool_calls is True
    assert policy.strict_history_budget is False


def test_resolved_policy_carries_the_descriptor_and_merged_capabilities() -> None:
    descriptor = ModelDescriptor("test", "model")
    policy = router().resolve(
        descriptor=descriptor,
        provider_capabilities=capabilities(recommended_tier=ModelTier.HIGH),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.model == descriptor
    assert isinstance(policy.capabilities, ModelCapabilities)
    assert policy.prompt_overlay_ids == ()


def test_catalog_entry_prompt_overlay_ids_propagate() -> None:
    entry = ModelCatalogEntry(
        provider="test",
        model="model",
        capabilities=ModelCapabilities(recommended_tier=ModelTier.LOW),
        prompt_overlay_ids=("test-overlay",),
    )
    r = ModelRouter(catalog_entries=[entry])
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.prompt_overlay_ids == ("test-overlay",)


# ---------------------------------------------------------------------------
# No lazy circular import between model_policy and model_catalog
# ---------------------------------------------------------------------------


def test_model_policy_does_not_import_model_catalog_anywhere() -> None:
    """The import graph must be one-directional: catalog -> policy only.

    A lazy, function-body import would still create a cycle at call time
    and would defeat static analysis; this walks the whole module AST
    (not just its top level) so a lazy import inside `resolve()` is
    caught too.
    """
    import korvid.agent.model_policy as model_policy_module

    source = inspect.getsource(model_policy_module)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "model_catalog" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or "model_catalog" not in node.module


def test_model_catalog_module_not_in_model_policy_dependencies() -> None:
    """Confirms `model_catalog` never ends up loaded as a side effect of
    importing `model_policy` in isolation (belt-and-braces on top of the
    AST check above, which only proves the source text is clean)."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import korvid.agent.model_policy; "
            "assert 'korvid.agent.model_catalog' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
