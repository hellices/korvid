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
        return {
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"containers": [{"name": "main"}]},
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
