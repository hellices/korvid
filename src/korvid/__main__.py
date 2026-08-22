"""Composition root — the only place real dependencies are wired together.

Everything (connect, app, close) runs inside ONE event loop via run_async:
kubernetes_asyncio's ApiClient binds its aiohttp session to the loop it was
created on, so separate asyncio.run() calls would break with
"Event loop is closed" / "attached to a different loop".
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import importlib.util
import logging
import secrets
import ssl
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from korvid import __version__
from korvid.agent.context import cluster_context_note
from korvid.agent.install_hint import isolated_install_hint
from korvid.agent.setup import AgentConfigurator, AgentSettings
from korvid.core.audit import AuditLog, default_audit_path
from korvid.core.config import (
    DEFAULT_CONFIG_PATH,
    KorvidConfig,
    ObservabilityBackend,
    context_is_protected,
    load_config,
    save_agent_config,
    save_topbar_state,
)
from korvid.core.mcp import MCPControllerBase
from korvid.core.portforward import ForwardRegistry
from korvid.core.session_timeline import SessionTimeline
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.client import (
    KubeClient,
    list_context_names,
    resolve_context_name,
    resolve_context_namespace,
)
from korvid.k8s.csp import ProviderInfo, detect_provider
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META
from korvid.k8s.helmcli import HelmCLI, find_helm
from korvid.k8s.metrics import MetricsPoller
from korvid.k8s.models import reset_age_memo
from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP
from korvid.k8s.telepresence import (
    TRAFFIC_MANAGER_NAME,
    TRAFFIC_MANAGER_NAMESPACE,
    TelepresenceCLI,
    find_telepresence,
)
from korvid.tools.executor import (
    ToolExecutor,
    UIBridge,
)
from korvid.tools.proposals import ProposalStore
from korvid.tools.registry import mcp_tool_schemas
from korvid.ui.app import (
    AppUIBridge,
    KorvidApp,
)
from korvid.ui.context_switch_coordinator import ContextSwitchResult
from korvid.ui.hints import EventsFetcher
from korvid.ui.widgets.resource_table import sanitize_views

if TYPE_CHECKING:
    # Embedded-agent types appear only in annotations here: an MCP-only or
    # base install must never import the agent runtime or provider ABC at
    # startup (issue #73 acceptance criterion).
    from korvid.agent.profiles import AgentProfile, PromptOverrides
    from korvid.agent.provider import LLMProvider
    from korvid.agent.runtime import AgentRuntime

logger = logging.getLogger(__name__)

#: Actionable install hints (issue #73): an explicitly requested feature
#: whose extra is missing must fail with instructions, never degrade
#: silently or dump an ImportError traceback.
_MCP_INSTALL_HINT = (
    "MCP support was requested (--mcp or mcp.enabled) but its dependencies "
    f"are not installed — {isolated_install_hint(feature='mcp')}"
)
_AGENT_INSTALL_HINT = (
    "the embedded agent is enabled (agent.provider in config.yaml) but its "
    f"dependencies are not installed — {isolated_install_hint(feature='agent')}"
)

#: Top-level packages each extra provides, probed explicitly before any
#: feature module is imported. Detection cannot rely on catching
#: ModuleNotFoundError: parts of an extra may arrive transitively or be
#: imported lazily (TokenStore falls back when
#: keyring is absent), which would misreport the capability as installed.
_MCP_EXTRA_ROOTS = frozenset({"mcp", "anyio", "starlette", "uvicorn"})
_AGENT_EXTRA_ROOTS = frozenset({"httpx", "keyring"})
#: The observability connectors need only an HTTP client.
_OBSERVABILITY_EXTRA_ROOTS = frozenset({"httpx"})
_OBSERVABILITY_INSTALL_HINT = (
    "an observability backend is configured (observability.prometheus/loki in "
    f"config.yaml) but its dependencies are not installed — {isolated_install_hint(feature='observability')}"
)


def _missing_extra_packages(extra_roots: frozenset[str]) -> list[str]:
    """The extra's packages that are not installed (empty = extra present)."""
    return sorted(pkg for pkg in extra_roots if importlib.util.find_spec(pkg) is None)


@dataclasses.dataclass(frozen=True, slots=True)
class ObservabilityWiring:
    """The observability connectors this session has, if any (issue #193).

    Both are None in the ordinary case: an unconfigured backend is not a
    tool that fails, it is a tool that is never offered. `backends` is
    what the tool registry gates on.
    """

    metrics: Any = None
    logs: Any = None

    @property
    def backends(self) -> frozenset[str]:
        """The backend names the tool registry should offer tools for."""
        names: set[str] = set()
        if self.metrics is not None:
            names.add("metrics")
        if self.logs is not None:
            names.add("logs")
        return frozenset(names)

    async def aclose(self) -> None:
        """Close every owned client; each is attempted even if one raises."""
        try:
            if self.metrics is not None:
                await self.metrics.aclose()
        finally:
            if self.logs is not None:
                await self.logs.aclose()


def _build_observability(config: KorvidConfig) -> ObservabilityWiring:
    """Build the configured read-only observability connectors (issue #193).

    Nothing configured means nothing imported: a base installation never
    pulls in the HTTP stack for a feature it is not using. A configured
    backend without the extra fails with an install hint rather than
    degrading, because the user asked for it explicitly.

    Every client comes from the providers' trust builder, so one
    `network.ca_bundle` governs every korvid-owned HTTPS client and an
    unloadable bundle fails startup instead of falling back to default
    trust.

    Raises:
        SystemExit: when a backend is configured but the extra or the
            configured CA bundle is unusable.
    """
    prometheus = config.observability_prometheus
    loki = config.observability_loki
    if prometheus is None and loki is None:
        return ObservabilityWiring()
    if _missing_extra_packages(_OBSERVABILITY_EXTRA_ROOTS):
        raise SystemExit(f"korvid: {_OBSERVABILITY_INSTALL_HINT}")

    from korvid.obs.connector import ConnectorError, QueryLimits
    from korvid.providers import net

    def limits(backend: ObservabilityBackend) -> QueryLimits:
        return QueryLimits(
            timeout_seconds=backend.timeout_seconds,
            default_window_minutes=backend.default_window_minutes,
            max_window_minutes=backend.max_window_minutes,
            max_series=backend.max_series,
            max_lines=backend.max_lines,
            max_response_bytes=backend.max_response_bytes,
            max_concurrency=backend.max_concurrency,
        )

    def client(backend: ObservabilityBackend) -> Any:
        # Looked up through the module so a test (and a future refactor)
        # sees one trust builder rather than a captured function.
        try:
            return net.make_client(config.network_ca_bundle, timeout=backend.timeout_seconds)
        except ValueError as exc:
            raise SystemExit(f"korvid: {exc}") from exc

    try:
        _validate_observability(prometheus, loki)
        # Built before any client: `QueryLimits` validates as well, and it
        # was evaluated *after* the client in the constructor's argument
        # list — so a bad limit stranded a client nobody could close.
        prometheus_limits = limits(prometheus) if prometheus is not None else None
        loki_limits = limits(loki) if loki is not None else None
        return _connectors(prometheus, loki, prometheus_limits, loki_limits, client)
    except (ConnectorError, ValueError) as exc:
        # Two refusal types, one outcome: a connector-level invariant
        # (`ConnectorError`) and an unusable limit (`ValueError` from
        # `QueryLimits`). Neither should reach the user as a traceback,
        # and both mean the same thing to them.
        raise SystemExit(f"korvid: observability configuration is unusable: {exc}") from exc


