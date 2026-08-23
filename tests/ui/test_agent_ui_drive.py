"""Tests for slice 3: the agent drives the TUI via UIBridge methods on KorvidApp."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from korvid.agent.model_policy import ModelDescriptor
from korvid.agent.runtime import AgentRuntime
from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.tools.executor import RecordedExecution, UIBridge
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
    manifest_init_containers: list[str] | None = None,
    manifest_ephemeral_containers: list[str] | None = None,
    manifest_uid: str | Callable[[], str] | None = None,
    agent_follow_bridge: UIBridge | None = None,
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
        spec: dict[str, Any] = {"containers": [{"name": c} for c in containers]}
        if manifest_init_containers:
            spec["initContainers"] = [{"name": c} for c in manifest_init_containers]
        if manifest_ephemeral_containers:
            spec["ephemeralContainers"] = [{"name": c} for c in manifest_ephemeral_containers]
        metadata: dict[str, Any] = {"name": name, "namespace": namespace}
        if manifest_uid is not None:
            metadata["uid"] = manifest_uid() if callable(manifest_uid) else manifest_uid
        return {"kind": "Pod", "metadata": metadata, "spec": spec}

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
        agent_follow_bridge=agent_follow_bridge,
    )


# --- navigate ---


async def test_agent_navigate_switches_view() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_navigate("deployments")
        await pilot.pause()
        assert app.current_kind == "deployments"
        assert not out.startswith("ERROR:")
        assert "deployments" in out


async def test_agent_navigate_with_namespace_switches_scope() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_navigate("pods", "prod")
        await pilot.pause()
        assert app.current_scope == "prod"
        assert "prod" in out


async def test_agent_navigate_unknown_view_is_error() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_navigate("wombats")
        assert out.startswith("ERROR:")
        assert app.current_kind == "pods"


async def test_agent_navigate_reports_row_count() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_navigate("pods")
        await pilot.pause()
        assert "2" in out  # two pods visible


# --- set_filter ---


async def test_agent_set_filter_applies_pattern() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_set_filter("web-1")
        await pilot.pause()
        assert app.filter_pattern == "web-1"
        assert not out.startswith("ERROR:")


async def test_agent_set_filter_empty_clears() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._agent_ui.agent_set_filter("web-1")
        await pilot.pause()
        out = await app._agent_ui.agent_set_filter("")
        await pilot.pause()
        assert app.filter_pattern == ""
        assert "clear" in out.lower()


# --- open_describe ---


async def test_agent_open_describe_pushes_screen() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_open_describe("pods", "web-1", "default")
        await pilot.pause()
        assert isinstance(app.screen, DescribeScreen)
        assert not out.startswith("ERROR:")


async def test_agent_open_describe_unknown_kind_is_error() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_open_describe("wombats", "x", "default")
        assert out.startswith("ERROR:")


async def test_agent_open_describe_without_manifest_source_is_error() -> None:
    app = make_app(with_manifest=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_open_describe("pods", "web-1", "default")
        assert out.startswith("ERROR:")


# --- open_logs ---


async def test_agent_open_logs_opens_pane() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_open_logs("web-1", "default")
        await pilot.pause()
        assert app.query_one(LogPane).display is True
        assert not out.startswith("ERROR:")
        assert "web-1" in out


async def test_agent_open_logs_without_streaming_is_error() -> None:
    app = make_app(with_logs=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_open_logs("web-1", "default")
        assert out.startswith("ERROR:")


async def test_agent_open_logs_specific_container() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_open_logs("web-1", "default", "main")
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
        out = await app._agent_ui.agent_open_describe("pods", "web-1", "default")
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
        t1 = asyncio.create_task(app._agent_ui.agent_navigate("deployments"))
        t2 = asyncio.create_task(app.on_navigate_command(NavigateCommand("pods", "prod")))
        await asyncio.gather(t1, t2)
        assert max_concurrent == 1


async def test_agent_navigate_row_count_respects_filter() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._agent_ui.agent_set_filter("web-1")
        await pilot.pause()
        out = await app._agent_ui.agent_navigate("pods")
        assert "1" in out
        assert "2 resources" not in out


async def test_agent_open_logs_resolves_containers_from_manifest() -> None:
    """All containers must come from the manifest, not just the current store
    bucket — the agent may target a pod outside the visible view/scope."""
    app = make_app(manifest_containers=["main", "sidecar"])
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_open_logs("other-pod", "elsewhere")
        await pilot.pause()
        assert not out.startswith("ERROR:")
        containers = {c for _, _, c in app._logs.current_triples}
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
        await app._agent_ui.agent_open_logs("web-1", "default")
        await pilot.pause()
        assert app.query_one(LogPane).display is True
        before = list(app._logs.current_triples)

        out = await app._agent_ui.agent_open_logs("ghost", "default")
        await pilot.pause()
        assert out.startswith("ERROR:")
        assert "ghost" in out
        assert app._logs.current_triples == before


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
        task = asyncio.create_task(app._agent_ui.agent_open_logs("web-1", "default"))
        await asyncio.sleep(0.02)
        # User opens logs for web-2 while the agent's manifest lookup is pending.
        await app._logs.open_pane(
            "default", [("web-2", "main")], triples=[("default", "web-2", "main")]
        )
        release.set()
        out = await task
        await pilot.pause()
        assert out.startswith("ERROR:")
        assert ("default", "web-2", "main") in app._logs.current_triples


async def test_agent_navigate_all_namespace_maps_to_all_scope() -> None:
    """namespace='all' must select the first-class all-namespaces scope,
    matching the human command path (':pods all')."""
    from korvid.core.store import ALL_NAMESPACES

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_navigate("pods", "all")
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
        out_nav = await app._agent_ui.agent_navigate("deployments")
        assert not out_nav.startswith("ERROR:")
        await pilot.pause()
        # 'api' exists in the store — but as a Deployment, not a Pod.
        out = await app._agent_ui.agent_open_logs("api", "default")
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
        out = await app._agent_ui.agent_open_logs("ghost", "default", "main")
        assert out.startswith("ERROR:")
        assert "ghost" in out


async def test_agent_open_logs_rejects_unknown_container() -> None:
    """A container name not present in the pod manifest is an ERROR, not a
    silently-erroring background stream."""
    app = make_app(manifest_containers=["main", "sidecar"])
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_open_logs("web-1", "default", "nope")
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
        task = asyncio.create_task(app._agent_ui.agent_open_describe("pods", "web-1", "default"))
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
        t1 = asyncio.create_task(app._agent_ui.agent_navigate("deployments"))
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
        out = await app._agent_ui.agent_open_logs("web-1", "default")
        await pilot.pause()
        assert out.startswith("ERROR:")
        assert app.query_one(LogPane).display is False


async def test_agent_open_logs_rechecks_pane_gen_after_cancel() -> None:
    """A user pane change landing during the agent's cancel await must
    still win — the generation is rechecked right before opening."""
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        orig_cancel = app._logs.cancel_tasks

        async def cancel_then_user_opens() -> None:
            await orig_cancel()
            app._logs.cancel_tasks = orig_cancel  # type: ignore[method-assign]  # restoring the original bound method after the one-shot intercept
            await app._logs.open_pane(
                "default", [("web-2", "main")], triples=[("default", "web-2", "main")]
            )

        app._logs.cancel_tasks = cancel_then_user_opens  # type: ignore[method-assign]  # simulating a user pane change inside the agent's cancel window
        out = await app._agent_ui.agent_open_logs("web-1", "default")
        await pilot.pause()
        assert out.startswith("ERROR:")
        assert ("default", "web-2", "main") in app._logs.current_triples


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
        out = await app._agent_ui.agent_open_describe("pods", "web-1", "default")
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
        await app._agent_ui.agent_open_describe("pods", "web-1", "default")
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
        out = await app._agent_ui.agent_open_describe("pods", "web-1", "default")
        await pilot.pause()
        assert not out.startswith("ERROR:")
        assert isinstance(app.screen, DescribeScreen)


async def test_agent_open_logs_accepts_init_container() -> None:
    """Init containers are valid log targets (the human picker exposes them),
    so the agent path must accept them too."""
    app = make_app(manifest_containers=["main"], manifest_init_containers=["setup"])
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_open_logs("web-1", "default", "setup")
        await pilot.pause()
        assert not out.startswith("ERROR:")
        assert ("default", "web-1", "setup") in app._logs.current_triples


async def test_agent_open_logs_all_includes_init_and_ephemeral() -> None:
    """'All containers' must mean all: regular + init + ephemeral."""
    app = make_app(
        manifest_containers=["main"],
        manifest_init_containers=["setup"],
        manifest_ephemeral_containers=["debugger"],
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_open_logs("web-1", "default")
        await pilot.pause()
        assert not out.startswith("ERROR:")
        assert ("default", "web-1", "main") in app._logs.current_triples
        assert ("default", "web-1", "setup") in app._logs.current_triples
        assert ("default", "web-1", "debugger") in app._logs.current_triples


async def test_navigation_closes_shared_describe_pane() -> None:
    """Navigating changes the table behind the pane — leaving a stale
    manifest covering the new view would mislead the user."""
    from korvid.ui.widgets.agent_panel import AgentPanel
    from korvid.ui.widgets.describe_screen import DescribePane

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(AgentPanel).display = True
        await app._agent_ui.agent_open_describe("pods", "web-1", "default")
        await pilot.pause()
        assert app.query_one(DescribePane).display is True
        out = await app._agent_ui.agent_navigate("deployments")
        await pilot.pause()
        assert not out.startswith("ERROR:")
        assert app.query_one(DescribePane).display is False


async def test_navigation_closes_pane_even_when_view_already_matches() -> None:
    from korvid.ui.widgets.agent_panel import AgentPanel
    from korvid.ui.widgets.describe_screen import DescribePane

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(AgentPanel).display = True
        await app._agent_ui.agent_open_describe("pods", "web-1", "default")
        await pilot.pause()
        out = await app._agent_ui.agent_navigate("pods")  # same kind/scope
        await pilot.pause()
        assert not out.startswith("ERROR:")
        assert app.query_one(DescribePane).display is False


async def test_agent_navigate_rejected_while_user_describe_modal_open() -> None:
    """A user-opened describe modal means the user is reading — the agent
    must not report 'switched' while the modal still covers the screen."""
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.push_screen(DescribeScreen("pods/default/web-1", {"kind": "Pod"}, []))
        await pilot.pause()
        out = await app._agent_ui.agent_navigate("deployments")
        assert out.startswith("ERROR:")
        assert isinstance(app.screen, DescribeScreen)
        assert app.current_kind == "pods"


async def test_agent_open_logs_reports_panel_truncation() -> None:
    """The pane caps panels at MAX_PANELS; the tool result must say so, or
    the model will assume every container's logs are on screen."""
    from korvid.ui.widgets.log_pane import MAX_PANELS

    total = MAX_PANELS + 2
    app = make_app(manifest_containers=[f"c{i}" for i in range(total)])
    async with app.run_test() as pilot:
        await pilot.pause()
        out = await app._agent_ui.agent_open_logs("web-1", "default")
        await pilot.pause()
        assert not out.startswith("ERROR:")
        assert f"first {MAX_PANELS} of {total}" in out


