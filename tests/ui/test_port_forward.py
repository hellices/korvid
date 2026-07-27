"""Tests for port-forward UI flows (issue #38): shift+f dialog, :pf list."""

from __future__ import annotations

import asyncio
import io
import queue
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from textual.widgets import Input

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.portforward import ForwardRegistry, ForwardSpec
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.messages import NavigateCommand
from korvid.ui.widgets.port_forward_screen import ForwardListScreen, PortForwardScreen

from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_SVC_META = ResourceMeta("Service", "services", "", "v1", True, ("svc",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))

_TEST_ALIASES: dict[str, ResourceMeta] = {
    "pods": _PODS_META,
    "services": _SVC_META,
    "svc": _SVC_META,
    "deployments": _DEPLOY_META,
}


class _FakeProc:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.returncode: int | None = None
        self.terminated = False
        # None = no readiness channel; gated tests swap in a fed stream.
        self.stdout: Any = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise RuntimeError("wait() on a live fake proc")
        return self.returncode


def _registry(procs: list[_FakeProc]) -> ForwardRegistry:
    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        procs.append(proc)
        return proc

    return ForwardRegistry(popen=_popen)


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


def _svc(name: str, namespace: str = "default") -> GenericSummary:
    return GenericSummary(name=name, namespace=namespace, kind="Service", created="")


_POD_MANIFEST = {"spec": {"containers": [{"name": "app", "ports": [{"containerPort": 8080}]}]}}


async def _pod_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
    return dict(_POD_MANIFEST)


def make_app(
    pods: list[PodSummary],
    *,
    forwards: ForwardRegistry | None = None,
    extra_data: dict[str, list[Summary]] | None = None,
    audit: AuditLog | None = None,
    get_manifest: Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None = None,
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
        audit=audit,
        get_manifest=get_manifest,
        forwards=forwards,
    )


def _audit_log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.log", context="test-ctx")


def _audit_lines(tmp_path: Path) -> str:
    path = tmp_path / "audit.log"
    return path.read_text() if path.exists() else ""


async def _wait_rows(app: KorvidApp, pilot: Any) -> None:
    """Wait until the resource table is populated (no fixed sleeps — CI runners are slow)."""
    from korvid.ui.widgets.resource_table import ResourceTable

    table = app.query_one(ResourceTable)
    await until(pilot, lambda: table.row_count > 0, label="resource table rows")


# ---------------------------------------------------------------------------
# shift+f dialog
# ---------------------------------------------------------------------------


async def test_forward_dialog_prefills_port_from_manifest() -> None:
    procs: list[_FakeProc] = []
    app = make_app([_pod("api-1")], forwards=_registry(procs), get_manifest=_pod_manifest)
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            assert isinstance(app.screen, PortForwardScreen)
            from textual.widgets import Input

            remote = app.screen.query_one("#pf-remote", Input)
            local = app.screen.query_one("#pf-local", Input)
            assert remote.value == "8080"
            assert local.value == "8080"


async def test_forward_submit_starts_kubectl_and_audits(tmp_path: Path) -> None:
    procs: list[_FakeProc] = []
    app = make_app(
        [_pod("api-1")],
        forwards=_registry(procs),
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1)
            assert procs[0].argv[:2] == ["kubectl", "port-forward"]
            assert "pod/api-1" in procs[0].argv
            assert "8080:8080" in procs[0].argv
            await until(pilot, lambda: "port-forward-start" in _audit_lines(tmp_path))
            assert "port-forward-start" in _audit_lines(tmp_path)


async def test_forward_dialog_custom_local_port() -> None:
    procs: list[_FakeProc] = []
    app = make_app([_pod("api-1")], forwards=_registry(procs), get_manifest=_pod_manifest)
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            from textual.widgets import Input

            app.screen.query_one("#pf-local", Input).value = "9999"
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1)
            assert "9999:8080" in procs[0].argv


async def test_forward_works_for_services() -> None:
    procs: list[_FakeProc] = []

    async def svc_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return {"spec": {"ports": [{"port": 80}]}}

    app = make_app(
        [],
        forwards=_registry(procs),
        extra_data={"services": [_svc("web")]},
        get_manifest=svc_manifest,
    )
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.on_navigate_command(NavigateCommand("services", None))
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1)
            assert "service/web" in procs[0].argv