def _validate_observability(
    prometheus: ObservabilityBackend | None, loki: ObservabilityBackend | None
) -> None:
    """Refuse an unusable configuration before any client is allocated.

    The connectors validate in their constructors, but their arguments —
    the HTTP client among them — are evaluated first, so a refusal there
    would strand a client nobody can close.

    Raises:
        ConnectorError: `config` for anything a connector would refuse.
    """
    from korvid.obs import loki as loki_module
    from korvid.obs.http import validate_endpoint

    if prometheus is not None:
        validate_endpoint(prometheus.url, "prometheus")
    if loki is not None:
        validate_endpoint(loki.url, "loki")
        loki_module.validate_options(tenant=loki.tenant, label_mappings=loki.label_mappings)


def _connectors(
    prometheus: ObservabilityBackend | None,
    loki: ObservabilityBackend | None,
    prometheus_limits: Any,
    loki_limits: Any,
    client: Callable[[ObservabilityBackend], Any],
) -> ObservabilityWiring:
    """Construct whichever connectors are configured (see `_build_observability`)."""
    from korvid.obs.loki import LokiConnector
    from korvid.obs.prometheus import PrometheusConnector

    metrics = (
        PrometheusConnector(
            prometheus.url,
            client=client(prometheus),
            limits=prometheus_limits,
            token_env=prometheus.token_env,
            token_file=prometheus.token_file,
            mask_labels=frozenset(prometheus.mask_labels),
        )
        if prometheus is not None
        else None
    )
    logs = (
        LokiConnector(
            loki.url,
            client=client(loki),
            limits=loki_limits,
            token_env=loki.token_env,
            token_file=loki.token_file,
            tenant=loki.tenant,
            label_mappings=loki.label_mappings,
            mask_labels=frozenset(loki.mask_labels),
        )
        if loki is not None
        else None
    )
    return ObservabilityWiring(metrics=metrics, logs=logs)


def _custom_column_names(config: KorvidConfig) -> dict[str, tuple[str, ...]]:
    """Configured custom column *names* per plural (issue #158): the client
    computes the values onto GenericSummary.custom; the tool layer needs the
    names to render them as name=value in list_resources."""
    return {kind: tuple(col.name for col in view.columns) for kind, view in config.views.items()}


class _MCPAppHooks:
    """Late-bound app hooks for MCP follow mode (issue #153).

    Built (like `_UIBridgeProxy`) before the app exists; the composition
    root points `app` at the live instance right after construction. Until
    then follow reads as off and activity notes are dropped - external
    reads simply stay response-only, never an error.
    """

    def __init__(self) -> None:
        self.app: KorvidApp | None = None

    def follow_enabled(self) -> bool:
        return self.app is not None and self.app.integrations.follow_enabled

    def note_activity(self, line: str) -> None:
        if self.app is not None:
            self.app.integrations.note_activity(line)


def _build_mcp_controller(
    config: KorvidConfig,
    kube: KubeClient,
    aliases: dict[str, ResourceMeta],
    ui: UIBridge | None,
    mcp_hooks: _MCPAppHooks | None = None,
    observability: ObservabilityWiring | None = None,
) -> MCPControllerBase | None:
    """Import and wire the MCP adapter only when its extra is installed.

    Base installations get None (the `:mcp` command reports the feature as
    unavailable); a config that explicitly enables MCP fails with an
    actionable install hint instead of silently degrading.

    The surface is read + UI-drive tools only - write tools stay with the
    built-in agent until an approval UX for external callers is designed
    (issue #11 non-goal)."""
    missing = _missing_extra_packages(_MCP_EXTRA_ROOTS)
    if missing:
        if config.mcp_enabled:
            raise SystemExit(f"korvid: {_MCP_INSTALL_HINT}")
        logger.info("MCP adapter not installed; :mcp disabled (missing %s)", ", ".join(missing))
        return None

    from korvid.mcp.server import KorvidMCPServer, MCPController, default_endpoint_path

    obs = observability or ObservabilityWiring()

    def factory() -> KorvidMCPServer:
        # A fresh capability token per server run (issue #110): the token is
        # published only in the owner-readable endpoint file, so echoing it
        # proves same-user local file access; a restart invalidates every
        # previously handed-out token together with the pending proposals.
        token = secrets.token_urlsafe(32) if config.mcp_write_proposals else None
        return KorvidMCPServer(
            ToolExecutor(
                kube,
                aliases,
                ui=ui,
                # The only surface allowed to reach the write-proposal tools:
                # this server enforces the capability token before dispatch.
                proposal_tools=config.mcp_write_proposals,
                custom_columns=_custom_column_names(config),
                metrics=obs.metrics,
                logs=obs.logs,
            ),
            mcp_tool_schemas(
                write_proposals=config.mcp_write_proposals,
                observability_backends=obs.backends,
            ),
            port=config.mcp_port,
            endpoint_path=default_endpoint_path(),
            capability_token=token,
            # Follow mode (issue #153): mirror external cluster reads via
            # the same serialized UI proxy the ui_only tools use.
            ui=ui,
            follow_enabled=mcp_hooks.follow_enabled if mcp_hooks is not None else None,
            note_activity=mcp_hooks.note_activity if mcp_hooks is not None else None,
        )

    return MCPController(factory)


