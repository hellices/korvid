"""Tests for the eval scenario schema and YAML loader (issue #69)."""

from __future__ import annotations

from pathlib import Path

import pytest

from korvid.evals.scenario import load_scenario, load_scenarios

_MINIMAL = """\
id: oom-killed
question: Why does the checkout pod keep dying?
interaction:
  kube_context: eval-cluster
  context_epoch: 1
  focused_pane: {kind: pods, scope: shop}
root_cause: oom_killed
grading:
  must_mention:
    - [oomkilled, oom]
    - "137"
  must_not_mention:
    - image pull
  expected_evidence:
    - tool: diagnose_pod
      args: {pod: checkout-1, namespace: shop}
      contains: exit=137
cluster:
  objects:
    - kind: Pod
      metadata: {name: checkout-1, namespace: shop}
      spec: {nodeName: node-a, containers: [{name: app}]}
      status: {phase: Running}
  events:
    - type: Warning
      reason: BackOff
      message: restarting failed container
      involvedObject: {kind: Pod, name: checkout-1, namespace: shop}
  logs:
    shop/checkout-1/app:
      current: ["allocating buffers", "out of memory"]
      previous: ["previous crash line"]
"""


def _write(tmp_path: Path, text: str, name: str = "scenario.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_load_scenario_parses_all_fields(tmp_path: Path) -> None:
    scenario = load_scenario(_write(tmp_path, _MINIMAL))
    assert scenario.id == "oom-killed"
    assert scenario.root_cause == "oom_killed"
    assert scenario.question.startswith("Why does")
    assert scenario.interaction.kube_context == "eval-cluster"
    assert scenario.interaction.context_epoch == 1
    assert scenario.interaction.focused_pane.kind == "pods"
    assert scenario.interaction.focused_pane.scope == "shop"
    assert scenario.must_mention == (("oomkilled", "oom"), ("137",))
    assert scenario.must_not_mention == (("image pull",),)
    evidence = scenario.expected_evidence[0][0]
    assert evidence.tool == "diagnose_pod"
    assert evidence.contains == "exit=137"
    assert evidence.args == {"pod": "checkout-1", "namespace": "shop"}
    assert scenario.objects[0]["kind"] == "Pod"
    assert scenario.events[0]["reason"] == "BackOff"
    assert scenario.logs["shop/checkout-1/app"].current == ("allocating buffers", "out of memory")
    assert scenario.logs["shop/checkout-1/app"].previous == ("previous crash line",)


def test_load_scenario_parses_evidence_alternative_groups(tmp_path: Path) -> None:
    text = _MINIMAL.replace(
        """\
  expected_evidence:
    - tool: diagnose_pod
      args: {pod: checkout-1, namespace: shop}
      contains: exit=137
""",
        """\
  expected_evidence:
    - - tool: diagnose_pod
        args: {pod: checkout-1, namespace: shop}
        contains: exit=137
      - tool: get_resource
        args: {kind: pods, name: checkout-1, namespace: shop}
        contains: "exitCode: 137"
""",
    )
    scenario = load_scenario(_write(tmp_path, text))
    assert len(scenario.expected_evidence) == 1
    group = scenario.expected_evidence[0]
    assert [evidence.tool for evidence in group] == ["diagnose_pod", "get_resource"]


def test_load_scenario_rejects_missing_expected_evidence(tmp_path: Path) -> None:
    text = _MINIMAL.replace(
        "  expected_evidence:\n"
        "    - tool: diagnose_pod\n"
        "      args: {pod: checkout-1, namespace: shop}\n"
        "      contains: exit=137\n",
        "",
    )
    with pytest.raises(ValueError, match="expected_evidence"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_requires_forbidden_claims_for_negative_controls(tmp_path: Path) -> None:
    text = _MINIMAL.replace("root_cause: oom_killed", "root_cause: none").replace(
        "  must_not_mention:\n    - image pull\n",
        "",
    )
    with pytest.raises(ValueError, match="must_not_mention"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_unknown_grading_key(tmp_path: Path) -> None:
    text = _MINIMAL.replace("  must_not_mention:", "  must_not_claim:")
    with pytest.raises(ValueError, match="must_not_claim"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_non_string_evidence_tool(tmp_path: Path) -> None:
    text = _MINIMAL.replace("    - tool: diagnose_pod", "    - tool: [diagnose_pod]")
    with pytest.raises(ValueError, match="'tool' must be a non-blank string"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_non_string_mention_keywords(tmp_path: Path) -> None:
    text = _MINIMAL.replace('- "137"', "- 137")
    with pytest.raises(ValueError, match="non-blank strings"):
        load_scenario(_write(tmp_path, text))


def test_load_scenarios_rejects_duplicate_ids(tmp_path: Path) -> None:
    _write(tmp_path, _MINIMAL, "a.yaml")
    _write(tmp_path, _MINIMAL, "b.yaml")
    with pytest.raises(ValueError, match="duplicate"):
        load_scenarios(tmp_path)


def test_load_scenario_rejects_timestamps_after_the_anchor(tmp_path: Path) -> None:
    """Fixture timestamps are authored against SCENARIO_NOW; a later instant
    would rebase into the run's future and distort ages and event order."""
    text = _MINIMAL.replace(
        "      metadata: {name: checkout-1, namespace: shop}",
        '      metadata: {name: checkout-1, namespace: shop, creationTimestamp: "2026-07-28T00:00:00Z"}',
    )
    with pytest.raises(ValueError, match="after the scenario anchor"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_reads_the_selected_resource_identity(tmp_path: Path) -> None:
    text = _MINIMAL.replace(
        "  focused_pane: {kind: pods, scope: shop}\n",
        "  focused_pane:\n"
        "    kind: pods\n"
        "    scope: shop\n"
        "    filter: checkout\n"
        "    selected: {kind: Pod, namespace: shop, name: checkout-1, uid: pod-1}\n",
    )
    selected = load_scenario(_write(tmp_path, text)).interaction.focused_pane.selected
    assert selected is not None
    assert (selected.kind, selected.namespace, selected.name, selected.uid) == (
        "Pod",
        "shop",
        "checkout-1",
        "pod-1",
    )
