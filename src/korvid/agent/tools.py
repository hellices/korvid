"""Read-only tool definitions and executor for the agent runtime (spec §5)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml

from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError

MAX_RESULT_CHARS = 8000

_TRUNCATION_SUFFIX = "\n… [truncated — narrow the query]"

READ_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_resources",
            "description": "List all resources of a given kind, optionally filtered by namespace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": (
                            "Resource kind or alias (e.g. 'pods', 'deployments', 'svc')."
                        ),
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Kubernetes namespace. Omit for all namespaces.",
                    },
                },
                "required": ["kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource",
            "description": "Fetch the full YAML manifest for a single Kubernetes resource.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "Resource kind or alias.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Resource name.",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Kubernetes namespace (required for namespaced resources).",
                    },
                },
                "required": ["kind", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_logs",
            "description": "Retrieve recent log lines from a pod container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pod": {
                        "type": "string",
                        "description": "Pod name.",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Kubernetes namespace the pod lives in.",
                    },
                    "container": {
                        "type": "string",
                        "description": "Container name. Defaults to the first container.",
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "Number of log lines to fetch (1-500, default 100).",
                    },
                },
                "required": ["pod", "namespace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_events",
            "description": "List Kubernetes events for a specific resource.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "Resource kind, e.g. 'pods' or 'deployment'.",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Kubernetes namespace.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Name of the resource whose events to fetch.",
                    },
                },
                "required": ["kind", "namespace", "name"],
            },
        },
    },
]


class ToolExecutor:
    """Dispatches OpenAI tool calls to the Kubernetes client."""

    def __init__(self, kube: KubeClient, aliases: Mapping[str, ResourceMeta]) -> None:
        self._kube = kube
        self._aliases = aliases

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Dispatch a tool call; never raises — exceptions are returned as 'ERROR: ...'."""
        try:
            result = await self._dispatch(name, arguments)
        except Exception as exc:
            return f"ERROR: {exc}"
        if len(result) > MAX_RESULT_CHARS:
            return result[:MAX_RESULT_CHARS] + _TRUNCATION_SUFFIX
        return result

    async def _dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "list_resources":
            return await self._list_resources(arguments)
        if name == "get_resource":
            return await self._get_resource(arguments)
        if name == "get_logs":
            return await self._get_logs(arguments)
        if name == "get_events":
            return await self._get_events(arguments)
        raise ValueError(f"unknown tool: {name!r}")

    async def _list_resources(self, args: dict[str, Any]) -> str:
        kind = str(args["kind"]).strip().lower()
        namespace: str | None = args.get("namespace")
        if kind not in self._aliases:
            raise ValueError(f"unknown kind {kind!r}")
        meta = self._aliases[kind]
        summaries = await self._kube.list_objects(meta, namespace)
        if not summaries:
            return "(none)"
        return "\n".join(f"{s.namespace}/{s.name}  -  age={s.age()}" for s in summaries)

    async def _get_resource(self, args: dict[str, Any]) -> str:
        kind = str(args["kind"]).strip().lower()
        name = str(args["name"])
        namespace: str | None = args.get("namespace")
        if kind not in self._aliases:
            raise ValueError(f"unknown kind {kind!r}")
        meta = self._aliases[kind]
        manifest = await self._kube.get_object(meta, namespace, name)
        _mask_manifest(manifest)
        return yaml.safe_dump(manifest, default_flow_style=False, allow_unicode=True)

    async def _get_logs(self, args: dict[str, Any]) -> str:
        pod = str(args["pod"])
        namespace = str(args["namespace"])
        container: str = str(args.get("container") or "")
        raw_tail = args.get("tail_lines", 100)
        tail_lines = max(1, min(500, int(raw_tail)))

        if not container:
            pods_meta = self._aliases.get("pods") or self._aliases.get("pod")
            if pods_meta is not None:
                pod_manifest = await self._kube.get_object(pods_meta, namespace, pod)
                first_container = ((pod_manifest.get("spec") or {}).get("containers") or [{}])[
                    0
                ].get("name")
                if first_container:
                    container = str(first_container)

        lines: list[str] = []
        async for log_line in self._kube.stream_logs(
            namespace, pod, container, follow=False, tail_lines=tail_lines
        ):
            lines.append(log_line.text)
        return "\n".join(lines)

    async def _get_events(self, args: dict[str, Any]) -> str:
        kind = str(args["kind"]).strip().lower()
        namespace = str(args["namespace"])
        name = str(args["name"])
        if kind not in self._aliases:
            raise ValueError(f"unknown kind {kind!r}")
        meta = self._aliases[kind]
        # Fetch the live object so events are scoped to this exact incarnation
        # (kind + UID), not merely anything sharing the name.
        uid: str | None = None
        try:
            manifest = await self._kube.get_object(meta, namespace, name)
        except ApiStatusError as exc:
            # Only 404 proves the object is gone (fall back to kind+name
            # scope); any other failure propagates as an ERROR: tool result.
            if exc.status != 404:
                raise
            manifest = None
        if manifest is not None:
            raw_uid = (manifest.get("metadata") or {}).get("uid")
            uid = str(raw_uid) if raw_uid else None
        events = await self._kube.list_events_for(namespace, name, kind=meta.kind, uid=uid)
        if not events:
            return "(no events)"
        parts: list[str] = []
        for ev in events:
            ev_type = str(ev.get("type") or "")
            reason = str(ev.get("reason") or "")
            count = int(ev.get("count") or 1)
            message = str(ev.get("message") or "")
            parts.append(f"{ev_type} {reason} ({count}x): {message}")
        return "\n".join(parts)


def _mask_manifest(manifest: dict[str, Any]) -> None:
    """Mutate manifest in-place: strip managedFields for all kinds; mask Secret data."""
    meta = manifest.get("metadata")
    if isinstance(meta, dict):
        meta.pop("managedFields", None)
    if manifest.get("kind") == "Secret":
        for field in ("data", "stringData"):
            section = manifest.get(field)
            if isinstance(section, dict):
                for key in section:
                    section[key] = "***MASKED***"
