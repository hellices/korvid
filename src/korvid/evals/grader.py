"""Deterministic grading for eval runs (issue #69): no LLM judge.

Keyword assertions are matched on token runs: text is split on camel-case
boundaries, punctuation, and whitespace, then lowercased, and a keyword
matches when its concatenated tokens equal the concatenation of some
contiguous run of answer tokens. "OOM-killed", "OOMKilled", "oomkilled"
and "oom killed" therefore all count as the same mention, while
"unhealthy" never matches "healthy".

Polarity applies to both assertion kinds. `must_mention` requires a
*positive* claim: a match negated by an earlier negator ("the pod is not
healthy") does not satisfy the group — otherwise every healthy negative
control would credit its own over-diagnosis. `must_not_mention`
symmetrically fails only *positive* claims of the misdiagnosis: ruling
out the competing cause ("there is no evidence of an image pull
problem") is part of a correct answer, while hedging both causes
("either OOM or an image pull failure") is not a diagnosis. A negator's
scope runs to the end of its clause: it stops at sentence/clause
punctuation and at coordinating conjunctions, so "not restarting and
healthy now" still claims "healthy" positively.

Evidence assertions grade provenance, not the diagnostic path: any
successful read whose full result contains the expected substring — tool
output is machine-formatted, so exactness is the point there — and whose
arguments target the same object (every expected argument *value* except
the route-specific "kind" appears among the call's argument values;
"kind" values, when both sides name one, are canonicalized through the
resource alias table and must agree) counts, whichever tool fetched it.
Fetching the *wrong* object whose output happens to contain the same
substring is still not credited.
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

#: How a negator's scope ends: clause punctuation splits the text before
#: tokenization, and these coordinating tokens end the scope within one.
_CLAUSE_SPLIT = re.compile(r"[.,;:!?\n()]")

_SCOPE_BREAKERS = frozenset(
    {"and", "but", "however", "yet", "although", "though", "whereas", "while"}
)


def _clause_tokens(text: str) -> tuple[list[str], list[int]]:
    """Flat token list plus, per token, the id of the clause it came from."""
    tokens: list[str] = []
    clause_ids: list[int] = []
    for clause_id, clause in enumerate(_CLAUSE_SPLIT.split(text)):
        for token in _tokens(clause):
            tokens.append(token)
            clause_ids.append(clause_id)
    return tokens, clause_ids


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


def _negated(start: int, answer_tokens: list[str], clause_ids: list[int]) -> bool:
    """True when a negator precedes `start` within the same clause with no
    scope breaker in between — its negation still covers the match."""
    for index in range(start - 1, -1, -1):
        if clause_ids[index] != clause_ids[start]:
            return False
        token = answer_tokens[index]
        if token in _SCOPE_BREAKERS:
            return False
        if token in _NEGATORS:
            return True
    return False


def _mentions_positively(keyword: str, answer_tokens: list[str], clause_ids: list[int]) -> bool:
    """True when some match of the keyword is *not* under an earlier
    negator's scope — "the pod is not healthy" must not satisfy a required
    "healthy" claim (negative controls catch over-diagnosis)."""
    return any(
        not _negated(start, answer_tokens, clause_ids)
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
    """Provenance is the fetched fact plus its target, not the diagnostic
    path: any successful call whose result contains the expected content
    and whose arguments name the same object counts, whichever read tool
    fetched it (`evidence.tool` documents one known-good route, verified
    reachable by the fixture-integrity test)."""
    if evidence.contains not in record.result:
        return False
    # A failed call is not evidence even when its error message echoes the
    # expected substring (e.g. the object name in a not-found message).
    if record.result.startswith("ERROR:"):
        return False
    expected = _canonical_args(evidence.args)
    actual = _canonical_args(record.arguments)
    # "kind" is route-specific (diagnose_pod has no kind argument), but when
    # both sides name one they must agree: a deployment `web` is not
    # evidence about a pod `web`.
    if "kind" in expected and "kind" in actual and expected["kind"] != actual["kind"]:
        return False
    actual_values = set(actual.values())
    return all(value in actual_values for key, value in expected.items() if key != "kind")


def grade(scenario: Scenario, answer: str, records: list[ToolRecord]) -> GradeResult:
    """Grade one run: the final answer text plus the recorded tool trace."""
    answer_tokens, clause_ids = _clause_tokens(answer)
    missing_mentions = tuple(
        group
        for group in scenario.must_mention
        if not any(_mentions_positively(alt, answer_tokens, clause_ids) for alt in group)
    )
    forbidden_mentions = tuple(
        # One violation per group: alternates are spellings of the same
        # claim, and token-run matching makes several of them hit at once.
        next(alt for alt in group if _mentions_positively(alt, answer_tokens, clause_ids))
        for group in scenario.must_not_mention
        if any(_mentions_positively(alt, answer_tokens, clause_ids) for alt in group)
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
