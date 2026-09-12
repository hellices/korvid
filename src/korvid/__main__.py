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
import functools
import logging
import os
import secrets
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from korvid import __version__
from korvid import composition_support as _composition_support
from korvid.agent.install_hint import isolated_install_hint
from korvid.agent.interaction import (
    AgentUiBridge,
    ClusterFacts,
)
from korvid.composition_support import (
    _PROMPT_DEGRADE_HINT,
    _UNKNOWN_CLUSTER,
    AgentWiring,
    ObservabilityWiring,
    _active_model_name,
    _agent_environment,
    _agent_unavailable_wiring,
    _AgentToolUIBridgeProxy,
    _AgentUiBridgeProxy,
    _close_agent_in_background,
    _close_provider_in_background,
    _cluster_facts,
    _custom_column_names,
    _discover_in_background,
    _log_cleanup_loop_error,
    _make_disconnect_agent,
    _make_rebuild_agent,
    _MCPAppHooks,
    _missing_extra_packages,
    _own_run_tasks,
    _RunState,
    _start_mcp_if_enabled,
    _validate_ca_bundle,
    _warn_agent_disabled,
)
from korvid.composition_support import (
    _protected_context_name as _support_protected_context_name,
)
from korvid.composition_support import (
    _shutdown as _support_shutdown,
)
from korvid.composition_support import (
    _teardown as _support_teardown,
)
from korvid.core.audit import AuditLog, default_audit_path
from korvid.core.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    ConfigFileModelConnectionsWriter,
    KorvidConfig,
    ModelConnectionConfig,
    ModelConnectionsWriter,
    ObservabilityBackend,
    load_config,
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
from korvid.k8s.telepresence import (
    TRAFFIC_MANAGER_NAME,
    TRAFFIC_MANAGER_NAMESPACE,
    TelepresenceCLI,
    find_telepresence,
)
from korvid.k8s.watch_events import WatchEvent
from korvid.tools.executor import (
    ToolExecutor,
    UIBridge,
)
from korvid.tools.proposals import ProposalStore
from korvid.tools.registry import mcp_tool_schemas
from korvid.ui.agent_ui_controller import AgentUiController
from korvid.ui.app import KorvidApp
from korvid.ui.app_runtime import AppRuntime, AppRuntimeInputs, _LateReference
from korvid.ui.app_surfaces import (
    AppAgentPanel,
    AppAgentScreens,
    AppContextSurface,
    AppInspectSurface,
    AppProposalEvents,
    AppProposalScreens,
    AppReviewTasks,
    AppSessionConfiguration,
    AppTransferScreens,
    AppUIBridge,
    AppUiSurface,
    AppViewState,
    AppWorkspaceSurface,
    _RelationshipLister,
)
from korvid.ui.bridge_dispatch import AppContextDispatch
from korvid.ui.command_router import CommandRouter
from korvid.ui.context_switch_coordinator import ContextSwitchCoordinator, ContextSwitchResult
from korvid.ui.debug import DebugController, DebugSettings
from korvid.ui.drain import DrainController
from korvid.ui.forward_controller import ForwardController
from korvid.ui.helm_controller import HelmController
from korvid.ui.hints import EventsFetcher, HintController
from korvid.ui.integration_controller import IntegrationController
from korvid.ui.log_controller import LogController
from korvid.ui.operator_controller import OperatorController
from korvid.ui.proposal_controller import ProposalController
from korvid.ui.relationship_controller import RelationshipSnapshotLoader
from korvid.ui.resource_inspect_controller import ResourceInspectController
from korvid.ui.resource_write_controller import ResourceWriteController
from korvid.ui.session_timeline_controller import SessionTimelineController
from korvid.ui.shell_controller import ShellController, ShellSettings
from korvid.ui.transfer import TransferController
from korvid.ui.workspace_controller import WorkspaceController
from korvid.ui.workspace_state import WorkspaceState
from korvid.ui.write_coordinator import WriteCoordinator

if TYPE_CHECKING:
    # Embedded-agent types appear only in annotations here: an MCP-only or
    # base install must never import the agent loop or provider ABC at
    # startup (issue #73 acceptance criterion).
    from korvid.agent.model_profiles import ModelCatalog
    from korvid.agent.provider import LLMProvider
    from korvid.agent.session import AgentSession
    from korvid.providers.litellm_factory import CredentialStore

__all__ = (
    "_PROMPT_DEGRADE_HINT",
    "AgentWiring",
    "ObservabilityWiring",
    "_AgentToolUIBridgeProxy",
    "_AgentUiBridgeProxy",
    "_MCPAppHooks",
    "_RunState",
    "_active_model_name",
    "_agent_environment",
    "_close_agent_in_background",
    "_close_provider_in_background",
    "_cluster_facts",
    "_custom_column_names",
    "_discover_in_background",
    "_missing_extra_packages",
    "_protected_context_name",
    "_shutdown",
    "_start_mcp_if_enabled",
    "_teardown",
    "_validate_ca_bundle",
    "_warn_agent_disabled",
    "assemble_app_runtime",
)

logger = logging.getLogger(__name__)

_CLEANUP_GRACE_SECONDS = 5.0
_CLEANUP_CANCEL_SECONDS = 1.0
_MCP_SHUTDOWN_GRACE_SECONDS = 11.0
_RUNNER_SHUTDOWN_SECONDS = 5.0
AppT = TypeVar("AppT", bound=KorvidApp)