def _with_runtime(app: KorvidApp) -> AgentRuntime:
    """Attach a minimal runtime so the app has an evidence ledger.

    The citation entry point reads the ledger off the live runtime; this
    harness does not otherwise need an agent.
    """
    runtime = AgentRuntime(_SilentProvider(), _NoToolExecutor())
    app._agent_ui._runtime = runtime
    return runtime


class _SilentProvider:
    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor("test", "silent")

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, stream: bool = True
    ) -> AsyncIterator[dict[str, Any]]:  # pragma: no cover - never driven here
        yield {"type": "done"}


class _NoToolExecutor(RecordedExecution):
    async def execute(
        self, name: str, arguments: dict[str, Any]
    ) -> str:  # pragma: no cover - never driven here
        return ""


async def test_opening_a_citation_shows_the_evidence_it_points_at() -> None:
    """Selecting [E1] puts the read that supports the claim on screen.

    The whole point of a reference is that it can be followed; a citation
    the user cannot open is decoration (issue #192).
    """
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        runtime = _with_runtime(app)
        ref = runtime.evidence.record(
            "get_resource", {"kind": "pods", "name": "web-1", "namespace": "default"}, "ok"
        )
        assert ref is not None

        out = await app._agent_ui.open_evidence(ref)
        await pilot.pause()

        assert isinstance(app.screen, DescribeScreen)
        assert not out.startswith("ERROR:")