async def test_forward_rejected_for_unforwardable_kind() -> None:
    procs: list[_FakeProc] = []
    app = make_app(
        [],
        forwards=_registry(procs),
        extra_data={"deployments": [GenericSummary("web", "default", "Deployment", "")]},
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.on_navigate_command(NavigateCommand("deployments", None))
        await _wait_rows(app, pilot)
        await pilot.press("F")
        await pilot.pause()
        assert not isinstance(app.screen, PortForwardScreen)
        assert procs == []


async def test_forward_unavailable_without_registry() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await pilot.press("F")
        await pilot.pause()
        assert not isinstance(app.screen, PortForwardScreen)


async def test_forward_dialog_escape_cancels() -> None:
    procs: list[_FakeProc] = []
    app = make_app([_pod("api-1")], forwards=_registry(procs), get_manifest=_pod_manifest)
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("escape")
            await until(pilot, lambda: not isinstance(app.screen, PortForwardScreen))
            assert procs == []


async def test_forward_dialog_rejects_invalid_port() -> None:
    procs: list[_FakeProc] = []
    app = make_app([_pod("api-1")], forwards=_registry(procs), get_manifest=_pod_manifest)
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            from textual.widgets import Input

            app.screen.query_one("#pf-local", Input).value = "not-a-port"
            await pilot.press("enter")
            await pilot.pause()
            # Screen stays open, nothing spawned.
            assert isinstance(app.screen, PortForwardScreen)
            assert procs == []


# ---------------------------------------------------------------------------
# :pf list screen
# ---------------------------------------------------------------------------


def _forward_rows(app: KorvidApp) -> list[str]:
    from textual.widgets import OptionList

    screen = app.screen
    assert isinstance(screen, ForwardListScreen)
    options = screen.query_one(OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


async def _open_pf(app: KorvidApp, pilot: Any) -> None:
    await pilot.press("colon")
    for ch in "pf":
        await pilot.press(ch)
    await pilot.press("enter")
    await until(pilot, lambda: isinstance(app.screen, ForwardListScreen))


async def test_pf_command_lists_active_forwards() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry)
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await _open_pf(app, pilot)
        rows = _forward_rows(app)
        assert len(rows) == 1
        assert "alive" in rows[0]
        assert "localhost:8080" in rows[0]
        assert "default/pod/api-1:80" in rows[0]


async def test_pf_ctrl_d_stops_forward_and_audits(tmp_path: Path) -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry, audit=_audit_log(tmp_path))
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await _open_pf(app, pilot)
        await pilot.press("ctrl+d")
        await until(pilot, lambda: registry.forwards() == [])
        assert procs[0].terminated
        await until(pilot, lambda: "port-forward-stop" in _audit_lines(tmp_path))
        assert "port-forward-stop" in _audit_lines(tmp_path)


async def test_pf_marks_broken_forward_and_reattaches() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry)
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    procs[0].returncode = 1  # target pod died; kubectl exited
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await _open_pf(app, pilot)
        await until(pilot, lambda: any("broken" in row for row in _forward_rows(app)))
        await pilot.press("r")
        await until(pilot, lambda: len(procs) == 2)
        await until(pilot, lambda: any("alive" in row for row in _forward_rows(app)))
        assert registry.forwards()[0].status == "alive"


async def test_pf_empty_registry_shows_placeholder() -> None:
    app = make_app([_pod("api-1")], forwards=_registry([]))
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await _open_pf(app, pilot)
        rows = _forward_rows(app)
        assert rows == ["No active port-forwards — press shift+f on a pod or service"]


async def test_pf_unavailable_without_registry() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await pilot.press("colon")
        for ch in "pf":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, ForwardListScreen)


def test_pf_in_command_help() -> None:
    from korvid.ui.command import command_help

    assert any(":pf" in cmd for cmd, _ in command_help())


# ---------------------------------------------------------------------------
# Background liveness + session teardown
# ---------------------------------------------------------------------------


async def test_broken_forward_notifies_without_pf_screen() -> None:
    """A forward dying must surface a toast even when :pf is closed."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry)
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    async with app.run_test() as pilot:
        app.notify = _capture  # type: ignore[method-assign]  # test spy
        await pilot.pause()
        procs[0].returncode = 1
        await until(pilot, lambda: any("broken" in n for n in notices), timeout=6.0)
        assert any("localhost:8080" in n for n in notices)


async def test_forwards_torn_down_on_exit() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry)
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
    assert procs[0].terminated
    assert registry.forwards() == []


async def test_teardown_stops_are_audited(tmp_path: Path) -> None:
    """Session teardown must audit each stopped forward before unmount returns."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry, audit=_audit_log(tmp_path))
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=9090, remote_port=90)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
    lines = _audit_lines(tmp_path)
    assert lines.count("port-forward-stop") == 2
    assert "session teardown" in lines


async def test_pf_reattach_blocked_when_target_pod_gone() -> None:
    """A Deployment replacement pod has a new name — re-attach must not retry it."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)

    async def _gone(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        raise ApiStatusError(404, f'pods "{name}" not found')

    app = make_app([_pod("api-1")], forwards=registry, get_manifest=_gone)
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    procs[0].returncode = 1  # target pod died; kubectl exited
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await _open_pf(app, pilot)
        await until(pilot, lambda: any("broken" in row for row in _forward_rows(app)))
        await pilot.press("r")
        await pilot.pause()
        assert len(procs) == 1  # no new kubectl spawned for a vanished pod
        assert registry.forwards()[0].status == "broken"


async def test_pf_reattach_verifies_target_still_exists() -> None:
    """When the target pod still exists, re-attach proceeds after the check."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry, get_manifest=_pod_manifest)
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    procs[0].returncode = 1
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await _open_pf(app, pilot)
        await until(pilot, lambda: any("broken" in row for row in _forward_rows(app)))
        await pilot.press("r")
        await until(pilot, lambda: len(procs) == 2)
        assert registry.forwards()[0].status == "alive"