#: Actionable install hints (issue #73): an explicitly requested feature
#: whose extra is missing must fail with instructions, never degrade
#: silently or dump an ImportError traceback.
_MCP_INSTALL_HINT = (
    "MCP support was requested (--mcp or mcp.enabled) but its dependencies "
    f"are not installed — {isolated_install_hint(feature='mcp')}"
)
_AGENT_INSTALL_HINT = (
    "the embedded agent is enabled (agent.active names a profile in config.yaml) but its "
    f"dependencies are not installed — {isolated_install_hint(feature='agent')}"
)

#: Top-level packages each extra provides, probed explicitly before any
#: feature module is imported. Detection cannot rely on catching
#: ModuleNotFoundError: parts of an extra may arrive transitively or be
#: imported lazily (TokenStore falls back when
#: keyring is absent), which would misreport the capability as installed.
_MCP_EXTRA_ROOTS = frozenset({"mcp", "httpx2", "anyio", "starlette", "uvicorn"})
_AGENT_EXTRA_ROOTS = frozenset({"httpx", "keyring"})
#: The observability connectors need only an HTTP client.
_OBSERVABILITY_EXTRA_ROOTS = frozenset({"httpx"})
_OBSERVABILITY_INSTALL_HINT = (
    "an observability backend is configured (observability.prometheus/loki in "
    f"config.yaml) but its dependencies are not installed — {isolated_install_hint(feature='observability')}"
)


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
        # A fresh internal capability per server run: the token is
        # published only in the owner-readable endpoint file, so echoing it
        # proves same-user local file access; a restart invalidates every
        # previously handed-out token together with the pending proposals.
        token = secrets.token_urlsafe(32)
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


def _sync_cleanup_limits() -> None:
    """Keep relocated lifecycle helpers aligned with the root's test seams."""
    _composition_support._CLEANUP_GRACE_SECONDS = _CLEANUP_GRACE_SECONDS
    _composition_support._CLEANUP_CANCEL_SECONDS = _CLEANUP_CANCEL_SECONDS
    _composition_support._MCP_SHUTDOWN_GRACE_SECONDS = _MCP_SHUTDOWN_GRACE_SECONDS
    _composition_support._RUNNER_SHUTDOWN_SECONDS = _RUNNER_SHUTDOWN_SECONDS


async def _shutdown(
    discovery_task: asyncio.Task[None] | None,
    provider: LLMProvider | None,
    kube: KubeClient,
    *,
    session: AgentSession | None = None,
    close_tasks: set[asyncio.Future[Any]] | None = None,
) -> None:
    """Delegate bounded cleanup while preserving composition-root seams."""
    _sync_cleanup_limits()
    await _support_shutdown(
        discovery_task,
        provider,
        kube,
        session=session,
        close_tasks=close_tasks,
    )


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


def _create_provider_from_active_profile(
    profile: ModelConnectionConfig,
    credentials: CredentialStore | None,
    ca_bundle: str | None,
) -> LLMProvider | None:
    """Build the provider a named connection profile describes.

    The profile is the source of truth from Task 15 on: no scalar
    projection happens on the way in, so a connection the legacy
    transport could not express is built here directly. A profile the
    factory refuses returns None with the reason logged — a
    misconfigured connection disables the agent, it never stops korvid
    from starting.

    Args:
        profile: The active connection profile.
        credentials: The secret store `keyring` auth reads, or None for
            the OS keyring.
        ca_bundle: `network.ca_bundle` — one trust decision for every
            korvid-owned HTTPS client, this one included.
    """
    from korvid.providers.litellm_catalog import LiteLLMModelCatalog
    from korvid.providers.litellm_factory import create_provider_from_profile
    from korvid.providers.litellm_runtime import models_by_provider
    from korvid.providers.provider_default import ProviderDefaultRegistry
    from korvid.providers.special_flows import SpecialFlowRegistry

    # One registry, shared, and built the same way the catalog and the
    # wizard build theirs: an installed flow that is not discovered here
    # is a prefix the factory hands to routing instead of to the flow
    # that owns it, so the provider it would have built never exists.
    flows = SpecialFlowRegistry.from_entry_points(reserved_prefixes=models_by_provider())
    return create_provider_from_profile(
        profile,
        catalog=LiteLLMModelCatalog(flows=flows),
        flows=flows,
        credentials=credentials,
        provider_defaults=ProviderDefaultRegistry.from_entry_points(),
        ca_bundle=ca_bundle,
    )


def _create_initial_provider(
    config: KorvidConfig,
    credentials: CredentialStore | None = None,
) -> LLMProvider | None:
    """Build the initial LLM provider from the active connection profile.

    One factory, one input. A config with no active profile has the agent
    off, which is a `None` provider rather than a fallback path: the
    legacy scalars a second factory used to read are rejected by
    `load_config` and must be converted to a named profile before startup.
    """
    profile = config.model_connections.active_profile
    if profile is None:
        return None
    return _create_provider_from_active_profile(profile, credentials, config.network_ca_bundle)


