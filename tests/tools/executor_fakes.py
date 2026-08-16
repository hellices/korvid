"""Shared executor test fakes and builders."""

from __future__ import annotations

import copy
from typing import Any

from korvid.k8s.discovery import PODS_META
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.k8s.models import summary_for
from korvid.tools.executor import ToolExecutor, UIBridge


class FakeKube:
    def __init__(self) -> None:
        self.manifest: dict[str, Any] = {"kind": "Pod", "metadata": {"name": "a"}}

    async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
        return self.manifest


def make_executor(kube: Any) -> ToolExecutor:
    return ToolExecutor(kube, {"pods": PODS_META, "pod": PODS_META})


class FakeLogKube(FakeKube):
    """FakeKube with a scripted stream_logs recording its call arguments."""

    def __init__(self) -> None:
        super().__init__()
        self.manifest = {
            "kind": "Pod",
            "metadata": {"name": "web"},
            "spec": {"containers": [{"name": "app"}, {"name": "sidecar"}]},
        }
        self.log_calls: list[dict[str, Any]] = []

    async def stream_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> Any:
        self.log_calls.append(
            {
                "namespace": namespace,
                "pod": pod,
                "container": container,
                "follow": follow,
                "tail_lines": tail_lines,
            }
        )
        for text in ("line-1", "line-2"):
            yield LogLine(pod=pod, container=container, text=text)


class FakeEventKube(FakeKube):
    def __init__(self) -> None:
        super().__init__()
        self.manifest = {"kind": "Pod", "metadata": {"name": "web", "uid": "abc-123"}}
        self.event_calls: list[dict[str, Any]] = []

    async def list_events_for(
        self,
        namespace: str,
        name: str,
        *,
        kind: str | None = None,
        uid: str | None = None,
    ) -> list[dict[str, Any]]:
        self.event_calls.append({"namespace": namespace, "name": name, "kind": kind, "uid": uid})
        return [{"type": "Warning", "reason": "BackOff", "count": 3, "message": "restarting"}]


