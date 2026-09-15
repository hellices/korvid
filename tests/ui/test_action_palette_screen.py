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
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

from korvid.ui.action_availability import ActionAvailability, AvailabilityCode, UnavailableReason
from korvid.ui.action_palette import (
    AppActionInvocation,
    CommandInvocation,
    PaletteEntry,
    derive_action_entries,
    derive_command_entries,
    derive_palette_entries,
)
from korvid.ui.app_bindings import APP_BINDINGS
from korvid.ui.command import COMMANDS
from korvid.ui.read_availability import relationships_reason
from korvid.ui.widgets.action_palette import ActionPaletteScreen
from korvid.ui.write_availability import WriteAvailability

from .test_integration_controller import Harness as IntegrationHarness
from .waits import until


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


def _rewrapping_entries(count: int = 30) -> list[PaletteEntry]:
    """`count` invocable rows whose height depends on the terminal width.

    Every description is a real-length sentence that fits one line at 80
    columns and wraps to several at 36, so the list's virtual height more
    than doubles on the way down. A scroll offset computed for the wide
    terminal therefore points at a genuinely different row afterwards —
    which is the condition the resize finding is about, rather than an
    offset that happens to still land near the end.
    """
    entries: list[PaletteEntry] = []
    for index in range(count):
        entries.append(
            PaletteEntry(
                id=f"action:item_{index:02d}",
                title=f"Item {index:02d}",
                description=f"Safely evict every workload from selected node {index:02d}",
                category="Global",
                trigger=f"F{index}",
                search_terms=(),
                declaration_order=index,
                availability=ActionAvailability.enabled(),
                invocation=AppActionInvocation(f"item_{index:02d}"),
            )
        )
    return entries


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


def _visible_text(options: OptionList) -> str:
    """The viewport's rendered words, rejoined across wrap points.

    A row at 36 columns wraps at spaces, so the words the user can read are
    the ones this returns; searching it for a whole phrase asks exactly the
    question the finding did - is the trigger, the description or the
    reason *on screen* - without pinning where the wrap happens to fall.
    """
    return " ".join(word for line in _viewport_lines(options) for word in line.split())


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


#: Rows of results content a real catalog row needs to render whole at 36
#: columns (the design doc's "eight rows of results"): a long title plus a
#: `:` spelling, or an owner's refusal, wraps to six to eight lines there.
_READABLE_RESULT_ROWS = 8


@pytest.mark.parametrize("size", [(80, 24), (80, 40)], ids=["80x24", "80x40"])
async def test_a_roomy_terminal_keeps_the_modal_inside_its_height_share(
    size: tuple[int, int],
) -> None:
    """The 80% cap still applies wherever it can be afforded.

    A terminal that can spare the share and still show a readable list
    keeps the palette a palette: bounded above by 80% of the terminal, and
    by the eighteen-row result cap no matter how tall the terminal is.
    """
    screen = ActionPaletteScreen(_mixed_category_entries(30))
    app = PaletteHarness(screen)
    async with app.run_test(size=size):
        container = screen.query_one("#action-palette")
        results = screen.query_one(OptionList)
        assert container.region.height <= size[1] * 80 // 100
        assert results.size.height >= _READABLE_RESULT_ROWS
        assert results.size.height <= 18


async def test_a_short_terminal_spends_the_height_share_on_readable_rows() -> None:
    """Below that, the cap is what gives way — never the screen.

    80% of 16 rows leaves two rows of results, too few for one real row,
    and a clipped row cannot be recovered by keyboard because the list
    scrolls by whole options. So the modal exceeds the share here, up to
    the terminal height, and pays for the rows with its own chrome.
    """
    screen = ActionPaletteScreen(_mixed_category_entries(30))
    app = PaletteHarness(screen)
    async with app.run_test(size=(36, 16)):
        container = screen.query_one("#action-palette")
        results = screen.query_one(OptionList)
        hint = screen.query_one("#action-hint", Static)
        assert container.region.height > 16 * 80 // 100
        assert app.screen.region.contains_region(container.region)
        assert results.size.height >= _READABLE_RESULT_ROWS
        assert container.region.contains_region(hint.region)


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


