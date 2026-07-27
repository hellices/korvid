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
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from korvid.agent.context import cluster_context_note
from korvid.agent.mcp_server import KorvidMCPServer, MCPController, default_endpoint_path
from korvid.agent.profiles import build_profile
from korvid.agent.provider import LLMProvider
from korvid.agent.runtime import AgentRuntime
from korvid.agent.setup import AgentSettings
from korvid.agent.tools import (
    READ_TOOLS,
    UI_TOOLS,
    ToolExecutor,
    UIBridge,
)
from korvid.core.audit import AuditLog, default_audit_path
from korvid.core.config import DEFAULT_CONFIG_PATH, KorvidConfig, load_config, save_agent_config
from korvid.core.portforward import ForwardRegistry
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.client import KubeClient, resolve_context_name, resolve_context_namespace
from korvid.k8s.csp import ProviderInfo, detect_provider
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META
from korvid.k8s.helmcli import HelmCLI, find_helm
from korvid.k8s.metrics import MetricsPoller
from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP
from korvid.providers.configurator import ProviderConfigurator
from korvid.providers.ollama import OllamaOptions
from korvid.providers.registry import create_provider
from korvid.providers.token_store import TokenStore
from korvid.ui.app import AppUIBridge, EventsFetcher, KorvidApp
from korvid.ui.widgets.resource_table import sanitize_views

logger = logging.getLogger(__name__)


def _make_mcp_factory(
    config: KorvidConfig,
    kube: KubeClient,
    aliases: dict[str, ResourceMeta],
    ui: UIBridge | None,
) -> Callable[[], KorvidMCPServer]:
    """Factory the :mcp controller uses to build a fresh server per start.

    The surface is read + UI-drive tools only - write tools stay with the
    built-in agent until an approval UX for external callers is designed
    (issue #11 non-goal)."""

    def factory() -> KorvidMCPServer:
        return KorvidMCPServer(
            ToolExecutor(kube, aliases, ui=ui),
            READ_TOOLS + UI_TOOLS,
            port=config.mcp_port,
            endpoint_path=default_endpoint_path(),
        )

    return factory


async def _shutdown(
    discovery_task: asyncio.Task[None], provider: LLMProvider | None, kube: KubeClient
) -> None:
    """Tear down background work and owned clients; each step is attempted
    even if an earlier one raises."""
    try:
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
            logger.debug("old provider close failed", exc_info=t.exception())

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


