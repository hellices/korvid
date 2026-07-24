"""Namespace picker — opened by bare `:ns`, Enter selects, Esc dismisses."""

from __future__ import annotations

from textual.events import Key
from textual.widgets import OptionList

from korvid.ui.messages import NavigateCommand


class NamespacePicker(OptionList):
    def on_mount(self) -> None:
        self.display = False

    def open(self, namespaces: list[str]) -> None:
        self.clear_options()
        self.add_options(namespaces)
        self.highlighted = 0
        self.display = True
        self.focus()

    def dismiss_picker(self) -> None:
        self.display = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss_picker()
        self.post_message(NavigateCommand("pods", namespace=str(event.option.prompt)))

    async def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss_picker()
            event.stop()
