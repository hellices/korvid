"""Async Kubernetes client wrapper. The only module that talks to the API server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio import watch as k8s_watch

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary


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

    async def watch_pods(self, namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
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
        if namespace is not None:
            list_path = f"{meta.api_base}/namespaces/{namespace}/{meta.plural}"
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

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        """Fetch the raw manifest for a single object. ApiException → ApiStatusError."""
        if meta.namespaced and namespace is not None:
            path = f"{meta.api_base}/namespaces/{namespace}/{meta.plural}/{name}"
        else:
            path = f"{meta.api_base}/{meta.plural}/{name}"
        return await self._request_json(path)

    async def list_events_for(self, namespace: str, name: str) -> list[dict[str, Any]]:
        """Return core v1 Events where involvedObject.name == name."""
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        try:
            resp = await self._core_v1.list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={name}",
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
        """Return every list+watch-able resource from /api/v1 and /apis."""
        metas: list[ResourceMeta] = []
        core = await self._request_json("/api/v1")
        metas += _parse_resource_list(core, group="", version="v1")
        groups = await self._request_json("/apis")
        for g in groups.get("groups", []):
            version = (g.get("preferredVersion") or {}).get("version")
            if not version:
                continue
            try:
                rl = await self._request_json(f"/apis/{g['name']}/{version}")
            except ApiStatusError:
                continue  # a broken aggregated API must not kill discovery
            metas += _parse_resource_list(rl, group=g["name"], version=version)
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
        verbs: list[str] = r.get("verbs", [])
        if "/" in r["name"] or "list" not in verbs or "watch" not in verbs:
            continue
        out.append(
            ResourceMeta(
                r["kind"],
                r["name"],
                group,
                version,
                r["namespaced"],
                tuple(r.get("shortNames") or ()),
            )
        )
    return out
