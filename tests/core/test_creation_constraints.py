"""Tests for reading a workload's creation failures and namespace policies (#411)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core.api_versions import DiscoverySnapshot, observe_discovery
from korvid.core.capability import Basis, EvidenceState, Observation, RunToken
from korvid.core.creation_constraints import (
    CreationTarget,
    declarations,
    explain_creation,
)
from korvid.core.creation_failures import RejectionKind, Subject
from korvid.core.evidence_reads import LIST_PAGE_SIZE, MAX_BYTES
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
)
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.evidence_requests import EvidencePage, EvidenceReader, EvidenceTarget

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
TOKEN = RunToken(4, 2, 7)
TARGET = CreationTarget("Deployment", "shop", "api", "uid-api")
EXCEEDED = (
    'Error creating: pods "api-7d9f-x" is forbidden: exceeded quota: compute, '
    "requested: pods=1, used: pods=0, limited: pods=0"
)
_SERVED = {
    ("", "v1"): ("resourcequotas", "limitranges", "events"),
    ("apps", "v1"): ("deployments", "replicasets", "statefulsets", "daemonsets"),
    ("batch", "v1"): ("jobs", "cronjobs"),
    ("events.k8s.io", "v1"): ("events",),
}


def _clock() -> datetime:
    return NOW


def _snapshot(served: dict[tuple[str, str], tuple[str, ...]] = _SERVED) -> DiscoverySnapshot:
    outcomes = tuple(
        GroupVersionOutcome(
            group, version, tuple(ServedResource(r, frozenset({"get", "list"})) for r in resources)
        )
        for (group, version), resources in served.items()
    )
    groups = tuple((g, v) for g, v in served if g)
    raw = RawDiscovery(
        1, NOW, 0.0, RootOutcome("/api", (("", "v1"),)), RootOutcome("/apis", groups), outcomes
    )
    return observe_discovery(raw)


def _status(
    code: int, reason: str, *, name: str = "", kind: str = "", group: str = ""
) -> ApiStatusError:
    details = {"name": name, "group": group, "kind": kind}
    return ApiStatusError(
        code, reason, json.dumps({"kind": "Status", "reason": reason, "details": details})
    )


def _meta(name: str, uid: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "namespace": "shop", "uid": uid, **extra}


DEPLOYMENT = {
    "metadata": _meta("api", "uid-api"),
    "spec": {
        "replicas": 3,
        "template": {
            "spec": {
                "containers": [{"name": "app", "env": [{"name": "T", "value": "s3cr3t-literal"}]}]
            }
        },
    },
    "status": {},
}
REPLICA_SET = {
    "metadata": _meta(
        "api-7d9f", "uid-rs", ownerReferences=[{"uid": "uid-api", "controller": True}]
    ),
    "spec": {"template": {"spec": {"containers": [{"env": [{"value": "s3cr3t-literal"}]}]}}},
}
FAILED_CREATE = {
    "metadata": _meta("e1", "uid-e1"),
    "reason": "FailedCreate",
    "regarding": {"kind": "ReplicaSet", "name": "api-7d9f", "uid": "uid-rs"},
    "note": EXCEEDED,
    "series": {"count": 12, "lastObservedTime": "2026-10-30T09:58:00Z"},
}
QUOTA = {
    "metadata": _meta("compute", "uid-q"),
    "spec": {"hard": {"pods": "0"}},
    "status": {"hard": {"pods": "0"}, "used": {"pods": "0"}},
}
LIMIT_RANGE = {
    "metadata": _meta("defaults", "uid-lr"),
    "spec": {"limits": [{"type": "Container", "max": {"cpu": "1"}}]},
}


class FakeReader(EvidenceReader):
    """Answers each resource from its own script, since the reads run together."""

    def __init__(self, **answers: object) -> None:
        self.answers: dict[str, object] = {
            "deployments": DEPLOYMENT,
            "replicasets": (REPLICA_SET,),
            "events": (FAILED_CREATE,),
            "resourcequotas": (QUOTA,),
            "limitranges": (LIMIT_RANGE,),
            **answers,
        }
        self.gets: list[tuple[EvidenceTarget, str]] = []
        self.lists: list[tuple[EvidenceTarget, int, int]] = []

    def _answer(self, target: EvidenceTarget) -> object:
        answer = self.answers.get(
            f"{target.group}/{target.resource}", self.answers.get(target.resource)
        )
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.lists.append((target, limit, max_bytes))
        answer = self._answer(target)
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, EvidencePage):
            return answer
        assert isinstance(answer, tuple)
        return EvidencePage(answer)

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        self.gets.append((target, name))
        answer = self._answer(target)
        assert isinstance(answer, dict)
        return answer


def _truncated(item: dict[str, Any]) -> list[EvidencePage]:
    """Two pages, each with more pending: the default page cap truncates the read."""
    return [EvidencePage((item,), continuation="a"), EvidencePage((), continuation="b")]


def _state(observation: Observation | None) -> tuple[EvidenceState, Basis] | None:
    return None if observation is None else (observation.state, observation.basis)


async def _explain(
    reader: FakeReader, target: CreationTarget = TARGET, snapshot: DiscoverySnapshot | None = None
) -> Any:
    return await explain_creation(
        reader, snapshot or _snapshot(), target, token=TOKEN, clock=_clock
    )


async def test_a_deployments_failures_come_from_its_replica_sets_next_to_the_policies() -> None:
    reader = FakeReader()

    report = await _explain(reader)

    assert report.workload.subject == Subject("Deployment", "api", "uid-api")
    assert [c.subject.name for c in report.children] == ["api-7d9f"]
    (failure,) = report.failures
    assert (failure.subject.name, failure.count) == ("api-7d9f", 12)
    assert failure.rejection.kinds == (RejectionKind.QUOTA_EXCEEDED,)
    assert [q.name for q in report.quotas] == ["compute"]
    assert [r.name for r in report.limit_ranges] == ["defaults"]
    assert report.baseline.state is EvidenceState.AVAILABLE
    assert report.quotas_conclusive(NOW)
    assert report.limit_ranges_conclusive(NOW)
    assert "s3cr3t-literal" not in repr(report)


async def test_the_reads_and_their_budgets() -> None:
    reader = FakeReader()

    await _explain(reader)

    assert reader.gets == [(EvidenceTarget("apps", "v1", "deployments", "shop"), "api")]
    lists = {(t.group, t.resource): (limit, max_bytes) for t, limit, max_bytes in reader.lists}
    assert lists[("apps", "replicasets")] == (LIST_PAGE_SIZE, MAX_BYTES)
    assert lists[("events.k8s.io", "events")] == (LIST_PAGE_SIZE, MAX_BYTES)
    assert set(lists) == {
        ("apps", "replicasets"),
        ("events.k8s.io", "events"),
        ("", "resourcequotas"),
        ("", "limitranges"),
    }


async def test_a_cronjob_reads_its_jobs_and_a_statefulset_no_children() -> None:
    cron = {"metadata": _meta("nightly", "uid-cron"), "spec": {}, "status": {}}
    job = {
        "metadata": _meta(
            "nightly-1", "uid-job", ownerReferences=[{"uid": "uid-cron", "controller": True}]
        )
    }
    reader = FakeReader(cronjobs=cron, jobs=(job,), events=())

    report = await _explain(reader, CreationTarget("CronJob", "shop", "nightly", "uid-cron"))

    assert [c.subject for c in report.children] == [Subject("Job", "nightly-1", "uid-job")]
    stateful = FakeReader(statefulsets={"metadata": _meta("db", "uid-db"), "spec": {}}, events=())
    db = await _explain(stateful, CreationTarget("StatefulSet", "shop", "db", "uid-db"))
    assert db.children == ()
    assert db.children_observation is None
    assert all(t.resource != "replicasets" for t, _, _ in stateful.lists)


async def test_a_recreated_workload_matches_events_against_its_new_uid() -> None:
    recreated = {**DEPLOYMENT, "metadata": _meta("api", "uid-new")}
    old_event = {
        **FAILED_CREATE,
        "regarding": {"kind": "Deployment", "name": "api", "uid": "uid-api"},
    }
    new_event = {
        **FAILED_CREATE,
        "regarding": {"kind": "Deployment", "name": "api", "uid": "uid-new"},
    }
    reader = FakeReader(deployments=recreated, replicasets=(), events=(old_event, new_event))

    report = await _explain(reader)

    assert report.recreated
    assert [f.subject.uid for f in report.failures] == ["uid-new"]
    assert report.earlier_failures == 1


async def test_a_denied_workload_read_still_matches_events_by_the_selected_uid() -> None:
    event = {**FAILED_CREATE, "regarding": {"kind": "Deployment", "name": "api", "uid": "uid-api"}}
    reader = FakeReader(deployments=_status(403, "Forbidden"), events=(event,))

    report = await _explain(reader)

    assert report.workload is None
    assert _state(report.workload_observation) == (EvidenceState.DENIED, Basis.FORBIDDEN)
    assert [f.subject for f in report.failures] == [Subject("Deployment", "api", "uid-api")]
    assert [c.subject.name for c in report.children] == ["api-7d9f"]
    assert report.baseline.state is not EvidenceState.AVAILABLE


async def test_an_absent_workload_has_no_failures_or_children() -> None:
    reader = FakeReader(
        deployments=_status(404, "NotFound", name="api", kind="deployments", group="apps")
    )

    report = await _explain(reader, CreationTarget("Deployment", "shop", "api"))

    assert _state(report.workload_observation) == (EvidenceState.AVAILABLE, Basis.OBJECT_ABSENT)
    assert (report.workload, report.children, report.failures) == (None, (), ())


async def test_a_replica_failure_condition_is_a_failure_next_to_the_events() -> None:
    condition = {
        "type": "ReplicaFailure",
        "status": "True",
        "reason": "FailedCreate",
        "message": EXCEEDED,
        "lastUpdateTime": "2026-10-30T09:59:00Z",
    }
    deployment = {**DEPLOYMENT, "status": {"conditions": [condition]}}

    report = await _explain(FakeReader(deployments=deployment))

    assert [(f.subject.kind, f.source.value) for f in report.failures] == [
        ("Deployment", "condition"),
        ("ReplicaSet", "event"),
    ]


async def test_a_stale_quota_downgrades_an_available_quota_list() -> None:
    stale = {**QUOTA, "status": {"hard": {"pods": "5"}, "used": {"pods": "0"}}}

    report = await _explain(FakeReader(resourcequotas=(stale,)))

    assert _state(report.quotas_observation) == (EvidenceState.PARTIAL, Basis.STALE_STATUS)
    assert "compute" in report.quotas_observation.detail
    assert not report.quotas_conclusive(NOW)


async def test_a_stale_quota_in_a_truncated_list_keeps_its_basis() -> None:
    stale = {**QUOTA, "status": {}}
    reader = FakeReader(resourcequotas=_truncated(stale))

    report = await explain_creation(reader, _snapshot(), TARGET, token=TOKEN, clock=_clock)

    assert _state(report.quotas_observation) == (EvidenceState.PARTIAL, Basis.TRUNCATED)
    assert report.quotas[0].stale


async def test_an_empty_conclusive_quota_list_rules_out_quotas_but_a_denied_one_does_not() -> None:
    empty = await _explain(FakeReader(resourcequotas=()))
    denied = await _explain(FakeReader(resourcequotas=_status(403, "Forbidden")))

    assert (empty.quotas, empty.quotas_conclusive(NOW)) == ((), True)
    assert (denied.quotas, denied.quotas_conclusive(NOW)) == ((), False)


async def test_core_events_are_read_when_events_k8s_io_is_not_served() -> None:
    served = {key: value for key, value in _SERVED.items() if key[0] != "events.k8s.io"}
    core_event = {
        "metadata": _meta("e", "uid-e"),
        "reason": "FailedCreate",
        "involvedObject": {"kind": "ReplicaSet", "name": "api-7d9f", "uid": "uid-rs"},
        "message": EXCEEDED,
        "count": 2,
    }
    reader = FakeReader(events=(core_event,))

    report = await _explain(reader, snapshot=_snapshot(served))

    assert [f.count for f in report.failures] == [2]
    assert any(t.group == "" and t.resource == "events" for t, _, _ in reader.lists)


async def test_every_observation_carries_the_run_token() -> None:
    report = await _explain(FakeReader())

    observations = [
        report.workload_observation,
        report.children_observation,
        report.events_observation,
        report.quotas_observation,
        report.limit_ranges_observation,
    ]
    assert {o.token for o in observations if o is not None} == {TOKEN}


def test_declarations_name_each_source_and_its_role() -> None:
    keys = {(d.key, d.required, d.role.value) for d in declarations(TARGET)}

    assert keys == {
        ("workload", True, "confirms"),
        ("children", True, "confirms"),
        ("events", True, "confirms"),
        ("quotas", True, "rules_out"),
        ("limit-ranges", True, "rules_out"),
    }
    assert "children" not in {d.key for d in declarations(CreationTarget("Job", "shop", "j"))}


@pytest.mark.parametrize(
    ("kind", "namespace", "name"),
    [("Pod", "shop", "x"), ("Deployment", "", "x"), ("Deployment", "shop", "")],
)
def test_a_target_is_a_named_workload_in_a_namespace(kind: str, namespace: str, name: str) -> None:
    with pytest.raises(ValueError, match="workload"):
        CreationTarget(kind, namespace, name)


async def test_an_absent_workloads_old_events_are_not_its_failures() -> None:
    event = {**FAILED_CREATE, "regarding": {"kind": "Deployment", "name": "api", "uid": "uid-api"}}
    absent = _status(404, "NotFound", name="api", kind="deployments", group="apps")

    report = await _explain(FakeReader(deployments=absent, events=(event,)))

    assert report.failures == ()


async def test_a_replica_sets_own_failure_condition_is_reported() -> None:
    condition = {
        "type": "ReplicaFailure",
        "status": "True",
        "reason": "FailedCreate",
        "message": EXCEEDED,
    }
    replica_set = {**REPLICA_SET, "status": {"conditions": [condition]}}

    report = await _explain(FakeReader(replicasets=(replica_set,), events=()))

    assert [(f.subject.kind, f.source.value) for f in report.failures] == [
        ("ReplicaSet", "condition")
    ]


async def test_an_unserved_workload_resource_is_reported_with_the_token_and_not_read() -> None:
    served = {key: value for key, value in _SERVED.items() if key != ("apps", "v1")}
    reader = FakeReader()

    report = await _explain(reader, snapshot=_snapshot(served))

    assert report.workload_observation.state is EvidenceState.UNSUPPORTED
    assert report.workload_observation.token == TOKEN
    assert reader.gets == []


@pytest.mark.parametrize(
    ("resource", "item"),
    [
        ("resourcequotas", {"metadata": {"namespace": "shop"}, "spec": {"hard": {"pods": "0"}}}),
        ("limitranges", {"metadata": {"namespace": "shop"}, "spec": {"limits": []}}),
    ],
)
async def test_a_policy_the_projection_cannot_name_makes_its_list_inconclusive(
    resource: str, item: dict[str, Any]
) -> None:
    report = await _explain(FakeReader(**{resource: (item,)}))

    observation = (
        report.quotas_observation
        if resource == "resourcequotas"
        else report.limit_ranges_observation
    )
    assert _state(observation) == (EvidenceState.PARTIAL, Basis.MALFORMED)
    assert not report.quotas_conclusive(NOW) or resource != "resourcequotas"
    assert not report.limit_ranges_conclusive(NOW) or resource != "limitranges"
    assert report.baseline.state is not EvidenceState.AVAILABLE


async def test_an_unnamed_policy_in_a_truncated_list_keeps_the_lists_basis() -> None:
    unnamed = {"metadata": {"namespace": "shop"}, "spec": {"limits": []}}
    reader = FakeReader(limitranges=_truncated(unnamed))

    report = await _explain(reader)

    assert _state(report.limit_ranges_observation) == (EvidenceState.PARTIAL, Basis.TRUNCATED)
