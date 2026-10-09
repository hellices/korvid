"""Namespace picker — opened by bare `:ns`, Enter selects, Esc dismisses."""

from __future__ import annotations

from rich.text import Text
from textual.events import Key
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from korvid.core.namespace_slots import SlotMap
from korvid.ui.messages import NavigateCommand


def _options(namespaces: list[str], slots: SlotMap | None) -> list[Option]:
    """Listed names labelled with their 1-9 slot (issue #406).

    The label is display only: each option's id is the namespace, so
    selection never parses a label. Slots whose namespace the listing lacks
    follow; only unavailable ones are disabled, matching what the number key
    would refuse (a pin is never judged by discovery).
    """
    if slots is None:
        return [Option(Text(name), id=name) for name in namespaces]
    by_name = {entry.namespace: slot for slot, entry in slots.items()}
    options = [
        Option(Text(f"{by_name[name]}  {name}" if name in by_name else f"   {name}"), id=name)
        for name in namespaces
    ]
    listed = set(namespaces)
    options.extend(
        Option(
            Text(f"{slot}  {entry.namespace}" + ("" if entry.available else " (unavailable)")),
            id=entry.namespace,
            disabled=not entry.available,
        )
        for slot, entry in slots.items()
        if entry.namespace not in listed
    )
    return options


class NamespacePicker(OptionList):
    def on_mount(self) -> None:
        self.display = False

    def open(self, namespaces: list[str], slots: SlotMap | None = None) -> None:
        self.clear_options()
        self.add_options(_options(namespaces, slots))
        self.highlighted = 0
        self.display = True
        self.focus()

    def dismiss_picker(self) -> None:
        self.display = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss_picker()
        namespace = event.option.id
        if namespace is not None:
            self.post_message(NavigateCommand(None, namespace=namespace))

    async def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss_picker()
            event.stop()
