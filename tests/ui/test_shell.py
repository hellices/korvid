"""Tests for shell.py argv builder and action_shell integration."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
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
from korvid.ui.shell import (
    DEBUG_IMAGE,
    build_debug_argv,
    build_exec_argv,
    build_pod_get_argv,
    build_probe_argv,
)
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ImagePrompt
from korvid.ui.widgets.pick_screen import PickScreen

from .waits import until

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
    get_manifest: Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None = None,
    debug_images: dict[str, str] | None = None,
    debug_default_image: str | None = None,
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
        config=KorvidConfig(
            namespace="default",
            kube_context=kube_context,
            readonly=readonly,
            debug_images=debug_images or {},
            debug_default_image=debug_default_image,
        ),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_TEST_ALIASES),
        audit=audit,
        get_manifest=get_manifest,
        check_permission=None if permitted is None else check_permission,
    )


@contextmanager
def _noop_cm() -> Any:
    yield


def _pick_options(app: KorvidApp) -> list[str]:
    """Prompts of the currently shown PickScreen, in order."""
    from textual.widgets import OptionList

    screen = app.screen
    assert isinstance(screen, PickScreen)
    option_list = screen.query_one(OptionList)
    return [str(option_list.get_option_at_index(i).prompt) for i in range(option_list.option_count)]


class _FakeProc:
    """subprocess.Popen stand-in: 'ok' exits 0 immediately; 'hang' raises
    TimeoutExpired on timed waits until killed (a stuck image pull);
    'hang-once' survives exactly one poll cycle, then exits 0."""

    def __init__(self, argv: list[str], behavior: str) -> None:
        self.argv = argv
        self.behavior = behavior
        self.killed = False
        self.timed_waits = 0

    def wait(self, timeout: float | None = None) -> int:
        if self.killed:
            return 137
        if self.behavior == "ok":
            return 0
        if timeout is not None:
            self.timed_waits += 1
            if self.behavior == "hang-once" and self.timed_waits > 1:
                return 0
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)
        return 0

    def kill(self) -> None:
        self.killed = True


def _fake_popen(
    records: list[list[str]], behaviors: list[str] | None = None
) -> Callable[..., _FakeProc]:
    """Popen factory recording argv; behaviors are consumed per call ('ok' default)."""

    def factory(argv: list[str], **kwargs: Any) -> _FakeProc:
        behavior = behaviors[len(records)] if behaviors and len(records) < len(behaviors) else "ok"
        records.append(argv)
        return _FakeProc(argv, behavior)

    return factory


def _recording_call(records: list[list[str]], exit_code: int = 1) -> Callable[[list[str]], int]:
    """subprocess.call fake recording argv and returning `exit_code`."""

    def call(argv: list[str]) -> int:
        records.append(argv)
        return exit_code

    return call


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


def test_build_debug_argv_custom_image() -> None:
    """The recommended toolkit image (issue #52) replaces the busybox default."""
    result = build_debug_argv("ns", "pod", "app", image="lightruncom/koolkits:jvm")
    assert "--image=lightruncom/koolkits:jvm" in result
    assert f"--image={DEBUG_IMAGE}" not in result


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
    """Failed exec (distroless) → image picker, then ConfirmScreen; y runs
    kubectl debug. A pod mutation, so the executed fallback is audited."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_app([_pod("api-1")], audit=AuditLog(audit_path))
    calls: list[list[str]] = []
    debug_calls: list[list[str]] = []

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", side_effect=_recording_call(calls)),
        patch("korvid.ui.app.subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            await pilot.press("enter")  # accept the recommended image
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")

            # The debug fallback and its audit records are written in a worker
            # after the dialog closes: wait for the final observable outcome
            # (the success audit entry) instead of a fixed sleep.
            def _debug_done() -> bool:
                if not debug_calls or not audit_path.exists():
                    return False
                lines = audit_path.read_text().splitlines()
                return bool(lines) and json.loads(lines[-1]).get("outcome") == "success"

            await until(pilot, _debug_done)
            assert calls[0] == build_exec_argv("default", "api-1")
            assert debug_calls[0] == build_debug_argv("default", "api-1")
            entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
            assert entries[0]["action"] == "debug"
            assert entries[0]["outcome"] == "intent"
            assert entries[-1]["outcome"] == "success"


async def test_debug_fallback_not_offered_over_open_dialog(tmp_path: Path) -> None:
    """If another dialog opened while the probe/RBAC pre-check ran, the offer
    aborts instead of stacking the picker where a buffered Enter would select
    "Yes" and start a pod mutation the user never saw."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        blocker = PickScreen("unrelated dialog", ["a", "b"])
        await app.push_screen(blocker)
        await app._offer_debug_fallback("default", "api-1", None, 127)
        await pilot.pause(0.1)
        assert app.screen is blocker  # nothing stacked on top


async def test_shell_nonzero_exit_with_working_shell_no_fallback() -> None:
    """Non-zero exec exit but probe succeeds (user's command failed) → no offer."""
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
            assert not isinstance(app.screen, ConfirmScreen)


async def test_shell_exec_failure_no_declines_debug(tmp_path: Path) -> None:
    """Declining the fallback dialog runs nothing further."""
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
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("n")
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


def test_build_pod_get_argv() -> None:
    result = build_pod_get_argv("prod", "api-1", context="staging")
    assert result == [
        "kubectl",
        "get",
        "pod",
        "--context",
        "staging",
        "-n",
        "prod",
        "api-1",
        "-o",
        "json",
    ]


def test_build_pod_get_argv_no_context() -> None:
    result = build_pod_get_argv("default", "api-1")
    assert result == ["kubectl", "get", "pod", "-n", "default", "api-1", "-o", "json"]


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
            assert not isinstance(app.screen, ConfirmScreen)
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
            assert not isinstance(app.screen, ConfirmScreen)
            mock_call.assert_called_once()


async def test_debug_fallback_not_offered_without_permission(tmp_path: Path) -> None:
    """RBAC pre-check (spec 7 safety contract): without patch
    pods/ephemeralcontainers the offer is never shown - the user sees
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
            assert not isinstance(app.screen, ConfirmScreen)
            notifications = [n.message for n in app._notifications]
            assert any(
                "missing permission: patch pods/ephemeralcontainers" in m for m in notifications
            )
            mock_call.assert_called_once()  # only the failed exec; no debug


async def test_debug_fallback_offered_with_permission(tmp_path: Path) -> None:
    """With patch pods/ephemeralcontainers allowed the offer still appears."""
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


# ---------------------------------------------------------------------------
# Runtime-aware debug image recommendation (issue #52)
# ---------------------------------------------------------------------------


def _jvm_manifest() -> Callable[[str, str | None, str], Awaitable[dict[str, Any]]]:
    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return {
            "metadata": {"name": name, "namespace": ns or "", "uid": "uid-jvm"},
            "spec": {"containers": [{"name": "app", "image": "openjdk:17-jdk"}]},
        }

    return get_manifest


async def test_debug_picker_recommends_runtime_image(tmp_path: Path) -> None:
    """A detected JVM runtime leads with koolkits:jvm; the chosen image lands
    in the approval dialog text, the kubectl debug argv, and the audit detail."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_app([_pod("api-1")], audit=AuditLog(audit_path), get_manifest=_jvm_manifest())
    debug_calls: list[list[str]] = []

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1),
        patch("korvid.ui.app.subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            options = _pick_options(app)
            assert "lightruncom/koolkits:jvm" in options[0]
            assert any("netshoot" in opt for opt in options)
            assert any("busybox" in opt for opt in options)
            assert "Custom image…" in options[-1]
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            screen = app.screen
            assert isinstance(screen, ConfirmScreen)
            assert "lightruncom/koolkits:jvm" in screen._operation
            await pilot.press("y")

            await until(pilot, lambda: len(debug_calls) >= 1)
    assert "--image=lightruncom/koolkits:jvm" in debug_calls[0]
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    assert "lightruncom/koolkits:jvm" in entries[0]["detail"]


async def test_debug_picker_busybox_first_without_manifest(tmp_path: Path) -> None:
    """No manifest source → no runtime detection → busybox leads the picker."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"))
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1),
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            options = _pick_options(app)
            assert options[0].startswith(DEBUG_IMAGE)
            assert "netshoot" in options[1]
            assert "Custom image…" in options[-1]


async def test_debug_picker_air_gapped_config_only_configured_images(tmp_path: Path) -> None:
    """With debug.images configured, only configured images are offered —
    no public-registry assumption (koolkits/netshoot never appear)."""
    app = make_app(
        [_pod("api-1")],
        audit=AuditLog(tmp_path / "audit.jsonl"),
        get_manifest=_jvm_manifest(),
        debug_images={"jvm": "registry.corp.local/tools/debug-jvm:latest"},
        debug_default_image="registry.corp.local/tools/busybox:1.36",
    )
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1),
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            options = _pick_options(app)
            assert "registry.corp.local/tools/debug-jvm:latest" in options[0]
            assert "registry.corp.local/tools/busybox:1.36" in options[1]
            joined = " ".join(options)
            assert "koolkits" not in joined
            assert "netshoot" not in joined


async def test_debug_picker_air_gapped_without_default_omits_busybox(tmp_path: Path) -> None:
    """debug.images without debug.default_image must not leak public busybox
    into the picker - only the configured image plus the custom prompt."""
    app = make_app(
        [_pod("api-1")],
        audit=AuditLog(tmp_path / "audit.jsonl"),
        get_manifest=_jvm_manifest(),
        debug_images={"jvm": "registry.corp.local/tools/debug-jvm:latest"},
    )
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1),
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            options = _pick_options(app)
            assert "registry.corp.local/tools/debug-jvm:latest" in options[0]
            assert options[-1] == "Custom image…"
            assert len(options) == 2
            assert "busybox" not in " ".join(options)


async def test_debug_picker_custom_image_prompt(tmp_path: Path) -> None:
    """'Custom image…' opens an input prompt; the typed image reaches the
    approval dialog and the kubectl debug argv."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"))
    debug_calls: list[list[str]] = []

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1),
        patch("korvid.ui.app.subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            for _ in range(len(_pick_options(app)) - 1):
                await pilot.press("down")
            await pilot.press("enter")  # Custom image…
            await until(pilot, lambda: isinstance(app.screen, ImagePrompt))
            for ch in "my.registry/dbg:1":
                await pilot.press(ch)
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            screen = app.screen
            assert isinstance(screen, ConfirmScreen)
            assert "my.registry/dbg:1" in screen._operation
            await pilot.press("y")

            await until(pilot, lambda: len(debug_calls) >= 1)
    assert "--image=my.registry/dbg:1" in debug_calls[0]


async def test_debug_picker_escape_cancels(tmp_path: Path) -> None:
    """Escape on the image picker cancels the whole fallback: no ConfirmScreen,
    no debug execution, no debug audit entry."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_app([_pod("api-1")], audit=AuditLog(audit_path))
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1) as mock_call,
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, (PickScreen, ConfirmScreen))
            mock_call.assert_called_once()  # only the failed exec
    assert not audit_path.exists() or "debug" not in audit_path.read_text()


def _pull_failure_run(failed_image: str) -> Callable[..., SimpleNamespace]:
    """subprocess.run fake: shell probe fails (sh missing); the first pod get
    (the pre-attach snapshot) sees no ephemeral containers, later gets report
    an ErrImagePull ephemeral container for `failed_image` - modelling the
    entry kubectl debug creates after the snapshot."""
    empty_json = json.dumps({"status": {"ephemeralContainerStatuses": []}})
    pod_json = json.dumps(
        {
            "status": {
                "ephemeralContainerStatuses": [
                    {
                        "name": "debugger",
                        "image": failed_image,
                        "state": {"waiting": {"reason": "ErrImagePull"}},
                    }
                ]
            }
        }
    )
    gets = 0

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        nonlocal gets
        if argv[1] == "exec":
            return SimpleNamespace(returncode=1)  # probe: no sh in the target
        gets += 1
        return SimpleNamespace(returncode=0, stdout=empty_json if gets == 1 else pod_json)

    return fake_run


async def test_debug_pull_failure_offers_retry_with_fallback(tmp_path: Path) -> None:
    """A hung attach whose ephemeral container reports ErrImagePull is killed
    and a retry with the fallback image is offered; the dialog states that the
    failed entry stays in the pod spec. y attaches with the fallback image."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_app([_pod("api-1")], audit=AuditLog(audit_path), get_manifest=_jvm_manifest())
    debug_calls: list[list[str]] = []

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1),
        # First attach (koolkits:jvm) hangs on the pull; the retry succeeds.
        patch("korvid.ui.app.subprocess.Popen", side_effect=_fake_popen(debug_calls, ["hang"])),
        patch(
            "korvid.ui.app.subprocess.run",
            side_effect=_pull_failure_run("lightruncom/koolkits:jvm"),
        ),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            await pilot.press("enter")  # koolkits:jvm recommendation
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")

            def _retry_offered() -> bool:
                screen = app.screen
                return isinstance(screen, ConfirmScreen) and "ErrImagePull" in screen._title

            await until(pilot, _retry_offered)
            screen = app.screen
            assert isinstance(screen, ConfirmScreen)
            assert "ErrImagePull" in screen._title
            assert DEBUG_IMAGE in screen._operation
            assert "cannot be removed" in screen._operation
            await pilot.press("y")
            await until(pilot, lambda: len(debug_calls) >= 2)
    assert "--image=lightruncom/koolkits:jvm" in debug_calls[0]
    assert f"--image={DEBUG_IMAGE}" in debug_calls[1]
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    outcomes = [e["outcome"] for e in entries]
    assert any(o.startswith("error: image pull failed") for o in outcomes)
    assert outcomes[-1] == "success"


