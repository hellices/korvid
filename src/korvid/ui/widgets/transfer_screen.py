"""File-transfer dialogs (issue #47): the ctrl+t spec dialog and the
progress modal shown while a transfer streams.

The dialog only collects a ``TransferSpec``; validation, the upload approval
gate, auditing, and the stream itself are the app's responsibility.
"""

from __future__ import annotations

from typing import ClassVar

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, RadioButton, RadioSet, Static

from korvid.core.transfer import TransferSpec, default_local_path, validate_spec
from korvid.ui.messages import TransferCancelRequested

_DIALOG_CSS = """
TransferScreen, TransferProgressScreen {
    align: center middle;
}
TransferScreen > Vertical, TransferProgressScreen > Vertical {
    width: 76;
    max-width: 90%;
    height: auto;
    max-height: 80%;
    border: round $accent;
    background: $surface;
    padding: 1 2;
}
TransferScreen .transfer-title, TransferProgressScreen .transfer-title {
    text-style: bold;
    margin-bottom: 1;
}
TransferScreen .transfer-hint {
    color: $text-muted;
    margin-top: 1;
}
TransferScreen RadioSet {
    width: 100%;
    layout: horizontal;
    margin-bottom: 1;
}
TransferScreen Input {
    margin-bottom: 1;
}
"""


class TransferScreen(ModalScreen[TransferSpec | None]):
    """Direction + remote/local path dialog; dismisses with the spec.

    A submitted spec is pre-validated here (so typos keep the dialog open
    with a toast) but the caller re-validates before running — the dialog
    never touches the cluster.
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
            yield Static(f"Transfer: {self._target}", classes="transfer-title", markup=False)
            with RadioSet(id="transfer-direction"):
                yield RadioButton("Download from pod", value=True, id="direction-download")
                yield RadioButton("Upload to pod", id="direction-upload")
            yield Input(
                placeholder="remote path in container, e.g. /var/log/app.log",
                id="transfer-remote",
            )
            yield Input(
                # Mirrors default_local_path: ~/Downloads when present,
                # the home directory otherwise.
                placeholder="local path (empty = ~/Downloads/<name>, or ~/<name>)",
                id="transfer-local",
            )
            yield Static("Enter = start    Esc = cancel", classes="transfer-hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#transfer-remote", Input).focus()

    @property
    def direction(self) -> str:
        return "upload" if self.query_one("#direction-upload", RadioButton).value else "download"

    def select_upload(self) -> None:
        """Switch the dialog to upload mode (used by tests and key handling)."""
        self.query_one("#direction-upload", RadioButton).value = True

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        event.stop()
        direction = self.direction
        remote = self.query_one("#transfer-remote", Input).value.strip()
        local = self.query_one("#transfer-local", Input).value.strip()
        if direction == "download" and not local and remote:
            local = default_local_path(remote)
        spec = TransferSpec(direction=direction, remote_path=remote, local_path=local)  # type: ignore[arg-type]  # direction is one of the two literals by construction
        error = validate_spec(spec)
        if error is not None:
            self.notify(error, severity="warning")
            return
        self.dismiss(spec)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TransferProgressScreen(ModalScreen[None]):
    """Byte-count progress while a transfer streams; escape cancels it.

    The cancel keystroke only *requests* cancellation: the app cancels the
    transfer task, audits the aborted transfer, and dismisses this screen.
    """

    CSS = _DIALOG_CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel transfer", show=True),
    ]

    def __init__(self, description: str) -> None:
        super().__init__()
        self._description = description
        self._cancelling = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._description, classes="transfer-title", markup=False)
            yield Static("0 B", id="transfer-progress", markup=False)
            yield Static("Esc = cancel", classes="transfer-hint", markup=False)

    def update_progress(self, transferred: int) -> None:
        if self.is_mounted and not self._cancelling:
            self.query_one("#transfer-progress", Static).update(_human_bytes(transferred))

    def action_cancel(self) -> None:
        self._cancelling = True
        self.query_one("#transfer-progress", Static).update("cancelling…")
        self.post_message(TransferCancelRequested())

    def on_key(self, event: events.Key) -> None:
        # Swallow everything else: the transfer owns the app until it ends.
        if event.key != "escape":
            event.stop()


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(value)} B"  # pragma: no cover - loop always returns
