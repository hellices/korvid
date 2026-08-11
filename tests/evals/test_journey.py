"""Conversation-journey schema tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.grader import grade
from korvid.evals.journey import (
    ConversationJourney,
    JourneyTurn,
    bundled_journeys_dir,
    load_journey,
    load_journeys,
)
from korvid.evals.scenario import Scenario
from korvid.tools.executor import UI_TOOL_NAMES, ToolExecutor


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_load_journey_preserves_ordered_turns_and_cluster(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "triage.yaml",
        """
id: triage-and-correct
root_cause: image_pull_auth
turns:
  - user: What needs attention in namespace shop?
    screen: "resource view: pods, namespace shop"
    grading:
      must_mention: [[checkout, payments]]
      must_not_mention: [[healthy]]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods, namespace: shop}
          contains: payments
      max_tool_calls: 3
  - user: Focus on payments, not checkout. What is the exact cause?
    screen: "resource view: pods, namespace shop"
    grading:
      must_mention: [[payments], [unauthorized, authentication]]
      must_not_mention: [[oomkilled]]
      expected_evidence:
        - tool: diagnose_pod
          args: {pod: payments-1, namespace: shop}
          contains: unauthorized
    forbidden_targets:
      - {pod: checkout-1, namespace: shop}
cluster:
  objects:
    - kind: Pod
      apiVersion: v1
      metadata: {name: payments-1, namespace: shop, uid: pod-payments}
      status: {phase: Pending}
  events: []
  logs: {}
""",
    )

    journey = load_journey(path)

    assert journey.id == "triage-and-correct"
    assert [turn.user for turn in journey.turns] == [
        "What needs attention in namespace shop?",
        "Focus on payments, not checkout. What is the exact cause?",
    ]
    assert journey.turns[0].max_tool_calls == 3
    assert journey.turns[1].forbidden_targets == ({"pod": "checkout-1", "namespace": "shop"},)
    assert journey.objects[0]["metadata"]["name"] == "payments-1"


def test_load_journey_rejects_unknown_turn_keys(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "bad.yaml",
        """
id: bad
root_cause: none
turns:
  - user: hello
    screen: pods
    surprise: true
    grading:
      must_mention: [healthy]
      must_not_mention: [oomkilled]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: healthy
  - user: stop?
    screen: pods
    grading:
      must_mention: [healthy]
      must_not_mention: [oomkilled]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: healthy
cluster: {objects: [], events: [], logs: {}}
""",
    )
    with pytest.raises(ValueError, match="unknown keys"):
        load_journey(path)


def test_load_journey_rejects_zero_tool_call_budget(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "zero-budget.yaml",
        """
id: zero-budget
root_cause: none
turns:
  - user: inspect
    screen: pods
    grading:
      must_mention: [healthy]
      must_not_mention: [broken]
      max_tool_calls: 0
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: pod
  - user: stop
    screen: pods
    grading:
      must_mention: [stop]
      must_not_mention: [broken]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: pod
cluster: {objects: [], events: [], logs: {}}
""",
    )
    with pytest.raises(ValueError, match="positive integer"):
        load_journey(path)


def test_load_journey_rejects_empty_forbidden_target(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "empty-target.yaml",
        """
id: empty-target
root_cause: none
turns:
  - user: inspect
    screen: pods
    grading:
      must_mention: [healthy]
      must_not_mention: [broken]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: pod
    forbidden_targets: [{}]
  - user: stop
    screen: pods
    grading:
      must_mention: [stop]
      must_not_mention: [broken]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: pod
cluster: {objects: [], events: [], logs: {}}
""",
    )
    with pytest.raises(ValueError, match="non-empty"):
        load_journey(path)


def test_load_journeys_rejects_duplicate_ids(tmp_path: Path) -> None:
    text = """