async def test_opening_an_unknown_citation_reports_it() -> None:
    """A reference korvid never minted resolves to nothing, visibly."""
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        _with_runtime(app)

        out = await app._agent_ui.open_evidence("E9")

        assert out.startswith("ERROR:")
        assert "E9" in out


async def test_a_citation_with_nowhere_to_go_says_so() -> None:
    """Better than opening the wrong object."""
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        runtime = _with_runtime(app)
        ref = runtime.evidence.record("helm_list_releases", {"namespace": "default"}, "ok")
        assert ref is not None

        out = await app._agent_ui.open_evidence(ref)

        assert out.startswith("ERROR:")


async def test_a_log_citation_opens_the_container_the_read_used() -> None:
    """The read defaulted to the pod's first container; so does the citation."""
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        runtime = _with_runtime(app)
        ref = runtime.evidence.record(
            "get_logs", {"pod": "web-1", "namespace": "default"}, "log line"
        )
        assert ref is not None

        out = await app._agent_ui.open_evidence(ref)
        await pilot.pause()

        assert not out.startswith("ERROR:")
        # The pod's first container, as the read itself defaulted to -
        # not every container, which would show streams that were not
        # the cited evidence.
        assert "main" in out


async def test_a_cluster_wide_list_citation_opens_all_namespaces() -> None:
    """An omitted namespace on a listing means every namespace.

    Forwarding None would instead keep the pane's current scope, so the
    citation would open a narrower view than the evidence covered.
    """
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        runtime = _with_runtime(app)
        ref = runtime.evidence.record("list_resources", {"kind": "pods"}, "web-1")
        assert ref is not None

        out = await app._agent_ui.open_evidence(ref)
        await pilot.pause()

        assert not out.startswith("ERROR:")
        assert app.current_scope == ALL_NAMESPACES


