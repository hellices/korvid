"""Composition root — the only place real dependencies are wired together.

Everything (connect, app, close) runs inside ONE event loop via run_async:
kubernetes_asyncio's ApiClient binds its aiohttp session to the loop it was
created on, so separate asyncio.run() calls would break with
"Event loop is closed" / "attached to a different loop".
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from korvid.agent.provider import LLMProvider
from korvid.agent.runtime import AgentRuntime
from korvid.agent.setup import AgentSettings
from korvid.agent.tools import ToolExecutor
from korvid.core.config import DEFAULT_CONFIG_PATH, KorvidConfig, load_config, save_agent_config
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.client import KubeClient, resolve_context_name
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.providers.configurator import ProviderConfigurator
from korvid.providers.registry import create_provider
from korvid.providers.token_store import TokenStore
from korvid.ui.app import KorvidApp

logger = logging.getLogger(__name__)


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


def _build_agent_wiring(
    config: KorvidConfig, kube: KubeClient, aliases: dict[str, ResourceMeta]
) -> tuple[
    AgentRuntime | None,
    ProviderConfigurator,
    Callable[[AgentSettings], AgentRuntime | None],
    list[LLMProvider | None],
]:
    """Build the initial agent runtime plus the :ai wizard's configurator/rebuild hooks."""
    token_store = TokenStore()
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
    agent_runtime = AgentRuntime(provider, ToolExecutor(kube, aliases)) if provider else None

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

    def rebuild_agent(settings: AgentSettings) -> AgentRuntime | None:
        old = provider_box[0]
        if old is not None:
            # Close in the background; the new provider takes over immediately.
            asyncio.get_running_loop().create_task(old.aclose())
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
        return AgentRuntime(new_provider, ToolExecutor(kube, aliases))

    return agent_runtime, configurator, rebuild_agent, provider_box


async def _run() -> None:
    config = load_config()
    # Pin the actual context name so kubectl subprocesses (shell/debug) and the
    # status bar reference this cluster even if current-context changes later.
    resolved_ctx = resolve_context_name(config.kube_context)
    if resolved_ctx != config.kube_context:
        config = dataclasses.replace(config, kube_context=resolved_ctx)
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

    agent_runtime, configurator, rebuild_agent, provider_box = _build_agent_wiring(
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
        agent_runtime=agent_runtime,
        agent_model_name=config.agent_model,
        agent_configurator=configurator,
        rebuild_agent=rebuild_agent,
    )

    discovery_task = asyncio.create_task(_discover_in_background(kube, aliases, app))
    try:
        await app.run_async()
    finally:
        await _shutdown(discovery_task, provider_box[0], kube)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
