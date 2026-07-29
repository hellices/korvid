"""Path picker modals for the transfer dialog (issue #124).

`LocalPathPickerScreen` browses the local filesystem with Textual's
`DirectoryTree`; `RemotePathPickerScreen` browses the container over the
exec API, one `ls` round-trip per directory. Both dismiss with the chosen
path string — a trailing slash marks a directory choice — or None when
cancelled. Browsing is a convenience layer: every failure degrades to the
manual path entry the dialog already has.
"""

from __future__ import annotations

import posixpath
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DirectoryTree, OptionList, Static
from textual.widgets.option_list import Option

from korvid.core.transfer import RemoteEntry, TransferError

RemoteLister = Callable[[str], Awaitable[list[RemoteEntry]]]

_PICKER_CSS = """
LocalPathPickerScreen, RemotePathPickerScreen {
    align: center middle;
}
LocalPathPickerScreen > Vertical, RemotePathPickerScreen > Vertical {
    width: 76;
    max-width: 90%;
    height: auto;
    max-height: 80%;
    border: round $accent;
    background: $surface;
    padding: 1 2;
}
LocalPathPickerScreen .picker-title, RemotePathPickerScreen .picker-title {
    text-style: bold;
    margin-bottom: 1;
}
LocalPathPickerScreen .picker-hint, RemotePathPickerScreen .picker-hint {
    color: $text-muted;
    margin-top: 1;
}
LocalPathPickerScreen DirectoryTree, RemotePathPickerScreen OptionList {
    height: auto;
    max-height: 60%;
}
"""


class LocalPathPickerScreen(ModalScreen[str | None]):
    """Browse the local filesystem; dismisses with the chosen path.

    Selecting a file dismisses with its path. `s` chooses the directory
    under the cursor (its parent for a file cursor) and dismisses with the
    directory path plus a trailing separator — the caller decides what
    filename to append.
    """

    CSS = _PICKER_CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("s", "select_dir", "Select dir", show=True),
    ]

    def __init__(self, start: Path, *, select_dirs: bool) -> None:
        super().__init__()
        self._start = start
        self._select_dirs = select_dirs

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Local: {self._start}", classes="picker-title", markup=False)
            yield DirectoryTree(self._start)
            hint = "Enter = pick file    Esc = cancel"
            if self._select_dirs:
                hint = "Enter = pick file    s = pick directory    Esc = cancel"
            yield Static(hint, classes="picker-hint", markup=False)

    def on_mount(self) -> None:
        self.query_one(DirectoryTree).focus()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        event.stop()
        self.dismiss(str(event.path))

    def action_select_dir(self) -> None:
        if not self._select_dirs:
            self.notify("uploads need a source file, not a directory", severity="warning")
            return
        node = self.query_one(DirectoryTree).cursor_node
        path = self._start if node is None or node.data is None else Path(node.data.path)
        if not path.is_dir():
            path = path.parent
        self.dismiss(str(path).rstrip("/") + "/")

    def action_cancel(self) -> None:
        self.dismiss(None)


class RemotePathPickerScreen(ModalScreen[str | None]):
    """Browse the container filesystem one `ls` round-trip per directory.

    Enter descends into a directory or dismisses with a file's full path;
    `s` dismisses with the current directory plus a trailing slash. When
    the *initial* listing fails (no `ls` in a distroless image, exec
    forbidden) the picker closes with an explanatory toast and the dialog
    keeps working exactly as before — browsing is never a gate.
    """

    CSS = _PICKER_CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("s", "select_dir", "Select dir", show=True),
    ]

    def __init__(self, lister: RemoteLister, start: str) -> None:
        super().__init__()
        self._lister = lister
        self._path = start or "/"
        self._entries: list[RemoteEntry] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Remote: {self._path}", classes="picker-title", markup=False)
            yield OptionList()
            yield Static(
                "Enter = open / pick file    s = pick this directory    Esc = cancel",
                classes="picker-hint",
                markup=False,
            )

    async def on_mount(self) -> None:
        await self._load(self._path, initial=True)

    async def _load(self, path: str, *, initial: bool = False) -> None:
        path = posixpath.normpath(path) if path != "/" else "/"
        try:
            entries = await self._lister(path)
        except TransferError as exc:
            if initial:
                self.app.notify(
                    "directory listing unavailable in this container — "
                    f"enter the path manually ({exc})",
                    severity="warning",
                )
                self.dismiss(None)
            else:
                self.notify(f"cannot list {path}: {exc}", severity="warning")
            return
        self._path = path
        self._entries = entries
        options = self.query_one(OptionList)
        options.clear_options()
        if path != "/":
            options.add_option(Option("../"))
        for entry in entries:
            options.add_option(Option(f"{entry.name}/" if entry.is_dir else entry.name))
        if options.option_count:
            options.highlighted = 0
        self.query_one(".picker-title", Static).update(f"Remote: {path}")

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if self._path != "/":
            if index == 0:
                await self._load(posixpath.dirname(self._path.rstrip("/")) or "/")
                return
            index -= 1
        entry = self._entries[index]
        target = posixpath.join(self._path, entry.name)
        if entry.is_dir:
            await self._load(target)
        else:
            self.dismiss(target)

    def action_select_dir(self) -> None:
        self.dismiss(self._path.rstrip("/") + "/")

    def action_cancel(self) -> None:
        self.dismiss(None)
