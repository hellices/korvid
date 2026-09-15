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
from textual import on
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


def _prompt_for(entry: PaletteEntry) -> Text:
    """Build the two-line prompt for `entry`.

    Always a `Text` object, never a raw `str`: Textual renders a plain
    `str` `Option` prompt as Rich console markup, which would misrender a
    catalog-derived title, description, or unavailable-reason that happens
    to contain `[`/`]`. Wrapping in `Text` keeps that content literal.
    """
    if entry.availability.invocable:
        second_line = entry.description
    else:
        reason = entry.availability.reason
        message = reason.message if reason is not None else "unavailable"
        second_line = f"Unavailable: {message}"
    lines = [entry.title]
    if second_line:
        lines.append(second_line)
    return Text("\n".join(lines))


class ActionPaletteScreen(ModalScreen[str | None]):
    """Search-filtered, keyboard-first list over `PaletteEntry` values.

    Dismisses with the selected entry's stable `id`, or `None` on cancel or
    when there is nothing selectable to activate. The caller — not this
    screen — re-resolves that id against live entries and dispatches it;
    this screen never invokes an action or command itself.
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape,ctrl+p", "cancel", "Close", show=False),
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

    DEFAULT_CSS = """
    ActionPaletteScreen {
        align: center middle;
    }
    ActionPaletteScreen #action-palette {
        width: 76;
        max-width: 94%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    ActionPaletteScreen #action-results {
        height: auto;
        max-height: 18;
    }
    ActionPaletteScreen #action-hint {
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(self, entries: Sequence[PaletteEntry]) -> None:
        super().__init__()
        self._entries = tuple(entries)
        self._visible: dict[str, PaletteEntry] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="action-palette"):
            yield Input(placeholder="Search actions and commands", id="action-query")
            yield OptionList(id="action-results")
            yield Static("Enter run · Esc close", id="action-hint", markup=False)

    def on_mount(self) -> None:
        self._render_results("")
        self.query_one(Input).focus()

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
        options = self.query_one(OptionList)
        if direction > 0:
            options.action_cursor_down()
        else:
            options.action_cursor_up()

    def action_page(self, direction: int) -> None:
        options = self.query_one(OptionList)
        if direction > 0:
            # Textual's own `action_page_down`/`action_page_up` have no
            # return-type annotation, so mypy --strict flags calling them
            # as an untyped call; this is a gap in Textual's public API,
            # not ours, and there is no typed alternative to reach the same
            # page-navigation behavior other widgets get from these keys.
            options.action_page_down()  # type: ignore[no-untyped-call]
        else:
            options.action_page_up()  # type: ignore[no-untyped-call]

    def action_edge(self, direction: int) -> None:
        options = self.query_one(OptionList)
        if direction > 0:
            options.action_last()
        else:
            options.action_first()

    def _activate_highlighted(self) -> None:
        options = self.query_one(OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return
        self._activate(options.get_option_at_index(highlighted).id)

    def _activate(self, option_id: str | None) -> None:
        if option_id is None:
            return
        entry = self._visible.get(option_id)
        if entry is not None and entry.availability.invocable:
            self.dismiss(entry.id)

    def _render_results(self, query: str) -> None:
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
            rendered.append(
                Option(
                    _prompt_for(entry),
                    id=entry.id,
                    disabled=not entry.availability.invocable,
                )
            )
        options.add_options(rendered)
        options.highlighted = 0
