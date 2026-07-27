"""Tests for the deterministic grader (issue #69)."""

from __future__ import annotations

from typing import Any

from korvid.evals.grader import GradeResult, ToolRecord, grade
from korvid.evals.scenario import Evidence, Scenario

_EVIDENCE = Evidence(
    tool="diagnose_pod",
    contains="exit=137",
    args={"pod": "checkout-1", "namespace": "shop"},
)


def _scenario(**overrides: Any) -> Scenario:
    fields: dict[str, Any] = {
        "id": "s1",
        "question": "q",
        "screen": "pods view",
        "root_cause": "oom_killed",
        "must_mention": (("oomkilled", "oom killed"), ("137",)),
        "must_not_mention": (("image pull", "imagepull"),),
        "expected_evidence": ((_EVIDENCE,),),
    }
    fields.update(overrides)
    return Scenario(**fields)


def _record(
    name: str = "diagnose_pod",
    result: str = "... exit=137 (OOMKilled) ...",
    arguments: dict[str, Any] | None = None,
) -> ToolRecord:
    if arguments is None:
        arguments = {"pod": "checkout-1", "namespace": "shop"}
    return ToolRecord(name=name, arguments=arguments, result=result)


def test_grade_passes_when_all_assertions_hold() -> None:
    result = grade(
        _scenario(),
        "The checkout container was OOMKilled (exit code 137); raise its memory limit.",
        [_record()],
    )
    assert isinstance(result, GradeResult)
    assert result.diagnosis_success
    assert result.evidence_fetched
    assert result.missing_mentions == ()
    assert result.forbidden_mentions == ()
    assert result.missing_evidence == ()


def test_grade_matches_keywords_across_punctuation_and_case() -> None:
    """'OOM-killed' and 'oom killed' normalize to the same token stream."""
    result = grade(_scenario(), "It was OOM-killed with exit 137.", [_record()])
    assert result.diagnosis_success


def test_grade_matches_camel_case_answers_against_spaced_keywords() -> None:
    """'OOMKilled' in the answer matches the keyword 'oom killed'."""
    scenario = _scenario(must_mention=(("oom killed",), ("137",)))
    result = grade(scenario, "The container was OOMKilled (exit 137).", [_record()])
    assert result.diagnosis_success


def test_grade_matches_spaced_answers_against_compact_keywords() -> None:
    """'oom killed' in the answer matches the keyword 'oomkilled'."""
    scenario = _scenario(must_mention=(("oomkilled",), ("137",)))
    result = grade(scenario, "The container was oom killed (exit 137).", [_record()])
    assert result.diagnosis_success


def test_grade_keywords_never_match_inside_larger_words() -> None:
    """'healthy' must not match 'unhealthy' — token boundaries are exact."""
    scenario = _scenario(must_mention=(("healthy",),), expected_evidence=())
    result = grade(scenario, "The pod is unhealthy.", [])
    assert not result.diagnosis_success


def test_grade_fails_when_a_mention_group_is_missing() -> None:
    result = grade(_scenario(), "The container was OOMKilled.", [_record()])
    assert not result.diagnosis_success
    assert result.missing_mentions == (("137",),)


def test_grade_fails_on_a_forbidden_claim_even_with_the_right_answer() -> None:
    answer = "OOMKilled, exit 137 — though it could also be an image pull problem."
    result = grade(_scenario(), answer, [_record()])
    assert not result.diagnosis_success
    assert result.forbidden_mentions == ("image pull",)


def test_grade_reports_unfetched_evidence() -> None:
    result = grade(_scenario(), "OOMKilled, exit 137.", [])
    assert result.diagnosis_success  # the answer itself is right...
    assert not result.evidence_fetched  # ...but the model never fetched proof
    assert result.missing_evidence == ((_EVIDENCE,),)


def test_grade_evidence_requires_the_matching_tool_name() -> None:
    records = [_record(name="get_logs", result="exit=137 seen in logs")]
    result = grade(_scenario(), "OOMKilled, exit 137.", records)
    assert not result.evidence_fetched


def test_grade_evidence_matches_raw_substrings_not_normalized_text() -> None:
    records = [_record(result="terminated exit = 137")]  # spaced differently
    result = grade(_scenario(), "OOMKilled, exit 137.", records)
    assert not result.evidence_fetched


def test_grade_evidence_requires_the_expected_arguments() -> None:
    """The same substring from a call against the *wrong* object is not
    credited as evidence."""
    records = [_record(arguments={"pod": "other-pod", "namespace": "shop"})]
    result = grade(_scenario(), "OOMKilled, exit 137.", records)
    assert not result.evidence_fetched
    assert result.missing_evidence == ((_EVIDENCE,),)


