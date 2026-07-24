"""Composition root — the only place real dependencies are wired together.

Everything (connect, app, close) runs inside ONE event loop via run_async:
kubernetes_asyncio's ApiClient binds its aiohttp session to the loop it was
created on, so separate asyncio.run() calls would break with
"Event loop is closed" / "attached to a different loop".
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from korvid.core.config import load_config
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import PODS_META, build_alias_map
from korvid.ui.app import KorvidApp

logger = logging.getLogger(__name__)


async def _run() -> None:
    config = load_config()
    kube = KubeClient()
    await kube.connect(config.kube_context)
    store = ResourceStore()

    # Discover available resources; fall back to pods-only on any failure.
    try:
        aliases = build_alias_map(await kube.discover_resources())
    except Exception:
        logger.warning("Resource discovery failed; falling back to pods only", exc_info=True)
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

    watch_manager = WatchManager(store, source)
    app = KorvidApp(
        config=config,
        store=store,
        watch_manager=watch_manager,
        list_namespaces=kube.list_namespaces,
        aliases=aliases,
    )
    try:
        await app.run_async()
    finally:
        await kube.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
