"""Keyboard-first Action Palette modal (issue #388 task 5).

This is a standalone `ModalScreen` over a `Sequence[PaletteEntry]`: no
`Ctrl-P` open binding and no app dispatch live here — a later task pushes
this screen from `KorvidApp` and re-resolves the dismissed `entry.id`
against live entries before routing it through `run_action`/
`parse_command`. Everything below is built from public Textual
widgets/APIs; there is no subclassing of Textual's private `CommandPalette`
internals.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from korvid.ui.action_palette import PaletteEntry, rank_entries

#: The sole, disabled row shown when a query matches nothing — visible and
#: explicit rather than an empty, ambiguous-looking list.
_NO_RESULTS_PROMPT = "No matching actions or commands"

#: How a row that cannot run right now is drawn. A Rich style on the row's
#: own prompt rather than Textual's `Option.disabled`: a disabled option is
#: one `OptionList.render_line` picks the disabled component style for
#: *before* it looks at `highlighted`, so it can never show a cursor, and
#: this palette's unavailable rows are exactly the ones a user opens it to
#: read. `dim` greys the whole row without touching its background, which
#: is what leaves the block cursor visible when the row is the highlighted
#: one.
_UNAVAILABLE_STYLE = "dim"

#: Rows the modal's own height spends on everything that is not results
#: content: the query `Input` (3), the key hint (1), the container's
#: vertical padding (2), the container's border (2) and the results list's
#: own border (2). The modal sets an exact height of these plus the rows
#: it decided to give the results, which is what keeps the hint on screen
#: when the catalog is longer than the terminal.
_CHROME_ROWS = 10

#: The same rows once the modal gives up its vertical padding and the
#: results list its border (Textual's own `compact` style): 3 + 1 + 0 + 2
#: + 0. A terminal too short to afford `_MIN_RESULT_ROWS` any other way
#: gets this instead of a readable-looking modal with an unreadable list.
_TIGHT_CHROME_ROWS = 6

#: Rows of results content below which a real catalog row stops being
#: readable. At 36 columns the longest rows — a long title plus a `:`
#: spelling, or an owner's refusal such as "Relationships unavailable in
#: this session" — wrap to six to eight lines, and an entry whose trigger
#: or reason is clipped away cannot be reached by keyboard: the list
#: scrolls by whole options, never within one.
_MIN_RESULT_ROWS = 8

#: Upper bound on the results viewport, so the palette stays a palette on a
#: very tall terminal instead of becoming a full-height list.
_MAX_RESULT_ROWS = 18

#: Percentage of the terminal height the whole modal may occupy while that
#: still leaves `_MIN_RESULT_ROWS` of results.
_HEIGHT_SHARE = 80

#: What separates the three parts of a row's first line (category, title,
#: trigger). A middle dot rather than a bracket or a pipe: the parts are
#: catalog text rendered literally, and this must not look like syntax the
#: user could mistake for part of a name.
_HEADING_SEPARATOR = " · "


def _prompt_for(entry: PaletteEntry) -> Text:
    """Build the two-line prompt for `entry`.

    The first line says what the row is and how to run it: the catalog's
    own `category`, the entry `title`, and the `trigger` that invokes it
    right now — the effective key for a bound action (already resolved
    through any `keybindings:` remap when the entry was derived) or the
    canonical `:` spelling for a command. The second line stays the
    description, or the owner's reason when the entry cannot run. None of
    it is a separate display table: every part is the derived entry's own
    catalog-derived field.

    Always a `Text` object, never a raw `str`: Textual renders a plain
    `str` `Option` prompt as Rich console markup, which would misrender a
    catalog-derived title, description, or unavailable-reason that happens
    to contain `[`/`]`. Appending each part to a `Text` keeps that content
    literal — `Text.append` never parses markup.

    A row that cannot run is greyed by the `Text`'s own base style
    (`_UNAVAILABLE_STYLE`), not by `Option.disabled`: the row stays one the
    cursor can be drawn on while its second line says, in words, that it
    cannot run.
    """
    prompt = Text(style="" if entry.availability.invocable else _UNAVAILABLE_STYLE)
    if entry.category:
        prompt.append(entry.category, style="dim")
        prompt.append(_HEADING_SEPARATOR)
    prompt.append(entry.title, style="bold")
    if entry.trigger:
        prompt.append(_HEADING_SEPARATOR)
        prompt.append(entry.trigger, style="dim")
    if entry.availability.invocable:
        second_line = entry.description
    else:
        reason = entry.availability.reason
        message = reason.message if reason is not None else "unavailable"
        second_line = f"Unavailable: {message}"
    if second_line:
        prompt.append("\n")
        prompt.append(second_line)
    return prompt


def _pull_back_rows(origin: int, target: int) -> tuple[int, ...]:
    """The rows a page's pull-back may step through, `target` back to `origin`.

    The correction in `_keep_the_page_inside_the_viewport` walks this
    tuple, and every property that makes that walk terminate is decided
    here, in arithmetic, rather than by asking a widget where its cursor
    ended up:

    - it is finite — at most `abs(target - origin) - 1` rows;
    - every row lies strictly *between* `origin` and `target`, so the
      correction can never step outside the interval the page itself
      covered, and never wraps around either end of the list;
    - each row is one closer to `origin` than the last, so the view can
      only shrink back towards where the page started;
    - `origin` itself is never in it, so a page press always leaves the
      cursor at least one row from where the user pressed it.

    A page that did not advance (`target == origin`, which is what
    `PageDown` on the last row and `PageUp` on the first both produce)
    has nothing between the two, so the tuple is empty.

    Args:
        origin: The row the cursor was on before the page key.
        target: The row the page key left it on.

    Returns:
        The rows to try, nearest `target` first.
    """
    step = -1 if target > origin else 1
    return tuple(range(target + step, origin, step))


class ActionPaletteScreen(ModalScreen[str | None]):
    """Search-filtered, keyboard-first list over `PaletteEntry` values.

    Dismisses with the selected entry's stable `id`, or `None` on cancel or
    when there is nothing selectable to activate. The caller — not this
    screen — re-resolves that id against live entries and dispatches it;
    this screen never invokes an action or command itself.

    Every navigation key below is `OptionList`'s own public navigation
    action: a row whose owner has refused it is a *navigable* row here (see
    `_render_results`), so the list's cursor reaches it like any other and
    the owner's reason on it can be read. Invocation is the separate
    question, answered once in `_activate`.
    """

    # The app's keymap planner reserves these from competing priority actions,
    # which Textual dispatches before screen bindings.
    CLOSE_KEYS = ("escape", "ctrl+p")
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding(",".join(CLOSE_KEYS), "cancel", "Close", show=False),
        Binding("down", "move(1)", "Next", show=False),
        Binding("up", "move(-1)", "Previous", show=False),
        Binding("pagedown", "page(1)", "Next page", show=False),
        Binding("pageup", "page(-1)", "Previous page", show=False),
        # `Input` binds `home`/`end` itself (cursor to start/end of the
        # query text). Marking these two `priority=True` lets the screen's
        # binding win the resolution race so Home/End jump the results
        # list instead, while the `Input` keeps focus throughout — plain
        # (non-priority) bindings on a screen lose to the focused widget's
        # own bindings for the same key.
        Binding("home", "edge(-1)", "First", show=False, priority=True),
        Binding("end", "edge(1)", "Last", show=False, priority=True),
    ]

    DEFAULT_CSS = f"""
    ActionPaletteScreen {{
        align: center middle;
    }}
    ActionPaletteScreen #action-palette {{
        width: 76;
        max-width: 94%;
        /* An exact height, never `auto`: `_fit_results` replaces this
           one with the terminal's own budget on mount, before the first
           paint, and again on every resize. What is left here is only
           that one frame's fallback — the budget a roomy terminal gets
           — and `max-height` keeps even it inside a terminal too short
           to afford it. */
        height: {_MIN_RESULT_ROWS + _CHROME_ROWS};
        max-height: 100%;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }}
    ActionPaletteScreen #action-results {{
        /* The results take whatever the modal's exact height leaves after
           the query input, the key hint and the chrome — they never ask
           for a height of their own. A list sized from its own content
           has to be *measured* first, and Textual measures a row against
           the width it has before the vertical scrollbar is decided while
           it draws that row against the width after: a row that wraps to
           six lines is then given five, and the sixth is never
           composited. Flex sizing asks no such question. */
        height: 1fr;
        /* Reserve the vertical scrollbar's two columns whether or not a
           scrollbar is showing, so the width a row wraps against stays
           the same number as rows come and go with the query. */
        scrollbar-gutter: stable;
    }}
    ActionPaletteScreen #action-hint {{
        height: 1;
        color: $text-muted;
    }}
    """

    def __init__(self, entries: Sequence[PaletteEntry]) -> None:
        super().__init__()
        self._entries = tuple(entries)
        self._visible: dict[str, PaletteEntry] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="action-palette"):
            yield Input(placeholder="Search actions and commands", id="action-query")
            results = OptionList(id="action-results")
            # The query `Input` is the modal's only focus. Now that every
            # real row is navigable, a click on one is a click on a
            # focusable widget, and it would take the keyboard away from
            # the query the user is still typing — a refused row does not
            # dismiss the palette, so they would be left in a palette they
            # can no longer search.
            results.can_focus = False
            yield results
            yield Static("Enter run · Esc close", id="action-hint", markup=False)

    def on_mount(self) -> None:
        self._fit_results(self.app.size.height)
        self._render_results("")
        self.query_one(Input).focus()

    def on_resize(self, event: events.Resize) -> None:
        """Re-budget the modal for a terminal that changed size."""
        self._fit_results(event.size.height)
        self._reveal_highlighted_after_layout()

    def _fit_results(self, screen_rows: int) -> None:
        """Give the modal the exact height `screen_rows` can afford.

        CSS alone cannot say this, and neither can the rows: a modal that
        takes its height from what its results measure is a modal whose
        height changes every time a keystroke changes the query — and one
        whose results viewport is only ever as tall as some earlier
        measurement of a row, at a width that is not the width the row is
        finally drawn at. So the budget is decided here, from the terminal
        alone, and the results list takes what is left of it (`height:
        1fr`, see `DEFAULT_CSS`).

        The budget is `_HEIGHT_SHARE` of the terminal — but that cap is
        cosmetic, and on a short terminal it is not affordable: 80% of 16
        rows leaves two rows of results, too few to render one real
        catalog row, and a row whose trigger or reason is clipped cannot
        be reached by any keystroke because the list scrolls by whole
        options. So when the share cannot fund `_MIN_RESULT_ROWS`, the
        modal drops to tight chrome (no vertical padding, and Textual's
        compact `OptionList`, which also widens every row by the border it
        gives up) and grows up to — never past — the terminal height. A
        taller terminal keeps both the share cap and `_MAX_RESULT_ROWS`.

        Everything is set through the public `styles`/`compact` API, and
        never below one row, so the palette still renders on a terminal
        too short for even that (below ~7 rows it is the modal's own
        chrome, not the results, that no longer fits, and the container's
        `max-height` keeps what is left inside the screen).
        """
        share = screen_rows * _HEIGHT_SHARE // 100
        tight = share - _CHROME_ROWS < _MIN_RESULT_ROWS
        chrome = _TIGHT_CHROME_ROWS if tight else _CHROME_ROWS
        budget = min(screen_rows, max(share, chrome + _MIN_RESULT_ROWS))
        rows = max(1, min(_MAX_RESULT_ROWS, budget - chrome))
        container = self.query_one("#action-palette")
        container.styles.padding = (0, 2) if tight else (1, 2)
        container.styles.height = rows + chrome
        self.query_one(OptionList).compact = tight

    def _reveal_highlighted_after_layout(self) -> None:
        """Order the post-resize scroll for once the new geometry is settled.

        A resize invalidates the scroll offset twice over: the viewport was
        re-budgeted, and every row re-wrapped to a different number of
        lines. Scrolling from inside `on_resize` would therefore aim at
        rows that no longer exist at those offsets, which is how `End` at
        80x24 used to render a middle row at 36x16. Ordering it through
        the list's own `call_after_refresh` is what makes it a fix rather
        than a race — Textual drains that widget's pending messages (its
        own `Resize` among them) and lays the screen out before running
        the callback, so the scroll aims at the rows at their final
        heights.

        Only a scroll, and never a rebuild: the rows are what they always
        were, and the height they are shown in comes from the modal's
        budget rather than from anything measured about them. No timer, no
        second resize, and nothing the user typed or highlighted moves.
        """
        options = self.query_one(OptionList)
        options.call_after_refresh(self._reveal_after_resize)

    def _reveal_after_resize(self) -> None:
        """Put the highlighted row back in the viewport, if there still is one.

        Runs from a refresh callback, so the palette may already be gone by
        the time it fires — a resize immediately before an `Esc`, or before
        the caller dismisses the screen. The guard is the rows themselves
        rather than `is_mounted`: Textual takes a dismissed screen's
        children away without ever clearing that flag, so a callback that
        trusted it would query a screen with nothing on it and raise
        `NoMatches` out of the callback.
        """
        results = self.query(OptionList)
        if not results:
            return
        self._reveal_highlighted(results.first(OptionList))

    @on(Input.Changed)
    def _query_changed(self, event: Input.Changed) -> None:
        self._render_results(event.value)

    @on(Input.Submitted)
    def _query_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._activate_highlighted()

    @on(OptionList.OptionSelected)
    def _option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._activate(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_move(self, direction: int) -> None:
        """Move the cursor one row, whether or not that row can run."""
        options = self.query_one(OptionList)
        if direction > 0:
            options.action_cursor_down()
        else:
            options.action_cursor_up()
        self._reveal_highlighted(options)

    def action_page(self, direction: int) -> None:
        """Move the cursor one page, bounded by the viewport's own height."""
        options = self.query_one(OptionList)
        origin = options.highlighted
        start = options.scroll_offset.y
        # Textual leaves these two `action_*` methods unannotated, alone
        # among the list's navigation actions, and `mypy --strict` refuses
        # an untyped call; both return `None` like the rest of them.
        if direction > 0:
            options.action_page_down()  # type: ignore[no-untyped-call]
        else:
            options.action_page_up()  # type: ignore[no-untyped-call]
        self._reveal_highlighted(options)
        self._keep_the_page_inside_the_viewport(options, origin, start)

    def _keep_the_page_inside_the_viewport(
        self, options: OptionList, origin: int | None, start: int
    ) -> None:
        """Pull a page that overshot back a row at a time, never past `origin`.

        `OptionList` pages in lines — it takes the highlighted row's own
        first line, adds or subtracts the viewport height, and moves to the
        row that line falls in — so the *cursor* never jumps more than a
        screenful. The *view* still can, because rows are not all the same
        height: landing on a taller row scrolls further than the anchor
        moved, and paging up snaps back to the start of the row the anchor
        landed inside, which at 36 columns (where a refused helm row wraps
        to five lines against an eight-line viewport) skipped several lines
        the user never saw.

        So the page is bounded here rather than recomputed here: step the
        cursor back towards `origin`, one row at a time, until the view has
        moved no further than one viewport. Two things make that walk
        finite, and neither of them asks the widget where it ended up. A
        page that could not advance — `PageDown` on the last row, `PageUp`
        on the first, either of them on a viewport too short to hold a row
        — leaves the highlight on `origin`, and is corrected not at all:
        the key has already re-asserted the scroll onto that row, and
        there is nothing to pull back from. A page that did advance walks
        `_pull_back_rows`, a tuple of the rows strictly between `origin`
        and where the page landed.

        The rows are set through the list's public `highlighted`, never
        through `action_cursor_up`/`_down`, because those two *wrap*:
        stepping up from row 0 lands on the last row, and a correction
        that started from a page which never moved then cycled through
        every row in the list for ever, inside one synchronous key
        handler. Assigning the index cannot leave the interval the page
        itself covered.
        """
        target = options.highlighted
        if origin is None or target is None or target == origin:
            return
        viewport = options.scrollable_content_region.height
        for row in _pull_back_rows(origin, target):
            if abs(options.scroll_offset.y - start) <= viewport:
                return
            options.highlighted = row
            self._reveal_highlighted(options)

    def action_edge(self, direction: int) -> None:
        """Move the cursor to the last row, or back to the first one."""
        options = self.query_one(OptionList)
        if direction > 0:
            options.action_last()
        else:
            options.action_first()
        self._reveal_highlighted(options)

    def _reveal_highlighted(self, options: OptionList) -> None:
        """Make sure the highlighted row is in the viewport after a key.

        `OptionList` scrolls from its `highlighted` watcher, so a key that
        lands on the row that is already highlighted — `End` at the end of
        the list, `Home` at the top — changes no reactive and scrolls
        nowhere. That is invisible until something else moved the viewport
        away from the highlight (a resize re-wrapping every row, say), and
        then the key the user reaches for to fix it does nothing at all.
        Re-asserting the scroll costs nothing when the highlight did move:
        `scroll_to_highlight` is idempotent.
        """
        options.scroll_to_highlight()

    def _activate_highlighted(self) -> None:
        options = self.query_one(OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return
        self._activate(options.get_option_at_index(highlighted).id)

    def _activate(self, option_id: str | None) -> None:
        """Dismiss with `option_id`'s entry, if that entry can run right now.

        The one gate between a refused row and a dispatch, and every
        selection path reaches it: `Enter` on the query `Input`, a click,
        and `OptionList`'s own `action_select`. Nothing upstream refuses
        for it — a refused row is a navigable row, so `OptionList` posts
        `OptionSelected` for it like any other — which is why the entry's
        own `availability.invocable` is checked here and nowhere else.
        """
        if option_id is None:
            return
        entry = self._visible.get(option_id)
        if entry is not None and entry.availability.invocable:
            self.dismiss(entry.id)

    def _render_results(self, query: str) -> None:
        """Render the rows matching `query`, starting on the first one.

        A new query is the only thing that rebuilds the rows. A resize
        never does: it changes the modal's height budget, and the results
        list follows that budget on its own.

        Every real catalog row is added as a *navigable* option, including
        one its owner has refused. Textual's `Option.disabled` would be the
        obvious spelling for "cannot run", but it answers a different
        question: `OptionList.render_line` picks the disabled component
        style before it looks at `highlighted`, and every navigation action
        skips disabled options outright (`find_next_enabled` answers `None`
        when none is enabled), so a disabled row can neither show a cursor
        nor be reached. Those rows are exactly the ones the palette exists
        to explain — a base install without helm refuses four of them at
        once, more than a viewport holds. So the refusal is carried by the
        row itself: an `Unavailable: …` second line, greyed by
        `_UNAVAILABLE_STYLE`, with `_activate` refusing to run it.

        The no-results row is the one genuinely disabled option: it has no
        entry behind it, and no id, so there is nothing to browse and
        nothing to run, and the cursor never lands on it.
        """
        options = self.query_one(OptionList)
        options.clear_options()
        self._visible = {}
        ranked = rank_entries(self._entries, query)
        if not ranked:
            options.add_option(Option(Text(_NO_RESULTS_PROMPT), disabled=True))
            return
        rendered: list[Option | None] = []
        previous_category: str | None = None
        for entry in ranked:
            if previous_category is not None and entry.category != previous_category:
                rendered.append(None)
            previous_category = entry.category
            self._visible[entry.id] = entry
            rendered.append(Option(_prompt_for(entry), id=entry.id))
        options.add_options(rendered)
        options.highlighted = 0
        self._reveal_highlighted(options)