class FakeBridge(UIBridge):
    """Records UI-control calls; returns canned confirmations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        self.calls.append(("navigate", {"view": view, "namespace": namespace}))
        return f"switched to {view}"

    async def agent_set_filter(self, pattern: str) -> str:
        self.calls.append(("set_filter", {"pattern": pattern}))
        return f"filter set to {pattern!r}"

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        self.calls.append(
            ("open_logs", {"pod": pod, "namespace": namespace, "container": container})
        )
        return f"log pane opened for {namespace}/{pod}"

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        self.calls.append(("open_describe", {"kind": kind, "name": name, "namespace": namespace}))
        return f"describe opened for {kind}/{name}"

    async def agent_drill_down(self, name: str) -> str:
        self.calls.append(("drill_down", {"name": name}))
        return f"drilled into {name}"

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        self.calls.append(
            (
                "request_write",
                {
                    "action": action,
                    "kind": kind,
                    "name": name,
                    "namespace": namespace,
                    "replicas": replicas,
                    "resources": resources,
                },
            )
        )
        return f"approved and executed: {action} {kind}/{name}"

    async def agent_submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        self.calls.append(
            (
                "submit_proposal",
                {
                    "action": action,
                    "kind": kind,
                    "name": name,
                    "namespace": namespace,
                    "replicas": replicas,
                    "resources": resources,
                    "session_id": session_id,
                    "client_name": client_name,
                    "client_version": client_version,
                },
            )
        )
        return "proposal abc123 pending"

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        self.calls.append(("get_proposal", {"proposal_id": proposal_id}))
        return "proposal pending"

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        self.calls.append(
            ("cancel_proposal", {"proposal_id": proposal_id, "session_id": session_id})
        )
        return "proposal cancelled"


def make_ui_executor(bridge: Any) -> ToolExecutor:
    kube: Any = FakeKube()
    # proposal_tools mirrors the MCP server's wiring — the only surface the
    # write-proposal tools may dispatch from.
    return ToolExecutor(kube, {"pods": PODS_META}, ui=bridge, proposal_tools=True)


def _diagnose_aliases() -> dict[str, Any]:
    from korvid.k8s.discovery import ResourceMeta

    return {
        "pods": PODS_META,
        "pod": PODS_META,
        "replicasets": ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True),
        "deployments": ResourceMeta("Deployment", "deployments", "apps", "v1", True),
        "nodes": ResourceMeta("Node", "nodes", "", "v1", False),
        "persistentvolumeclaims": ResourceMeta(
            "PersistentVolumeClaim", "persistentvolumeclaims", "", "v1", True
        ),
    }


class FakeDiagnoseKube:
    """Scripted cluster for the compound tool: pod, owners, node, PVC, events, logs."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {
            ("pods", "api-1"): {
                "kind": "Pod",
                "metadata": {
                    "name": "api-1",
                    "namespace": "default",
                    "uid": "pod-uid",
                    "creationTimestamp": "2026-07-27T06:00:00Z",
                    "ownerReferences": [
                        {"kind": "ReplicaSet", "name": "api-6f", "controller": True}
                    ],
                },
                "spec": {
                    "nodeName": "node-a",
                    "containers": [{"name": "app"}],
                    "volumes": [
                        {"name": "data", "persistentVolumeClaim": {"claimName": "data-claim"}}
                    ],
                },
                "status": {
                    "phase": "Running",
                    "conditions": [
                        {"type": "Ready", "status": "False", "reason": "ContainersNotReady"}
                    ],
                    "containerStatuses": [
                        {
                            "name": "app",
                            "ready": False,
                            "restartCount": 7,
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                            "lastState": {"terminated": {"exitCode": 1, "reason": "Error"}},
                        }
                    ],
                },
            },
            ("replicasets", "api-6f"): {
                "kind": "ReplicaSet",
                "metadata": {
                    "name": "api-6f",
                    "ownerReferences": [{"kind": "Deployment", "name": "api", "controller": True}],
                },
            },
            ("nodes", "node-a"): {
                "metadata": {"name": "node-a"},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "MemoryPressure", "status": "True"},
                    ]
                },
            },
            ("persistentvolumeclaims", "data-claim"): {
                "metadata": {"name": "data-claim"},
                "status": {"phase": "Bound"},
            },
        }
        self.events: list[dict[str, Any]] = [
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "restarting failed container",
                "count": 9,
                "lastTimestamp": "2026-07-27T06:20:00Z",
            }
        ]
        self.log_lines: list[str] = [
            *(f"serving request {i}" for i in range(40)),
            "ERROR: db connection refused",
            *(f"retrying {i}" for i in range(20)),
        ]
        self.log_calls: list[dict[str, Any]] = []

    async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
        obj = self.objects.get((meta.plural, name))
        if obj is None:
            raise ApiStatusError(404, "NotFound")
        return obj

    async def list_objects(self, meta: Any, namespace: str | None) -> list[Any]:
        summaries: list[Any] = []
        for obj in self.objects.values():
            if obj.get("kind") != meta.kind:
                continue
            metadata = obj.get("metadata") or {}
            if meta.namespaced and namespace is not None and metadata.get("namespace") != namespace:
                continue
            summaries.append(summary_for(meta.kind, obj, group=meta.group, version=meta.version))
        return summaries

    async def list_events_for(
        self, namespace: str, name: str, *, kind: str | None = None, uid: str | None = None
    ) -> list[dict[str, Any]]:
        return self.events

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
        self.log_calls.append(
            {"pod": pod, "container": container, "previous": previous, "tail_lines": tail_lines}
        )
        for text in self.log_lines:
            yield LogLine(pod=pod, container=container, text=text)


def _diagnose_executor(kube: Any) -> ToolExecutor:
    return ToolExecutor(kube, _diagnose_aliases())


#: Marker values that must never reach any consumer of a tool result.
NESTED_SECRET_SENTINEL = "bmVzdGVkLWNyZWQ="
LONG_NAME_ENV_SENTINEL = "primary-db-admin-pw"


def oversized_crd_with_nested_credentials() -> dict[str, Any]:
    """A CRD too large for the result budget that buries two classifiers.

    The embedded Secret's `kind` and the env entry's long `name` are what
    tell a redactor that `data.kubeconfig` and `value` are credentials.
    Both are removed by *size* reduction (mapping elision, scalar
    clamping), so anything that bounds before it redacts can no longer
    recognize the values it is about to ship.
    """
    return {
        "apiVersion": "apps.example.com/v1",
        "kind": "CompositeApp",
        "metadata": {"name": "composite-0", "namespace": "prod"},
        "spec": {
            "secretTemplate": {
                "data": {"kubeconfig": NESTED_SECRET_SENTINEL},
                **{f"annotationTemplate{index}": "x" * 200 for index in range(240)},
                "kind": "Secret",
                "apiVersion": "v1",
            },
            "podTemplate": {
                "containers": [
                    {
                        "name": "api",
                        "env": [
                            {
                                "name": "PRIMARY_DATABASE_CONNECTION_STRING_ADMIN_PASSWORD",
                                "value": LONG_NAME_ENV_SENTINEL,
                            }
                        ],
                    }
                ]
            },
        },
    }