def _build_agent_wiring(
    config: KorvidConfig,
    kube: KubeClient,
    aliases: dict[str, ResourceMeta],
    *,
    pod_resize_supported: bool = False,
    cluster_context: str | None = None,
) -> tuple[
    AgentRuntime | None,
    ProviderConfigurator,
    Callable[[AgentSettings], AgentRuntime | None],
    list[LLMProvider | None],
    _UIBridgeProxy,
]:
    """Build the initial agent runtime plus the :ai wizard's configurator/rebuild hooks."""
    token_store = TokenStore()
    ui_proxy = _UIBridgeProxy()
    # Model-capability profile (issue #71): tool surface, budgets, and
    # prompts come from one place so the initial build and every wizard
    # rebuild stay consistent. In readonly mode the model is never even
    # told write tools exist; resize is offered only when discovery found
    # pods/resize (1.35 GA).
    profile = build_profile(
        config.agent_profile or "full",
        readonly=config.readonly,
        resize_supported=pod_resize_supported,
    )
    oauth = token_store.load("github-oauth") if config.agent_provider == "github-copilot" else None
    ollama_options = OllamaOptions(
        num_ctx=config.agent_ollama_num_ctx,
        temperature=config.agent_ollama_temperature,
        seed=config.agent_ollama_seed,
        think=config.agent_ollama_think,
        keep_alive=config.agent_ollama_keep_alive,
    )
    provider = create_provider(
        enabled=config.agent_enabled,
        provider=config.agent_provider,
        auth_method=config.agent_auth_method,
        base_url=config.agent_base_url,
        model=config.agent_model,
        api_key_env=config.agent_api_key_env,
        oauth_token=oauth,
        ollama=ollama_options,
    )
    agent_runtime = (
        AgentRuntime(
            provider,
            ToolExecutor(kube, aliases, ui=ui_proxy),
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

    # Mutable holder so rebuild_agent/_shutdown always see the live provider.
    provider_box: list[LLMProvider | None] = [provider]

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

    configurator = ProviderConfigurator(token_store, persist)
    close_tasks: set[asyncio.Task[None]] = set()

    def rebuild_agent(settings: AgentSettings) -> AgentRuntime | None:
        old = provider_box[0]
        if old is not None:
            # Close in the background; the new provider takes over immediately.
            _close_provider_in_background(old, close_tasks)
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
        )
        provider_box[0] = new_provider
        if new_provider is None:
            return None
        # The wizard's rebuild carries its own profile choice (e.g. small
        # when the provider is ollama).
        new_profile = build_profile(
            settings.profile,
            readonly=config.readonly,
            resize_supported=pod_resize_supported,
        )
        return AgentRuntime(
            new_provider,
            ToolExecutor(kube, aliases, ui=ui_proxy),
            tools=new_profile.tools,
            max_iterations=new_profile.max_iterations,
            max_history_chars=new_profile.max_history_chars,
            max_result_chars=new_profile.max_result_chars,
            max_tool_calls_per_iteration=new_profile.max_tool_calls_per_iteration,
            strict_history_budget=new_profile.strict_history_budget,
            system_prompt=new_profile.system_prompt,
            ui_prompt=new_profile.ui_prompt,
            cluster_context=cluster_context,
        )

    return agent_runtime, configurator, rebuild_agent, provider_box, ui_proxy


def _load_startup_config(readonly: bool, mcp: bool = False) -> KorvidConfig:
    config = load_config()
    if readonly:
        config = dataclasses.replace(config, readonly=True)
    if mcp:
        config = dataclasses.replace(config, mcp_enabled=True)
    # Pin the actual context name so kubectl subprocesses (shell/debug) and the
    # status bar reference this cluster even if current-context changes later.
    resolved_ctx = resolve_context_name(config.kube_context)
    if resolved_ctx != config.kube_context:
        config = dataclasses.replace(config, kube_context=resolved_ctx)
    # Kind-aware column validation lives in the UI layer (only it knows each
    # kind's headers); config parsing already rejected the universal names.
    views, view_warnings = sanitize_views(config.views)
    if view_warnings:
        config = dataclasses.replace(
            config, views=views, warnings=(*config.warnings, *view_warnings)
        )
    return config


async def _start_mcp_if_enabled(config: KorvidConfig, controller: MCPController) -> None:
    if not config.mcp_enabled:
        return
    startup_msg = await controller.start()
    if startup_msg.startswith("ERROR"):
        logger.error("%s", startup_msg)


async def _teardown(
    controller: MCPController,
    discovery_task: asyncio.Task[None],
    provider: LLMProvider | None,
    kube: KubeClient,
) -> None:
    """Bounded graceful MCP stop first; anything still pending is awaited
    only *after* the critical provider/kube cleanup, matching what
    asyncio.run()'s final task-gathering would do anyway - but explicitly,
    with the exception consumed instead of swallowed."""
    leftover = await controller.shutdown()
    await _shutdown(discovery_task, provider, kube)
    if leftover is not None:
        with contextlib.suppress(BaseException):
            await leftover


def _build_helm(config: KorvidConfig) -> HelmCLI | None:
    """Wrap a detected helm binary, or None so the UI gates helm actions off."""
    binary = find_helm()
    if binary is None:
        return None
    return HelmCLI(binary, kube_context=config.kube_context)


def _fallback_namespaces(config: KorvidConfig) -> tuple[str, ...]:
    """Namespaces an RBAC-limited user can fall back to (issue #49), deduped
    in priority order: explicit `namespaces:` config, the kubeconfig
    context's namespace, korvid's default namespace."""
    candidates = [
        *config.namespaces,
        resolve_context_namespace(config.kube_context),
        config.namespace,
    ]
    return tuple(dict.fromkeys(ns for ns in candidates if ns))


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


async def _run(readonly: bool = False, mcp: bool = False) -> None:
    config = _load_startup_config(readonly, mcp)
    # Custom columns (issue #45) are extracted from raw manifests inside the
    # client — the manifests are discarded once summaries are built.
    kube = KubeClient(custom_columns={kind: view.columns for kind, view in config.views.items()})
    await kube.connect(config.kube_context)
    store = ResourceStore()

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

    # RBAC-limited fallback namespaces (issue #49): the config list plus the
    # kubeconfig context's namespace and korvid's default namespace, deduped
    # in priority order. Feeds the picker and the per-namespace watch fanout.
    fallback_namespaces = _fallback_namespaces(config)

    def _is_namespaced(kind: str) -> bool:
        # Cluster-scoped kinds must not fan out per namespace (the source
        # ignores the namespace for them); unknown kinds fail the watch with
        # a ValueError anyway, so answer False conservatively.
        meta = aliases.get(kind)
        return meta.namespaced if meta is not None else False

    watch_manager = WatchManager(
        store,
        source,
        fallback_namespaces=fallback_namespaces,
        is_namespaced=_is_namespaced,
    )

    # One bounded discovery round trip decides both the R keybinding and
    # whether the agent is offered the resize tool (issue #27).
    pod_resize_supported = await _probe_pod_resize(kube, readonly=config.readonly)

    # Detect the cloud provider once per connection (issue #30): it grounds
    # the agent system prompt and the Service/Ingress describe footer.
    provider_info = await _probe_cloud_provider(kube)

    agent_runtime, configurator, rebuild_agent, provider_box, ui_proxy = _build_agent_wiring(
        config,
        kube,
        aliases,
        pod_resize_supported=pod_resize_supported,
        cluster_context=cluster_context_note(provider_info),
    )

    mcp_controller = MCPController(_make_mcp_factory(config, kube, aliases, ui_proxy))

    app = KorvidApp(
        config=config,
        store=store,
        watch_manager=watch_manager,
        list_namespaces=kube.list_namespaces,
        fallback_namespaces=fallback_namespaces,
        aliases=aliases,
        get_manifest=get_manifest,
        get_events=get_events,
        stream_logs=kube.stream_logs,
        write_ops=kube,
        audit=AuditLog(default_audit_path(), context=config.kube_context),
        check_permission=kube.can_i,
        agent_runtime=agent_runtime,
        agent_model_name=config.agent_model,
        agent_configurator=configurator,
        rebuild_agent=rebuild_agent,
        mcp=mcp_controller,
        metrics=MetricsPoller(kube.list_pod_metrics),
        pod_resize_supported=pod_resize_supported,
        forwards=ForwardRegistry(context=config.kube_context),
        provider_hint=provider_info.display if provider_info.known else None,
        open_pod_exec=kube.open_pod_exec,
        helm=_build_helm(config),
    )
    # Late-bind the UI bridge: from here on the agent's UI-control tools
    # (navigate/set_filter/open_logs/open_describe) land in this app.
    ui_proxy.target = AppUIBridge(app)

    await _start_mcp_if_enabled(config, mcp_controller)

    discovery_task = asyncio.create_task(_discover_in_background(kube, aliases, app))
    try:
        await app.run_async()
    finally:
        await _teardown(mcp_controller, discovery_task, provider_box[0], kube)


def main() -> None:
    parser = argparse.ArgumentParser(prog="korvid", description="Kubernetes TUI with an agent.")
    parser.add_argument(
        "--readonly",
        action="store_true",
        help="Disable all cluster write operations (keybindings and agent tools).",
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help="Expose read + UI-drive tools to external MCP hosts over"
        " Streamable HTTP on 127.0.0.1 (port from config mcp.port, default 7878).",
    )
    args = parser.parse_args()
    asyncio.run(_run(readonly=args.readonly, mcp=args.mcp))


if __name__ == "__main__":
    main()