async def test_a_growing_terminal_gives_the_height_share_back() -> None:
    """Tight chrome is a concession, not a new default.

    A terminal that grows back to 80x24 can afford the 80% cap again, so
    the modal has to take its vertical padding and the results list its
    own border back rather than stay in the compact shape a 16-row
    terminal needed.
    """
    screen = ActionPaletteScreen(_mixed_category_entries(30))
    app = PaletteHarness(screen)
    async with app.run_test(size=(36, 16)) as pilot:
        container = screen.query_one("#action-palette")
        results = screen.query_one(OptionList)
        hint = screen.query_one("#action-hint", Static)
        assert results.compact is True
        await pilot.resize_terminal(80, 24)
        await until(
            pilot,
            lambda: not results.compact and container.region.height <= 24 * 80 // 100,
            label="the height share restored at 80x24",
        )
        assert results.size.height >= _READABLE_RESULT_ROWS
        assert app.screen.region.contains_region(container.region)
        assert container.region.contains_region(hint.region)


async def test_end_then_a_shrinking_terminal_keeps_the_last_result_visible() -> None:
    """A resize under an open palette must not strand the highlight.

    `End` at 80x24 scrolls to an offset that means nothing once 36 columns
    re-wrap every row and 16 rows shrink the viewport: the list keeps the
    old offset and renders an arbitrary middle row while the highlight is
    still the last entry. Re-fitting has to end with the highlighted row
    rendered again - without a keypress and without taking focus off the
    query `Input`.
    """
    screen = ActionPaletteScreen(_rewrapping_entries(30))
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await pilot.press("end")
        assert any("Item 29" in line for line in _viewport_lines(options))
        await pilot.resize_terminal(36, 16)
        await until(
            pilot,
            lambda: any("Item 29" in line for line in _viewport_lines(options)),
            label="the highlighted last result rendered again after the resize",
        )
        assert options.highlighted == options.option_count - 1
        assert any("Item 29" in line for line in _viewport_lines(options))
        assert screen.query_one(Input).has_focus


async def test_end_restores_the_last_result_even_when_the_highlight_cannot_move() -> None:
    """`End` on an already-last highlight must still bring it back on screen.

    `OptionList` only scrolls from its `highlighted` watcher, so pressing
    `End` when the highlight is already the last option changes no reactive
    and scrolls nowhere. After anything moved the viewport away from it -
    here a resize plus an explicit scroll back to the top - the key the
    user reaches for has to work the second time too.
    """
    screen = ActionPaletteScreen(_rewrapping_entries(30))
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await pilot.press("end")
        await pilot.resize_terminal(36, 16)
        await until(
            pilot,
            lambda: any("Item 29" in line for line in _viewport_lines(options)),
            label="the highlighted last result rendered again after the resize",
        )
        options.scroll_to(y=0, animate=False, immediate=True)
        await pilot.pause()
        assert not any("Item 29" in line for line in _viewport_lines(options))
        await pilot.press("end")
        assert options.highlighted == options.option_count - 1
        assert any("Item 29" in line for line in _viewport_lines(options))
        assert screen.query_one(Input).has_focus


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


def _long_owner_reason() -> UnavailableReason:
    """The real refusal `WorkspaceController` answers `g` with.

    Taken from its owner (`read_availability.relationships_reason`) rather
    than retyped here: the palette has to render whatever wording the owner
    actually produces, and this is one of the longest of them.
    """
    reason = relationships_reason(
        loader=False, switching=False, meta=None, kind="pods", selected=False
    )
    assert reason is not None
    return reason


def _derived_unavailable_action_entry(action: str, reason: UnavailableReason) -> list[PaletteEntry]:
    """The real `APP_BINDINGS` row for `action`, refused by its owner."""
    entries = derive_action_entries(
        APP_BINDINGS,
        overrides={},
        availability=lambda _action: ActionAvailability(True, reason),
    )
    return [entry for entry in entries if entry.id == f"action:{action}"]


