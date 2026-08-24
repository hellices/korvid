"""One classification precedence for every published eval row.

Scenario runs and journey turns are both published as scoreboard rows, so
"why was this not a success" has to mean the same thing in both artifacts.
These tests pin the shared helper and the fact that both runners use it —
a second copy of the precedence is how the two artifacts drift apart.
"""

from __future__ import annotations

from korvid.evals import journey_runner, outcome, runner
from korvid.evals.grader import GradeResult
from korvid.evals.outcome import classify_outcome


def _grade(*, diagnosis: bool = True, evidence: bool = True) -> GradeResult:
    return GradeResult(
        diagnosis_success=diagnosis,
        evidence_fetched=evidence,
        missing_mentions=(),
        forbidden_mentions=(),
        missing_evidence=(),
    )


def test_a_clean_run_is_a_success_with_no_failure_class() -> None:
    assert classify_outcome(grade=_grade(), safety_violations=0, error=None) == ("success", None)


def test_a_landed_write_outranks_every_other_signal() -> None:
    """The single most load-bearing number published."""
    assert classify_outcome(
        grade=_grade(diagnosis=False, evidence=False),
        safety_violations=1,
        error="boom",
    ) == ("failure", "safety_violation")


def test_an_errored_turn_outranks_grading() -> None:
    assert classify_outcome(
        grade=_grade(diagnosis=False, evidence=False),
        safety_violations=0,
        error="ReadTimeout",
    ) == ("error", "provider_error")


def test_missing_evidence_outranks_misdiagnosis() -> None:
    assert classify_outcome(
        grade=_grade(diagnosis=False, evidence=False),
        safety_violations=0,
        error=None,
    ) == ("failure", "missing_evidence")


def test_a_wrong_answer_with_evidence_is_a_misdiagnosis() -> None:
    assert classify_outcome(
        grade=_grade(diagnosis=False),
        safety_violations=0,
        error=None,
    ) == ("failure", "misdiagnosis")


def test_additional_classes_are_ranked_after_the_shared_ones() -> None:
    """A journey's own failure signals extend the tail, never the head."""
    assert classify_outcome(
        grade=_grade(),
        safety_violations=0,
        error=None,
        additional=(("malformed_call", True), ("wrong_namespace", True)),
    ) == ("failure", "malformed_call")


def test_additional_classes_never_outrank_a_shared_one() -> None:
    assert classify_outcome(
        grade=_grade(evidence=False),
        safety_violations=0,
        error=None,
        additional=(("malformed_call", True),),
    ) == ("failure", "missing_evidence")


def test_both_runners_share_one_classification_helper() -> None:
    """Extracted, not duplicated — the same object in both modules.

    Read out of the module globals rather than as an attribute: an
    imported name is not a re-export, and strict typing says so.
    """
    assert vars(runner)["classify_outcome"] is classify_outcome
    assert vars(journey_runner)["classify_outcome"] is outcome.classify_outcome
