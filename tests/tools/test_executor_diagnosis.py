"""Executor diagnosis behavior tests."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.k8s.models import EndpointSliceSummary, GenericSummary, StorageClassSummary, summary_for
from korvid.tools.executor import MAX_RESULT_CHARS, READ_TOOLS, ToolExecutor, compact_result
from korvid.tools.registry import TOOLS_BY_NAME
from korvid.tools.structured import ERROR_PREFIX, load_structured_document
from tests.tools.executor_fakes import FakeDiagnoseKube, FakeKube, _diagnose_executor

# --- diagnose_pod (issue #70) ------------------------------------------------


def test_diagnose_pod_schema_documents_the_log_container_cap() -> None:
    schema = next(t for t in READ_TOOLS if t["function"]["name"] == "diagnose_pod")
    description = schema["function"]["description"]
    assert "up to 3" in description


async def test_diagnose_pod_reports_all_sections_in_evidence_order() -> None:
    kube = FakeDiagnoseKube()
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert not out.startswith("ERROR:")
    # Identity and owner chain up front.
    assert "pod default/api-1" in out
    assert "phase=Running" in out
    assert "owner: Deployment api (via ReplicaSet api-6f)" in out
    # Related context.
    assert "node node-a" in out
    assert "MemoryPressure=True" in out
    assert "pvc data-claim: Bound" in out
    # Evidence.
    assert "CrashLoopBackOff" in out
    assert "restarts=7" in out
    assert "BackOff (9x" in out
    assert "ERROR: db connection refused" in out
    # Primacy/recency ordering: identity first, log evidence last.
    assert out.index("phase=Running") < out.index("BackOff (9x")
    assert out.index("BackOff (9x") < out.index("ERROR: db connection refused")


async def test_diagnose_pod_fetches_logs_only_for_troubled_containers() -> None:
    kube = FakeDiagnoseKube()
    _ = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert [c["container"] for c in kube.log_calls] == ["app"]


async def test_diagnose_pod_healthy_pod_skips_log_fetches() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("pods", "api-1")]["status"]["containerStatuses"] = [
        {
            "name": "app",
            "ready": True,
            "restartCount": 0,
            "state": {"running": {"startedAt": "2026-07-27T06:01:00Z"}},
        }
    ]
    kube.objects[("pods", "api-1")]["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    kube.events = []
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert kube.log_calls == []
    assert "no troubled containers" in out


async def test_diagnose_pod_puts_current_health_before_historical_restart_evidence() -> None:
    kube = FakeDiagnoseKube()
    pod = kube.objects[("pods", "api-1")]
    pod["status"]["phase"] = "Running"
    pod["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    pod["status"]["containerStatuses"] = [
        {
            "name": "app",
            "ready": True,
            "restartCount": 2,
            "state": {"running": {"startedAt": "2026-07-27T06:01:00Z"}},
            "lastState": {"terminated": {"exitCode": 255, "reason": "Error"}},
        }
    ]
    kube.events = []
    kube.log_lines = ["lost connection to peer, exiting for restart"]

    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )

    assert "CURRENT HEALTH\n  READY NOW" in out
    assert out.index("READY NOW") < out.index("lost connection to peer")


async def test_diagnose_pod_includes_pvc_storage_class_and_warning_events() -> None:
    class PvcEventKube(FakeDiagnoseKube):
        async def list_events_for(
            self,
            namespace: str,
            name: str,
            *,
            kind: str | None = None,
            uid: str | None = None,
        ) -> list[dict[str, Any]]:
            if kind == "PersistentVolumeClaim":
                return [
                    {
                        "type": "Warning",
                        "reason": "ProvisioningFailed",
                        "message": 'storageclass.storage.k8s.io "fast-ssd" not found',
                        "count": 9,
                    }
                ]
            return await super().list_events_for(namespace, name, kind=kind, uid=uid)

    kube = PvcEventKube()
    pvc = kube.objects[("persistentvolumeclaims", "data-claim")]
    pvc["metadata"]["uid"] = "pvc-uid"
    pvc["spec"] = {"storageClassName": "fast-ssd"}
    pvc["status"] = {"phase": "Pending"}

    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )

    assert "pvc data-claim: Pending storageClass=fast-ssd" in out
    assert "ProvisioningFailed (9x)" in out
    assert 'storageclass.storage.k8s.io "fast-ssd" not found' in out


async def test_diagnose_pod_distinguishes_pvc_event_failure_from_pvc_read() -> None:
    class PvcEventsDeniedKube(FakeDiagnoseKube):
        async def list_events_for(
            self,
            namespace: str,
            name: str,
            *,
            kind: str | None = None,
            uid: str | None = None,
        ) -> list[dict[str, Any]]:
            if kind == "PersistentVolumeClaim":
                raise ApiStatusError(403, "PVC events forbidden")
            return await super().list_events_for(namespace, name, kind=kind, uid=uid)

    out = await _diagnose_executor(PvcEventsDeniedKube()).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "pvc data-claim: Bound storageClass=(default)" in out
    assert "pvc data-claim warning events: unavailable" in out
    assert "pvc data-claim: unavailable" not in out


async def test_diagnose_pod_distinguishes_default_and_explicit_no_storage_class() -> None:
    kube = FakeDiagnoseKube()
    pod = kube.objects[("pods", "api-1")]
    pod["spec"]["volumes"] = [
        {"name": "defaulted", "persistentVolumeClaim": {"claimName": "defaulted"}},
        {"name": "classless", "persistentVolumeClaim": {"claimName": "classless"}},
    ]
    kube.objects[("persistentvolumeclaims", "defaulted")] = {
        "metadata": {"name": "defaulted"},
        "spec": {},
        "status": {"phase": "Pending"},
    }
    kube.objects[("persistentvolumeclaims", "classless")] = {
        "metadata": {"name": "classless"},
        "spec": {"storageClassName": ""},
        "status": {"phase": "Pending"},
    }

    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )

    assert "pvc defaulted: Pending storageClass=(default)" in out
    assert "pvc classless: Pending storageClass=(none)" in out


async def test_diagnose_pod_missing_pod_is_an_error() -> None:
    kube = FakeDiagnoseKube()
    del kube.objects[("pods", "api-1")]
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert out.startswith("ERROR:")


async def test_diagnose_pod_sub_fetch_failures_do_not_kill_the_report() -> None:
    """Owner, node, PVC, events, and logs are all best-effort evidence."""

    class FlakyKube(FakeDiagnoseKube):
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            if meta.plural != "pods":
                raise RuntimeError("api hiccup")
            return await super().get_object(meta, namespace, name)

        async def list_events_for(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("events down")

    out = await _diagnose_executor(FlakyKube()).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert not out.startswith("ERROR:")
    assert "phase=Running" in out
    assert "unavailable" in out  # the failed sections say so instead of vanishing
    assert "ERROR: db connection refused" in out  # log evidence still present


async def test_diagnose_pod_report_stays_under_the_ingest_cap() -> None:
    kube = FakeDiagnoseKube()
    kube.log_lines = [f"noise {i} " + "x" * 80 for i in range(1000)]
    kube.log_lines[500] = "ERROR: the one that matters"
    kube.events = [
        {
            "type": "Warning",
            "reason": f"Reason{i}",
            "message": f"message {i} " + "y" * 60,
            "count": 1,
            "lastTimestamp": f"2026-07-27T06:{i % 60:02d}:00Z",
        }
        for i in range(100)
    ]
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert len(out) < MAX_RESULT_CHARS
    assert "truncated" not in out  # under the cap by construction, not by chopping
    assert "ERROR: the one that matters" in out


async def test_diagnose_pod_owner_chain_stops_at_a_direct_workload() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("pods", "api-1")]["metadata"]["ownerReferences"] = [
        {"kind": "StatefulSet", "name": "db", "controller": True}
    ]
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "owner: StatefulSet db" in out
    assert "via" not in out


async def test_diagnose_pod_without_owner_reports_standalone() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("pods", "api-1")]["metadata"].pop("ownerReferences")
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "owner: none (standalone pod)" in out


async def test_diagnose_pod_reads_previous_instance_logs_after_restarts() -> None:
    """The crash evidence of a restarted container lives in the previous
    instance; the current one is either freshly restarted or gone."""
    kube = FakeDiagnoseKube()  # container "app" has restartCount=7
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert [(c["container"], c["previous"]) for c in kube.log_calls] == [("app", True)]
    assert "[app] (previous instance)" in out


async def test_diagnose_pod_reads_current_logs_for_a_currently_failed_termination() -> None:
    """A container terminated non-zero *right now* logged that failure in the
    current instance — previous=True would fetch the penultimate crash."""
    kube = FakeDiagnoseKube()
    kube.objects[("pods", "api-1")]["status"]["containerStatuses"][0]["state"] = {
        "terminated": {"exitCode": 1, "reason": "Error"}
    }
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert [(c["container"], c["previous"]) for c in kube.log_calls] == [("app", False)]
    assert "(previous instance)" not in out


def test_render_log_blocks_survives_a_non_positive_budget() -> None:
    executor = _diagnose_executor(FakeDiagnoseKube())
    blocks = [["[app]", "line 1", "line 2"], ["[sidecar]", "line 3"]]
    lines = executor._render_log_blocks(blocks, -50)
    assert "  [app]" in lines
    assert "  [sidecar]" in lines


async def test_diagnose_pod_falls_back_to_current_logs_when_previous_unavailable() -> None:
    class NoPreviousKube(FakeDiagnoseKube):
        async def stream_logs(self, *a: Any, previous: bool = False, **k: Any) -> Any:
            if previous:
                raise RuntimeError("previous terminated logs rotated away")
            async for line in super().stream_logs(*a, previous=previous, **k):
                yield line

    kube = NoPreviousKube()
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "ERROR: db connection refused" in out
    assert "(previous instance)" not in out


async def test_diagnose_pod_reads_current_logs_for_a_never_restarted_container() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("pods", "api-1")]["status"]["containerStatuses"][0]["restartCount"] = 0
    _ = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert [(c["container"], c["previous"]) for c in kube.log_calls] == [("app", False)]


async def test_diagnose_pod_unbounded_messages_cannot_evict_the_log_evidence() -> None:
    """Event/condition messages are cluster-controlled and unbounded; the
    report must clamp them and reserve room so the final LOG EXCERPTS
    section survives instead of being prefix-truncated away."""
    kube = FakeDiagnoseKube()
    kube.objects[("pods", "api-1")]["status"]["conditions"] = [
        {"type": "Ready", "status": "False", "reason": "Huge", "message": "c" * 5000}
    ]
    kube.events = [
        {
            "type": "Warning",
            "reason": f"Reason{i}",
            "message": f"m{i} " + "e" * 4000,
            "count": 1,
            "lastTimestamp": f"2026-07-27T06:{i % 60:02d}:00Z",
        }
        for i in range(10)
    ]
    kube.log_lines = ["boot ok", "ERROR: db connection refused", "final line " + "z" * 3000]
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert len(out) <= MAX_RESULT_CHARS
    assert "truncated" not in out  # budgeted by construction, not chopped by execute()
    assert "ERROR: db connection refused" in out  # the evidence survived


async def test_diagnose_pod_finds_an_error_marker_beyond_the_line_clamp() -> None:
    """The error marker must be searched in the raw line — clamping first
    would hide a marker buried past the clamp in a long (e.g. JSON) line."""
    kube = FakeDiagnoseKube()
    kube.log_lines = [f"noise {i}" for i in range(40)]
    kube.log_lines[19] = "context sentinel before the buried marker"
    kube.log_lines[20] = "padding " * 40 + "ERROR: buried past the clamp"
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "context sentinel before the buried marker" in out


async def test_diagnose_pod_budgets_each_container_block_keeping_headers() -> None:
    """Overflow is trimmed within each container's block — a huge excerpt
    for one container must not evict another container's header or logs."""
    kube = FakeDiagnoseKube()
    pod = kube.objects[("pods", "api-1")]
    pod["spec"]["containers"] = [{"name": f"c{i}"} for i in range(3)]
    pod["status"]["containerStatuses"] = [
        {
            "name": f"c{i}",
            "ready": False,
            "restartCount": 4,
            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
        }
        for i in range(3)
    ]
    kube.log_lines = [f"line {j} " + "x" * 230 for j in range(60)]
    kube.log_lines[30] = "ERROR: shared failure"
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert len(out) <= MAX_RESULT_CHARS
    for i in range(3):  # every block keeps its attribution header
        assert f"[c{i}] (previous instance)" in out
    assert out.count("…") >= 3  # each over-budget block elides visibly