id: same
root_cause: none
turns:
  - user: hello
    screen: pods
    grading:
      must_mention: [healthy]
      must_not_mention: [oomkilled]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: healthy
  - user: stop?
    screen: pods
    grading:
      must_mention: [healthy]
      must_not_mention: [oomkilled]
      expected_evidence:
        - tool: list_resources
          args: {kind: pods}
          contains: healthy
cluster: {objects: [], events: [], logs: {}}
"""
    _write(tmp_path / "a.yaml", text)
    _write(tmp_path / "b.yaml", text)
    with pytest.raises(ValueError, match="duplicate journey id"):
        load_journeys(tmp_path)


def test_bundled_journey_pack_covers_the_planned_conversational_behaviors() -> None:
    journeys = load_journeys(bundled_journeys_dir())
    assert {journey.id for journey in journeys} == {
        "compare-namespaces",
        "healthy-stop",
        "logs-to-events",
        "namespace-triage",
        "rbac-evidence-gap",
        "rollout-owner-chain",
        "triage-and-correct",
        "tui-follow",
    }
    assert all(len(journey.turns) >= 2 for journey in journeys)
    # #176 sets eight as the floor for a publishable journey score; the
    # pack shipping fewer is the condition that kept that row unpublishable.
    assert len(journeys) >= 8


@pytest.mark.parametrize("journey", load_journeys(bundled_journeys_dir()), ids=lambda j: j.id)
async def test_bundled_journey_evidence_is_reachable_through_the_real_tools(
    journey: ConversationJourney,
) -> None:
    """Every declared evidence item must be fetchable from the fixture.

    The scenario pack has had this guard since #69; journeys did not, so a
    fixture that drifted from its assertions would only surface as an
    unexplained model failure during a paid live run.
    """
    scenario = Scenario(
        id=journey.id,
        question="q",
        screen="s",
        root_cause=journey.root_cause,
        must_mention=(),
        must_not_mention=(),
        objects=journey.objects,
        events=journey.events,
        logs=journey.logs,
        # The withheld reads belong here too, or the guard would certify a
        # route the journey itself denies at runtime and the drift it exists
        # to catch would reappear as an unexplained model failure.
        forbidden=journey.forbidden,
    )
    executor = ToolExecutor(FakeKubeClient(scenario), builtin_aliases())
    for index, turn in enumerate(journey.turns, start=1):
        assert turn.expected_evidence, f"{journey.id} turn {index} declares no evidence"
        for group in turn.expected_evidence:
            # UI tools need a live bridge this fixture-only executor does
            # not have; their reachability is a runner concern, not a
            # fixture one.
            cluster_reads = [e for e in group if e.tool not in UI_TOOL_NAMES]
            if not cluster_reads:
                continue
            # Every alternative, not merely one per group. The grader
            # documents each listed tool as "one known-good route, verified
            # reachable by the fixture-integrity test", and an any-of check
            # does not verify that: a route that silently stopped matching
            # would keep passing behind a working sibling, and the pack
            # would then advertise a path no model can take. Caught a real
            # one while authoring `rbac-evidence-gap`.
            for evidence in cluster_reads:
                result = await executor.execute(evidence.tool, dict(evidence.args))
                assert not result.startswith("ERROR:"), (
                    f"{journey.id} turn {index}: {evidence.tool} failed\n{result[:200]}"
                )
                assert evidence.contains in result, (
                    f"{journey.id} turn {index}: {evidence.tool} does not contain "
                    f"{evidence.contains!r}\n{result[:200]}"
                )


def test_triage_requires_an_explicit_priority_not_just_both_names() -> None:
    journey = next(
        item for item in load_journeys(bundled_journeys_dir()) if item.id == "triage-and-correct"
    )
    turn = journey.turns[0]
    scenario = Scenario(
        id="priority",
        question=turn.user,
        screen=turn.screen,
        root_cause=journey.root_cause,
        must_mention=turn.must_mention,
        must_not_mention=turn.must_not_mention,
        expected_evidence=turn.expected_evidence,
    )
    result = grade(
        scenario,
        "Checkout and payments both need attention.",
        [],
    )
    assert result.diagnosis_success is False


#: Per-turn phrasings the rollout journey must accept and reject. Keyword
#: lists are only as good as the phrasings they survive, so they are pinned
#: rather than eyeballed once (the same lesson as the scenario pack).
_ROLLOUT_CASES: tuple[tuple[int, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        0,
        (
            "The rollout is stalled: the new ReplicaSet api-7b9d has a pod stuck"
            " in ImagePullBackOff because the image tag v27 does not exist.",
            "api-7b9d cannot pull the image edge/api:v27 \u2014 the tag looks like"
            " a typo, so the rollout never completes.",
            "The new pod fails to pull edge/api:v27 (manifest not found)."
            " api-7b9d is the wrong tag rollout.",
        ),
        (
            # Names the ReplicaSet and the symptom but never the bad tag —
            # incomplete, and it passed before the groups were split.
            "api-7b9d is in ImagePullBackOff.",
            "The new ReplicaSet api-7b9d cannot pull its image.",
            # Names the symptom in words that once appeared in both the
            # symptom and cause groups, satisfying each without ever
            # identifying which tag is wrong.
            "The pod in api-7b9d shows manifest not found.",
            "The pod was OOMKilled and restarted.",
            "The readiness probe is failing on the new pod.",
            "The namespace is out of CPU quota.",
        ),
    ),
    (
        1,
        (
            "It belongs to ReplicaSet api-7b9d. The previous ReplicaSet api-5c2f"
            " still has 2 ready pods, so traffic is still served.",
            "That pod is owned by api-7b9d; api-5c2f is the old ReplicaSet and is"
            " still running with 2 replicas.",
        ),
        (
            "This is a total outage; all pods are down.",
            # Names the owning ReplicaSet only; says nothing about the old one.
            "It belongs to api-7b9d.",
        ),
    ),
)


def _turn_scenario(turn: JourneyTurn) -> Scenario:
    return Scenario(
        id="x",
        question="q",
        screen="s",
        root_cause="r",
        must_mention=turn.must_mention,
        must_not_mention=turn.must_not_mention,
    )


@pytest.mark.parametrize(("index", "correct", "wrong"), _ROLLOUT_CASES)
def test_rollout_journey_keywords_discriminate(
    index: int, correct: tuple[str, ...], wrong: tuple[str, ...]
) -> None:
    journey = next(
        item for item in load_journeys(bundled_journeys_dir()) if item.id == "rollout-owner-chain"
    )
    scenario = _turn_scenario(journey.turns[index])
    for answer in correct:
        assert grade(scenario, answer, []).diagnosis_success, (
            f"turn {index + 1}: a correct answer was graded wrong\n  {answer}"
        )
    for answer in wrong:
        assert not grade(scenario, answer, []).diagnosis_success, (
            f"turn {index + 1}: a wrong answer was graded correct\n  {answer}"
        )


#: A minimal journey whose fixture withholds one read. Shared so the
#: acceptance and rejection tests cannot drift apart.
_JOURNEY_WITH_FORBIDDEN = """
id: j
root_cause: none
turns:
  - user: u
    screen: s
    grading:
      must_mention: [[a]]
      expected_evidence:
        - tool: get_events
          args: {kind: pods, name: p, namespace: n}
          contains: x
  - user: u2
    screen: s
    grading:
      must_mention: [[a]]
      expected_evidence:
        - tool: get_events
          args: {kind: pods, name: p, namespace: n}
          contains: x
