"""Tests for the deterministic grader (issue #69)."""

from __future__ import annotations

from typing import Any

import pytest

from korvid.evals.grader import GradeResult, ToolRecord, citation_report, grade, matches_target
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


def test_grade_evidence_accepts_any_route_to_the_same_fact() -> None:
    """Provenance is the fetched fact plus its target, not the diagnostic
    path: observing the ground-truth content through another read tool
    aimed at the same object counts as evidence."""
    records = [
        ToolRecord(
            name="get_resource",
            arguments={"kind": "pods", "name": "checkout-1", "namespace": "shop"},
            result="... exit=137 (OOMKilled) ...",
        )
    ]
    result = grade(_scenario(), "OOMKilled, exit 137.", records)
    assert result.evidence_fetched


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


def test_grade_evidence_rejects_error_results() -> None:
    """A failed call whose ERROR message echoes the expected substring
    (e.g. the object name in a not-found message) is not evidence."""
    record = _record(result="ERROR: could not diagnose: exit=137 pod not found")
    result = grade(_scenario(), "OOMKilled, exit 137.", [record])
    assert not result.evidence_fetched
    assert result.missing_evidence == ((_EVIDENCE,),)


def test_grade_evidence_rejects_another_kind_with_the_same_name() -> None:
    """When both the expected and actual call name a kind, they must agree —
    a deployment `web` is not evidence about a pod `web`."""
    evidence = Evidence(
        tool="get_resource",
        contains="app: web",
        args={"kind": "pods", "name": "web", "namespace": "shop"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        ToolRecord(
            name="get_resource",
            arguments={"kind": "deployments", "name": "web", "namespace": "shop"},
            result="... app: web ...",
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert not result.evidence_fetched


def test_grade_negation_scope_covers_longer_rule_outs() -> None:
    """A rule-out whose negator sits more than a fixed window before the
    keyword ('no evidence of an image pull problem') is still a rule-out."""
    answer = "OOMKilled, exit 137. There is no evidence of an image pull problem."
    result = grade(_scenario(), answer, [_record()])
    assert result.diagnosis_success
    assert result.forbidden_mentions == ()


def test_grade_negation_stops_at_sentence_boundaries() -> None:
    """A negator in the previous sentence must not negate this one."""
    scenario = _scenario(
        must_mention=(("image pull",),),
        must_not_mention=(),
        expected_evidence=(),
    )
    result = grade(scenario, "It is not. The image pull failed.", [])
    assert result.diagnosis_success


def test_grade_negation_stops_at_coordinating_conjunctions() -> None:
    """'not restarting and healthy now' claims healthy positively — the
    conjunction ends the negator's scope."""
    scenario = _scenario(
        root_cause="none",
        must_mention=(("healthy",),),
        must_not_mention=(),
        expected_evidence=(),
    )
    result = grade(scenario, "It is not restarting and healthy now.", [])
    assert result.diagnosis_success


def test_grade_evidence_rejects_swapped_argument_values() -> None:
    """Argument matching is keyed, not an unordered value set: a call whose
    pod and namespace values are swapped targets a different object even
    when its result happens to contain the expected substring."""
    records = [
        ToolRecord(
            name="diagnose_pod",
            arguments={"pod": "shop", "namespace": "checkout-1"},
            result="... exit=137 (OOMKilled) ...",
        )
    ]
    result = grade(_scenario(), "OOMKilled, exit 137.", records)
    assert not result.evidence_fetched


def test_matches_target_equates_pod_and_name_identity_keys() -> None:
    """`pod` and `name` are the same identity key across read tools, and
    target matching looks only at arguments — result content and success
    are graded separately."""
    record = ToolRecord(
        name="get_resource",
        arguments={"kind": "pods", "name": "checkout-1", "namespace": "shop"},
        result="ERROR: unreachable",
    )
    assert matches_target(_EVIDENCE, record)


def test_matches_target_requires_every_expected_argument() -> None:
    """An expected argument missing from the call entirely is a different
    target — repeated values in the call must not mask the gap."""
    record = ToolRecord(
        name="list_resources",
        arguments={"kind": "pods", "namespace": "shop"},
        result="checkout-1 ... exit=137",
    )
    assert not matches_target(_EVIDENCE, record)


def test_grade_negation_scope_ends_at_causal_conjunctions() -> None:
    """'the pod is not healthy because the readiness probe is failing' —
    the negator scopes over 'healthy' only; the cause after 'because' is a
    positive claim, not a rule-out."""
    scenario = _scenario(
        must_mention=(("readiness",), ("failing", "fails")),
        must_not_mention=(),
        expected_evidence=(),
    )
    answer = "The pod is not healthy because the readiness probe is failing."
    result = grade(scenario, answer, [])
    assert result.diagnosis_success


def test_grade_credits_diagnose_service_against_name_keyed_evidence() -> None:
    """`diagnose_service(service=...)` names the same object as `name=...`.

    Evidence is written against the resource identity, not one tool's
    parameter spelling. Without the alias a model that correctly reaches for
    the deterministic Service tool is graded as having fetched no evidence,
    which would make a baseline-versus-diagnostic comparison meaningless.
    """
    evidence = Evidence(
        tool="get_resource",
        contains="endpoints: 0",
        args={"kind": "services", "name": "web", "namespace": "front"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="diagnose_service",
            result="outcome: findings\nendpoints: 0",
            arguments={"service": "web", "namespace": "front"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert result.evidence_fetched


def test_grade_credits_diagnose_pvc_against_name_keyed_evidence() -> None:
    """`diagnose_pvc(pvc=...)` names the same object as `name=...`."""
    evidence = Evidence(
        tool="get_resource",
        contains="phase: Pending",
        args={"kind": "persistentvolumeclaims", "name": "data", "namespace": "front"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="diagnose_pvc",
            result="outcome: findings\nphase: Pending",
            arguments={"pvc": "data", "namespace": "front"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert result.evidence_fetched


def test_grade_still_rejects_a_diagnostic_call_against_a_different_object() -> None:
    """Folding the key must not fold the value: a different name is not evidence."""
    evidence = Evidence(
        tool="get_resource",
        contains="endpoints: 0",
        args={"kind": "services", "name": "web", "namespace": "front"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="diagnose_service",
            result="outcome: findings\nendpoints: 0",
            arguments={"service": "api", "namespace": "front"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert not result.evidence_fetched


def test_grade_rejects_a_diagnostic_call_against_a_different_kind() -> None:
    """Folding `pvc`/`service`/`pod` onto `name` must not fold away the kind.

    `matches_target` compares `kind` only when both sides carry one, and a
    diagnostic tool has no `kind` argument. Without an implied kind,
    `diagnose_pvc(pvc="web")` satisfies evidence about a *Service* named
    `web` whenever the report happens to contain the substring — inflating
    both evidence and on-target metrics.
    """
    evidence = Evidence(
        tool="get_resource",
        contains="endpoints: 0",
        args={"kind": "services", "name": "web", "namespace": "front"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="diagnose_pvc",
            result="outcome: findings\nendpoints: 0",
            arguments={"pvc": "web", "namespace": "front"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert not result.evidence_fetched


def test_grade_rejects_a_pod_read_against_deployment_evidence() -> None:
    """The same hole existed for `pod` before the diagnostic aliases."""
    evidence = Evidence(
        tool="get_resource",
        contains="readyReplicas: 0",
        args={"kind": "deployments", "name": "web", "namespace": "front"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="get_logs",
            result="readyReplicas: 0",
            arguments={"pod": "web", "namespace": "front"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert not result.evidence_fetched


def test_grade_still_credits_a_diagnostic_call_for_its_own_kind() -> None:
    """The implied kind must match its own evidence, not block it."""
    evidence = Evidence(
        tool="get_resource",
        contains="phase: Pending",
        args={"kind": "persistentvolumeclaims", "name": "data", "namespace": "front"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="diagnose_pvc",
            result="outcome: findings\nphase: Pending",
            arguments={"pvc": "data", "namespace": "front"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert result.evidence_fetched


def test_grade_prefers_the_implied_kind_over_a_conflicting_kind_argument() -> None:
    """An identity alias determines the kind; a stray `kind` cannot override it.

    Read handlers take the argument mapping directly and ignore keys they
    do not use — `_diagnose_pvc` reads only `pvc` and `namespace` — and the
    runner does not treat an undeclared key as malformed. So
    `diagnose_pvc(pvc="web", kind="services")` really fetches a PVC. If the
    explicit `kind` won, that call would canonicalize as a Service and
    satisfy Service evidence, reopening the cross-kind hole.
    """
    evidence = Evidence(
        tool="get_resource",
        contains="endpoints: 0",
        args={"kind": "services", "name": "web", "namespace": "front"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="diagnose_pvc",
            result="outcome: findings\nendpoints: 0",
            arguments={"pvc": "web", "kind": "services", "namespace": "front"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert not result.evidence_fetched


def test_grade_still_uses_an_explicit_kind_when_no_alias_is_present() -> None:
    """Routes without an identity alias — `get_resource`, `get_events` —
    must keep comparing on their own `kind` argument."""
    evidence = Evidence(
        tool="get_resource",
        contains="readyReplicas: 2",
        args={"kind": "deployments", "name": "web", "namespace": "front"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="get_resource",
            result="status:\n  readyReplicas: 2",
            arguments={"kind": "deploy", "name": "web", "namespace": "front"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert result.evidence_fetched


def test_grade_credits_diagnose_service_for_endpoint_evidence() -> None:
    """`diagnose_service` reports on the Service *and* its endpoints, so a
    single implied kind is too narrow.

    The bundled Service scenarios express endpoint evidence as
    `get_resource(kind: endpoints)`. Implying only `services` would leave
    this PR's target scenario ungraded — the tool would be used correctly
    and still score no evidence.
    """
    evidence = Evidence(
        tool="get_resource",
        contains="subsets: []",
        args={"kind": "endpoints", "name": "web", "namespace": "front"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="diagnose_service",
            result="outcome: findings\nsubsets: []",
            arguments={"service": "web", "namespace": "front"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert result.evidence_fetched


def test_grade_ignores_an_identity_key_the_tool_does_not_read() -> None:
    """The implied kind comes from the tool, not from whichever identity-
    shaped key happens to appear last.

    `_diagnose_pvc` reads `pvc` and `namespace`; a stray `service` key is
    inert at execution and the runner does not mark the trace malformed.
    Letting it decide the kind would satisfy Service evidence with a PVC
    read.
    """
    evidence = Evidence(
        tool="get_resource",
        contains="endpoints: 0",
        args={"kind": "services", "name": "web", "namespace": "front"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="diagnose_pvc",
            result="outcome: findings\nendpoints: 0",
            arguments={"pvc": "web", "service": "web", "namespace": "front"},
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert not result.evidence_fetched


@pytest.mark.parametrize(
    "arguments",
    [
        {"pvc": "web", "name": "other", "namespace": "front"},
        {"name": "other", "pvc": "web", "namespace": "front"},
    ],
)
def test_grade_target_does_not_depend_on_argument_order(
    arguments: dict[str, str],
) -> None:
    """A tool's identity argument decides the target, whichever order the
    keys arrived in.

    `_diagnose_pvc` reads `pvc`; a stray `name` is inert at execution. If
    it competed for the same canonical slot, the identical call would grade
    differently depending on JSON key order.
    """
    evidence = Evidence(
        tool="get_resource",
        contains="phase: Pending",
        args={"kind": "persistentvolumeclaims", "name": "web", "namespace": "front"},
    )
    scenario = _scenario(expected_evidence=((evidence,),))
    records = [
        _record(
            name="diagnose_pvc",
            result="outcome: findings\nphase: Pending",
            arguments=arguments,
        )
    ]
    result = grade(scenario, "OOMKilled, exit 137.", records)
    assert result.evidence_fetched


@pytest.mark.parametrize("dash", ["\u2014", "\u2013", " - "])
def test_grade_treats_a_dash_as_a_clause_boundary(dash: str) -> None:
    """A dash separates clauses, so a negator before it must not suppress
    the claim after it.

    Models punctuate with dashes constantly. Without this, "the pod was not
    restarted — it was OOMKilled" scores as never having claimed OOMKilled,
    and the identical sentence written with a full stop passes. That is a
    grading artifact, not a difference in diagnosis.
    """
    answer = (
        f"The container was not restarted by the operator{dash}it was OOMKilled with exit code 137."
    )
    result = grade(_scenario(), answer, [_record()])
    assert result.diagnosis_success, result.missing_mentions


def test_grade_still_scopes_a_negator_within_one_clause() -> None:
    """The fix must not stop negation working where there is no boundary."""
    result = grade(_scenario(), "The container was not OOMKilled at all.", [_record()])
    assert not result.diagnosis_success


# -- Citation honesty (issue #192): whether the answer's claims point at
# -- reads that actually happened.


def test_an_answer_with_no_citations_scores_zero_coverage() -> None:
    """Coverage is the share of sentences carrying a reference.

    Without it, the citation work of #192 cannot be shown to have changed
    anything - which is the point of measuring it.
    """
    report = citation_report("The pod is crash-looping. The image is missing.", minted=("E1",))

    assert report.coverage == 0.0
    assert report.cited == ()


def test_every_sentence_cited_is_full_coverage() -> None:
    report = citation_report(
        "The pod is crash-looping [E1]. The image is missing [E2].", minted=("E1", "E2")
    )

    assert report.coverage == 1.0
    assert report.precision == 1.0


def test_precision_counts_only_references_korvid_minted() -> None:
    """An invented reference is the failure this metric exists to catch."""
    report = citation_report("up [E1], node fine [E9]", minted=("E1",))

    assert report.precision == 0.5
    assert report.unsupported == ("E9",)


def test_precision_is_undefined_rather_than_perfect_without_citations() -> None:
    """Zero of zero is not 100% precision; reporting it as such would make
    an uncited answer look maximally honest."""
    report = citation_report("no citations here", minted=("E1",))

    assert report.precision is None


def test_a_repeated_citation_does_not_inflate_precision() -> None:
    report = citation_report("up [E1], still up [E1]", minted=("E1",))

    assert report.precision == 1.0
    assert report.cited == ("E1",)


def test_an_evidence_gap_is_reported_when_reads_go_uncited() -> None:
    """Reads the answer never leaned on are worth surfacing.

    Not a failure - an agent may read more than it needs - but a run
    where nothing read is cited is a different kind of answer from one
    where everything is.
    """
    report = citation_report("up [E1]", minted=("E1", "E2", "E3"))

    assert report.uncited_evidence == ("E2", "E3")
