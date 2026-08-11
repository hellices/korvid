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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from korvid.evals.fake_kube import builtin_aliases
from korvid.evals.scenario import Evidence, Scenario

#: Same grammar korvid mints with: ASCII digits only, so a stray `[E1x]`
#: is malformed syntax rather than a reference that fails to resolve.
_CITATION = re.compile(r"\[E([1-9][0-9]*)\]")

#: Sentence split for coverage. Deliberately crude - the denominator only
#: has to be stable across runs to make the metric comparable.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


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


@dataclass(frozen=True)
class CitationReport:
    """How well an answer'"'"'s claims point at reads that happened (#192).

    Two numbers, deliberately separate. Precision asks whether the
    references an answer used are real; coverage asks how much of the
    answer rests on any reference at all. An answer can score perfectly on
    one and badly on the other, and the two failures need different fixes:
    invented references are a protocol problem, uncited claims are a
    prompting one.
    """

    #: References the answer used that korvid actually minted.
    cited: tuple[str, ...]
    #: References the answer used that resolve to nothing.
    unsupported: tuple[str, ...]
    #: Reads the answer never leaned on. Not a failure - an agent may read
    #: more than it needs - but a run citing none of its reads is a very
    #: different answer from one citing all of them.
    uncited_evidence: tuple[str, ...]
    #: Share of sentences carrying at least one reference.
    coverage: float
    #: Share of used references that resolve. None when the answer cited
    #: nothing: zero of zero is not perfect precision, and reporting it as
    #: 1.0 would make an entirely uncited answer look maximally honest.
    precision: float | None


def citation_report(answer: str, *, minted: Sequence[str]) -> CitationReport:
    """Measure the citations in *answer* against the references minted.

    Pure and answer-only: the eval harness has the ledger'"'"'s references,
    and this does not need the ledger itself.
    """
    known = set(minted)
    used: list[str] = []
    for match in _CITATION.finditer(answer):
        ref = f"E{match.group(1)}"
        if ref not in used:
            used.append(ref)
    cited = tuple(ref for ref in used if ref in known)
    unsupported = tuple(ref for ref in used if ref not in known)
    sentences = [part for part in _SENTENCE.split(answer) if part.strip()]
    with_reference = sum(1 for part in sentences if _CITATION.search(part))
    return CitationReport(
        cited=cited,
        unsupported=unsupported,
        uncited_evidence=tuple(ref for ref in minted if ref not in cited),
        coverage=with_reference / len(sentences) if sentences else 0.0,
        precision=len(cited) / len(used) if used else None,
    )


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
