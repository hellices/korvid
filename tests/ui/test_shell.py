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
from korvid.ui.debug import DebugController
from korvid.ui.shell import (
    DEBUG_IMAGE,
    build_debug_argv,
    build_exec_argv,
)
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ImagePrompt
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

SH_FALLBACK = "command -v bash >/dev/null 2>&1 && exec bash || exec sh"


async def test_shell_uses_config_context() -> None:
    """s must invoke kubectl exec pinned to the app's kubeconfig context."""
    app = make_app([_pod("api-1")], kube_context="pinned-ctx")
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=0) as mock_call,
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(pilot, lambda: mock_call.call_count == 1, label="shell exec invoked")
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
        uid="uid-1",
    )


def _deploy(name: str, namespace: str = "default") -> GenericSummary:
    return GenericSummary(name=name, namespace=namespace, kind="Deployment", created="")


class _DefaultManifest:
    pass


_DEFAULT_MANIFEST = _DefaultManifest()


def make_app(
    pods: list[PodSummary],
    *,
    extra_data: dict[str, list[Summary]] | None = None,
    kube_context: str | None = None,
    audit: AuditLog | None = None,
    readonly: bool = False,
    permitted: bool | None = None,
    get_manifest: (
        Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | _DefaultManifest | None
    ) = _DEFAULT_MANIFEST,
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

    manifest_source: Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None
    if isinstance(get_manifest, _DefaultManifest):

        async def default_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
            del kind
            pod = next(pod for pod in pods if pod.namespace == ns and pod.name == name)
            return {"metadata": {"uid": pod.uid}}

        manifest_source = default_manifest
    else:
        manifest_source = get_manifest

    return KorvidApp(
        config=KorvidConfig(
            namespace="default",
            kube_context=kube_context,
            readonly=readonly,
            debug_images=debug_images,
            debug_default_image=debug_default_image,
        ),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_TEST_ALIASES),
        audit=audit,
        get_manifest=manifest_source,
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
    'hang-once' survives exactly one poll cycle, then exits 0; 'fail' exits
    nonzero immediately (kubectl gave up on its own)."""

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
        if self.behavior == "fail":
            return 1
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
        patch("shutil.which", return_value=None),
        patch("subprocess.call") as mock_call,
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await until(
                pilot,
                lambda: app._inspect_surface.cursor_row_key() == "default/api-1",
                label="pod row selected",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: any("kubectl not found" in n.message for n in app._notifications),
                label="kubectl missing notified",
            )
            notifications = [n.message for n in app._notifications]
            assert any("kubectl not found" in m for m in notifications)
            mock_call.assert_not_called()


async def test_shell_non_pods_kind_is_inert() -> None:
    """s off the pods/nodes views is gated (issue #114): the key is inert
    and subprocess.call is NOT invoked. The action itself still warns."""
    app = make_app(
        [],
        extra_data={"deployments": [_deploy("frontend")]},
    )
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call") as mock_call,
    ):
        async with app.run_test() as pilot:
            # Navigate to deployments
            await pilot.press("colon")
            for ch in "deployments":
                await pilot.press(ch)
            await pilot.press("enter")
            table = app.query_one(ResourceTable)
            await until(
                pilot,
                lambda: app.current_kind == "deployments" and table.row_count == 1,
                label="deployment view visible",
            )
            assert app.current_kind == "deployments"
            await pilot.press("s")
            await pilot.pause(0.1)
            assert not any("Shell is available" in n.message for n in app._notifications)
            mock_call.assert_not_called()
            # A direct invocation (bypassing the key gate) still explains itself.
            app.action_shell()
            await until(
                pilot,
                lambda: any(
                    "Shell is available for pods and nodes" in n.message for n in app._notifications
                ),
                label="shell scope warning shown",
            )
            notifications = [n.message for n in app._notifications]
            assert any("Shell is available for pods and nodes" in m for m in notifications)
            mock_call.assert_not_called()


async def test_shell_empty_table_warning() -> None:
    """s with empty table → warning notification; subprocess.call NOT invoked."""
    app = make_app([])
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call") as mock_call,
    ):
        async with app.run_test() as pilot:
            await pilot.press("s")
            await until(
                pilot,
                lambda: any("No resource selected" in n.message for n in app._notifications),
                label="empty selection warned",
            )
            notifications = [n.message for n in app._notifications]
            assert any("No resource selected" in m for m in notifications)
            mock_call.assert_not_called()


async def test_shell_selected_pod_invokes_kubectl() -> None:
    """s on a selected pod → subprocess.call called with correct argv."""
    app = make_app([_pod("api-1")])
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=0) as mock_call,
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(pilot, lambda: mock_call.call_count == 1, label="shell exec invoked")
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


async def test_shell_multi_container_shows_picker() -> None:
    """s on a multi-container pod → PickScreen listing containers; pick runs exec -c."""
    app = make_app([_multi_container_pod("web-1")])
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=0) as mock_call,
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="container picker opened",
            )
            assert isinstance(app.screen, PickScreen)
            mock_call.assert_not_called()  # nothing runs until a container is chosen
            await pilot.press("down")  # highlight "sidecar"
            await pilot.press("enter")
            await until(
                pilot,
                lambda: mock_call.call_count == 1,
                label="sidecar exec invoked",
            )
            mock_call.assert_called_once_with(build_exec_argv("default", "web-1", "sidecar"))


async def test_shell_multi_container_picker_escape_cancels() -> None:
    """Escaping the container picker runs nothing."""
    app = make_app([_multi_container_pod("web-1")])
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call") as mock_call,
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="container picker opened",
            )
            assert isinstance(app.screen, PickScreen)
            await pilot.press("escape")
            await until(
                pilot,
                lambda: not isinstance(app.screen, PickScreen),
                label="container picker closed",
            )
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
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", side_effect=_recording_call(calls)),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("enter")  # accept the recommended image
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")

            # The debug fallback and its audit records are written in a worker
            # after the dialog closes: wait for the final observable outcome
            # (the success audit entry) instead of a fixed sleep.
            def _debug_done() -> bool:
                if not debug_calls or not audit_path.exists():
                    return False
                lines = audit_path.read_text().splitlines()
                return bool(lines) and json.loads(lines[-1]).get("outcome") == "success"

            await until(pilot, _debug_done, label="debug execution audited")
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
        await until(
            pilot,
            lambda: app.query_one(ResourceTable).row_count == 1,
            label="pod row visible",
        )
        blocker = PickScreen("unrelated dialog", ["a", "b"])
        await app.push_screen(blocker)
        await until(pilot, lambda: app.screen is blocker, label="blocking dialog open")
        await app._shell._offer_debug_fallback("default", "api-1", None, 127, app._ctx.epoch())
        assert app.screen is blocker  # nothing stacked on top


async def test_shell_nonzero_exit_with_working_shell_no_fallback() -> None:
    """Non-zero exec exit but probe succeeds (user's command failed) → no offer."""
    app = make_app([_pod("api-1")])
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1) as mock_call,
        patch("subprocess.run", return_value=SimpleNamespace(returncode=0)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: mock_call.call_count == 1,
                label="shell exec attempted",
            )
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ConfirmScreen)


async def test_shell_exec_failure_no_declines_debug(tmp_path: Path) -> None:
    """Declining the fallback dialog runs nothing further."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"))
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1) as mock_call,
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("enter")
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("n")
            await until(
                pilot,
                lambda: not isinstance(app.screen, ConfirmScreen),
                label="debug dialog dismissed",
            )
            mock_call.assert_called_once()  # only the failed exec; no debug


async def test_debug_fallback_not_offered_in_readonly(tmp_path: Path) -> None:
    """kubectl debug mutates the pod spec: readonly sessions never get the
    fallback offer, matching every other gated write."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"), readonly=True)
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1) as mock_call,
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: mock_call.call_count == 1,
                label="shell exec attempted",
            )
            await pilot.pause(0.3)
            assert not isinstance(app.screen, ConfirmScreen)
            mock_call.assert_called_once()  # only the failed exec; no debug


