"""Read-only and UI-control tool definitions and executor (spec §5, §6)."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

import yaml

from korvid.agent.diagnose import (
    condition_lines,
    container_state_lines,
    identity_lines,
    log_excerpt,
    node_condition_line,
    previous_log_containers,
    pvc_names,
    troubled_containers,
    warning_event_lines,
)
from korvid.core.portforward import controller_owner
from korvid.core.secrets import mask_secret_manifest
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import parse_quantity
from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP, resolve_olm_meta

MAX_RESULT_CHARS = 8000

#: OperatorHub catalogs commonly serve hundreds of packages; keep the
#: catalog listing well under the shared result cap so the installed
#: section is never sacrificed to it.
_MAX_CATALOG_PACKAGES = 60

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
    {
        "type": "function",
        "function": {
            "name": "list_operators",
            "description": (
                "List OLM operators: available packages from the cluster's"
                " operator catalog plus installed subscriptions with their"
                " status. Read-only; installing an operator is done by the"
                " user through the UI. Explains itself when OLM is absent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": (
                            "Namespace to scope installed subscriptions to."
                            " Omit for all namespaces."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diagnose_pod",
            "description": (
                "Compound diagnostic for a broken pod: one call gathers the"
                " pod's identity and owner chain, per-container states"
                " (waiting reasons, exit codes, restart counts), failing"
                " conditions, recent Warning events, node and PVC context,"
                " and a targeted log excerpt from up to 3 of the troubled"
                " containers (any further troubled containers are named but"
                " their logs are not fetched; use get_logs for those)."
                " Prefer this over chaining"
                " get_resource/get_events/get_logs when investigating why a"
                " pod is failing."
            ),
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
                },
                "required": ["pod", "namespace"],
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
                "Apply a filter expression to the visible resource table. "
                "Space-separated tokens are AND-combined: plain text is a "
                "case-insensitive substring on the resource name; '~pat' is a "
                "fuzzy subsequence match; '/pat/' or 're:pat' is a regex; "
                "'!' before any name token negates it; '-l key=value[,k2=v2]' "
                "is a label selector (a bare key tests existence); '-s' hides "
                "Succeeded/Completed pods. Pass an empty pattern to clear."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Filter expression (grammar in the tool "
                            "description); '' clears the filter."
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
                "revision history, a replicaset opens its pods, and a helm "
                "release opens its revision history. Screen-only."
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


#: RFC 1123 DNS label: lowercase alphanumerics and hyphens, alphanumeric
#: endpoints, at most 63 characters - the grammar container names must match.
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def _positive_quantity(amount: str) -> bool:
    """Positive Kubernetes quantity (zero rejected: in a resize it means an
    accidental request removal, which belongs to a manifest edit)."""
    try:
        return parse_quantity(amount) > 0
    except ValueError:
        return False


def _validated_sections(container: str, sections: Any) -> dict[str, dict[str, str]]:
    """Validate one container's requests/limits mapping and return it with
    whitespace-normalized amounts (see `_validated_resources`)."""
    if not isinstance(sections, dict) or not sections:
        raise ValueError(f"invalid resources entry for {container!r}: {sections!r}")
    validated: dict[str, dict[str, str]] = {}
    for section, quantities in sections.items():
        if section not in ("requests", "limits"):
            raise ValueError(f"'resources' sections must be requests/limits, got {section!r}")
        if not isinstance(quantities, dict) or not quantities:
            raise ValueError(f"invalid {section!r} for {container!r}: {quantities!r}")
        validated[section] = {}
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
            # parse_quantity strips whitespace but the apiserver does not:
            # a padded amount must be normalized before it is forwarded.
            validated[section][quantity] = amount.strip()
    return validated


def _validated_resources(value: Any) -> dict[str, dict[str, dict[str, str]]]:
    """Shape-check a resize 'resources' argument (container -> requests/limits
    -> quantity) and return a copy with whitespace-normalized amounts. Tool
    schemas are not runtime validation; a malformed value must fail here,
    before the user is shown an approval dialog for it."""
    if not isinstance(value, dict) or not value:
        raise ValueError(f"'resources' must be a non-empty object, got {value!r}")
    validated: dict[str, dict[str, dict[str, str]]] = {}
    for container, sections in value.items():
        if not isinstance(container, str) or not container.strip():
            raise ValueError(f"container name must be a non-empty string, got {container!r}")
        # Normalize padded keys the same way amounts are normalized, then
        # require the DNS label grammar container names must follow - an
        # invalid name produces a patch the apiserver must reject, and it
        # has to fail here, not after an approval dialog. Two keys
        # collapsing to one name must not silently drop a change.
        key = container.strip()
        if not _DNS_LABEL_RE.match(key):
            raise ValueError(
                f"invalid container name {key!r}: must be a lowercase DNS "
                "label (alphanumerics and hyphens, at most 63 characters)"
            )
        if key in validated:
            raise ValueError(f"duplicate container {key!r} in 'resources'")
        validated[key] = _validated_sections(container, sections)
    return validated


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
        if name == "list_operators":
            return await self._list_operators(arguments)
        if name == "diagnose_pod":
            return await self._diagnose_pod(arguments)
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

    def _api_meta(self, kind: str) -> ResourceMeta:
        """Alias lookup for tools that build API paths: synthetic view kinds
        (helm browser) have no endpoint and must be rejected here, not turned
        into a nonexistent ``/api/v1/helmreleases`` request."""
        meta = self._aliases.get(kind)
        if meta is None:
            raise ValueError(f"unknown kind {kind!r}")
        if meta.synthetic:
            raise ValueError(f"kind {kind!r} is a korvid view, not an API resource")
        return meta

    async def _list_resources(self, args: dict[str, Any]) -> str:
        kind = str(args["kind"]).strip().lower()
        namespace: str | None = args.get("namespace")
        meta = self._api_meta(kind)
        summaries = await self._kube.list_objects(meta, namespace)
        if not summaries:
            return "(none)"
        return "\n".join(f"{s.namespace}/{s.name}  -  age={s.age()}" for s in summaries)

    async def _list_operators(self, args: dict[str, Any]) -> str:
        """Catalog packages + installed subscriptions, straight from the
        cluster's own OLM objects (issue #29: no hardcoded operator
        knowledge; the tool explains itself when OLM is absent)."""
        pkg_meta = resolve_olm_meta(self._aliases, "packagemanifests", PACKAGES_GROUP)
        sub_meta = resolve_olm_meta(self._aliases, "subscriptions", OPERATORS_GROUP)
        if pkg_meta is None and sub_meta is None:
            return (
                "OLM was not detected: neither packages.operators.coreos.com"
                " nor operators.coreos.com API groups were discovered (OLM"
                " may be absent, or discovery may still be running), so"
                " there are no operators to list."
            )
        namespace: str | None = args.get("namespace")
        lines: list[str] = []
        # Installed state first: it is what the user most likely asked
        # about, and a large catalog must not push it past the result cap.
        if sub_meta is not None:
            lines.append("INSTALLED (subscriptions):")
            subs = await self._kube.list_objects(sub_meta, namespace)
            if not subs:
                lines.append("  (none)")
            for sub in sorted(subs, key=lambda s: (s.namespace, s.name)):
                lines.append(
                    f"  {sub.namespace}/{sub.name}"
                    f"  channel={getattr(sub, 'channel', '') or '?'}"
                    f"  csv={getattr(sub, 'installed_csv', '') or '?'}"
                    f"  state={getattr(sub, 'state', '') or '?'}"
                )
        else:
            lines.append(
                "INSTALLED (subscriptions): unavailable -"
                " the operators.coreos.com API group was not discovered"
            )
        if pkg_meta is None:
            lines.append(
                "AVAILABLE (operator catalog): unavailable -"
                " the packages.operators.coreos.com API group was not"
                " discovered (the package server may be down or hidden)"
            )
            return "\n".join(lines)
        lines.append("AVAILABLE (operator catalog):")
        packages = sorted(await self._kube.list_objects(pkg_meta, None), key=lambda p: p.name)
        # OperatorHub catalogs commonly serve hundreds of packages; cap the
        # listing so the tool result stays within the shared result budget.
        shown = packages[:_MAX_CATALOG_PACKAGES]
        for pkg in shown:
            channels = ",".join(getattr(pkg, "channels", ()) or ())
            lines.append(
                f"  {pkg.name}  catalog={getattr(pkg, 'catalog', '') or '?'}"
                f"  default={getattr(pkg, 'default_channel', '') or '?'}"
                f"  channels={channels or '?'}"
            )
        if len(packages) > len(shown):
            lines.append(f"  ...and {len(packages) - len(shown)} more catalog packages")
        return "\n".join(lines)

    async def _get_resource(self, args: dict[str, Any]) -> str:
        kind = str(args["kind"]).strip().lower()
        name = str(args["name"])
        namespace: str | None = args.get("namespace")
        meta = self._api_meta(kind)
        # A namespaced kind without a namespace would hit an invalid
        # cluster-scoped path — give the model an actionable error instead.
        if meta.namespaced and not namespace:
            raise ValueError(f"kind {kind!r} is namespaced — provide the 'namespace' argument")
        manifest = await self._kube.get_object(meta, namespace, name)
        manifest = _mask_manifest(manifest)
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
        meta = self._api_meta(kind)
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

    #: Log lines fetched per troubled container before excerpting.
    _DIAGNOSE_LOG_TAIL = 200
    #: Troubled containers whose logs are excerpted; more are named only.
    _DIAGNOSE_MAX_LOG_CONTAINERS = 3
    #: Mounted PVCs whose phase is fetched; more are named only.
    _DIAGNOSE_MAX_PVCS = 5
    #: Per-line clamp — event/condition messages and log lines are
    #: cluster-controlled and unbounded.
    _DIAGNOSE_LINE_CLAMP = 240
    #: Per-section budget for the non-log sections, so the final LOG
    #: EXCERPTS section always has reserved room under MAX_RESULT_CHARS.
    _DIAGNOSE_SECTION_BUDGET = 1000
    #: Stable built-in APIs the diagnosis relies on — used as fallbacks so
    #: the related evidence never depends on background API discovery
    #: having populated the alias table.
    _DIAGNOSE_BUILTIN_METAS: ClassVar[dict[str, ResourceMeta]] = {
        "ReplicaSet": ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True),
        "Node": ResourceMeta("Node", "nodes", "", "v1", False),
        "PersistentVolumeClaim": ResourceMeta(
            "PersistentVolumeClaim", "persistentvolumeclaims", "", "v1", True
        ),
    }

    def _meta_for_kind_name(self, kind_name: str) -> ResourceMeta | None:
        """Discovery metadata for an API kind name (e.g. ``"ReplicaSet"``),
        falling back to fixed metadata for the stable built-in kinds."""
        discovered = next(
            (m for m in self._aliases.values() if m.kind == kind_name and not m.synthetic),
            None,
        )
        return discovered or self._DIAGNOSE_BUILTIN_METAS.get(kind_name)

    async def _diagnose_owner_chain(self, namespace: str, pod: dict[str, Any]) -> str:
        """``Deployment api (via ReplicaSet api-6f)`` — best-effort, never raises."""
        owner = controller_owner(pod)
        if owner is None:
            return "owner: none (standalone pod)"
        kind_name, name = owner
        if kind_name != "ReplicaSet":
            return f"owner: {kind_name} {name}"
        # One more hop: a ReplicaSet is usually a Deployment's generation.
        meta = self._meta_for_kind_name(kind_name)
        if meta is None:
            return f"owner: {kind_name} {name}"
        try:
            parent = controller_owner(await self._kube.get_object(meta, namespace, name))
        except Exception as exc:  # the direct owner stands, but say why the hop failed
            return f"owner: {kind_name} {name} (parent lookup unavailable ({exc}))"
        if parent is None:
            return f"owner: {kind_name} {name}"
        return f"owner: {parent[0]} {parent[1]} (via {kind_name} {name})"

    async def _diagnose_related(self, namespace: str, pod: dict[str, Any]) -> list[str]:
        """Node condition summary and PVC phases — cheap context, best-effort."""
        lines: list[str] = []
        node_name = (pod.get("spec") or {}).get("nodeName")
        node_meta = self._meta_for_kind_name("Node")
        if node_name and node_meta is not None:
            try:
                node = await self._kube.get_object(node_meta, None, str(node_name))
                lines.append(node_condition_line(node))
            except Exception as exc:
                lines.append(f"node {node_name}: unavailable ({exc})")
        pvc_meta = self._meta_for_kind_name("PersistentVolumeClaim")
        if pvc_meta is not None:
            claims = pvc_names(pod)
            for claim in claims[: self._DIAGNOSE_MAX_PVCS]:
                try:
                    pvc = await self._kube.get_object(pvc_meta, namespace, claim)
                    phase = (pvc.get("status") or {}).get("phase") or "?"
                    lines.append(f"pvc {claim}: {phase}")
                except Exception as exc:
                    lines.append(f"pvc {claim}: unavailable ({exc})")
            omitted = claims[self._DIAGNOSE_MAX_PVCS :]
            if omitted:
                lines.append(f"({len(omitted)} more claims not fetched: {', '.join(omitted)})")
        return lines

    async def _diagnose_events(self, namespace: str, name: str, pod: dict[str, Any]) -> list[str]:
        raw_uid = (pod.get("metadata") or {}).get("uid")
        try:
            events = await self._kube.list_events_for(
                namespace, name, kind="Pod", uid=str(raw_uid) if raw_uid else None
            )
        except Exception as exc:
            return [f"unavailable ({exc})"]
        return warning_event_lines(events) or ["(no warning events)"]

    async def _fetch_log_excerpt(
        self, namespace: str, pod_name: str, container: str, *, previous: bool
    ) -> tuple[bool, str]:
        """(succeeded, excerpt-or-diagnostic) for one container instance."""
        try:
            tail: list[str] = []
            async for log_line in self._kube.stream_logs(
                namespace,
                pod_name,
                container,
                previous=previous,
                follow=False,
                tail_lines=self._DIAGNOSE_LOG_TAIL,
            ):
                tail.append(log_line.text)
        except Exception as exc:
            return False, f"unavailable ({exc})"
        if not tail:
            return False, "(no log output)"
        # Search the raw lines — clamping first could hide an error marker
        # buried past the clamp in a long (e.g. JSON) line. Only the lines
        # selected for the report are clamped.
        excerpt = log_excerpt(tail)
        return True, "\n".join(self._clamp_line(seg) for seg in excerpt.splitlines())

    async def _diagnose_log_blocks(
        self, namespace: str, name: str, pod: dict[str, Any]
    ) -> list[list[str]]:
        """One block (header + excerpt lines) per troubled container.

        A restarted container's crash evidence usually lives in the
        *previous* instance's logs — unless it is currently terminated
        with a non-zero exit, where the current logs hold the latest
        failure (`previous_log_containers` encodes that split). Previous
        reads fall back to current when those logs have rotated away.
        """
        troubled = troubled_containers(pod)
        if not troubled:
            return [["(no troubled containers — logs skipped)"]]
        previous_first = previous_log_containers(pod)
        blocks: list[list[str]] = []
        for container in troubled[: self._DIAGNOSE_MAX_LOG_CONTAINERS]:
            previous = container in previous_first
            ok, text = await self._fetch_log_excerpt(namespace, name, container, previous=previous)
            if previous and not ok:
                ok, text = await self._fetch_log_excerpt(namespace, name, container, previous=False)
                previous = False
            suffix = " (previous instance)" if previous else ""
            blocks.append([f"[{container}]{suffix}", *text.splitlines()])
        skipped = troubled[self._DIAGNOSE_MAX_LOG_CONTAINERS :]
        if skipped:
            blocks.append([f"(also troubled, logs not fetched: {', '.join(skipped)})"])
        return blocks

    @classmethod
    def _clamp_line(cls, line: str) -> str:
        limit = cls._DIAGNOSE_LINE_CLAMP
        return line if len(line) <= limit else line[: limit - 1] + "…"

    @classmethod
    def _budget_section(cls, lines: list[str]) -> list[str]:
        """Keep leading lines within the per-section budget, eliding the rest."""
        out: list[str] = []
        used = 0
        for index, line in enumerate(lines):
            used += len(line) + 1
            if used > cls._DIAGNOSE_SECTION_BUDGET:
                out.append(f"…{len(lines) - index} more line(s) elided")
                return out
            out.append(line)
        return out

    @staticmethod
    def _trim_front(lines: list[str], budget: int) -> list[str]:
        """Drop leading lines until the joined text fits the budget.

        The tail is the most recent — and most diagnostic — log evidence,
        so overflow is cut from the front, never from the end, and the
        cut is marked visibly.
        """
        marker = "  … (earlier lines elided)"
        total = sum(len(line) + 1 for line in lines)
        if total <= budget:
            return lines
        trimmed = list(lines)
        while len(trimmed) > 1 and total + len(marker) + 1 > budget:
            total -= len(trimmed[0]) + 1
            trimmed.pop(0)
        return [marker, *trimmed]

    def _render_log_blocks(self, blocks: list[list[str]], budget: int) -> list[str]:
        """Render container blocks within the budget, trimming *within*
        each block — one container's huge excerpt must never evict another
        container's header or evidence."""
        share = max(0, budget) // max(1, len(blocks))
        lines: list[str] = []
        for block in blocks:
            header = f"  {self._clamp_line(block[0])}"
            body = [f"  {segment}" for segment in block[1:]]
            lines.append(header)
            lines.extend(self._trim_front(body, share - len(header) - 1))
        return lines

    async def _diagnose_pod(self, args: dict[str, Any]) -> str:
        """Compound read-only diagnosis (issue #70).

        Evidence gathering is deterministic code; the model only interprets.
        Only the pod fetch may fail the tool — every other section degrades
        to an ``unavailable`` line. Ordered for primacy/recency: identity
        first, the most diagnostic evidence (events, then logs) last. Lines
        are clamped and sections budgeted so the report stays under
        ``MAX_RESULT_CHARS`` without the shared prefix-truncation ever
        eating the final log evidence.
        """
        name = str(args["pod"])
        namespace = str(args["namespace"])
        pods_meta = self._api_meta("pods")
        pod = await self._kube.get_object(pods_meta, namespace, name)
        head_sections: list[tuple[str, list[str]]] = [
            (
                f"IDENTITY — pod {namespace}/{name}",
                [*identity_lines(pod), await self._diagnose_owner_chain(namespace, pod)],
            ),
            ("RELATED", await self._diagnose_related(namespace, pod) or ["(none)"]),
            ("CONDITIONS (failing first)", condition_lines(pod) or ["(none reported)"]),
            ("CONTAINERS", container_state_lines(pod) or ["(no container statuses)"]),
            (
                "WARNING EVENTS (newest first)",
                await self._diagnose_events(namespace, name, pod),
            ),
        ]
        report: list[str] = []
        for title, lines in head_sections:
            report.append(title)
            clamped = [self._clamp_line(line) for line in lines]
            report.extend(f"  {line}" for line in self._budget_section(clamped))
        log_title = "LOG EXCERPTS (troubled containers)"
        blocks = await self._diagnose_log_blocks(namespace, name, pod)
        budget = MAX_RESULT_CHARS - sum(len(line) + 1 for line in report) - len(log_title) - 1
        return "\n".join([*report, log_title, *self._render_log_blocks(blocks, budget)])


def _mask_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Strip managedFields for all kinds; mask Secret data.

    Secret masking delegates to `korvid.core.secrets.mask_secret_manifest`
    so the leak filter has exactly one implementation across every
    LLM-facing path (agent tools here, the UI describe path in `app.py`).
    """
    meta = manifest.get("metadata")
    if isinstance(meta, dict):
        meta.pop("managedFields", None)
    if manifest.get("kind") == "Secret":
        return mask_secret_manifest(manifest)
    return manifest
