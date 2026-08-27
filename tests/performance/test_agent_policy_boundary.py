"""The performance harness gets its agent budgets from the production router.

Issue #316 task 13 moved every eval and performance path onto the production
agent composition (a resolved `ResolvedAgentPolicy` + `ModelTier` from
`ModelRouter`). The performance harness measures the TUI's read path and
legitimately needs no agent at all — but it is also where someone reaching
for "a budget number" would land, so this module says exactly where such a
number must come from.

The negative half — that no module anywhere names a retired agent symbol or
a capability-profile name — is `tests/test_agent_replacement_guard.py`, which
scans the whole tree rather than this directory alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PERFORMANCE_DIR = Path(__file__).parent


def _python_sources(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


@pytest.mark.parametrize("path", _python_sources(_PERFORMANCE_DIR), ids=lambda p: p.name)
def test_no_performance_module_hardcodes_a_capability_profile_name(path: Path) -> None:
    """`full`/`small` were the retired capability profiles; tiers are `low`/`high`."""
    if path.name == Path(__file__).name:
        pytest.skip("this module names the strings in order to forbid them")
    text = path.read_text(encoding="utf-8")
    for literal in ('"full"', "'full'", '"small"', "'small'"):
        assert literal not in text, f"{path.name} still names a capability profile"


def test_agent_budgets_come_from_the_production_router() -> None:
    """The one supported way to get an agent budget in a harness.

    Named here so a future workload copies this instead of inventing its
    own budget constants.
    """
    from korvid.agent.model_catalog import MODEL_CATALOG
    from korvid.agent.model_policy import (
        ModelCapabilities,
        ModelDescriptor,
        ModelRouter,
        ModelTier,
        PolicyEnvironment,
    )

    policy = ModelRouter(MODEL_CATALOG).resolve(
        descriptor=ModelDescriptor("ollama", "qwen3:8b"),
        provider_capabilities=ModelCapabilities.unknown(),
        explicit_tier=None,
        environment=PolicyEnvironment(
            readonly=True, resize_supported=False, observability_backends=frozenset()
        ),
    )
    assert policy.tier is ModelTier.LOW
    assert policy.max_iterations > 0
    assert policy.max_history_chars > 0
