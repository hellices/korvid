from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
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
        intent=DeploymentScaleIntent(target=target, replicas=3),
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


async def test_screen_lists_bounded_pod_evidence_without_markup() -> None:
    app = _HostApp()
    screen = DeploymentOutcomeScreen(_snapshot(pods=(_pod(),)))

    async with app.run_test():
        await app.push_screen(screen)
        table = screen.query_one("#deployment-outcome-pods", DataTable)

        assert table.row_count == 1
        assert "registry [unavailable]" in str(table.get_row_at(0)[4])


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
        await pilot.pause()

        status = str(screen.query_one("#deployment-outcome-hint", Static).render())
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
