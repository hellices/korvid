"""Conversation-journey schema tests."""

from __future__ import annotations

from pathlib import Path

from korvid.evals.journey import (
    load_journey,
    load_journeys,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_load_journey_preserves_ordered_turns_and_cluster(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "triage.yaml",
        """
id: triage-and-correct
root_cause: image_pull_auth
interaction:
  kube_context: eval-cluster
  context_epoch: 1
  focused_pane: {kind: pods, scope: shop}
turns:
  - user: What needs attention in namespace shop?
    grading:
      must_mention: [[checkout, payments]]
      must_not_mention: [[healthy]]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods, namespace: shop}
          contains: payments
      max_tool_calls: 3
  - user: Focus on payments, not checkout. What is the exact cause?
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


#: A minimal journey whose fixture withholds one read. Shared so the
#: acceptance and rejection tests cannot drift apart.
_JOURNEY_WITH_FORBIDDEN = """
id: j
root_cause: none
interaction:
  kube_context: eval-cluster
  context_epoch: 1
  focused_pane: {kind: pods, scope: shop}
turns:
  - user: u
    grading:
      must_mention: [[a]]
      expected_evidence:
        - tool: get_events
          args: {kind: pods, name: p, namespace: n}
          contains: x
  - user: u2
    grading:
      must_mention: [[a]]
      expected_evidence:
        - tool: get_events
          args: {kind: pods, name: p, namespace: n}
          contains: x
cluster:
  objects: []
  events: []
  logs: {}
  forbidden:
    - {kind: pods, namespace: n, subresource: log}
"""


def test_a_journey_can_withhold_a_read_the_way_rbac_does(tmp_path: Path) -> None:
    """`cluster.forbidden` reaches the fixture, so a journey can measure
    what the model does when evidence is denied rather than absent."""
    _write(tmp_path / "j.yaml", _JOURNEY_WITH_FORBIDDEN)
    journey = load_journeys(tmp_path)[0]
    assert journey.forbidden == ({"kind": "pods", "namespace": "n", "subresource": "log"},)