def test_grade_evidence_ignores_extra_arguments_beyond_the_expected() -> None:
    records = [_record(arguments={"pod": "checkout-1", "namespace": "shop", "tail": 50})]
    result = grade(_scenario(), "OOMKilled, exit 137.", records)
    assert result.evidence_fetched


def test_grade_evidence_canonicalizes_kind_aliases() -> None:
    """`kind: deploy` and `kind: deployments` compare equal via the alias table."""
    evidence = Evidence(
        tool="get_resource",
        contains="readyReplicas: 2",
        args={"kind": "deployments", "name": "web", "namespace": "shop"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="get_resource",
            result="status:\n  readyReplicas: 2",
            arguments={"kind": "deploy", "name": "web", "namespace": "shop"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert result.evidence_fetched


def test_grade_evidence_group_is_satisfied_by_any_alternative() -> None:
    """A group of alternative locations accepts whichever path the model took."""
    alternative = Evidence(
        tool="get_resource",
        contains="exit=137",
        args={"kind": "pods", "name": "checkout-1", "namespace": "shop"},
    )
    scenario = _scenario(expected_evidence=((_EVIDENCE, alternative),))
    records = [
        _record(
            name="get_resource",
            result="lastState: exit=137",
            arguments={"kind": "pods", "name": "checkout-1", "namespace": "shop"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert result.evidence_fetched


def test_grade_negative_control_scenario() -> None:
    scenario = _scenario(
        root_cause="none",
        must_mention=(("healthy", "no issue", "nothing is wrong"),),
        must_not_mention=(("crashloop",), ("oomkilled",)),
        expected_evidence=(),
    )
    result = grade(scenario, "Everything looks healthy; no action needed.", [])
    assert result.diagnosis_success
    assert result.evidence_fetched  # nothing was required


def test_grade_required_mention_must_be_a_positive_claim() -> None:
    """Negative controls exist to catch over-diagnosis: 'the pod is not
    healthy' must not satisfy a required 'healthy' claim."""
    scenario = _scenario(
        root_cause="none",
        must_mention=(("healthy", "no issues"),),
        must_not_mention=(),
        expected_evidence=(),
    )
    result = grade(scenario, "The pod is not healthy — something is wrong.", [])
    assert not result.diagnosis_success
    assert result.missing_mentions == (("healthy", "no issues"),)


def test_grade_required_mention_rejects_contraction_negations() -> None:
    scenario = _scenario(
        root_cause="none",
        must_mention=(("healthy",),),
        must_not_mention=(),
        expected_evidence=(),
    )
    result = grade(scenario, "This pod isn't healthy.", [])
    assert not result.diagnosis_success


def test_grade_required_mention_counts_a_separate_positive_match() -> None:
    scenario = _scenario(
        root_cause="none",
        must_mention=(("healthy",),),
        must_not_mention=(),
        expected_evidence=(),
    )
    answer = "The restarts were not healthy signs at first, but the pod is healthy now."
    result = grade(scenario, answer, [])
    assert result.diagnosis_success


def test_grade_required_mention_keywords_may_start_with_a_negator() -> None:
    """Keywords like 'no issues' or 'not set' negate themselves by design;
    the negation window only scans tokens *before* the match."""
    scenario = _scenario(
        root_cause="none",
        must_mention=(("no issues",),),
        must_not_mention=(),
        expected_evidence=(),
    )
    result = grade(scenario, "There are no issues with this pod.", [])
    assert result.diagnosis_success


def test_grade_forbidden_mentions_allow_explicit_rule_outs() -> None:
    """Ruling out the competing cause is part of a correct diagnosis:
    a *negated* mention of a forbidden keyword must not fail the run."""
    answer = "OOMKilled, exit 137 — this is not an image pull problem."
    result = grade(_scenario(), answer, [_record()])
    assert result.diagnosis_success
    assert result.forbidden_mentions == ()


def test_grade_forbidden_mentions_fail_on_hedged_double_diagnoses() -> None:
    """An answer that positively claims the competing cause alongside the
    right one is a hedge, not a diagnosis."""
    answer = "Either OOMKilled (exit 137) or an image pull failure."
    result = grade(_scenario(), answer, [_record()])
    assert not result.diagnosis_success
    assert result.forbidden_mentions == ("image pull",)


def test_grade_forbidden_keywords_may_contain_their_own_negator() -> None:
    """Negative-control forbidden keywords like 'no endpoints' negate
    themselves; the window only scans tokens *before* the match."""
    scenario = _scenario(
        root_cause="none",
        must_mention=(("healthy",),),
        must_not_mention=(("no endpoints",),),
        expected_evidence=(),
    )
    result = grade(scenario, "The service looks healthy but has no endpoints.", [])
    assert not result.diagnosis_success
    assert result.forbidden_mentions == ("no endpoints",)
