"""Tests for the deterministic grader (issue #69)."""

from __future__ import annotations

from typing import Any

import pytest

from korvid.evals.grader import GradeResult, ToolRecord, citation_report, grade
from korvid.evals.scenario import Evidence, Scenario
from tests.evals.fixtures import EVAL_INTERACTION

_EVIDENCE = Evidence(
    tool="diagnose_pod",
    contains="exit=137",
    args={"pod": "checkout-1", "namespace": "shop"},
)


def _scenario(**overrides: Any) -> Scenario:
    fields: dict[str, Any] = {
        "id": "s1",
        "question": "q",
        "interaction": EVAL_INTERACTION,
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


def test_grade_evidence_group_is_satisfied_by_any_alternative() -> None:
    """A group of alternative locations accepts whichever path the model took.

    Sixteen shipped groups list two or more routes to the same fact. If the
    group were scored as an *all*-of, a model that read `endpoints` but not
    `endpointslices` would be reported as missing evidence it actually
    fetched - a false model regression published from a paid campaign.
    """
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
    assert result.missing_evidence == ()


def test_grade_evidence_requires_the_expected_arguments() -> None:
    """The same substring from a call against the *wrong* object is not
    credited as evidence."""
    records = [_record(arguments={"pod": "other-pod", "namespace": "shop"})]
    result = grade(_scenario(), "OOMKilled, exit 137.", records)
    assert not result.evidence_fetched
    assert result.missing_evidence == ((_EVIDENCE,),)


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


def test_grade_exculpatory_predicate_does_not_satisfy_a_required_claim() -> None:
    """ "The liveness probe is fine" must not satisfy a required "liveness
    probe" claim.

    A required group naming a *topic* rather than a *claim* is satisfied by
    saying the topic is not the problem - the exact opposite of what the
    group exists to require. The grader already refuses "not X"; declaring X
    healthy is the same assertion in positive grammar, and it appeared in
    four of the eight bundled journeys.
    """
    scenario = _scenario(
        must_mention=(("liveness probe",),),
        must_not_mention=(),
        expected_evidence=(),
    )
    exculpated = grade(scenario, "The liveness probe is fine; look elsewhere.", [])
    assert not exculpated.diagnosis_success
    assert exculpated.missing_mentions == (("liveness probe",),)
    asserted = grade(scenario, "The liveness probe failed 27 times.", [])
    assert asserted.diagnosis_success


def test_grade_exculpation_is_off_for_a_negative_control() -> None:
    """ "The service endpoints are healthy" is the *correct* answer when the
    scenario has no fault.

    Suppressing an entity match because a healthy predicate follows it is
    right in a fault scenario and exactly wrong in a negative control, where
    the all-clear is the claim being graded. Without this the bundled
    `healthy-service-endpoints` scenario rejects its own answer.
    """
    scenario = _scenario(
        root_cause="none",
        must_mention=(("healthy", "no issues"), ("endpoints", "endpoint")),
        must_not_mention=(),
        expected_evidence=(),
    )
    assert grade(scenario, "The service endpoints are healthy.", []).diagnosis_success


def test_grade_negation_stops_at_sentence_boundaries() -> None:
    """A negator in the previous sentence must not negate this one."""
    scenario = _scenario(
        must_mention=(("image pull",),),
        must_not_mention=(),
        expected_evidence=(),
    )
    result = grade(scenario, "It is not. The image pull failed.", [])
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


def test_grade_evidence_requires_every_expected_target_argument() -> None:
    records = [
        ToolRecord(
            name="list_resources",
            arguments={"kind": "pods", "namespace": "shop"},
            result="... exit=137 (OOMKilled) ...",
        )
    ]
    result = grade(_scenario(), "OOMKilled, exit 137.", records)
    assert not result.evidence_fetched


@pytest.mark.parametrize(
    "arguments",
    [
        {"pvc": "web", "name": "other", "namespace": "front"},
        {"name": "other", "pvc": "web", "namespace": "front"},
    ],
)
def test_grade_target_does_not_depend_on_argument_order(arguments: dict[str, str]) -> None:
    """A model's JSON key order cannot change the canonical target name."""
    evidence = Evidence(
        tool="get_resource",
        contains="phase: Pending",
        args={"kind": "persistentvolumeclaims", "name": "web", "namespace": "front"},
    )
    records = [
        _record(
            name="diagnose_pvc",
            result="outcome: findings\nphase: Pending",
            arguments=arguments,
        )
    ]
    assert grade(
        _scenario(expected_evidence=((evidence,),)),
        "OOMKilled, exit 137.",
        records,
    ).evidence_fetched


@pytest.mark.parametrize("dash", ["—", "\u2013", " - "])
def test_grade_treats_a_dash_as_a_clause_boundary(dash: str) -> None:
    """Negation before a dash must not suppress the causal claim after it."""
    answer = f"The container was not restarted by the operator{dash}it was OOMKilled with exit 137."
    result = grade(_scenario(), answer, [_record()])
    assert result.diagnosis_success


def test_grade_keeps_negation_within_an_unbroken_clause() -> None:
    result = grade(_scenario(), "The container was not OOMKilled with exit code 137.", [_record()])
    assert not result.diagnosis_success


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


def test_grade_credits_diagnose_service_for_endpoint_evidence() -> None:
    """`diagnose_service` reports on the Service *and* its endpoints, so a
    single implied kind is too narrow.

    The bundled Service scenarios express endpoint evidence as
    `get_resource(kind: endpoints)` / `kind: endpointslices`. Implying only
    `services` would leave those entries ungraded - the tool would be used
    correctly and still score no evidence.
    """
    for kind in ("endpoints", "endpointslices"):
        evidence = Evidence(
            tool="get_resource",
            contains="subsets: []",
            args={"kind": kind, "name": "web", "namespace": "front"},
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
        assert result.evidence_fetched, kind


def test_grade_rejects_a_diagnostic_call_against_a_different_kind() -> None:
    """Tool-implied kinds must not expand one diagnostic into another resource."""
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
    assert not grade(scenario, "OOMKilled, exit 137.", records).evidence_fetched


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


def test_an_evidence_gap_is_reported_when_reads_go_uncited() -> None:
    """Reads the answer never leaned on are worth surfacing.

    Not a failure - an agent may read more than it needs - but a run
    where nothing read is cited is a different kind of answer from one
    where everything is.
    """
    report = citation_report("up [E1]", minted=("E1", "E2", "E3"))

    assert report.uncited_evidence == ("E2", "E3")


def test_coverage_counts_markdown_list_items_separately() -> None:
    """Answers are rendered as Markdown, so a list is several claims.

    Splitting on sentence punctuation alone made `- a [E1]\\n- b` one unit
    and reported 100% coverage while half the claims were uncited
    (#192 review).
    """
    report = citation_report("- the pod is up [E1]\n- the node is fine", minted=("E1",))

    assert report.coverage == 0.5


# --- screen actions are not cluster evidence --------------------------------
#
# The eval bridge files each applied screen action into the same ordered
# record stream as the reads, so a journey can grade "and it put that on
# screen". That stream is also what evidence is graded against, and a
# screen action's *message* names the resource it moved to — so an action
# whose target collides with an expected read must never be credited as
# having fetched it. Only evidence that explicitly names the screen action
# may be satisfied by one.


def _screen_record(
    name: str = "select_resource",
    result: str = "selected worker-1",
    arguments: dict[str, Any] | None = None,
) -> ToolRecord:
    """One applied screen action, as the eval bridge reports it."""
    if arguments is None:
        arguments = {"kind": "Pod", "name": "worker-1", "namespace": "jobs", "uid": None}
    return ToolRecord(name=name, arguments=arguments, result=result, screen_action=True)


def _read_scenario() -> Scenario:
    return _scenario(
        must_mention=(("oomkilled",),),
        must_not_mention=(),
        expected_evidence=(
            (
                Evidence(
                    tool="get_resource",
                    contains="worker-1",
                    args={"kind": "pods", "name": "worker-1", "namespace": "jobs"},
                ),
            ),
        ),
    )


def test_a_screen_action_never_satisfies_cluster_evidence_it_does_not_name() -> None:
    """A colliding target *and* a colliding result must still not count.

    `select_resource(kind=Pod, name=worker-1, namespace=jobs)` targets the
    same object as the expected `get_resource` read, and its message
    ("selected worker-1") contains the expected substring. Nothing was
    fetched, so nothing may be graded as fetched.
    """
    result = grade(_read_scenario(), "worker-1 was OOMKilled.", [_screen_record()])

    assert result.evidence_fetched is False
    assert result.missing_evidence != ()


def test_evidence_that_names_the_screen_action_is_still_credited() -> None:
    """The TUI-following journeys grade exactly this, and must keep working."""
    scenario = _scenario(
        must_mention=(("oomkilled",),),
        must_not_mention=(),
        expected_evidence=(
            (
                Evidence(
                    tool="open_describe",
                    contains="opened describe",
                    args={"kind": "pods", "name": "worker-1", "namespace": "jobs"},
                ),
            ),
        ),
    )
    record = _screen_record(
        name="open_describe",
        result="opened describe for worker-1",
        arguments={"kind": "pods", "name": "worker-1", "namespace": "jobs"},
    )

    result = grade(scenario, "worker-1 was OOMKilled.", [record])

    assert result.evidence_fetched is True


def test_read_record_cannot_satisfy_ui_action_evidence() -> None:
    scenario = _scenario(
        must_mention=(("oomkilled",),),
        must_not_mention=(),
        expected_evidence=(
            (
                Evidence(
                    tool="open_describe",
                    contains="opened describe",
                    args={"kind": "pods", "name": "worker-1", "namespace": "jobs"},
                ),
            ),
        ),
    )
    read = ToolRecord(
        name="get_resource",
        arguments={"kind": "pods", "name": "worker-1", "namespace": "jobs"},
        result="opened describe for worker-1",
    )

    assert not grade(scenario, "worker-1 was OOMKilled.", [read]).evidence_fetched
