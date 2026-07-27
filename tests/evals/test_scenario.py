"""Tests for the eval scenario schema and YAML loader (issue #69)."""

from __future__ import annotations

from pathlib import Path

import pytest

from korvid.evals.scenario import Scenario, load_scenario, load_scenarios

_MINIMAL = """\
id: oom-killed
question: Why does the checkout pod keep dying?
screen: "pods view, namespace shop"
root_cause: oom_killed
grading:
  must_mention:
    - [oomkilled, oom]
    - "137"
  must_not_claim:
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
    assert scenario.screen == "pods view, namespace shop"
    assert scenario.must_mention == (("oomkilled", "oom"), ("137",))
    assert scenario.must_not_claim == (("image pull",),)
    evidence = scenario.expected_evidence[0]
    assert evidence.tool == "diagnose_pod"
    assert evidence.contains == "exit=137"
    assert evidence.args == {"pod": "checkout-1", "namespace": "shop"}
    assert scenario.objects[0]["kind"] == "Pod"
    assert scenario.events[0]["reason"] == "BackOff"
    assert scenario.logs["shop/checkout-1/app"].current == ("allocating buffers", "out of memory")
    assert scenario.logs["shop/checkout-1/app"].previous == ("previous crash line",)


def test_load_scenario_defaults_optional_sections(tmp_path: Path) -> None:
    text = """\
id: healthy
question: Is anything wrong with this deployment?
screen: deployments view
root_cause: none
grading:
  must_mention:
    - [healthy, "no issue", "nothing is wrong", "working as expected"]
cluster:
  objects: []
"""
    scenario = load_scenario(_write(tmp_path, text))
    assert scenario.must_not_claim == ()
    assert scenario.expected_evidence == ()
    assert scenario.events == ()
    assert scenario.logs == {}


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("id: ''", "id"),
        ("question: ''", "question"),
        ("root_cause: ''", "root_cause"),
    ],
)
def test_load_scenario_rejects_blank_required_fields(
    tmp_path: Path, mutation: str, message: str
) -> None:
    lines = _MINIMAL.splitlines()
    key = mutation.split(":")[0]
    replaced = [mutation if line.startswith(f"{key}:") else line for line in lines]
    with pytest.raises(ValueError, match=message):
        load_scenario(_write(tmp_path, "\n".join(replaced)))


def test_load_scenario_rejects_empty_must_mention(tmp_path: Path) -> None:
    text = _MINIMAL.replace(
        '  must_mention:\n    - [oomkilled, oom]\n    - "137"\n', "  must_mention: []\n"
    )
    with pytest.raises(ValueError, match="must_mention"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_unknown_evidence_shape(tmp_path: Path) -> None:
    text = _MINIMAL.replace("      contains: exit=137", "      substring: exit=137")
    with pytest.raises(ValueError, match="contains"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_bad_log_key(tmp_path: Path) -> None:
    text = _MINIMAL.replace("shop/checkout-1/app:", "checkout-1-app:")
    with pytest.raises(ValueError, match="namespace/pod/container"):
        load_scenario(_write(tmp_path, text))


def test_load_scenarios_loads_a_directory_sorted_by_id(tmp_path: Path) -> None:
    _write(tmp_path, _MINIMAL.replace("id: oom-killed", "id: z-last"), "b.yaml")
    _write(tmp_path, _MINIMAL, "a.yaml")
    scenarios = load_scenarios(tmp_path)
    assert [s.id for s in scenarios] == ["oom-killed", "z-last"]
    assert all(isinstance(s, Scenario) for s in scenarios)


def test_load_scenarios_rejects_duplicate_ids(tmp_path: Path) -> None:
    _write(tmp_path, _MINIMAL, "a.yaml")
    _write(tmp_path, _MINIMAL, "b.yaml")
    with pytest.raises(ValueError, match="duplicate"):
        load_scenarios(tmp_path)