def _resolve_agent_policy(
    provider: LLMProvider,
    config: KorvidConfig,
    model_tier: str | None,
    environment: Any,
) -> Any:
    """Route one provider onto a resolved policy.

    The catalogue decides the tier unless the operator named one, in which
    case the choice is honoured and reported as theirs — the header shows
    where the decision came from, so a silent fallback stays visible.
    """
    from korvid.agent.model_catalog import MODEL_CATALOG
    from korvid.agent.model_policy import ModelRouter

    return ModelRouter(MODEL_CATALOG).resolve(
        descriptor=provider.descriptor,
        provider_capabilities=provider.capabilities,
        # Config parsing already rejected anything but `low`/`high`/absent,
        # so the router is handed a tier it can route: it takes an explicit
        # tier as the user's own decision (route source `user`) rather than
        # validating or falling back, and the header shows it as theirs.
        explicit_tier=model_tier or None,
        environment=environment,
    )


def _build_session(
    provider: LLMProvider,
    policy: Any,
    cluster: ClusterFacts,
    *,
    config: KorvidConfig,
    kube: KubeClient,
    aliases: dict[str, ResourceMeta],
    tool_bridge: UIBridge,
    ui_bridge: AgentUiBridge,
    obs: ObservabilityWiring,
) -> AgentSession:
    """Compose one whole agent session over an already-built provider.

    Every collaborator is created here and owned by the session that comes
    out: a caller that drops the return value has dropped the whole graph,
    which is what makes rebuild a transaction.
    """
    from korvid.agent.conversation import ConversationState
    from korvid.agent.diagnostics import TurnDiagnosticsFactory
    from korvid.agent.evidence import EvidenceLedger
    from korvid.agent.native_engine import NativeAgentEngine
    from korvid.agent.prompt_harness import PromptHarness
    from korvid.agent.request_gateway import RequestGateway
    from korvid.agent.session import DefaultAgentSession
    from korvid.agent.tool_harness import ToolHarness

    execution = ToolExecutor(
        kube,
        aliases,
        ui=tool_bridge,
        custom_columns=_custom_column_names(config),
        metrics=obs.metrics,
        logs=obs.logs,
    )
    tools = ToolHarness(
        policy=policy,
        execution=execution,
        bridge=ui_bridge,
        evidence=EvidenceLedger(),
    )
    conversation = ConversationState(
        max_history_chars=policy.max_history_chars,
        strict_history_budget=policy.strict_history_budget,
    )
    gateway = RequestGateway(provider, RequestGateway.prepare_policy(policy))
    engine = NativeAgentEngine(conversation=conversation, gateway=gateway, tools=tools)
    return DefaultAgentSession(
        engine=engine,
        bridge=ui_bridge,
        prompt_harness=PromptHarness(),
        conversation=conversation,
        gateway=gateway,
        tools=tools,
        policy=policy,
        cluster=cluster,
        user_rules=config.agent_rules,
        diagnostics_factory=TurnDiagnosticsFactory(),
    )


def _build_model_catalog(
    *, ca_bundle: str | None = None, models_dev: bool = True
) -> ModelCatalog | None:
    """Build the catalog, or None when the agent extra is absent.

    A missing extra degrades to None — the TUI runs without an agent.
    A *broken* extra is different: it is reported, not swallowed.

    The wizard's connection test is a real request built by the same
    factory the running agent uses, trust and credential store included,
    so a profile that tests green cannot fail differently at startup.

    Nothing here touches the network. `ModelsDevSource()` reads whatever
    cache is already on disk and stops; the only thing that revalidates it
    is the setup UI's explicit refresh action, through
    `ModelCatalog.refresh_metadata`.

    Args:
        ca_bundle: `network.ca_bundle` — one trust decision for every
            korvid-owned HTTPS client, the probe's, the models.dev
            refresh's and setup discovery's included. The bundle is held,
            not opened: a client is built when an operator asks for a
            refresh or a listing, so a misconfigured path can never keep
            the TUI from starting.
        models_dev: `agent.model_search.models_dev`. `False` builds **no**
            metadata source at all rather than a source nobody calls:
            an air-gapped deployment's guarantee is that the object which
            could make the request does not exist, and the refresh action
            then reports itself disabled.
    """
    try:
        from korvid.providers.endpoint_discovery import EndpointDiscovery
        from korvid.providers.litellm_catalog import LiteLLMModelCatalog
        from korvid.providers.litellm_runtime import models_by_provider
        from korvid.providers.models_dev import ModelMetadataSource, ModelsDevSource
        from korvid.providers.profile_probe import ProfileProbe
        from korvid.providers.provider_default import ProviderDefaultRegistry
        from korvid.providers.special_flows import SpecialFlowRegistry
        from korvid.providers.token_store import TokenStore
    except ImportError:
        return None
    # Constructed only when enabled: "disabled" has to mean the object that
    # could make the request does not exist, not that nobody calls it.
    enrichment: ModelMetadataSource | None = (
        ModelsDevSource(ca_bundle=ca_bundle) if models_dev else None
    )
    flows = SpecialFlowRegistry.from_entry_points(reserved_prefixes=models_by_provider())
    return LiteLLMModelCatalog(
        flows=flows,
        enrichment=enrichment,
        discovery=EndpointDiscovery(ca_bundle=ca_bundle),
        tester=ProfileProbe(
            catalog=LiteLLMModelCatalog(flows=flows),
            flows=flows,
            credentials=TokenStore(),
            provider_defaults=ProviderDefaultRegistry.from_entry_points(),
            ca_bundle=ca_bundle,
        ),
    )


