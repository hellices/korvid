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
from pathlib import Path

import pytest

from korvid.evals.fake_kube import SCENARIO_NOW, FakeKubeClient, builtin_aliases
from korvid.evals.grader import grade
from korvid.evals.scenario import Scenario, bundled_scenarios_dir, load_scenarios
from korvid.tools.executor import ToolExecutor

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


def test_stuck_rollout_accepts_the_compound_workload_evidence_path() -> None:
    scenario = next(item for item in BUNDLED if item.id == "stuck-rollout")
    assert any(
        evidence.tool == "diagnose_workload"
        and evidence.args == {"kind": "deployments", "name": "api", "namespace": "shop"}
        and evidence.contains == "ImagePullBackOff"
        for group in scenario.expected_evidence
        for evidence in group
    )


def test_stuck_rollout_accepts_plain_deployment_stuck_wording() -> None:
    scenario = next(item for item in BUNDLED if item.id == "stuck-rollout")
    first_group = scenario.must_mention[0]
    assert "deployment is stuck" in first_group
    assert "rollout is stalled" in first_group


def test_pvc_scenario_accepts_the_compound_pod_evidence_path() -> None:
    scenario = next(item for item in BUNDLED if item.id == "pvc-pending-no-storageclass")
    assert any(
        evidence.tool == "diagnose_pod"
        and evidence.args == {"pod": "db-0", "namespace": "data"}
        and evidence.contains == 'storageclass.storage.k8s.io "fast-ssd" not found'
        for group in scenario.expected_evidence
        for evidence in group
    )


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


def _timestamps(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if _TS_PATTERN.match(value) else []
    if isinstance(value, dict):
        return [ts for item in value.values() for ts in _timestamps(item)]
    if isinstance(value, list | tuple):
        return [ts for item in value for ts in _timestamps(item)]
    return []


_TS_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


@pytest.mark.parametrize("scenario", BUNDLED, ids=lambda s: s.id)
def test_fixture_timestamps_never_exceed_scenario_now(scenario: Scenario) -> None:
    """Rebasing maps SCENARIO_NOW to the wall clock; a fixture timestamp
    after it would land in the future and render as age '-'."""
    for raw in _timestamps([*scenario.objects, *scenario.events]):
        parsed = datetime.fromisoformat(raw)
        assert parsed <= SCENARIO_NOW, f"{scenario.id}: {raw} is after SCENARIO_NOW"


@pytest.mark.parametrize("scenario", BUNDLED, ids=lambda s: s.id)
def test_fixture_owner_references_use_uids(scenario: Scenario) -> None:
    """Ownership-chain tools match Kubernetes objects by immutable UID.

    The API always populates ownerReference.uid; name-only fixtures make
    Deployment -> ReplicaSet -> Pod traversal impossible in the fake cluster
    even though the same tool works against a real API server.
    """
    for obj in scenario.objects:
        metadata = obj.get("metadata") or {}
        for ref in metadata.get("ownerReferences") or []:
            uid = str(ref.get("uid") or "")
            assert uid, (
                f"{scenario.id}: {obj.get('kind')} {metadata.get('name')} "
                f"owner {ref.get('kind')} {ref.get('name')} has no uid"
            )


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


#: Realistic answers for the two diagnostic scenarios: phrasings a model
#: actually produces, plus the wrong conclusions each scenario exists to
#: catch. Keyword lists are only as good as the phrasings they survive, so
#: they are pinned here rather than eyeballed once.
_GRADING_CASES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "service-endpoints-not-ready": (
        (
            "The checkout Service has an EndpointSlice, but its only endpoint is"
            " not ready — the readiness probe returns 503.",
            "Traffic never reaches checkout because none of its endpoints are"
            " ready; the readiness probe is failing.",
            "The endpoint for checkout-5d8f-1 is not ready: readiness probe failed with 503.",
            "checkout has no ready endpoints. The pod fails its readiness probe"
            " (503) because inventory is unreachable.",
            "The pod is running but not ready, so its endpoint is not serving"
            " traffic. Readiness probe returns 503.",
            "No endpoints are ready; the readiness probe returns 503.",
            "The endpoints are not ready because the readiness probe fails with 503.",
        ),
        (
            "The Service selector does not match the pod labels, so there are no endpoints.",
            "The container was OOMKilled and restarted repeatedly.",
            "Something is wrong with the service.",
        ),
    ),
    "pvc-wait-for-first-consumer": (
        (
            "Nothing is broken. The storage class standard-delayed uses"
            " WaitForFirstConsumer, so the claim stays Pending by design until a"
            " Pod that mounts it is scheduled.",
            "This is expected: with first consumer binding the PVC waits for a"
            " pod before it binds.",
            "The PVC is Pending because its StorageClass uses"
            " WaitForFirstConsumer — that is normal until a pod consumes it.",
            "No action needed. volumeBindingMode is WaitForFirstConsumer, so"
            " binding is deferred until a pod is scheduled; this is working as"
            " intended.",
            "The StorageClass uses WaitForFirstConsumer, so binding waits until"
            " a consumer Pod exists; that Pod does not exist yet. This is"
            " expected.",
            "With WaitForFirstConsumer the consuming Pod is still missing, so"
            " binding is deferred. This is normal.",
            "The PVC is Pending because the StorageClass uses"
            " WaitForFirstConsumer. This is expected and there is no storage"
            " class problem.",
            "Expected: WaitForFirstConsumer. No storage class issue here.",
        ),
        (
            "Provisioning failed because the storageclass was not found.",
            "The provisioner could not create the volume; provisioning failed.",
            "The StorageClass standard-delayed does not exist.",
            "There is no default storage class, so the claim cannot bind.",
            "The pod was OOMKilled.",
        ),
    ),
}


