"""Tests for the pod → container drill-down (Enter opens ContainersScreen)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.containers_screen import ContainersScreen, build_container_rows

# ---------------------------------------------------------------------------
# Pure unit tests: build_container_rows
# ---------------------------------------------------------------------------

_MANIFEST: dict[str, Any] = {
    "spec": {
        "containers": [
            {"name": "app", "image": "nginx:1.27"},
            {"name": "sidecar", "image": "envoy:1.30"},
        ],
        "initContainers": [{"name": "setup", "image": "busybox:1.36"}],
    },
    "status": {
        "containerStatuses": [
            {"name": "app", "ready": True, "restartCount": 2, "state": {"running": {}}},
            {
                "name": "sidecar",
                "ready": False,
                "restartCount": 5,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
            },
        ],
        "initContainerStatuses": [
            {
                "name": "setup",
                "ready": False,
                "restartCount": 0,
                "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
            }
        ],
    },
}


def test_build_container_rows_full_manifest() -> None:
    rows = build_container_rows(_MANIFEST)
    assert rows == [
        ("app", "nginx:1.27", "true", "Running", "2"),
        ("sidecar", "envoy:1.30", "false", "CrashLoopBackOff", "5"),
        ("setup (init)", "busybox:1.36", "false", "Completed (0)", "0"),
    ]


def test_build_container_rows_no_status() -> None:
    rows = build_container_rows({"spec": {"containers": [{"name": "app", "image": "img"}]}})
    assert rows == [("app", "img", "false", "-", "0")]


def test_build_container_rows_empty_manifest() -> None:
    assert build_container_rows({}) == []


# ---------------------------------------------------------------------------
# Pilot tests: Enter on a pod row opens ContainersScreen
# ---------------------------------------------------------------------------

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_TEST_ALIASES = {"pods": _PODS_META, "po": _PODS_META, "pod": _PODS_META}


def _pod(name: str, containers: tuple[str, ...] = ("app", "sidecar")) -> PodSummary:
    return PodSummary(
        name=name,
        namespace="default",
        phase="Running",
        ready="2/2",
        restarts=0,
        node=None,
        qos="-",
        containers=containers,
    )


def make_app(pods: list[PodSummary], **kwargs: Any) -> KorvidApp:
    store = ResourceStore()
    data: dict[str, list[Summary]] = {"pods": list(pods)}

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_TEST_ALIASES),
        **kwargs,
    )


async def _get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
    assert kind == "pods"
    return _MANIFEST


async def test_enter_on_pod_opens_containers_screen() -> None:
    app = make_app([_pod("web-1")], get_manifest=_get_manifest)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ContainersScreen)
        text = app.screen.query_one("#containers-title").render()
        assert "web-1" in str(text)


async def test_containers_screen_escape_closes() -> None:
    app = make_app([_pod("web-1")], get_manifest=_get_manifest)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ContainersScreen)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, ContainersScreen)


async def test_containers_screen_enter_is_noop() -> None:
    """Enter must not trigger logs — only l/s act (user request)."""
    app = make_app([_pod("web-1")], get_manifest=_get_manifest)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ContainersScreen)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ContainersScreen)  # still open, nothing ran


async def test_containers_screen_l_opens_logs() -> None:
    lines_streamed: list[tuple[str, str, str]] = []

    async def stream_logs(
        namespace: str, pod: str, container: str = "", **kwargs: Any
    ) -> AsyncIterator[Any]:
        lines_streamed.append((namespace, pod, container))
        return
        yield  # pragma: no cover

    app = make_app([_pod("web-1")], get_manifest=_get_manifest, stream_logs=stream_logs)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ContainersScreen)
        await pilot.press("l")  # first row = "app" → logs
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ContainersScreen)
        assert ("default", "web-1", "app") in lines_streamed


async def test_containers_screen_s_opens_shell() -> None:
    calls: list[list[str]] = []

    def _record_call(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    app = make_app([_pod("web-1")], get_manifest=_get_manifest)
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", side_effect=_record_call),
        patch.object(type(app), "suspend", side_effect=lambda *a: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ContainersScreen)
            await pilot.press("down", "s")  # second row = "sidecar" → shell
            await pilot.pause(0.2)
    assert calls, "kubectl exec was not invoked"
    assert "-c" in calls[0]
    assert calls[0][calls[0].index("-c") + 1] == "sidecar"


async def test_enter_without_manifest_falls_back_to_store() -> None:
    app = make_app([_pod("web-1", containers=("only",))])  # no get_manifest
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ContainersScreen)
        table = app.screen.query_one("DataTable")
        assert table.row_count == 1  # type: ignore[attr-defined]


@contextmanager
def _noop_cm() -> Any:
    yield


async def test_containers_screen_pick_cancelled_when_context_switched() -> None:
    """A containers screen that stayed open across a completed :ctx switch
    must not exec or stream: the pod selection belongs to the old cluster
    (issue #36 review round 12)."""
    calls: list[list[str]] = []

    def _record_call(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    app = make_app([_pod("web-1")], get_manifest=_get_manifest)
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", side_effect=_record_call),
        patch.object(type(app), "suspend", side_effect=lambda *a: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ContainersScreen)
            app._ctx_epoch += 1  # a context switch completed under the screen
            await pilot.press("s")
            await pilot.pause(0.2)
    assert calls == []
    assert any("kube context" in n.message for n in app._notifications)