def test_forward_row_includes_target_kind() -> None:
    """Pods and services can share namespace/name/port — the row must disambiguate."""
    from korvid.core.portforward import ForwardRecord
    from korvid.ui.widgets.port_forward_screen import forward_row

    pod_row = forward_row(
        ForwardRecord(
            id=1,
            spec=ForwardSpec(
                kind="pods", namespace="default", name="api", local_port=80, remote_port=80
            ),
        )
    )
    svc_row = forward_row(
        ForwardRecord(
            id=2,
            spec=ForwardSpec(
                kind="services", namespace="default", name="api", local_port=80, remote_port=80
            ),
        )
    )
    assert "pod/api" in pod_row
    assert "service/api" in svc_row


async def test_pf_reattach_gone_message_is_kind_appropriate() -> None:
    """A deleted Service keeps its name — don't claim its replacement is renamed."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)

    async def _gone(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        raise ApiStatusError(404, f'{kind} "{name}" not found')

    app = make_app([_pod("api-1")], forwards=registry, get_manifest=_gone)
    registry.start(
        ForwardSpec(
            kind="services", namespace="default", name="web", local_port=8080, remote_port=80
        )
    )
    procs[0].returncode = 1
    notices: list[str] = []
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await _open_pf(app, pilot)
        screen = app.screen
        original = screen.notify

        def _capture(message: str, **kwargs: Any) -> Any:
            notices.append(message)
            return original(message, **kwargs)

        screen.notify = _capture  # type: ignore[method-assign]  # test spy
        await until(pilot, lambda: any("broken" in row for row in _forward_rows(app)))
        await pilot.press("r")
        await until(pilot, lambda: len(notices) > 0)
        assert len(procs) == 1
        assert "replacement has a new" not in notices[0]
        assert "no longer exists" in notices[0]


async def test_pf_reattach_follows_the_owning_workload_when_pod_gone(tmp_path: Path) -> None:
    """Issue #38: a Deployment pod's replacement has a new name — re-attach
    retargets the forward at the owning workload so kubectl resolves the
    replacement pod, instead of telling the user to start over."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    pod_gone = False

    async def _manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        if kind == "pods":
            if pod_gone:
                raise ApiStatusError(404, f'pods "{name}" not found')
            return {
                "metadata": {
                    "ownerReferences": [
                        {"kind": "ReplicaSet", "name": "api-6d5f", "controller": True}
                    ]
                },
                "spec": {"containers": [{"name": "app", "ports": [{"containerPort": 8080}]}]},
            }
        if kind == "replicasets":
            return {
                "metadata": {
                    "ownerReferences": [{"kind": "Deployment", "name": "api", "controller": True}]
                }
            }
        raise ApiStatusError(404, f'{kind} "{name}" not found')

    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_manifest,
        audit=_audit_log(tmp_path),
    )
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1, label="forward started")
            record = registry.forwards()[0]
            # The pod dies and its Deployment replaces it under a new name.
            procs[0].returncode = 1
            pod_gone = True
            await _open_pf(app, pilot)
            await until(pilot, lambda: any("broken" in row for row in _forward_rows(app)))
            await pilot.press("r")
            await until(pilot, lambda: len(procs) == 2, label="workload re-attach spawned")
            assert "deployment/api" in procs[1].argv
            await until(pilot, lambda: record.status == "alive", label="replacement confirmed")
            assert any("deployment/api" in row for row in _forward_rows(app))
            # The retargeted start is audited with the workload's full GVR —
            # a deployments entry recorded as core/v1 would name the wrong
            # resource (core/audit.py records group+version for this reason).
            await until(
                pilot,
                lambda: '"outcome": "reattached"' in _audit_lines(tmp_path),
                label="retargeted start audited",
            )
            reattached = next(
                line
                for line in _audit_lines(tmp_path).splitlines()
                if '"outcome": "reattached"' in line
            )
            assert '"kind": "deployments"' in reattached
            assert '"group": "apps"' in reattached


async def test_teardown_audit_failure_does_not_abort_shutdown(tmp_path: Path) -> None:
    """A full disk during a teardown audit must not skip the rest of unmount."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)

    class _FailingAudit(AuditLog):
        def append(self, **kwargs: Any) -> None:
            raise OSError("disk full")

    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        audit=_FailingAudit(tmp_path / "audit.log", context="test-ctx"),
    )
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
    # Shutdown completed despite the audit failure; the forward was stopped.
    assert procs[0].terminated
    assert registry.forwards() == []


async def test_reattach_rearms_broken_notification_immediately() -> None:
    """After re-attach the same forward must be able to notify again on breakage."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry, get_manifest=_pod_manifest)
    record = registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    procs[0].returncode = 1
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        app._broken_forwards.add(record.id)  # background poll already toasted
        await _open_pf(app, pilot)
        await until(pilot, lambda: any("broken" in row for row in _forward_rows(app)))
        await pilot.press("r")
        await until(pilot, lambda: len(procs) == 2)
        # Re-armed right away — not deferred to the next global poll tick.
        assert record.id not in app._broken_forwards


