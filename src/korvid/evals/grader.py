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
#: Clause boundaries for negation scoping. Dashes are included because
#: models punctuate with them constantly: without them "the pod was not
#: restarted — it was OOMKilled" scores as never claiming OOMKilled, while
#: the identical sentence with a full stop passes. A spaced hyphen is the
#: ASCII spelling of the same break; an unspaced one is left alone so
#: hyphenated words like `oom-killed` stay a single token.
_CLAUSE_SPLIT = re.compile(r"[.,;:!?\n()\u2014\u2013]|(?<=\s)-(?=\s)")

_SCOPE_BREAKERS = frozenset(
    {
        "and",
        "but",
        "however",
        "yet",
        "although",
        "though",
        "whereas",
        "while",
        # Causal conjunctions: in "not healthy because the probe is
        # failing" the negator scopes over "healthy" only — the stated
        # cause is a positive claim.
        "because",
        "since",
        "so",
    }
)


#: Linking verbs that can carry an all-clear predicate.
_COPULAS = frozenset(
    {"is", "are", "was", "were", "looks", "look", "seems", "seem", "appears", "appear"}
)

#: Tokens allowed between the copula and the adjective ("is still fine").
_EXCULPATION_FILLER = frozenset(
    {"still", "all", "quite", "perfectly", "completely", "totally", "to", "be"}
)

