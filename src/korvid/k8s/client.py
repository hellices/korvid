"""Async Kubernetes client wrapper. The only module that talks to the API server."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from urllib.parse import quote

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio import watch as k8s_watch

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary, PodSummary


def _path_segment(value: str) -> str:
    """Percent-encode *value* for safe use as a single URL path segment.

    Namespaces arrive from user input (command bar); encoding prevents
    ``/`` or ``..`` from altering the request path.
    """
    return quote(value, safe="")


_DNS1123_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_UID_RE = re.compile(r"^[a-fA-F0-9-]+$")


def _parse_log_line(pod: str, container: str, text: str) -> LogLine:
    """Split the kubelet ``timestamps=true`` RFC3339 prefix off a log line.

    ``datetime.fromisoformat`` (3.11+) accepts the RFC3339Nano form kubelet
    emits, truncating nanoseconds to microseconds. Unparsable prefixes leave
    the line untouched with ``timestamp=None``.
    """
    ts_str, _, rest = text.partition(" ")
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return LogLine(pod=pod, container=container, text=text)
    return LogLine(pod=pod, container=container, text=rest, timestamp=ts)


def resolve_context_name(context: str | None = None, config_file: str | None = None) -> str | None:
    """Return the effective kubeconfig context name, or None if unresolvable.

    Used to pin kubectl subprocesses (shell/debug) with ``--context`` so a
    ``kubectl config use-context`` in another terminal cannot retarget them
    at a different cluster mid-session (k9s parity).
    """
    if context:
        return context
    try:
        _, active = k8s_config.list_kube_config_contexts(config_file=config_file)
    except Exception:
        return None
    if not active:
        return None
    name = active.get("name")
    return str(name) if name else None


class KubeClient:
    """Thin wrapper over kubernetes_asyncio; returns typed summaries."""

    def __init__(self) -> None:
        self._api: k8s_client.ApiClient | None = None
        self._core_v1: k8s_client.CoreV1Api | None = None

    async def connect(self, context: str | None = None) -> None:
        await k8s_config.load_kube_config(context=context)
        self._api = k8s_client.ApiClient()
        self._core_v1 = k8s_client.CoreV1Api(self._api)

    async def list_namespaces(self) -> list[str]:
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        try:
            resp = await self._core_v1.list_namespace(_preload_content=False)
            data = await _to_dict(resp)
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
        return [item["metadata"]["name"] for item in data.get("items", [])]

    async def list_pods(self, namespace: str) -> list[PodSummary]:
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        try:
            resp = await self._core_v1.list_namespaced_pod(namespace, _preload_content=False)
            data = await _to_dict(resp)
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
        return [PodSummary.from_manifest(item) for item in data.get("items", [])]

    async def watch_pods(self, namespace: str | None) -> AsyncIterator[tuple[str, PodSummary]]:
        """LIST then watch pods; namespace=None watches cluster-wide."""
        if namespace is not None:
            async for item in self._watch_pods_namespaced(namespace):
                yield item
        else:
            async for item in self._watch_pods_cluster():
                yield item

    async def _watch_pods_namespaced(self, namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        """Per-namespace pod watch via CoreV1Api (LIST then stream)."""
        if self._core_v1 is None:
            raise RuntimeError("connect() first")

        # LIST first: yield pre-existing pods as ADDED and anchor the watch at
        # the snapshot's resourceVersion so no events are missed between LIST
        # and Watch.  A 410-Gone mid-stream propagates to WatchManager, which
        # retries by calling watch_pods again — the fresh call re-LISTs,
        # which is the intended relist fallback; no separate 410 handling needed.
        try:
            resp = await self._core_v1.list_namespaced_pod(namespace, _preload_content=False)
            data = await _to_dict(resp)
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

        resource_version: str | None = (data.get("metadata") or {}).get("resourceVersion")
        for item in data.get("items", []):
            yield ("ADDED", PodSummary.from_manifest(item))

        watch_kwargs: dict[str, Any] = {}
        if resource_version is not None:
            watch_kwargs["resource_version"] = resource_version

        w = k8s_watch.Watch()
        try:
            async with w.stream(
                self._core_v1.list_namespaced_pod, namespace, **watch_kwargs
            ) as stream:
                async for event in stream:
                    yield (
                        str(event["type"]),
                        PodSummary.from_manifest(event["raw_object"]),
                    )
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    async def _watch_pods_cluster(self) -> AsyncIterator[tuple[str, PodSummary]]:
        """Cluster-wide pod watch via raw /api/v1/pods path (LIST then stream)."""
        if self._api is None:
            raise RuntimeError("connect() first")

        path = "/api/v1/pods"
        data = await self._request_json(path)

        resource_version: str | None = (data.get("metadata") or {}).get("resourceVersion")
        for item in data.get("items", []):
            yield ("ADDED", PodSummary.from_manifest(item))

        watch_kwargs: dict[str, Any] = {}
        if resource_version is not None:
            watch_kwargs["resource_version"] = resource_version

        watch_func = self._make_raw_watch_callable(path)
        w = k8s_watch.Watch()
        try:
            async with w.stream(watch_func, **watch_kwargs) as stream:
                async for event in stream:
                    yield (
                        str(event["type"]),
                        PodSummary.from_manifest(event["raw_object"]),
                    )
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    async def watch_objects(
        self, meta: ResourceMeta, namespace: str | None
    ) -> AsyncIterator[tuple[str, GenericSummary]]:
        """LIST then watch any resource kind; None namespace = all namespaces.

        Contract mirrors watch_pods: pre-existing items are yielded as ADDED
        first, then live watch events from the snapshot resourceVersion.
        ApiException is wrapped as ApiStatusError at both the LIST and watch phases.
        """
        if self._api is None:
            raise RuntimeError("connect() first")

        # LIST phase --------------------------------------------------------
        # Cluster-scoped kinds have no namespaced path regardless of scope.
        if namespace is not None and meta.namespaced:
            list_path = f"{meta.api_base}/namespaces/{_path_segment(namespace)}/{meta.plural}"
        else:
            list_path = f"{meta.api_base}/{meta.plural}"

        data = await self._request_json(list_path)

        resource_version: str | None = (data.get("metadata") or {}).get("resourceVersion")
        for item in data.get("items", []):
            yield ("ADDED", GenericSummary.from_manifest(meta.kind, item))

        # Watch phase -------------------------------------------------------
        watch_kwargs: dict[str, Any] = {}
        if resource_version is not None:
            watch_kwargs["resource_version"] = resource_version

        watch_func = self._make_raw_watch_callable(list_path)

        w = k8s_watch.Watch()
        try:
            async with w.stream(watch_func, **watch_kwargs) as stream:
                async for event in stream:
                    yield (
                        str(event["type"]),
                        GenericSummary.from_manifest(meta.kind, event["raw_object"]),
                    )
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        """LIST any resource kind and return GenericSummary items.

        Reuses the path logic of watch_objects' LIST phase.
        ApiException is wrapped as ApiStatusError.
        """
        if self._api is None:
            raise RuntimeError("connect() first")
        if namespace is not None and meta.namespaced:
            list_path = f"{meta.api_base}/namespaces/{_path_segment(namespace)}/{meta.plural}"
        else:
            list_path = f"{meta.api_base}/{meta.plural}"
        data = await self._request_json(list_path)
        return [GenericSummary.from_manifest(meta.kind, item) for item in data.get("items", [])]

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        """Fetch the raw manifest for a single object. ApiException → ApiStatusError."""
        if meta.namespaced and namespace is not None:
            path = (
                f"{meta.api_base}/namespaces/{_path_segment(namespace)}"
                f"/{meta.plural}/{_path_segment(name)}"
            )
        else:
            path = f"{meta.api_base}/{meta.plural}/{_path_segment(name)}"
        return await self._request_json(path)

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
        """Stream log lines from a pod container; yields LogLine one per line.

        previous=True forces follow=False (terminated containers can't be followed).
        An empty ``container`` omits the parameter (single-container pods).
        Lines are requested with ``timestamps=true``; the RFC3339 prefix is
        parsed into ``LogLine.timestamp`` and stripped from ``LogLine.text``
        so callers can deduplicate the ~tail_lines replay on reconnect.
        ApiException is wrapped as ApiStatusError both at call time and mid-stream.
        """
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        if previous:
            follow = False
        kwargs: dict[str, Any] = {
            "name": pod,
            "namespace": namespace,
            "follow": follow,
            "previous": previous,
            "tail_lines": tail_lines,
            "timestamps": True,
            "_preload_content": False,
        }
        # An empty container name would 400 on multi-container pods; omitting
        # it lets the API server pick the default for single-container pods.
        if container:
            kwargs["container"] = container
        try:
            resp: Any = await self._core_v1.read_namespaced_pod_log(**kwargs)
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
        try:
            async for raw in resp.content:
                if not raw:
                    continue
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                yield _parse_log_line(pod, container, text)
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
        finally:
            # Hard-close the connection: release() would try to drain an
            # infinite follow stream. Guards against leaks on cancel/error.
            resp.close()

    async def list_events_for(
        self,
        namespace: str,
        name: str,
        *,
        kind: str | None = None,
        uid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return core v1 Events for the involved object.

        ``kind``/``uid`` narrow the field selector so same-named objects of a
        different kind (or an earlier incarnation of a recreated object) are
        excluded.
        """
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        # fieldSelector has no escaping mechanism; a name with "," would
        # inject extra selectors. Valid k8s names can't fail this check.
        if not _DNS1123_NAME.match(name):
            return []
        selector = f"involvedObject.name={name}"
        if kind and kind.isalnum():
            selector += f",involvedObject.kind={kind}"
        if uid and _UID_RE.match(uid):
            selector += f",involvedObject.uid={uid}"
        try:
            resp = await self._core_v1.list_namespaced_event(
                namespace,
                field_selector=selector,
                _preload_content=False,
            )
            data = await _to_dict(resp)
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
        result: list[dict[str, Any]] = list(data.get("items", []))
        return result

    def _make_raw_watch_callable(self, path: str) -> Any:
        """Return an async callable compatible with k8s_watch.Watch.stream.

        Watch.stream injects ``watch=True``, ``_preload_content=False``, and
        ``resource_version`` as keyword arguments. This adapter translates those
        to the raw ``call_api`` contract so the correct path is used for **both**
        core-group (group=="", api_base="/api/v1") and extension-group resources,
        eliminating the broken ``/apis//v1/...`` URL that CustomObjectsApi would
        produce when ``group`` is empty.
        """
        api = self._api
        if api is None:
            raise RuntimeError("connect() first")

        async def _watch_call(
            watch: bool = False,
            _preload_content: bool = True,
            resource_version: str | None = None,
            **_rest: Any,
        ) -> Any:
            # watch/_preload_content are injected by Watch.stream; _rest absorbs
            # any future kwargs it may add.
            query_params: list[tuple[str, Any]] = [("watch", "true")]
            if resource_version is not None:
                query_params.append(("resourceVersion", resource_version))
            return await api.call_api(
                path,
                "GET",
                auth_settings=["BearerToken"],
                query_params=query_params,
                _preload_content=False,
            )

        return _watch_call

    async def _request_json(self, path: str) -> dict[str, Any]:
        """Raw GET through the ApiClient; wraps ApiException as ApiStatusError."""
        if self._api is None:
            raise RuntimeError("connect() first")
        try:
            resp = await self._api.call_api(
                path,
                "GET",
                auth_settings=["BearerToken"],
                _preload_content=False,
            )
            body = await resp.read()
            result: dict[str, Any] = json.loads(body)
            return result
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    async def discover_resources(self) -> list[ResourceMeta]:
        """Return every list+watch-able resource from /api/v1 and /apis.

        Group version lists are fetched concurrently — sequential fetching adds
        one RTT per API group and dominates startup on clusters with many CRDs.
        """
        metas: list[ResourceMeta] = []
        core = await self._request_json("/api/v1")
        metas += _parse_resource_list(core, group="", version="v1")
        groups = await self._request_json("/apis")

        async def _fetch(name: str, version: str) -> list[ResourceMeta]:
            try:
                rl = await self._request_json(f"/apis/{name}/{version}")
            except ApiStatusError:
                return []  # a broken aggregated API must not kill discovery
            return _parse_resource_list(rl, group=name, version=version)

        tasks = []
        for g in groups.get("groups", []):
            name = g.get("name")
            version = (g.get("preferredVersion") or {}).get("version")
            if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
                continue  # malformed group must not kill discovery
            tasks.append(_fetch(name, version))
        for group_metas in await asyncio.gather(*tasks):
            metas += group_metas
        return metas

    async def close(self) -> None:
        if self._api is not None:
            await self._api.close()


async def _to_dict(resp: Any) -> dict[str, Any]:
    """Normalize aiohttp response or dict into a plain dict."""
    if isinstance(resp, dict):
        return resp
    body = await resp.read()
    result: dict[str, Any] = json.loads(body)
    return result


def _parse_resource_list(data: dict[str, Any], *, group: str, version: str) -> list[ResourceMeta]:
    out = []
    for r in data.get("resources", []):
        name = r.get("name")
        kind = r.get("kind")
        namespaced = r.get("namespaced")
        verbs: list[str] = r.get("verbs", [])
        if not isinstance(name, str) or not isinstance(kind, str) or namespaced is None:
            continue  # malformed entry must not kill discovery
        if "/" in name or "list" not in verbs or "watch" not in verbs:
            continue
        out.append(
            ResourceMeta(
                kind,
                name,
                group,
                version,
                bool(namespaced),
                tuple(r.get("shortNames") or ()),
            )
        )
    return out
