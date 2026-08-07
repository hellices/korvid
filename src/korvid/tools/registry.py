"""Validated tool metadata registry (issue #91 Finding A).

Single source of tool metadata. Each `ToolDef` models the policy
dimensions independently — identity (name + OpenAI function schema),
dispatch target, cluster effect, approval policy, capability gate, and
exposure surfaces — so a misplaced tool fails validation at import time
instead of silently widening a surface.

The registry deliberately does **not** store bound handler methods:
definitions are instance-independent, and dispatch resolves the validated
method name against the executor/bridge instance at call time.

External plugin loading (the documented `korvid.tool` entry-point group)
is explicitly out of scope here; plugin trust, collision, exposure, and
approval policy need their own threat model.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

Effect = Literal["cluster_read", "ui_only", "cluster_write", "write_proposal"]
Approval = Literal["none", "user_confirmation"]
Capability = Literal["none", "pod_resize"]
ResultFormat = Literal["structured_yaml", "untrusted_text"]
Surface = Literal["full_agent", "small_agent", "mcp", "mcp_proposal"]

_EFFECTS = ("cluster_read", "ui_only", "cluster_write", "write_proposal")
_APPROVALS = ("none", "user_confirmation")
_CAPABILITIES = ("none", "pod_resize")
_RESULT_FORMATS = ("structured_yaml", "untrusted_text")
_SURFACES = ("full_agent", "small_agent", "mcp", "mcp_proposal")

#: The only bridge method allowed to receive a cluster write: the
#: user-confirmation approval gate (design doc security invariant).
_WRITE_ENTRYPOINT = "agent_request_write"

#: The only bridge methods a write-proposal tool may dispatch on: proposal
#: submission/status/cancel never execute anything (issue #110) — routing a
#: proposal tool into the direct write path is rejected at import time.
_PROPOSAL_ENTRYPOINTS = frozenset(
    {
        "agent_submit_write_proposal",
        "agent_get_write_proposal",
        "agent_cancel_write_proposal",
    }
)


@dataclass(frozen=True)
class ToolDef:
    """One tool's complete metadata; validated by `validate_tool_defs`."""

    #: Unique tool name; must equal ``schema["function"]["name"]``.
    name: str
    #: OpenAI function-calling schema, ``{"type": "function", ...}``.
    schema: dict[str, Any]
    #: What the tool touches: the cluster (read/write) or only the screen.
    effect: Effect
    #: Dispatch key: a ``ToolExecutor`` method name for cluster reads, or a
    #: ``UIBridge`` method name for screen/write tools. Validated against
    #: the real classes by `validate_dispatch_targets`.
    dispatch: str
    #: Which derived surfaces offer this tool to callers.
    surfaces: frozenset[Surface]
    #: How the tool's outward result should be treated by downstream callers.
    result_format: ResultFormat
    #: Cluster writes always require the user-confirmation approval gate.
    approval: Approval = "none"
    #: Cluster capability the tool depends on; gated at surface derivation.
    capability: Capability = "none"
    #: Action keyword passed to ``UIBridge.agent_request_write``; required
    #: for (and only valid on) cluster writes.
    write_action: str | None = None


def validate_tool_defs(defs: list[ToolDef]) -> None:
    """Reject inconsistent definitions at import time (issue #91 rules).

    Raises:
        ValueError: on duplicate names, schema/name disagreement, a missing
            dispatch key, a cluster write without the user-confirmation
            approval or a write action, a write action or approval gate on
            a non-write, an unknown enum value, or an MCP-exposed write.
    """
    seen: set[str] = set()
    for d in defs:
        if d.name in seen:
            raise ValueError(f"duplicate tool name {d.name!r}")
        seen.add(d.name)
        _validate_one(d)


def _validate_one(d: ToolDef) -> None:
    schema_name = d.schema.get("function", {}).get("name")
    if d.schema.get("type") != "function" or schema_name != d.name:
        raise ValueError(f"tool {d.name!r}: schema name {schema_name!r} disagrees")
    if not d.dispatch:
        raise ValueError(f"tool {d.name!r} has no dispatch target")
    _validate_enums(d)
    _validate_write_policy(d)


