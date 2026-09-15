"""Action Palette wiring: guarded opening and exact existing-route dispatch.

Issue #388 task 6. The palette modal (task 5) and its catalog derivation
(task 2) are already reviewed; what is under test here is the *app* half:
`Ctrl-P` replaces Textual's implicit system palette, the open is refused on
every protected or transient surface, the entry list is rebuilt after the
modal is dismissed, and a selection is routed through the app's existing
action/command routes exactly once.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from textual.content import Content
from textual.widgets import Input, OptionList

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.ui.action_availability import (
    AGENT_UNAVAILABLE,
    ActionAvailability,
    AvailabilityCode,
    UnavailableReason,
)
from korvid.ui.action_palette import AppActionInvocation, PaletteEntry
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.action_palette import ActionPaletteScreen
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.describe_screen import DescribeScreen
from korvid.ui.widgets.help_screen import HelpScreen
from korvid.ui.widgets.log_pane import LogPane
from korvid.ui.widgets.pulse import PulseScreen
from korvid.ui.widgets.resource_table import ResourceTable
from tests.app_factory import build_test_app

from .agent_session_fakes import FakeSession
from .test_app import _pod, make_app
from .test_integration_controller import FakeMCP
from .test_telepresence import FakeTelepresence
from .test_write_ops import Recorder
from .test_write_ops import make_app as make_write_app
from .waits import until


def _build_app(**kwargs: Any) -> KorvidApp:
    """A minimal app whose optional capabilities the caller chooses."""
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        yield ("ADDED", _pod("web"))
        while True:
            await asyncio.sleep(0.01)

    async def list_namespaces() -> list[str]:
        return ["default"]

    return build_test_app(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        **kwargs,
    )


def _app_with_integrations() -> KorvidApp:
    return _build_app(mcp=FakeMCP(), telepresence=FakeTelepresence())


def _app_without_agent() -> KorvidApp:
    return _build_app(agent_available=False)


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
    workspace surface, not only the bare table (issue #388).

    Each leg asserts the surface is genuinely in the state it claims before
    pressing `Ctrl-P`: an open log pane, a real second pane owning focus,
    and the Agent prompt `Input` holding the keyboard. Otherwise a leg could
    "pass" against a workspace that never changed.
    """
    app = _build_app(agent_session=FakeSession(), agent_model_name="test-model")
    async with app.run_test() as pilot:
        await _loaded(pilot, app)

        await app._logs.open_pane("default", [("web", "app")])
        log_pane = app.query_one(LogPane)
        await until(pilot, lambda: log_pane.display, label="log pane open")
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is app.screen_stack[0], label="palette closed")

        await pilot.press("ctrl+w", "v")  # split workspace
        await until(pilot, lambda: len(app.query(ResourceTable)) == 2, label="split panes exist")
        second = app.query_one("#pane-1", ResourceTable)
        await until(pilot, lambda: app.focused is second, label="new pane focused")
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is app.screen_stack[0], label="palette closed")
        assert app.focused is second

        await pilot.press("ctrl+a")  # agent panel
        await until(pilot, lambda: app._agent_panel.display, label="agent panel open")
        # A session is wired, so opening the panel focuses its prompt: this
        # leg really exercises "an Input that is not the command or filter
        # bar owns the keyboard when Ctrl-P is pressed".
        prompt = app._agent_panel.query_one("#agent-input", Input)
        await until(pilot, lambda: app.focused is prompt, label="agent prompt focused")
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is app.screen_stack[0], label="palette closed")
        assert app.focused is prompt


async def test_palette_opens_over_the_inline_describe_pane() -> None:
    """The agent-shared describe *pane* is an ordinary workspace surface -
    it is mounted on the base screen, not pushed - so the palette opens over
    it exactly as it does over the table (task 6 review)."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        pane = app._describe_pane
        pane.show("pods/default/web", {"kind": "Pod", "metadata": {"name": "web"}}, [])
        await until(pilot, lambda: pane.display, label="describe pane shown")
        assert len(app.screen_stack) == 1
        assert app._actions.binding_enabled("open_action_palette") is True
        await _open_palette(pilot)
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is app.screen_stack[0], label="palette closed")
        assert pane.display


async def test_palette_refuses_to_open_over_the_describe_modal() -> None:
    """The describe *screen* is a modal (what `d` opens): stacking the
    palette over it is exactly what the surface guard forbids."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await app.push_screen(DescribeScreen("pods/default/web", {"kind": "Pod"}, []))
        await until(pilot, lambda: isinstance(app.screen, DescribeScreen), label="describe modal")
        screen = app.screen
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.screen is screen
        assert len(app.screen_stack) == 2
        assert app._actions.binding_enabled("open_action_palette") is False