def _build_proposal_store(config: KorvidConfig) -> ProposalStore | None:
    """One store shared by the app (indicator/review/execution) and — through
    the UI bridge — the MCP server's proposal tools (issue #110). None keeps
    the feature reporting itself as disabled."""
    if not config.mcp_write_proposals:
        return None
    return ProposalStore()


async def _shutdown(
    discovery_task: asyncio.Task[None] | None, provider: LLMProvider | None, kube: KubeClient
) -> None:
    """Tear down background work and owned clients; each step is attempted
    even if an earlier one raises. *discovery_task* is None when startup
    wiring failed before discovery began (issue #166)."""
    try:
        if discovery_task is not None:
            discovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await discovery_task
    finally:
        try:
            if provider is not None:
                await provider.aclose()
        finally:
            await kube.close()


async def _discover_in_background(
    kube: KubeClient, aliases: dict[str, ResourceMeta], app: KorvidApp
) -> None:
    """Merge full API discovery into *aliases* once available (shared dict)."""
    try:
        metas = await kube.discover_resources()
        discovered = build_alias_map(metas)
    except Exception:
        logger.warning("Resource discovery failed; staying pods-only", exc_info=True)
        return
    # Synthetic view kinds own their plurals outright: every discovered alias
    # whose target plural collides (e.g. Flux's HelmRelease CRD contributes
    # "hr" -> plural "helmreleases") is dropped, because navigation routes by
    # plural and the alias would silently open the Secret-backed browser
    # instead of the CRD it named.
    reserved = {HELM_RELEASES_META.plural, HELM_REVISIONS_META.plural}
    aliases.update({a: m for a, m in discovered.items() if m.plural not in reserved})
    aliases.update(build_alias_map([HELM_RELEASES_META, HELM_REVISIONS_META]))
    # build_alias_map keeps only the first meta per colliding alias, which
    # can hide the OLM kinds behind a same-plural CRD from another group
    # (e.g. a messaging "subscriptions"). Keep them reachable under their
    # kubectl-style plural.group alias so the install flow and the agent's
    # operator tool resolve the right API regardless of discovery order.
    for meta in metas:
        if meta.group in (PACKAGES_GROUP, OPERATORS_GROUP) and meta.plural not in reserved:
            aliases.setdefault(f"{meta.plural}.{meta.group}", meta)
    # Where OLM serves the operator catalog, `:operators` opens it - unless a
    # real kind (e.g. OLM v1's Operator) already claims that alias.
    pkg_meta = aliases.get(f"packagemanifests.{PACKAGES_GROUP}")
    if pkg_meta is not None:
        aliases.setdefault("operators", pkg_meta)
    app.on_aliases_updated()


def _close_provider_in_background(provider: LLMProvider, tasks: set[asyncio.Task[None]]) -> None:
    """Close an old provider without blocking, keeping a strong task reference.

    asyncio only holds weak references to tasks, so fire-and-forget tasks can
    be garbage-collected before completion; the done callback also consumes
    any close error to avoid 'Task exception was never retrieved' warnings.
    """
    task = asyncio.get_running_loop().create_task(provider.aclose())
    tasks.add(task)

    def _reap(t: asyncio.Task[None]) -> None:
        tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            # Consume the exception with a fixed message only — never log
            # exc_info or the exception message (plugin payloads may contain
            # secrets or unbounded text).
            logger.debug("old provider close failed")

    task.add_done_callback(_reap)


class _UIBridgeProxy(UIBridge):
    """Late-bound UI bridge: the ToolExecutor is built before the app exists,
    so it holds this proxy and the composition root points ``target`` at the
    app's bridge adapter right after construction. Until then every UI tool
    degrades to an ERROR result instead of crashing the turn.

    All delegated calls are serialized through one lock: the built-in agent
    and the MCP server's concurrent stateless requests share this proxy, and
    the app's UI operations (log pane swaps, describe views) are not safe to
    interleave - only navigation has its own lock inside the app."""

    _NOT_READY = "ERROR: UI not ready"

    def __init__(self) -> None:
        self.target: UIBridge | None = None
        self._lock = asyncio.Lock()

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_navigate(view, namespace)

    async def agent_set_filter(self, pattern: str) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_set_filter(pattern)

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_open_logs(pod, namespace, container)

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_open_describe(kind, name, namespace)

    async def agent_drill_down(self, name: str) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_drill_down(name)

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_request_write(
                action, kind, name, namespace, replicas, resources
            )

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
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_submit_write_proposal(
                action,
                kind,
                name,
                namespace,
                replicas,
                resources,
                session_id=session_id,
                client_name=client_name,
                client_version=client_version,
            )

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_get_write_proposal(proposal_id)

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        if self.target is None:
            return self._NOT_READY
        async with self._lock:
            return await self.target.agent_cancel_write_proposal(proposal_id, session_id=session_id)


#: Upper bound on the pods/resize discovery probe at startup: the TUI must
#: appear promptly even against a slow or hung apiserver.
_RESIZE_PROBE_TIMEOUT = 3.0


async def _probe_pod_resize(kube: KubeClient, *, readonly: bool = False) -> bool:
    """Bounded pods/resize capability probe (issue #27). A probe slower than
    _RESIZE_PROBE_TIMEOUT answers False - the feature stays off for this
    session rather than delaying startup (full resource discovery already
    runs in the background for the same reason). Readonly sessions skip the
    round trip entirely: neither resize entry point can ever be exposed, so
    a slow discovery endpoint must not delay their startup either."""
    if readonly:
        return False
    try:
        return await asyncio.wait_for(kube.supports_pod_resize(), _RESIZE_PROBE_TIMEOUT)
    except TimeoutError:
        logger.warning("pods/resize discovery timed out; in-place resize disabled")
        return False


async def _probe_cloud_provider(kube: KubeClient) -> ProviderInfo:
    """Bounded cloud-provider detection at startup (issue #30). Detection is a
    hint — a slow or unresponsive node list answers "unknown" rather than
    delaying the TUI (same policy as the resize probe)."""
    try:
        return await asyncio.wait_for(kube.detect_cloud_provider(), _RESIZE_PROBE_TIMEOUT)
    except TimeoutError:
        logger.warning("cloud provider detection timed out; provider unknown")
        return detect_provider([])


