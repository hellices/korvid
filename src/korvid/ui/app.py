"""KorvidApp — constructed with injected dependencies (composition in __main__)."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.widgets import Footer, Header, Static

from korvid.core.config import KorvidConfig
from korvid.core.errors import explain_api_error
from korvid.core.logbuffer import LogBuffer
from korvid.core.store import ALL_NAMESPACES, ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.k8s.models import PodSummary
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
from korvid.ui.widgets.log_pane import LogPane
from korvid.ui.widgets.namespace_picker import NamespacePicker
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.status_bar import StatusBar

_DEFAULT_ALIASES: dict[str, ResourceMeta] = {
    "pods": PODS_META,
    "po": PODS_META,
    "pod": PODS_META,
}

_MAX_MULTI_STREAM_PODS = 8


class KorvidApp(App[None]):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("colon", "open_command", "Command"),
        ("slash", "open_filter", "Filter"),
        ("0", "toggle_all_namespaces", "All NS"),
        ("d", "describe", "Describe"),
        ("s", "shell", "Shell"),
        ("l", "logs", "Logs"),
        ("shift+l", "logs_multi", "Multi-log"),
    ]

    DEFAULT_CSS = """
    ResourceTable {
        height: 1fr;
    }
    """

    def __init__(
        self,
        config: KorvidConfig,
        store: ResourceStore,
        watch_manager: WatchManager,
        list_namespaces: Callable[[], Awaitable[list[str]]] | None = None,
        aliases: dict[str, ResourceMeta] | None = None,
        get_manifest: (Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None) = None,
        get_events: (Callable[[str, str], Awaitable[list[dict[str, Any]]]] | None) = None,
        stream_logs: Callable[..., AsyncIterator[LogLine]] | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.store = store
        self.watch_manager = watch_manager
        self._list_namespaces = list_namespaces
        self._get_manifest = get_manifest
        self._get_events = get_events
        self._stream_logs = stream_logs
        self.aliases: dict[str, ResourceMeta] = (
            aliases if aliases is not None else dict(_DEFAULT_ALIASES)
        )
        self.current_kind: str = "pods"
        self.current_scope: str = config.namespace or "default"
        self.filter_pattern = ""
        self._log_tasks: set[asyncio.Task[None]] = set()
        self._log_buffer: LogBuffer | None = None
        self._log_error: bool = False

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
        yield LogPane()
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
            await self._close_log_pane()
            await self.watch_manager.stop(self.current_kind, self.current_scope)
            self.current_kind = new_kind
            self.current_scope = new_scope
            await self.watch_manager.start(self.current_kind, self.current_scope)
        self.post_message(ResourcesUpdated(self.current_kind))
        self._refresh_status()

    async def action_toggle_all_namespaces(self) -> None:
        """Toggle scope between ALL_NAMESPACES and the config-default namespace."""
        await self._close_log_pane()
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

    async def on_key(self, event: Key) -> None:
        """Close log pane on Escape when pane is open and no bar/picker is open."""
        if event.key == "escape":
            log_pane = self.query_one(LogPane)
            if not log_pane.display:
                return
            filter_bar = self.query_one(FilterBar)
            command_bar = self.query_one(CommandBar)
            namespace_picker = self.query_one(NamespacePicker)
            if not filter_bar.display and not command_bar.display and not namespace_picker.display:
                await self._close_log_pane()
                event.stop()

    async def action_logs(self) -> None:
        """Open or close the log pane for the selected pod (``l`` binding)."""
        if self.current_kind != "pods":
            self.notify("Logs are only available for pods", severity="warning")
            return

        log_pane = self.query_one(LogPane)
        if log_pane.display:
            await self._close_log_pane()
            return

        if self._stream_logs is None:
            self.notify("Log streaming unavailable", severity="warning")
            return

        ns, name = self._selected_ns_name()
        if ns is None or name is None:
            return

        containers = self._get_pod_containers(ns, name)
        sources: list[tuple[str, str]] = (
            [(name, ctr) for ctr in containers] if containers else [(name, "")]
        )
        await self._open_log_pane(ns, sources)

    async def action_logs_multi(self) -> None:
        """Stream all filtered pods' containers (``L`` binding); cap at 8."""
        if self.current_kind != "pods":
            self.notify("Logs are only available for pods", severity="warning")
            return

        if self._stream_logs is None:
            self.notify("Log streaming unavailable", severity="warning")
            return

        table = self.query_one(ResourceTable)
        if table.row_count == 0:
            self.notify("No resource selected", severity="warning")
            return

        triples = self._build_multi_stream_triples(table)
        if not triples:
            self.notify("No pods to stream", severity="warning")
            return

        if self.query_one(LogPane).display:
            await self._close_log_pane()

        ns0 = triples[0][0]
        await self._open_log_pane(
            ns0, [(pod, ctr) for _, pod, ctr in triples], triples=triples, force_prefix=True
        )

    def _build_multi_stream_triples(self, table: ResourceTable) -> list[tuple[str, str, str]]:
        """Collect (namespace, pod, container) triples for all visible pods; cap at 8."""
        ordered = table.ordered_rows
        pod_keys = [str(row.key.value) for row in ordered]
        total = len(pod_keys)
        if total > _MAX_MULTI_STREAM_PODS:
            pod_keys = pod_keys[:_MAX_MULTI_STREAM_PODS]
            self.notify(f"Streaming first {_MAX_MULTI_STREAM_PODS} of {total} matching pods")

        triples: list[tuple[str, str, str]] = []
        for pod_key in pod_keys:
            parts = pod_key.split("/", 1)
            if len(parts) != 2:
                continue
            ns, name = parts[0], parts[1]
            containers = self._get_pod_containers(ns, name)
            if containers:
                for ctr in containers:
                    triples.append((ns, name, ctr))
            else:
                triples.append((ns, name, ""))
        return triples

    def _selected_ns_name(self) -> tuple[str | None, str | None]:
        """Return (namespace, name) of the currently selected row or (None, None) + warn."""
        table = self.query_one(ResourceTable)
        if table.row_count == 0:
            self.notify("No resource selected", severity="warning")
            return None, None

        row_index = table.cursor_row
        ordered = table.ordered_rows
        if row_index >= len(ordered):
            self.notify("No resource selected", severity="warning")
            return None, None

        row_key = str(ordered[row_index].key.value)
        parts = row_key.split("/", 1)
        if len(parts) != 2:
            self.notify("Cannot determine resource from selection", severity="warning")
            return None, None
        return parts[0], parts[1]

    def _get_pod_containers(self, namespace: str, name: str) -> tuple[str, ...]:
        """Return container names for pod from the store, or empty tuple if not found."""
        for obj in self.store.get(self.current_kind, self.current_scope):
            if obj.namespace == namespace and obj.name == name and isinstance(obj, PodSummary):
                return obj.containers
        return ()

    async def _open_log_pane(
        self,
        namespace: str,
        sources: list[tuple[str, str]],
        triples: list[tuple[str, str, str]] | None = None,
        force_prefix: bool = False,
    ) -> None:
        """Show log pane and spawn one streaming task per (pod, container)."""
        log_pane = self.query_one(LogPane)
        log_pane.open(sources, force_prefix=force_prefix)
        log_pane.set_state("streaming")

        self._log_buffer = LogBuffer()
        self._log_tasks = set()
        self._log_error = False

        # triples carries per-entry namespaces; fall back to the single namespace
        if triples is None:
            triples = [(namespace, pod, ctr) for pod, ctr in sources]

        for ns, pod, container in triples:
            task: asyncio.Task[None] = asyncio.create_task(
                self._spawn_log_stream(ns, pod, container)
            )
            self._log_tasks.add(task)

    async def _spawn_log_stream(self, namespace: str, pod: str, container: str) -> None:
        """Consume one log stream, feeding lines to the pane and buffer."""
        if self._stream_logs is None:
            return
        log_pane = self.query_one(LogPane)
        current = asyncio.current_task()
        try:
            async for line in self._stream_logs(namespace, pod, container, follow=True):
                log_pane.feed(line)
                self._buffer_line(log_pane, line)
        except ApiStatusError as exc:
            msg = explain_api_error(exc.status, exc.reason, "pods", namespace)
            self.notify(msg, title="Log stream error", severity="error")
            self._log_error = True
            if log_pane.display:
                log_pane.set_state("error")
            if current is not None:
                self._log_tasks.discard(current)
        except asyncio.CancelledError:
            raise
        else:
            # Stream ended naturally; remove self and check if all tasks done.
            if current is not None:
                self._log_tasks.discard(current)
            if not self._log_tasks and log_pane.display and not self._log_error:
                log_pane.set_state("ended")

    def _buffer_line(self, log_pane: LogPane, line: LogLine) -> None:
        """Append *line* to the shared buffer; show overflow banner on first overflow."""
        if self._log_buffer is None:
            return
        was_full = self._log_buffer.overflowed
        self._log_buffer.append(line)
        if not was_full and self._log_buffer.overflowed:
            log_pane.show_overflow_banner()

    async def _close_log_pane(self) -> None:
        """Cancel all stream tasks and hide the log pane."""
        tasks = list(self._log_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._log_tasks.clear()
        self._log_buffer = None
        self._log_error = False
        with contextlib.suppress(Exception):
            self.query_one(LogPane).close()

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
        # Cancel any active log stream tasks before the event loop shuts down.
        tasks = list(self._log_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
        self._log_tasks.clear()
        await self.watch_manager.stop_all()