async def test_ctrl_p_over_an_approval_dialog_changes_nothing() -> None:
    """The security case, pressed rather than reasoned about: `Ctrl-P` is a
    priority binding, so it reaches the app even while an approval dialog is
    up. Nothing may move - not the screen, not the focus, and no palette."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        await app.push_screen(ConfirmScreen("Delete pod default/web?", "delete pods/default/web"))
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval open")
        screen = app.screen
        focused = app.focused
        stack = list(app.screen_stack)
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.screen is screen
        assert app.focused is focused
        assert list(app.screen_stack) == stack
        assert not any(isinstance(s, ActionPaletteScreen) for s in app.screen_stack)
        assert app._actions.binding_enabled("open_action_palette") is False


# ---------------------------------------------------------------------------
# Command rows answer to their own owners' capabilities
# ---------------------------------------------------------------------------


def _row(app: KorvidApp, entry_id: str) -> PaletteEntry:
    entries = {entry.id: entry for entry in app._palette_entries()}
    return entries[entry_id]


async def test_command_rows_are_disabled_without_their_capability() -> None:
    """No MCP controller and no telepresence CLI wired: pressing `:mcp` or
    `:tp` only earns a refusal, so the palette must not advertise them."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        assert app._mcp is None
        assert app._integrations.telepresence_available is False
        for entry_id in ("command:mcp", "command:tp"):
            row = _row(app, entry_id)
            assert row.availability.invokable is False
            reason = row.availability.reason
            assert reason is not None
            assert reason.code is AvailabilityCode.MISSING_CAPABILITY
        # The agent *is* available here, so its rows stay runnable.
        assert _row(app, "command:ai").availability.invokable is True
        assert _row(app, "command:model").availability.invokable is True
        # And an unavailable row renders as a disabled option.
        screen = await _open_palette(pilot)
        await _type_query(pilot, "mcp")
        options = screen.query_one(OptionList)
        await until(
            pilot,
            lambda: options.option_count > 0 and options.get_option_at_index(0).id == "command:mcp",
            label="mcp ranked first",
        )
        assert options.get_option_at_index(0).disabled is True


async def test_command_rows_are_enabled_once_their_capability_is_wired() -> None:
    app = _app_with_integrations()
    async with app.run_test() as pilot:
        await until(pilot, lambda: app._mcp is not None, label="app composed")
        await pilot.pause()
        assert _row(app, "command:mcp").availability.invokable is True
        assert _row(app, "command:tp").availability.invokable is True


async def test_agent_command_rows_are_disabled_without_the_agent() -> None:
    """`:ai`/`:model` availability is composed at the wiring (the agent
    controller is at its reviewed size cap, so the answer is assembled from
    its existing `available` flag and the shared reason rather than added as
    a method on it - task 6 re-review)."""
    app = _app_without_agent()
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app._notifications)
        for entry_id in ("command:ai", "command:model"):
            row = _row(app, entry_id)
            assert row.availability.invokable is False
            assert row.availability.reason == AGENT_UNAVAILABLE
        # Deriving the catalog is a silent probe: it asks, it never tells.
        assert len(app._notifications) == before


async def test_agent_command_rows_are_enabled_with_the_agent() -> None:
    app = _build_app(agent_session=FakeSession(), agent_model_name="test-model")
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        assert app._agent_ui.available is True
        for entry_id in ("command:ai", "command:model"):
            assert _row(app, entry_id).availability == ActionAvailability.enabled()


async def test_the_agent_row_explains_the_capability_not_the_router_fallback() -> None:
    """The two wordings differ on purpose, so nothing should assert parity.

    Without the [agent] extra no owner claims `:ai`, so `CommandRouter` falls
    through to its generic "nothing routed this" report - a *routing* outcome
    phrased for a mistyped command. The palette is answering a different
    question ("why is this row greyed out?"), and says what is actually
    missing. Each stays the right sentence for its own moment.
    """
    app = _app_without_agent()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert _row(app, "command:ai").availability.reason == AGENT_UNAVAILABLE
        await pilot.press("colon")
        for character in "ai":
            await pilot.press(character)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("Unknown resource or command: ai" in n.message for n in app._notifications),
            label="router fallback reported",
        )
        assert not any(n.message == AGENT_UNAVAILABLE.message for n in app._notifications)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_the_palette_closes_its_door_once_the_app_is_exiting() -> None:
    """`_accepting_input()` is the one place that reads Textual's exit flag;
    this pins its observable effect rather than the attribute."""
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        assert app._accepting_input() is True
        assert app._actions.binding_enabled("open_action_palette") is True
        app.exit()
        assert app._accepting_input() is False
        assert app._actions.binding_enabled("open_action_palette") is False
        assert app._actions.availability("open_action_palette").reason == UnavailableReason(
            AvailabilityCode.PROTECTED_UI, "korvid is shutting down"
        )


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


# ---------------------------------------------------------------------------
# Owner text is data, not markup
# ---------------------------------------------------------------------------


async def test_an_owner_reason_is_notified_literally_not_as_markup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capability reasons quote install hints like `pip install korvid[mcp]`.
    Rendered as Rich content markup, `[mcp]` is a style tag: the text the
    user needs would silently disappear (or restyle the toast). The palette
    notifies the owner's message literally, exactly as the owners' own
    handlers do with `markup=False` (task 6 re-review)."""
    message = "MCP unavailable — install korvid[mcp] to enable it"
    reason = UnavailableReason(AvailabilityCode.MISSING_CAPABILITY, message)
    app = make_app([_pod("web")])
    async with app.run_test() as pilot:
        await _loaded(pilot, app)
        monkeypatch.setattr(
            app,
            "_palette_entries",
            lambda: [
                _entry("action:help", ActionAvailability(binding_enabled=True, reason=reason))
            ],
        )
        await app._palette_selected("action:help")
        await until(
            pilot,
            lambda: any(n.message == message for n in app._notifications),
            label="owner reason notified",
        )
        notification = next(n for n in app._notifications if n.message == message)
        assert notification.markup is False
        # Why it matters: as markup, the bracketed extra name is parsed away.
        assert Content.from_markup(message).plain != message
        assert Content(notification.message).plain == message
