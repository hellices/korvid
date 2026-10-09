from collections.abc import Callable
from typing import Any, cast

import pytest

from korvid.ui.deployment_outcome_actions import DeploymentOutcomePodActions
from korvid.ui.ui_surface import UiSurface
from korvid.ui.workspace_controller import ContextGuard, WorkspaceController


class _Ui:
    def notify(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Context:
    def __init__(self) -> None:
        self.changed = False

    def crossed(self, epoch: int) -> bool:
        return self.changed


class _Workspace:
    async def jump_to_object(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.mark.parametrize("verb", ["describe", "logs"])
async def test_pod_reads_use_explicit_outcome_identity(verb: str) -> None:
    calls: list[tuple[str, ...]] = []

    async def target_uid(kind: str, namespace: str | None, name: str) -> str:
        return "pod-uid"

    async def describe(namespace: str, name: str, uid: str) -> None:
        calls.append(("describe", namespace, name, uid))

    async def logs(namespace: str, name: str) -> None:
        calls.append(("logs", namespace, name))

    actions = DeploymentOutcomePodActions(
        ui=cast(UiSurface, _Ui()),
        target_uid=target_uid,
        context=cast(ContextGuard, _Context()),
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
        else ("logs", "workloads", "tracked-pod")
    )
    assert calls == [expected]


async def test_context_change_during_uid_lookup_blocks_pod_action() -> None:
    context = _Context()
    jumps: list[str] = []

    async def target_uid(kind: str, namespace: str | None, name: str) -> str:
        context.changed = True
        return "pod-uid"

    class Workspace(_Workspace):
        async def jump_to_object(self, *args: Any, **kwargs: Any) -> None:
            jumps.append("jumped")

    async def describe(namespace: str, name: str, uid: str) -> None:
        raise AssertionError("describe must not run")

    async def logs(namespace: str, name: str) -> None:
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
