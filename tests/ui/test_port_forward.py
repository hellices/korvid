"""Tests for port-forward UI flows (issue #38): shift+f dialog, :pf list."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.portforward import ForwardRegistry, ForwardSpec
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
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


# ---------------------------------------------------------------------------
# shift+f dialog
# ---------------------------------------------------------------------------


async def test_forward_dialog_prefills_port_from_manifest() -> None:
    procs: list[_FakeProc] = []
    app = make_app([_pod("api-1")], forwards=_registry(procs), get_manifest=_pod_manifest)
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
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
            await pilot.pause(0.1)
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
            await pilot.pause(0.1)
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
            await pilot.pause(0.1)
            await app.on_navigate_command(NavigateCommand("services", None))
            await pilot.pause(0.1)
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
        await pilot.pause(0.1)
        await app.on_navigate_command(NavigateCommand("deployments", None))
        await pilot.pause(0.1)
        await pilot.press("F")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, PortForwardScreen)
        assert procs == []


async def test_forward_unavailable_without_registry() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("F")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, PortForwardScreen)


async def test_forward_dialog_escape_cancels() -> None:
    procs: list[_FakeProc] = []
    app = make_app([_pod("api-1")], forwards=_registry(procs), get_manifest=_pod_manifest)
    with patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
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
            await pilot.pause(0.1)
            await pilot.press("F")
            await until(pilot, lambda: isinstance(app.screen, PortForwardScreen))
            from textual.widgets import Input

            app.screen.query_one("#pf-local", Input).value = "not-a-port"
            await pilot.press("enter")
            await pilot.pause(0.1)
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
        await pilot.pause(0.1)
        await _open_pf(app, pilot)
        rows = _forward_rows(app)
        assert len(rows) == 1
        assert "alive" in rows[0]
        assert "localhost:8080" in rows[0]
        assert "default/api-1:80" in rows[0]


async def test_pf_ctrl_d_stops_forward_and_audits(tmp_path: Path) -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    app = make_app([_pod("api-1")], forwards=registry, audit=_audit_log(tmp_path))
    registry.start(
        ForwardSpec(kind="pods", namespace="default", name="api-1", local_port=8080, remote_port=80)
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
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
        await pilot.pause(0.1)
        await _open_pf(app, pilot)
        await until(pilot, lambda: any("broken" in row for row in _forward_rows(app)))
        await pilot.press("r")
        await until(pilot, lambda: len(procs) == 2)
        await until(pilot, lambda: any("alive" in row for row in _forward_rows(app)))
        assert registry.forwards()[0].status == "alive"


async def test_pf_empty_registry_shows_placeholder() -> None:
    app = make_app([_pod("api-1")], forwards=_registry([]))
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _open_pf(app, pilot)
        rows = _forward_rows(app)
        assert rows == ["No active port-forwards — press shift+f on a pod or service"]


async def test_pf_unavailable_without_registry() -> None:
    app = make_app([_pod("api-1")])
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("colon")
        for ch in "pf":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, ForwardListScreen)


def test_pf_in_command_help() -> None:
    from korvid.ui.command import command_help

    assert any(":pf" in cmd for cmd, _ in command_help())
