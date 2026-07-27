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
    assert scenario.screen == "pods view, namespace shop"
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


def test_load_scenario_defaults_optional_sections(tmp_path: Path) -> None:
    text = """\
id: crashloop
question: Why does this deployment keep restarting?
screen: deployments view
root_cause: crashloop
grading:
  must_mention:
    - [crashloopbackoff, crash loop]
  expected_evidence:
    - tool: get_events
      args: {namespace: shop}
      contains: BackOff
cluster:
  objects: []
"""
    scenario = load_scenario(_write(tmp_path, text))
    assert scenario.must_not_mention == ()
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


def test_load_scenario_rejects_non_string_evidence_tool(tmp_path: Path) -> None:
    """A list value would coerce to a string that can never match a real
    tool name, silently making the evidence group unsatisfiable."""
    text = _MINIMAL.replace("    - tool: diagnose_pod", "    - tool: [diagnose_pod]")
    with pytest.raises(ValueError, match="'tool' must be a non-blank string"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_non_string_evidence_contains(tmp_path: Path) -> None:
    text = _MINIMAL.replace("      contains: exit=137", "      contains: 137")
    with pytest.raises(ValueError, match="'contains' must be a non-blank string"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_bad_log_key(tmp_path: Path) -> None:
    text = _MINIMAL.replace("shop/checkout-1/app:", "checkout-1-app:")
    with pytest.raises(ValueError, match="namespace/pod/container"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_non_mapping_log_entry(tmp_path: Path) -> None:
    text = _MINIMAL.replace(
        'shop/checkout-1/app:\n      current: ["allocating buffers", "out of memory"]\n'
        '      previous: ["previous crash line"]',
        "shop/checkout-1/app: just a string",
    )
    with pytest.raises(ValueError, match="must be a mapping"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_scalar_log_stream(tmp_path: Path) -> None:
    """A scalar stream would otherwise iterate character-by-character."""
    text = _MINIMAL.replace(
        'current: ["allocating buffers", "out of memory"]',
        "current: out of memory",
    )
    with pytest.raises(ValueError, match="list of strings"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_unknown_log_stream_key(tmp_path: Path) -> None:
    text = _MINIMAL.replace(
        'previous: ["previous crash line"]',
        'extra: ["previous crash line"]',
    )
    with pytest.raises(ValueError, match="extra"):
        load_scenario(_write(tmp_path, text))


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


def test_load_scenario_rejects_unknown_grading_key(tmp_path: Path) -> None:
    text = _MINIMAL.replace("  must_not_mention:", "  must_not_claim:")
    with pytest.raises(ValueError, match="must_not_claim"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_unknown_cluster_key(tmp_path: Path) -> None:
    text = _MINIMAL.replace("  events:", "  eventz:")
    with pytest.raises(ValueError, match="eventz"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    text = _MINIMAL + "notes: scratch\n"
    with pytest.raises(ValueError, match="notes"):
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


def test_load_scenario_rejects_non_string_mention_keywords(tmp_path: Path) -> None:
    """YAML booleans/numbers would coerce to strings like 'False' that can
    never match answer text, silently weakening the benchmark's assertions."""
    text = _MINIMAL.replace('- "137"', "- 137")
    with pytest.raises(ValueError, match="non-blank strings"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_requires_forbidden_groups_for_negative_controls(tmp_path: Path) -> None:
    """A negative control without must_not_mention cannot catch over-diagnosis:
    'healthy and ready, but OOMKilled' would satisfy every assertion."""
    text = _MINIMAL.replace("root_cause: oom_killed", "root_cause: none").replace(
        "  must_not_mention:\n    - image pull\n", ""
    )
    with pytest.raises(ValueError, match="must_not_mention"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_missing_expected_evidence(tmp_path: Path) -> None:
    """Every scenario must declare ground-truth evidence (issue #69) — an
    omitted section would silently grade evidence_fetched as a free pass."""
    text = _MINIMAL.replace(
        "  expected_evidence:\n"
        "    - tool: diagnose_pod\n"
        "      args: {pod: checkout-1, namespace: shop}\n"
        "      contains: exit=137\n",
        "",
    )
    with pytest.raises(ValueError, match="expected_evidence"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_timestamps_after_the_anchor(tmp_path: Path) -> None:
    """Fixture timestamps are authored against SCENARIO_NOW; a later instant
    would rebase into the run's future and distort ages and event order."""
    text = _MINIMAL.replace(
        "      metadata: {name: checkout-1, namespace: shop}",
        '      metadata: {name: checkout-1, namespace: shop, creationTimestamp: "2026-07-28T00:00:00Z"}',
    )
    with pytest.raises(ValueError, match="after the scenario anchor"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_future_unquoted_yaml_datetimes(tmp_path: Path) -> None:
    """Unquoted RFC 3339 values arrive as datetime objects from yaml.safe_load
    and must hit the same anchor validation as strings."""
    text = _MINIMAL.replace(
        "      message: restarting failed container",
        "      message: restarting failed container\n      lastTimestamp: 2026-07-28T00:00:00Z",
    )
    with pytest.raises(ValueError, match="after the scenario anchor"):
        load_scenario(_write(tmp_path, text))
