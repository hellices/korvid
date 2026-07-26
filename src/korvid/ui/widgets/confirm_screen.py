"""Approval dialogs for cluster write operations (spec §5 #4, §6.2).

ConfirmScreen shows the exact operation about to run and resolves True only
from a user keystroke — no agent tool can open, focus, or confirm it. The
layered variant (``require_name``) demands typing the resource name exactly,
used for high-blast-radius deletes. ReplicasPrompt collects a replica count
for scale operations.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static

_DIALOG_CSS = """
ConfirmScreen, ReplicasPrompt, ImagePrompt {
    align: center middle;
}
ConfirmScreen > VerticalScroll, ReplicasPrompt > Vertical, ImagePrompt > Vertical {
    width: 70;
    height: auto;
    max-height: 80%;
    border: heavy $error;
    padding: 1 2;
    background: $surface;
}
ConfirmScreen .confirm-title, ReplicasPrompt .confirm-title, ImagePrompt .confirm-title {
    text-style: bold;
}
ConfirmScreen .confirm-hint, ReplicasPrompt .confirm-hint, ImagePrompt .confirm-hint {
    color: $text-muted;
}
"""


class FreshKeysInput(Input):
    """Input that discards key events created before ``created_time``.

    Keystrokes buffered while the caller's pre-checks ran (an RBAC or
    manifest-fetch round trip) predate the dialog: they must never type
    into or submit a prompt the user has not yet seen.
    """

    def __init__(self, created_time: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._created_time = created_time

    async def _on_key(self, event: events.Key) -> None:
        if event.time < self._created_time:
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


class ConfirmScreen(ModalScreen[bool]):
    """y/n approval dialog; with ``require_name`` the exact resource name
    must be typed (cluster-scoped or otherwise high-blast-radius deletes)."""

    CSS = _DIALOG_CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(
        self,
        title: str,
        operation: str,
        *,
        require_name: str | None = None,
        preview: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._operation = operation
        self._require_name = require_name
        self._preview = preview
        # Same clock as event timestamps (Message.time): key events created
        # before this moment were buffered while the caller's pre-checks ran
        # and must never confirm an operation the user had not yet seen.
        # Captured at construction (always after those pre-checks) rather
        # than on_mount, which can be processed after a legitimate keystroke
        # on the already-visible dialog was created.
        self._created_time = Message().time

    def compose(self) -> ComposeResult:
        # A resize on a multi-container pod can produce more operation and
        # preview lines than a short terminal shows; a scrollable body keeps
        # every requested change reviewable before approval.
        with VerticalScroll():
            yield Static(self._title, classes="confirm-title", markup=False)
            yield Static(self._operation, classes="confirm-operation", markup=False)
            if self._preview is not None:
                yield Static(self._preview_text(), classes="confirm-preview")
            if self._require_name is None:
                yield Static("y = confirm    n/Esc = cancel", classes="confirm-hint")
            else:
                yield Static(
                    f"Type {self._require_name!r} and press Enter to confirm (Esc cancels)",
                    classes="confirm-hint",
                )
                yield FreshKeysInput(
                    self._created_time, placeholder=self._require_name, id="confirm-name"
                )

    def on_mount(self) -> None:
        if self._require_name is not None:
            self.query_one(Input).focus()

    def _preview_text(self) -> Text:
        """Server dry-run outcome (issue #19), one styled line per change:
        additions green, removals red, modifications yellow. An empty diff is
        rendered explicitly - 'the server reports no changes' is information,
        distinct from 'no preview was available' (no widget at all)."""
        text = Text("server dry-run preview:", style="bold")
        if not self._preview:
            text.append("\n  no changes reported", style="dim")
            return text
        styles = {"+": "green", "-": "red", "~": "yellow"}
        for line in self._preview:
            text.append("\n  ")
            text.append(line, style=styles.get(line[:1], "dim"))
        return text

    def on_key(self, event: events.Key) -> None:
        if self._require_name is None:
            if event.key == "y":
                event.stop()
                if event.time < self._created_time:
                    return  # buffered before the dialog existed: discard
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

    def __init__(self, target: str, *, current: int | None) -> None:
        super().__init__()
        self._target = target
        self._current = current

    def compose(self) -> ComposeResult:
        shown = "unknown" if self._current is None else str(self._current)
        with Vertical():
            yield Static(f"Scale {self._target}", classes="confirm-title", markup=False)
            yield Static(f"Current replicas: {shown}", markup=False)
            yield Static("Enter the new replica count (Esc cancels)", classes="confirm-hint")
            # Prefill the actual value (not just a placeholder) so Enter alone
            # keeps the current count; select-on-focus lets typing replace it.
            yield Input(
                value="" if self._current is None else str(self._current),
                id="replicas-input",
                type="integer",
                select_on_focus=True,
            )

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


class ImagePrompt(ModalScreen[str | None]):
    """Collects a custom container image reference for the debug fallback.

    Selecting the image is a read-only choice: the pod mutation itself is
    still gated by the ConfirmScreen that follows.
    """

    CSS = _DIALOG_CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, target: str) -> None:
        super().__init__()
        self._target = target

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"Custom debug image for {self._target}", classes="confirm-title", markup=False
            )
            yield Static("Enter an image reference (Esc cancels)", classes="confirm-hint")
            yield Input(placeholder="registry.example.com/tools/debug:latest", id="image-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted, "#image-input")
    def _submit(self, event: Input.Submitted) -> None:
        event.stop()
        image = event.value.strip()
        if not image:
            self.notify("Enter an image reference", severity="warning")
            return
        self.dismiss(image)

    def action_cancel(self) -> None:
        self.dismiss(None)
