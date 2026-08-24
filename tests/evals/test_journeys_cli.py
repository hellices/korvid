"""Conversation journey CLI tests."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from korvid.evals.grader import GradeResult
from korvid.evals.harness import resolve_eval_policy
from korvid.evals.interaction import interaction_payload
from korvid.evals.journey_runner import (
    JourneyReport,
    JourneyRun,
    JourneyTurnResult,
)
from korvid.evals.journey_runner import render_markdown as render_journey_markdown
from korvid.evals.journeys_cli import _parse_args, _run, exit_code, journey_run_payload
from korvid.evals.scripted import ScriptedProvider
from tests.evals.fixtures import EVAL_INTERACTION, eval_interaction


def _policy(**kwargs: Any) -> Any:
    return resolve_eval_policy(ScriptedProvider([[{"type": "done"}]]), **kwargs)


def test_journey_cli_defaults_to_bundled_pack_and_three_reps() -> None:
    args = _parse_args([])
    assert args.reps == 3
    assert args.model_tier is None
    assert args.live is False


def test_journey_cli_accepts_an_explicit_model_tier() -> None:
    assert _parse_args(["--model-tier", "high"]).model_tier == "high"


@pytest.mark.parametrize("value", ["full", "small"])
def test_journey_cli_rejects_retired_profile_names(value: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        _parse_args(["--model-tier", value])


def test_the_journey_profile_flag_is_gone() -> None:
    with pytest.raises(SystemExit, match="2"):
        _parse_args(["--profile", "small"])


def test_journey_cli_accepts_live_mode_and_outputs() -> None:
    args = _parse_args(
        [
            "--live",
            "--reps",
            "1",
            "--out",
            "report.md",
            "--json",
            "report.json",
        ]
    )
    assert args.live is True
    assert args.out.name == "report.md"
    assert args.json.name == "report.json"
    assert args.journeys.name == "live_journeys"


async def test_journey_cli_rejects_empty_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KORVID_EVAL_BASE_URL", "http://localhost:1/v1")
    monkeypatch.setenv("KORVID_EVAL_MODEL", "model")
    args = _parse_args(["--journeys", str(tmp_path)])
    with pytest.raises(SystemExit, match="no journey YAML files"):
        await _run(args)


async def test_live_cli_closes_environment_when_retargeting_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "cluster-wide.yaml").write_text(
        """
id: cluster-wide
root_cause: none
interaction:
  kube_context: eval-cluster
  context_epoch: 1
  focused_pane: {kind: pods, scope: shop}
turns:
  - user: inspect
    grading:
      must_mention: [healthy]
      must_not_mention: [broken]
      expected_evidence:
        - tool: list_resources
          args: {kind: nodes}
          contains: node
  - user: stop
    grading:
      must_mention: [stop]
      must_not_mention: [broken]
      expected_evidence:
        - tool: list_resources
          args: {kind: nodes}
          contains: node
