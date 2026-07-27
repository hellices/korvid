"""Fixture-integrity tests for the bundled scenario pack (issue #69).

Every bundled scenario must load, and every ground-truth `Evidence` entry
must actually be reachable: probing the real ToolExecutor with the
evidence's own tool + args against the scenario's fake cluster must yield
a result containing the expected substring. This keeps the YAML fixtures
honest — a scenario can't assert evidence the tools would never return.
"""

from __future__ import annotations

from typing import Any

import pytest

from korvid.agent.tools import ToolExecutor
from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.scenario import Scenario, bundled_scenarios_dir, load_scenarios

BUNDLED = load_scenarios(bundled_scenarios_dir())


def _executor(scenario: Scenario) -> ToolExecutor:
    kube: Any = FakeKubeClient(scenario)
    return ToolExecutor(kube, builtin_aliases())


def test_bundled_pack_loads_and_covers_the_planned_fault_matrix() -> None:
    ids = [scenario.id for scenario in BUNDLED]
    assert len(ids) >= 10
    negative_controls = [s for s in BUNDLED if s.root_cause == "none"]
    assert len(negative_controls) >= 2


@pytest.mark.parametrize("scenario", BUNDLED, ids=lambda s: s.id)
async def test_bundled_evidence_is_reachable_through_the_real_tools(
    scenario: Scenario,
) -> None:
    executor = _executor(scenario)
    assert scenario.must_mention  # loader guarantees it; keep the invariant visible
    for evidence in scenario.expected_evidence:
        result = await executor.execute(evidence.tool, dict(evidence.args))
        assert evidence.contains in result, (
            f"{scenario.id}: {evidence.tool}({evidence.args}) result does not"
            f" contain {evidence.contains!r}:\n{result}"
        )
