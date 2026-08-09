"""Conversation-journey schema tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.grader import grade
from korvid.evals.journey import (
    ConversationJourney,
    JourneyTurn,
    bundled_journeys_dir,
    load_journey,
    load_journeys,
)
from korvid.evals.scenario import Scenario
from korvid.tools.executor import UI_TOOL_NAMES, ToolExecutor


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_load_journey_preserves_ordered_turns_and_cluster(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "triage.yaml",
        """
id: triage-and-correct
root_cause: image_pull_auth
turns:
  - user: What needs attention in namespace shop?
    screen: "resource view: pods, namespace shop"
    grading:
      must_mention: [[checkout, payments]]
      must_not_mention: [[healthy]]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods, namespace: shop}
          contains: payments
      max_tool_calls: 3
  - user: Focus on payments, not checkout. What is the exact cause?
    screen: "resource view: pods, namespace shop"
    grading:
      must_mention: [[payments], [unauthorized, authentication]]
      must_not_mention: [[oomkilled]]
      expected_evidence:
        - tool: diagnose_pod
          args: {pod: payments-1, namespace: shop}
          contains: unauthorized
    forbidden_targets:
      - {pod: checkout-1, namespace: shop}
cluster:
  objects:
    - kind: Pod
      apiVersion: v1
      metadata: {name: payments-1, namespace: shop, uid: pod-payments}
      status: {phase: Pending}
  events: []
  logs: {}
""",
    )

    journey = load_journey(path)

    assert journey.id == "triage-and-correct"
    assert [turn.user for turn in journey.turns] == [
        "What needs attention in namespace shop?",
        "Focus on payments, not checkout. What is the exact cause?",
    ]
    assert journey.turns[0].max_tool_calls == 3
    assert journey.turns[1].forbidden_targets == ({"pod": "checkout-1", "namespace": "shop"},)
    assert journey.objects[0]["metadata"]["name"] == "payments-1"


def test_load_journey_rejects_unknown_turn_keys(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "bad.yaml",
        """
id: bad
root_cause: none
turns:
  - user: hello
    screen: pods
    surprise: true
    grading:
      must_mention: [healthy]
      must_not_mention: [oomkilled]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: healthy
  - user: stop?
    screen: pods
    grading:
      must_mention: [healthy]
      must_not_mention: [oomkilled]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: healthy
cluster: {objects: [], events: [], logs: {}}
""",
    )
    with pytest.raises(ValueError, match="unknown keys"):
        load_journey(path)


def test_load_journey_rejects_zero_tool_call_budget(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "zero-budget.yaml",
        """
id: zero-budget
root_cause: none
turns:
  - user: inspect
    screen: pods
    grading:
      must_mention: [healthy]
      must_not_mention: [broken]
      max_tool_calls: 0
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: pod
  - user: stop
    screen: pods
    grading:
      must_mention: [stop]
      must_not_mention: [broken]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: pod
cluster: {objects: [], events: [], logs: {}}
""",
    )
    with pytest.raises(ValueError, match="positive integer"):
        load_journey(path)


def test_load_journey_rejects_empty_forbidden_target(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "empty-target.yaml",
        """
id: empty-target
root_cause: none
turns:
  - user: inspect
    screen: pods
    grading:
      must_mention: [healthy]
      must_not_mention: [broken]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: pod
    forbidden_targets: [{}]
  - user: stop
    screen: pods
    grading:
      must_mention: [stop]
      must_not_mention: [broken]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: pod
cluster: {objects: [], events: [], logs: {}}
""",
    )
    with pytest.raises(ValueError, match="non-empty"):
        load_journey(path)


def test_load_journeys_rejects_duplicate_ids(tmp_path: Path) -> None:
    text = """
id: same
root_cause: none
turns:
  - user: hello
    screen: pods
    grading:
      must_mention: [healthy]
      must_not_mention: [oomkilled]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: healthy
  - user: stop?
    screen: pods
    grading:
      must_mention: [healthy]
      must_not_mention: [oomkilled]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: healthy