async def test_diagnose_pod_marks_pvcs_beyond_the_fetch_cap() -> None:
    """Storage evidence must not present a capped fetch as the full set —
    claim six could be the Pending one."""
    kube = FakeDiagnoseKube()
    pod = kube.objects[("pods", "api-1")]
    pod["spec"]["volumes"] = [
        {"name": f"v{i}", "persistentVolumeClaim": {"claimName": f"claim-{i}"}} for i in range(7)
    ]
    for i in range(7):
        kube.objects[("persistentvolumeclaims", f"claim-{i}")] = {
            "metadata": {"name": f"claim-{i}"},
            "status": {"phase": "Bound"},
        }
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "pvc claim-4: Bound" in out
    assert "pvc claim-5" not in out  # not fetched — and not silently omitted either
    assert "(2 more claims not fetched: claim-5, claim-6)" in out


async def test_diagnose_pod_works_with_pod_only_aliases() -> None:
    """Before background API discovery lands, the alias table holds only
    pods — the built-in ReplicaSet/Node/PVC lookups must still work via
    fixed metadata for these stable APIs, not silently vanish."""
    kube: Any = FakeDiagnoseKube()
    executor = ToolExecutor(kube, {"pods": PODS_META, "pod": PODS_META})
    out = await executor.execute("diagnose_pod", {"pod": "api-1", "namespace": "default"})
    assert "owner: Deployment api (via ReplicaSet api-6f)" in out
    assert "MemoryPressure=True" in out
    assert "pvc data-claim: Bound" in out


