import asyncio
from collections.abc import Callable
from typing import Any, cast

import pytest

from korvid.k8s.errors import ApiStatusError
from korvid.ui.deployment_outcome_actions import DeploymentOutcomePodActions
from korvid.ui.hints import EventsFetcher
from korvid.ui.ui_surface import UiSurface
from korvid.ui.workspace_controller import ContextGuard, WorkspaceController


class _Ui:
    def __init__(self) -> None:
        self.notifications: list[str] = []
        self.screens: list[object] = []

    def notify(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.notifications.append(message)

    async def push_screen(self, screen: object) -> None:
        self.screens.append(screen)


class _Context:
    def __init__(self) -> None:
        self.changed = False
        self.epochs: list[int] = []

    def crossed(self, epoch: int) -> bool:
        self.epochs.append(epoch)
        return self.changed


class _Workspace:
    def __init__(self) -> None:
        self.nav_lock = asyncio.Lock()

    async def jump_to_object(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.mark.parametrize("verb", ["describe", "logs"])
async def test_pod_reads_use_explicit_outcome_identity(verb: str) -> None:
    calls: list[tuple[str, ...]] = []
    uid_calls: list[tuple[str, str | None, str]] = []
    context = _Context()

    async def target_uid(kind: str, namespace: str | None, name: str) -> str:
        uid_calls.append((kind, namespace, name))
        return "pod-uid"

    async def describe(namespace: str, name: str, uid: str) -> None:
        calls.append(("describe", namespace, name, uid))

    async def logs(namespace: str, name: str, uid: str) -> None:
        calls.append(("logs", namespace, name, uid))

    actions = DeploymentOutcomePodActions(
        ui=cast(UiSurface, _Ui()),
        target_uid=target_uid,
        context=cast(ContextGuard, context),
        workspace=cast(
            "Callable[[], WorkspaceController]",
            _Workspace,
        ),
        describe=describe,
        logs=logs,
        events=lambda: None,
    )

    await actions(verb, 4, "workloads", "tracked-pod", "pod-uid")

    expected = (
        ("describe", "workloads", "tracked-pod", "pod-uid")
        if verb == "describe"
        else ("logs", "workloads", "tracked-pod", "pod-uid")
    )
    assert calls == [expected]
    assert uid_calls == [
        ("pods", "workloads", "tracked-pod"),
        ("pods", "workloads", "tracked-pod"),
    ]
    assert context.epochs
    assert set(context.epochs) == {4}


async def test_pod_read_is_serialized_with_context_switch() -> None:
    workspace = _Workspace()

    async def target_uid(kind: str, namespace: str | None, name: str) -> str:
        return "pod-uid"

    async def describe(namespace: str, name: str, uid: str) -> None:
        raise AssertionError("describe must not run")

    async def logs(namespace: str, name: str, uid: str) -> None:
        assert workspace.nav_lock.locked()

    actions = DeploymentOutcomePodActions(
        ui=cast(UiSurface, _Ui()),
        target_uid=target_uid,
        context=cast(ContextGuard, _Context()),
        workspace=lambda: cast(WorkspaceController, workspace),
        describe=describe,
        logs=logs,
        events=lambda: None,
    )

    await actions("logs", 4, "workloads", "tracked-pod", "pod-uid")

    assert workspace.nav_lock.locked() is False


async def test_deleted_evidence_pod_reports_identity_change() -> None:
    ui = _Ui()
    context = _Context()
    uid_calls: list[tuple[str, str | None, str]] = []

    async def target_uid(kind: str, namespace: str | None, name: str) -> str:
        uid_calls.append((kind, namespace, name))
        raise ApiStatusError(404, "Not Found")

    async def describe(namespace: str, name: str, uid: str) -> None:
        raise AssertionError("describe must not run")

    async def logs(namespace: str, name: str, uid: str) -> None:
        raise AssertionError("logs must not run")

    actions = DeploymentOutcomePodActions(
        ui=cast(UiSurface, ui),
        target_uid=target_uid,
        context=cast(ContextGuard, context),
        workspace=cast("Callable[[], WorkspaceController]", _Workspace),
        describe=describe,
        logs=logs,
        events=lambda: None,
    )

    await actions("describe", 4, "workloads", "deleted-pod", "pod-uid")

    assert ui.notifications == ["Pod identity changed; refresh the Deployment outcome"]
    assert uid_calls == [("pods", "workloads", "deleted-pod")]
    assert context.epochs
    assert set(context.epochs) == {4}


async def test_context_change_during_event_fetch_discards_rows() -> None:
    context = _Context()
    ui = _Ui()
    uid_calls: list[tuple[str, str | None, str]] = []
    fetch_calls: list[tuple[str, str, str | None]] = []

    async def target_uid(kind: str, namespace: str | None, name: str) -> str:
        uid_calls.append((kind, namespace, name))
        return "pod-uid"

    class Events:
        async def fetch(
            self, namespace: str, name: str, *, uid: str | None = None
        ) -> list[dict[str, Any]]:
            fetch_calls.append((namespace, name, uid))
            context.changed = True
            return [{"reason": "Scheduled", "message": "assigned"}]

    async def describe(namespace: str, name: str, uid: str) -> None:
        raise AssertionError("describe must not run")

    async def logs(namespace: str, name: str, uid: str) -> None:
        raise AssertionError("logs must not run")

    actions = DeploymentOutcomePodActions(
        ui=cast(UiSurface, ui),
        target_uid=target_uid,
        context=cast(ContextGuard, context),
        workspace=cast("Callable[[], WorkspaceController]", _Workspace),
        describe=describe,
        logs=logs,
        events=lambda: cast(EventsFetcher, Events()),
    )

    await actions("events", 4, "workloads", "tracked-pod", "pod-uid")

    assert ui.screens == []
    assert uid_calls == [
        ("pods", "workloads", "tracked-pod"),
        ("pods", "workloads", "tracked-pod"),
    ]
    assert fetch_calls == [("workloads", "tracked-pod", "pod-uid")]
    assert context.epochs
    assert set(context.epochs) == {4}


async def test_context_change_during_uid_lookup_blocks_pod_action() -> None:
    context = _Context()
    jumps: list[str] = []
    uid_calls: list[tuple[str, str | None, str]] = []

    async def target_uid(kind: str, namespace: str | None, name: str) -> str:
        uid_calls.append((kind, namespace, name))
        context.changed = True
        return "pod-uid"

    class Workspace(_Workspace):
        async def jump_to_object(self, *args: Any, **kwargs: Any) -> None:
            jumps.append("jumped")

    async def describe(namespace: str, name: str, uid: str) -> None:
        raise AssertionError("describe must not run")

    async def logs(namespace: str, name: str, uid: str) -> None:
        raise AssertionError("logs must not run")

    actions = DeploymentOutcomePodActions(
        ui=cast(UiSurface, _Ui()),
        target_uid=target_uid,
        context=cast(ContextGuard, context),
        workspace=cast("Callable[[], WorkspaceController]", Workspace),
        describe=describe,
        logs=logs,
        events=lambda: None,
    )

    await actions("describe", 4, "workloads", "tracked-pod", "pod-uid")

    assert jumps == []
    assert uid_calls == [("pods", "workloads", "tracked-pod")]
    assert context.epochs
    assert set(context.epochs) == {4}