def _build_agent_wiring(
    config: KorvidConfig,
    kube: KubeClient,
    aliases: dict[str, ResourceMeta],
    *,
    pod_resize_supported: bool = False,
    cluster: ClusterFacts | None = None,
    provider_box: list[LLMProvider | None] | None = None,
    session_box: list[AgentSession | None] | None = None,
    close_tasks: set[asyncio.Future[Any]] | None = None,
    startup_warnings: list[str] | None = None,
    observability: ObservabilityWiring | None = None,
) -> AgentWiring:
    """Build the initial agent session plus the `:ai` wizard's rebuild hook.

    Provider adapters and credential storage are optional (issue #73): a
    base installation gets a session-less wiring whose `:ai` command reports
    the feature as unavailable, while a config that explicitly enables the
    agent fails with an actionable install hint.
    """
    ui_proxy = _AgentToolUIBridgeProxy()
    agent_ui_proxy = _AgentUiBridgeProxy()
    obs = observability or ObservabilityWiring()
    # The caller may hand in the boxes that its teardown guard reads (issue
    # #166): provider and session are owned by those boxes from the moment
    # they exist, so a failure in the *rest* of the wiring still cleans up.
    if provider_box is None:
        provider_box = [None]
    if session_box is None:
        session_box = [None]
    missing = _missing_extra_packages(_AGENT_EXTRA_ROOTS)
    if missing:
        return _agent_unavailable_wiring(
            config, missing, ui_proxy, agent_ui_proxy, provider_box, session_box
        )

    # Deferred behind the capability probe: the agent loop is only composed
    # when this wiring is actually built (issue #73 requires MCP-only
    # startups not to import the session or the engine at all).
    from korvid.providers.token_store import TokenStore

    token_store = TokenStore()
    provider = _create_initial_provider(config, token_store)
    # Ownership transfers immediately: if anything below raises (tools or
    # session), the teardown guard still closes the provider — a provider
    # can eagerly hold a credential HTTP client.
    provider_box[0] = provider

    # Per-cluster agent inputs: a `:ctx` switch replaces both, so a wizard
    # rebuild after the switch is armed for the cluster the user is on, not
    # the one korvid started against. The requested tier rides along so a
    # retarget re-resolves the same intent (issue #71).
    resize_box: list[bool] = [pod_resize_supported]
    cluster_box: list[ClusterFacts] = [cluster if cluster is not None else _UNKNOWN_CLUSTER]
    tier_box: list[str | None] = [config.agent_model_tier]

    def compose(built: LLMProvider, model_tier: str | None) -> tuple[AgentSession, Any]:
        environment = _agent_environment(config, resize_box[0], obs.backends)
        policy = _resolve_agent_policy(built, config, model_tier, environment)
        session = _build_session(
            built,
            policy,
            cluster_box[0],
            config=config,
            kube=kube,
            aliases=aliases,
            tool_bridge=ui_proxy,
            ui_bridge=agent_ui_proxy,
            obs=obs,
        )
        return session, policy

    from korvid.agent.model_policy import ModelRoutingError

    if provider is not None:
        # Imported here, not above: with the agent off there is no session
        # to compose and no refusal to classify, and the prompt harness is
        # part of the session graph a disabled start must not pull in
        # (`tests/test_optional_extras.py`).
        from korvid.agent.prompt_harness import PromptCompositionError

        try:
            session_box[0] = compose(provider, tier_box[0])[0]
        except (ModelRoutingError, PromptCompositionError) as error:
            # A model that cannot call tools, or a system prompt that does
            # not fit it, is a configuration problem — not a reason to
            # refuse to start: korvid comes up with the agent off and a
            # warning, and `:ai` can point it elsewhere. The provider stays
            # in the box, so teardown still releases it.
            _warn_agent_disabled(error, startup_warnings)

    if close_tasks is None:
        close_tasks = set()

    def build_provider(profile: ModelConnectionConfig) -> LLMProvider | None:
        return _create_provider_from_active_profile(profile, token_store, config.network_ca_bundle)

    return AgentWiring(
        session=session_box[0],
        available=True,
        rebuild=_make_rebuild_agent(
            build_provider, compose, provider_box, session_box, tier_box, close_tasks
        ),
        retarget=_make_retarget_agent(config, obs, provider_box, resize_box, cluster_box, tier_box),
        disconnect=_make_disconnect_agent(provider_box, session_box, close_tasks),
        provider_box=provider_box,
        session_box=session_box,
        tool_bridge=ui_proxy,
        ui_bridge=agent_ui_proxy,
    )


def _profile_writer() -> ModelConnectionsWriter:
    """The seam the UI persists profiles through.

    Built here and nowhere else: the composition root owns every path
    korvid writes to, so the screens are handed a writer rather than a
    location. The path is read at call time so a test can rebind
    `DEFAULT_CONFIG_PATH` and get a writer that honours it.

    Failures propagate out of the returned writer: the caller applied the
    profile to the live session already and must tell the operator the
    change reverts on restart.

    `model_tier` defaults to leaving `agent.model_tier` untouched — only
    the first-run wizard, which actually asks, sends one, and it lands in
    the same write as the profiles so the two can never disagree.
    """
    return ConfigFileModelConnectionsWriter(DEFAULT_CONFIG_PATH)