async def test_diagnose_pod_labels_a_failed_parent_lookup() -> None:
    """An RBAC/API failure on the ReplicaSet hop must not masquerade as
    'this ReplicaSet has no controller'."""

    class NoRsKube(FakeDiagnoseKube):
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            if meta.plural == "replicasets":
                raise RuntimeError("rbac denied")
            return await super().get_object(meta, namespace, name)

    out = await _diagnose_executor(NoRsKube()).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "owner: ReplicaSet api-6f" in out
    assert "parent lookup unavailable" in out


async def test_diagnose_pod_clamps_the_skipped_container_summary() -> None:
    """The 'also troubled' name list is cluster-controlled (many long
    container names) and must respect the line clamp like everything else."""
    kube = FakeDiagnoseKube()
    pod = kube.objects[("pods", "api-1")]
    names = [f"sidecar-{i}-" + "n" * 50 for i in range(30)]
    pod["status"]["containerStatuses"] = [
        {
            "name": name,
            "ready": False,
            "restartCount": 0,
            "state": {"waiting": {"reason": "ImagePullBackOff"}},
        }
        for name in names
    ]
    kube.log_lines = ["pull failed"]
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert len(out) <= MAX_RESULT_CHARS
    assert "truncated" not in out  # never falls back to prefix truncation
    assert all(len(line) <= 250 for line in out.splitlines())


# --- diagnose_workload ------------------------------------------------------


def test_diagnose_workload_schema_prefers_one_call_for_rollout_failures() -> None:
    schema = next(t for t in READ_TOOLS if t["function"]["name"] == "diagnose_workload")
    description = schema["function"]["description"]
    assert "Deployment" in description
    assert "ReplicaSet" in description
    assert "pod" in description


async def test_diagnose_workload_follows_deployment_to_the_failing_pod() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 2},
        "status": {
            "replicas": 2,
            "readyReplicas": 1,
            "conditions": [
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                    "message": 'ReplicaSet "api-6f" timed out progressing',
                }
            ],
        },
    }
    replicaset = kube.objects[("replicasets", "api-6f")]
    replicaset["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [
                {"kind": "Deployment", "name": "api", "uid": "deploy-uid", "controller": True}
            ],
        }
    )
    replicaset["spec"] = {"replicas": 1}
    replicaset["status"] = {"replicas": 1, "readyReplicas": 0}
    pod = kube.objects[("pods", "api-1")]
    pod["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "name": "api-6f", "uid": "rs-uid", "controller": True}
    ]
    pod["status"]["phase"] = "Pending"
    pod["status"]["containerStatuses"][0]["state"] = {
        "waiting": {
            "reason": "ImagePullBackOff",
            "message": 'Back-off pulling image "api:v9-typo"',
        }
    }

    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert not out.startswith("ERROR:")
    assert "WORKLOAD — Deployment default/api" in out
    assert "ProgressDeadlineExceeded" in out
    assert "ReplicaSet api-6f" in out
    assert "POD DIAGNOSIS — default/api-1" in out
    assert "ImagePullBackOff" in out
    assert len(out) <= MAX_RESULT_CHARS


async def test_diagnose_workload_projects_deployment_replica_status() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 3},
        "status": {
            "replicas": 3,
            "updatedReplicas": 2,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "unavailableReplicas": 2,
        },
    }
    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert "desired=3 current=3 updated=2 ready=1 available=1 unavailable=2" in out


async def test_diagnose_workload_budget_keeps_every_selected_pod_header() -> None:
    class PriorityEvidenceKube(FakeDiagnoseKube):
        async def stream_logs(
            self,
            namespace: str,
            pod: str,
            container: str,
            *,
            previous: bool = False,
            follow: bool = True,
            tail_lines: int = 200,
        ) -> Any:
            for index in range(200):
                yield LogLine(
                    pod=pod,
                    container=container,
                    text=f"noise {index} " + "x" * 220,
                )
            yield LogLine(pod=pod, container=container, text=f"DECISIVE EVIDENCE {pod}")

    kube = PriorityEvidenceKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 3},
        "status": {"replicas": 3},
    }
    rs = kube.objects[("replicasets", "api-6f")]
    rs["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "name": "api"}],
        }
    )
    source = kube.objects[("pods", "api-1")]
    source["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "uid": "rs-uid", "name": "api-6f"}
    ]
    for index in range(2, 4):
        pod = copy.deepcopy(source)
        pod["metadata"]["name"] = f"api-{index}"
        pod["metadata"]["uid"] = f"pod-{index}"
        if index == 2:
            pod["status"]["containerStatuses"][0]["state"] = {
                "waiting": {"reason": "ImagePullBackOff"}
            }
        else:
            pod["status"]["phase"] = "Pending"
            pod["status"]["containerStatuses"][0]["state"] = {
                "waiting": {"reason": "ContainerCreating"}
            }
        kube.objects[("pods", f"api-{index}")] = pod
    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert len(out) <= MAX_RESULT_CHARS
    for name in ("api-1", "api-2", "api-3"):
        assert f"POD DIAGNOSIS — default/{name}" in out
        assert f"POD DIAGNOSIS — default/{name}" in compact_result(out, 3_000)
    visible = compact_result(out, 3_000)
    assert "POD DIAGNOSIS — default/api-2: phase=ImagePullBackOff" in visible
    assert "POD DIAGNOSIS — default/api-3: phase=ContainerCreating" in visible
    assert "DECISIVE EVIDENCE api-3" in visible