cluster:
  objects: []
  events: []
  logs: {}
  forbidden:
    - {kind: pods, namespace: n, subresource: log}
"""


def test_a_journey_can_withhold_a_read_the_way_rbac_does(tmp_path: Path) -> None:
    """`cluster.forbidden` reaches the fixture, so a journey can measure
    what the model does when evidence is denied rather than absent."""
    _write(tmp_path / "j.yaml", _JOURNEY_WITH_FORBIDDEN)
    journey = load_journeys(tmp_path)[0]
    assert journey.forbidden == ({"kind": "pods", "namespace": "n", "subresource": "log"},)


#: Per-turn phrasings the journeys added for #176 must accept and reject.
#: Each `wrong` entry is the specific failure its journey exists to catch,
#: not a generic bad answer - a keyword list that cannot separate those is
#: worse than no journey, because it publishes a score for a behavior it
#: never measured.
_JOURNEY_CASES: tuple[tuple[str, int, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "rbac-evidence-gap",
        0,
        (
            "ledger-1 is in CrashLoopBackOff. I could not read the container log -"
            " access is forbidden - so I used the events and pod status instead,"
            " and I cannot determine why the process exits.",
            "The pod keeps restarting (back-off). Reading its log is denied by RBAC,"
            " so the application-level reason is not visible to me.",
        ),
        (
            # Reports the crash loop and silently omits that the evidence
            # was withheld. This is the failure the journey exists for.
            "ledger-1 is in CrashLoopBackOff and keeps restarting.",
            # Carries both required claims - the crash loop and the denial -
            # so the invented cause is the only thing left to reject it. An
            # earlier draft omitted the crash loop and would have kept
            # passing with the cause prohibition deleted.
            "ledger-1 is in CrashLoopBackOff and reading its log is forbidden;"
            " it was OOMKilled and ran out of memory.",
            # The sharpest form: the exact cause the withheld log holds,
            # which no allowed read can reach.
            "ledger-1 is in CrashLoopBackOff and log access is forbidden;"
            " it could not parse the rate table.",
            # A guess no prohibition names. Enumerating causes cannot catch
            # this class, which is why the turn requires the ceiling to be
            # stated rather than the causes to be avoided.
            "ledger-1 is in CrashLoopBackOff; log access is forbidden, but it"
            " is probably a database connection failure.",
        ),
    ),
    (
        "namespace-triage",
        0,
        (
            "invoicer and shipper are both abnormal; invoicer first, it is fully"
            " down while shipper still serves.",
            "Two workloads look wrong: invoicer and shipper. Prioritize invoicer.",
        ),
        (
            # Both names, no ordering: triage without the triage.
            "invoicer and shipper both look abnormal.",
            # Concedes the right order and then inverts it.
            "Invoicer first seems plausible, but prioritize shipper; both"
            " invoicer and shipper are abnormal.",
            "The orders namespace is healthy.",
        ),
    ),
    (
        "compare-namespaces",
        0,
        (
            "prod first: its checkout Service has no endpoints, which is an outage."
            " staging only shows an old restart.",
            "Between prod and staging, prod needs attention: its checkout Service"
            " has no endpoints, so it is not serving. staging's warnings are"
            " historical.",
        ),
        (
            # Severity by event count, which is the trap the fixture sets.
            "staging first, it has more warning events than prod.",
            # The right pick for the wrong reason, and the reason inverts the
            # fixture: staging holds every warning event. Naming prod alone
            # used to be enough.
            "prod first, because prod has more warning events than staging.",
            # The same inversion with an object name standing in for a
            # reason - "endpoints" alone used to satisfy the reason group.
            "Prod needs attention first because it has more warnings than"
            " staging; I checked its endpoints.",
            "prod and staging are both equally urgent.",
        ),
    ),
    (
        "compare-namespaces",
        1,
        (
            "The checkout Service selects app: checkout-v2 but the pod carries"
            " app: checkout - a label mismatch. staging recovered and is now ready.",
        ),
        (
            # The symptom offered as the cause the turn asked for.
            "Prod has no endpoints; staging recovered.",
        ),
    ),
    (
        "rbac-evidence-gap",
        1,
        ("It exited with exit code 1; I would need permission to read the log to go further.",),
        (
            # Never reports the one hard fact the allowed reads supply.
            "There is an error; I need permission to read the log.",
            # Claims the access the fixture withholds.
            "exit code 1; I already have permission and log access.",
        ),
    ),
    (
        "tui-follow",
        2,
        (
            "Opened the log pane for web-1; the container never started, so it is empty.",
            "Here is the log view for web-1 - no output, the image never pulled.",
        ),
        (
            # Narrates a log it cannot have read.
            "The log shows the application failing to connect to its database.",
        ),
    ),
    # The four journeys that predate the pinning rule. Every one of them had
    # a required group naming a topic instead of a claim.
    (
        "healthy-stop",
        0,
        ("Both catalog pods are running normally, with no restarts.",),
        (
            "catalog-1 is in CrashLoopBackOff.",
            # Describes the namespace without judging its health, which is
            # the only thing the turn asks for.
            "The namespace contains two pods spread over two nodes.",
        ),
    ),
    (
        "healthy-stop",
        1,
        ("No further investigation is needed: catalog-1 has no warning events.",),
        (
            "catalog-1 is broken and needs work.",
            "Keep digging into catalog-1 before deciding anything.",
        ),
    ),
    (
        "logs-to-events",
        0,
        ("gateway-1 restarts because its liveness probe failed with context deadline exceeded.",),
        (
            # Names the probe in order to rule it out.
            "The liveness probe is fine; gateway-1 restarts for another reason.",
            "gateway-1 was OOMKilled.",
        ),
    ),
    (
        "logs-to-events",
        1,
        (
            "The kubelet is killing and restarting gateway-1 because the liveness probe failed 27 times.",
        ),
        (
            "The liveness probe is fine; the kubelet restarts the container for another reason.",
            "The application log is normal, so nothing is happening.",
        ),
    ),
    (
        "namespace-triage",
        1,
        (
            "invoicer-1 crashloops because DATABASE_DSN is missing from its environment,"
            " and shipper is degraded but still serving with 2 of 3 replicas.",
        ),
        (
            # Names the configuration without saying anything is wrong with it.
            "invoicer-1 has an environment variable and shipper is degraded.",
            "invoicer-1 is missing its DSN and shipper is down.",
        ),
    ),
    (
        "rollout-owner-chain",
        0,
        ("The new ReplicaSet api-7b9d cannot pull its image because tag v27 does not exist.",),
        (
            # The symptom and the ReplicaSet, but never the bad tag.
            "api-7b9d is in ImagePullBackOff.",
            "The image pull is fine for api-7b9d; tag v27 is correct.",
        ),
    ),
    (
        "rollout-owner-chain",
        1,
        (
            "The pod belongs to api-7b9d; the previous ReplicaSet api-5c2f is still"
            " serving traffic with 2 ready replicas.",
        ),
        (
            # Both names and "still running", attached to the wrong subject.
            "api-7b9d-x1 is still running, while api-5c2f has already been scaled away.",
            "Every replica is gone - this is a total outage.",
        ),
    ),
    (
        "triage-and-correct",
        0,
        (
            "checkout-1 and payments-1 both need attention; inspect checkout first, it is crashlooping.",
        ),
        (
            "Both checkout and payments need attention; rather than checkout first, prioritize payments.",
            "The shop namespace has no issues.",
        ),
    ),
    (
        "triage-and-correct",
        1,
        (
            "payments-1 cannot pull its image: the registry rejected the credentials as unauthorized.",
        ),
        (
            # Right words, wrong subject - the turn says focus on payments.
            "payments-1 is failing; checkout-1 is the one with registry credentials problems.",
            "payments-1 was OOMKilled.",
        ),
    ),
    (
        "triage-and-correct",
        2,
        ("The payments-1 describe pane is on screen; next, fix the registry credentials secret.",),
        (
            "payments-1 needs more memory; rotate the secret later.",
            "Increase the memory limit for payments-1.",
        ),
    ),
    (
        "tui-follow",
        0,
        ("web-1 cannot pull its image because tag v99 does not exist in the registry.",),
        (
            "The image pull is fine; web-1 uses tag v99 and works.",
            "web-1 is failing its readiness probe.",
        ),
    ),
    (
        "tui-follow",
        1,
        # The weight of this turn is the evidence assertion - the bridge must
        # acknowledge the pane. The text only has to name the pod and not
        # claim the screen is unreachable.
        ("The web-1 details are on screen now.",),
        ("I cannot open that pane for you.",),
    ),
)


@pytest.mark.parametrize(("journey_id", "index", "correct", "wrong"), _JOURNEY_CASES)
def test_new_journey_keywords_discriminate(
    journey_id: str, index: int, correct: tuple[str, ...], wrong: tuple[str, ...]
) -> None:
    journey = next(item for item in load_journeys(bundled_journeys_dir()) if item.id == journey_id)
    scenario = _turn_scenario(journey.turns[index])
    for answer in correct:
        assert grade(scenario, answer, []).diagnosis_success, (
            f"{journey_id} turn {index + 1}: a correct answer was graded wrong\n  {answer}"
        )
    for answer in wrong:
        assert not grade(scenario, answer, []).diagnosis_success, (
            f"{journey_id} turn {index + 1}: a wrong answer was graded correct\n  {answer}"
        )


def test_every_journey_turn_pins_an_accepting_and_a_rejecting_answer() -> None:
    """A turn with no pinned pair is a grading rule nobody has ever exercised.

    Every finding in the #249 review was a keyword group that graded something
    other than what its comment claimed - a positive phrase inside a group
    demanding a denial, a prohibition that rejected the truth, a "reason" group
    satisfied by a bare object name. None was visible by reading the YAML, and
    each became obvious the moment one specific phrasing was run through the
    grader. This test makes that exercise mandatory rather than optional.
    """
    # An entry with an empty side pins nothing, so presence alone is not the
    # rule: a turn counts as covered only when both directions are exercised.
    pinned = {
        (journey_id, index)
        for journey_id, index, correct, wrong in _JOURNEY_CASES
        if correct and wrong
    }
    missing = [
        f"{journey.id} turn {index + 1}"
        for journey in load_journeys(bundled_journeys_dir())
        for index in range(len(journey.turns))
        if (journey.id, index) not in pinned
    ]
    assert not missing, (
        "these turns have no pinned accepting/rejecting phrasings, so their"
        " grading rules are unverified:\n  " + "\n  ".join(missing)
    )


def test_an_unsupported_subresource_is_rejected_at_load(tmp_path: Path) -> None:
    """`log` is the only subresource the matcher knows.

    Accepting `logs` would load cleanly and silently deny nothing, so the
    journey would report a score for an evidence gap it never created -
    the failure this parser's strictness exists to prevent.
    """
    _write(
        tmp_path / "j.yaml",
        _JOURNEY_WITH_FORBIDDEN.replace("subresource: log", "subresource: logs"),
    )
    with pytest.raises(ValueError, match="subresource"):
        load_journeys(tmp_path)


@pytest.mark.parametrize(
    ("replacement", "match"),
    [
        # `_deny` compares the plural resource name, so a singular kind
        # loads and denies nothing.
        ("{kind: pod, namespace: n, subresource: log}", "kind"),
        ("{kind: pods, namespace: '', subresource: log}", "blank"),
        ("{kind: pods, namespace: n, name: '', subresource: log}", "blank"),
        # `log` only ever reaches the matcher for a pod read, so pairing it
        # with any other kind is a rule that cannot fire.
        ("{kind: secrets, namespace: n, subresource: log}", "subresource"),
    ],
)
def test_a_selector_the_matcher_cannot_honour_is_rejected(
    tmp_path: Path, replacement: str, match: str
) -> None:
    """A rule that loads but matches nothing is the worst outcome here: the
    journey reports a score for an evidence gap it never created, and the
    run looks like a model that handled the gap well."""
    _write(
        tmp_path / "j.yaml",
        _JOURNEY_WITH_FORBIDDEN.replace(
            "{kind: pods, namespace: n, subresource: log}", replacement
        ),
    )
    with pytest.raises(ValueError, match=match):
        load_journeys(tmp_path)