@pytest.mark.parametrize("scenario_id", sorted(_GRADING_CASES))
def test_diagnostic_scenario_keywords_accept_every_correct_phrasing(
    scenario_id: str,
) -> None:
    scenario = next(item for item in BUNDLED if item.id == scenario_id)
    correct, _ = _GRADING_CASES[scenario_id]
    for answer in correct:
        result = grade(scenario, answer, [])
        assert result.diagnosis_success, (
            f"{scenario_id}: a correct answer was graded wrong — "
            f"missing {result.missing_mentions}, forbidden "
            f"{result.forbidden_mentions}\n  {answer}"
        )


@pytest.mark.parametrize("scenario_id", sorted(_GRADING_CASES))
def test_diagnostic_scenario_keywords_reject_the_wrong_conclusions(
    scenario_id: str,
) -> None:
    scenario = next(item for item in BUNDLED if item.id == scenario_id)
    _, wrong = _GRADING_CASES[scenario_id]
    for answer in wrong:
        result = grade(scenario, answer, [])
        assert not result.diagnosis_success, (
            f"{scenario_id}: a wrong answer was graded correct\n  {answer}"
        )


# Backticked hyphenated tokens in the scoreboard that are deliberately not
# pack ids. Keeping this list explicit rather than inferring "looks like a
# pack id" from the shipped fixtures means a deleted fixture is reported
# instead of silently dropping out of the candidate set.
_NON_PACK_CITATIONS = frozenset(
    {
        "checkout-1",
        "eval-results",
        "korvid-agent-eval-124b1aa",
        "payments-1",
        "search-1",
    }
)


def _cited_pack_ids(text: str) -> set[str]:
    """Return every backticked token in `text` that claims to be a pack id."""
    tokens = set(re.findall(r"`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`", text))
    return tokens - _NON_PACK_CITATIONS


def test_cited_pack_ids_reports_a_deleted_fixture() -> None:
    """A citation whose fixture is gone must be reported, not filtered away.

    Deriving the candidate set from the fixtures that still exist makes the
    guard blind to exactly the case it exists for: delete the file and the
    citation stops looking like a pack id.
    """
    cited = _cited_pack_ids("prose about `healthy-stop` and `eval-results`")
    assert cited - {"healthy-stop"} == set(), "known non-pack tokens must be excluded"
    assert cited - set() == {"healthy-stop"}, (
        "a citation must still be reported when no fixture matches it"
    )


def test_scoreboard_only_names_scenarios_and_journeys_that_exist() -> None:
    """Every pack identifier the scoreboard cites must resolve to a file.

    The scoreboard is the human-readable half of the eval record, and it
    cites scenarios and journeys by id. A renamed or deleted fixture would
    otherwise leave the published document pointing at nothing, with no
    signal until a reader tried to find it.
    """
    scoreboard = Path(__file__).parents[2] / "docs" / "evals" / "scoreboard.md"
    text = scoreboard.read_text(encoding="utf-8")
    scenarios = {path.stem for path in bundled_scenarios_dir().glob("*.yaml")}
    journeys = {path.stem for path in (bundled_scenarios_dir().parent / "journeys").glob("*.yaml")}
    known = scenarios | journeys
    cited = _cited_pack_ids(text)
    missing = sorted(cited - known)
    assert not missing, (
        f"scoreboard cites pack ids that do not exist: {missing}\n"
        f"known scenarios: {sorted(scenarios)}\nknown journeys: {sorted(journeys)}"
    )


def test_every_bundled_scenario_records_a_starting_interaction() -> None:
    """Issue #316 task 13: the starting workspace is fixture data, not prose."""
    for scenario in BUNDLED:
        pane = scenario.interaction.focused_pane
        assert scenario.interaction.kube_context, scenario.id
        assert scenario.interaction.context_epoch >= 0, scenario.id
        assert pane.kind, scenario.id
        assert pane.scope, scenario.id


def test_a_scenario_that_names_one_target_selects_it_on_screen() -> None:
    """A scenario whose evidence all points at one object starts focused on it.

    Otherwise the fixture claims the operator asked about a pod they were
    not looking at, and the interaction context stops describing the run.
    """
    for scenario in BUNDLED:
        targets = {
            (
                str(evidence.args.get("pod") or evidence.args.get("name") or ""),
                str(evidence.args.get("namespace") or ""),
            )
            for group in scenario.expected_evidence
            for evidence in group
        }
        named = {target for target in targets if target[0]}
        if len(named) != 1:
            continue
        (name, namespace) = next(iter(named))
        selected = scenario.interaction.focused_pane.selected
        assert selected is not None, scenario.id
        assert selected.name == name, scenario.id
        if namespace:
            assert selected.namespace == namespace, scenario.id


def test_every_selected_resource_carries_a_uid() -> None:
    """A selection without a uid cannot survive a same-named replacement."""
    for scenario in BUNDLED:
        selected = scenario.interaction.focused_pane.selected
        if selected is not None:
            assert selected.uid, scenario.id