async def test_debug_fallback_not_offered_without_audit() -> None:
    """Fail-closed: no audit sink means the mutating fallback is not offered."""
    app = make_app([_pod("api-1")])  # audit=None
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1) as mock_call,
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: mock_call.call_count == 1,
                label="shell exec attempted",
            )
            await pilot.pause(0.3)
            assert not isinstance(app.screen, ConfirmScreen)
            mock_call.assert_called_once()


async def test_debug_fallback_not_offered_without_permission(tmp_path: Path) -> None:
    """RBAC pre-check (spec 7 safety contract): without patch
    pods/ephemeralcontainers the offer is never shown - the user sees
    'missing permission' instead of an approval that would then fail."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"), permitted=False)
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1) as mock_call,
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: any(
                    "missing permission: patch pods/ephemeralcontainers" in n.message
                    for n in app._notifications
                ),
                label="missing permission notified",
            )
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
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
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
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            options = _pick_options(app)
            assert "lightruncom/koolkits:jvm" in options[0]
            assert any("netshoot" in opt for opt in options)
            assert any("busybox" in opt for opt in options)
            assert "Custom image…" in options[-1]
            await pilot.press("enter")
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            screen = app.screen
            assert isinstance(screen, ConfirmScreen)
            assert "lightruncom/koolkits:jvm" in screen._operation
            await pilot.press("y")

            await until(
                pilot,
                lambda: len(debug_calls) >= 1,
                label="runtime-aware debug launched",
            )
    assert "--image=lightruncom/koolkits:jvm" in debug_calls[0]
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    assert "lightruncom/koolkits:jvm" in entries[0]["detail"]


async def test_debug_picker_busybox_first_without_manifest(tmp_path: Path) -> None:
    """No manifest source offers generic images but blocks the mutation."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        [_pod("api-1")],
        audit=AuditLog(audit_path),
        get_manifest=None,
    )
    debug_calls: list[list[str]] = []
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            options = _pick_options(app)
            assert options[0].startswith(DEBUG_IMAGE)
            assert "netshoot" in options[1]
            assert "Custom image…" in options[-1]
            await pilot.press("enter")
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")
            await until(
                pilot,
                lambda: any("could not be verified" in str(n.message) for n in app._notifications),
                label="missing identity notification shown",
            )

    assert debug_calls == []
    assert not audit_path.exists() or "debug" not in audit_path.read_text()


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
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
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
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            options = _pick_options(app)
            assert "registry.corp.local/tools/debug-jvm:latest" in options[0]
            assert options[-1] == "Custom image…"
            assert len(options) == 2
            assert "busybox" not in " ".join(options)