async def test_debug_pull_failure_no_retry_when_fallback_is_chosen_image(tmp_path: Path) -> None:
    """When the failed image IS the fallback there is nothing to retry with:
    the failure is surfaced as an error notification instead of a dialog."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"))
    debug_calls: list[list[str]] = []

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1),
        patch("korvid.ui.app.subprocess.Popen", side_effect=_fake_popen(debug_calls, ["hang"])),
        patch("korvid.ui.app.subprocess.run", side_effect=_pull_failure_run(DEBUG_IMAGE)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            await pilot.press("enter")  # busybox (no runtime detected)
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")

            def _failure_notified() -> bool:
                return any("image pull failed" in n.message for n in app._notifications)

            await until(pilot, _failure_notified)
            assert not isinstance(app.screen, ConfirmScreen)  # no retry dialog
    assert len(debug_calls) == 1


async def test_debug_pull_failure_air_gapped_without_default_notifies_only(
    tmp_path: Path,
) -> None:
    """debug.images without debug.default_image: a pull failure must not offer
    a public busybox retry - error notification only."""
    app = make_app(
        [_pod("api-1")],
        audit=AuditLog(tmp_path / "audit.jsonl"),
        get_manifest=_jvm_manifest(),
        debug_images={"jvm": "registry.corp.local/tools/debug-jvm:latest"},
    )
    debug_calls: list[list[str]] = []

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1),
        patch("korvid.ui.app.subprocess.Popen", side_effect=_fake_popen(debug_calls, ["hang"])),
        patch(
            "korvid.ui.app.subprocess.run",
            side_effect=_pull_failure_run("registry.corp.local/tools/debug-jvm:latest"),
        ),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            await pilot.press("enter")  # configured jvm image
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")

            def _failure_notified() -> bool:
                return any("image pull failed" in n.message for n in app._notifications)

            await until(pilot, _failure_notified)
            assert not isinstance(app.screen, ConfirmScreen)  # no busybox retry
    assert len(debug_calls) == 1


async def test_debug_stale_failed_entry_with_same_image_not_blamed(tmp_path: Path) -> None:
    """A failed ephemeral container from an EARLIER attempt (same image - such
    entries can never be removed from the pod spec) must not kill a new attach
    that is pulling fine: pre-existing entries are snapshotted before the
    attach and ignored while polling."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_app([_pod("api-1")], audit=AuditLog(audit_path))
    debug_calls: list[list[str]] = []
    # The stale entry is present from the very first pod get (the snapshot).
    stale_json = json.dumps(
        {
            "status": {
                "ephemeralContainerStatuses": [
                    {
                        "name": "debugger-old",
                        "image": DEBUG_IMAGE,
                        "state": {"waiting": {"reason": "ImagePullBackOff"}},
                    }
                ]
            }
        }
    )

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        if argv[1] == "exec":
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0, stdout=stale_json)

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        # 'hang-once': the attach survives one poll cycle, then exits cleanly.
        patch(
            "korvid.ui.app.subprocess.Popen", side_effect=_fake_popen(debug_calls, ["hang-once"])
        ),
        patch("korvid.ui.app.subprocess.call", return_value=1),
        patch("korvid.ui.app.subprocess.run", side_effect=fake_run),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            await pilot.press("enter")  # busybox (no runtime detected)
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")
            await until(pilot, lambda: audit_path.exists() and "success" in audit_path.read_text())
            assert not isinstance(app.screen, ConfirmScreen)  # no retry dialog
            assert not any("image pull failed" in n.message for n in app._notifications)
    assert len(debug_calls) == 1


