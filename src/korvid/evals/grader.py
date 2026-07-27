"""Deterministic grading for eval runs (issue #69): no LLM judge.

Keyword assertions are matched on token runs: text is split on camel-case
boundaries, punctuation, and whitespace, then lowercased, and a keyword
matches when its concatenated tokens equal the concatenation of some
contiguous run of answer tokens. "OOM-killed", "OOMKilled", "oomkilled"
and "oom killed" therefore all count as the same mention, while
"unhealthy" never matches "healthy".

Polarity applies to both assertion kinds. `must_mention` requires a
*positive* claim: a match whose preceding tokens contain a negator ("the
pod is not healthy") does not satisfy the group — otherwise every healthy
negative control would credit its own over-diagnosis. `must_not_mention`
symmetrically fails only *positive* claims of the misdiagnosis: ruling
out the competing cause ("this is not an image pull problem") is part of
a correct answer, while hedging both causes ("either OOM or an image
pull failure") is not a diagnosis.

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

    #: Every must_mention group claimed positively and no must_not_mention
    #: keyword claimed positively.
    diagnosis_success: bool
    #: Every expected_evidence group had at least one alternative fetched
    #: with the expected arguments.
    evidence_fetched: bool
    missing_mentions: tuple[tuple[str, ...], ...]
    forbidden_mentions: tuple[str, ...]
    missing_evidence: tuple[tuple[Evidence, ...], ...]


def _tokens(text: str) -> list[str]:
    split = _CAMEL_BOUNDARY.sub(" ", text)
    return [token for token in _NON_ALNUM.split(split.lower()) if token]


#: Tokens that flip the polarity of a nearby claim. Contraction stems
#: (isn't → "isn" + "t") are included because tokenization splits them.
_NEGATORS = frozenset(
    {
        "not",
        "no",
        "never",
        "none",
        "nothing",
        "neither",
        "nor",
        "without",
        "cannot",
        "isn",
        "aren",
        "wasn",
        "weren",
        "don",
        "doesn",
        "didn",
        "hasn",
        "haven",
        "hadn",
        "won",
        "wouldn",
        "couldn",
        "shouldn",
    }
)

#: How many tokens before a match to scan for a negator.
_NEGATION_WINDOW = 3


def _match_starts(keyword: str, answer_tokens: list[str]) -> list[int]:
    """Start indices of every token run matching the keyword."""
    target = "".join(_tokens(keyword))
    if not target:
        return []
    starts: list[int] = []
    for start in range(len(answer_tokens)):
        run = ""
        for token in answer_tokens[start:]:
            run += token
            if len(run) >= len(target):
                break
        if run == target:
            starts.append(start)
    return starts


def _mentions_positively(keyword: str, answer_tokens: list[str]) -> bool:
    """True when some match of the keyword is *not* preceded by a negator
    within the negation window — "the pod is not healthy" must not satisfy
    a required "healthy" claim (negative controls catch over-diagnosis)."""
    return any(
        not any(
            token in _NEGATORS for token in answer_tokens[max(0, start - _NEGATION_WINDOW) : start]
        )
        for start in _match_starts(keyword, answer_tokens)
    )


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
    # A failed call is not evidence even when its error message echoes the
    # expected substring (e.g. the object name in a not-found message).
    if record.result.startswith("ERROR:"):
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
        if not any(_mentions_positively(alt, answer_tokens) for alt in group)
    )
    forbidden_mentions = tuple(
        # One violation per group: alternates are spellings of the same
        # claim, and token-run matching makes several of them hit at once.
        next(alt for alt in group if _mentions_positively(alt, answer_tokens))
        for group in scenario.must_not_mention
        if any(_mentions_positively(alt, answer_tokens) for alt in group)
    )
    missing_evidence = tuple(
        group
        for group in scenario.expected_evidence
        if not any(_satisfies(alt, record) for alt in group for record in records)
    )
    return GradeResult(
        diagnosis_success=not missing_mentions and not forbidden_mentions,
        evidence_fetched=not missing_evidence,
        missing_mentions=missing_mentions,
        forbidden_mentions=forbidden_mentions,
        missing_evidence=missing_evidence,
    )
