"""MCP-boundary UI context safety (issue #165).

External MCP calls (and the follow mirrors they spawn) run in tasks
created from the ASGI request context, which does not carry Textual's
`active_app` ContextVar. Composing a new widget tree there (DescribeScreen's
`with VerticalScroll():`) raised `NoActiveAppError` and terminated the app.

The fix marshals every `AppUIBridge` call onto an app-owned context
captured at mount, so the bridge - the single boundary every foreign
caller crosses - guarantees widget work runs where Textual expects it,
and tasks spawned downstream (log streams) inherit that context too.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest
from textual._context import active_app

from korvid.ui.app import AppUIBridge
from korvid.ui.widgets.describe_screen import DescribeScreen
from tests.ui.test_agent_ui_drive import make_app

from .waits import until


def _in_empty_context(coro: object) -> asyncio.Task[str]:
    """The issue's minimal reproduction: a task whose context carries no
    Textual ContextVars - exactly what the MCP/ASGI boundary produces."""
    return asyncio.create_task(coro, context=contextvars.Context())  # type: ignore[arg-type]  # test seam


async def test_describe_from_a_foreign_context_mounts_without_crashing() -> None:
    """The headline crash: agent_open_describe over the bridge from an
    empty context must mount the screen, not kill the app."""
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        bridge = AppUIBridge(app)
        result = await _in_empty_context(bridge.agent_open_describe("pods", "web-1", "default"))
        assert not result.startswith("ERROR:")
        await until(
            pilot,
            lambda: isinstance(app.screen, DescribeScreen),
            label="describe mounted from the foreign context",
        )
        # Reaching here means composition survived; run_test's exit would
        # re-raise any app-terminating exception (NoActiveAppError).
        await pilot.press("escape")


async def test_every_ui_tool_crosses_the_boundary_safely() -> None:
    """navigate / set_filter / logs / drill: none may depend on the
    caller's context (safe-by-accident today becomes a crash the moment
    one of them pushes a screen)."""
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        bridge = AppUIBridge(app)
        nav = await _in_empty_context(bridge.agent_navigate("deployments"))
        assert nav.startswith("switched")
        flt = await _in_empty_context(bridge.agent_set_filter("web"))
        assert "filter set" in flt
        await _in_empty_context(bridge.agent_set_filter(""))
        back = await _in_empty_context(bridge.agent_navigate("pods"))
        assert back.startswith("switched")
        logs = await _in_empty_context(bridge.agent_open_logs("web-1", "default"))
        assert not logs.startswith("ERROR:")


async def test_downstream_log_tasks_carry_the_app_context() -> None:
    """The audit's latent defect: log-stream tasks spawned by an
    MCP-driven agent_open_logs must carry `active_app` - a future error
    path composing a widget from a stream task must not become a
    delayed crash. Inheritance is observed from *inside* a spawned
    stream (`Task.get_context()` is 3.12-only; the repo supports 3.11)."""
    from collections.abc import AsyncIterator
    from typing import Any

    from korvid.k8s.logs import LogLine

    seen: list[object] = []

    app = make_app(with_logs=False)

    async def recording_stream(
        namespace: str, pod: str, container: str, **kwargs: Any
    ) -> AsyncIterator[LogLine]:
        seen.append(active_app.get(None))
        yield LogLine(pod=pod, container=container, text="hello", timestamp=None)
        while True:
            await asyncio.sleep(0.01)

    app._stream_logs = recording_stream  # test seam: duck-typed stream source
    async with app.run_test() as pilot:
        await pilot.pause()
        bridge = AppUIBridge(app)
        result = await _in_empty_context(bridge.agent_open_logs("web-1", "default"))
        assert not result.startswith("ERROR:")
        await until(pilot, lambda: bool(seen), label="stream task ran")
        assert seen == [app]  # the spawned stream sees the app context


async def test_concurrent_bridge_calls_from_foreign_contexts_stay_serialized() -> None:
    """The composition root's proxy serializes UI calls; the context
    marshaling underneath must not break that ordering."""
    from korvid.__main__ import _UIBridgeProxy

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        proxy = _UIBridgeProxy()
        proxy.target = AppUIBridge(app)
        first = _in_empty_context(proxy.agent_navigate("deployments"))
        second = _in_empty_context(proxy.agent_navigate("pods"))
        await first
        await second
        await pilot.pause()
        assert app.current_kind == "pods"  # the later call landed last


async def test_real_mcp_http_open_describe_and_follow_mirror() -> None:
    """The issue's end-to-end requirement: a real Streamable HTTP MCP
    round-trip against a running Textual test app - direct `open_describe`
    and a follow-mirrored `get_resource` both cross the ASGI boundary
    without NoActiveAppError, and the app survives."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from korvid.__main__ import _UIBridgeProxy
    from korvid.k8s.discovery import PODS_META
    from korvid.mcp.server import KorvidMCPServer
    from korvid.tools.executor import READ_TOOLS, UI_TOOLS, ToolExecutor

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        proxy = _UIBridgeProxy()
        proxy.target = AppUIBridge(app)

        class ManifestKube:
            async def get_object(
                self, meta: object, namespace: str | None, name: str
            ) -> dict[str, object]:
                return {"kind": "Pod", "metadata": {"name": name, "namespace": namespace}}

        executor = ToolExecutor(
            ManifestKube(),  # type: ignore[arg-type]  # read-only fake
            {"pods": PODS_META},
            ui=proxy,
        )
        server = KorvidMCPServer(
            executor,
            READ_TOOLS + UI_TOOLS,
            port=0,
            ui=proxy,
            follow_enabled=lambda: True,
        )
        run_task = asyncio.create_task(server.run())
        try:
            port = await asyncio.wait_for(server.wait_started(), timeout=10)
            async with (
                streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                # 1) direct UI drive across the real ASGI boundary
                result = await session.call_tool(
                    "open_describe", {"kind": "pods", "name": "web-1", "namespace": "default"}
                )
                text = getattr(result.content[0], "text", "")
                assert "describe screen opened" in text
                await until(
                    pilot,
                    lambda: isinstance(app.screen, DescribeScreen),
                    label="describe mounted over real MCP",
                )
                await pilot.press("escape")
                await until(
                    pilot,
                    lambda: not isinstance(app.screen, DescribeScreen),
                    label="describe closed",
                )
                # 2) follow mirror of a cluster read (fire-and-forget task
                # from the ASGI context - the original crash path)
                result = await session.call_tool(
                    "get_resource", {"kind": "pods", "name": "web-2", "namespace": "default"}
                )
                assert "web-2" in getattr(result.content[0], "text", "")
                while server._follow_tasks:
                    await asyncio.gather(*server._follow_tasks)
                await until(
                    pilot,
                    lambda: isinstance(app.screen, DescribeScreen),
                    label="follow mirror mounted over real MCP",
                )
        finally:
            server.request_shutdown()
            await asyncio.wait_for(run_task, timeout=10)


async def test_premount_bridge_call_refuses_instead_of_running_foreign() -> None:
    """The None-snapshot case is reachable in production: the MCP endpoint
    goes live before app.run_async(), so a call can arrive before on_mount
    captured the context. Running the coroutine directly would execute the
    widget operation in the foreign ASGI context during Textual startup -
    refuse as UI-not-ready instead (and never leak an unawaited coroutine)."""
    import warnings

    app = make_app()
    bridge = AppUIBridge(app)  # no run_test: the app is not mounted yet
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # an unclosed coroutine would warn
        result = await bridge.agent_open_describe("pods", "web-1", "default")
    assert result.startswith("ERROR:")
    assert "not ready" in result


async def test_drill_down_crosses_the_boundary_safely() -> None:
    """The issue's drill-down acceptance criterion: a real deployment ->
    replicasets transition driven from an empty context."""
    from korvid.ui.messages import NavigateCommand
    from tests.ui.test_drilldown import _default_data
    from tests.ui.test_drilldown import make_app as make_drill_app

    app = make_drill_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await app.on_navigate_command(NavigateCommand("deployments", None))
        await pilot.pause(0.1)
        bridge = AppUIBridge(app)
        result = await _in_empty_context(bridge.agent_drill_down("web"))
        assert not result.startswith("ERROR:")
        await until(pilot, lambda: app.current_kind == "replicasets", label="drilled")


async def test_cancelled_foreign_caller_reaps_the_dispatched_task() -> None:
    """The issue's shutdown criterion: cancelling the foreign caller while
    a dispatched bridge coroutine is in flight cancels and awaits the
    inner task - its finally runs, and nothing stays pending."""
    entered = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    app = make_app()

    async def blocking_describe(kind: str, name: str, namespace: str | None = None) -> str:
        entered.set()
        try:
            await release.wait()
            return "ok"
        finally:
            cleaned.set()  # the inner task's finally must run on cancel

    app.agent_open_describe = blocking_describe  # type: ignore[method-assign]  # test seam
    async with app.run_test() as pilot:
        await pilot.pause()
        bridge = AppUIBridge(app)
        caller = _in_empty_context(bridge.agent_open_describe("pods", "web-1", "default"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await asyncio.wait_for(cleaned.wait(), timeout=5)  # reaped, not stranded
