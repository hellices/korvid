from collections.abc import Callable
from typing import Any, cast

import pytest

from korvid.ui.deployment_outcome_actions import DeploymentOutcomePodActions
from korvid.ui.ui_surface import UiSurface
from korvid.ui.workspace_controller import ContextGuard, WorkspaceController


class _Ui:
    pass


class _Context:
    def crossed(self, epoch: int) -> bool:
        return False


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
