"""Terminal batches must enter command mode before routing their remaining keys."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from textual._xterm_parser import XTermParser
from textual.widgets import Input

from korvid.core.config import KorvidConfig
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.help_screen import HelpScreen
from korvid.ui.widgets.keybinding_editor import KeybindingEditorScreen
from korvid.ui.widgets.resource_table import ResourceTable

from .test_app import _pod, make_app
from .waits import until


def _send_burst(app: KorvidApp, sequence: str) -> None:
    assert app._driver is not None
    for event in XTermParser().feed(sequence):
        app._driver.process_message(event)


@pytest.mark.parametrize("after_help", [False, True])
@pytest.mark.parametrize(
    ("key", "prefix"),
    [("colon", ":"), ("semicolon", ";"), ("f1", "\x1bOP"), ("f12", "\x1b[24~")],
)
async def test_command_burst_opens_editor_without_losing_characters(
    after_help: bool, key: str, prefix: str
) -> None:
    saved: list[Mapping[str, str]] = []
    app = make_app(
        [_pod("web-1"), _pod("web-2")],
        config=KorvidConfig(namespace="default", readonly=True, keybindings={"open_command": key}),
        kubectl=False,
        save_keybindings=saved.append,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2 and table.has_focus)
        if after_help:
            await pilot.press("question_mark", "escape")
        await pilot.press("down")
        assert table.cursor_row == 1
        _send_burst(app, prefix + "keys")
        await until(pilot, lambda: app._command_bar.has_focus)
        assert app._command_bar.value == "keys"
        await pilot.press("enter")
        assert isinstance(app.screen, KeybindingEditorScreen)
        assert table.cursor_row == 1
        assert saved == []


async def test_command_burst_can_include_submit() -> None:
    saved: list[Mapping[str, str]] = []
    app = make_app(
        [_pod("web")],
        config=KorvidConfig(readonly=True),
        kubectl=False,
        save_keybindings=saved.append,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).has_focus)
        _send_burst(app, ":keybindings\r")
        await until(pilot, lambda: isinstance(app.screen, KeybindingEditorScreen))
        assert isinstance(app.screen, KeybindingEditorScreen)
        assert not app._command_bar.display
        assert saved == []


async def test_command_bar_takes_focus_without_waiting_for_reflow() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).has_focus)
        with app.batch_update():
            app.action_open_command()
            assert app.focused is app._command_bar


@pytest.mark.parametrize("bar_key", ["colon", "slash"])
async def test_colon_in_an_open_input_remains_text(bar_key: str) -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await pilot.press(bar_key)
        bar = app.focused
        assert isinstance(bar, Input)
        _send_burst(app, "text:keys")
        await until(pilot, lambda: bar.value == "text:keys")
        assert app.focused is bar
        assert bar.value == "text:keys"


@pytest.mark.parametrize("modal_kind", ["help", "approval"])
async def test_command_burst_cannot_open_a_bar_behind_a_modal(modal_kind: str) -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        modal = HelpScreen([], []) if modal_kind == "help" else ConfirmScreen("Delete", "delete")
        app.push_screen(modal)
        await until(pilot, lambda: app.screen is modal and modal.is_mounted)
        _send_burst(app, ":keys")
        await pilot.press("escape")
        assert len(app.screen_stack) == 1
        assert not app._command_bar.display


async def test_remapped_command_key_keeps_modal_decline_available() -> None:
    results: list[bool | None] = []
    app = make_app([_pod("web")], config=KorvidConfig(keybindings={"open_command": "ctrl+n"}))
    async with app.run_test() as pilot:
        assert app._keybinding_overrides == {"open_command": "ctrl+n"}
        modal = ConfirmScreen("Delete", "delete")
        app.push_screen(modal, results.append)
        await until(pilot, lambda: app.screen is modal and modal.is_mounted)
        await pilot.press("ctrl+n")
        await until(pilot, lambda: results)
        assert results == [False]
        assert not app._command_bar.display


async def test_command_burst_preserves_priority_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupted: list[bool] = []
    app = make_app([_pod("web")], config=KorvidConfig(readonly=True), kubectl=False)
    monkeypatch.setattr(app, "action_interrupt_agent", lambda: interrupted.append(True))
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).has_focus)
        _send_burst(app, ":keys\x18")
        await until(pilot, lambda: interrupted and app._command_bar.value == "keys")
        assert interrupted == [True]
        assert app._command_bar.value == "keys"


@pytest.mark.parametrize("single_burst", [False, True])
async def test_pane_chord_keeps_a_remapped_command_key(single_burst: bool) -> None:
    app = make_app([_pod("web")], config=KorvidConfig(keybindings={"open_command": "v"}))
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).has_focus)
        assert app._keybinding_overrides == {"open_command": "v"}
        if single_burst:
            _send_burst(app, "\x17v")
        else:
            await pilot.press("ctrl+w")
            assert app._workspace_ctl.chord_pending
            _send_burst(app, "v")
        await until(pilot, lambda: app._workspace.is_split or app._command_bar.display)
        assert app._workspace.is_split
        assert not app._command_bar.display


async def test_unknown_pane_chord_does_not_enter_command_mode() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await pilot.press("ctrl+w", "colon")
        assert not app._workspace_ctl.chord_pending
        assert not app._command_bar.display
        await pilot.press("colon")
        assert app._command_bar.has_focus


async def test_repeated_chords_in_one_burst_close_a_pane_without_quitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    quit_requested: list[bool] = []
    app = make_app([_pod("web")])
    close_pane = app._workspace_ctl.close_focused_pane

    async def record_close() -> None:
        await close_pane()
        closed.append(len(app._workspace.panes))

    monkeypatch.setattr(app._workspace_ctl, "close_focused_pane", record_close)
    monkeypatch.setattr(app, "action_quit", lambda: quit_requested.append(True))
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).has_focus)
        _send_burst(app, "\x17v\x17q")
        await until(pilot, lambda: closed or quit_requested)
        assert quit_requested == []
        assert closed == [1]
        assert not app._workspace.is_split
        assert not app._workspace_ctl.chord_pending


async def test_repeated_chords_in_one_burst_preserve_dispatch_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled: list[str] = []
    app = make_app([_pod("web")])
    handle_key = app._workspace_ctl.handle_pane_chord_key

    async def record_key(key: str) -> bool:
        handled.append(key)
        return await handle_key(key)

    monkeypatch.setattr(app._workspace_ctl, "handle_pane_chord_key", record_key)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).has_focus)
        _send_burst(app, "\x17v\x17w")
        await until(pilot, lambda: len(handled) == 4 and not app._workspace_ctl.chord_pending)
        assert handled == ["ctrl+w", "v", "ctrl+w", "w"]
        assert app._workspace.is_split
        assert not app._workspace_ctl.chord_pending


@pytest.mark.parametrize(("earlier_key", "expected_row"), [("x", 0), ("\x1b[B", 1)])
async def test_forwarded_key_cannot_consume_a_later_chord_prefix(
    earlier_key: str, expected_row: int
) -> None:
    app = make_app(
        [_pod("web-1"), _pod("web-2")],
        config=KorvidConfig(keybindings={"open_command": "v"}),
    )
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2 and table.has_focus)
        _send_burst(app, earlier_key + "\x17")
        await pilot.pause()
        assert app._workspace_ctl.chord_pending
        assert table.cursor_row == expected_row
        await pilot.press("v")
        assert app._workspace.is_split
        assert not app._command_bar.display
