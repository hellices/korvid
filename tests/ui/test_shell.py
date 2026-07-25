"""Tests for shell.py argv builder and action_shell integration."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.shell import DEBUG_IMAGE, build_debug_argv, build_exec_argv, build_probe_argv
from korvid.ui.widgets.pick_screen import PickScreen

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


def test_argv_builders_pin_context_when_given() -> None:
    for builder in (build_exec_argv, build_probe_argv, build_debug_argv):
        argv = builder("ns", "pod", "ctr", context="my-cluster")
        idx = argv.index("--context")
        assert argv[idx + 1] == "my-cluster"
        # --context must precede `--` so kubectl parses it as its own flag.
        assert idx < argv.index("--")


def test_argv_builders_omit_context_when_none() -> None:
    for builder in (build_exec_argv, build_probe_argv, build_debug_argv):
        assert "--context" not in builder("ns", "pod", "ctr", context=None)


async def test_shell_uses_config_context() -> None:
    """s must invoke kubectl exec pinned to the app's kubeconfig context."""
    app = make_app([_pod("api-1")], kube_context="pinned-ctx")
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=0) as mock_call,
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.1)
            argv = mock_call.call_args[0][0]
            idx = argv.index("--context")
            assert argv[idx + 1] == "pinned-ctx"


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
    kube_context: str | None = None,
    audit: AuditLog | None = None,
    readonly: bool = False,
    permitted: bool | None = None,
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

    async def check_permission(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        assert permitted is not None
        return permitted

    return KorvidApp(
        config=KorvidConfig(namespace="default", kube_context=kube_context, readonly=readonly),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_TEST_ALIASES),
        audit=audit,
        check_permission=None if permitted is None else check_permission,
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
        patch("korvid.ui.app.subprocess.call", return_value=0) as mock_call,
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.1)
            expected_argv = build_exec_argv("default", "api-1")
            mock_call.assert_called_once_with(expected_argv)


# ---------------------------------------------------------------------------
# Container picker + kubectl debug fallback
# ---------------------------------------------------------------------------


def _multi_container_pod(name: str, namespace: str = "default") -> PodSummary:
    return PodSummary(
        name=name,
        namespace=namespace,
        phase="Running",
        ready="2/2",
        restarts=0,
        node=None,
        qos="-",
        containers=("app", "sidecar"),
    )


def test_build_debug_argv_with_target() -> None:
    result = build_debug_argv("kube-system", "cilium-abc", "cilium-agent")
    assert result == [
        "kubectl",
        "debug",
        "-it",
        "-n",
        "kube-system",
        "cilium-abc",
        f"--image={DEBUG_IMAGE}",
        "--target=cilium-agent",
        "--",
        "sh",
    ]


def test_build_debug_argv_without_target() -> None:
    result = build_debug_argv("ns", "pod", None)
    assert "--target" not in " ".join(result)
    assert f"--image={DEBUG_IMAGE}" in result


async def test_shell_multi_container_shows_picker() -> None:
    """s on a multi-container pod → PickScreen listing containers; pick runs exec -c."""
    app = make_app([_multi_container_pod("web-1")])
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=0) as mock_call,
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.2)
            assert isinstance(app.screen, PickScreen)
            mock_call.assert_not_called()  # nothing runs until a container is chosen
            await pilot.press("down")  # highlight "sidecar"
            await pilot.press("enter")
            await pilot.pause(0.2)
            mock_call.assert_called_once_with(build_exec_argv("default", "web-1", "sidecar"))


async def test_shell_multi_container_picker_escape_cancels() -> None:
    """Escaping the container picker runs nothing."""
    app = make_app([_multi_container_pod("web-1")])
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call") as mock_call,
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.2)
            assert isinstance(app.screen, PickScreen)
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, PickScreen)
            mock_call.assert_not_called()


async def test_shell_exec_failure_offers_debug_fallback(tmp_path: Path) -> None:
    """Failed exec (distroless) → PickScreen offering kubectl debug; Yes runs it.
    kubectl debug is a pod mutation, so the executed fallback is audited."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_app([_pod("api-1")], audit=AuditLog(audit_path))
    calls: list[list[str]] = []

    def _fake_call(argv: list[str]) -> int:
        calls.append(argv)
        return 1 if argv[1] == "exec" else 0

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", side_effect=_fake_call),
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.2)
            assert isinstance(app.screen, PickScreen)
            await pilot.press("enter")  # first option = Yes
            await pilot.pause(0.2)
            assert calls[0] == build_exec_argv("default", "api-1")
            assert calls[1] == build_debug_argv("default", "api-1")
            entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
            assert entries[0]["action"] == "debug"
            assert entries[0]["outcome"] == "intent"
            assert entries[-1]["outcome"] == "success"


async def test_shell_nonzero_exit_with_working_shell_no_fallback() -> None:
    """Non-zero exec exit but probe succeeds (user's command failed) → no picker."""
    app = make_app([_pod("api-1")])
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1),
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=0)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, PickScreen)


async def test_shell_exec_failure_no_declines_debug(tmp_path: Path) -> None:
    """Choosing No in the fallback picker runs nothing further."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"))
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1) as mock_call,
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.2)
            assert isinstance(app.screen, PickScreen)
            await pilot.press("down")  # highlight "No"
            await pilot.press("enter")
            await pilot.pause(0.2)
            mock_call.assert_called_once()  # only the failed exec; no debug


def test_build_probe_argv() -> None:
    result = build_probe_argv("kube-system", "coredns-abc", "coredns")
    assert result == [
        "kubectl",
        "exec",
        "-n",
        "kube-system",
        "coredns-abc",
        "-c",
        "coredns",
        "--",
        "sh",
        "-c",
        "exit 0",
    ]
    assert "-it" not in result  # probe must be non-interactive


async def test_debug_fallback_not_offered_in_readonly(tmp_path: Path) -> None:
    """kubectl debug mutates the pod spec: readonly sessions never get the
    fallback offer, matching every other gated write."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"), readonly=True)
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1) as mock_call,
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, PickScreen)
            mock_call.assert_called_once()  # only the failed exec; no debug


async def test_debug_fallback_not_offered_without_audit() -> None:
    """Fail-closed: no audit sink means the mutating fallback is not offered."""
    app = make_app([_pod("api-1")])  # audit=None
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1) as mock_call,
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, PickScreen)
            mock_call.assert_called_once()


async def test_debug_fallback_not_offered_without_permission(tmp_path: Path) -> None:
    """RBAC pre-check (spec 7 safety contract): without patch
    pods/ephemeralcontainers the picker is never shown - the user sees
    'missing permission' instead of an approval that would then fail."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"), permitted=False)
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1) as mock_call,
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, PickScreen)
            notifications = [n.message for n in app._notifications]
            assert any(
                "missing permission: patch pods/ephemeralcontainers" in m for m in notifications
            )
            mock_call.assert_called_once()  # only the failed exec; no debug


async def test_debug_fallback_offered_with_permission(tmp_path: Path) -> None:
    """With patch pods/ephemeralcontainers allowed the picker still appears."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"), permitted=True)
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1),
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.3)
            assert isinstance(app.screen, PickScreen)