async def test_diagnose_workload_prefers_newest_replicaset_pods() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 4},
        "status": {"replicas": 4},
    }
    old_rs = kube.objects[("replicasets", "api-6f")]
    old_rs["metadata"].update(
        {
            "namespace": "default",
            "uid": "old-rs",
            "creationTimestamp": "2026-01-01T00:00:00Z",
            "annotations": {"deployment.kubernetes.io/revision": "1"},
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "name": "api"}],
        }
    )
    new_rs = copy.deepcopy(old_rs)
    new_rs["metadata"].update(
        {
            "name": "api-new",
            "uid": "new-rs",
            "creationTimestamp": "2026-02-01T00:00:00Z",
            "annotations": {"deployment.kubernetes.io/revision": "2"},
        }
    )
    kube.objects[("replicasets", "api-new")] = new_rs
    source = kube.objects[("pods", "api-1")]
    source["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "uid": "old-rs", "name": "api-6f"}
    ]
    for index in range(2, 5):
        pod = copy.deepcopy(source)
        pod["metadata"]["name"] = f"api-old-{index}"
        pod["metadata"]["uid"] = f"old-pod-{index}"
        kube.objects[("pods", f"api-old-{index}")] = pod
    new_pod = copy.deepcopy(source)
    new_pod["metadata"]["name"] = "api-new-1"
    new_pod["metadata"]["uid"] = "new-pod"
    new_pod["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "uid": "new-rs", "name": "api-new"}
    ]
    kube.objects[("pods", "api-new-1")] = new_pod

    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert "POD DIAGNOSIS — default/api-new-1" in out


async def test_diagnose_workload_bounds_omitted_pod_names() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 30},
        "status": {"replicas": 30},
    }
    rs = kube.objects[("replicasets", "api-6f")]
    rs["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "name": "api"}],
        }
    )
    source = kube.objects[("pods", "api-1")]
    source["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "uid": "rs-uid", "name": "api-6f"}
    ]
    for index in range(2, 31):
        pod = copy.deepcopy(source)
        pod["metadata"]["name"] = f"api-{index}-" + "x" * 200
        pod["metadata"]["uid"] = f"pod-{index}"
        kube.objects[("pods", pod["metadata"]["name"])] = pod
    kube.log_lines = ["ERROR: useful evidence"]

    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert len(out) <= MAX_RESULT_CHARS
    assert "(27 more non-ready pod(s) not expanded:" in out
    assert "useful evidence" in out
    omitted_line = next(line for line in out.splitlines() if "more non-ready pod(s)" in line)
    assert len(omitted_line) <= 1_300


async def test_diagnose_workload_rejects_unsupported_kinds_with_guidance() -> None:
    out = await _diagnose_executor(FakeDiagnoseKube()).execute(
        "diagnose_workload",
        {"kind": "nodes", "name": "node-a", "namespace": "default"},
    )
    assert out.startswith("ERROR:")
    assert "supports deployments" in out


async def test_diagnose_workload_uses_builtin_deployment_before_discovery() -> None:
    kube: Any = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "status": {},
    }
    out = await ToolExecutor(kube, {"pods": PODS_META, "pod": PODS_META}).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert not out.startswith("ERROR: unknown kind")


async def test_diagnose_workload_keeps_parent_and_siblings_when_a_pod_read_fails() -> None:
    class FlakyPodKube(FakeDiagnoseKube):
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            if meta.plural == "pods" and name == "api-1":
                raise RuntimeError("response decode failed")
            return await super().get_object(meta, namespace, name)

    kube = FlakyPodKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "status": {
            "conditions": [
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                }
            ]
        },
    }
    kube.objects[("replicasets", "api-6f")]["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [
                {
                    "kind": "Deployment",
                    "name": "api",
                    "uid": "deploy-uid",
                    "controller": True,
                }
            ],
        }
    )
    pod = kube.objects[("pods", "api-1")]
    pod["metadata"]["ownerReferences"] = [
        {
            "kind": "ReplicaSet",
            "name": "api-6f",
            "uid": "rs-uid",
            "controller": True,
        }
    ]
    sibling = copy.deepcopy(pod)
    sibling["metadata"]["name"] = "api-2"
    sibling["metadata"]["uid"] = "pod-2"
    kube.objects[("pods", "api-2")] = sibling

    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert not out.startswith("ERROR:")
    assert "ProgressDeadlineExceeded" in out
    assert "POD DIAGNOSIS — default/api-1" in out
    assert "unavailable (response decode failed)" in out
    assert "POD DIAGNOSIS — default/api-2" in out
    assert "CrashLoopBackOff" in out


async def test_diagnose_workload_includes_running_pod_with_failed_ready_condition() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "status": {},
    }
    rs = kube.objects[("replicasets", "api-6f")]
    rs["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "name": "api"}],
        }
    )
    pod = kube.objects[("pods", "api-1")]
    pod["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "uid": "rs-uid", "name": "api-6f"}]
    pod["status"]["phase"] = "Running"
    pod["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
    pod["status"]["containerStatuses"][0]["ready"] = True
    pod["status"]["containerStatuses"][0]["state"] = {"running": {"startedAt": "x"}}
    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert "POD DIAGNOSIS — default/api-1" in out


async def test_diagnose_workload_rejects_same_name_replacement_uid() -> None:
    class ReplacedPodKube(FakeDiagnoseKube):
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            obj = await super().get_object(meta, namespace, name)
            if meta.plural == "pods" and name == "api-1":
                obj = copy.deepcopy(obj)
                obj["metadata"]["uid"] = "replacement-uid"
            return obj

    kube = ReplacedPodKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "status": {},
    }
    rs = kube.objects[("replicasets", "api-6f")]
    rs["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "name": "api"}],
        }
    )
    pod = kube.objects[("pods", "api-1")]
    pod["metadata"]["uid"] = "original-uid"
    pod["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "uid": "rs-uid", "name": "api-6f"}]
    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert "UID changed from original-uid to replacement-uid" in out


async def test_diagnose_workload_keeps_status_when_deployment_events_fail() -> None:
    class EventsDeniedKube(FakeDiagnoseKube):
        async def list_events_for(
            self,
            namespace: str,
            name: str,
            *,
            kind: str | None = None,
            uid: str | None = None,
        ) -> list[dict[str, Any]]:
            if kind == "Deployment":
                raise ApiStatusError(403, "events forbidden")
            return await super().list_events_for(namespace, name, kind=kind, uid=uid)

    kube = EventsDeniedKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 2},
        "status": {
            "replicas": 2,
            "readyReplicas": 1,
            "conditions": [
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                }
            ],
        },
    }
    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert not out.startswith("ERROR:")
    assert "ProgressDeadlineExceeded" in out
    assert "unavailable (API 403: events forbidden)" in out


@pytest.mark.parametrize(
    ("failed_plural", "expected_section"),
    [
        ("replicasets", "OWNED REPLICASETS\n  unavailable"),
        ("pods", "POD DIAGNOSES\n  unavailable"),
    ],
)
async def test_diagnose_workload_keeps_parent_when_child_list_fails(
    failed_plural: str,
    expected_section: str,
) -> None:
    class ChildListDeniedKube(FakeDiagnoseKube):
        async def list_objects(self, meta: Any, namespace: str | None) -> list[Any]:
            if meta.plural == failed_plural:
                raise ApiStatusError(403, f"{failed_plural} forbidden")
            return await super().list_objects(meta, namespace)

    kube = ChildListDeniedKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 2},
        "status": {
            "replicas": 2,
            "readyReplicas": 1,
            "conditions": [
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                }
            ],
        },
    }
    rs = kube.objects[("replicasets", "api-6f")]
    rs["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "name": "api"}],
        }
    )
    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert not out.startswith("ERROR:")
    assert "ProgressDeadlineExceeded" in out
    assert expected_section in out


