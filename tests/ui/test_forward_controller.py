"""Direct tests for `ForwardController.open_dialog` (issue #187 / Deep Task 9).

The controller owns the whole shift+f journey now: the forwardable-kind
check, the `:ctx` read gate, the "no registry / no kubectl" refusals, the
Service port resolution, the dialog, and the epoch revalidation that
cancels an approval left open across a context switch. None of it needs a
running app — the Textual surface, the view and the write perimeter all
arrive as injected interfaces.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import patch

from korvid.core.portforward import ForwardRegistry
from korvid.k8s.discovery import ResourceMeta
from korvid.ui.forward_controller import ForwardController
from korvid.ui.widgets.port_forward_screen import PortForwardScreen
from korvid.ui.write_gate import WriteGate

from .test_write_coordinator import FakeUi, FakeView

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_SVC_META = ResourceMeta("Service", "services", "", "v1", True, ("svc",))

_ALIASES = {"pods": _PODS_META, "services": _SVC_META}

_SERVICE_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Service",
    "metadata": {"name": "api", "namespace": "default"},
    "spec": {"ports": [{"port": 80, "protocol": "TCP"}]},
}

_UDP_SERVICE_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Service",
    "metadata": {"name": "api", "namespace": "default"},
    "spec": {"ports": [{"port": 53, "protocol": "UDP"}]},
}


class StubGate(WriteGate):
    """The three perimeter reads the dialog flow makes, and nothing else.

    Every mutation entry point raises: opening a forward performs no cluster
    write, so an edit that reaches for one fails loudly here.
    """

    def __init__(self) -> None:
        self.value = 0
        self.is_switching = False
        self.reads = True

    async def confirm(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the forward dialog performs no write")

    async def confirm_interactive(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the forward dialog performs no write")

    def context_intact(self, *args: Any, **kwargs: Any) -> bool:
        raise AssertionError("the forward dialog performs no write")

    async def permitted(self, *args: Any, **kwargs: Any) -> bool:
        raise AssertionError("the forward dialog performs no write")

    def run(self, *args: Any, **kwargs: Any) -> Coroutine[Any, Any, str]:
        raise AssertionError("the forward dialog performs no write")

    def reserve_write(self) -> Callable[[], None]:
        raise AssertionError("the forward dialog performs no write")

    def audit_configured(self) -> bool:
        return True

    def epoch(self) -> int:
        return self.value

    def reads_allowed(self) -> bool:
        return self.reads

    def switching(self) -> bool:
        return self.is_switching


class Harness:
    """A controller over fake Textual/view/perimeter surfaces."""

    def __init__(
        self,
        *,
        kind: str = "pods",
        selected: tuple[str | None, str | None] = ("default", "api"),
        registry: ForwardRegistry | None = None,
        manifest: dict[str, Any] | None = None,
        manifest_error: BaseException | None = None,
    ) -> None:
        self.ui = FakeUi()
        self.view = FakeView(kind=kind, selected=selected, aliases=dict(_ALIASES))
        self.gate = StubGate()
        self.registry = registry
        self.started: list[tuple[str, str, str, int, int, int]] = []
        self._manifest = manifest
        self._manifest_error = manifest_error
        self.controller = ForwardController(
            gate=self.gate,
            ui=self.ui,
            view=self.view,
            forwards=lambda: self.registry,
            audit=lambda: None,
            get_manifest=lambda: self._get_manifest,
        )
        # Spawning kubectl has its own tests; what matters here is the
        # dialog's decision to launch and the target it launches for.
        self.controller.start = self._record_start  # type: ignore[method-assign]

    async def _get_manifest(self, kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        if self._manifest_error is not None:
            raise self._manifest_error
        return self._manifest or {}

    async def _record_start(
        self,
        kind: str,
        namespace: str,
        name: str,
        *,
        local_port: int,
        remote_port: int,
        epoch: int,
    ) -> None:
        self.started.append((kind, namespace, name, local_port, remote_port, epoch))

    def dialog(self) -> PortForwardScreen:
        screen, _callback = self.ui.screens[-1]
        assert isinstance(screen, PortForwardScreen)
        return screen

    def answer(self, result: tuple[int, int] | None) -> None:
        _screen, callback = self.ui.screens[-1]
        assert callback is not None
        callback(result)


def _harness(**kwargs: Any) -> Harness:
    return Harness(**kwargs)


def _with_kubectl() -> Any:
    return patch("shutil.which", return_value="/usr/bin/kubectl")


async def test_open_dialog_rejects_unforwardable_kind() -> None:
    harness = _harness(kind="deployments")
    with _with_kubectl():
        await harness.controller.open_dialog()
    assert harness.ui.screens == []
    assert "Port-forward is only available for pods and services" in harness.ui.messages()


async def test_open_dialog_refuses_while_a_context_switch_is_in_flight() -> None:
    harness = _harness(registry=ForwardRegistry())
    harness.gate.reads = False
    with _with_kubectl():
        await harness.controller.open_dialog()
    assert harness.ui.screens == []


async def test_open_dialog_reports_a_missing_registry() -> None:
    harness = _harness(registry=None)
    with _with_kubectl():
        await harness.controller.open_dialog()
    assert harness.ui.screens == []
    assert "Port-forward unavailable in this build" in harness.ui.messages()


async def test_open_dialog_reports_missing_kubectl() -> None:
    harness = _harness(registry=ForwardRegistry())
    with patch("shutil.which", return_value=None):
        await harness.controller.open_dialog()
    assert harness.ui.screens == []
    assert any("kubectl not found on PATH" in message for message in harness.ui.messages())


async def test_open_dialog_rejects_a_service_without_tcp_ports() -> None:
    harness = _harness(
        kind="services",
        registry=ForwardRegistry(),
        manifest=_UDP_SERVICE_MANIFEST,
    )
    with _with_kubectl():
        await harness.controller.open_dialog()
    assert harness.ui.screens == []
    assert any("kubectl port-forward is TCP-only" in message for message in harness.ui.messages())


async def test_open_dialog_prefills_service_ports_and_launches_on_accept() -> None:
    harness = _harness(kind="services", registry=ForwardRegistry(), manifest=_SERVICE_MANIFEST)
    with _with_kubectl():
        await harness.controller.open_dialog()
    dialog = harness.dialog()
    assert dialog._target == "services/default/api"
    assert dialog._remote_ports == [80]
    harness.answer((8080, 80))
    await harness.ui.settle()
    assert harness.started == [("services", "default", "api", 8080, 80, 0)]


async def test_open_dialog_still_opens_when_the_manifest_fetch_fails() -> None:
    """A failed prefill is a lost convenience, not a refusal: kubectl has
    the final say on whether the port exists."""
    harness = _harness(
        kind="services",
        registry=ForwardRegistry(),
        manifest_error=RuntimeError("boom"),
    )
    with _with_kubectl():
        await harness.controller.open_dialog()
    assert harness.dialog()._remote_ports == []


async def test_open_dialog_cancels_when_the_context_changed_while_it_was_open() -> None:
    harness = _harness(registry=ForwardRegistry())
    with _with_kubectl():
        await harness.controller.open_dialog()
    harness.gate.value += 1  # a `:ctx` switch completed behind the dialog
    harness.answer((8080, 80))
    await harness.ui.settle()
    assert harness.started == []
    assert any("the kube context" in message for message in harness.ui.messages())


async def test_open_dialog_does_nothing_without_a_selection() -> None:
    harness = _harness(registry=ForwardRegistry(), selected=(None, None))
    with _with_kubectl():
        await harness.controller.open_dialog()
    assert harness.ui.screens == []


async def test_dismissed_dialog_launches_nothing() -> None:
    harness = _harness(registry=ForwardRegistry())
    with _with_kubectl():
        await harness.controller.open_dialog()
    harness.answer(None)
    await asyncio.sleep(0)
    assert harness.started == []


async def test_open_dialog_restricts_remote_ports_for_services_only() -> None:
    service = _harness(kind="services", registry=ForwardRegistry(), manifest=_SERVICE_MANIFEST)
    pod = _harness(registry=ForwardRegistry(), manifest={"spec": {"containers": []}})
    with _with_kubectl():
        await service.controller.open_dialog()
        await pod.controller.open_dialog()
    assert service.dialog()._restrict_remote is True
    assert pod.dialog()._restrict_remote is False
