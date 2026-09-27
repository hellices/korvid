"""Unsafe global remaps must not starve text inputs or typed approval gates."""

from __future__ import annotations

import pytest
from textual.widgets import Input

from korvid.core.config import KorvidConfig
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.resource_table import ResourceTable

from .test_app import _pod, make_app
from .test_command_input_burst import _send_burst
from .waits import until


@pytest.mark.parametrize("input_kind", ["command", "filter", "approval"])
async def test_printable_priority_remap_cannot_steal_input_text(
    monkeypatch: pytest.MonkeyPatch, input_kind: str
) -> None:
    interrupted: list[bool] = []
    approved: list[bool | None] = []
    app = make_app([_pod("worker-k")], config=KorvidConfig(keybindings={"interrupt_agent": "k"}))
    monkeypatch.setattr(app, "action_interrupt_agent", lambda: interrupted.append(True))
    async with app.run_test() as pilot:
        if input_kind == "approval":
            dialog = ConfirmScreen("Delete", "delete pod/worker-k", require_name="worker-k")
            app.push_screen(dialog, approved.append)
            await until(pilot, lambda: isinstance(app.focused, Input))
        else:
            await pilot.press("colon" if input_kind == "command" else "slash")
        text_input = app.focused
        assert isinstance(text_input, Input)
        _send_burst(app, "worker-k")
        await pilot.pause()
        assert text_input.value == "worker-k"
        assert interrupted == []
        assert approved == []
        assert app._keybinding_overrides == {}
        await pilot.press("ctrl+x")
        assert interrupted == [True]
        assert text_input.value == "worker-k"
        if input_kind == "approval":
            await pilot.press("enter")
            await until(pilot, lambda: approved)
            assert approved == [True]


async def test_printable_priority_remap_cannot_steal_a_pane_chord_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupted: list[bool] = []
    app = make_app([_pod("web")], config=KorvidConfig(keybindings={"interrupt_agent": "v"}))
    monkeypatch.setattr(app, "action_interrupt_agent", lambda: interrupted.append(True))
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).has_focus)
        _send_burst(app, "\x17v")
        await until(pilot, lambda: app._workspace.is_split or interrupted)
        assert app._workspace.is_split
        assert interrupted == []
        assert app._keybinding_overrides == {}