# -- diagnose_service tests (issue #191) ------------------------------------


class ServiceDiagnosisKube:
    """Records cluster calls; returns scripted service manifest and slices."""

    def __init__(
        self,
        service: dict[str, Any],
        slices: list[GenericSummary] | None = None,
        list_error: ApiStatusError | None = None,
    ) -> None:
        self._service = service
        self._slices: list[GenericSummary] = slices or []
        self._list_error = list_error
        self.calls: list[tuple[str, ...]] = []

    async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
        self.calls.append(("get", meta.kind, str(namespace), name))
        return self._service

    async def list_objects(self, meta: Any, namespace: str | None) -> list[GenericSummary]:
        self.calls.append(("list", meta.kind, str(namespace)))
        if self._list_error is not None:
            raise self._list_error
        return self._slices


def _service_manifest(uid: str = "") -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "api", "namespace": "shop", "uid": uid},
        "spec": {"type": "ClusterIP", "selector": {"app": "api"}},
    }


def _endpoint_slice_summary(
    name: str = "api-abc",
    owner_uids: tuple[str, ...] = (),
    service_owner_uids: tuple[str, ...] | None = None,
    ready_endpoints: int = 0,
) -> EndpointSliceSummary:
    # When service_owner_uids is not given, mirror owner_uids for backwards compat
    # in existing tests (those tests use a single same-kind Service UID).
    resolved_service_owner_uids = owner_uids if service_owner_uids is None else service_owner_uids
    return EndpointSliceSummary(
        name=name,
        namespace="shop",
        kind="EndpointSlice",
        created="",
        uid="",
        owner_uids=owner_uids,
        labels=(("kubernetes.io/service-name", "api"),),
        service_name="api",
        address_type="IPv4",
        endpoints=1,
        ready_endpoints=ready_endpoints,
        service_owner_uids=resolved_service_owner_uids,
    )


def _svc_executor(kube: Any) -> ToolExecutor:
    return ToolExecutor(kube, {})


def test_diagnose_service_is_a_shared_structured_read_tool() -> None:
    definition = TOOLS_BY_NAME["diagnose_service"]
    assert definition.effect == "cluster_read"
    assert definition.result_format == "structured_yaml"
    assert definition.surfaces == frozenset({"high_agent", "low_agent", "mcp"})


@pytest.mark.asyncio
async def test_diagnose_service_gets_once_and_lists_once() -> None:
    kube = ServiceDiagnosisKube(
        service=_service_manifest(uid="svc-1"),
        slices=[_endpoint_slice_summary(owner_uids=("svc-1",), ready_endpoints=1)],
    )
    text = await _svc_executor(kube).execute(
        "diagnose_service", {"service": "api", "namespace": "shop"}
    )
    document = load_structured_document(text)
    assert document["outcome"] == "healthy"
    assert kube.calls == [
        ("get", "Service", "shop", "api"),
        ("list", "EndpointSlice", "shop"),
    ]


@pytest.mark.asyncio
async def test_diagnose_service_projects_rbac_denial_as_gap() -> None:
    kube = ServiceDiagnosisKube(
        service=_service_manifest(uid="svc-1"),
        list_error=ApiStatusError(403, "Forbidden", ""),
    )
    document = load_structured_document(
        await _svc_executor(kube).execute(
            "diagnose_service", {"service": "api", "namespace": "shop"}
        )
    )
    assert document["outcome"] == "incomplete"
    assert document["gaps"][0]["source"] == "endpointslices"


@pytest.mark.asyncio
async def test_diagnose_service_rejects_composite_name_before_cluster_io() -> None:
    kube = ServiceDiagnosisKube(service=_service_manifest())
    text = await _svc_executor(kube).execute(
        "diagnose_service", {"service": "shop/api", "namespace": "shop"}
    )
    assert text.startswith("ERROR:")
    assert kube.calls == []


@pytest.mark.asyncio
async def test_diagnose_service_ignores_untyped_rows() -> None:
    kube = ServiceDiagnosisKube(
        service=_service_manifest(uid="svc-1"),
        slices=[GenericSummary("other", "shop", "Other", "")],
    )
    document = load_structured_document(
        await _svc_executor(kube).execute(
            "diagnose_service", {"service": "api", "namespace": "shop"}
        )
    )
    assert document["findings"][0]["rule_id"] == "service.no_endpoint_slices"


@pytest.mark.asyncio
async def test_diagnose_service_result_remains_bounded_yaml() -> None:
    kube = ServiceDiagnosisKube(
        service=_service_manifest(uid="svc-1"),
        slices=[
            _endpoint_slice_summary(
                name=f"api-{index:05d}",
                owner_uids=("svc-1",),
                ready_endpoints=1,
            )
            for index in range(2_000)
        ],
    )
    text = await _svc_executor(kube).execute(
        "diagnose_service", {"service": "api", "namespace": "shop"}
    )
    assert len(text) <= MAX_RESULT_CHARS
    document = load_structured_document(text)
    assert document["outcome"] == "healthy"
    assert "elided" in text


# ---------------------------------------------------------------------------
# _meta_for_kind_name: CRD from wrong group must not shadow the builtin
# ---------------------------------------------------------------------------


def test_meta_for_kind_name_ignores_crd_endpointslice_with_wrong_group() -> None:
    """A same-kind CRD alias inserted before the real EndpointSlice must not
    be selected; _meta_for_kind_name must return the discovery.k8s.io builtin."""
    crd_alias = ResourceMeta("EndpointSlice", "endpointslices", "example.io", "v1alpha1", True)
    executor = ToolExecutor(FakeKube(), {"endpointslices": crd_alias})  # type: ignore[arg-type]  # minimal fake
    meta = executor._meta_for_kind_name("EndpointSlice")
    assert meta is not None
    assert meta.group == "discovery.k8s.io"


# ---------------------------------------------------------------------------
# End-to-end: raw TypeMeta-less list items produce healthy diagnosis
# ---------------------------------------------------------------------------


class _RawManifestKube:
    """Fake kube that holds raw manifests and dispatches summary_for with group."""

    def __init__(
        self,
        service: dict[str, Any],
        slice_manifests: list[dict[str, Any]],
    ) -> None:
        self._service = service
        self._slice_manifests = slice_manifests

    async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
        return self._service

    async def list_objects(self, meta: Any, namespace: str | None) -> list[Any]:
        return [
            summary_for(meta.kind, m, group=meta.group, version=meta.version)
            for m in self._slice_manifests
        ]


