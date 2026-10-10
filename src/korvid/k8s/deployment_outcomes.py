"""Bounded, read-only raw Deployment outcome snapshots."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError, KubeClientError

DEPLOYMENT_META = ResourceMeta(
    "Deployment", "deployments", "apps", "v1", True, ("deploy", "deployment")
)
REPLICA_SET_META = ResourceMeta(
    "ReplicaSet", "replicasets", "apps", "v1", True, ("rs", "replicaset")
)
POD_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po", "pod"))

GetObject = Callable[[ResourceMeta, str | None, str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class RawDeploymentOutcomeSnapshot:
    """One bounded raw read; normalization belongs to the importing layer."""

    deployment: dict[str, Any]
    replica_sets: tuple[dict[str, Any], ...]
    pods: tuple[dict[str, Any], ...]
    partial: bool
    pod_ownership_ambiguous: bool = False


class DeploymentOutcomeReader(ABC):
    """Read-only Kubernetes boundary consumed by the UI tracker."""

    @abstractmethod
    async def snapshot(self, namespace: str, name: str) -> RawDeploymentOutcomeSnapshot:
        """Read one namespace-bounded raw snapshot."""


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _entries(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _selector_text(raw: object) -> tuple[str | None, bool]:
    selector = _mapping(raw)
    if not selector:
        return None, True
    parts: list[str] = []
    labels = _mapping(selector.get("matchLabels"))
    parts.extend(f"{key}={value}" for key, value in sorted(labels.items()))
    partial = False
    for expression in _entries(selector.get("matchExpressions")):
        key = str(expression.get("key") or "")
        operator = str(expression.get("operator") or "")
        values = expression.get("values")
        normalized_values = (
            tuple(str(value) for value in values) if isinstance(values, list) else ()
        )
        if not key:
            partial = True
        elif operator == "In" and normalized_values:
            parts.append(f"{key} in ({','.join(normalized_values)})")
        elif operator == "NotIn" and normalized_values:
            parts.append(f"{key} notin ({','.join(normalized_values)})")
        elif operator == "Exists":
            parts.append(key)
        elif operator == "DoesNotExist":
            parts.append(f"!{key}")
        else:
            partial = True
    return (",".join(parts) if parts else None), partial


class KubeDeploymentOutcomeReader(DeploymentOutcomeReader):
    """Collect raw Deployment, ReplicaSet, and Pod evidence."""

    def __init__(
        self,
        *,
        get_object: GetObject,
        list_raw_objects: Callable[
            ...,
            Awaitable[tuple[list[dict[str, Any]], bool]],
        ],
        list_limit: int = 200,
    ) -> None:
        if list_limit < 1:
            raise ValueError("list_limit must be positive")
        self._get_object = get_object
        self._list_raw_objects = list_raw_objects
        self._list_limit = list_limit

    async def snapshot(self, namespace: str, name: str) -> RawDeploymentOutcomeSnapshot:
        """Read one server-bounded page for each related resource kind."""

        deployment = await self._get_object(DEPLOYMENT_META, namespace, name)
        spec = _mapping(deployment.get("spec"))
        selector, selector_partial = _selector_text(spec.get("selector"))
        if selector is None:
            return RawDeploymentOutcomeSnapshot(deployment, (), (), True, True)
        replica_sets, replica_sets_partial = await self._list_raw_objects(
            REPLICA_SET_META,
            namespace,
            label_selector=selector,
            limit=self._list_limit,
        )
        pods, pods_partial = await self._list_raw_objects(
            POD_META,
            namespace,
            label_selector=selector,
            limit=self._list_limit,
        )
        current = await self._get_object(DEPLOYMENT_META, namespace, name)
        before = _mapping(deployment.get("metadata"))
        after = _mapping(current.get("metadata"))
        revision_keys = ("uid", "generation", "resourceVersion")
        if any(before.get(key) != after.get(key) for key in revision_keys):
            return RawDeploymentOutcomeSnapshot(current, (), (), True, True)
        return RawDeploymentOutcomeSnapshot(
            deployment=current,
            replica_sets=tuple(replica_sets),
            pods=tuple(pods),
            partial=selector_partial or replica_sets_partial or pods_partial,
            pod_ownership_ambiguous=selector_partial or replica_sets_partial,
        )


def parse_list_page(data: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Validate one Kubernetes LIST response page."""

    items = data.get("items")
    metadata = data.get("metadata", {})
    continuation = metadata.get("continue", "") if isinstance(metadata, Mapping) else None
    if (
        not isinstance(items, list)
        or any(not isinstance(item, dict) for item in items)
        or not isinstance(continuation, str)
    ):
        raise KubeClientError(
            "Kubernetes API returned a malformed response; retry, then check the API server"
        )
    return items, continuation


async def list_raw_object_page(
    meta: ResourceMeta,
    namespace: str | None,
    label_selector: str | None,
    limit: int,
    *,
    list_path: Callable[[ResourceMeta, str | None], str],
    request_json: Callable[..., Awaitable[dict[str, Any]]],
    observe: Callable[..., None],
    observe_error: Callable[[str, ApiStatusError], None],
) -> tuple[list[dict[str, Any]], bool]:
    """Read one bounded raw LIST page through a KubeClient's transport seams."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    path = list_path(meta, namespace)
    query = [("limit", str(limit))]
    if label_selector:
        query.append(("labelSelector", label_selector))
    try:
        data = await request_json(path, query_params=query)
        items, continuation = parse_list_page(data)
    except ApiStatusError as exc:
        observe_error(path, exc)
        raise
    observe("list", path, payload=data, object_count=len(items))
    return items, bool(continuation)


async def iter_raw_object_pages(
    meta: ResourceMeta,
    namespace: str | None,
    *,
    page_size: int,
    list_path: Callable[[ResourceMeta, str | None], str],
    request_json: Callable[..., Awaitable[dict[str, Any]]],
    observe: Callable[..., None],
    observe_error: Callable[[str, ApiStatusError], None],
) -> AsyncIterator[dict[str, Any]]:
    """Yield validated raw objects across bounded Kubernetes LIST pages."""

    path = list_path(meta, namespace)
    continuation = ""
    seen: set[str] = set()
    while True:
        query = [("limit", str(page_size))]
        if continuation:
            query.append(("continue", continuation))
        try:
            data = await request_json(path, query_params=query)
            items, next_token = parse_list_page(data)
        except ApiStatusError as exc:
            observe_error(path, exc)
            raise
        except KubeClientError:
            observe("error", path)
            raise
        observe("list", path, payload=data, object_count=len(items))
        for item in items:
            yield item
        if not next_token:
            return
        if next_token in seen:
            observe("error", path)
            raise KubeClientError("LIST continuation did not advance")
        seen.add(next_token)
        continuation = next_token
        await asyncio.sleep(0)
