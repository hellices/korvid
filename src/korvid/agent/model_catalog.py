"""Shipped model capability catalog.

Version 1: exact entries only.  No substring or provider heuristics.

Imports one-directionally from `model_policy` (never the reverse):
`MODEL_CATALOG_VERSION` is defined in `model_policy` precisely so this
module can depend on it without creating a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from korvid.agent.model_policy import (
    MODEL_CATALOG_VERSION,
    ModelCapabilities,
    ModelCatalogEntry,
    ModelTier,
)

__all__ = [
    "MODEL_CATALOG",
    "MODEL_CATALOG_VERSION",
    "get_catalog_entry",
]

#: Exact-match entries retained from eval artifacts.
#:
#: A tuple, not a list: this is shipped module state that the composition
#: root, the evals and every `ModelRouter` read from the same object. One
#: stray `append`/`clear` anywhere would silently re-tier every session
#: built afterwards while the header still reported the routing as
#: catalogue-derived.
MODEL_CATALOG: Final[tuple[ModelCatalogEntry, ...]] = (
    ModelCatalogEntry(
        provider="ollama",
        model="qwen3:8b",
        capabilities=ModelCapabilities(
            recommended_tier=ModelTier.LOW,
            supports_tools=True,
            supports_parallel_tools=False,
        ),
        prompt_overlay_ids=(),
    ),
)

#: Fast exact-match lookup; keyed by (provider, model).
_CATALOG_INDEX: Final[Mapping[tuple[str, str], ModelCatalogEntry]] = MappingProxyType(
    {(e.provider, e.model): e for e in MODEL_CATALOG}
)


def get_catalog_entry(provider: str, model: str) -> ModelCatalogEntry | None:
    """Return the exact catalog entry for *provider*/*model*, or ``None``."""
    return _CATALOG_INDEX.get((provider, model))