def _agent_unavailable_wiring(
    config: KorvidConfig,
    missing: list[str],
    ui_proxy: _UIBridgeProxy,
    provider_box: list[LLMProvider | None],
) -> tuple[
    None,
    None,
    None,
    Callable[[AgentRuntime | None, bool, str | None], None],
    Callable[[], None],
    list[LLMProvider | None],
    _UIBridgeProxy,
]:
    """Runtime-less wiring for installs without the [agent] extra.

    An explicitly enabled agent fails with an actionable install hint; an
    unrequested one degrades to a wiring the app renders as "unavailable".
    """
    if config.agent_enabled:
        raise SystemExit(f"korvid: {_AGENT_INSTALL_HINT}")
    logger.info(
        "embedded-agent providers not installed; :ai disabled (missing %s)", ", ".join(missing)
    )

    def _retarget_noop(
        runtime: AgentRuntime | None,
        pod_resize_supported: bool,
        cluster_context: str | None,
    ) -> None:
        return None

    return None, None, None, _retarget_noop, lambda: None, provider_box, ui_proxy


def _create_initial_provider(
    config: KorvidConfig,
    oauth: str | None,
    ollama_options: Any,
    plugin_registry: Any,
    startup_warnings: list[str] | None,
) -> LLMProvider | None:
    """Build the initial LLM provider, converting plugin errors to warnings."""
    from korvid.providers.plugin_registry import ProviderPluginError
    from korvid.providers.registry import create_provider

    try:
        return create_provider(
            enabled=config.agent_enabled,
            provider=config.agent_provider,
            auth_method=config.agent_auth_method,
            base_url=config.agent_base_url,
            model=config.agent_model,
            api_key_env=config.agent_api_key_env,
            oauth_token=oauth,
            ollama=ollama_options,
            ca_bundle=config.network_ca_bundle,
            plugin_registry=plugin_registry,
            options=config.agent_options,
            options_error=config.agent_options_error,
        )
    except ProviderPluginError as exc:
        # Plugin failure at startup degrades to warning — the TUI remains
        # operational with provider=None; the :ai wizard can still reconfigure.
        if startup_warnings is not None:
            startup_warnings.append(f"Provider plugin failed: {exc}")
        logger.warning("provider plugin failed at startup: %s — agent disabled", exc)
        return None


def _prompt_overrides(config: KorvidConfig) -> PromptOverrides:
    """Configured prompt slots (`agent.prompts`) as the agent layer wants them."""
    from korvid.agent.profiles import PromptOverrides

    return PromptOverrides(
        system=config.agent_prompt_system,
        append=config.agent_prompt_append,
        tool_descriptions=config.agent_prompt_tool_descriptions,
    )


def _initial_profile(
    config: KorvidConfig,
    overrides: PromptOverrides,
    pod_resize_supported: bool,
    startup_warnings: list[str] | None,
    observability_backends: frozenset[str] = frozenset(),
) -> AgentProfile:
    """The starting capability profile, reporting advisory prompt warnings.

    Unusable slots have already fallen back to the shipped prompt during
    config parsing. What is reported here is advisory and leaves the
    configured prompt **active**: an override naming an unknown tool (the
    remaining overrides still apply), and a prompt large enough to crowd
    the profile's history budget, which is used as configured. Neither is
    ever fatal.
    """
    from korvid.agent.profiles import build_profile, validate_prompt_overrides

    profile = build_profile(
        config.agent_profile or "full",
        readonly=config.readonly,
        resize_supported=pod_resize_supported,
        observability_backends=observability_backends,
        overrides=overrides,
    )
    if startup_warnings is not None:
        startup_warnings.extend(validate_prompt_overrides(profile, overrides))
    return profile