#: Adjectives that assert the named thing is *not* the fault. Deliberately
#: narrow: "unaffected", "serving" and "ready" are required claims in the
#: bundled pack, so admitting them here would reject correct answers. For
#: the same reason "working" and "good" are absent - "the liveness probe is
#: working too slowly and timing out" is a fault claim, and exculpating it
#: would drop the very diagnosis being graded.
_ALL_CLEAR = frozenset(
    {
        "fine",
        "normal",
        "ok",
        "okay",
        "healthy",
        "correct",
        "clean",
        "green",
        "passing",
        "succeeding",
    }
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


def _match_spans(keyword: str, answer_tokens: list[str]) -> list[tuple[int, int]]:
    """(start, end) token index pairs for every run matching the keyword.

    `end` is exclusive, so it is where a trailing predicate would begin.
    """
    target = "".join(_tokens(keyword))
    if not target:
        return []
    spans: list[tuple[int, int]] = []
    for start in range(len(answer_tokens)):
        run = ""
        end = start
        for token in answer_tokens[start:]:
            run += token
            end += 1
            if len(run) >= len(target):
                break
        if run == target:
            spans.append((start, end))
    return spans


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


def _exculpated(end: int, answer_tokens: list[str], clause_ids: list[int]) -> bool:
    """True when the match is followed, in its own clause, by a predicate
    declaring it *not* the problem — "the liveness probe is fine".

    This is the positive-grammar spelling of a negation, and a required
    group naming a topic is otherwise satisfied by ruling that topic out.
    Only a copula plus an all-clear adjective counts: scanning for any
    reassuring word would reject "api-5c2f is unaffected", which is a
    required claim elsewhere in the pack.
    """
    if end >= len(answer_tokens):
        return False
    index = end
    clause = clause_ids[end - 1]
    while index < len(answer_tokens) and clause_ids[index] == clause:
        token = answer_tokens[index]
        if token in _SCOPE_BREAKERS or token in _NEGATORS:
            return False
        if token in _ALL_CLEAR:
            return True
        if token not in _COPULAS and token not in _EXCULPATION_FILLER:
            return False
        index += 1
    return False


def _mentions_positively(
    keyword: str,
    answer_tokens: list[str],
    clause_ids: list[int],
    *,
    exculpation: bool = True,
) -> bool:
    """True when some match of the keyword is neither under an earlier
    negator's scope nor followed by an all-clear predicate — "the pod is not
    healthy" and "the probe is fine" must both fail to satisfy the claim they
    name (negative controls catch over-diagnosis).

    `exculpation` is off for a scenario with no fault, where the all-clear is
    the answer being graded rather than a way of dodging it."""
    return any(
        not _negated(start, answer_tokens, clause_ids)
        and not (exculpation and _exculpated(end, answer_tokens, clause_ids))
        for start, end in _match_spans(keyword, answer_tokens)
    )


#: Per-tool identity: which argument names the target, and which resource
#: kinds that tool's result carries evidence about.
#:
#: Keyed by tool, not by argument name. Read handlers take the argument
#: mapping directly and ignore keys they do not use, and the runner does
#: not mark an undeclared key as malformed — so `diagnose_pvc(pvc=…,
#: service=…)` really fetches a PVC, and letting a stray identity-shaped
#: key decide the kind would let it satisfy Service evidence.
#:
#: The kinds are a *set* because a compound diagnostic reports across
#: several resources: `diagnose_service` returns the Service together with
#: its endpoint readiness, so evidence expressed against any of those is
#: genuinely reachable through it.
_TOOL_IDENTITY: dict[str, tuple[str, frozenset[str]]] = {
    "get_logs": ("pod", frozenset({"pods"})),
    "diagnose_pod": ("pod", frozenset({"pods"})),
    "diagnose_service": (
        "service",
        frozenset({"services", "endpoints", "endpointslices"}),
    ),
    "diagnose_pvc": ("pvc", frozenset({"persistentvolumeclaims"})),
}


def _canonical_args(args: dict[str, Any], tool: str | None = None) -> dict[str, str]:
    """Lowercase argument values keyed by canonical parameter name.

    The identity argument of a known tool (`pod`, `service`, `pvc`) folds
    onto `name`, so evidence written against a resource is satisfied
    whichever read tool reached it. `kind` values go through the resource
    alias table so `deploy`, `deployment` and `deployments` compare equal.
    """
    identity = _TOOL_IDENTITY.get(tool or "")
    identity_key = identity[0] if identity is not None else None
    canonical: dict[str, str] = {}
    for key, value in args.items():
        text = str(value).strip().lower()
        if key == "kind":
            canonical["kind"] = _canonical_kind(text)
        elif key == identity_key:
            continue  # settled after the loop, so key order cannot decide it
        elif key == "name" and identity_key is not None:
            continue  # the tool does not read it; it must not claim `name`
        else:
            canonical[key] = text
    if identity_key is not None and identity_key in args:
        canonical["name"] = str(args[identity_key]).strip().lower()
    return canonical


def _kinds(args: dict[str, str], tool: str | None) -> frozenset[str] | None:
    """Resource kinds a call or an evidence item is about, or None when it
    places no constraint. A tool's own kinds win over a `kind` argument it
    does not read."""
    identity = _TOOL_IDENTITY.get(tool or "")
    if identity is not None:
        return identity[1]
    kind = args.get("kind")
    return frozenset({kind}) if kind is not None else None


def _canonical_kind(text: str) -> str:
    meta = _ALIASES.get(text)
    return meta.plural if meta is not None else text


def matches_target(evidence: Evidence, record: ToolRecord) -> bool:
    """True when the call's arguments name the same object as the evidence.

    Matching is keyed, not positional: each expected argument must appear
    under the same canonical parameter name with the same value, so a call
    with `pod` and `namespace` values swapped is a different target. Only
    result-independent targeting is checked here — success and content are
    `_satisfies`' job (the runner also uses this for its on-target rate).
    """
    expected = _canonical_args(evidence.args, evidence.tool)
    actual = _canonical_args(record.arguments, record.name)
    # Kinds are route-specific (a diagnostic has no `kind` argument), but
    # when both sides name any, they must overlap: a deployment `web` is
    # not evidence about a pod `web`. A compound diagnostic covers several
    # kinds, so this is an intersection rather than equality.
    expected_kinds = _kinds(expected, evidence.tool)
    actual_kinds = _kinds(actual, record.name)
    if (
        expected_kinds is not None
        and actual_kinds is not None
        and not expected_kinds & actual_kinds
    ):
        return False
    return all(actual.get(key) == value for key, value in expected.items() if key != "kind")


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
    return matches_target(evidence, record)


def grade(scenario: Scenario, answer: str, records: list[ToolRecord]) -> GradeResult:
    """Grade one run: the final answer text plus the recorded tool trace."""
    answer_tokens, clause_ids = _clause_tokens(answer)
    # Polarity is not symmetric here. A scenario with no fault is graded on
    # an all-clear, so "the endpoints are healthy" is its required answer
    # rather than a way of dodging the question - exculpation must not
    # suppress it. A *forbidden* keyword is the opposite in every scenario:
    # "the image pull looks normal" rules the pull out, and counting it as a
    # positive diagnosis would reject the answer for saying the truth.
    requires_all_clear = scenario.root_cause == "none"

    def claims(keyword: str) -> bool:
        return _mentions_positively(
            keyword, answer_tokens, clause_ids, exculpation=not requires_all_clear
        )

    def diagnoses(keyword: str) -> bool:
        return _mentions_positively(keyword, answer_tokens, clause_ids, exculpation=True)

    missing_mentions = tuple(
        group for group in scenario.must_mention if not any(claims(alt) for alt in group)
    )
    forbidden_mentions = tuple(
        # One violation per group: alternates are spellings of the same
        # claim, and token-run matching makes several of them hit at once.
        next(alt for alt in group if diagnoses(alt))
        for group in scenario.must_not_mention
        if any(diagnoses(alt) for alt in group)
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
