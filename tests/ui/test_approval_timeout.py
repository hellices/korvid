"""The approval timeout is injectable; production keeps today's default."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from korvid.ui.agent_ui_controller import APPROVAL_TIMEOUT as AGENT_APPROVAL_TIMEOUT
from korvid.ui.app import AppUIBridge
from korvid.ui.proposal_controller import APPROVAL_TIMEOUT as PROPOSAL_APPROVAL_TIMEOUT
from korvid.ui.widgets.confirm_screen import ConfirmScreen

from .agent_write_support import Recorder, _expand_panel, make_app
from .waits import until


def test_the_default_reproduces_the_shipped_approval_timeout(tmp_path: Path) -> None:
    app = make_app(Recorder(), tmp_path / "audit.jsonl")
    assert app._agent_ui._approval_timeout == AGENT_APPROVAL_TIMEOUT
    assert app._proposals._approval_timeout == PROPOSAL_APPROVAL_TIMEOUT


def test_an_injected_timeout_replaces_the_default(tmp_path: Path) -> None:
    app = make_app(Recorder(), tmp_path / "audit.jsonl", approval_timeout_seconds=0.25)
    assert app._agent_ui._approval_timeout == pytest.approx(0.25)
    assert app._proposals._approval_timeout == pytest.approx(0.25)


def test_a_non_positive_timeout_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="approval_timeout_seconds must be finite and positive"):
        make_app(Recorder(), tmp_path / "audit.jsonl", approval_timeout_seconds=0.0)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf")])
def test_a_non_finite_timeout_is_rejected(tmp_path: Path, timeout: float) -> None:
    with pytest.raises(ValueError, match="approval_timeout_seconds must be finite and positive"):
        make_app(Recorder(), tmp_path / "audit.jsonl", approval_timeout_seconds=timeout)


async def test_an_injected_short_timeout_expires_an_agent_write(tmp_path: Path) -> None:
    """A short injected window should expire an unanswered write request."""
    recorder = Recorder()
    app = make_app(recorder, tmp_path / "audit.jsonl", approval_timeout_seconds=1.0)
    async with app.run_test() as pilot:
        _expand_panel(app)
        task = asyncio.ensure_future(
            AppUIBridge(app).agent_request_write(
                "scale", "deployments", "web", namespace="default", replicas=4
            )
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="agent approval dialog opened",
        )
        await until(pilot, task.done, timeout=10.0, label="approval request expired")
        result = await task
        assert "expired" in result
        assert recorder.calls == []