async def test_debug_picker_explicit_empty_images_config_custom_only(tmp_path: Path) -> None:
    """debug.images configured as an empty mapping is a deliberate
    restriction: no public images are offered, only the custom prompt."""
    app = make_app(
        [_pod("api-1")],
        audit=AuditLog(tmp_path / "audit.jsonl"),
        get_manifest=_jvm_manifest(),
        debug_images={},
    )
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            assert _pick_options(app) == ["Custom image…"]


async def test_debug_picker_custom_image_prompt(tmp_path: Path) -> None:
    """'Custom image…' opens an input prompt; the typed image reaches the
    approval dialog and the kubectl debug argv."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"))
    debug_calls: list[list[str]] = []

    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            for _ in range(len(_pick_options(app)) - 1):
                await pilot.press("down")
            await pilot.press("enter")  # Custom image…
            await until(
                pilot,
                lambda: isinstance(app.screen, ImagePrompt),
                label="custom image prompt opened",
            )
            for ch in "my.registry/dbg:1":
                await pilot.press(ch)
            await pilot.press("enter")
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            screen = app.screen
            assert isinstance(screen, ConfirmScreen)
            assert "my.registry/dbg:1" in screen._operation
            await pilot.press("y")

            await until(
                pilot,
                lambda: len(debug_calls) >= 1,
                label="custom-image debug launched",
            )
    assert "--image=my.registry/dbg:1" in debug_calls[0]


async def test_debug_picker_escape_cancels(tmp_path: Path) -> None:
    """Escape on the image picker cancels the whole fallback: no ConfirmScreen,
    no debug execution, no debug audit entry."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_app([_pod("api-1")], audit=AuditLog(audit_path))
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1) as mock_call,
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("escape")
            await until(
                pilot,
                lambda: not isinstance(app.screen, (PickScreen, ConfirmScreen)),
                label="debug picker dismissed",
            )
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
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1),
        # First attach (koolkits:jvm) hangs on the pull; the retry succeeds.
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls, ["hang"])),
        patch(
            "subprocess.run",
            side_effect=_pull_failure_run("lightruncom/koolkits:jvm"),
        ),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("enter")  # koolkits:jvm recommendation
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")

            def _retry_offered() -> bool:
                screen = app.screen
                return isinstance(screen, ConfirmScreen) and "ErrImagePull" in screen._title

            await until(pilot, _retry_offered, label="retry offer opened")
            screen = app.screen
            assert isinstance(screen, ConfirmScreen)
            assert "ErrImagePull" in screen._title
            assert DEBUG_IMAGE in screen._operation
            assert "cannot be removed" in screen._operation
            await pilot.press("y")
            await until(
                pilot,
                lambda: len(debug_calls) >= 2,
                label="fallback retry launched",
            )
            # The outcome audit append is asynchronous: wait for the retry's
            # success entry before leaving the app context.
            await until(
                pilot,
                lambda: audit_path.exists() and '"success"' in audit_path.read_text(),
                label="fallback retry audited",
            )
    assert "--image=lightruncom/koolkits:jvm" in debug_calls[0]
    assert f"--image={DEBUG_IMAGE}" in debug_calls[1]
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    outcomes = [e["outcome"] for e in entries]
    assert any(o.startswith("error: image pull failed") for o in outcomes)
    assert outcomes[-1] == "success"


