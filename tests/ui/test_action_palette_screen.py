"""ActionPaletteScreen: the keyboard-first modal (issue #388 task 5).

Standalone modal tests only: no `Ctrl-P` open binding and no app dispatch
live here (a later task pushes this screen from `KorvidApp` and routes its
stable `entry.id` result through `run_action`/`parse_command`). Every
scenario here drives the screen through a minimal host `App` and public
Textual widgets/APIs — no subclassing of Textual's private `CommandPalette`
internals.
"""

from __future__ import annotations

import subprocess
import sys
from itertools import pairwise
from pathlib import Path

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
from korvid.ui.widgets.action_palette import ActionPaletteScreen, _pull_back_rows
from korvid.ui.write_availability import WriteAvailability

from .test_helm_actions import make_app as make_helm_app
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
        options = screen.query_one(OptionList)
        assert _says_it_cannot_run(options, 0)
        assert "Unavailable: select a node first" in str(options.get_option_at_index(0).prompt)
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


#: What a refused row's second line starts with. The row carries its own
#: refusal, so "can this run?" is a question the *rendered row* answers -
#: never one `Option.disabled` answers on its behalf, because a Textual
#: option that is disabled is also a row `OptionList` refuses to draw a
#: cursor on (`render_line` picks the disabled component class before it
#: ever looks at `highlighted`).
_REFUSED_PREFIX = "unavailable:"


def _says_it_cannot_run(options: OptionList, index: int) -> bool:
    """Whether row `index` tells the user, in its own words, that it can't run."""
    lines = str(options.get_option_at_index(index).prompt).splitlines()
    return len(lines) > 1 and lines[1].casefold().startswith(_REFUSED_PREFIX)


def _refused_rows(options: OptionList) -> list[bool]:
    """Which rendered rows say they cannot run, in rendered order."""
    return [_says_it_cannot_run(options, index) for index in range(options.option_count)]


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


async def test_an_action_row_shows_the_topbar_key_as_the_character_it_types() -> None:
    """A row's trigger is the key the user has to press, spelled the way a
    keyboard spells it.

    `toggle_topbar` is bound to Textual's `tilde`, and the palette resolves
    every trigger through the same shared `key_label` the help overlay
    uses - so the row has to read `~`, never the Textual key name, in the
    default binding and through a remap onto the same key.
    """
    screen = ActionPaletteScreen(_derived_action_entry("toggle_topbar"))
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)):
        heading = _heading(screen.query_one(OptionList))
        assert "~" in heading
        assert "tilde" not in heading