def _build_agent_wiring(
    config: KorvidConfig,
    kube: KubeClient,
    aliases: dict[str, ResourceMeta],
    *,
    pod_resize_supported: bool = False,
    cluster_context: str | None = None,
    provider_box: list[LLMProvider | None] | None = None,
    startup_warnings: list[str] | None = None,
    observability: ObservabilityWiring | None = None,
) -> tuple[
    AgentRuntime | None,
    AgentConfigurator | None,
    Callable[[AgentSettings], AgentRuntime | None] | None,
    Callable[[AgentRuntime | None, bool, str | None], None],
    Callable[[], None],
    list[LLMProvider | None],
    _UIBridgeProxy,
]:
    """Build the initial agent runtime plus the :ai wizard's configurator/rebuild hooks.

    Provider adapters and credential storage are optional (issue #73): a
    base installation gets a runtime-less wiring whose `:ai` command reports
    the feature as unavailable, while a config that explicitly enables the
    agent fails with an actionable install hint.
    """
    ui_proxy = _UIBridgeProxy()
    obs = observability or ObservabilityWiring()
    # The caller may hand in the box that its teardown guard reads (issue
    # #166): the provider is owned by that box from the moment it exists,
    # so a failure in the *rest* of the agent wiring still cleans it up.
    if provider_box is None:
        provider_box = [None]
    missing = _missing_extra_packages(_AGENT_EXTRA_ROOTS)
    if missing:
        return _agent_unavailable_wiring(config, missing, ui_proxy, provider_box)

    # Deferred behind the capability probe: the embedded-agent loop is only
    # composed when this wiring is actually built (issue #73 requires
    # MCP-only startups not to import AgentRuntime at all).
    from korvid.agent.profiles import build_profile
    from korvid.agent.runtime import AgentRuntime
    from korvid.providers.configurator import ProviderConfigurator
    from korvid.providers.ollama import OllamaOptions
    from korvid.providers.plugin_registry import ProviderPluginRegistry
    from korvid.providers.registry import create_provider
    from korvid.providers.token_store import TokenStore

    plugin_registry = ProviderPluginRegistry()
    token_store = TokenStore()
    # Model-capability profile (issue #71): tool surface, budgets, and
    # prompts come from one place so the initial build and every wizard
    # rebuild stay consistent. In readonly mode the model is never even
    # told write tools exist; resize is offered only when discovery found
    # pods/resize (1.35 GA). `agent.prompts` swaps the role statement and
    # tool wording only — the write/no-write and UI clauses stay conditional
    # on what is actually armed.
    prompt_overrides = _prompt_overrides(config)
    profile = _initial_profile(
        config, prompt_overrides, pod_resize_supported, startup_warnings, obs.backends
    )
    oauth = token_store.load("github-oauth") if config.agent_provider == "github-copilot" else None
    ollama_options = OllamaOptions(
        num_ctx=config.agent_ollama_num_ctx,
        temperature=config.agent_ollama_temperature,
        seed=config.agent_ollama_seed,
        think=config.agent_ollama_think,
        keep_alive=config.agent_ollama_keep_alive,
    )
    provider = _create_initial_provider(
        config, oauth, ollama_options, plugin_registry, startup_warnings
    )
    # Ownership transfers immediately: if anything below raises (executor,
    # runtime, configurator), the teardown guard still closes the provider
    # (a GitHub Copilot provider eagerly holds a credential HTTP client).
    provider_box[0] = provider
    agent_runtime = (
        AgentRuntime(
            provider,
            ToolExecutor(
                kube,
                aliases,
                ui=ui_proxy,
                custom_columns=_custom_column_names(config),
                metrics=obs.metrics,
                logs=obs.logs,
            ),
            tools=profile.tools,
            max_iterations=profile.max_iterations,
            max_history_chars=profile.max_history_chars,
            max_result_chars=profile.max_result_chars,
            max_tool_calls_per_iteration=profile.max_tool_calls_per_iteration,
            strict_history_budget=profile.strict_history_budget,
            system_prompt=profile.system_prompt,
            ui_prompt=profile.ui_prompt,
            cluster_context=cluster_context,
        )
        if provider
        else None
    )

    # Mutable holder (shared with the caller's teardown guard) so
    # rebuild_agent/_shutdown always see the live provider.
    # Per-cluster agent inputs: a `:ctx` switch replaces both, so a wizard
    # rebuild after the switch must not resurrect the old cluster's prompt
    # note or capability-gated tool set. The profile name rides along so a
    # retarget recomposes the same profile's surface (issue #71).
    profile_box: list[str] = [profile.name]
    resize_box: list[bool] = [pod_resize_supported]
    note_box: list[str | None] = [cluster_context]

    def persist(settings: AgentSettings) -> None:
        save_agent_config(
            DEFAULT_CONFIG_PATH,
            provider=settings.provider,
            auth_method=settings.auth_method,
            base_url=settings.base_url,
            model=settings.model,
            api_key_env=settings.api_key_env,
            profile=settings.profile,
        )

    configurator = ProviderConfigurator(
        token_store,
        persist,
        # network.ca_bundle (issue #168): endpoint calls (probe + model
        # listing) share the live providers' trust; GitHub Copilot
        # discovery keeps default trust like the live copilot provider.
        ca_bundle=config.network_ca_bundle,
        plugin_registry=plugin_registry,
    )
    close_tasks: set[asyncio.Task[None]] = set()

    def rebuild_agent(settings: AgentSettings) -> AgentRuntime | None:
        new_provider = create_provider(
            enabled=True,
            provider=settings.provider,
            auth_method=settings.auth_method,
            base_url=settings.base_url,
            model=settings.model,
            api_key_env=settings.api_key_env,
            oauth_token=token_store.load("github-oauth"),
            # ollama_options is captured from startup config: the :ai wizard
            # does not edit agent.ollama.*, so the values cannot go stale. If
            # config reload is ever added, re-derive the options here.
            ollama=ollama_options,
            ca_bundle=config.network_ca_bundle,
            plugin_registry=plugin_registry,
            options=settings.options,
        )
        if new_provider is None:
            return None
        # Fully transactional: build profile, executor, and runtime BEFORE
        # swapping provider_box or scheduling old provider close. If anything
        # raises, close the new provider exactly once and leave old state live.
        try:
            new_profile = build_profile(
                settings.profile,
                readonly=config.readonly,
                resize_supported=resize_box[0],
                observability_backends=obs.backends,
                overrides=prompt_overrides,
            )
            new_runtime = AgentRuntime(
                new_provider,
                ToolExecutor(
                    kube,
                    aliases,
                    ui=ui_proxy,
                    custom_columns=_custom_column_names(config),
                    metrics=obs.metrics,
                    logs=obs.logs,
                ),
                tools=new_profile.tools,
                max_iterations=new_profile.max_iterations,
                max_history_chars=new_profile.max_history_chars,
                max_result_chars=new_profile.max_result_chars,
                max_tool_calls_per_iteration=new_profile.max_tool_calls_per_iteration,
                strict_history_budget=new_profile.strict_history_budget,
                system_prompt=new_profile.system_prompt,
                ui_prompt=new_profile.ui_prompt,
                cluster_context=note_box[0],
            )
        except Exception:
            _close_provider_in_background(new_provider, close_tasks)
            raise
        # Success — now atomically swap state.
        old = provider_box[0]
        provider_box[0] = new_provider
        if old is not None:
            _close_provider_in_background(old, close_tasks)
        profile_box[0] = new_profile.name
        return new_runtime

    def retarget_agent(
        runtime: AgentRuntime | None,
        pod_resize_supported: bool,
        cluster_context: str | None,
    ) -> None:
        """Re-arm the agent for a new cluster (issue #36, `:ctx`).

        Recomposes the active profile's tool surface with the new cluster's
        capabilities and updates the live runtime's system prompt in place —
        conversation history survives the switch, but later turns must
        describe the new environment, not the one the runtime was built
        against.
        """
        resize_box[0] = pod_resize_supported
        note_box[0] = cluster_context
        if runtime is not None:
            retarget_profile = build_profile(
                profile_box[0],
                readonly=config.readonly,
                resize_supported=pod_resize_supported,
                observability_backends=obs.backends,
                overrides=prompt_overrides,
            )
            runtime.retarget(tools=retarget_profile.tools, cluster_context=cluster_context)

    return (
        agent_runtime,
        configurator,
        rebuild_agent,
        retarget_agent,
        _make_disconnect_agent(provider_box, close_tasks),
        provider_box,
        ui_proxy,
    )


def _make_disconnect_agent(
    provider_box: list[LLMProvider | None], close_tasks: set[asyncio.Task[None]]
) -> Callable[[], None]:
    """`:ai off` (issue #167): release the live provider for the session.

    Persisted configuration is untouched, so a later wizard rebuild
    reconnects with the kept settings. Idempotent when already off.
    """

    def disconnect_agent() -> None:
        old = provider_box[0]
        provider_box[0] = None
        if old is not None:
            _close_provider_in_background(old, close_tasks)

    return disconnect_agent


def _validate_ca_bundle(path: str | None) -> None:
    """Fail startup actionably when `network.ca_bundle` cannot be loaded.

    Missing, unreadable, and malformed bundles must never silently fall
    back to default trust (issue #168) — a user who configured a corporate
    CA needs to know it is not in effect, not debug TLS errors later.
    """
    if path is None:
        return
    try:
        ssl.create_default_context(cafile=path)
    except (OSError, ssl.SSLError) as exc:
        raise SystemExit(f"korvid: network.ca_bundle {path!r} could not be loaded: {exc}") from exc