cluster: {objects: [], events: [], logs: {}}
"""
    _write(tmp_path / "a.yaml", text)
    _write(tmp_path / "b.yaml", text)
    with pytest.raises(ValueError, match="duplicate journey id"):
        load_journeys(tmp_path)


def test_bundled_journey_pack_covers_the_planned_conversational_behaviors() -> None:
    journeys = load_journeys(bundled_journeys_dir())
    assert {journey.id for journey in journeys} == {
        "healthy-stop",
        "logs-to-events",
        "rollout-owner-chain",
        "triage-and-correct",
    }
    assert all(len(journey.turns) >= 2 for journey in journeys)


@pytest.mark.parametrize("journey", load_journeys(bundled_journeys_dir()), ids=lambda j: j.id)
async def test_bundled_journey_evidence_is_reachable_through_the_real_tools(
    journey: ConversationJourney,
) -> None:
    """Every declared evidence item must be fetchable from the fixture.

    The scenario pack has had this guard since #69; journeys did not, so a
    fixture that drifted from its assertions would only surface as an
    unexplained model failure during a paid live run.
    """
    scenario = Scenario(
        id=journey.id,
        question="q",
        screen="s",
        root_cause=journey.root_cause,
        must_mention=(),
        must_not_mention=(),
        objects=journey.objects,
        events=journey.events,
        logs=journey.logs,
    )
    executor = ToolExecutor(FakeKubeClient(scenario), builtin_aliases())
    for index, turn in enumerate(journey.turns, start=1):
        assert turn.expected_evidence, f"{journey.id} turn {index} declares no evidence"
        for group in turn.expected_evidence:
            # UI tools need a live bridge this fixture-only executor does
            # not have; their reachability is a runner concern, not a
            # fixture one.
            cluster_reads = [e for e in group if e.tool not in UI_TOOL_NAMES]
            if not cluster_reads:
                continue
            results = [
                await executor.execute(evidence.tool, dict(evidence.args))
                for evidence in cluster_reads
            ]
            assert any(
                not result.startswith("ERROR:") and evidence.contains in result
                for evidence, result in zip(cluster_reads, results, strict=True)
            ), f"{journey.id} turn {index}: no route satisfies {group[0].contains!r}\n" + "\n".join(
                r[:200] for r in results
            )


def test_triage_requires_an_explicit_priority_not_just_both_names() -> None:
    journey = next(
        item for item in load_journeys(bundled_journeys_dir()) if item.id == "triage-and-correct"
    )
    turn = journey.turns[0]
    scenario = Scenario(
        id="priority",
        question=turn.user,
        screen=turn.screen,
        root_cause=journey.root_cause,
        must_mention=turn.must_mention,
        must_not_mention=turn.must_not_mention,
        expected_evidence=turn.expected_evidence,
    )
    result = grade(
        scenario,
        "Checkout and payments both need attention.",
        [],
    )
    assert result.diagnosis_success is False


#: Per-turn phrasings the rollout journey must accept and reject. Keyword
#: lists are only as good as the phrasings they survive, so they are pinned
#: rather than eyeballed once (the same lesson as the scenario pack).
_ROLLOUT_CASES: tuple[tuple[int, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        0,
        (
            "The rollout is stalled: the new ReplicaSet api-7b9d has a pod stuck"
            " in ImagePullBackOff because the image tag v27 does not exist.",
            "api-7b9d cannot pull the image edge/api:v27 \u2014 the tag looks like"
            " a typo, so the rollout never completes.",
            "The new pod fails to pull edge/api:v27 (manifest not found)."
            " api-7b9d is the wrong tag rollout.",
        ),
        (
            # Names the ReplicaSet and the symptom but never the bad tag —
            # incomplete, and it passed before the groups were split.
            "api-7b9d is in ImagePullBackOff.",
            "The new ReplicaSet api-7b9d cannot pull its image.",
            "The pod was OOMKilled and restarted.",
            "The readiness probe is failing on the new pod.",
            "The namespace is out of CPU quota.",
        ),
    ),
    (
        1,
        (
            "It belongs to ReplicaSet api-7b9d. The previous ReplicaSet api-5c2f"
            " still has 2 ready pods, so traffic is still served.",
            "That pod is owned by api-7b9d; api-5c2f is the old ReplicaSet and is"
            " still running with 2 replicas.",
        ),
        (
            "This is a total outage; all pods are down.",
            # Names the owning ReplicaSet only; says nothing about the old one.
            "It belongs to api-7b9d.",
        ),
    ),
)


def _turn_scenario(turn: JourneyTurn) -> Scenario:
    return Scenario(
        id="x",
        question="q",
        screen="s",
        root_cause="r",
        must_mention=turn.must_mention,
        must_not_mention=turn.must_not_mention,
    )


@pytest.mark.parametrize(("index", "correct", "wrong"), _ROLLOUT_CASES)
def test_rollout_journey_keywords_discriminate(
    index: int, correct: tuple[str, ...], wrong: tuple[str, ...]
) -> None:
    journey = next(
        item for item in load_journeys(bundled_journeys_dir()) if item.id == "rollout-owner-chain"
    )
    scenario = _turn_scenario(journey.turns[index])
    for answer in correct:
        assert grade(scenario, answer, []).diagnosis_success, (
            f"turn {index + 1}: a correct answer was graded wrong\n  {answer}"
        )
    for answer in wrong:
        assert not grade(scenario, answer, []).diagnosis_success, (
            f"turn {index + 1}: a wrong answer was graded correct\n  {answer}"
        )