_LOG_SECRET = "9f3c1a7e42b85d06c7e1f0a2b3d4e5f60718293a4b5c6d7e8f90"


def _credential_log_kube(assignment: str) -> FakeDiagnoseKube:
    """A rollout failure whose log excerpts carry a credential assignment."""
    kube = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 2},
        "status": {"replicas": 2, "readyReplicas": 1, "conditions": []},
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
    base = kube.objects[("pods", "api-1")]
    for index in range(2, 4):
        clone = copy.deepcopy(base)
        clone["metadata"]["name"] = f"api-{index}"
        kube.objects[("pods", f"api-{index}")] = clone
    for key, obj in list(kube.objects.items()):
        if key[0] != "pods":
            continue
        obj["metadata"]["ownerReferences"] = [
            {"kind": "ReplicaSet", "name": "api-6f", "uid": "rs-uid", "controller": True}
        ]
        obj["metadata"].setdefault("namespace", "default")
        obj["status"]["phase"] = "Pending"
    # Long enough that each pod block still exceeds its share after
    # masking, so the head+tail cut genuinely fires on this fixture.
    kube.log_lines = [
        "." * (index % 11) + f"level=error {assignment} retry " + "z" * 520 for index in range(200)
    ]
    return kube


PARENT_SECRET = "4d2e6b8a1c0f73951e8a0d2c4b6e8f01"


class ParentCredentialKube(FakeDiagnoseKube):
    """A rollout failure whose *parent* sections carry the credential.

    The pod blocks are clean on purpose: they have been redacted since
    round 9, so only an assignment in a workload condition, a workload
    Warning event, or a failed child LIST can show whether the assembled
    parent report is covered too.
    """

    def __init__(
        self,
        *,
        condition_message: str = "replicas are unavailable",
        event_message: str = "failed to create pods",
        list_error: str = "",
        pod_log_line: str = "connection refused",
    ) -> None:
        super().__init__()
        self.list_error = list_error
        self.objects[("deployments", "api")] = {
            "kind": "Deployment",
            "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
            "spec": {"replicas": 2},
            "status": {
                "replicas": 2,
                "readyReplicas": 1,
                "conditions": [
                    {
                        "type": "Available",
                        "status": "False",
                        "reason": "MinimumReplicasUnavailable",
                        "message": condition_message,
                    }
                ],
            },
        }
        replicaset = self.objects[("replicasets", "api-6f")]
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
        for key, obj in list(self.objects.items()):
            if key[0] != "pods":
                continue
            obj["metadata"]["ownerReferences"] = [
                {"kind": "ReplicaSet", "name": "api-6f", "uid": "rs-uid", "controller": True}
            ]
            obj["metadata"].setdefault("namespace", "default")
            obj["status"]["phase"] = "Pending"
        self.log_lines = [pod_log_line]
        self.workload_events: list[dict[str, Any]] = [
            {
                "type": "Warning",
                "reason": "FailedCreate",
                "message": event_message,
                "count": 3,
                "lastTimestamp": "2026-07-27T06:30:00Z",
            }
        ]

    async def list_objects(self, meta: Any, namespace: str | None) -> list[Any]:
        if self.list_error and meta.kind == "Pod":
            raise ApiStatusError(500, self.list_error)
        return await super().list_objects(meta, namespace)

    async def list_events_for(
        self, namespace: str, name: str, *, kind: str | None = None, uid: str | None = None
    ) -> list[dict[str, Any]]:
        # The workload's own events, not the pod's: this fixture has to be
        # able to plant a credential in exactly one of the two.
        return self.workload_events if kind == "Deployment" else self.events


def identity_last_crd() -> dict[str, Any]:
    """An oversized CRD whose identity keys are last in insertion order."""
    document: dict[str, Any] = {f"extensionField{index:02d}": "y" * 400 for index in range(60)}
    document["apiVersion"] = "example.com/v1"
    document["kind"] = "CompositeApp"
    document["metadata"] = {
        "labels": {f"team-{index}": "z" * 60 for index in range(40)},
        "name": "composite-0",
        "namespace": "prod",
    }
    return document


def _ambiguous_key_manifest() -> dict[str, Any]:
    """Annotation keys that YAML would resolve to bools, nulls and numbers.

    Serialized carelessly they collapse into one another; the boundary
    refuses a document whose keys collapse, so the producer has to emit
    them as the distinct strings they are.
    """
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "flags",
            "annotations": {
                "true": "a",
                "yes": "b",
                "null": "c",
                "~": "d",
                "1": "e",
                "1.0": "f",
                "on": "g",
            },
        },
    }
