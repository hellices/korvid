from __future__ import annotations

from textual.widgets import Input

from korvid.ui.messages import ClearFilter, FilterCommand


class FilterBar(Input):
    """`/` live filter; Esc clears."""

    def on_mount(self) -> None:
        self.display = False

    def open(self) -> None:
        self.value = ""
        self.display = True
        self.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.post_message(FilterCommand(event.value))

    async def on_key(self, event) -> None:  # type: ignore[no-untyped-def]  # Textual event union
        if event.key == "escape":
            self.display = False
            self.value = ""
            self.post_message(ClearFilter())
            event.stop()
