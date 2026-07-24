"""Tests for shell.py argv builder and action_shell integration."""

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
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.shell import build_exec_argv

# ---------------------------------------------------------------------------
# Pure unit tests: argv builder
# ---------------------------------------------------------------------------

SH_FALLBACK = "command -v bash >/dev/null 2>&1 && exec bash || exec sh"


def test_build_exec_argv_without_container() -> None:
    result = build_exec_argv("default", "my-pod")
    assert result == [
        "kubectl",
        "exec",
        "-it",
        "-n",
        "default",
        "my-pod",
        "--",
        "sh",
        "-c",
        SH_FALLBACK,
    ]


def test_build_exec_argv_with_container() -> None:
    result = build_exec_argv("kube-system", "coredns-abc", "coredns")
    assert result == [
        "kubectl",
        "exec",
        "-it",
        "-n",
        "kube-system",
        "coredns-abc",
        "-c",
        "coredns",
        "--",
        "sh",
        "-c",
        SH_FALLBACK,
    ]


def test_build_exec_argv_container_none_omits_flag() -> None:
    result = build_exec_argv("ns", "pod", None)
    # "--" must immediately follow the pod name (no "-c <container>" between them)
    double_dash_idx = result.index("--")
    assert result[double_dash_idx - 1] == "pod"


# ---------------------------------------------------------------------------
# Pilot tests: action_shell integration
# ---------------------------------------------------------------------------

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))

_TEST_ALIASES: dict[str, ResourceMeta] = {
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
    )


def _deploy(name: str, namespace: str = "default") -> GenericSummary:
    return GenericSummary(name=name, namespace=namespace, kind="Deployment", created="")


def make_app(
    pods: list[PodSummary],
    *,
    extra_data: dict[str, list[Summary]] | None = None,
) -> KorvidApp:
    store = ResourceStore()
    all_data: dict[str, list[Summary]] = {"pods": list(pods)}
    if extra_data:
        all_data.update(extra_data)

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in all_data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_TEST_ALIASES),
    )


@contextmanager
def _noop_cm() -> Any:
    yield


async def test_shell_kubectl_missing_error_notify() -> None:
    """s with kubectl missing → error notification; subprocess.call NOT invoked."""
    app = make_app([_pod("api-1")])
    with (
        patch("korvid.ui.app.shutil.which", return_value=None),
        patch("korvid.ui.app.subprocess.call") as mock_call,
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.1)
            notifications = [n.message for n in app._notifications]
            assert any("kubectl not found" in m for m in notifications)
            mock_call.assert_not_called()


async def test_shell_non_pods_kind_warning() -> None:
    """s on non-pods kind → warning notification; subprocess.call NOT invoked."""
    app = make_app(
        [],
        extra_data={"deployments": [_deploy("frontend")]},
    )
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call") as mock_call,
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            # Navigate to deployments
            await pilot.press("colon")
            for ch in "deployments":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert app.current_kind == "deployments"
            await pilot.press("s")
            await pilot.pause(0.1)
            notifications = [n.message for n in app._notifications]
            assert any("Shell is only available for pods" in m for m in notifications)
            mock_call.assert_not_called()


async def test_shell_empty_table_warning() -> None:
    """s with empty table → warning notification; subprocess.call NOT invoked."""
    app = make_app([])
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call") as mock_call,
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.1)
            notifications = [n.message for n in app._notifications]
            assert any("No resource selected" in m for m in notifications)
            mock_call.assert_not_called()


async def test_shell_selected_pod_invokes_kubectl() -> None:
    """s on a selected pod → subprocess.call called with correct argv."""
    app = make_app([_pod("api-1")])
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call") as mock_call,
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.1)
            expected_argv = build_exec_argv("default", "api-1")
            mock_call.assert_called_once_with(expected_argv)
