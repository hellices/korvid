"""KorvidApp — constructed with injected dependencies (composition in __main__)."""

from __future__ import annotations

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.ui.messages import (
    NavigateCommand,
    QuitCommand,
    ResourcesUpdated,
    ShowError,
    UnknownCommand,
)
from korvid.ui.widgets.command_bar import CommandBar
from korvid.ui.widgets.resource_table import ResourceTable


class KorvidApp(App[None]):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("colon", "open_command", "Command"),
    ]

    def __init__(
        self,
        config: KorvidConfig,
        store: ResourceStore,
        watch_manager: WatchManager,
    ) -> None:
        super().__init__()
        self.config = config
        self.store = store
        self.watch_manager = watch_manager
        self.current_namespace = config.namespace or "default"
        self.filter_pattern = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield ResourceTable()
        yield CommandBar()
        yield Footer()

    async def on_mount(self) -> None:
        # Both callbacks fire from watch tasks on the same loop; post_message is
        # loop-safe. Watch tasks are cancelled in on_unmount before shutdown to
        # avoid posting to a closing app.
        def _on_store_update(kind: str) -> None:
            self.post_message(ResourcesUpdated(kind))

        def _on_watch_error(detail: str) -> None:
            self.post_message(ShowError("Watch failed", detail))

        self.store.subscribe(_on_store_update)
        self.watch_manager.on_error = _on_watch_error
        await self.watch_manager.start("pods", self.current_namespace)

    def on_resources_updated(self, message: ResourcesUpdated) -> None:
        table = self.query_one(ResourceTable)
        table.update_rows(self.store.get(message.kind, self.current_namespace), self.filter_pattern)

    def on_show_error(self, message: ShowError) -> None:
        self.notify(message.detail, title=message.title, severity="error")

    def action_open_command(self) -> None:
        self.query_one(CommandBar).open()

    async def on_navigate_command(self, message: NavigateCommand) -> None:
        if message.namespace and message.namespace != self.current_namespace:
            await self.watch_manager.stop("pods", self.current_namespace)
            self.current_namespace = message.namespace
            await self.watch_manager.start("pods", self.current_namespace)
        self.post_message(ResourcesUpdated("pods"))

    def on_quit_command(self, message: QuitCommand) -> None:
        self.exit()

    def on_unknown_command(self, message: UnknownCommand) -> None:
        self.notify(f"Unknown command: {message.text}", severity="warning")

    async def on_unmount(self) -> None:
        await self.watch_manager.stop_all()
