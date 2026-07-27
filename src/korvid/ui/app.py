"""KorvidApp — constructed with injected dependencies (composition in __main__)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar, Literal

import yaml
from rich.text import Text
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.events import Key
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Static
from textual.widgets.data_table import CellDoesNotExist
from textual.worker import Worker

from korvid.agent.events import AgentError
from korvid.agent.mcp_server import MCPController
from korvid.agent.runtime import AgentRuntime
from korvid.agent.setup import AgentConfigurator, AgentSettings
from korvid.agent.tools import UIBridge
from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.debugimage import (
    FALLBACK_IMAGE,
    ephemeral_container_names,
    find_pull_failure,
    recommend_debug_images,
    same_image_ref,
)
from korvid.core.errors import explain_api_error
from korvid.core.filters import ResourceFilter, parse_filter
from korvid.core.keybindings import plan_keybindings, shift_alias_keys
from korvid.core.logbuffer import LogBuffer
from korvid.core.logexport import default_log_export_dir, export_log_lines
from korvid.core.secrets import mask_secret_manifest
from korvid.core.sorting import SortSpec, toggle_sort
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.drain import DrainPlan, DrainTarget
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.k8s.metrics import MetricsPoller
from korvid.k8s.models import PodSummary
from korvid.k8s.olm import (
    OPERATORS_GROUP,
    PACKAGES_GROUP,
    PackageInstallFacts,
    build_subscription,
    package_install_facts,
    resolve_olm_meta,
)
from korvid.k8s.relations import drill_child, owned_by
from korvid.k8s.writes import WriteOps, restart_stamp
from korvid.ui.command import command_help
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
from korvid.ui.navigation import DrillLevel, NavigationStack
from korvid.ui.shell import (
    DEBUG_IMAGE,
    build_debug_argv,
    build_exec_argv,
    build_pod_get_argv,
    build_probe_argv,
)
from korvid.ui.widgets.agent_panel import AgentPanel
from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen
from korvid.ui.widgets.command_bar import CommandBar
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ImagePrompt, ReplicasPrompt
from korvid.ui.widgets.containers_screen import ContainersScreen, build_container_rows
from korvid.ui.widgets.describe_screen import DescribePane, DescribeScreen
from korvid.ui.widgets.filter_bar import FilterBar
from korvid.ui.widgets.help_screen import HelpScreen, collect_help
from korvid.ui.widgets.hint_detail import HintDetailScreen
from korvid.ui.widgets.hint_strip import HintStrip, parse_rfc3339
from korvid.ui.widgets.log_pane import MAX_PANELS, LogPane
from korvid.ui.widgets.logo import SplashLogo
from korvid.ui.widgets.namespace_picker import NamespacePicker
from korvid.ui.widgets.operator_install import OperatorInstallPrompt
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.widgets.resize_prompt import ResizePrompt
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.secret_screen import SecretScreen
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
#: Seconds an agent-requested approval dialog stays open before it counts as
#: a denial - an unanswered dialog must never hang the agent turn forever.
_APPROVAL_TIMEOUT = 120.0
#: Upper bound on the SubjectAccessReview pre-check: a stalled authorization
#: endpoint must never hang a binding handler or an agent turn. On timeout
#: the check fails open (writes stay approval-gated and audited).
_PERMISSION_CHECK_TIMEOUT = 10.0


def _event_timestamp(event: dict[str, Any]) -> datetime | None:
    """Absolute time of the event's latest occurrence.

    Repeating events record it in `series.lastObservedTime`; `lastTimestamp`
    (core v1) and `eventTime` (events.k8s.io initial observation) are
    fallbacks for non-series events, then the deprecated `firstTimestamp`
    and finally `metadata.creationTimestamp` — a valid event may carry
    only those, and treating it as undated would misorder or suppress it.
    """
    series = event.get("series") or {}
    raw = (
        series.get("lastObservedTime")
        or event.get("lastTimestamp")
        or event.get("eventTime")
        or event.get("firstTimestamp")
        or (event.get("metadata") or {}).get("creationTimestamp")
        or ""
    )
    return parse_rfc3339(str(raw))


def _newest_warning(events: list[dict[str, Any]]) -> tuple[str, datetime | None] | None:
    """(line, timestamp) of the most recent Warning event, or None.

    Timestamps are parsed before comparing: RFC 3339 strings do not sort
    chronologically once fractional seconds or offsets differ.
    """
    warnings = [e for e in events if e.get("type") == "Warning"]
    if not warnings:
        return None
    epoch = datetime.min.replace(tzinfo=UTC)
    newest = max(warnings, key=lambda e: _event_timestamp(e) or epoch)
    reason = str(newest.get("reason") or "Warning")
    message = str(newest.get("message") or "").strip()
    line = f"{reason}: {message}" if message else reason
    return line, _event_timestamp(newest)


class EventsFetcher(ABC):
    """Events for one object — layer-boundary interface (AGENTS.md: `abc.ABC`).

    The concrete adapter wraps the k8s client and is wired in `__main__.py`.
    `uid` narrows the query so earlier same-named incarnations are excluded.
    """

    @abstractmethod
    async def fetch(
        self, namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]: ...


#: Display phases that are routine on their own — no hint without other signals.
_ROUTINE_PHASES = frozenset(
    {
        "Running",
        "Succeeded",
        "Completed",
        "Pending",
        "ContainerCreating",
        "PodInitializing",
        "Terminating",
    }
)


def _pod_needs_hint(summary: PodSummary) -> bool:
    """Abnormal rows: captured trouble, an abnormal display phase (Unknown,
    status-only Failed), or a Running pod that is not fully ready.

    The latter two carry no container trouble — the explanation lives only
    in Warning events (e.g. `Unhealthy` for a failing readiness probe), so
    they still qualify for an event-only hint.
    """
    if summary.trouble:
        return True
    if summary.phase.startswith("Init:"):
        # Routine init progress renders as Init:i/n; actual init failures
        # already surface as trouble entries above.
        return False
    if summary.phase not in _ROUTINE_PHASES:
        return True
    if summary.phase != "Running":
        # Routine startup/finish/deletion phases are legitimately not-ready
        # (Pending 0/1, Completed 0/N, Terminating): no hint, no event fetch.
        return False
    ready, _, desired = summary.ready.partition("/")
    return bool(desired) and ready != desired


def _event_line_fresh(event_ts: datetime | None, summary: PodSummary) -> bool:
    """Whether a Warning may explain the *current* status.

    An event older than the last termination or the last Ready-condition
    flip explains a previous failure; an undated event cannot be proven
    fresher than a dated status (timestamp fields are optional). Both are
    suppressed. Since nearly every pod carries a Ready condition, an
    undated Warning is in practice always suppressed — a deliberate trade:
    real Warnings virtually always carry a timestamp, and a wrong "cause"
    is worse than none.
    """
    cutoffs = [
        ts
        for t in summary.trouble
        if t.finished_at and (ts := parse_rfc3339(t.finished_at)) is not None
    ]
    if summary.ready_transition_at:
        ready_ts = parse_rfc3339(summary.ready_transition_at)
        if ready_ts is not None:
            cutoffs.append(ready_ts)
    if not cutoffs:
        return True
    return event_ts is not None and event_ts >= max(cutoffs)


def _yaml_equal(a: object, b: object) -> bool:
    """Type-sensitive structural equality for parsed YAML documents.
    Python's ``==`` conflates YAML booleans and integers (``True == 1``),
    and comparing ``yaml.safe_dump`` output is not canonical either: shared
    nodes are emitted as anchors/aliases, so an aliased-but-equal document
    would falsely report a change. Compare recursively instead, requiring
    identical scalar types (including mapping keys)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        if len(a) != len(b):
            return False
        # Fast path for the overwhelmingly common case of string-keyed
        # mappings: direct lookup is O(n), and str-to-str comparison has
        # no cross-type conflation. Kubernetes objects can be large (a
        # ConfigMap may carry thousands of data keys), so the structural
        # scan below must not run on every comparison.
        if all(isinstance(k, str) for k in a) and all(isinstance(k, str) for k in b):
            return all(key in b and _yaml_equal(value, b[key]) for key, value in a.items())
        # Unusual YAML key types: key lookup would conflate True/1 the same
        # way == does, so match key/value pairs structurally. Quadratic,
        # but such mappings are rare and rejected upstream for manifests.
        b_items = list(b.items())
        return all(
            any(
                _yaml_equal(a_key, b_key) and _yaml_equal(a_value, b_value)
                for b_key, b_value in b_items
            )
            for a_key, a_value in a.items()
        )
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_yaml_equal(x, y) for x, y in zip(a, b, strict=True))
    return a == b


#: Upper bound on the pre-approval uid lookup: a stalled API server must
#: never leave an agent tool call (or the debug offer) pending indefinitely.
#: On timeout the lookup fails open (write proceeds without a precondition,
#: still approval-gated and audited).
_UID_LOOKUP_TIMEOUT = 10.0

#: Upper bound on the pre-dialog dry-run round trip (issue #19): a slow or
#: unreachable API server delays the approval dialog by at most this long,
#: after which it opens without a preview - a preview must never block the
#: approval flow.
_PREVIEW_TIMEOUT = 3.0

