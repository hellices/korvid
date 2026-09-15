"""ActionPaletteScreen: the keyboard-first modal (issue #388 task 5).

Standalone modal tests only: no `Ctrl-P` open binding and no app dispatch
live here (a later task pushes this screen from `KorvidApp` and routes its
stable `entry.id` result through `run_action`/`parse_command`). Every
scenario here drives the screen through a minimal host `App` and public
Textual widgets/APIs — no subclassing of Textual's private `CommandPalette`
internals.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, Static

from korvid.ui.action_availability import ActionAvailability, AvailabilityCode, UnavailableReason
from korvid.ui.action_palette import (
    AppActionInvocation,
    CommandInvocation,
    PaletteEntry,
    derive_action_entries,
    derive_command_entries,
)
from korvid.ui.app_bindings import APP_BINDINGS
from korvid.ui.command import COMMANDS
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
            search_terms=("drain",),
            declaration_order=0,
            availability=ActionAvailability(True, reason),
            invocation=AppActionInvocation("drain_node"),
        )
    ]


def _mixed_category_entries(count: int = 30) -> list[PaletteEntry]:
    """`count` invocable entries, split evenly across two categories.

    All invocable with an empty query, `rank_entries` breaks ties purely by
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
                search_terms=(),
                declaration_order=index,
                availability=ActionAvailability.enabled(),
                invocation=AppActionInvocation(f"item_{index:02d}"),
            )
        )
    return entries


#: The two terminals the layout is pinned at: the classic 80x24 and a
#: cramped 36x16 that cannot fit the palette's natural content height. Both
#: must show the *whole* modal — results and the key hint — inside the
#: screen, with the highlighted row rendered in the results viewport.
_LAYOUT_SIZES = [(80, 24), (36, 16)]
_LAYOUT_IDS = ["80x24", "36x16"]


def _viewport_lines(options: OptionList) -> list[str]:
    """The text the results list is actually showing at its scroll offset.

    `Widget.render_line` is the same public rendering entry point Textual's
    compositor calls, so this is what the user sees — not what the option
    list holds.
    """
    return [
        "".join(segment.text for segment in options.render_line(y))
        for y in range(options.size.height)
    ]


@pytest.mark.parametrize("size", _LAYOUT_SIZES, ids=_LAYOUT_IDS)
async def test_the_whole_modal_stays_inside_the_screen(size: tuple[int, int]) -> None:
    """More entries than fit must shrink the results, not push the modal off
    screen: the container stays inside the screen and both the results list
    and the key hint stay inside the container."""
    screen = ActionPaletteScreen(_mixed_category_entries(30))
    app = PaletteHarness(screen)
    async with app.run_test(size=size):
        container = screen.query_one("#action-palette")
        results = screen.query_one(OptionList)
        hint = screen.query_one("#action-hint", Static)
        assert app.screen.region.contains_region(container.region)
        assert container.region.contains_region(results.region)
        assert container.region.contains_region(hint.region)


@pytest.mark.parametrize("size", _LAYOUT_SIZES, ids=_LAYOUT_IDS)
async def test_the_key_hint_is_composited_on_screen(size: tuple[int, int]) -> None:
    """The hint is the only thing telling the user how to run or leave the
    palette, so it must be a real, hit-testable widget at its own region -
    a clipped one answers `NoWidget` (or is never laid out on screen)."""
    screen = ActionPaletteScreen(_mixed_category_entries(30))
    app = PaletteHarness(screen)
    async with app.run_test(size=size):
        hint = screen.query_one("#action-hint", Static)
        assert app.screen.region.contains_region(hint.region)
        widget, _ = app.screen.get_widget_at(hint.region.x, hint.region.y)
        assert widget is hint


@pytest.mark.parametrize("size", _LAYOUT_SIZES, ids=_LAYOUT_IDS)
async def test_end_renders_the_last_result_inside_the_results_viewport(
    size: tuple[int, int],
) -> None:
    """End must highlight a row the user can actually see: the results
    viewport stays inside the modal, and the last entry is rendered in it."""
    screen = ActionPaletteScreen(_mixed_category_entries(30))
    app = PaletteHarness(screen)
    async with app.run_test(size=size) as pilot:
        options = screen.query_one(OptionList)
        container = screen.query_one("#action-palette")
        await pilot.press("end")
        assert options.highlighted == options.option_count - 1
        assert container.region.contains_region(options.region)
        assert any("Item 29" in line for line in _viewport_lines(options))