def _load_startup_config(
    readonly: bool, mcp: bool = False, namespace: str | None = None
) -> KorvidConfig:
    config = load_config()
    _validate_ca_bundle(config.network_ca_bundle)
    if readonly:
        config = dataclasses.replace(config, readonly=True)
    if mcp:
        config = dataclasses.replace(config, mcp_enabled=True)
    # Pin the actual context name so kubectl subprocesses (shell/debug) and the
    # status bar reference this cluster even if current-context changes later.
    resolved_ctx = resolve_context_name(config.kube_context)
    if resolved_ctx != config.kube_context:
        config = dataclasses.replace(config, kube_context=resolved_ctx)
    # Startup namespace (issue #108): CLI -n > config `namespace:` >
    # kubeconfig context namespace > "default". All four select one concrete
    # namespace — none represents an RBAC grant or namespace discovery.
    resolved_ns = (
        namespace or config.namespace or resolve_context_namespace(config.kube_context) or "default"
    )
    if resolved_ns != config.namespace:
        config = dataclasses.replace(config, namespace=resolved_ns)
    # Kind-aware column validation lives in the UI layer (only it knows each
    # kind's headers); config parsing already rejected the universal names.
    views, view_warnings = sanitize_views(config.views)
    if view_warnings:
        config = dataclasses.replace(
            config, views=views, warnings=(*config.warnings, *view_warnings)
        )
    return config


async def _start_mcp_if_enabled(config: KorvidConfig, controller: MCPControllerBase | None) -> None:
    if not config.mcp_enabled or controller is None:
        return
    startup_msg = await controller.start()
    if startup_msg.startswith("ERROR"):
        logger.error("%s", startup_msg)


async def _teardown(
    controller: MCPControllerBase | None,
    discovery_task: asyncio.Task[None] | None,
    provider: LLMProvider | None,
    kube: KubeClient,
    observability: ObservabilityWiring | None = None,
) -> None:
    """Bounded graceful MCP stop first; anything still pending is awaited
    only *after* the critical provider/kube cleanup, matching what
    asyncio.run()'s final task-gathering would do anyway - but explicitly,
    with the exception consumed instead of swallowed."""
    leftover = await controller.shutdown() if controller is not None else None
    try:
        await _shutdown(discovery_task, provider, kube)
    finally:
        if observability is not None:
            await observability.aclose()
    if leftover is not None:
        with contextlib.suppress(BaseException):
            await leftover


def _build_helm(config: KorvidConfig) -> HelmCLI | None:
    """Wrap a detected helm binary, or None so the UI gates helm actions off."""
    binary = find_helm()
    if binary is None:
        return None
    return HelmCLI(binary, kube_context=config.kube_context)


def _build_telepresence(config: KorvidConfig) -> TelepresenceCLI | None:
    """Wrap a detected telepresence binary (issue #159), or None when the
    binary is absent or the kill-switch (`integrations.telepresence: off`)
    disabled the integration."""
    if not config.telepresence_enabled:
        return None
    binary = find_telepresence()
    if binary is None:
        return None
    return TelepresenceCLI(binary)


def _make_traffic_manager_probe(kube: KubeClient) -> Callable[[], Awaitable[bool]]:
    """Cluster-side telepresence detection (issue #159): a pure API GET for
    the traffic-manager deployment - never the telepresence binary."""

    async def probe() -> bool:
        meta = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
        try:
            await kube.get_object(meta, TRAFFIC_MANAGER_NAMESPACE, TRAFFIC_MANAGER_NAME)
        except ApiStatusError:
            return False  # absent or forbidden: either way, no hint
        return True

    return probe


def _protected_context_name(config: KorvidConfig, context: str | None) -> str | None:
    """The effective context's name when it matches `protected_contexts`
    (issue #83), None otherwise. *context* is explicit (not read from config)
    so a runtime `:ctx` switch can re-derive protection for the new cluster;
    None falls back to the kubeconfig's active context name."""
    effective = resolve_context_name(context)
    if context_is_protected(effective, config.protected_contexts):
        return effective
    return None


def _make_switch_context(
    config: KorvidConfig,
    kube: KubeClient,
    aliases: dict[str, ResourceMeta],
    app_box: list[KorvidApp],
    discovery_box: list[asyncio.Task[None]],
    retarget_agent: Callable[[AgentRuntime | None, bool, str | None], None],
) -> Callable[[str | None], Awaitable[ContextSwitchResult]]:
    """Build the `:ctx` retarget closure (issue #36).

    Owns everything the composition root wired per-cluster at startup:
    the client connection, the shared alias map (reset to the synthetic
    base, then re-discovered in the background), and the capability
    probes whose results gate the R keybinding and provider hints.
    ``app_box``/``discovery_box`` are late-bound because the app and the
    first discovery task are created after this closure.
    """

    async def switch_context(name: str | None) -> ContextSwitchResult:
        # The embedded MCP server is quiesced by the app BEFORE any teardown
        # (KorvidApp._switch_context_locked) — by the time this closure runs
        # no external caller shares the client or alias map being swapped.
        # switch_context closes the old ApiClient — the background discovery
        # task still issues requests on it, so quiesce it (and reseed the
        # alias map it mutates) before the connection is torn down.
        old_task = discovery_box[0] if discovery_box else None
        if old_task is not None and not old_task.done():
            old_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await old_task
        aliases.clear()
        aliases.update(build_alias_map([PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META]))
        await kube.switch_context(name)
        discovery_box[:] = [asyncio.create_task(_discover_in_background(kube, aliases, app_box[0]))]
        pod_resize_supported = await _probe_pod_resize(kube, readonly=config.readonly)
        provider_info = await _probe_cloud_provider(kube)
        # The surviving conversation must be re-armed for this cluster: new
        # provider note in the system prompt, resize tool gated by the new
        # cluster's capability (issue #36 review).
        retarget_agent(
            app_box[0].agent_runtime if app_box else None,
            pod_resize_supported,
            cluster_context_note(provider_info),
        )
        # The startup `config` is a stale snapshot here: _apply_context_switch
        # folds each applied context's namespace into app.config.
        effective_config = app_box[0].config if app_box else config
        return ContextSwitchResult(
            pod_resize_supported=pod_resize_supported,
            provider_hint=provider_info.display if provider_info.known else None,
            context_namespace=resolve_context_namespace(name),
            protected_context=_protected_context_name(effective_config, name),
            # HelmCLI pins --kube-context per instance: rebuild it for the
            # new context so helm writes follow the active cluster.
            helm=_build_helm(dataclasses.replace(effective_config, kube_context=name)),
        )

    return switch_context


