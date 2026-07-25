"""Read-only and UI-control tool definitions and executor (spec §5, §6)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

import yaml

from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError

MAX_RESULT_CHARS = 8000

_TRUNCATION_SUFFIX = "\n… [truncated — narrow the query]"


def cap_result(result: str) -> str:
    """Enforce the tool-result ingest cap; shared by every path that feeds
    a result into conversation history."""
    if len(result) > MAX_RESULT_CHARS:
        return result[:MAX_RESULT_CHARS] + _TRUNCATION_SUFFIX
    return result


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


class UIBridge(ABC):
    """Screen-control surface the agent may drive (spec §4.1 UI Bus).

    Layer-boundary interface (AGENTS.md: `abc.ABC`); the concrete adapter
    lives in the ui layer and is injected at the composition root, so the
    agent layer never imports ui. Every method returns a short human/model-
    readable confirmation, or an "ERROR: …" string — implementations must
    not raise.
    """

    @abstractmethod
    async def agent_navigate(self, view: str, namespace: str | None = None) -> str: ...

    @abstractmethod
    async def agent_set_filter(self, pattern: str) -> str: ...

    @abstractmethod
    async def agent_open_logs(
        self, pod: str, namespace: str, container: str | None = None
    ) -> str: ...

    @abstractmethod
    async def agent_open_describe(
        self, kind: str, name: str, namespace: str | None = None
    ) -> str: ...


UI_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": (
                "Switch the korvid main table to a resource view the user can see, "
                "optionally scoping to a namespace. Screen-only; changes nothing "
                "in the cluster."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "view": {
                        "type": "string",
                        "description": "Resource kind or alias to display (e.g. 'pods', 'deploy').",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Namespace scope. Omit to keep the current scope.",
                    },
                },
                "required": ["view"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_filter",
            "description": (
                "Apply a case-insensitive substring filter to the visible resource "
                "table so the user sees only rows whose name contains the pattern. "
                "Not a regex. Pass an empty pattern to clear."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Case-insensitive substring to match against resource "
                            "names; '' clears the filter."
                        ),
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_logs",
            "description": (
                "Open the live log pane for a pod so the user can watch the logs "
                "on screen alongside your analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pod": {"type": "string", "description": "Pod name."},
                    "namespace": {"type": "string", "description": "Pod namespace."},
                    "container": {
                        "type": "string",
                        "description": "Container name. Omit to show all containers.",
                    },
                },
                "required": ["pod", "namespace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_describe",
            "description": (
                "Open the describe screen (manifest + events) for a resource so the "
                "user sees the evidence you are citing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "Resource kind or alias."},
                    "name": {"type": "string", "description": "Resource name."},
                    "namespace": {
                        "type": "string",
                        "description": "Namespace (required for namespaced resources).",
                    },
                },
                "required": ["kind", "name"],
            },
        },
    },
]

UI_TOOL_NAMES = frozenset(t["function"]["name"] for t in UI_TOOLS)


class ToolExecutor:
    """Dispatches OpenAI tool calls to the Kubernetes client or the UI bridge."""

    def __init__(
        self,
        kube: KubeClient,
        aliases: Mapping[str, ResourceMeta],
        ui: UIBridge | None = None,
    ) -> None:
        self._kube = kube
        self._aliases = aliases
        self._ui = ui

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Dispatch a tool call; never raises — exceptions are returned as 'ERROR: ...'."""
        try:
            result = await self._dispatch(name, arguments)
        except Exception as exc:
            # Errors flow through the same cap below: a client error with a
            # long reason must not bypass the ingest limit.
            result = f"ERROR: {exc}"
        return cap_result(result)

    async def _dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        if name in UI_TOOL_NAMES:
            return await self._dispatch_ui(name, arguments)
        if name == "list_resources":
            return await self._list_resources(arguments)
        if name == "get_resource":
            return await self._get_resource(arguments)
        if name == "get_logs":
            return await self._get_logs(arguments)
        if name == "get_events":
            return await self._get_events(arguments)
        raise ValueError(f"unknown tool: {name!r}")

    async def _dispatch_ui(self, name: str, args: dict[str, Any]) -> str:
        if self._ui is None:
            raise ValueError("UI control unavailable in this session")
        if name == "navigate":
            return await self._ui.agent_navigate(str(args["view"]), args.get("namespace"))
        if name == "set_filter":
            return await self._ui.agent_set_filter(str(args["pattern"]))
        if name == "open_logs":
            return await self._ui.agent_open_logs(
                str(args["pod"]), str(args["namespace"]), args.get("container")
            )
        return await self._ui.agent_open_describe(
            str(args["kind"]), str(args["name"]), args.get("namespace")
        )

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
        # A namespaced kind without a namespace would hit an invalid
        # cluster-scoped path — give the model an actionable error instead.
        if meta.namespaced and not namespace:
            raise ValueError(f"kind {kind!r} is namespaced — provide the 'namespace' argument")
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
        # kubectl's client-side apply stores the full original manifest —
        # including unmasked data/stringData — in this annotation.
        if isinstance(meta, dict):
            annotations = meta.get("annotations")
            if isinstance(annotations, dict):
                annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
        for field in ("data", "stringData"):
            section = manifest.get(field)
            if isinstance(section, dict):
                for key in section:
                    section[key] = "***MASKED***"
