"""File-transfer dialogs (issue #47): the ctrl+t spec dialog and the
progress modal shown while a transfer streams.

The dialog only collects a ``TransferSpec``; validation, the upload approval
gate, auditing, and the stream itself are the app's responsibility.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, RadioButton, RadioSet, Static

from korvid.core.transfer import RemoteEntry, TransferSpec, default_local_path, validate_spec
from korvid.ui.messages import TransferCancelRequested
from korvid.ui.widgets.path_picker import LocalPathPickerScreen, RemotePathPickerScreen

RemoteLister = Callable[[str], Awaitable[list[RemoteEntry]]]

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
    itself never runs a transfer. The optional ctrl+o remote picker is the
    one cluster touch: injected read-only directory listings (issue #124).
    """

    CSS = _DIALOG_CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+o", "browse", "Browse", show=True),
    ]

    def __init__(self, target: str, remote_lister: RemoteLister | None = None) -> None:
        super().__init__()
        self._target = target
        self._remote_lister = remote_lister

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
            yield Static(
                "Enter = start    ^o = browse    Esc = cancel",
                classes="transfer-hint",
                markup=False,
            )

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
        # Verbatim: picker-selected names may legitimately end in whitespace
        # ("report" vs "report " are different files); strip only to decide
        # whether a field is blank.
        remote = self.query_one("#transfer-remote", Input).value
        remote = remote if remote.strip() else ""
        local = self.query_one("#transfer-local", Input).value
        local = local if local.strip() else ""
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

    def action_browse(self) -> None:
        """ctrl+o: open the picker for whichever path input has focus."""
        focused = self.focused.id if self.focused is not None else None
        if focused == "transfer-remote":
            self._browse_remote()
        elif focused == "transfer-local":
            self._browse_local()
        # Any other focus (direction radio): the binding is screen-level,
        # but browsing is advertised for the path fields only — do nothing.

    def _browse_local(self) -> None:
        # Verbatim (blank check aside): a directory name may end in
        # whitespace, and stripping would probe a different path.
        current = self.query_one("#transfer-local", Input).value
        start = _deepest_existing_dir(current if current.strip() else "")
        picker = LocalPathPickerScreen(start, select_dirs=self.direction == "download")
        self.app.push_screen(picker, self._apply_local_pick)

    def _apply_local_pick(self, result: str | None) -> None:
        field = self.query_one("#transfer-local", Input)
        if result is not None:
            if result.endswith(("/", os.sep)):
                # Directory choice: complete it with the remote basename when
                # one is already typed, otherwise leave the name to the user.
                # Basename taken verbatim — it may end in whitespace.
                remote = self.query_one("#transfer-remote", Input).value
                base = posixpath.basename(remote.rstrip("/")) if remote.strip() else ""
                if base:
                    result = str(Path(result) / base)
            field.value = result
        field.focus()

    def _browse_remote(self) -> None:
        if self._remote_lister is None:
            self.notify("remote browsing unavailable", severity="warning")
            return
        current = self.query_one("#transfer-remote", Input).value
        if not current.strip():
            current = ""
        start = current if current.endswith("/") else posixpath.dirname(current)
        if not posixpath.isabs(start):
            # A relative start would list the container's working directory
            # and produce selections that can never validate (absolute
            # remote paths required) — browse from the root instead.
            start = "/"
        picker = RemotePathPickerScreen(self._remote_lister, start)
        self.app.push_screen(picker, self._apply_remote_pick)

    def _apply_remote_pick(self, result: str | None) -> None:
        field = self.query_one("#transfer-remote", Input)
        if result is not None:
            if result.endswith("/"):
                # Directory choice: an upload destination gets the local
                # file's name appended; otherwise the user completes it.
                # Name taken verbatim — it may end in whitespace.
                local = self.query_one("#transfer-local", Input).value
                if self.direction == "upload" and local.strip():
                    result += Path(local).name
            field.value = result
        field.focus()


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


def _deepest_existing_dir(value: str) -> Path:
    """The deepest existing directory along ``value``; home when none fits."""
    if value:
        try:
            path = Path(value).expanduser()
        except RuntimeError:
            # "~no_such_user/f": same fallback validate_spec applies — never
            # let the expansion failure escape the dialog handler.
            return Path("~").expanduser()
        for candidate in (path, *path.parents):
            if candidate.is_dir():
                return candidate
    return Path("~").expanduser()


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(value)} B"  # pragma: no cover - loop always returns
