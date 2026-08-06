"""FakeKubeClient: a scenario-seeded cluster for the eval harness (issue #69).

Backs the **real** ToolExecutor so whatever tools the model chooses get
consistent, realistic responses. Implements exactly the read surface the
read tools touch: list_objects, get_object, list_events_for, stream_logs.

Scenario timestamps are authored against a fixed instant (`SCENARIO_NOW`)
and rebased to the wall clock at construction, so relative ages in tool
output (`age=3h`, event recency) stay identical no matter when a benchmark
runs — repeated runs of the same scenario see the same cluster.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from korvid.evals.scenario import SCENARIO_NOW, TIMESTAMP_PATTERN, ContainerLogs
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.helm import HELM_SECRET_TYPE, HelmReleaseSummary, release_from_secret
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary, summary_for
from korvid.k8s.reads import ReadOps

__all__ = ["SCENARIO_NOW", "FakeKubeClient", "builtin_aliases"]


class _ClusterFixture(Protocol):
    @property
    def objects(self) -> tuple[dict[str, Any], ...]: ...

    @property
    def events(self) -> tuple[dict[str, Any], ...]: ...

    @property
    def logs(self) -> dict[str, ContainerLogs]: ...


def _rebase(value: Any, delta: timedelta) -> Any:
    """Deep-copy `value`, shifting every RFC 3339 timestamp by `delta` and
    normalizing the shifted instant to a UTC string. Handles both timestamp
    strings and the `datetime` objects `yaml.safe_load` produces for
    unquoted RFC 3339 values (naive ones are read as UTC, the timezone
    fixtures are authored against)."""
    if isinstance(value, str) and TIMESTAMP_PATTERN.match(value):
        shifted = (datetime.fromisoformat(value) + delta).astimezone(UTC)
        return shifted.isoformat().replace("+00:00", "Z")
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return ((aware + delta).astimezone(UTC)).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _rebase(item, delta) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_rebase(item, delta) for item in value]
    return value


_BUILTIN_METAS: tuple[ResourceMeta, ...] = (
    ResourceMeta("Pod", "pods", "", "v1", True, ("po",)),
    ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",)),
    ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True, ("rs",)),
    ResourceMeta("StatefulSet", "statefulsets", "apps", "v1", True, ("sts",)),
    ResourceMeta("DaemonSet", "daemonsets", "apps", "v1", True, ("ds",)),
    ResourceMeta("Job", "jobs", "batch", "v1", True),
    ResourceMeta("Service", "services", "", "v1", True, ("svc",)),
    ResourceMeta("Endpoints", "endpoints", "", "v1", True, ("ep",)),
    ResourceMeta("Node", "nodes", "", "v1", False, ("no",)),
    ResourceMeta("PersistentVolumeClaim", "persistentvolumeclaims", "", "v1", True, ("pvc",)),
    ResourceMeta("PersistentVolume", "persistentvolumes", "", "v1", False, ("pv",)),
    ResourceMeta("StorageClass", "storageclasses", "storage.k8s.io", "v1", False, ("sc",)),
    ResourceMeta("ResourceQuota", "resourcequotas", "", "v1", True, ("quota",)),
    ResourceMeta("Namespace", "namespaces", "", "v1", False, ("ns",)),
    ResourceMeta("ConfigMap", "configmaps", "", "v1", True, ("cm",)),
    ResourceMeta("Secret", "secrets", "", "v1", True),
    ResourceMeta("Event", "events", "", "v1", True, ("ev",)),
)


def builtin_aliases() -> dict[str, ResourceMeta]:
    """Alias table (plural, kind, shortnames) for the built-in kinds the
    diagnostic scenarios exercise — what discovery would produce live."""
    aliases: dict[str, ResourceMeta] = {}
    for meta in _BUILTIN_METAS:
        aliases[meta.plural] = meta
        aliases[meta.kind.lower()] = meta
        for shortname in meta.shortnames:
            aliases[shortname] = meta
    return aliases


class FakeKubeClient(ReadOps):
    """Read-only fake of the k8s client surface the agent read tools use.

    Implements the `ReadOps` boundary ABC so it substitutes for the real
    client under strict typing — read-surface drift becomes a type error.
    """

    def __init__(self, scenario: _ClusterFixture) -> None:
        self._scenario = scenario
        delta = datetime.now(UTC) - SCENARIO_NOW
        self._objects: list[dict[str, Any]] = [_rebase(obj, delta) for obj in scenario.objects]
        self._events: list[dict[str, Any]] = [_rebase(event, delta) for event in scenario.events]

    def _matches(self, manifest: dict[str, Any], meta: ResourceMeta, namespace: str | None) -> bool:
        if str(manifest.get("kind") or "") != meta.kind:
            return False
        if meta.namespaced and namespace is not None:
            metadata = manifest.get("metadata") or {}
            return str(metadata.get("namespace") or "") == namespace
        return True

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        return [
            summary_for(meta.kind, manifest, group=meta.group)
            for manifest in self._objects
            if self._matches(manifest, meta, namespace)
        ]

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        for manifest in self._objects:
            metadata = manifest.get("metadata") or {}
            if self._matches(manifest, meta, namespace) and str(metadata.get("name")) == name:
                # The real client deserializes a fresh response per call;
                # callers may mutate the result (ToolExecutor strips
                # managedFields) without affecting later reads.
                return deepcopy(manifest)
        raise ApiStatusError(404, f"{meta.plural} {namespace or ''}/{name} not found")

    async def list_helm_releases(self, namespace: str | None) -> list[HelmReleaseSummary]:
        """Latest revision per release from helm-owned Secrets in the scenario."""
        latest: dict[tuple[str, str], HelmReleaseSummary] = {}
        for manifest in self._objects:
            if str(manifest.get("type") or "") != HELM_SECRET_TYPE:
                continue
            metadata = manifest.get("metadata") or {}
            if (metadata.get("labels") or {}).get("owner") != "helm":
                continue
            if namespace is not None and str(metadata.get("namespace") or "") != namespace:
                continue
            release = release_from_secret(manifest)
            key = (release.namespace, release.name)
            current = latest.get(key)
            if current is None or release.revision > current.revision:
                latest[key] = release
        return sorted(latest.values(), key=lambda r: (r.namespace, r.name))

    async def list_events_for(
        self,
        namespace: str,
        name: str,
        *,
        kind: str | None = None,
        uid: str | None = None,
    ) -> list[dict[str, Any]]:
        matched: list[dict[str, Any]] = []
        for event in self._events:
            involved = event.get("involvedObject") or {}
            if (
                str(involved.get("name") or "") == name
                and (kind is None or str(involved.get("kind") or "") == kind)
                and str(involved.get("namespace") or "") == namespace
                and (uid is None or str(involved.get("uid") or "") == uid)
            ):
                matched.append(deepcopy(event))
        return matched

    def _resolve_container(self, namespace: str, pod: str, container: str) -> str:
        """An empty container mirrors the real client: valid only when the
        pod has exactly one container to default to."""
        if container:
            return container
        prefix = f"{namespace}/{pod}/"
        names = [key[len(prefix) :] for key in self._scenario.logs if key.startswith(prefix)]
        if len(names) == 1:
            return names[0]
        raise ApiStatusError(400, f"container name required for pod {namespace}/{pod}")

    async def stream_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncIterator[LogLine]:
        resolved = self._resolve_container(namespace, pod, container)
        logs = self._scenario.logs.get(f"{namespace}/{pod}/{resolved}")
        if logs is None:
            raise ApiStatusError(404, f"container {resolved!r} in pod {namespace}/{pod} not found")
        lines = logs.previous if previous else logs.current
        if previous and not lines:
            raise ApiStatusError(400, f"previous terminated container {resolved!r} not found")
        for text in lines[-max(1, tail_lines) :]:
            yield LogLine(pod=pod, container=resolved, text=text)
