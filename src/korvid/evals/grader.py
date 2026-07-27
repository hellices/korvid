"""Deterministic grading for eval runs (issue #69): no LLM judge.

Keyword assertions are matched on normalized text (lowercase, punctuation
collapsed to single spaces) so "OOM-killed", "OOMKilled" and "oom killed"
all count as the same mention. Evidence assertions match raw substrings
of full tool results — tool output is machine-formatted, so exactness is
the point there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from korvid.evals.scenario import Evidence, Scenario

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ToolRecord:
    """One executed tool call with its **full** (uncapped-summary) result."""

    name: str
    arguments: str
    result: str


@dataclass(frozen=True)
class GradeResult:
    """Outcome of grading one run's final answer + tool trace."""

    #: Every must_mention group hit and no must_not_claim keyword present.
    diagnosis_success: bool
    #: Every expected_evidence pair appeared in a matching tool result.
    evidence_fetched: bool
    missing_mentions: tuple[tuple[str, ...], ...]
    forbidden_claims: tuple[str, ...]
    missing_evidence: tuple[Evidence, ...]


def _normalize(text: str) -> str:
    return f" {_NON_ALNUM.sub(' ', text.lower()).strip()} "


def grade(scenario: Scenario, answer: str, records: list[ToolRecord]) -> GradeResult:
    """Grade one run: the final answer text plus the recorded tool trace."""
    normalized_answer = _normalize(answer)
    missing_mentions = tuple(
        group
        for group in scenario.must_mention
        if not any(_normalize(alt) in normalized_answer for alt in group)
    )
    forbidden_claims = tuple(
        alt
        for group in scenario.must_not_claim
        for alt in group
        if _normalize(alt) in normalized_answer
    )
    missing_evidence = tuple(
        evidence
        for evidence in scenario.expected_evidence
        if not any(
            record.name == evidence.tool and evidence.contains in record.result
            for record in records
        )
    )
    return GradeResult(
        diagnosis_success=not missing_mentions and not forbidden_claims,
        evidence_fetched=not missing_evidence,
        missing_mentions=missing_mentions,
        forbidden_claims=forbidden_claims,
        missing_evidence=missing_evidence,
    )
