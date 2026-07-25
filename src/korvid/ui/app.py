"""KorvidApp — constructed with injected dependencies (composition in __main__)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import shutil
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from time import monotonic
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.widgets import DataTable, Footer, Static

from korvid.agent.events import AgentError
from korvid.agent.runtime import AgentRuntime
from korvid.agent.setup import AgentConfigurator, AgentSettings
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
    AgentPromptSubmitted,
    ClearFilter,
    FilterCommand,
    NavigateCommand,
    QuitCommand,
    ResourcesUpdated,
    ShowError,
    ShowNamespacePicker,
    UnknownCommand,
)
from korvid.ui.shell import DEBUG_IMAGE, build_debug_argv, build_exec_argv, build_probe_argv
from korvid.ui.widgets.agent_panel import AgentPanel
from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen
from korvid.ui.widgets.command_bar import CommandBar
from korvid.ui.widgets.containers_screen import ContainersScreen, build_container_rows
from korvid.ui.widgets.describe_screen import DescribeScreen
from korvid.ui.widgets.filter_bar import FilterBar
from korvid.ui.widgets.log_pane import MAX_PANELS, LogPane
from korvid.ui.widgets.logo import SplashLogo
from korvid.ui.widgets.namespace_picker import NamespacePicker
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.status_bar import StatusBar

_DEFAULT_ALIASES: dict[str, ResourceMeta] = {
    "pods": PODS_META,
    "po": PODS_META,
    "pod": PODS_META,
}

logger = logging.getLogger(__name__)

_MAX_MULTI_STREAM_PODS = 8
# ``l`` accumulates side-by-side pod logs; beyond 4 pods each panel gets too
# small to read — comparing >4 replicas is what ``L`` (multi-stream) is for.
_MAX_LOG_PODS = 4
_MAX_RECONNECT_ATTEMPTS = 5


class _ReplayFilter:
    """Drops tail lines replayed by the API after a reconnect.

    Every (re)connection returns the last ~tail_lines existing lines before
    following.  The cursor is (last displayed timestamp, count of displayed
    lines carrying that exact timestamp) rather than a bare ``<=`` timestamp
    comparison, so *new* lines that happen to share the last displayed
    timestamp (kubelet is nanosecond-precise but parsing truncates to
    microseconds) are not lost across a reconnect.
    """

    def __init__(self) -> None:
        self._last_ts: datetime | None = None
        self._last_ts_count = 0
        self._resume_ts: datetime | None = None
        self._remaining = 0

    def start_connection(self) -> None:
        """Snapshot the cursor; replayed lines up to it will be dropped."""
        self._resume_ts = self._last_ts
        self._remaining = self._last_ts_count

    def is_replayed(self, line: LogLine) -> bool:
        ts = line.timestamp
        if ts is None or self._resume_ts is None:
            return False
        if ts < self._resume_ts:
            return True
        if ts == self._resume_ts and self._remaining > 0:
            self._remaining -= 1
            return True
        return False

    def record(self, line: LogLine) -> None:
        """Advance the cursor past a line that was just displayed."""
        ts = line.timestamp
        if ts is None:
            return
        if ts == self._last_ts:
            self._last_ts_count += 1
        else:
            self._last_ts = ts
            self._last_ts_count = 1


class KorvidApp(App[None]):
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("colon", "open_command", "Command"),
        ("slash", "open_filter", "Filter/Search"),
        ("0", "toggle_all_namespaces", "All NS"),
        ("d", "describe", "Describe"),
        ("s", "shell", "Shell"),
        ("l", "logs", "Logs"),
        Binding("shift+l", "logs_multi", "Multi-log"),
        # Real terminals deliver Shift+<letter> as the uppercase character,
        # not "shift+x"; bind both so the shortcut works outside Pilot tests.
        Binding("L", "logs_multi", "Multi-log", show=False),
        ("f", "log_format", "JSON/raw"),
        ("p", "log_previous", "Prev logs"),
        ("n", "log_search_next", "Next hit"),
        Binding("shift+n", "log_search_prev", "Prev hit"),
        Binding("N", "log_search_prev", "Prev hit", show=False),
        Binding("ctrl+a", "toggle_agent", "AI", priority=True),
    ]

    DEFAULT_CSS = """
    ResourceTable {
        height: 1fr;
    }
    """

    # CSS (not DEFAULT_CSS) so it outranks Footer's own `dock: bottom` default.
    CSS = """
    Footer {
        dock: top;
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
        agent_runtime: AgentRuntime | None = None,
        agent_model_name: str | None = None,
        agent_configurator: AgentConfigurator | None = None,
        rebuild_agent: Callable[[AgentSettings], AgentRuntime | None] | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.store = store
        self.watch_manager = watch_manager
        self._list_namespaces = list_namespaces
        self._get_manifest = get_manifest
        self._get_events = get_events
        self._stream_logs = stream_logs
        self._agent_runtime = agent_runtime
        self._agent_model_name = agent_model_name
        self._agent_configurator = agent_configurator
        self._rebuild_agent = rebuild_agent
        self._agent_settings: AgentSettings | None = None
        # A runtime built from config.yaml at startup must seed the settings
        # snapshot so :model works without running the :ai wizard first.
        if agent_runtime is not None and config.agent_provider and config.agent_model:
            self._agent_settings = AgentSettings(
                provider=config.agent_provider,
                auth_method=config.agent_auth_method or "none",
                base_url=config.agent_base_url,
                model=config.agent_model,
                api_key_env=config.agent_api_key_env,
            )
        self._agent_task: asyncio.Task[None] | None = None
        self.aliases: dict[str, ResourceMeta] = (
            aliases if aliases is not None else dict(_DEFAULT_ALIASES)
        )
        self.current_kind: str = "pods"
        self.current_scope: str = config.namespace or "default"
        self.filter_pattern = ""
        self._log_tasks: set[asyncio.Task[None]] = set()
        self._log_buffer: LogBuffer | None = None
        self._log_error: bool = False
        self._current_log_triples: list[tuple[str, str, str]] = []
        self._current_log_force_prefix: bool = False
        self._log_pane_mode: str = ""
        self._reconnect_sleep: float = 1.0
        self._ns_prefetch_task: asyncio.Task[None] | None = None
        self._splash_shown_at: float = monotonic()
        self._log_buffer_max_lines: int = config.log_buffer_lines
        # Kinds with a table render already queued — coalesces the per-object
        # notifications of a LIST seed into a single rebuild (see _on_store_update).
        self._render_pending: set[str] = set()

    @property
    def current_namespace(self) -> str:
        """Alias for current_scope; kept for backward-compatible test access."""
        return self.current_scope

    @current_namespace.setter
    def current_namespace(self, value: str) -> None:
        self.current_scope = value

    def compose(self) -> ComposeResult:
        # Footer is docked top (see CSS): the key legend replaces the stock
        # Header so shortcuts are visible where users look first.
        yield Footer()
        yield SplashLogo()
        table = ResourceTable()
        table.display = False  # hidden behind the splash until first data
        yield table
        empty_state = Static(id="empty-state")
        empty_state.display = False  # hidden until the first store notification
        yield empty_state
        yield LogPane()
        agent_panel = AgentPanel()
        agent_panel.display = False
        yield agent_panel
        yield CommandBar()
        yield FilterBar()
        yield NamespacePicker()
        yield StatusBar()

    async def on_mount(self) -> None:
        # Wire the `known` closure into CommandBar so parse_command can resolve aliases.
        command_bar = self.query_one(CommandBar)
        command_bar.known = lambda a: self.aliases[a].plural if a in self.aliases else None
        command_bar.command_words = sorted({*self.aliases, "ns", "namespaces", "q", "quit"})
        self._prefetch_namespaces()

        # Both callbacks fire from watch tasks on the same loop; post_message is
        # loop-safe. Watch tasks are cancelled in on_unmount before shutdown to
        # avoid posting to a closing app.
        def _on_store_update(kind: str) -> None:
            # The initial LIST seeds objects one apply_event at a time in a
            # single event-loop slice; posting one message per object would
            # rebuild the whole table N times. Post at most one render request
            # per kind until it is consumed — _render_table reads the current
            # store state, so a single deferred rebuild covers every event.
            if kind in self._render_pending:
                return
            self._render_pending.add(kind)
            self.post_message(ResourcesUpdated(kind))

        def _on_watch_error(detail: str) -> None:
            self.post_message(ShowError("Watch failed", detail))

        self.store.subscribe(_on_store_update)
        self.watch_manager.on_error = _on_watch_error
        self._splash_shown_at = monotonic()
        await self.watch_manager.start(self.current_kind, self.current_scope)
        self._refresh_status()
        # Safety net: never leave the splash up if the watch produces nothing
        # (e.g. connection failure) — swap to the table after a short grace.
        self.set_timer(5.0, self._dismiss_splash)

    #: Minimum time the startup splash stays visible in a real terminal.
    #: Skipped in headless (test) mode so Pilot tests see the table at once.
    SPLASH_MIN_SECONDS = 1.2

    def _dismiss_splash(self) -> None:
        splash = self.query_one(SplashLogo)
        if not splash.display:
            return
        if not self.is_headless:
            remaining = self._splash_shown_at + self.SPLASH_MIN_SECONDS - monotonic()
            if remaining > 0:
                self.set_timer(remaining, self._dismiss_splash)
                return
        splash.display = False
        self.query_one(ResourceTable).display = True

    def on_aliases_updated(self) -> None:
        """Refresh command autocompletion after background resource discovery."""
        try:
            command_bar = self.query_one(CommandBar)
        except Exception:
            return  # app is shutting down or not composed yet
        command_bar.command_words = sorted({*self.aliases, "ns", "namespaces", "q", "quit"})

    def on_resources_updated(self, message: ResourcesUpdated) -> None:
        self._render_pending.discard(message.kind)
        self._render_table(message.kind)

    def _prefetch_namespaces(self) -> None:
        """Warm the command-bar namespace completions in the background."""
        if self._list_namespaces is None:
            return

        async def _fetch() -> None:
            try:
                namespaces = await self._list_namespaces()  # type: ignore[misc]
            except Exception:
                logger.debug("namespace prefetch for completion failed", exc_info=True)
                return
            self.query_one(CommandBar).namespace_words = namespaces

        self._ns_prefetch_task = asyncio.create_task(_fetch())

    def _render_table(self, kind: str) -> None:
        """Single choke point: table rows and empty-state always update together."""
        # First store notification: replace the startup splash with real content.
        self._dismiss_splash()
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
        # When the log pane is open, / opens the pane's inline search instead.
        log_pane = self.query_one(LogPane)
        if log_pane.display:
            log_pane.open_search()
            return
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
        self.query_one(CommandBar).namespace_words = namespaces
        self.query_one(NamespacePicker).open(namespaces)

    def on_quit_command(self, message: QuitCommand) -> None:
        self.exit()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a pod row drills into its container list (k9s convention)."""
        if not isinstance(event.data_table, ResourceTable) or self.current_kind != "pods":
            return
        event.stop()
        row_key = str(event.row_key.value)
        parts = row_key.split("/", 1)
        if len(parts) != 2:
            return
        namespace, name = parts[0], parts[1]

        rows = await self._build_container_rows(namespace, name)
        if not rows:
            self.notify("No containers found for this pod", severity="warning")
            return

        def _on_pick(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            action, container = result
            if action == "shell":
                self._run_shell(namespace, name, container)
            else:
                if self._stream_logs is None:
                    self.notify("Log streaming unavailable", severity="warning")
                    return
                self.run_worker(self._open_log_pane(namespace, [(name, container)]))

        await self.push_screen(ContainersScreen(name, rows), _on_pick)

    async def _build_container_rows(
        self, namespace: str, name: str
    ) -> list[tuple[str, str, str, str, str]]:
        """Container rows from the live manifest; store names as fallback."""
        if self._get_manifest is not None:
            try:
                manifest = await self._get_manifest("pods", namespace, name)
            except (ApiStatusError, ValueError) as exc:
                logger.debug("manifest fetch for container list failed: %s", exc)
            else:
                rows = build_container_rows(manifest)
                if rows:
                    return rows
        return [(ctr, "-", "-", "-", "-") for ctr in self._get_pod_containers(namespace, name)]

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
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return

        events: list[dict[str, Any]] = []
        # Events are filtered by involvedObject.name only, so restrict to pods
        # to avoid showing events for unrelated objects with the same name.
        if self._get_events is not None and ns is not None and self.current_kind == "pods":
            try:
                events = await self._get_events(namespace, name)
            except ApiStatusError as exc:
                # Events are best-effort; surface but still show the manifest.
                msg = explain_api_error(exc.status, exc.reason, "events", namespace)
                self.notify(msg, severity="warning")

        title = f"{self.current_kind}/{namespace}/{name}"
        await self.push_screen(DescribeScreen(title, manifest, events))

    def on_unknown_command(self, message: UnknownCommand) -> None:
        parts = message.text.strip().split()
        head = parts[0] if parts else ""
        if head in {"ai", "agent"}:
            self._open_agent_setup()
            return
        if head == "model":
            self._handle_model_command(parts[1:])
            return
        self.notify(
            f"Unknown resource or command: {message.text}"
            " — not found in this cluster's API (CRD not installed?)",
            severity="warning",
        )

    def _open_agent_setup(self) -> None:
        if self._agent_configurator is None:
            self.notify("Agent setup unavailable in this build", severity="warning")
            return
        # The wizard applies the settings itself (via apply_settings) before
        # persisting, so a refused swap keeps the wizard open and unsaved.
        self.push_screen(
            AgentSetupScreen(self._agent_configurator, apply_settings=self._apply_agent_settings)
        )

    def _handle_model_command(self, args: list[str]) -> None:
        """`:model` shows the current model; `:model <name>` switches and persists it."""
        if not args:
            # Report only a live model: at startup config may carry a model
            # name even though provider creation failed (runtime is None).
            if self._agent_runtime is not None and self._agent_model_name:
                self.notify(f"Agent model: {self._agent_model_name}")
            else:
                self.notify("Agent not configured — run :ai first", severity="warning")
            return
        settings = self._agent_settings
        configurator = self._agent_configurator
        if settings is None or configurator is None:
            self.notify("Agent not configured — run :ai first", severity="warning")
            return
        new_settings = dataclasses.replace(settings, model=args[0])

        async def _switch() -> None:
            # Apply first: persistence must be conditional on a successful
            # swap, or a refused change would silently take effect on restart.
            if not self._apply_agent_settings(new_settings):
                return  # _apply_agent_settings already notified the reason
            try:
                await configurator.save(new_settings)
            except Exception as exc:  # runtime is live but disk is stale
                # Do not name a revert target: after a previous failed save
                # the in-memory snapshot may itself never have been persisted.
                self.notify(
                    f"Model applied, but save failed: {exc} — will revert to "
                    "the last saved model on restart",
                    severity="warning",
                )
                return
            self.notify(f"Agent model set to {new_settings.model}")

        self.run_worker(_switch(), exclusive=False)

    def _apply_agent_settings(self, settings: AgentSettings) -> bool:
        """Swap in a fresh runtime built from the wizard's settings.

        Transactional: on any failure the previous runtime/settings are kept
        and False is returned; the swap is also refused while a turn is live.
        """
        if self._rebuild_agent is None:
            self.notify("Agent rebuild unavailable in this build", severity="warning")
            return False
        if self._agent_task is not None and not self._agent_task.done():
            self.notify("Agent is busy — wait for the current turn to finish", severity="warning")
            return False
        try:
            runtime = self._rebuild_agent(settings)
        except Exception as exc:
            self.notify(f"Agent rebuild failed: {exc}", severity="error")
            return False
        if runtime is None:
            self.notify(
                "Agent rebuild failed — check configuration; keeping previous agent",
                severity="error",
            )
            return False
        self._agent_runtime = runtime
        self._agent_model_name = settings.model
        self._agent_settings = settings
        self._refresh_status()
        panel = self.query_one(AgentPanel)
        agent_input = panel.query_one("#agent-input")
        # Always re-enable: the hint may have disabled it while the panel was
        # open earlier; only focus/header rendering depends on visibility.
        agent_input.disabled = False
        if panel.display:
            in_tok, out_tok = runtime.total_tokens
            panel.set_header(settings.model, in_tok, out_tok, estimated=runtime.usage_estimated)
            agent_input.focus()
        return True

    def action_shell(self) -> None:
        """Drop into a shell inside the selected pod via kubectl exec.

        Multi-container pods show a container picker first; if exec fails
        (typically a distroless image without sh/bash) a `kubectl debug`
        ephemeral-container fallback is offered.
        """
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

        containers = self._get_pod_containers(namespace, name)
        if len(containers) > 1:

            def _on_pick(container: str | None) -> None:
                if container is not None:
                    self._run_shell(namespace, name, container)

            self.push_screen(
                PickScreen(f"Container in {name}:", list(containers)),
                _on_pick,
            )
            return

        self._run_shell(namespace, name, containers[0] if containers else None)

    _DEBUG_YES = f"Yes — attach a {DEBUG_IMAGE} debug container (kubectl debug)"

    @staticmethod
    def _run_interactive(argv: list[str], banner: str) -> int:
        """Run an interactive subprocess on a cleared screen for a direct feel.

        Suspending Textual drops back to the primary screen, exposing old
        scrollback (including the command that launched korvid). Clearing
        first makes it look like we connected straight into the pod.
        """
        print(f"\x1b[2J\x1b[H\x1b[2m{banner}\x1b[0m", flush=True)
        return subprocess.call(argv)

    def _run_shell(self, namespace: str, name: str, container: str | None) -> None:
        """Run kubectl exec; offer the kubectl debug fallback only if sh is missing."""
        argv = build_exec_argv(namespace, name, container, context=self.config.kube_context)
        target = f"{name}/{container}" if container else name
        with self.suspend():
            exit_code = self._run_interactive(argv, f"korvid shell → {target} (exit to return)")
        self.refresh()
        if exit_code == 0:
            return

        # kubectl exec propagates the remote command's exit code, so a non-zero
        # status can just mean the user's last command failed or they hit Ctrl+C.
        # Probe non-interactively: if sh runs fine, the shell session was real.
        # Run in a thread worker so a slow API server can't freeze the UI.
        def _probe_and_maybe_offer() -> None:
            try:
                probe = subprocess.run(
                    build_probe_argv(namespace, name, container, context=self.config.kube_context),
                    capture_output=True,
                    timeout=5,
                )
                shell_exists = probe.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                shell_exists = False  # inconclusive — keep offering the fallback
            if shell_exists:
                return
            self.call_from_thread(self._offer_debug_fallback, namespace, name, container, exit_code)

        self.run_worker(_probe_and_maybe_offer, thread=True)

    def _offer_debug_fallback(
        self, namespace: str, name: str, container: str | None, exit_code: int
    ) -> None:
        """Ask whether to attach a kubectl debug container after a failed shell."""
        target = f"{name}/{container}" if container else name

        def _on_choice(choice: str | None) -> None:
            if choice == self._DEBUG_YES:
                self._run_debug(namespace, name, container)

        self.push_screen(
            PickScreen(
                f"Shell failed in {target} (exit {exit_code}) — the image likely has"
                " no sh/bash (distroless). Attach a debug container instead?\n"
                "Note: the ephemeral container stays in the pod spec until restart.",
                [self._DEBUG_YES, "No"],
            ),
            _on_choice,
        )

    def _run_debug(self, namespace: str, name: str, container: str | None) -> None:
        """Attach an ephemeral busybox container via kubectl debug."""
        argv = build_debug_argv(namespace, name, container, context=self.config.kube_context)
        target = f"{name}/{container}" if container else name
        with self.suspend():
            exit_code = self._run_interactive(argv, f"korvid debug → {target} (exit to return)")
        self.refresh()
        if exit_code != 0:
            self.notify(
                f"kubectl debug exited with status {exit_code}"
                " — check RBAC (pods/ephemeralcontainers) and cluster version",
                severity="warning",
            )

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
        """Open logs for the selected pod, or toggle it in/out of the pane (``l``).

        With the pane already open in live mode, ``l`` on another pod adds its
        containers side-by-side (max ``_MAX_LOG_PODS`` pods); ``l`` on a pod
        already shown removes it (closing the pane when it was the last one).
        Adding/removing reopens the streams, so panels restart at the last
        ~200 tailed lines.
        """
        if self.current_kind != "pods":
            self.notify("Logs are only available for pods", severity="warning")
            return

        log_pane = self.query_one(LogPane)
        if log_pane.display and self._log_pane_mode != "l":
            # L (multi-stream) and p (previous) modes don't accumulate.
            await self._close_log_pane()
            return

        if self._stream_logs is None:
            self.notify("Log streaming unavailable", severity="warning")
            return

        ns, name = self._selected_ns_name()
        if ns is None or name is None:
            return

        if log_pane.display:
            await self._toggle_log_pod(ns, name)
            return

        self._log_pane_mode = "l"
        triples = self._pod_triples(ns, name)
        await self._open_log_pane(ns, [(pod, ctr) for _, pod, ctr in triples], triples=triples)

    def _pod_triples(self, namespace: str, name: str) -> list[tuple[str, str, str]]:
        """Return (ns, pod, container) triples for one pod (one per container)."""
        containers = self._get_pod_containers(namespace, name)
        if containers:
            return [(namespace, name, ctr) for ctr in containers]
        return [(namespace, name, "")]

    async def _toggle_log_pod(self, namespace: str, name: str) -> None:
        """Add or remove *namespace/name* from the accumulated live-log panels."""
        existing = list(self._current_log_triples)
        pods: list[tuple[str, str]] = []
        for t_ns, t_pod, _ in existing:
            if (t_ns, t_pod) not in pods:
                pods.append((t_ns, t_pod))

        if (namespace, name) in pods:
            triples = [t for t in existing if (t[0], t[1]) != (namespace, name)]
            if not triples:
                await self._close_log_pane()
                return
        else:
            if len(pods) >= _MAX_LOG_PODS:
                self.notify(
                    f"Log pane caps at {_MAX_LOG_PODS} pods — Esc closes all",
                    severity="warning",
                )
                return
            triples = existing + self._pod_triples(namespace, name)
            if len(triples) > MAX_PANELS:
                self.notify(
                    f"Panel cap is {MAX_PANELS} containers — cannot add {name}",
                    severity="warning",
                )
                return

        await self._cancel_log_tasks()
        sources = [(pod, ctr) for _, pod, ctr in triples]
        await self._open_log_pane(triples[0][0], sources, triples=triples)

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

        self._log_pane_mode = "L"
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
        previous: bool = False,
    ) -> None:
        """Show log pane and spawn one streaming task per (pod, container)."""
        # Resolve triples before saving so _current_log_triples is always complete.
        if triples is None:
            triples = [(namespace, pod, ctr) for pod, ctr in sources]

        # LogPane silently ignores sources beyond MAX_PANELS; enforce the same
        # cap here so no stream task is ever spawned without a panel to feed.
        if len(triples) > MAX_PANELS:
            self.notify(
                f"Showing first {MAX_PANELS} of {len(triples)} containers",
                severity="warning",
            )
            triples = triples[:MAX_PANELS]
            sources = sources[:MAX_PANELS]

        self._current_log_triples = list(triples)
        self._current_log_force_prefix = force_prefix

        log_pane = self.query_one(LogPane)
        self._log_buffer = LogBuffer(self._log_buffer_max_lines)
        log_pane.open(sources, force_prefix=force_prefix, log_buffer=self._log_buffer)

        if previous:
            log_pane.write_banner("\u2500\u2500 previous container logs \u2500\u2500")

        log_pane.set_state("streaming")

        # Defensive: callers cancel+gather before re-opening, but never let a
        # stale task survive the set replacement below.
        for stale in self._log_tasks:
            stale.cancel()
        self._log_tasks = set()
        self._log_error = False

        for ns, pod, container in triples:
            task: asyncio.Task[None] = asyncio.create_task(
                self._spawn_log_stream(ns, pod, container, previous=previous)
            )
            self._log_tasks.add(task)

    async def _spawn_log_stream(
        self, namespace: str, pod: str, container: str, *, previous: bool = False
    ) -> None:
        """Delegate to the appropriate streaming coroutine based on follow flag."""
        stream_logs = self._stream_logs
        if stream_logs is None:
            return
        if previous:
            await self._previous_log_stream(namespace, pod, container, stream_logs)
        else:
            await self._live_log_stream(namespace, pod, container, stream_logs)

    async def _live_log_stream(
        self,
        namespace: str,
        pod: str,
        container: str,
        stream_logs: Callable[..., AsyncIterator[LogLine]],
    ) -> None:
        """Retry loop for live (follow=True) streams.

        Retries up to ``_MAX_RECONNECT_ATTEMPTS`` times on transient errors or
        unexpected EOF.  ApiStatusError and CancelledError are never retried.
        Each (re)connection replays the last ~tail_lines existing lines;
        ``_ReplayFilter`` drops the ones already displayed so reconnects
        don't duplicate output.
        """
        log_pane = self.query_one(LogPane)
        current = asyncio.current_task()
        consecutive_failures = 0
        replay = _ReplayFilter()

        while True:
            replay.start_connection()
            try:
                async for line in stream_logs(
                    namespace, pod, container, previous=False, follow=True
                ):
                    if replay.is_replayed(line):
                        continue  # replayed tail line already shown pre-reconnect
                    replay.record(line)
                    self._mark_stream_healthy(log_pane, consecutive_failures)
                    consecutive_failures = 0
                    log_pane.feed(line)
                    self._buffer_line(log_pane, line)
            except ApiStatusError as exc:
                self._handle_stream_api_error(log_pane, current, namespace, exc)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                # Transient (network hiccup, rotation EOF); logged so
                # programming bugs aren't silently disguised as reconnects.
                logger.debug(
                    "log stream for %s/%s failed; will reconnect", pod, container, exc_info=True
                )

            if not log_pane.display:
                # Pane was closed while the stream was suspended; exit quietly.
                self._discard_task(current)
                return
            consecutive_failures += 1
            if not await self._pause_before_reconnect(log_pane, current, consecutive_failures):
                return

    def _mark_stream_healthy(self, log_pane: LogPane, consecutive_failures: int) -> None:
        """Restore the streaming indicator after a successful reconnect."""
        if consecutive_failures > 0 and not self._log_error:
            log_pane.set_state("streaming")

    async def _pause_before_reconnect(
        self,
        log_pane: LogPane,
        current: asyncio.Task[None] | None,
        consecutive_failures: int,
    ) -> bool:
        """Sleep before the next attempt; False when retries are exhausted."""
        if consecutive_failures > _MAX_RECONNECT_ATTEMPTS or self._log_error:
            if not self._log_error:
                self.notify(
                    f"log stream lost after {_MAX_RECONNECT_ATTEMPTS} reconnect attempts",
                    title="Log stream error",
                    severity="error",
                )
                self._log_error = True
                log_pane.set_state("error")
            self._discard_task(current)
            return False
        log_pane.set_state("reconnecting")
        await asyncio.sleep(self._reconnect_sleep)
        return True

    async def _previous_log_stream(
        self,
        namespace: str,
        pod: str,
        container: str,
        stream_logs: Callable[..., AsyncIterator[LogLine]],
    ) -> None:
        """One-shot previous-container-log stream (follow=False, no reconnect)."""
        log_pane = self.query_one(LogPane)
        current = asyncio.current_task()
        try:
            async for line in stream_logs(namespace, pod, container, previous=True, follow=False):
                log_pane.feed(line)
                self._buffer_line(log_pane, line)
        except ApiStatusError as exc:
            self._handle_stream_api_error(log_pane, current, namespace, exc)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unlike live streams there is no reconnect: surface the failure.
            self._discard_task(current)
            if log_pane.display and not self._log_error:
                self._log_error = True
                self.notify(
                    "previous logs stream failed",
                    title="Log stream error",
                    severity="error",
                )
                log_pane.set_state("error")
            return
        self._discard_task(current)
        if self._all_streams_ended():
            log_pane.set_state("ended")

    def _handle_stream_api_error(
        self,
        log_pane: LogPane,
        current: asyncio.Task[None] | None,
        namespace: str,
        exc: ApiStatusError,
    ) -> None:
        """Notify the user of an API error and put the stream into error state."""
        if not log_pane.display:
            self._discard_task(current)
            return
        msg = explain_api_error(exc.status, exc.reason, "pods", namespace)
        self.notify(msg, title="Log stream error", severity="error")
        self._log_error = True
        log_pane.set_state("error")
        self._discard_task(current)

    def _all_streams_ended(self) -> bool:
        """True when every spawned stream task has finished without an error."""
        return not self._log_tasks and not self._log_error

    def _discard_task(self, current: asyncio.Task[None] | None) -> None:
        """Remove *current* from the live task set (no-op if None or absent)."""
        if current is not None:
            self._log_tasks.discard(current)

    def _buffer_line(self, log_pane: LogPane, line: LogLine) -> None:
        """Append *line* to the shared buffer; show overflow banner on first overflow."""
        if self._log_buffer is None:
            return
        was_full = self._log_buffer.overflowed
        self._log_buffer.append(line)
        if not was_full and self._log_buffer.overflowed:
            log_pane.show_overflow_banner()

    async def _cancel_log_tasks(self) -> None:
        """Cancel and await stream tasks without hiding the pane (reopen path)."""
        tasks = list(self._log_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._log_tasks.clear()
        self._log_buffer = None
        self._log_error = False

    async def _close_log_pane(self) -> None:
        """Cancel all stream tasks and hide the log pane."""
        await self._cancel_log_tasks()
        self._current_log_triples = []
        self._current_log_force_prefix = False
        self._log_pane_mode = ""
        with contextlib.suppress(Exception):
            self.query_one(LogPane).close()

    def _refresh_status(self) -> None:
        # Availability comes from the actual runtime, not the config flag —
        # create_provider may return None (unknown provider, missing base_url/
        # model) while agent_enabled is still true in config.
        label = "AI on" if self._agent_runtime is not None else "AI off"
        self.query_one(StatusBar).update_status(self.config.kube_context, self.current_scope, label)

    # ------------------------------------------------------------------
    # Task-10 actions: JSON toggle, previous logs, search navigation
    # ------------------------------------------------------------------

    async def action_log_format(self) -> None:
        """Toggle JSON/raw formatting and re-render the buffer (``f`` key)."""
        log_pane = self.query_one(LogPane)
        if not log_pane.display:
            return
        log_pane.toggle_format()
        if self._log_buffer is not None:
            log_pane.replay(self._log_buffer.lines())

    async def action_log_previous(self) -> None:
        """Re-open the same streams in previous-container-log mode (``p`` key)."""
        log_pane = self.query_one(LogPane)
        if not log_pane.display:
            return
        if not self._current_log_triples:
            return
        triples = list(self._current_log_triples)
        force_prefix = self._current_log_force_prefix
        sources = [(pod, ctr) for _, pod, ctr in triples]
        # Cancel live tasks without hiding the pane.
        await self._cancel_log_tasks()
        self._log_pane_mode = "p"
        # Re-open with previous=True (clears RichLog, writes banner, spawns tasks).
        ns0 = triples[0][0]
        await self._open_log_pane(
            ns0, sources, triples=triples, force_prefix=force_prefix, previous=True
        )

    def action_log_search_next(self) -> None:
        """Advance to the next search hit (``n`` key)."""
        log_pane = self.query_one(LogPane)
        if log_pane.display:
            log_pane.search_next()

    def action_log_search_prev(self) -> None:
        """Go back to the previous search hit (``N`` / shift+n key)."""
        log_pane = self.query_one(LogPane)
        if log_pane.display:
            log_pane.search_prev()

    # ------------------------------------------------------------------
    # Agent panel (Ctrl-A) — wiring only; rendering lives in AgentPanel,
    # loop logic in AgentRuntime.
    # ------------------------------------------------------------------

    def action_toggle_agent(self) -> None:
        """Toggle the agent chat panel; show setup hint when unconfigured."""
        panel = self.query_one(AgentPanel)
        if panel.display:
            panel.display = False
            self.query_one(ResourceTable).focus()
            return
        panel.display = True
        if self._agent_runtime is None:
            panel.show_setup_hint()
            return
        if self._agent_model_name:
            runtime = self._agent_runtime
            in_tok, out_tok = runtime.total_tokens
            panel.set_header(
                self._agent_model_name, in_tok, out_tok, estimated=runtime.usage_estimated
            )
        panel.query_one("#agent-input").focus()

    def on_agent_prompt_submitted(self, message: AgentPromptSubmitted) -> None:
        if self._agent_runtime is None:
            return
        if self._agent_task is not None and not self._agent_task.done():
            return
        panel = self.query_one(AgentPanel)
        panel.begin_turn(message.text)
        self._agent_task = asyncio.create_task(self._run_agent_turn(message.text))

    def _selected_row_name(self) -> str | None:
        table = self.query_one(ResourceTable)
        if table.row_count == 0:
            return None
        ordered = table.ordered_rows
        if table.cursor_row >= len(ordered):
            return None
        return str(ordered[table.cursor_row].key.value)

    async def _run_agent_turn(self, user_text: str) -> None:
        runtime = self._agent_runtime
        if runtime is None:
            return
        panel = self.query_one(AgentPanel)
        screen_context = (
            f"view={self.current_kind} scope={self.current_scope} "
            f"selected={self._selected_row_name() or '-'} "
            f"filter={self.filter_pattern or '-'}"
        )
        try:
            async for event in runtime.run_turn(user_text, screen_context):
                panel.apply_event(event)
        except Exception as exc:
            panel.apply_event(AgentError(message=str(exc)))

    # ------------------------------------------------------------------
    # UIBridge implementation (spec §4.1 UI Bus): the agent drives the
    # exact same handlers as user keystrokes. Every method returns a
    # confirmation or an "ERROR: …" string and never raises (executor
    # contract), and every screen change is announced via notify so the
    # user always sees what the agent did.
    # ------------------------------------------------------------------

    def _mark_agent_action(self, summary: str) -> None:
        self.notify(summary, title="agent", severity="information", timeout=3)

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        key = view.strip().lower()
        meta = self.aliases.get(key)
        if meta is None:
            return f"ERROR: unknown view {view!r} — not a resource kind in this cluster"
        try:
            await self.on_navigate_command(NavigateCommand(meta.plural, namespace))
        except Exception as exc:
            return f"ERROR: {exc}"
        rows = self.store.get(self.current_kind, self.current_scope)
        self._mark_agent_action(f"view → {self.current_kind} ({self.current_scope})")
        suffix = " (list may still be loading)" if not rows else ""
        return (
            f"switched to {self.current_kind} in {self.current_scope} — "
            f"{len(rows)} resources{suffix}"
        )

    async def agent_set_filter(self, pattern: str) -> str:
        try:
            if pattern:
                self.on_filter_command(FilterCommand(pattern))
            else:
                self.on_clear_filter(ClearFilter())
        except Exception as exc:
            return f"ERROR: {exc}"
        if pattern:
            self._mark_agent_action(f"filter → {pattern!r}")
            return f"filter set to {pattern!r} on the {self.current_kind} view"
        self._mark_agent_action("filter cleared")
        return "filter cleared"

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        if self._stream_logs is None:
            return "ERROR: log streaming unavailable in this session"
        try:
            if container:
                triples = [(namespace, pod, container)]
            else:
                triples = self._pod_triples(namespace, pod)
            await self._cancel_log_tasks()
            self._log_pane_mode = "l"
            await self._open_log_pane(namespace, [(p, c) for _, p, c in triples], triples=triples)
        except Exception as exc:
            return f"ERROR: {exc}"
        target = f"{namespace}/{pod}" + (f" [{container}]" if container else "")
        self._mark_agent_action(f"logs → {target}")
        return f"log pane opened for {target} — the user can now see the live logs"

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        if self._get_manifest is None:
            return "ERROR: describe unavailable in this session"
        key = kind.strip().lower()
        meta = self.aliases.get(key)
        if meta is None:
            return f"ERROR: unknown kind {kind!r} — not a resource kind in this cluster"
        if meta.namespaced and not namespace:
            return f"ERROR: kind {kind!r} is namespaced — provide the 'namespace' argument"
        try:
            manifest = await self._get_manifest(meta.plural, namespace, name)
        except ApiStatusError as exc:
            return f"ERROR: {explain_api_error(exc.status, exc.reason, meta.plural, namespace)}"
        except Exception as exc:
            return f"ERROR: {exc}"
        events: list[dict[str, Any]] = []
        # Events are name-scoped only, so restrict to pods (same rule as `d`).
        if self._get_events is not None and namespace and meta.plural == "pods":
            try:
                events = await self._get_events(namespace, name)
            except Exception:  # events are best-effort; the manifest still shows
                logger.debug("agent describe: event fetch failed", exc_info=True)
        title = f"{meta.plural}/{namespace or '-'}/{name}"
        try:
            await self.push_screen(DescribeScreen(title, manifest, events))
        except Exception as exc:
            return f"ERROR: {exc}"
        self._mark_agent_action(f"describe → {title}")
        return f"describe screen opened for {title} — manifest and events are on screen"

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
        if self._ns_prefetch_task is not None:
            self._ns_prefetch_task.cancel()
        if self._agent_task is not None:
            self._agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._agent_task
        tasks = list(self._log_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
        self._log_tasks.clear()
        await self.watch_manager.stop_all()