def _validate_enums(d: ToolDef) -> None:
    if d.effect not in _EFFECTS:
        raise ValueError(f"tool {d.name!r}: unknown effect {d.effect!r}")
    if d.approval not in _APPROVALS:
        raise ValueError(f"tool {d.name!r}: unknown approval {d.approval!r}")
    if d.capability not in _CAPABILITIES:
        raise ValueError(f"tool {d.name!r}: unknown capability {d.capability!r}")
    if d.result_format not in _RESULT_FORMATS:
        raise ValueError(f"tool {d.name!r}: unknown result format {d.result_format!r}")
    unknown = set(d.surfaces) - set(_SURFACES)
    if unknown:
        raise ValueError(f"tool {d.name!r}: unknown surfaces {sorted(unknown)}")


def _validate_write_policy(d: ToolDef) -> None:
    if d.effect == "cluster_write":
        if d.approval != "user_confirmation":
            raise ValueError(f"cluster write {d.name!r} requires the approval gate")
        if not d.write_action:
            raise ValueError(f"cluster write {d.name!r} requires a write_action")
        if "mcp" in d.surfaces or "mcp_proposal" in d.surfaces:
            raise ValueError(f"cluster write {d.name!r} must not be exposed on mcp")
    else:
        if d.write_action is not None:
            raise ValueError(f"tool {d.name!r}: write_action is only valid on cluster writes")
        if d.approval != "none":
            raise ValueError(f"tool {d.name!r}: approval gate is only valid on cluster writes")
    if d.effect == "write_proposal" and d.surfaces != frozenset({"mcp_proposal"}):
        raise ValueError(
            f"write proposal tool {d.name!r} may only be exposed on the mcp_proposal surface"
        )
    if d.effect != "write_proposal" and "mcp_proposal" in d.surfaces:
        raise ValueError(
            f"tool {d.name!r}: the mcp_proposal surface is reserved for write proposal tools"
        )


def tool_result_format(name: str) -> ResultFormat | None:
    """The registry's result format for `name`, or None if it defines none.

    None is not a default — it means this registry has nothing to say
    about the tool, and the caller has to have been told. Returning
    "untrusted_text" here let an undeclared tool return a `Secret`
    document that took only the text pass (PR #197 review).
    """
    definition = TOOLS_BY_NAME.get(name)
    return definition.result_format if definition is not None else None


@dataclass(frozen=True, slots=True)
class CustomToolResult:
    """The result format of a tool this registry does not define.

    A caller that offers the agent its own tool has to say which of the
    two treatments its results get, because the boundary cannot tell a
    manifest from a paragraph by looking, and the wrong guess is a leak:
    a document that only gets the text pass ships every entry that is not
    spelled like a credential.

    Attributes:
        name: The tool name, exactly as its schema declares it.
        result_format: `structured_yaml` for results that are documents —
            parsed and recursively redacted — or `untrusted_text` for
            free-form output that gets pattern redaction.
    """

    name: str
    result_format: ResultFormat


def tool_schema_names(tools: Sequence[Mapping[str, Any]]) -> list[str]:
    """The tool names an OpenAI-style schema list offers, in order.

    Raises:
        ValueError: if an entry is not a function schema with a name.
    """
    names: list[str] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise ValueError("each tool schema must be a mapping")
        if tool.get("type") != "function":
            raise ValueError("each tool schema must be a function schema")
        function = tool.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
        if not isinstance(name, str) or not name:
            raise ValueError("each tool schema must name a function")
        names.append(name)
    return names


def resolve_result_formats(
    tools: Sequence[Mapping[str, Any]],
    declared: Sequence[CustomToolResult] = (),
) -> dict[str, ResultFormat]:
    """Map every offered tool to the treatment its results get.

    Registry tools resolve from the registry and cannot be redeclared —
    otherwise a caller could downgrade `get_resource` to the text pass,
    which is the hole this closes. Every other offered tool must be
    declared, exactly once, with a valid format.

    Args:
        tools: Tool schemas as the agent will offer them.
        declared: Formats for the tools this registry does not define.

    Returns:
        Tool name to result format, covering every offered tool.

    Raises:
        ValueError: on an unusable schema, a duplicate or unmatched
            declaration, an attempt to redeclare a registry tool, an
            invalid format, or an offered tool nobody declared.
    """
    offered = tool_schema_names(tools)
    duplicates = {name for name in offered if offered.count(name) > 1}
    if duplicates:
        raise ValueError(f"tool {sorted(duplicates)[0]!r}: offered more than once")

    resolved: dict[str, ResultFormat] = {}
    for item in declared:
        if item.result_format not in _RESULT_FORMATS:
            raise ValueError(f"tool {item.name!r}: unknown result format {item.result_format!r}")
        if item.name in resolved:
            raise ValueError(f"tool {item.name!r}: declared more than once")
        if item.name in TOOLS_BY_NAME:
            raise ValueError(f"tool {item.name!r}: the registry already defines its result format")
        if item.name not in offered:
            raise ValueError(f"tool {item.name!r}: declared a result format but is not offered")
        resolved[item.name] = item.result_format

    for name in offered:
        builtin = tool_result_format(name)
        if builtin is not None:
            resolved[name] = builtin
        elif name not in resolved:
            raise ValueError(
                f"tool {name!r}: result format must be declared as "
                f"structured_yaml or untrusted_text — the boundary cannot guess it"
            )
    return resolved


