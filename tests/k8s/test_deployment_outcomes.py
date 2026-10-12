from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from korvid.core.deployment_outcome import normalize_deployment_observation
from korvid.k8s.deployment_outcomes import (
    POD_META,
    KubeDeploymentOutcomeReader,
    iter_raw_object_pages,
    list_raw_object_page,
)
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import KubeClientError


def _owner(kind: str, uid: str) -> list[dict[str, object]]:
    return [
        {
            "apiVersion": "apps/v1",
            "kind": kind,
            "name": kind.lower(),
            "uid": uid,
            "controller": True,
        }
    ]


def _deployment(*, uid: str = "deploy-uid") -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "web",
            "namespace": "default",
            "uid": uid,
            "generation": 7,
            "resourceVersion": "10",
        },
        "spec": {
            "replicas": 3,
            "selector": {
                "matchLabels": {"app": "web"},
                "matchExpressions": [
                    {"key": "track", "operator": "In", "values": ["stable", "canary"]}
                ],
            },
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": "2026-10-09T12:00:00+00:00"
                    }
                }
            },
        },
        "status": {
            "observedGeneration": 7,
            "replicas": 3,
            "updatedReplicas": 2,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "unavailableReplicas": 2,
            "conditions": [
                {
                    "type": "Progressing",
                    "status": "True",
                    "reason": "ReplicaSetUpdated",
                    "message": "new replica set progressing",
                },
                {"type": "Ignored", "status": "True", "reason": "not retained"},
            ],
        },
    }


def _replica_set(*, uid: str, owner_uid: str) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {
            "name": f"web-{uid}",
            "namespace": "default",
            "uid": uid,
            "annotations": {"deployment.kubernetes.io/revision": "1"},
            "ownerReferences": _owner("Deployment", owner_uid),
        },
    }


def _pod(*, index: int, owner_uid: str, reason: str = "ImagePullBackOff") -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"web-{index}",
            "namespace": "default",
            "uid": f"pod-{index}",
            "ownerReferences": _owner("ReplicaSet", owner_uid),
            "labels": {"app": "web", "track": "stable"},
        },
        "status": {
            "phase": "Pending",
            "containerStatuses": [
                {
                    "name": "web",
                    "state": {
                        "waiting": {
                            "reason": reason,
                            "message": "registry unavailable",
                        }
                    },
                }
            ],
        },
    }


