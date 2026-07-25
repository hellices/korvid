"""Tests for slice 3: the agent drives the TUI via UIBridge methods on KorvidApp."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.describe_screen import DescribeScreen
from korvid.ui.widgets.log_pane import LogPane

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))

_ALIASES: dict[str, ResourceMeta] = {
    "pods": _PODS_META,
    "po": _PODS_META,
    "pod": _PODS_META,
    "deployments": _DEPLOY_META,
    "deploy": _DEPLOY_META,
}


def _pod(name: str, namespace: str = "default") -> PodSummary:
    return PodSummary(
        name=name,
        namespace=namespace,
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        qos="-",
        containers=("main",),
    )


def _deploy(name: str, namespace: str = "default") -> GenericSummary:
    return GenericSummary(name=name, namespace=namespace, kind="Deployment", created="")


def make_app(
    *,
    with_manifest: bool = True,
    with_logs: bool = True,
    manifest_containers: list[str] | None = None,
) -> KorvidApp:
    store = ResourceStore()
    data: dict[str, list[Summary]] = {
        "pods": [_pod("web-1"), _pod("web-2")],
        "deployments": [_deploy("api")],
    }

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        containers = manifest_containers or ["main"]
        return {
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"containers": [{"name": c} for c in containers]},
        }

    async def stream_logs(
        namespace: str, pod: str, container: str, **kwargs: Any
    ) -> AsyncIterator[LogLine]:
        yield LogLine(pod=pod, container=container, text="hello", timestamp=None)
        while True:
            await asyncio.sleep(0.01)

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        get_manifest=get_manifest if with_manifest else None,
        stream_logs=stream_logs if with_logs else None,
    )


# --- navigate ---


async def test_agent_navigate_switches_view() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_navigate("deployments")
        await pilot.pause()
        assert app.current_kind == "deployments"
        assert not out.startswith("ERROR:")
        assert "deployments" in out


async def test_agent_navigate_with_namespace_switches_scope() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_navigate("pods", "prod")
        await pilot.pause()
        assert app.current_scope == "prod"
        assert "prod" in out


async def test_agent_navigate_unknown_view_is_error() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_navigate("wombats")
        assert out.startswith("ERROR:")
        assert app.current_kind == "pods"


async def test_agent_navigate_reports_row_count() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_navigate("pods")
        await pilot.pause()
        assert "2" in out  # two pods visible


# --- set_filter ---


async def test_agent_set_filter_applies_pattern() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_set_filter("web-1")
        await pilot.pause()
        assert app.filter_pattern == "web-1"
        assert not out.startswith("ERROR:")


async def test_agent_set_filter_empty_clears() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.agent_set_filter("web-1")
        await pilot.pause()
        out = await app.agent_set_filter("")
        await pilot.pause()
        assert app.filter_pattern == ""
        assert "clear" in out.lower()


# --- open_describe ---


async def test_agent_open_describe_pushes_screen() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_open_describe("pods", "web-1", "default")
        await pilot.pause()
        assert isinstance(app.screen, DescribeScreen)
        assert not out.startswith("ERROR:")


async def test_agent_open_describe_unknown_kind_is_error() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_open_describe("wombats", "x", "default")
        assert out.startswith("ERROR:")


async def test_agent_open_describe_without_manifest_source_is_error() -> None:
    app = make_app(with_manifest=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_open_describe("pods", "web-1", "default")
        assert out.startswith("ERROR:")


# --- open_logs ---


async def test_agent_open_logs_opens_pane() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_open_logs("web-1", "default")
        await pilot.pause()
        assert app.query_one(LogPane).display is True
        assert not out.startswith("ERROR:")
        assert "web-1" in out


async def test_agent_open_logs_without_streaming_is_error() -> None:
    app = make_app(with_logs=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_open_logs("web-1", "default")
        assert out.startswith("ERROR:")


async def test_agent_open_logs_specific_container() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_open_logs("web-1", "default", "main")
        await pilot.pause()
        assert app.query_one(LogPane).display is True
        assert not out.startswith("ERROR:")


# --- bridge never raises ---


async def test_bridge_methods_return_error_instead_of_raising() -> None:
    """Executor contract: bridge failures surface as ERROR strings."""
    app = make_app()

    async def boom(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        raise RuntimeError("api down")

    app._get_manifest = boom
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_open_describe("pods", "web-1", "default")
        assert out.startswith("ERROR:")
        assert "api down" in out


# --- review round 1 fixes ---


async def test_concurrent_navigations_serialize() -> None:
    """Agent-path and keyboard-path navigation must not interleave mid-handler
    (both stop/start watches and mutate current_kind/current_scope)."""
    from korvid.ui.messages import NavigateCommand

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        active = 0
        max_concurrent = 0
        orig_stop = app.watch_manager.stop

        async def slow_stop(kind: str, scope: str) -> None:
            nonlocal active, max_concurrent
            active += 1
            max_concurrent = max(max_concurrent, active)
            await asyncio.sleep(0.05)
            active -= 1
            await orig_stop(kind, scope)

        app.watch_manager.stop = slow_stop  # type: ignore[method-assign]  # instrumenting stop to observe handler overlap; restored via orig_stop
        t1 = asyncio.create_task(app.agent_navigate("deployments"))
        t2 = asyncio.create_task(app.on_navigate_command(NavigateCommand("pods", "prod")))
        await asyncio.gather(t1, t2)
        assert max_concurrent == 1


async def test_agent_navigate_row_count_respects_filter() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.agent_set_filter("web-1")
        await pilot.pause()
        out = await app.agent_navigate("pods")
        assert "1" in out
        assert "2 resources" not in out


async def test_agent_open_logs_resolves_containers_from_manifest() -> None:
    """All containers must come from the manifest, not just the current store
    bucket — the agent may target a pod outside the visible view/scope."""
    app = make_app(manifest_containers=["main", "sidecar"])
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_open_logs("other-pod", "elsewhere")
        await pilot.pause()
        assert not out.startswith("ERROR:")
        containers = {c for _, _, c in app._current_log_triples}
        assert containers == {"main", "sidecar"}


async def test_agent_open_logs_unknown_pod_errors_without_disturbing_pane() -> None:
    """A pod the model hallucinated must not tear down existing log streams
    or open a blank pane — the agent gets an ERROR it can act on instead."""
    app = make_app()

    async def manifest_or_404(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        if name == "ghost":
            raise RuntimeError('pods "ghost" not found')
        return {"kind": "Pod", "spec": {"containers": [{"name": "main"}]}}

    app._get_manifest = manifest_or_404
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.agent_open_logs("web-1", "default")
        await pilot.pause()
        assert app.query_one(LogPane).display is True
        before = list(app._current_log_triples)

        out = await app.agent_open_logs("ghost", "default")
        await pilot.pause()
        assert out.startswith("ERROR:")
        assert "ghost" in out
        assert app._current_log_triples == before


async def test_agent_open_logs_yields_to_user_log_action_during_lookup() -> None:
    """If the user opens/changes the log pane while the agent is still
    resolving containers, the user's choice wins — the agent must not
    clobber it when its coroutine resumes."""
    app = make_app()
    release = asyncio.Event()

    async def slow_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        await release.wait()
        return {"kind": "Pod", "spec": {"containers": [{"name": "main"}]}}

    app._get_manifest = slow_manifest
    async with app.run_test() as pilot:
        await pilot.pause()
        task = asyncio.create_task(app.agent_open_logs("web-1", "default"))
        await asyncio.sleep(0.02)
        # User opens logs for web-2 while the agent's manifest lookup is pending.
        await app._open_log_pane(
            "default", [("web-2", "main")], triples=[("default", "web-2", "main")]
        )
        release.set()
        out = await task
        await pilot.pause()
        assert out.startswith("ERROR:")
        assert ("default", "web-2", "main") in app._current_log_triples


async def test_agent_navigate_all_namespace_maps_to_all_scope() -> None:
    """namespace='all' must select the first-class all-namespaces scope,
    matching the human command path (':pods all')."""
    from korvid.core.store import ALL_NAMESPACES

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_navigate("pods", "all")
        await pilot.pause()
        assert app.current_scope == ALL_NAMESPACES
        assert not out.startswith("ERROR:")


async def test_agent_open_logs_ignores_same_named_non_pod_in_store() -> None:
    """A deployment named like the requested pod must not make an unknown
    pod look 'known' when the current view is not pods."""
    app = make_app()

    async def manifest_404(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        raise RuntimeError(f'pods "{name}" not found')

    app._get_manifest = manifest_404
    async with app.run_test() as pilot:
        await pilot.pause()
        out_nav = await app.agent_navigate("deployments")
        assert not out_nav.startswith("ERROR:")
        await pilot.pause()
        # 'api' exists in the store — but as a Deployment, not a Pod.
        out = await app.agent_open_logs("api", "default")
        assert out.startswith("ERROR:")
        assert "api" in out


async def test_agent_open_logs_rejects_unknown_pod_even_with_container() -> None:
    """Supplying a container must not bypass pod validation."""
    app = make_app()

    async def manifest_404(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        raise RuntimeError(f'pods "{name}" not found')

    app._get_manifest = manifest_404
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_open_logs("ghost", "default", "main")
        assert out.startswith("ERROR:")
        assert "ghost" in out


async def test_agent_open_logs_rejects_unknown_container() -> None:
    """A container name not present in the pod manifest is an ERROR, not a
    silently-erroring background stream."""
    app = make_app(manifest_containers=["main", "sidecar"])
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_open_logs("web-1", "default", "nope")
        assert out.startswith("ERROR:")
        assert "nope" in out
        assert "main" in out


async def test_agent_open_describe_yields_to_user_screen_change_during_fetch() -> None:
    """If the user opens a screen or navigates while the agent's manifest
    fetch is pending, the agent must not cover it with a stale modal."""
    app = make_app()
    release = asyncio.Event()

    async def slow_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        await release.wait()
        return {"kind": "Pod", "metadata": {"name": name}}

    app._get_manifest = slow_manifest
    async with app.run_test() as pilot:
        await pilot.pause()
        task = asyncio.create_task(app.agent_open_describe("pods", "web-1", "default"))
        await asyncio.sleep(0.02)
        user_screen = DescribeScreen("user's screen", {"kind": "Pod"}, [])
        await app.push_screen(user_screen)
        release.set()
        out = await task
        await pilot.pause()
        assert out.startswith("ERROR:")
        assert app.screen is user_screen


async def test_toggle_all_namespaces_serializes_with_agent_navigate() -> None:
    """The 'a' key path mutates scope and stops/starts watches across awaits,
    so it must serialize through the same nav lock as agent navigation."""
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        active = 0
        max_concurrent = 0
        orig_stop = app.watch_manager.stop

        async def slow_stop(kind: str, scope: str) -> None:
            nonlocal active, max_concurrent
            active += 1
            max_concurrent = max(max_concurrent, active)
            await asyncio.sleep(0.05)
            active -= 1
            await orig_stop(kind, scope)

        app.watch_manager.stop = slow_stop  # type: ignore[method-assign]  # instrumenting stop to observe handler overlap; restored via orig_stop
        t1 = asyncio.create_task(app.agent_navigate("deployments"))
        t2 = asyncio.create_task(app.action_toggle_all_namespaces())
        await asyncio.gather(t1, t2)
        assert max_concurrent == 1


async def test_agent_open_logs_api_rejection_beats_stale_cache() -> None:
    """A 404 from the API is authoritative: a recently deleted pod that is
    still in the watch cache must not open a doomed stream."""
    from korvid.k8s.errors import ApiStatusError

    app = make_app()

    async def manifest_gone(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        raise ApiStatusError(404, f'pods "{name}" not found')

    app._get_manifest = manifest_gone
    async with app.run_test() as pilot:
        await pilot.pause()
        # web-1 is still in the store (watch cache), but the API says 404.
        out = await app.agent_open_logs("web-1", "default")
        await pilot.pause()
        assert out.startswith("ERROR:")
        assert app.query_one(LogPane).display is False


async def test_agent_open_logs_rechecks_pane_gen_after_cancel() -> None:
    """A user pane change landing during the agent's cancel await must
    still win — the generation is rechecked right before opening."""
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        orig_cancel = app._cancel_log_tasks

        async def cancel_then_user_opens() -> None:
            await orig_cancel()
            app._cancel_log_tasks = orig_cancel  # type: ignore[method-assign]  # restoring the original bound method after the one-shot intercept
            await app._open_log_pane(
                "default", [("web-2", "main")], triples=[("default", "web-2", "main")]
            )

        app._cancel_log_tasks = cancel_then_user_opens  # type: ignore[method-assign]  # simulating a user pane change inside the agent's cancel window
        out = await app.agent_open_logs("web-1", "default")
        await pilot.pause()
        assert out.startswith("ERROR:")
        assert ("default", "web-2", "main") in app._current_log_triples


async def test_agent_open_describe_shares_screen_when_panel_visible() -> None:
    """When the chat panel is open, describe must not become the active
    (modal) screen — the chat input has to stay reachable while the manifest
    is on screen (agent actions must not steal focus)."""
    from textual.widgets import Input

    from korvid.ui.widgets.agent_panel import AgentPanel
    from korvid.ui.widgets.describe_screen import DescribePane

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(AgentPanel).display = True
        out = await app.agent_open_describe("pods", "web-1", "default")
        await pilot.pause()
        assert not out.startswith("ERROR:")
        assert not isinstance(app.screen, DescribeScreen)
        pane = app.query_one(DescribePane)
        assert pane.display is True
        assert "web-1" in pane.body_text
        # the chat input remains focusable — describe didn't take the screen
        agent_input = app.query_one("#agent-input", Input)
        agent_input.focus()
        await pilot.pause()
        assert app.focused is agent_input


async def test_escape_closes_shared_describe_pane() -> None:
    from korvid.ui.widgets.agent_panel import AgentPanel
    from korvid.ui.widgets.describe_screen import DescribePane

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(AgentPanel).display = True
        await app.agent_open_describe("pods", "web-1", "default")
        await pilot.pause()
        assert app.query_one(DescribePane).display is True
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(DescribePane).display is False


async def test_agent_open_describe_fullscreen_when_panel_hidden() -> None:
    from korvid.ui.widgets.agent_panel import AgentPanel

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(AgentPanel).display = False
        out = await app.agent_open_describe("pods", "web-1", "default")
        await pilot.pause()
        assert not out.startswith("ERROR:")
        assert isinstance(app.screen, DescribeScreen)


async def test_agent_open_logs_reports_panel_truncation() -> None:
    """The pane caps panels at MAX_PANELS; the tool result must say so, or
    the model will assume every container's logs are on screen."""
    from korvid.ui.widgets.log_pane import MAX_PANELS

    total = MAX_PANELS + 2
    app = make_app(manifest_containers=[f"c{i}" for i in range(total)])
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app.agent_open_logs("web-1", "default")
        await pilot.pause()
        assert not out.startswith("ERROR:")
        assert f"first {MAX_PANELS} of {total}" in out