async def test_a_remap_onto_the_topbar_key_shows_the_same_character() -> None:
    """A `keybindings:` remap reaches `key_label` by the same route, so a
    key remapped *onto* `tilde` shows `~` too."""
    screen = ActionPaletteScreen(_derived_action_entry("describe", describe="tilde"))
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)):
        heading = _heading(screen.query_one(OptionList))
        assert "~" in heading
        assert "tilde" not in heading


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

    At 36 columns the owner's real refusal wraps over several lines, and a
    viewport shorter than the row cannot be scrolled *inside* the row: a
    cursor reveals whole rows, so a line the budget left out is a line no
    keystroke brings back. All of them - plus the trigger that would have
    run it - must be inside the results viewport.
    """
    reason = _long_owner_reason()
    entries = _derived_unavailable_action_entry("relationships", reason)
    screen = ActionPaletteScreen(entries)
    app = PaletteHarness(screen)
    async with app.run_test(size=(36, 16)):
        options = screen.query_one(OptionList)
        assert _says_it_cannot_run(options, 0)
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

    If the resize leaves the viewport shorter than the row, the owner's
    refusal is simply cut off: revealing the row again only aligns its
    top, so the lines past the viewport stay gone. It must be composited
    whole - and the row must stay inert (Enter runs nothing) with the
    query `Input` still focused, exactly as it is at a narrow start.
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
        assert _says_it_cannot_run(options, 0)
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

    Whatever the resize leaves of a row is all the user gets - a reveal
    aligns the row's top, and the list scrolls between rows rather than
    inside one. Among several results a refused row
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
        assert _says_it_cannot_run(options, index)
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

    Whatever the resize leaves of a row is all the user gets: a reveal
    aligns the row's top, and the list scrolls between rows rather than
    inside one. Typing
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
        assert _says_it_cannot_run(options, 0)
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
    session does not have. A row taller than the viewport cannot be
    rescued by any keystroke - revealing it aligns its top, and
    `OptionList` scrolls between rows rather than inside one - so whatever
    the viewport holds is all the user ever gets. At the narrow terminals the
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
        assert _says_it_cannot_run(options, 0)
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
    outruns the viewport, and no keystroke rescues a row taller than the
    viewport - a reveal aligns its top, and the list scrolls between rows
    rather than inside one. The probe's
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
        assert _says_it_cannot_run(options, 0)
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


#: A node name at Kubernetes' own limit. Node names are DNS subdomains
#: (RFC 1123): at most 253 characters, in dot-separated labels of at most
#: 63. Nothing about this one is exotic - it is the shape a managed
#: cluster's fully-qualified instance name already has, taken to the
#: longest value the API server accepts, because korvid controls neither
#: the length nor the content.
_MAX_NODE = ".".join(
    (
        "aks-userpool-41763029-vmss000003".ljust(63, "x"),
        "internal-cloudapp-private-dns-zone".ljust(63, "y"),
        "kube-prod-eastus2-node-resource-group".ljust(63, "z"),
        "example-customer-internal-net".ljust(61, "w"),
    )
)

#: Palette action -> the title `_humanize_action_id` derives for it, which
#: is the row heading the refusal has to be composited beside.
_SELECTED_NODE_ACTIONS = (
    ("cordon_node", "Cordon node", "c"),
    ("uncordon_node", "Uncordon node", "u"),
)


def test_the_fixture_node_name_is_one_kubernetes_would_accept() -> None:
    """A fixture no cluster could produce would prove nothing about rows.

    253 characters, every label within 63, and only the characters a DNS
    subdomain allows - the longest name `nodes/<name>` can legally carry.
    """
    labels = _MAX_NODE.split(".")
    assert len(_MAX_NODE) == 253
    assert all(0 < len(label) <= 63 for label in labels)
    assert all(label.replace("-", "").isalnum() for label in labels)
    assert all(not label.startswith("-") and not label.endswith("-") for label in labels)


def test_the_two_drain_refusals_are_both_bounded_and_distinct() -> None:
    """Cordon/uncordon are refused while *this* row is being drained; the
    drain key is refused while *another* node is. Both rows are bounded,
    and they have to stay distinguishable, because they ask for different
    things: wait here, or go to the node that is draining."""
    selected = WriteAvailability.selected_drain_reason()
    other = WriteAvailability.other_drain_reason()
    assert _MAX_NODE not in selected.message
    assert _MAX_NODE not in other.message
    assert selected.message != other.message
    assert _MAX_NODE in WriteAvailability.drain_in_progress_detail(_MAX_NODE)


@pytest.mark.parametrize(("action", "title", "trigger"), _SELECTED_NODE_ACTIONS)
async def test_a_selected_node_drain_row_fits_a_narrow_terminal(
    action: str, title: str, trigger: str
) -> None:
    """Cordon and uncordon are refused while the row under the cursor is
    the node being drained - the drain owns its schedulable state until it
    finishes or is cancelled.

    That refusal used to name the node, and a node name has no bounded
    length: at 253 characters it outruns a 36-column row many times over,
    and no keystroke rescues a row taller than the viewport - a reveal
    aligns its top, and the list scrolls between rows rather than inside
    one. The probe's reason therefore states the
    bounded fact, and the keypress keeps the name in its toast (#388,
    round 9).
    """
    reason = WriteAvailability.selected_drain_reason()
    screen = ActionPaletteScreen(_derived_catalog((action, reason)))
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await _filter_to_first_row(pilot, options, title.split()[0], f"action:{action}")
        await pilot.resize_terminal(36, 24)
        await until(
            pilot,
            lambda: _second_line(options) in _visible_text(options),
            label=f"the whole refused {action} row composited at 36x24",
        )
        visible = _visible_text(options)
        assert _says_it_cannot_run(options, 0)
        heading = _heading(options)
        assert title in heading
        assert trigger in heading  # the trigger the refusal is about
        assert heading in visible
        assert _second_line(options) == f"Unavailable: {reason.message}"
        assert _second_line(options) in visible
        await pilot.press("enter")
        assert app.results == []
        assert screen.query_one(Input).has_focus


@pytest.mark.parametrize(("action", "title", "trigger"), _SELECTED_NODE_ACTIONS)
async def test_the_detailed_selected_node_sentence_would_not_have_fitted(
    action: str, title: str, trigger: str
) -> None:
    """Why this split exists, asserted rather than asserted-about: the
    sentence `_cordon_action` notifies - the one the row used to carry -
    does not fit the row it would have to be composited into."""
    detail = WriteAvailability.drain_in_progress_detail(_MAX_NODE)
    screen = ActionPaletteScreen(
        _derived_catalog((action, UnavailableReason(AvailabilityCode.PROTECTED_UI, detail)))
    )
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await _filter_to_first_row(pilot, options, title.split()[0], f"action:{action}")
        await pilot.resize_terminal(36, 24)
        await pilot.pause()
        assert trigger in _heading(options)
        assert _MAX_NODE in _second_line(options)
        assert _second_line(options) not in _visible_text(options)


#: The four real `APP_BINDINGS` helm actions one owner refuses together.
#: A base install without the helm binary greys out all four at once, which
#: is what makes them the finding's own repro: every surviving row is one
#: that cannot run, so there is no runnable row for the navigation to fall
#: back on, and the four of them outrun the viewport at both terminals.
_HELM_ACTIONS = ("helm_install", "helm_upgrade", "helm_rollback", "helm_history")

#: The title `_humanize_action_id` derives for the *last* of them, which is
#: the row past the fold at both terminals under test.
_LAST_HELM_TITLE = "Helm history"


def _helm_missing_reason() -> UnavailableReason:
    """The real refusal the helm keys' owner answers a probe with when this
    session found no helm binary.

    Asked of `HelmController.cli_unavailable_reason` rather than retyped
    here: what has to be reachable is whatever wording that owner actually
    produces, and this one wraps to two lines at 80 columns and to three at
    36 - which is exactly why four such rows outgrow the viewport.
    """
    reason = make_helm_app(helm=None)._helm_ctl.cli_unavailable_reason()
    assert reason is not None
    return reason


def _helmless_catalog() -> list[PaletteEntry]:
    """The whole real catalog with every helm key refused by its owner."""
    reason = _helm_missing_reason()

    def availability(action: str) -> ActionAvailability:
        return (
            ActionAvailability(True, reason)
            if action in _HELM_ACTIONS
            else ActionAvailability.enabled()
        )

    return derive_palette_entries(
        APP_BINDINGS,
        COMMANDS,
        overrides={},
        availability=availability,
        command_availability=lambda _command: ActionAvailability.enabled(),
    )


def _unwrapped_text(options: OptionList) -> str:
    """Every character the viewport is showing, with the wraps closed up.

    `_visible_text` rejoins words, which answers the question whenever a
    row breaks at a space. A 36-column row does not always: the helm
    owner's refusal breaks *inside*
    `install/upgrade/rollback/uninstall`, and no space-preserving rejoin
    can find that word again. Dropping the whitespace from both sides of
    the comparison asks the only question that survives either break - are
    these characters, in this order, on screen - without pinning where the
    terminal chose to wrap.
    """
    return "".join(_viewport_lines(options)).replace(" ", "")


def _assert_row_composited(options: OptionList, index: int) -> None:
    """Fail unless row `index` is on screen whole - heading and second line.

    Both halves matter to the finding: the heading carries the trigger the
    row is about, and the second line carries the owner's reason. A row
    that can be highlighted but not read is not reachable in any sense the
    user would recognise.
    """
    composited = _unwrapped_text(options)
    heading = _heading(options, index).replace(" ", "")
    second = _second_line(options, index).replace(" ", "")
    assert heading in composited, f"row {index} heading is not composited: {heading!r}"
    assert second in composited, f"row {index} second line is not composited: {second!r}"


async def _helm_results(pilot: Pilot[None], options: OptionList) -> None:
    """Type `helm` and wait until only the four refused helm rows are left."""
    await pilot.press("h", "e", "l", "m")
    await until(
        pilot,
        lambda: options.option_count == len(_HELM_ACTIONS),
        label="the results filtered to the four refused helm rows",
    )
    assert all(_refused_rows(options))


@pytest.mark.parametrize("size", _LAYOUT_SIZES, ids=_LAYOUT_IDS)
async def test_arrows_reach_every_unavailable_row_past_the_fold(size: tuple[int, int]) -> None:
    """Down must walk onto a row that cannot run, not step over the list.

    A base install without helm leaves `helm` matching four rows, all of
    them refused by the same owner. Their reason wraps, so the four rows
    are twelve lines at 80x24 against a nine-row viewport and twenty
    against eight at 36x16: the last row - `Helm history`, and the reason
    that says why it is greyed out - starts below the fold. Textual's
    navigation moves between *enabled* options only, so marking these rows
    disabled left every arrow press with nothing to land on: the highlight
    stayed unset, the viewport stayed at the top, and no keystroke could
    ever composite that row. They are ordinary navigable rows instead -
    refused by the entry behind them, not by the widget.
    """
    screen = ActionPaletteScreen(_helmless_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=size) as pilot:
        options = screen.query_one(OptionList)
        await _helm_results(pilot, options)
        assert options.virtual_size.height > options.scrollable_content_region.height
        for index in range(1, options.option_count):
            await pilot.press("down")
            assert options.highlighted == index
            _assert_row_composited(options, index)
        assert _LAST_HELM_TITLE in _heading(options, options.option_count - 1)
        for index in reversed(range(options.option_count - 1)):
            await pilot.press("up")
            assert options.highlighted == index
            _assert_row_composited(options, index)
        await pilot.press("enter")
        assert app.results == []
        assert screen.query_one(Input).has_focus


@pytest.mark.parametrize("size", _LAYOUT_SIZES, ids=_LAYOUT_IDS)
async def test_end_and_home_reach_the_edge_unavailable_rows(size: tuple[int, int]) -> None:
    """End belongs to the list's last row, not to its last *runnable* row.

    With every surviving row refused there was no enabled option at all,
    so `find_last_enabled` answered None and `End` did nothing: the row
    the user pressed it to read stayed below the fold. Every row is
    navigable now, so both edges have to land, composite the row whole,
    and stay inert.
    """
    screen = ActionPaletteScreen(_helmless_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=size) as pilot:
        options = screen.query_one(OptionList)
        await _helm_results(pilot, options)
        last = options.option_count - 1
        await pilot.press("end")
        assert options.highlighted == last
        _assert_row_composited(options, last)
        await pilot.press("enter")
        assert app.results == []
        await pilot.press("home")
        assert options.highlighted == 0
        _assert_row_composited(options, 0)
        assert screen.query_one(Input).has_focus


@pytest.mark.parametrize("size", _LAYOUT_SIZES, ids=_LAYOUT_IDS)
async def test_page_keys_walk_the_unavailable_rows_to_both_edges(
    size: tuple[int, int],
) -> None:
    """A page key must move across refused rows too, and reveal where it lands.

    `OptionList._move_page` looks for the next *enabled* option from the
    line a page away, so an all-refused list left the highlight where it
    was. Paging down repeatedly has to reach the last row - compositing
    each row it stops on - and paging up has to come back to the first.
    """
    screen = ActionPaletteScreen(_helmless_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=size) as pilot:
        options = screen.query_one(OptionList)
        await _helm_results(pilot, options)
        last = options.option_count - 1
        for _ in range(options.option_count):
            if options.highlighted == last:
                break
            previous = options.highlighted
            await pilot.press("pagedown")
            assert options.highlighted is not None
            assert options.highlighted > (previous or 0)
            _assert_row_composited(options, options.highlighted)
        assert options.highlighted == last
        for _ in range(options.option_count):
            if options.highlighted == 0:
                break
            await pilot.press("pageup")
            assert options.highlighted is not None
            _assert_row_composited(options, options.highlighted)
        assert options.highlighted == 0
        await pilot.press("enter")
        assert app.results == []
        assert screen.query_one(Input).has_focus


async def test_the_empty_query_end_reaches_the_trailing_unavailable_rows() -> None:
    """The palette's own default view ends on rows `End` could not reach.

    With no query, `rank_entries` puts every unavailable entry last - so on
    a session without helm the four refused helm rows are the bottom of the
    list, below the last enabled one. `End` stopped on that last *enabled*
    row and the rows underneath it were unreachable by any key. It has to
    land on the real last row, render it whole, stay inert, and `Home` has
    to bring the first row back.
    """
    screen = ActionPaletteScreen(_helmless_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        last = options.option_count - 1
        assert options.get_option_at_index(last).id == f"action:{_HELM_ACTIONS[-1]}"
        assert _says_it_cannot_run(options, last)
        assert not _says_it_cannot_run(options, last - len(_HELM_ACTIONS))
        await pilot.press("end")
        assert options.highlighted == last
        _assert_row_composited(options, last)
        assert _LAST_HELM_TITLE in _heading(options, last)
        await pilot.press("enter")
        assert app.results == []
        await pilot.press("home")
        assert options.highlighted == 0
        _assert_row_composited(options, 0)
        assert screen.query_one(Input).has_focus


#: Two real log keys one owner refuses together, and the query that leaves
#: them among rows that still run. `rank_entries` only breaks *ties* by
#: availability, so a real query renders the two refused rows in the middle
#: of the results rather than in a block at the end - the arrangement an
#: enabled-only navigation steps straight over.
_MIXED_REFUSED = ("logs_multi", "log_save")
_MIXED_QUERY = "log"


def _mixed_availability_catalog() -> list[PaletteEntry]:
    """The whole real catalog with `_MIXED_REFUSED` refused by their owner."""
    reason = _helm_missing_reason()

    def availability(action: str) -> ActionAvailability:
        return (
            ActionAvailability(True, reason)
            if action in _MIXED_REFUSED
            else ActionAvailability.enabled()
        )

    return derive_palette_entries(
        APP_BINDINGS,
        COMMANDS,
        overrides={},
        availability=availability,
        command_availability=lambda _command: ActionAvailability.enabled(),
    )


async def _mixed_results(pilot: Pilot[None], options: OptionList) -> list[bool]:
    """Type `_MIXED_QUERY` and wait for a genuinely mixed result list.

    Returns the rendered availability pattern once at least one refused row
    has an enabled row both above and below it - the arrangement the
    finding is about, asserted rather than assumed, so a future change to
    ranking cannot quietly turn this into an all-enabled list.
    """
    await pilot.press(*_MIXED_QUERY)

    def mixed() -> bool:
        flags = _refused_rows(options)
        return any(
            flag and any(not f for f in flags[:index]) and any(not f for f in flags[index + 1 :])
            for index, flag in enumerate(flags)
        )

    await until(pilot, mixed, label="a refused row rendered between rows that still run")
    return _refused_rows(options)


async def test_traversal_does_not_skip_an_unavailable_row_in_either_direction() -> None:
    """Every index has to be visited once, going down and coming back up.

    Enabled-only navigation jumps the refused rows entirely: `Down` from
    the runnable row above them lands past both, and neither is ever
    reached or scrolled to. Every index has to be visited in turn - and a
    runnable row must still run when Enter is pressed on it, exactly
    once.
    """
    screen = ActionPaletteScreen(_mixed_availability_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        flags = await _mixed_results(pilot, options)
        assert any(flags)
        assert not all(flags)
        descending = [options.highlighted]
        for _ in range(options.option_count - 1):
            await pilot.press("down")
            descending.append(options.highlighted)
        assert descending == list(range(options.option_count))
        ascending = [options.highlighted]
        for _ in range(options.option_count - 1):
            await pilot.press("up")
            ascending.append(options.highlighted)
        assert ascending == list(reversed(range(options.option_count)))
        assert not _says_it_cannot_run(options, 0)
        first = options.get_option_at_index(0)
        await pilot.press("enter")
        assert app.results == [first.id]


async def test_enter_on_an_unavailable_row_reached_by_arrows_runs_nothing() -> None:
    """The highlight may rest on a refused row; Enter must still do nothing.

    Browsable and invocable are different questions. Walking onto a
    refused row is how its reason becomes readable, and the row must stay
    inert there: `_activate` stays guarded by the entry's own
    availability, and the palette must not dismiss. The next `Down` lands
    on a row that does run, and runs it exactly once.
    """
    screen = ActionPaletteScreen(_mixed_availability_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        flags = await _mixed_results(pilot, options)
        refused = flags.index(True)
        runnable = flags.index(False, refused)
        for index in range(1, runnable + 1):
            await pilot.press("down")
            assert options.highlighted == index
            if index == refused:
                _assert_row_composited(options, index)
                await pilot.press("enter")
                assert app.results == []
        assert options.highlighted == runnable
        assert not _says_it_cannot_run(options, runnable)
        target = options.get_option_at_index(runnable)
        await pilot.press("enter")
        assert app.results == [target.id]


async def test_a_page_key_never_leaves_a_hidden_row_highlighted() -> None:
    """Whatever a page lands on has to be on screen when it gets there.

    A page is a jump, so it is the key most able to leave the highlight
    somewhere the viewport is not - an enabled row the user cannot see but
    Enter would still run. Paging to the bottom of the full catalog and
    back has to composite the highlighted row after every single press.
    """
    screen = ActionPaletteScreen(_derived_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        last = options.option_count - 1
        for _ in range(options.option_count):
            if options.highlighted == last:
                break
            await pilot.press("pagedown")
            assert options.highlighted is not None
            _assert_row_composited(options, options.highlighted)
        assert options.highlighted == last
        for _ in range(options.option_count):
            if options.highlighted == 0:
                break
            await pilot.press("pageup")
            assert options.highlighted is not None
            _assert_row_composited(options, options.highlighted)
        assert options.highlighted == 0
        assert screen.query_one(Input).has_focus


async def test_the_no_results_row_stays_inert_under_every_navigation_key() -> None:
    """The one row a query with no matches leaves is not an entry at all.

    It has no id and no entry behind it, so every key that now moves a
    cursor has to leave it unreached and run nothing: the palette must not
    dismiss, and the query `Input` must keep focus so the user can correct
    the query.
    """
    screen = ActionPaletteScreen(_derived_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await pilot.press("z", "z", "z", "z", "z")
        await until(
            pilot,
            lambda: options.option_count == 1,
            label="the query left the single no-results row",
        )
        assert options.get_option_at_index(0).disabled is True
        assert options.get_option_at_index(0).id is None
        for key in ("down", "up", "pagedown", "pageup", "end", "home", "enter"):
            await pilot.press(key)
            assert options.option_count == 1
            assert app.results == []
        assert "no match" in str(options.get_option_at_index(0).prompt).casefold()
        assert screen.query_one(Input).has_focus


def _cursor_background(options: OptionList) -> object:
    """The background colour `OptionList` paints the highlighted row with.

    Read from the widget's own resolved component styles rather than from a
    literal, and asserted to differ from an ordinary row's: the palette's
    list is never focused (the query `Input` is), so this is the theme's
    blurred block cursor, and checking it is a different colour first stops
    every "the cursor is visible" assertion below from passing vacuously.
    """
    cursor = options.get_visual_style("option-list--option", "option-list--option-highlighted")
    plain = options.get_visual_style("option-list--option")
    assert cursor.rich_style.bgcolor != plain.rich_style.bgcolor
    return cursor.rich_style.bgcolor


def _cursor_text(options: OptionList) -> str:
    """Every character the viewport draws in the cursor's own background.

    The frame, not the widget's bookkeeping: `render_line` is the entry
    point Textual's compositor calls, and the cells carrying the highlight
    background are exactly the ones the user sees the cursor on. An empty
    result therefore means *no cursor is on screen at all* - which is what
    `highlighted` pointing at a disabled option leaves behind. Spaces are
    dropped so a row that wrapped, at a space or inside a word, still reads
    as the characters it is made of.
    """
    background = _cursor_background(options)
    return "".join(
        segment.text
        for y in range(options.size.height)
        for segment in options.render_line(y)
        if segment.style is not None and segment.style.bgcolor == background
    ).replace(" ", "")


def _assert_cursor_on(options: OptionList, index: int) -> None:
    """Fail unless the frame draws a cursor, and draws it on row `index`."""
    drawn = _cursor_text(options)
    assert drawn, f"row {index} is highlighted but no cursor is drawn in the frame"
    heading = _heading(options, index).replace(" ", "")
    assert heading in drawn, f"the cursor is not drawn on row {index}: {drawn!r}"


@pytest.mark.parametrize("size", _LAYOUT_SIZES, ids=_LAYOUT_IDS)
async def test_the_cursor_is_drawn_on_each_unavailable_row_it_moves_to(
    size: tuple[int, int],
) -> None:
    """A row the user can walk onto has to show where the cursor is.

    Moving the highlight is not the same thing as moving the *cursor*.
    `OptionList.render_line` chooses the disabled component style before it
    looks at `highlighted`, so an option marked disabled is drawn
    identically whether or not it is the highlighted one: on a list where
    every row is refused - a base install without helm, `helm` typed - each
    `Down` changed an index nobody could see and the frame stayed exactly
    as it was. The cursor has to be in the frame, on the row that was
    reached, after every single press.
    """
    screen = ActionPaletteScreen(_helmless_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=size) as pilot:
        options = screen.query_one(OptionList)
        await _helm_results(pilot, options)
        assert all(_refused_rows(options))
        _assert_cursor_on(options, 0)
        previous = _cursor_text(options)
        for index in range(1, options.option_count):
            await pilot.press("down")
            assert options.highlighted == index
            _assert_cursor_on(options, index)
            drawn = _cursor_text(options)
            assert drawn != previous, f"the frame did not change moving onto row {index}"
            previous = drawn
        assert app.results == []


async def test_the_cursor_survives_the_step_from_a_runnable_row_to_a_refused_one() -> None:
    """A mixed list must not lose its cursor on the way through a refusal.

    The `log` query leaves rows that still run above and below the two the
    logs owner has refused, so `Down` crosses the boundary in both
    directions. Every row on that walk - runnable or not - has to be the
    one the cursor is drawn on, or the user loses their place in the middle
    of the list.
    """
    screen = ActionPaletteScreen(_mixed_availability_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        flags = await _mixed_results(pilot, options)
        refused = flags.index(True)
        assert refused > 0
        for index in range(1, refused + 1):
            await pilot.press("down")
            assert options.highlighted == index
            _assert_cursor_on(options, index)
        assert _says_it_cannot_run(options, refused)
        await pilot.press("up")
        _assert_cursor_on(options, refused - 1)
        assert app.results == []


def _click_offset(options: OptionList, line: int) -> tuple[int, int]:
    """Where to click to hit viewport `line`, in the list's own coordinates.

    The gutter between the widget's region and its content region is its
    border and padding, and that differs between the two terminals (the
    short one drops the border for Textual's compact style), so it is
    measured rather than assumed.
    """
    gutter = options.content_region.offset - options.region.offset
    return (gutter.x, gutter.y + line)


def _viewport_line_showing(options: OptionList, text: str) -> int:
    """The first viewport line whose characters contain `text`."""
    wanted = text.replace(" ", "")
    for number, line in enumerate(_viewport_lines(options)):
        if wanted in line.replace(" ", ""):
            return number
    raise AssertionError(f"{text!r} is not on screen: {_viewport_lines(options)}")


async def test_clicking_an_unavailable_row_moves_the_cursor_there_and_runs_nothing() -> None:
    """The mouse reaches a refused row exactly as far as the keyboard does.

    `OptionList._on_click` ignores a disabled option outright, so the row
    a user clicked to read stayed unhighlighted and unscrolled. A row that
    is navigable answers the click - the cursor moves onto it, its reason
    is on screen - and the selection that click raises must still run
    nothing: `OptionSelected` goes through the same availability guard the
    keyboard does, so the palette neither dismisses nor dispatches, and the
    query `Input` keeps focus.
    """
    screen = ActionPaletteScreen(_helmless_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await _helm_results(pilot, options)
        assert options.highlighted == 0
        line = _viewport_line_showing(options, _heading(options, 1))
        await pilot.click(OptionList, offset=_click_offset(options, line))
        await pilot.pause()
        assert options.highlighted == 1
        _assert_cursor_on(options, 1)
        _assert_row_composited(options, 1)
        assert _says_it_cannot_run(options, 1)
        assert app.results == []
        assert screen.query_one(Input).has_focus


async def test_selecting_an_unavailable_row_runs_nothing() -> None:
    """`OptionList`'s own selection is guarded by the entry, not by the row.

    Every selection path - Enter on the query `Input`, a click, the list's
    own `action_select` - ends at the palette's one availability guard. A
    navigable refused row means that guard is now the *only* thing between
    a refusal and a dispatch, so removing it has to fail here: the palette
    must not dismiss, and the next runnable row must still run.
    """
    screen = ActionPaletteScreen(_mixed_availability_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        flags = await _mixed_results(pilot, options)
        refused = flags.index(True)
        options.highlighted = refused
        options.action_select()
        await pilot.pause()
        assert app.results == []
        runnable = flags.index(False, refused)
        options.highlighted = runnable
        options.action_select()
        await pilot.pause()
        assert app.results == [options.get_option_at_index(runnable).id]


@pytest.mark.parametrize("size", _LAYOUT_SIZES, ids=_LAYOUT_IDS)
async def test_a_page_key_never_scrolls_past_the_lines_the_viewport_was_showing(
    size: tuple[int, int],
) -> None:
    """One page may not skip content the user never saw.

    A page is a *line* measure, not a row one: the guarantee is that the
    view after the press still touches the view before it, so nothing
    scrolls past unread. Row heights in the real catalog vary - a refused
    helm row is three lines at 80 columns and five at 36, a plain row two -
    so a page counted in rows times an integer-truncated average row height
    overshoots, jumping thirteen to fifteen lines across a viewport that
    holds eight or nine. Each press may move the view by at most the
    viewport's own height, in both directions, and the row it lands on has
    to be composited whole.
    """
    screen = ActionPaletteScreen(_helmless_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=size) as pilot:
        options = screen.query_one(OptionList)
        viewport = options.scrollable_content_region.height
        assert options.virtual_size.height > viewport
        last = options.option_count - 1
        for _ in range(options.option_count):
            if options.highlighted == last:
                break
            before = options.scroll_offset.y
            await pilot.press("pagedown")
            moved = options.scroll_offset.y - before
            assert 0 <= moved <= viewport, (
                f"pagedown scrolled {moved} lines across a {viewport}-line viewport"
            )
            assert options.highlighted is not None
            _assert_row_composited(options, options.highlighted)
        assert options.highlighted == last
        assert _LAST_HELM_TITLE in _heading(options, last)
        assert _says_it_cannot_run(options, last)
        for _ in range(options.option_count):
            if options.highlighted == 0:
                break
            before = options.scroll_offset.y
            await pilot.press("pageup")
            moved = before - options.scroll_offset.y
            assert 0 <= moved <= viewport, (
                f"pageup scrolled {moved} lines across a {viewport}-line viewport"
            )
            assert options.highlighted is not None
            _assert_row_composited(options, options.highlighted)
        assert options.highlighted == 0
        assert app.results == []
        assert screen.query_one(Input).has_focus


async def test_the_no_results_row_is_never_given_a_cursor() -> None:
    """The one row a query with no matches leaves is not navigable at all.

    Every real catalog row is navigable now, which is exactly why this one
    must not be: it has no entry behind it, so there is nothing to browse
    and nothing to run. It stays a disabled, id-less option - no cursor is
    ever drawn on it, no navigation key dismisses the palette, and the
    query `Input` keeps focus so the user can correct the query.
    """
    screen = ActionPaletteScreen(_derived_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(80, 24)) as pilot:
        options = screen.query_one(OptionList)
        await pilot.press("z", "z", "z", "z", "z")
        await until(
            pilot,
            lambda: options.option_count == 1,
            label="the query left the single no-results row",
        )
        for key in ("down", "up", "pagedown", "pageup", "end", "home", "enter"):
            await pilot.press(key)
            assert options.option_count == 1
            assert options.get_option_at_index(0).disabled is True
            assert options.get_option_at_index(0).id is None
            assert _cursor_text(options) == ""
            assert app.results == []
        assert screen.query_one(Input).has_focus


#: A terminal one row short of the smallest layout the palette supports,
#: which is what makes it the sharpest test of the pull-back's arithmetic:
#: below roughly seven rows the modal's own chrome no longer fits and the
#: results clamp to a *single line*, so every row in the list is taller
#: than the viewport and no page press can ever satisfy the one-viewport
#: bound. The palette is not promised to be readable here — it is promised
#: to keep answering keys.
_ONE_LINE_VIEWPORT = (36, 7)

#: Lines one mouse-wheel notch scrolls, which is Textual's own
#: `App.scroll_sensitivity_y`. The regression below scrolls a viewport
#: *plus* one notch, which is the smallest manual scroll that leaves the
#: view further from the cursor's row than a page press is allowed to
#: move it - the state the correction then has to answer for.
_WHEEL_LINES = 2

#: Seconds the child is given before it is killed. It is a safety net,
#: never a measurement: a palette that answers the key at all answers it
#: in the time one process takes to import Textual and paint four frames,
#: and one that does not never answers at all. Nothing here asserts how
#: long the working path took.
_PROBE_TIMEOUT = 120

#: Driven in a child process, because the failure this pins is a *hang*:
#: the pull-back used to walk the list with `OptionList`'s own wrapping
#: cursor actions, so a page press that could not advance (`End`, then a
#: wheel notch, then `PageDown`) span through all 52 rows for ever inside
#: one synchronous message handler. A test that called it in-process would
#: take the whole suite down with it; a child can simply be killed.
_PAGE_SETTLES_PROBE = """
import asyncio
import sys

sys.path.insert(0, sys.argv[1])

from textual.widgets import OptionList

from korvid.ui.widgets.action_palette import ActionPaletteScreen
from tests.ui.test_action_palette_screen import (
    PaletteHarness,
    _assert_row_composited,
    _helmless_catalog,
)

WHEEL = int(sys.argv[2])
COLUMNS = int(sys.argv[3])
SCREEN_ROWS = int(sys.argv[4])
READABLE = sys.argv[5] == "readable"


async def drive() -> str:
    screen = ActionPaletteScreen(_helmless_catalog())
    app = PaletteHarness(screen)
    async with app.run_test(size=(COLUMNS, SCREEN_ROWS)) as pilot:
        options = screen.query_one(OptionList)
        await pilot.pause()
        viewport = options.scrollable_content_region.height
        rows = options.option_count
        # Far enough that the view is more than one viewport from the row
        # the cursor is on - the state the page press has to correct.
        away = viewport + WHEEL

        await pilot.press("end")
        await pilot.pause()
        assert options.highlighted == rows - 1, options.highlighted
        options.scroll_to(y=options.scroll_offset.y - away, animate=False)
        await pilot.pause()
        await pilot.press("pagedown")
        await pilot.pause()
        down = options.highlighted
        down_scroll = options.scroll_offset.y
        options.scroll_to_highlight()
        await pilot.pause()
        down_settled = options.scroll_offset.y == down_scroll
        if READABLE:
            _assert_row_composited(options, rows - 1)

        await pilot.press("home")
        await pilot.pause()
        assert options.highlighted == 0, options.highlighted
        options.scroll_to(y=options.scroll_offset.y + away, animate=False)
        await pilot.pause()
        await pilot.press("pageup")
        await pilot.pause()
        up = options.highlighted
        up_scroll = options.scroll_offset.y
        options.scroll_to_highlight()
        await pilot.pause()
        up_settled = options.scroll_offset.y == up_scroll
        if READABLE:
            _assert_row_composited(options, 0)
        dispatched = len(app.results)
    return (
        f"viewport={viewport} rows={rows} down={down} up={up} "
        f"down_settled={down_settled} up_settled={up_settled} dispatched={dispatched}"
    )


print(asyncio.run(drive()))
"""


def _probe_report(stdout: str) -> dict[str, str]:
    """Parse the child's one `key=value` line into a dict."""
    line = stdout.strip().splitlines()[-1]
    return dict(item.split("=", 1) for item in line.split())


#: The two supported layouts plus the one-line viewport below them. Only
#: the supported two are asked for a readable row.
_SETTLE_SIZES = [*_LAYOUT_SIZES, _ONE_LINE_VIEWPORT]
_SETTLE_IDS = [*_LAYOUT_IDS, "36x7"]


@pytest.mark.parametrize("size", _SETTLE_SIZES, ids=_SETTLE_IDS)
def test_a_page_press_the_list_cannot_answer_still_returns(size: tuple[int, int]) -> None:
    """A page that cannot advance must end, not walk the list for ever.

    `End` puts the cursor on the last row; a manual scroll then moves the
    *view* further than a viewport away from it without moving the cursor
    at all - a mouse wheel does exactly this. The `PageDown` that follows
    has nowhere to go: `OptionList` leaves the highlight exactly where it
    was, while the re-assert that reveals that row scrolls the view back
    by more than the viewport's own height. The pull-back that corrects an
    overshooting page therefore started from a page that never moved, and
    stepped with the list's own `action_cursor_up`, which *wraps*: from
    row 51 it walked 50, 49, ..., 0, 51, for ever, inside one synchronous
    key handler, with no frame ever painted again. The one-line viewport
    below the supported sizes is the sharpest case - no row fits it, so no
    page press there can ever satisfy the bound - but the supported
    layouts hang on the same arithmetic once the view is scrolled far
    enough by hand.

    The bound is arithmetic now: a page that did not advance is corrected
    not at all, and one that did may only step through the rows strictly
    between where it started and where it landed. Both directions are
    driven here, and the child prints what the palette settled on.
    """
    root = Path(__file__).resolve().parents[2]
    columns, screen_rows = size
    readable = "readable" if size in _LAYOUT_SIZES else "clamped"
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _PAGE_SETTLES_PROBE,
                str(root),
                str(_WHEEL_LINES),
                str(columns),
                str(screen_rows),
                readable,
            ],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            cwd=root,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "the palette never came back from the page keys: the viewport "
            "correction did not terminate"
        )
    assert result.returncode == 0, result.stderr
    report = _probe_report(result.stdout)
    rows = int(report["rows"])
    assert rows > 1, report
    # The list is longer than the viewport at every size under test, which
    # is what lets a manual scroll leave the cursor's row off screen.
    assert int(report["viewport"]) < rows, report
    if size == _ONE_LINE_VIEWPORT:
        # The precondition that makes this terminal the hardest case: not
        # one row of the list fits the viewport it is drawn in.
        assert report["viewport"] == "1", report
    # A page with nowhere to go leaves the cursor exactly where it was,
    # never wrapped around to the other end of the list.
    assert report["down"] == str(rows - 1), report
    assert report["up"] == "0", report
    # ...and the view it settles on is the one that reveals that row.
    assert report["down_settled"] == "True", report
    assert report["up_settled"] == "True", report
    assert report["dispatched"] == "0", report