class _Api:
    def __init__(
        self,
        *,
        deployment: dict[str, Any] | None = None,
        replica_sets: list[dict[str, Any]] | None = None,
        pods: list[dict[str, Any]] | None = None,
        replica_sets_partial: bool = False,
        pods_partial: bool = False,
    ) -> None:
        self.deployment = deployment or _deployment()
        self.replica_sets = replica_sets or []
        self.pods = pods or []
        self.replica_sets_partial = replica_sets_partial
        self.pods_partial = pods_partial
        self.calls: list[tuple[str, str | None, str | None, int]] = []

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        assert meta.kind == "Deployment"
        assert namespace == "default"
        assert name == "web"
        return self.deployment

    async def list_raw_objects(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        *,
        label_selector: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        self.calls.append((meta.kind, namespace, label_selector, limit))
        if meta.kind == "ReplicaSet":
            return self.replica_sets[:limit], self.replica_sets_partial or len(
                self.replica_sets
            ) > limit
        assert meta.kind == "Pod"
        return self.pods[:limit], self.pods_partial or len(self.pods) > limit


def _reader(api: _Api, *, list_limit: int = 200) -> KubeDeploymentOutcomeReader:
    get_object: Callable[[ResourceMeta, str | None, str], Awaitable[dict[str, Any]]] = (
        api.get_object
    )
    list_raw: Callable[..., Awaitable[tuple[list[dict[str, Any]], bool]]] = api.list_raw_objects
    return KubeDeploymentOutcomeReader(
        get_object=get_object,
        list_raw_objects=list_raw,
        list_limit=list_limit,
    )


@pytest.mark.parametrize("page_size", [0, -1, True, 1.5])
async def test_raw_page_iterator_rejects_unbounded_page_sizes(page_size: Any) -> None:
    request = AsyncMock()
    pages = iter_raw_object_pages(
        POD_META,
        "default",
        page_size=page_size,
        list_path=lambda meta, namespace: "/api/v1/namespaces/default/pods",
        request_json=request,
        observe=lambda *args, **kwargs: None,
        observe_error=lambda path, error: None,
    )

    with pytest.raises(ValueError, match="page_size must be a positive integer"):
        await anext(pages)
    request.assert_not_awaited()


async def test_single_raw_page_observes_malformed_response_error() -> None:
    observations: list[tuple[str, str]] = []

    with pytest.raises(KubeClientError, match="malformed response"):
        await list_raw_object_page(
            POD_META,
            "default",
            None,
            5,
            list_path=lambda meta, namespace: "/api/v1/namespaces/default/pods",
            request_json=AsyncMock(return_value={"items": "invalid"}),
            observe=lambda action, path, **kwargs: observations.append((action, path)),
            observe_error=lambda path, error: None,
        )

    assert observations == [("error", "/api/v1/namespaces/default/pods")]


async def test_snapshot_reads_exact_deployment_and_caps_owned_pod_evidence() -> None:
    api = _Api(
        replica_sets=[
            _replica_set(uid="rs-current", owner_uid="deploy-uid"),
            _replica_set(uid="rs-other", owner_uid="other-deploy"),
        ],
        pods=[
            _pod(index=99, owner_uid="rs-other"),
            *[_pod(index=index, owner_uid="rs-current") for index in range(8)],
        ],
    )

    raw = await _reader(api, list_limit=10).snapshot("default", "web")
    observation = normalize_deployment_observation(raw, max_pod_evidence=5)

    assert observation.uid == "deploy-uid"
    assert observation.generation == 7
    assert observation.updated_replicas == 2
    assert observation.restart_stamp == "2026-10-09T12:00:00+00:00"
    assert [pod.uid for pod in observation.pods] == [f"pod-{index}" for index in range(5)]
    assert observation.partial_evidence is True
    assert observation.pod_evidence_ambiguous is False
    assert all(pod.uid != "pod-99" for pod in observation.pods)
    assert [(kind, limit) for kind, _, _, limit in api.calls] == [
        ("ReplicaSet", 10),
        ("Pod", 10),
    ]
    assert all(namespace == "default" for _, namespace, _, _ in api.calls)


async def test_snapshot_discards_related_evidence_when_deployment_changes_during_read() -> None:
    class ChangingApi(_Api):
        def __init__(self) -> None:
            super().__init__(replica_sets=[_replica_set(uid="rs-current", owner_uid="deploy-uid")])
            self.gets = 0

        async def get_object(
            self, meta: ResourceMeta, namespace: str | None, name: str
        ) -> dict[str, Any]:
            self.gets += 1
            deployment = _deployment()
            if self.gets == 2:
                deployment["metadata"]["generation"] = 8
                deployment["metadata"]["resourceVersion"] = "11"
            return deployment

    raw = await _reader(ChangingApi()).snapshot("default", "web")

    assert raw.deployment["metadata"]["generation"] == 8
    assert raw.replica_sets == ()
    assert raw.pods == ()
    assert raw.pod_ownership_ambiguous is True


async def test_snapshot_uses_server_side_deployment_selector() -> None:
    api = _Api()

    await _reader(api).snapshot("default", "web")

    selectors = [selector for _, _, selector, _ in api.calls]
    assert selectors == [
        "app=web,track in (stable,canary)",
        "app=web,track in (stable,canary)",
    ]


async def test_snapshot_retains_only_relevant_conditions() -> None:
    observation = normalize_deployment_observation(await _reader(_Api()).snapshot("default", "web"))

    assert [(condition.type, condition.reason) for condition in observation.conditions] == [
        ("Progressing", "ReplicaSetUpdated")
    ]


@pytest.mark.parametrize(
    ("replica_sets_partial", "pods_partial", "ownership_ambiguous"),
    [(True, False, True), (False, True, False)],
)
async def test_snapshot_marks_each_server_truncation_as_partial(
    replica_sets_partial: bool,
    pods_partial: bool,
    ownership_ambiguous: bool,
) -> None:
    observation = normalize_deployment_observation(
        await _reader(
            _Api(
                replica_sets_partial=replica_sets_partial,
                pods_partial=pods_partial,
            )
        ).snapshot("default", "web")
    )

    assert observation.partial_evidence is True
    assert observation.pod_evidence_ambiguous is ownership_ambiguous


async def test_snapshot_does_not_default_malformed_counts_to_zero() -> None:
    manifest = _deployment()
    manifest["status"]["replicas"] = True
    manifest["status"]["readyReplicas"] = "3"

    observation = normalize_deployment_observation(
        await _reader(_Api(deployment=manifest)).snapshot("default", "web")
    )

    assert observation.current_replicas is None
    assert observation.ready_replicas is None


async def test_snapshot_propagates_authoritative_get_failure() -> None:
    api = _Api()

    async def denied(meta: ResourceMeta, namespace: str | None, name: str) -> dict[str, Any]:
        raise PermissionError("denied")

    reader = KubeDeploymentOutcomeReader(
        get_object=denied,
        list_raw_objects=api.list_raw_objects,
    )

    with pytest.raises(PermissionError, match="denied"):
        await reader.snapshot("default", "web")
