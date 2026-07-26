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
from collections.abc import AsyncIterator, Callable
from typing import Any

from korvid.agent.mcp_server import KorvidMCPServer, default_endpoint_path
from korvid.agent.provider import LLMProvider
from korvid.agent.runtime import AgentRuntime
from korvid.agent.setup import AgentSettings
from korvid.agent.tools import READ_TOOLS, UI_TOOLS, WRITE_TOOLS, ToolExecutor, UIBridge
from korvid.core.audit import AuditLog, default_audit_path
from korvid.core.config import DEFAULT_CONFIG_PATH, KorvidConfig, load_config, save_agent_config
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.client import KubeClient, resolve_context_name
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.providers.configurator import ProviderConfigurator
from korvid.providers.registry import create_provider
from korvid.providers.token_store import TokenStore
from korvid.ui.app import AppUIBridge, KorvidApp

logger = logging.getLogger(__name__)


def _build_mcp_server(
    config: KorvidConfig,
    kube: KubeClient,
    aliases: dict[str, ResourceMeta],
    ui: UIBridge | None,
) -> KorvidMCPServer | None:
    """MCP server for external hosts when enabled; read + UI-drive tools
    only - write tools stay with the built-in agent until an approval UX
    for external callers is designed (issue #11 non-goal)."""
    if not config.mcp_enabled:
        return None
    return KorvidMCPServer(
        ToolExecutor(kube, aliases, ui=ui),
        READ_TOOLS + UI_TOOLS,
        port=config.mcp_port,
        endpoint_path=default_endpoint_path(),
    )


def _start_mcp(
    config: KorvidConfig,
    kube: KubeClient,
    aliases: dict[str, ResourceMeta],
    ui: UIBridge | None,
) -> tuple[KorvidMCPServer, asyncio.Task[None]] | None:
    """Build and launch the MCP server when enabled; pair it with its task
    so ``_stop_mcp`` can tear both down."""
    server = _build_mcp_server(config, kube, aliases, ui)
    if server is None:
        return None
    return server, asyncio.create_task(server.run())


async def _stop_mcp(server: KorvidMCPServer, task: asyncio.Task[None]) -> None:
    """Graceful exit first (uvicorn drains connections and removes the
    endpoint file); a hung shutdown must never block terminal restore."""
    server.request_shutdown()
    try:
        await asyncio.wait_for(task, timeout=5)
    except Exception:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


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
        discovered = build_alias_map(await kube.discover_resources())
    except Exception:
        logger.warning("Resource discovery failed; staying pods-only", exc_info=True)
        return
    aliases.update(discovered)
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
    degrades to an ERROR result instead of crashing the turn."""

    _NOT_READY = "ERROR: UI not ready"

    def __init__(self) -> None:
        self.target: UIBridge | None = None

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        if self.target is None:
            return self._NOT_READY
        return await self.target.agent_navigate(view, namespace)

    async def agent_set_filter(self, pattern: str) -> str:
        if self.target is None:
            return self._NOT_READY
        return await self.target.agent_set_filter(pattern)

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        if self.target is None:
            return self._NOT_READY
        return await self.target.agent_open_logs(pod, namespace, container)

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        if self.target is None:
            return self._NOT_READY
        return await self.target.agent_open_describe(kind, name, namespace)

    async def agent_drill_down(self, name: str) -> str:
        if self.target is None:
            return self._NOT_READY
        return await self.target.agent_drill_down(name)

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
    ) -> str:
        if self.target is None:
            return self._NOT_READY
        return await self.target.agent_request_write(action, kind, name, namespace, replicas)


def _build_agent_wiring(
    config: KorvidConfig, kube: KubeClient, aliases: dict[str, ResourceMeta]
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
    agent_tools = READ_TOOLS + UI_TOOLS
    if not config.readonly:
        # In readonly mode the model is never even told write tools exist.
        agent_tools = agent_tools + WRITE_TOOLS
    oauth = token_store.load("github-oauth") if config.agent_provider == "github-copilot" else None
    provider = create_provider(
        enabled=config.agent_enabled,
        provider=config.agent_provider,
        auth_method=config.agent_auth_method,
        base_url=config.agent_base_url,
        model=config.agent_model,
        api_key_env=config.agent_api_key_env,
        oauth_token=oauth,
    )
    agent_runtime = (
        AgentRuntime(provider, ToolExecutor(kube, aliases, ui=ui_proxy), tools=agent_tools)
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
        )
        provider_box[0] = new_provider
        if new_provider is None:
            return None
        return AgentRuntime(
            new_provider, ToolExecutor(kube, aliases, ui=ui_proxy), tools=agent_tools
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
    return config


async def _run(readonly: bool = False, mcp: bool = False) -> None:
    config = _load_startup_config(readonly, mcp)
    kube = KubeClient()
    await kube.connect(config.kube_context)
    store = ResourceStore()

    # Start with pods only so the UI appears immediately; full discovery runs
    # in the background and merges into this dict (closures + app share it).
    aliases = build_alias_map([PODS_META])

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        ns = None if scope == ALL_NAMESPACES else scope
        if kind == "pods":
            async for ev, pod in kube.watch_pods(ns):
                yield (ev, pod)
        elif kind in aliases:
            meta = aliases[kind]
            async for ev, obj in kube.watch_objects(meta, ns):
                yield (ev, obj)
        else:
            logger.warning("Unknown resource kind %r requested for watch; stopping", kind)
            raise ValueError(f"Unknown resource kind: {kind!r}")

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        meta = aliases.get(kind)
        if meta is None:
            raise ValueError(f"Unknown resource kind: {kind!r}")
        return await kube.get_object(meta, namespace, name)

    async def get_events(namespace: str, name: str) -> list[dict[str, Any]]:
        return await kube.list_events_for(namespace, name)

    watch_manager = WatchManager(store, source)

    agent_runtime, configurator, rebuild_agent, provider_box, ui_proxy = _build_agent_wiring(
        config, kube, aliases
    )

    app = KorvidApp(
        config=config,
        store=store,
        watch_manager=watch_manager,
        list_namespaces=kube.list_namespaces,
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
    )
    # Late-bind the UI bridge: from here on the agent's UI-control tools
    # (navigate/set_filter/open_logs/open_describe) land in this app.
    ui_proxy.target = AppUIBridge(app)

    mcp_running = _start_mcp(config, kube, aliases, ui_proxy)

    discovery_task = asyncio.create_task(_discover_in_background(kube, aliases, app))
    try:
        await app.run_async()
    finally:
        if mcp_running is not None:
            await _stop_mcp(*mcp_running)
        await _shutdown(discovery_task, provider_box[0], kube)


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
