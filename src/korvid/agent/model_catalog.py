"""Shipped model capability catalog (Task 3).

Version 1: exact entries only.  No substring or provider heuristics.

Imports one-directionally from `model_policy` (never the reverse):
`MODEL_CATALOG_VERSION` is defined in `model_policy` precisely so this
module can depend on it without creating a cycle.
"""

from __future__ import annotations

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
MODEL_CATALOG: list[ModelCatalogEntry] = [
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
]

#: Fast exact-match lookup; keyed by (provider, model).
_CATALOG_INDEX: dict[tuple[str, str], ModelCatalogEntry] = {
    (e.provider, e.model): e for e in MODEL_CATALOG
}


def get_catalog_entry(provider: str, model: str) -> ModelCatalogEntry | None:
    """Return the exact catalog entry for *provider*/*model*, or ``None``."""
    return _CATALOG_INDEX.get((provider, model))
