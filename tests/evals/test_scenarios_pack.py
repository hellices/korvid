"""Fixture-integrity gates for the bundled scenario pack (issue #69).

Every bundled scenario must load, and every ground-truth `Evidence` entry
must actually be reachable: probing the real ToolExecutor with the
evidence's own tool + args against the scenario's fake cluster must yield a
result containing the expected substring. Without this a scenario can name
an evidence path the fixture cannot produce — no model can ever pass it,
and the failure surfaces as a paid campaign scoring zero rather than as a
CI failure. `src/korvid/evals/grader.py` and `scenario.py` document this
file as the guarantee behind their "verified reachable" claims.
"""

from __future__ import annotations

import pytest

from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.grader import grade
from korvid.evals.scenario import Scenario, bundled_scenarios_dir, load_scenarios
from korvid.tools.executor import ToolExecutor

BUNDLED = load_scenarios(bundled_scenarios_dir())

_GRADING_CASES = {
    "service-endpoints-not-ready": (
        (
            "No endpoints are ready; the readiness probe returns 503.",
            "The endpoint is not serving traffic because its readiness probe fails.",
        ),
        "The Service selector does not match, so there are no endpoints.",
    ),
    "pvc-wait-for-first-consumer": (
        (
            "WaitForFirstConsumer defers binding until a Pod is scheduled; this is expected.",
            "The StorageClass uses WaitForFirstConsumer, so binding waits until a consumer Pod "
            "exists; that Pod does not exist yet. This is expected.",
        ),
        "The StorageClass does not exist, so provisioning failed.",
    ),
}


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
            # Fixture drift can produce an error whose message echoes the
            # expected substring; the grader rejects those, so must this.
            assert not result.startswith("ERROR:"), (
                f"{scenario.id}: {evidence.tool}({evidence.args}) failed:\n{result}"
            )
            assert evidence.contains in result, (
                f"{scenario.id}: {evidence.tool}({evidence.args}) result does not"
                f" contain {evidence.contains!r}:\n{result}"
            )


def test_fixture_owner_references_use_uids() -> None:
    """Ownership-chain tools match Kubernetes objects by immutable UID.

    The API always populates ownerReference.uid; name-only fixtures make
    Deployment -> ReplicaSet -> Pod traversal impossible in the fake cluster
    even though the same tool works against a real API server.
    """
    for scenario in BUNDLED:
        for obj in scenario.objects:
            metadata = obj.get("metadata") or {}
            for ref in metadata.get("ownerReferences") or []:
                uid = str(ref.get("uid") or "")
                assert uid, (
                    f"{scenario.id}: {obj.get('kind')} {metadata.get('name')} "
                    f"owner {ref.get('kind')} {ref.get('name')} has no uid"
                )


@pytest.mark.parametrize("scenario_id", sorted(_GRADING_CASES))
def test_diagnostic_scenario_keywords_discriminate_realistic_answers(scenario_id: str) -> None:
    scenario = next(item for item in BUNDLED if item.id == scenario_id)
    correct, wrong = _GRADING_CASES[scenario_id]

    for answer in correct:
        assert grade(scenario, answer, []).diagnosis_success, answer
    assert not grade(scenario, wrong, []).diagnosis_success


@pytest.mark.parametrize(
    "scenario_id", ["service-endpoints-not-ready", "pvc-wait-for-first-consumer"]
)
async def test_diagnostic_scenarios_are_gradeable_without_the_diagnostic_tools(
    scenario_id: str,
) -> None:
    """A baseline arm must be able to satisfy the same evidence.

    #176 compares runs that differ only in whether `diagnose_service` and
    `diagnose_pvc` are offered. If a scenario's evidence were reachable
    *only* through those tools, the comparison would measure tool
    availability rather than diagnosis quality, and the baseline would fail
    by construction.
    """
    scenario = next(item for item in BUNDLED if item.id == scenario_id)
    executor = _executor(scenario)
    diagnostic = {"diagnose_service", "diagnose_pvc"}
    for group in scenario.expected_evidence:
        alternatives = [e for e in group if e.tool not in diagnostic]
        assert alternatives, (
            f"{scenario_id}: an evidence group is reachable only through "
            f"{ {e.tool for e in group} } — a baseline run cannot satisfy it"
        )
        results = [
            await executor.execute(evidence.tool, dict(evidence.args)) for evidence in alternatives
        ]
        assert any(
            not result.startswith("ERROR:") and evidence.contains in result
            for evidence, result in zip(alternatives, results, strict=True)
        ), f"{scenario_id}: no non-diagnostic route satisfies {alternatives[0].contains!r}"