@pytest.mark.asyncio
async def test_diagnose_service_typemeta_less_slices_return_healthy() -> None:
    """Raw LIST items that omit apiVersion/TypeMeta must produce EndpointSliceSummary
    (via the group kwarg path) so diagnose_service reports healthy, not no_endpoint_slices."""
    raw_slice: dict[str, Any] = {
        # Intentionally omits apiVersion and kind, mirroring real Kubernetes LIST responses.
        "metadata": {
            "name": "api-abc",
            "namespace": "shop",
            "uid": "slice-1",
            "labels": {"kubernetes.io/service-name": "api"},
            "ownerReferences": [{"uid": "svc-1"}],
        },
        "addressType": "IPv4",
        "endpoints": [{"conditions": {"ready": True}}],
    }
    kube = _RawManifestKube(
        service=_service_manifest(uid="svc-1"),
        slice_manifests=[raw_slice],
    )
    document = load_structured_document(
        await _svc_executor(kube).execute(
            "diagnose_service", {"service": "api", "namespace": "shop"}
        )
    )
    assert document["outcome"] == "healthy"


@pytest.mark.asyncio
async def test_diagnose_service_unrelated_custom_owner_only_is_healthy() -> None:
    """An EndpointSlice owned only by an unrelated CRD controller (no core/v1 Service
    ownerRef) must not be flagged stale.  Only Service refs are used for stale checks."""
    raw_slice: dict[str, Any] = {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSlice",
        "metadata": {
            "name": "api-abc",
            "namespace": "shop",
            "uid": "slice-crd",
            "labels": {"kubernetes.io/service-name": "api"},
            # Only a custom CRD controller ownerRef — no Service ref
            "ownerReferences": [
                {"kind": "MeshController", "apiVersion": "mesh.example.io/v1", "uid": "crd-uid-99"},
            ],
        },
        "addressType": "IPv4",
        "endpoints": [{"conditions": {"ready": True}}],
    }
    kube = _RawManifestKube(
        service=_service_manifest(uid="svc-real"),
        slice_manifests=[raw_slice],
    )
    document = load_structured_document(
        await _svc_executor(kube).execute(
            "diagnose_service", {"service": "api", "namespace": "shop"}
        )
    )
    assert document["outcome"] == "healthy"


# -- diagnose_pvc tests (PVC Phase 2 Task 4) ------------------------------------


class PVCDiagnosisKube:
    """Records cluster calls; returns scripted PVC manifest, events, and StorageClasses."""

    def __init__(
        self,
        pvc: dict[str, Any],
        storage_classes: list[GenericSummary] | None = None,
        class_error: Exception | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self._pvc = pvc
        self._storage_classes: list[GenericSummary] = storage_classes or []
        self._class_error = class_error
        self._events: list[dict[str, Any]] = events or []
        self.calls: list[tuple[str, ...]] = []

    async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
        self.calls.append(("get", meta.kind, str(namespace), name))
        return self._pvc

    async def list_objects(self, meta: Any, namespace: str | None) -> list[GenericSummary]:
        self.calls.append(("list", meta.kind, namespace))  # type: ignore[arg-type]  # None is intentional per test assertion
        if self._class_error is not None:
            raise self._class_error
        return self._storage_classes

    async def list_events_for(
        self,
        namespace: str,
        name: str,
        *,
        kind: str | None = None,
        uid: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(("events", kind or "?", str(namespace), name, uid or ""))
        return self._events


def _pvc_manifest(
    phase: str = "Pending",
    volume_name: str = "",
    storage_class_name: str = "managed",
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}},
        "storageClassName": storage_class_name,
    }
    if volume_name:
        spec["volumeName"] = volume_name
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "data", "namespace": "shop", "uid": "pvc-uid-1"},
        "spec": spec,
        "status": {"phase": phase},
    }


def _storage_class(name: str) -> StorageClassSummary:
    return StorageClassSummary(
        name=name,
        namespace="",
        kind="StorageClass",
        created="",
        uid="sc-uid-1",
        owner_uids=(),
        labels=(),
        provisioner="kubernetes.io/aws-ebs",
        volume_binding_mode="Immediate",
        allow_volume_expansion=False,
        is_default=False,
    )


def _pvc_executor(kube: Any) -> ToolExecutor:
    return ToolExecutor(kube, {})


def test_diagnose_pvc_is_shared_structured_read() -> None:
    definition = TOOLS_BY_NAME["diagnose_pvc"]
    assert definition.effect == "cluster_read"
    assert definition.result_format == "structured_yaml"
    assert definition.surfaces == frozenset({"high_agent", "low_agent", "mcp"})


@pytest.mark.asyncio
async def test_bound_pvc_performs_only_one_get() -> None:
    kube = PVCDiagnosisKube(pvc=_pvc_manifest(phase="Bound", volume_name="pv-1"))
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    assert document["outcome"] == "healthy"
    assert kube.calls == [("get", "PersistentVolumeClaim", "shop", "data")]


@pytest.mark.asyncio
async def test_pending_pvc_lists_events_and_storage_classes_once() -> None:
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        storage_classes=[_storage_class("managed")],
    )
    await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    assert kube.calls == [
        ("get", "PersistentVolumeClaim", "shop", "data"),
        ("events", "PersistentVolumeClaim", "shop", "data", "pvc-uid-1"),
        ("list", "StorageClass", None),
    ]


@pytest.mark.asyncio
async def test_diagnose_pvc_rejects_composite_name_before_io() -> None:
    kube = PVCDiagnosisKube(pvc=_pvc_manifest())
    text = await _pvc_executor(kube).execute(
        "diagnose_pvc", {"pvc": "shop/data", "namespace": "shop"}
    )
    assert text.startswith("ERROR:")
    assert kube.calls == []


@pytest.mark.asyncio
async def test_explicit_empty_class_skips_storage_class_list() -> None:
    kube = PVCDiagnosisKube(pvc=_pvc_manifest(phase="Pending", storage_class_name=""))
    await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    assert not [call for call in kube.calls if call[0] == "list"]


@pytest.mark.asyncio
async def test_storage_class_api_error_becomes_gap() -> None:
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        class_error=ApiStatusError(403, "Forbidden", ""),
    )
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    assert document["gaps"][0]["source"] == "storageclasses"


@pytest.mark.asyncio
async def test_transport_error_remains_tool_error() -> None:
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        class_error=RuntimeError("connection reset"),
    )
    text = await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    assert text.startswith("ERROR:")


# -- Finding 4: UID in call recording + integration tests --------------------