async def test_a_shrinking_terminal_refits_the_open_palette() -> None:
    """The palette is modal, so a terminal resize can happen under it: the
    results must re-fit rather than keep a viewport sized for the old
    terminal and push the hint back off screen."""
    screen = ActionPaletteScreen(_mixed_category_entries(30))
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.resize_terminal(36, 16)
        await pilot.pause()
        container = screen.query_one("#action-palette")
        hint = screen.query_one("#action-hint", Static)
        assert app.screen.region.contains_region(container.region)
        assert container.region.contains_region(screen.query_one(OptionList).region)
        assert container.region.contains_region(hint.region)


def _command_entries() -> list[PaletteEntry]:
    return [
        PaletteEntry(
            id="command:pulse",
            title="Open Pulse / Problems",
            description="Current problems, recent warnings and observation coverage",
            category="Commands",
            trigger=":pulse",
            search_terms=("problems", "warnings"),
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


def _derived_action_entry(action: str, **overrides: str) -> list[PaletteEntry]:
    """The real `APP_BINDINGS` row for `action`, with optional key remaps.

    Derived, never hand-written: the category and the trigger a row renders
    have to be the ones the catalog (and the user's `keybindings:` remap)
    actually produce, not a second copy maintained in this file.
    """
    entries = derive_action_entries(
        APP_BINDINGS,
        overrides=overrides,
        availability=lambda _action: ActionAvailability.enabled(),
    )
    return [entry for entry in entries if entry.id == f"action:{action}"]


def _derived_command_entry(canonical: str) -> list[PaletteEntry]:
    """The real `COMMANDS` row for one canonical command text."""
    entries = derive_command_entries(
        COMMANDS, availability=lambda _command: ActionAvailability.enabled()
    )
    return [entry for entry in entries if entry.id == f"command:{canonical}"]


def _heading(options: OptionList, index: int = 0) -> str:
    """The first rendered line of a row: category, title and trigger."""
    return str(options.get_option_at_index(index).prompt).splitlines()[0]


def _second_line(options: OptionList, index: int = 0) -> str:
    return str(options.get_option_at_index(index).prompt).splitlines()[1]


async def test_an_action_row_shows_its_category_title_and_default_trigger() -> None:
    screen = ActionPaletteScreen(_derived_action_entry("describe"))
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)):
        heading = _heading(screen.query_one(OptionList))
        assert "Table" in heading
        assert "Describe" in heading
        assert "d" in heading
        assert "Describe" in _second_line(screen.query_one(OptionList))


async def test_an_action_row_shows_the_remapped_trigger_that_actually_runs_it() -> None:
    """A `keybindings:` remap changes what the user has to press, so the row
    has to show `Ctrl-K` rather than the binding's declared default key."""
    screen = ActionPaletteScreen(_derived_action_entry("describe", describe="ctrl+k"))
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)):
        heading = _heading(screen.query_one(OptionList))
        assert "Ctrl-K" in heading
        assert "Table" in heading
        assert "Describe" in heading


async def test_a_command_row_shows_its_category_title_and_colon_spelling() -> None:
    screen = ActionPaletteScreen(_derived_command_entry("pulse"))
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)):
        options = screen.query_one(OptionList)
        heading = _heading(options)
        assert "Commands" in heading
        assert "Open Pulse / Problems" in heading
        assert ":pulse" in heading
        assert "Current problems" in _second_line(options)


async def test_a_row_renders_catalog_text_literally_not_as_markup() -> None:
    """Every rendered part is catalog text, so a `[`/`]` in a category,
    title, trigger or description must survive as characters rather than be
    parsed away as a Rich style tag."""
    entry = PaletteEntry(
        id="action:danger",
        title="[bold red]Delete[/] pod",
        description="[link=http://x]details[/link]",
        category="[Table]",
        trigger="[d]",
        search_terms=(),
        declaration_order=0,
        availability=ActionAvailability.enabled(),
        invocation=AppActionInvocation("danger"),
    )
    screen = ActionPaletteScreen([entry])
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)):
        options = screen.query_one(OptionList)
        prompt = str(options.get_option_at_index(0).prompt)
        rendered = "\n".join(_viewport_lines(options))
        for literal in (
            "[bold red]Delete[/] pod",
            "[link=http://x]details[/link]",
            "[Table]",
            "[d]",
        ):
            assert literal in prompt
        assert "[bold red]Delete[/] pod" in rendered


async def test_a_narrow_terminal_still_renders_category_title_and_trigger() -> None:
    """36 columns wraps the heading instead of dropping any of its parts."""
    screen = ActionPaletteScreen(_derived_command_entry("pulse"))
    app = PaletteHarness(screen)
    async with app.run_test(size=(36, 16)):
        options = screen.query_one(OptionList)
        rendered = "".join(_viewport_lines(options))
        assert "Commands" in rendered
        assert "Pulse" in rendered
        assert ":pulse" in rendered
