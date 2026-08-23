"""The performance harness never depends on the retired agent profiles.

Issue #316 task 13 moved every eval and performance path onto the
production agent composition (resolved `ResolvedAgentPolicy` + `ModelTier`
+ the production builders). The performance harness measures the TUI's
read path and legitimately needs no agent at all — but "needs none" has to
be *pinned*, because the way this regresses is someone reaching for the v1
`AgentProfile`/`AgentRuntime` constants for a budget number, which would
re-attach the performance numbers to a program that is being deleted.

If a future workload does need agent budgets, these tests say where they
must come from: `ModelRouter` over `MODEL_CATALOG`, not profile constants.
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: The v1 agent surface no migrated module may name again.
_RETIRED_SYMBOLS = (
    "AgentRuntime",
    "AgentProfile",
    "build_profile",
    "PromptOverrides",
    "korvid.agent.runtime",
    "korvid.agent.profiles",
    "korvid.agent.prompts",
    "compose_system_prompt",
)

_PERFORMANCE_DIR = Path(__file__).parent
_EVALS_DIR = Path(__file__).parents[2] / "src" / "korvid" / "evals"


def _python_sources(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


@pytest.mark.parametrize("path", _python_sources(_PERFORMANCE_DIR), ids=lambda p: p.name)
def test_no_performance_module_names_a_retired_agent_symbol(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if path.name == Path(__file__).name:
        pytest.skip("this module names the symbols in order to forbid them")
    found = [symbol for symbol in _RETIRED_SYMBOLS if symbol in text]
    assert found == [], f"{path.name} still names {found}"


@pytest.mark.parametrize("path", _python_sources(_EVALS_DIR), ids=lambda p: p.name)
def test_no_eval_module_names_a_retired_agent_symbol(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    found = [symbol for symbol in _RETIRED_SYMBOLS if symbol in text]
    assert found == [], f"{path.name} still names {found}"


@pytest.mark.parametrize("path", _python_sources(_PERFORMANCE_DIR), ids=lambda p: p.name)
def test_no_performance_module_hardcodes_a_capability_profile_name(path: Path) -> None:
    """`full`/`small` were the v1 capability profiles; tiers are `low`/`high`."""
    if path.name == Path(__file__).name:
        pytest.skip("this module names the strings in order to forbid them")
    text = path.read_text(encoding="utf-8")
    for literal in ('"full"', "'full'", '"small"', "'small'"):
        assert literal not in text, f"{path.name} still names a capability profile"


def test_agent_budgets_come_from_the_production_router() -> None:
    """The one supported way to get an agent budget in a harness.

    Named here so a future workload copies this instead of re-introducing
    profile constants.
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
