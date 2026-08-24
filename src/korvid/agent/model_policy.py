"""Model capability types and routing logic (Task 3).

Resolves an immutable `ResolvedAgentPolicy` from a model descriptor,
provider-reported capabilities, an optional user tier override, and the
current cluster/backend environment.  All routing decisions are
deterministic and side-effect-free so they can be re-run whenever the
model or provider changes.

`MODEL_CATALOG_VERSION` lives here (not in `model_catalog`) so the import
graph stays one-directional: `model_catalog` imports from this module, this
module never imports from `model_catalog`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from korvid.agent.prompt_packs import LOW_TOOL_DESCRIPTIONS
from korvid.tools.registry import agent_tool_schemas

#: Version of the shipped exact-match model catalog (`model_catalog.py`).
#: Persisted on every `ResolvedAgentPolicy` whose route used a catalog entry.
MODEL_CATALOG_VERSION: int = 1

#: The agent layer's immutable/copy-owned view of the existing
#: OpenAI-compatible tool-schema mapping. It does not introduce a second wire
#: format — every value actually stored in one is a deep-frozen
#: `MappingProxyType`/`tuple` produced by `_deep_freeze`.
ToolSchema = Mapping[str, Any]


class ModelTier(Enum):
    """Coarse capability tier that drives surface and budget selection."""

    LOW = "low"
    HIGH = "high"


class CapabilitySource(Enum):
    """Which source of evidence determined a resolved capability fact."""

    USER = "user"
    PROVIDER = "provider"
    CATALOG = "catalog"
    FALLBACK = "fallback"


class ModelRoutingError(Exception):
    """Raised when the merged capabilities report no tool support."""


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    """Identifies a model by provider and model tag."""

    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """Capability facts for a model, each independently unknown or known.

    Any field may be ``None`` (unknown).  Distinct from ``False`` (known
    unsupported): an unknown field is treated permissively; ``False`` for
    ``supports_tools`` is a hard stop. ``provenance`` records, per fact
    name, which source (`CapabilitySource`) supplied it; a fact with no
    provenance entry is unknown. The mapping is deep-copied and frozen so
    the capabilities object cannot be mutated after construction.
    """

    context_window_tokens: int | None = None
    supports_tools: bool | None = None
    supports_parallel_tools: bool | None = None
    supports_reasoning: bool | None = None
    recommended_tier: ModelTier | None = None
    provenance: Mapping[str, CapabilitySource] = field(default_factory=dict)

    def __post_init__(self) -> None:
        frozen_provenance = _deep_freeze(dict(self.provenance))
        object.__setattr__(self, "provenance", frozen_provenance)

    @classmethod
    def unknown(cls) -> ModelCapabilities:
        """Return capabilities with every fact ``None`` and empty provenance."""
        return cls()


@dataclass(frozen=True, slots=True)
class ModelCatalogEntry:
    """A single exact-match entry in the shipped model catalog."""

    provider: str
    model: str
    capabilities: ModelCapabilities
    #: Exact prompt overlay ids this model qualifies for (layer 5 of the
    #: prompt harness). Empty for entries with no reproduced failing
    #: scenario justifying an overlay yet.
    prompt_overlay_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyEnvironment:
    """Cluster and backend environment facts passed to the router."""

    readonly: bool
    resize_supported: bool
    observability_backends: frozenset[str]


def _deep_freeze(obj: Any) -> Any:
    """Recursively wrap mappings in `MappingProxyType`, sequences in tuples."""
    if isinstance(obj, MappingProxyType):
        return obj
    if isinstance(obj, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(_deep_freeze(v) for v in obj)
    return obj


@dataclass(frozen=True, slots=True)
class ResolvedAgentPolicy:
    """Immutable routing result.

    All mutable containers (`tools`, and `capabilities.provenance`) are
    deep-frozen at construction time. Cluster-dependent tool gates create a
    new whole policy during retarget rather than mutating individual
    fields.
    """

    model: ModelDescriptor
    capabilities: ModelCapabilities
    tier: ModelTier
    route_source: CapabilitySource
    prompt_pack_id: str
    prompt_overlay_ids: tuple[str, ...]
    tools: tuple[ToolSchema, ...]
    max_iterations: int
    max_history_chars: int
    max_result_chars: int | None
    max_tool_calls_per_iteration: int | None
    allow_parallel_tool_calls: bool
    strict_history_budget: bool
    #: Catalog version used when a catalog entry contributed a fact;
    #: `None` when no catalog entry matched.
    catalog_version: int | None


@dataclass(frozen=True, slots=True)
class _TierBudget:
    """Fixed budgets and prompt identity shared by every model of one tier."""

    prompt_pack_id: str
    max_iterations: int
    max_history_chars: int
    max_result_chars: int | None
    max_tool_calls_per_iteration: int | None
    strict_history_budget: bool


#: Bounded operation phases, smallest tool surface, sequential calls,
#: strict budgets (design doc #6, low tier).
_LOW_TIER_BUDGET = _TierBudget(
    prompt_pack_id="low-korvid-operator",
    max_iterations=6,
    max_history_chars=24_000,
    max_result_chars=3_000,
    max_tool_calls_per_iteration=1,
    strict_history_budget=True,
)

#: Broader diagnostic surface, larger budgets, parallel calls gated on
#: provider confirmation (design doc #6, high tier).
_HIGH_TIER_BUDGET = _TierBudget(
    prompt_pack_id="high-korvid-operator",
    max_iterations=15,
    max_history_chars=120_000,
    max_result_chars=None,
    max_tool_calls_per_iteration=None,
    strict_history_budget=False,
)

_CAPABILITY_FACTS = (
    "context_window_tokens",
    "supports_tools",
    "supports_parallel_tools",
    "supports_reasoning",
    "recommended_tier",
)


def _merge_capabilities(
    provider: ModelCapabilities, catalog_entry: ModelCatalogEntry | None
) -> ModelCapabilities:
    """Merge provider and catalog capabilities field-by-field.

    Provider non-``None`` facts win over the catalog; provenance follows
    whichever source supplied the winning value. A fact left ``None`` by
    both sources gets no provenance entry (unknown).
    """
    base = catalog_entry.capabilities if catalog_entry is not None else ModelCapabilities.unknown()
    provenance: dict[str, CapabilitySource] = {}
    merged: dict[str, Any] = {}

    for fact in _CAPABILITY_FACTS:
        provider_value = getattr(provider, fact)
        if provider_value is not None:
            merged[fact] = provider_value
            provenance[fact] = CapabilitySource.PROVIDER
            continue
        catalog_value = getattr(base, fact)
        if catalog_value is not None:
            merged[fact] = catalog_value
            provenance[fact] = CapabilitySource.CATALOG
            continue
        merged[fact] = None

    return ModelCapabilities(provenance=provenance, **merged)


def _route_tier(
    explicit_tier: str | None, merged: ModelCapabilities
) -> tuple[ModelTier, CapabilitySource]:
    """Resolve `(tier, route_source)` from the four-level precedence.

    Precedence: explicit user override; provider-reported tier; exact
    catalog entry tier; conservative low fallback. An explicit override
    changes only the route — it never rewrites `merged`'s own provenance.
    """
    if explicit_tier is not None:
        return ModelTier(explicit_tier), CapabilitySource.USER

    if merged.recommended_tier is not None:
        source = merged.provenance.get("recommended_tier", CapabilitySource.PROVIDER)
        return merged.recommended_tier, source

    return ModelTier.LOW, CapabilitySource.FALLBACK


def apply_low_tool_descriptions(schemas: list[dict[str, Any]]) -> None:
    """Swap in the shipped low-tier wording, in place, by exact tool name.

    Called on the deep copies `agent_tool_schemas` already produced and
    *before* they are deep-frozen, so the registry is never touched and no
    consumer ever receives a mutable schema.

    Only `function.description` is ever replaced. Names, parameters and
    required fields are left exactly as the registry declared them, so a
    rewording can shorten what the model reads but never widen what a tool
    accepts or does. Matching is by exact name: a schema this map does not
    name — an unmapped registry tool, or a tool a plugin contributed —
    keeps the description it declared.

    Args:
        schemas: Mutable, caller-owned OpenAI function schemas.
    """
    for schema in schemas:
        function = schema.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str):
            continue
        replacement = LOW_TOOL_DESCRIPTIONS.get(name)
        if replacement is not None:
            function["description"] = replacement


class ModelRouter:
    """Resolve routing policies from capability evidence.

    Args:
        catalog_entries: exact-match catalog entries.  Callers typically
            pass the module-level ``MODEL_CATALOG``.
    """

    def __init__(self, catalog_entries: list[ModelCatalogEntry]) -> None:
        self._catalog: dict[tuple[str, str], ModelCatalogEntry] = {
            (e.provider, e.model): e for e in catalog_entries
        }

    def resolve(
        self,
        *,
        descriptor: ModelDescriptor,
        provider_capabilities: ModelCapabilities,
        explicit_tier: str | None,
        environment: PolicyEnvironment,
    ) -> ResolvedAgentPolicy:
        """Resolve an immutable agent policy for *descriptor*.

        Args:
            descriptor: provider + model tag.
            provider_capabilities: facts reported by the provider at
                session start; ``None`` fields are unknown.
            explicit_tier: user-supplied tier string (``"high"`` or
                ``"low"``); ``None`` means no override.
            environment: cluster and backend facts used to gate the armed
                tool surface (readonly, resize support, configured
                observability backends).

        Raises:
            ModelRoutingError: if the merged capabilities report
                ``supports_tools=False`` (from either the provider or the
                catalog).
        """
        catalog_entry = self._catalog.get((descriptor.provider, descriptor.model))
        merged = _merge_capabilities(provider_capabilities, catalog_entry)

        if merged.supports_tools is False:
            raise ModelRoutingError(
                f"{descriptor.provider}/{descriptor.model} explicitly reports no tool support"
            )

        tier, route_source = _route_tier(explicit_tier, merged)
        budget = _LOW_TIER_BUDGET if tier is ModelTier.LOW else _HIGH_TIER_BUDGET

        surface = "low_agent" if tier is ModelTier.LOW else "high_agent"
        raw_schemas = agent_tool_schemas(
            surface,
            readonly=environment.readonly,
            resize_supported=environment.resize_supported,
            observability_backends=environment.observability_backends,
        )
        if tier is ModelTier.LOW:
            # Model-facing wording only, and only for the low tier: the
            # schema list rides on every request, and a small serving
            # context pays for each character of it. Applied here, on the
            # mutable copies, so what freezes below is already final.
            apply_low_tool_descriptions(raw_schemas)
        tools: tuple[ToolSchema, ...] = tuple(_deep_freeze(schema) for schema in raw_schemas)

        # Parallel tool calls are gated on the provider's own confirmation,
        # never on a catalog fact. The catalog may still contribute the
        # *merged* `supports_parallel_tools` value (with CATALOG provenance)
        # for callers inspecting `capabilities`, but permission to actually
        # run parallel calls requires the provider to say `True` itself —
        # an unknown or provider-silent catalog `True` must not unlock it.
        allow_parallel = (
            tier is ModelTier.HIGH and provider_capabilities.supports_parallel_tools is True
        )

        prompt_overlay_ids = catalog_entry.prompt_overlay_ids if catalog_entry is not None else ()
        catalog_version = MODEL_CATALOG_VERSION if catalog_entry is not None else None

        return ResolvedAgentPolicy(
            model=descriptor,
            capabilities=merged,
            tier=tier,
            route_source=route_source,
            prompt_pack_id=budget.prompt_pack_id,
            prompt_overlay_ids=prompt_overlay_ids,
            tools=tools,
            max_iterations=budget.max_iterations,
            max_history_chars=budget.max_history_chars,
            max_result_chars=budget.max_result_chars,
            max_tool_calls_per_iteration=budget.max_tool_calls_per_iteration,
            allow_parallel_tool_calls=allow_parallel,
            strict_history_budget=budget.strict_history_budget,
            catalog_version=catalog_version,
        )
