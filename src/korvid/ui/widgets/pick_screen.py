"""Small centered pick modal — Enter selects an option, Esc dismisses with None."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static


class PickScreen(ModalScreen[str | None]):
    """Modal option picker used for container selection and yes/no fallbacks."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    PickScreen {
        align: center middle;
    }
    PickScreen Vertical {
        width: auto;
        max-width: 80%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    PickScreen #pick-title-scroll {
        height: auto;
        max-height: 50%;
    }
    PickScreen #pick-title {
        padding-bottom: 1;
    }
    PickScreen OptionList {
        width: auto;
        min-width: 30;
        height: auto;
        max-height: 16;
    }
    """

    def __init__(self, title: str, options: list[str]) -> None:
        super().__init__()
        self._pick_title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical():
            # The title may carry multi-line detail (e.g. helm stderr in the
            # render-failure dialog): scroll it inside a capped area so the
            # options below always stay visible and reachable.
            with VerticalScroll(id="pick-title-scroll"):
                # markup=False: titles may embed pod/container names from the cluster.
                yield Static(self._pick_title, id="pick-title", markup=False)
            yield OptionList(*self._options)

    def on_mount(self) -> None:
        self._focus_options()

    def _focus_options(self, *, retry: bool = True) -> None:
        try:
            option_list = self.query_one(OptionList)
        except NoMatches:
            # `on_mount` can fire before compose children are queryable; try
            # again once the screen has refreshed rather than taking the app
            # down. Only once, so a screen that somehow never composes an
            # option list fails quietly instead of looping forever.
            if retry:
                self.call_after_refresh(self._focus_options, retry=False)
            return
        option_list.highlighted = 0
        option_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(str(event.option.prompt))

    def action_cancel(self) -> None:
        self.dismiss(None)