async def test_failed_reattach_marks_breakage_as_already_reported(tmp_path: Path) -> None:
    """A failed re-attach's specific error must not be followed by the poll's
    generic broken toast for the same breakage."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    record = registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    procs[0].stdout.feed(None)
    procs[0].returncode = 1
    registry.refresh()
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    with patch("korvid.ui.app._FORWARD_READY_SECONDS", 0.05):
        async with app.run_test() as pilot:
            app.notify = _capture  # type: ignore[method-assign]  # test spy
            await _wait_rows(app, pilot)
            app._broken_forwards.add(record.id)  # first breakage already toasted
            await _open_pf(app, pilot)
            await until(pilot, lambda: any("broken" in row for row in _forward_rows(app)))
            await pilot.press("r")
            await until(pilot, lambda: len(procs) == 2, label="replacement spawned")
            # The silent replacement fails the re-attach handshake...
            await until(
                pilot,
                lambda: any("did not confirm" in n for n in notices),
                label="re-attach failure toast",
            )
            # ...which must re-mark the breakage as reported (the re-attach
            # re-armed the toast) so the poll doesn't repeat the bad news.
            await until(
                pilot,
                lambda: record.id in app._broken_forwards,
                label="failed re-attach re-marks the breakage",
            )
            assert not any("target gone?" in n for n in notices)  # no generic re-toast
            assert registry.get(record.id) is record  # still listed for another try
            procs[1].stdout.feed(None)  # release the reader thread


async def test_poll_stays_quiet_while_a_confirmation_reports_the_failure(tmp_path: Path) -> None:
    """A startup failure gets its specific error toast only — the liveness
    poll must not precede it with the generic 'target gone?' breakage toast."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            app.notify = _capture  # type: ignore[method-assign]  # test spy
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1)
            record = registry.forwards()[0]
            await until(
                pilot,
                lambda: record.id in app._current_confirmations,
                label="confirmation tracked",
            )
            # kubectl dies silently while the readiness confirmation still waits.
            procs[0].returncode = 1
            app._poll_forwards()  # marks it broken and wakes the waiter
            await until(
                pilot,
                lambda: any("failed to start" in n for n in notices),
                label="specific failed-start toast",
            )
            assert not any("target gone?" in n for n in notices)
            procs[0].stdout.feed(None)  # release the reader thread


async def test_poll_stays_quiet_while_a_launch_is_still_in_flight(tmp_path: Path) -> None:
    """A record published by registry.start() before the launch coroutine
    resumes is still owned by that launch — the poll must not toast its
    breakage generically nor retain it in the armed set."""
    procs: list[_FakeProc] = []
    release = threading.Event()

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        release.wait(2.0)  # hold the spawn until the test lines up the race
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            app.notify = _capture  # type: ignore[method-assign]  # test spy
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            release.set()
            # Busy-wait without yielding: the spawn thread publishes the
            # record, but the launch coroutine cannot resume while this
            # coroutine holds the event loop — the exact window at issue.
            deadline = time.monotonic() + 2.0
            while not registry.forwards() and time.monotonic() < deadline:
                time.sleep(0.005)
            record = registry.forwards()[0]
            assert record.id not in app._current_confirmations  # still launching
            procs[0].returncode = 1  # kubectl died before the coroutine resumed
            app._poll_forwards()
            assert not any("target gone?" in n for n in notices)
            assert record.id not in app._broken_forwards
            await until(
                pilot,
                lambda: any("failed to start" in n for n in notices),
                label="specific failed-start toast",
            )
            assert not any("target gone?" in n for n in notices)
            procs[0].stdout.feed(None)  # release the reader thread


async def test_launch_on_another_port_does_not_defer_a_breakage_toast(tmp_path: Path) -> None:
    """An in-flight launch defers only its own record's generic toast — an
    unrelated established forward's breakage still toasts immediately."""
    procs: list[_FakeProc] = []
    release = threading.Event()

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        if procs:  # gate only the second (in-flight) launch
            release.wait(2.0)
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    established = registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    procs[0].stdout.feed("Forwarding from 127.0.0.1:8080 -> 80\n")
    deadline = time.monotonic() + 2.0
    while established.status != "alive" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert established.status == "alive"
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            app.notify = _capture  # type: ignore[method-assign]  # test spy
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            assert isinstance(app.screen, PortForwardScreen)
            app.screen.query_one("#pf-local", Input).value = "9090"
            await pilot.press("enter")
            await until(pilot, lambda: bool(app._launching_forwards), label="launch in flight")
            # The established forward on the *other* port breaks meanwhile.
            procs[0].returncode = 1
            procs[0].stdout.feed(None)
            app._poll_forwards()
            assert any("target gone?" in n for n in notices)  # not deferred
            release.set()
            await until(pilot, lambda: len(procs) == 2, label="gated launch lands")
    procs[1].stdout.feed(None)


async def test_forward_audit_failure_does_not_crash_app(tmp_path: Path) -> None:
    """A failing audit sink must not kill the app on a normal forward start."""
    procs: list[_FakeProc] = []

    class _FailingAudit(AuditLog):
        def append(self, **kwargs: Any) -> None:
            raise OSError("disk full")

    app = make_app(
        [_pod("api-1")],
        forwards=_registry(procs),
        get_manifest=_pod_manifest,
        audit=_FailingAudit(tmp_path / "audit.log", context="test-ctx"),
    )
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1)
            await pilot.pause()
            assert app.is_running  # audit failure logged, app alive


