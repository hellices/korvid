"""Tests for the eval scenario schema and YAML loader (issue #69)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from korvid.evals.scenario import (
    Scenario,
    case_pack_identity,
    load_scenario,
    load_scenarios,
    select_scenarios,
)

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


def test_load_scenario_defaults_optional_sections(tmp_path: Path) -> None:
    text = """\
id: crashloop
question: Why does this deployment keep restarting?
interaction:
  kube_context: eval-cluster
  context_epoch: 1
  focused_pane: {kind: pods, scope: shop}
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


def test_load_scenario_requires_an_explicit_starting_interaction(tmp_path: Path) -> None:
    """The workspace a turn starts from is authored, never inferred.

    Deriving it from the question would make the eval measure a screen no
    fixture ever declared, and a change in phrasing would silently change
    what the model was shown.
    """
    text = _MINIMAL.replace(
        "interaction:\n"
        "  kube_context: eval-cluster\n"
        "  context_epoch: 1\n"
        "  focused_pane: {kind: pods, scope: shop}\n",
        "",
    )
    with pytest.raises(ValueError, match="interaction"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_the_retired_screen_prose_field(tmp_path: Path) -> None:
    text = _MINIMAL + 'screen: "pods view, namespace shop"\n'
    with pytest.raises(ValueError, match="unknown keys"):
        load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_an_interaction_without_a_focused_pane(tmp_path: Path) -> None:
    text = _MINIMAL.replace("  focused_pane: {kind: pods, scope: shop}\n", "")
    with pytest.raises(ValueError, match="focused_pane"):
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


# --- exact scenario selection + deterministic case-pack identity -----------
#
# The external-optimizer protocol needs to name one scenario, or a fixed
# named set, and run exactly that every time — without copying fixture
# files into a scratch directory just to change which ones load — and to
# publish an identity for the exact set it measured against that a
# consumer can compare across runs.

_SECOND = _MINIMAL.replace("id: oom-killed", "id: crashloop-app-panic").replace(
    "root_cause: oom_killed", "root_cause: crashloop"
)


def _two_scenarios(tmp_path: Path) -> list[Scenario]:
    _write(tmp_path, _MINIMAL, "a.yaml")
    _write(tmp_path, _SECOND, "b.yaml")
    return load_scenarios(tmp_path)


def test_select_scenarios_returns_the_named_subset_sorted_by_id(tmp_path: Path) -> None:
    scenarios = _two_scenarios(tmp_path)
    selected = select_scenarios(scenarios, ["crashloop-app-panic", "oom-killed"])
    assert [s.id for s in selected] == ["crashloop-app-panic", "oom-killed"]


def test_select_scenarios_result_does_not_depend_on_request_order(tmp_path: Path) -> None:
    scenarios = _two_scenarios(tmp_path)
    forward = select_scenarios(scenarios, ["oom-killed", "crashloop-app-panic"])
    backward = select_scenarios(scenarios, ["crashloop-app-panic", "oom-killed"])
    assert [s.id for s in forward] == [s.id for s in backward]


def test_select_scenarios_rejects_an_empty_selection(tmp_path: Path) -> None:
    scenarios = _two_scenarios(tmp_path)
    with pytest.raises(ValueError, match="at least one scenario id"):
        select_scenarios(scenarios, [])


def test_select_scenarios_rejects_a_blank_id(tmp_path: Path) -> None:
    scenarios = _two_scenarios(tmp_path)
    with pytest.raises(ValueError, match="non-empty strings"):
        select_scenarios(scenarios, ["oom-killed", "   "])


def test_select_scenarios_rejects_a_duplicate_id(tmp_path: Path) -> None:
    scenarios = _two_scenarios(tmp_path)
    with pytest.raises(ValueError, match="duplicate scenario id"):
        select_scenarios(scenarios, ["oom-killed", "oom-killed"])


def test_select_scenarios_rejects_an_unknown_id(tmp_path: Path) -> None:
    scenarios = _two_scenarios(tmp_path)
    with pytest.raises(ValueError, match="unknown scenario id"):
        select_scenarios(scenarios, ["nonexistent-scenario"])


def test_case_pack_identity_is_deterministic_regardless_of_input_order(tmp_path: Path) -> None:
    scenarios = _two_scenarios(tmp_path)
    forward = case_pack_identity(scenarios)
    reversed_input = case_pack_identity(list(reversed(scenarios)))
    assert forward == reversed_input
    assert forward["scenario_ids"] == ["crashloop-app-panic", "oom-killed"]
    assert forward["count"] == 2
    assert len(forward["sha256"]) == 64


def test_case_pack_identity_is_unaffected_by_the_loading_directory_or_file_name(
    tmp_path: Path,
) -> None:
    """The hash is derived from scenario content, not paths or mtimes: the
    same fixture text loaded from a different directory and file name must
    still produce the same identity."""
    scenarios = _two_scenarios(tmp_path)
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    _write(other_dir, _MINIMAL, "x.yaml")
    _write(other_dir, _SECOND, "y.yaml")
    same_scenarios = load_scenarios(other_dir)
    assert case_pack_identity(scenarios) == case_pack_identity(same_scenarios)


def test_case_pack_identity_changes_when_scenario_content_changes(tmp_path: Path) -> None:
    baseline = case_pack_identity(_two_scenarios(tmp_path))
    mutated_dir = tmp_path / "mutated"
    mutated_dir.mkdir()
    changed_question = _MINIMAL.replace(
        "question: Why does the checkout pod keep dying?",
        "question: Why is checkout crash-looping?",
    )
    _write(mutated_dir, changed_question, "a.yaml")
    _write(mutated_dir, _SECOND, "b.yaml")
    mutated = case_pack_identity(load_scenarios(mutated_dir))
    assert mutated["scenario_ids"] == baseline["scenario_ids"]
    assert mutated["sha256"] != baseline["sha256"]


def test_case_pack_identity_reflects_a_selected_subset(tmp_path: Path) -> None:
    scenarios = _two_scenarios(tmp_path)
    selected = select_scenarios(scenarios, ["oom-killed"])
    identity = case_pack_identity(selected)
    full = case_pack_identity(scenarios)
    assert identity["scenario_ids"] == ["oom-killed"]
    assert identity["count"] == 1
    assert identity["sha256"] != full["sha256"]


# --- canonical content hashing: type-preserving, fail-closed --------------
#
# `case_pack_identity`'s digest must derive from the scenarios' own typed
# content, not from a lossy `json.dumps(..., default=str)` fallback: a
# YAML-parsed `datetime` (from an unquoted fixture timestamp) and a string
# that merely renders the same way must never hash identically, and a
# scenario whose content holds something the encoding cannot represent
# (a non-string mapping key, or a value of an unrecognized type) must fail
# closed instead of silently being coerced into a string.

_UNQUOTED_TIMESTAMP = _MINIMAL.replace(
    "      metadata: {name: checkout-1, namespace: shop}\n",
    "      metadata: {name: checkout-1, namespace: shop, createdAt: 2026-07-20T10:00:00Z}\n",
)

# `str(datetime.datetime(2026, 7, 20, 10, 0, tzinfo=UTC))` renders exactly
# this text — the naive `default=str` fallback this test guards against
# would have hashed the two fixtures below identically.
_STRING_THAT_LOOKS_LIKE_THE_SAME_DATETIME = _MINIMAL.replace(
    "      metadata: {name: checkout-1, namespace: shop}\n",
    '      metadata: {name: checkout-1, namespace: shop, createdAt: "2026-07-20 10:00:00+00:00"}\n',
)


def test_case_pack_identity_distinguishes_a_datetime_value_from_an_equal_looking_string(
    tmp_path: Path,
) -> None:
    datetime_dir = tmp_path / "datetime"
    datetime_dir.mkdir()
    _write(datetime_dir, _UNQUOTED_TIMESTAMP, "a.yaml")
    datetime_scenario = load_scenario(datetime_dir / "a.yaml")

    string_dir = tmp_path / "string"
    string_dir.mkdir()
    _write(string_dir, _STRING_THAT_LOOKS_LIKE_THE_SAME_DATETIME, "a.yaml")
    string_scenario = load_scenario(string_dir / "a.yaml")

    # Sanity check: the fixture text really does differ only by a `datetime`
    # vs. a `str` that `str()` would render identically, not by the value.
    assert (
        datetime_scenario.objects[0]["metadata"]["createdAt"]
        != string_scenario.objects[0]["metadata"]["createdAt"]
    )
    assert (
        str(datetime_scenario.objects[0]["metadata"]["createdAt"])
        == (string_scenario.objects[0]["metadata"]["createdAt"])
    )

    datetime_identity = case_pack_identity([datetime_scenario])
    string_identity = case_pack_identity([string_scenario])
    assert datetime_identity["sha256"] != string_identity["sha256"]


def test_case_pack_identity_is_deterministic_for_a_fixture_with_a_typed_timestamp(
    tmp_path: Path,
) -> None:
    """The same unquoted-timestamp fixture, loaded twice, hashes identically —
    the canonical encoding does not introduce nondeterminism of its own."""
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    _write(first_dir, _UNQUOTED_TIMESTAMP, "a.yaml")
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    _write(second_dir, _UNQUOTED_TIMESTAMP, "a.yaml")

    first = case_pack_identity(load_scenarios(first_dir))
    second = case_pack_identity(load_scenarios(second_dir))
    assert first == second


def test_case_pack_identity_rejects_a_non_string_mapping_key(tmp_path: Path) -> None:
    scenario = load_scenario(_write(tmp_path, _MINIMAL))
    # A fixture author's stray unquoted numeric key parses through YAML as
    # an `int` key — `load_scenario` does not itself validate manifest
    # mapping keys, so this is exactly the shape the content hash must
    # still catch.
    bad_object: dict[str, Any] = dict(scenario.objects[0])
    bad_object["metadata"] = yaml.safe_load("name: checkout-1\n42: not-a-string-key\n")
    mutated = replace(scenario, objects=(bad_object,))
    with pytest.raises(ValueError, match="mapping keys must be strings"):
        case_pack_identity([mutated])


def test_case_pack_identity_rejects_an_unsupported_value_type(tmp_path: Path) -> None:
    scenario = load_scenario(_write(tmp_path, _MINIMAL))
    unsupported: Any = {1, 2, 3}
    bad_object: dict[str, Any] = dict(scenario.objects[0])
    bad_object["weird"] = unsupported
    mutated = replace(scenario, objects=(bad_object,))
    with pytest.raises(ValueError, match="unsupported value of type"):
        case_pack_identity([mutated])
