"""Tests for model capability routing and policy resolution."""

from __future__ import annotations

import ast
import inspect
from types import MappingProxyType
from typing import Any

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
    ResolvedAgentPolicy,
    apply_low_tool_descriptions,
)
from korvid.agent.prompt_packs import (
    LOW_TOOL_DESCRIPTION_MAX_CHARS,
    LOW_TOOL_DESCRIPTIONS,
)
from korvid.tools.executor import MAX_RESULT_CHARS
from korvid.tools.registry import agent_tool_schemas

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


def test_high_tier_catalog_parallel_true_with_unknown_provider_denies_parallel() -> None:
    """Parallel permission reads the provider fact directly, not the merged one.

    A catalog `supports_parallel_tools=True` still fills the *merged*
    capability (and its provenance stays CATALOG) when the provider is
    silent, but `allow_parallel_tool_calls` must not honor that catalog
    provenance: only a provider-confirmed `True` unlocks parallel calls.
    """
    r = router(
        catalog_capabilities=ModelCapabilities(
            recommended_tier=ModelTier.HIGH, supports_parallel_tools=True
        )
    )
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(supports_parallel_tools=None),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.capabilities.supports_parallel_tools is True
    assert policy.capabilities.provenance["supports_parallel_tools"] is CapabilitySource.CATALOG
    assert policy.allow_parallel_tool_calls is False


def test_high_tier_catalog_parallel_true_with_provider_false_denies_parallel() -> None:
    """A provider-confirmed `False` still wins over a catalog `True`."""
    r = router(
        catalog_capabilities=ModelCapabilities(
            recommended_tier=ModelTier.HIGH, supports_parallel_tools=True
        )
    )
    policy = r.resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(supports_parallel_tools=False),
        explicit_tier=None,
        environment=environment(),
    )
    assert policy.capabilities.supports_parallel_tools is False
    assert policy.capabilities.provenance["supports_parallel_tools"] is CapabilitySource.PROVIDER
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
    function = policy.tools[0]["function"]
    with pytest.raises(TypeError):
        function["name"] = "mutated"


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


def test_the_shipped_catalog_cannot_be_edited_in_place() -> None:
    """The catalog is a shipped fact, and every router reads the same object.

    `MODEL_CATALOG` is module state imported by the composition root, the
    evals and any caller resolving a policy. As a list, one
    `append`/`clear` anywhere — a test that forgot to copy, a plugin
    poking at the module — silently re-tiers every later session, and the
    header would still report the routing as catalogue-derived. A tuple
    makes that a `AttributeError` at the point of the mistake.
    """
    from korvid.agent.model_catalog import MODEL_CATALOG

    assert isinstance(MODEL_CATALOG, tuple)
    assert not hasattr(MODEL_CATALOG, "append")


def test_the_router_still_takes_the_shipped_catalog_as_it_ships() -> None:
    """Freezing the container must not force every caller to convert it."""
    from korvid.agent.model_catalog import MODEL_CATALOG

    policy = ModelRouter(MODEL_CATALOG).resolve(
        descriptor=ModelDescriptor(provider="ollama", model="qwen3:8b"),
        provider_capabilities=ModelCapabilities.unknown(),
        explicit_tier=None,
        environment=PolicyEnvironment(
            readonly=False, resize_supported=False, observability_backends=frozenset()
        ),
    )

    assert policy.tier is ModelTier.LOW
    assert policy.catalog_version == MODEL_CATALOG_VERSION


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
    assert policy.max_result_chars == MAX_RESULT_CHARS
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


# ---------------------------------------------------------------------------
# Tier budget arithmetic (migrated from the retired profile suite)
# ---------------------------------------------------------------------------


def _resolved(tier: ModelTier) -> ResolvedAgentPolicy:
    from korvid.agent.model_catalog import MODEL_CATALOG

    return ModelRouter(MODEL_CATALOG).resolve(
        descriptor=ModelDescriptor("ollama", "qwen3:8b"),
        provider_capabilities=ModelCapabilities.unknown(),
        explicit_tier=tier.value,
        environment=environment(resize_supported=True),
    )


def test_a_low_tier_turn_fits_inside_its_own_history_budget() -> None:
    """Every result of a full low-tier turn must fit the retained history.

    History trimming never drops the sole most recent turn, so if one
    turn's worth of capped results could exceed the budget, the budget
    would silently stop being a bound — on exactly the small local models
    that cannot absorb the overflow.
    """
    policy = _resolved(ModelTier.LOW)

    assert policy.max_result_chars is not None
    assert policy.max_iterations * policy.max_result_chars <= policy.max_history_chars


def test_the_low_tier_answers_in_fewer_iterations_and_less_history_than_high() -> None:
    """The tiers are a real capability split, not two names for one budget."""
    low = _resolved(ModelTier.LOW)
    high = _resolved(ModelTier.HIGH)

    assert low.max_iterations < high.max_iterations
    assert low.max_history_chars < high.max_history_chars
    assert low.max_tool_calls_per_iteration == 1
    assert high.max_result_chars == MAX_RESULT_CHARS
    assert len(low.tools) < len(high.tools)