async def test_debug_pull_failure_detected_when_process_exits_nonzero(tmp_path: Path) -> None:
    """kubectl debug can give up and exit nonzero on its own when the pull
    fails; the pod status is still checked once so the fallback retry is
    offered instead of a generic exit warning."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_app([_pod("api-1")], audit=AuditLog(audit_path), get_manifest=_jvm_manifest())
    debug_calls: list[list[str]] = []

    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1),
        # The first attach exits nonzero immediately (kubectl gave up).
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls, ["fail"])),
        patch(
            "subprocess.run",
            side_effect=_pull_failure_run("lightruncom/koolkits:jvm"),
        ),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("enter")  # koolkits:jvm recommendation
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")

            def _retry_offered() -> bool:
                screen = app.screen
                return isinstance(screen, ConfirmScreen) and "ErrImagePull" in screen._title

            await until(pilot, _retry_offered, label="retry offer opened")
            await until(
                pilot,
                lambda: audit_path.exists() and "image pull failed" in audit_path.read_text(),
                label="image pull failure audited",
            )
    assert len(debug_calls) == 1


async def test_debug_pull_failure_no_retry_when_fallback_is_chosen_image(tmp_path: Path) -> None:
    """When the failed image IS the fallback there is nothing to retry with:
    the failure is surfaced as an error notification instead of a dialog."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"))
    debug_calls: list[list[str]] = []

    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls, ["hang"])),
        patch("subprocess.run", side_effect=_pull_failure_run(DEBUG_IMAGE)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("enter")  # busybox (no runtime detected)
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")

            def _failure_notified() -> bool:
                return any("image pull failed" in n.message for n in app._notifications)

            await until(pilot, _failure_notified, label="image pull failure notified")
            assert not isinstance(app.screen, ConfirmScreen)  # no retry dialog
    assert len(debug_calls) == 1


async def test_debug_pull_failure_no_retry_when_fallback_is_equivalent_ref(
    tmp_path: Path,
) -> None:
    """An untagged failed image and a :latest fallback are the same Kubernetes
    image: retrying would pull the identical image and permanently add another
    ephemeral container entry - notify only."""
    app = make_app(
        [_pod("api-1")],
        audit=AuditLog(tmp_path / "audit.jsonl"),
        debug_default_image="nicolaka/netshoot:latest",
    )
    debug_calls: list[list[str]] = []

    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls, ["hang"])),
        patch("subprocess.run", side_effect=_pull_failure_run("nicolaka/netshoot")),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            for _ in range(len(_pick_options(app)) - 1):
                await pilot.press("down")
            await pilot.press("enter")  # Custom image…
            await until(
                pilot,
                lambda: isinstance(app.screen, ImagePrompt),
                label="custom image prompt opened",
            )
            for ch in "nicolaka/netshoot":
                await pilot.press(ch)
            await pilot.press("enter")
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")

            def _failure_notified() -> bool:
                return any("image pull failed" in n.message for n in app._notifications)

            await until(pilot, _failure_notified, label="image pull failure notified")
            assert not isinstance(app.screen, ConfirmScreen)  # no retry dialog
    assert len(debug_calls) == 1