#: Every origin/target pair a page press can produce on a short list -
#: both edges, both directions, and the pairs a page that did not move
#: produces.
_PULLBACK_PAIRS = [(origin, target) for origin in range(6) for target in range(6)]


@pytest.mark.parametrize(("origin", "target"), _PULLBACK_PAIRS)
def test_the_pull_back_walks_a_bounded_path_that_never_reaches_the_origin(
    origin: int, target: int
) -> None:
    """The pull-back's path is finite, monotone, and inside its own interval.

    This is the whole termination argument, stated over the arithmetic
    rather than over a widget: the rows a pull-back may visit are a
    *tuple*, so the loop that walks it cannot fail to end; they lie
    strictly between where the page started and where it landed, so the
    view can only shrink back towards `origin` and never overshoot past
    it; each is one row closer to `origin` than the last, so no row is
    visited twice; and `origin` itself is never among them, which is what
    keeps every page press moving at least one row.
    """
    rows = _pull_back_rows(origin, target)
    assert len(rows) == max(abs(target - origin) - 1, 0)
    assert origin not in rows
    assert target not in rows
    assert all(min(origin, target) < row < max(origin, target) for row in rows)
    assert len(set(rows)) == len(rows)
    assert all(abs(later - origin) < abs(earlier - origin) for earlier, later in pairwise(rows))
    if rows:
        assert abs(rows[0] - target) == 1
        assert abs(rows[-1] - origin) == 1


def test_a_page_that_did_not_advance_walks_nothing() -> None:
    """The exact state that used to hang: a page press that moved no row.

    `PageDown` on the last row and `PageUp` on the first leave the
    highlight where it was, and so does any page press on a list one row
    long. There is nothing between the origin and the target to walk, so
    the correction is empty and the key returns having only re-asserted
    the scroll.
    """
    assert _pull_back_rows(51, 51) == ()
    assert _pull_back_rows(0, 0) == ()
    assert _pull_back_rows(0, 1) == ()
    assert _pull_back_rows(1, 0) == ()


def test_the_pull_back_steps_from_the_landing_row_back_towards_the_origin() -> None:
    """Across the whole list, in both directions, spelled out in full."""
    assert _pull_back_rows(0, 51) == tuple(range(50, 0, -1))
    assert _pull_back_rows(51, 0) == tuple(range(1, 51))