def validate_dispatch_targets(defs: list[ToolDef], *, executor_cls: type, bridge_cls: type) -> None:
    """Verify every dispatch key names a method on the class its effect uses.

    Cluster reads dispatch on the executor; UI-only tools dispatch on the
    UI bridge; cluster writes must dispatch on the bridge's approval-gated
    `agent_request_write` entrypoint — no other bridge method, however
    callable, may receive a write (security invariant). Called from the
    executor module at import time so a typo'd or wrong-class handler
    fails startup/tests, not a live call.

    Raises:
        ValueError: when a dispatch key does not resolve on the class
            required by the tool's effect, or a write dispatches anywhere
            but the approval-gated entrypoint.
    """
    for d in defs:
        if d.effect == "cluster_write" and d.dispatch != _WRITE_ENTRYPOINT:
            raise ValueError(
                f"tool {d.name!r} (cluster_write): dispatch target "
                f"{d.dispatch!r} bypasses the approval-gated "
                f"{_WRITE_ENTRYPOINT!r} entrypoint"
            )
        if d.effect == "write_proposal" and d.dispatch not in _PROPOSAL_ENTRYPOINTS:
            raise ValueError(
                f"tool {d.name!r} (write_proposal): dispatch target "
                f"{d.dispatch!r} is not a proposal entrypoint "
                f"({sorted(_PROPOSAL_ENTRYPOINTS)})"
            )
        if d.effect != "write_proposal" and d.dispatch in _PROPOSAL_ENTRYPOINTS:
            raise ValueError(
                f"tool {d.name!r} ({d.effect}): dispatch target "
                f"{d.dispatch!r} is a reserved proposal entrypoint — only "
                f"write_proposal tools may route there, otherwise the "
                f"proposal capability check is skipped"
            )
        if d.effect == "cluster_read":
            cls, role = executor_cls, "executor"
        else:
            cls, role = bridge_cls, "UI bridge"
        if not callable(getattr(cls, d.dispatch, None)):
            raise ValueError(
                f"tool {d.name!r} ({d.effect}): dispatch target "
                f"{d.dispatch!r} is not a method of the {role}"
            )


def agent_tool_schemas(
    surface: str, *, readonly: bool, resize_supported: bool
) -> list[dict[str, Any]]:
    """Derive one agent surface's schema list, in registry order.

    Args:
        surface: `full_agent` or `small_agent`.
        readonly: when True, write tools are omitted entirely — the model
            is never even told they exist.
        resize_supported: whether discovery found pods/resize; the resize
            tool is offered only when the cluster can honor it.

    Raises:
        ValueError: for a surface other than the two agent surfaces.
    """
    if surface not in ("full_agent", "small_agent"):
        raise ValueError(f"unknown surface {surface!r}")
    schemas: list[dict[str, Any]] = []
    for d in TOOL_DEFS:
        if surface not in d.surfaces:
            continue
        if d.effect == "cluster_write" and readonly:
            continue
        if d.capability == "pod_resize" and not resize_supported:
            continue
        # Deep copies (issue #97): providers are plugins, and the schema
        # dicts they receive ride along on every request — a caller
        # mutating one must not corrupt the registry for other surfaces.
        schemas.append(copy.deepcopy(d.schema))
    return schemas


def mcp_tool_schemas(*, write_proposals: bool = False) -> list[dict[str, Any]]:
    """The MCP exposure surface: read + UI-drive tools only.

    Direct write tools stay with the built-in agent; `validate_tool_defs`
    rejects any MCP-exposed cluster write independent of list placement.
    When `write_proposals` is enabled (issue #110), the surface adds the
    proposal submission/status/cancel tools — those never execute a write
    themselves, they queue an immutable proposal for TUI review.
    Returns deep copies so a caller mutating a schema cannot corrupt the
    registry (issue #97).
    """
    surfaces: set[Surface] = {"mcp"}
    if write_proposals:
        surfaces.add("mcp_proposal")
    return [copy.deepcopy(d.schema) for d in TOOL_DEFS if d.surfaces & surfaces]


