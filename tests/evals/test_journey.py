"""Conversation-journey schema tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from korvid.evals.grader import grade
from korvid.evals.journey import bundled_journeys_dir, load_journey, load_journeys
from korvid.evals.scenario import Scenario


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


def test_bundled_journey_pack_has_three_conversational_behaviors() -> None:
    journeys = load_journeys(bundled_journeys_dir())
    assert {journey.id for journey in journeys} == {
        "healthy-stop",
        "logs-to-events",
        "triage-and-correct",
    }
    assert all(len(journey.turns) >= 2 for journey in journeys)


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
