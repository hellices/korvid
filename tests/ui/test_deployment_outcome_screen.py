from __future__ import annotations

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import DataTable, Static

from korvid.core.deployment_outcome import (
    DeploymentOperationTarget,
    DeploymentOutcome,
    DeploymentOutcomePhase,
    DeploymentPodEvidence,
    DeploymentScaleIntent,
)
from korvid.ui.deployment_outcome_controller import DeploymentTrackerSnapshot
from korvid.ui.widgets.deployment_outcome_screen import DeploymentOutcomeScreen
from tests.ui.waits import until


class _HostApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: object = "unset"

    def compose(self) -> ComposeResult:
        yield Static("host")


class _NothingMounted(DeploymentOutcomeScreen):
    async def mount_composed_widgets(self, widgets: list[Widget]) -> None:
        """Mount nothing, as `Widget.mount` does for a screen being pruned."""


def _snapshot(
    *,
    phase: DeploymentOutcomePhase = DeploymentOutcomePhase.OBSERVING,
    pods: tuple[DeploymentPodEvidence, ...] = (),
    summary: str = "Waiting for replicas [not markup]",
) -> DeploymentTrackerSnapshot:
    target = DeploymentOperationTarget(
        epoch=4,
        cluster_id="cluster-a",
        namespace="default",
        name="web",
        uid="deploy-uid",
    )
    return DeploymentTrackerSnapshot(
        tracker_id="tracker-1",
        intent=DeploymentScaleIntent(target=target, replicas=3, generation=2),
        accepted_at="2026-10-09T12:00:00Z",
        outcome=DeploymentOutcome(
            phase=phase,
            summary=summary,
            evidence=("generation 2/3", "ready 1/3"),
            pods=pods,
        ),
        attempts=2,
    )


def _pod(*, uid: str = "pod-uid") -> DeploymentPodEvidence:
    return DeploymentPodEvidence(
        namespace="default",
        name="web-new-1",
        uid=uid,
        phase="Pending",
        reason="ImagePullBackOff",
        message="registry [unavailable]",
    )


async def test_screen_keeps_api_acceptance_separate_from_convergence() -> None:
    app = _HostApp()
    screen = DeploymentOutcomeScreen(_snapshot())

    async with app.run_test():
        await app.push_screen(screen)
        status = str(screen.query_one("#deployment-outcome-status", Static).render())
        summary = str(screen.query_one("#deployment-outcome-summary", Static).render())

        assert "API request accepted" in status
        assert "Deployment convergence: observing" in status
        assert "Waiting for replicas [not markup]" in summary


async def test_screen_target_label_includes_captured_cluster_identity() -> None:
    app = _HostApp()
    screen = DeploymentOutcomeScreen(_snapshot())

    async with app.run_test():
        await app.push_screen(screen)
        title = str(screen.query_one("#deployment-outcome-title", Static).render())

        assert "cluster-a" in title
        assert "default/web" in title


async def test_screen_lists_bounded_pod_evidence_without_markup() -> None:
    app = _HostApp()
    screen = DeploymentOutcomeScreen(_snapshot(pods=(_pod(),)))

    async with app.run_test():
        await app.push_screen(screen)
        table = screen.query_one("#deployment-outcome-pods", DataTable)

        assert table.row_count == 1
        message = table.get_row_at(0)[4]
        assert isinstance(message, Text)
        assert message.plain == "registry [unavailable]"
        assert message.spans == []


async def test_enter_returns_uid_bearing_pod_navigation() -> None:
    app = _HostApp()
    screen = DeploymentOutcomeScreen(_snapshot(pods=(_pod(),)))

    async with app.run_test() as pilot:
        await app.push_screen(screen, lambda result: setattr(app, "result", result))
        await pilot.press("enter")
        await until(pilot, lambda: app.result != "unset", label="pod navigation result")

    assert app.result == (
        "goto",
        "tracker-1",
        "default",
        "web-new-1",
        "pod-uid",
    )


async def test_pod_action_is_refused_without_uid() -> None:
    app = _HostApp()
    screen = DeploymentOutcomeScreen(_snapshot(pods=(_pod(uid=""),)))

    async with app.run_test() as pilot:
        await app.push_screen(screen, lambda result: setattr(app, "result", result))
        await pilot.press("l")
        hint = screen.query_one("#deployment-outcome-hint", Static)
        await until(
            pilot, lambda: "identity" in str(hint.render()).lower(), label="identity warning"
        )

        status = str(hint.render())
        assert app.result == "unset"
        assert "identity" in status.lower()


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("x", ("stop", "tracker-1", "", "", "")),
        ("r", ("refresh", "tracker-1", "", "", "")),
    ],
)
async def test_stop_and_refresh_return_typed_actions(
    key: str,
    expected: tuple[str, str, str, str, str],
) -> None:
    app = _HostApp()
    screen = DeploymentOutcomeScreen(_snapshot())
    async with app.run_test() as pilot:
        await app.push_screen(screen, lambda result: setattr(app, "result", result))
        await pilot.press(key)
        await until(pilot, lambda: app.result != "unset", label=f"{key} result")
    assert app.result == expected


async def test_screen_whose_composed_widgets_never_mount_does_not_raise() -> None:
    """The controller pushes this screen as soon as a scale is accepted, so it
    can arrive while the app shuts down (#455). Shutdown prunes the screen
    before it composes, and `Widget.mount` then mounts nothing for it, but
    Textual still sends `Mount`."""
    app = _HostApp()
    screen = _NothingMounted(_snapshot(pods=(_pod(),)))

    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()

        assert not screen.query(DataTable)