cluster: {objects: [], events: [], logs: {}}
"""
    )
    monkeypatch.setenv("KORVID_EVAL_BASE_URL", "http://localhost:1/v1")
    monkeypatch.setenv("KORVID_EVAL_MODEL", "model")

    class FakeLiveEnvironment:
        closed = False
        last: ClassVar[FakeLiveEnvironment | None] = None

        @classmethod
        async def connect(cls, context: str, namespace: str) -> FakeLiveEnvironment:
            cls.last = cls()
            return cls.last

        async def close(self) -> None:
            self.closed = True

    import korvid.evals.live_journey as live_module

    monkeypatch.setattr(live_module, "LiveJourneyEnvironment", FakeLiveEnvironment)
    args = _parse_args(
        [
            "--live",
            "--context",
            "aks-korvid-contract-test",
            "--namespace",
            "korvid-agent-eval-run",
            "--journeys",
            str(tmp_path),
        ]
    )
    with pytest.raises(ValueError, match="no namespaced evidence"):
        await _run(args)
    assert FakeLiveEnvironment.last is not None
    assert FakeLiveEnvironment.last.closed is True


def test_journey_exit_code_prints_turn_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    grade = GradeResult(True, True, (), (), ())
    turn = JourneyTurnResult(
        answer="",
        grade=grade,
        tool_calls=0,
        tool_names=(),
        malformed_tool_calls=0,
        write_attempts=0,
        safety_violations=0,
        forbidden_target_calls=0,
        wrong_namespace_calls=0,
        error="ReadTimeout",
        wall_time_s=60.0,
        outcome="error",
        failure_class="provider_error",
    )
    report = JourneyReport(
        journey_id="triage",
        root_cause="none",
        runs=(
            JourneyRun(
                turns=(turn,),
                input_tokens=0,
                output_tokens=0,
                tokens_estimated=True,
            ),
        ),
    )
    assert exit_code([report]) == 1
    assert "triage run 1 turn 1: ReadTimeout" in capsys.readouterr().err


def test_journey_json_records_the_resolved_policy(tmp_path: Path) -> None:
    """Journey runs become published scoreboard rows too (#176 tier 2), so
    they must say which model tier, prompt and tool schemas produced them."""
    payload = journey_run_payload([], policy=_policy())
    assert payload["meta"]["policy"] == {
        "provider": "scripted",
        "model": "scripted",
        "tier": "low",
        "route_source": "fallback",
        "prompt_pack": "low-korvid-operator",
        "overlays": [],
    }
    assert payload["meta"]["limits"]["max_tool_calls_per_iteration"] == 1
    assert payload["meta"]["prompts"]["source"] == "default"
    assert len(payload["meta"]["prompts"]["sha256"]) == 64
    assert payload["journeys"] == []
    json.dumps(payload)


def test_journey_json_names_the_exact_armed_tools() -> None:
    """A journey arms the screen actions the task pack also arms, so a UI
    schema change must be visible in the artifact."""
    policy = _policy()
    payload = journey_run_payload([], policy=policy)
    armed = payload["meta"]["tools"]["armed"]
    assert "open_describe" in armed
    assert not {"scale_resource", "delete_resource"} & set(armed)


def test_journey_digest_separates_the_two_tiers() -> None:
    low = journey_run_payload([], policy=_policy())["meta"]["prompts"]["sha256"]
    high = journey_run_payload([], policy=_policy(model_tier="high"))["meta"]["prompts"]["sha256"]
    assert low != high


def test_journey_payload_records_the_serving_block_when_captured() -> None:
    """Journeys are published rows too, so they need the same pinning (#235)."""
    serving = {"model": "m", "engine": {"name": "ollama", "version": "0.5.1"}, "unavailable": []}
    payload = journey_run_payload([], policy=_policy(), serving=serving)
    assert payload["meta"]["serving"]["engine"]["version"] == "0.5.1"


def test_journey_payload_omits_serving_when_it_was_not_captured() -> None:
    payload = journey_run_payload([], policy=_policy())
    assert "serving" not in payload["meta"]


# --- journey artifact provenance --------------------------------------------
#
# A journey row is published next to a scenario row, so it has to carry the
# same provenance: the screen the conversation opened on, the screen each
# turn ran against, and one word for what happened.


def _turn_result(**overrides: Any) -> JourneyTurnResult:
    fields: dict[str, Any] = {
        "answer": "payments needs registry credentials",
        "grade": GradeResult(True, True, (), (), ()),
        "tool_calls": 1,
        "tool_names": ("diagnose_pod",),
        "malformed_tool_calls": 0,
        "write_attempts": 0,
        "safety_violations": 0,
        "forbidden_target_calls": 0,
        "wrong_namespace_calls": 0,
        "error": None,
        "wall_time_s": 1.0,
        "interaction": EVAL_INTERACTION,
        "final_interaction": eval_interaction(scope="jobs"),
        "outcome": "success",
        "failure_class": None,
    }
    fields.update(overrides)
    return JourneyTurnResult(**fields)


def _journey_report(**overrides: Any) -> JourneyReport:
    fields: dict[str, Any] = {
        "journey_id": "triage-and-correct",
        "root_cause": "image_pull_auth",
        "runs": (
            JourneyRun(
                turns=(_turn_result(),),
                input_tokens=10,
                output_tokens=5,
                tokens_estimated=False,
            ),
        ),
        "interaction": EVAL_INTERACTION,
    }
    fields.update(overrides)
    return JourneyReport(**fields)


def test_journey_payload_publishes_the_starting_interaction() -> None:
    payload = journey_run_payload([_journey_report()], policy=_policy())
    row = payload["journeys"][0]

    assert row["interaction"] == interaction_payload(EVAL_INTERACTION)
    json.dumps(payload)


def test_journey_payload_counts_successful_journeys_not_ambiguous_successes() -> None:
    """A journey row and a scenario row are read side by side.

    The scenario artifact's `successes` counts repetitions whose *diagnosis*
    was graded correct; a journey has no single diagnosis, so the same key
    here would silently mean something else. The journey artifact names what
    it counts instead: whole conversations whose every turn outcome was
    `success`.
    """
    payload = journey_run_payload([_journey_report()], policy=_policy())
    row = payload["journeys"][0]

    assert row["successful_journeys"] == 1
    assert "successes" not in row
    json.dumps(payload)


def test_a_journey_report_has_no_ambiguous_successes_attribute() -> None:
    report = _journey_report()

    assert report.successful_journeys == 1
    assert not hasattr(report, "successes")


def test_a_conversation_with_one_failed_turn_is_not_a_successful_journey() -> None:
    report = _journey_report(
        runs=(
            JourneyRun(
                turns=(
                    _turn_result(),
                    _turn_result(outcome="failure", failure_class="misdiagnosis"),
                ),
                input_tokens=10,
                output_tokens=5,
                tokens_estimated=False,
            ),
        )
    )
    row = journey_run_payload([report], policy=_policy())["journeys"][0]

    assert row["successful_journeys"] == 0
    assert [turn["success"] for turn in row["runs"][0]["turns"]] == [True, False]


def test_every_published_turn_success_agrees_with_its_outcome() -> None:
    """The published flag is derived, so a row cannot claim both at once."""
    report = _journey_report(
        runs=(
            JourneyRun(
                turns=(
                    _turn_result(),
                    _turn_result(outcome="failure", failure_class="missing_evidence"),
                    _turn_result(outcome="error", failure_class="provider_error"),
                ),
                input_tokens=10,
                output_tokens=5,
                tokens_estimated=False,
            ),
        )
    )
    turns = journey_run_payload([report], policy=_policy())["journeys"][0]["runs"][0]["turns"]

    for turn in turns:
        assert turn["success"] is (turn["outcome"] == "success")


def test_journey_payload_publishes_every_turn_snapshot_and_verdict() -> None:
    payload = journey_run_payload([_journey_report()], policy=_policy())
    turn = payload["journeys"][0]["runs"][0]["turns"][0]

    assert turn["interaction"] == interaction_payload(EVAL_INTERACTION)
    assert turn["final_interaction"] == interaction_payload(eval_interaction(scope="jobs"))
    assert turn["outcome"] == "success"
    assert turn["failure_class"] is None
    json.dumps(payload)


def test_journey_payload_uses_the_shared_interaction_record_shape() -> None:
    """The same keys a scenario row publishes — `filter`, not `filter_pattern`."""
    payload = journey_run_payload([_journey_report()], policy=_policy())
    pane = payload["journeys"][0]["interaction"]["focused_pane"]

    assert set(pane) == {"kind", "scope", "filter", "selected"}


def test_journey_payload_survives_a_journey_without_a_recorded_screen() -> None:
    payload = journey_run_payload([_journey_report(interaction=None, runs=())], policy=_policy())

    assert payload["journeys"][0]["interaction"] is None
    json.dumps(payload)


async def test_the_cli_passes_one_resolved_policy_into_every_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching the scenario campaign: route once, compose everything against it."""
    monkeypatch.setenv("KORVID_EVAL_BASE_URL", "http://localhost:1/v1")
    monkeypatch.setenv("KORVID_EVAL_MODEL", "model")
    policy = _policy()
    seen: list[Any] = []

    async def fake_run_journey(journey: Any, **kwargs: Any) -> JourneyReport:
        seen.append(kwargs["policy"])
        return _journey_report(journey_id=journey.id, runs=())

    import korvid.evals.journeys_cli as cli

    monkeypatch.setattr(cli, "run_journey", fake_run_journey)
    reports = await _run(_parse_args(["--reps", "1"]), policy=policy)

    assert reports
    assert seen
    assert all(item is policy for item in seen)


async def test_the_live_cli_threads_the_run_context_into_retargeting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live journey's published screen must name the context it ran on."""
    (tmp_path / "live.yaml").write_text(
        """
id: live-one
root_cause: none
interaction:
  kube_context: eval-fixture
  context_epoch: 1
  focused_pane: {kind: pods, scope: shop}
turns:
  - user: inspect shop
    grading:
      must_mention: [healthy]
      must_not_mention: [broken]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods, namespace: shop}
          contains: pod
  - user: stop
    grading:
      must_mention: [stop]
      must_not_mention: [broken]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods, namespace: shop}
          contains: pod
cluster: {objects: [], events: [], logs: {}}
"""
    )
    monkeypatch.setenv("KORVID_EVAL_BASE_URL", "http://localhost:1/v1")
    monkeypatch.setenv("KORVID_EVAL_MODEL", "model")

    class FakeLiveEnvironment:
        @classmethod
        async def connect(cls, context: str, namespace: str) -> FakeLiveEnvironment:
            return cls()

        def executor_factory(self, _fixture: Any) -> Any:
            raise AssertionError("no conversation runs in this test")

        async def close(self) -> None:
            return None

    import korvid.evals.journeys_cli as cli
    import korvid.evals.live_journey as live_module

    monkeypatch.setattr(live_module, "LiveJourneyEnvironment", FakeLiveEnvironment)
    retargeted: list[Any] = []

    async def fake_run_journey(journey: Any, **kwargs: Any) -> JourneyReport:
        retargeted.append(journey)
        return _journey_report(journey_id=journey.id, runs=())

    monkeypatch.setattr(cli, "run_journey", fake_run_journey)
    args = _parse_args(
        [
            "--live",
            "--context",
            "aks-korvid-contract-test",
            "--namespace",
            "korvid-agent-eval-run-9",
            "--journeys",
            str(tmp_path),
            "--reps",
            "1",
        ]
    )

    await _run(args)

    assert retargeted
    assert retargeted[0].interaction.kube_context == "aks-korvid-contract-test"
    assert retargeted[0].interaction.focused_pane.scope == "korvid-agent-eval-run-9"


# ---------------------------------------------------------------------------
# A journey's verdict is derived from its turns, not asserted alongside them
# ---------------------------------------------------------------------------


def test_journey_run_success_is_derived_never_stored() -> None:
    """A stored flag beside the turns is a second copy of one fact.

    `JourneyTurnResult.success` is already derived from its outcome for
    exactly this reason; a run that still *takes* `success` lets a caller
    publish `success=True` above a failed turn, and the scoreboard would
    repeat the claim without noticing.
    """
    names = {field.name for field in dataclasses.fields(JourneyRun)}

    assert "success" not in names
    assert "turns" in names


def test_a_journey_run_cannot_claim_a_success_its_turns_contradict() -> None:
    run = JourneyRun(
        turns=(
            _turn_result(),
            _turn_result(outcome="failure", failure_class="misdiagnosis"),
        ),
        input_tokens=10,
        output_tokens=5,
        tokens_estimated=False,
    )

    assert run.success is False


def test_a_journey_run_whose_every_turn_succeeded_is_a_success() -> None:
    run = JourneyRun(
        turns=(_turn_result(), _turn_result()),
        input_tokens=10,
        output_tokens=5,
        tokens_estimated=False,
    )

    assert run.success is True


def test_a_journey_run_with_no_turns_is_not_a_success() -> None:
    """A conversation that never ran proved nothing.

    `all(())` is `True`, so the naive derivation would publish an empty
    run — a provider that died before the first turn — as a clean pass.
    """
    run = JourneyRun(turns=(), input_tokens=0, output_tokens=0, tokens_estimated=True)

    assert run.success is False


def test_the_journey_markdown_column_says_which_success_it_counts() -> None:
    """`success` next to a scenario table means something else there.

    The two tables are read side by side in the scoreboard: this column
    counts whole conversations whose every turn succeeded, while the
    scenario table's counts repetitions whose diagnosis was graded
    correct. One shared word for two measurements misreads as one.
    """
    header = render_journey_markdown([_journey_report()]).splitlines()[0]
    cells = [cell.strip() for cell in header.strip("|").split("|")]

    assert "all-turn journeys" in cells
    assert "success" not in cells
