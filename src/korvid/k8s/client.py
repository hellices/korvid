"""Async Kubernetes client wrapper. The only module that talks to the API server."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio import watch as k8s_watch

from korvid.k8s.models import PodSummary


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
        resp = await self._core_v1.list_namespace(_preload_content=False)
        data = await _to_dict(resp)
        return [item["metadata"]["name"] for item in data.get("items", [])]

    async def list_pods(self, namespace: str) -> list[PodSummary]:
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        resp = await self._core_v1.list_namespaced_pod(namespace, _preload_content=False)
        data = await _to_dict(resp)
        return [PodSummary.from_manifest(item) for item in data.get("items", [])]

    async def watch_pods(self, namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        w = k8s_watch.Watch()
        async with w.stream(self._core_v1.list_namespaced_pod, namespace) as stream:
            async for event in stream:
                yield (
                    str(event["type"]),
                    PodSummary.from_manifest(event["raw_object"]),
                )

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
