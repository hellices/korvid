"""Action Palette wiring: guarded opening and exact existing-route dispatch.

Issue #388 task 6. The palette modal (task 5) and its catalog derivation
(task 2) are already reviewed; what is under test here is the *app* half:
`Ctrl-P` replaces Textual's implicit system palette, the open is refused on
every protected or transient surface, the entry list is rebuilt after the
modal is dismissed, and a selection is routed through the app's existing
action/command routes exactly once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import OptionList

from korvid.ui.action_availability import ActionAvailability, AvailabilityCode, UnavailableReason
from korvid.ui.action_palette import AppActionInvocation, PaletteEntry
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.action_palette import ActionPaletteScreen
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.help_screen import HelpScreen
from korvid.ui.widgets.log_pane import LogPane
from korvid.ui.widgets.pulse import PulseScreen
from korvid.ui.widgets.resource_table import ResourceTable

from .test_app import _pod, make_app
from .test_write_ops import Recorder
from .test_write_ops import make_app as make_write_app
from .waits import until


async def _loaded(pilot: Any, app: KorvidApp) -> ResourceTable:
    table = app.query_one(ResourceTable)
    await until(pilot, lambda: table.row_count == 1, label="pod loaded")
    return table


async def _open_palette(pilot: Any) -> ActionPaletteScreen:
    await pilot.press("ctrl+p")
    await until(
        pilot,
        lambda: isinstance(pilot.app.screen, ActionPaletteScreen),
        label="palette open",
    )
    screen = pilot.app.screen
    assert isinstance(screen, ActionPaletteScreen)
    return screen


async def _type_query(pilot: Any, query: str) -> None:
    for character in query:
        await pilot.press("space" if character == " " else character)


async def _select_palette_entry(pilot: Any, query: str, expected_id: str) -> None:
    screen = await _open_palette(pilot)
    await _type_query(pilot, query)
    options = screen.query_one(OptionList)
    await until(
        pilot,
        lambda: options.option_count > 0 and options.get_option_at_index(0).id == expected_id,
        label=f"{expected_id} ranked first",
    )
    await pilot.press("enter")


def _entry(entry_id: str, availability: ActionAvailability) -> PaletteEntry:
    return PaletteEntry(
        id=entry_id,
        title="Help",
        description="Help",
        category="Global",
        trigger="?",
        aliases=(),
        declaration_order=0,
        availability=availability,
        invocation=AppActionInvocation("help"),
    )


# ---------------------------------------------------------------------------
# Opening: Ctrl-P is korvid's own binding, not Textual's system palette
# ---------------------------------------------------------------------------


def test_textual_system_command_palette_is_disabled() -> None:
    """Textual binds `Ctrl-P` to its own command palette implicitly; korvid
    owns that key, so the stock palette must be switched off rather than
    shadowed by a competing binding."""
    assert KorvidApp.ENABLE_COMMAND_PALETTE is False


async def test_ctrl_p_opens_palette_and_escape_restores_table_focus() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        table = await _loaded(pilot, app)
        table.focus()
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(
            pilot,
            lambda: app.screen is app.screen_stack[0],
            label="palette closed",
        )
        assert app.focused is table


async def test_palette_opens_from_the_log_split_and_agent_surfaces() -> None:
    """The palette is the keyboard-first entry point for every ordinary
    workspace surface, not only the bare table (issue #388)."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await app._logs.open_pane("default", [("web", "app")])
        log_pane = app.query_one(LogPane)
        await until(pilot, lambda: log_pane.display, label="log pane open")
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is app.screen_stack[0], label="palette closed")

        await pilot.press("ctrl+w", "v")  # split workspace
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is app.screen_stack[0], label="palette closed")

        await pilot.press("ctrl+a")  # agent panel
        await until(pilot, lambda: app._agent_panel.display, label="agent panel open")
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is app.screen_stack[0], label="palette closed")


# ---------------------------------------------------------------------------
# Dispatch: one route, once, after the modal is gone
# ---------------------------------------------------------------------------


async def test_palette_action_dispatches_once_after_dismissal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app([_pod("web")])
    calls = 0
    original = app.action_help

    def counted_help() -> None:
        nonlocal calls
        calls += 1
        original()

    monkeypatch.setattr(app, "action_help", counted_help)
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await _select_palette_entry(pilot, "help", "action:help")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
        assert calls == 1
        assert not any(isinstance(screen, ActionPaletteScreen) for screen in app.screen_stack)


