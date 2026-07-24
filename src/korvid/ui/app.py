"""KorvidApp — constructed with injected dependencies (composition in __main__)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Static

from korvid.core.config import KorvidConfig
from korvid.core.errors import explain_api_error
from korvid.core.store import ALL_NAMESPACES, ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.ui.messages import (
    ClearFilter,
    FilterCommand,
    NavigateCommand,
    QuitCommand,
    ResourcesUpdated,
    ShowError,
    ShowNamespacePicker,
    UnknownCommand,
)
from korvid.ui.shell import build_exec_argv
from korvid.ui.widgets.command_bar import CommandBar
from korvid.ui.widgets.describe_screen import DescribeScreen
from korvid.ui.widgets.filter_bar import FilterBar
from korvid.ui.widgets.namespace_picker import NamespacePicker
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.status_bar import StatusBar

_DEFAULT_ALIASES: dict[str, ResourceMeta] = {
    "pods": PODS_META,
    "po": PODS_META,
    "pod": PODS_META,
}


class KorvidApp(App[None]):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("colon", "open_command", "Command"),
        ("slash", "open_filter", "Filter"),
        ("0", "toggle_all_namespaces", "All NS"),
        ("d", "describe", "Describe"),
        ("s", "shell", "Shell"),
    ]

    def __init__(
        self,
        config: KorvidConfig,
        store: ResourceStore,
        watch_manager: WatchManager,
        list_namespaces: Callable[[], Awaitable[list[str]]] | None = None,
        aliases: dict[str, ResourceMeta] | None = None,
        get_manifest: (Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None) = None,
        get_events: (Callable[[str, str], Awaitable[list[dict[str, Any]]]] | None) = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.store = store
        self.watch_manager = watch_manager
        self._list_namespaces = list_namespaces
        self._get_manifest = get_manifest
        self._get_events = get_events
        self.aliases: dict[str, ResourceMeta] = (
            aliases if aliases is not None else dict(_DEFAULT_ALIASES)
        )
        self.current_kind: str = "pods"
        self.current_scope: str = config.namespace or "default"
        self.filter_pattern = ""

    @property
    def current_namespace(self) -> str:
        """Alias for current_scope; kept for backward-compatible test access."""
        return self.current_scope

    @current_namespace.setter
    def current_namespace(self, value: str) -> None:
        self.current_scope = value

    def compose(self) -> ComposeResult:
        yield Header()
        yield ResourceTable()
        empty_state = Static(id="empty-state")
        empty_state.display = False  # hidden until the first store notification
        yield empty_state
        yield CommandBar()
        yield FilterBar()
        yield NamespacePicker()
        yield StatusBar()
        yield Footer()

    async def on_mount(self) -> None:
        # Wire the `known` closure into CommandBar so parse_command can resolve aliases.
        self.query_one(CommandBar).known = lambda a: (
            self.aliases[a].plural if a in self.aliases else None
        )

        # Both callbacks fire from watch tasks on the same loop; post_message is
        # loop-safe. Watch tasks are cancelled in on_unmount before shutdown to
        # avoid posting to a closing app.
        def _on_store_update(kind: str) -> None:
            self.post_message(ResourcesUpdated(kind))

        def _on_watch_error(detail: str) -> None:
            self.post_message(ShowError("Watch failed", detail))

        self.store.subscribe(_on_store_update)
        self.watch_manager.on_error = _on_watch_error
        await self.watch_manager.start(self.current_kind, self.current_scope)
        self._refresh_status()

    def on_resources_updated(self, message: ResourcesUpdated) -> None:
        self._render_table(message.kind)

    def _render_table(self, kind: str) -> None:
        """Single choke point: table rows and empty-state always update together."""
        table = self.query_one(ResourceTable)
        rows = self.store.get(kind, self.current_scope)
        all_namespaces = self.current_scope == ALL_NAMESPACES
        table.show(kind, rows, all_namespaces=all_namespaces, pattern=self.filter_pattern)
        self._refresh_empty_state(kind, table.row_count)

    def on_show_error(self, message: ShowError) -> None:
        self.notify(message.detail, title=message.title, severity="error")

    def action_open_command(self) -> None:
        # Dismiss the filter bar first so no invisible filter stays active.
        self.query_one(FilterBar).dismiss_bar()
        self.query_one(CommandBar).open()

    def action_open_filter(self) -> None:
        # Dismiss the command bar first to enforce mutual exclusion.
        self.query_one(CommandBar).dismiss_bar()
        self.query_one(FilterBar).open()

    def on_filter_command(self, message: FilterCommand) -> None:
        self.filter_pattern = message.pattern
        self.post_message(ResourcesUpdated(self.current_kind))

    def on_clear_filter(self, message: ClearFilter) -> None:
        self.filter_pattern = ""
        self.post_message(ResourcesUpdated(self.current_kind))

    async def on_navigate_command(self, message: NavigateCommand) -> None:
        new_kind = message.view if message.view is not None else self.current_kind
        new_scope = message.namespace if message.namespace is not None else self.current_scope
        if new_kind != self.current_kind or new_scope != self.current_scope:
            await self.watch_manager.stop(self.current_kind, self.current_scope)
            self.current_kind = new_kind
            self.current_scope = new_scope
            await self.watch_manager.start(self.current_kind, self.current_scope)
        self.post_message(ResourcesUpdated(self.current_kind))
        self._refresh_status()

    async def action_toggle_all_namespaces(self) -> None:
        """Toggle scope between ALL_NAMESPACES and the config-default namespace."""
        if self.current_scope == ALL_NAMESPACES:
            new_scope = self.config.namespace or "default"
        else:
            new_scope = ALL_NAMESPACES
        await self.watch_manager.stop(self.current_kind, self.current_scope)
        self.current_scope = new_scope
        await self.watch_manager.start(self.current_kind, self.current_scope)
        self._render_table(self.current_kind)
        self._refresh_status()

    async def on_show_namespace_picker(self, message: ShowNamespacePicker) -> None:
        if self._list_namespaces is None:
            self.notify("Namespace listing unavailable", severity="warning")
            return
        try:
            namespaces = await self._list_namespaces()
        except ApiStatusError as exc:  # API failures get the actionable mapping (§5-5)
            msg = explain_api_error(exc.status, exc.reason, "namespaces", None)
            self.notify(msg, title="Failed to list namespaces", severity="error")
            return
        except Exception as exc:  # surface any other listing failure to the user
            self.notify(str(exc), title="Failed to list namespaces", severity="error")
            return
        if not namespaces:
            self.notify("No namespaces visible (check RBAC)", severity="warning")
            return
        self.query_one(NamespacePicker).open(namespaces)

    def on_quit_command(self, message: QuitCommand) -> None:
        self.exit()

    async def action_describe(self) -> None:
        """Fetch and display the manifest + events for the currently highlighted row."""
        if self._get_manifest is None:
            self.notify("Describe unavailable", severity="warning")
            return

        table = self.query_one(ResourceTable)
        if table.row_count == 0:
            self.notify("No resource selected", severity="warning")
            return

        # cursor_row is the index; ordered_rows gives us Row objects with .key
        row_index = table.cursor_row
        ordered = table.ordered_rows
        if row_index >= len(ordered):
            self.notify("No resource selected", severity="warning")
            return

        row_key = str(ordered[row_index].key.value)  # "namespace/name"
        parts = row_key.split("/", 1)
        if len(parts) != 2:
            self.notify("Cannot determine resource from selection", severity="warning")
            return
        namespace, name = parts[0], parts[1]
        ns: str | None = namespace if namespace else None

        try:
            manifest = await self._get_manifest(self.current_kind, ns, name)
        except ApiStatusError as exc:
            msg = explain_api_error(exc.status, exc.reason, self.current_kind, namespace or None)
            self.notify(msg, severity="error")
            return

        events: list[dict[str, Any]] = []
        if self._get_events is not None and ns is not None:
            try:
                events = await self._get_events(namespace, name)
            except ApiStatusError as exc:
                # Events are best-effort; surface but still show the manifest.
                msg = explain_api_error(exc.status, exc.reason, "events", namespace)
                self.notify(msg, severity="warning")

        title = f"{self.current_kind}/{namespace}/{name}"
        await self.push_screen(DescribeScreen(title, manifest, events))

    def on_unknown_command(self, message: UnknownCommand) -> None:
        self.notify(f"Unknown command: {message.text}", severity="warning")

    def action_shell(self) -> None:
        """Drop into a shell inside the selected pod via kubectl exec."""
        if self.current_kind != "pods":
            self.notify("Shell is only available for pods", severity="warning")
            return

        table = self.query_one(ResourceTable)
        if table.row_count == 0:
            self.notify("No resource selected", severity="warning")
            return

        row_index = table.cursor_row
        ordered = table.ordered_rows
        if row_index >= len(ordered):
            self.notify("No resource selected", severity="warning")
            return

        row_key = str(ordered[row_index].key.value)  # "namespace/name"
        parts = row_key.split("/", 1)
        if len(parts) != 2:
            self.notify("Cannot determine resource from selection", severity="warning")
            return
        namespace, name = parts[0], parts[1]

        if shutil.which("kubectl") is None:
            self.notify(
                "kubectl not found on PATH — shell-in requires kubectl",
                severity="error",
            )
            return

        argv = build_exec_argv(namespace, name)
        with self.suspend():
            subprocess.call(argv)
        self.refresh()

    def _refresh_status(self) -> None:
        label = "AI on" if self.config.agent_enabled else "AI off"
        self.query_one(StatusBar).update_status(self.config.kube_context, self.current_scope, label)

    def _refresh_empty_state(self, kind: str, visible_rows: int) -> None:
        """Show guidance instead of a silent blank table (empty ns or no filter match)."""
        empty = self.query_one("#empty-state", Static)
        if visible_rows > 0:
            empty.display = False
            return
        if self.filter_pattern:
            message = f"No {kind} matching '{self.filter_pattern}' — Esc to clear the filter"
        else:
            message = f"No {kind} in namespace '{self.current_scope}' — :ns <name> to switch"
        empty.update(message)
        empty.display = True

    async def on_unmount(self) -> None:
        await self.watch_manager.stop_all()
