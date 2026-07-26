"""Read-only and UI-control tool definitions and executor (spec §5, §6)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

import yaml

from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import parse_quantity

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

    @abstractmethod
    async def agent_drill_down(self, name: str) -> str: ...

    @abstractmethod
    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        """Request an approval-gated cluster write (spec §6.2).

        The implementation must open a confirmation dialog that only the
        *user's* keystroke can approve — the agent can neither open-and-confirm
        nor bypass it. Returns the outcome (executed / denied / ERROR).
        """
        ...


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
                        "description": (
                            "Namespace scope. Pass 'all' for the all-namespaces "
                            "scope. Omit to keep the current scope."
                        ),
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
                "on screen alongside your analysis. At most 8 container panels "
                "fit on screen; the result reports which containers are shown "
                "if the pod has more."
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
    {
        "type": "function",
        "function": {
            "name": "drill_down",
            "description": (
                "Drill into a row of the visible table following the ownership "
                "chain the user sees on screen: a deployment opens its replicaset "
                "revision history, a replicaset opens its pods. Screen-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the row in the current view to drill into.",
                    },
                },
                "required": ["name"],
            },
        },
    },
]

UI_TOOL_NAMES = frozenset(t["function"]["name"] for t in UI_TOOLS)

#: Cluster mutations (spec §6.2). Every call routes through
#: UIBridge.agent_request_write, which shows the user an approval dialog;
#: the tool result reports whether the user approved and what happened.
WRITE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "delete_resource",
            "description": (
                "Request deletion of a resource. This does NOT delete anything "
                "directly: it opens an approval dialog in the TUI and the "
                "operation runs only if the user approves it with a keystroke."
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
    {
        "type": "function",
        "function": {
            "name": "scale_resource",
            "description": (
                "Request scaling a deployment/replicaset/statefulset to a replica "
                "count. Runs only after the user approves the request in the TUI "
                "approval dialog."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "Resource kind or alias."},
                    "name": {"type": "string", "description": "Resource name."},
                    "namespace": {
                        "type": "string",
                        "description": "Namespace of the workload.",
                    },
                    "replicas": {
                        "type": "integer",
                        "description": "Desired replica count (>= 0).",
                    },
                },
                # scalable kinds are all namespaced apps/* workloads, so a
                # call without a namespace can never succeed
                "required": ["kind", "name", "namespace", "replicas"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rollout_restart",
            "description": (
                "Request a rolling restart of a deployment/statefulset/daemonset. "
                "Runs only after the user approves the request in the TUI "
                "approval dialog."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "Resource kind or alias."},
                    "name": {"type": "string", "description": "Resource name."},
                    "namespace": {
                        "type": "string",
                        "description": "Namespace of the workload.",
                    },
                },
                # restartable kinds are all namespaced apps/* workloads, so a
                # call without a namespace can never succeed
                "required": ["kind", "name", "namespace"],
            },
        },
    },
]

#: In-place pod resize (issue #27), kept out of WRITE_TOOLS so the
#: composition root registers it only when discovery found the pods/resize
#: subresource (1.35 GA) - the model is never told about a tool the cluster
#: cannot honor. Dispatch below still recognizes it unconditionally: an
#: unregistered tool call fails in the UI gate, not with "unknown tool".
RESIZE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "resize_pod",
            "description": (
                "Request an in-place resize of a running pod's CPU/memory "
                "requests and limits without recreating the pod (Kubernetes "
                "1.35+; containers whose resizePolicy is RestartContainer "
                "are restarted in place). Runs only after the user approves "
                "the request in the TUI approval dialog."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Pod name."},
                    "namespace": {"type": "string", "description": "Namespace of the pod."},
                    "resources": {
                        "type": "object",
                        "description": (
                            "Container name -> {'requests'/'limits' -> "
                            "{'cpu'/'memory' -> quantity}}. Only the "
                            "quantities present are changed, e.g. "
                            '{"app": {"requests": {"cpu": "200m"}}}.'
                        ),
                    },
                },
                "required": ["name", "namespace", "resources"],
            },
        },
    },
]

WRITE_TOOL_NAMES = frozenset(t["function"]["name"] for t in WRITE_TOOLS + RESIZE_TOOLS)

#: tool name -> action keyword passed to UIBridge.agent_request_write.
_WRITE_ACTIONS = {
    "delete_resource": "delete",
    "scale_resource": "scale",
    "rollout_restart": "rollout_restart",
    "resize_pod": "resize",
}


def _positive_quantity(amount: str) -> bool:
    """Positive Kubernetes quantity (zero rejected: in a resize it means an
    accidental request removal, which belongs to a manifest edit)."""
    try:
        return parse_quantity(amount) > 0
    except ValueError:
        return False


def _validated_resources(value: Any) -> dict[str, dict[str, dict[str, str]]]:
    """Shape-check a resize 'resources' argument (container -> requests/limits
    -> quantity). Tool schemas are not runtime validation; a malformed value
    must fail here, before the user is shown an approval dialog for it."""
    if not isinstance(value, dict) or not value:
        raise ValueError(f"'resources' must be a non-empty object, got {value!r}")
    for container, sections in value.items():
        if not isinstance(container, str) or not isinstance(sections, dict) or not sections:
            raise ValueError(f"invalid resources entry for {container!r}: {sections!r}")
        for section, quantities in sections.items():
            if section not in ("requests", "limits"):
                raise ValueError(f"'resources' sections must be requests/limits, got {section!r}")
            if not isinstance(quantities, dict) or not quantities:
                raise ValueError(f"invalid {section!r} for {container!r}: {quantities!r}")
            for quantity, amount in quantities.items():
                if quantity not in ("cpu", "memory") or not isinstance(amount, str):
                    raise ValueError(f"invalid quantity {quantity!r}={amount!r} for {container!r}")
                if not _positive_quantity(amount):
                    # Same grammar the prompt enforces: a malformed or
                    # non-positive amount must fail here, not in an approval
                    # dialog for a request the apiserver is guaranteed to
                    # reject (previews deliberately degrade to no preview).
                    raise ValueError(
                        f"{container}.{section}.{quantity}: {amount!r} is not a "
                        "positive quantity (e.g. 250m, 512Mi)"
                    )
    return value


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
        if name in WRITE_TOOL_NAMES:
            return await self._dispatch_write(name, arguments)
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
        if name == "drill_down":
            return await self._ui.agent_drill_down(str(args["name"]))
        return await self._ui.agent_open_describe(
            str(args["kind"]), str(args["name"]), args.get("namespace")
        )

    async def _dispatch_write(self, name: str, args: dict[str, Any]) -> str:
        if self._ui is None:
            raise ValueError("write actions require the interactive TUI session")
        # Tool schemas are not runtime validation: reject wrong-typed values
        # instead of coercing them (str(123) would show the user a target the
        # model never named; int(1.9) an operation it never asked for).
        # resize_pod targets pods by definition, so its schema has no 'kind'.
        kind = "pods" if name == "resize_pod" else args.get("kind")
        target = args.get("name")
        namespace = args.get("namespace")
        if not isinstance(kind, str):
            raise ValueError(f"'kind' must be a string, got {kind!r}")
        if not isinstance(target, str):
            raise ValueError(f"'name' must be a string, got {target!r}")
        if namespace is not None and not isinstance(namespace, str):
            raise ValueError(f"'namespace' must be a string, got {namespace!r}")
        replicas = args.get("replicas")
        if replicas is not None and (isinstance(replicas, bool) or not isinstance(replicas, int)):
            raise ValueError(f"'replicas' must be an integer, got {replicas!r}")
        resources = args.get("resources")
        if name == "resize_pod":
            resources = _validated_resources(resources)
        return await self._ui.agent_request_write(
            _WRITE_ACTIONS[name],
            kind,
            target,
            namespace,
            replicas,
            resources,
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