# ---------------------------------------------------------------------------
# Low-tier tool descriptions (migrated from the retired profile suite)
# ---------------------------------------------------------------------------


def _descriptions(policy: ResolvedAgentPolicy) -> dict[str, str]:
    return {tool["function"]["name"]: tool["function"]["description"] for tool in policy.tools}


def _registry_descriptions(surface: str) -> dict[str, str]:
    return {
        schema["function"]["name"]: schema["function"]["description"]
        for schema in agent_tool_schemas(
            surface,
            readonly=False,
            resize_supported=True,
            observability_backends=frozenset({"metrics", "logs"}),
        )
    }


def _armed(tier: ModelTier, **env: bool) -> ResolvedAgentPolicy:
    return router().resolve(
        descriptor=ModelDescriptor("test", "model"),
        provider_capabilities=capabilities(),
        explicit_tier=tier.value,
        environment=environment(
            readonly=env.get("readonly", False),
            resize_supported=env.get("resize_supported", True),
            observability_backends=frozenset({"metrics", "logs"}),
        ),
    )


def test_a_low_route_swaps_in_the_shipped_low_tool_wording() -> None:
    """Every request retransmits the schemas; the low tier pays per character."""
    resolved = _descriptions(_armed(ModelTier.LOW))

    for name, description in LOW_TOOL_DESCRIPTIONS.items():
        assert resolved[name] == description


def test_a_high_route_keeps_the_registry_tool_wording() -> None:
    """The map is a low-tier artifact; the high tier is not retuned by it."""
    resolved = _descriptions(_armed(ModelTier.HIGH))
    registry = _registry_descriptions("high_agent")

    assert resolved == registry
    for name in LOW_TOOL_DESCRIPTIONS:
        assert resolved[name] == registry[name]


def test_a_low_route_keeps_the_registry_wording_for_every_unmapped_tool() -> None:
    """Exact names only — an unmapped tool keeps the description it declared."""
    resolved = _descriptions(_armed(ModelTier.LOW))
    registry = _registry_descriptions("low_agent")

    unmapped = set(registry) - set(LOW_TOOL_DESCRIPTIONS)
    assert unmapped  # the surface really has tools the map does not name
    for name in unmapped:
        assert resolved[name] == registry[name]


def test_an_unknown_tool_schema_keeps_the_description_it_declared() -> None:
    """A plugin tool the map never heard of must survive the low pass intact."""
    plugin: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "vendor_inspect",
                "description": "Vendor-declared description.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "diagnose_pod_extended",
                "description": "Not diagnose_pod; a prefix must not match.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]

    apply_low_tool_descriptions(plugin)

    assert plugin[0]["function"]["description"] == "Vendor-declared description."
    assert plugin[1]["function"]["description"] == "Not diagnose_pod; a prefix must not match."


@pytest.mark.parametrize("readonly", [False, True])
@pytest.mark.parametrize("resize_supported", [False, True])
def test_every_shipped_low_tool_description_is_nonempty_and_bounded(
    readonly: bool, resize_supported: bool
) -> None:
    """A 4k-token serving context cannot afford an unbounded schema list."""
    resolved = _descriptions(
        _armed(ModelTier.LOW, readonly=readonly, resize_supported=resize_supported)
    )

    assert resolved
    for name, description in resolved.items():
        assert description.strip(), name
        assert len(description) <= LOW_TOOL_DESCRIPTION_MAX_CHARS, (name, len(description))


def test_the_low_route_leaves_the_registry_schemas_untouched() -> None:
    """The rewrite works on the deep copy, never on the shared registry."""
    before = _registry_descriptions("low_agent")

    _armed(ModelTier.LOW)

    assert _registry_descriptions("low_agent") == before
    assert before["diagnose_pod"] != LOW_TOOL_DESCRIPTIONS["diagnose_pod"]


def test_a_reworded_low_schema_is_still_deeply_frozen() -> None:
    """Rewording happens before the freeze, so it cannot leave a live dict."""
    policy = _armed(ModelTier.LOW)

    reworded = next(
        tool for tool in policy.tools if tool["function"]["name"] in LOW_TOOL_DESCRIPTIONS
    )
    assert isinstance(reworded, MappingProxyType)
    assert isinstance(reworded["function"], MappingProxyType)
    with pytest.raises(TypeError, match="does not support item assignment"):
        reworded["function"]["description"] = "mutated"  # type: ignore[index]  # frozen schema


def test_two_low_routes_do_not_share_a_schema_object() -> None:
    """A second resolve builds its own copies, reworded from the registry again."""
    first = _armed(ModelTier.LOW)
    second = _armed(ModelTier.LOW)

    assert _descriptions(first) == _descriptions(second)
    assert all(a is not b for a, b in zip(first.tools, second.tools, strict=True))