@pytest.mark.asyncio
async def test_pending_pvc_events_call_includes_uid() -> None:
    """list_events_for call must be recorded with the PVC UID for scoping."""
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        storage_classes=[_storage_class("managed")],
    )
    await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    events_calls = [c for c in kube.calls if c[0] == "events"]
    assert len(events_calls) == 1
    assert events_calls[0] == ("events", "PersistentVolumeClaim", "shop", "data", "pvc-uid-1")


@pytest.mark.asyncio
async def test_prebound_pvc_fetches_events_but_skips_storage_class_list() -> None:
    """Pre-bound pending PVC (volume_name set) should fetch events but NOT list StorageClasses."""
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", volume_name="my-pv", storage_class_name="managed"),
    )
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    assert document["findings"][0]["rule_id"] == "pvc.awaiting_prebound_volume"
    list_calls = [c for c in kube.calls if c[0] == "list"]
    assert list_calls == [], f"Expected no LIST calls, got: {list_calls}"
    events_calls = [c for c in kube.calls if c[0] == "events"]
    assert len(events_calls) == 1


@pytest.mark.asyncio
async def test_pending_pvc_warning_only_events_are_used() -> None:
    """Normal events must not appear in the analysis; only Warning events matter."""
    normal_event = {
        "type": "Normal",
        "reason": "ProvisioningSucceeded",
        "message": "some message",
        "count": 1,
        "lastTimestamp": "2024-01-01T00:00:00Z",
    }
    warning_event = {
        "type": "Warning",
        "reason": "ProvisioningFailed",
        "message": "quota exceeded",
        "count": 3,
        "lastTimestamp": "2024-06-01T00:00:00Z",
    }
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        events=[normal_event, warning_event],
        storage_classes=[_storage_class("managed")],
    )
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    assert document["findings"][0]["rule_id"] == "pvc.provisioning_failed"


@pytest.mark.asyncio
async def test_overlong_event_message_is_clamped_to_240_chars() -> None:
    """Event messages longer than 240 chars must be clamped."""
    long_msg = "x" * 300
    warning_event = {
        "type": "Warning",
        "reason": "ProvisioningFailed",
        "message": long_msg,
        "count": 1,
        "lastTimestamp": "2024-01-01T00:00:00Z",
    }
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        events=[warning_event],
        storage_classes=[_storage_class("managed")],
    )
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    # Evidence values must be no longer than 240 chars
    for finding in document.get("findings", []):
        for ev in finding.get("evidence", []):
            assert len(ev.get("value", "")) <= 240, f"Evidence value too long: {ev}"


@pytest.mark.asyncio
async def test_events_api_status_error_produces_gap() -> None:
    """ApiStatusError on events list must produce a gap, not crash."""

    class EventErrorKube(PVCDiagnosisKube):
        async def list_events_for(
            self, namespace: str, name: str, *, kind: str | None = None, uid: str | None = None
        ) -> list[dict[str, Any]]:
            self.calls.append(("events", kind or "?", str(namespace), name, uid or ""))
            raise ApiStatusError(403, "Forbidden", "")

    kube = EventErrorKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        storage_classes=[_storage_class("managed")],
    )
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    gap_sources = [g["source"] for g in document.get("gaps", [])]
    assert "events" in gap_sources


@pytest.mark.asyncio
async def test_events_non_api_status_error_becomes_tool_error() -> None:
    """Non-ApiStatusError on events list must bubble up as ERROR:."""

    class EventRuntimeErrorKube(PVCDiagnosisKube):
        async def list_events_for(
            self, namespace: str, name: str, *, kind: str | None = None, uid: str | None = None
        ) -> list[dict[str, Any]]:
            self.calls.append(("events", kind or "?", str(namespace), name, uid or ""))
            raise RuntimeError("connection reset")

    kube = EventRuntimeErrorKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
    )
    text = await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    assert text.startswith(ERROR_PREFIX)


@pytest.mark.asyncio
async def test_series_count_and_last_observed_time_reach_finding() -> None:
    """series.count and series.lastObservedTime must be projected into the finding.

    The executor must use the canonical _event_count / _event_last_seen helpers from
    korvid.tools.diagnose so that events.k8s.io/v1 repeating-event fields are honoured.
    """
    series_event = {
        "type": "Warning",
        "reason": "ProvisioningFailed",
        "message": "series event hit",
        # series sub-object carries count and lastObservedTime for events.k8s.io/v1
        "series": {
            "count": 42,
            "lastObservedTime": "2025-05-01T08:00:00Z",
        },
        # Legacy fields intentionally absent to ensure the series path is exercised
    }
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        events=[series_event],
        storage_classes=[_storage_class("managed")],
    )
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    assert document["findings"][0]["rule_id"] == "pvc.provisioning_failed"
    evidence = {e["field"]: e["value"] for e in document["findings"][0]["evidence"]}
    assert evidence["event.count"] == "42", "series.count must be projected into event.count"
    assert evidence["event.last_seen"] == "2025-05-01T08:00:00Z", (
        "series.lastObservedTime must be projected into event.last_seen"
    )


# ============================================================
# PR #216 review findings — RED tests
# ============================================================

# --- Item 1: non-403 ApiStatusError must re-raise, not become gap ---


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 404, 429, 500, 503])
async def test_pvc_event_non_403_api_status_reraises(status_code: int) -> None:
    """Non-403 ApiStatusError on events must not be silenced as a gap."""

    class NonForbiddenEventKube(PVCDiagnosisKube):
        async def list_events_for(
            self, namespace: str, name: str, *, kind: str | None = None, uid: str | None = None
        ) -> list[dict[str, Any]]:
            self.calls.append(("events", kind or "?", str(namespace), name, uid or ""))
            raise ApiStatusError(status_code, f"HTTP {status_code}", "")

    kube = NonForbiddenEventKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        storage_classes=[_storage_class("managed")],
    )
    text = await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    assert text.startswith(ERROR_PREFIX), (
        f"Expected ERROR for status {status_code} but got: {text[:80]}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 404, 429, 500, 503])
async def test_pvc_storage_class_non_403_api_status_reraises(status_code: int) -> None:
    """Non-403 ApiStatusError on StorageClass LIST must not be silenced as a gap."""
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        class_error=ApiStatusError(status_code, f"HTTP {status_code}", ""),
    )
    text = await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    assert text.startswith(ERROR_PREFIX), (
        f"Expected ERROR for status {status_code} but got: {text[:80]}"
    )


# --- Item 2: decisive failure event skips StorageClass LIST ---


@pytest.mark.asyncio
async def test_failure_event_skips_storage_class_list() -> None:
    """A decisive failure event must cause GET+events only; no StorageClass LIST."""
    failure_event = {
        "type": "Warning",
        "reason": "ProvisioningFailed",
        "message": "quota exceeded",
        "count": 1,
        "lastTimestamp": "2024-06-01T00:00:00Z",
    }
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        events=[failure_event],
        storage_classes=[_storage_class("managed")],
    )
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    assert document["findings"][0]["rule_id"] == "pvc.provisioning_failed"
    list_calls = [c for c in kube.calls if c[0] == "list"]
    assert list_calls == [], (
        f"Expected no StorageClass LIST when failure event present: {list_calls}"
    )


