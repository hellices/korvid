"""ActionPaletteScreen: the keyboard-first modal (issue #388 task 5).

Standalone modal tests only: no `Ctrl-P` open binding and no app dispatch
live here (a later task pushes this screen from `KorvidApp` and routes its
stable `entry.id` result through `run_action`/`parse_command`). Every
scenario here drives the screen through a minimal host `App` and public
Textual widgets/APIs — no subclassing of Textual's private `CommandPalette`
internals.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, Static

from korvid.ui.action_availability import ActionAvailability, AvailabilityCode, UnavailableReason
from korvid.ui.action_palette import AppActionInvocation, CommandInvocation, PaletteEntry
from korvid.ui.widgets.action_palette import ActionPaletteScreen


class PaletteHarness(App[None]):
    # Textual's implicit system command palette also answers to `ctrl+p`
    # (as a `priority=True` App-level binding, so it wins the resolution
    # race over any screen-level binding for the same key); disabling it
    # here — rather than subclassing `CommandPalette`'s private methods —
    # is the documented way to let this modal's own Ctrl-P close it.
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, palette: ActionPaletteScreen) -> None:
        super().__init__()
        self.palette = palette
        self.results: list[str | None] = []

    def compose(self) -> ComposeResult:
        yield Static("workspace")

    def on_mount(self) -> None:
        self.push_screen(self.palette, self.results.append)


def _entries(*, drain_available: bool = True) -> list[PaletteEntry]:
    reason = (
        None
        if drain_available
        else UnavailableReason(
            AvailabilityCode.NO_SELECTION,
            "select a node first",
        )
    )
    return [
        PaletteEntry(
            id="action:drain_node",
            title="Drain node",
            description="Safely evict workloads from a node",
            category="Actions",
            trigger="Shift-D",
            aliases=("drain",),
            declaration_order=0,
            availability=ActionAvailability(True, reason),
            invocation=AppActionInvocation("drain_node"),
        )
    ]


def _mixed_category_entries(count: int = 30) -> list[PaletteEntry]:
    """`count` invokable entries, split evenly across two categories.

    All invokable with an empty query, `rank_entries` breaks ties purely by
    `declaration_order`, so this fixture's rendered order is exactly this
    list's order — deterministic enough to test paging, edges, and the one
    category transition it contains.
    """
    entries: list[PaletteEntry] = []
    half = count // 2
    for index in range(count):
        category = "Global" if index < half else "Table"
        entries.append(
            PaletteEntry(
                id=f"action:item_{index:02d}",
                title=f"Item {index:02d}",
                description=f"Description for item {index:02d}",
                category=category,
                trigger=f"F{index}",
                aliases=(),
                declaration_order=index,
                availability=ActionAvailability.enabled(),
                invocation=AppActionInvocation(f"item_{index:02d}"),
            )
        )
    return entries


def _command_entries() -> list[PaletteEntry]:
    return [
        PaletteEntry(
            id="command:pulse",
            title="Open Pulse / Problems",
            description="Current problems, recent warnings and observation coverage",
            category="Commands",
            trigger=":pulse",
            aliases=("problems", "warnings"),
            declaration_order=0,
            availability=ActionAvailability.enabled(),
            invocation=CommandInvocation("pulse"),
        )
    ]


async def test_palette_filters_and_selects_an_available_entry() -> None:
    screen = ActionPaletteScreen(_entries())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("d", "r", "a", "i", "n")
        options = screen.query_one(OptionList)
        assert "Drain node" in str(options.get_option_at_index(0).prompt)
        await pilot.press("enter")
        assert app.results == ["action:drain_node"]


async def test_unavailable_entry_is_visible_but_not_selectable() -> None:
    screen = ActionPaletteScreen(_entries(drain_available=False))
    app = PaletteHarness(screen)
    async with app.run_test() as pilot:
        await pilot.press("d", "r", "a", "i", "n")
        option = screen.query_one(OptionList).get_option_at_index(0)
        assert option.disabled is True
        assert "Unavailable: select a node first" in str(option.prompt)
        await pilot.press("enter")
        assert app.results == []


async def test_palette_renders_in_a_narrow_terminal() -> None:
    app = PaletteHarness(ActionPaletteScreen(_entries()))
    async with app.run_test(size=(36, 16)):
        container = app.screen.query_one("#action-palette")
        assert container.size.width <= 34
        assert app.screen.query_one(Input).has_focus


async def test_no_results_shows_an_explicit_disabled_row() -> None:
    screen = ActionPaletteScreen(_entries())
    app = PaletteHarness(screen)
    async with app.run_test() as pilot:
        await pilot.press("z", "z", "z", "z", "z")
        options = screen.query_one(OptionList)
        assert options.option_count == 1
        option = options.get_option_at_index(0)
        assert option.disabled is True
        assert "no match" in str(option.prompt).casefold()
        await pilot.press("enter")
        assert app.results == []


async def test_escape_dismisses_with_none() -> None:
    app = PaletteHarness(ActionPaletteScreen(_entries()))
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.results == [None]


async def test_ctrl_p_dismisses_with_none() -> None:
    app = PaletteHarness(ActionPaletteScreen(_entries()))
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        assert app.results == [None]


async def test_down_and_up_move_the_option_list_highlight_while_input_keeps_focus() -> None:
    screen = ActionPaletteScreen(_mixed_category_entries())
    app = PaletteHarness(screen)
    async with app.run_test() as pilot:
        options = screen.query_one(OptionList)
        assert options.highlighted == 0
        await pilot.press("down")
        assert options.highlighted == 1
        await pilot.press("down")
        assert options.highlighted == 2
        await pilot.press("up")
        assert options.highlighted == 1
        assert screen.query_one(Input).has_focus


async def test_page_down_and_page_up_move_the_highlight_by_a_page() -> None:
    screen = ActionPaletteScreen(_mixed_category_entries())
    app = PaletteHarness(screen)
    async with app.run_test() as pilot:
        options = screen.query_one(OptionList)
        await pilot.press("pagedown")
        after_page_down = options.highlighted
        assert after_page_down is not None
        # A page is a real fraction of the list: neither stuck at the top
        # nor jumped straight to the bottom (that's Home/End's job).
        assert 0 < after_page_down < options.option_count - 1
        assert screen.query_one(Input).has_focus
        await pilot.press("pageup")
        after_page_up = options.highlighted
        assert after_page_up is not None
        assert after_page_up < after_page_down
        assert screen.query_one(Input).has_focus


async def test_home_and_end_jump_to_first_and_last_while_input_keeps_focus() -> None:
    screen = ActionPaletteScreen(_mixed_category_entries())
    app = PaletteHarness(screen)
    async with app.run_test() as pilot:
        options = screen.query_one(OptionList)
        input_widget = screen.query_one(Input)
        await pilot.press("end")
        assert options.highlighted == options.option_count - 1
        assert input_widget.has_focus
        await pilot.press("home")
        assert options.highlighted == 0
        assert input_widget.has_focus


async def test_category_separators_appear_between_different_categories() -> None:
    """Consecutive entries whose `category` differs get a visual divider
    between them; `OptionList` implements this as a flag on the preceding
    `Option` (`_divider`) rather than as an extra list item, so indices
    stay untouched — checked here alongside the private flag itself, since
    `OptionList` exposes no public accessor for it.
    """
    entries = _mixed_category_entries(30)
    screen = ActionPaletteScreen(entries)
    app = PaletteHarness(screen)
    async with app.run_test() as pilot:
        del pilot
        options = screen.query_one(OptionList)
        assert options.option_count == 30
        last_global = options.get_option_at_index(14)
        first_table = options.get_option_at_index(15)
        assert last_global.id == "action:item_14"
        assert first_table.id == "action:item_15"
        assert last_global._divider is True
        assert first_table._divider is False
        assert options.get_option_at_index(0)._divider is False


async def test_commands_and_actions_are_both_selectable() -> None:
    entries = [*_entries(), *_command_entries()]
    screen = ActionPaletteScreen(entries)
    app = PaletteHarness(screen)
    async with app.run_test() as pilot:
        await pilot.press("p", "u", "l", "s", "e")
        options = screen.query_one(OptionList)
        assert "Open Pulse" in str(options.get_option_at_index(0).prompt)
        await pilot.press("enter")
        assert app.results == ["command:pulse"]