def _make_watch_source(
    kube: KubeClient, aliases: dict[str, ResourceMeta]
) -> Callable[[str, str], AsyncIterator[tuple[str, Summary]]]:
    """Watch source for the WatchManager: kind + scope -> summary events.

    Extracted from _run for complexity; *aliases* is the live shared dict
    that background discovery mutates.
    """

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        ns = None if scope == ALL_NAMESPACES else scope
        if kind == "pods":
            async for ev, pod in kube.watch_pods(ns):
                yield (ev, pod)
        elif kind == HELM_RELEASES_META.plural:
            async for ev, rel in kube.watch_helm_releases(ns):
                yield (ev, rel)
        elif kind == HELM_REVISIONS_META.plural:
            async for ev, rev in kube.watch_helm_revisions(ns):
                yield (ev, rev)
        elif kind in aliases:
            meta = aliases[kind]
            async for ev, obj in kube.watch_objects(meta, ns):
                yield (ev, obj)
        else:
            logger.warning("Unknown resource kind %r requested for watch; stopping", kind)
            raise ValueError(f"Unknown resource kind: {kind!r}")

    return source


def _make_get_manifest(
    kube: KubeClient, aliases: dict[str, ResourceMeta]
) -> Callable[[str, str | None, str], Awaitable[dict[str, Any]]]:
    """Describe fetcher: helm kinds decode release Secrets, the rest GET raw."""

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        if kind == HELM_RELEASES_META.plural:
            if namespace is None:
                raise ValueError("helm releases are namespaced; namespace required")
            return await kube.get_helm_release(namespace, name)
        if kind == HELM_REVISIONS_META.plural:
            # Revision rows are named "<release>.v<revision>".
            release, _, rev = name.rpartition(".v")
            if namespace is None or not release or not rev.isdigit():
                raise ValueError(f"not a helm revision row: {name!r}")
            return await kube.get_helm_release(namespace, release, revision=int(rev))
        meta = aliases.get(kind)
        if meta is None:
            raise ValueError(f"Unknown resource kind: {kind!r}")
        return await kube.get_object(meta, namespace, name)

    return get_manifest


@dataclasses.dataclass
class _RunState:
    """What `_run`'s teardown guard must release — filled progressively by
    `_wire_and_run` so a wiring failure releases exactly what was built."""

    mcp: MCPControllerBase | None = None
    provider_box: list[LLMProvider | None] = dataclasses.field(default_factory=lambda: [None])
    discovery_box: list[asyncio.Task[None]] = dataclasses.field(default_factory=list)
    #: Observability connectors (issue #193): each owns an HTTP client
    #: that teardown must close, however far wiring got.
    observability: ObservabilityWiring | None = None


async def _run(readonly: bool = False, mcp: bool = False, namespace: str | None = None) -> None:
    config = _load_startup_config(readonly, mcp, namespace)
    # Custom columns (issue #45) are extracted from raw manifests inside the
    # client — the manifests are discarded once summaries are built.
    kube = KubeClient(custom_columns={kind: view.columns for kind, view in config.views.items()})
    await kube.connect(config.kube_context)
    # Everything below runs under the client's teardown guard: a wiring or
    # probe failure between connect and the run loop must not leak the
    # connected client (or a built provider/MCP controller) into a
    # crash-recovery restart (issue #166). The state is filled as wiring
    # progresses, so teardown releases exactly what was built.
    state = _RunState()
    try:
        await _wire_and_run(config, kube, state)
    finally:
        await _teardown(
            state.mcp,
            state.discovery_box[0] if state.discovery_box else None,
            state.provider_box[0],
            kube,
            state.observability,
        )