async def test_failed_reattach_is_audited_with_error_outcome(tmp_path: Path) -> None:
    """A re-attach spawn failure must land in the audit log like a failed start."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        if procs:  # first spawn succeeds, the re-attach spawn fails
            raise OSError("kubectl vanished")
        proc = _FakeProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    procs[0].returncode = 1
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await _open_pf(app, pilot)
        await until(pilot, lambda: any("broken" in row for row in _forward_rows(app)))
        await pilot.press("r")
        await until(
            pilot,
            lambda: "error: kubectl vanished" in _audit_lines(tmp_path),
            label="failed re-attach audited",
        )
        assert "port-forward-start" in _audit_lines(tmp_path)


async def test_forward_dialog_opens_when_manifest_fetch_breaks(tmp_path: Path) -> None:
    """A transport failure during prefill must fall back to empty fields."""
    procs: list[_FakeProc] = []

    async def _boom(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        raise RuntimeError("connection reset")

    app = make_app([_pod("api-1")], forwards=_registry(procs), get_manifest=_boom)
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            from textual.widgets import Input

            assert app.screen.query_one("#pf-remote", Input).value == ""


async def test_reattach_fails_open_on_transport_error() -> None:
    """Only a confirmed 404 blocks re-attach — transport errors fail open."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)

    async def _boom(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        raise RuntimeError("connection reset")

    app = make_app([_pod("api-1")], forwards=registry, get_manifest=_boom)
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    procs[0].returncode = 1
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await _open_pf(app, pilot)
        await until(pilot, lambda: any("broken" in row for row in _forward_rows(app)))
        await pilot.press("r")
        await until(pilot, lambda: len(procs) == 2)
        assert registry.forwards()[0].status == "alive"


async def test_service_forward_rejects_undeclared_remote_port() -> None:
    """kubectl resolves a Service remote port against spec.ports — undeclared fails."""
    procs: list[_FakeProc] = []

    async def svc_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return {"spec": {"ports": [{"port": 80}]}}

    app = make_app(
        [],
        forwards=_registry(procs),
        extra_data={"services": [_svc("web")]},
        get_manifest=svc_manifest,
    )
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.on_navigate_command(NavigateCommand("services", None))
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            from textual.widgets import Input

            app.screen.query_one("#pf-remote", Input).value = "9999"
            await pilot.press("enter")
            await pilot.pause()
            # Screen stays open, nothing spawned — kubectl would reject it anyway.
            assert isinstance(app.screen, PortForwardScreen)
            assert procs == []


async def test_pod_forward_allows_undeclared_remote_port() -> None:
    """Pod declarations stay informational — any remote port is forwardable."""
    procs: list[_FakeProc] = []
    app = make_app([_pod("api-1")], forwards=_registry(procs), get_manifest=_pod_manifest)
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            from textual.widgets import Input

            app.screen.query_one("#pf-remote", Input).value = "9999"
            app.screen.query_one("#pf-local", Input).value = "9999"
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1)
            assert "9999:9999" in procs[0].argv


async def test_dialog_warns_on_privileged_local_port() -> None:
    """A local port below 1024 gets a heads-up warning but is not blocked."""
    procs: list[_FakeProc] = []
    app = make_app([_pod("api-1")], forwards=_registry(procs), get_manifest=_pod_manifest)
    notices: list[str] = []
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            screen = app.screen
            original = screen.notify

            def _capture(message: str, **kwargs: Any) -> Any:
                notices.append(message)
                return original(message, **kwargs)

            screen.notify = _capture  # type: ignore[method-assign]  # test spy
            screen.query_one("#pf-local", Input).value = "443"
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1)
            assert any("privileged" in n for n in notices)
            assert "443:8080" in procs[0].argv


async def test_duplicate_local_port_start_shows_clear_error() -> None:
    """A local-port collision must surface as a clear toast, not a broken toast."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry, get_manifest=_pod_manifest)
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            app.notify = _capture  # type: ignore[method-assign]  # test spy
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")  # prefilled 8080 collides with the live forward
            await until(pilot, lambda: any("already forwarded" in n for n in notices))
            assert len(procs) == 1  # no second kubectl was spawned
            assert app.is_running


async def test_stopping_broken_forward_releases_broken_flag() -> None:
    """Stopping a broken forward must not leak its id in the broken set."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry)
    record = registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    procs[0].returncode = 1
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await until(pilot, lambda: record.id in app._broken_forwards, timeout=6.0)
        await _open_pf(app, pilot)
        await until(pilot, lambda: any("broken" in row for row in _forward_rows(app)))
        await pilot.press("ctrl+d")
        await until(pilot, lambda: registry.forwards() == [])
        assert record.id not in app._broken_forwards


