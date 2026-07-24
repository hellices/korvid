from __future__ import annotations

from textual.events import Key
from textual.widgets import Input

from korvid.ui.messages import ClearFilter, FilterCommand


class FilterBar(Input):
    """`/` live filter; Enter keeps the filter and closes the bar, Esc clears."""

    def on_mount(self) -> None:
        self.display = False
        self.placeholder = "/ filter pods (Enter keep, Esc clear)"

    def open(self) -> None:
        self.value = ""
        self.display = True
        self.focus()

    def dismiss_bar(self) -> None:
        # Switching away from the filter bar: hide it and clear any active
        # filter so no invisible filter remains active in the background.
        self.display = False
        self.post_message(ClearFilter())

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.post_message(FilterCommand(event.value))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter: keep the active filter, just close the bar so app bindings
        # (q, :, /) work again. Esc remains the way to clear the filter.
        event.stop()
        self.display = False

    async def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.display = False
            # Do NOT set self.value = "" here: that fires on_input_changed →
            # FilterCommand("") → one table rebuild, then ClearFilter triggers
            # a second rebuild. Value is reset in open() before next use.
            self.post_message(ClearFilter())
            event.stop()