async def _wire_and_run(config: KorvidConfig, kube: KubeClient, state: _RunState) -> None:
    """Wire everything that depends on the connected client and run the app.

    Fills *state* as pieces come alive so `_run`'s teardown guard can
    release exactly what was built, however far wiring got.
    """
    # The age memo is keyed by creation timestamps, so a context switch
    # retires every key it holds; wired here rather than imported by
    # `core` so the store keeps knowing only the `Summary` protocol.
    store = ResourceStore(on_purge=reset_age_memo)

    # Start with pods only so the UI appears immediately; full discovery runs
    # in the background and merges into this dict (closures + app share it).
    # The helm browser kinds are synthetic (Secret-backed, issue #28) and are
    # always present - discovery never returns them.
    aliases = build_alias_map([PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META])
    source = _make_watch_source(kube, aliases)
    get_manifest = _make_get_manifest(kube, aliases)

    class KubeEventsFetcher(EventsFetcher):
        """Concrete events adapter over the shared KubeClient."""

        async def fetch(
            self, namespace: str, name: str, *, uid: str | None = None
        ) -> list[dict[str, Any]]:
            return await kube.list_events_for(namespace, name, uid=uid)

    get_events = KubeEventsFetcher()

    watch_manager = WatchManager(store, source)

    # One bounded discovery round trip decides both the R keybinding and
    # whether the agent is offered the resize tool (issue #27).
    pod_resize_supported = await _probe_pod_resize(kube, readonly=config.readonly)

    # Detect the cloud provider once per connection (issue #30): it grounds
    # the agent system prompt and the Service/Ingress describe footer.
    provider_info = await _probe_cloud_provider(kube)

    # Observability connectors (issue #193): built once and shared by the
    # embedded agent and the MCP surface, so both see the same endpoint,
    # the same limits, and one connection pool per backend.
    observability = _build_observability(config)
    state.observability = observability

    agent_warnings: list[str] = []
    agent_runtime, configurator, rebuild_agent, retarget_agent, disconnect_agent, _, ui_proxy = (
        _build_agent_wiring(
            config,
            kube,
            aliases,
            pod_resize_supported=pod_resize_supported,
            cluster_context=cluster_context_note(provider_info),
            # Ownership lands in the teardown guard's box the moment the
            # provider exists, so partial agent wiring is also cleaned up.
            provider_box=state.provider_box,
            startup_warnings=agent_warnings,
            observability=observability,
        )
    )
    if agent_warnings:
        config = dataclasses.replace(config, warnings=(*config.warnings, *agent_warnings))

    mcp_hooks = _MCPAppHooks()
    mcp_controller = _build_mcp_controller(
        config, kube, aliases, ui_proxy, mcp_hooks=mcp_hooks, observability=observability
    )
    state.mcp = mcp_controller
    proposal_store = _build_proposal_store(config)

    # `:ctx` switching (issue #36): the closure needs the app (for discovery
    # restarts) and the live discovery task, both created below — boxes
    # late-bind them, mirroring ui_proxy.target.
    app_box: list[KorvidApp] = []
    discovery_box = state.discovery_box

    app = KorvidApp(
        config=config,
        store=store,
        watch_manager=watch_manager,
        list_namespaces=kube.list_namespaces,
        aliases=aliases,
        get_manifest=get_manifest,
        get_helm_components=kube.get_helm_release_components,
        get_events=get_events,
        stream_logs=kube.stream_logs,
        write_ops=kube,
        audit=AuditLog(default_audit_path(), context=config.kube_context),
        check_permission=kube.can_i,
        agent_runtime=agent_runtime,
        agent_model_name=config.agent_model,
        agent_configurator=configurator,
        rebuild_agent=rebuild_agent,
        disconnect_agent=disconnect_agent,
        # The wiring returns no configurator only when the [agent] extra is
        # absent — the app then hides the agent panel and its commands.
        agent_available=configurator is not None,
        mcp=mcp_controller,
        metrics=MetricsPoller(kube.list_pod_metrics),
        pod_resize_supported=pod_resize_supported,
        forwards=ForwardRegistry(context=config.kube_context),
        provider_hint=provider_info.display if provider_info.known else None,
        protected_context=_protected_context_name(config, config.kube_context),
        open_pod_exec=kube.open_pod_exec,
        list_contexts=list_context_names,
        probe_context=kube.probe_context,
        switch_context=_make_switch_context(
            config, kube, aliases, app_box, discovery_box, retarget_agent
        ),
        helm=_build_helm(config),
        telepresence=_build_telepresence(config),
        probe_traffic_manager=_make_traffic_manager_probe(kube),
        # Agent follow mirrors route through the same serialized proxy: the
        # built-in agent and concurrent MCP UI calls must never interleave
        # (log-pane swaps and describes are not overlap-safe).
        agent_follow_bridge=ui_proxy,
        proposal_store=proposal_store,
        save_topbar=lambda expanded: save_topbar_state(DEFAULT_CONFIG_PATH, expanded=expanded),
        list_relationship_objects=kube.list_relationship_objects,
        # Bounded session record (issue #282): the composition root owns the
        # buffer's limits, so a long session cannot grow it without bound.
        session_timeline=SessionTimeline(config.timeline_max_entries, config.timeline_max_bytes),
        # The only timeline producer the store does not already feed: a live
        # Warning-Event stream, read-only and filtered server-side.
        watch_warning_events=kube.watch_warning_events,
    )
    app_box.append(app)
    # Late-bind the UI bridge: from here on the agent's UI-control tools
    # (navigate/set_filter/open_logs/open_describe) land in this app.
    ui_proxy.target = AppUIBridge(app)
    # Follow mode (issue #153): the MCP server reads follow state from and
    # sends activity notes to the live app.
    mcp_hooks.app = app

    await _start_mcp_if_enabled(config, mcp_controller)

    discovery_box.append(asyncio.create_task(_discover_in_background(kube, aliases, app)))
    # Teardown lives in `_run`'s guard: discovery_box[0] is read there as the
    # *live* task — a `:ctx` switch may have replaced the one started above.
    await app.run_async()


RESTART_CAP = 3
RESTART_WINDOW_SECONDS = 60.0


def _run_with_recovery(
    runner: Callable[[], None],
    *,
    allow_restart: bool,
    prompt: Callable[[], str],
    clock: Callable[[], float],
) -> None:
    """Crash-recovery loop at the composition root (issue #166).

    Runs *runner* (one full `asyncio.run(_run(...))` attempt — a fresh event
    loop, fresh wiring, fresh clients) and, when it dies with an unexpected
    exception, logs the traceback and offers a restart. Clean exits,
    `KeyboardInterrupt`, and `SystemExit` propagate untouched. A cap of
    `RESTART_CAP` crashes within `RESTART_WINDOW_SECONDS` stops a
    deterministic crash loop; with *allow_restart* false (non-interactive
    stdin/stderr or `--no-restart`) the exception re-raises immediately,
    preserving today's exit-non-zero behavior.
    """
    crash_times: list[float] = []
    while True:
        try:
            runner()
            return
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            logging.getLogger(__name__).exception("korvid crashed: %s", exc)
            if not allow_restart:
                raise
            now = clock()
            crash_times = [t for t in crash_times if now - t <= RESTART_WINDOW_SECONDS]
            crash_times.append(now)
            if len(crash_times) >= RESTART_CAP:
                print(
                    f"korvid crashed {len(crash_times)} times within"
                    f" {RESTART_WINDOW_SECONDS:.0f}s -- not restarting.",
                    file=sys.stderr,
                )
                raise
            print(f"korvid crashed: {exc}", file=sys.stderr)
            answer = prompt().strip().lower()
            if answer not in ("", "y", "yes"):
                raise


def _restart_prompt() -> str:
    # Interactivity keys off stdin/stderr; a redirected stdout must neither
    # swallow the question nor be contaminated by it.
    print("korvid crashed -- restart? [Y/n] ", end="", file=sys.stderr, flush=True)
    return input()


def main() -> None:
    parser = argparse.ArgumentParser(prog="korvid", description="Kubernetes TUI with an agent.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--readonly",
        action="store_true",
        help="Disable all cluster write operations (keybindings and agent tools).",
    )
    parser.add_argument(
        "-n",
        "--namespace",
        default=None,
        help="Namespace to start in (overrides config `namespace:` and the"
        " kubeconfig context namespace).",
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help="Expose read + UI-drive tools to external MCP hosts over"
        " Streamable HTTP on 127.0.0.1 (port from config mcp.port, default 7878).",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Exit on a fatal error instead of offering to restart (issue #166).",
    )
    args = parser.parse_args()
    interactive = sys.stdin.isatty() and sys.stderr.isatty()
    _run_with_recovery(
        # Each attempt is a fresh asyncio.run: new event loop, new wiring,
        # new clients — nothing survives from a crashed run.
        lambda: asyncio.run(_run(readonly=args.readonly, mcp=args.mcp, namespace=args.namespace)),
        allow_restart=interactive and not args.no_restart,
        prompt=_restart_prompt,
        clock=time.monotonic,
    )


if __name__ == "__main__":
    main()
