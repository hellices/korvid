"""Fixture-integrity tests for the bundled scenario pack (issue #69).

Every bundled scenario must load, and every ground-truth `Evidence` entry
must actually be reachable: probing the real ToolExecutor with the
evidence's own tool + args against the scenario's fake cluster must yield
a result containing the expected substring. This keeps the YAML fixtures
honest — a scenario can't assert evidence the tools would never return.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from korvid.agent.tools import ToolExecutor
from korvid.evals.fake_kube import SCENARIO_NOW, FakeKubeClient, builtin_aliases
from korvid.evals.scenario import Scenario, bundled_scenarios_dir, load_scenarios

BUNDLED = load_scenarios(bundled_scenarios_dir())


def _executor(scenario: Scenario) -> ToolExecutor:
    return ToolExecutor(FakeKubeClient(scenario), builtin_aliases())


def test_bundled_pack_loads_and_covers_the_planned_fault_matrix() -> None:
    """Issue #69 acceptance criteria: 20-40 fault scenarios plus 3-5
    negative controls (healthy fixtures that catch over-diagnosis)."""
    faults = [s for s in BUNDLED if s.root_cause != "none"]
    negative_controls = [s for s in BUNDLED if s.root_cause == "none"]
    assert 20 <= len(faults) <= 40
    assert 3 <= len(negative_controls) <= 5


@pytest.mark.parametrize("scenario", BUNDLED, ids=lambda s: s.id)
async def test_bundled_evidence_is_reachable_through_the_real_tools(
    scenario: Scenario,
) -> None:
    executor = _executor(scenario)
    # Every bundled scenario (negative controls included) must declare
    # evidence, so "answered without fetching evidence" is detectable.
    assert scenario.expected_evidence
    for group in scenario.expected_evidence:
        for evidence in group:
            result = await executor.execute(evidence.tool, dict(evidence.args))
            assert evidence.contains in result, (
                f"{scenario.id}: {evidence.tool}({evidence.args}) result does not"
                f" contain {evidence.contains!r}:\n{result}"
            )


def _timestamps(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if _TS_PATTERN.match(value) else []
    if isinstance(value, dict):
        return [ts for item in value.values() for ts in _timestamps(item)]
    if isinstance(value, list | tuple):
        return [ts for item in value for ts in _timestamps(item)]
    return []


_TS_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.mark.parametrize("scenario", BUNDLED, ids=lambda s: s.id)
def test_fixture_timestamps_never_exceed_scenario_now(scenario: Scenario) -> None:
    """Rebasing maps SCENARIO_NOW to the wall clock; a fixture timestamp
    after it would land in the future and render as age '-'."""
    for raw in _timestamps([*scenario.objects, *scenario.events]):
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        assert parsed <= SCENARIO_NOW, f"{scenario.id}: {raw} is after SCENARIO_NOW"
