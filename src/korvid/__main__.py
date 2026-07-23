"""Composition root — the only place real dependencies are wired together.

Everything (connect, app, close) runs inside ONE event loop via run_async:
kubernetes_asyncio's ApiClient binds its aiohttp session to the loop it was
created on, so separate asyncio.run() calls would break with
"Event loop is closed" / "attached to a different loop".
"""

from __future__ import annotations

import asyncio

from korvid.core.config import load_config
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.client import KubeClient
from korvid.ui.app import KorvidApp


async def _run() -> None:
    config = load_config()
    kube = KubeClient()
    await kube.connect(config.kube_context)
    store = ResourceStore()
    watch_manager = WatchManager(store, kube.watch_pods)
    app = KorvidApp(config=config, store=store, watch_manager=watch_manager)
    try:
        await app.run_async()
    finally:
        await kube.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