async def test_forward_audit_entries_keep_event_order(tmp_path: Path) -> None:
    """A stalled start audit must not let the stop entry overtake it on disk."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    audit = _audit_log(tmp_path)
    real_append = audit.append
    release_start = threading.Event()

    def _stalling_append(**kwargs: Any) -> None:
        if kwargs.get("action") == "port-forward-start":
            release_start.wait(timeout=5)
        real_append(**kwargs)

    audit.append = _stalling_append  # type: ignore[method-assign]  # test shim
    app = make_app([_pod("api-1")], forwards=registry, get_manifest=_pod_manifest, audit=audit)
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1)
            await _open_pf(app, pilot)
            await pilot.press("ctrl+d")
            await until(pilot, lambda: registry.forwards() == [])
            # Only now may the start entry hit the disk.
            release_start.set()
            await until(pilot, lambda: _audit_lines(tmp_path).count("port-forward") >= 2)
    events = [line for line in _audit_lines(tmp_path).splitlines() if "port-forward" in line]
    assert len(events) == 2
    assert "port-forward-start" in events[0]
    assert "port-forward-stop" in events[1]


async def test_pending_forward_audit_flushed_on_exit(tmp_path: Path) -> None:
    """A stop audited right before quit must still reach the log."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry, audit=_audit_log(tmp_path))
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await _open_pf(app, pilot)
        await pilot.press("ctrl+d")
        # Exit immediately: no wait for the audit write to land.
    lines = _audit_lines(tmp_path)
    assert lines.count("port-forward-stop") == 1


async def test_reattach_port_conflict_shows_clear_error() -> None:
    """Re-attaching onto a port claimed by a live forward must toast clearly."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry, get_manifest=_pod_manifest)
    broken = registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    procs[0].returncode = 1
    registry.refresh()
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-2", local_port=8080, remote_port=80)
    )
    notices: list[str] = []
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        await _open_pf(app, pilot)
        screen = app.screen
        original = screen.notify

        def _capture(message: str, **kwargs: Any) -> Any:
            notices.append(message)
            return original(message, **kwargs)

        screen.notify = _capture  # type: ignore[method-assign]  # test spy
        await until(pilot, lambda: any("broken" in row for row in _forward_rows(app)))
        await pilot.press("r")  # first row is the broken forward
        await until(pilot, lambda: any("already forwarded" in n for n in notices))
        assert broken.status == "broken"
        assert len(procs) == 2  # no doomed kubectl was spawned


async def test_failed_start_reports_error_not_success(tmp_path: Path) -> None:
    """kubectl dying before its ready line is a failed start, not a success."""
    procs: list[_FakeProc] = []

    class _DoomedProc(_FakeProc):
        def __init__(self, argv: list[str]) -> None:
            super().__init__(argv)
            self.returncode = 1
            self.stdout = io.StringIO("error: unable to listen on any of the requested ports\n")

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _DoomedProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")], forwards=registry, get_manifest=_pod_manifest, audit=_audit_log(tmp_path)
    )
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            app.notify = _capture  # type: ignore[method-assign]  # test spy
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(pilot, lambda: any("failed to start" in n for n in notices))
            assert not any(n.startswith("Forwarding") for n in notices)
            assert registry.forwards() == []
            await until(pilot, lambda: "unable to listen" in _audit_lines(tmp_path))


class _GatedStream:
    """File-like stdout whose lines are fed by the test (None ends the stream)."""

    def __init__(self) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()

    def __iter__(self) -> _GatedStream:
        return self

    def __next__(self) -> str:
        line = self._lines.get()
        if line is None:
            raise StopIteration
        return line

    def feed(self, line: str | None) -> None:
        self._lines.put(line)


async def test_stop_during_startup_keeps_audit_order(tmp_path: Path) -> None:
    """Stopping a still-starting forward must not log its stop before its start."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1)
            # kubectl has not confirmed the listener yet — stop it from :pf.
            await _open_pf(app, pilot)
            await pilot.press("ctrl+d")
            await until(pilot, lambda: registry.forwards() == [])
            procs[0].stdout.feed(None)  # the stopped child exits
            await until(
                pilot,
                lambda: "port-forward-stop" in _audit_lines(tmp_path),
                label="stop audited",
            )
            lines = _audit_lines(tmp_path)
            assert "port-forward-start" in lines
            assert lines.index("port-forward-start") < lines.index("port-forward-stop")