async def test_a_narrow_terminal_shows_a_whole_command_row_not_just_its_first_wrap() -> None:
    """36x16 must still show the row the user is on, all of it.

    `:proposals` is one of the longest real catalog rows: at 36 columns its
    category, title and `:` spelling wrap over several lines before the
    description even starts. A viewport of two content rows renders the
    first wrap and clips the rest, so the trigger the user needs in order
    to run the command - and the description that says what it does - are
    unreachable by keyboard. Both have to be in the viewport.
    """
    screen = ActionPaletteScreen(_derived_command_entry("proposals"))
    app = PaletteHarness(screen)
    async with app.run_test(size=(36, 16)):
        options = screen.query_one(OptionList)
        visible = _visible_text(options)
        assert ":proposals" in _heading(options)
        assert _heading(options) in visible
        assert _second_line(options) in visible
        assert screen.query_one(Input).has_focus


async def test_a_narrow_terminal_shows_a_whole_unavailable_reason() -> None:
    """An unavailable row is only useful if its reason is readable.

    The row is disabled and inert, so no keystroke can scroll it into view;
    at 36 columns the owner's real refusal wraps over several lines, and
    all of them - plus the trigger that would have run it - must be inside
    the results viewport.
    """
    reason = _long_owner_reason()
    entries = _derived_unavailable_action_entry("relationships", reason)
    screen = ActionPaletteScreen(entries)
    app = PaletteHarness(screen)
    async with app.run_test(size=(36, 16)):
        options = screen.query_one(OptionList)
        assert options.get_option_at_index(0).disabled is True
        visible = _visible_text(options)
        assert _heading(options) in visible
        assert _second_line(options) == f"Unavailable: {reason.message}"
        assert _second_line(options) in visible
        assert screen.query_one(Input).has_focus


async def _filter_to_single_row(pilot: Pilot[None], options: OptionList, query: str) -> None:
    """Type `query` and wait until it has narrowed the list to one row."""
    await pilot.press(*query)
    await until(
        pilot,
        lambda: options.option_count == 1,
        label=f"the results filtered to the single {query!r} row",
    )


async def test_a_shrinking_terminal_shows_a_whole_filtered_command_row() -> None:
    """The narrow row has to survive the resize, not just a narrow start.

    Reaching 36x16 by shrinking is not the same layout path as opening
    there: the modal switches to tight chrome mid-life, which widens every
    row by the list border it gives up. The list re-wraps against the old,
    narrower width first - and the line heights it caches then outlive the
    settled geometry, so the viewport ends up one row shorter than the row
    it is showing and the last words of `:proposals` are never composited.
    No keystroke recovers them, because the list scrolls by whole options.
    """
    screen = ActionPaletteScreen(_derived_command_entry("proposals"))
    app = PaletteHarness(screen)
    async with app.run_test(size=(36, 24)) as pilot:
        options = screen.query_one(OptionList)
        await _filter_to_single_row(pilot, options, "proposals")
        assert _second_line(options) in _visible_text(options)
        await pilot.resize_terminal(36, 16)
        await until(
            pilot,
            lambda: _second_line(options) in _visible_text(options),
            label="the whole filtered command row composited after the resize",
        )
        visible = _visible_text(options)
        assert ":proposals" in _heading(options)
        assert _heading(options) in visible
        assert _second_line(options) in visible
        assert screen.query_one(Input).has_focus


async def test_a_shrinking_terminal_shows_a_whole_filtered_unavailable_reason() -> None:
    """The same resize path, on the row no keystroke can rescue.

    An unavailable row is disabled, so it is never highlighted and never
    scrolled to: if the resize leaves the viewport shorter than the row,
    the owner's refusal is simply cut off. It must be composited whole -
    and the row must stay inert (Enter runs nothing) with the query
    `Input` still focused, exactly as it is at a narrow start.
    """
    reason = _long_owner_reason()
    entries = _derived_unavailable_action_entry("relationships", reason)
    screen = ActionPaletteScreen(entries)
    app = PaletteHarness(screen)
    async with app.run_test(size=(36, 24)) as pilot:
        options = screen.query_one(OptionList)
        await _filter_to_single_row(pilot, options, "relationships")
        assert _second_line(options) in _visible_text(options)
        await pilot.resize_terminal(36, 16)
        await until(
            pilot,
            lambda: _second_line(options) in _visible_text(options),
            label="the whole filtered unavailable reason composited after the resize",
        )
        visible = _visible_text(options)
        assert options.get_option_at_index(0).disabled is True
        assert _heading(options) in visible
        assert _second_line(options) == f"Unavailable: {reason.message}"
        assert _second_line(options) in visible
        await pilot.press("enter")
        assert app.results == []
        assert screen.query_one(Input).has_focus


