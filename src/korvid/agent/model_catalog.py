"""Shipped model capability catalog (Task 3).

Version 1: exact entries only.  No substring or provider heuristics.
"""

from __future__ import annotations

from korvid.agent.model_policy import ModelCapabilities, ModelCatalogEntry, ModelTier

MODEL_CATALOG_VERSION: int = 1

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
    ),
]

#: Fast exact-match lookup; keyed by (provider, model).
_CATALOG_INDEX: dict[tuple[str, str], ModelCatalogEntry] = {
    (e.provider, e.model): e for e in MODEL_CATALOG
}


def get_catalog_entry(provider: str, model: str) -> ModelCatalogEntry | None:
    """Return the exact catalog entry for *provider*/*model*, or ``None``."""
    return _CATALOG_INDEX.get((provider, model))