async def test_debug_pull_monitoring_disabled_without_baseline(tmp_path: Path) -> None:
    """When the pre-attach snapshot cannot be taken, pull monitoring is
    disabled for the attempt: without a reliable baseline a stale same-image
    failure from an earlier attempt could be blamed on this attach."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_app([_pod("api-1")], audit=AuditLog(audit_path))
    debug_calls: list[list[str]] = []
    stale_json = json.dumps(
        {
            "status": {
                "ephemeralContainerStatuses": [
                    {
                        "name": "debugger-old",
                        "image": DEBUG_IMAGE,
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
            return SimpleNamespace(returncode=1)
        gets += 1
        if gets == 1:  # the snapshot get fails transiently
            return SimpleNamespace(returncode=1, stdout=b"")
        return SimpleNamespace(returncode=0, stdout=stale_json)

    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls, ["hang"])),
        patch("subprocess.call", return_value=1),
        patch("subprocess.run", side_effect=fake_run),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("enter")
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")
            await until(
                pilot,
                lambda: audit_path.exists() and "success" in audit_path.read_text(),
                label="debug success audited",
            )
            assert not any("image pull failed" in n.message for n in app._notifications)
    assert len(debug_calls) == 1


async def test_debug_pull_failure_detected_on_final_poll_at_deadline(tmp_path: Path) -> None:
    """A pull failure appearing just as the polling window expires must still
    be detected: the status check runs after every timed wait, before the
    deadline switches to an unbounded wait."""
    app = make_app([_pod("api-1")], audit=AuditLog(tmp_path / "audit.jsonl"))
    debug_calls: list[list[str]] = []

    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls, ["hang"])),
        patch("subprocess.call", return_value=1),
        patch("subprocess.run", side_effect=_pull_failure_run(DEBUG_IMAGE)),
        # Deadline elapses immediately: the first timed wait is also the last.
        patch.object(DebugController, "PULL_CHECK_DEADLINE", 0.0),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("enter")  # busybox — same as fallback: notify only
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")
            await until(
                pilot,
                lambda: any("image pull failed" in n.message for n in app._notifications),
                label="final-poll failure notified",
            )
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
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls, ["hang"])),
        patch(
            "subprocess.run",
            side_effect=_pull_failure_run("registry.corp.local/tools/debug-jvm:latest"),
        ),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("enter")  # configured jvm image
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")

            def _failure_notified() -> bool:
                return any("image pull failed" in n.message for n in app._notifications)

            await until(pilot, _failure_notified, label="image pull failure notified")
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
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        # 'hang-once': the attach survives one poll cycle, then exits cleanly.
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls, ["hang-once"])),
        patch("subprocess.call", return_value=1),
        patch("subprocess.run", side_effect=fake_run),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("enter")  # busybox (no runtime detected)
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")
            await until(
                pilot,
                lambda: audit_path.exists() and "success" in audit_path.read_text(),
                label="debug success audited",
            )
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
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", side_effect=_recording_call(calls)),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("enter")
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")
            await until(
                pilot,
                lambda: any("was replaced" in str(n.message) for n in app._notifications),
                label="replacement notification shown",
            )
    assert [argv[1] for argv in calls] == ["exec"]
    assert debug_calls == []  # the debug never ran
    # No mutation happened, so no debug intent may have been audited either.
    assert not audit_path.exists() or "debug" not in audit_path.read_text()


async def test_debug_aborts_when_final_pod_uid_lookup_unavailable(tmp_path: Path) -> None:
    calls = 0

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"metadata": {"name": name, "namespace": ns or "", "uid": "uid-original"}}
        raise TimeoutError

    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        [_pod("api-1")],
        audit=AuditLog(audit_path),
        get_manifest=get_manifest,
    )
    shell_calls: list[list[str]] = []
    debug_calls: list[list[str]] = []

    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", side_effect=_recording_call(shell_calls)),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row loaded",
            )
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")
            await until(
                pilot,
                lambda: any("could not be verified" in str(n.message) for n in app._notifications),
            )

    assert [argv[1] for argv in shell_calls] == ["exec"]
    assert debug_calls == []
    assert not audit_path.exists() or "debug" not in audit_path.read_text()


async def test_debug_aborts_when_no_pod_uid_was_captured(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    app = make_app([_pod("api-1")], audit=AuditLog(audit_path))
    debug_calls: list[list[str]] = []

    with (
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test():
            await app._debug.run("default", "api-1", None, None, DEBUG_IMAGE)

    assert debug_calls == []
    assert not audit_path.exists() or "debug" not in audit_path.read_text()
    assert any("could not be verified" in str(n.message) for n in app._notifications)


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
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", side_effect=_recording_call(calls)),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("enter")
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")
            await until(
                pilot,
                lambda: len(debug_calls) >= 1,
                label="debug attach launched",
            )
    assert [argv[1] for argv in calls] == ["exec"]
    assert debug_calls[0][1] == "debug"


async def test_debug_aborts_when_baseline_snapshot_sees_replacement(tmp_path: Path) -> None:
    """The pre-attach baseline snapshot can block for seconds; if it observes
    a different pod uid than the approved one, the attach never starts."""
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        [_pod("api-1")],
        audit=AuditLog(audit_path),
        get_manifest=_uid_manifests(["uid-original"]),
    )
    calls: list[list[str]] = []
    debug_calls: list[list[str]] = []
    replaced_pod = json.dumps({"metadata": {"uid": "uid-replacement"}, "status": {}}).encode()

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        if argv[1] == "exec":  # shell probe: no usable shell -> debug offered
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0, stdout=replaced_pod)

    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", side_effect=_recording_call(calls)),
        patch("subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("subprocess.run", side_effect=fake_run),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="debug picker opened",
            )
            await pilot.press("enter")
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="debug confirmation opened",
            )
            await pilot.press("y")
            await until(
                pilot,
                lambda: any("was replaced" in str(n.message) for n in app._notifications),
                label="baseline replacement notified",
            )
            await until(
                pilot,
                lambda: (
                    audit_path.exists() and "pod replaced before attach" in audit_path.read_text()
                ),
                label="baseline replacement audited",
            )
    assert debug_calls == []  # kubectl debug never started


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
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call", return_value=1) as mock_call,
        patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: any("no longer exists" in str(n.message) for n in app._notifications),
                label="missing pod notified",
            )
            assert not isinstance(app.screen, (PickScreen, ConfirmScreen))
            mock_call.assert_called_once()  # only the failed exec; no debug


async def test_shell_refused_while_context_switching() -> None:
    """Pressing `s` during a :ctx switch is refused up front: the exec would
    race the teardown and could attach to whichever cluster wins (issue #36)."""
    app = make_app([_pod("api-1")])
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call") as mock_call,
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count == 1,
                label="pod row visible",
            )
            app._ctx._switching = True
            try:
                await pilot.press("s")
                await until(
                    pilot,
                    lambda: any(
                        "context switch is in progress" in n.message for n in app._notifications
                    ),
                    label="shell refusal",
                )
            finally:
                app._ctx._switching = False
            mock_call.assert_not_called()


async def test_shell_picker_cancelled_when_context_switched_while_open() -> None:
    """A container picker that stayed open across a completed :ctx switch
    must not exec: the selection belongs to the old cluster while kubectl
    would target the new one (issue #36 review round 11)."""
    app = make_app([_multi_container_pod("web-1")])
    with (
        patch("shutil.which", return_value="/usr/bin/kubectl"),
        patch("subprocess.call") as mock_call,
        patch.object(type(app), "suspend", return_value=_noop_cm()),
    ):
        async with app.run_test() as pilot:
            await until(
                pilot,
                lambda: app.query_one(ResourceTable).row_count > 0,
                label="pod row visible",
            )
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, PickScreen),
                label="container picker open",
            )
            app._ctx._epoch += 1  # a context switch completed under the picker
            await pilot.press("enter")
            await until(
                pilot,
                lambda: any("kube context" in n.message for n in app._notifications),
                label="picker epoch refusal",
            )
            mock_call.assert_not_called()