#: Upper bound on the hint-overlay events fetch (issue #34): the trouble half
#: comes from the status the app already holds, so a stalled API connection
#: must not delay the overlay past this - the events are marked unavailable
#: instead.
_HINT_EVENTS_TIMEOUT = 3.0


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
    # Every binding carries an ``id`` so the `keybindings:` config section
    # can remap it via Textual's keymap (issue #35); uppercase duplicates
    # of shift+<letter> keys share the action under an ``--alt`` id.
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit", id="quit"),
        Binding("question_mark", "help", "Help", id="help"),
        Binding("colon", "open_command", "Command", id="open_command"),
        Binding("slash", "open_filter", "Filter/Search", id="open_filter"),
        Binding("0", "toggle_all_namespaces", "All NS", id="toggle_all_namespaces"),
        Binding("d", "describe", "Describe", id="describe"),
        Binding("s", "shell", "Shell", id="shell"),
        Binding("l", "logs", "Logs", id="logs"),
        Binding("shift+l", "logs_multi", "Multi-log", id="logs_multi"),
        # Real terminals deliver Shift+<letter> as the uppercase character,
        # not "shift+x"; bind both so the shortcut works outside Pilot tests.
        Binding("L", "logs_multi", "Multi-log", show=False, id="logs_multi--alt"),
        Binding("f", "log_format", "JSON/raw", id="log_format"),
        Binding("w", "log_wrap", "Wrap", show=False, id="log_wrap"),
        Binding("t", "log_timestamps", "Timestamps", show=False, id="log_timestamps"),
        Binding("ctrl+s", "log_save", "Save logs", show=False, id="log_save"),
        Binding("p", "log_previous", "Prev logs", id="log_previous"),
        Binding("n", "log_search_next", "Next hit", id="log_search_next"),
        Binding("shift+n", "log_search_prev", "Prev hit / Sort name", id="log_search_prev"),
        Binding(
            "N", "log_search_prev", "Prev hit / Sort name", show=False, id="log_search_prev--alt"
        ),
        # Column sorting (issue #37); shift+n doubles as sort-by-name when
        # no search pane is open (see action_log_search_prev).
        Binding("shift+a", "sort_by_age", "Sort age", show=False, id="sort_by_age"),
        Binding("A", "sort_by_age", "Sort age", show=False, id="sort_by_age--alt"),
        Binding("shift+c", "sort_by_cpu", "Sort CPU", show=False, id="sort_by_cpu"),
        Binding("C", "sort_by_cpu", "Sort CPU", show=False, id="sort_by_cpu--alt"),
        Binding("shift+m", "sort_by_mem", "Sort MEM", show=False, id="sort_by_mem"),
        Binding("M", "sort_by_mem", "Sort MEM", show=False, id="sort_by_mem--alt"),
        Binding("ctrl+a", "toggle_agent", "AI", priority=True, id="toggle_agent"),
        Binding("ctrl+d", "delete_resource", "Delete", id="delete_resource"),
        Binding("r", "rollout_restart", "Restart", show=False, id="rollout_restart"),
        Binding("R", "resize_pod", "Resize", show=False, id="resize_pod"),
        Binding("S", "scale_resource", "Scale", show=False, id="scale_resource"),
        Binding("e", "edit_resource", "Edit", show=False, id="edit_resource"),
        Binding("i", "hint_details", "Hint details", show=False, id="hint_details"),
        Binding("I", "operator_install", "Install/Approve", show=False, id="operator_install"),
        # Node ops (issue #40): cordon / uncordon / drain, nodes view only.
        Binding("c", "cordon_node", "Cordon", show=False, id="cordon_node"),
        Binding("u", "uncordon_node", "Uncordon", show=False, id="uncordon_node"),
        Binding("shift+d", "drain_node", "Drain", show=False, id="drain_node"),
        Binding("D", "drain_node", "Drain", show=False, id="drain_node--alt"),
    ]

    # User-facing keys handled in event handlers rather than BINDINGS:
    # Enter drills down via `on_data_table_row_selected`, Escape closes
    # panes / pops a drill level via `on_key`.  Listed here so the help
    # overlay (`?`) renders them alongside the real bindings.
    HANDLER_KEY_HELP: ClassVar[tuple[tuple[str, str, str], ...]] = (
        ("Table", "enter", "Drill down (pods → containers, deploy → rs → pods)"),
        ("Table", "escape", "Pop one drill-down level"),
        ("Logs", "escape", "Close pane (or dismiss search)"),
    )

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
        get_events: EventsFetcher | None = None,
        stream_logs: Callable[..., AsyncIterator[LogLine]] | None = None,
        agent_runtime: AgentRuntime | None = None,
        agent_model_name: str | None = None,
        agent_configurator: AgentConfigurator | None = None,
        rebuild_agent: Callable[[AgentSettings], AgentRuntime | None] | None = None,
        write_ops: WriteOps | None = None,
        audit: AuditLog | None = None,
        check_permission: Callable[[str, str, str, str | None, str, str], Awaitable[bool]]
        | None = None,
        mcp: MCPController | None = None,
        edit_text: Callable[[str], Awaitable[str | None]] | None = None,
        metrics: MetricsPoller | None = None,
        pod_resize_supported: bool = False,
    ) -> None:
        super().__init__()
        self.config = config
        self.store = store
        self.watch_manager = watch_manager
        self._list_namespaces = list_namespaces
        self._get_manifest = get_manifest
        self._get_events = get_events
        self._stream_logs = stream_logs
        self._write_ops = write_ops
        self._audit = audit
        self._check_permission = check_permission
        self._mcp = mcp
        self._edit_text = edit_text
        self._metrics = metrics
        #: pods/resize subresource discovered on the connected cluster
        #: (1.35 GA); gates the R keybinding and the resize agent tool.
        self._pod_resize_supported = pod_resize_supported
        #: the in-flight drain worker, if any - pressing the drain key again
        #: cancels it (evictions stop; the node stays cordoned).
        self._drain_worker: Worker[None] | None = None
        self._drain_node: str | None = None
        self._drain_progress: str = ""
        # Bounded post-eviction termination wait (tests shrink these).
        self._drain_wait_timeout: float = 120.0
        self._drain_wait_poll: float = 2.0
        self._permission_check_warned = False
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
        # Serializes view/scope switches: keyboard NavigateCommands and the
        # agent's navigate tool share this handler, which yields while
        # stopping/starting watches — interleaving would corrupt state.
        self._nav_lock = asyncio.Lock()
        self.aliases: dict[str, ResourceMeta] = (
            aliases if aliases is not None else dict(_DEFAULT_ALIASES)
        )
        self.current_kind: str = "pods"
        self.current_scope: str = config.namespace or "default"
        self.filter_pattern = ""
        #: Parsed form of filter_pattern (label selector / regex / fuzzy /
        #: inverse / hide-completed — issue #44); single matcher shared by
        #: the table render and the agent's view of "what the user sees".
        self._resource_filter: ResourceFilter = parse_filter("")
        #: Validated `keybindings:` overrides (action → key), applied via the
        #: keymap in on_mount; the help overlay renders these keys (issue #35).
        self._keybinding_overrides: dict[str, str] = {}
        #: Active sort per view kind (issue #37): the choice survives watch
        #: updates (every render re-applies it) and switching views restores
        #: each kind's own sort.
        self._sorts: dict[str, SortSpec] = {}
        # Drill-down levels (deploy -> rs -> pods); single source for the
        # breadcrumb line and the owner-uid filter on the current table.
        self._drill = NavigationStack()
        self._log_tasks: set[asyncio.Task[None]] = set()
        self._log_buffer: LogBuffer | None = None
        self._log_error: bool = False
        self._current_log_triples: list[tuple[str, str, str]] = []
        self._log_pane_gen: int = 0
        self._current_log_force_prefix: bool = False
        self._log_pane_mode: str = ""
        self._reconnect_sleep: float = 1.0
        self._ns_prefetch_task: asyncio.Task[None] | None = None
        self._splash_shown_at: float = monotonic()
        self._log_buffer_max_lines: int = config.log_buffer_lines
        # Kinds with a table render already queued — coalesces the per-object
        # notifications of a LIST seed into a single rebuild (see _on_store_update).
        self._render_pending: set[str] = set()
        # Hint strip event cache: "ns/name" -> (fetched_at, newest warning line
        # or None). Short TTL so a lingering cursor eventually sees new events.
        self._hint_event_cache: dict[str, tuple[float, str | None, datetime | None]] = {}
        self._hint_refresh_timer: Timer | None = None

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
        yield DescribePane()
        agent_panel = AgentPanel()
        agent_panel.display = False
        yield agent_panel
        yield CommandBar()
        yield FilterBar()
        yield NamespacePicker()
        yield HintStrip()
        yield StatusBar()

    @classmethod
    def _binding_actions(cls) -> dict[str, tuple[str, ...]]:
        """Every remappable app action mapped to its default keys."""
        actions: dict[str, tuple[str, ...]] = {}
        for raw in cls.BINDINGS:
            binding = raw if isinstance(raw, Binding) else Binding(*raw)
            actions[binding.action] = (*actions.get(binding.action, ()), binding.key)
        return actions

    def _apply_keybindings(self) -> None:
        """Apply the `keybindings:` config overrides via the keymap (issue #35).

        Only app bindings carry keymap ids, so the approval dialogs'
        confirm keys are structurally out of reach; `plan_keybindings`
        additionally rejects their action names and blocks priority
        actions from taking the dialogs' keys. Shifted-letter overrides
        expand to both spellings (`shift+g,G`) because real terminals
        deliver Shift+<letter> as the uppercase character.
        """
        bindings = [raw if isinstance(raw, Binding) else Binding(*raw) for raw in self.BINDINGS]
        priority_actions = {binding.action for binding in bindings if binding.priority}
        plan = plan_keybindings(self.config.keybindings, self._binding_actions(), priority_actions)
        self._keybinding_overrides = plan.overrides
        keymap: dict[str, str] = {}
        for binding in bindings:
            if binding.id is not None and binding.action in plan.overrides:
                keymap[binding.id] = shift_alias_keys(plan.overrides[binding.action])
        if keymap:
            self.set_keymap(keymap)
        for warning in plan.warnings:
            self.notify(warning, title="Keybindings", severity="warning")

    async def on_mount(self) -> None:
        self._apply_keybindings()
        # Wire the `known` closure into CommandBar so parse_command can resolve aliases.
        command_bar = self.query_one(CommandBar)
        command_bar.known = lambda a: self._canonical_kind(a) if a in self.aliases else None
        command_bar.command_words = sorted({*self.aliases, "ns", "namespaces", "q", "quit"})
        # Seed session-scoped log display settings from config (logs.wrap /
        # logs.timestamps); the w/t keys toggle them from there.
        log_pane = self.query_one(LogPane)
        log_pane.wrap_lines = self.config.log_wrap
        log_pane.show_timestamps = self.config.log_timestamps
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
        if self._metrics is not None:
            # Metrics updates reuse the pods render path; the pending guard in
            # _on_store_update coalesces them with watch events.
            self._metrics.on_update = lambda: _on_store_update("pods")
        await self._sync_metrics_poller()
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
        try:
            splash = self.query_one(SplashLogo)
            table = self.query_one(ResourceTable)
        except NoMatches:
            return  # app is shutting down; a queued render must not crash
        if not splash.display:
            return
        if not self.is_headless:
            remaining = self._splash_shown_at + self.SPLASH_MIN_SECONDS - monotonic()
            if remaining > 0:
                self.set_timer(remaining, self._dismiss_splash)
                return
        splash.display = False
        table.display = True

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
        try:
            table = self.query_one(ResourceTable)
        except NoMatches:
            return  # shutdown race: a queued render after widgets are removed
        rows = self.store.get(kind, self.current_scope)
        drill_uid = self._drill.parent_uid
        if drill_uid is not None and kind == self._drill.child_kind:
            rows = [r for r in rows if owned_by(r, drill_uid)]
        rows = self._filtered_rows(rows)
        all_namespaces = self.current_scope == ALL_NAMESPACES
        metrics = None
        if kind == "pods" and self._metrics is not None and self._metrics.available:
            metrics = self._metrics.get
        # Dispatch rendering on the resolved meta: a group-qualified view
        # kind (alias collision) must still get its typed table, and the
        # serving group scopes group-specific renderings (the OLM tables).
        meta = self.aliases.get(kind)
        table.show(
            meta.plural if meta is not None else kind,
            rows,
            all_namespaces=all_namespaces,
            # Filtering happened upstream (issue #44: labels/regex/fuzzy need
            # the full summaries, not just names) — no name pattern remains.
            pattern="",
            metrics=metrics,
            group=meta.group if meta is not None else "",
            sort=self._sorts.get(kind),
        )
        self._refresh_empty_state(kind, table.row_count)
        # The strip is driven by RowHighlighted on the pods view; anything
        # else (view switch, table now empty) must not leave a stale hint.
        if kind != "pods" or table.row_count == 0:
            with contextlib.suppress(NoMatches):  # shutdown race, same as the table guard
                self.query_one(HintStrip).clear_hint()

    def on_show_error(self, message: ShowError) -> None:
        self.notify(message.detail, title=message.title, severity="error")

    def action_help(self) -> None:
        """Open the help overlay generated from the live binding lists (issue #41)."""
        groups = collect_help(
            list(self.BINDINGS),
            list(DescribeScreen.BINDINGS),
            handler_keys=self.HANDLER_KEY_HELP,
            overrides=self._keybinding_overrides,
        )
        self.push_screen(HelpScreen(groups, command_help()))

    def action_open_command(self) -> None:
        # Dismiss the filter bar first so no invisible filter stays active.
        self.query_one(FilterBar).dismiss_bar()
        self.query_one(CommandBar).open()

    def action_open_filter(self) -> None:
        # When the describe pane is open, / searches inside it (issue #42).
        describe_pane = self.query_one(DescribePane)
        if describe_pane.display:
            describe_pane.open_search()
            return
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
        self._resource_filter = parse_filter(message.pattern)
        self.post_message(ResourcesUpdated(self.current_kind))
        self._refresh_status()

    def on_clear_filter(self, message: ClearFilter) -> None:
        self.filter_pattern = ""
        self._resource_filter = parse_filter("")
        self.post_message(ResourcesUpdated(self.current_kind))
        self._refresh_status()

    def _filtered_rows(self, rows: list[Summary]) -> list[Summary]:
        """Apply the active filter the way the table renders it (issue #44)."""
        flt = self._resource_filter
        if not flt.active:
            return rows
        return [
            r
            for r in rows
            if flt.matches(
                r.name,
                labels=dict(getattr(r, "labels", ())),
                phase=getattr(r, "phase", None),
            )
        ]

    async def on_navigate_command(self, message: NavigateCommand) -> None:
        # An explicit :view / agent navigate abandons any drill-down context;
        # drill navigation goes through _navigate directly to keep its stack.
        # The stack clear happens inside the navigation lock so a concurrent
        # drill (agent path) can never interleave between clear and the
        # kind/scope transition, which would strand a filterless child view.
        await self._navigate(message.view, message.namespace, drill_op=self._drill.clear)

    def _default_scope_for(self, view: str | None, namespace: str | None) -> str | None:
        """Catalog entries live in catalog namespaces (e.g. "olm"), not the
        user's workload namespace: any packagemanifests view without an
        explicit namespace defaults to the cluster-wide scope or the table
        would commonly come up empty. Applied inside _navigate so every
        entry path (command bar, agent, drill) behaves alike."""
        if namespace is not None or view is None:
            return namespace
        meta = self.aliases.get(view)
        if meta is not None and (meta.group, meta.plural) == (
            PACKAGES_GROUP,
            "packagemanifests",
        ):
            return ALL_NAMESPACES
        return None

    async def _navigate(
        self,
        view: str | None,
        namespace: str | None,
        *,
        drill_op: Callable[[], None] | None = None,
    ) -> None:
        # The lock serializes the agent path (direct call from the agent
        # task) with the keyboard path (message pump): both mutate
        # current_kind/current_scope across awaits. The final state is the
        # latest command's — a user keystroke arriving after an agent
        # navigate always lands last. ``drill_op`` mutates the drill stack
        # inside the same critical section so stack and view transition as
        # one transaction.
        async with self._nav_lock:
            if drill_op is not None:
                drill_op()
            await self._navigate_locked(view, self._default_scope_for(view, namespace))
        self.post_message(ResourcesUpdated(self.current_kind))
        self._refresh_status()

    async def _navigate_locked(self, view: str | None, namespace: str | None) -> None:
        """Kind/scope transition body; caller must hold ``_nav_lock``."""
        # A describe pane covering the table would show a stale manifest
        # over the new view — dismiss it on any navigation, even when the
        # requested kind/scope already matches.
        self.query_one(DescribePane).hide()
        new_kind = view if view is not None else self.current_kind
        new_scope = namespace if namespace is not None else self.current_scope
        if new_kind != self.current_kind or new_scope != self.current_scope:
            await self._close_log_pane()
            await self.watch_manager.stop(self.current_kind, self.current_scope)
            self.current_kind = new_kind
            self.current_scope = new_scope
            await self.watch_manager.start(self.current_kind, self.current_scope)
            await self._sync_metrics_poller()

    async def _sync_metrics_poller(self) -> None:
        """Poll metrics only while the pods view is on screen, in its scope.

        metrics.k8s.io has no watch support, so this poller is the one
        recurring request the app makes - stopping it off the pods view
        keeps background load at zero for other kinds.
        """
        if self._metrics is None:
            return
        if self.current_kind != "pods":
            await self._metrics.stop()
            return
        namespace = None if self.current_scope == ALL_NAMESPACES else self.current_scope
        await self._metrics.start(namespace)

    async def action_toggle_all_namespaces(self) -> None:
        """Toggle scope between ALL_NAMESPACES and the config-default namespace.

        Routed through the locked navigate handler so it serializes with
        agent-driven navigation (both stop/start watches across awaits).
        """
        if self.current_scope == ALL_NAMESPACES:
            new_scope = self.config.namespace or "default"
        else:
            new_scope = ALL_NAMESPACES
        await self.on_navigate_command(NavigateCommand(None, new_scope))

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

    #: Seconds a fetched warning-event line stays cached per pod. Short enough
    #: that a cursor parked on a crashing pod eventually sees fresh events.
    _HINT_EVENT_TTL = 15.0

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Cursor movement drives the ops hint strip (pods view only)."""
        if not isinstance(event.data_table, ResourceTable):
            return
        if self.current_kind != "pods" or event.row_key is None:
            self.query_one(HintStrip).clear_hint()
            return
        self._show_hint_for_row(str(event.row_key.value))

    def _show_hint_for_row(self, row_key: str) -> None:
        """Render the hint for one pod row: cached event line when fresh,
        otherwise the status-derived hint plus a background event fetch."""
        strip = self.query_one(HintStrip)
        summary = self._find_pod_summary(row_key)
        if summary is None or not _pod_needs_hint(summary):
            strip.clear_hint()
            return
        # uid in the cache key: a recreated pod must not inherit the cached
        # event line of its previous incarnation.
        cache_key = f"{row_key}#{summary.uid}"
        cached = self._hint_event_cache.get(cache_key)
        if cached is not None and (age := monotonic() - cached[0]) < self._HINT_EVENT_TTL:
            _at, line, event_ts = cached
            if line is not None and not _event_line_fresh(event_ts, summary):
                # A newer termination arrived since the line was cached.
                line = None
            if summary.trouble or line:
                strip.show_trouble(summary.trouble, event=line)
            else:
                strip.clear_hint()
            # Keep the parked-cursor refresh armed for the entry's remaining
            # life — switching rows and back must not strand it timerless.
            self._schedule_hint_refresh(row_key, delay=self._HINT_EVENT_TTL - age)
            return
        if summary.trouble:
            strip.show_trouble(summary.trouble)
        else:
            # Event-only hint (e.g. Running but not ready): nothing to show
            # until the warning event arrives.
            strip.clear_hint()
        if self._get_events is not None:
            self.run_worker(
                self._fetch_hint_event(row_key, cache_key, summary),
                exclusive=True,
                group="hint-events",
            )

    def _cursor_row_key(self) -> str | None:
        """Row key under the table cursor, or None (empty table / no cursor)."""
        try:
            table = self.query_one(ResourceTable)
        except NoMatches:  # timer fired while the app is shutting down
            return None
        if table.cursor_row < 0:
            return None
        try:
            key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        except CellDoesNotExist:
            return None
        return None if key is None else str(key.value)

    def _schedule_hint_refresh(self, row_key: str, *, delay: float | None = None) -> None:
        """Re-evaluate a parked cursor when the cache entry expires; without
        this a cursor that never moves would show the same event forever."""
        if self._hint_refresh_timer is not None:
            self._hint_refresh_timer.stop()

        def _refresh() -> None:
            self._hint_refresh_timer = None
            if self.current_kind == "pods" and self._cursor_row_key() == row_key:
                self._show_hint_for_row(row_key)

        self._hint_refresh_timer = self.set_timer(
            max(0.05, delay if delay is not None else self._HINT_EVENT_TTL), _refresh
        )

    def _find_pod_summary(self, row_key: str) -> PodSummary | None:
        parts = row_key.split("/", 1)
        if len(parts) != 2:
            return None
        namespace, name = parts
        for obj in self.store.get("pods", self.current_scope):
            if obj.namespace == namespace and obj.name == name and isinstance(obj, PodSummary):
                return obj
        return None

    async def _fetch_hint_event(self, row_key: str, cache_key: str, summary: PodSummary) -> None:
        """Best-effort: append the newest warning event to the visible strip."""
        if self._get_events is None:  # caller guards; satisfy the type checker
            return
        try:
            events = await self._get_events.fetch(
                summary.namespace, summary.name, uid=summary.uid or None
            )
        except Exception:  # events are decoration; the status-derived hint already shows
            self._store_hint_event(cache_key, None, None)
            # Retry once the TTL passes: a transient API failure must not
            # hide the hint forever while the cursor stays parked.
            if self.current_kind == "pods" and self._cursor_row_key() == row_key:
                self._schedule_hint_refresh(row_key)
            return
        # The snapshot taken at highlight time may be stale after the await:
        # re-read the store and filter/render against the *current* status.
        fresh = self._find_pod_summary(row_key)
        if fresh is None or fresh.uid != summary.uid:
            # Deleted or recreated mid-fetch: the results describe the old
            # incarnation. Re-evaluate the row so the new one gets its own pass.
            if self.current_kind == "pods" and self._cursor_row_key() == row_key:
                self._show_hint_for_row(row_key)
            return
        found = _newest_warning(events)
        line, event_ts = found if found is not None else (None, None)
        if line is not None and not _event_line_fresh(event_ts, fresh):
            line, event_ts = None, None
        self._store_hint_event(cache_key, line, event_ts)
        if self.current_kind != "pods" or self._cursor_row_key() != row_key:
            return
        self._schedule_hint_refresh(row_key)
        if not _pod_needs_hint(fresh):
            self.query_one(HintStrip).clear_hint()
            return
        if fresh.trouble or line:
            self.query_one(HintStrip).show_trouble(fresh.trouble, event=line)
        else:
            self.query_one(HintStrip).clear_hint()

    def _store_hint_event(
        self, cache_key: str, line: str | None, event_ts: datetime | None
    ) -> None:
        """Cache the fetched line (with its occurrence time, so cache hits can
        re-apply freshness); expired entries are swept on every write so the
        cache cannot grow without bound in a long-running session."""
        now = monotonic()
        expired = [
            k
            for k, (at, _line, _ts) in self._hint_event_cache.items()
            if now - at >= self._HINT_EVENT_TTL
        ]
        for k in expired:
            del self._hint_event_cache[k]
        self._hint_event_cache[cache_key] = (now, line, event_ts)

    def action_hint_details(self) -> None:
        """Open the read-only detail overlay for the hinted pod row (issue #34):
        the full trouble list plus recent Warning events - everything the
        two-line strip folded away."""
        if self.current_kind != "pods":
            return
        row_key = self._cursor_row_key()
        if row_key is None:
            return
        summary = self._find_pod_summary(row_key)
        if summary is None or not _pod_needs_hint(summary):
            return
        self.run_worker(
            self._open_hint_details(row_key, summary), exclusive=True, group="hint-detail"
        )

    async def _open_hint_details(self, row_key: str, summary: PodSummary) -> None:
        """Fetch events best-effort, then push the overlay: the trouble half
        renders even when the events API fails ("unavailable" is stated, not
        conflated with "no events"). The context is revalidated after the
        await - the cursor, view, or screen stack may have changed meanwhile,
        and stale details for the wrong pod are worse than none."""
        events: list[dict[str, Any]] = []
        events_unavailable = False
        if self._get_events is not None:
            try:
                events = await asyncio.wait_for(
                    self._get_events.fetch(
                        summary.namespace, summary.name, uid=summary.uid or None
                    ),
                    timeout=_HINT_EVENTS_TIMEOUT,
                )
            except Exception:  # events are decoration; trouble alone still helps
                events_unavailable = True
        if len(self.screen_stack) > 1:  # another dialog opened during the fetch
            return
        if self.current_kind != "pods" or self._cursor_row_key() != row_key:
            return
        fresh = self._find_pod_summary(row_key)
        if fresh is None or fresh.uid != summary.uid:
            return  # deleted or recreated mid-fetch
        if not _pod_needs_hint(fresh):
            return  # recovered mid-fetch: the strip is gone, details would be noise
        await self.push_screen(
            HintDetailScreen(
                f"{summary.namespace}/{summary.name}",
                fresh.trouble,
                events,
                events_unavailable=events_unavailable,
            )
        )

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter drills down: pods -> containers (k9s convention); kinds with a
        registered ownership child (deploy -> rs -> pods) push a drill level."""
        if not isinstance(event.data_table, ResourceTable):
            return
        if self.current_kind != "pods":
            if drill_child(self._canonical_kind(self.current_kind)) is None:
                # No drill chain for this kind: leave Enter unconsumed so
                # future handlers (e.g. a default describe) can claim it.
                return
            event.stop()
            await self._drill_down_selected(str(event.row_key.value))
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

    def _canonical_kind(self, kind: str) -> str:
        meta = self.aliases.get(kind)
        if meta is None:
            return kind
        if self.aliases.get(meta.plural) == meta:
            return meta.plural
        # The bare plural belongs to a different meta (a same-plural CRD from
        # another group won the alias collision): keep the qualified alias as
        # the canonical view kind so watching, rendering, and writes all
        # resolve the meta this alias actually names.
        return kind

    async def _drill_down_selected(self, row_key: str) -> None:
        """Keyboard Enter: push a drill level for the selected row."""
        if drill_child(self._canonical_kind(self.current_kind)) is None:
            return  # kind has no drill-down chain; Enter is a no-op
        parts = row_key.split("/", 1)
        if len(parts) != 2:
            return
        error = await self._drill_into(parts[0], parts[1])
        if error is not None:
            self.notify(error, severity="warning")

    async def _drill_into(self, namespace: str, name: str) -> str | None:
        """Push a drill level for (namespace, name) in the current view and
        navigate to the child kind. Returns an error message, or None on success."""
        canonical = self._canonical_kind(self.current_kind)
        child = drill_child(canonical)
        if child is None:
            return f"{canonical} has no drill-down chain"
        if child not in self.aliases:
            return f"{child} not discovered yet, try again shortly"
        obj = next(
            (
                o
                for o in self.store.get(self.current_kind, self.current_scope)
                if o.namespace == namespace and o.name == name
            ),
            None,
        )
        if obj is None:
            return f"no {canonical} named {name!r} in the current view"
        uid = str(getattr(obj, "uid", "") or "")
        if not uid:
            return f"cannot drill into {name}: no uid available"
        level = DrillLevel(
            parent_kind=canonical,
            parent_name=name,
            parent_namespace=namespace,
            parent_uid=uid,
            child_kind=child,
        )
        # Push and navigate as one transaction under the navigation lock:
        # a concurrent :view/agent navigate can then never observe (or
        # strand) a pushed level without its matching child view. If the
        # transition itself fails, the pushed level is rolled back.
        async with self._nav_lock:
            self._drill.push(level)
            try:
                await self._navigate_locked(child, None)
            except BaseException:
                self._drill.pop()
                raise
        self.post_message(ResourcesUpdated(self.current_kind))
        self._refresh_status()
        return None

    async def _pop_drill(self) -> bool:
        """Pop one drill level and navigate back to its parent kind as one
        transaction under the navigation lock. Returns False when the stack
        was empty (nothing to pop)."""
        async with self._nav_lock:
            popped = self._drill.pop()
            if popped is None:
                return False
            await self._navigate_locked(popped.parent_kind, None)
        self.post_message(ResourcesUpdated(self.current_kind))
        self._refresh_status()
        return True

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
                events = await self._get_events.fetch(namespace, name)
            except ApiStatusError as exc:
                # Events are best-effort; surface but still show the manifest.
                msg = explain_api_error(exc.status, exc.reason, "events", namespace)
                self.notify(msg, severity="warning")

        title = f"{self.current_kind}/{namespace}/{name}"
        if manifest.get("kind") == "Secret":
            # Secrets get the dedicated masked viewer (spec §5 #9): values
            # render masked; per-key reveal is explicit and audit-logged.
            await self.push_screen(SecretScreen(title, manifest, audit=self._audit))
            return
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
        if head == "mcp":
            self._handle_mcp_command(parts[1:])
            return
        if head == "operators" and "operators" not in self.aliases:
            # The catalog view only exists where OLM serves PackageManifests;
            # explain the absence instead of a generic unknown-kind error.
            # Only when the alias is genuinely unavailable: a syntax error on
            # a discovered view (":operators ns extra") falls through to the
            # normal unknown-command message.
            # "Not discovered" and not "absent": background discovery may
            # still be running, or may have failed (pods-only fallback) -
            # indistinguishable states from here.
            self.notify(
                "The operator catalog is unavailable: the"
                " packages.operators.coreos.com API group was not discovered"
                " (OLM may be absent, or discovery may still be running)",
                severity="warning",
            )
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

    def _handle_mcp_command(self, args: list[str]) -> None:
        """`:mcp` shows server state; `:mcp on` / `:mcp off` toggle it live."""
        mcp = self._mcp
        if mcp is None:
            self.notify("MCP unavailable in this build", severity="warning")
            return
        if not args:
            self.notify(mcp.status())
            return
        action = args[0].lower()
        if action not in ("on", "off"):
            self.notify("Usage: :mcp [on|off]", severity="warning")
            return

        async def _switch() -> None:
            msg = await (mcp.start() if action == "on" else mcp.stop())
            self.notify(msg, severity="error" if msg.startswith("ERROR") else "information")
            self._refresh_status()

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
            self.call_from_thread(self._schedule_debug_offer, namespace, name, container, exit_code)

        self.run_worker(_probe_and_maybe_offer, thread=True)

    def _schedule_debug_offer(
        self, namespace: str, name: str, container: str | None, exit_code: int
    ) -> None:
        """Sync shim for call_from_thread: the offer itself is async because
        it awaits the RBAC pre-check."""
        self.run_worker(self._offer_debug_fallback(namespace, name, container, exit_code))

    async def _offer_debug_fallback(
        self, namespace: str, name: str, container: str | None, exit_code: int
    ) -> None:
        """Ask whether to attach a kubectl debug container after a failed shell."""
        if self.config.readonly or self._audit is None:
            # kubectl debug mutates the pod spec (ephemeral container):
            # never offer a write we would refuse to run.
            self.notify(
                "Shell failed and the debug fallback is unavailable"
                " (read-only mode or no audit log)",
                severity="warning",
            )
            return
        pods_meta = self.aliases.get("pods")
        if pods_meta is None:
            # Fail-open like the other permission paths, but never silently.
            logger.warning("pods alias missing; skipping debug RBAC pre-check (fail-open)")
        elif not await self._permitted("debug", pods_meta, namespace, name):
            # RBAC pre-check (spec debug safety contract): don't offer a
            # picker the API server would reject; _permitted notified with
            # "missing permission: patch pods/ephemeralcontainers".
            return
        target = f"{name}/{container}" if container else name
        try:
            # One manifest fetch serves two purposes: binding the offer to
            # this pod incarnation (kubectl debug addresses the pod by
            # namespace/name only, so without the uid a same-named
            # replacement created while the dialogs are open would receive
            # the ephemeral container - _run_debug re-checks the uid just
            # before executing) and runtime detection for the image
            # recommendation (issue #52). 404 -> the pod is already gone.
            manifest = await self._debug_manifest(namespace, name)
        except ApiStatusError:
            self.notify(
                f"Debug fallback for {target} not offered - the pod no longer exists.",
                severity="warning",
            )
            return
        approved_uid: str | None = None
        if manifest is not None:
            raw_uid = (manifest.get("metadata") or {}).get("uid")
            approved_uid = str(raw_uid) if raw_uid else None
        if len(self.screen_stack) > 1:
            # The probe/RBAC pre-check ran concurrently with user input: never
            # stack the offer over a dialog that opened meanwhile.
            self.notify(
                f"Debug fallback for {target} not offered - another dialog is open."
                " Close it and press 's' again to retry.",
                severity="warning",
            )
            return
        self._pick_debug_image(namespace, name, container, exit_code, approved_uid, manifest or {})

    def _pick_debug_image(
        self,
        namespace: str,
        name: str,
        container: str | None,
        exit_code: int,
        approved_uid: str | None,
        manifest: dict[str, Any],
    ) -> None:
        """Debug image picker (issue #52): runtime-aware recommendation first,
        alternatives after, plus a custom-image prompt."""
        target = f"{name}/{container}" if container else name
        options = recommend_debug_images(
            manifest,
            container,
            images_cfg=self.config.debug_images,
            default_image=self.config.debug_default_image,
        )
        prompts = {f"{opt.image}  ({opt.label})": opt.image for opt in options}
        custom_choice = "Custom image…"

        def _on_image(choice: str | None) -> None:
            if choice is None:
                return
            if choice == custom_choice:

                def _on_custom(image: str | None) -> None:
                    if image:
                        self._confirm_debug(
                            namespace, name, container, exit_code, approved_uid, image
                        )

                self.push_screen(ImagePrompt(target), _on_custom)
                return
            self._confirm_debug(
                namespace, name, container, exit_code, approved_uid, prompts[choice]
            )

        # Choosing an image is read-only: even if input buffered before this
        # asynchronous picker existed selects an entry, the pod mutation is
        # still gated by the ConfirmScreen pushed in _confirm_debug, whose
        # creation-time key cutoff discards such buffered keystrokes.
        # Air-gapped configs without a matching mapping produce no options:
        # the picker then offers only the custom-image prompt.
        title = f"Shell failed in {target} (exit {exit_code}) - choose a debug image."
        if options:
            title += f"\nRecommended: {options[0].image} - {options[0].reason}"
        self.push_screen(
            PickScreen(title, [*prompts, custom_choice]),
            _on_image,
        )

    async def _debug_manifest(self, namespace: str, name: str) -> dict[str, Any] | None:
        """Pod manifest at debug-offer time (uid binding + runtime detection).

        Same semantics as `_target_uid`: raises `ApiStatusError(404)` when the
        pod is gone; fails open (`None`) when no manifest source is wired or
        the lookup fails or times out - the debug stays approval-gated and
        audited, just without a uid precondition or a runtime recommendation.
        """
        if self._get_manifest is None:
            return None
        try:
            return await asyncio.wait_for(
                self._get_manifest("pods", namespace, name), _UID_LOOKUP_TIMEOUT
            )
        except ApiStatusError as exc:
            if exc.status == 404:
                raise
            logger.warning(
                "manifest lookup for %s/%s failed; offering debug without it", namespace, name
            )
            return None
        except TimeoutError:
            logger.warning(
                "manifest lookup for %s/%s timed out; offering debug without it", namespace, name
            )
            return None
        except Exception:
            # Fail open like _target_uid: an infrastructure error must not
            # escape the worker and silently swallow the debug offer.
            logger.exception(
                "manifest lookup for %s/%s failed; offering debug without it", namespace, name
            )
            return None

    def _confirm_debug(
        self,
        namespace: str,
        name: str,
        container: str | None,
        exit_code: int,
        approved_uid: str | None,
        image: str,
    ) -> None:
        """Approval gate for the debug fallback with the chosen image.

        ConfirmScreen, not a generic picker: its creation-time key cutoff
        discards any input buffered before the prompt existed - a queued
        Enter or y must never start a pod mutation the user has not seen.
        """
        target = f"{name}/{container}" if container else name

        def _on_choice(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(self._run_debug(namespace, name, container, approved_uid, image))

        self.push_screen(
            ConfirmScreen(
                f"Shell failed in {target} (exit {exit_code})",
                f"kubectl debug: attach a {image} debug container to pod"
                f" {name}{self._write_locus(namespace)} - the target image likely"
                " has no sh/bash (distroless). Note: the ephemeral container stays"
                " in the pod spec until restart.",
            ),
            _on_choice,
        )

    async def _run_debug(
        self,
        namespace: str,
        name: str,
        container: str | None,
        approved_uid: str | None,
        image: str = DEBUG_IMAGE,
    ) -> None:
        """Attach an ephemeral busybox container via kubectl debug. This is a
        pod mutation: blocked in readonly sessions and audited fail-closed
        like every other write (user approval came from the fallback prompt).
        kubectl cannot carry a uid precondition, so the approved pod
        incarnation is re-verified immediately before executing and the debug
        aborts when the pod was replaced or removed while the dialog was open
        (narrowing the race from the unbounded dialog lifetime to the exec
        latency). Audit appends take blocking locks and fsync, so they run off
        the event loop (like _audit_write) - intent is still recorded before
        the mutation starts."""
        if self.config.readonly:
            self.notify("Read-only mode: cluster writes are disabled", severity="warning")
            return
        audit = self._audit
        if audit is None:
            self.notify("Writes disabled: no audit log configured", severity="warning")
            return
        if approved_uid is not None and not await self._debug_uid_unchanged(
            namespace, name, approved_uid
        ):
            return
        detail = f"ephemeral debug container (kubectl debug, image {image})"
        try:
            await asyncio.to_thread(self._audit_debug, audit, namespace, name, detail, "intent")
        except Exception:
            logger.exception("audit append failed; blocking kubectl debug")
            self.notify("Write blocked: audit log unavailable", severity="error")
            return
        argv = build_debug_argv(
            namespace, name, container, context=self.config.kube_context, image=image
        )
        target = f"{name}/{container}" if container else name
        with self.suspend():
            exit_code, pull_failure = self._run_debug_process(
                argv,
                f"korvid debug → {target} (exit to return)",
                namespace,
                name,
                image,
                approved_uid,
            )
        self.refresh()
        if exit_code is None:
            # The baseline snapshot saw a different pod incarnation: the
            # attach never started.
            await self._audit_debug_outcome(
                audit, namespace, name, detail, "error: pod replaced before attach"
            )
            self.notify(
                f"kubectl debug cancelled - pod {name} was replaced since the prompt was shown.",
                severity="warning",
            )
            return
        if pull_failure is not None:
            outcome = f"error: image pull failed ({pull_failure})"
        else:
            outcome = "success" if exit_code == 0 else f"error: exit {exit_code}"
        await self._audit_debug_outcome(audit, namespace, name, detail, outcome)
        if pull_failure is not None:
            self._offer_pull_retry(namespace, name, container, approved_uid, image, pull_failure)
            return
        if exit_code != 0:
            self.notify(
                f"kubectl debug exited with status {exit_code}"
                " — check RBAC (pods/ephemeralcontainers) and cluster version",
                severity="warning",
            )

    async def _audit_debug_outcome(
        self, audit: AuditLog, namespace: str, name: str, detail: str, outcome: str
    ) -> None:
        """Record how a kubectl debug run ended. Best-effort: the mutation
        already happened (or was aborted), so a failed append only warns."""
        try:
            await asyncio.to_thread(self._audit_debug, audit, namespace, name, detail, outcome)
        except Exception:
            logger.exception("audit append failed after kubectl debug")
            self.notify("Audit write failed for the executed debug", severity="warning")

    _PULL_CHECK_INTERVAL = 2.5
    _PULL_CHECK_DEADLINE = 30.0

    async def _debug_uid_unchanged(self, namespace: str, name: str, approved_uid: str) -> bool:
        """Re-verify the approved pod incarnation just before kubectl debug
        executes; notifies and returns False when the pod is gone or replaced."""
        try:
            current_uid = await self._target_uid("pods", namespace, name)
        except ApiStatusError:
            self.notify(
                f"kubectl debug cancelled - pod {name} no longer exists.",
                severity="warning",
            )
            return False
        if current_uid is not None and current_uid != approved_uid:
            self.notify(
                f"kubectl debug cancelled - pod {name} was replaced since the prompt was shown.",
                severity="warning",
            )
            return False
        return True

    def _run_debug_process(
        self,
        argv: list[str],
        banner: str,
        namespace: str,
        name: str,
        image: str,
        approved_uid: str | None,
    ) -> tuple[int | None, str | None]:
        """Run kubectl debug while watching for image pull failures.

        kubectl debug hangs silently on `ErrImagePull`/`ImagePullBackOff`; for
        the first `_PULL_CHECK_DEADLINE` seconds the pod's
        `ephemeralContainerStatuses` are polled and a failing pull kills the
        attach so the caller can offer a retry with the fallback image instead
        of leaving the user staring at a hung terminal. Returns
        `(exit_code, pull_failure_reason)`; an exit code of `None` means the
        attach was aborted because the baseline snapshot saw a different pod
        incarnation than the one approved.
        """
        print(f"\x1b[2J\x1b[H\x1b[2m{banner}\x1b[0m", flush=True)
        # Snapshot ephemeral containers already on the pod: failed entries
        # from earlier attempts can never be removed from the spec, and one
        # using the same image must not be blamed on this new attach. Without
        # a reliable baseline (snapshot failed) pull monitoring is disabled
        # for this attempt - a plain wait, exactly as before this feature.
        baseline = self._pod_status(namespace, name)
        if baseline is not None and approved_uid is not None:
            # The snapshot can block for seconds; don't let it widen the UID
            # TOCTOU window - re-verify the incarnation it actually saw.
            baseline_uid = (baseline.get("metadata") or {}).get("uid")
            if baseline_uid is not None and baseline_uid != approved_uid:
                return None, None
        proc = subprocess.Popen(argv)
        if baseline is None:
            return proc.wait(), None
        pre_existing = ephemeral_container_names(baseline)
        deadline = monotonic() + self._PULL_CHECK_DEADLINE
        while True:
            try:
                exit_code = proc.wait(timeout=self._PULL_CHECK_INTERVAL)
            except subprocess.TimeoutExpired:
                pass
            else:
                return self._debug_exit_result(exit_code, namespace, name, image, pre_existing)
            # Check for a failure after EVERY timed wait - including the one
            # during which the deadline elapsed - so a pull failure appearing
            # at the edge of the window is still caught.
            failure = self._check_pull_failure(namespace, name, image, ignore=pre_existing)
            if failure is not None:
                proc.kill()
                proc.wait()
                return 1, failure
            if monotonic() > deadline:
                # Pulls that survive the window are treated as slow-but-alive:
                # stop polling and wait for the interactive session to end.
                return self._debug_exit_result(proc.wait(), namespace, name, image, pre_existing)

    def _debug_exit_result(
        self, exit_code: int, namespace: str, name: str, image: str, ignore: frozenset[str]
    ) -> tuple[int, str | None]:
        """Final result for a finished kubectl debug process.

        kubectl can give up and exit nonzero on its own when the pull fails:
        a nonzero exit triggers one last pull-failure check so the fallback
        retry is offered instead of a generic exit warning.
        """
        if exit_code != 0:
            failure = self._check_pull_failure(namespace, name, image, ignore=ignore)
            if failure is not None:
                return exit_code, failure
        return exit_code, None

    def _pod_status(self, namespace: str, name: str) -> dict[str, Any] | None:
        """Pod manifest via kubectl shell-out, best-effort (None on any error).

        Used while the TUI is suspended - the async client sits behind the
        paused event loop, so the manifest is fetched with a subprocess.
        """
        argv = build_pod_get_argv(namespace, name, context=self.config.kube_context)
        try:
            result = subprocess.run(argv, capture_output=True, timeout=5)
            if result.returncode != 0:
                return None
            manifest: dict[str, Any] = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return None
        return manifest

    def _check_pull_failure(
        self, namespace: str, name: str, image: str, *, ignore: frozenset[str] = frozenset()
    ) -> str | None:
        """Pull-failure reason for `image`'s ephemeral container, best-effort.

        Runs while the TUI is suspended, so it shells out to kubectl; any
        infrastructure error means "no failure detected" and the attach keeps
        running. Containers named in `ignore` (pre-existing before the attach)
        are never blamed.
        """
        manifest = self._pod_status(namespace, name)
        if manifest is None:
            return None
        return find_pull_failure(manifest, image, ignore=ignore)

    def _offer_pull_retry(
        self,
        namespace: str,
        name: str,
        container: str | None,
        approved_uid: str | None,
        image: str,
        reason: str,
    ) -> None:
        """Offer an immediate retry with the fallback image after a pull failure.

        Air-gapped guard: when `debug.images` is configured without a
        `debug.default_image`, no public busybox is offered - notify only.
        """
        if self.config.debug_images is not None and not self.config.debug_default_image:
            fallback = None
        else:
            fallback = self.config.debug_default_image or FALLBACK_IMAGE
        target = f"{name}/{container}" if container else name
        # Equivalent references (untagged vs :latest) would retry the very
        # image that just failed - and each retry permanently adds another
        # ephemeral container entry to the pod spec.
        if fallback is None or same_image_ref(fallback, image) or len(self.screen_stack) > 1:
            self.notify(
                f"kubectl debug: image pull failed for {image} ({reason})",
                severity="error",
            )
            return

        def _on_choice(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(self._run_debug(namespace, name, container, approved_uid, fallback))

        self.push_screen(
            ConfirmScreen(
                f"Image pull failed for {image} ({reason})",
                f"Retry kubectl debug on {target}{self._write_locus(namespace)} with"
                f" {fallback}? Note: the failed ephemeral container entry cannot be"
                " removed from the pod spec; the retry attaches an additional"
                " container.",
            ),
            _on_choice,
        )

    @staticmethod
    def _audit_debug(audit: AuditLog, namespace: str, name: str, detail: str, outcome: str) -> None:
        audit.append(
            action="debug",
            kind="pods",
            group="",  # pods are core/v1; kubectl debug always targets a pod
            version="v1",
            namespace=namespace,
            name=name,
            detail=detail,
            outcome=outcome,
        )

    async def on_key(self, event: Key) -> None:
        """Escape closes describe/log panes, then pops one drill-down level."""
        if event.key != "escape":
            return
        filter_bar = self.query_one(FilterBar)
        command_bar = self.query_one(CommandBar)
        namespace_picker = self.query_one(NamespacePicker)
        if filter_bar.display or command_bar.display or namespace_picker.display:
            return  # bars and pickers own Escape while open
        describe_pane = self.query_one(DescribePane)
        if describe_pane.display:
            # An active pane search (input open or submitted hits) consumes
            # Escape first; a second Escape closes the pane itself.
            if not describe_pane.dismiss_search():
                describe_pane.hide()
            event.stop()
            return
        log_pane = self.query_one(LogPane)
        if log_pane.display:
            await self._close_log_pane()
            event.stop()
            return
        popped = await self._pop_drill()
        if popped:
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

    # -- Write operations (issue #16): every path goes through a ConfirmScreen
    # -- confirmed only by a user keystroke; executed writes are audited.

    #: Workload eligibility is keyed on (group, plural): a custom-group CRD
    #: whose plural collides with a built-in (e.g. 'deployments') must never
    #: be treated as an apps/* workload.
    _RESTARTABLE: ClassVar[frozenset[tuple[str, str]]] = frozenset(
        {("apps", "deployments"), ("apps", "statefulsets"), ("apps", "daemonsets")}
    )
    _SCALABLE: ClassVar[frozenset[tuple[str, str]]] = frozenset(
        {("apps", "deployments"), ("apps", "replicasets"), ("apps", "statefulsets")}
    )
    #: action -> (verb, subresource) for the SubjectAccessReview pre-check.
    _WRITE_VERBS: ClassVar[dict[str, tuple[str, str]]] = {
        "delete": ("delete", ""),
        "scale": ("patch", "scale"),
        "rollout_restart": ("patch", ""),
        "debug": ("patch", "ephemeralcontainers"),
        "edit": ("update", ""),
        "resize": ("patch", "resize"),
        "install": ("create", ""),
        "approve": ("update", ""),
        # Cordon/uncordon patch node.spec.unschedulable; the drain pre-check
        # covers its cordon step (evictions are per-namespace pod
        # subresource creations that surface individually during execution).
        "cordon": ("patch", ""),
        "uncordon": ("patch", ""),
        "drain": ("patch", ""),
    }

    @staticmethod
    def _gvr_label(meta: ResourceMeta) -> str:
        """Group-qualified plural ('deployments.example.io') so rejection
        messages disambiguate same-plural resources across API groups."""
        return f"{meta.plural}.{meta.group}" if meta.group else meta.plural

    @classmethod
    def _write_perm_target(cls, action: str, meta: ResourceMeta) -> tuple[str, str]:
        """(verb, resource[/subresource]) as shown in permission messages."""
        verb, subresource = cls._WRITE_VERBS[action]
        target = f"{meta.plural}/{subresource}" if subresource else meta.plural
        return verb, target

    @staticmethod
    def _write_locus(ns: str | None) -> str:
        """Namespace qualifier shown in every approval dialog so identically
        named workloads in different namespaces are distinguishable."""
        return f" in namespace {ns}" if ns else " (cluster-scoped)"

    def _selected_uid(self, ns: str | None, name: str) -> str | None:
        """Uid of the selected row's object from the store, binding an
        approval to the exact incarnation on screen; None when the summary
        type carries no uid (the write then runs without a precondition)."""
        for obj in self.store.get(self.current_kind, self.current_scope):
            if obj.namespace == (ns or "") and obj.name == name:
                uid = str(getattr(obj, "uid", "") or "")
                return uid or None
        return None

    def _uid_intact_after_fetch(
        self, manifest: dict[str, Any], ns: str | None, name: str, uid: str | None
    ) -> bool:
        """Post-await UID guarantee: after a manifest fetch, both the fetched
        object and the selected row must still be the incarnation the user
        acted on. An object deleted and recreated under the same name would
        otherwise render in the dialog while the write pins the stale UID
        (guaranteed conflict at best, wrong-object action at worst)."""
        if not uid:
            return True
        fetched_uid = str(manifest.get("metadata", {}).get("uid") or "")
        if fetched_uid and fetched_uid != uid:
            return False
        return self._selected_uid(ns, name) == uid

    def _write_target(self) -> tuple[ResourceMeta, str | None, str, str | None] | None:
        """Resolve (meta, namespace, name, uid) of the selected row for a
        write, or None (with a notification) when writes are disabled or
        nothing usable is selected. Cluster-scoped kinds get namespace=None.
        The uid pins the object incarnation the user saw: if it is deleted
        and recreated under the same name while the dialog is open, the API
        server rejects the write with a 409 instead of hitting the
        replacement."""
        if self.config.readonly:
            self.notify("Read-only mode: cluster writes are disabled", severity="warning")
            return None
        if self._audit is None:
            # Fail-closed auditing (AGENTS.md): no audit sink means no writes.
            self.notify("Writes disabled: no audit log configured", severity="warning")
            return None
        kind = self._canonical_kind(self.current_kind)
        meta = self.aliases.get(kind)
        if meta is None:
            self.notify(f"Unknown resource kind {kind!r}", severity="warning")
            return None
        if meta.synthetic:
            # Helm browser rows etc. are read-only views over other objects.
            self.notify(f"{meta.kind} is a read-only view", severity="warning")
            return None
        ns, name = self._selected_ns_name()
        if name is None:
            return None
        namespace = ns if meta.namespaced and ns else None
        return meta, namespace, name, self._selected_uid(namespace, name)

    def _write_context_intact(
        self,
        action: str,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        phase: str = "the permission check",
    ) -> bool:
        """Re-validate after an awaited gap (the RBAC round-trip, or an
        editor session - named by ``phase`` so cancellation messages state
        the true cause), before pushing a dialog: the user may have opened
        another screen or moved the selection meanwhile - and keystrokes
        typed during the await must never land on a confirmation they did
        not see. Abort (with a notification) unless the base screen is still
        on top and the same row is still selected."""
        if len(self.screen_stack) > 1:
            self.notify(
                f"{action} {self._gvr_label(meta)}/{name} cancelled -"
                f" another dialog opened during {phase}",
                severity="warning",
            )
            return False
        kind = self._canonical_kind(self.current_kind)
        current_ns, current_name = self._selected_ns_name()
        if (
            # Value comparison, not identity: background discovery replaces
            # alias values with freshly constructed (equal) ResourceMeta
            # instances, which must not cancel a write on the same row -
            # the editor round-trip in particular is arbitrarily long.
            self.aliases.get(kind) != meta
            or current_name != name
            or (meta.namespaced and (current_ns or None) != ns)
        ):
            self.notify(
                f"{action} {self._gvr_label(meta)}/{name} cancelled -"
                f" the selection changed during {phase}",
                severity="warning",
            )
            return False
        return True

    async def _precheck_keybinding_write(
        self, action: str, meta: ResourceMeta, ns: str | None, name: str
    ) -> bool:
        """RBAC pre-check plus post-await re-validation for binding handlers:
        the check is an API round trip, so confirm the screen and selection
        are unchanged before any dialog is pushed."""
        if not await self._permitted(action, meta, ns, name):
            return False
        return self._write_context_intact(action, meta, ns, name)

    async def _permitted(
        self, action: str, meta: ResourceMeta, namespace: str | None, name: str
    ) -> bool:
        """SubjectAccessReview pre-check at the approval stage (spec §5 #5):
        surface 'missing permission' before the dialog instead of after a
        failed mutation. No checker injected -> allowed (still gated+audited)."""
        if self._check_permission is None:
            return True
        verb, subresource = self._WRITE_VERBS[action]
        try:
            allowed = await asyncio.wait_for(
                self._check_permission(verb, meta.plural, subresource, namespace, meta.group, name),
                timeout=_PERMISSION_CHECK_TIMEOUT,
            )
        except Exception:
            # Fail-open, but visibly: warn once so a persistently failing
            # checker (e.g. SSAR forbidden) does not disable the gate silently.
            if self._permission_check_warned:
                logger.debug("permission pre-check failed; allowing", exc_info=True)
            else:
                self._permission_check_warned = True
                logger.warning(
                    "permission pre-check failed; allowing (fail-open) -"
                    " writes remain approval-gated and audited",
                    exc_info=True,
                )
            return True
        if not allowed:
            _, target = self._write_perm_target(action, meta)
            self.notify(f"missing permission: {verb} {target}", severity="error")
        return allowed

    async def _audit_write(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        detail: str,
        outcome: str,
    ) -> None:
        """Append one audit record; raises if it cannot be persisted (the
        caller decides whether that blocks the write - see _run_write)."""
        if self._audit is None:
            raise RuntimeError("audit log not configured")
        audit = self._audit
        await asyncio.to_thread(
            lambda: audit.append(
                action=action,
                kind=meta.plural,
                group=meta.group,
                version=meta.version,
                namespace=namespace,
                name=name,
                detail=detail,
                outcome=outcome,
            )
        )

    async def _run_write(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        op: Awaitable[None],
        detail: str = "",
    ) -> str:
        """Execute an approved write with fail-closed auditing (AGENTS.md):
        the intent record must persist *before* the mutation - if it cannot,
        the write is blocked. Returns a short outcome string ('done' /
        'blocked: ...' / 'failed: ...') for callers that report back."""
        kind = meta.plural
        try:
            await self._audit_write(action, meta, namespace, name, detail, "intent")
        except Exception as exc:
            close = getattr(op, "close", None)
            if callable(close):
                close()  # avoid "coroutine was never awaited" for the blocked op
            # The exception can embed the local audit path (home directory):
            # log it here, but keep the notification and the tool result -
            # which is sent to the LLM provider - free of filesystem details.
            logger.exception("audit intent record failed; write blocked: %s", exc)
            self.notify(
                f"{action} {kind}/{name} blocked: audit log unavailable",
                severity="error",
            )
            return "blocked: audit log unavailable"
        try:
            await op
        except ApiStatusError as exc:
            with contextlib.suppress(Exception):
                await self._audit_write(action, meta, namespace, name, detail, f"error: {exc}")
            if exc.status == 403:
                # The SSAR pre-check fails open and permissions can change
                # mid-flight: keep the actionable RBAC message contract
                # instead of a bare "API 403: Forbidden".
                verb, target = self._write_perm_target(action, meta)
                message = f"missing permission: {verb} {target}"
            elif exc.status == 409:
                # The uid precondition tripped: the object was deleted and
                # recreated (or otherwise changed) after the approval was
                # given - nothing was modified.
                message = "conflict: the target changed since it was approved - refresh and retry"
            else:
                message = str(exc)
            self.notify(f"{action} {kind}/{name} failed: {message}", severity="error")
            return f"failed: {message}"
        except Exception as exc:
            with contextlib.suppress(Exception):
                await self._audit_write(action, meta, namespace, name, detail, f"error: {exc}")
            self.notify(f"{action} {kind}/{name} failed: {exc}", severity="error")
            return f"failed: {exc}"
        try:
            await self._audit_write(action, meta, namespace, name, detail, "success")
        except Exception:
            logger.exception("audit outcome record failed after successful write")
            self.notify("Audit log write failed (operation already executed)", severity="warning")
        self.notify(f"{action} {kind}/{name}: done", severity="information")
        return "done"

    async def _dry_run_preview(self, coro: Awaitable[list[str] | None]) -> list[str] | None:
        """Await a WriteOps preview with a hard deadline; None on timeout or
        any error (the dialog then opens without a preview, exactly as before
        issue #19 - a preview must never block or break the approval flow)."""
        try:
            return await asyncio.wait_for(coro, _PREVIEW_TIMEOUT)
        except Exception:
            logger.debug("dry-run preview failed; dialog opens without it", exc_info=True)
            return None

    async def action_delete_resource(self) -> None:
        """Ctrl-D: delete the selected resource behind a layered confirmation
        (cluster-scoped kinds require typing the resource name)."""
        ops = self._write_ops
        if ops is None:
            self.notify("Delete unavailable in this session", severity="warning")
            return
        target = self._write_target()
        if target is None:
            return
        meta, ns, name, uid = target
        if not await self._precheck_keybinding_write("delete", meta, ns, name):
            return
        preview = await self._dry_run_preview(ops.preview_delete(meta, ns, name, uid=uid))
        if not self._write_context_intact("delete", meta, ns, name, phase="the dry-run preview"):
            return
        operation = f"DELETE {self._gvr_label(meta)}/{name}{self._write_locus(ns)}"
        require = None if meta.namespaced else name

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(
                    self._run_write(
                        "delete", meta, ns, name, ops.delete_object(meta, ns, name, uid=uid)
                    )
                )

        await self.push_screen(
            ConfirmScreen(
                f"Delete {self._gvr_label(meta)}/{name}?",
                operation,
                require_name=require,
                preview=preview,
            ),
            _done,
        )

    async def action_rollout_restart(self) -> None:
        """r: rolling restart of the selected deployment/statefulset/daemonset."""
        ops = self._write_ops
        if ops is None:
            self.notify("Rollout restart unavailable in this session", severity="warning")
            return
        target = self._write_target()
        if target is None:
            return
        meta, ns, name, uid = target
        if (meta.group, meta.plural) not in self._RESTARTABLE:
            self.notify(
                f"rollout restart does not apply to {self._gvr_label(meta)}", severity="warning"
            )
            return
        if not await self._precheck_keybinding_write("rollout_restart", meta, ns, name):
            return
        # One stamp per approval: the previewed request and the executed
        # write are byte-identical (exact-replay guarantee).
        stamp = restart_stamp()
        preview = await self._dry_run_preview(
            ops.preview_rollout_restart(meta, ns, name, uid=uid, restarted_at=stamp)
        )
        if not self._write_context_intact(
            "rollout_restart", meta, ns, name, phase="the dry-run preview"
        ):
            return

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(
                    self._run_write(
                        "rollout_restart",
                        meta,
                        ns,
                        name,
                        ops.rollout_restart_with_stamp(meta, ns, name, uid=uid, restarted_at=stamp),
                    )
                )

        await self.push_screen(
            ConfirmScreen(
                f"Rollout restart {self._gvr_label(meta)}/{name}?",
                f"PATCH {self._gvr_label(meta)}/{name} pod template (restartedAt annotation)"
                f"{self._write_locus(ns)}",
                preview=preview,
            ),
            _done,
        )

    async def _fetch_manifest_for_edit(
        self, label: str, meta: ResourceMeta, ns: str | None, name: str
    ) -> dict[str, Any] | None:
        """Fetch the manifest for an edit; None (with a notification) aborts.
        The fetch is another awaited round-trip: a selection change while it
        was in flight must abort before the editor opens for a stale target,
        not merely discard the completed edit afterwards."""
        if self._get_manifest is None:
            return None
        try:
            manifest = await self._get_manifest(self._canonical_kind(self.current_kind), ns, name)
        except Exception as exc:
            self.notify(f"edit {label} failed: {exc}", severity="error")
            return None
        if not self._write_context_intact("edit", meta, ns, name, phase="the manifest fetch"):
            return None
        # managedFields is server-side bookkeeping noise; kubectl edit hides
        # it too. resourceVersion stays so concurrent modifications 409.
        metadata = manifest.get("metadata")
        if isinstance(metadata, dict):
            metadata.pop("managedFields", None)
        return manifest

    async def action_edit_resource(self) -> None:
        """e: open the selected resource's manifest in $EDITOR and PUT the
        edited version back (kubectl edit parity)."""
        ops = self._write_ops
        if ops is None or self._get_manifest is None:
            self.notify("Edit unavailable in this session", severity="warning")
            return
        target = self._write_target()
        if target is None:
            return
        meta, ns, name, uid = target
        if not await self._precheck_keybinding_write("edit", meta, ns, name):
            return
        label = f"{self._gvr_label(meta)}/{name}"
        manifest = await self._fetch_manifest_for_edit(label, meta, ns, name)
        if manifest is None:
            return
        original_text = yaml.safe_dump(manifest, sort_keys=False)
        edit = self._edit_text or self._edit_in_external_editor
        edited = self._parse_edited_manifest(
            label, manifest, original_text, await edit(original_text)
        )
        if edited is None:
            return
        # The editor round-trip is arbitrarily long: re-validate that the
        # same row is still selected before pushing the confirmation.
        if not self._write_context_intact("edit", meta, ns, name, phase="the editor session"):
            return
        detail = self._edit_detail(manifest, edited)

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(
                    self._run_write(
                        "edit",
                        meta,
                        ns,
                        name,
                        ops.replace_object(meta, ns, name, edited, uid=uid),
                        detail=detail,
                    )
                )

        await self.push_screen(
            ConfirmScreen(
                f"Apply edited {label}?",
                # Issue #21: the approval dialog summarizes the change, not
                # just the target and verb.
                f"PUT {label}{self._write_locus(ns)} - {detail}",
            ),
            _done,
        )

    def _parse_edited_manifest(
        self,
        label: str,
        original: dict[str, Any],
        original_text: str,
        edited_text: str | None,
    ) -> dict[str, Any] | None:
        """Validate the editor output; None (with a notification) aborts the
        edit. Re-injects the fetched resourceVersion if the user deleted it -
        an unversioned PUT would silently clobber concurrent changes."""
        if edited_text is None:
            self.notify(f"edit {label} cancelled", severity="warning")
            return None
        if edited_text == original_text:
            self.notify(f"edit {label}: no changes", severity="information")
            return None
        try:
            parsed = yaml.safe_load(edited_text)
        except yaml.YAMLError as exc:
            self.notify(f"edit {label} aborted: invalid YAML: {exc}", severity="error")
            return None
        if not isinstance(parsed, dict):
            self.notify(f"edit {label} aborted: not a mapping", severity="error")
            return None
        if any(not isinstance(key, str) for key in parsed):
            # YAML legally allows non-string mapping keys, but a manifest
            # never has them and the change summary sorts keys together.
            self.notify(f"edit {label} aborted: non-string top-level key", severity="error")
            return None
        # Restore the fetched resourceVersion *before* the semantic no-op
        # comparison: an edit that only deleted it is still "no changes",
        # and `metadata: null` must not defeat the restore - an unversioned
        # PUT would silently clobber concurrent changes.
        original_meta = original.get("metadata")
        rv = original_meta.get("resourceVersion") if isinstance(original_meta, dict) else None
        if rv is not None:
            parsed_meta = parsed.get("metadata")
            if not isinstance(parsed_meta, dict):
                parsed_meta = {}
                parsed["metadata"] = parsed_meta
            # Not setdefault: a blank `resourceVersion:` loads as None - the
            # key is present but the PUT would still be unversioned.
            edited_rv = parsed_meta.get("resourceVersion")
            if not (isinstance(edited_rv, str) and edited_rv):
                parsed_meta["resourceVersion"] = rv
        if _yaml_equal(parsed, original):
            self.notify(f"edit {label}: no changes", severity="information")
            return None
        return parsed

    @staticmethod
    def _edit_detail(original: dict[str, Any], edited: dict[str, Any]) -> str:
        """Audit detail: which top-level sections changed. Key presence is
        checked separately (dict.get returns None for both an absent key and
        a present null key) and values compare YAML-canonically."""
        changed = sorted(
            key
            for key in set(original) | set(edited)
            if (key in original) != (key in edited)
            or not _yaml_equal(original.get(key), edited.get(key))
        )
        return "changed: " + ", ".join(changed)

    async def _edit_in_external_editor(self, text: str) -> str | None:
        """Suspend the TUI and open $VISUAL/$EDITOR (vi fallback) on a temp
        file; None cancels. Invocation and I/O failures (missing executable,
        malformed quoting, temp-dir exhaustion, undecodable editor output)
        abort with a notification instead of an unhandled action error. The
        blocking call runs in a thread so background tasks keep running
        while the editor is open."""
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        try:
            fd, tmp = tempfile.mkstemp(suffix=".yaml", prefix="korvid-edit-")
        except OSError as exc:
            self.notify(f"edit temp file failed: {exc}", severity="error")
            return None
        try:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                argv = shlex.split(editor)
                if not argv:
                    # A whitespace-only $VISUAL/$EDITOR passes the fallback
                    # expression but yields no executable to run.
                    raise ValueError("empty editor command")
                argv.append(tmp)
                with self.suspend():
                    code = await asyncio.to_thread(subprocess.call, argv)
            except SuspendNotSupported:
                # Windows and other non-suspending drivers: cancel with a
                # notification instead of an unhandled action error.
                self.notify(
                    "edit unavailable: this environment does not support"
                    " suspending the TUI for an external editor",
                    severity="error",
                )
                return None
            except (OSError, ValueError) as exc:
                self.notify(f"editor {editor!r} failed: {exc}", severity="error")
                return None
            self.refresh()
            if code != 0:
                return None
            try:
                # Explicit UTF-8: a locale mismatch or binary editor output
                # raises UnicodeDecodeError (a ValueError, not an OSError).
                return Path(tmp).read_text(encoding="utf-8")
            except (OSError, ValueError) as exc:
                self.notify(f"editor result unreadable: {exc}", severity="error")
                return None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    def _current_replicas(self, ns: str | None, name: str) -> int | None:
        """Desired replicas of the selected row, or None when the summary type
        does not carry it (0 would be indistinguishable from scaled-to-zero)."""
        for obj in self.store.get(self.current_kind, self.current_scope):
            if obj.namespace == (ns or "") and obj.name == name:
                desired = getattr(obj, "desired", None)
                return None if desired is None else int(desired)
        return None

    async def action_scale_resource(self) -> None:
        """S: scale the selected deployment/replicaset/statefulset (prompt, then confirm)."""
        ops = self._write_ops
        if ops is None:
            self.notify("Scale unavailable in this session", severity="warning")
            return
        target = self._write_target()
        if target is None:
            return
        meta, ns, name, uid = target
        if (meta.group, meta.plural) not in self._SCALABLE:
            self.notify(f"scale does not apply to {self._gvr_label(meta)}", severity="warning")
            return
        if not await self._precheck_keybinding_write("scale", meta, ns, name):
            return
        current = self._current_replicas(ns, name)

        def _on_replicas(replicas: int | None) -> None:
            if replicas is None:
                return
            # The dry-run round trip must not run inside a screen callback:
            # a worker fetches the preview, revalidates, then confirms.
            self.run_worker(self._confirm_scale(meta, ns, name, uid, current, replicas))

        await self.push_screen(
            ReplicasPrompt(f"{self._gvr_label(meta)}/{name}", current=current), _on_replicas
        )

    async def _confirm_scale(
        self,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        current: int | None,
        replicas: int,
    ) -> None:
        """Dry-run preview + approval dialog for a scale, after the replica
        count is known. Revalidates the selection after the preview round
        trip: keystrokes during the await must never land on a confirmation
        for a different row."""
        ops = self._write_ops
        if ops is None:
            return
        preview = await self._dry_run_preview(ops.preview_scale(meta, ns, name, replicas, uid=uid))
        if not self._write_context_intact("scale", meta, ns, name, phase="the dry-run preview"):
            return

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(
                    self._run_write(
                        "scale",
                        meta,
                        ns,
                        name,
                        ops.scale_object(meta, ns, name, replicas, uid=uid),
                        detail=f"replicas -> {replicas}",
                    )
                )

        shown = "?" if current is None else current
        await self.push_screen(
            ConfirmScreen(
                f"Scale {self._gvr_label(meta)}/{name}?",
                f"PATCH {self._gvr_label(meta)}/{name}/scale: replicas {shown} -> {replicas}"
                f"{self._write_locus(ns)}",
                preview=preview,
            ),
            _done,
        )

    async def action_resize_pod(self) -> None:
        """R: in-place resize of the selected pod (prompt, then confirm).

        Only offered on the pods view and only when discovery found the
        pods/resize subresource (Kubernetes 1.35 GA)."""
        ops = self._write_ops
        if ops is None:
            self.notify("Resize unavailable in this session", severity="warning")
            return
        target = self._write_target()
        if target is None:
            return
        meta, ns, name, uid = target
        if (meta.group, meta.plural) != ("", "pods"):
            self.notify(f"resize does not apply to {self._gvr_label(meta)}", severity="warning")
            return
        if not self._pod_resize_supported:
            self.notify(
                "This cluster does not expose pods/resize (requires Kubernetes 1.35+)",
                severity="warning",
            )
            return
        if not await self._precheck_keybinding_write("resize", meta, ns, name):
            return
        containers = await self._pod_container_resources(ns, name)
        if containers is None:
            return
        if not self._write_context_intact("resize", meta, ns, name, phase="the manifest fetch"):
            return

        def _on_resources(resources: dict[str, dict[str, dict[str, str]]] | None) -> None:
            if not resources:
                return
            # The dry-run round trip must not run inside a screen callback:
            # a worker fetches the preview, revalidates, then confirms.
            self.run_worker(self._confirm_resize(meta, ns, name, uid, resources))

        await self.push_screen(
            ResizePrompt(f"{self._gvr_label(meta)}/{name}", containers=containers), _on_resources
        )

    async def _pod_container_resources(
        self, ns: str | None, name: str
    ) -> list[tuple[str, dict[str, dict[str, str]]]] | None:
        """Current per-container requests/limits from the live manifest, in
        spec order, to prefill the resize prompt; None (with a notification)
        when the manifest cannot be fetched."""
        if self._get_manifest is None:
            self.notify("Resize unavailable: no manifest source", severity="warning")
            return None
        try:
            manifest = await self._get_manifest("pods", ns, name)
        except Exception as exc:
            self.notify(f"Could not fetch pod manifest: {exc}", severity="error")
            return None
        containers: list[tuple[str, dict[str, dict[str, str]]]] = []
        for spec in manifest.get("spec", {}).get("containers", []):
            resources = {
                section: dict(values)
                for section, values in spec.get("resources", {}).items()
                if section in ("requests", "limits") and isinstance(values, dict)
            }
            containers.append((str(spec.get("name", "")), resources))
        if not containers:
            self.notify("Pod manifest lists no containers", severity="warning")
            return None
        return containers

    @staticmethod
    def _resize_summary(resources: dict[str, dict[str, dict[str, str]]]) -> str:
        """One-line 'app: requests.cpu=200m, limits.memory=1Gi; ...' summary
        shown in the approval dialog and recorded in the audit detail."""
        parts = []
        for container, sections in resources.items():
            changes = ", ".join(
                f"{section}.{quantity}={value}"
                for section, values in sections.items()
                for quantity, value in values.items()
            )
            parts.append(f"{container}: {changes}")
        return "; ".join(parts)

    async def _confirm_resize(
        self,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        resources: dict[str, dict[str, dict[str, str]]],
    ) -> None:
        """Dry-run preview + approval dialog for an in-place pod resize.
        Revalidates the selection after the preview round trip: keystrokes
        during the await must never land on a confirmation for a different
        row."""
        ops = self._write_ops
        if ops is None:
            return
        namespace = ns or ""
        preview = await self._dry_run_preview(
            ops.preview_resize(namespace, name, resources, uid=uid)
        )
        if not self._write_context_intact("resize", meta, ns, name, phase="the dry-run preview"):
            return
        summary = self._resize_summary(resources)

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(
                    self._run_write(
                        "resize",
                        meta,
                        ns,
                        name,
                        ops.resize_pod(namespace, name, resources, uid=uid),
                        detail=summary,
                    )
                )

        await self.push_screen(
            ConfirmScreen(
                f"Resize pods/{name}?",
                f"PATCH pods/{name}/resize: {summary}{self._write_locus(ns)}",
                preview=preview,
            ),
            _done,
        )

    def _node_target(self, action: str) -> tuple[WriteOps, ResourceMeta, str, str | None] | None:
        """Resolve the selected node for a node op, or None (with a
        notification) when writes are disabled, nothing is selected, or the
        current view is not the nodes view."""
        ops = self._write_ops
        if ops is None:
            self.notify(f"{action} unavailable in this session", severity="warning")
            return None
        target = self._write_target()
        if target is None:
            return None
        meta, _, name, uid = target
        if (meta.group, meta.plural) != ("", "nodes"):
            self.notify(f"{action} does not apply to {self._gvr_label(meta)}", severity="warning")
            return None
        return ops, meta, name, uid

    async def action_cordon_node(self) -> None:
        """c: mark the selected node unschedulable (kubectl cordon parity)."""
        await self._cordon_action(unschedulable=True)

    async def action_uncordon_node(self) -> None:
        """u: mark the selected node schedulable again (kubectl uncordon)."""
        await self._cordon_action(unschedulable=False)

    async def _cordon_action(self, *, unschedulable: bool) -> None:
        """Shared cordon/uncordon flow: SSAR pre-check, dry-run preview,
        approval dialog, audited write (issue #40)."""
        action = "cordon" if unschedulable else "uncordon"
        resolved = self._node_target(action)
        if resolved is None:
            return
        ops, meta, name, uid = resolved
        worker = self._drain_worker
        if worker is not None and worker.is_running and name == self._drain_node:
            # Uncordoning (or re-cordoning) mid-drain would let new pods
            # schedule behind the drain's back; the drain owns the node's
            # schedulable state until it finishes or is cancelled.
            self.notify(
                f"nodes/{name} is being drained - cancel the drain first",
                severity="warning",
            )
            return
        if not await self._precheck_keybinding_write(action, meta, None, name):
            return
        preview = await self._dry_run_preview(ops.preview_cordon(name, unschedulable, uid=uid))
        if not self._write_context_intact(action, meta, None, name, phase="the dry-run preview"):
            return
        flag = "true" if unschedulable else "false"

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(
                    self._run_write(
                        action,
                        meta,
                        None,
                        name,
                        ops.cordon_node(name, unschedulable, uid=uid),
                        detail=f"spec.unschedulable={flag}",
                    )
                )

        await self.push_screen(
            ConfirmScreen(
                f"{action.capitalize()} nodes/{name}?",
                f"PATCH nodes/{name} spec.unschedulable={flag}",
                preview=preview,
            ),
            _done,
        )

    async def action_drain_node(self) -> None:
        """shift+d: drain the selected node behind a typed-name approval
        showing the PDB-aware impact plan (issue #40). Pressing the key
        again while a drain is running cancels it: no further evictions are
        issued and the node stays cordoned."""
        worker = self._drain_worker
        if worker is not None and worker.is_running:
            _, selected = self._selected_ns_name()
            kind_meta = self.aliases.get(self._canonical_kind(self.current_kind))
            on_nodes = kind_meta is not None and (kind_meta.group, kind_meta.plural) == (
                "",
                "nodes",
            )
            if self._drain_node is not None and (not on_nodes or selected != self._drain_node):
                # Cancelling is a targeted act: another node (or a same-named
                # row in another view - the binding is global) being selected
                # must not silently kill the running drain (issue #40 review).
                self.notify(
                    f"drain of nodes/{self._drain_node} in progress"
                    " - press the drain key on it to cancel",
                    severity="warning",
                )
                return
            worker.cancel()
            return
        resolved = self._node_target("drain")
        if resolved is None:
            return
        ops, meta, name, uid = resolved
        if not await self._precheck_keybinding_write("drain", meta, None, name):
            return
        try:
            plan = await ops.drain_plan(name)
        except Exception as exc:
            self.notify(
                f"drain nodes/{name} aborted: could not compute the impact plan: {exc}",
                severity="error",
            )
            return
        if not self._write_context_intact("drain", meta, None, name, phase="the drain plan"):
            return

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self._drain_node = name
                self._drain_worker = self.run_worker(self._run_drain(ops, meta, name, uid, plan))

        blocked_now = sum(1 for t in plan.targets if t.pdb_blocked is not None)
        note = f"; {blocked_now} currently PDB-blocked" if blocked_now else ""
        await self.push_screen(
            ConfirmScreen(
                f"Drain nodes/{name}?",
                f"Cordon nodes/{name}, then attempt eviction of {len(plan.targets)} pods"
                f" via the Eviction API{note}"
                " (press the drain key again to cancel mid-drain)",
                require_name=name,
                preview=plan.preview_lines(),
                preview_title="drain impact plan:",
            ),
            _done,
        )

    def _set_drain_progress(self, label: str) -> None:
        """Publish live drain progress on the status bar (issue #40); a
        failure to render must never interrupt the drain itself."""
        self._drain_progress = label
        with contextlib.suppress(Exception):
            self._refresh_status()

    async def _evict_one(self, ops: WriteOps, target: DrainTarget) -> str:
        """Issue one eviction; returns 'evicted', 'blocked' (PDB refused,
        429 - warn live and move on instead of hanging on a budget that may
        never free) or 'failed'. Cancellation propagates."""
        try:
            await ops.evict_pod(target.namespace, target.name, uid=target.uid)
        except asyncio.CancelledError:
            raise
        except ApiStatusError as exc:
            if exc.status == 429:
                self.notify(
                    f"eviction refused by PodDisruptionBudget: {target.ref}",
                    severity="warning",
                )
                return "blocked"
            self.notify(f"eviction failed: {target.ref}: {exc}", severity="error")
            return "failed"
        except Exception as exc:
            self.notify(f"eviction failed: {target.ref}: {exc}", severity="error")
            return "failed"
        return "evicted"

    async def _recheck_drain_plan(
        self,
        ops: WriteOps,
        meta: ResourceMeta,
        name: str,
        plan: DrainPlan,
    ) -> tuple[DrainTarget, ...] | None:
        """Re-list the node's pods right after cordoning: the node was still
        schedulable while the user reviewed the plan, so pods may have landed
        since. Pods that disappeared are simply dropped; pods the user never
        approved abort the drain (the node stays cordoned, so a re-run shows
        a stable plan to re-approve). None means the drain must not proceed
        (already audited and notified)."""
        try:
            fresh = await ops.drain_plan(name)
        except Exception as exc:
            summary = f"plan re-check failed after cordon: {exc}"
            with contextlib.suppress(Exception):
                await self._audit_write("drain", meta, None, name, summary, "aborted")
            self.notify(
                f"drain nodes/{name} aborted: {summary}; node remains cordoned",
                severity="error",
            )
            return None
        approved = {t.uid or t.ref for t in plan.targets}
        unapproved = [t.ref for t in fresh.targets if (t.uid or t.ref) not in approved]
        if unapproved:
            summary = (
                f"plan changed after cordon: {len(unapproved)} unapproved pods"
                f" ({', '.join(unapproved[:3])})"
            )
            with contextlib.suppress(Exception):
                await self._audit_write("drain", meta, None, name, summary, "aborted")
            self.notify(
                f"drain nodes/{name} aborted: {summary};"
                " node remains cordoned - re-run drain to review the updated plan",
                severity="warning",
            )
            return None
        return fresh.targets

    async def _run_drain(
        self,
        ops: WriteOps,
        meta: ResourceMeta,
        name: str,
        uid: str | None,
        plan: DrainPlan,
    ) -> None:
        """Execute an approved drain: cordon, re-check the plan (pods may
        have landed while the dialog was open), evict pod by pod, then wait
        (bounded) for the accepted evictions to actually terminate.
        Auditing is fail-closed (no intent record, no drain); a PDB-refused
        eviction (429) surfaces as a live warning and the drain moves on;
        cancellation anywhere after the intent record is audited, stops
        issuing evictions, and never uncordons the node."""
        total = len(plan.targets)
        counts = {"evicted": 0, "blocked": 0, "failed": 0, "still": 0}
        try:
            try:
                await self._audit_write(
                    "drain", meta, None, name, f"planned evictions: {total}", "intent"
                )
            except Exception as exc:
                logger.exception("audit intent record failed; drain blocked: %s", exc)
                self.notify(f"drain nodes/{name} blocked: audit log unavailable", severity="error")
                return
            try:
                try:
                    await ops.cordon_node(name, True, uid=uid)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        await self._audit_write(
                            "drain", meta, None, name, "cordon step failed", f"error: {exc}"
                        )
                    self.notify(f"drain nodes/{name} failed: cordon: {exc}", severity="error")
                    return
                targets = await self._recheck_drain_plan(ops, meta, name, plan)
                if targets is None:
                    return
                total = len(targets)
                self.notify(
                    f"drain nodes/{name}: cordoned; evicting {total} pods"
                    " (press the drain key again to cancel)",
                    severity="information",
                )
                await self._evict_targets(ops, name, targets, counts)
            except asyncio.CancelledError:
                summary = (
                    f"cancelled: evicted {counts['evicted']} of {total}; node was not uncordoned"
                )
                with contextlib.suppress(Exception):
                    await self._audit_write("drain", meta, None, name, summary, "cancelled")
                self.notify(f"drain nodes/{name} {summary}", severity="warning")
                raise
            summary = (
                f"evicted {counts['evicted']}, pdb-blocked {counts['blocked']},"
                f" failed {counts['failed']} of {total}"
            )
            if counts["still"]:
                summary += f"; {counts['still']} evicted pods not yet terminated"
            clean = counts["blocked"] == 0 and counts["failed"] == 0 and counts["still"] == 0
            outcome = "success" if clean else f"partial: {summary}"
            try:
                await self._audit_write("drain", meta, None, name, summary, outcome)
            except Exception:
                logger.exception("audit outcome record failed after drain")
                self.notify("Audit log write failed (drain already executed)", severity="warning")
            if clean:
                self.notify(f"drain nodes/{name}: {summary}")
            else:
                self.notify(f"drain nodes/{name}: {summary}", severity="warning")
        finally:
            # Last: while the worker is finalizing (outcome audit/notify)
            # the targeted-cancel guard must still see its node.
            self._drain_node = None

    async def _evict_targets(
        self,
        ops: WriteOps,
        name: str,
        targets: tuple[DrainTarget, ...],
        counts: dict[str, int],
    ) -> None:
        """Evict *targets* one by one with live progress, then wait for the
        accepted evictions' pods to leave the node. Mutates *counts* in
        place so a cancellation mid-loop still leaves accurate numbers for
        the caller's audit record."""
        total = len(targets)
        accepted: list[DrainTarget] = []
        try:
            for done, target in enumerate(targets, start=1):
                self._set_drain_progress(f"drain {name}: {done - 1}/{total}")
                result = await self._evict_one(ops, target)
                if result == "evicted":
                    accepted.append(target)
                counts[result] += 1
                self._set_drain_progress(f"drain {name}: {done}/{total}")
            if accepted:
                keys = {t.uid or t.ref for t in accepted}
                counts["still"] = await self._await_evictions(ops, name, keys)
        finally:
            self._set_drain_progress("")

    async def _await_evictions(self, ops: WriteOps, name: str, keys: set[str]) -> int:
        """A 201 from the Eviction API only *starts* graceful deletion: the
        pod can linger for its grace period, a finalizer, or an unreachable
        kubelet. Poll the node's pod list until the accepted pods are gone
        (bounded) so progress and the audit outcome reflect reality; returns
        how many are still present at the deadline (audited as partial)."""
        deadline = monotonic() + self._drain_wait_timeout
        while True:
            try:
                current = await ops.drain_plan(name)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("post-eviction termination poll failed", exc_info=True)
                return len(keys)
            keys &= {t.uid or t.ref for t in current.targets}
            if not keys:
                return 0
            if monotonic() >= deadline:
                return len(keys)
            self._set_drain_progress(f"drain {name}: waiting for {len(keys)} pods to terminate")
            await asyncio.sleep(self._drain_wait_poll)

    async def action_operator_install(self) -> None:
        """I: on the operator catalog, install the selected package (wizard,
        then approval with the full Subscription manifest); on InstallPlans,
        approve a pending manual plan. Everything offered comes from the
        cluster's own catalog objects - no hardcoded operator knowledge."""
        if self._write_ops is None:
            self.notify("Install unavailable in this session", severity="warning")
            return
        target = self._write_target()
        if target is None:
            return
        meta, ns, name, uid = target
        if (meta.group, meta.plural) == (PACKAGES_GROUP, "packagemanifests"):
            await self._start_operator_install(meta, ns, name, uid)
        elif (meta.group, meta.plural) == (OPERATORS_GROUP, "installplans"):
            await self._start_installplan_approve(meta, ns, name, uid)
        else:
            self.notify(
                f"Install/Approve does not apply to {self._gvr_label(meta)}"
                " (use it on packagemanifests or installplans)",
                severity="warning",
            )

    async def _start_operator_install(
        self, pkg_meta: ResourceMeta, ns: str | None, name: str, uid: str | None
    ) -> None:
        """Fetch the PackageManifest and open the install wizard."""
        sub_meta = resolve_olm_meta(self.aliases, "subscriptions", OPERATORS_GROUP)
        if sub_meta is None:
            self.notify(
                "Install unavailable: the OLM Subscription API was not discovered",
                severity="warning",
            )
            return
        if self._get_manifest is None:
            self.notify("Install unavailable: no manifest source", severity="warning")
            return
        try:
            # Fetch by the canonical view kind (which may be a group-qualified
            # alias), as the edit path does: a bare plural would resolve to a
            # colliding foreign CRD's meta when names overlap.
            manifest = await self._get_manifest(self._canonical_kind(self.current_kind), ns, name)
        except Exception as exc:
            self.notify(f"Could not fetch the package manifest: {exc}", severity="error")
            return
        # The wizard must be fed the incarnation the user selected: if the
        # catalog entry was deleted and recreated under the same name during
        # the fetch, its facts (channels, catalog source) may differ.
        if not self._uid_intact_after_fetch(manifest, ns, name, uid):
            self.notify(
                f"install {self._gvr_label(pkg_meta)}/{name} cancelled -"
                " the catalog entry changed during the manifest fetch",
                severity="warning",
            )
            return
        facts = package_install_facts(manifest)
        if not self._write_context_intact(
            "install", pkg_meta, ns, name, phase="the manifest fetch"
        ):
            return

        def _on_choices(choices: tuple[str, str, str] | None) -> None:
            if choices is None:
                return
            # The SSAR round trip must not run inside a screen callback:
            # a worker re-checks, revalidates, then confirms.
            self.run_worker(
                self._confirm_operator_install(pkg_meta, sub_meta, ns, uid, facts, choices)
            )

        # The row namespace is where the catalog lives (e.g. "olm"), not
        # where the user works: prefill the wizard with the active view
        # namespace, or the configured workload namespace on the
        # all-namespaces view (the catalog default since `:operators`
        # opens cluster-wide).
        view_ns = self.current_namespace
        # Same fallback as current_scope's initialization: with zero config,
        # config.namespace is None while the effective workload namespace is
        # "default" - an empty prefill would fail validation on submit.
        default_ns = view_ns if view_ns != ALL_NAMESPACES else (self.config.namespace or "default")
        await self.push_screen(
            OperatorInstallPrompt(facts, namespace=default_ns),
            _on_choices,
        )

    async def _confirm_operator_install(
        self,
        pkg_meta: ResourceMeta,
        sub_meta: ResourceMeta,
        ns: str | None,
        uid: str | None,
        facts: PackageInstallFacts,
        choices: tuple[str, str, str],
    ) -> None:
        """Approval dialog for an operator install: the full Subscription
        manifest is shown before it is created (issue #29 requirement)."""
        ops = self._write_ops
        if ops is None:
            return
        namespace, channel, approval = choices
        try:
            manifest = build_subscription(
                package=facts.package,
                namespace=namespace,
                channel=channel,
                source=facts.catalog_source,
                source_namespace=facts.catalog_source_namespace,
                approval=approval,
            )
        except ValueError as exc:
            # Blank catalog facts (malformed PackageManifest status) land
            # here; the wizard already validated its own inputs.
            self.notify(f"install cancelled: {exc}", severity="warning")
            return
        # Create is authorized against the collection POST before the object
        # name exists (resourceNames rules cannot grant create), so the SSAR
        # must omit the name to match the real request.
        if not await self._permitted("install", sub_meta, namespace, ""):
            return
        if not self._write_context_intact(
            "install", pkg_meta, ns, facts.package, phase="the install wizard"
        ):
            return
        if uid and self._selected_uid(ns, facts.package) != uid:
            # Same name, different incarnation: the catalog entry was
            # replaced while the wizard was open.
            self.notify(
                f"install {self._gvr_label(pkg_meta)}/{facts.package} cancelled -"
                " the catalog entry changed during the install wizard",
                severity="warning",
            )
            return
        operation = (
            f"CREATE subscriptions/{facts.package} in namespace {namespace}\n"
            "note: OLM requires an OperatorGroup in the target namespace -"
            " without one the Subscription is accepted but stays pending\n\n"
            + yaml.safe_dump(manifest, sort_keys=False)
        )

        def _done(confirmed: bool | None) -> None:
            if not confirmed:
                return
            # create has no server-side uid precondition (there is no target
            # object yet): recheck the catalog incarnation one last time at
            # execution, after the confirmation gap.
            if uid and self._selected_uid(ns, facts.package) != uid:
                self.notify(
                    f"install {self._gvr_label(pkg_meta)}/{facts.package} cancelled -"
                    " the catalog entry changed during the approval dialog",
                    severity="warning",
                )
                return
            self.run_worker(
                self._run_write(
                    "install",
                    sub_meta,
                    namespace,
                    facts.package,
                    ops.create_object(sub_meta, namespace, manifest),
                    detail=f"channel={channel} approval={approval} source={facts.catalog_source}",
                )
            )

        await self.push_screen(
            ConfirmScreen(f"Install operator {facts.package}?", operation), _done
        )

    async def _start_installplan_approve(
        self, meta: ResourceMeta, ns: str | None, name: str, uid: str | None
    ) -> None:
        """Approve a pending manual InstallPlan: fetch, flip spec.approved,
        and replace behind the standard approval dialog listing the CSVs the
        approval unblocks."""
        ops = self._write_ops
        if ops is None:
            return
        if not await self._precheck_keybinding_write("approve", meta, ns, name):
            return
        if self._get_manifest is None:
            self.notify("Approve unavailable: no manifest source", severity="warning")
            return
        try:
            # Canonical view kind, not the bare plural: safe under alias
            # collisions (see _start_operator_install).
            manifest = await self._get_manifest(self._canonical_kind(self.current_kind), ns, name)
        except Exception as exc:
            self.notify(f"Could not fetch the install plan: {exc}", severity="error")
            return
        if not self._uid_intact_after_fetch(manifest, ns, name, uid):
            self.notify(
                f"approve installplans/{name} cancelled -"
                " the install plan changed during the manifest fetch",
                severity="warning",
            )
            return
        spec = self._approvable_plan_spec(manifest, name)
        if spec is None:
            return
        if not self._write_context_intact("approve", meta, ns, name, phase="the manifest fetch"):
            return
        updated = dict(manifest)
        updated["spec"] = {**spec, "approved": True}
        csvs = ", ".join(str(c) for c in spec.get("clusterServiceVersionNames") or []) or "?"
        operation = (
            f"REPLACE installplans/{name} with spec.approved=true"
            f"{self._write_locus(ns)}\ninstalls: {csvs}"
        )

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(
                    self._run_write(
                        "approve",
                        meta,
                        ns,
                        name,
                        ops.replace_object(meta, ns, name, updated, uid=uid),
                        detail=f"installs: {csvs}",
                    )
                )

        await self.push_screen(ConfirmScreen(f"Approve installplans/{name}?", operation), _done)

    def _approvable_plan_spec(self, manifest: dict[str, Any], name: str) -> dict[str, Any] | None:
        """The plan's spec if it is a pending Manual plan, else None (with
        the reason notified). An Automatic (or malformed) plan is OLM's own
        to approve; flipping it manually would race the operator."""
        spec = manifest.get("spec")
        spec = spec if isinstance(spec, dict) else {}
        approval_mode = str(spec.get("approval") or "")
        if approval_mode != "Manual":
            self.notify(
                f"installplans/{name} has approval mode"
                f" {approval_mode or '?'!r} - only pending Manual plans"
                " can be approved here",
                severity="warning",
            )
            return None
        if spec.get("approved"):
            self.notify(f"installplans/{name} is already approved", severity="information")
            return None
        return spec

    async def _open_log_pane(
        self,
        namespace: str,
        sources: list[tuple[str, str]],
        triples: list[tuple[str, str, str]] | None = None,
        force_prefix: bool = False,
        previous: bool = False,
    ) -> None:
        """Show log pane and spawn one streaming task per (pod, container)."""
        self._log_pane_gen += 1
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
        self._log_pane_gen += 1
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
        mcp_label = self._mcp.status() if self._mcp is not None else ""
        self.query_one(StatusBar).update_status(
            self.config.kube_context,
            self.current_scope,
            label,
            breadcrumb=self._drill.breadcrumb(),
            mcp_label=mcp_label,
            filter_label=self._resource_filter.describe(),
            progress_label=self._drain_progress,
        )

    # ------------------------------------------------------------------
    # Task-10 actions: JSON toggle, previous logs, search navigation
    # ------------------------------------------------------------------

    async def action_log_format(self) -> None:
        """Toggle JSON/raw formatting and re-render the buffer (``f`` key)."""
        self._toggle_log_display(LogPane.toggle_format)

    async def action_log_wrap(self) -> None:
        """Toggle line wrapping and re-render the buffer (``w`` key)."""
        self._toggle_log_display(LogPane.toggle_wrap)

    async def action_log_timestamps(self) -> None:
        """Toggle the timestamp prefix and re-render the buffer (``t`` key)."""
        self._toggle_log_display(LogPane.toggle_timestamps)

    def _toggle_log_display(self, toggle: Callable[[LogPane], None]) -> None:
        """Shared path for display toggles: flip the setting, replay the buffer.

        ``LogPane.replay`` restores contextual banners (previous-logs,
        overflow), so every toggle must funnel through here instead of
        clearing panels ad hoc.
        """
        log_pane = self.query_one(LogPane)
        if not log_pane.display:
            return
        toggle(log_pane)
        if self._log_buffer is not None:
            log_pane.replay(self._log_buffer.lines())

    def action_log_save(self) -> None:
        """Save the current log buffer to a generated file (``ctrl+s``)."""
        log_pane = self.query_one(LogPane)
        if not log_pane.display or self._log_buffer is None:
            return
        lines = self._log_buffer.lines()
        if not lines:
            self.notify("Log buffer is empty — nothing to save", severity="warning")
            return
        try:
            path = export_log_lines(lines, default_log_export_dir())
        except OSError as exc:
            self.notify(f"Failed to save logs: {exc}", severity="error")
            return
        self.notify(f"Logs saved to {path}")

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
        describe_pane = self.query_one(DescribePane)
        if describe_pane.display:
            describe_pane.search_next()
            return
        log_pane = self.query_one(LogPane)
        if log_pane.display:
            log_pane.search_next()

    def action_log_search_prev(self) -> None:
        """Previous search hit in an open pane; sort by name otherwise (``N``)."""
        describe_pane = self.query_one(DescribePane)
        if describe_pane.display:
            describe_pane.search_prev()
            return
        log_pane = self.query_one(LogPane)
        if log_pane.display:
            log_pane.search_prev()
            return
        self._toggle_sort("name")

    # ------------------------------------------------------------------
    # Column sorting (issue #37) — data-model sort keys, per-kind state.
    # ------------------------------------------------------------------

    def _toggle_sort(self, column: str) -> None:
        """Apply/flip a sort column for the current view kind and re-render."""
        kind = self.current_kind
        if column in ("cpu", "mem") and kind != "pods":
            # Only the pods view has CPU/MEM columns and a metrics feed;
            # elsewhere the keypress would silently discard the current
            # order while showing no indicator, so ignore it.
            return
        self._sorts[kind] = toggle_sort(self._sorts.get(kind), column)
        self._render_table(kind)

    def action_sort_by_age(self) -> None:
        self._toggle_sort("age")

    def action_sort_by_cpu(self) -> None:
        self._toggle_sort("cpu")

    def action_sort_by_mem(self) -> None:
        self._toggle_sort("mem")

    # ------------------------------------------------------------------
    # Agent panel (Ctrl-A) — wiring only; rendering lives in AgentPanel,
    # loop logic in AgentRuntime.
    # ------------------------------------------------------------------

    def _agent_panel_expanded(self) -> bool:
        """True when the agent chat panel is mounted and visible on screen."""
        panels = self.query(AgentPanel)
        return bool(panels) and panels.first(AgentPanel).display

    def _can_surface_approval(self) -> bool:
        """An approval dialog may only appear when the panel is expanded AND
        no other screen is stacked on top: pushing it over an active dialog
        would let the user's next y/Enter approve an unexpected write."""
        return self._agent_panel_expanded() and len(self.screen_stack) == 1

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
        if isinstance(self.screen, DescribeScreen):
            # The user opened a describe modal and is reading it; switching
            # the table underneath while reporting 'switched' would lie about
            # what's on screen. User action takes priority.
            return (
                "ERROR: a describe screen is open — the user is reading it; "
                "ask them to close it (Esc) before changing the view"
            )
        key = view.strip().lower()
        meta = self.aliases.get(key)
        if meta is None:
            return f"ERROR: unknown view {view!r} — not a resource kind in this cluster"
        if namespace and namespace.strip().lower() in ("all", ALL_NAMESPACES):
            # Same mapping as the human ':view all' command path.
            namespace = ALL_NAMESPACES
        try:
            # Canonical view kind, not the bare plural: safe under alias
            # collisions (same rule as the command-bar path).
            await self.on_navigate_command(NavigateCommand(self._canonical_kind(key), namespace))
        except Exception as exc:
            return f"ERROR: {exc}"
        rows = self.store.get(self.current_kind, self.current_scope)
        # Report what the user actually sees: apply the same filter as the
        # table render (substring/label/regex/… — issue #44) before counting.
        rows = self._filtered_rows(rows)
        self._mark_agent_action(f"view → {self.current_kind} ({self.current_scope})")
        suffix = " (list may still be loading)" if not rows else ""
        filter_note = f" (filter {self.filter_pattern!r} applied)" if self.filter_pattern else ""
        return (
            f"switched to {self.current_kind} in {self.current_scope} — "
            f"{len(rows)} resources{filter_note}{suffix}"
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
        pane_gen = self._log_pane_gen
        try:
            known = await self._agent_pod_triples(namespace, pod)
            if not known:
                # Validate before _cancel_log_tasks: a hallucinated pod name
                # must not tear down the streams the user is watching.
                return f"ERROR: pod {namespace}/{pod} not found (check the name and namespace)"
            if container:
                names = [c for _, _, c in known if c]
                if names and container not in names:
                    return (
                        f"ERROR: container {container!r} not found in pod "
                        f"{namespace}/{pod} (containers: {', '.join(names)})"
                    )
                triples = [(namespace, pod, container)]
            else:
                triples = known
            if pane_gen != self._log_pane_gen:
                # The user (or another turn) changed the log pane while we were
                # resolving containers — user keystrokes take priority.
                return (
                    "ERROR: the log pane changed while resolving containers "
                    "(user action takes priority) — retry if still needed"
                )
            await self._cancel_log_tasks()
            if pane_gen != self._log_pane_gen:
                # Recheck after the cancel await: a user pane change landing
                # in that window still wins.
                return (
                    "ERROR: the log pane changed while preparing the streams "
                    "(user action takes priority) — retry if still needed"
                )
            self._log_pane_mode = "l"
            await self._open_log_pane(namespace, [(p, c) for _, p, c in triples], triples=triples)
        except Exception as exc:
            return f"ERROR: {exc}"
        target = f"{namespace}/{pod}" + (f" [{container}]" if container else "")
        self._mark_agent_action(f"logs → {target}")
        # _open_log_pane caps at MAX_PANELS; tell the model which subset is
        # actually visible so it never assumes every container is on screen.
        truncated = ""
        if len(triples) > MAX_PANELS:
            truncated = (
                f" (showing first {MAX_PANELS} of {len(triples)} containers; "
                f"pass 'container' to view a specific one)"
            )
        return f"log pane opened for {target} — the user can now see the live logs{truncated}"

    async def _agent_pod_triples(self, namespace: str, pod: str) -> list[tuple[str, str, str]]:
        """All (ns, pod, container) triples for a pod the agent targets.

        The agent may open logs for a pod outside the visible view/scope, so
        the live manifest is authoritative; the store bucket is only a
        fallback. Returns an empty list when the pod cannot be found at all.
        """
        if self._get_manifest is not None:
            try:
                manifest = await self._get_manifest("pods", namespace, pod)
            except ApiStatusError:
                # The API authoritatively rejected the target (e.g. 404 for a
                # freshly deleted pod still in the watch cache) — surface it
                # instead of falling back to stale cache data.
                raise
            except Exception:
                logger.debug("agent logs: manifest container lookup failed", exc_info=True)
            else:
                spec = manifest.get("spec") or {}
                # Init and ephemeral containers are valid log targets too
                # (the human container picker exposes init containers).
                names = [
                    c.get("name")
                    for section in ("containers", "initContainers", "ephemeralContainers")
                    for c in spec.get(section) or []
                ]
                triples = [(namespace, pod, str(n)) for n in names if n]
                if triples:
                    return triples
        containers = self._get_pod_containers(namespace, pod)
        if containers:
            return [(namespace, pod, ctr) for ctr in containers]
        if any(
            obj.namespace == namespace and obj.name == pod and isinstance(obj, PodSummary)
            for obj in self.store.get(self.current_kind, self.current_scope)
        ):
            # Known pod without container info: blank container = server default.
            return [(namespace, pod, "")]
        return []

    async def agent_drill_down(self, name: str) -> str:
        if isinstance(self.screen, DescribeScreen):
            # Same user-priority guard as agent_navigate: drilling would
            # change the table hidden under the modal the user is reading.
            return (
                "ERROR: a describe screen is open — the user is reading it; "
                "ask them to close it (Esc) before changing the view"
            )
        canonical = self._canonical_kind(self.current_kind)
        child = drill_child(canonical)
        if child is None:
            return (
                f"ERROR: {canonical} has no drill-down chain - "
                "drill_down works on deployments, replicasets, and helm releases"
            )
        rows = self.store.get(self.current_kind, self.current_scope)
        drill_uid = self._drill.parent_uid
        if drill_uid is not None and self.current_kind == self._drill.child_kind:
            rows = [r for r in rows if owned_by(r, drill_uid)]
        # drill_down acts on the visible table: apply the same filter as the
        # table render so the agent cannot drill into a hidden row.
        rows = self._filtered_rows(rows)
        matches = [r for r in rows if r.name == name]
        if not matches:
            return f"ERROR: no {canonical} named {name!r} in the current view"
        if len(matches) > 1:
            return (
                f"ERROR: multiple {canonical} named {name!r} across namespaces - "
                "navigate to one namespace first"
            )
        error = await self._drill_into(matches[0].namespace, name)
        if error is not None:
            return f"ERROR: {error}"
        self._mark_agent_action(f"drill → {self._drill.breadcrumb()}")
        return (
            f"drilled into {canonical}/{name} — now showing the {child} it owns "
            f"({self._drill.breadcrumb()})"
        )

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        if self._get_manifest is None:
            return "ERROR: describe unavailable in this session"
        key = kind.strip().lower()
        meta = self.aliases.get(key)
        if meta is None:
            return f"ERROR: unknown kind {kind!r} — not a resource kind in this cluster"
        if meta.namespaced and not namespace:
            return f"ERROR: kind {kind!r} is namespaced — provide the 'namespace' argument"
        # Snapshot the visible state: if the user pushes a screen or navigates
        # while the fetches below are pending, abort instead of covering it.
        top_screen = self.screen_stack[-1] if self.screen_stack else None
        view_before = (self.current_kind, self.current_scope)
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
                events = await self._get_events.fetch(namespace, name)
            except Exception:  # events are best-effort; the manifest still shows
                logger.debug("agent describe: event fetch failed", exc_info=True)
        title = f"{meta.plural}/{namespace or '-'}/{name}"
        current_top = self.screen_stack[-1] if self.screen_stack else None
        if current_top is not top_screen or (self.current_kind, self.current_scope) != view_before:
            return (
                "ERROR: the screen changed while fetching the manifest "
                "(user action takes priority) — retry if still needed"
            )
        # When the chat panel is visible, show the non-modal pane on the left
        # instead of pushing a modal: a modal becomes the active screen and
        # would keep the chat input from taking focus. Resolved outside the
        # try below so a missing widget isn't masked as a generic push error.
        share = self._agent_panel_expanded()
        try:
            await self._show_describe(share, title, manifest, events)
        except Exception as exc:
            return f"ERROR: {exc}"
        self._mark_agent_action(f"describe → {title}")
        return f"describe screen opened for {title} — manifest and events are on screen"

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        """Approval-gated write requested by the agent (spec §6.2): opens the
        same ConfirmScreen as the keybindings; only the user's keystroke can
        approve it, and the outcome (executed/denied/error) flows back as the
        tool result. Every executed write is audited with an agent marker."""
        name = name.strip()  # every stage below must see the exact same target
        # One stamp per approval request (rollout restarts only): the preview
        # and the executed write send the identical patch body.
        stamp = restart_stamp()
        built = self._agent_write_op(
            action, kind, name, namespace, replicas, resources, restarted_at=stamp
        )
        if isinstance(built, str):
            return built
        meta, ns, op, operation, detail = built
        if not await self._permitted(action, meta, ns, name):
            verb, target = self._write_perm_target(action, meta)
            return f"ERROR: missing permission: {verb} {target}"
        try:
            # Capture the target's uid *before* asking for approval: the
            # executed write carries it as a precondition, so the approval is
            # bound to this exact object incarnation - a same-named
            # replacement created while the dialog is open gets a 409, not
            # the mutation. The lookup uses the caller's validated alias, not
            # meta.plural: alias resolution is first-wins, so a plural that
            # collides across groups could otherwise resolve to a different
            # resource than the one validated above.
            uid = await self._target_uid(kind.strip().lower(), ns, name)
        except ApiStatusError:
            return f"ERROR: {self._gvr_label(meta)}/{name} not found{self._write_locus(ns)}"
        preview = await self._preview_for_action(
            action, meta, ns, name, replicas, resources, uid, stamp
        )
        require = name if action == "delete" and not meta.namespaced else None
        decision = await self._await_user_approval(
            f"Agent requests: {action} {self._gvr_label(meta)}/{name}{self._write_locus(ns)}",
            operation,
            require_name=require,
            preview=preview,
        )
        if decision == "expired":
            return (
                f"not approved: the request expired before the user responded"
                f" ({action} {self._gvr_label(meta)}/{name})"
            )
        if decision != "approved":
            return (
                f"denied: the user declined the {action} request for {self._gvr_label(meta)}/{name}"
            )
        outcome = await self._run_write(action, meta, ns, name, op(uid), detail=detail)
        if outcome != "done":
            return f"ERROR: {action} {self._gvr_label(meta)}/{name} {outcome}"
        self._mark_agent_action(f"{action} → {self._gvr_label(meta)}/{name}")
        return f"approved and executed: {action} {self._gvr_label(meta)}/{name}"

    async def _preview_for_action(
        self,
        action: str,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        replicas: int | None,
        resources: dict[str, dict[str, dict[str, str]]] | None,
        uid: str | None,
        restarted_at: str,
    ) -> list[str] | None:
        """Dry-run preview for an agent-requested write; None (no preview)
        for unknown actions or a scale without a validated replica count.
        The captured ``uid`` and per-approval ``restarted_at`` stamp ride
        along so the dry run replays the exact request that would execute
        on approval."""
        ops = self._write_ops
        if ops is None:
            return None
        if action == "delete":
            return await self._dry_run_preview(ops.preview_delete(meta, ns, name, uid=uid))
        if action == "scale" and replicas is not None:
            return await self._dry_run_preview(ops.preview_scale(meta, ns, name, replicas, uid=uid))
        if action == "rollout_restart":
            return await self._dry_run_preview(
                ops.preview_rollout_restart(meta, ns, name, uid=uid, restarted_at=restarted_at)
            )
        if action == "resize" and resources:
            return await self._dry_run_preview(
                ops.preview_resize(ns or "", name, resources, uid=uid)
            )
        return None

    async def _target_uid(self, kind_alias: str, ns: str | None, name: str) -> str | None:
        """Uid of a write target at request time, looked up by the same alias
        the write was validated with (both resolve through the one aliases
        mapping wired in __main__, so the manifest and the mutation address
        the same resource even when plurals collide across groups).
        Raises ApiStatusError(404) when the target does not exist (the caller
        turns that into an actionable error before bothering the user with a
        dialog). Fails open (None -> no precondition, matching the previous
        behaviour) when no manifest source is wired or the lookup fails for
        infrastructure reasons - including a lookup slower than
        _UID_LOOKUP_TIMEOUT, so a stalled API server cannot leave the caller
        pending forever - the write stays approval-gated and audited."""
        if self._get_manifest is None:
            return None
        try:
            manifest = await asyncio.wait_for(
                self._get_manifest(kind_alias, ns, name), _UID_LOOKUP_TIMEOUT
            )
        except ApiStatusError as exc:
            if exc.status == 404:
                raise
            logger.warning("uid lookup for %s/%s failed; writing without precondition", ns, name)
            return None
        except TimeoutError:
            logger.warning("uid lookup for %s/%s timed out; writing without precondition", ns, name)
            return None
        except Exception:
            logger.exception("uid lookup for %s/%s failed; writing without precondition", ns, name)
            return None
        raw = (manifest.get("metadata") or {}).get("uid")
        return str(raw) if raw else None

    def _agent_write_op(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None,
        replicas: int | None,
        resources: dict[str, dict[str, dict[str, str]]] | None,
        *,
        restarted_at: str,
    ) -> tuple[ResourceMeta, str | None, Callable[[str | None], Awaitable[None]], str, str] | str:
        """Validate an agent write request; return (meta, ns, op, operation
        description, audit detail) or an 'ERROR: ...' string. ``restarted_at``
        is the per-approval stamp a rollout restart shares with its preview."""
        if self.config.readonly:
            return "ERROR: read-only mode - cluster writes are disabled"
        if self._audit is None:
            # Fail-closed auditing (AGENTS.md): no audit sink means no writes.
            return "ERROR: writes disabled - no audit log configured"
        name = name.strip()
        if not name:
            # JSON Schema 'required' does not reject empty strings; an empty
            # name would build a collection path instead of one exact object.
            # (agent_request_write pre-strips: keep this for direct callers.)
            return "ERROR: 'name' must be a non-empty resource name"
        namespace = namespace.strip() or None if namespace is not None else None
        resolved = self._agent_write_meta(kind, namespace)
        if isinstance(resolved, str):
            return resolved
        meta, ns = resolved
        if action == "delete":
            return self._agent_delete_op(meta, ns, name)
        if action == "scale":
            return self._agent_scale_op(meta, ns, name, replicas)
        if action == "rollout_restart":
            return self._agent_restart_op(meta, ns, name, restarted_at)
        if action == "resize":
            return self._agent_resize_op(meta, ns, name, resources)
        return f"ERROR: unknown write action {action!r}"

    def _agent_write_meta(
        self, kind: str, namespace: str | None
    ) -> tuple[ResourceMeta, str | None] | str:
        """Resolve an agent write's kind to a writable (meta, ns), or an
        'ERROR: ...' string: synthetic view kinds (helm browser) are
        read-only presentations of other objects and can never be written."""
        meta = self.aliases.get(kind.strip().lower())
        if meta is None:
            return f"ERROR: unknown kind {kind!r} - not a resource kind in this cluster"
        if meta.synthetic:
            return f"ERROR: kind {kind!r} is a read-only korvid view - it cannot be written"
        if meta.namespaced and not namespace:
            return f"ERROR: kind {kind!r} is namespaced - provide the 'namespace' argument"
        return meta, namespace if meta.namespaced else None

    def _agent_delete_op(
        self, meta: ResourceMeta, ns: str | None, name: str
    ) -> tuple[ResourceMeta, str | None, Callable[[str | None], Awaitable[None]], str, str] | str:
        ops = self._write_ops
        if ops is None:
            return "ERROR: delete unavailable in this session"
        return (
            meta,
            ns,
            lambda uid: ops.delete_object(meta, ns, name, uid=uid),
            f"DELETE {self._gvr_label(meta)}/{name}{self._write_locus(ns)}",
            "requested by agent",
        )

    def _agent_scale_op(
        self, meta: ResourceMeta, ns: str | None, name: str, replicas: int | None
    ) -> tuple[ResourceMeta, str | None, Callable[[str | None], Awaitable[None]], str, str] | str:
        ops = self._write_ops
        if ops is None:
            return "ERROR: scale unavailable in this session"
        if (meta.group, meta.plural) not in self._SCALABLE:
            return f"ERROR: scale does not apply to {self._gvr_label(meta)}"
        if replicas is None or replicas < 0:
            return "ERROR: scale requires a 'replicas' argument >= 0"
        return (
            meta,
            ns,
            lambda uid: ops.scale_object(meta, ns, name, replicas, uid=uid),
            f"PATCH {self._gvr_label(meta)}/{name} scale -> {replicas} replicas{self._write_locus(ns)}",
            f"replicas -> {replicas}; requested by agent",
        )

    def _agent_restart_op(
        self, meta: ResourceMeta, ns: str | None, name: str, restarted_at: str
    ) -> tuple[ResourceMeta, str | None, Callable[[str | None], Awaitable[None]], str, str] | str:
        ops = self._write_ops
        if ops is None:
            return "ERROR: rollout restart unavailable in this session"
        if (meta.group, meta.plural) not in self._RESTARTABLE:
            return f"ERROR: rollout restart does not apply to {self._gvr_label(meta)}"
        return (
            meta,
            ns,
            lambda uid: ops.rollout_restart_with_stamp(
                meta, ns, name, uid=uid, restarted_at=restarted_at
            ),
            f"PATCH {self._gvr_label(meta)}/{name} pod template (restartedAt annotation)"
            f"{self._write_locus(ns)}",
            "requested by agent",
        )

    def _agent_resize_op(
        self,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        resources: dict[str, dict[str, dict[str, str]]] | None,
    ) -> tuple[ResourceMeta, str | None, Callable[[str | None], Awaitable[None]], str, str] | str:
        ops = self._write_ops
        if ops is None:
            return "ERROR: resize unavailable in this session"
        if (meta.group, meta.plural) != ("", "pods"):
            return f"ERROR: resize does not apply to {self._gvr_label(meta)}"
        if not self._pod_resize_supported:
            return "ERROR: this cluster does not expose pods/resize (requires Kubernetes 1.35+)"
        if not resources:
            return "ERROR: resize requires a non-empty 'resources' argument"
        namespace = ns or ""
        summary = self._resize_summary(resources)
        return (
            meta,
            ns,
            lambda uid: ops.resize_pod(namespace, name, resources, uid=uid),
            f"PATCH pods/{name}/resize: {summary}{self._write_locus(ns)}",
            f"{summary}; requested by agent",
        )

    async def _wait_until_surfaceable(self, deadline: float) -> bool:
        """Poll until an approval dialog may surface (panel expanded, no other
        screen on top); False when the deadline passes first."""
        loop = asyncio.get_running_loop()
        if self._can_surface_approval():
            return True
        pending_msg = "Agent write approval pending - open the agent panel (Ctrl-A) to review"
        self.notify(pending_msg, severity="warning", timeout=10)
        last_reminder = loop.time()
        while not self._can_surface_approval():
            if loop.time() >= deadline:
                return False
            if loop.time() - last_reminder >= 30:
                # The first toast fades after 10s: keep reminding so the
                # request does not silently expire.
                self.notify(pending_msg, severity="warning", timeout=10)
                last_reminder = loop.time()
            await asyncio.sleep(0.05)
        return True

    async def _await_user_approval(
        self,
        title: str,
        operation: str,
        *,
        require_name: str | None = None,
        preview: list[str] | None = None,
    ) -> Literal["approved", "declined", "expired"]:
        """Show a ConfirmScreen and wait for the user's decision. Only real key
        input can resolve it. While the agent panel is collapsed, or another
        screen (a user dialog, describe, picker) is on top, the request stays
        pending instead of pushing a modal (spec 6.1: approval dialogs are
        never auto-opened from the collapsed state, and never stacked over an
        active dialog where a stray keystroke could approve it); it surfaces
        when the panel is expanded with a clear screen. Pending and on-screen
        time share one deadline, so an unanswered or never-surfaced request
        resolves as "expired" (distinct from an explicit "declined", so the
        agent is never told the user declined when nobody answered) and an
        agent turn can never hang forever."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _APPROVAL_TIMEOUT
        if not await self._wait_until_surfaceable(deadline):
            return "expired"
        fut: asyncio.Future[bool] = loop.create_future()

        def _done(confirmed: bool | None) -> None:
            if not fut.done():
                fut.set_result(bool(confirmed))

        screen = ConfirmScreen(title, operation, require_name=require_name, preview=preview)
        await self.push_screen(screen, _done)
        # Recheck after mounting: surfacing the dialog (or push_screen itself)
        # can consume the last of the budget, and a fixed minimum here would
        # quietly extend the expiry contract past _APPROVAL_TIMEOUT.
        remaining = deadline - loop.time()
        try:
            if remaining <= 0:
                raise TimeoutError
            confirmed = await asyncio.wait_for(fut, timeout=remaining)
            return "approved" if confirmed else "declined"
        except TimeoutError:
            # Late keystrokes are a no-op (the future is already resolved),
            # but clear the dialog when possible so it doesn't linger.
            if self.screen is screen:
                with contextlib.suppress(Exception):
                    self.pop_screen()
            elif screen in self.screen_stack:
                self.notify(
                    "Agent write request expired - dismiss the pending dialog with Esc",
                    severity="warning",
                )
            return "expired"

    async def _show_describe(
        self,
        share: bool,
        title: str,
        manifest: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> None:
        """Present a describe view: non-modal pane when sharing with the chat
        panel (modal screens would steal focus from the chat input)."""
        if manifest.get("kind") == "Secret":
            # Masking pipeline (design §7): this path is agent-driven, so the
            # rendered body is LLM-adjacent — secret values must never appear.
            manifest = mask_secret_manifest(manifest)
        if share:
            self.query_one(DescribePane).show(title, manifest, events)
        else:
            await self.push_screen(DescribeScreen(title, manifest, events))

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
        # Text keeps user-entered filter text literal (never Rich markup).
        empty.update(Text(message))
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
        if self._metrics is not None:
            await self._metrics.stop()
        await self.watch_manager.stop_all()


class AppUIBridge(UIBridge):
    """Nominal `UIBridge` adapter over `KorvidApp`.

    The layer-boundary interface must be an `abc.ABC` (AGENTS.md), but
    Textual's `App` metaclass conflicts with `ABCMeta`, so the app cannot
    inherit `UIBridge` directly — this thin adapter conforms nominally and
    delegates to the app's bridge methods.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        return await self._app.agent_navigate(view, namespace)

    async def agent_set_filter(self, pattern: str) -> str:
        return await self._app.agent_set_filter(pattern)

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        return await self._app.agent_open_logs(pod, namespace, container)

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        return await self._app.agent_open_describe(kind, name, namespace)

    async def agent_drill_down(self, name: str) -> str:
        return await self._app.agent_drill_down(name)

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        return await self._app.agent_request_write(
            action, kind, name, namespace, replicas, resources
        )
