"""Tests for composition-root helpers in korvid.__main__."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from korvid.__main__ import _close_provider_in_background
from korvid.agent.tools import UIBridge


class _BoomProvider:
    async def aclose(self) -> None:
        raise RuntimeError("boom")


class _OkProvider:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_close_task_reference_is_retained_until_done() -> None:
    from korvid.agent.provider import LLMProvider

    provider = _OkProvider()
    tasks: set[asyncio.Task[None]] = set()
    _close_provider_in_background(cast("LLMProvider", provider), tasks)
    assert len(tasks) == 1  # strong reference held while pending
    for _ in range(3):
        await asyncio.sleep(0)
    assert provider.closed
    assert not tasks  # reaped once complete


async def test_close_errors_are_consumed() -> None:
    from korvid.agent.provider import LLMProvider

    tasks: set[asyncio.Task[None]] = set()
    _close_provider_in_background(cast("LLMProvider", _BoomProvider()), tasks)
    for _ in range(3):
        await asyncio.sleep(0)
    # Exception must be retrieved by the done callback (no unhandled-task
    # warning); the set must not leak the failed task.
    assert not tasks


# --- Slice 3: late-bound UI bridge proxy ---


class _FakeApp(UIBridge):
    """Nominal test double for the app-side bridge."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        self.calls.append(f"navigate:{view}:{namespace}")
        return "ok-nav"

    async def agent_set_filter(self, pattern: str) -> str:
        self.calls.append(f"filter:{pattern}")
        return "ok-filter"

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        self.calls.append(f"logs:{namespace}/{pod}")
        return "ok-logs"

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        self.calls.append(f"describe:{kind}/{name}")
        return "ok-describe"

    async def agent_drill_down(self, name: str) -> str:
        self.calls.append(f"drill:{name}")
        return "ok-drill"


async def test_proxy_without_target_returns_error() -> None:
    from korvid.__main__ import _UIBridgeProxy

    proxy = _UIBridgeProxy()
    assert (await proxy.agent_navigate("pods")).startswith("ERROR:")
    assert (await proxy.agent_set_filter("x")).startswith("ERROR:")
    assert (await proxy.agent_open_logs("p", "ns")).startswith("ERROR:")
    assert (await proxy.agent_open_describe("pods", "p")).startswith("ERROR:")
    assert (await proxy.agent_drill_down("web")).startswith("ERROR:")


async def test_proxy_forwards_to_target() -> None:
    from korvid.__main__ import _UIBridgeProxy

    proxy = _UIBridgeProxy()
    app = _FakeApp()
    proxy.target = app
    assert await proxy.agent_navigate("pods", "prod") == "ok-nav"
    assert await proxy.agent_set_filter("web") == "ok-filter"
    assert await proxy.agent_open_logs("p", "ns") == "ok-logs"
    assert await proxy.agent_open_describe("pods", "p", "ns") == "ok-describe"
    assert await proxy.agent_drill_down("web") == "ok-drill"
    assert app.calls == [
        "navigate:pods:prod",
        "filter:web",
        "logs:ns/p",
        "describe:pods/p",
        "drill:web",
    ]


def test_agent_wiring_includes_ui_tools(monkeypatch: object) -> None:
    """The composition root arms the runtime with READ_TOOLS + UI_TOOLS."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
    )
    kube_stub = cast("Any", object())  # wiring never touches kube before a tool call
    runtime, _, _, _, proxy = _build_agent_wiring(config, kube_stub, {})
    assert runtime is not None
    names = [t["function"]["name"] for t in runtime._tools]
    assert "navigate" in names
    assert "list_resources" in names
    executor = cast("Any", runtime._executor)
    assert executor._ui is proxy