_ALL_SURFACES: frozenset[Surface] = frozenset({"full_agent", "small_agent", "mcp"})
_FULL_AND_MCP: frozenset[Surface] = frozenset({"full_agent", "mcp"})
_AGENT_SURFACES: frozenset[Surface] = frozenset({"full_agent", "small_agent"})


TOOL_DEFS: list[ToolDef] = [
    ToolDef(
        name="list_resources",
        effect="cluster_read",
        dispatch="_list_resources",
        surfaces=_ALL_SURFACES,
        result_format="untrusted_text",
        schema={
            "type": "function",
            "function": {
                "name": "list_resources",
                "description": (
                    "List all resources of a given kind, optionally filtered by namespace."
                ),
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
    ),
    ToolDef(
        name="get_resource",
        effect="cluster_read",
        dispatch="_get_resource",
        surfaces=_ALL_SURFACES,
        result_format="structured_yaml",
        schema={
            "type": "function",
            "function": {
                "name": "get_resource",
                "description": ("Fetch the full YAML manifest for a single Kubernetes resource."),
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
                            "description": (
                                "Kubernetes namespace (required for namespaced resources)."
                            ),
                        },
                    },
                    "required": ["kind", "name"],
                },
            },
        },
    ),
    ToolDef(
        name="get_logs",
        effect="cluster_read",
        dispatch="_get_logs",
        surfaces=_ALL_SURFACES,
        result_format="untrusted_text",
        schema={
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
    ),
    ToolDef(
        name="get_events",
        effect="cluster_read",
        dispatch="_get_events",
        surfaces=_ALL_SURFACES,
        result_format="untrusted_text",
        schema={
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
    ),
    ToolDef(
        name="list_operators",
        effect="cluster_read",
        dispatch="_list_operators",
        surfaces=_ALL_SURFACES,
        result_format="untrusted_text",
        schema={
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
    ),
    ToolDef(
        name="helm_list_releases",
        effect="cluster_read",
        dispatch="_helm_list_releases",
        surfaces=_ALL_SURFACES,
        result_format="untrusted_text",
        schema={
            "type": "function",
            "function": {
                "name": "helm_list_releases",
                "description": (
                    "List installed Helm releases with their status: one line"
                    " per release - revision, status (deployed/failed/"
                    "pending-…), chart and app version. Read-only, parsed"
                    " from the cluster's own release Secrets (no helm binary"
                    " involved); installing or upgrading a release is done by"
                    " the user through the UI."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "namespace": {
                            "type": "string",
                            "description": (
                                "Namespace to scope releases to. Omit for all namespaces."
                            ),
                        },
                    },
                    "required": [],
                },
            },
        },
    ),
    ToolDef(
        name="diagnose_pod",
        effect="cluster_read",
        dispatch="_diagnose_pod",
        surfaces=_ALL_SURFACES,
        result_format="untrusted_text",
        schema={
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
    ),
    ToolDef(
        name="diagnose_workload",
        effect="cluster_read",
        dispatch="_diagnose_workload",
        surfaces=_ALL_SURFACES,
        result_format="untrusted_text",
        schema={
            "type": "function",
            "function": {
                "name": "diagnose_workload",
                "description": (
                    "Compound rollout diagnostic for a Deployment. One call"
                    " gathers workload conditions and Warning events, follows"
                    " ownership through ReplicaSets to non-ready pods, and"
                    " embeds compact pod diagnoses with container states,"
                    " events, and logs. Prefer this over manually chaining"
                    " get_resource, list_resources, and diagnose_pod when a"
                    " Deployment rollout is stuck."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "description": "Workload kind. Currently: deployments.",
                        },
                        "name": {
                            "type": "string",
                            "description": "Workload name.",
                        },
                        "namespace": {
                            "type": "string",
                            "description": "Kubernetes namespace containing the workload.",
                        },
                    },
                    "required": ["kind", "name", "namespace"],
                },
            },
        },
    ),
    ToolDef(
        name="diagnose_service",
        effect="cluster_read",
        dispatch="_diagnose_service",
        surfaces=_ALL_SURFACES,
        result_format="structured_yaml",
        schema={
            "type": "function",
            "function": {
                "name": "diagnose_service",
                "description": (
                    "Deterministically check whether a Service has current ready "
                    "EndpointSlice endpoints. Returns versioned findings and explicit "
                    "evidence gaps; prefer this when traffic cannot reach a Service."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "service": {"type": "string", "description": "Service name."},
                        "namespace": {
                            "type": "string",
                            "description": "Kubernetes namespace containing the Service.",
                        },
                    },
                    "required": ["service", "namespace"],
                },
            },
        },
    ),
    ToolDef(
        name="diagnose_pvc",
        effect="cluster_read",
        dispatch="_diagnose_pvc",
        surfaces=_ALL_SURFACES,
        result_format="structured_yaml",
        schema={
            "type": "function",
            "function": {
                "name": "diagnose_pvc",
                "description": (
                    "Deterministically check why a PersistentVolumeClaim is not Bound. "
                    "One GET resolves Bound/Lost claims immediately. "
                    "For unresolved (Pending) claims, Warning events are fetched first; "
                    "StorageClasses are listed only when no decisive failure event, "
                    "pre-bound volume (spec.volumeName set), or explicit-empty/static-binding "
                    "evidence already determines the result. "
                    "Returns versioned findings with explicit evidence gaps when reads are denied. "
                    "Follow opens persistentvolumeclaims describe. "
                    "Prefer this over get_resource/get_events when a PVC is stuck."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pvc": {"type": "string", "description": "PVC name."},
                        "namespace": {
                            "type": "string",
                            "description": "Kubernetes namespace containing the PVC.",
                        },
                    },
                    "required": ["pvc", "namespace"],
                },
            },
        },
    ),
    ToolDef(
        name="navigate",
        effect="ui_only",
        dispatch="agent_navigate",
        surfaces=_FULL_AND_MCP,
        result_format="untrusted_text",
        schema={
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
                            "description": (
                                "Resource kind or alias to display (e.g. 'pods', 'deploy')."
                            ),
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
    ),
    ToolDef(
        name="set_filter",
        effect="ui_only",
        dispatch="agent_set_filter",
        surfaces=_FULL_AND_MCP,
        result_format="untrusted_text",
        schema={
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
    ),
    ToolDef(
        name="open_logs",
        effect="ui_only",
        dispatch="agent_open_logs",
        surfaces=_ALL_SURFACES,
        result_format="untrusted_text",
        schema={
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
    ),
    ToolDef(
        name="open_describe",
        effect="ui_only",
        dispatch="agent_open_describe",
        surfaces=_ALL_SURFACES,
        result_format="untrusted_text",
        schema={
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
    ),
    ToolDef(
        name="drill_down",
        effect="ui_only",
        dispatch="agent_drill_down",
        surfaces=_FULL_AND_MCP,
        result_format="untrusted_text",
        schema={
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
    ),
    ToolDef(
        name="delete_resource",
        effect="cluster_write",
        approval="user_confirmation",
        write_action="delete",
        dispatch="agent_request_write",
        surfaces=_AGENT_SURFACES,
        result_format="untrusted_text",
        schema={
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
    ),
    ToolDef(
        name="scale_resource",
        effect="cluster_write",
        approval="user_confirmation",
        write_action="scale",
        dispatch="agent_request_write",
        surfaces=_AGENT_SURFACES,
        result_format="untrusted_text",
        schema={
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
    ),
    ToolDef(
        name="rollout_restart",
        effect="cluster_write",
        approval="user_confirmation",
        write_action="rollout_restart",
        dispatch="agent_request_write",
        surfaces=_AGENT_SURFACES,
        result_format="untrusted_text",
        schema={
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
    ),
    ToolDef(
        name="resize_pod",
        effect="cluster_write",
        approval="user_confirmation",
        capability="pod_resize",
        write_action="resize",
        dispatch="agent_request_write",
        surfaces=_AGENT_SURFACES,
        result_format="untrusted_text",
        schema={
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
    ),
    ToolDef(
        name="propose_write",
        effect="write_proposal",
        dispatch="agent_submit_write_proposal",
        surfaces=frozenset({"mcp_proposal"}),
        result_format="untrusted_text",
        schema={
            "type": "function",
            "function": {
                "name": "propose_write",
                "description": (
                    "Submit an immutable cluster-write proposal for review in "
                    "the korvid TUI. This NEVER mutates the cluster: the "
                    "proposal waits in the TUI's inbox until the user reviews "
                    "and approves it with a keystroke, denies it, or it "
                    "expires. Returns the proposal id — poll it with "
                    "get_write_proposal. Changing an operation requires a new "
                    "proposal."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["delete", "scale", "rollout_restart", "resize"],
                            "description": "The write operation to propose.",
                        },
                        "kind": {
                            "type": "string",
                            "description": (
                                "Resource kind or alias (ignored for resize, "
                                "which always targets pods)."
                            ),
                        },
                        "name": {"type": "string", "description": "Resource name."},
                        "namespace": {
                            "type": "string",
                            "description": "Namespace (required for namespaced resources).",
                        },
                        "replicas": {
                            "type": "integer",
                            "description": "Desired replica count (scale only).",
                        },
                        "resources": {
                            "type": "object",
                            "description": (
                                "Resize only: container name -> "
                                "{'requests'/'limits' -> {'cpu'/'memory' -> quantity}}."
                            ),
                        },
                        "capability": {
                            "type": "string",
                            "description": (
                                "Write-proposal capability token from the "
                                "korvid MCP endpoint registry file."
                            ),
                        },
                    },
                    "required": ["action", "name", "capability"],
                    "allOf": [
                        {
                            "if": {
                                "properties": {"action": {"enum": ["delete"]}},
                                "required": ["action"],
                            },
                            "then": {
                                "required": ["kind"],
                                "not": {
                                    "anyOf": [
                                        {"required": ["replicas"]},
                                        {"required": ["resources"]},
                                    ]
                                },
                            },
                        },
                        {
                            "if": {
                                "properties": {"action": {"enum": ["rollout_restart"]}},
                                "required": ["action"],
                            },
                            "then": {
                                "required": ["kind", "namespace"],
                                "not": {
                                    "anyOf": [
                                        {"required": ["replicas"]},
                                        {"required": ["resources"]},
                                    ]
                                },
                            },
                        },
                        {
                            "if": {
                                "properties": {"action": {"enum": ["scale"]}},
                                "required": ["action"],
                            },
                            "then": {
                                "required": ["kind", "replicas", "namespace"],
                                "not": {"required": ["resources"]},
                            },
                        },
                        {
                            "if": {
                                "properties": {"action": {"enum": ["resize"]}},
                                "required": ["action"],
                            },
                            "then": {
                                "required": ["resources", "namespace"],
                                "not": {"required": ["replicas"]},
                            },
                        },
                    ],
                },
            },
        },
    ),
    ToolDef(
        name="get_write_proposal",
        effect="write_proposal",
        dispatch="agent_get_write_proposal",
        surfaces=frozenset({"mcp_proposal"}),
        result_format="untrusted_text",
        schema={
            "type": "function",
            "function": {
                "name": "get_write_proposal",
                "description": (
                    "Status of a previously submitted write proposal: pending, "
                    "approved, executed, failed, denied, expired, or cancelled. "
                    "Requires the proposal id returned by propose_write."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "proposal_id": {
                            "type": "string",
                            "description": "Id returned by propose_write.",
                        },
                        "capability": {
                            "type": "string",
                            "description": (
                                "Write-proposal capability token from the "
                                "korvid MCP endpoint registry file."
                            ),
                        },
                    },
                    "required": ["proposal_id", "capability"],
                },
            },
        },
    ),
    ToolDef(
        name="cancel_write_proposal",
        effect="write_proposal",
        dispatch="agent_cancel_write_proposal",
        surfaces=frozenset({"mcp_proposal"}),
        result_format="untrusted_text",
        schema={
            "type": "function",
            "function": {
                "name": "cancel_write_proposal",
                "description": (
                    "Cancel a pending write proposal you submitted. A proposal "
                    "the user already resolved cannot be cancelled."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "proposal_id": {
                            "type": "string",
                            "description": "Id returned by propose_write.",
                        },
                        "capability": {
                            "type": "string",
                            "description": (
                                "Write-proposal capability token from the "
                                "korvid MCP endpoint registry file."
                            ),
                        },
                    },
                    "required": ["proposal_id", "capability"],
                },
            },
        },
    ),
]

TOOLS_BY_NAME: dict[str, ToolDef] = {d.name: d for d in TOOL_DEFS}

validate_tool_defs(TOOL_DEFS)