async def test_teardown_during_startup_keeps_audit_order(tmp_path: Path) -> None:
    """Exiting while a forward is still starting must not orphan its stop audit."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1)
            # Exit while kubectl has not confirmed the listener yet.
    lines = _audit_lines(tmp_path)
    assert "port-forward-start" in lines
    assert "port-forward-stop" in lines
    assert lines.index("port-forward-start") < lines.index("port-forward-stop")


async def test_quit_during_spawn_still_audits_the_start_first(tmp_path: Path) -> None:
    """Quit while Popen is still off-loop: the start entry must land, before any stop.

    Between registry.start() publishing the record in its thread and the
    launch coroutine resuming on the loop, no confirmation is tracked yet —
    a teardown in that window used to audit the stop with no start at all.
    """
    gate = threading.Event()
    spawn_started = threading.Event()
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        spawn_started.set()
        gate.wait(5.0)  # the launch is mid-spawn while the app shuts down
        proc = _FakeProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(pilot, lambda: spawn_started.is_set(), label="spawn in flight")
            # Release the spawn only once teardown is already waiting on it.
            threading.Timer(0.2, gate.set).start()
    lines = _audit_lines(tmp_path)
    assert "port-forward-start" in lines, "the start never reached the audit log"
    if "port-forward-stop" in lines:
        assert lines.index("port-forward-start") < lines.index("port-forward-stop")


async def test_teardown_during_reattach_window_keeps_audit_order(tmp_path: Path) -> None:
    """Quit after the registry adopts a re-attached replacement but before the
    re-attach coroutine resumes: the replacement's start entry must still
    reach the log before any stop entry."""
    release = threading.Event()
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        if procs:  # gate only the replacement spawn
            release.wait(5.0)
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1)
            record = registry.forwards()[0]
            procs[0].stdout.feed("Forwarding from 127.0.0.1:8080 -> 80\n")
            await until(pilot, lambda: record.status == "alive", label="forward confirmed")
            # The child dies; the poll marks it broken.
            procs[0].returncode = 1
            procs[0].stdout.feed(None)
            app._poll_forwards()
            await until(pilot, lambda: record.status == "broken", label="marked broken")
            await _open_pf(app, pilot)
            await pilot.press("r")  # re-attach blocks in the gated spawn
            release.set()
            # Busy-wait without yielding: the registry adopts the replacement
            # in its thread, but the re-attach coroutine cannot resume while
            # this coroutine holds the event loop — then quit in that window.
            deadline = time.monotonic() + 2.0
            while record.status != "starting" and time.monotonic() < deadline:
                time.sleep(0.005)
            assert record.status == "starting"  # replacement adopted
    lines = _audit_lines(tmp_path)
    assert lines.count("port-forward-start") == 2, "the replacement's start never landed"
    assert "port-forward-stop" in lines
    assert lines.rindex("port-forward-start") < lines.index("port-forward-stop")
    if len(procs) == 2:
        procs[1].stdout.feed(None)  # release the replacement's reader thread


async def test_stale_confirmation_never_reports_the_replacement_as_its_own(
    tmp_path: Path,
) -> None:
    """A timed-out confirmation whose generation was superseded must audit the
    supersession — inferring from the reused record would report the
    replacement's state (here: alive) as the old generation's success."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    record = registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    stale_generation = registry.generation(record.id)
    procs[0].stdout.feed(None)  # EOF: the first child broke
    procs[0].returncode = 1
    assert registry.wait_ready(record.id, timeout=2.0) == "broken"
    assert registry.reattach(record.id) is record
    procs[1].stdout.feed("Forwarding from 127.0.0.1:8080 -> 80\n")
    deadline = time.monotonic() + 2.0
    while record.status != "alive" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert record.status == "alive"  # the replacement confirmed
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    async with app.run_test() as pilot:
        app.notify = _capture  # type: ignore[method-assign]  # test spy
        # The old generation's timed-out confirmation lands only now.
        app._report_failed_forward_start(
            registry, record, "starting", reattached=False, generation=stale_generation
        )
        await until(
            pilot,
            lambda: "superseded by re-attach" in _audit_lines(tmp_path),
            label="supersession audited",
        )
        assert record.status == "alive"  # the replacement was left untouched
        assert not procs[1].terminated
    assert not any(n.startswith("Forwarding localhost") for n in notices)
    procs[1].stdout.feed(None)  # release the replacement's reader thread


async def test_udp_only_service_is_rejected_up_front() -> None:
    """A Service with no TCP ports cannot be forwarded — kubectl is TCP-only."""
    procs: list[_FakeProc] = []

    async def svc_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return {"spec": {"ports": [{"port": 53, "protocol": "UDP"}]}}

    app = make_app(
        [],
        forwards=_registry(procs),
        extra_data={"services": [_svc("dns")]},
        get_manifest=svc_manifest,
    )
    notices: list[str] = []
    original_notify = app.notify

    def _spy(message: str, **kwargs: Any) -> None:
        notices.append(message)
        original_notify(message, **kwargs)

    app.notify = _spy  # type: ignore[method-assign]  # test spy
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.on_navigate_command(NavigateCommand("services", None))
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: any("TCP" in note for note in notices))
            assert not isinstance(app.screen, PortForwardScreen)
            assert procs == []


async def test_stop_during_startup_survives_immediate_exit(tmp_path: Path) -> None:
    """Ctrl-D on a starting forward, then instant quit — both audits must land."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(pilot, lambda: len(procs) == 1)
            await _open_pf(app, pilot)
            await pilot.press("ctrl+d")
            # Exit right away — no waiting for workers to settle.
    lines = _audit_lines(tmp_path)
    assert "port-forward-start" in lines
    assert "port-forward-stop" in lines
    assert lines.index("port-forward-start") < lines.index("port-forward-stop")


async def test_silent_start_times_out_as_failure(tmp_path: Path) -> None:
    """kubectl that never confirms its listener is failed, not guessed ready."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app._FORWARD_READY_SECONDS", 0.05),
    ):
        async with app.run_test() as pilot:
            app.notify = _capture  # type: ignore[method-assign]  # test spy
            await _wait_rows(app, pilot)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            await pilot.press("enter")
            await until(
                pilot,
                lambda: any("failed to start" in n for n in notices),
                label="timeout failure toast",
            )
            assert any("did not confirm" in n for n in notices)
            assert not any(n.startswith("Forwarding") for n in notices)
            assert registry.forwards() == []
            await until(pilot, lambda: "did not confirm" in _audit_lines(tmp_path))
            procs[0].stdout.feed(None)  # release the reader thread