def _derived_catalog(refused: tuple[str, UnavailableReason] | None = None) -> list[PaletteEntry]:
    """The whole catalog the app itself opens the palette with.

    `derive_palette_entries` is `KorvidApp._palette_entries`' own
    composition, so a query against this list leaves *several* rows and the
    results list keeps a scrollbar - the state the single-row fixtures
    above can never reach, and the one the scrollbar's own two columns make
    different. `refused` names one action its owner turns down, with the
    owner's real wording.
    """

    def availability(action: str) -> ActionAvailability:
        if refused is not None and action == refused[0]:
            return ActionAvailability(True, refused[1])
        return ActionAvailability.enabled()

    return derive_palette_entries(
        APP_BINDINGS,
        COMMANDS,
        overrides={},
        availability=availability,
        command_availability=lambda _command: ActionAvailability.enabled(),
    )


def _index_of(options: OptionList, entry_id: str) -> int:
    """Where `entry_id` is rendered, or fail naming what is there instead."""
    ids = [options.get_option_at_index(index).id for index in range(options.option_count)]
    assert entry_id in ids, f"{entry_id} is not among the rendered rows: {ids}"
    return ids.index(entry_id)


async def _highlight_entry(pilot: Pilot[None], options: OptionList, entry_id: str) -> int:
    """Walk the highlight down onto `entry_id` with `Down`, and return its index."""
    index = _index_of(options, entry_id)
    for _ in range(options.option_count):
        if options.highlighted == index:
            break
        await pilot.press("down")
    assert options.highlighted == index
    return index


def _separator_rules(options: OptionList) -> list[int]:
    """Viewport lines that are a category rule, by line number.

    The rule is the last line `OptionList` renders for a row that ends a
    category, so it is also the first line a row that measures one line
    short loses - and the only thing separating two categories once it is
    gone.
    """
    return [
        number
        for number, line in enumerate(_viewport_lines(options))
        if line.strip() and set(line.strip()) == {"─"}
    ]