async def test_palette_command_posts_the_typed_pulse_route_once() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await _select_palette_entry(pilot, "pulse", "command:pulse")
        await until(pilot, lambda: isinstance(app.screen, PulseScreen), label="pulse open")
        assert len([s for s in app.screen_stack if isinstance(s, PulseScreen)]) == 1


async def test_palette_enter_never_reaches_an_approval_dialog(tmp_path: Path) -> None:
    """The Enter that runs a palette entry must not also answer the approval
    dialog that entry opens: the palette consumes the keystroke, and the
    dialog is confirmed only by a fresh user keystroke (security invariant)."""
    recorder = Recorder()
    app = make_write_app(recorder, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: app.query_one(ResourceTable).row_count > 0,
            label="pods loaded",
        )
        await _select_palette_entry(pilot, "delete resource", "action:delete_resource")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="approval dialog open",
        )
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert recorder.calls == []


# ---------------------------------------------------------------------------
# Re-resolution: the list is rebuilt, the id re-checked, nothing stale runs
# ---------------------------------------------------------------------------


async def test_a_stale_selection_notifies_and_dispatches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The screen returns only a stable id; the app re-derives fresh entries
    after dismissal, so an entry that disappeared meanwhile cannot run."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        screen = await _open_palette(pilot)
        await _type_query(pilot, "help")
        options = screen.query_one(OptionList)
        await until(
            pilot,
            lambda: options.option_count > 0 and options.get_option_at_index(0).id == "action:help",
            label="help ranked first",
        )
        monkeypatch.setattr(app, "_palette_entries", list)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("no longer available" in n.message for n in app._notifications),
            label="stale-selection notice",
        )
        assert not isinstance(app.screen, HelpScreen)


async def test_an_entry_that_became_unavailable_reports_its_owner_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Availability is re-checked against freshly derived entries, and the
    owner's own wording is what the user sees - no second vocabulary."""
    app = make_app([_pod("web")])
    reason = UnavailableReason(AvailabilityCode.NO_SELECTION, "Select a resource first")
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        screen = await _open_palette(pilot)
        await _type_query(pilot, "help")
        options = screen.query_one(OptionList)
        await until(
            pilot,
            lambda: options.option_count > 0 and options.get_option_at_index(0).id == "action:help",
            label="help ranked first",
        )
        monkeypatch.setattr(
            app,
            "_palette_entries",
            lambda: [
                _entry("action:help", ActionAvailability(binding_enabled=True, reason=reason))
            ],
        )
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any(reason.message in n.message for n in app._notifications),
            label="owner reason notified",
        )
        assert not isinstance(app.screen, HelpScreen)


# ---------------------------------------------------------------------------
# Protected and transient surfaces refuse the open
# ---------------------------------------------------------------------------


async def test_palette_refuses_to_open_while_the_command_bar_is_editing() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await pilot.press("colon")
        await until(pilot, lambda: app._command_bar.display, label="command bar open")
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert not isinstance(app.screen, ActionPaletteScreen)
        assert app._actions.binding_enabled("open_action_palette") is False


async def test_palette_refuses_to_open_while_the_filter_bar_is_editing() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await pilot.press("slash")
        await until(pilot, lambda: app._filter_bar.display, label="filter bar open")
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert not isinstance(app.screen, ActionPaletteScreen)
        assert app._actions.binding_enabled("open_action_palette") is False


async def test_palette_refuses_to_open_over_a_modal() -> None:
    """`Ctrl-P` is a priority binding, so it fires over any screen: the
    policy - not the screen stack - is what keeps it off a modal."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert len(app.screen_stack) == 2
        assert app._actions.binding_enabled("open_action_palette") is False


async def test_palette_refuses_to_open_during_a_context_switch() -> None:
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        app._ctx._switching = True
        try:
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert not isinstance(app.screen, ActionPaletteScreen)
            assert app._actions.binding_enabled("open_action_palette") is False
        finally:
            app._ctx._switching = False


async def test_the_direct_action_refuses_even_when_dispatch_was_bypassed() -> None:
    """`check_action` already refuses the key, but the action itself must
    not be a back door for any caller that reaches it another way."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
        app.action_open_action_palette()
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert len(app.screen_stack) == 2