async def test_an_event_citation_on_a_non_pod_says_what_is_shown() -> None:
    """Describe fetches events for pods only, so the citation says so.

    Silently opening a manifest with none of the cited events, while the
    answer claims the events support it, is the failure mode: the user
    would look for evidence that is not on screen (#192 review).
    """
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        runtime = _with_runtime(app)
        ref = runtime.evidence.record(
            "get_events",
            {"kind": "deployments", "name": "web", "namespace": "default"},
            "BackOff",
        )
        assert ref is not None

        out = await app._agent_ui.open_evidence(ref)
        await pilot.pause()

        assert "events are not shown" in out.lower()


async def test_an_event_citation_on_a_pod_shows_them_without_a_caveat() -> None:
    """The pod case is the one describe actually renders events for."""
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        runtime = _with_runtime(app)
        ref = runtime.evidence.record(
            "get_events", {"kind": "pods", "name": "web-1", "namespace": "default"}, "BackOff"
        )
        assert ref is not None

        out = await app._agent_ui.open_evidence(ref)
        await pilot.pause()

        assert "not shown" not in out.lower()


async def test_a_log_citation_resolves_a_pod_outside_the_current_view() -> None:
    """The cited pod need not be in the table the user is looking at.

    `_get_pod_containers` only searches the current kind and scope, so it
    returns nothing for a pod elsewhere - and an empty result reopens
    every container, which is the defect this was meant to fix
    (#192 review).
    """
    app = make_app(manifest_containers=["app", "sidecar"])
    async with app.run_test() as pilot:
        await pilot.pause()
        runtime = _with_runtime(app)
        # A namespace the pods pane is not scoped to.
        ref = runtime.evidence.record(
            "get_logs", {"pod": "web-1", "namespace": "other-ns"}, "log line"
        )
        assert ref is not None

        out = await app._agent_ui.open_evidence(ref)
        await pilot.pause()

        assert not out.startswith("ERROR:")
        assert "app" in out
        assert "sidecar" not in out


async def test_opening_a_citation_for_a_replaced_object_says_so() -> None:
    """A recreated pod is not the evidence, however identical the name.

    The cited read was scoped to one incarnation. Showing the replacement
    without a word is the quiet failure #250 exists to remove - the user
    would read the new object's state as support for a claim about the
    old one.
    """
    app = make_app(manifest_uid="uid-new")
    async with app.run_test() as pilot:
        await pilot.pause()
        runtime = _with_runtime(app)
        ref = runtime.evidence.record(
            "get_events",
            {"kind": "pods", "name": "web-1", "namespace": "default"},
            "BackOff",
            incarnation="uid-old",
        )
        assert ref is not None

        out = await app._agent_ui.open_evidence(ref)
        await pilot.pause()

        assert "replaced" in out.lower()


async def test_opening_a_citation_for_the_same_object_is_not_flagged() -> None:
    """The warning has to be rare, or it stops being read."""
    app = make_app(manifest_uid="uid-same")
    async with app.run_test() as pilot:
        await pilot.pause()
        runtime = _with_runtime(app)
        ref = runtime.evidence.record(
            "get_events",
            {"kind": "pods", "name": "web-1", "namespace": "default"},
            "BackOff",
            incarnation="uid-same",
        )
        assert ref is not None

        out = await app._agent_ui.open_evidence(ref)
        await pilot.pause()

        assert "replaced" not in out.lower()


async def test_a_replacement_between_the_check_and_the_open_is_still_reported() -> None:
    """Checking identity in a separate fetch leaves the same race open.

    The check used to run against its own fetch, so a pod replaced between
    that fetch and the one the opener displayed was shown without a word -
    the quiet failure this change exists to remove, with a smaller window.
    The verdict now comes from the manifest actually put on screen, so the
    first lookup this test serves is the displayed one (#250 review).
    """
    uids = iter(["uid-new"])
    app = make_app(manifest_uid=lambda: next(uids, "uid-new"))
    async with app.run_test() as pilot:
        await pilot.pause()
        runtime = _with_runtime(app)
        ref = runtime.evidence.record(
            "get_resource",
            {"kind": "pods", "name": "web-1", "namespace": "default"},
            "ok",
            incarnation="uid-old",
        )
        assert ref is not None

        out = await app._agent_ui.open_evidence(ref)
        await pilot.pause()

        assert "replaced" in out.lower()
