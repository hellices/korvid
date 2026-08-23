"""Model capability types and routing logic (Task 3).

Resolves an immutable `ResolvedAgentPolicy` from a model descriptor,
provider-reported capabilities, an optional user tier override, and the
current cluster/backend environment.  All routing decisions are
deterministic and side-effect-free so they can be re-run whenever the
model or provider changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ModelTier(StrEnum):
    """Coarse capability tier that drives surface and budget selection."""

    LOW = "low"
    HIGH = "high"


class CapabilitySource(StrEnum):
    """Which source of evidence determined the resolved tier."""

    USER = "user"
    PROVIDER = "provider"
    CATALOG = "catalog"
    FALLBACK = "fallback"


class ModelRoutingError(Exception):
    """Raised when a model explicitly reports it cannot use tools."""


@dataclass(frozen=True)
class ModelDescriptor:
    """Identifies a model by provider and model tag."""

    provider: str
    model: str


@dataclass(frozen=True)
class ModelCapabilities:
    """Capability facts for a model.

    Any field may be ``None`` (unknown).  Distinct from ``False`` (known
    unsupported): an unknown field is treated permissively; ``False`` for
    ``supports_tools`` is a hard stop.
    """

    recommended_tier: ModelTier | None = None
    supports_tools: bool | None = None
    supports_parallel_tools: bool | None = None

    @classmethod
    def unknown(cls) -> ModelCapabilities:
        """Return a capabilities object with all facts unknown."""
        return cls()


@dataclass(frozen=True)
class ModelCatalogEntry:
    """A single exact-match entry in the shipped model catalog."""

    provider: str
    model: str
    capabilities: ModelCapabilities


@dataclass(frozen=True)
class PolicyEnvironment:
    """Cluster and backend environment facts passed to the router."""

    readonly: bool
    resize_supported: bool
    observability_backends: frozenset[str]


def _deep_freeze(obj: Any) -> Any:
    """Recursively wrap mappings in MappingProxyType and freeze lists."""
    if isinstance(obj, MappingProxyType):
        return obj
    if isinstance(obj, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return tuple(_deep_freeze(v) for v in obj)
    return obj


@dataclass(frozen=True)
class ResolvedAgentPolicy:
    """Immutable routing result.

    All mutable containers are deep-frozen at construction time.
    """

    tier: ModelTier
    route_source: CapabilitySource
    supports_parallel_tools: bool
    #: Frozen tool schemas for this surface (surface → tuple[frozen schema, ...]).
    tool_schemas: MappingProxyType[str, Any]
    #: Per-fact provenance: field → CapabilitySource.
    provenance: MappingProxyType[str, CapabilitySource]
    #: Catalog version used when a catalog entry contributed.
    catalog_version: int | None


class ModelRouter:
    """Resolve routing policies from capability evidence.

    Args:
        catalog_entries: exact-match catalog entries.  The list is stored
            as-is; callers typically pass the module-level ``MODEL_CATALOG``.
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

        Routing precedence for ``recommended_tier``:
        1. User explicit override (``explicit_tier``)
        2. Provider-reported ``recommended_tier``
        3. Exact catalog entry ``recommended_tier``
        4. Low fallback

        Args:
            descriptor: provider + model tag.
            provider_capabilities: facts reported by the provider at
                session start; ``None`` fields are unknown.
            explicit_tier: user-supplied tier string (``"high"`` or
                ``"low"``); ``None`` means no override.
            environment: cluster and backend facts.

        Raises:
            ModelRoutingError: if the provider explicitly reports
                ``supports_tools=False``.
        """
        if provider_capabilities.supports_tools is False:
            raise ModelRoutingError(
                f"{descriptor.provider}/{descriptor.model} explicitly reports no tool support"
            )

        catalog_entry = self._catalog.get((descriptor.provider, descriptor.model))

        # Merge capabilities: provider non-None facts override catalog facts.
        merged, provenance = self._merge(provider_capabilities, catalog_entry)

        # Routing precedence
        tier, route_source = self._route_tier(explicit_tier, merged, catalog_entry, provenance)

        # Parallel tools: only True when explicitly reported True.
        supports_parallel = merged.supports_parallel_tools is True

        catalog_version: int | None = None
        if catalog_entry is not None:
            from korvid.agent.model_catalog import MODEL_CATALOG_VERSION

            catalog_version = MODEL_CATALOG_VERSION

        prov_frozen: MappingProxyType[str, CapabilitySource] = MappingProxyType(
            dict(provenance)
        )

        return ResolvedAgentPolicy(
            tier=tier,
            route_source=route_source,
            supports_parallel_tools=supports_parallel,
            tool_schemas=MappingProxyType({}),
            provenance=prov_frozen,
            catalog_version=catalog_version,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _merge(
        self,
        provider: ModelCapabilities,
        catalog_entry: ModelCatalogEntry | None,
    ) -> tuple[ModelCapabilities, dict[str, CapabilitySource]]:
        """Merge provider and catalog capabilities field-by-field.

        Provider non-None facts win; provenance follows each winning fact.
        """
        provenance: dict[str, CapabilitySource] = {}
        base = catalog_entry.capabilities if catalog_entry is not None else ModelCapabilities()

        # recommended_tier
        if provider.recommended_tier is not None:
            rec_tier = provider.recommended_tier
            provenance["recommended_tier"] = CapabilitySource.PROVIDER
        elif base.recommended_tier is not None:
            rec_tier = base.recommended_tier
            provenance["recommended_tier"] = CapabilitySource.CATALOG
        else:
            rec_tier = None

        # supports_tools
        if provider.supports_tools is not None:
            sup_tools = provider.supports_tools
            provenance["supports_tools"] = CapabilitySource.PROVIDER
        elif base.supports_tools is not None:
            sup_tools = base.supports_tools
            provenance["supports_tools"] = CapabilitySource.CATALOG
        else:
            sup_tools = None

        # supports_parallel_tools
        if provider.supports_parallel_tools is not None:
            sup_parallel = provider.supports_parallel_tools
            provenance["supports_parallel_tools"] = CapabilitySource.PROVIDER
        elif base.supports_parallel_tools is not None:
            sup_parallel = base.supports_parallel_tools
            provenance["supports_parallel_tools"] = CapabilitySource.CATALOG
        else:
            sup_parallel = None

        merged = ModelCapabilities(
            recommended_tier=rec_tier,
            supports_tools=sup_tools,
            supports_parallel_tools=sup_parallel,
        )
        return merged, provenance

    @staticmethod
    def _route_tier(
        explicit_tier: str | None,
        merged: ModelCapabilities,
        catalog_entry: ModelCatalogEntry | None,
        provenance: dict[str, CapabilitySource],
    ) -> tuple[ModelTier, CapabilitySource]:
        if explicit_tier is not None:
            tier = ModelTier(explicit_tier)
            return tier, CapabilitySource.USER

        if merged.recommended_tier is not None:
            source = provenance.get("recommended_tier", CapabilitySource.PROVIDER)
            return merged.recommended_tier, source

        return ModelTier.LOW, CapabilitySource.FALLBACK
