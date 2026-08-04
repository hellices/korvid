"""Read-only and UI-control tool definitions and executor (spec §5, §6)."""

from __future__ import annotations

import copy
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar

import yaml

from korvid.core.portforward import controller_owner
from korvid.core.secrets import mask_secret_manifest
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.helm import HelmReleaseSummary, HelmRevisionSummary
from korvid.k8s.models import (
    CSVSummary,
    GenericSummary,
    OLMSubscriptionSummary,
    PackageManifestSummary,
    PodListSummary,
    ReplicaSetSummary,
    parse_quantity,
)
from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP, resolve_olm_meta
from korvid.k8s.reads import ReadOps
from korvid.k8s.relations import owned_by
from korvid.tools.diagnose import (
    condition_lines,
    container_state_lines,
    current_health_line,
    identity_lines,
    log_excerpt,
    node_condition_line,
    previous_log_containers,
    pvc_names,
    troubled_containers,
    warning_event_lines,
)
from korvid.tools.registry import TOOL_DEFS, TOOLS_BY_NAME, ToolDef, validate_dispatch_targets

MAX_RESULT_CHARS = 8000

#: OperatorHub catalogs commonly serve hundreds of packages; keep the
#: catalog listing well under the shared result cap so the installed
#: section is never sacrificed to it.
_MAX_CATALOG_PACKAGES = 60

_TRUNCATION_SUFFIX = "\n… [truncated — narrow the query]"


def _reject_slash_name(value: str, field: str) -> str:
    """Reject a 'namespace/name' composite before it reaches the cluster.

    Kubernetes object and namespace names can never contain '/' (DNS
    subdomain rules), but models — small ones especially — paste
    composites from row keys or prose into either field. The call still
    consumes its agent-loop iteration like any errored tool call; what
    failing locally buys is no API round-trip and recovery guidance that
    teaches the split instead of a bare 404.
    """
    if "/" in value:
        raise ValueError(
            f"invalid {field} {value!r}: Kubernetes names never contain '/'. "
            "If this is 'namespace/name', pass the namespace and the name "
            "separately, each in its own field."
        )
    return value


def cap_result(result: str, limit: int = MAX_RESULT_CHARS) -> str:
    """Enforce the tool-result ingest cap; shared by every path that feeds
    a result into conversation history. Profiles may pass a tighter
    `limit` (issue #71) so a full turn of results fits their history
    budget."""
    if len(result) > limit:
        return result[:limit] + _TRUNCATION_SUFFIX
    return result


_MIDDLE_TRUNCATION_MARKER = "\n… [middle truncated — profile result budget]\n"


#: Per-value clamp for list_resources facts: long enough for any real
#: resource name/version, short enough that one hostile custom-column
#: value cannot dominate the result budget.
_FACT_VALUE_LIMIT = 80


def _clamp(value: str) -> str:
    """One printable, bounded line: values may come from arbitrary
    annotations/JSONPath (custom columns) or cluster object fields -
    newlines/control characters must not forge extra result rows, and one
    hostile value must not dominate the result budget."""
    flat = "".join(ch if ch.isprintable() else " " for ch in value)
    return flat if len(flat) <= _FACT_VALUE_LIMIT else flat[: _FACT_VALUE_LIMIT - 1] + "…"


def _pod_facts(s: PodListSummary) -> str:
    parts = [
        f"phase={_clamp(s.phase) or '?'}",
        f"ready={_clamp(s.ready) or '?'}",
        f"restarts={s.restarts}",
    ]
    if s.node:
        parts.append(f"node={_clamp(s.node)}")
    return " ".join(parts)


def _replicaset_facts(s: ReplicaSetSummary) -> str:
    # revision comes from a freely writable annotation: clamp like the rest.
    return (
        f"revision={_clamp(s.revision)} desired={s.desired}"
        f" current={s.current} ready={_clamp(s.ready)}"
    )


def _subscription_facts(s: OLMSubscriptionSummary) -> str:
    return (
        f"channel={_clamp(s.channel) or '?'} source={_clamp(s.source) or '?'}"
        f" csv={_clamp(s.installed_csv) or '?'} state={_clamp(s.state) or '?'}"
    )


