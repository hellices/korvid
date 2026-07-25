"""Approval dialogs for cluster write operations (spec §5 #4, §6.2).

ConfirmScreen shows the exact operation about to run and resolves True only
from a user keystroke — no agent tool can open, focus, or confirm it. The
layered variant (``require_name``) demands typing the resource name exactly,
used for high-blast-radius deletes. ReplicasPrompt collects a replica count
for scale operations.
"""

from __future__ import annotations

from typing import ClassVar

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

_DIALOG_CSS = """
ConfirmScreen, ReplicasPrompt {
    align: center middle;
}
ConfirmScreen > Vertical, ReplicasPrompt > Vertical {
    width: 70;
    height: auto;
    border: heavy $error;
    padding: 1 2;
    background: $surface;
}
ConfirmScreen .confirm-title, ReplicasPrompt .confirm-title {
    text-style: bold;
}
ConfirmScreen .confirm-hint, ReplicasPrompt .confirm-hint {
    color: $text-muted;
}
"""


class ConfirmScreen(ModalScreen[bool]):
    """y/n approval dialog; with ``require_name`` the exact resource name
    must be typed (cluster-scoped or otherwise high-blast-radius deletes)."""

    CSS = _DIALOG_CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, title: str, operation: str, *, require_name: str | None = None) -> None:
        super().__init__()
        self._title = title
        self._operation = operation
        self._require_name = require_name

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title, classes="confirm-title", markup=False)
            yield Static(self._operation, classes="confirm-operation", markup=False)
            if self._require_name is None:
                yield Static("y = confirm    n/Esc = cancel", classes="confirm-hint")
            else:
                yield Static(
                    f"Type {self._require_name!r} and press Enter to confirm (Esc cancels)",
                    classes="confirm-hint",
                )
                yield Input(placeholder=self._require_name, id="confirm-name")

    def on_mount(self) -> None:
        if self._require_name is not None:
            self.query_one(Input).focus()

    def on_key(self, event: events.Key) -> None:
        if self._require_name is None:
            if event.key == "y":
                event.stop()
                self.dismiss(True)
            elif event.key == "n":
                event.stop()
                self.dismiss(False)

    @on(Input.Submitted, "#confirm-name")
    def _check_name(self, event: Input.Submitted) -> None:
        event.stop()
        if event.value == self._require_name:
            self.dismiss(True)
        else:
            self.notify(
                f"Name does not match {self._require_name!r}",
                severity="warning",
            )

    def action_cancel(self) -> None:
        self.dismiss(False)


class ReplicasPrompt(ModalScreen[int | None]):
    """Collects the target replica count for a scale operation."""

    CSS = _DIALOG_CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, target: str, *, current: int) -> None:
        super().__init__()
        self._target = target
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Scale {self._target}", classes="confirm-title", markup=False)
            yield Static(f"Current replicas: {self._current}", markup=False)
            yield Static("Enter the new replica count (Esc cancels)", classes="confirm-hint")
            yield Input(placeholder=str(self._current), id="replicas-input", type="integer")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted, "#replicas-input")
    def _submit(self, event: Input.Submitted) -> None:
        event.stop()
        try:
            replicas = int(event.value)
        except ValueError:
            self.notify("Enter a whole number", severity="warning")
            return
        if replicas < 0:
            self.notify("Replicas cannot be negative", severity="warning")
            return
        self.dismiss(replicas)

    def action_cancel(self) -> None:
        self.dismiss(None)