# ---------------------------------------------------------------------------
# Debug fallback bound to the approved pod incarnation
# ---------------------------------------------------------------------------


def _uid_manifests(uids: list[str]) -> Callable[[str, str | None, str], Awaitable[dict[str, Any]]]:
    """get_manifest fake yielding the next uid per call (the last uid repeats)."""
    calls: list[str] = []

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        uid = uids[min(len(calls), len(uids) - 1)]
        calls.append(kind)
        return {"metadata": {"name": name, "namespace": ns or "", "uid": uid}}

    return get_manifest


async def test_debug_aborts_when_pod_replaced_after_prompt(tmp_path: Path) -> None:
    """kubectl debug addresses the pod by namespace/name only, so the offer
    captures the pod uid and the execution re-checks it: a same-named
    replacement created while the dialog was open aborts the debug."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        [_pod("api-1")],
        audit=AuditLog(audit_path),
        get_manifest=_uid_manifests(["uid-original", "uid-replacement"]),
    )
    calls: list[list[str]] = []
    debug_calls: list[list[str]] = []

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", side_effect=_recording_call(calls)),
        patch("korvid.ui.app.subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")
            await until(
                pilot,
                lambda: any("was replaced" in str(n.message) for n in app._notifications),
            )
    assert [argv[1] for argv in calls] == ["exec"]
    assert debug_calls == []  # the debug never ran
    # No mutation happened, so no debug intent may have been audited either.
    assert not audit_path.exists() or "debug" not in audit_path.read_text()


async def test_debug_runs_when_pod_uid_unchanged(tmp_path: Path) -> None:
    """Same incarnation at prompt and execution time -> the debug proceeds."""
    app = make_app(
        [_pod("api-1")],
        audit=AuditLog(tmp_path / "audit.jsonl"),
        get_manifest=_uid_manifests(["uid-stable"]),
    )
    calls: list[list[str]] = []
    debug_calls: list[list[str]] = []

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", side_effect=_recording_call(calls)),
        patch("korvid.ui.app.subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")
            await until(pilot, lambda: len(debug_calls) >= 1)
    assert [argv[1] for argv in calls] == ["exec"]
    assert debug_calls[0][1] == "debug"


async def test_debug_not_offered_when_pod_gone(tmp_path: Path) -> None:
    """404 on the pre-prompt uid capture means the pod is already gone: no
    ConfirmScreen is offered for a target that cannot be debugged."""
    from korvid.k8s.errors import ApiStatusError

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        raise ApiStatusError(404, "NotFound")

    app = make_app(
        [_pod("api-1")],
        audit=AuditLog(tmp_path / "audit.jsonl"),
        get_manifest=get_manifest,
    )
    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", return_value=1) as mock_call,
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, (PickScreen, ConfirmScreen))
            mock_call.assert_called_once()  # only the failed exec; no debug