def _csv_facts(s: CSVSummary) -> str:
    parts = [f"version={_clamp(s.version) or '?'}", f"phase={_clamp(s.phase) or '?'}"]
    if s.display_name:
        parts.append(f"display={_clamp(s.display_name)}")
    return " ".join(parts)


def _package_facts(s: PackageManifestSummary) -> str:
    return (
        f"catalog={_clamp(s.catalog) or '?'}"
        f" default_channel={_clamp(s.default_channel) or '?'}"
        f" channels={_clamp(','.join(s.channels)) or '?'}"
    )


def _generic_facts(s: GenericSummary) -> str:
    return f"desired={s.desired}" if s.desired is not None else ""


def _helm_release_facts(s: HelmReleaseSummary) -> str:
    return (
        f"revision={s.revision} status={_clamp(s.status) or '?'}"
        f" chart={_clamp(s.chart)} app_version={_clamp(s.app_version)}"
    )


def _helm_revision_facts(s: HelmRevisionSummary) -> str:
    parts = [
        f"release={_clamp(s.release)}",
        f"revision={s.revision}",
        f"status={_clamp(s.status) or '?'}",
        f"chart={_clamp(s.chart)}",
        f"app_version={_clamp(s.app_version)}",
    ]
    if s.description:
        parts.append(f"description={_clamp(s.description)}")
    return " ".join(parts)


#: The column-parity contract (issue #158): every typed summary registers
#: the facts the TUI table shows for its kind, so list_resources answers
#: match the screen. tests/tools/test_list_resources.py asserts every
#: GenericSummary subclass appears here - a new typed summary cannot
#: silently degrade back to name+age.
_SUMMARY_FACTS: dict[type[GenericSummary], Callable[[Any], str]] = {
    PodListSummary: _pod_facts,
    ReplicaSetSummary: _replicaset_facts,
    OLMSubscriptionSummary: _subscription_facts,
    CSVSummary: _csv_facts,
    PackageManifestSummary: _package_facts,
    # Helm's synthetic kinds are not reachable through list_resources today
    # (a follow-up adds a helm listing tool), but the contract keeps their
    # facts registered so that tool renders release status on day one.
    HelmReleaseSummary: _helm_release_facts,
    HelmRevisionSummary: _helm_revision_facts,
}


def summary_facts(s: GenericSummary) -> str:
    """The status facts for one list_resources line, mirroring the TUI's
    columns for that kind; "" when the kind has nothing beyond name+age.

    Dispatch walks the MRO so a subclass of a typed summary inherits its
    parent's renderer instead of silently degrading to the generic facts.
    """
    for klass in type(s).__mro__:
        renderer = _SUMMARY_FACTS.get(klass)
        if renderer is not None:
            return renderer(s)
    return _generic_facts(s)


def compact_result(result: str, limit: int) -> str:
    """Shrink an oversized result to `limit` chars keeping BOTH ends.

    Reports like diagnose_pod deliberately place Warning events and log
    excerpts last, so a prefix-only cap would chop the most diagnostic
    sections. The head keeps identity/context, the (larger) tail keeps the
    evidence. The output never exceeds `limit`, which also makes the
    function idempotent — re-applying the same limit is a no-op.
    """
    if len(result) <= limit:
        return result
    if limit <= len(_MIDDLE_TRUNCATION_MARKER):
        # The marker alone would exceed a tiny limit; degrade to a hard cut
        # so the output-never-exceeds-limit contract holds for any input.
        return result[: max(limit, 0)]
    content = limit - len(_MIDDLE_TRUNCATION_MARKER)
    head = content * 2 // 5
    tail = content - head
    return result[:head] + _MIDDLE_TRUNCATION_MARKER + result[len(result) - tail :]


