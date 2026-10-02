"""Preview and confirm an explicit namespace slot reallocation (issue #406)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from korvid.core.namespace_slots import SlotChange, SlotEntry


def _cell(entry: SlotEntry | None) -> str:
    return "-" if entry is None else entry.describe()


def change_lines(changes: Sequence[SlotChange]) -> list[str]:
    """One `n: before -> after` line per changed slot."""
    return [f"{c.slot}: {_cell(c.before)} -> {_cell(c.after)}" for c in changes]


class NamespaceSlotsScreen(ModalScreen[bool]):
    """Show the slots a reallocation changes; Enter confirms, Esc cancels.

    Confirming only dismisses with True. The controller saves and commits,
    so this screen never touches the map or the state file itself.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "confirm", "Reallocate", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    NamespaceSlotsScreen {
        align: center middle;
    }
    NamespaceSlotsScreen .slots-dialog {
        width: auto;
        max-width: 80%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    NamespaceSlotsScreen .slots-preview {
        height: auto;
        max-height: 80%;
    }
    NamespaceSlotsScreen .slots-hint {
        padding-top: 1;
    }
    """

    def __init__(self, changes: Sequence[SlotChange]) -> None:
        super().__init__()
        self.changes = tuple(changes)

    def compose(self) -> ComposeResult:
        with Vertical(classes="slots-dialog"):
            yield Static("Reallocate namespace shortcuts", classes="slots-title")
            with VerticalScroll(classes="slots-preview"):
                # markup=False: namespace names come from the cluster.
                yield Static("\n".join(change_lines(self.changes)), markup=False)
            yield Static(
                "Pinned favorites keep their keys. Enter = reallocate    Esc = cancel",
                classes="slots-hint",
            )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