def _make_retarget_agent(
    config: KorvidConfig,
    obs: ObservabilityWiring,
    provider_box: list[LLMProvider | None],
    resize_box: list[bool],
    cluster_box: list[ClusterFacts],
    tier_box: list[str | None],
) -> Callable[[AgentSession | None, bool, ClusterFacts | None], None]:
    """Re-arm the agent for a new cluster (issue #36, `:ctx`).

    The policy is re-resolved from the *current* provider's facts and the
    new cluster's environment, so the switch picks up capabilities the new
    cluster has (resize) without changing the routed model — a retarget
    that would move the routed model is refused by
    `AgentSession.retarget`, because the model is the wizard's to change,
    not a context switch's. Conversation history survives; what the next
    turn is looking at does not.

    The boxes are updated even when there is no live session: a later
    wizard rebuild must arm the agent for the cluster the user is on, not
    the one korvid started against.

    A failure here — re-resolution or the session's own refusal — is
    raised, not logged. Retargeting is one step of the `:ctx` transaction,
    and that transaction owns rollback and telling the user (it records a
    failed switch, notifies, and returns to the previous context). Absorbing
    the failure would report a successful switch while the session still
    holds the *previous* cluster's policy and evidence, and the agent would
    answer questions about the new cluster from the old one's facts. Failing
    closed keeps the agent's idea of the cluster and the UI's the same one.
    """

    def retarget_agent(
        session: AgentSession | None,
        pod_resize_supported: bool,
        cluster: ClusterFacts | None,
    ) -> None:
        resize_box[0] = pod_resize_supported
        if cluster is not None:
            cluster_box[0] = cluster
        live_provider = provider_box[0]
        if session is None or live_provider is None:
            return
        environment = _agent_environment(config, pod_resize_supported, obs.backends)
        policy = _resolve_agent_policy(live_provider, config, tier_box[0], environment)
        session.retarget(policy, cluster_box[0])

    return retarget_agent


def _load_startup_config(
    readonly: bool, mcp: bool = False, namespace: str | None = None
) -> KorvidConfig:
    try:
        config = load_config()
    except ConfigError as exc:
        # One clear, actionable line — never an unfiltered traceback — and
        # unconditional: a stale removed key must fail startup even when
        # the agent block would otherwise be disabled.
        raise SystemExit(f"korvid: {exc}") from exc
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
    return config


async def _teardown(state: _RunState, kube: KubeClient) -> None:
    """Delegate full teardown while preserving composition-root seams."""
    _sync_cleanup_limits()
    await _support_teardown(state, kube)


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
    """Delegate protection matching while retaining the root monkeypatch seam."""
    return _support_protected_context_name(config, context, resolve_context_name)