#: Derived surfaces (issue #91): the registry in `korvid.tools.registry`
#: is the single source of tool metadata; these lists are kept as the
#: public module API for the agent runtime, profiles, evals, and tests.
#: Built from deep copies (issue #97): the lists ride into provider
#: plugins by default, and a mutation there must not corrupt the registry.
READ_TOOLS: list[dict[str, Any]] = [
    copy.deepcopy(d.schema) for d in TOOL_DEFS if d.effect == "cluster_read"
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

    @abstractmethod
    async def agent_submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        """Queue an immutable external write proposal (issue #110).

        Must never mutate the cluster or open a modal: validation, UID
        capture and dry-run preview happen up front, then the proposal
        waits in the TUI inbox for a user keystroke. Returns the proposal
        id (as `proposal <id> pending ...`) or an "ERROR: …" string.
        `session_id`/`client_name`/`client_version` are caller-supplied
        transport metadata — never authenticated identity.
        """
        ...

    @abstractmethod
    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        """Status of a proposal; possession of the id is the capability."""
        ...

    @abstractmethod
    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        """Caller-cancel a pending proposal (distinct from user deny)."""
        ...


UI_TOOLS: list[dict[str, Any]] = [
    copy.deepcopy(d.schema) for d in TOOL_DEFS if d.effect == "ui_only"
]

UI_TOOL_NAMES = frozenset(d.name for d in TOOL_DEFS if d.effect == "ui_only")

#: Cluster mutations (spec §6.2). Every call routes through
#: UIBridge.agent_request_write, which shows the user an approval dialog;
#: the tool result reports whether the user approved and what happened.
WRITE_TOOLS: list[dict[str, Any]] = [
    copy.deepcopy(d.schema)
    for d in TOOL_DEFS
    if d.effect == "cluster_write" and d.capability == "none"
]

#: In-place pod resize (issue #27), kept out of WRITE_TOOLS so the
#: composition root registers it only when discovery found the pods/resize
#: subresource (1.35 GA) - the model is never told about a tool the cluster
#: cannot honor. Dispatch below still recognizes it unconditionally: an
#: unregistered tool call fails in the UI gate, not with "unknown tool".
RESIZE_TOOLS: list[dict[str, Any]] = [
    copy.deepcopy(d.schema) for d in TOOL_DEFS if d.capability == "pod_resize"
]

WRITE_TOOL_NAMES = frozenset(d.name for d in TOOL_DEFS if d.effect == "cluster_write")

#: External write proposals (issue #110): submission/status/cancel tools for
#: the MCP proposal surface. They never mutate the cluster — a proposal only
#: becomes a write after the user approves it inside the TUI.
PROPOSAL_TOOLS: list[dict[str, Any]] = [
    copy.deepcopy(d.schema) for d in TOOL_DEFS if d.effect == "write_proposal"
]

PROPOSAL_TOOL_NAMES = frozenset(d.name for d in TOOL_DEFS if d.effect == "write_proposal")

#: UI dispatch key -> argument adapter unpacking the model's JSON arguments
#: into the bridge call. Keyed by the registry's validated dispatch target
#: (not the tool name) so a definition can never silently invoke a
#: different handler; coverage is enforced at import time below.
_UI_ARG_ADAPTERS: dict[str, Callable[[UIBridge, dict[str, Any]], Awaitable[str]]] = {
    "agent_navigate": lambda ui, a: ui.agent_navigate(str(a["view"]), a.get("namespace")),
    "agent_set_filter": lambda ui, a: ui.agent_set_filter(str(a["pattern"])),
    "agent_open_logs": lambda ui, a: ui.agent_open_logs(
        str(a["pod"]), str(a["namespace"]), a.get("container")
    ),
    "agent_open_describe": lambda ui, a: ui.agent_open_describe(
        str(a["kind"]), str(a["name"]), a.get("namespace")
    ),
    "agent_drill_down": lambda ui, a: ui.agent_drill_down(str(a["name"])),
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


#: Write operations an external proposal may name (issue #110 first slice).
_PROPOSAL_ACTIONS = frozenset({"delete", "scale", "rollout_restart", "resize"})

#: Every caller-supplied propose_write key the validator models. Anything
#: else is rejected, not dropped — a silently ignored option (say, a delete
#: propagation policy) would queue a proposal that is not the operation the
#: caller submitted. `capability` is popped by the MCP server before
#: dispatch and `_`-prefixed keys are server-injected transport metadata.
_PROPOSAL_CALLER_KEYS = frozenset({"action", "kind", "name", "namespace", "replicas", "resources"})


def _reject_unknown_proposal_args(args: dict[str, Any]) -> None:
    """Fail loudly on caller keys the proposal record would not carry."""
    unknown = sorted(
        key
        for key in args
        if key not in _PROPOSAL_CALLER_KEYS and key != "capability" and not key.startswith("_")
    )
    if unknown:
        raise ValueError(f"unknown propose_write argument(s): {', '.join(unknown)}")


def _validated_proposal_args(
    args: dict[str, Any],
) -> tuple[str, str, str, str | None, int | None, dict[str, dict[str, dict[str, str]]] | None]:
    """Type-check propose_write arguments; same rules as the direct write path.

    Tool schemas are not runtime validation: wrong-typed values are
    rejected rather than coerced, so the user is never shown a proposal
    the caller did not literally submit.
    """
    _reject_unknown_proposal_args(args)
    action = args.get("action")
    if not isinstance(action, str) or action not in _PROPOSAL_ACTIONS:
        raise ValueError(f"'action' must be one of {sorted(_PROPOSAL_ACTIONS)}, got {action!r}")
    kind = "pods" if action == "resize" else args.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError(f"'kind' must be a non-empty string, got {kind!r}")
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"'name' must be a non-empty string, got {name!r}")
    namespace = args.get("namespace")
    if namespace is not None and not isinstance(namespace, str):
        raise ValueError(f"'namespace' must be a string, got {namespace!r}")
    replicas = args.get("replicas")
    if replicas is not None and (isinstance(replicas, bool) or not isinstance(replicas, int)):
        raise ValueError(f"'replicas' must be an integer, got {replicas!r}")
    if action == "scale" and replicas is None:
        raise ValueError("'replicas' is required for a scale proposal")
    if action != "scale" and replicas is not None:
        raise ValueError("'replicas' is only valid for a scale proposal")
    resources = args.get("resources")
    if action == "resize":
        resources = _validated_resources(resources)
    elif resources is not None:
        raise ValueError("'resources' is only valid for a resize proposal")
    return action, kind, name, namespace, replicas, resources


class ToolExecutor:
    """Dispatches OpenAI tool calls to the Kubernetes client or the UI bridge.

    `proposal_tools` is fail-closed: the write-proposal tools dispatch only
    when the constructing surface opted in. The MCP server does (it enforces
    the per-run capability token before dispatch); the built-in agent's
    executor never does — a hallucinated or prompt-injected `propose_write`
    from the model must not reach the proposal queue with empty transport
    metadata and no capability check.
    """

    def __init__(
        self,
        kube: ReadOps,
        aliases: Mapping[str, ResourceMeta],
        ui: UIBridge | None = None,
        *,
        proposal_tools: bool = False,
        custom_columns: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._kube = kube
        self._aliases = aliases
        self._ui = ui
        self._proposal_tools = proposal_tools
        #: Configured custom column *names* per plural (issue #45): values
        #: arrive on GenericSummary.custom from the client; the names let
        #: list_resources render them as name=value for the model.
        self._custom_columns = dict(custom_columns or {})

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
        # Routing derives from the registry's validated metadata: the
        # effect picks the route and every handler resolves through the
        # validated dispatch key (issue #91) — an unknown or misregistered
        # tool fails import-time validation, not here.
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            raise ValueError(f"unknown tool: {name!r}")
        if tool.effect == "ui_only":
            return await self._dispatch_ui(tool, arguments)
        if tool.effect == "cluster_write":
            return await self._dispatch_write(tool, arguments)
        if tool.effect == "write_proposal":
            if not self._proposal_tools:
                raise ValueError(
                    f"tool {tool.name!r} is only available over the MCP proposal surface"
                )
            return await self._dispatch_proposal(tool, arguments)
        handler: Callable[[dict[str, Any]], Awaitable[str]] = getattr(self, tool.dispatch)
        return await handler(arguments)

    async def _dispatch_ui(self, tool: ToolDef, args: dict[str, Any]) -> str:
        if self._ui is None:
            raise ValueError("UI control unavailable in this session")
        adapter = _UI_ARG_ADAPTERS.get(tool.dispatch)
        if adapter is None:
            raise ValueError(
                f"tool {tool.name!r}: no argument adapter for UI dispatch {tool.dispatch!r}"
            )
        return await adapter(self._ui, args)

    async def _dispatch_write(self, tool: ToolDef, args: dict[str, Any]) -> str:
        if self._ui is None:
            raise ValueError("write actions require the interactive TUI session")
        action = tool.write_action
        if action is None:  # registry validation guarantees this; defensive
            raise ValueError(f"tool {tool.name!r} has no write action")
        # Tool schemas are not runtime validation: reject wrong-typed values
        # instead of coercing them (str(123) would show the user a target the
        # model never named; int(1.9) an operation it never asked for).
        # The resize action targets pods by definition, so its schema has no
        # 'kind'; keyed off the registry's write_action, not the tool name.
        kind = "pods" if action == "resize" else args.get("kind")
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
        if action == "resize":
            resources = _validated_resources(resources)
        # The validated dispatch key names the approval-gated bridge
        # entrypoint; the registry rejects writes routed anywhere else.
        request_write: Callable[..., Awaitable[str]] = getattr(self._ui, tool.dispatch)
        return await request_write(
            action,
            kind,
            target,
            namespace,
            replicas,
            resources,
        )

    async def _dispatch_proposal(self, tool: ToolDef, args: dict[str, Any]) -> str:
        """Route a proposal tool (issue #110): submit/status/cancel only.

        Proposal tools never execute a write — the validated dispatch key
        is one of the bridge's proposal entrypoints, and the registry
        rejects any proposal tool routed at the direct write path. The
        `_session_id`/`_client_name`/`_client_version` keys are injected by
        the MCP server from transport metadata, never taken from the model.
        """
        if self._ui is None:
            raise ValueError("write proposals require the interactive TUI session")
        session_id = str(args.get("_session_id", ""))
        if tool.dispatch == "agent_submit_write_proposal":
            action, kind, target, namespace, replicas, resources = _validated_proposal_args(args)
            return await self._ui.agent_submit_write_proposal(
                action,
                kind,
                target,
                namespace,
                replicas,
                resources,
                session_id=session_id,
                client_name=str(args.get("_client_name", "")),
                client_version=str(args.get("_client_version", "")),
            )
        proposal_id = args.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id:
            raise ValueError(f"'proposal_id' must be a non-empty string, got {proposal_id!r}")
        if tool.dispatch == "agent_cancel_write_proposal":
            return await self._ui.agent_cancel_write_proposal(proposal_id, session_id=session_id)
        return await self._ui.agent_get_write_proposal(proposal_id)

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
        column_names = self._custom_columns.get(meta.plural, ())
        lines = []
        for s in summaries:
            line = f"{s.namespace}/{s.name}  -  age={s.age()}"
            facts = summary_facts(s)
            if facts:
                line += f"  {facts}"
            # User-configured columns (issue #45): the same extra facts the
            # user asked their table to show reach the model, clamped so a
            # hostile value cannot dominate the result budget.
            for column, value in zip(column_names, s.custom, strict=False):
                line += f"  {column}={_clamp(value)}"
            lines.append(line)
        return "\n".join(lines)

    async def _helm_list_releases(self, args: dict[str, Any]) -> str:
        """Installed helm releases with status (issue #161): parsed from the
        cluster's release Secrets - same path as the TUI's helm browser, so
        the tool line and the table always agree."""
        namespace: str | None = args.get("namespace")
        releases = await self._kube.list_helm_releases(namespace)
        if not releases:
            return "(none)"
        return "\n".join(
            f"{r.namespace}/{r.name}  -  age={r.age()}  {summary_facts(r)}" for r in releases
        )

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
        name = _reject_slash_name(str(args["name"]), "name")
        namespace: str | None = args.get("namespace")
        if namespace is not None:
            namespace = _reject_slash_name(str(namespace), "namespace")
        meta = self._api_meta(kind)
        # A namespaced kind without a namespace would hit an invalid
        # cluster-scoped path — give the model an actionable error instead.
        if meta.namespaced and not namespace:
            raise ValueError(f"kind {kind!r} is namespaced — provide the 'namespace' argument")
        manifest = await self._kube.get_object(meta, namespace, name)
        manifest = _mask_manifest(manifest)
        return yaml.safe_dump(manifest, default_flow_style=False, allow_unicode=True)

    async def _get_logs(self, args: dict[str, Any]) -> str:
        pod = _reject_slash_name(str(args["pod"]), "pod")
        namespace = _reject_slash_name(str(args["namespace"]), "namespace")
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
        namespace = _reject_slash_name(str(args["namespace"]), "namespace")
        name = _reject_slash_name(str(args["name"]), "name")
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
    #: Non-ready pods expanded under a workload diagnosis.
    _DIAGNOSE_MAX_WORKLOAD_PODS = 3
    #: Each embedded pod diagnosis gets a fixed share so parent evidence and
    #: sibling attribution remain visible under the shared ingest cap.
    _DIAGNOSE_WORKLOAD_POD_BUDGET = 2200
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
        "Deployment": ResourceMeta("Deployment", "deployments", "apps", "v1", True),
        "Pod": ResourceMeta("Pod", "pods", "", "v1", True),
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
        """Node conditions and mounted-PVC provisioning evidence."""
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
                    pvc_spec = pvc.get("spec") or {}
                    raw_storage_class = pvc_spec.get("storageClassName")
                    if "storageClassName" not in pvc_spec or raw_storage_class is None:
                        storage_class = "(default)"
                    elif raw_storage_class == "":
                        storage_class = "(none)"
                    else:
                        storage_class = str(raw_storage_class)
                    lines.append(f"pvc {claim}: {phase} storageClass={storage_class}")
                    raw_uid = (pvc.get("metadata") or {}).get("uid")
                    events = await self._kube.list_events_for(
                        namespace,
                        claim,
                        kind="PersistentVolumeClaim",
                        uid=str(raw_uid) if raw_uid else None,
                    )
                    lines.extend(
                        f"pvc {claim} warning: {event}" for event in warning_event_lines(events)
                    )
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
        name = _reject_slash_name(str(args["pod"]), "pod")
        namespace = _reject_slash_name(str(args["namespace"]), "namespace")
        pods_meta = self._api_meta("pods")
        pod = await self._kube.get_object(pods_meta, namespace, name)
        head_sections: list[tuple[str, list[str]]] = [
            (
                f"IDENTITY — pod {namespace}/{name}",
                [*identity_lines(pod), await self._diagnose_owner_chain(namespace, pod)],
            ),
            ("CURRENT HEALTH", [current_health_line(pod)]),
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

    async def _owned_summaries(
        self, namespace: str, parent_uid: str, child_kind: str
    ) -> list[GenericSummary]:
        meta = self._meta_for_kind_name(child_kind)
        if meta is None:
            raise ValueError(f"{child_kind} API was not discovered")
        return [
            summary
            for summary in await self._kube.list_objects(meta, namespace)
            if owned_by(summary, parent_uid)
        ]

    async def _workload_event_lines(
        self, namespace: str, name: str, workload: dict[str, Any]
    ) -> list[str]:
        metadata = workload.get("metadata") or {}
        raw_uid = metadata.get("uid")
        events = await self._kube.list_events_for(
            namespace,
            name,
            kind="Deployment",
            uid=str(raw_uid) if raw_uid else None,
        )
        return warning_event_lines(events) or ["(no warning events)"]

    @staticmethod
    def _pod_summary_is_ready(summary: GenericSummary) -> bool:
        if not isinstance(summary, PodListSummary) or summary.phase != "Running":
            return False
        ready, separator, total = summary.ready.partition("/")
        return bool(separator) and ready == total and total != "0"

    async def _diagnose_deployment(
        self, namespace: str, name: str, workload: dict[str, Any]
    ) -> str:
        metadata = workload.get("metadata") or {}
        uid = str(metadata.get("uid") or "")
        if not uid:
            raise ValueError(f"Deployment {namespace}/{name} has no metadata.uid")

        replicasets = await self._owned_summaries(namespace, uid, "ReplicaSet")
        replica_uids = {summary.uid for summary in replicasets if summary.uid}
        pods_meta = self._meta_for_kind_name("Pod")
        if pods_meta is None:
            raise ValueError("Pod API was not discovered")
        pods = [
            summary
            for summary in await self._kube.list_objects(pods_meta, namespace)
            if any(owner_uid in replica_uids for owner_uid in summary.owner_uids)
        ]
        non_ready = [pod for pod in pods if not self._pod_summary_is_ready(pod)]

        sections: list[tuple[str, list[str]]] = [
            (
                f"WORKLOAD — Deployment {namespace}/{name}",
                identity_lines(workload),
            ),
            (
                "WORKLOAD CONDITIONS (failing first)",
                condition_lines(workload) or ["(none reported)"],
            ),
            (
                "WORKLOAD WARNING EVENTS (newest first)",
                await self._workload_event_lines(namespace, name, workload),
            ),
            (
                "OWNED REPLICASETS",
                [f"ReplicaSet {summary.name}: {summary_facts(summary)}" for summary in replicasets]
                or ["(none found)"],
            ),
        ]
        report: list[str] = []
        for title, lines in sections:
            report.append(title)
            report.extend(
                f"  {line}"
                for line in self._budget_section([self._clamp_line(line) for line in lines])
            )

        if not non_ready:
            report.extend(["POD DIAGNOSES", "  (no non-ready owned pods found)"])
        for pod in non_ready[: self._DIAGNOSE_MAX_WORKLOAD_PODS]:
            report.append(f"POD DIAGNOSIS — {namespace}/{pod.name}")
            try:
                diagnosis = await self._diagnose_pod({"pod": pod.name, "namespace": namespace})
            except ApiStatusError as exc:
                diagnosis = f"unavailable ({exc})"
            report.extend(
                f"  {line}"
                for line in compact_result(
                    diagnosis, self._DIAGNOSE_WORKLOAD_POD_BUDGET
                ).splitlines()
            )
        omitted = non_ready[self._DIAGNOSE_MAX_WORKLOAD_PODS :]
        if omitted:
            report.append(
                f"({len(omitted)} more non-ready pod(s) not expanded: "
                + ", ".join(pod.name for pod in omitted)
                + ")"
            )
        return compact_result("\n".join(report), MAX_RESULT_CHARS)

    async def _diagnose_workload(self, args: dict[str, Any]) -> str:
        """One-call rollout diagnosis for supported workload kinds."""
        kind = str(args["kind"]).strip().lower()
        name = _reject_slash_name(str(args["name"]), "name")
        namespace = _reject_slash_name(str(args["namespace"]), "namespace")
        meta = self._api_meta(kind)
        if meta.plural != "deployments":
            raise ValueError(f"diagnose_workload currently supports deployments, not {meta.plural}")
        workload = await self._kube.get_object(meta, namespace, name)
        return await self._diagnose_deployment(namespace, name, workload)


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


# Fail at import time (startup/tests), not at a live tool call, when a
# registry dispatch key stops matching a real handler (issue #91 rule 3).
validate_dispatch_targets(TOOL_DEFS, executor_cls=ToolExecutor, bridge_cls=UIBridge)

# Same import-time guarantee for argument adapters: every registered UI
# dispatch key must have an adapter, so an accepted definition cannot fall
# through to a different handler at call time.
for _tool_def in TOOL_DEFS:
    if _tool_def.effect == "ui_only" and _tool_def.dispatch not in _UI_ARG_ADAPTERS:
        raise ValueError(
            f"tool {_tool_def.name!r}: no argument adapter for UI dispatch {_tool_def.dispatch!r}"
        )
