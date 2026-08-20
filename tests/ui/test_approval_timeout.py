"""The approval timeout is injectable; production keeps today's default."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

import korvid.__main__ as composition_root
from korvid.ui.app import _APPROVAL_TIMEOUT
from korvid.ui.widgets.confirm_screen import ConfirmScreen

from .agent_write_support import Recorder, _expand_panel, make_app
from .waits import until


def test_the_default_reproduces_the_shipped_approval_timeout(tmp_path: Path) -> None:
    app = make_app(Recorder(), tmp_path / "audit.jsonl")
    assert app._approval_timeout == _APPROVAL_TIMEOUT


def test_an_injected_timeout_replaces_the_default(tmp_path: Path) -> None:
    app = make_app(Recorder(), tmp_path / "audit.jsonl", approval_timeout_seconds=0.25)
    assert app._approval_timeout == pytest.approx(0.25)


def test_a_non_positive_timeout_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="approval_timeout_seconds must be positive"):
        make_app(Recorder(), tmp_path / "audit.jsonl", approval_timeout_seconds=0.0)


def test_production_wiring_passes_no_override() -> None:
    """The composition root must never shorten the shipped approval window."""
    assert "approval_timeout_seconds" not in inspect.getsource(composition_root)


async def test_an_injected_short_timeout_expires_an_agent_write(tmp_path: Path) -> None:
    """A short injected window should expire an unanswered write request."""
    recorder = Recorder()
    app = make_app(recorder, tmp_path / "audit.jsonl", approval_timeout_seconds=1.0)
    async with app.run_test() as pilot:
        _expand_panel(app)
        task = asyncio.ensure_future(
            app.agent_request_write("scale", "deployments", "web", namespace="default", replicas=4)
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
