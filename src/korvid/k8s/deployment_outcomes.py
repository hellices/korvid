"""Bounded, read-only raw Deployment outcome snapshots."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from korvid.k8s.discovery import ResourceMeta

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
            return RawDeploymentOutcomeSnapshot(deployment, (), (), True)
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
        return RawDeploymentOutcomeSnapshot(
            deployment=deployment,
            replica_sets=tuple(replica_sets),
            pods=tuple(pods),
            partial=selector_partial or replica_sets_partial or pods_partial,
        )
