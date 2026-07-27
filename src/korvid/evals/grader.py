"""Deterministic grading for eval runs (issue #69): no LLM judge.

Keyword assertions are matched on token runs: text is split on camel-case
boundaries, punctuation, and whitespace, then lowercased, and a keyword
matches when its concatenated tokens equal the concatenation of some
contiguous run of answer tokens. "OOM-killed", "OOMKilled", "oomkilled"
and "oom killed" therefore all count as the same mention, while
"unhealthy" never matches "healthy". Matching is mention-based: a negated
mention ("this is not an image pull problem") still counts, so
`must_not_mention` keywords must be ones a correct answer would never
bring up.

Evidence assertions match raw substrings of full tool results — tool
output is machine-formatted, so exactness is the point there — and the
call's arguments must cover the expected ones ("kind" values are
canonicalized through the resource alias table), so fetching the *wrong*
object whose output happens to contain the same substring is not
credited.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from korvid.evals.fake_kube import builtin_aliases
from korvid.evals.scenario import Evidence, Scenario

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

_ALIASES = builtin_aliases()


@dataclass(frozen=True)
class ToolRecord:
    """One executed tool call: structured arguments plus its **full**
    (uncapped-summary) result."""

    name: str
    arguments: dict[str, Any]
    result: str


@dataclass(frozen=True)
class GradeResult:
    """Outcome of grading one run's final answer + tool trace."""

    #: Every must_mention group hit and no must_not_mention keyword present.
    diagnosis_success: bool
    #: Every expected_evidence entry appeared in a tool result whose call
    #: also used the expected arguments.
    evidence_fetched: bool
    missing_mentions: tuple[tuple[str, ...], ...]
    forbidden_mentions: tuple[str, ...]
    missing_evidence: tuple[Evidence, ...]


def _tokens(text: str) -> list[str]:
    split = _CAMEL_BOUNDARY.sub(" ", text)
    return [token for token in _NON_ALNUM.split(split.lower()) if token]


def _mentions(keyword: str, answer_tokens: list[str]) -> bool:
    """True when the keyword's concatenated tokens equal the concatenation
    of some contiguous run of answer tokens (exact-boundary matching)."""
    target = "".join(_tokens(keyword))
    if not target:
        return False
    for start in range(len(answer_tokens)):
        run = ""
        for token in answer_tokens[start:]:
            run += token
            if len(run) >= len(target):
                break
        if run == target:
            return True
    return False


def _canonical_args(args: dict[str, Any]) -> dict[str, str]:
    """Lowercase argument values; canonicalize "kind" through the resource
    alias table so `deploy`, `deployment` and `deployments` compare equal."""
    canonical: dict[str, str] = {}
    for key, value in args.items():
        text = str(value).strip().lower()
        if key == "kind":
            meta = _ALIASES.get(text)
            if meta is not None:
                text = meta.plural
        canonical[key] = text
    return canonical


def _satisfies(evidence: Evidence, record: ToolRecord) -> bool:
    if record.name != evidence.tool or evidence.contains not in record.result:
        return False
    actual = _canonical_args(record.arguments)
    return all(
        actual.get(key) == expected for key, expected in _canonical_args(evidence.args).items()
    )


def grade(scenario: Scenario, answer: str, records: list[ToolRecord]) -> GradeResult:
    """Grade one run: the final answer text plus the recorded tool trace."""
    answer_tokens = _tokens(answer)
    missing_mentions = tuple(
        group
        for group in scenario.must_mention
        if not any(_mentions(alt, answer_tokens) for alt in group)
    )
    forbidden_mentions = tuple(
        # One violation per group: alternates are spellings of the same
        # claim, and token-run matching makes several of them hit at once.
        next(alt for alt in group if _mentions(alt, answer_tokens))
        for group in scenario.must_not_mention
        if any(_mentions(alt, answer_tokens) for alt in group)
    )
    missing_evidence = tuple(
        evidence
        for evidence in scenario.expected_evidence
        if not any(_satisfies(evidence, record) for record in records)
    )
    return GradeResult(
        diagnosis_success=not missing_mentions and not forbidden_mentions,
        evidence_fetched=not missing_evidence,
        missing_mentions=missing_mentions,
        forbidden_mentions=forbidden_mentions,
        missing_evidence=missing_evidence,
    )