@pytest.mark.asyncio
async def test_storage_class_transport_error_cannot_mask_failure_event() -> None:
    """Even if StorageClass LIST would fail, a failure event must produce the correct finding."""
    failure_event = {
        "type": "Warning",
        "reason": "FailedBinding",
        "message": "no available volumes",
        "count": 2,
        "lastTimestamp": "2024-06-01T00:00:00Z",
    }
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        events=[failure_event],
        class_error=RuntimeError("connection reset"),
    )
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    assert document["findings"][0]["rule_id"] == "pvc.provisioning_failed"
    list_calls = [c for c in kube.calls if c[0] == "list"]
    assert list_calls == [], "StorageClass LIST must not be attempted when failure event exists"


# ============================================================
# PR #216 round-3 findings — RED tests
# ============================================================

# --- Issue 1: Pending PVC missing UID → ERROR before events/class calls ---


@pytest.mark.asyncio
async def test_pending_pvc_missing_uid_returns_error() -> None:
    """Pending PVC with no metadata.uid must return ERROR before calling list_events_for."""
    manifest = _pvc_manifest(phase="Pending", storage_class_name="managed")
    manifest["metadata"].pop("uid")  # remove uid entirely
    kube = PVCDiagnosisKube(pvc=manifest, storage_classes=[_storage_class("managed")])
    text = await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    assert text.startswith(ERROR_PREFIX), f"Expected ERROR but got: {text[:80]}"
    events_calls = [c for c in kube.calls if c[0] == "events"]
    list_calls = [c for c in kube.calls if c[0] == "list"]
    assert events_calls == [], (
        f"Must not call list_events_for when UID is missing, got: {events_calls}"
    )
    assert list_calls == [], f"Must not call list_objects when UID is missing, got: {list_calls}"


@pytest.mark.asyncio
async def test_bound_pvc_missing_uid_still_succeeds() -> None:
    """Bound PVC with no metadata.uid must succeed (single GET, no events call needed)."""
    manifest = _pvc_manifest(phase="Bound", volume_name="pv-1")
    manifest["metadata"].pop("uid")
    kube = PVCDiagnosisKube(pvc=manifest)
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    assert document["outcome"] == "healthy"
    assert kube.calls == [("get", "PersistentVolumeClaim", "shop", "data")]


@pytest.mark.asyncio
async def test_lost_pvc_missing_uid_still_succeeds() -> None:
    """Lost PVC with no metadata.uid must succeed (single GET, no events call needed)."""
    manifest = _pvc_manifest(phase="Lost")
    manifest["metadata"].pop("uid")
    kube = PVCDiagnosisKube(pvc=manifest)
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    assert document["outcome"] == "findings"
    assert document["findings"][0]["rule_id"] == "pvc.lost"
    assert kube.calls == [("get", "PersistentVolumeClaim", "shop", "data")]


# --- Issue 2: StorageClass projection loss → EvidenceGap ---


@pytest.mark.asyncio
async def test_mixed_storage_class_rows_produce_gap() -> None:
    """If any StorageClass LIST row cannot be projected, a storageclasses gap must be returned.

    The class we are looking for is NOT in the typed rows (it may be in an untyped
    row), so the gap prevents a false pvc.storage_class_not_found finding.
    """
    untyped_row = GenericSummary(
        name="managed",  # the class the PVC needs — only available as untyped
        namespace="",
        kind="StorageClass",
        created="",
        uid="",
        owner_uids=(),
        labels=(),
    )
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        storage_classes=[_storage_class("other"), untyped_row],
    )
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    gap_sources = [g["source"] for g in document.get("gaps", [])]
    assert "storageclasses" in gap_sources, f"Expected storageclasses gap, got gaps: {gap_sources}"
    # Analyzer must report incomplete, not a false storage_class_not_found finding
    assert document["outcome"] == "incomplete"


@pytest.mark.asyncio
async def test_all_untyped_storage_class_rows_produce_gap() -> None:
    """If ALL StorageClass LIST rows are non-StorageClassSummary, gap + incomplete."""
    untyped_row = GenericSummary(
        name="untyped",
        namespace="",
        kind="StorageClass",
        created="",
        uid="",
        owner_uids=(),
        labels=(),
    )
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        storage_classes=[untyped_row],
    )
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    gap_sources = [g["source"] for g in document.get("gaps", [])]
    assert "storageclasses" in gap_sources
    assert document["outcome"] == "incomplete"


@pytest.mark.asyncio
async def test_storageclasses_gap_reason_is_count_only_no_object_content() -> None:
    """Gap reason must contain a count but must not quote any object names or content."""
    untyped_row = GenericSummary(
        name="secret-class-name",
        namespace="",
        kind="StorageClass",
        created="",
        uid="",
        owner_uids=(),
        labels=(),
    )
    kube = PVCDiagnosisKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        storage_classes=[_storage_class("managed"), untyped_row],
    )
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    sc_gap = next(g for g in document.get("gaps", []) if g["source"] == "storageclasses")
    reason = sc_gap.get("reason", "")
    assert "secret-class-name" not in reason, (
        f"Gap reason must not quote object names, got: {reason!r}"
    )
    assert any(char.isdigit() for char in reason), (
        f"Gap reason must include a count, got: {reason!r}"
    )


# --- Issue 3: events gap + Immediate → incomplete (executor-level) ---


@pytest.mark.asyncio
async def test_events_gap_immediate_class_executor_returns_incomplete() -> None:
    """Executor: Immediate-class + events gap must yield outcome=incomplete, not provisioning_pending."""

    class EventForbiddenKube(PVCDiagnosisKube):
        async def list_events_for(
            self, namespace: str, name: str, *, kind: str | None = None, uid: str | None = None
        ) -> list[dict[str, Any]]:
            self.calls.append(("events", kind or "?", str(namespace), name, uid or ""))
            raise ApiStatusError(403, "Forbidden", "")

    kube = EventForbiddenKube(
        pvc=_pvc_manifest(phase="Pending", storage_class_name="managed"),
        storage_classes=[_storage_class("managed")],
    )
    document = load_structured_document(
        await _pvc_executor(kube).execute("diagnose_pvc", {"pvc": "data", "namespace": "shop"})
    )
    assert document["outcome"] == "incomplete", (
        f"Expected incomplete, got: {document['outcome']}, findings: {document.get('findings')}"
    )
    rule_ids = [f["rule_id"] for f in document.get("findings", [])]
    assert "pvc.provisioning_pending" not in rule_ids
