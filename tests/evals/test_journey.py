"""Conversation-journey schema and bundled-pack fixture-integrity tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.journey import (
    ConversationJourney,
    bundled_journeys_dir,
    load_journey,
    load_journeys,
)
from korvid.evals.scenario import Scenario
from korvid.tools.executor import UI_TOOL_NAMES, ToolExecutor
from tests.evals.fixtures import EVAL_INTERACTION


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


def test_bundled_journey_pack_covers_the_planned_conversational_behaviors() -> None:
    journeys = load_journeys(bundled_journeys_dir())
    assert {journey.id for journey in journeys} == {
        "compare-namespaces",
        "healthy-stop",
        "logs-to-events",
        "namespace-triage",
        "rbac-evidence-gap",
        "rollout-owner-chain",
        "triage-and-correct",
        "tui-follow",
    }
    assert all(len(journey.turns) >= 2 for journey in journeys)
    # #176 sets eight as the floor for a publishable journey score; the
    # pack shipping fewer is the condition that kept that row unpublishable.
    assert len(journeys) >= 8


@pytest.mark.parametrize("journey", load_journeys(bundled_journeys_dir()), ids=lambda j: j.id)
async def test_bundled_journey_evidence_is_reachable_through_the_real_tools(
    journey: ConversationJourney,
) -> None:
    """Every declared evidence item must be fetchable from the fixture.

    A journey whose assertions drifted from its fixture only surfaces as an
    unexplained model failure during a paid live run.
    """
    scenario = Scenario(
        id=journey.id,
        question="q",
        interaction=EVAL_INTERACTION,
        root_cause=journey.root_cause,
        must_mention=(),
        must_not_mention=(),
        objects=journey.objects,
        events=journey.events,
        logs=journey.logs,
        # The withheld reads belong here too, or the guard would certify a
        # route the journey itself denies at runtime and the drift it exists
        # to catch would reappear as an unexplained model failure.
        forbidden=journey.forbidden,
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
            # Every alternative, not merely one per group. The grader
            # documents each listed tool as "one known-good route, verified
            # reachable by the fixture-integrity test", and an any-of check
            # does not verify that: a route that silently stopped matching
            # would keep passing behind a working sibling, and the pack
            # would then advertise a path no model can take.
            for evidence in cluster_reads:
                result = await executor.execute(evidence.tool, dict(evidence.args))
                assert not result.startswith("ERROR:"), (
                    f"{journey.id} turn {index}: {evidence.tool} failed\n{result[:200]}"
                )
                assert evidence.contains in result, (
                    f"{journey.id} turn {index}: {evidence.tool} does not contain "
                    f"{evidence.contains!r}\n{result[:200]}"
                )
