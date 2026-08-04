"""Conversation-journey schema tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from korvid.evals.journey import bundled_journeys_dir, load_journey, load_journeys


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
