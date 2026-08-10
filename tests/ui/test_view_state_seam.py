"""The `ViewState` seam must be read-only in fact, not only by convention (#187).

A controller receives this interface so it can answer "what is the user
looking at". If the same object also hands out the live alias table or a
store that can be cleared, a controller can erase the app's view while
still type-checking - which is exactly the coupling the seam removes.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from typing import Any

import pytest

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary
from korvid.ui.app import AppViewState, KorvidApp
from korvid.ui.view_state import ViewState

_ALIASES = {
    "pods": ResourceMeta("", "v1", "pods", "Pod", True),
    "nodes": ResourceMeta("", "v1", "nodes", "Node", False),
}


def _app() -> KorvidApp:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        while True:  # pragma: no cover - the seam tests never start a watch
            await asyncio.sleep(0.01)
            yield (
                "ADDED",
                GenericSummary(name="unused", namespace="default", kind="Pod", created=""),
            )

    async def list_namespaces() -> list[str]:
        return ["default"]

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=dict(_ALIASES),
    )


def test_view_state_hands_out_no_mutable_collection() -> None:
    """No accessor may return an object that can rewrite the app's view.

    `store()` returned a `ResourceStore`, whose `clear`, `clear_all` and
    `apply_event` are reachable from any controller holding the seam.
    """
    offenders = [
        name
        for name, member in inspect.getmembers(ViewState, inspect.isfunction)
        if not name.startswith("_")
        and member.__annotations__.get("return") in {"ResourceStore", "dict[str, ResourceMeta]"}
    ]
    assert offenders == []


def test_aliases_cannot_be_rewritten_through_the_seam() -> None:
    """The discovered alias table is a live read, never a handle to mutate."""
    view: Any = AppViewState(_app())
    aliases = view.aliases()
    with pytest.raises(TypeError, match="does not support item assignment"):
        aliases["pods"] = _ALIASES["nodes"]


def test_resources_reads_the_live_store() -> None:
    """Narrowing `store()` to a query must not stale-cache the result."""
    app = _app()
    view: Any = AppViewState(app)
    assert view.resources("pods", "default") == []
    app.store.apply_event(
        "pods",
        "default",
        "ADDED",
        GenericSummary(name="api-1", namespace="default", kind="Pod", created="", uid="u1"),
    )
    assert [obj.name for obj in view.resources("pods", "default")] == ["api-1"]
