"""Tests for projecting workloads and their children for creation diagnostics (#411)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core.creation_failures import FailureSource, Subject
from korvid.core.creation_workloads import (
    WORKLOAD_KINDS,
    ClaimTemplate,
    ContainerDemand,
    Counter,
    project_children,
    project_workload,
)

POD_SPEC: dict[str, Any] = {
    "priorityClassName": "gold",
    "activeDeadlineSeconds": 600,
    "runtimeClassName": "kata",
    "resources": {"requests": {"cpu": "1"}},
    "initContainers": [
        {
            "name": "migrate",
            "image": "registry/migrate:secret-tag",
            "resources": {"requests": {"cpu": "2"}},
        },
        {"name": "proxy", "restartPolicy": "Always", "resources": {"limits": {"memory": "64Mi"}}},
    ],
    "containers": [
        {
            "name": "app",
            "image": "registry/app:secret-tag",
            "command": ["run", "--token=s3cr3t-literal"],
            "env": [{"name": "TOKEN", "value": "s3cr3t-literal"}],
            "resources": {
                "requests": {"cpu": "500m", "memory": "128Mi"},
                "limits": {"memory": "256Mi"},
            },
        }
    ],
    "volumes": [{"name": "v", "secret": {"secretName": "creds"}}],
}


def _workload(
    kind: str, spec: dict[str, Any], status: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": "api",
            "namespace": "shop",
            "uid": "uid-api",
            "annotations": {"note": "annotation-literal"},
        },
        "spec": spec,
        "status": status or {},
    }


def test_the_six_kinds_and_their_children() -> None:
    assert {kind: (k.group, k.resource, k.child) for kind, k in WORKLOAD_KINDS.items()} == {
        "Deployment": ("apps", "deployments", "ReplicaSet"),
        "ReplicaSet": ("apps", "replicasets", None),
        "StatefulSet": ("apps", "statefulsets", None),
        "DaemonSet": ("apps", "daemonsets", None),
        "Job": ("batch", "jobs", None),
        "CronJob": ("batch", "cronjobs", "Job"),
    }


def test_a_deployment_keeps_only_its_demand_and_never_a_literal() -> None:
    item = _workload("Deployment", {"replicas": 3, "template": {"spec": POD_SPEC}}, {"replicas": 0})

    projected = project_workload("Deployment", item)

    assert projected is not None
    assert projected.subject == Subject("Deployment", "api", "uid-api")
    assert projected.counters == (Counter("desired", "3"), Counter("created", "0"))
    template = projected.template
    assert template is not None
    assert template.containers == (
        ContainerDemand(
            "app", "container", (("cpu", "500m"), ("memory", "128Mi")), (("memory", "256Mi"),)
        ),
    )
    assert template.init_containers == (
        ContainerDemand("migrate", "init", (("cpu", "2"),), ()),
        ContainerDemand("proxy", "sidecar", (), (("memory", "64Mi"),)),
    )
    assert (template.priority_class, template.active_deadline_seconds, template.runtime_class) == (
        "gold",
        600,
        "kata",
    )
    assert (template.pod_requests, template.pod_limits) == ((("cpu", "1"),), ())
    for literal in ("s3cr3t-literal", "secret-tag", "annotation-literal", "creds"):
        assert literal not in repr(projected)


@pytest.mark.parametrize(
    ("kind", "spec", "status", "counters"),
    [
        ("ReplicaSet", {"replicas": 2}, {}, (Counter("desired", "2"), Counter("created", "0"))),
        (
            "StatefulSet",
            {"replicas": 1},
            {"replicas": 1},
            (Counter("desired", "1"), Counter("created", "1")),
        ),
        (
            "DaemonSet",
            {},
            {"desiredNumberScheduled": 4, "currentNumberScheduled": 2},
            (Counter("desired", "4"), Counter("created", "2")),
        ),
        (
            "Job",
            {"parallelism": 2},
            {"active": 1},
            (Counter("parallelism", "2"), Counter("active", "1")),
        ),
        (
            "CronJob",
            {"suspend": True},
            {"active": [{"name": "a"}, {"name": "b"}], "lastScheduleTime": "2026-10-30T09:00:00Z"},
            (
                Counter("suspended", "yes"),
                Counter("active", "2"),
                Counter("last schedule", "2026-10-30T09:00:00Z"),
            ),
        ),
        ("Deployment", {}, {}, (Counter("desired", None), Counter("created", "0"))),
        (
            "CronJob",
            {},
            {},
            (Counter("suspended", "no"), Counter("active", "0"), Counter("last schedule", None)),
        ),
    ],
)
def test_each_kind_has_its_own_counters(
    kind: str, spec: dict[str, Any], status: dict[str, Any], counters: tuple[Counter, ...]
) -> None:
    projected = project_workload(kind, _workload(kind, spec, status))

    assert projected is not None
    assert projected.counters == counters


def test_a_cronjob_template_is_its_job_templates_pod_template() -> None:
    spec = {"jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "task"}]}}}}}

    projected = project_workload("CronJob", _workload("CronJob", spec))

    assert projected is not None
    assert projected.template is not None
    assert [c.name for c in projected.template.containers] == ["task"]


def test_a_statefulset_keeps_its_claim_templates_and_whether_the_class_was_omitted() -> None:
    claims = [
        {
            "metadata": {"name": "data"},
            "spec": {"storageClassName": "gold", "resources": {"requests": {"storage": "10Gi"}}},
        },
        {"metadata": {"name": "logs"}, "spec": {"resources": {"requests": {"storage": "1Gi"}}}},
        {"metadata": {"name": "scratch"}, "spec": {"storageClassName": ""}},
    ]
    spec = {"replicas": 1, "template": {"spec": {"containers": []}}, "volumeClaimTemplates": claims}

    projected = project_workload("StatefulSet", _workload("StatefulSet", spec))

    assert projected is not None
    assert projected.template is not None
    assert projected.template.claims == (
        ClaimTemplate("data", "10Gi", "gold"),
        ClaimTemplate("logs", "1Gi", None),
        ClaimTemplate("scratch", None, ""),
    )


def test_a_replica_failure_on_the_workload_is_kept() -> None:
    status = {
        "conditions": [
            {
                "type": "ReplicaFailure",
                "status": "True",
                "reason": "FailedCreate",
                "message": "exceeded quota: q, x",
            }
        ]
    }

    projected = project_workload("Deployment", _workload("Deployment", {"replicas": 1}, status))

    assert projected is not None
    assert projected.failure is not None
    assert (projected.failure.source, projected.failure.rejection.quotas) == (
        FailureSource.CONDITION,
        ("q",),
    )


@pytest.mark.parametrize(
    "item",
    [{"metadata": {"name": "api"}}, {"metadata": {"uid": "u"}}, {}],
    ids=["no-uid", "no-name", "no-metadata"],
)
def test_a_workload_without_a_name_or_uid_is_not_projected(item: dict[str, Any]) -> None:
    assert project_workload("Deployment", item) is None


def test_an_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="workload kind"):
        project_workload("Pod", _workload("Pod", {}))


def _child(
    name: str, owner_uid: str | None, *, controller: bool = True, **extra: Any
) -> dict[str, Any]:
    owners = (
        []
        if owner_uid is None
        else [{"uid": owner_uid, "controller": controller, "kind": "X", "name": "x"}]
    )
    return {
        "metadata": {
            "name": name,
            "namespace": "shop",
            "uid": f"uid-{name}",
            "ownerReferences": owners,
            **extra.pop("metadata", {}),
        },
        "spec": {
            "template": {
                "spec": {"containers": [{"env": [{"name": "T", "value": "s3cr3t-literal"}]}]}
            }
        },
        **extra,
    }


def test_only_children_the_workload_controls_are_kept() -> None:
    items = [
        _child("api-1", "uid-api"),
        _child("api-2", "uid-api", controller=False),
        _child("other-1", "uid-other"),
        _child("orphan", None),
        "not-an-object",
    ]

    children = project_children("ReplicaSet", items, "uid-api")

    assert [c.subject for c in children] == [Subject("ReplicaSet", "api-1", "uid-api-1")]
    assert "s3cr3t-literal" not in repr(children)


def test_a_replica_sets_failed_create_condition_is_kept() -> None:
    status = {
        "conditions": [
            {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate", "message": "m"}
        ]
    }

    (child,) = project_children(
        "ReplicaSet", [_child("api-1", "uid-api", status=status)], "uid-api"
    )

    assert child.failure is not None
    assert child.failure.subject == child.subject


def test_jobs_are_newest_first() -> None:
    items = [
        _child("job-old", "uid-cron", metadata={"creationTimestamp": "2026-10-30T08:00:00Z"}),
        _child("job-new", "uid-cron", metadata={"creationTimestamp": "2026-10-30T09:00:00Z"}),
        _child("job-undated", "uid-cron"),
    ]

    children = project_children("Job", items, "uid-cron")

    assert [c.subject.name for c in children] == ["job-new", "job-old", "job-undated"]
    assert children[0].created_at == datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


def test_malformed_template_fields_are_dropped_and_numbers_kept_as_text() -> None:
    pod = {
        "activeDeadlineSeconds": True,
        "containers": [
            {"resources": {"requests": {"cpu": "1"}}},
            {"name": "app", "resources": {"requests": {"cpu": 2, "memory": None}}},
        ],
    }

    projected = project_workload("Job", _workload("Job", {"template": {"spec": pod}}))

    assert projected is not None
    assert projected.template is not None
    assert projected.template.active_deadline_seconds is None
    assert projected.template.containers == (
        ContainerDemand("app", "container", (("cpu", "2"),), ()),
    )


def test_only_a_statefulset_has_claim_templates() -> None:
    spec = {
        "template": {"spec": {"containers": []}},
        "volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {}}],
    }

    projected = project_workload("Deployment", _workload("Deployment", spec))

    assert projected is not None
    assert projected.template is not None
    assert projected.template.claims == ()