async def test_last_instant_confirmation_wins_over_the_timeout(tmp_path: Path) -> None:
    """A ready line landing after the wait snapshot is a success, not a failure."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    record = registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    async with app.run_test() as pilot:
        app.notify = _capture  # type: ignore[method-assign]  # test spy
        await _wait_rows(app, pilot)
        procs[0].stdout.feed("Forwarding from 127.0.0.1:8080 -> 80\n")
        await until(pilot, lambda: record.status == "alive", label="listener confirmed")
        # The wait snapshot said "starting", but the listener confirmed since:
        # the failure path must yield to the registry's atomic check.
        app._report_failed_forward_start(registry, record, "starting", reattached=False)
        await until(
            pilot,
            lambda: any(n.startswith("Forwarding") for n in notices),
            label="success toast",
        )
        assert not any("failed to start" in n for n in notices)
        assert registry.get(record.id) is record  # never torn down
        assert not procs[0].terminated
        procs[0].stdout.feed(None)  # release the reader thread


async def test_superseded_wait_result_never_fails_the_replacement(tmp_path: Path) -> None:
    """A stale waiter woken mid-re-attach must not tear down the replacement.

    The woken confirmation can resume before the re-attach publishes the
    replacement's confirmation token — the registry's ``superseded`` result
    is what stops it from acting on the replacement's state.
    """
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    record = registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    async with app.run_test() as pilot:
        await _wait_rows(app, pilot)
        # Simulate the stale generation's wake: the re-attach already swapped
        # the process, but its confirmation token is not published yet.
        with patch.object(registry, "wait_ready", return_value="superseded"):
            app._track_confirmation(record)
            await until(
                pilot,
                lambda: "superseded by re-attach" in _audit_lines(tmp_path),
                label="supersession audited",
            )
        assert registry.get(record.id) is record  # replacement left untouched
        assert record.status == "starting"
        assert not procs[0].terminated
        procs[0].stdout.feed(None)  # release the reader thread


async def test_superseded_confirmation_never_reports_success(tmp_path: Path) -> None:
    """A confirmation replaced by a re-attach must not claim the new process."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    async with app.run_test() as pilot:
        app.notify = _capture  # type: ignore[method-assign]  # test spy
        await _wait_rows(app, pilot)
        record = registry.start(
            ForwardSpec(
                kind="pods", namespace="default", name="api-1", local_port=18080, remote_port=80
            )
        )
        # Simulate a re-attach having installed the replacement's own
        # confirmation under the same id while ours was still waiting.
        replacement = app.run_worker(asyncio.sleep(0))
        stale = app.run_worker(app._confirm_forward(record))
        app._confirming_forwards[record.id] = [stale, replacement]
        app._current_confirmations[record.id] = replacement
        procs[0].stdout.feed("Forwarding from 127.0.0.1:18080 -> 80\n")
        await stale.wait()
        # The observed 'alive' belongs to the replacement generation — the
        # stale confirmation must not toast success nor stop the forward.
        assert not any(n.startswith("Forwarding") for n in notices)
        assert registry.get(record.id) is record
        # ...and it removes only its own tracking entry on the way out.
        assert app._confirming_forwards.get(record.id) == [replacement]
        await until(pilot, lambda: "superseded by re-attach" in _audit_lines(tmp_path))
        procs[0].stdout.feed(None)  # release the reader thread


async def test_finished_replacement_does_not_promote_a_stale_confirmation(
    tmp_path: Path,
) -> None:
    """A superseded confirmation stays superseded after the current one exits.

    The current generation is tracked by an explicit token, not by position
    in the pending list — otherwise a replacement finishing (and removing
    itself) before the stale worker resumed would wrongly promote the stale
    worker back to "current" and let it claim the replacement's result.
    """
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    app = make_app(
        [_pod("api-1")],
        forwards=registry,
        get_manifest=_pod_manifest,
        audit=_audit_log(tmp_path),
    )
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    async with app.run_test() as pilot:
        app.notify = _capture  # type: ignore[method-assign]  # test spy
        await _wait_rows(app, pilot)
        record = registry.start(
            ForwardSpec(
                kind="pods", namespace="default", name="api-1", local_port=18080, remote_port=80
            )
        )
        # The replacement confirmation already finished and cleaned up its
        # token — only the superseded worker is still pending.
        stale = app.run_worker(app._confirm_forward(record))
        app._confirming_forwards[record.id] = [stale]
        app._current_confirmations.pop(record.id, None)
        procs[0].stdout.feed("Forwarding from 127.0.0.1:18080 -> 80\n")
        await stale.wait()
        # Being last in the pending list must not make the stale worker
        # "current" again — it may not toast the replacement's success.
        assert not any(n.startswith("Forwarding") for n in notices)
        await until(pilot, lambda: "superseded by re-attach" in _audit_lines(tmp_path))
        procs[0].stdout.feed(None)  # release the reader thread