def _make_switch_context(
    config: KorvidConfig,
    kube: KubeClient,
    aliases: dict[str, ResourceMeta],
    app_box: list[KorvidApp],
    discovery_box: list[asyncio.Task[None]],
    retarget_agent: Callable[[AgentSession | None, bool, ClusterFacts | None], None],
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
        # The surviving conversation must be re-armed for this cluster:
        # typed cluster facts the prompt harness renders, and a tool
        # surface gated by the new cluster's capabilities (issue #36).
        retarget_agent(
            app_box[0].agent_session if app_box else None,
            pod_resize_supported,
            _cluster_facts(provider_info),
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
) -> Callable[[str, str], AsyncIterator[WatchEvent[Summary]]]:
    """Watch source for the WatchManager: kind + scope -> summary events.

    Extracted from _run for complexity; *aliases* is the live shared dict
    that background discovery mutates.
    """

    async def source(kind: str, scope: str) -> AsyncIterator[WatchEvent[Summary]]:
        ns = None if scope == ALL_NAMESPACES else scope
        meta = aliases.get(kind)
        if meta is None:
            logger.warning("Unknown resource kind %r requested for watch; stopping", kind)
            raise ValueError(f"Unknown resource kind: {kind!r}")
        async for event in kube.watch_resources(meta, ns):
            yield event

    return source


def _make_get_manifest(
    kube: KubeClient, aliases: dict[str, ResourceMeta]
) -> Callable[[str, str | None, str], Awaitable[dict[str, Any]]]:
    """Describe fetcher: helm kinds decode release Secrets, the rest GET raw."""

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        meta = aliases.get(kind)
        if meta is None:
            raise ValueError(f"Unknown resource kind: {kind!r}")
        if meta.identity == HELM_RELEASES_META.identity:
            if namespace is None:
                raise ValueError("helm releases are namespaced; namespace required")
            return await kube.get_helm_release(namespace, name)
        if meta.identity == HELM_REVISIONS_META.identity:
            # Revision rows are named "<release>.v<revision>".
            release, _, rev = name.rpartition(".v")
            if namespace is None or not release or not rev.isdigit():
                raise ValueError(f"not a helm revision row: {name!r}")
            return await kube.get_helm_release(namespace, release, revision=int(rev))
        return await kube.get_object(meta, namespace, name)

    return get_manifest


def _construct_app_runtime(app: KorvidApp, inputs: AppRuntimeInputs) -> AppRuntime:
    """Construct the complete session-scoped controller graph."""
    config = inputs.config
    workspace_ref = _LateReference[WorkspaceController]()
    logs_ref = _LateReference[LogController]()
    hints_ref = _LateReference[HintController]()
    timeline_ref = _LateReference[SessionTimelineController]()
    proposals_ref = _LateReference[ProposalController]()
    forward_ref = _LateReference[ForwardController]()
    writes_ref = _LateReference[WriteCoordinator]()
    agent_ref = _LateReference[AgentUiController]()
    shell_ref = _LateReference[ShellController]()
    debug_ref = _LateReference[DebugController]()

    view = AppViewState(app)
    relationship_loader: RelationshipSnapshotLoader | None = (
        RelationshipSnapshotLoader(_RelationshipLister(inputs.list_relationship_objects))
        if inputs.list_relationship_objects is not None
        else None
    )
    context = ContextSwitchCoordinator(
        ui=AppUiSurface(app),
        surface=AppContextSurface(app),
        view=view,
        session=AppSessionConfiguration(app),
        store=app.store,
        watches=app.watch_manager,
        workspace=workspace_ref.get,
        logs=logs_ref.get,
        hints=hints_ref.get,
        timeline=timeline_ref.get,
        proposals=proposals_ref.get,
        forwards=forward_ref.get,
        registry=lambda: app._forwards,
        writes=writes_ref.get,
        agent=agent_ref.get,
        mcp=lambda: app._mcp,
        audit=lambda: app._audit,
        list_contexts=inputs.list_contexts,
        probe_context=inputs.probe_context,
        switch_context=inputs.switch_context,
    )
    timeline = SessionTimelineController(
        ui=AppUiSurface(app),
        view=view,
        watch_manager=app.watch_manager,
        timeline=inputs.session_timeline,
        get_epoch=context.epoch,
        epoch_crossed=context.crossed,
        watch_warning_events=inputs.watch_warning_events,
        selected_resource=lambda: workspace_ref.get().selected_timeline_resource(),
        navigate=lambda kind, namespace, name, epoch: workspace_ref.get().jump_to_object(
            kind, namespace, name, epoch=epoch
        ),
    )
    timeline_ref.bind(timeline)
    writes = WriteCoordinator(
        ui=AppUiSurface(app),
        view=view,
        context=context,
        audit=lambda: app._audit,
        timeline=timeline,
        check_permission=lambda: app._check_permission,
        relationship_loader=lambda: relationship_loader,
        focused_pane=lambda: app._pane,
        canonical_meta_kind=app._canonical_meta_kind,
        protected_context=inputs.protected_context,
    )
    writes_ref.bind(writes)
    bridge_dispatch = AppContextDispatch()
    inspect_surface = AppInspectSurface(app)
    inspect_controller = ResourceInspectController(
        ui=AppUiSurface(app),
        view=view,
        context=context,
        surface=inspect_surface,
        shell=shell_ref.get,
        logs=logs_ref.get,
        get_manifest=lambda: app._get_manifest,
        get_events=lambda: app._get_events,
        stream_logs=lambda: app._stream_logs,
        target_uid=lambda kind, ns, name: app._target_uid(kind, ns, name),
        audit=lambda: app._audit,
        provider_hint=lambda: app._provider_hint,
    )
    shell = ShellController(
        gate=writes,
        view=view,
        ui=AppUiSurface(app),
        debug=debug_ref.get,
        audit=lambda: app._audit,
        get_manifest=lambda: app._get_manifest,
        pod_containers=inspect_controller.pod_containers,
        node_target=lambda action: app._node_target(action),
        target_uid=lambda kind, ns, name: app._target_uid(kind, ns, name),
        settings=lambda: ShellSettings(
            kube_context=app.config.kube_context,
            debug_default_image=app.config.debug_default_image,
            debug_images=app.config.debug_images,
            node_shell_image=app.config.node_shell_image,
            node_shell_namespace=app.config.node_shell_namespace,
        ),
    )
    shell_ref.bind(shell)
    forward_controller = ForwardController(
        gate=writes,
        ui=AppUiSurface(app),
        view=view,
        forwards=lambda: app._forwards,
        audit=lambda: app._audit,
        get_manifest=lambda: app._get_manifest,
    )
    forward_ref.bind(forward_controller)
    transfer = TransferController(
        ui=AppUiSurface(app),
        view=view,
        writes=writes,
        screens=AppTransferScreens(app),
        open_pod_exec=lambda: app._open_pod_exec,
        audit=lambda: app._audit,
        find_pod=inspect_controller.find_pod,
        target_uid=lambda kind, ns, name: app._target_uid(kind, ns, name),
        pod_uid_unchanged=inspect_controller.pod_uid_unchanged,
    )
    operators = OperatorController(
        gate=writes,
        view=view,
        ui=AppUiSurface(app),
        write_ops=lambda: app._write_ops,
        get_manifest=lambda: app._get_manifest,
        confirm_screen=writes.confirm_screen,
        uid_intact_after_fetch=writes.uid_intact_after_fetch,
        precheck_keybinding_write=writes.precheck_keybinding_write,
        write_target=writes.write_target,
    )
    helm_controller = HelmController(
        helm=lambda: app._helm,
        get_release_identity=lambda: app._get_helm_release_identity,
        gate=writes,
        view=view,
        ui=AppUiSurface(app),
        navigation=workspace_ref.get,
        edit_in_external_editor=lambda *args, **kwargs: app._edit_in_external_editor(
            *args, **kwargs
        ),
        edit_text=lambda: app._edit_text,
    )
    debug = DebugController(
        ui=AppUiSurface(app),
        audit=lambda: app._audit,
        readonly=lambda: app.config.readonly,
        settings=lambda: DebugSettings(
            kube_context=app.config.kube_context,
            default_image=app.config.debug_default_image,
            images=app.config.debug_images,
        ),
        pod_uid_unchanged=inspect_controller.pod_uid_unchanged,
        get_epoch=context.epoch,
        epoch_crossed=context.crossed,
        confirm_screen=writes.confirm_screen,
        run_debug=lambda: shell_ref.get().run_debug,
    )
    debug_ref.bind(debug)
    drain = DrainController(
        notify=app.notify,
        audit_write=writes.audit_write,
        set_progress=functools.partial(app._set_progress, "drain"),
    )
    resource_writes = ResourceWriteController(
        writes=writes,
        view=view,
        ui=AppUiSurface(app),
        drain=drain,
        write_ops=lambda: app._write_ops,
        get_manifest=lambda: app._get_manifest,
        edit_text=lambda: app._edit_text,
        managed_note=app._managed_note,
        managed_note_from=app._managed_note_from,
        pod_resize_supported=lambda: app._pod_resize_supported,
        helm_uninstall=lambda: helm_controller.uninstall_selected(),
        operators=operators,
    )
    workspace = WorkspaceState("pods", config.namespace or "default")
    hints = HintController(
        find_pod_summary=inspect_controller.find_pod_summary,
        cursor_row_key=inspect_surface.cursor_row_key,
        on_pods_view=lambda: app.current_kind == "pods",
        get_events=lambda: app._get_events,
        show_trouble=inspect_surface.show_trouble,
        clear_hint=inspect_surface.clear_hint,
        start_fetch=lambda coro: app.run_worker(coro, exclusive=True, group="hint-events"),
        set_timer=app.set_timer,
        ctx_epoch=context.epoch,
        ctx_crossed=context.crossed,
    )
    hints_ref.bind(hints)
    logs = LogController(
        ui=AppUiSurface(app),
        get_log_pane=lambda: app._log_pane,
        get_stream_logs=lambda: app._stream_logs,
        pod_containers=inspect_controller.pod_containers,
        selected_ns_name=view.selected_ns_name,
        visible_pod_keys=lambda: [str(row.key.value) for row in app._focused_table().ordered_rows],
        current_kind=lambda: app.current_kind,
        focused_pane=lambda: app._pane,
        ctx_epoch=context.epoch,
        ctx_switch_crossed=context.crossed,
        ctx_reads_allowed=context.reads_allowed,
        refresh_bindings=app.refresh_bindings,
        buffer_max_lines=config.log_buffer_lines,
    )
    logs_ref.bind(logs)
    workspace_controller = WorkspaceController(
        state=workspace,
        store=app.store,
        watch_manager=app.watch_manager,
        metrics=app._metrics,
        relationship_loader=relationship_loader,
        ui=AppUiSurface(app),
        surface=AppWorkspaceSurface(app),
        view=view,
        context=context,
        logs=logs,
        hints=hints,
        config=lambda: app.config,
        get_manifest=lambda: app._get_manifest,
        get_helm_components=lambda: app._get_helm_components,
        olm_alias_key=operators.alias_key,
        describe_named=inspect_controller.describe_named,
        check_permission=lambda: app._check_permission,
        list_namespaces=lambda: app._list_namespaces,
    )
    workspace_ref.bind(workspace_controller)
    proposals = ProposalController(
        store=inputs.proposal_store,
        ui=AppUiSurface(app),
        screens=AppProposalScreens(app),
        tasks=AppReviewTasks(app),
        events=AppProposalEvents(app),
        context=context,
        writes=writes,
        navigation=workspace_controller,
        builder=agent_ref.get,
        config=lambda: app.config,
        audit=lambda: app._audit,
        approval_timeout_seconds=inputs.approval_timeout_seconds,
        refresh_status=lambda: app._refresh_status(),
    )
    proposals_ref.bind(proposals)
    integrations = IntegrationController(
        ui=AppUiSurface(app),
        context=context,
        proposals=proposals,
        serializer=workspace_controller,
        mcp=lambda: app._mcp,
        telepresence=inputs.telepresence,
        probe_traffic_manager=inputs.probe_traffic_manager,
        telepresence_enabled=lambda: app.config.telepresence_enabled,
        follow_enabled=config.mcp_follow,
        refresh_status=lambda: app._refresh_status(),
    )
    agent_ui = AgentUiController(
        panel=AppAgentPanel(app),
        screens=AppAgentScreens(app),
        ui=AppUiSurface(app),
        view=view,
        context=context,
        writes=writes,
        workspace=workspace,
        navigation=workspace_controller,
        logs=logs,
        proposals=proposals,
        dispatch=bridge_dispatch,
        config=lambda: app.config,
        get_manifest=lambda: app._get_manifest,
        get_events=lambda: app._get_events,
        stream_logs=lambda: app._stream_logs,
        pod_containers=inspect_controller.pod_containers,
        write_ops=lambda: app._write_ops,
        audit=lambda: app._audit,
        pod_resize_supported=lambda: app._pod_resize_supported,
        provider_hint=lambda: app._provider_hint,
        approval_timeout_seconds=inputs.approval_timeout_seconds,
        refresh_status=lambda: app._refresh_status(),
        follow_bridge=lambda: inputs.agent_follow_bridge,
        session=inputs.agent_session,
        model_name=inputs.agent_model_name,
        catalog=inputs.agent_catalog,
        save_profiles=inputs.agent_save_profiles,
        rebuild=inputs.rebuild_agent,
        disconnect=inputs.disconnect_agent,
        available=inputs.agent_available,
    )
    agent_ref.bind(agent_ui)
    commands = CommandRouter(
        ui=AppUiSurface(app),
        agent=agent_ui,
        integrations=integrations,
        proposals=proposals,
        forwards=forward_controller,
        operators=operators,
    )
    return AppRuntime(
        view=view,
        relationship_loader=relationship_loader,
        context=context,
        timeline=timeline,
        writes=writes,
        bridge_dispatch=bridge_dispatch,
        inspect_surface=inspect_surface,
        inspect=inspect_controller,
        shell=shell,
        forward=forward_controller,
        transfer=transfer,
        operators=operators,
        helm=helm_controller,
        debug=debug,
        drain=drain,
        resource_writes=resource_writes,
        workspace=workspace,
        hints=hints,
        logs=logs,
        workspace_controller=workspace_controller,
        proposals=proposals,
        integrations=integrations,
        agent_ui=agent_ui,
        commands=commands,
    )


def assemble_app_runtime(app: AppT) -> AppT:
    """Construct and bind one app runtime at the composition root."""
    runtime = _construct_app_runtime(app, app.runtime_inputs)
    app.bind_runtime(runtime)
    return app


async def _run(readonly: bool = False, mcp: bool = False, namespace: str | None = None) -> None:
    preexisting_tasks = asyncio.all_tasks()
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
    state = _RunState(preexisting_tasks=preexisting_tasks)
    with _own_run_tasks():
        try:
            await _wire_and_run(config, kube, state)
        finally:
            await _teardown(state, kube)


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
    agent = _build_agent_wiring(
        config,
        kube,
        aliases,
        pod_resize_supported=pod_resize_supported,
        cluster=_cluster_facts(provider_info),
        # Ownership lands in the teardown guard's boxes the moment provider
        # and session exist, so partial agent wiring is also cleaned up.
        provider_box=state.provider_box,
        session_box=state.session_box,
        close_tasks=state.close_tasks,
        startup_warnings=agent_warnings,
        observability=observability,
    )
    ui_proxy = agent.tool_bridge
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
        get_helm_release_identity=kube.get_helm_release_identity,
        get_events=get_events,
        stream_logs=kube.stream_logs,
        write_ops=kube,
        audit=AuditLog(default_audit_path(), context=config.kube_context),
        check_permission=kube.can_i,
        agent_session=agent.session,
        agent_model_name=_active_model_name(config),
        # The profile screens' single source of answers, and the one path
        # that writes `agent.profiles` back (issue #182).
        agent_catalog=_build_model_catalog(
            ca_bundle=config.network_ca_bundle,
            models_dev=config.agent_model_search_models_dev,
        ),
        agent_save_profiles=_profile_writer(),
        rebuild_agent=agent.rebuild,
        disconnect_agent=agent.disconnect,
        # The wiring reports unavailable only when the [agent] extra is
        # absent — the app then hides the agent panel and its commands.
        agent_available=agent.available,
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
            config, kube, aliases, app_box, discovery_box, agent.retarget
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
    app = assemble_app_runtime(app)
    app_box.append(app)
    # Late-bind both ports: from here on the agent's UI-control tools
    # (navigate/set_filter/open_logs/open_describe) land in this app, and
    # the session reads its workspace snapshots from the live controller.
    ui_proxy.target = AppUIBridge(app)
    agent.ui_bridge.target = app.agent_ui.workspace_bridge
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


def _force_runner_exit() -> None:
    """Exit without blocking I/O or logging locks on the watchdog thread."""
    os._exit(1)


def _close_runner(runner: asyncio.Runner) -> None:
    """Bound stdlib finalization without cancelling executor shutdown.

    A daemon watchdog covers task gathering, asynchronous generators, and
    executor threads even if Python 3.11 blocks the loop in thread.join().
    It is armed only after run-owned client cleanup. Expiry or failure is
    terminal, forfeiting remaining finalizers rather than offering recovery.
    """
    runner.get_loop().set_exception_handler(_log_cleanup_loop_error)
    watchdog = threading.Timer(_RUNNER_SHUTDOWN_SECONDS, _force_runner_exit)
    watchdog.daemon = True
    watchdog.start()
    try:
        runner.close()
    except (Exception, KeyboardInterrupt, SystemExit):
        try:
            logger.critical("Event loop finalization failed; exiting without restart")
        finally:
            _force_runner_exit()
    finally:
        watchdog.cancel()


def _run_once(readonly: bool = False, mcp: bool = False, namespace: str | None = None) -> None:
    """Give each recovery attempt a fresh loop with bounded finalization."""
    runner = asyncio.Runner()
    try:
        runner.get_loop().set_exception_handler(_log_cleanup_loop_error)
        runner.run(_run(readonly=readonly, mcp=mcp, namespace=namespace))
    finally:
        _close_runner(runner)


def main() -> None:
    if sys.argv[1:2] == ["mcp"]:
        from korvid.cli import main as cli_main

        cli_main()
        return
    parser = argparse.ArgumentParser(
        prog="korvid",
        description="Kubernetes TUI with an agent.",
        epilog="Connect an MCP host to a running TUI with: korvid mcp stdio [--instance PID]",
    )
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
        help="Enable the TUI-owned local MCP endpoint for korvid mcp stdio"
        " (loopback port from config mcp.port, default 7878).",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Exit on a fatal error instead of offering to restart (issue #166).",
    )
    args = parser.parse_args()
    interactive = sys.stdin.isatty() and sys.stderr.isatty()
    _run_with_recovery(
        lambda: _run_once(readonly=args.readonly, mcp=args.mcp, namespace=args.namespace),
        allow_restart=interactive and not args.no_restart,
        prompt=_restart_prompt,
        clock=time.monotonic,
    )


if __name__ == "__main__":
    main()