async def test_a_shrinking_terminal_shows_the_whole_selected_row_among_many_results() -> None:
    """A scrollbar makes the resize a different width, and it must not clip.

    One filtered row leaves the list no scrollbar, so the width it measures
    its rows against and the width it renders them at are the same number
    either way. A real query does not: the catalog `Ctrl-P` opens with
    leaves several rows, the list keeps a vertical scrollbar for them, and
    the two columns that scrollbar takes are exactly the difference between
    a row measured while the list is momentarily empty and the same row
    rendered afterwards. `:proposals` then reports one line fewer than it
    draws and "write proposals" is never composited - and because
    `OptionList` scrolls by whole options, walking back onto the row with
    `Home` and `Down` renders the same clipped row again.
    """
    screen = ActionPaletteScreen(_derived_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await pilot.press("p")
        await until(
            pilot,
            lambda: options.option_count > 1,
            label="the results filtered to several 'p' rows",
        )
        index = await _highlight_entry(pilot, options, "command:proposals")
        await pilot.resize_terminal(36, 16)
        await until(
            pilot,
            lambda: _second_line(options, index) in _visible_text(options),
            label="the whole highlighted row composited after the resize",
        )
        visible = _visible_text(options)
        assert ":proposals" in _heading(options, index)
        assert _heading(options, index) in visible
        assert _second_line(options, index) in visible
        await pilot.press("home")
        assert await _highlight_entry(pilot, options, "command:proposals") == index
        reasserted = _visible_text(options)
        assert _heading(options, index) in reasserted
        assert _second_line(options, index) in reasserted
        assert screen.query_one(Input).has_focus


async def test_a_shrinking_terminal_keeps_an_unavailable_row_whole_among_many_results() -> None:
    """The same width transition on the row no keystroke can rescue.

    An unavailable row is disabled, so nothing ever scrolls to it: whatever
    the resize leaves of it is all the user gets. Among several results it
    loses its last line to the scrollbar's two columns the same way - here
    the rule that closes its category, so the refused `Table` row and the
    `Commands` rows below it run together with no boundary between them.
    The refusal itself, the trigger, and that rule all have to be
    composited, and the row has to stay inert.
    """
    reason = _long_owner_reason()
    screen = ActionPaletteScreen(_derived_catalog(("relationships", reason)))
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await pilot.press("r", "e", "l")
        await until(
            pilot,
            lambda: options.option_count > 1,
            label="the results filtered to several 'rel' rows",
        )
        index = _index_of(options, "action:relationships")
        assert options.get_option_at_index(index).disabled is True
        await pilot.resize_terminal(36, 16)
        await until(
            pilot,
            lambda: bool(_separator_rules(options)),
            label="the refused row's category rule composited after the resize",
        )
        visible = _visible_text(options)
        assert _heading(options, index) in visible
        assert _second_line(options, index) == f"Unavailable: {reason.message}"
        assert _second_line(options, index) in visible
        await pilot.press("enter")
        assert app.results == []
        assert screen.query_one(Input).has_focus


#: The terminal the narrowing finding reproduces at. 38 columns is wide
#: enough that the modal keeps its normal chrome — border, padding, and a
#: results list with its own border and reserved scrollbar gutter — and
#: narrow enough that a real command row wraps to six lines there. Any
#: results viewport derived from a *measurement* of that row rather than
#: from the modal's own height budget lands one line short of it.
_NARROWING_SIZE = (38, 24)

#: Two real `:` commands whose description reaches the last line of the
#: row at `_NARROWING_SIZE`: `:ctx` ends "…to switch clusters" and `:tp`
#: ends "(also :telepresence)". Each is the whole reason its row exists,
#: and each is what a viewport one line short drops.
_NARROWING_QUERIES = [":ctx", ":tp"]


@pytest.mark.parametrize("query", _NARROWING_QUERIES, ids=["ctx", "tp"])
async def test_a_narrowing_terminal_composites_a_whole_filtered_command_row(query: str) -> None:
    """A narrower terminal must not cut the row the query left behind.

    The modal opens at 80x24 over the catalog `Ctrl-P` really opens, the
    query narrows it to one row, and the terminal narrows to 38 columns
    where that row wraps to six lines. A results viewport sized from what
    the row *measured* at some other width renders five of them and drops
    the last — and `OptionList` scrolls by whole options, so no keystroke
    brings the missing line back. The viewport has to come from the
    modal's own height budget instead, so the whole row is composited and
    the query `Input` still has focus.
    """
    screen = ActionPaletteScreen(_derived_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await _filter_to_single_row(pilot, options, query)
        await pilot.resize_terminal(*_NARROWING_SIZE)
        await until(
            pilot,
            lambda: _second_line(options) in _visible_text(options),
            label=f"the whole {query} row composited after the narrowing resize",
        )
        visible = _visible_text(options)
        assert query in _heading(options)
        assert _heading(options) in visible
        assert _second_line(options) in visible
        assert screen.query_one(Input).has_focus


async def test_a_narrowing_terminal_composites_a_whole_unavailable_reason() -> None:
    """The same narrowing, on the row no keystroke can rescue.

    A refused row is disabled: it is never highlighted and never scrolled
    to, so whatever the resize leaves of it is all the user gets. Typing
    the action's own id narrows the catalog to that one row, the owner's
    real refusal wraps well past one line at 38 columns, and every line of
    it — plus the heading that says which key would have run it — has to
    be composited, with the row still inert and the query `Input` still
    focused.
    """
    reason = _long_owner_reason()
    screen = ActionPaletteScreen(_derived_catalog(("relationships", reason)))
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await _filter_to_single_row(pilot, options, "relationships")
        assert options.get_option_at_index(0).disabled is True
        await pilot.resize_terminal(*_NARROWING_SIZE)
        await until(
            pilot,
            lambda: _second_line(options) in _visible_text(options),
            label="the whole refused row composited after the narrowing resize",
        )
        visible = _visible_text(options)
        assert _heading(options) in visible
        assert _second_line(options) == f"Unavailable: {reason.message}"
        assert _second_line(options) in visible
        await pilot.press("enter")
        assert app.results == []
        assert screen.query_one(Input).has_focus


#: The two real integration rows a base install greys out, each at the
#: narrow terminal the design supports: `:mcp` with the [mcp] extra absent
#: at 36x16, and `:tp` with no telepresence CLI at 36x24. Their reasons
#: are the palette's whole content for those rows — a row whose refusal
#: only half fits says nothing the user can act on.
_UNAVAILABLE_INTEGRATIONS = [("mcp", (36, 16)), ("tp", (36, 24))]


def _integration_reason(command: str) -> UnavailableReason:
    """The real `IntegrationController` refusal for `:mcp` / `:tp`.

    Asked of the owner itself, in a session that has neither integration —
    the state a base install is in — rather than retyped here: the palette
    has to fit whatever wording that owner actually answers with.
    """
    controller = IntegrationHarness(mcp=None, telepresence=None).controller
    reason = (
        controller.mcp_unavailable_reason()
        if command == "mcp"
        else controller.telepresence_unavailable_reason()
    )
    assert reason is not None
    return reason


def _derived_catalog_refusing_command(
    command: str, reason: UnavailableReason
) -> list[PaletteEntry]:
    """The whole real catalog, with one `:` command refused by its owner."""
    return derive_palette_entries(
        APP_BINDINGS,
        COMMANDS,
        overrides={},
        availability=lambda _action: ActionAvailability.enabled(),
        command_availability=lambda text: (
            ActionAvailability(True, reason) if text == command else ActionAvailability.enabled()
        ),
    )


@pytest.mark.parametrize(
    ("command", "size"), _UNAVAILABLE_INTEGRATIONS, ids=["mcp-36x16", "tp-36x24"]
)
async def test_a_real_unavailable_integration_row_fits_a_narrow_terminal(
    command: str, size: tuple[int, int]
) -> None:
    """A base install's greyed-out `:mcp` / `:tp` row must be readable.

    These two rows are the palette's only view of an integration this
    session does not have, and they are disabled: no keystroke highlights
    them, and `OptionList` scrolls by whole options, so whatever the
    viewport holds is all the user ever gets. At the narrow terminals the
    design supports, the whole row — the category, title and `:` spelling
    that say which command it is, and every word of the owner's reason —
    has to be composited inside the results viewport, with the row inert
    and the query `Input` still focused.
    """
    reason = _integration_reason(command)
    screen = ActionPaletteScreen(_derived_catalog_refusing_command(command, reason))
    app = PaletteHarness(screen)
    async with app.run_test(size=size) as pilot:
        options = screen.query_one(OptionList)
        await _filter_to_single_row(pilot, options, f":{command}")
        assert options.get_option_at_index(0).disabled is True
        visible = _visible_text(options)
        heading = _heading(options)
        assert "Commands" in heading
        assert f":{command}" in heading
        assert heading in visible
        assert _second_line(options) == f"Unavailable: {reason.message}"
        assert _second_line(options) in visible
        await pilot.press("enter")
        assert app.results == []
        assert screen.query_one(Input).has_focus


#: The modal's exact height and its results viewport at three terminals,
#: as the bounded rule spends them. 80x24: the whole modal is 80% of the
#: terminal (19 rows), of which the results keep 9. 80x40: the 18-row
#: result cap binds first, so the modal is 28 — well inside that
#: terminal's 32-row share. 36x16: the share (12) cannot fund eight
#: readable rows, so the modal drops its padding and the list its border
#: and grows to 14 — over the share, still inside the terminal.
_HEIGHT_BUDGETS = [
    ((80, 24), 19, 9),
    ((80, 40), 28, 18),
    ((36, 16), 14, 8),
]


@pytest.mark.parametrize("query", ["", ":ctx"], ids=["whole-catalog", "one-row"])
@pytest.mark.parametrize(
    ("size", "modal_rows", "result_rows"),
    _HEIGHT_BUDGETS,
    ids=["80x24", "80x40", "36x16"],
)
async def test_the_modal_takes_an_exact_responsive_height(
    size: tuple[int, int], modal_rows: int, result_rows: int, query: str
) -> None:
    """The modal's height is a budget it decides, not one its rows report.

    Every terminal gets one exact height — 80% of it where that is
    affordable, the eighteen-row result cap on a tall one, and a floor
    paid for out of the modal's own chrome on a short one — and the
    results list is whatever is left inside it. The same numbers have to
    hold whether the list is the whole catalog or a query's single
    surviving row: a height that follows the content is the defect, not
    the rule.
    """
    screen = ActionPaletteScreen(_derived_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=size) as pilot:
        container = screen.query_one("#action-palette")
        results = screen.query_one(OptionList)
        hint = screen.query_one("#action-hint", Static)
        if query:
            await _filter_to_single_row(pilot, results, query)
        await until(
            pilot,
            lambda: container.region.height == modal_rows,
            label=f"the modal sized to its {modal_rows}-row budget at {size}",
        )
        assert container.region.height == modal_rows
        assert results.size.height == result_rows
        assert app.screen.region.contains_region(container.region)
        assert container.region.contains_region(hint.region)


async def test_the_modal_keeps_its_height_when_a_query_leaves_one_row() -> None:
    """The budget belongs to the terminal, not to what the list holds.

    A height that follows the results is the whole defect: one short row
    shrinks the modal, and the next row the user types into it no longer
    fits. Filtering the full catalog down to a single row must leave the
    modal exactly as tall as it was, with the results viewport unchanged
    and the one surviving row composited whole.
    """
    screen = ActionPaletteScreen(_derived_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=_NARROWING_SIZE) as pilot:
        container = screen.query_one("#action-palette")
        options = screen.query_one(OptionList)
        full_height = container.region.height
        full_viewport = options.size.height
        await _filter_to_single_row(pilot, options, ":ctx")
        assert container.region.height == full_height
        assert options.size.height == full_viewport
        visible = _visible_text(options)
        assert _heading(options) in visible
        assert _second_line(options) in visible


async def test_a_run_of_resizes_leaves_the_last_size_showing_a_whole_row() -> None:
    """Several resizes in a row must leave the last one in charge.

    Dragging a terminal corner walks the palette through a run of sizes,
    each of them re-budgeting the modal. Only the final size's budget may
    survive: the palette has to end there, with the filtered row
    composited whole, rather than on the geometry of a size it passed
    through on the way.
    """
    screen = ActionPaletteScreen(_derived_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        container = screen.query_one("#action-palette")
        options = screen.query_one(OptionList)
        await _filter_to_single_row(pilot, options, ":ctx")
        await pilot.resize_terminal(*_NARROWING_SIZE)
        await pilot.resize_terminal(80, 40)
        await pilot.resize_terminal(*_NARROWING_SIZE)
        await until(
            pilot,
            lambda: container.region.height == 19,
            label="the modal settled on the final size's budget",
        )
        await until(
            pilot,
            lambda: _second_line(options) in _visible_text(options),
            label="the whole filtered row composited at the final size",
        )
        visible = _visible_text(options)
        assert container.region.height == 19
        assert _heading(options) in visible
        assert _second_line(options) in visible
        assert screen.query_one(Input).has_focus


async def test_a_resize_scroll_that_lands_after_dismissal_does_nothing() -> None:
    """The scroll a resize orders can outlive the palette.

    It is scheduled for after the next refresh, so a resize immediately
    before `Esc` leaves the callback queued against a screen whose
    children Textual has already taken away - while the screen object
    itself still reports as mounted. Running it then has to be inert,
    rather than raise `NoMatches` out of a Textual callback and take the
    app down behind an already-closed modal.
    """
    screen = ActionPaletteScreen(_derived_command_entry("proposals"))
    app = PaletteHarness(screen)
    async with app.run_test(size=(36, 24)) as pilot:
        await pilot.press("escape")
        await until(pilot, lambda: app.results == [None], label="the palette dismissed")
        assert not screen.query(OptionList)
        screen._reveal_after_resize()
        await pilot.pause()
        assert app.results == [None]
        assert not screen.query(OptionList)


async def _filter_to_first_row(
    pilot: Pilot[None], options: OptionList, query: str, entry_id: str
) -> None:
    """Type `query` and wait until `entry_id` is the top result.

    The narrow-row findings are about what the *viewport* holds, so the
    row under test has to be the first one - and a real query usually
    leaves more than one row behind it, which is the state a scrollbar
    exists in.
    """
    await pilot.press(*query)
    await until(
        pilot,
        lambda: options.option_count > 0 and options.get_option_at_index(0).id == entry_id,
        label=f"{entry_id} ranked first for {query!r}",
    )


#: A real node name of the shape a managed cluster hands out: a VMSS
#: instance under a fully-qualified private DNS zone. Nothing about it is
#: unusual - it is a legal DNS subdomain well inside Kubernetes' 253-byte
#: limit - and a palette reason that interpolates one cannot fit a 36-column
#: row whatever the viewport does.
_LONG_NODE = "aks-userpool-41763029-vmss000003.internal.cloudapp.kube-prod-eastus2.example.net"


def _cross_node_drain_reason() -> UnavailableReason:
    """The real refusal the drain key's owner answers a palette probe with
    while some *other* node is being drained.

    Asked of `WriteAvailability` itself rather than retyped here: what the
    row has to fit is whatever that owner actually says.
    """
    return WriteAvailability.other_drain_reason()


async def test_a_cross_node_drain_row_fits_a_narrow_terminal() -> None:
    """The drain row's refusal names a node the user is not looking at.

    While a drain runs, pressing the drain key on any other row is refused
    with the draining node's name and the instruction to press it on that
    node instead - a sentence whose length is cluster data. As a *row* it
    is unreadable: at 36 columns a real managed-cluster node name alone
    outruns the viewport, the row is disabled so no keystroke highlights
    or scrolls it, and the list scrolls by whole options. The probe's
    reason therefore states the bounded fact, and the keypress keeps the
    name in its toast (issue #388, round 8).
    """
    reason = _cross_node_drain_reason()
    assert _LONG_NODE not in reason.message
    assert _LONG_NODE in WriteAvailability.other_drain_detail(_LONG_NODE)
    screen = ActionPaletteScreen(_derived_catalog(("drain_node", reason)))
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await _filter_to_first_row(pilot, options, "Drain", "action:drain_node")
        await pilot.resize_terminal(36, 24)
        await until(
            pilot,
            lambda: _second_line(options) in _visible_text(options),
            label="the whole refused drain row composited at 36x24",
        )
        visible = _visible_text(options)
        assert options.get_option_at_index(0).disabled is True
        heading = _heading(options)
        assert "Drain node" in heading
        assert "Shift-D" in heading  # the trigger the refusal is about
        assert heading in visible
        assert _second_line(options) == f"Unavailable: {reason.message}"
        assert _second_line(options) in visible
        await pilot.press("enter")
        assert app.results == []
        assert screen.query_one(Input).has_focus


async def test_the_detailed_cross_node_drain_sentence_would_not_have_fitted() -> None:
    """Why the split exists, asserted rather than asserted-about: the
    sentence the keypress notifies - the one the row used to carry - does
    not fit the row it would have to be composited into."""
    detail = WriteAvailability.other_drain_detail(_LONG_NODE)
    screen = ActionPaletteScreen(
        _derived_catalog(("drain_node", UnavailableReason(AvailabilityCode.PROTECTED_UI, detail)))
    )
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await _filter_to_first_row(pilot, options, "Drain", "action:drain_node")
        await pilot.resize_terminal(36, 24)
        await pilot.pause()
        assert _LONG_NODE in _second_line(options)
        assert _second_line(options) not in _visible_text(options)
