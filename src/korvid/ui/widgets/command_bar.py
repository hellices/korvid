from __future__ import annotations

from textual.events import Key
from textual.widgets import Input

from korvid.ui.command import parse_command


class CommandBar(Input):
    """Hidden `:` command input; Enter dispatches onto the UI Bus."""

    def on_mount(self) -> None:
        self.display = False

    def open(self) -> None:
        self.value = ""
        self.display = True
        self.focus()

    def dismiss_bar(self) -> None:
        self.display = False
        self.value = ""

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.post_message(parse_command(event.value))
        self.dismiss_bar()

    async def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss_bar()
