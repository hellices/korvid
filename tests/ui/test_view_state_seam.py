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
from textual.worker import WorkerError

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary
from korvid.ui.app import AppUiSurface, AppViewState, KorvidApp
from korvid.ui.ui_surface import UiSurface
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


def test_config_is_not_handed_out_whole() -> None:
    """`KorvidConfig` is only shallowly frozen.

    Returning it exposes mutable `keybindings` and `agent_options` dicts
    through a seam that promises read-only access, and hands controllers
    agent configuration they have no business seeing. The two values they
    actually use are the default namespace and the read-only flag.
    """
    offenders = [
        name
        for name, member in inspect.getmembers(ViewState, inspect.isfunction)
        if not name.startswith("_") and member.__annotations__.get("return") == "KorvidConfig"
    ]
    assert offenders == []


def test_ui_surface_exposes_no_mutable_screen_stack() -> None:
    """The modal stack is inspected for depth, never reordered.

    Handing out Textual's live list lets a controller `pop` or `clear`
    screens outside Textual's lifecycle. The only real use is `len(...)`.
    """
    offenders = [
        name
        for name, member in inspect.getmembers(UiSurface, inspect.isfunction)
        if not name.startswith("_") and member.__annotations__.get("return") == "list[Any]"
    ]
    assert offenders == []


def test_notify_severity_is_a_closed_set() -> None:
    """`str` lets an invalid severity pass strict mypy and fail at runtime."""
    severity = UiSurface.notify.__annotations__["severity"]
    assert severity != "str"


def test_ui_surface_hands_out_no_untyped_screen() -> None:
    """Returning a live `Screen` as `Any` type-checks anything done to it.

    The only real need is "is this still the screen I opened", but `Any`
    also admits `Screen.dismiss` and `Screen.app`, which is app access
    routed around the named surface.
    """
    offenders = [
        name
        for name, member in inspect.getmembers(UiSurface, inspect.isfunction)
        if not name.startswith("_") and member.__annotations__.get("return") == "Any"
    ]
    assert offenders == []


def test_ui_surface_names_the_terminal_capabilities() -> None:
    """Suspending the TUI and re-entering it are app-owned capabilities.

    An interactive shell hands the terminal to a child process, so the
    controller driving it needs `suspend`, `refresh` and the ability to
    get back onto the message pump from the worker thread. Passing those
    as three loose callables is the pattern the seams replaced.
    """
    missing = [
        name
        for name in ("suspend", "refresh", "call_from_thread", "cancel_workers")
        if not hasattr(UiSurface, name)
    ]
    assert missing == []


class _CancelWorker:
    def __init__(self, name: str, calls: list[str], *, fail: bool = False) -> None:
        self.name = name
        self._calls = calls
        self._fail = fail

    async def wait(self) -> None:
        self._calls.append(self.name)
        if self._fail:
            raise WorkerError("cancelled")


def test_app_ui_surface_forwards_untrusted_markup_and_worker_error_policy() -> None:
    """The adapter must delegate every new policy bit to Textual."""
    app = _app()
    surface: Any = AppUiSurface(app)
    calls: list[tuple[str, object]] = []

    def notify_spy(message: str, **kwargs: object) -> None:
        calls.append(("notify", kwargs.get("markup")))

    def run_worker_spy(work: object, **kwargs: object) -> object:
        calls.append(("worker", kwargs.get("exit_on_error")))
        return object()

    app.notify = notify_spy  # type: ignore[method-assign]  # spy
    app.run_worker = run_worker_spy  # type: ignore[assignment,method-assign]  # spy

    surface.notify("cluster text", markup=False)
    surface.run_worker(lambda: None, exit_on_error=False)

    assert calls == [("notify", False), ("worker", False)]


def test_app_ui_surface_cancels_and_awaits_a_worker_group() -> None:
    """Cancellation must wait for each worker returned by the app manager."""
    app = _app()
    surface: Any = AppUiSurface(app)
    calls: list[str] = []
    cancel_calls: list[tuple[object, str]] = []
    workers = [
        _CancelWorker("first", calls, fail=True),
        _CancelWorker("second", calls),
    ]

    def cancel_group_spy(app_arg: object, group: str) -> list[_CancelWorker]:
        cancel_calls.append((app_arg, group))
        return workers

    app.workers.cancel_group = cancel_group_spy  # type: ignore[assignment,method-assign]  # spy

    asyncio.run(surface.cancel_workers("timeline-test"))

    assert cancel_calls == [(app, "timeline-test")]
    assert calls == ["first", "second"]


def test_app_ui_surface_delegates_the_terminal_capabilities() -> None:
    """The adapter must route to the app, not reimplement."""
    app = _app()
    surface: Any = AppUiSurface(app)
    calls: list[str] = []
    app.refresh = lambda *a, **k: calls.append("refresh")  # type: ignore[assignment,method-assign]  # spy
    app.call_from_thread = lambda fn, *a, **k: calls.append("thread")  # type: ignore[assignment,method-assign]  # spy

    surface.refresh()
    surface.call_from_thread(lambda: None)

    assert calls == ["refresh", "thread"]
