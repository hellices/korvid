"""KorvidApp — constructed with injected dependencies (composition in __main__)."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import functools
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import weakref
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, ClassVar, Concatenate, Literal, ParamSpec, TypeVar

if TYPE_CHECKING:
    # Annotation-only: the base TUI must not import the embedded-agent
    # runtime at startup (issue #73) — the composition root injects it
    # only when the [agent] extra is installed and wired.
    from korvid.agent.runtime import AgentRuntime

import yaml
from rich.text import Text
from textual.app import App, ComposeResult, ScreenStackError, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.events import DescendantBlur, DescendantFocus, Key
from textual.widgets import DataTable, Static
from textual.widgets.data_table import CellDoesNotExist, RowDoesNotExist
from textual.worker import Worker, get_current_worker

from korvid.agent.events import AgentError, AgentEvent, ToolCallFinished, ToolCallStarted
from korvid.agent.setup import AgentConfigurator, AgentSettings
from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig, ViewConfig
from korvid.core.debugimage import (
    FALLBACK_IMAGE,
    recommend_debug_images,
    same_image_ref,
)
from korvid.core.errors import explain_api_error
from korvid.core.filters import ResourceFilter, parse_filter
from korvid.core.keybindings import plan_keybindings, shift_alias_keys
from korvid.core.logbuffer import LogBuffer
from korvid.core.logexport import default_log_export_dir, export_log_lines
from korvid.core.mcp import MCPControllerBase
from korvid.core.portforward import (
    ForwardRecord,
    ForwardRegistry,
    ForwardSpec,
    candidate_remote_ports,
    controller_owner,
)
from korvid.core.secrets import mask_secret_manifest
from korvid.core.sorting import SORT_COLUMNS, SortSpec, toggle_sort
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.transfer import RemoteEntry, TransferError, TransferSpec, list_remote_dir
from korvid.core.watch import WatchManager
from korvid.k8s.components import (
    MAX_COMPONENT_DOCS,
    ComponentRef,
    installplan_components,
    reference_components,
)
from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.drain import DrainPlan
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.helm import (
    HELM_RELEASES_META,
    HELM_REVISIONS_META,
    HelmReleaseSummary,
    HelmRevisionSummary,
)
from korvid.k8s.helmcli import ChartHit, HelmCLI, HelmError, HelmPreviewUnsupported
from korvid.k8s.logs import LogLine
from korvid.k8s.managed import manager_of
from korvid.k8s.metrics import MetricsPoller
from korvid.k8s.models import ContainerTrouble, PodSummary
from korvid.k8s.olm import (
    OPERATORS_GROUP,
    PACKAGES_GROUP,
    PackageInstallFacts,
    build_subscription,
    package_install_facts,
    resolve_olm_meta,
)
from korvid.k8s.portforward import FORWARDABLE_KINDS, forward_target_gvr
from korvid.k8s.relations import drill_child, owned_by
from korvid.k8s.telepresence import TelepresenceCLI, TelepresenceError
from korvid.k8s.writes import WriteOps, restart_stamp
from korvid.tools.executor import UIBridge
from korvid.tools.follow import FOLLOWABLE_TOOLS, mirror_read
from korvid.tools.proposals import (
    ProposalClosedError,
    ProposalLimitError,
    ProposalState,
    ProposalStore,
    ProposalTooLargeError,
    WriteProposal,
)
from korvid.ui.command import command_help
from korvid.ui.debug import DebugController
from korvid.ui.drain import DrainController
from korvid.ui.hints import EventsFetcher, HintController, pod_needs_hint
from korvid.ui.messages import (
    AgentPromptSubmitted,
    ClearFilter,
    ExternalProposalExpired,
    ExternalProposalsChanged,
    FilterCommand,
    NavigateCommand,
    QuitCommand,
    ResourcesUpdated,
    ShowContextPicker,
    ShowError,
    ShowNamespacePicker,
    SortCommand,
    SwitchContextCommand,
    TransferCancelRequested,
    UnknownCommand,
)
from korvid.ui.navigation import DrillLevel, NavigationStack
from korvid.ui.shell import (
    DEBUG_IMAGE,
    build_exec_argv,
    build_node_debug_create_argv,
    build_pod_attach_argv,
    build_pod_get_argv,
    build_pod_wait_argv,
    build_probe_argv,
    parse_debug_pod_name,
)
from korvid.ui.transfer import TransferController, TransferProgress
from korvid.ui.widgets.agent_panel import AgentPanel
from korvid.ui.widgets.agent_setup_screen import AgentSetupScreen
from korvid.ui.widgets.command_bar import CommandBar
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ImagePrompt, ReplicasPrompt
from korvid.ui.widgets.containers_screen import ContainersScreen, build_container_rows
from korvid.ui.widgets.describe_screen import DescribePane, DescribeScreen
from korvid.ui.widgets.filter_bar import FilterBar
from korvid.ui.widgets.helm_chart_search import HelmChartSearchScreen
from korvid.ui.widgets.helm_install import HelmInstallPrompt, HelmReleaseChoices
from korvid.ui.widgets.helm_repos import HelmRepoScreen
from korvid.ui.widgets.help_screen import HelpScreen, collect_help
from korvid.ui.widgets.hierarchy_screen import HierarchyScreen, build_hierarchy
from korvid.ui.widgets.hint_detail import HintDetailScreen
from korvid.ui.widgets.hint_strip import HintStrip
from korvid.ui.widgets.log_pane import MAX_PANELS, LogPane
from korvid.ui.widgets.logo import SplashLogo
from korvid.ui.widgets.namespace_picker import NamespacePicker
from korvid.ui.widgets.operator_install import OperatorInstallPrompt
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.widgets.port_forward_screen import ForwardListScreen, PortForwardScreen
from korvid.ui.widgets.resize_prompt import ResizePrompt
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.secret_screen import SecretScreen
from korvid.ui.widgets.status_bar import StatusBar
from korvid.ui.widgets.telepresence_screen import TelepresenceScreen
from korvid.ui.widgets.top_bar import KeyEntry, TopBar
from korvid.ui.widgets.transfer_screen import TransferProgressScreen, TransferScreen

_DEFAULT_ALIASES: dict[str, ResourceMeta] = {
    "pods": PODS_META,
    "po": PODS_META,
    "pod": PODS_META,
}

logger = logging.getLogger(__name__)

#: How often the app polls the forward registry for dead kubectl processes.
_FORWARD_POLL_SECONDS = 2.0

#: How long to wait for kubectl's readiness line before the start is failed
#: explicitly — the silent child is terminated, never assumed to be ready.
_FORWARD_READY_SECONDS = 5.0

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


def _looks_like_admission_rejection(stderr: str) -> bool:
    """True when kubectl stderr clearly shows the API server refused the
    create — only then is it safe to state that no pod was committed.

    Matches the stable phrases of the two refusal shapes: API-server
    refusals (`Error from server (Forbidden): ... is forbidden: ...`) and
    admission webhooks (`admission webhook ... denied the request`). The
    match is deliberately tight — a false positive here suppresses the
    cleanup hint and can strand a privileged pod, so a bare `forbidden`
    substring (which could appear in a pod or image name, or quoted inside
    an unrelated server error) is not enough.
    """
    lowered = stderr.lower()
    return "error from server (forbidden)" in lowered or "denied the request" in lowered


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


def _manifest_uid(manifest: dict[str, Any]) -> str | None:
    """The metadata.uid of a fetched manifest, or None when absent."""
    raw = (manifest.get("metadata") or {}).get("uid")
    return str(raw) if raw else None


def _installed_csv_name(manifest: dict[str, Any]) -> str:
    """`status.installedCSV` of a Subscription manifest, or '' when absent
    (the operator never finished installing, or the status is malformed)."""
    status = manifest.get("status")
    if not isinstance(status, dict):
        return ""
    return str(status.get("installedCSV") or "")


class _CsvTargetUnavailable(Exception):
    """The Subscription records an installed CSV that cannot be safely
    targeted right now. The uninstall must abort: skipping the CSV would
    leave the operator running after an approved *full* uninstall, and
    deleting it without a uid pin could remove a replacement incarnation
    created while the dialog was open."""


#: Upper bound on the pre-dialog dry-run round trip (issue #19): a slow or
#: unreachable API server delays the approval dialog by at most this long,
#: after which it opens without a preview - a preview must never block the
#: approval flow.
_PREVIEW_TIMEOUT = 3.0

#: Upper bound on a helm preview (issue #31): `helm ... --dry-run` shells out
#: and may pull the chart from a repo, so it gets more budget than an API
#: server dry-run - still bounded, the approval dialog is never wedged.
_HELM_PREVIEW_TIMEOUT = 20.0

#: A rendered chart can run to thousands of lines; the approval dialog shows
#: at most this many so the operation summary stays reviewable.
_HELM_PREVIEW_MAX_LINES = 60

#: Upper bound on the hint-overlay events fetch (issue #34): the trouble half
#: comes from the status the app already holds, so a stalled API connection
#: must not delay the overlay past this - the events are marked unavailable
#: instead.
_HINT_EVENTS_TIMEOUT = 3.0

#: Header label -> builtin sort column (issue #138): the reverse of the
#: table's ▲/▼ decoration map, for header-click sorting.
_HEADER_SORT_COLUMNS = {"NAME": "name", "AGE": "age", "CPU": "cpu", "MEM": "mem"}


@dataclasses.dataclass(frozen=True)
class ContextSwitchResult:
    """What the composition root re-derived for the new cluster (issue #36).

    Returned by the injected ``switch_context`` callable once the connection
    is retargeted: capability gates are per-cluster facts the app must adopt
    atomically with the switch.
    """

    pod_resize_supported: bool
    provider_hint: str | None
    context_namespace: str | None
    #: helm CLI wrapper rebound to the new context (issue #31 x #36): the
    #: startup HelmCLI pins --kube-context, so keeping it across a switch
    #: would send approval-gated helm writes to the OLD cluster.
    helm: HelmCLI | None = None
    #: The new context's name when it matches `protected_contexts` (issue
    #: #83), None otherwise — the marker is re-derived on every switch.
    protected_context: str | None = None


@dataclasses.dataclass(frozen=True)
class _HierarchyReturn:
    """The way back to a hierarchy tree after a goto jump (issue #135).

    Captured when a tree node's Enter navigates away: Escape on the jump
    target (with no drill level left to pop) rebuilds the tree over the
    origin view, cursor on the picked node. Lives on the pane that jumped
    (`PaneState.hierarchy_return`) — panes neither see nor clear each
    other's returns. One-shot: consumed or dropped on first eligible use,
    and abandoned when its pane explicitly navigates away."""

    origin_view: str  # canonical kind alias the tree was opened over
    origin_scope: str  # that pane's namespace scope at pick time
    title: str
    refs: list[ComponentRef]
    namespace: str
    tree_scope: str  # store-lookup scope the tree was built with
    picked: tuple[str, str, str]  # (kind alias, namespace, name) of the node
    epoch: int


_WriteParams = ParamSpec("_WriteParams")
_WriteResult = TypeVar("_WriteResult")


def _tracks_cluster_write(
    method: Callable[Concatenate[KorvidApp, _WriteParams], Awaitable[_WriteResult]],
) -> Callable[Concatenate[KorvidApp, _WriteParams], Coroutine[Any, Any, _WriteResult]]:
    """Count in-flight cluster mutations on the app (issue #36).

    An approved write worker is neither an open dialog nor the agent task,
    so `:ctx` switching checks this counter: a mutation approved for one
    cluster must never execute against another after a mid-flight retarget.
    """

    def wrapper(
        self: KorvidApp, /, *args: _WriteParams.args, **kwargs: _WriteParams.kwargs
    ) -> Coroutine[Any, Any, _WriteResult]:
        # Reserve the slot synchronously: confirmation callbacks construct
        # this coroutine and hand it to run_worker, which only starts it on
        # a later event-loop iteration — a queued `:ctx` processed in that
        # gap must already see the write as in flight.
        self._active_cluster_writes += 1
        released = False

        def release() -> None:
            # Idempotent: normally fired by run()'s finally, but also by the
            # GC finalizer when the coroutine is closed or collected without
            # ever running (worker cancelled before start, app shutdown) —
            # a leaked +1 would block every future `:ctx` switch.
            nonlocal released
            if not released:
                released = True
                self._active_cluster_writes -= 1

        async def run() -> _WriteResult:
            try:
                return await method(self, *args, **kwargs)
            finally:
                release()

        coro = run()
        weakref.finalize(coro, release)
        return coro

    # Not functools.wraps: its _Wrapped return type keeps the explicit
    # 'self' arg and fails the plain-Callable return annotation under
    # mypy --strict; worker/log names only need these two attributes.
    wrapper.__name__ = method.__name__
    wrapper.__qualname__ = method.__qualname__
    return wrapper


def _chart_base(chart: str) -> str:
    """`"nginx-18.1.0"` -> `"nginx"`: strip the version suffix helm appends
    to a release's chart field, so an upgrade can pre-filter the chart search
    by name. Charts whose last dash segment is not a version stay whole."""
    base, sep, tail = chart.rpartition("-")
    if sep and tail[:1].isdigit():
        return base
    return chart


@dataclasses.dataclass(frozen=True)
class _HelmRenderFailure:
    """A dry-run render that helm itself rejected (issue #139).

    The dry-run runs the same command the approval would execute, so its
    failure is the real failure delivered early — the flow must stop before
    approval and show `error` (helm's stderr tail names the missing value)
    instead of letting the user approve a doomed mutation.
    """

    error: str


def _clip_preview(text: str) -> list[str] | None:
    """Dry-run/diff output as approval-dialog preview lines, capped at
    `_HELM_PREVIEW_MAX_LINES`; None when there is nothing to show."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) > _HELM_PREVIEW_MAX_LINES:
        hidden = len(lines) - _HELM_PREVIEW_MAX_LINES
        return [*lines[:_HELM_PREVIEW_MAX_LINES], f"... ({hidden} more lines)"]
    return lines


@contextlib.asynccontextmanager
async def _temp_values_file(values_text: str | None) -> AsyncIterator[str | None]:
    """A 0600 temp file holding the edited values for one helm invocation,
    deleted as soon as the command returns (values may embed credentials);
    None passes straight through as "no values override"."""
    if values_text is None:
        yield None
        return
    fd, tmp = tempfile.mkstemp(suffix=".yaml", prefix="korvid-helm-values-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(values_text)
        yield tmp
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


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


class PaneState:
    """One workspace pane's independent view state (issue #48).

    The app keeps one of these per pane; `current_kind` and friends
    delegate to the focused pane, so every existing action and command
    naturally targets the pane the user is working in.
    """

    def __init__(self, kind: str, scope: str, table_id: str = "pane-0") -> None:
        self.kind = kind
        self.scope = scope
        self.table_id = table_id  # the ResourceTable widget this pane renders into
        self.filter_pattern = ""
        self.resource_filter: ResourceFilter = parse_filter("")
        self.drill = NavigationStack()
        #: Monotonic navigation counter: every _navigate_locked call on this
        #: pane advances it, including same-target ones. A drill pre-warm
        #: (issue #157) captures it before waiting and revalidates under the
        #: lock - a `:view deployments` while already on deployments is
        #: still the newer command and must not be overridden.
        self.nav_gen = 0
        #: Pending way back to a hierarchy tree a goto jump navigated away
        #: from (issue #135); consumed by Escape in this pane. View state
        #: like the drill stack - never shared across panes.
        self.hierarchy_return: _HierarchyReturn | None = None
        #: Per-kind sort state - view state like the filter, so it belongs
        #: to the pane: sorting one pane must not reorder the other.
        self.sorts: dict[str, SortSpec] = {}

    def clone(self, table_id: str) -> PaneState:
        """A split starts as a clone of the focused view: same kind, scope,
        filter and drill position - with independent state from then on.
        A pending hierarchy return is deliberately not cloned: it is a
        one-shot ticket back to one tree, not repeatable view state."""
        pane = PaneState(self.kind, self.scope, table_id)
        pane.filter_pattern = self.filter_pattern
        pane.resource_filter = parse_filter(self.filter_pattern)
        pane.drill = self.drill.copy()
        pane.sorts = dict(self.sorts)
        return pane


class KorvidApp(App[None]):
    # Every binding carries an ``id`` so the `keybindings:` config section
    # can remap it via Textual's keymap (issue #35); uppercase duplicates
    # of shift+<letter> keys share the action under an ``--alt`` id.
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit", id="quit"),
        Binding("question_mark", "help", "Help", id="help"),
        # Top bar collapse/expand (issue #142): the grouped legend's toggle.
        Binding("tilde", "toggle_topbar", "Legend", show=False, id="toggle_topbar"),
        Binding("colon", "open_command", "Command", id="open_command"),
        Binding("slash", "open_filter", "Filter/Search", id="open_filter"),
        Binding("0", "toggle_all_namespaces", "All NS", id="toggle_all_namespaces"),
        # `favorite_namespaces` shortcuts (issue #108): UI-only jumps, bound
        # in config order. Hidden from the footer; the help overlay merges
        # the nine bindings into a single row.
        *[
            Binding(
                str(i),
                f"favorite_namespace({i})",
                "Jump to favorite namespace (1-9)",
                show=False,
            )
            for i in range(1, 10)
        ],
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
        # Interactive column picker (issue #138): every sortable column of
        # the current view, no exact names to remember.
        Binding("o", "sort_picker", "Sort by column", show=False, id="sort_picker"),
        Binding("ctrl+a", "toggle_agent", "AI", priority=True, id="toggle_agent"),
        Binding("ctrl+d", "delete_resource", "Delete", id="delete_resource"),
        Binding("r", "rollout_restart", "Restart", id="rollout_restart"),
        Binding(
            "R",
            "resize_pod",
            "Resize pod CPU/memory in place (K8s 1.35+)",
            show=False,
            id="resize_pod",
        ),
        Binding("S", "scale_resource", "Scale", id="scale_resource"),
        Binding("e", "edit_resource", "Edit", show=False, id="edit_resource"),
        Binding("i", "hint_details", "Hint details", show=False, id="hint_details"),
        Binding(
            "I",
            "operator_install",
            "Install operator / approve InstallPlan",
            id="operator_install",
        ),
        # Real terminals deliver Shift+F as "F" (see shift+l above).
        Binding("shift+f", "port_forward", "Port-forward", id="port_forward"),
        Binding("F", "port_forward", "Port-forward", show=False, id="port_forward--alt"),
        # Node ops (issue #40): cordon / uncordon / drain, nodes view only.
        Binding("c", "cordon_node", "Cordon", id="cordon_node"),
        Binding("u", "uncordon_node", "Uncordon", id="uncordon_node"),
        Binding("shift+d", "drain_node", "Drain", id="drain_node"),
        Binding("D", "drain_node", "Drain", show=False, id="drain_node--alt"),
        # Helm ops (issues #31/#114): dedicated per-view bindings so the
        # overloaded i/u/r keys carry the right footer label and remain
        # independently remappable; `check_action` routes each key to the
        # binding whose view is on screen.
        Binding("i", "helm_install", "Install chart", id="helm_install"),
        Binding("u", "helm_upgrade", "Upgrade", id="helm_upgrade"),
        Binding("r", "helm_rollback", "Rollback", id="helm_rollback"),
        # Revision history moved off Enter (issue #120): Enter opens the
        # hierarchy tree, `h` keeps the flat history drill-down.
        Binding("h", "helm_history", "History", id="helm_history"),
        Binding("ctrl+t", "transfer", "Transfer", show=False, id="transfer"),
    ]

    # User-facing keys handled in event handlers rather than BINDINGS:
    # Enter drills down via `on_data_table_row_selected`, Escape closes
    # panes / pops a drill level via `on_key`.  Listed here so the help
    # overlay (`?`) renders them alongside the real bindings.
    #: user-facing keys handled in event handlers or via dispatch rather than
    #: dedicated bindings: (help group, default key, description, action id).
    #: A non-empty action id ties the row to a remappable binding so the help
    #: overlay shows the effective key (issue #35), not the default.
    HANDLER_KEY_HELP: ClassVar[tuple[tuple[str, str, str, str], ...]] = (
        (
            "Table",
            "enter",
            "Drill down (pods → containers, deploy → rs → pods, helm/operator → hierarchy tree)",
            "",
        ),
        ("Table", "escape", "Pop one drill-down level", ""),
        ("Table", "ctrl+w v", "Split workspace into two panes", ""),
        ("Table", "ctrl+w w", "Focus the other pane", ""),
        ("Table", "ctrl+w q", "Close the focused pane", ""),
        ("Logs", "escape", "Close pane (or dismiss search)", ""),
    )

    DEFAULT_CSS = """
    #workspace {
        height: 1fr;
    }
    #workspace ResourceTable {
        width: 1fr;
        height: 1fr;
    }
    ResourceTable.split-pane {
        border: round $panel;
    }
    /* A class, not `:focus`: the accent border marks the command-routing
       target (`_focused_pane`), which must stay visible while an Input
       (command/filter bar, agent panel) owns keyboard focus. */
    ResourceTable.split-pane.focused-pane {
        border: round $accent;
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
        get_helm_components: (Callable[[str, str], Awaitable[list[ComponentRef]]] | None) = None,
        get_events: EventsFetcher | None = None,
        stream_logs: Callable[..., AsyncIterator[LogLine]] | None = None,
        agent_runtime: AgentRuntime | None = None,
        agent_model_name: str | None = None,
        agent_configurator: AgentConfigurator | None = None,
        rebuild_agent: Callable[[AgentSettings], AgentRuntime | None] | None = None,
        agent_available: bool = True,
        write_ops: WriteOps | None = None,
        audit: AuditLog | None = None,
        check_permission: Callable[[str, str, str, str | None, str, str], Awaitable[bool]]
        | None = None,
        mcp: MCPControllerBase | None = None,
        edit_text: Callable[[str], Awaitable[str | None]] | None = None,
        metrics: MetricsPoller | None = None,
        pod_resize_supported: bool = False,
        forwards: ForwardRegistry | None = None,
        provider_hint: str | None = None,
        protected_context: str | None = None,
        open_pod_exec: Callable[..., contextlib.AbstractAsyncContextManager[Any]] | None = None,
        list_contexts: Callable[[], tuple[list[str], str | None]] | None = None,
        probe_context: Callable[[str], Awaitable[None]] | None = None,
        switch_context: Callable[[str | None], Awaitable[ContextSwitchResult]] | None = None,
        helm: HelmCLI | None = None,
        proposal_store: ProposalStore | None = None,
        save_topbar: Callable[[bool], None] | None = None,
        telepresence: TelepresenceCLI | None = None,
        probe_traffic_manager: Callable[[], Awaitable[bool]] | None = None,
        agent_follow_bridge: UIBridge | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.store = store
        self.watch_manager = watch_manager
        self._list_namespaces = list_namespaces
        #: `:ctx` collaborators (issue #36), wired by the composition root:
        #: kubeconfig context listing, the pre-switch auth probe, and the
        #: connection/capability retarget. All None in builds without a
        #: cluster connection.
        self._list_contexts = list_contexts
        self._probe_context = probe_context
        self._switch_context = switch_context
        #: Optional telepresence integration (issue #159): None = binary
        #: absent or kill-switched; the `:tp` panel simply reports that.
        self._telepresence = telepresence
        self._probe_traffic_manager = probe_traffic_manager
        self._telepresence_hinted = False
        self._telepresence_probing = False
        self._telepresence_reprobe = False
        #: True while a context switch is tearing down / retargeting;
        #: refuses concurrent switches.
        self._ctx_switching = False
        self._active_cluster_writes = 0
        #: Bumped every time a switch is applied: pre-approval awaits capture
        #: it and refuse to proceed if the cluster changed under them.
        self._ctx_epoch = 0
        #: One-shot notice injected into the agent's next screen context
        #: after a switch, so a running conversation learns the cluster
        #: changed under it.
        self._ctx_switch_note: str | None = None
        self._get_manifest = get_manifest
        self._get_helm_components = get_helm_components
        self._get_events = get_events
        self._stream_logs = stream_logs
        self._write_ops = write_ops
        self._audit = audit
        self._check_permission = check_permission
        self._mcp = mcp
        #: MCP follow mode (issue #153): mirror external cluster reads in
        #: the TUI. Config seeds the state; `:mcp follow on|off` toggles it.
        self._mcp_follow: bool = config.mcp_follow
        #: Agent follow: mirror the built-in agent's cluster reads on screen
        #: — small models rarely volunteer the UI tools, so without this the
        #: screen sits idle while the agent reads "behind its back". Config
        #: seeds the state (default on); `:ai follow on|off` toggles it.
        self._agent_follow: bool = config.agent_follow
        #: The shared serialized UI bridge (the composition root's
        #: `_UIBridgeProxy`): agent-follow mirrors route through it so they
        #: serialize with the agent's own UI tools and concurrent MCP UI
        #: calls - log-pane swaps and describes must never interleave.
        #: None (tests, degraded wiring) falls back to a direct adapter.
        self._agent_follow_bridge = agent_follow_bridge
        #: App-owned execution context (issue #165), captured in on_mount;
        #: None until then. AppUIBridge._dispatch refuses pre-mount calls
        #: as 'UI not ready' (production-reachable: the MCP endpoint goes
        #: live before app.run_async()) - it must never run a bridge
        #: coroutine directly in the caller's foreign context.
        self._app_context: contextvars.Context | None = None
        #: In-flight AppUIBridge dispatches (issue #165): on_unmount cancels
        #: and reaps them so a foreign caller racing shutdown cannot leave
        #: work alive against an unmounted app.
        self._dispatch_tasks: set[asyncio.Task[str]] = set()
        #: External MCP write proposals (issue #110): shared with the MCP
        #: server; None when the feature is disabled.
        self._proposal_store = proposal_store
        #: Top bar collapse/expand (issue #142): seeded from `ui.topbar`,
        #: toggled at runtime, persisted through the injected callback.
        self._topbar_expanded = config.ui_topbar_expanded
        self._save_topbar = save_topbar
        self._edit_text = edit_text
        self._metrics = metrics
        self._forwards = forwards
        self._broken_forwards: set[int] = set()
        #: FIFO of pending forward audit entries; a single drainer preserves
        #: event order (start before stop) that per-entry workers would not.
        self._forward_audit_queue: deque[dict[str, Any]] = deque()
        # Serializes write+dequeue across drain threads: a cancelled drain's
        # lingering `to_thread` must finish its pop before another drain
        # (e.g. the unmount flush) may look at the queue head.
        self._forward_audit_io_lock = threading.Lock()
        self._forward_audit_worker: Worker[None] | None = None
        #: forward id -> its in-flight readiness confirmations, oldest first
        #: (a re-attach may add a new generation while a superseded one is
        #: still waiting; the last entry is the current generation). A stop
        #: audit for such a record is serialized behind every outstanding
        #: confirmation so each start entry always reaches the log first.
        self._confirming_forwards: dict[int, list[Worker[None]]] = {}
        #: forward id -> the current-generation confirmation worker. An
        #: explicit token, not the pending list's tail: a finished current
        #: generation removes its token, and a still-pending superseded
        #: worker must never be promoted back to "current" by that.
        self._current_confirmations: dict[int, Worker[None]] = {}
        #: launches whose registry.start() may still be off-loop, keyed by
        #: their spec. Teardown awaits them (and stop audits / poll toasts
        #: for the same local port defer behind them) so a quit, stop, or
        #: poll landing between the registry publishing a record and the
        #: launch coroutine resuming can never audit a stop before its start
        #: or double-report the launch's failure.
        self._launching_forwards: dict[Worker[None], ForwardSpec] = {}
        #: re-attaches whose registry.reattach() may still be off-loop,
        #: keyed by their spec — same contract as _launching_forwards; the
        #: event resolves when the re-attach coroutine has landed its
        #: tracking (or its audit fallback).
        self._reattaching_forwards: dict[asyncio.Event, ForwardSpec] = {}
        #: set by _teardown_forwards: audits enqueue directly (the teardown
        #: flush drains them) since no new workers may spawn mid-shutdown.
        self._forwards_closing = False
        #: stops deferred behind a pending confirmation, keyed by forward id;
        #: teardown flushes leftovers so a cancelled worker can't lose them.
        self._deferred_stop_audits: dict[int, ForwardSpec] = {}
        #: pods/resize subresource discovered on the connected cluster
        #: (1.35 GA); gates the R keybinding and the resize agent tool.
        self._pod_resize_supported = pod_resize_supported
        #: the in-flight drain worker, if any - pressing the drain key again
        #: cancels it (evictions stop; the node stays cordoned).
        self._drain_worker: Worker[None] | None = None
        self._drain_node: str | None = None
        #: status-bar progress labels keyed by owner (drain, helm preview):
        #: concurrent operations must not clear each other's feedback.
        self._progress_labels: dict[str, str] = {}
        #: monotonic token for `_progress()` scopes: an exclusive-worker
        #: replacement can publish before its cancelled predecessor's
        #: cleanup runs, which must then not clear the replacement's label.
        self._progress_seq = 0
        #: detected cloud provider short name ("aks", "aws", ...) or None;
        #: drives the Service/Ingress describe footer (issue #30).
        self._provider_hint = provider_hint
        #: Active context's name when it matches `protected_contexts` (issue
        #: #83); None otherwise. Drives the red status marker, the extra
        #: type-the-context-name confirm layer, and the optional agent block.
        self._protected_context = protected_context
        #: KubeClient.open_pod_exec, bound at composition; None means no
        #: cluster connection, so file transfer (issue #47) is unavailable.
        self._open_pod_exec = open_pod_exec
        #: detected helm binary wrapper (issue #31), or None when helm is
        #: not on PATH - the install/upgrade/rollback keys then explain
        #: their absence instead of doing nothing.
        self._helm = helm
        #: transfer execution lifecycle (issue #91 U3a): the controller owns
        #: the stream task and in-flight serialization; the app keeps the
        #: dialogs, approval gate, epoch checks, and run_worker ownership.
        self._transfer = TransferController(
            notify=self.notify,
            open_pod_exec=lambda: self._open_pod_exec,
            audit=lambda: self._audit,
            pod_uid_unchanged=self._pod_uid_unchanged,
            show_progress=self._show_transfer_progress,
            close_progress=self._close_transfer_progress,
        )
        # Debug-fallback execution (issue #97 U3c): the controller owns the
        # gated, audited kubectl debug run; dialogs, RBAC pre-check and
        # run_worker ownership stay here. suspend/refresh are late-binding so
        # tests that patch the app's methods keep working.
        self._debug = DebugController(
            notify=self.notify,
            audit=lambda: self._audit,
            readonly=lambda: self.config.readonly,
            kube_context=lambda: self.config.kube_context,
            pod_uid_unchanged=self._pod_uid_unchanged,
            suspend=lambda: self.suspend(),
            refresh=lambda: self.refresh(),
            offer_pull_retry=self._offer_pull_retry,
        )
        # Drain execution (issue #97 U3d): the controller owns the approved
        # drain's cordon/evict/wait/audit lifecycle; keybinding routing, the
        # press-again-to-cancel semantics and the approval dialog stay here.
        # audit_write is late-binding so tests that patch the app's method
        # keep working.
        self._drain = DrainController(
            notify=self.notify,
            audit_write=lambda *args: self._audit_write(*args),
            set_progress=functools.partial(self._set_progress, "drain"),
        )
        self._permission_check_warned = False
        self._agent_runtime = agent_runtime
        self._agent_model_name = agent_model_name
        self._agent_configurator = agent_configurator
        self._rebuild_agent = rebuild_agent
        #: False when the [agent] extra is absent (issue #73): the agent
        #: panel is not mounted and :ai/:model/Ctrl-A are not offered.
        self._agent_available = agent_available
        self._agent_settings: AgentSettings | None = None
        #: capability profile of the live runtime (issue #71); shown in the
        #: agent panel header so users know which mode the agent runs in.
        self._agent_profile = config.agent_profile or "full"
        #: profile as explicitly configured (None = unset) — the `:ai`
        #: wizard only suggests `small` for Ollama when this is unset.
        self._configured_agent_profile = config.agent_profile
        # A runtime built from config.yaml at startup must seed the settings
        # snapshot so :model works without running the :ai wizard first.
        if agent_runtime is not None and config.agent_provider and config.agent_model:
            self._agent_settings = AgentSettings(
                provider=config.agent_provider,
                auth_method=config.agent_auth_method or "none",
                base_url=config.agent_base_url,
                model=config.agent_model,
                api_key_env=config.agent_api_key_env,
                profile=config.agent_profile or "full",
            )
        self._agent_task: asyncio.Task[None] | None = None
        # Serializes view/scope switches: keyboard NavigateCommands and the
        # agent's navigate tool share this handler, which yields while
        # stopping/starting watches — interleaving would corrupt state.
        self._nav_lock = asyncio.Lock()
        self.aliases: dict[str, ResourceMeta] = (
            aliases if aliases is not None else dict(_DEFAULT_ALIASES)
        )
        # Workspace panes (issue #48): each holds an independent view
        # (kind/scope/filter/drill); `current_kind` & co. delegate to the
        # focused pane so commands and keybindings target it naturally.
        self._panes: list[PaneState] = [PaneState("pods", config.namespace or "default")]
        self._focused_pane: int = 0
        #: Monotonic id source for split-pane table widgets: a survivor keeps
        #: its widget (and cursor/scroll state) when the other pane closes,
        #: so ids must stay unique across split/close cycles.
        self._pane_counter: int = 0
        #: Scope the metrics poller currently serves (None = stopped); a
        #: restart drops collected data, so equal targets are skipped.
        self._metrics_target: tuple[str | None] | None = None
        #: `ctrl+w` pressed, waiting for the chord's second key (v/w/q).
        self._pane_chord_pending: bool = False
        #: Validated `keybindings:` overrides (action → key), applied via the
        #: keymap in on_mount; the help overlay renders these keys (issue #35).
        self._keybinding_overrides: dict[str, str] = {}
        #: Active sort per view kind (issue #37): the choice survives watch
        #: updates (every render re-applies it) and switching views restores
        #: each kind's own sort. Lives in PaneState (see `_sorts` property).
        self._log_tasks: set[asyncio.Task[None]] = set()
        self._log_buffer: LogBuffer | None = None
        self._log_error: bool = False
        self._current_log_triples: list[tuple[str, str, str]] = []
        self._log_pane_gen: int = 0
        self._current_log_force_prefix: bool = False
        self._log_pane_mode: str = ""
        #: Pane whose selection opened the log pane: only that pane's
        #: navigation (or close) tears the stream down - the split workflow
        #: is watching one pane while tailing logs from the other.
        self._log_pane_owner: PaneState | None = None
        self._reconnect_sleep: float = 1.0
        self._ns_prefetch_task: asyncio.Task[None] | None = None
        self._ctx_prefetch_task: asyncio.Task[None] | None = None
        self._splash_shown_at: float = monotonic()
        self._log_buffer_max_lines: int = config.log_buffer_lines
        # Kinds with a table render already queued — coalesces the per-object
        # notifications of a LIST seed into a single rebuild (see _on_store_update).
        self._render_pending: set[str] = set()
        #: Outstanding drill pre-warm leases per (kind, scope) (issue #157):
        #: overlapping drills each hold one; only the last release may reap
        #: a stream no pane displays.
        self._prewarm_leases: dict[tuple[str, str], int] = {}
        # Rebuild inputs for an open HierarchyScreen: (title, refs, namespace,
        # scope). Store updates rebuild the tree in place while it is open.
        self._hierarchy_ctx: tuple[str, list[ComponentRef], str, str] | None = None
        # Cursor-placement poll budget for hierarchy goto (50ms per attempt);
        # an attribute so tests can shrink the give-up window.
        self._jump_poll_attempts: int = 200
        # Hint-strip lifecycle (issue #97 U3b): the controller owns the event
        # cache and the parked-cursor refresh timer; widget access and worker
        # scheduling stay here, injected as narrow callables.
        self._hints = HintController(
            find_pod_summary=self._find_pod_summary,
            cursor_row_key=self._cursor_row_key,
            on_pods_view=lambda: self.current_kind == "pods",
            get_events=lambda: self._get_events,
            show_trouble=self._hint_show_trouble,
            clear_hint=self._hint_clear,
            start_fetch=lambda coro: self.run_worker(coro, exclusive=True, group="hint-events"),
            set_timer=self.set_timer,
            ctx_epoch=lambda: self._ctx_epoch,
            ctx_crossed=self._ctx_switch_crossed,
        )

    # -- Focused-pane delegation (issue #48): the pane list is the single
    # source of view state; these properties keep the whole action surface
    # (and tests) working against "the view the user is focused on".

    @property
    def _pane(self) -> PaneState:
        return self._panes[self._focused_pane]

    @property
    def current_kind(self) -> str:
        return self._pane.kind

    @current_kind.setter
    def current_kind(self, value: str) -> None:
        self._pane.kind = value
        # The footer legend is view-scoped (issue #114): Textual cannot see
        # internal kind switches, so prompt it to re-evaluate check_action.
        self.refresh_bindings()

    @property
    def current_scope(self) -> str:
        return self._pane.scope

    @current_scope.setter
    def current_scope(self, value: str) -> None:
        self._pane.scope = value

    @property
    def filter_pattern(self) -> str:
        return self._pane.filter_pattern

    @filter_pattern.setter
    def filter_pattern(self, value: str) -> None:
        self._pane.filter_pattern = value

    @property
    def _resource_filter(self) -> ResourceFilter:
        """Parsed form of filter_pattern (issue #44); single matcher shared
        by the table render and the agent's view of "what the user sees"."""
        return self._pane.resource_filter

    @_resource_filter.setter
    def _resource_filter(self, value: ResourceFilter) -> None:
        self._pane.resource_filter = value

    @property
    def _sorts(self) -> dict[str, SortSpec]:
        """Per-kind sort state of the focused pane (view state, issue #37)."""
        return self._pane.sorts

    @property
    def _drill(self) -> NavigationStack:
        """Drill-down levels (deploy -> rs -> pods) of the focused pane."""
        return self._pane.drill

    @property
    def agent_runtime(self) -> AgentRuntime | None:
        """The live runtime — the :ai wizard may have replaced the initial
        one, so per-cluster retargeting (issue #36) must read it here."""
        return self._agent_runtime

    @property
    def current_namespace(self) -> str:
        """Alias for current_scope; kept for backward-compatible test access."""
        return self.current_scope

    @current_namespace.setter
    def current_namespace(self, value: str) -> None:
        self.current_scope = value

    def _focused_table(self) -> ResourceTable:
        return self.query_one(f"#{self._pane.table_id}", ResourceTable)

    # -- Typed root-widget accessors (issue #91 U2): these widgets are
    # composed once and stay mounted for the app's lifetime, so the hot
    # call sites read them through one named property each instead of
    # repeating raw `query_one(Class)` lookups. The accessors raise
    # `NoMatches` exactly like a raw query, so the intentional
    # startup/shutdown guards around them keep working unchanged.

    @property
    def _log_pane(self) -> LogPane:
        return self.query_one(LogPane)

    @property
    def _describe_pane(self) -> DescribePane:
        return self.query_one(DescribePane)

    @property
    def _command_bar(self) -> CommandBar:
        return self.query_one(CommandBar)

    @property
    def _filter_bar(self) -> FilterBar:
        return self.query_one(FilterBar)

    @property
    def _namespace_picker(self) -> NamespacePicker:
        return self.query_one(NamespacePicker)

    @property
    def _hint_strip(self) -> HintStrip:
        return self.query_one(HintStrip)

    @property
    def _status_bar(self) -> StatusBar:
        return self.query_one(StatusBar)

    @property
    def _agent_panel(self) -> AgentPanel:
        """Composed only when the agent is available; raises `NoMatches`
        otherwise, matching the guarded call sites' expectations."""
        return self.query_one(AgentPanel)

    def compose(self) -> ComposeResult:
        # The grouped top bar (issue #142) replaces the stock Footer at the
        # top: the key legend lives where users look first, grouped and
        # collapsible instead of one flat run of uniform keys.
        yield TopBar()
        yield SplashLogo()
        # The workspace hosts one or two side-by-side panes (issue #48);
        # pane 1 is composed here, pane 2 mounts on `ctrl+w v`.
        table = ResourceTable(id="pane-0")
        table.display = False  # hidden behind the splash until first data
        workspace = Horizontal(table, id="workspace")
        workspace.display = False
        yield workspace
        empty_state = Static(id="empty-state")
        empty_state.display = False  # hidden until the first store notification
        yield empty_state
        yield LogPane()
        yield DescribePane()
        if self._agent_available:
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
        """Every remappable app action mapped to its default keys.

        Bindings without a keymap ``id`` (the parametrised 1-9 favorites)
        are excluded: Textual's keymap moves bindings by id, so an
        id-less binding cannot actually be remapped.
        """
        actions: dict[str, tuple[str, ...]] = {}
        for raw in cls.BINDINGS:
            binding = raw if isinstance(raw, Binding) else Binding(*raw)
            if binding.id is None:
                continue
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
        # Id-less bindings (the 1-9 favorites) are not remappable, but their
        # keys stay reserved so an override cannot silently shadow them.
        reserved_keys = {binding.key: binding.action for binding in bindings if binding.id is None}
        plan = plan_keybindings(
            self.config.keybindings,
            self._binding_actions(),
            priority_actions,
            reserved_keys=reserved_keys,
        )
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
        # Snapshot the app-owned execution context (issue #165): on_mount
        # runs inside Textual's message pump, so this snapshot carries
        # `active_app` (and the pump ContextVars). AppUIBridge marshals
        # every foreign call - MCP requests, follow mirrors - onto a copy
        # of it, because composing a widget tree outside it raises
        # NoActiveAppError and terminates the app.
        self._app_context = contextvars.copy_context()
        # AUTO_FOCUS skips the hidden #workspace container: the table must
        # take initial focus explicitly or keys land on the CommandBar.
        self.query_one("#pane-0", ResourceTable).focus()
        # The top bar re-renders whenever the active bindings change (view
        # navigation, log pane open/close) - the same signal the stock
        # Footer subscribed to, so check_action stays the single source of
        # which keys are visible (issue #142).
        self.screen.bindings_updated_signal.subscribe(self, self._on_bindings_updated)
        self._refresh_top_bar()
        self._apply_keybindings()
        # Wire the `known` closure into CommandBar so parse_command can resolve aliases.
        command_bar = self._command_bar
        command_bar.known = lambda a: self._canonical_kind(a) if a in self.aliases else None
        command_bar.command_words = sorted(
            {*self.aliases, "ns", "namespaces", "ctx", "context", "contexts", "q", "quit"}
        )
        # Seed session-scoped log display settings from config (logs.wrap /
        # logs.timestamps); the w/t keys toggle them from there.
        log_pane = self._log_pane
        log_pane.wrap_lines = self.config.log_wrap
        log_pane.show_timestamps = self.config.log_timestamps
        if self._forwards is not None:
            # Liveness is the point of tracked forwards (issue #38): a toast
            # must fire when one breaks even while :pf is closed.
            self.set_interval(_FORWARD_POLL_SECONDS, self._poll_forwards)
        self._prefetch_namespaces()
        if self._list_contexts is not None:
            # Kubeconfig contexts feed the `:ctx` completion; a local file
            # read, but off-loop so a slow filesystem never blocks mount.
            list_contexts = self._list_contexts

            async def _fetch_contexts() -> None:
                names, _ = await asyncio.to_thread(list_contexts)
                self._command_bar.context_words = names

            self._ctx_prefetch_task = asyncio.create_task(_fetch_contexts())
        for warning in self.config.warnings:
            # Config problems (e.g. an invalid custom column) surface once at
            # startup instead of hiding in a log file (issue #45).
            self.notify(warning, title="Config warning", severity="warning")

        if self._proposal_store is not None:
            self._subscribe_proposal_updates(self._proposal_store)

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
        # Telepresence install hint (issue #159): fire-and-forget probe; a
        # failed or slow GET never delays startup.
        self.run_worker(self._maybe_hint_telepresence(), exclusive=False)

    #: Minimum time the startup splash stays visible in a real terminal.
    #: Skipped in headless (test) mode so Pilot tests see the table at once.
    SPLASH_MIN_SECONDS = 1.2

    def _dismiss_splash(self) -> None:
        try:
            splash = self.query_one(SplashLogo)
            workspace = self.query_one("#workspace")
            table = self.query_one("#pane-0", ResourceTable)
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
        workspace.display = True
        table.display = True

    def on_aliases_updated(self) -> None:
        """Refresh command autocompletion after background resource discovery."""
        try:
            command_bar = self._command_bar
        except Exception:
            return  # app is shutting down or not composed yet
        command_bar.command_words = sorted(
            {*self.aliases, "ns", "namespaces", "ctx", "context", "contexts", "q", "quit"}
        )
        # A kind discovered late can turn display-only tree nodes navigable.
        self._refresh_hierarchy()

    def on_resources_updated(self, message: ResourcesUpdated) -> None:
        self._render_pending.discard(message.kind)
        self._render_table(message.kind)
        self._refresh_hierarchy()

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
            self._command_bar.namespace_words = namespaces

        self._ns_prefetch_task = asyncio.create_task(_fetch())

    def _render_table(self, kind: str, *, only: PaneState | None = None) -> None:
        """Single choke point: every pane showing `kind` re-renders, and the
        empty-state stays in step (single-pane only - a split has its own
        per-pane content as guidance).

        `only` restricts the render to the initiating pane: view-state
        changes (filter, navigation) must not repaint the other pane -
        `show()` clears and rebuilds, resetting its cursor/scroll. Store
        and metrics updates fan out to every pane (data really changed).
        """
        # First store notification: replace the startup splash with real content.
        self._dismiss_splash()
        for pane in self._panes:
            if pane.kind != kind or (only is not None and pane is not only):
                continue
            try:
                table = self.query_one(f"#{pane.table_id}", ResourceTable)
            except NoMatches:
                return  # shutdown race: a queued render after widgets are removed
            self._render_pane(kind, pane, table, empty_state=len(self._panes) == 1)

    def _render_pane(
        self, kind: str, pane: PaneState, table: ResourceTable, *, empty_state: bool
    ) -> None:
        rows = self.store.get(kind, pane.scope)
        drill_uid = pane.drill.parent_uid
        if drill_uid is not None and kind == pane.drill.child_kind:
            rows = [r for r in rows if owned_by(r, drill_uid)]
        rows = self._filtered_rows(rows, pane.resource_filter)
        all_namespaces = pane.scope == ALL_NAMESPACES
        metrics = None
        if kind == "pods" and self._metrics is not None and self._metrics.available:
            metrics = self._metrics.get
        # Dispatch rendering on the resolved meta: a group-qualified view
        # kind (alias collision) must still get its typed table, and the
        # serving group scopes group-specific renderings (the OLM tables).
        meta = self.aliases.get(kind)
        plural = meta.plural if meta is not None else kind
        table.show(
            plural,
            rows,
            all_namespaces=all_namespaces,
            # Filtering happened upstream (issue #44: labels/regex/fuzzy need
            # the full summaries, not just names) — no name pattern remains.
            pattern="",
            metrics=metrics,
            group=meta.group if meta is not None else "",
            sort=pane.sorts.get(kind),
            view=self.config.views.get(plural),
        )
        if empty_state:
            self._refresh_empty_state(kind, table.row_count)
        # The strip is driven by RowHighlighted on the pods view; anything
        # else (view switch, table now empty) must not leave a stale hint.
        if pane is self._pane and (kind != "pods" or table.row_count == 0):
            with contextlib.suppress(NoMatches):  # shutdown race, same as the table guard
                self._hint_strip.clear_hint()

    def on_show_error(self, message: ShowError) -> None:
        self.notify(message.detail, title=message.title, severity="error")

    def action_help(self) -> None:
        """Open the help overlay generated from the live binding lists (issue #41)."""
        overrides = self._keybinding_overrides
        handler_keys = [
            (group, overrides.get(action, key) if action else key, description)
            for group, key, description, action in self.HANDLER_KEY_HELP
        ]
        # The static BINDINGS list bypasses check_action, so drop entries
        # whose action is unavailable in this composition (e.g. Ctrl-A
        # without the [agent] extra, issue #73). View-gated actions stay:
        # the overlay documents every view, not just the current one.
        app_bindings = [
            binding
            for binding in (
                raw if isinstance(raw, Binding) else Binding(*raw) for raw in self.BINDINGS
            )
            if self._action_available(binding.action)
        ]
        groups = collect_help(
            app_bindings,
            list(DescribeScreen.BINDINGS),
            handler_keys=handler_keys,
            overrides=overrides,
        )
        self.push_screen(
            HelpScreen(groups, command_help(telepresence=self._telepresence is not None))
        )

    def action_open_command(self) -> None:
        # Dismiss the filter bar first so no invisible filter stays active.
        self._filter_bar.dismiss_bar()
        self._command_bar.open()

    def action_open_filter(self) -> None:
        # When the describe pane is open, / searches inside it (issue #42).
        describe_pane = self._describe_pane
        if describe_pane.display:
            describe_pane.open_search()
            return
        # When the log pane is open, / opens the pane's inline search instead.
        log_pane = self._log_pane
        if log_pane.display:
            log_pane.open_search()
            return
        # Dismiss the command bar first to enforce mutual exclusion.
        self._command_bar.dismiss_bar()
        self._filter_bar.open()

    def on_filter_command(self, message: FilterCommand) -> None:
        self.filter_pattern = message.pattern
        self._resource_filter = parse_filter(message.pattern)
        self._render_table(self.current_kind, only=self._pane)
        self._refresh_status()

    def on_clear_filter(self, message: ClearFilter) -> None:
        self.filter_pattern = ""
        self._resource_filter = parse_filter("")
        self._render_table(self.current_kind, only=self._pane)
        self._refresh_status()

    def _filtered_rows(
        self, rows: list[Summary], resource_filter: ResourceFilter | None = None
    ) -> list[Summary]:
        """Apply the active filter the way the table renders it (issue #44)."""
        flt = resource_filter if resource_filter is not None else self._resource_filter
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
        # The stack is bound *now*: a focus flip while this waits for the
        # lock must not redirect the clear to another pane's drill.
        pane = self._pane
        drill = pane.drill

        def _abandon() -> None:
            drill.clear()
            # Walking away also drops this pane's pending hierarchy-tree
            # return (issue #135) - Escape afterwards must not teleport
            # back. Per-pane state: other panes' returns are untouched.
            pane.hierarchy_return = None

        await self._navigate(message.view, message.namespace, drill_op=_abandon)

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
        # Capture the pane identity before waiting on the lock: the user may
        # switch pane focus while this navigation queues behind another, or
        # during `_navigate_locked`'s watch/log/metrics awaits - the
        # transition (and drill_op, which callers bind to the same pane's
        # stack at call time) must land in the pane that initiated it.
        pane = self._pane
        async with self._nav_lock:
            if pane not in self._panes:
                return  # the initiating pane was closed while queued
            if drill_op is not None:
                drill_op()
            await self._navigate_locked(pane, view, self._default_scope_for(view, namespace))
        self._render_table(pane.kind, only=pane)
        self._refresh_status()

    async def _navigate_locked(
        self, pane: PaneState, view: str | None, namespace: str | None
    ) -> None:
        """Kind/scope transition body; caller must hold ``_nav_lock``."""
        # Advance the pane's navigation generation first: a queued drill
        # revalidating after its pre-warm must observe this command even
        # when the kind/scope tuple ends up unchanged.
        pane.nav_gen += 1
        # A describe pane covering the table would show a stale manifest
        # over the new view — dismiss it on any navigation, even when the
        # requested kind/scope already matches.
        self._describe_pane.hide()
        new_kind = view if view is not None else pane.kind
        new_scope = namespace if namespace is not None else pane.scope
        if new_kind != pane.kind or new_scope != pane.scope:
            if self._log_pane_owner is pane:
                # Only the owning pane's navigation closes the logs: the
                # other pane must keep its stream (issue #48 workflow).
                await self._close_log_pane()
            old = (pane.kind, pane.scope)
            # Another pane may still be watching the old (kind, scope):
            # stopping it would freeze that pane's view (issue #48).
            others = {(p.kind, p.scope) for p in self._panes if p is not pane}
            if old not in others and self._prewarm_leases.get(old, 0) == 0:
                # An outstanding drill pre-warm lease keeps the stream alive
                # (issue #157): killing it here would force that drill's own
                # navigate to re-LIST into the empty flash. The last lease
                # release reaps it if no pane ends up displaying it.
                await self.watch_manager.stop(*old)
            pane.kind = new_kind
            pane.scope = new_scope
            # The footer legend follows the focused pane's kind (issue #114).
            self.refresh_bindings()
            await self.watch_manager.start(new_kind, new_scope)
            await self._sync_metrics_poller()

    async def _sync_metrics_poller(self) -> None:
        """Poll metrics only while a pods view is on screen, in its scope.

        metrics.k8s.io has no watch support, so this poller is the one
        recurring request the app makes - stopping it off the pods view
        keeps background load at zero for other kinds. With a split
        workspace the poller serves every pod pane, not just the focused
        one; two pod panes in different scopes poll cluster-wide so
        neither goes stale. A restart drops collected data, so a target
        the poller already serves is left running untouched.
        """
        if self._metrics is None:
            return
        scopes = {p.scope for p in self._panes if p.kind == "pods"}
        if not scopes:
            if self._metrics_target is not None:
                self._metrics_target = None
                await self._metrics.stop()
            return
        scope = scopes.pop() if len(scopes) == 1 else ALL_NAMESPACES
        namespace = None if scope == ALL_NAMESPACES else scope
        target = (namespace,)
        if target == self._metrics_target:
            return
        self._metrics_target = target
        await self._metrics.start(namespace)

    async def action_toggle_all_namespaces(self) -> None:
        """Toggle scope between ALL_NAMESPACES and the config-default namespace.

        Routed through the locked navigate handler so it serializes with
        agent-driven navigation (both stop/start watches across awaits).
        """
        if self.current_scope == ALL_NAMESPACES:
            new_scope = self.config.namespace or "default"
        else:
            if not await self._cluster_list_permitted():
                return  # notified inside; stay in the current namespace
            new_scope = ALL_NAMESPACES
        await self.on_navigate_command(NavigateCommand(None, new_scope))

    async def action_favorite_namespace(self, index: int) -> None:
        """Jump to `favorite_namespaces[index-1]` (issue #108, keys 1-9).

        A favorite is a UI-only shortcut: it uses the exact same navigation
        path as `:ns <name>` — no access is granted, no namespace list is
        derived, and a forbidden watch reports its own concise notice.
        """
        favorites = self.config.favorite_namespaces
        if index > len(favorites):
            return
        await self.on_navigate_command(NavigateCommand(None, favorites[index - 1]))

    async def _cluster_list_permitted(self) -> bool:
        """All-namespaces guard (issue #108): a forbidden cluster-wide LIST
        would only loop error cards — SSAR-check first and stay put with an
        inline notice instead. Checked fresh on every `0` press so a later
        RBAC grant is observed; SSAR failures pass through (fail-open; the
        watch reports real errors on its own)."""
        if self._check_permission is None:
            return True
        meta = self.aliases.get(self.current_kind)
        if meta is None:
            return True  # unknown kind: the watch reports its own error
        if meta.synthetic:
            if meta.backing is None:
                return True  # nothing to probe
            plural, group = meta.backing  # e.g. helm views LIST Secrets
        else:
            plural, group = meta.plural, meta.group
        try:
            allowed = await asyncio.wait_for(
                self._check_permission("list", plural, "", None, group, ""),
                timeout=_PERMISSION_CHECK_TIMEOUT,
            )
        except Exception:
            logger.debug("cluster-wide list pre-check failed; allowing", exc_info=True)
            return True
        if not allowed:
            self.notify(
                f"Cluster-wide {plural} list forbidden — staying in"
                f" {self.current_scope!r}. Press 0 again after access is"
                " granted, or switch namespaces with `:ns <name>`.",
                severity="warning",
            )
        return allowed

    async def on_show_namespace_picker(self, message: ShowNamespacePicker) -> None:
        if self._list_namespaces is None:
            self.notify("Namespace listing unavailable", severity="warning")
            return
        # The listing would race the client swap and could return either
        # cluster's namespaces — refuse up front.
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch
        try:
            namespaces = await self._list_namespaces()
        except ApiStatusError as exc:  # API failures get the actionable mapping (§5-5)
            if self._ctx_switch_crossed(epoch):
                return  # a stale old-cluster error is not worth surfacing
            self._handle_namespace_list_error(exc)
            return
        except Exception as exc:  # surface any other listing failure to the user
            if self._ctx_switch_crossed(epoch):
                return
            self.notify(str(exc), title="Failed to list namespaces", severity="error")
            return
        if self._ctx_switch_crossed(epoch):
            # The listing awaited through a :ctx switch: opening the picker
            # now would offer old-cluster namespaces to the new session.
            self.notify(
                "Namespace picker cancelled - the kube context changed",
                severity="warning",
            )
            return
        if not namespaces:
            self.notify("No namespaces visible (check RBAC)", severity="warning")
            return
        self._command_bar.namespace_words = namespaces
        self._namespace_picker.open(namespaces)

    def _ctx_switch_crossed(self, epoch: int) -> bool:
        """True when a :ctx switch started or completed since *epoch* was taken."""
        return self._ctx_switching or epoch != self._ctx_epoch

    def _ctx_reads_allowed(self) -> bool:
        """Refuse read actions that spawn cluster streams during a :ctx switch.

        Streams started mid-swap would attach to whichever cluster wins the
        switch while still labeled with the old selection (issue #84).
        Returns True when it is safe to proceed.
        """
        if self._ctx_switching:
            self.notify(
                "A context switch is in progress — try again once it completes",
                severity="warning",
            )
            return False
        return True

    def _handle_namespace_list_error(self, exc: ApiStatusError) -> None:
        """403 is an authorization boundary (issue #108): show one concise
        permission notice pointing at `:ns <name>` free-text entry — never
        manufacture a namespace list from configuration."""
        msg = explain_api_error(exc.status, exc.reason, "namespaces", None)
        if exc.status == 403:
            msg += " Switch directly with `:ns <name>`."
        self.notify(msg, title="Failed to list namespaces", severity="error")

    # ------------------------------------------------------------------
    # `:ctx` — runtime context switching (issue #36)
    # ------------------------------------------------------------------

    def on_show_context_picker(self, message: ShowContextPicker) -> None:
        self.run_worker(self._show_context_picker(), exclusive=False)

    def on_switch_context_command(self, message: SwitchContextCommand) -> None:
        self.run_worker(self._switch_context_flow(message.name), exclusive=False)

    _CURRENT_CTX_SUFFIX = " (current)"

    async def _show_context_picker(self) -> None:
        if self._list_contexts is None:
            self.notify("Context switching unavailable in this build", severity="warning")
            return
        names, active = await asyncio.to_thread(self._list_contexts)
        if not names:
            self.notify("No contexts found in kubeconfig", severity="warning")
            return
        self._command_bar.context_words = names
        # Sessions started from the kubeconfig current-context have no
        # explicit config value — fall back to what the kubeconfig reports.
        current = self.config.kube_context or active
        # Explicit display->name mapping: decoding the label (suffix strip)
        # would corrupt a real context whose name ends in " (current)".
        labels: dict[str, str] = {}
        for n in names:
            label = f"{n}{self._CURRENT_CTX_SUFFIX}" if n == current else n
            if label != n and (label in names or label in labels):
                label = n  # marker collides with another context's name
            labels[label] = n

        def _on_pick(choice: str | None) -> None:
            if choice is None:
                return
            self.post_message(SwitchContextCommand(labels.get(choice, choice)))

        self.push_screen(PickScreen("Switch context:", list(labels)), _on_pick)

    async def _switch_context_flow(self, name: str) -> None:
        """Orchestrate a context switch: guards, auth probe, teardown, swap.

        The probe runs against a private client configuration first — on any
        failure nothing has been torn down and the old context keeps working
        (issue #36's "don't strand the user" requirement). Only a proven
        target proceeds to teardown and retarget.
        """
        if self._probe_context is None or self._switch_context is None:
            self.notify("Context switching unavailable in this build", severity="warning")
            return
        # Claim before the first await: two queued SwitchContextCommands
        # must not both pass the guards and race the teardown.
        if self._ctx_switching:
            self.notify("A context switch is already in progress", severity="warning")
            return
        self._ctx_switching = True
        try:
            await self._switch_context_locked(name)
        finally:
            self._ctx_switching = False

    async def _switch_context_locked(self, name: str) -> None:
        """The body of `_switch_context_flow`; runs with the claim held."""
        old = self.config.kube_context
        if await self._is_ctx_noop(name, old):
            self.notify(f"Already on context {name}")
            return
        if not await self._ctx_switch_guards_pass(name):
            return
        try:
            await self._probe_context(name)  # type: ignore[misc]  # guarded by caller
        except Exception as exc:
            self.notify(
                f"Cannot switch to context {name!r}: {self._describe_ctx_error(exc)}"
                f" — staying on {old or 'the current context'}",
                severity="error",
                timeout=10,
            )
            return
        async with self._nav_lock:
            # The probe awaited network I/O — an agent turn or a dialog may
            # have started meanwhile; re-check before anything is torn down.
            blocker = self._ctx_switch_blocker()
            if blocker is not None:
                self.notify(blocker, severity="warning")
                return
            # Quiesce the embedded MCP server BEFORE any teardown: external
            # callers share the client and alias map being swapped, and an
            # undrainable server must abort while the old context is still
            # fully usable (watches, forwards, store all intact).
            mcp_restart = await self._quiesce_mcp_for_switch()
            if mcp_restart is None:
                return
            # Old-context proposals are stale the moment this committed
            # transition begins: the old MCP run (and its capability) is
            # already stopped, and both the teardown below and the retarget
            # perform fallible awaits — expire them now, not at a later
            # point that an exception could keep from ever being reached.
            await self._expire_proposals_audited("kube context switched")
            await self._teardown_for_context_switch()
            ok, applied = await self._retarget_context(name, old)
            if not ok:
                if mcp_restart:
                    self.notify(
                        "Embedded MCP server was stopped for the switch —"
                        " restart it with :mcp on once reconnected",
                        severity="warning",
                        timeout=15,
                    )
                return
            if mcp_restart and self._mcp is not None:
                # Resume on the same endpoint, now serving whichever context
                # was actually applied (target, or the restored old one).
                msg = await self._mcp.start()
                self.notify(msg, severity="error" if msg.startswith("ERROR") else "information")
            await self.watch_manager.start(self.current_kind, self.current_scope)
            await self._sync_metrics_poller()
        self.post_message(ResourcesUpdated(self.current_kind))
        self._refresh_status()
        self._prefetch_namespaces()
        self.on_aliases_updated()
        if applied == name:
            self.notify(f"Switched to context {name} (ns: {self.current_scope})")

    async def _quiesce_mcp_for_switch(self) -> bool | None:
        """Drain and stop the embedded MCP server ahead of a context switch.

        Returns True when a restart is owed after the switch, False when the
        server was not running, and None when the server could not be drained
        in time — the switch must then abort with nothing torn down.
        """
        if self._mcp is None or not self._mcp.running:
            return False
        pending = await self._mcp.shutdown()
        if pending is not None:
            # Even cancellation didn't land within its deadline: an in-flight
            # tool call could cross the context boundary if we proceeded.
            self.notify(
                "Embedded MCP server did not stop in time — context"
                " switch aborted (old context untouched)",
                severity="error",
                timeout=10,
            )
            return None
        return True

    async def _is_ctx_noop(self, name: str, old: str | None) -> bool:
        """True when *name* is already the active context — explicitly, or as
        the kubeconfig's active context for sessions started without
        -c/--context (old stays None there for the recovery path)."""
        effective = old
        if effective is None and self._list_contexts is not None:
            with contextlib.suppress(Exception):
                _, effective = await asyncio.to_thread(self._list_contexts)
        return name == effective

    def _ctx_switch_blocker(self) -> str | None:
        """Why a switch cannot proceed right now, or None when it can."""
        if self._agent_task is not None and not self._agent_task.done():
            return "Agent is busy — wait for the current turn to finish before switching contexts"
        if self._active_cluster_writes:
            return (
                "A cluster write is in progress — wait for it to finish before switching contexts"
            )
        if len(self.screen_stack) > 1:
            return "Close open dialogs before switching contexts"
        try:
            # The inline namespace picker is not a screen: its old-cluster
            # options would survive teardown and a later selection would
            # navigate the new cluster to a namespace picked from the old.
            if self._namespace_picker.display:
                return "Close the namespace picker before switching contexts"
        except NoMatches:  # widget tree not composed (shutdown/startup)
            pass
        return None

    async def _ctx_switch_guards_pass(self, name: str) -> bool:
        """Pre-probe refusals; each states why the switch cannot start now."""
        blocker = self._ctx_switch_blocker()
        if blocker is not None:
            self.notify(blocker, severity="warning")
            return False
        if self._list_contexts is not None:
            names, _ = await asyncio.to_thread(self._list_contexts)
            if names and name not in names:
                self.notify(
                    f"Unknown context {name!r} — kubeconfig has: {', '.join(names)}",
                    severity="error",
                )
                return False
        return True

    @staticmethod
    def _describe_ctx_error(exc: Exception) -> str:
        if isinstance(exc, TimeoutError):
            return "authentication check timed out"
        return str(exc) or type(exc).__name__

    async def _teardown_for_context_switch(self) -> None:
        """Stop every consumer of the old cluster before the client swaps.

        Order matters: streams and pollers first (they hold the old
        connection), then session state that would otherwise leak old-cluster
        rows, breadcrumbs, or hints into the new one.
        """
        await self._close_log_pane()
        self._describe_pane.hide()
        # Every pane's kind/scope/filter/drill describes the old cluster: a
        # surviving second pane would keep stale-but-actionable rows on
        # screen, so the switch collapses the split back to a single view.
        await self._collapse_split()
        # An old-cluster namespace prefetch still in flight could land after
        # the new cluster's and overwrite its completions — cancel it first.
        if self._ns_prefetch_task is not None:
            self._ns_prefetch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ns_prefetch_task
            self._ns_prefetch_task = None
        # Completions that already loaded are old-cluster names — drop them
        # now so they aren't offered while (or if) the new prefetch fails.
        self._command_bar.namespace_words = []
        if self._metrics is not None:
            await self._metrics.stop()
            # The poller is gone: drop the served-target cache too, or a
            # same-namespace switch would look already-served to
            # _sync_metrics_poller and metrics would never restart.
            self._metrics_target = None
        await self.watch_manager.stop_all()
        if self._forwards is not None:
            # Same quiesce-stop-audit sequence as app exit: in-flight
            # launches land first, stop_all runs off-loop (it polls up to
            # the grace deadline), and every stop is enqueued for audit.
            stopped = await self._teardown_forwards(self._forwards)
            if stopped:
                self.notify(f"Stopped {len(stopped)} port-forward(s) targeting the old cluster")
        # Old-cluster audit entries resolve their context only at append();
        # flush them before _apply_context_switch re-points the audit log,
        # or they would be written as belonging to the new cluster.
        worker = self._forward_audit_worker
        if worker is not None and not worker.is_finished:
            with contextlib.suppress(Exception):
                await worker.wait()
        await self._drain_forward_audits()
        self._drill.clear()
        self.store.clear_all()
        # The hint-events worker holds the old client and its exception path
        # re-populates the cache — cancel it (and the parked-cursor refresh
        # timer) before the cache is cleared, so no late result or retry can
        # resurrect old-cluster hints.
        self.workers.cancel_group(self, "hint-events")
        self._hints.teardown()
        self.filter_pattern = ""
        self._resource_filter = parse_filter("")

    async def _retarget_context(self, name: str, old: str | None) -> tuple[bool, str | None]:
        """Swap the connection to *name*; on failure fall back to *old*.

        Returns ``(ok, applied)``: ``ok`` is False only when even the
        fallback failed (the session then needs a restart — everything is
        already torn down and nothing is connected). ``applied`` is the
        context actually in effect, which may legitimately be None (the
        kubeconfig default) — that is why success is a separate flag.

        Old-context proposals were already expired by the caller (right
        after MCP quiescing), before teardown or either switch attempt.
        """
        try:
            result = await self._switch_context(name)  # type: ignore[misc]  # guarded by caller
            self._apply_context_switch(name, old, result)
            return True, name
        except Exception as exc:
            self.notify(
                f"Context switch to {name!r} failed mid-swap: {self._describe_ctx_error(exc)}",
                severity="error",
                timeout=10,
            )
        try:
            result = await self._switch_context(old)  # type: ignore[misc]  # guarded by caller
            self._apply_context_switch(old, old, result)
            self.notify(f"Restored context {old or '(kubeconfig default)'}")
            return True, old
        except Exception as exc:
            self.notify(
                f"Could not restore context {old or '(kubeconfig default)'}:"
                f" {self._describe_ctx_error(exc)} — restart korvid",
                severity="error",
                timeout=15,
            )
            return False, None

    def _apply_context_switch(
        self, name: str | None, old: str | None, result: ContextSwitchResult
    ) -> None:
        """Adopt the new cluster's identity and re-probed capabilities."""
        self._ctx_epoch += 1
        # Adopt the target context's kubeconfig namespace as the session
        # default too: `ns` toggle-back and the helm/operator namespace
        # fallbacks read config.namespace, and jumping to the *startup*
        # context's namespace after a switch would cross clusters.
        self.config = dataclasses.replace(
            self.config,
            kube_context=name,
            namespace=result.context_namespace or self.config.namespace,
        )
        self._pod_resize_supported = result.pod_resize_supported
        self._provider_hint = result.provider_hint
        self._protected_context = result.protected_context
        if result.protected_context is not None:
            self.notify(
                f"Context {result.protected_context!r} is protected — writes"
                " require typing the context name",
                severity="warning",
                timeout=10,
            )
        if self._audit is not None:
            self._audit.set_context(name)
        if self._forwards is not None:
            # Reopen the registry that _teardown_forwards latched closed;
            # forwards started from now on target the new cluster.
            self._forwards.retarget(name)
            self._forwards_closing = False
        self.current_kind = "pods"
        self.current_scope = self.config.namespace or "default"
        # Rebind the helm wrapper: it pins --kube-context per instance, and
        # helm writes must follow the active cluster (None when helm is off).
        self._helm = result.helm
        # The new cluster may run a telepresence traffic-manager the old one
        # lacked: re-probe (a no-op once the session's hint was shown).
        self.run_worker(self._maybe_hint_telepresence(), exclusive=False)
        if name != old:
            self._ctx_switch_note = (
                f"kube context switched from {old or '(default)'} to {name};"
                " all cluster state was reset"
            )

    def on_quit_command(self, message: QuitCommand) -> None:
        self.exit()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Cursor movement drives the ops hint strip (pods view only)."""
        if not isinstance(event.data_table, ResourceTable):
            return
        if event.data_table.id != self._pane.table_id:
            # Highlight from the non-focused pane (e.g. a watch-driven
            # re-render moving its cursor): the hint strip reflects the
            # focused pane's selection only.
            return
        if self.current_kind != "pods" or event.row_key is None:
            self._hint_clear()
            return
        self._hints.show_for_row(str(event.row_key.value))

    def _hint_show_trouble(
        self, trouble: tuple[ContainerTrouble, ...], *, event: str | None = None
    ) -> None:
        """Strip adapter for the hint controller (widget access stays here).

        A render dispatched during shutdown/teardown can arrive after the
        strip is unmounted; nothing to render then.
        """
        with contextlib.suppress(NoMatches):
            self._hint_strip.show_trouble(trouble, event=event)

    def _hint_clear(self) -> None:
        """Strip adapter for the hint controller (widget access stays here)."""
        with contextlib.suppress(NoMatches):  # strip unmounted during shutdown/teardown
            self._hint_strip.clear_hint()

    def _cursor_row_key(self) -> str | None:
        """Row key under the table cursor, or None (empty table / no cursor)."""
        try:
            table = self._focused_table()
        except NoMatches:  # timer fired while the app is shutting down
            return None
        if table.cursor_row < 0:
            return None
        try:
            key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        except CellDoesNotExist:
            return None
        return None if key is None else str(key.value)

    def _find_pod_summary(self, row_key: str) -> PodSummary | None:
        parts = row_key.split("/", 1)
        if len(parts) != 2:
            return None
        namespace, name = parts
        for obj in self.store.get("pods", self.current_scope):
            if obj.namespace == namespace and obj.name == name and isinstance(obj, PodSummary):
                return obj
        return None

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
        if summary is None or not pod_needs_hint(summary):
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
        if not pod_needs_hint(fresh):
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
        """Enter drills down: pods -> containers; kinds with a
        registered ownership child (deploy -> rs -> pods) push a drill level."""
        if not isinstance(event.data_table, ResourceTable):
            return
        if self.current_kind != "pods":
            if self._hierarchy_root_kind() is not None:
                # Helm release / OLM Subscription / CSV: Enter opens the
                # component hierarchy tree (issue #120).
                event.stop()
                parts = str(event.row_key.value).split("/", 1)
                if len(parts) == 2:
                    self.run_worker(
                        self._open_hierarchy(parts[0], parts[1]),
                        exclusive=True,
                        group="hierarchy",
                    )
                return
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
        await self._open_containers_screen(parts[0], parts[1])

    async def _open_containers_screen(self, namespace: str, name: str) -> None:
        """Push the containers screen for a pod; shell/logs run per pick.

        The row fetch and the open screen both span awaited gaps, so the
        context epoch captured here cancels stale picks: a shell or log
        stream started after a completed switch would target the new cluster
        with the old cluster's pod selection.
        """
        epoch = self._ctx_epoch
        rows = await self._build_container_rows(namespace, name)
        if not rows:
            self.notify("No containers found for this pod", severity="warning")
            return
        if self._ctx_switching or epoch != self._ctx_epoch:
            # The row fetch awaited through a context switch: the selection
            # belongs to the old cluster.
            return

        def _on_pick(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            if self._ctx_switching or epoch != self._ctx_epoch:
                self.notify(
                    f"container action on {name} cancelled - the kube context"
                    " changed while the containers screen was open",
                    severity="warning",
                )
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

    #: Longest a drill transition waits for the target view's initial LIST
    #: before switching anyway (issue #157). A slow cluster degrades to the
    #: old switch-then-fill behavior, never worse.
    DRILL_PREWARM_TIMEOUT = 1.0

    async def _prewarm_view(
        self,
        kind: str,
        scope: str,
        ready: Callable[[list[Summary]], bool],
    ) -> None:
        """Warm the drill target before the pane switches (issue #157).

        Starting the watch for a kind no pane displays renders nowhere, so
        the LIST happens invisibly while the current view stays up; the
        bounded wait ends as soon as `ready` sees the expected rows. The
        subsequent `_navigate_locked` start() is then a no-op (the watch is
        already running), the bucket is warm, and the single post-switch
        render lands with real rows instead of flashing an empty table.

        A pane-backed watch (a split pane displays this kind/scope) is
        already warm - restarting it would clear the bucket it serves, so
        both restart and wait are skipped. A watch that is merely *active*
        may be another drill's in-flight pre-warm whose LIST has not landed:
        each caller waits on its own readiness, and the lease count makes
        `_stop_watch_if_unused` reap the stream only when the last pre-warm
        released it.
        """
        key = (kind, scope)
        self._prewarm_leases[key] = self._prewarm_leases.get(key, 0) + 1
        # Pane-backed *and* live: a pane's watch mid-teardown (a concurrent
        # navigation awaiting stop()) leaves the pane tuple unchanged while
        # the stream is already gone - skipping then would recreate the
        # empty flash. Require the watch itself.
        if any((p.kind, p.scope) == key for p in self._panes) and key in self.watch_manager.active:
            return
        await self.watch_manager.start(kind, scope)
        deadline = monotonic() + self.DRILL_PREWARM_TIMEOUT
        with self._progress(f"loading {kind}"):
            while monotonic() < deadline:
                if ready(self.store.get(kind, scope)):
                    return
                await asyncio.sleep(0.03)

    async def _stop_watch_if_unused(self, kind: str, scope: str) -> None:
        """Release one pre-warm lease; reap the stream when it was the last
        lease and no pane displays the (kind, scope) (issue #157): a drill
        that lost its pane (or its race) must not leak a watch, and must
        not stop one a concurrent pre-warm or pane still relies on."""
        key = (kind, scope)
        remaining = self._prewarm_leases.get(key, 0) - 1
        if remaining > 0:
            self._prewarm_leases[key] = remaining
            return
        self._prewarm_leases.pop(key, None)
        if all((p.kind, p.scope) != key for p in self._panes):
            await self.watch_manager.stop(kind, scope)

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
        # Capture before waiting on the lock: focus may move (or the pane may
        # close) while this drill queues behind another navigation.
        pane = self._pane
        # Staleness anchors (review on #160): the pre-warm below can wait up
        # to a second, so a newer :view/:ns/:ctx may land first. The drill
        # was issued against *this* view in *this* cluster - anything else
        # under the lock means the newer command wins and the drill abandons.
        origin = (pane.kind, pane.scope)
        epoch = self._ctx_epoch
        nav_gen = pane.nav_gen
        # Warm the child view first (issue #157): wait - bounded - until the
        # rows this drill will show exist, so the switch renders once with
        # content instead of flashing an empty table while the LIST runs.
        # Inside the try: a cancellation mid-pre-warm must still release the
        # lease (the acquire is synchronous before the first await, so the
        # finally never releases a lease that was not taken).
        prewarm_scope = pane.scope
        try:
            await self._prewarm_view(
                child,
                prewarm_scope,
                lambda rows: any(owned_by(r, uid) for r in rows),
            )
            async with self._nav_lock:
                if pane not in self._panes:
                    # An accurate outcome (review on #160): a None here reads
                    # as success to agent_drill_down, which would report a
                    # drill that never happened.
                    return "the pane closed while preparing the drill — drill abandoned"
                if (
                    (pane.kind, pane.scope) != origin
                    or pane.nav_gen != nav_gen
                    or self._ctx_switching
                    or epoch != self._ctx_epoch
                ):
                    return (
                        "the view changed while preparing the drill — drill abandoned "
                        "(the newer navigation takes priority)"
                    )
                pane.drill.push(level)
                try:
                    await self._navigate_locked(pane, child, None)
                except BaseException:
                    pane.drill.pop()
                    raise
        finally:
            # No-op when the navigation landed (the pane now displays the
            # warmed kind/scope); reaps the stream when the drill lost its
            # pane or raced a scope change.
            await self._stop_watch_if_unused(child, prewarm_scope)
        self._render_table(pane.kind, only=pane)
        self._refresh_status()
        return None

    async def _pop_drill(self) -> bool:
        """Pop one drill level and navigate back to its parent kind as one
        transaction under the navigation lock. Returns False when the stack
        was empty (nothing to pop)."""
        # Capture before waiting on the lock: focus may move (or the pane may
        # close) while this pop queues behind another navigation.
        pane = self._pane
        peeked = pane.drill.peek()
        if peeked is None:
            return False
        # Staleness anchors (review on #160): same rule as the push side -
        # the Esc was issued against this view in this cluster.
        origin = (pane.kind, pane.scope)
        epoch = self._ctx_epoch
        nav_gen = pane.nav_gen
        # Warm the parent view first (issue #157): its watch was stopped
        # when we drilled away, so navigating straight back would re-LIST
        # into an empty flash. Readiness is what the post-pop view will
        # actually show: a remaining drill level keeps filtering by its
        # parent UID (pods -> replicasets keeps the deployment filter), so
        # an unrelated row must not satisfy the wait; only a pop back to
        # the root accepts any row.
        under = pane.drill.copy()
        under.pop()
        uid_after = under.parent_uid
        if uid_after is None:
            ready: Callable[[list[Summary]], bool] = bool
        else:

            def ready(rows: list[Summary]) -> bool:
                return any(owned_by(r, uid_after) for r in rows)

        prewarm_scope = pane.scope
        try:
            await self._prewarm_view(peeked.parent_kind, prewarm_scope, ready)
            async with self._nav_lock:
                if pane not in self._panes:
                    return False  # the initiating pane was closed while queued
                if (
                    (pane.kind, pane.scope) != origin
                    or pane.nav_gen != nav_gen
                    or self._ctx_switching
                    or epoch != self._ctx_epoch
                    or pane.drill.peek() is not peeked
                ):
                    # A newer navigation landed during the pre-warm: it wins.
                    # Consume the Esc (True) so it does not cascade into the
                    # hierarchy-return fallback against the changed view.
                    return True
                popped = pane.drill.pop()
                if popped is None:
                    return False
                await self._navigate_locked(pane, popped.parent_kind, None)
        finally:
            await self._stop_watch_if_unused(peeked.parent_kind, prewarm_scope)
        self._render_table(pane.kind, only=pane)
        self._refresh_status()
        return True

    def _hierarchy_root_kind(self) -> str | None:
        """The current view's hierarchy root kind ("HelmRelease",
        "Subscription", "ClusterServiceVersion"), or None when the view has
        no component tree (issue #120). Helm requires the components
        accessor; without it Enter falls back to the revision drill."""
        meta = self.aliases.get(self._canonical_kind(self.current_kind))
        if meta is None:
            return None
        ident = (meta.group, meta.plural)
        if ident == (HELM_RELEASES_META.group, HELM_RELEASES_META.plural):
            return "HelmRelease" if self._get_helm_components is not None else None
        if ident == (OPERATORS_GROUP, "subscriptions"):
            return "Subscription"
        if ident == (OPERATORS_GROUP, "clusterserviceversions"):
            return "ClusterServiceVersion"
        return None

    def _view_for_component(self, ref: ComponentRef) -> tuple[str, bool] | None:
        """Canonical view alias plus namespacedness for a component ref, or
        None when no real (non-synthetic) view was discovered for it. A
        declared apiVersion must match the discovered group - two CRDs
        sharing a Kind across groups must not resolve to the wrong view."""
        group = ref.api_version.rpartition("/")[0]  # core "v1" -> ""
        fallback: tuple[str, bool] | None = None
        for alias, meta in self.aliases.items():
            if meta.kind != ref.kind or meta.synthetic or self._canonical_kind(alias) != alias:
                continue
            if meta.group == group:
                return alias, meta.namespaced
            if not ref.api_version and fallback is None:
                fallback = (alias, meta.namespaced)
        return fallback

    async def _hierarchy_refs(
        self, root: str, namespace: str, name: str
    ) -> list[ComponentRef] | None:
        """Component refs for the root, or None when unavailable (notified)."""
        if root == "HelmRelease":
            fetch = self._get_helm_components
            if fetch is None:
                return None
            try:
                return await fetch(namespace, name)
            except (ApiStatusError, ValueError) as exc:
                self.notify(f"hierarchy for {name} unavailable: {exc}", severity="error")
                return None
        return await self._operator_component_refs(root, namespace, name)

    def _on_hierarchy_pick(
        self,
        epoch: int,
        origin: tuple[PaneState, str, str],
        result: tuple[str, str, str, str] | None,
    ) -> None:
        """A tree node action: jump to the object's view or describe it.

        ``origin`` is (pane, canonical view, scope) captured when the tree
        was *opened* — the pane may show something else by dismissal time
        (agent navigation is not blocked by the modal), and the return
        must lead back to where the tree actually came from."""
        ctx = self._hierarchy_ctx
        self._hierarchy_ctx = None  # tree closed: stop live rebuilds
        if result is None:
            return
        if self._ctx_switching or epoch != self._ctx_epoch:
            self.notify(
                "hierarchy action cancelled - the kube context changed while the tree was open",
                severity="warning",
            )
            return
        action, kind, ns, obj = result
        if action == "describe":
            coro = self._describe_named(kind, ns, obj)
        else:
            if ctx is not None:
                # The jump must stay reversible (issue #135): Escape on the
                # target reopens this tree over the view it was opened from.
                title, refs, namespace, scope = ctx
                origin_pane, origin_view, origin_scope = origin
                origin_pane.hierarchy_return = _HierarchyReturn(
                    origin_view=origin_view,
                    origin_scope=origin_scope,
                    title=title,
                    refs=refs,
                    namespace=namespace,
                    tree_scope=scope,
                    picked=(kind, ns, obj),
                    epoch=epoch,
                )
            coro = self._jump_to_object(kind, ns, obj, epoch=epoch)
        self.run_worker(coro, exclusive=True, group="hierarchy")

    async def _reopen_hierarchy_return(self) -> bool:
        """Escape on a hierarchy jump target: rebuild the tree over its
        origin view, cursor on the picked node (issue #135). False when
        the focused pane has no pending return or this Escape is not
        eligible to use it — the return stays pending while another modal
        is open, and is dropped only when its pane moved on (kind change,
        context switch)."""
        pane = self._pane
        ret = pane.hierarchy_return
        if ret is None:
            return False
        if len(self.screen_stack) > 1:
            # This Escape belongs to the modal on top (its own close
            # binding handles it) - the return stays pending for the next
            # Escape on the base view.
            return False
        if self._ctx_switch_crossed(ret.epoch):
            pane.hierarchy_return = None
            return False
        if self._canonical_kind(self.current_kind) != ret.picked[0]:
            # The pane navigated elsewhere; nothing to return to.
            pane.hierarchy_return = None
            return False
        pane.hierarchy_return = None  # consumed - never replayed
        await self._navigate(ret.origin_view, ret.origin_scope)
        if self._ctx_switch_crossed(ret.epoch):
            # A :ctx switch started while the navigate held the nav lock:
            # the refs describe the old cluster - do not expose the tree.
            # The keystroke was still consumed (the navigation happened).
            return True
        tree_root = build_hierarchy(
            ret.title,
            ret.refs,
            namespace=ret.namespace,
            resolve=self._view_for_component,
            lookup=self._hierarchy_lookup(ret.tree_scope),
        )
        self._hierarchy_ctx = (ret.title, ret.refs, ret.namespace, ret.tree_scope)
        origin = (pane, ret.origin_view, ret.origin_scope)
        await self.push_screen(
            HierarchyScreen(ret.title, tree_root, initial_cursor=ret.picked),
            functools.partial(self._on_hierarchy_pick, ret.epoch, origin),
        )
        return True

    def _hierarchy_lookup(self, scope: str) -> Callable[[str, str], list[Summary] | None]:
        """Store lookup for the tree: a list only for views a live watch is
        actually feeding, else None - the builder must not claim "missing"
        from a bucket nothing fills. The watch must cover the *component's*
        namespace; cluster-scoped components (ns "") use the tree's scope.

        Results are memoized per (view, watch scope) for this lookup's
        lifetime (one tree build), so an all-namespaces watch serving many
        component namespaces is fetched - and indexed by the builder, which
        caches on bucket identity - exactly once."""
        buckets: dict[tuple[str, str], list[Summary]] = {}

        def lookup(view: str, namespace: str) -> list[Summary] | None:
            active = self.watch_manager.active
            for view_scope in (namespace or scope, ALL_NAMESPACES):
                if (view, view_scope) in active:
                    key = (view, view_scope)
                    if key not in buckets:
                        buckets[key] = self.store.get(view, view_scope)
                    return buckets[key]
            return None

        return lookup

    def _refresh_hierarchy(self) -> None:
        """Rebuild an open hierarchy tree from the current store state."""
        ctx = self._hierarchy_ctx
        try:
            screen = self.screen
        except ScreenStackError:
            # A ResourcesUpdated dispatched during app teardown can land
            # after the screen stack is emptied (flaky-CI issue #147):
            # no screen simply means no tree to refresh.
            return
        if ctx is None or not isinstance(screen, HierarchyScreen):
            return
        title, refs, namespace, scope = ctx
        screen.update_tree(
            build_hierarchy(
                title,
                refs,
                namespace=namespace,
                resolve=self._view_for_component,
                lookup=self._hierarchy_lookup(scope),
            )
        )

    async def _open_hierarchy(self, namespace: str, name: str) -> None:
        """Gather component refs for the selected root and push the tree.

        The fetches span awaited gaps: the captured epoch cancels a tree (or
        a node action) that would otherwise describe the old cluster after a
        context switch completed underneath, and the captured pane view
        keeps the tree from popping over a view the user moved to."""
        root = self._hierarchy_root_kind()
        if root is None or not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch
        pane = self._pane
        kind, scope = pane.kind, pane.scope
        refs = await self._hierarchy_refs(root, namespace, name)
        if refs is None:
            return
        if self._ctx_switching or epoch != self._ctx_epoch:
            return
        if self._pane is not pane or pane.kind != kind or pane.scope != scope:
            return  # the user moved on while components were being fetched
        if len(self.screen_stack) > 1:  # another dialog opened during the fetch
            return
        title = f"{root} {namespace}/{name}" if namespace else f"{root} {name}"
        tree_root = build_hierarchy(
            title,
            refs,
            namespace=namespace,
            resolve=self._view_for_component,
            lookup=self._hierarchy_lookup(scope),
        )
        self._hierarchy_ctx = (title, refs, namespace, scope)
        origin = (pane, self._canonical_kind(kind), scope)
        await self.push_screen(
            HierarchyScreen(title, tree_root),
            functools.partial(self._on_hierarchy_pick, epoch, origin),
        )

    async def _operator_component_refs(
        self, root: str, namespace: str, name: str
    ) -> list[ComponentRef] | None:
        """Component refs for an OLM root, in the issue #120 preference
        order: Operator ``status.components.refs`` (live, includes CRDs and
        RBAC), then the Subscription's InstallPlan ``status.plan``. Returns
        None when the root manifest itself cannot be fetched (already
        notified); an empty list renders a root-only tree."""
        if self._get_manifest is None:
            self.notify("Hierarchy unavailable in this session", severity="warning")
            return None
        try:
            manifest = await self._get_manifest(self.current_kind, namespace or None, name)
        except (ApiStatusError, ValueError) as exc:
            self.notify(f"hierarchy for {name} unavailable: {exc}", severity="error")
            return None
        refs = await self._refs_from_operator_object(manifest, namespace, root)
        # The Operator's refs include the root object itself (the CSV, and
        # sometimes the Subscription); a root listed as its own child would
        # just loop Enter back to the same tree. Match the full identity:
        # OLM copies CSVs into other namespaces under the same name, and
        # those copies are real components.
        refs = [
            r
            for r in refs
            if (r.kind, r.name) != (root, name) or (r.namespace and r.namespace != namespace)
        ]
        if refs:
            return refs
        if root == "Subscription":
            return await self._refs_from_installplan(manifest, namespace)
        return self._refs_from_owned_workloads(manifest, namespace)

    def _refs_from_owned_workloads(
        self, manifest: dict[str, Any], namespace: str
    ) -> list[ComponentRef]:
        """CSV fallback (issue #120 third source): Deployments whose
        ownerReferences point at the CSV, from buckets a live watch feeds.
        Capped like the manifest parsers so a pathological bucket cannot
        flood the tree."""
        uid = str((manifest.get("metadata") or {}).get("uid") or "")
        if not uid:
            return []
        lookup = self._hierarchy_lookup(self.current_scope)
        refs: list[ComponentRef] = []
        for obj in lookup("deployments", namespace) or []:
            if obj.namespace != namespace or uid not in getattr(obj, "owner_uids", ()):
                continue
            refs.append(ComponentRef(kind="Deployment", name=str(obj.name), namespace=namespace))
            if len(refs) >= MAX_COMPONENT_DOCS:
                break
        return refs

    async def _refs_from_installplan(
        self, manifest: dict[str, Any], namespace: str
    ) -> list[ComponentRef]:
        """InstallPlan fallback: ``status.plan`` records exactly what the
        install created (older OLM without the Operator API)."""
        key = self._olm_alias_key("installplans")
        ref = (manifest.get("status") or {}).get("installPlanRef") or {}
        plan_name = str(ref.get("name") or "")
        if key is None or not plan_name or self._get_manifest is None:
            return []
        plan_ns = str(ref.get("namespace") or namespace)
        try:
            plan = await self._get_manifest(key, plan_ns or None, plan_name)
        except (ApiStatusError, ValueError):
            return []
        return installplan_components((plan.get("status") or {}).get("plan"))

    async def _refs_from_operator_object(
        self, manifest: dict[str, Any], namespace: str, root: str
    ) -> list[ComponentRef]:
        """Refs from the cluster-scoped Operator object named
        ``{package}.{namespace}`` (Subscriptions) or via the
        ``operators.coreos.com/<name>`` component labels OLM stamps on CSVs."""
        key = self._olm_alias_key("operators")
        if key is None or self._get_manifest is None:
            return []
        names: list[str] = []
        if root == "Subscription":
            package = str((manifest.get("spec") or {}).get("name") or "")
            if package:
                names.append(f"{package}.{namespace}")
        labels = (manifest.get("metadata") or {}).get("labels") or {}
        prefix = f"{OPERATORS_GROUP}/"
        names += [
            k.removeprefix(prefix) for k in labels if isinstance(k, str) and k.startswith(prefix)
        ]
        for op_name in dict.fromkeys(names):
            try:
                operator = await self._get_manifest(key, None, op_name)
            except (ApiStatusError, ValueError):
                continue  # Operator object missing: fall through to the next source
            components = (operator.get("status") or {}).get("components") or {}
            refs = reference_components(components.get("refs"))
            if refs:
                return refs
        return []

    async def _jump_to_object(
        self, kind: str, namespace: str, name: str, *, epoch: int | None = None
    ) -> None:
        """Navigate to *kind*'s view and put the cursor on the object - the
        tree's Enter lands where every normal action works unchanged. Rows
        stream in after the navigate, so the cursor placement polls briefly.
        A context switch crossing *epoch* aborts: the same-named object in
        the new cluster is not what the user picked."""
        if epoch is not None and self._ctx_switch_crossed(epoch):
            return
        meta = self.aliases.get(kind)
        if meta is None:
            self.notify(f"{kind} is not a discovered view", severity="warning")
            return
        await self._navigate(kind, namespace if meta.namespaced and namespace else None)
        row_key = f"{namespace}/{name}"
        for _ in range(self._jump_poll_attempts):
            if epoch is not None and self._ctx_switch_crossed(epoch):
                return
            if self.current_kind != kind:
                return  # the user moved on - stop quietly
            if self._focus_row(row_key):
                return
            await asyncio.sleep(0.05)
        self.notify(
            f"{name} is not visible in {kind} - it may be gone or outside the current scope",
            severity="warning",
        )

    def _focus_row(self, row_key: str) -> bool:
        """Move the focused table's cursor to *row_key*; False when absent."""
        table = self._focused_table()
        try:
            index = table.get_row_index(row_key)
        except RowDoesNotExist:
            return False
        table.move_cursor(row=index)
        return True

    async def _describe_named(self, kind: str, namespace: str, name: str) -> None:
        """Describe an object named by a hierarchy tree node (no table row
        to read the selection from - `action_describe`'s selection-bound
        path does not apply)."""
        if self._get_manifest is None:
            self.notify("Describe unavailable", severity="warning")
            return
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch
        try:
            manifest = await self._get_manifest(kind, namespace or None, name)
        except ApiStatusError as exc:
            self.notify(
                explain_api_error(exc.status, exc.reason, kind, namespace or None),
                severity="error",
            )
            return
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        if self._ctx_switching or epoch != self._ctx_epoch:
            return
        title = f"{kind}/{namespace}/{name}" if namespace else f"{kind}/{name}"
        if manifest.get("kind") == "Secret":
            # Same masking rule as action_describe (spec §5 #9).
            await self.push_screen(SecretScreen(title, manifest, audit=self._audit))
            return
        await self.push_screen(
            DescribeScreen(title, manifest, [], footer_note=self._provider_footer(manifest))
        )

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
        # The fetch would race the client swap and could render either
        # cluster's manifest — refuse up front.
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch

        namespace, name = self._selected_ns_name()
        if namespace is None or name is None:
            return
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

        if self._ctx_switching or epoch != self._ctx_epoch:
            # The fetches awaited through a context switch: the manifest (or
            # a mixed manifest/events pair) describes the old cluster and
            # must not be pushed over the new session.
            self.notify(
                f"describe {name} cancelled - the kube context changed during the fetch",
                severity="warning",
            )
            return
        title = f"{self.current_kind}/{namespace}/{name}"
        if manifest.get("kind") == "Secret":
            # Secrets get the dedicated masked viewer (spec §5 #9): values
            # render masked; per-key reveal is explicit and audit-logged.
            await self.push_screen(SecretScreen(title, manifest, audit=self._audit))
            return
        await self.push_screen(
            DescribeScreen(title, manifest, events, footer_note=self._provider_footer(manifest))
        )

    def on_unknown_command(self, message: UnknownCommand) -> None:
        parts = message.text.strip().split()
        head = parts[0] if parts else ""
        if head in {"ai", "agent"} and self._agent_available:
            if len(parts) > 1 and parts[1].lower() == "follow":
                self._handle_agent_follow_command(parts[2:])
                return
            self._open_agent_setup()
            return
        if head == "model" and self._agent_available:
            self._handle_model_command(parts[1:])
            return
        if head == "mcp":
            self._handle_mcp_command(parts[1:])
            return
        if head in {"tp", "telepresence"}:
            self._handle_telepresence_command()
            return
        if head == "proposals":
            self._open_proposal_review()
            return
        if head == "pf":
            self._open_forward_list()
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
            self.notify(
                "Agent setup unavailable — install with: pip install 'korvid[agent]'",
                severity="warning",
            )
            return
        # The wizard applies the settings itself (via apply_settings) before
        # persisting, so a refused swap keeps the wizard open and unsaved.
        self.push_screen(
            AgentSetupScreen(
                self._agent_configurator,
                apply_settings=self._apply_agent_settings,
                current_profile=self._configured_agent_profile,
            )
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

    def _handle_telepresence_command(self) -> None:
        """`:tp` / `:telepresence` (issue #159): open the read-only status
        panel. Queries run only here, on the explicit user action - the
        telepresence CLI spawns its local user daemon to answer, so korvid
        never polls it in the background."""
        tp = self._telepresence
        if tp is None:
            self.notify(
                "telepresence not available — binary not on PATH, or disabled "
                "via `integrations: {telepresence: off}`",
                severity="warning",
            )
            return

        async def _open() -> None:
            with self._progress("querying telepresence"):
                try:
                    status = await tp.status()
                    intercepts = (
                        await tp.list_intercepts(daemon=status.daemon_name or None)
                        if status.connected
                        else []
                    )
                except TelepresenceError as exc:
                    # stderr tails are hostile input for a markup toast.
                    self.notify(str(exc), title="telepresence", severity="error", markup=False)
                    return
            await self.push_screen(TelepresenceScreen(status, intercepts))

        self.run_worker(_open(), exclusive=True, group="telepresence")

    async def _maybe_hint_telepresence(self) -> None:
        """One dim hint per session (issue #159): the cluster runs a
        traffic-manager but the local client is absent. The probe is an
        injected pure API check - never the telepresence binary; a missing
        probe, a failure or the kill-switch all silently mean no hint.

        Re-runnable until the hint actually shows: the startup cluster may
        lack a manager while a later `:ctx` target runs one. Results are
        epoch-bound - a probe answering for a context that was already left
        is discarded - and a re-probe requested while one is in flight is
        queued instead of lost.
        """
        if (
            self._telepresence is not None
            or self._telepresence_hinted
            or not self.config.telepresence_enabled
            or self._probe_traffic_manager is None
        ):
            return
        if self._telepresence_probing:
            # A :ctx switch mid-probe: run again once the old probe (whose
            # answer describes the old cluster) unwinds.
            self._telepresence_reprobe = True
            return
        self._telepresence_probing = True
        epoch = self._ctx_epoch
        try:
            present = await self._probe_traffic_manager()
        except Exception:
            return  # absent / forbidden / transient: all mean "no hint"
        finally:
            self._telepresence_probing = False
            if self._telepresence_reprobe:
                self._telepresence_reprobe = False
                self.run_worker(self._maybe_hint_telepresence(), exclusive=False)
        if not present or epoch != self._ctx_epoch:
            return  # no manager, or the answer describes a left context
        self._telepresence_hinted = True
        self.notify(
            "telepresence traffic-manager detected in this cluster — install "
            "the client and restart korvid to inspect intercepts (`:tp`)",
            severity="information",
            timeout=8,
        )

    def _handle_mcp_follow_command(self, args: list[str]) -> None:
        """`:mcp follow [on|off]` (issue #153): toggle mirroring of external
        cluster reads in the TUI. Bare `:mcp follow` flips the state."""
        if args and args[0].lower() not in ("on", "off"):
            self.notify("Usage: :mcp follow [on|off]", severity="warning")
            return
        self._mcp_follow = args[0].lower() == "on" if args else not self._mcp_follow
        state = "on" if self._mcp_follow else "off"
        self.notify(
            f"MCP follow {state} — external reads are {'mirrored on screen' if self._mcp_follow else 'no longer mirrored'}"
        )
        self._refresh_status()

    def _handle_agent_follow_command(self, args: list[str]) -> None:
        """`:ai follow [on|off]`: toggle mirroring of the built-in agent's
        cluster reads on screen. Bare `:ai follow` flips the state."""
        if args and args[0].lower() not in ("on", "off"):
            self.notify("Usage: :ai follow [on|off]", severity="warning")
            return
        self._agent_follow = args[0].lower() == "on" if args else not self._agent_follow
        state = "on" if self._agent_follow else "off"
        self.notify(
            f"Agent follow {state} — the agent's reads are "
            f"{'mirrored on screen' if self._agent_follow else 'no longer mirrored'}"
        )

    @property
    def mcp_follow_enabled(self) -> bool:
        """Current follow-mode state; read by the MCP server's wiring."""
        return self._mcp_follow

    def note_mcp_activity(self, line: str) -> None:
        """Transient activity note for an external MCP read (issue #153):
        with follow off, this is the only trace an external host leaves on
        screen. Display only — never raises into the caller."""
        with contextlib.suppress(Exception):
            # markup=False: parts of the line are caller-controlled (pod and
            # namespace names from the MCP host) - Rich tags must render
            # literally, never restyle or forge toast content.
            self.notify(line, title="MCP", severity="information", timeout=3, markup=False)

    def _handle_mcp_command(self, args: list[str]) -> None:
        """`:mcp` shows server state; `:mcp on` / `:mcp off` toggle it live."""
        mcp = self._mcp
        if mcp is None:
            self.notify(
                "MCP unavailable — install with: pip install 'korvid[mcp]'",
                severity="warning",
            )
            return
        if not args:
            follow = "follow on" if self._mcp_follow else "follow off"
            self.notify(f"{mcp.status()} · {follow}")
            return
        action = args[0].lower()
        if action == "follow":
            self._handle_mcp_follow_command(args[1:])
            return
        if action not in ("on", "off"):
            self.notify("Usage: :mcp [on|off] | :mcp follow [on|off]", severity="warning")
            return
        self._toggle_mcp_server(mcp, action)

    def _toggle_mcp_server(self, mcp: MCPControllerBase, action: str) -> None:
        """Start/stop the MCP server live (`:mcp on` / `:mcp off`)."""
        if self._ctx_switching:
            # The switch quiesced the server before swapping the client and
            # alias map; a toggle landing mid-swap could restart it against
            # state that is being replaced.
            self.notify(
                "A context switch is in progress — try again once it completes",
                severity="warning",
            )
            return

        async def _switch() -> None:
            # Serialize with the `:ctx` flow (which holds _nav_lock through
            # quiesce/teardown/retarget) and re-check inside the lock: a
            # toggle queued just before the switch claimed _ctx_switching
            # could otherwise start the server against the client/alias map
            # mid-swap, or have its stop undone by the switch's restart.
            async with self._nav_lock:
                if self._ctx_switching:
                    self.notify(
                        "A context switch is in progress — try again once it completes",
                        severity="warning",
                    )
                    return
                was_running = mcp.running
                if action == "on" and not was_running:
                    # Any pending proposal predates the run about to start —
                    # its capability token is from an older, ended run.
                    # Sweep BEFORE the endpoint goes live: once start()
                    # returns, the new run's callers may already have
                    # submitted, and their work must not be expired as
                    # old-run stragglers.
                    await self._expire_proposals_audited("the MCP server was restarted")
                msg = await (mcp.start() if action == "on" else mcp.stop())
                # A real stop invalidates every capability token handed out
                # for that run (issue #110): pending proposals from it must
                # not survive. A stop whose bounded teardown timed out
                # (`running` still True) has still ended that run's
                # authority, so it expires too; only an idempotent
                # status-preserving toggle (`:mcp on` while already
                # running) keeps pending work.
                stopped = action == "off" and was_running
                # Captured under the lock and BEFORE the audited sweep: the
                # sweep awaits audit appends, and the dying run's task can
                # finish during that wait — `running` re-read afterwards
                # would be False, skipping the follow-up sweep that catches
                # the run's last in-flight submissions. The wait below must
                # bind to *this* dying run, never to whichever run the
                # controller owns once the lock is released.
                old_task = mcp.pending_task() if stopped and mcp.running else None
                if stopped:
                    await self._expire_proposals_audited("the MCP server was stopped")
            self.notify(msg, severity="error" if msg.startswith("ERROR") else "information")
            self._refresh_status()
            if old_task is not None:
                # The bounded stop timed out and the old run is still dying
                # in the background: wait it out, then sweep again so an
                # in-flight submission that raced the teardown cannot
                # outlive its server run.
                await self._sweep_after_mcp_teardown(mcp, old_task)

        self.run_worker(_switch(), exclusive=False)

    async def _sweep_after_mcp_teardown(
        self, mcp: MCPControllerBase, task: asyncio.Task[None]
    ) -> None:
        """Final proposal sweep once a dragged-out MCP teardown completes.

        `stop()`'s bounded wait can return while the old server run is still
        dying; an in-flight proposal call on that run may land *after* the
        stop-time sweep. *task* is the old run's server task, captured under
        `_nav_lock` before it was released: the follow-up wait binds to that
        exact run, so a fresh server started by a racing `:mcp on` is never
        the one waited on or torn down here.
        """
        with contextlib.suppress(Exception):
            await asyncio.wait({task})
        # Serialize the sweep decision against a racing `:mcp on`: if a
        # fresh run came up while the old teardown dragged on, pending
        # proposals belong to that live run (its start transition already
        # swept old-run stragglers) and must not be expired here.
        async with self._nav_lock:
            if mcp.running:
                return
            await self._expire_proposals_audited("the MCP server was stopped")

    def _apply_agent_settings(self, settings: AgentSettings) -> bool:
        """Swap in a fresh runtime built from the wizard's settings.

        Transactional: on any failure the previous runtime/settings are kept
        and False is returned; the swap is also refused while a turn is live.
        """
        if self._rebuild_agent is None:
            self.notify(
                "Agent rebuild unavailable — install with: pip install 'korvid[agent]'",
                severity="warning",
            )
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
        self._agent_profile = settings.profile
        # Once applied (and persisted by the wizard) the profile is an
        # explicit choice — reopening :ai must preserve it.
        self._configured_agent_profile = settings.profile
        self._refresh_status()
        panel = self._agent_panel
        agent_input = panel.query_one("#agent-input")
        # Always re-enable: the hint may have disabled it while the panel was
        # open earlier; only focus/header rendering depends on visibility.
        agent_input.disabled = False
        if panel.display:
            in_tok, out_tok = runtime.total_tokens
            panel.set_header(
                settings.model,
                in_tok,
                out_tok,
                estimated=runtime.usage_estimated,
                profile=settings.profile,
            )
            agent_input.focus()
        return True

    def action_shell(self) -> None:
        """Drop into a shell inside the selected pod via kubectl exec.

        Multi-container pods show a container picker first; if exec fails
        (typically a distroless image without sh/bash) a `kubectl debug`
        ephemeral-container fallback is offered. On the nodes view the same
        key opens a node shell via `kubectl debug node/` behind an approval
        dialog (issue #46).
        """
        kind = self._canonical_kind(self.current_kind)
        meta = self.aliases.get(kind)
        # The exec would race the teardown/retarget and could attach to
        # whichever cluster wins — refuse up front.
        if not self._ctx_reads_allowed():
            return
        if meta is not None and (meta.group, meta.plural) == ("", "nodes"):
            self.run_worker(self._node_shell_flow())
            return
        if kind != "pods":
            self.notify("Shell is available for pods and nodes", severity="warning")
            return

        ns, name = self._selected_ns_name()
        if ns is None or name is None:
            return
        namespace = ns

        if shutil.which("kubectl") is None:
            self.notify(
                "kubectl not found on PATH — shell-in requires kubectl",
                severity="error",
            )
            return

        containers = self._get_pod_containers(namespace, name)
        if len(containers) > 1:
            epoch = self._ctx_epoch

            def _on_pick(container: str | None) -> None:
                if container is None:
                    return
                if self._ctx_switching or epoch != self._ctx_epoch:
                    # The picker stayed open across a context switch: the
                    # selection belongs to the old cluster while kubectl
                    # would now target the new one.
                    self.notify(
                        f"shell into {name} cancelled - the kube context"
                        " changed while the container picker was open",
                        severity="warning",
                    )
                    return
                self._run_shell(namespace, name, container)

            self.push_screen(
                PickScreen(f"Container in {name}:", list(containers)),
                _on_pick,
            )
            return

        self._run_shell(namespace, name, containers[0] if containers else None)

    async def action_port_forward(self) -> None:
        """Open the port-forward dialog for the selected pod or service (shift+f)."""
        if self.current_kind not in FORWARDABLE_KINDS:
            self.notify("Port-forward is only available for pods and services", severity="warning")
            return
        # The forward would race the teardown/retarget and could spawn
        # against whichever cluster wins — refuse up front.
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch
        if self._forwards is None:
            self.notify("Port-forward unavailable in this build", severity="warning")
            return
        if shutil.which("kubectl") is None:
            self.notify(
                "kubectl not found on PATH — port-forward requires kubectl", severity="error"
            )
            return
        ns, name = self._selected_ns_name()
        if ns is None or name is None:
            return
        kind = self.current_kind
        ports, manifest_ok = await self._forward_prefill_ports(kind, ns, name)
        if kind == "services" and manifest_ok and not ports:
            # A fetched Service with no TCP ports can never be forwarded —
            # kubectl port-forward is TCP-only. (A failed fetch still opens
            # the dialog: the port list is a convenience, not the source of
            # truth, and kubectl gives the authoritative error.)
            self.notify(
                f"{name} declares no TCP ports — kubectl port-forward is TCP-only",
                severity="error",
            )
            return

        def _on_result(result: tuple[int, int] | None) -> None:
            if result is None:
                return
            if self._ctx_switching or epoch != self._ctx_epoch:
                # The dialog stayed open across a context switch (or the
                # port prefill awaited through one): the selection belongs
                # to the old cluster while kubectl and the reopened forward
                # registry now target the new one.
                self.notify(
                    f"port-forward to {name} cancelled - the kube context"
                    " changed while the dialog was open",
                    severity="warning",
                )
                return
            self.run_worker(
                self._start_forward(
                    kind, ns, name, local_port=result[0], remote_port=result[1], epoch=epoch
                )
            )

        await self.push_screen(
            PortForwardScreen(f"{kind}/{ns}/{name}", ports, restrict_remote=kind == "services"),
            _on_result,
        )

    async def _forward_prefill_ports(
        self, kind: str, namespace: str, name: str
    ) -> tuple[list[int], bool]:
        """Declared TCP ports for the forward dialog, plus fetch success.

        The success flag lets the caller tell "no TCP ports declared"
        (reject a Service up front) apart from "manifest unavailable"
        (open the dialog unrestricted — kubectl has the final say).
        """
        if self._get_manifest is None:
            return [], False
        try:
            manifest = await self._get_manifest(kind, namespace, name)
        except Exception as exc:  # prefill is a convenience — dialog works without it
            logger.debug("manifest fetch for port prefill failed: %s", exc)
            return [], False
        return candidate_remote_ports(kind, manifest), True

    #: Pod controller kinds a re-attach can follow, mapped to their plural.
    #: ReplicaSets are chased one level up so the forward survives rollouts,
    #: not just single pod replacements.
    _WORKLOAD_PLURALS: ClassVar[dict[str, str]] = {
        "Deployment": "deployments",
        "ReplicaSet": "replicasets",
        "ReplicationController": "replicationcontrollers",
        "StatefulSet": "statefulsets",
        "DaemonSet": "daemonsets",
        "Job": "jobs",
    }

    #: Chain the ownership banner may walk (issue #119): a superset of the
    #: re-attach map — a CronJob-spawned pod must reach the CronJob, where
    #: the helm/OLM markers live (re-attach never targets a CronJob: it
    #: forwards to pods, which Jobs own directly).
    _OWNER_CHAIN_PLURALS: ClassVar[dict[str, str]] = {
        **_WORKLOAD_PLURALS,
        "CronJob": "cronjobs",
    }

    async def _resolve_forward_workload(self, namespace: str, name: str) -> str | None:
        """The pod's owning workload as ``"<plural>/<name>"``, best effort.

        Captured when a forward starts so a later re-attach can follow the
        pod to its replacement (issue #38). ReplicaSets are resolved to their
        Deployment when they have one. Any failure yields None — the forward
        still works, only the follow-the-workload re-attach is unavailable.
        """
        if self._get_manifest is None:
            return None
        try:
            owner = controller_owner(await self._get_manifest("pods", namespace, name))
        except Exception as exc:  # a convenience — never blocks the forward
            logger.debug("workload resolution for port-forward failed: %s", exc)
            return None
        if owner is not None and owner[0] == "ReplicaSet":
            # A failed chase (e.g. discovery has not learned replicasets yet)
            # keeps the ReplicaSet as the fallback target — the parent lookup
            # improves the target to a Deployment, it is not required.
            try:
                parent = controller_owner(
                    await self._get_manifest("replicasets", namespace, owner[1])
                )
            except Exception as exc:
                logger.debug("deployment lookup failed; keeping replicaset owner: %s", exc)
                parent = None
            if parent is not None and parent[0] == "Deployment":
                owner = parent
        if owner is None:
            return None
        plural = self._WORKLOAD_PLURALS.get(owner[0])
        return f"{plural}/{owner[1]}" if plural is not None else None

    async def _start_forward(
        self,
        kind: str,
        namespace: str,
        name: str,
        *,
        local_port: int,
        remote_port: int,
        epoch: int,
    ) -> None:
        """Spawn a forward from the registry, audit it, and confirm to the user."""
        registry = self._forwards
        if registry is None:  # pragma: no cover - action guard already checked
            return
        if self._ctx_switching or epoch != self._ctx_epoch:
            # The worker was scheduled just as a switch started: it is not
            # yet registered in _launching_forwards, so teardown could not
            # cancel it and it would spawn against the new cluster.
            self.notify(
                f"port-forward to {name} cancelled - the kube context changed",
                severity="warning",
            )
            return
        workload = await self._resolve_forward_workload(namespace, name) if kind == "pods" else None
        if self._ctx_switching or epoch != self._ctx_epoch:
            # The workload lookup awaited through a switch (this coroutine
            # registers below only after the lookup, so teardown missed it):
            # the old-cluster pod selection must not spawn kubectl against
            # the retargeted context.
            self.notify(
                f"port-forward to {name} cancelled - the kube context changed",
                severity="warning",
            )
            return
        spec = ForwardSpec(
            kind=kind,
            namespace=namespace,
            name=name,
            local_port=local_port,
            remote_port=remote_port,
            workload=workload,
        )
        worker = get_current_worker()
        self._launching_forwards[worker] = spec
        try:
            # Off the event loop: reclaiming the local port may block briefly
            # on reaping a previously stopped child that ignored SIGTERM.
            record = await asyncio.to_thread(registry.start, spec)
        except (OSError, ValueError) as exc:
            # OSError: spawn failed (kubectl missing). ValueError: local
            # port collision detected up front by the registry, or a spawn
            # that lost the race against teardown (registry shut down).
            if not self._forwards_closing:
                self.notify(f"Port-forward failed to start: {exc}", severity="error")
            self._audit_forward_shutdown_safe("port-forward-start", spec, outcome=f"error: {exc}")
            return
        except asyncio.CancelledError:
            # Shutdown cancelled the launch mid-spawn — the start must still
            # reach the log before any teardown stop entry (enqueue
            # directly: no new workers during shutdown).
            if self._audit is not None:
                self._enqueue_forward_audit(
                    "port-forward-start", spec, outcome="stopped before ready"
                )
            raise
        finally:
            self._launching_forwards.pop(worker, None)
        if self._forwards_closing or registry.get(record.id) is None:
            # A quit or stop won the race between the registry publishing
            # the record and this coroutine resuming: no confirmation may
            # spawn (shutdown) or would ever resolve (record gone) — audit
            # the start here so its stop entry never reaches the log first.
            self._audit_forward_shutdown_safe(
                "port-forward-start", spec, outcome="stopped before ready"
            )
            return
        # Popen returning only proves the child exists — success is reported
        # after kubectl confirms the listener (or fails the bind/RBAC check).
        self._track_confirmation(record)

    async def _spawn_reattach(
        self, registry: ForwardRegistry, record: ForwardRecord, *, retarget: bool = False
    ) -> ForwardRecord | None:
        """Re-attach off-loop while teardown (and stops) can see and await it.

        With ``retarget`` the replacement follows the spec's recorded owning
        workload instead of the vanished pod (issue #38).

        Mirrors `_start_forward`'s tracking: between the registry adopting
        the replacement in its thread and this coroutine resuming, a quit or
        stop must defer behind this event so the replacement's start entry
        always reaches the audit log before any stop entry.
        """
        done = asyncio.Event()
        # The in-flight kubectl targets the retargeted spec — tracking and
        # the cancellation audit must name that GVR, not the vanished pod.
        attempted = (record.spec.retargeted() if retarget else None) or record.spec
        self._reattaching_forwards[done] = attempted
        try:
            revived = await asyncio.to_thread(
                functools.partial(registry.reattach, record.id, retarget=retarget)
            )
        except asyncio.CancelledError:
            # Shutdown cancelled the re-attach mid-spawn. The registry only
            # adopts a replacement while it is still open, so if one was (or
            # will be) adopted, its start must reach the log before the
            # teardown stop entries (enqueue directly: no new workers now).
            if self._audit is not None:
                self._enqueue_forward_audit(
                    "port-forward-start", attempted, outcome="stopped before ready"
                )
            raise
        finally:
            self._reattaching_forwards.pop(done, None)
            done.set()
        if revived is None:
            # Never adopted (broken no more, stopped, or torn down mid-spawn)
            # — no replacement started, so there is nothing to report.
            return None
        if self._forwards_closing or registry.get(revived.id) is None:
            # A quit or stop won the race between the adoption and this
            # coroutine resuming: no confirmation may spawn (shutdown) or
            # would ever resolve (record gone) — audit the start here so its
            # stop entry never reaches the log first.
            self._audit_forward_shutdown_safe(
                "port-forward-start", revived.spec, outcome="stopped before ready"
            )
            return revived
        # Re-arm the broken toast right away: waiting for the next global
        # poll would silently swallow a breakage of the fresh process.
        self._broken_forwards.discard(revived.id)
        # Same readiness handshake as a fresh start (issue #38 review).
        self._track_confirmation(revived, reattached=True)
        return revived

    def _audit_forward_shutdown_safe(self, action: str, spec: ForwardSpec, *, outcome: str) -> None:
        """Audit a forward event without spawning workers once teardown began.

        The teardown flush drains directly-enqueued entries, so nothing is
        lost — outside of teardown this is a plain `_audit_forward` call.
        """
        if not self._forwards_closing:
            self._audit_forward(action, spec, outcome=outcome)
            return
        if self._audit is not None:
            self._enqueue_forward_audit(action, spec, outcome=outcome)

    async def _confirm_forward(self, record: ForwardRecord, *, reattached: bool = False) -> None:
        """Toast and audit a forward start only once kubectl signals ready.

        An exit before the ready line is a failed start: the record is
        dropped (fresh starts only — a failed re-attach stays listed as
        broken for another try) and kubectl's last words become the error.
        """
        registry = self._forwards
        if registry is None:  # pragma: no cover - callers hold a registry
            return
        spec = record.spec
        worker = get_current_worker()
        try:
            # Snapshot before waiting: a re-attach during the wait bumps the
            # generation, and the abort below must never hit the replacement.
            generation = registry.generation(record.id)
            status = await asyncio.to_thread(
                registry.wait_ready, record.id, timeout=_FORWARD_READY_SECONDS
            )
            if status == "superseded" or self._current_confirmation(record.id) is not worker:
                # A re-attach superseded this confirmation: the record was
                # re-armed in place, so ``status`` (and everything on the
                # record) may describe the replacement process — nothing
                # observed here may be toasted, audited, or failed as this
                # generation's result. The registry reports the supersession
                # itself because the woken waiter can resume before the
                # re-attach publishes the replacement's confirmation token.
                self._audit_forward("port-forward-start", spec, outcome="superseded by re-attach")
                return
            if registry.get(record.id) is None:
                # The user stopped the still-starting forward from :pf. Its
                # deferred stop entry is queued behind this confirmation, so
                # the start still reaches the audit log first — and a
                # "failed to start" toast would be wrong for a deliberate stop.
                self._audit_forward("port-forward-start", spec, outcome="stopped before ready")
                return
            if status != "alive":
                self._report_failed_forward_start(
                    registry, record, status, reattached=reattached, generation=generation
                )
                return
            self._report_forward_ready(record, reattached=reattached)
        except asyncio.CancelledError:
            # Shutdown cancelled the confirmation mid-handshake — the start
            # must still reach the log before its teardown stop entry
            # (enqueue directly: no new workers during shutdown).
            if self._audit is not None:
                self._enqueue_forward_audit(
                    "port-forward-start", spec, outcome="stopped before ready"
                )
            raise
        finally:
            # Drop only this generation's entry, and only once its start
            # audit is enqueued (above) — stops defer behind every
            # outstanding confirmation, so a superseded generation must stay
            # tracked until its entry cannot land after a stop anymore.
            self._untrack_confirmation(record.id, worker)

    def _untrack_confirmation(self, forward_id: int, worker: Worker[None]) -> None:
        """Remove one finished confirmation generation from the tracking maps."""
        entries = self._confirming_forwards.get(forward_id)
        if entries is not None:
            with contextlib.suppress(ValueError):
                entries.remove(worker)
            if not entries:
                del self._confirming_forwards[forward_id]
        # Only the current generation clears its own token — a superseded
        # worker leaving must not disturb the replacement's marker.
        if self._current_confirmations.get(forward_id) is worker:
            del self._current_confirmations[forward_id]

    def _track_confirmation(self, record: ForwardRecord, *, reattached: bool = False) -> None:
        """Spawn a readiness confirmation and register it as the current one."""
        worker = self.run_worker(self._confirm_forward(record, reattached=reattached))
        self._confirming_forwards.setdefault(record.id, []).append(worker)
        self._current_confirmations[record.id] = worker

    def _current_confirmation(self, forward_id: int) -> Worker[None] | None:
        """The forward's current-generation confirmation worker, if any."""
        return self._current_confirmations.get(forward_id)

    def _report_forward_ready(self, record: ForwardRecord, *, reattached: bool) -> None:
        """Toast and audit a confirmed forward (fresh start or re-attach)."""
        spec = record.spec
        if reattached:
            self._audit_forward("port-forward-start", spec, outcome="reattached")
            self.notify(f"Re-attached forward localhost:{spec.local_port}")
            return
        self._audit_forward("port-forward-start", spec)
        self.notify(
            f"Forwarding localhost:{spec.local_port} → "
            f"{spec.namespace}/{spec.name}:{spec.remote_port}"
        )

    def _report_failed_forward_start(
        self,
        registry: ForwardRegistry,
        record: ForwardRecord,
        status: str,
        *,
        reattached: bool,
        generation: int | None = None,
    ) -> None:
        """Handle a readiness handshake that did not end in ``alive``.

        A ``starting`` result means kubectl stayed silent through the wait
        window: readiness was never confirmed and liveness polling could not
        correct a false success later (it only detects exits), so the forward
        is failed explicitly instead of reported ready on a guess. The caller
        already verified this confirmation is the record's current one.

        ``status`` is only a timeout snapshot: the abort itself is the
        registry's atomic compare-and-transition, whose returned outcome says
        exactly what happened — a readiness line that landed after the
        snapshot (``alive``) is reported as the success it is, a re-attach
        that raced the snapshot (``superseded``) keeps its replacement and
        only audits the supersession, and a stop that unlisted the record
        (``gone``) stands as the deliberate outcome it was.
        """
        spec = record.spec
        outcome = registry.fail_start(record.id, keep=reattached, generation=generation)
        if outcome == "alive":
            # The handshake resolved between the wait snapshot and the abort.
            self._report_forward_ready(record, reattached=reattached)
            return
        if outcome == "superseded":
            # A re-attach adopted a replacement while this confirmation timed
            # out — the replacement reports its own fate; this generation
            # only records that it was superseded.
            self._audit_forward("port-forward-start", spec, outcome="superseded by re-attach")
            return
        if outcome == "gone":  # stopped from :pf (or torn down) in the same window
            self._audit_forward("port-forward-start", spec, outcome="stopped before ready")
            return
        if status == "starting":
            detail = f"kubectl did not confirm the forward within {_FORWARD_READY_SECONDS:g}s"
        else:
            detail = record.last_output or "kubectl exited before the forward was ready"
        if reattached:
            # A failed re-attach stays listed as broken for another try. Mark
            # the breakage as already reported: the specific error toasted
            # below must not be followed by the poll's generic broken toast.
            self._broken_forwards.add(record.id)
        self.notify(f"Port-forward failed to start: {detail}", severity="error")
        self._audit_forward("port-forward-start", spec, outcome=f"error: {detail}")

    async def _audit_stop_after_confirm(
        self, pending: list[Worker[None] | asyncio.Event], forward_id: int
    ) -> None:
        """Audit a stop only after every outstanding confirmation resolved.

        A superseded generation may still be waiting alongside the current
        one — each enqueues its own start entry, so the stop must defer
        behind all of them (in-flight launches and re-attaches included).
        The spec lives in `_deferred_stop_audits`
        (popped here on success) so that a shutdown cancelling this worker
        cannot lose the entry — teardown flushes whatever is left after the
        confirmations settle.
        """
        for confirm in pending:
            with contextlib.suppress(Exception):  # a cancelled confirm still frees the stop
                await confirm.wait()
        spec = self._deferred_stop_audits.pop(forward_id, None)
        if spec is not None:
            self._audit_forward("port-forward-stop", spec)

    def _audit_forward(self, action: str, spec: ForwardSpec, *, outcome: str = "success") -> None:
        """Queue a forward audit entry; a single worker drains in FIFO order."""
        if self._audit is None:
            # Forwards are read-only risk profile (issue #38): they stay
            # usable without an audit sink, unlike cluster writes.
            return
        self._enqueue_forward_audit(action, spec, outcome=outcome)
        worker = self._forward_audit_worker
        if worker is None or worker.is_finished:
            self._forward_audit_worker = self.run_worker(self._drain_forward_audits())

    def _enqueue_forward_audit(
        self, action: str, spec: ForwardSpec, *, outcome: str = "success", teardown: bool = False
    ) -> None:
        detail = f"localhost:{spec.local_port} -> {spec.name}:{spec.remote_port}"
        if teardown:
            detail += " (session teardown)"
        # Full GVR: a retargeted forward runs against an apps/batch workload,
        # and the audit schema disambiguates kinds by group (core/audit.py).
        group, version = forward_target_gvr(spec.kind)
        self._forward_audit_queue.append(
            {
                "action": action,
                "kind": spec.kind,
                "namespace": spec.namespace,
                "name": spec.name,
                "group": group,
                "version": version,
                "detail": detail,
                "outcome": outcome,
            }
        )

    async def _drain_forward_audits(self) -> None:
        """Write queued forward audit entries strictly in enqueue order.

        Entries are enqueued only on the event loop, and each write+dequeue
        runs atomically inside a single worker thread under
        `_forward_audit_io_lock`: even if the awaiting drain is cancelled
        mid-write, the thread finishes the pop, so a later flush (the
        unmount path) can neither duplicate the entry nor lose it.
        Append failures are best-effort by design (read-only risk profile):
        a full disk must not kill the app or block the forward.
        """
        audit = self._audit
        if audit is None:
            return
        queue = self._forward_audit_queue

        def _write_head() -> None:
            with self._forward_audit_io_lock:
                if not queue:
                    return
                entry = queue[0]
                try:
                    audit.append(**entry)
                except OSError as exc:
                    logger.warning("forward audit (%s) failed: %s", entry["action"], exc)
                queue.popleft()

        while queue:
            await asyncio.to_thread(_write_head)

    def _open_forward_list(self) -> None:
        """`:pf` — the active-forwards screen with stop / re-attach keys."""
        if self._forwards is None:
            self.notify("Port-forward unavailable in this build", severity="warning")
            return
        registry = self._forwards

        def _on_stop(record: ForwardRecord) -> None:
            # A stopped broken forward will never poll alive again — drop its
            # id so the broken set does not grow for the session's lifetime.
            self._broken_forwards.discard(record.id)
            # In-flight launches and re-attaches on this record's local port
            # count as pending too: a stop landing in the window between the
            # registry publishing (or adopting) a record and its coroutine
            # resuming has no confirmation to defer behind yet. Other ports'
            # launches are unrelated and must not delay this stop's entry.
            port = record.spec.local_port
            pending: list[Worker[None] | asyncio.Event] = [
                *(w for w, spec in self._launching_forwards.items() if spec.local_port == port),
                *(e for e, spec in self._reattaching_forwards.items() if spec.local_port == port),
                *self._confirming_forwards.get(record.id, ()),
            ]
            if not pending:
                self._audit_forward("port-forward-stop", record.spec)
            else:
                # Start entries are only enqueued as the readiness
                # confirmations resolve — queue this stop behind all of them
                # so the log never shows a stop before any of its starts.
                self._deferred_stop_audits[record.id] = record.spec
                self.run_worker(self._audit_stop_after_confirm(pending, record.id))
            self.notify(f"Stopped forward localhost:{record.spec.local_port}")

        def _on_reattach_error(spec: ForwardSpec, exc: Exception) -> None:
            self._audit_forward("port-forward-start", spec, outcome=f"error: {exc}")

        async def _reattach(record: ForwardRecord, retarget: bool) -> ForwardRecord | None:
            return await self._spawn_reattach(registry, record, retarget=retarget)

        async def _target_exists(record: ForwardRecord) -> bool:
            # Only a confirmed 404 blocks the re-attach; when the target
            # cannot be verified (no fetcher, transport or transient errors)
            # it fails open and lets kubectl report the truth.
            if self._get_manifest is None:
                return True
            spec = record.spec
            try:
                await self._get_manifest(spec.kind, spec.namespace, spec.name)
            except ApiStatusError as exc:
                return exc.status != 404
            except Exception as exc:  # verification is best-effort by design
                logger.debug("re-attach target check failed: %s", exc)
                return True
            return True

        self.push_screen(
            ForwardListScreen(
                self._forwards,
                on_stop=_on_stop,
                reattach=_reattach,
                on_reattach_error=_on_reattach_error,
                target_exists=_target_exists,
            )
        )

    def _poll_forwards(self) -> None:
        """Flag newly broken forwards with a toast (once per breakage)."""
        registry = self._forwards
        if registry is None:  # pragma: no cover - interval only set when present
            return
        registry.refresh()
        launching_ports = {
            spec.local_port
            for spec in (*self._launching_forwards.values(), *self._reattaching_forwards.values())
        }
        for record in registry.forwards():
            if record.status == "broken" and record.id not in self._broken_forwards:
                if (
                    record.id in self._current_confirmations
                    or record.spec.local_port in launching_ports
                ):
                    # A readiness confirmation — tracked, or still being
                    # installed by an in-flight launch or re-attach on this
                    # record's local port — is about to report this exact
                    # failure with its specific error; the generic breakage
                    # toast must not double it. Other ports' launches are
                    # unrelated and never defer this record's toast.
                    continue
                self._broken_forwards.add(record.id)
                self.notify(
                    f"Port-forward localhost:{record.spec.local_port} ->"
                    f" {record.spec.namespace}/{record.spec.name} broken"
                    " (target gone?) — :pf to re-attach",
                    severity="warning",
                )
            elif record.status == "alive":
                # Re-attached: arm the toast again for the next breakage.
                self._broken_forwards.discard(record.id)

    @staticmethod
    def _run_interactive(argv: list[str], banner: str) -> int:
        """Run an interactive subprocess on a cleared screen for a direct feel.

        Suspending Textual drops back to the primary screen, exposing old
        scrollback (including the command that launched korvid). Clearing
        first makes it look like we connected straight into the pod.
        """
        print(f"\x1b[2J\x1b[H\x1b[2m{banner}\x1b[0m", flush=True)
        return subprocess.call(argv)

    # -- File transfer (issue #47): download/upload over the exec API as a
    # -- tar stream; uploads are approval-gated, both directions audited
    # -- fail-closed.

    def action_transfer(self) -> None:
        """Open the ctrl+t transfer dialog for the selected pod."""
        if self.current_kind != "pods":
            self.notify("File transfer is only available for pods", severity="warning")
            return
        if self._open_pod_exec is None:
            self.notify("File transfer unavailable (no cluster connection)", severity="warning")
            return
        if self._transfer_in_flight:
            self.notify("A transfer is already in progress", severity="warning")
            return
        # The stream would race the teardown/retarget and could address
        # whichever cluster wins — refuse up front.
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch

        ns, name = self._selected_ns_name()
        if ns is None or name is None:
            return
        namespace = ns

        summary = self._find_pod(namespace, name)
        containers = summary.containers if summary is not None else ()
        # Bind the transfer to this pod *incarnation*: the exec API addresses
        # the pod by namespace/name only, so the uid is re-verified in
        # _run_transfer right before streaming (a same-named replacement
        # created while the dialogs are open must never receive the bytes).
        uid = (summary.uid or None) if summary is not None else None
        if len(containers) > 1:

            def _on_pick(container: str | None) -> None:
                if container is not None:
                    self._open_transfer_dialog(namespace, name, container, uid, epoch)

            self.push_screen(PickScreen(f"Container in {name}:", list(containers)), _on_pick)
            return
        self._open_transfer_dialog(
            namespace, name, containers[0] if containers else None, uid, epoch
        )

    def _open_transfer_dialog(
        self, namespace: str, name: str, container: str | None, uid: str | None, epoch: int
    ) -> None:
        target = f"{namespace}/{name}" + (f" ({container})" if container else "")

        def _on_spec(spec: TransferSpec | None) -> None:
            if spec is not None:
                self._start_transfer(namespace, name, container, spec, uid, epoch)

        self.push_screen(
            TransferScreen(
                target,
                remote_lister=self._remote_lister(namespace, name, container, uid, epoch),
            ),
            _on_spec,
        )

    def _remote_lister(
        self, namespace: str, name: str, container: str | None, uid: str | None, epoch: int
    ) -> Callable[[str], Awaitable[list[RemoteEntry]]] | None:
        """Directory-listing callable for the ctrl+o remote path picker.

        A read-only `ls` over the exec API (issue #124): names only, so the
        masking pipeline does not apply, and it is never exposed to the
        agent — browsing is user-driven like the transfer itself.

        Bound to the dialog's context *epoch* and pod *uid*: a :ctx switch
        retargets the shared exec client, and a same-named replacement pod
        does not change the epoch — either way the listing would come from
        somewhere other than what the dialog shows. Raises TransferError
        (the picker's degradation path) when either binding is stale,
        checked before each exec and again after the await so a listing
        that raced the change is discarded.
        """
        open_pod_exec = self._open_pod_exec
        if open_pod_exec is None:
            return None

        async def _guard() -> None:
            def check_epoch() -> None:
                if self._ctx_switch_crossed(epoch):
                    raise TransferError(
                        f"the kube context changed while the dialog for {namespace}/{name} was open"
                    )

            check_epoch()
            await self._verify_listing_pod(namespace, name, uid)
            # The uid lookup awaited the manifest source: a switch that
            # completed during that await retargeted the shared exec client,
            # so re-check before any exec follows.
            check_epoch()

        async def _list(path: str) -> list[RemoteEntry]:
            await _guard()

            def open_exec(
                command: list[str], stdin: bool
            ) -> contextlib.AbstractAsyncContextManager[Any]:
                return open_pod_exec(namespace, name, container, command, stdin=stdin)

            entries = await list_remote_dir(open_exec, path)
            # Re-check: a listing that raced a switch or a pod replacement
            # must never be presented under the old selection.
            await _guard()
            return entries

        return _list

    async def _verify_listing_pod(self, namespace: str, name: str, uid: str | None) -> None:
        """Raise TransferError unless pod `uid` is still the incarnation the
        transfer dialog was opened for. Fails open when no uid was captured
        (matching the transfer's own uid gate in ui/transfer.py); with a
        captured uid an unverifiable lookup fails closed — browsing is
        optional, so degrading to manual entry beats listing a same-named
        replacement pod."""
        if uid is None:
            return
        try:
            current = await self._target_uid("pods", namespace, name)
        except ApiStatusError as exc:
            raise TransferError(f"pod {name} no longer exists") from exc
        if current is None:
            raise TransferError(f"pod {name} could not be verified — enter the path manually")
        if current != uid:
            raise TransferError(f"pod {name} was replaced since the dialog was opened")

    def _start_transfer(
        self,
        namespace: str,
        name: str,
        container: str | None,
        spec: TransferSpec,
        uid: str | None,
        epoch: int,
    ) -> None:
        """Gate then launch: uploads write into the container filesystem, so
        they are blocked in read-only mode and pass the approval dialog."""
        if self._ctx_switching or epoch != self._ctx_epoch:
            # The picker/transfer dialogs stayed open across a context
            # switch: the pod selection (and its uid, which fails open when
            # missing) belongs to the old cluster while the shared exec
            # client now targets the new one.
            self.notify(
                f"transfer to {namespace}/{name} cancelled - the kube context"
                " changed while the dialog was open",
                severity="warning",
            )
            return
        if spec.direction == "upload":
            if self.config.readonly:
                self.notify("Upload disabled in read-only mode", severity="warning")
                return

            def _approved(approved: bool | None) -> None:
                if not approved:
                    return
                if self._ctx_switching or epoch != self._ctx_epoch:
                    self.notify(
                        f"transfer to {namespace}/{name} cancelled - the kube"
                        " context changed while the approval was open",
                        severity="warning",
                    )
                    return
                self.run_worker(self._run_transfer(namespace, name, container, spec, uid))

            self.push_screen(
                self._confirm_screen(
                    f"Upload file to {namespace}/{name}",
                    f"{spec.local_path} → {container or 'pod'}:{spec.remote_path}\n"
                    "This writes into the container filesystem.",
                ),
                _approved,
            )
            return
        self.run_worker(self._run_transfer(namespace, name, container, spec, uid))

    @_tracks_cluster_write
    async def _run_transfer(
        self,
        namespace: str,
        name: str,
        container: str | None,
        spec: TransferSpec,
        uid: str | None,
    ) -> None:
        """Delegate to the transfer controller under the cluster-write
        counter, so `:ctx` switching sees the transfer as in flight."""
        await self._transfer.run(namespace, name, container, spec, uid)

    async def _show_transfer_progress(self, label: str) -> TransferProgressScreen:
        """Widget access stays here: the controller only sees the protocol."""
        screen = TransferProgressScreen(label)
        await self.push_screen(screen)
        return screen

    def _close_transfer_progress(self, screen: TransferProgress) -> None:
        if self.screen is screen:  # type: ignore[comparison-overlap]  # protocol narrows the Screen type away
            self.pop_screen()

    def on_transfer_cancel_requested(self, message: TransferCancelRequested) -> None:
        message.stop()
        self._transfer.cancel()

    @property
    def _transfer_in_flight(self) -> bool:
        """True from transfer-worker launch through the outcome audit; a
        single task slot only works with one transfer at a time, so a
        second launch is refused for its whole lifecycle (issue #47)."""
        return self._transfer.in_flight

    @property
    def _transfer_task(self) -> asyncio.Task[int] | None:
        """The in-flight transfer stream; the progress screen's escape
        cancels it (never the surrounding worker)."""
        return self._transfer.task

    def _run_shell(self, namespace: str, name: str, container: str | None) -> None:
        """Run kubectl exec; offer the kubectl debug fallback only if sh is missing."""
        epoch = self._ctx_epoch
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
            self.call_from_thread(
                self._schedule_debug_offer, namespace, name, container, exit_code, epoch
            )

        self.run_worker(_probe_and_maybe_offer, thread=True)

    def _schedule_debug_offer(
        self, namespace: str, name: str, container: str | None, exit_code: int, epoch: int
    ) -> None:
        """Sync shim for call_from_thread: the offer itself is async because
        it awaits the RBAC pre-check."""
        self.run_worker(self._offer_debug_fallback(namespace, name, container, exit_code, epoch))

    async def _offer_debug_fallback(
        self, namespace: str, name: str, container: str | None, exit_code: int, epoch: int
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
        if self._ctx_switching or epoch != self._ctx_epoch:
            # The probe/RBAC/manifest awaits crossed a context switch: the
            # offer describes an old-cluster pod while kubectl debug would
            # now target the new context.
            self.notify(
                f"Debug fallback for {target} cancelled - the kube context changed",
                severity="warning",
            )
            return
        if len(self.screen_stack) > 1:
            # The probe/RBAC pre-check ran concurrently with user input: never
            # stack the offer over a dialog that opened meanwhile.
            self.notify(
                f"Debug fallback for {target} not offered - another dialog is open."
                " Close it and press 's' again to retry.",
                severity="warning",
            )
            return
        self._pick_debug_image(
            namespace, name, container, exit_code, approved_uid, manifest or {}, epoch
        )

    def _pick_debug_image(
        self,
        namespace: str,
        name: str,
        container: str | None,
        exit_code: int,
        approved_uid: str | None,
        manifest: dict[str, Any],
        epoch: int,
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
                            namespace, name, container, exit_code, approved_uid, image, epoch
                        )

                self.push_screen(ImagePrompt(target), _on_custom)
                return
            self._confirm_debug(
                namespace, name, container, exit_code, approved_uid, prompts[choice], epoch
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
        epoch: int,
    ) -> None:
        """Approval gate for the debug fallback with the chosen image.

        ConfirmScreen, not a generic picker: its creation-time key cutoff
        discards any input buffered before the prompt existed - a queued
        Enter or y must never start a pod mutation the user has not seen.
        """
        target = f"{name}/{container}" if container else name

        def _on_choice(confirmed: bool | None) -> None:
            if not confirmed:
                return
            if self._ctx_switching or epoch != self._ctx_epoch:
                # The image picker / approval stayed open across a context
                # switch: kubectl debug would mutate a same-named pod on the
                # new cluster (the uid re-check fails open without a uid).
                self.notify(
                    f"Debug fallback for {target} cancelled - the kube context changed",
                    severity="warning",
                )
                return
            self.run_worker(self._run_debug(namespace, name, container, approved_uid, image))

        self.push_screen(
            self._confirm_screen(
                f"Shell failed in {target} (exit {exit_code})",
                f"kubectl debug: attach a {image} debug container to pod"
                f" {name}{self._write_locus(namespace)} - the target image likely"
                " has no sh/bash (distroless). Note: the ephemeral container stays"
                " in the pod spec until restart.",
            ),
            _on_choice,
        )

    @_tracks_cluster_write
    async def _run_debug(
        self,
        namespace: str,
        name: str,
        container: str | None,
        approved_uid: str | None,
        image: str = DEBUG_IMAGE,
    ) -> None:
        """Delegate the gated, audited kubectl debug run to the controller
        (issue #97 U3c); the decorator keeps the write counted against
        `:ctx` switching, and worker ownership stays with the callers."""
        await self._debug.run(namespace, name, container, approved_uid, image)

    async def _pod_uid_unchanged(
        self, namespace: str, name: str, approved_uid: str, *, action: str
    ) -> bool:
        """Re-verify the approved pod incarnation just before `action`
        executes; notifies and returns False when the pod is gone or replaced."""
        try:
            current_uid = await self._target_uid("pods", namespace, name)
        except ApiStatusError:
            self.notify(
                f"{action} cancelled - pod {name} no longer exists.",
                severity="warning",
            )
            return False
        if current_uid is not None and current_uid != approved_uid:
            self.notify(
                f"{action} cancelled - pod {name} was replaced since the prompt was shown.",
                severity="warning",
            )
            return False
        return True

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
            self._confirm_screen(
                f"Image pull failed for {image} ({reason})",
                f"Retry kubectl debug on {target}{self._write_locus(namespace)} with"
                f" {fallback}? Note: the failed ephemeral container entry cannot be"
                " removed from the pod spec; the retry attaches an additional"
                " container.",
            ),
            _on_choice,
        )

    async def on_key(self, event: Key) -> None:
        """Pane chords (`ctrl+w` v/w/q) and Escape (closes describe/log
        panes, then pops one drill-down level)."""
        if self._pane_chord_pending or event.key == "ctrl+w":
            await self._handle_pane_chord(event)
            return
        if event.key != "escape":
            return
        if len(self.screen_stack) > 1:
            # A modal owns this Escape (its close binding handles it):
            # neither a drill pop nor a hierarchy return may piggyback on
            # the keystroke that merely dismissed Help or a dialog.
            return
        filter_bar = self._filter_bar
        command_bar = self._command_bar
        namespace_picker = self._namespace_picker
        if filter_bar.display or command_bar.display or namespace_picker.display:
            return  # bars and pickers own Escape while open
        describe_pane = self._describe_pane
        if describe_pane.display:
            # An active pane search (input open or submitted hits) consumes
            # Escape first; a second Escape closes the pane itself.
            if not describe_pane.dismiss_search():
                describe_pane.hide()
            event.stop()
            return
        log_pane = self._log_pane
        if log_pane.display:
            await self._close_log_pane()
            event.stop()
            return
        popped = await self._pop_drill()
        if popped:
            event.stop()
            return
        # No drill level left: a pending hierarchy return (issue #135)
        # reopens the component tree the last goto jumped away from.
        if await self._reopen_hierarchy_return():
            event.stop()
            # Without this the same Escape continues into binding
            # processing and hits the freshly pushed tree's own
            # escape=close binding, dismissing it on arrival.
            event.prevent_default()

    # -- 2-pane split workspace (issue #48) ---------------------------------

    async def _handle_pane_chord(self, event: Key) -> None:
        """`ctrl+w` chord state machine: the prefix always swallows the next
        key - an unmapped second key must not fall through to normal
        handling (q would quit)."""
        if not self._pane_chord_pending:
            # Arm only while a table is focused: with an Input (command/
            # filter bar) focused the second key never reaches App.on_key,
            # which would orphan the pending flag and silently swallow the
            # next table keypress after the bar closes.
            if not isinstance(self.focused, ResourceTable):
                return
            self._pane_chord_pending = True
            event.stop()
            event.prevent_default()
            return
        self._pane_chord_pending = False
        event.stop()
        event.prevent_default()
        if event.key == "v":
            await self._split_pane()
        elif event.key in ("w", "ctrl+w"):
            self._focus_other_pane()
        elif event.key == "q":
            await self._close_focused_pane()

    async def _split_pane(self) -> None:
        """`ctrl+w v`: clone the focused view into a second pane and focus it."""
        if len(self._panes) >= 2:
            self.notify("workspace is already split - ctrl+w q closes a pane", severity="warning")
            return
        # The pane list and watch lifecycle are also mutated by navigation:
        # take the same lock so a concurrent `:view`/`:ns` transition never
        # interleaves with the split's pane snapshot and watch start.
        async with self._nav_lock:
            if len(self._panes) >= 2:
                return  # lost the race to another split
            source = self._pane
            self._pane_counter += 1
            pane = source.clone(f"pane-{self._pane_counter}")
            table = ResourceTable(id=pane.table_id)
            await self.query_one("#workspace", Horizontal).mount(table)
            for pane_table in self.query(ResourceTable):
                pane_table.add_class("split-pane")
            self._panes.append(pane)
            self._focused_pane = 1
            # start() is idempotent - the clone usually shares the source's watch.
            await self.watch_manager.start(pane.kind, pane.scope)
        # A single-pane empty-state overlay must not linger over the split;
        # each pane's own content is the guidance now.
        self.query_one("#empty-state", Static).display = False
        # Render only the new pane: the source is already current, and a
        # repaint would reset its cursor/scroll.
        self._render_table(pane.kind, only=pane)
        self._update_pane_focus_classes()
        table.focus()
        self._refresh_status()

    def _update_pane_focus_classes(self) -> None:
        """Mark the command-routing target with `focused-pane`. A class, not
        `:focus`: opening the command/filter bar or agent panel moves keyboard
        focus to an Input, but `_focused_pane` still decides where the command
        goes - the indicator must not vanish at that moment."""
        for index, pane in enumerate(self._panes):
            try:
                table = self.query_one(f"#{pane.table_id}", ResourceTable)
            except NoMatches:
                continue
            table.set_class(len(self._panes) > 1 and index == self._focused_pane, "focused-pane")
        # Every focused-pane change funnels through here; the panes may show
        # different kinds, so the view-scoped footer legend must follow the
        # focus (issue #114).
        self.refresh_bindings()

    def _focus_other_pane(self) -> None:
        """`ctrl+w w`: move focus (commands, filters, keybindings) across."""
        if len(self._panes) < 2:
            return
        self._focused_pane = 1 - self._focused_pane
        self._update_pane_focus_classes()
        self._focused_table().focus()
        self._hints.refresh_for_focus()
        self._refresh_status()

    async def _close_focused_pane(self) -> None:
        """`ctrl+w q`: back to the single view; the other pane survives."""
        if len(self._panes) < 2:
            return
        async with self._nav_lock:
            if len(self._panes) < 2:
                return  # lost the race to another close
            closing = self._panes.pop(self._focused_pane)
            remaining = self._panes[0]
            self._focused_pane = 0
            if self._log_pane_owner is closing:
                # The pane whose selection drove the stream is gone: don't
                # leave orphaned logs pinned over the survivor's view.
                await self._close_log_pane()
            if (closing.kind, closing.scope) != (remaining.kind, remaining.scope):
                await self.watch_manager.stop(closing.kind, closing.scope)
            # The survivor keeps its own table widget - and with it the
            # cursor/scroll state the user had in that pane.
            await self.query_one(f"#{closing.table_id}", ResourceTable).remove()
            self.query_one(f"#{remaining.table_id}", ResourceTable).remove_class("split-pane")
            await self._sync_metrics_poller()
        self._update_pane_focus_classes()
        # No repaint: `show()` clears and re-adds rows, which would reset the
        # survivor's cursor/scroll; its table is already current. The
        # single-pane empty-state does need a refresh (an empty survivor
        # must show guidance, and a stale overlay must clear).
        table = self._focused_table()
        self._refresh_empty_state(remaining.kind, table.row_count)
        table.focus()
        self._hints.refresh_for_focus()
        self._refresh_status()

    async def _collapse_split(self) -> None:
        """Fold the workspace back to a single pane (context-switch teardown).

        The caller already holds the nav lock and stops all watches
        wholesale right after, so this only removes the extra pane's state
        and table widget. The survivor is pane 0; the switch resets its
        kind/scope/filter afterwards, so which pane survives is cosmetic.
        """
        if len(self._panes) < 2:
            return
        closing = self._panes.pop(1)
        self._focused_pane = 0
        remaining = self._panes[0]
        await self.query_one(f"#{closing.table_id}", ResourceTable).remove()
        self.query_one(f"#{remaining.table_id}", ResourceTable).remove_class("split-pane")
        self._update_pane_focus_classes()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        """Clicking a pane focuses it - command routing must follow. Any
        focus change also disarms a pending `ctrl+w` chord: the second key
        would go to the newly focused widget, leaving the flag set to
        swallow a later table keypress."""
        self._pane_chord_pending = False
        widget = event.widget
        if not isinstance(widget, ResourceTable):
            return
        for index, pane in enumerate(self._panes):
            if pane.table_id == widget.id:
                if index != self._focused_pane:
                    self._focused_pane = index
                    self._update_pane_focus_classes()
                    self._hints.refresh_for_focus()
                    self._refresh_status()
                return

    def on_descendant_blur(self, event: DescendantBlur) -> None:
        """Overlay widgets (command/filter bars, describe/log panes) hide
        themselves while focused. The tables now live inside #workspace, so
        Textual's sibling-fallback in `_reset_focus` finds nothing focusable
        and focus drops to None - restore it to the focused pane's table."""
        del event
        self.call_later(self._restore_table_focus)

    def _restore_table_focus(self) -> None:
        """Refocus the focused pane's table when nothing else holds focus."""
        if self.focused is not None or len(self.screen_stack) != 1:
            return
        if self.query(ResourceTable):
            self._focused_table().focus()

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
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch

        log_pane = self._log_pane
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
            await self._toggle_log_pod(ns, name, epoch)
            return

        self._log_pane_mode = "l"
        triples = self._pod_triples(ns, name)
        await self._open_log_pane(
            ns, [(pod, ctr) for _, pod, ctr in triples], triples=triples, epoch=epoch
        )

    def _pod_triples(self, namespace: str, name: str) -> list[tuple[str, str, str]]:
        """Return (ns, pod, container) triples for one pod (one per container)."""
        containers = self._get_pod_containers(namespace, name)
        if containers:
            return [(namespace, name, ctr) for ctr in containers]
        return [(namespace, name, "")]

    async def _toggle_log_pod(self, namespace: str, name: str, epoch: int) -> None:
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
        await self._open_log_pane(triples[0][0], sources, triples=triples, epoch=epoch)

    async def action_logs_multi(self) -> None:
        """Stream all filtered pods' containers (``L`` binding); cap at 8."""
        if self.current_kind != "pods":
            self.notify("Logs are only available for pods", severity="warning")
            return
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch

        if self._stream_logs is None:
            self.notify("Log streaming unavailable", severity="warning")
            return

        table = self._focused_table()
        if table.row_count == 0:
            self.notify("No resource selected", severity="warning")
            return

        triples = self._build_multi_stream_triples(table)
        if not triples:
            self.notify("No pods to stream", severity="warning")
            return

        if self._log_pane.display:
            await self._close_log_pane()

        self._log_pane_mode = "L"
        ns0 = triples[0][0]
        await self._open_log_pane(
            ns0,
            [(pod, ctr) for _, pod, ctr in triples],
            triples=triples,
            force_prefix=True,
            epoch=epoch,
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
        table = self._focused_table()
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
        summary = self._find_pod(namespace, name)
        return summary.containers if summary is not None else ()

    def _find_pod(self, namespace: str, name: str) -> PodSummary | None:
        """The store's PodSummary for the selection, or None once it is gone."""
        for obj in self.store.get(self.current_kind, self.current_scope):
            if obj.namespace == namespace and obj.name == name and isinstance(obj, PodSummary):
                return obj
        return None

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
        # Operator uninstall deletes the Subscription (then its CSV); the
        # pre-check and 403 messages therefore speak in delete terms.
        "uninstall": ("delete", ""),
        # Cordon/uncordon patch node.spec.unschedulable; the drain pre-check
        # covers its cordon step (evictions are per-namespace pod
        # subresource creations that surface individually during execution).
        "cordon": ("patch", ""),
        "uncordon": ("patch", ""),
        "drain": ("patch", ""),
        # Node shell creates a privileged debug pod in the shell namespace
        # (kubectl debug node/, issue #46); the pre-check runs against pods.
        "node-shell": ("create", ""),
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
        *,
        phase: str = "the permission check",
        epoch: int,
    ) -> bool:
        """Re-validate after an awaited gap (the RBAC round-trip, a dry-run
        preview, or an editor session - named by ``phase`` so cancellation
        messages state the true cause), before pushing a dialog: the user may
        have opened another screen or moved the selection meanwhile - and
        keystrokes typed during the await must never land on a confirmation
        they did not see. ``epoch`` (captured when the write flow began) also
        aborts on a context switch that started - or fully completed - during
        the gap: a same-named row on the new cluster would otherwise satisfy
        the selection checks. Abort (with a notification) unless everything
        still matches."""
        if self._ctx_switching or epoch != self._ctx_epoch:
            self.notify(
                f"{action} {self._gvr_label(meta)}/{name} cancelled -"
                f" the kube context changed during {phase}",
                severity="warning",
            )
            return False
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
        if self._ctx_switching:
            # The write would race the teardown/retarget and could execute
            # against whichever cluster wins — refuse up front.
            self.notify(
                "A context switch is in progress — try again once it completes",
                severity="warning",
            )
            return False
        epoch = self._ctx_epoch
        if not await self._permitted(action, meta, ns, name):
            return False
        # The permission check awaited network I/O — a switch may have
        # started (flag) or fully completed (epoch) meanwhile; the approved
        # intent must not land on a different cluster.
        return self._write_context_intact(action, meta, ns, name, epoch=epoch)

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

    @_tracks_cluster_write
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
        'blocked: ...' / 'failed: ...') for callers that report back.

        The whole span publishes an in-flight progress label (issue #143):
        between approval and the outcome toast there was previously no
        visible state at all."""
        kind = meta.plural
        with self._progress(f"{action} {kind}/{name}"):
            return await self._run_write_inner(action, meta, namespace, name, op, detail, kind)

    async def _run_write_inner(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        op: Awaitable[None],
        detail: str,
        kind: str,
    ) -> str:
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

    async def _push_write_confirmation(
        self,
        title: str,
        operation: str,
        *,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        op_factory: Callable[[], Awaitable[None]],
        detail: str = "",
        require_name: str | None = None,
        preview: list[str] | None = None,
        preview_title: str = "server dry-run preview:",
        managed_note: str | None = None,
    ) -> None:
        """The standard write-approval flow (issue #91 U1): push a confirm
        dialog and, on approval, launch `_run_write` on an app-owned worker.

        Takes an operation *factory*, not a coroutine: a declined dialog
        must never construct the mutation coroutine (nothing to leak
        unawaited, no side effects before approval). Flows with extra
        semantics — operator install's in-callback UID recheck, drain's
        dedicated worker, the agent gate's approval future — stay explicit.
        """

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(
                    self._run_write(action, meta, namespace, name, op_factory(), detail=detail)
                )

        await self.push_screen(
            self._confirm_screen(
                title,
                operation,
                require_name=require_name,
                preview=preview,
                preview_title=preview_title,
                managed_note=managed_note,
            ),
            _done,
        )

    async def action_delete_resource(self) -> None:
        """Ctrl-D: delete the selected resource behind a layered confirmation
        (cluster-scoped kinds require typing the resource name). On the helm
        release browser the key means `helm uninstall` (issue #117) - helm
        must remove the release's own bookkeeping, a raw Secret delete would
        orphan the deployed resources."""
        current = self.aliases.get(self._canonical_kind(self.current_kind))
        if current is not None and (current.group, current.plural) == (
            HELM_RELEASES_META.group,
            HELM_RELEASES_META.plural,
        ):
            self._helm_uninstall_start()
            return
        ops = self._write_ops
        if ops is None:
            self.notify("Delete unavailable in this session", severity="warning")
            return
        target = self._write_target()
        if target is None:
            return
        meta, ns, name, uid = target
        if (meta.group, meta.plural) == (OPERATORS_GROUP, "subscriptions"):
            # An OLM Subscription: deleting it alone leaves the operator
            # running (the CSV stays) - offer the full uninstall instead.
            await self._start_operator_uninstall(
                meta,
                ns,
                name,
                uid,
                fetch_kind=self._canonical_kind(self.current_kind),
                ctx=(meta, ns, name),
            )
            return
        if (meta.group, meta.plural) == (
            OPERATORS_GROUP,
            "clusterserviceversions",
        ) and await self._csv_uninstall_redirect(meta, ns, name):
            return
        epoch = self._ctx_epoch
        # Captured with the target: view state is mutable across the awaits
        # below, and the banner must describe the row the user acted on.
        kind_alias = self._canonical_kind(self.current_kind)
        if not await self._precheck_keybinding_write("delete", meta, ns, name):
            return
        preview = await self._dry_run_preview(ops.preview_delete(meta, ns, name, uid=uid))
        note = await self._managed_note(kind_alias, ns, name)
        if not self._write_context_intact(
            "delete", meta, ns, name, phase="the dry-run preview", epoch=epoch
        ):
            return
        operation = f"DELETE {self._gvr_label(meta)}/{name}{self._write_locus(ns)}"
        require = None if meta.namespaced else name
        await self._push_write_confirmation(
            f"Delete {self._gvr_label(meta)}/{name}?",
            operation,
            action="delete",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.delete_object(meta, ns, name, uid=uid),
            require_name=require,
            preview=preview,
            managed_note=note,
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
        epoch = self._ctx_epoch
        # Captured with the target — see action_delete_resource.
        kind_alias = self._canonical_kind(self.current_kind)
        if not await self._precheck_keybinding_write("rollout_restart", meta, ns, name):
            return
        # One stamp per approval: the previewed request and the executed
        # write are byte-identical (exact-replay guarantee).
        stamp = restart_stamp()
        preview = await self._dry_run_preview(
            ops.preview_rollout_restart(meta, ns, name, uid=uid, restarted_at=stamp)
        )
        note = await self._managed_note(kind_alias, ns, name)
        if not self._write_context_intact(
            "rollout_restart", meta, ns, name, phase="the dry-run preview", epoch=epoch
        ):
            return

        await self._push_write_confirmation(
            f"Rollout restart {self._gvr_label(meta)}/{name}?",
            f"PATCH {self._gvr_label(meta)}/{name} pod template (restartedAt annotation)"
            f"{self._write_locus(ns)}",
            action="rollout_restart",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.rollout_restart_with_stamp(
                meta, ns, name, uid=uid, restarted_at=stamp
            ),
            preview=preview,
            managed_note=note,
        )

    async def _fetch_manifest_for_edit(
        self,
        label: str,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        epoch: int,
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
        if not self._write_context_intact(
            "edit", meta, ns, name, phase="the manifest fetch", epoch=epoch
        ):
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
        epoch = self._ctx_epoch
        if not await self._precheck_keybinding_write("edit", meta, ns, name):
            return
        label = f"{self._gvr_label(meta)}/{name}"
        manifest = await self._fetch_manifest_for_edit(label, meta, ns, name, epoch=epoch)
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
        if not self._write_context_intact(
            "edit", meta, ns, name, phase="the editor session", epoch=epoch
        ):
            return
        detail = self._edit_detail(manifest, edited)
        # The pre-edit manifest is already in hand — the banner costs at
        # most the owner-chain walk. That walk is another awaited gap:
        # re-validate the selection after it, like every other pre-dialog
        # await, before pushing the confirmation.
        note = await self._managed_note_from(manifest, ns)
        if not self._write_context_intact(
            "edit", meta, ns, name, phase="the ownership lookup", epoch=epoch
        ):
            return
        await self._push_write_confirmation(
            f"Apply edited {label}?",
            # Issue #21: the approval dialog summarizes the change, not
            # just the target and verb.
            f"PUT {label}{self._write_locus(ns)} - {detail}",
            action="edit",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.replace_object(meta, ns, name, edited, uid=uid),
            detail=detail,
            managed_note=note,
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
        epoch = self._ctx_epoch
        # Captured with the target — see action_delete_resource.
        kind_alias = self._canonical_kind(self.current_kind)
        if not await self._precheck_keybinding_write("scale", meta, ns, name):
            return
        current = self._current_replicas(ns, name)

        def _on_replicas(replicas: int | None) -> None:
            if replicas is None:
                return
            # The dry-run round trip must not run inside a screen callback:
            # a worker fetches the preview, revalidates, then confirms.
            self.run_worker(
                self._confirm_scale(meta, ns, name, uid, current, replicas, epoch, kind_alias)
            )

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
        epoch: int,
        kind_alias: str,
    ) -> None:
        """Dry-run preview + approval dialog for a scale, after the replica
        count is known. Revalidates the selection after the preview round
        trip: keystrokes during the await must never land on a confirmation
        for a different row. `kind_alias` was captured with the target — the
        banner must describe the row the user acted on, not the current view."""
        ops = self._write_ops
        if ops is None:
            return
        preview = await self._dry_run_preview(ops.preview_scale(meta, ns, name, replicas, uid=uid))
        note = await self._managed_note(kind_alias, ns, name)
        if not self._write_context_intact(
            "scale", meta, ns, name, phase="the dry-run preview", epoch=epoch
        ):
            return

        shown = "?" if current is None else current
        await self._push_write_confirmation(
            f"Scale {self._gvr_label(meta)}/{name}?",
            f"PATCH {self._gvr_label(meta)}/{name}/scale: replicas {shown} -> {replicas}"
            f"{self._write_locus(ns)}",
            action="scale",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.scale_object(meta, ns, name, replicas, uid=uid),
            detail=f"replicas -> {replicas}",
            preview=preview,
            managed_note=note,
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
        epoch = self._ctx_epoch
        if not await self._precheck_keybinding_write("resize", meta, ns, name):
            return
        fetched = await self._pod_container_resources(ns, name)
        if fetched is None:
            return
        containers, pod_manifest = fetched
        if not self._write_context_intact(
            "resize", meta, ns, name, phase="the manifest fetch", epoch=epoch
        ):
            return

        def _on_resources(resources: dict[str, dict[str, dict[str, str]]] | None) -> None:
            if not resources:
                return
            # The dry-run round trip must not run inside a screen callback:
            # a worker fetches the preview, revalidates, then confirms.
            self.run_worker(
                self._confirm_resize(meta, ns, name, uid, resources, epoch, pod_manifest)
            )

        await self.push_screen(
            ResizePrompt(f"{self._gvr_label(meta)}/{name}", containers=containers), _on_resources
        )

    async def _pod_container_resources(
        self, ns: str | None, name: str
    ) -> tuple[list[tuple[str, dict[str, dict[str, str]]]], dict[str, Any]] | None:
        """Current per-container requests/limits from the live manifest, in
        spec order, to prefill the resize prompt — plus the manifest itself,
        so the ownership banner can reuse the snapshot instead of a second
        GET. None (with a notification) when the manifest cannot be
        fetched."""
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
        return containers, manifest

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
        epoch: int,
        pod_manifest: dict[str, Any],
    ) -> None:
        """Dry-run preview + approval dialog for an in-place pod resize.
        Revalidates the selection after the preview round trip: keystrokes
        during the await must never land on a confirmation for a different
        row. `pod_manifest` is the snapshot the prompt was prefilled from —
        the banner reuses it instead of refetching the same object."""
        ops = self._write_ops
        if ops is None:
            return
        namespace = ns or ""
        preview = await self._dry_run_preview(
            ops.preview_resize(namespace, name, resources, uid=uid)
        )
        note = await self._managed_note_from(pod_manifest, ns)
        if not self._write_context_intact(
            "resize", meta, ns, name, phase="the dry-run preview", epoch=epoch
        ):
            return
        summary = self._resize_summary(resources)
        await self._push_write_confirmation(
            f"Apply in-place pod resize to pods/{name}?",
            f"PATCH pods/{name}/resize: {summary}{self._write_locus(ns)}",
            action="resize",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.resize_pod(namespace, name, resources, uid=uid),
            detail=summary,
            preview=preview,
            managed_note=note,
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

    def _helm_view_guard(self, meta: ResourceMeta, what: str) -> bool:
        """`check_action` gates only key dispatch (issue #114); a direct
        action call must not trust the focused row of an unrelated view as a
        Helm release/revision name and open a write flow with it."""
        current = self.aliases.get(self._canonical_kind(self.current_kind))
        if current is not None and (current.group, current.plural) == (meta.group, meta.plural):
            return True
        self.notify(f"{what} is only available on the {meta.plural} view", severity="warning")
        return False

    def action_helm_install(self) -> None:
        """i on the helm browser: start the chart install wizard (issue #31).
        Synchronous: the picker must open with the keypress, before any
        buffered navigation input could change the view the namespace is
        derived from."""
        if not self._helm_view_guard(HELM_RELEASES_META, "Helm install"):
            return
        self._helm_install_flow()

    def action_helm_upgrade(self) -> None:
        """u on the helm browser: upgrade the selected release (issue #31).
        Synchronous: the picker opens with the keypress; buffered cursor
        keys must not retarget the upgrade."""
        if not self._helm_view_guard(HELM_RELEASES_META, "Helm upgrade"):
            return
        self._helm_upgrade_flow()

    async def action_helm_history(self) -> None:
        """h on the helm release browser: the flat revision drill-down.
        Revision history moved off Enter when Enter became the hierarchy
        tree (issue #120); rollback keeps working from the revisions view."""
        if not self._helm_view_guard(HELM_RELEASES_META, "Helm history"):
            return
        namespace, name = self._selected_ns_name()
        if namespace is None or name is None:
            return
        error = await self._drill_into(namespace, name)
        if error is not None:
            self.notify(error, severity="warning")

    def action_helm_rollback(self) -> None:
        """r on the helm revision drill-down: roll the release back to the
        selected revision (issue #31). Target captured synchronously with
        the keypress; only the slow preview + confirmation run in a worker
        (the diff render can block for up to _HELM_PREVIEW_TIMEOUT and must
        not freeze the message pump, or the status-bar progress could never
        paint)."""
        if not self._helm_view_guard(HELM_REVISIONS_META, "Helm rollback"):
            return
        helm = self._helm_gate()
        if helm is None:
            return
        epoch = self._ctx_epoch
        ns, name = self._selected_ns_name()
        if name is None:
            return
        row = self._helm_revision_row(ns, name)
        if row is None:
            self.notify("no helm revision selected", severity="warning")
            return
        namespace = ns or row.namespace
        self.run_worker(
            self._helm_rollback_flow(helm, row, ns, name, namespace, epoch),
            exclusive=True,
            group="helm-write",
        )

    def _helm_uninstall_start(self) -> None:
        """Ctrl+D on the helm release browser: uninstall the selected release
        (issue #117). Target captured synchronously with the keypress; the
        slow dry-run preview + confirmation run in a worker, exactly like
        rollback."""
        helm = self._helm_gate()
        if helm is None:
            return
        epoch = self._ctx_epoch
        ns, name = self._selected_ns_name()
        if name is None:
            return
        row = self._helm_release_row(ns, name)
        if row is None:
            self.notify("no helm release selected", severity="warning")
            return
        namespace = ns or row.namespace
        self.run_worker(
            self._helm_uninstall_flow(helm, row, ns, name, namespace, epoch),
            exclusive=True,
            group="helm-write",
        )

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
        epoch = self._ctx_epoch
        if not await self._precheck_keybinding_write(action, meta, None, name):
            return
        preview = await self._dry_run_preview(ops.preview_cordon(name, unschedulable, uid=uid))
        if not self._write_context_intact(
            action, meta, None, name, phase="the dry-run preview", epoch=epoch
        ):
            return
        flag = "true" if unschedulable else "false"
        await self._push_write_confirmation(
            f"{action.capitalize()} nodes/{name}?",
            f"PATCH nodes/{name} spec.unschedulable={flag}",
            action=action,
            meta=meta,
            namespace=None,
            name=name,
            op_factory=lambda: ops.cordon_node(name, unschedulable, uid=uid),
            detail=f"spec.unschedulable={flag}",
            preview=preview,
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
        epoch = self._ctx_epoch
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
        if not self._write_context_intact(
            "drain", meta, None, name, phase="the drain plan", epoch=epoch
        ):
            return

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self._drain_node = name
                self._drain_worker = self.run_worker(self._run_drain(ops, meta, name, uid, plan))

        blocked_now = sum(1 for t in plan.targets if t.pdb_blocked is not None)
        note = f"; {blocked_now} currently PDB-blocked" if blocked_now else ""
        await self.push_screen(
            self._confirm_screen(
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

    def _set_progress(self, owner: str, label: str) -> None:
        """Publish transient progress on the status bar, scoped to its
        owner (drain, helm preview): overlapping operations must never
        overwrite or clear each other's label. A failure to render must
        never interrupt the operation itself."""
        if label:
            self._progress_labels[owner] = label
        else:
            self._progress_labels.pop(owner, None)
        with contextlib.suppress(Exception):
            self._refresh_status()

    @contextlib.contextmanager
    def _progress(self, label: str) -> Iterator[None]:
        """Status-bar progress scoped exactly to the wrapped await: shown on
        entry, cleared on exit however the operation ends. Each scope gets a
        unique owner token so a cancelled predecessor's late cleanup cannot
        clear the label its exclusive-worker replacement published."""
        self._progress_seq += 1
        owner = f"helm:{self._progress_seq}"
        self._set_progress(owner, label)
        try:
            yield
        finally:
            self._set_progress(owner, "")

    @_tracks_cluster_write
    async def _run_drain(
        self,
        ops: WriteOps,
        meta: ResourceMeta,
        name: str,
        uid: str | None,
        plan: DrainPlan,
    ) -> None:
        """Delegate the approved drain to the controller (issue #97 U3d);
        the decorator keeps the write counted against `:ctx` switching, and
        worker ownership stays with `action_drain_node`."""
        try:
            await self._drain.run(ops, meta, name, uid, plan)
        finally:
            # Last: while the worker is finalizing (outcome audit/notify)
            # the targeted-cancel guard must still see its node.
            self._drain_node = None

    # -- Node shell (issue #46): privileged debug shell via kubectl debug node/

    async def _node_shell_flow(self) -> None:
        """`s` on the nodes view: approval-gated `kubectl debug node/` shell.

        The shell runs in a `node-debugger-…` pod with the node's filesystem
        mounted at `/host` — a privilege escalation, so it always passes the
        approval gate with that stated explicitly, is audit-logged
        fail-closed, and the debug pod is deleted when the shell exits.
        """
        resolved = self._node_target("node shell")
        if resolved is None:
            return
        ops, meta, name, uid = resolved
        if shutil.which("kubectl") is None:
            self.notify("kubectl not found on PATH — node shell requires kubectl", severity="error")
            return
        image = self.config.node_shell_image or DEBUG_IMAGE
        shell_ns = self.config.node_shell_namespace or "default"
        pods_meta = self.aliases.get("pods")
        epoch = self._ctx_epoch
        if pods_meta is None:
            # Fail-open like the pod-debug pre-check, but never silently.
            logger.warning("pods alias missing; skipping node-shell RBAC pre-check (fail-open)")
        elif not await self._permitted("node-shell", pods_meta, shell_ns, ""):
            return
        if not self._write_context_intact("node shell", meta, None, name, epoch=epoch):
            # The RBAC round-trip ran concurrently with user input: the
            # approval must stay bound to the selection that initiated it,
            # and never stack over a dialog that opened meanwhile.
            return

        def _on_choice(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(self._run_node_shell(ops, name, shell_ns, image, uid))

        self.push_screen(
            self._confirm_screen(
                f"Node shell on {name}",
                f"kubectl debug node/{name}: creates a privileged debug pod"
                f" (image {image}) in namespace {shell_ns} with the node's"
                " filesystem mounted at /host (uses --profile=sysadmin;"
                " requires kubectl 1.30+). The pod is deleted when the shell"
                " exits. This action is audit-logged.",
            ),
            _on_choice,
        )

    @_tracks_cluster_write
    async def _run_node_shell(
        self, ops: WriteOps, node: str, namespace: str, image: str, approved_uid: str | None
    ) -> None:
        """Run the approved node shell, then delete the debugger pod.

        A cluster write (pod creation via kubectl): the intent record must
        persist before the subprocess starts, or the shell is blocked.
        kubectl addresses the node by name only, so the approved node
        incarnation is re-verified just before creating the pod (like the
        pod debug path). The pod is created detached (`--attach=false`); its
        name is parsed from kubectl's creation message and its uid fetched
        with an exact `kubectl get pod`, so korvid knows precisely which pod
        it owns: the interactive session then `kubectl attach`es to it, and
        cleanup deletes exactly that pod with a uid precondition — a debugger
        another operator starts meanwhile is never touched.
        """
        audit = self._audit
        if audit is None:  # _node_target already refused; defensive re-check
            return
        if approved_uid is not None and not await self._node_uid_unchanged(node, approved_uid):
            return
        detail = f"privileged node shell (kubectl debug node, image {image}, namespace {namespace})"
        try:
            await asyncio.to_thread(self._audit_node_shell, audit, node, detail, "intent")
        except Exception:
            logger.exception("audit append failed; blocking node shell")
            self.notify("Write blocked: audit log unavailable", severity="error")
            return
        # The create itself is shielded + settled: cancelling an
        # asyncio.to_thread await does not stop the kubectl subprocess, so a
        # cancellation here could otherwise leak a pod that was created
        # moments later with no finalizer installed.
        create_task = asyncio.ensure_future(self._create_node_debug_pod(node, namespace, image))
        try:
            created = await asyncio.shield(create_task)
        except asyncio.CancelledError:
            try:
                created = await create_task
            except asyncio.CancelledError:
                # The create task itself was cancelled outright (e.g. loop
                # shutdown cancels every task, bypassing the shield): nothing
                # to settle, but a pod may still appear — leave a trace.
                logger.warning(
                    "node shell create cancelled outright; cleanup skipped -"
                    " check namespace %s for leftover node-debugger pods",
                    namespace,
                )
                raise
            if isinstance(created, str):
                await self._audit_create_failure(audit, node, detail, created)
            else:
                await self._finalize_node_shell(
                    ops,
                    audit,
                    node,
                    namespace,
                    created[0],
                    created[1],
                    detail,
                    "error: interrupted",
                )
            raise
        if isinstance(created, str):  # creation failed or pod unidentifiable
            await self._audit_create_failure(audit, node, detail, created)
            return
        pod_name, pod_uid = created
        # Everything after a successful create runs under a finalizer:
        # a worker cancellation, an attach launch error, or Ctrl-C raising
        # KeyboardInterrupt from subprocess.call must still delete the
        # privileged host-mounted pod and record the outcome.
        outcome = "error: interrupted"
        try:
            outcome = await self._wait_and_attach_node_shell(node, namespace, pod_name)
        finally:
            # Shielded + settled so a cancelled worker still deletes the pod
            # and records the outcome: shield() raises CancelledError here on
            # outer cancellation while the finalizer keeps running, so it is
            # re-awaited before the cancellation propagates.
            finalize = asyncio.ensure_future(
                self._finalize_node_shell(
                    ops, audit, node, namespace, pod_name, pod_uid, detail, outcome
                )
            )
            try:
                await asyncio.shield(finalize)
            except asyncio.CancelledError:
                await finalize
                raise

    async def _wait_and_attach_node_shell(self, node: str, namespace: str, pod_name: str) -> str:
        """Wait for the debugger pod, attach interactively, return the outcome.

        Runs entirely under the caller's finalizer, so every exit path —
        including an attach binary that cannot be launched — leaves the pod
        deletion and outcome audit to run.
        """
        wait_argv = build_pod_wait_argv(namespace, pod_name, context=self.config.kube_context)
        ready = await self._run_kubectl_ok(wait_argv, timeout=75)
        if not ready:
            self.notify(
                f"Debugger pod {pod_name} did not become Ready — the shell may"
                " fail to attach (image pull error or admission problem?)",
                severity="warning",
            )
        attach_argv = build_pod_attach_argv(namespace, pod_name, context=self.config.kube_context)
        try:
            with self.suspend():
                exit_code = self._run_interactive(
                    attach_argv, f"korvid node shell → {node} (exit to return)"
                )
        except SuspendNotSupported:
            # Non-suspending drivers (e.g. Windows, web): refuse gracefully —
            # the finalizer still deletes the pod that was just created.
            self.notify(
                "node shell unavailable: this environment does not support"
                " suspending the TUI for an interactive shell",
                severity="error",
            )
            outcome = "error: suspend not supported"
        except OSError as exc:
            # kubectl itself could not be launched (removed or not executable
            # since the create): keep a specific outcome and let the finalizer
            # delete the pod — an escaping exception would kill the worker and
            # take the TUI down with it.
            logger.warning("kubectl attach could not be launched", exc_info=True)
            self.notify(f"Could not launch kubectl attach: {exc}", severity="error")
            outcome = "error: attach could not be launched"
        else:
            outcome = "success" if exit_code == 0 else f"error: exit {exit_code}"
        self.refresh()
        return outcome

    async def _audit_create_failure(
        self, audit: AuditLog, node: str, detail: str, outcome: str
    ) -> None:
        """Persist a failed/unidentifiable create outcome; surfaced on
        failure because the outcome may record a skipped cleanup the user
        must act on."""
        try:
            await asyncio.to_thread(self._audit_node_shell, audit, node, detail, outcome)
        except Exception:
            logger.exception("audit append failed after node shell create failure")
            self.notify("Audit write failed for the node shell attempt", severity="warning")

    async def _finalize_node_shell(
        self,
        ops: WriteOps,
        audit: AuditLog,
        node: str,
        namespace: str,
        pod_name: str,
        pod_uid: str,
        detail: str,
        outcome: str,
    ) -> None:
        """Delete the debugger pod and record the outcome — always runs,
        even when the shell worker was cancelled or interrupted. The audit
        write is best-effort here: the cluster write already happened, so
        failing it must not hide the cleanup."""
        cleanup = await self._delete_node_debug_pod(ops, namespace, pod_name, pod_uid)
        try:
            await asyncio.to_thread(
                self._audit_node_shell, audit, node, detail, f"{outcome}; {cleanup}"
            )
        except Exception:
            logger.exception("audit append failed after node shell")
            self.notify("Audit write failed for the executed node shell", severity="warning")

    async def _create_node_debug_pod(
        self, node: str, namespace: str, image: str
    ) -> tuple[str, str] | str:
        """Create the node-debugger pod detached; returns (name, uid).

        On failure returns the audit outcome string instead — distinct per
        cause, because they leave different cluster states: a kubectl launch
        failure never reached the cluster, a clearly identified admission
        rejection (where PodSecurity refusals surface, hence the namespace
        hint) leaves nothing behind, while any other non-zero exit, a
        timeout, or a create whose output cannot be parsed may have created
        a pod korvid cannot identify, so the audit records cleanup as
        skipped and names the namespace to inspect.
        """
        argv = build_node_debug_create_argv(
            node, namespace, context=self.config.kube_context, image=image
        )
        try:
            proc = await asyncio.to_thread(subprocess.run, argv, capture_output=True, timeout=30)
        except OSError as exc:
            # kubectl itself could not be launched (removed or not executable
            # since the PATH check): no request reached the cluster, so no
            # pod exists and no namespace inspection is needed.
            logger.warning("kubectl could not be launched for node debug", exc_info=True)
            self.notify(f"Could not launch kubectl: {exc}", severity="error")
            return "error: kubectl could not be launched; no pod created"
        except subprocess.TimeoutExpired:
            logger.warning("node-debugger pod creation timed out", exc_info=True)
            self.notify(
                f"kubectl debug node did not respond — a debugger pod may still have"
                f" been created; check {namespace} for leftover node-debugger pods",
                severity="error",
            )
            return f"error: pod creation timed out; cleanup skipped: check namespace {namespace}"
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            logger.warning("node-debugger pod creation failed: %s", stderr)
            if _looks_like_admission_rejection(stderr):
                # The API server refused the create: nothing was committed.
                # The namespace remediation only applies when PodSecurity
                # did the refusing — an RBAC forbid or an unrelated webhook
                # denial would make that hint actionably wrong.
                hint = (
                    " — the cluster refuses privileged pods (PodSecurity admission);"
                    " try setting node_shell.namespace to a namespace that allows them"
                    if "podsecurity" in stderr.lower()
                    else ""
                )
                self.notify(
                    f"Could not create the debugger pod: {stderr}{hint}",
                    severity="error",
                )
                return "error: pod creation rejected"
            # A non-zero exit does not prove rejection: the server can commit
            # the pod and kubectl still fail afterwards (lost response, local
            # output error) — treat as ambiguous, the pod may exist.
            self.notify(
                f"Could not create the debugger pod: {stderr or f'exit {proc.returncode}'}"
                f" — a pod may still have been created; check {namespace} for"
                " leftover node-debugger pods",
                severity="error",
            )
            return f"error: pod creation failed; cleanup skipped: check namespace {namespace}"
        pod_name = parse_debug_pod_name(proc.stdout.decode(errors="replace"))
        if pod_name is None:
            # Pod created (exit 0) but unidentifiable: refuse to guess.
            self.notify(
                f"kubectl did not report the created pod — check {namespace} for"
                " leftover node-debugger pods",
                severity="error",
            )
            return (
                "error: created pod could not be identified;"
                f" cleanup skipped: check namespace {namespace}"
            )
        uid = await self._fetch_created_pod_uid(namespace, pod_name)
        if uid is None:
            # Without the uid the cleanup delete would lose its precondition
            # and could remove a same-name replacement pod: refuse.
            self.notify(
                f"kubectl did not report the created pod's uid — check pod"
                f" {pod_name} in namespace {namespace}",
                severity="error",
            )
            return (
                "error: created pod could not be identified;"
                f" cleanup skipped: check namespace {namespace}"
            )
        return pod_name, uid

    async def _fetch_created_pod_uid(self, namespace: str, pod_name: str) -> str | None:
        """Fetch the just-created debugger pod's uid with an exact get.

        `kubectl debug` has no machine-readable output, so after parsing the
        pod name from its message the uid — required as the cleanup delete's
        precondition — comes from `kubectl get pod <name> -o json`. Any
        failure (launch, timeout, non-zero exit, malformed JSON) returns
        None: the caller treats the pod as unidentifiable rather than guess.
        """
        argv = build_pod_get_argv(namespace, pod_name, context=self.config.kube_context)
        try:
            proc = await asyncio.to_thread(subprocess.run, argv, capture_output=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("could not fetch created debugger pod", exc_info=True)
            return None
        if proc.returncode != 0:
            logger.warning(
                "created debugger pod fetch failed: %s",
                proc.stderr.decode(errors="replace").strip(),
            )
            return None
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            return None
        item_meta = payload.get("metadata") if isinstance(payload, dict) else None
        if not isinstance(item_meta, dict):
            # Valid JSON with an unexpected shape (e.g. metadata is a scalar)
            # must land in the unidentifiable branch, not raise past the
            # finalizer while a privileged pod may exist.
            return None
        uid = item_meta.get("uid")
        return uid if isinstance(uid, str) and uid else None

    async def _run_kubectl_ok(self, argv: list[str], timeout: float) -> bool:
        """Run a non-interactive kubectl helper; True on exit 0."""
        try:
            proc = await asyncio.to_thread(
                subprocess.run, argv, capture_output=True, timeout=timeout
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("kubectl helper failed", exc_info=True)
            return False
        return proc.returncode == 0

    async def _node_uid_unchanged(self, name: str, approved_uid: str) -> bool:
        """Re-verify the approved node incarnation just before the shell
        launches; notifies and returns False when the node is gone or was
        replaced under the same name while the dialog was open."""
        try:
            current_uid = await self._target_uid("nodes", None, name)
        except ApiStatusError:
            self.notify(
                f"node shell cancelled - node {name} no longer exists.",
                severity="warning",
            )
            return False
        if current_uid is not None and current_uid != approved_uid:
            self.notify(
                f"node shell cancelled - node {name} was replaced since the prompt was shown.",
                severity="warning",
            )
            return False
        return True

    async def _delete_node_debug_pod(
        self, ops: WriteOps, namespace: str, pod_name: str, pod_uid: str
    ) -> str:
        """Delete exactly the debugger pod this session created (uid
        precondition), returning the audit note."""
        pods_meta = self.aliases.get("pods")
        if pods_meta is None:
            self.notify(
                f"Cannot delete debug pod {pod_name} in {namespace} — remove it manually",
                severity="warning",
            )
            return f"cleanup failed for: {pod_name}"
        try:
            await ops.delete_object(pods_meta, namespace, pod_name, uid=pod_uid)
        except Exception:
            logger.exception("node-debugger pod deletion failed")
            self.notify(
                f"Failed to delete debug pod {pod_name} in {namespace} — remove it manually",
                severity="warning",
            )
            return f"cleanup failed for: {pod_name}"
        return f"cleanup: deleted {pod_name}"

    @staticmethod
    def _audit_node_shell(audit: AuditLog, node: str, detail: str, outcome: str) -> None:
        audit.append(
            action="node-shell",
            kind="nodes",
            group="",  # nodes are core/v1
            version="v1",
            namespace=None,
            name=node,
            detail=detail,
            outcome=outcome,
        )

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
        epoch = self._ctx_epoch
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
            "install", pkg_meta, ns, name, phase="the manifest fetch", epoch=epoch
        ):
            return

        def _on_choices(choices: tuple[str, str, str] | None) -> None:
            if choices is None:
                return
            # The SSAR round trip must not run inside a screen callback:
            # a worker re-checks, revalidates, then confirms.
            self.run_worker(
                self._confirm_operator_install(pkg_meta, sub_meta, ns, uid, facts, choices, epoch)
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
        epoch: int,
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
            "install", pkg_meta, ns, facts.package, phase="the install wizard", epoch=epoch
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
            self._confirm_screen(f"Install operator {facts.package}?", operation), _done
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
        epoch = self._ctx_epoch
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
        if not self._write_context_intact(
            "approve", meta, ns, name, phase="the manifest fetch", epoch=epoch
        ):
            return
        updated = dict(manifest)
        updated["spec"] = {**spec, "approved": True}
        csvs = ", ".join(str(c) for c in spec.get("clusterServiceVersionNames") or []) or "?"
        operation = (
            f"REPLACE installplans/{name} with spec.approved=true"
            f"{self._write_locus(ns)}\ninstalls: {csvs}"
        )
        await self._push_write_confirmation(
            f"Approve installplans/{name}?",
            operation,
            action="approve",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.replace_object(meta, ns, name, updated, uid=uid),
            detail=f"installs: {csvs}",
        )

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

    # ------------------------------------------------------------------
    # operator uninstall (issue #117)
    # ------------------------------------------------------------------

    def _olm_alias_key(self, plural: str) -> str | None:
        """The aliases key resolving to the OLM *plural* (prefers the
        group-qualified alias, like `resolve_olm_meta`), or None when the
        API was not discovered."""
        for key in (f"{plural}.{OPERATORS_GROUP}", plural):
            meta = self.aliases.get(key)
            if meta is not None and meta.group == OPERATORS_GROUP:
                return key
        return None

    async def _start_operator_uninstall(
        self,
        sub_meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        *,
        fetch_kind: str,
        ctx: tuple[ResourceMeta, str | None, str],
    ) -> None:
        """Ctrl+D on an OLM Subscription (or redirected from its CSV):
        uninstall the operator - delete the Subscription, then its installed
        CSV; OLM garbage-collects the operator's Deployment and RBAC owned
        by the CSV. CRDs and custom resources are never touched (their data
        outlives the operator by design). ``ctx`` names the selection the
        user acted on - the CSV row on the redirect path - so the post-await
        re-validation checks the right row."""
        ops = self._write_ops
        if ops is None:
            return
        epoch = self._ctx_epoch
        if not await self._permitted("uninstall", sub_meta, ns, name):
            return
        manifest = await self._fetch_subscription_for_uninstall(fetch_kind, sub_meta, ns, name, uid)
        if manifest is None:
            return
        csv_name = _installed_csv_name(manifest)
        try:
            csv_meta, csv_uid = await self._installed_csv_target(ns, csv_name)
        except _CsvTargetUnavailable as exc:
            self.notify(
                f"uninstall {name} aborted: {exc} -"
                f" installed CSV {csv_name} cannot be safely removed",
                severity="error",
            )
            return
        if csv_meta is not None and not await self._permitted("uninstall", csv_meta, ns, csv_name):
            return
        ctx_meta, ctx_ns, ctx_name = ctx
        if not self._write_context_intact(
            "uninstall", ctx_meta, ctx_ns, ctx_name, phase="the manifest fetch", epoch=epoch
        ):
            return
        operation = self._operator_uninstall_operation(sub_meta, ns, name, csv_meta, csv_name)

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self.run_worker(
                    self._operator_apply_uninstall(
                        ops,
                        sub_meta,
                        ns,
                        name,
                        uid,
                        fetch_kind=fetch_kind,
                        csv_meta=csv_meta,
                        csv_name=csv_name,
                        csv_uid=csv_uid,
                    )
                )

        await self.push_screen(
            self._confirm_screen(f"Uninstall operator {name}?", operation), _done
        )

    async def _fetch_subscription_for_uninstall(
        self, fetch_kind: str, sub_meta: ResourceMeta, ns: str | None, name: str, uid: str | None
    ) -> dict[str, Any] | None:
        """The Subscription manifest for the uninstall dialog, or None (with
        a notification) when it cannot be fetched or is a different
        incarnation than the row the user acted on."""
        if self._get_manifest is None:
            self.notify("Uninstall unavailable: no manifest source", severity="warning")
            return None
        try:
            manifest = await self._get_manifest(fetch_kind, ns, name)
        except Exception as exc:
            self.notify(f"Could not fetch the subscription: {exc}", severity="error")
            return None
        fetched = _manifest_uid(manifest)
        if uid and fetched and fetched != uid:
            self.notify(
                f"uninstall {self._gvr_label(sub_meta)}/{name} cancelled -"
                " the subscription changed during the manifest fetch",
                severity="warning",
            )
            return None
        return manifest

    async def _installed_csv_target(
        self, ns: str | None, csv_name: str
    ) -> tuple[ResourceMeta | None, str | None]:
        """(meta, uid) of the Subscription's installed CSV, or (None, None)
        when there is nothing to delete - no CSV recorded, or the CSV is
        already gone (404). Raises `_CsvTargetUnavailable` when the CSV
        exists but cannot be uid-pinned (API undiscovered, lookup failed):
        the uninstall aborts rather than skip the CSV or delete it
        unpinned."""
        if not csv_name:
            return None, None
        key = self._olm_alias_key("clusterserviceversions")
        if key is None:
            raise _CsvTargetUnavailable("the CSV API was not discovered")
        if self._get_manifest is None:
            raise _CsvTargetUnavailable("no manifest source to pin the CSV uid")
        try:
            manifest = await self._get_manifest(key, ns, csv_name)
        except ApiStatusError as exc:
            if exc.status == 404:
                return None, None
            raise _CsvTargetUnavailable(f"the CSV uid lookup failed (API {exc.status})") from exc
        except Exception as exc:
            raise _CsvTargetUnavailable("the CSV uid lookup failed") from exc
        csv_uid = _manifest_uid(manifest)
        if not csv_uid:
            raise _CsvTargetUnavailable("the CSV manifest has no uid to pin")
        return self.aliases[key], csv_uid

    def _operator_uninstall_operation(
        self,
        sub_meta: ResourceMeta,
        ns: str | None,
        name: str,
        csv_meta: ResourceMeta | None,
        csv_name: str,
    ) -> str:
        """The uninstall dialog body: exactly what will be deleted, what OLM
        garbage-collects, and what is deliberately kept."""
        lines = [
            f"OPERATOR UNINSTALL {name}{self._write_locus(ns)}",
            "",
            f"  DELETE {self._gvr_label(sub_meta)}/{name}",
        ]
        if csv_meta is not None and csv_name:
            lines += [
                f"  DELETE {self._gvr_label(csv_meta)}/{csv_name}",
                "",
                "OLM garbage-collects the operator's Deployment and RBAC owned by the CSV.",
            ]
        elif csv_name:
            lines += [
                "",
                f"(installed CSV {csv_name} is already gone - only the Subscription is removed)",
            ]
        else:
            lines += ["", "(no installed CSV recorded - only the Subscription is removed)"]
        lines.append("CRDs and custom resources are KEPT - remove them manually if needed.")
        return "\n".join(lines)

    @_tracks_cluster_write
    async def _operator_apply_uninstall(
        self,
        ops: WriteOps,
        sub_meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        *,
        fetch_kind: str,
        csv_meta: ResourceMeta | None,
        csv_name: str,
        csv_uid: str | None,
    ) -> None:
        """Subscription first (stops OLM from reinstalling), then the CSV;
        each delete individually audited fail-closed. A failed or blocked
        Subscription delete leaves the CSV untouched - removing the CSV
        alone would only make OLM reinstall it."""
        if await self._subscription_target_stale(fetch_kind, ns, name, uid, csv_name):
            self.notify(
                f"uninstall {name} aborted: the subscription changed while"
                " the dialog was open - refresh and retry",
                severity="warning",
            )
            return
        outcome = await self._run_write(
            "uninstall",
            sub_meta,
            ns,
            name,
            ops.delete_object(sub_meta, ns, name, uid=uid),
            detail=f"csv={csv_name or '-'}",
        )
        if outcome != "done" or csv_meta is None or not csv_name:
            return
        await self._run_write(
            "uninstall",
            csv_meta,
            ns,
            csv_name,
            ops.delete_object(csv_meta, ns, csv_name, uid=csv_uid),
            detail=f"subscription={name}",
        )

    async def _subscription_target_stale(
        self, fetch_kind: str, ns: str | None, name: str, uid: str | None, csv_name: str
    ) -> bool:
        """Whether the Subscription no longer matches what the user approved:
        a different incarnation (uid changed), or OLM advanced
        `status.installedCSV` in place while the dialog was open - the
        approved deletes would then target a stale CSV and leave the new one
        running. Fail-open on fetch errors: the deletes' own uid
        preconditions still guard, and a vanished Subscription just makes
        the first delete fail loudly."""
        if self._get_manifest is None:
            return False
        try:
            manifest = await self._get_manifest(fetch_kind, ns, name)
        except Exception:
            return False
        fetched_uid = _manifest_uid(manifest)
        if uid and fetched_uid and fetched_uid != uid:
            return True
        return _installed_csv_name(manifest) != csv_name

    async def _csv_uninstall_redirect(
        self, csv_meta: ResourceMeta, ns: str | None, name: str
    ) -> bool:
        """Ctrl+D on a CSV installed by a known Subscription: warn that OLM
        would reinstall a deleted CSV and offer the full uninstall instead
        (issue #117). False - the plain delete proceeds - when no owning
        Subscription is found; the lookup reads the store, so only
        Subscriptions this session has watched count."""
        sub_key = self._olm_alias_key("subscriptions")
        if sub_key is None:
            return False
        row = next(
            (
                obj
                for obj in self.store.get(self._canonical_kind(sub_key), self.current_scope)
                if getattr(obj, "installed_csv", "") == name and (ns is None or obj.namespace == ns)
            ),
            None,
        )
        if row is None:
            return False
        self.notify(
            f"{name} was installed by subscriptions/{row.name} - OLM would"
            " reinstall a deleted CSV; uninstalling the operator instead",
            severity="warning",
        )
        await self._start_operator_uninstall(
            self.aliases[sub_key],
            row.namespace or None,
            row.name,
            str(getattr(row, "uid", "") or "") or None,
            fetch_kind=sub_key,
            ctx=(csv_meta, ns, name),
        )
        return True

    # ------------------------------------------------------------------
    # helm install / upgrade / rollback via the detected helm CLI (issue #31)
    # ------------------------------------------------------------------

    def _helm_gate(self) -> HelmCLI | None:
        """Common gate for helm write flows: read-only mode and the
        fail-closed audit rule apply exactly as to API writes, plus the
        binary must have been detected at startup. None (with a
        notification) blocks the flow."""
        if self.config.readonly:
            self.notify("Read-only mode: cluster writes are disabled", severity="warning")
            return None
        if self._audit is None:
            # Fail-closed auditing (AGENTS.md): no audit sink means no writes.
            self.notify("Writes disabled: no audit log configured", severity="warning")
            return None
        if self._helm is None:
            self.notify(
                "helm CLI not found on PATH - install/upgrade/rollback/uninstall unavailable",
                severity="error",
            )
            return None
        return self._helm

    def _helm_view_namespace(self) -> str:
        """Namespace a fresh install targets by default: the active view
        namespace, or the configured workload namespace on the
        all-namespaces view (same fallback as the operator install wizard)."""
        view_ns = self.current_namespace
        return view_ns if view_ns != ALL_NAMESPACES else (self.config.namespace or "default")

    def _helm_install_flow(self) -> None:
        """Install (the `hint_details` key on the helm view): search-first
        chart picker -> wizard -> dry-run preview -> approval -> audited
        `helm install`. The picker opens instantly and fetches charts per
        keyword (issue #106) instead of listing every repo upfront.
        Synchronous by design: no await may separate the keypress from the
        namespace/epoch capture and the modal push."""
        helm = self._helm_gate()
        if helm is None:
            return
        self._helm_open_chart_search(
            helm,
            release=None,
            namespace=self._helm_view_namespace(),
            epoch=self._ctx_epoch,
            initial="",
        )

    def _helm_upgrade_flow(self) -> None:
        """Upgrade (the `helm_upgrade` key on a release row): the same
        wizard with the release name and namespace fixed to the selected
        row's facts; the picker pre-searches the release's chart name.
        Synchronous by design: no await may separate the keypress from the
        row/epoch capture and the modal push."""
        helm = self._helm_gate()
        if helm is None:
            return
        epoch = self._ctx_epoch
        ns, name = self._selected_ns_name()
        if name is None:
            return
        row = self._helm_release_row(ns, name)
        keyword = _chart_base(row.chart) if row is not None else ""
        namespace = ns or (row.namespace if row is not None else self._helm_view_namespace())
        self._helm_open_chart_search(
            helm, release=name, namespace=namespace, epoch=epoch, initial=keyword
        )

    def _helm_open_chart_search(
        self, helm: HelmCLI, *, release: str | None, namespace: str, epoch: int, initial: str
    ) -> None:
        """Keyword-driven chart picker feeding the install/upgrade wizard;
        everything offered comes from `helm search repo`, nothing is
        hardcoded. Ctrl-R inside the picker manages chart repositories."""

        def _picked(hit: ChartHit | None) -> None:
            if hit is None:
                return

            def _chosen(choices: HelmReleaseChoices | None) -> None:
                if choices is None:
                    return
                self.run_worker(
                    self._helm_confirm_change(
                        hit, choices, upgrade=release is not None, epoch=epoch
                    ),
                    exclusive=True,
                    group="helm-write",
                )

            self.push_screen(
                HelmInstallPrompt(
                    hit,
                    namespace=namespace,
                    release=release,
                    # Chart metadata (issue #151): required values from the
                    # chart's schema and README access, both repo-local.
                    get_schema=helm.show_schema,
                    get_readme=helm.show_readme,
                ),
                _chosen,
            )

        title = f"Upgrade {release} with chart:" if release else "Install helm chart"
        search_screen = HelmChartSearchScreen(
            helm.search_repo,
            title=title,
            initial=initial,
            on_manage_repos=lambda: self._helm_open_repos(helm, browse_in=search_screen),
        )
        self.push_screen(search_screen, _picked)

    def _helm_open_repos(
        self, helm: HelmCLI, *, browse_in: HelmChartSearchScreen | None = None
    ) -> None:
        """Chart repository management (list/add/update). `helm repo` writes
        local helm config only — never the cluster — so the typed form in
        the screen is the confirmation, not the write-approval gate.

        Enter on a repo row hands its name back (issue #137): the chart
        picker in *browse_in* — when it is still the screen underneath —
        scopes its search to that repository."""

        def _picked(repo: str | None) -> None:
            if repo is None or browse_in is None:
                return
            if self.screen is browse_in:
                browse_in.browse_repo(repo)

        self.push_screen(
            HelmRepoScreen(
                repo_list=helm.repo_list,
                repo_add=helm.repo_add,
                repo_update=helm.repo_update,
            ),
            _picked,
        )

    async def _helm_confirm_change(
        self, hit: ChartHit, choices: HelmReleaseChoices, *, upgrade: bool, epoch: int
    ) -> None:
        """Optional values editing, dry-run/diff preview, then the standard
        approval dialog; the mutation itself runs through `_run_write`, so
        the fail-closed audit rule applies unchanged. A dry-run the helm
        binary itself rejects stops the flow before approval (issue #139):
        the same command would fail identically after approval, so the
        user gets helm's stderr now, with the option to fix the values and
        retry instead of approving a doomed mutation."""
        helm = self._helm
        if helm is None:  # gate already passed; helm cannot vanish, but be safe
            return
        values_text: str | None = None
        editor_buffer: str | None = None
        defaults_baseline: str | None = None
        if choices.edit_values:
            proceed, values_text, editor_buffer, defaults_baseline = await self._helm_edit_values(
                helm, hit, choices, previous=None
            )
            if not proceed:
                return  # editor failed or was aborted; already notified
        action = "helm-upgrade" if upgrade else "helm-install"
        outcome = await self._helm_preview_with_recovery(
            helm,
            hit,
            choices,
            values_text,
            editor_buffer,
            defaults_baseline,
            upgrade=upgrade,
            epoch=epoch,
            action=action,
        )
        if outcome is None:
            return
        rendered, values_text = outcome
        if rendered is not None:
            preview, preview_title = rendered
        else:
            # Environmental failure (timeout, unexpected error): approval
            # stays available, but say so instead of a silent blank.
            preview = ["(preview unavailable - the dry-run render did not complete)"]
            preview_title = "helm preview:"
        verb = "UPGRADE" if upgrade else "INSTALL"
        version_label = choices.version or "latest"
        if values_text is not None:
            values_label, values_detail = "edited in $EDITOR", "custom"
        elif choices.reuse_values:
            values_label, values_detail = "reuse current values", "reused"
        else:
            values_label, values_detail = "chart defaults", "defaults"
        operation = (
            f"HELM {verb} {choices.release} (chart {hit.name} {version_label})"
            f" in namespace {choices.namespace}\n"
            f"values: {values_label}"
        )
        detail = f"chart={hit.name} version={version_label} values={values_detail}"

        title = f"{'Upgrade' if upgrade else 'Install'} {choices.release}?"
        await self._push_write_confirmation(
            title,
            operation,
            action=action,
            meta=HELM_RELEASES_META,
            namespace=choices.namespace,
            name=choices.release,
            op_factory=lambda: self._helm_apply_change(
                helm, hit, choices, values_text, upgrade=upgrade
            ),
            detail=detail,
            preview=preview,
            preview_title=preview_title,
        )

    def _helm_context_after_preview(
        self, action: str, choices: HelmReleaseChoices, *, upgrade: bool, epoch: int
    ) -> bool:
        """The preview runs over the interactive table: the state the user
        approves must still be the state that was previewed."""
        if upgrade:
            # The row selected for upgrade must still be the one approved.
            return self._write_context_intact(
                action,
                HELM_RELEASES_META,
                choices.namespace,
                choices.release,
                phase="the preview render",
                epoch=epoch,
            )
        if self._ctx_switching or epoch != self._ctx_epoch:
            # The helm wrapper this flow captured is bound to the old
            # cluster's --kube-context: a switch completed during the wizard
            # or preview must cancel before an approval can open.
            self.notify(
                "helm install cancelled - the kube context changed during the preview",
                severity="warning",
            )
            return False
        if len(self.screen_stack) > 1:  # another dialog opened during the preview
            return False
        if self._canonical_kind(self.current_kind) != HELM_RELEASES_META.plural:
            self.notify(
                "helm install cancelled - left the helm view during the preview",
                severity="warning",
            )
            return False
        return True

    async def _helm_edit_values(
        self,
        helm: HelmCLI,
        hit: ChartHit,
        choices: HelmReleaseChoices,
        *,
        previous: str | None,
        defaults_baseline: str | None = None,
    ) -> tuple[bool, str | None, str | None, str | None]:
        """(proceed, values override, raw buffer, defaults baseline) from
        `$EDITOR`.

        A first edit opens on the chart's own annotated defaults
        (`helm show values`, issue #151) - the standard CLI workflow -
        falling back to the old comment stub when the fetch fails. Content
        matching the fetched defaults (or comments-only) keeps the chart
        defaults: no override file is passed. The *baseline* rides along so
        a retry after a failed render (issue #139, pre-filled with the
        previous raw buffer) still recognizes unchanged defaults instead of
        freezing them into the release. False means the editor was aborted
        or failed and the flow must stop.
        """
        template = previous
        if template is None:
            with self._progress("fetching chart default values"):
                defaults_baseline = await self._helm_default_values(helm, hit, choices)
            template = defaults_baseline
        if template is None:
            template = (
                f"# values override for {hit.name} {choices.version or hit.version}\n"
                "# an empty file (or comments only) keeps the chart defaults\n"
            )
        edit = self._edit_text or self._edit_in_external_editor
        text = await edit(template)
        if text is None:
            return False, None, None, defaults_baseline
        meaningful = any(
            line.strip() and not line.lstrip().startswith("#") for line in text.splitlines()
        )
        if defaults_baseline is not None and text == defaults_baseline:
            # Unchanged chart defaults are not an override (issue #151):
            # passing them as -f would freeze today's defaults into the
            # release for no reason.
            meaningful = False
        return True, (text if meaningful else None), text, defaults_baseline

    async def _helm_default_values(
        self, helm: HelmCLI, hit: ChartHit, choices: HelmReleaseChoices
    ) -> str | None:
        """`helm show values` output for the picked chart, or None when the
        fetch fails (the caller falls back to the comment stub). The wizard
        version passes through unchanged: an empty version means "latest",
        matching what the install itself will resolve."""
        try:
            return await asyncio.wait_for(
                helm.show_values(hit.name, choices.version),
                _HELM_PREVIEW_TIMEOUT,
            )
        except (HelmError, TimeoutError):
            logger.debug("helm show values failed; editor opens on the stub", exc_info=True)
            return None

    async def _helm_render_failure_choice(self, error: str, *, upgrade: bool) -> str:
        """Stop-before-approval decision on a rejected dry-run (issue #139):
        "edit", "retry", or "cancel" - Esc cancels. The picker is
        informational, not an approval gate: whatever the choice, the
        mutation still has to pass the ConfirmScreen approval afterwards."""
        verb = "upgrade" if upgrade else "install"
        title = f"helm {verb} --dry-run failed - the {verb} would fail the same way.\n\n{error}\n"
        options = ["edit values and retry", "retry preview", "cancel"]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str | None] = loop.create_future()

        def _done(choice: str | None) -> None:
            if not fut.done():
                fut.set_result(choice)

        await self.push_screen(PickScreen(title, options), _done)
        choice = await fut
        if choice == options[0]:
            return "edit"
        if choice == options[1]:
            return "retry"
        return "cancel"

    async def _helm_preview_with_recovery(
        self,
        helm: HelmCLI,
        hit: ChartHit,
        choices: HelmReleaseChoices,
        values_text: str | None,
        editor_buffer: str | None,
        defaults_baseline: str | None,
        *,
        upgrade: bool,
        epoch: int,
        action: str,
    ) -> tuple[tuple[list[str], str] | None, str | None] | None:
        """Render the preview, recovering from rejected dry-runs (issue #139).

        Returns `(rendered, values_text)` once a render succeeds (or fails
        only environmentally - `rendered` is then None), with `values_text`
        carrying any fixes made through the failure dialog's edit path;
        `editor_buffer` is the raw text last seen in `$EDITOR` (kept apart
        from the normalized override so a comments-only buffer survives a
        retry) and `defaults_baseline` the fetched chart defaults, carried
        across retries so an unchanged-defaults buffer never turns into a
        frozen override. None when the flow must stop (context lost, editor
        aborted, or the user cancelled at the failure dialog).
        """
        while True:
            # Rendering can take up to _HELM_PREVIEW_TIMEOUT (20s): show
            # progress for exactly as long as the render is pending, or the
            # UI looks frozen between the wizard and the approval dialog
            # (issue #106).
            with self._progress("rendering helm preview (dry-run)"):
                rendered = await self._helm_change_preview(
                    helm, hit, choices, values_text, upgrade=upgrade
                )
            if not self._helm_context_after_preview(action, choices, upgrade=upgrade, epoch=epoch):
                return None
            if not isinstance(rendered, _HelmRenderFailure):
                return rendered, values_text
            decision = await self._helm_render_failure_choice(rendered.error, upgrade=upgrade)
            if decision == "cancel":
                return None  # nothing was executed, nothing to audit
            if decision == "edit":
                (
                    proceed,
                    values_text,
                    editor_buffer,
                    defaults_baseline,
                ) = await self._helm_edit_values(
                    helm, hit, choices, previous=editor_buffer, defaults_baseline=defaults_baseline
                )
                if not proceed:
                    return None

    async def _helm_apply_change(
        self,
        helm: HelmCLI,
        hit: ChartHit,
        choices: HelmReleaseChoices,
        values_text: str | None,
        *,
        upgrade: bool,
    ) -> None:
        """The approved mutation, awaited by `_run_write` after the intent
        audit record persisted."""
        version = choices.version or None
        async with _temp_values_file(values_text) as values_file:
            if upgrade:
                await helm.upgrade(
                    choices.release,
                    hit.name,
                    choices.namespace,
                    version=version,
                    values_file=values_file,
                    reuse_values=choices.reuse_values,
                )
            else:
                await helm.install(
                    choices.release,
                    hit.name,
                    choices.namespace,
                    version=version,
                    values_file=values_file,
                )

    async def _helm_change_preview(
        self,
        helm: HelmCLI,
        hit: ChartHit,
        choices: HelmReleaseChoices,
        values_text: str | None,
        *,
        upgrade: bool,
    ) -> tuple[list[str], str] | _HelmRenderFailure | None:
        """Preview lines plus their heading for the approval dialog: `helm
        diff upgrade` when the plugin exists (issue #31), else the plain
        `--dry-run` render - the heading names which one the user is looking
        at. A dry-run helm itself rejects returns `_HelmRenderFailure` - the
        approval would run the same command, so the caller must stop instead
        of approving a proven failure (issue #139). Environmental failures
        (timeout, unexpected errors) return None - they say nothing about
        the mutation, so a preview must never block the approval flow."""

        async def _render() -> tuple[str, str]:
            version = choices.version or None
            async with _temp_values_file(values_text) as values_file:
                if upgrade and await helm.has_diff_plugin():
                    try:
                        return "helm diff upgrade preview:", await helm.diff_upgrade(
                            choices.release,
                            hit.name,
                            choices.namespace,
                            version=version,
                            values_file=values_file,
                            reuse_values=choices.reuse_values,
                        )
                    except HelmError:
                        # A diff-plugin failure is not a verdict on the
                        # upgrade itself: fall back to the plain dry-run,
                        # whose failure would be.
                        logger.debug("helm diff failed; falling back to --dry-run", exc_info=True)
                if upgrade:
                    return "helm upgrade --dry-run preview:", await helm.dry_run_upgrade(
                        choices.release,
                        hit.name,
                        choices.namespace,
                        version=version,
                        values_file=values_file,
                        reuse_values=choices.reuse_values,
                    )
                return "helm install --dry-run preview:", await helm.dry_run_install(
                    choices.release,
                    hit.name,
                    choices.namespace,
                    version=version,
                    values_file=values_file,
                )

        try:
            title, text = await asyncio.wait_for(_render(), _HELM_PREVIEW_TIMEOUT)
        except HelmPreviewUnsupported:
            # helm < 3.15 rejecting the preview-only --hide-secret flag is
            # a preview incompatibility, not a verdict: the real command
            # never carries the flag (see HelmCLI._dry_run).
            logger.debug("helm preview failed; dialog opens without it", exc_info=True)
            return None
        except HelmError as exc:
            return _HelmRenderFailure(str(exc))
        except Exception:
            logger.debug("helm preview failed; dialog opens without it", exc_info=True)
            return None
        lines = _clip_preview(text)
        # [] is a *successful* empty render (helm diff: no changes) —
        # ConfirmScreen states it explicitly; None stays reserved for
        # failures, which the caller marks "preview unavailable".
        return (lines if lines is not None else [], title)

    async def _helm_rollback_flow(
        self,
        helm: HelmCLI,
        row: HelmRevisionSummary,
        ns: str | None,
        name: str,
        namespace: str,
        epoch: int,
    ) -> None:
        """Rollback (the `rollout_restart` key on a revision row of the
        drill-down): approval-gated, audited `helm rollback` to that
        revision. The target row is captured by the action at keypress time
        and passed in — this worker must never re-read the selection."""
        with self._progress("rendering rollback preview"):
            preview = await self._helm_rollback_preview(helm, row.release, row.revision, namespace)
        if not self._write_context_intact(
            "helm-rollback", HELM_REVISIONS_META, ns, name, phase="the diff preview", epoch=epoch
        ):
            return
        operation = (
            f"HELM ROLLBACK {row.release} to revision {row.revision} in namespace {namespace}"
        )

        await self._push_write_confirmation(
            f"Rollback {row.release} to revision {row.revision}?",
            operation,
            action="helm-rollback",
            meta=HELM_RELEASES_META,
            namespace=namespace,
            name=row.release,
            op_factory=lambda: self._helm_apply_rollback(
                helm, row.release, row.revision, namespace
            ),
            detail=f"revision={row.revision}",
            preview=preview,
            preview_title="helm diff rollback preview:",
        )

    async def _helm_apply_rollback(
        self, helm: HelmCLI, release: str, revision: int, namespace: str
    ) -> None:
        await helm.rollback(release, revision, namespace)

    async def _helm_rollback_preview(
        self, helm: HelmCLI, release: str, revision: int, namespace: str
    ) -> list[str] | None:
        """`helm diff rollback` preview when the plugin exists; None
        otherwise (plain rollback has no meaningful dry-run output)."""

        async def _render() -> str | None:
            if not await helm.has_diff_plugin():
                return None
            return await helm.diff_rollback(release, revision, namespace)

        try:
            text = await asyncio.wait_for(_render(), _HELM_PREVIEW_TIMEOUT)
        except Exception:
            logger.debug("helm rollback preview failed; dialog opens without it", exc_info=True)
            return None
        return _clip_preview(text) if text is not None else None

    async def _helm_uninstall_flow(
        self,
        helm: HelmCLI,
        row: HelmReleaseSummary,
        ns: str | None,
        name: str,
        namespace: str,
        epoch: int,
    ) -> None:
        """Uninstall (ctrl+d on a release row): approval-gated, audited
        `helm uninstall`. The release name must be typed to confirm - the
        blast radius is every resource the release owns, so the y shortcut
        is not enough. The target row is captured by the action at keypress
        time and passed in - this worker must never re-read the selection."""
        with self._progress("rendering uninstall preview"):
            preview = await self._helm_uninstall_preview(helm, row.name, namespace)
        if not self._write_context_intact(
            "helm-uninstall", HELM_RELEASES_META, ns, name, phase="the dry-run preview", epoch=epoch
        ):
            return
        operation = (
            f"HELM UNINSTALL {row.name} ({row.chart}) from namespace {namespace}\n"
            "Deletes every resource this release owns and removes its history."
        )
        await self._push_write_confirmation(
            f"Uninstall release {row.name}?",
            operation,
            action="helm-uninstall",
            meta=HELM_RELEASES_META,
            namespace=namespace,
            name=row.name,
            op_factory=lambda: self._helm_apply_uninstall(helm, row.name, namespace),
            require_name=row.name,
            preview=preview,
            preview_title="helm uninstall --dry-run preview:",
        )

    async def _helm_apply_uninstall(self, helm: HelmCLI, release: str, namespace: str) -> None:
        await helm.uninstall(release, namespace)

    async def _helm_uninstall_preview(
        self, helm: HelmCLI, release: str, namespace: str
    ) -> list[str] | None:
        """`helm uninstall --dry-run` summary; None on any failure (the
        dialog then opens without a preview, like every other preview)."""
        try:
            text = await asyncio.wait_for(
                helm.dry_run_uninstall(release, namespace), _HELM_PREVIEW_TIMEOUT
            )
        except Exception:
            logger.debug("helm uninstall preview failed; dialog opens without it", exc_info=True)
            return None
        return _clip_preview(text)

    def _helm_release_row(self, ns: str | None, name: str) -> HelmReleaseSummary | None:
        for obj in self.store.get("helmreleases", self.current_scope):
            if (
                obj.name == name
                and (ns is None or obj.namespace == ns)
                and isinstance(obj, HelmReleaseSummary)
            ):
                return obj
        return None

    def _helm_revision_row(self, ns: str | None, name: str) -> HelmRevisionSummary | None:
        for obj in self.store.get("helmrevisions", self.current_scope):
            if (
                obj.name == name
                and (ns is None or obj.namespace == ns)
                and isinstance(obj, HelmRevisionSummary)
            ):
                return obj
        return None

    async def _open_log_pane(
        self,
        namespace: str,
        sources: list[tuple[str, str]],
        triples: list[tuple[str, str, str]] | None = None,
        force_prefix: bool = False,
        previous: bool = False,
        epoch: int | None = None,
    ) -> None:
        """Show log pane and spawn one streaming task per (pod, container).

        *epoch* is the `_ctx_epoch` captured when the user triggered the
        action; when a :ctx switch started or completed since (issue #84),
        the open is dropped — the streams would attach to the new cluster
        while labeled with the old selection. Callers without an awaited
        gap (epoch=None) are still refused while a switch is in flight.
        """
        if self._ctx_switch_crossed(self._ctx_epoch if epoch is None else epoch):
            self.notify(
                "Log streaming cancelled - the kube context changed",
                severity="warning",
            )
            return
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
        self._log_pane_owner = self._pane

        log_pane = self._log_pane
        self._log_buffer = LogBuffer(self._log_buffer_max_lines)
        log_pane.open(sources, force_prefix=force_prefix, log_buffer=self._log_buffer)
        # The pane controls (f/w/t/Ctrl-S/p) gate on pane visibility: tell
        # the footer legend the pane just appeared (issue #114).
        self.refresh_bindings()

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
        log_pane = self._log_pane
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
        log_pane = self._log_pane
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
        self._log_pane_owner = None
        with contextlib.suppress(Exception):
            self._log_pane.close()
        # The pane controls gate on pane visibility (issue #114).
        self.refresh_bindings()

    def _on_bindings_updated(self, _screen: object) -> None:
        self._refresh_top_bar()

    def _legend_entries(self) -> list[KeyEntry]:
        """The visible bindings as top-bar entries: pre-filtered by
        Textual's binding machinery (check_action / _ACTION_VIEWS - the
        single visibility source), deduplicated across --alt spellings and
        parametrised favorites."""
        entries: list[KeyEntry] = []
        seen: set[str] = set()
        for active in self.screen.active_bindings.values():
            binding = active.binding
            base = (binding.id or binding.action).removesuffix("--alt")
            action = binding.action.partition("(")[0]
            if action == "favorite_namespace" or base in seen:
                continue
            seen.add(base)
            entries.append(
                KeyEntry(
                    key=self.get_key_display(binding),
                    action=action,
                    description=binding.description,
                )
            )
        return entries

    def _topbar_toggle_key(self) -> str:
        """The effective toggle key's display form: resolved by action from
        the active bindings so a `toggle_topbar` remap moves the advertised
        hint with it (`active_bindings` is keyed by declared key names like
        "tilde", never by the display form)."""
        for active in self.screen.active_bindings.values():
            if active.binding.action == "toggle_topbar":
                return self.get_key_display(active.binding)
        return "~"

    def _topbar_can_drill(self) -> bool:
        """True when Enter drills on the current view: pods -> containers,
        hierarchy roots -> component tree, or a registered ownership child
        (mirrors on_data_table_row_selected, which otherwise leaves Enter
        unconsumed - such views must not advertise `enter: drill`)."""
        if self.current_kind == "pods":
            return True
        if self._hierarchy_root_kind() is not None:
            return True
        return drill_child(self._canonical_kind(self.current_kind)) is not None

    def _refresh_top_bar(self) -> None:
        """Re-render the grouped legend for the current view (issue #142)."""
        bars = self.query(TopBar)
        if not bars:
            return
        bars.first(TopBar).update_legend(
            self.current_kind,
            self._legend_entries(),
            expanded=self._topbar_expanded,
            toggle_key=self._topbar_toggle_key(),
            can_drill=self._topbar_can_drill(),
        )

    def action_toggle_topbar(self) -> None:
        """`~` (issue #142): collapse/expand the grouped key legend; the
        choice persists to config through the injected save callback."""
        self._topbar_expanded = not self._topbar_expanded
        self._refresh_top_bar()
        if self._save_topbar is None:
            return
        try:
            self._save_topbar(self._topbar_expanded)
        except Exception as exc:  # in-memory toggle stays; disk is stale
            self.notify(
                f"Top bar toggled, but save failed: {exc} — the previous state returns on restart",
                severity="warning",
            )

    def _refresh_status(self) -> None:
        # The top bar shows the view name: keep it in step with every
        # status refresh (navigation always lands here).
        self._refresh_top_bar()
        # Availability comes from the actual runtime, not the config flag —
        # create_provider may return None (unknown provider, missing base_url/
        # model) while agent_enabled is still true in config.
        label = "AI on" if self._agent_runtime is not None else "AI off"
        if self._agent_runtime is not None and self._agent_blocked_in_protected():
            label = "AI blocked"
        mcp_label = self._mcp.status() if self._mcp is not None else ""
        if mcp_label and self._mcp is not None and self._mcp.running and self._mcp_follow:
            mcp_label += " ·follow"
        self._status_bar.update_status(
            self.config.kube_context,
            self.current_scope,
            label,
            breadcrumb=self._drill.breadcrumb(),
            mcp_label=mcp_label,
            filter_label=self._resource_filter.describe(),
            progress_label=" · ".join(label for label in self._progress_labels.values() if label),
            proposals_label=self._proposals_label(),
            protected=self._protected_context is not None,
        )

    def _proposals_label(self) -> str:
        """Status-bar text for pending external write proposals (issue #110):
        a persistent, non-modal indicator naming source and target."""
        store = self._proposal_store
        if store is None:
            return ""
        pending = store.pending()
        if not pending:
            return ""
        p = pending[0]  # oldest — the next proposal a review would surface
        source = p.client_name or "mcp"
        target = f"{p.action} {p.kind}/{p.name}"
        if len(pending) == 1:
            return f"1 proposal from {source}: {target} — :proposals"
        return f"{len(pending)} proposals (next from {source}: {target}) — :proposals"

    def on_external_proposals_changed(self, message: ExternalProposalsChanged) -> None:
        self._refresh_status()

    async def on_external_proposal_expired(self, message: ExternalProposalExpired) -> None:
        """Audit a proposal the lazy TTL sweep expired: it reached a terminal
        state like any other and must not vanish from the audit trail."""
        await self._audit_proposal_outcome(message.proposal, "expired", message.reason)

    def _subscribe_proposal_updates(self, store: ProposalStore) -> None:
        """Keep the pending indicator live: post_message is loop-safe, so the
        callback may fire from the MCP server's task (or any thread) and
        never touches widgets directly."""

        def _proposals_changed() -> None:
            self.post_message(ExternalProposalsChanged())

        def _proposal_expired(proposal: WriteProposal, reason: str) -> None:
            self.post_message(ExternalProposalExpired(proposal, reason))

        store.subscribe(_proposals_changed)
        store.set_on_expired(_proposal_expired)

    def _agent_blocked_in_protected(self) -> bool:
        """`agent.disable_in_protected` (issue #83): agent turns are refused
        entirely while a protected context is active."""
        return self._protected_context is not None and self.config.agent_disable_in_protected

    def _confirm_screen(
        self,
        title: str,
        operation: str,
        *,
        require_name: str | None = None,
        preview: list[str] | None = None,
        preview_title: str = "server dry-run preview:",
        managed_note: str | None = None,
    ) -> ConfirmScreen:
        """Build every write-approval dialog through one place so the
        protected-context layer (issue #83) can never be forgotten: while a
        protected context is active, all confirms carry the red banner and
        demand a typed name instead of `y`."""
        return ConfirmScreen(
            title,
            operation,
            require_name=require_name,
            preview=preview,
            preview_title=preview_title,
            protected_context=self._protected_context,
            managed_note=managed_note,
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
        log_pane = self._log_pane
        if not log_pane.display:
            return
        toggle(log_pane)
        if self._log_buffer is not None:
            log_pane.replay(self._log_buffer.lines())

    def action_log_save(self) -> None:
        """Save the current log buffer to a generated file (``ctrl+s``)."""
        log_pane = self._log_pane
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
        log_pane = self._log_pane
        if not log_pane.display:
            return
        if not self._current_log_triples:
            return
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch
        triples = list(self._current_log_triples)
        force_prefix = self._current_log_force_prefix
        sources = [(pod, ctr) for _, pod, ctr in triples]
        # Cancel live tasks without hiding the pane.
        await self._cancel_log_tasks()
        self._log_pane_mode = "p"
        # Re-open with previous=True (clears RichLog, writes banner, spawns tasks).
        ns0 = triples[0][0]
        await self._open_log_pane(
            ns0, sources, triples=triples, force_prefix=force_prefix, previous=True, epoch=epoch
        )

    def action_log_search_next(self) -> None:
        """Advance to the next search hit (``n`` key)."""
        describe_pane = self._describe_pane
        if describe_pane.display:
            describe_pane.search_next()
            return
        log_pane = self._log_pane
        if log_pane.display:
            log_pane.search_next()

    def action_log_search_prev(self) -> None:
        """Previous search hit in an open pane; sort by name otherwise (``N``)."""
        describe_pane = self._describe_pane
        if describe_pane.display:
            describe_pane.search_prev()
            return
        log_pane = self._log_pane
        if log_pane.display:
            log_pane.search_prev()
            return
        self._toggle_sort("name")

    # ------------------------------------------------------------------
    # Column sorting (issue #37) — data-model sort keys, per-kind state.
    # ------------------------------------------------------------------

    def _view_for(self, kind: str) -> ViewConfig | None:
        """The `views:` config entry for a view kind, resolved via its meta."""
        meta = self.aliases.get(kind)
        return self.config.views.get(meta.plural if meta is not None else kind)

    def _toggle_sort(self, column: str) -> None:
        """Apply/flip a sort column for the current view kind and re-render."""
        kind = self.current_kind
        if column in ("cpu", "mem") and kind != "pods":
            # Only the pods view has CPU/MEM columns and a metrics feed;
            # elsewhere the keypress would silently discard the current
            # order while showing no indicator, so ignore it.
            return
        view = self._view_for(kind)
        if column != "name" and view is not None and view.replace:
            # `replace: true` hides AGE/CPU/MEM — sorting by an invisible
            # column would reorder rows with no indicator, so ignore it.
            return
        self._sorts[kind] = toggle_sort(self._sorts.get(kind), column)
        self._render_table(kind, only=self._pane)

    def action_sort_by_age(self) -> None:
        self._toggle_sort("age")

    def action_sort_by_cpu(self) -> None:
        self._toggle_sort("cpu")

    def action_sort_by_mem(self) -> None:
        self._toggle_sort("mem")

    def on_sort_command(self, message: SortCommand) -> None:
        """`:sort <column>` (issue #45): builtin or custom column; bare `:sort` clears."""
        kind = self.current_kind
        if message.column is None:
            self._sorts.pop(kind, None)
            self._render_table(kind, only=self._pane)
            return
        requested = message.column
        view = self._view_for(kind)
        custom_names = tuple(column.name for column in view.columns) if view is not None else ()
        builtins = ("name",) if view is not None and view.replace else SORT_COLUMNS
        if requested.lower() in builtins:
            self._toggle_sort(requested.lower())
            return
        matched = next((name for name in custom_names if name.lower() == requested.lower()), None)
        if matched is None:
            columns = ", ".join((*builtins, *custom_names))
            self.notify(
                f"Unknown sort column {requested!r} — available: {columns}",
                severity="warning",
            )
            return
        self._sorts[kind] = toggle_sort(self._sorts.get(kind), matched)
        self._render_table(kind, only=self._pane)

    def _sortable_columns(self, kind: str) -> tuple[str, ...]:
        """Every column the current view can sort by (issue #138): the
        visible builtins (cpu/mem only where a metrics feed exists, none
        but name under `replace: true`) plus the view's custom columns."""
        view = self._view_for(kind)
        custom = tuple(column.name for column in view.columns) if view is not None else ()
        if view is not None and view.replace:
            builtins: tuple[str, ...] = ("name",)
        elif kind == "pods":
            builtins = SORT_COLUMNS
        else:
            builtins = ("name", "age")
        return (*builtins, *custom)

    def _apply_sort_column(self, column: str, *, pane: PaneState | None = None) -> None:
        """Apply/flip *column* (builtin or custom, already validated for
        *pane*'s view) on that pane and re-render. Defaults to the focused
        pane; the header-click path passes the clicked table's pane."""
        pane = pane if pane is not None else self._pane
        kind = pane.kind
        pane.sorts[kind] = toggle_sort(pane.sorts.get(kind), column)
        self._render_table(kind, only=pane)

    def action_sort_picker(self) -> None:
        """`o` (issue #138): pick the sort column from a list instead of
        typing its exact name; re-picking the active column flips the
        direction, exactly like `:sort`."""
        if len(self.screen_stack) > 1:
            return  # never stack over another dialog
        pane = self._pane
        kind = pane.kind
        columns = self._sortable_columns(kind)
        current = self._sorts.get(kind)
        options = [
            f"{column} {'▼' if current.descending else '▲'}"
            if current is not None and current.column == column
            else column
            for column in columns
        ]

        def _picked(choice: str | None) -> None:
            if choice is None:
                return
            # Strip only the active-sort arrow: custom column names may
            # legitimately contain spaces.
            column = choice.removesuffix(" ▲").removesuffix(" ▼")
            if pane in self._panes and pane.kind == kind and column in self._sortable_columns(kind):
                self._apply_sort_column(column, pane=pane)

        self.push_screen(PickScreen(f"Sort {kind} by:", options), _picked)

    async def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """A header click sorts by that column (issue #138); clicking the
        active column flips the direction, same as the keys. The sort lands
        in the pane that owns the clicked table - not the focused one, so
        the split workspace never sorts the wrong pane."""
        if not isinstance(event.data_table, ResourceTable):
            return
        event.stop()
        pane = next((p for p in self._panes if p.table_id == event.data_table.id), None)
        if pane is None:
            return  # a table without a live pane (mid-teardown)
        # Labels may carry the ▲/▼ decoration of the active sort.
        label = str(event.label).removesuffix(" ▲").removesuffix(" ▼")
        kind = pane.kind
        columns = self._sortable_columns(kind)
        builtin = _HEADER_SORT_COLUMNS.get(label)
        # Configured custom names may carry Rich markup ([red]TEAM[/]) that
        # DataTable parses for display: match on the rendered plain text.
        custom = next((name for name in columns if Text.from_markup(name).plain == label), None)
        column = builtin if builtin in columns else custom
        if column is None:
            self.notify(
                f"{label} is not sortable — sortable: {', '.join(columns)}",
                severity="warning",
            )
            return
        self._apply_sort_column(column, pane=pane)

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

    #: Resource identities — (group, plural), see `_RESTARTABLE` — where each
    #: view-specific action applies; actions absent from the map work on
    #: every view. `check_action` consults this so the footer legend shows
    #: only the current view's keys and overloaded keys (i/u/r) dispatch to
    #: the binding whose view is on screen (issue #114). Identity, not the
    #: kind string: a foreign CRD claiming a bare plural (e.g.
    #: `packagemanifests`) must not surface another view's actions.
    #: `log_search_next`/`log_search_prev` stay unlisted on purpose: they
    #: also serve the describe pane's search (any view) and the
    #: sort-by-name fallback. Fail-closed by design: a kind missing from
    #: `aliases` (e.g. mid-discovery) hides every listed action until its
    #: identity is known.
    _ACTION_VIEWS: ClassVar[dict[str, frozenset[tuple[str, str]]]] = {
        "shell": frozenset({("", "pods"), ("", "nodes")}),
        "logs": frozenset({("", "pods")}),
        "logs_multi": frozenset({("", "pods")}),
        "hint_details": frozenset({("", "pods")}),
        "resize_pod": frozenset({("", "pods")}),
        "transfer": frozenset({("", "pods")}),
        # Core-group identities of FORWARDABLE_KINDS (pods, services).
        "port_forward": frozenset(("", plural) for plural in FORWARDABLE_KINDS),
        "cordon_node": frozenset({("", "nodes")}),
        "uncordon_node": frozenset({("", "nodes")}),
        "drain_node": frozenset({("", "nodes")}),
        "rollout_restart": _RESTARTABLE,
        "scale_resource": _SCALABLE,
        "operator_install": frozenset(
            {(PACKAGES_GROUP, "packagemanifests"), (OPERATORS_GROUP, "installplans")}
        ),
        # The synthetic helm views (group "", client-side plurals).
        "helm_install": frozenset({(HELM_RELEASES_META.group, HELM_RELEASES_META.plural)}),
        "helm_upgrade": frozenset({(HELM_RELEASES_META.group, HELM_RELEASES_META.plural)}),
        "helm_history": frozenset({(HELM_RELEASES_META.group, HELM_RELEASES_META.plural)}),
        "helm_rollback": frozenset({(HELM_REVISIONS_META.group, HELM_REVISIONS_META.plural)}),
    }

    #: Actions that operate on the visible log pane, not the focused view:
    #: the split workflow tails logs from one pane while the other shows a
    #: different kind, so these gate on pane visibility (review of #114).
    _LOG_PANE_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"log_format", "log_wrap", "log_timestamps", "log_save", "log_previous"}
    )

    #: Generic write actions that `_write_target` rejects on synthetic
    #: (client-side, read-only) views such as the helm browser: advertising
    #: them there would be a lie (review of #114). The dedicated helm write
    #: actions stay available through `_ACTION_VIEWS`.
    _SYNTHETIC_GATED_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"delete_resource", "edit_resource"}
    )

    def _action_available(self, action: str) -> bool:
        """Composition availability, independent of the current view: the
        help overlay filters on this alone so off-view keys stay documented
        (issues #73, #114)."""
        return not (action == "toggle_agent" and not self._agent_available)

    def _log_pane_open(self) -> bool:
        """Whether a log pane is currently visible (pre-compose: no)."""
        try:
            return bool(self._log_pane.display)
        except NoMatches:
            return False

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Gate bindings on composition availability and the current view.

        Returning False both hides the binding from the footer and skips it
        during key dispatch, so overloaded keys fall through to the binding
        whose view is on screen (issue #114).
        """
        if not self._action_available(action):
            return False
        if action in self._LOG_PANE_ACTIONS:
            return self._log_pane_open()
        if action in self._SYNTHETIC_GATED_ACTIONS:
            meta = self.aliases.get(self._canonical_kind(self.current_kind))
            if (
                action == "delete_resource"
                and meta is not None
                and (meta.group, meta.plural)
                == (HELM_RELEASES_META.group, HELM_RELEASES_META.plural)
            ):
                # Ctrl+D on the release browser is `helm uninstall`
                # (issue #117) - the one synthetic view where delete works.
                return True
            # Unknown kinds keep the keys: the handler's own guards decide.
            return meta is None or not meta.synthetic
        views = self._ACTION_VIEWS.get(action)
        if views is None:
            return True
        meta = self.aliases.get(self._canonical_kind(self.current_kind))
        return meta is not None and (meta.group, meta.plural) in views

    def action_toggle_agent(self) -> None:
        """Toggle the agent chat panel; show setup hint when unconfigured."""
        if not self._agent_available:
            return
        panel = self._agent_panel
        if panel.display:
            panel.display = False
            self._focused_table().focus()
            return
        panel.display = True
        if self._agent_runtime is None:
            panel.show_setup_hint()
            return
        if self._agent_model_name:
            runtime = self._agent_runtime
            in_tok, out_tok = runtime.total_tokens
            panel.set_header(
                self._agent_model_name,
                in_tok,
                out_tok,
                estimated=runtime.usage_estimated,
                profile=self._agent_profile,
            )
        panel.query_one("#agent-input").focus()

    def on_agent_prompt_submitted(self, message: AgentPromptSubmitted) -> None:
        if self._agent_blocked_in_protected():
            # agent.disable_in_protected (issue #83): in protected contexts
            # the agent must not run at all, not merely gate its writes.
            self.notify(
                f"Agent is disabled in protected context {self._protected_context!r}"
                " (agent.disable_in_protected)",
                severity="warning",
            )
            return
        if self._ctx_switching:
            # A turn started now would run during teardown/retarget and could
            # act on the new cluster with the old cluster's screen context.
            self.notify(
                "A context switch is in progress — try again once it completes",
                severity="warning",
            )
            return
        if self._agent_runtime is None:
            return
        if self._agent_task is not None and not self._agent_task.done():
            return
        panel = self._agent_panel
        panel.begin_turn(message.text)
        self._agent_task = asyncio.create_task(self._run_agent_turn(message.text))

    def _selected_row_name(self) -> str | None:
        table = self._focused_table()
        if table.row_count == 0:
            return None
        ordered = table.ordered_rows
        if table.cursor_row >= len(ordered):
            return None
        return str(ordered[table.cursor_row].key.value)

    def _screen_context(self) -> str:
        """What the agent is told about the screen: the focused pane in
        detail plus a one-line summary of the other pane (issue #48), so
        context stays bounded in a split workspace."""
        selected = self._selected_row_name() or "-"
        selected_ns = ""
        if "/" in selected:
            # Row keys are 'namespace/name' composites; fed verbatim they
            # teach the model to paste the whole string as a resource name
            # (observed: get_resource name='default/otel-…' -> 404). Hand
            # over the two fields the tool calls actually take.
            selected_ns, _, selected = selected.partition("/")
        context = (
            f"context={self.config.kube_context or '-'} "
            f"view={self.current_kind} scope={self.current_scope} "
            f"selected={selected}"
        )
        if selected_ns:
            context += f" selected_ns={selected_ns}"
        context += f" filter={self.filter_pattern or '-'}"
        if len(self._panes) == 2:
            other = self._panes[1 - self._focused_pane]
            context += f" other_pane={other.kind} other_scope={other.scope}"
        return context

    async def _run_agent_turn(self, user_text: str) -> None:
        runtime = self._agent_runtime
        if runtime is None:
            return
        panel = self._agent_panel
        screen_context = self._screen_context()
        if self._ctx_switch_note is not None:
            # One-shot: the conversation only needs to learn about the
            # switch once; afterwards the context= field carries the truth.
            screen_context += f" NOTE: {self._ctx_switch_note}"
            self._ctx_switch_note = None
        # Agent follow: started cluster reads awaiting their result, keyed
        # by call id (the finish event does not carry the arguments).
        pending_reads: dict[str, tuple[str, str]] = {}
        try:
            async for event in runtime.run_turn(user_text, screen_context):
                panel.apply_event(event)
                await self._maybe_follow_agent_read(event, pending_reads)
        except Exception as exc:
            panel.apply_event(AgentError(message=str(exc)))

    async def _maybe_follow_agent_read(
        self,
        event: AgentEvent,
        pending: dict[str, tuple[str, str]],
    ) -> None:
        """Mirror a successful agent cluster read on screen (agent follow).

        Small local models rarely volunteer the UI tools — they call the
        data-returning reads and answer in text while the screen sits
        idle. With follow on, each successful read is mirrored through the
        same UIBridge mapping MCP follow uses (issue #153). Best-effort:
        `mirror_read` never raises, and the bridge guards (approval
        dialogs, screens the user is reading) refuse rather than cover.
        """
        if isinstance(event, ToolCallStarted):
            if event.name in FOLLOWABLE_TOOLS:
                pending[event.call_id] = (event.name, event.arguments)
            return
        if not isinstance(event, ToolCallFinished):
            return
        started = pending.pop(event.call_id, None)
        if started is None or not event.ok or not self._agent_follow:
            return
        name, raw_arguments = started
        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return  # small models emit broken JSON; the read still answered
        if not isinstance(arguments, dict):
            return
        await mirror_read(self._agent_follow_bridge or AppUIBridge(self), name, arguments)

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
        if self._approval_dialog_active():
            # Same "user is deciding" rule as describe: swapping the view
            # beneath an approval dialog mid-decision is disorienting.
            return (
                "ERROR: an approval dialog is open — the user is deciding; "
                "wait for their decision before changing the view"
            )
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

    @staticmethod
    def _agent_log_targets(
        known: list[tuple[str, str, str]], namespace: str, pod: str, container: str | None
    ) -> list[tuple[str, str, str]] | str:
        """The (ns, pod, container) triples to stream, or an "ERROR: ..."."""
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
            return [(namespace, pod, container)]
        return known

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        if self._approval_dialog_active():
            # Opening logs tears down the current log stream (the one the
            # user may be watching beneath the dialog while deciding).
            return (
                "ERROR: an approval dialog is open — the user is deciding; "
                "wait for their decision before opening logs"
            )
        if isinstance(self.screen, DescribeScreen):
            # Same user-priority rule as describe/navigate/drill: opening
            # logs swaps the streams beneath the modal the user is reading.
            return (
                "ERROR: a describe screen is open — the user is reading it; "
                "ask them to close it (Esc) before opening logs"
            )
        if self._stream_logs is None:
            return "ERROR: log streaming unavailable in this session"
        pane_gen = self._log_pane_gen
        try:
            known = await self._agent_pod_triples(namespace, pod)
            triples = self._agent_log_targets(known, namespace, pod, container)
            if isinstance(triples, str):
                return triples
            if pane_gen != self._log_pane_gen:
                # The user (or another turn) changed the log pane while we were
                # resolving containers — user keystrokes take priority.
                return (
                    "ERROR: the log pane changed while resolving containers "
                    "(user action takes priority) — retry if still needed"
                )
            if self._approval_dialog_active():
                # The pre-check can go stale during the awaited lookup: an
                # approval dialog that opened meanwhile still wins before
                # the destructive log-pane teardown below.
                return (
                    "ERROR: an approval dialog is open — the user is deciding; "
                    "wait for their decision before opening logs"
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

    def _approval_dialog_active(self) -> bool:
        """True while a write-approval dialog or write-parameter wizard owns
        the screen: agent- or follow-driven screens must never steal its
        keystroke focus, and every one of these feeds a cluster write."""
        return isinstance(
            self.screen,
            (
                ConfirmScreen,
                ReplicasPrompt,
                ImagePrompt,
                ResizePrompt,
                OperatorInstallPrompt,
                HelmInstallPrompt,
            ),
        )

    def _describe_precheck(self, kind: str, namespace: str | None) -> ResourceMeta | str:
        """Guards + target resolution for agent_open_describe: the meta to
        describe, or an "ERROR: ..." string."""
        if self._approval_dialog_active():
            # Security invariant: approval dialogs are confirmed only by
            # user keystrokes. A describe pushed on top (agent- or MCP
            # follow-driven) would steal that focus mid-approval.
            return (
                "ERROR: an approval dialog is open — the user is deciding; "
                "wait for their decision before opening screens"
            )
        if isinstance(self.screen, DescribeScreen):
            # Same user-priority rule as agent_navigate/agent_drill_down
            # (and the docs/agent.md follow contract): a describe screen on
            # top is being read — covering it with another would replace
            # the content mid-read. User action takes priority.
            return (
                "ERROR: a describe screen is open — the user is reading it; "
                "ask them to close it (Esc) before opening another"
            )
        if self._get_manifest is None:
            return "ERROR: describe unavailable in this session"
        meta = self.aliases.get(kind.strip().lower())
        if meta is None:
            return f"ERROR: unknown kind {kind!r} — not a resource kind in this cluster"
        if meta.namespaced and not namespace:
            return f"ERROR: kind {kind!r} is namespaced — provide the 'namespace' argument"
        return meta

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        meta = self._describe_precheck(kind, namespace)
        if isinstance(meta, str):
            return meta
        if self._get_manifest is None:  # re-narrowed for typing; precheck guarantees it
            return "ERROR: describe unavailable in this session"
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
            # Capture the target's manifest *before* asking for approval:
            # the executed write carries its uid as a precondition, so the
            # approval is bound to this exact object incarnation - a
            # same-named replacement created while the dialog is open gets a
            # 409, not the mutation. The lookup uses the caller's validated
            # alias, not meta.plural: alias resolution is first-wins, so a
            # plural that collides across groups could otherwise resolve to
            # a different resource than the one validated above. The same
            # snapshot feeds the ownership banner - no second round trip.
            snapshot = await self._target_manifest(kind.strip().lower(), ns, name)
        except ApiStatusError:
            return f"ERROR: {self._gvr_label(meta)}/{name} not found{self._write_locus(ns)}"
        uid = _manifest_uid(snapshot) if snapshot is not None else None
        preview = await self._preview_for_action(
            action, meta, ns, name, replicas, resources, uid, stamp
        )
        note = await self._managed_note_from(snapshot, ns) if snapshot is not None else None
        require = name if action == "delete" and not meta.namespaced else None
        decision = await self._await_user_approval(
            f"Agent requests: {action} {self._gvr_label(meta)}/{name}{self._write_locus(ns)}",
            operation,
            require_name=require,
            preview=preview,
            managed_note=note,
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

    async def _target_manifest(
        self, kind_alias: str, ns: str | None, name: str
    ) -> dict[str, Any] | None:
        """Manifest of a write target at request time, looked up by the same
        alias the write was validated with (both resolve through the one
        aliases mapping wired in __main__, so the manifest and the mutation
        address the same resource even when plurals collide across groups).
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
            return await asyncio.wait_for(
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

    async def _target_uid(self, kind_alias: str, ns: str | None, name: str) -> str | None:
        """Uid of a write target at request time — `_target_manifest` with
        only the precondition extracted (same 404/fail-open semantics)."""
        manifest = await self._target_manifest(kind_alias, ns, name)
        return _manifest_uid(manifest) if manifest is not None else None

    async def _managed_note(self, kind_alias: str, ns: str | None, name: str) -> str | None:
        """Ownership banner text for a write dialog, or None (issue #119).

        Best-effort display support, fail-open: no manifest source, a slow
        or failed lookup, or an unmanaged target all yield None — the write
        flow is never blocked, and the target fetch plus the entire
        owner-chain walk share one `_UID_LOOKUP_TIMEOUT` deadline.
        """
        if self._get_manifest is None:
            return None
        try:
            async with asyncio.timeout(_UID_LOOKUP_TIMEOUT):
                manifest = await self._get_manifest(kind_alias, ns, name)
                return await self._walk_managed(manifest, ns)
        except Exception as exc:  # display support only — never blocks the write
            # An API error message can embed the response body (for a
            # Secret, its data): log the exception type, not its payload.
            logger.debug("manager lookup for %s/%s failed: %s", ns, name, type(exc).__name__)
            return None

    async def _managed_note_from(self, manifest: dict[str, Any], ns: str | None) -> str | None:
        """Manager note for an already-fetched manifest; the owner-chain
        walk shares one `_UID_LOOKUP_TIMEOUT` deadline and fails open like
        `_managed_note`."""
        try:
            async with asyncio.timeout(_UID_LOOKUP_TIMEOUT):
                return await self._walk_managed(manifest, ns)
        except Exception as exc:  # display support only — never blocks the write
            # Same payload caution as _managed_note — the exception type
            # only, and nothing derived from the manifest (which may be a
            # Secret's; CodeQL py/clear-text-logging-sensitive-data).
            logger.debug("owner-chain lookup in %s failed: %s", ns, type(exc).__name__)
            return None

    async def _walk_managed(self, manifest: dict[str, Any], ns: str | None) -> str | None:
        """Walk the built-in controller chain when the object itself looks
        unmanaged: a pod owned by rs -> deploy (or job -> cronjob) reports
        the top owner's manager — helm annotations live on the top-level
        object, not on every pod it produced. Callers bound this walk with
        one shared deadline and fail open on any error."""
        found = manager_of(manifest)
        current = manifest
        for _ in range(2):
            if found is not None:
                break
            owner = controller_owner(current)
            if owner is None:
                break
            plural = self._OWNER_CHAIN_PLURALS.get(owner[0])
            if plural is None or self._get_manifest is None:
                break
            current = await self._get_manifest(plural, ns, owner[1])
            found = manager_of(current)
        return found.note if found is not None else None

    async def agent_submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        """External MCP write proposal intake (issue #110).

        Validates exactly like the direct agent write path (kind, RBAC, UID
        capture, dry-run preview) but instead of opening a dialog it queues
        an immutable proposal for later user review — no modal, no focus
        steal. Returns the proposal id; the caller polls
        ``get_write_proposal`` for the terminal outcome.
        """
        store = self._proposal_store
        if store is None:
            return "ERROR: external write proposals are not enabled"
        name = name.strip()
        stamp = restart_stamp()
        # Intake is validated against exactly one context: snapshot it here
        # and recheck before queueing — RBAC, UID capture and the preview all
        # await, and a context switch landing mid-intake would otherwise
        # stamp old-context validation onto a new-context proposal.
        epoch = self._ctx_epoch
        context = self.config.kube_context
        built = self._agent_write_op(
            action, kind, name, namespace, replicas, resources, restarted_at=stamp
        )
        if isinstance(built, str):
            return built
        meta, ns, _op, operation, _detail = built
        arguments_json = json.dumps(
            {
                "action": action,
                "kind": kind.strip().lower(),
                "name": name,
                "namespace": ns,
                "replicas": replicas,
                "resources": resources,
                "restarted_at": stamp,
            }
        )
        # Untrusted-input bound: enforced before any cluster I/O (the store
        # atomically rechecks it at submit) so an oversized payload cannot
        # force an RBAC round trip, UID lookup, or server dry-run.
        if len(arguments_json) > store.max_argument_chars:
            return (
                f"ERROR: proposal arguments exceed {store.max_argument_chars}"
                " characters; the proposal was not queued"
            )
        if not await self._permitted(action, meta, ns, name):
            verb, target = self._write_perm_target(action, meta)
            return f"ERROR: missing permission: {verb} {target}"
        try:
            uid = await self._target_uid(kind.strip().lower(), ns, name)
        except ApiStatusError:
            return f"ERROR: {self._gvr_label(meta)}/{name} not found{self._write_locus(ns)}"
        if uid is None:
            # The interactive path fails open here (a user is watching);
            # an external proposal without a UID binding could mutate a
            # same-named replacement, so it must fail closed instead.
            return (
                "ERROR: could not verify the write target (UID capture"
                " failed); the proposal was not queued — try again"
            )
        preview = await self._preview_for_action(
            action, meta, ns, name, replicas, resources, uid, stamp
        )
        if self._ctx_switching or self._ctx_epoch != epoch or self.config.kube_context != context:
            return (
                "ERROR: the kube context changed while the proposal was being"
                " validated; the proposal was not queued — try again"
            )
        try:
            proposal = store.submit(
                action=action,
                group=meta.group,
                version=meta.version,
                kind=meta.plural,
                namespace=ns,
                name=name,
                arguments_json=arguments_json,
                uid=uid,
                context=context,
                context_epoch=epoch,
                summary=operation,
                preview=tuple(preview or ()),
                session_id=session_id,
                client_name=client_name,
                client_version=client_version,
            )
        except (ProposalClosedError, ProposalLimitError, ProposalTooLargeError) as exc:
            return f"ERROR: {exc}"
        source = client_name or "an external MCP caller"
        self.notify(
            f"Write proposal from {source}: {operation} — review with :proposals",
            severity="warning",
            timeout=10,
        )
        ttl = max(0, int(proposal.expires_at - proposal.created_at))
        return (
            f"proposal {proposal.id} is pending user review in the TUI"
            f" (expires in {ttl}s if unreviewed); poll get_write_proposal"
            " for the outcome"
        )

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        """Terminal-outcome lookup for an external write proposal."""
        store = self._proposal_store
        if store is None:
            return "ERROR: external write proposals are not enabled"
        found = store.get(proposal_id)
        if found is None:
            return "ERROR: unknown proposal id"
        proposal, state, reason = found
        line = f"proposal {proposal.id}: {state} — {proposal.summary}"
        return f"{line} ({reason})" if reason else line

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        """Caller-initiated cancel; only the submitting session may cancel."""
        store = self._proposal_store
        if store is None:
            return "ERROR: external write proposals are not enabled"
        found = store.get(proposal_id)
        if found is not None and store.cancel(proposal_id, session_id=session_id):
            await self._audit_proposal_outcome(found[0], "cancelled", "cancelled by caller")
            return f"proposal {proposal_id} cancelled"
        return "ERROR: proposal not found, not pending, or owned by another session"

    async def _audit_proposal_outcome(
        self, proposal: WriteProposal, state: str, reason: str
    ) -> None:
        """Best-effort audit for a terminal proposal outcome (issue #110).

        These transitions mutate nothing, so a failed append is logged
        instead of blocking — the mutation path itself stays fail-closed in
        `_run_write` (which separately audits executed/failed writes with
        the same proposal provenance). The blocking file append is offloaded
        per audit.py's contract for async contexts.
        """
        await asyncio.to_thread(self._append_proposal_audit, proposal, state, reason)

    def _append_proposal_audit(self, proposal: WriteProposal, state: str, reason: str) -> None:
        """Blocking append of a proposal outcome; call via to_thread."""
        audit = self._audit
        if audit is None:
            return
        try:
            audit.append(
                action=proposal.action,
                kind=proposal.kind,
                group=proposal.group,
                version=proposal.version,
                namespace=proposal.namespace,
                name=proposal.name,
                detail=self._proposal_provenance(proposal),
                outcome=f"proposal {state}: {reason}" if reason else f"proposal {state}",
                # These outcome appends are not serialized with `:ctx`'s
                # set_context: bind the entry to the proposal's own cluster.
                context=proposal.context,
            )
        except Exception:
            logger.warning("could not audit proposal %s outcome %s", proposal.id, state)

    @staticmethod
    def _proposal_provenance(proposal: WriteProposal) -> str:
        """Field-injection-safe audit provenance for an external proposal.

        `client_name`/`client_version` are untrusted MCP client metadata:
        non-printables are stripped at intake, but spaces and `=` could
        still forge field-looking tokens (`session=forged`). Quote them
        (shell-style, so benign values stay bare) so every `key=` field in
        the detail is korvid's own.
        """
        caller = shlex.quote(proposal.client_name or "unknown")
        version = (
            f" version={shlex.quote(proposal.client_version)}" if proposal.client_version else ""
        )
        return (
            f"source=external_mcp proposal={proposal.id}"
            f" caller={caller}{version} session={proposal.session_id}"
        )

    async def _expire_proposals_audited(self, reason: str) -> None:
        """Expire every pending proposal and audit each terminal outcome."""
        store = self._proposal_store
        if store is None:
            return
        for proposal in store.expire_all(reason=reason):
            await self._audit_proposal_outcome(proposal, "expired", reason)

    def _open_proposal_review(self) -> None:
        """`:proposals` — review pending external proposals one at a time."""
        store = self._proposal_store
        if store is None:
            self.notify(
                "External write proposals are disabled (set mcp.write_proposals: true)",
                severity="warning",
            )
            return
        if not store.pending():
            self.notify("No pending write proposals")
            return
        # Never *replace* a live review worker (exclusive=True would cancel
        # it): once a proposal is claimed, cancellation could interrupt
        # `_run_write` mid-mutation and strand the record as `approved`
        # with an uncertain cluster outcome. Duplicate opens are refused.
        if any(w.group == "proposal-review" and not w.is_finished for w in self.workers):
            self.notify("A proposal review is already open", severity="warning")
            return
        self.run_worker(self._review_proposals(store), group="proposal-review")

    async def _review_proposals(self, store: ProposalStore) -> None:
        """Review pending proposals oldest-first until none remain or the
        user dismisses the dialog (a dismissed proposal stays pending)."""
        while True:
            pending = store.pending()
            if not pending:
                self._refresh_status()
                return
            if not await self._review_one_proposal(store, pending[0]):
                self._refresh_status()
                return

    async def _review_one_proposal(self, store: ProposalStore, proposal: WriteProposal) -> bool:
        """Re-validate and put one proposal in front of the user; False stops
        the review loop (dismissal), True moves on to the next proposal."""
        if proposal.context_epoch != self._ctx_epoch or (
            proposal.context != self.config.kube_context
        ):
            await self._resolve_proposal_audited(
                store, proposal, "expired", "kube context changed since submission"
            )
            return True
        rebuilt = self._rebuild_proposal_op(proposal)
        if isinstance(rebuilt, str):
            await self._resolve_proposal_audited(
                store, proposal, "expired", rebuilt.removeprefix("ERROR: ")
            )
            return True
        meta, ns, op, operation, _detail = rebuilt
        if not await self._permitted(proposal.action, meta, ns, proposal.name):
            await self._resolve_proposal_audited(
                store, proposal, "failed", "permission revoked since submission"
            )
            return True
        # The awaited SSAR can be slow: re-read state and context before
        # surfacing the dialog. The proposal may have been cancelled or
        # expired meanwhile, and a `:ctx` switch begun in flight owns its
        # fate (the switch's sweep expires old-context proposals) — never
        # put an already-invalid proposal in front of the user.
        found = store.get(proposal.id)
        if found is None or found[1] != "pending":
            return True
        if self._ctx_switching or proposal.context_epoch != self._ctx_epoch:
            return False
        source = proposal.client_name or "external MCP caller"
        title = (
            f"External proposal from {source}: {proposal.action}"
            f" {self._gvr_label(meta)}/{proposal.name}{self._write_locus(ns)}"
        )
        require = proposal.name if proposal.action == "delete" and not meta.namespaced else None
        decision = await self._await_proposal_decision(
            title,
            self._proposal_dialog_body(proposal, operation),
            require_name=require,
            preview=list(proposal.preview),
        )
        if decision == "dismissed":
            return False
        if decision == "declined":
            await self._resolve_proposal_audited(store, proposal, "denied", "denied by user")
            return True
        await self._execute_proposal(store, proposal, meta, ns, op)
        return True

    async def _await_proposal_decision(
        self,
        title: str,
        operation: str,
        *,
        require_name: str | None,
        preview: list[str] | None,
    ) -> Literal["approved", "declined", "dismissed"]:
        """One ConfirmScreen decision for a proposal; only real key input can
        resolve it. Unlike agent writes this is user-initiated (:proposals),
        so there is no panel gate — but never stack over another dialog where
        a stray keystroke could approve, and treat an unanswered dialog as a
        dismissal (the proposal stays pending until its own TTL)."""
        if len(self.screen_stack) != 1:
            self.notify("Close the current dialog, then run :proposals again", severity="warning")
            return "dismissed"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool | None] = loop.create_future()

        def _done(confirmed: bool | None) -> None:
            if not fut.done():
                fut.set_result(confirmed)

        screen = self._confirm_screen(title, operation, require_name=require_name, preview=preview)
        await self.push_screen(screen, _done)
        try:
            confirmed = await asyncio.wait_for(fut, timeout=_APPROVAL_TIMEOUT)
        except TimeoutError:
            if self.screen is screen:
                with contextlib.suppress(Exception):
                    self.pop_screen()
            return "dismissed"
        if confirmed is None:  # Esc: no decision was made
            return "dismissed"
        return "approved" if confirmed else "declined"

    def _rebuild_proposal_op(
        self, proposal: WriteProposal
    ) -> tuple[ResourceMeta, str | None, Callable[[str | None], Awaitable[None]], str, str] | str:
        """Rebuild the write operation from the proposal's immutable
        arguments at review time: readonly mode, audit availability, kind
        resolution and argument validation are all rechecked — the stored
        record never carries an executable closure."""
        try:
            args = json.loads(proposal.arguments_json)
        except ValueError:
            return "ERROR: proposal arguments are unreadable"
        return self._agent_write_op(
            args["action"],
            args["kind"],
            args["name"],
            args["namespace"],
            args["replicas"],
            args["resources"],
            restarted_at=args["restarted_at"],
        )

    async def _resolve_proposal_audited(
        self, store: ProposalStore, proposal: WriteProposal, state: ProposalState, reason: str
    ) -> None:
        """Resolve a pending proposal and audit the terminal outcome."""
        if store.resolve(proposal.id, state, reason=reason):
            await self._audit_proposal_outcome(proposal, state, reason)

    def _proposal_dialog_body(self, proposal: WriteProposal, operation: str) -> str:
        """The dialog body for a proposal: the operation plus the immutable
        safety bindings issue #110 requires the user to see before approval —
        the caller (explicitly untrusted metadata), the bound kube context
        and epoch, the bound target UID, and the expiry."""
        caller = proposal.client_name or "unknown"
        version = f" {proposal.client_version}" if proposal.client_version else ""
        remaining = max(0, int(proposal.expires_at - time.monotonic()))
        return "\n".join(
            (
                operation,
                "",
                f"caller (untrusted metadata): {caller}{version}",
                f"bound kube context: {proposal.context or '(default)'}"
                f" (epoch {proposal.context_epoch})",
                f"bound target uid: {proposal.uid}",
                f"expires in {remaining}s unless approved or denied",
            )
        )

    async def _fail_proposal(
        self, store: ProposalStore, proposal: WriteProposal, meta: ResourceMeta, reason: str
    ) -> None:
        """Record and audit a pre-write failure of a claimed proposal."""
        store.finish_execution(proposal.id, executed=False, reason=reason)
        await self._audit_proposal_outcome(proposal, "failed", reason)
        self.notify(
            f"Proposal {proposal.action} {meta.plural}/{proposal.name} failed: {reason}",
            severity="error",
        )

    async def _execute_proposal(
        self,
        store: ProposalStore,
        proposal: WriteProposal,
        meta: ResourceMeta,
        ns: str | None,
        op: Callable[[str | None], Awaitable[None]],
    ) -> None:
        """Claim and execute an approved proposal under the nav lock so a
        context switch or `:mcp off` cannot interleave: the claim itself is
        linearized with the shutdown/switch expiry sweeps (if one of those
        won while the dialog was open or the lock was contended, there is no
        pending proposal left to claim). After the claim, the context epoch
        and RBAC are rechecked, then the UID binding, then the same
        fail-closed audit-before-mutation path as every other write."""
        async with self._nav_lock:
            if not store.begin_execution(proposal.id):
                # Cancelled, TTL-expired, or invalidated (MCP shutdown /
                # context switch) before the claim landed: the approval no
                # longer has a pending proposal to execute.
                self.notify("The proposal was withdrawn before approval landed", severity="warning")
                return
            if proposal.context_epoch != self._ctx_epoch or (
                proposal.context != self.config.kube_context
            ):
                await self._fail_proposal(
                    store, proposal, meta, "the kube context changed before execution"
                )
                return
            if not await self._permitted(proposal.action, meta, ns, proposal.name):
                await self._fail_proposal(
                    store, proposal, meta, "permission revoked before execution"
                )
                return
            args = json.loads(proposal.arguments_json)
            try:
                current_uid = await self._target_uid(args["kind"], ns, proposal.name)
            except ApiStatusError:
                await self._fail_proposal(store, proposal, meta, "the target no longer exists")
                return
            if current_uid != proposal.uid:
                await self._fail_proposal(
                    store,
                    proposal,
                    meta,
                    "the target was replaced since the proposal was created",
                )
                return
            detail = self._proposal_provenance(proposal)
            write = asyncio.ensure_future(
                self._run_write(
                    proposal.action, meta, ns, proposal.name, op(proposal.uid), detail=detail
                )
            )
            try:
                outcome = await asyncio.shield(write)
            except asyncio.CancelledError:
                # Worker cancellation (TUI shutdown) after the claim: the
                # record must still reach a terminal state, never a
                # permanent `approved` over an uncertain cluster outcome.
                await self._settle_interrupted_execution(store, proposal, write)
                raise
            store.finish_execution(
                proposal.id, executed=outcome == "done", reason="" if outcome == "done" else outcome
            )
            if outcome == "done":
                self.notify(f"Executed proposal: {proposal.summary}")

    async def _settle_interrupted_execution(
        self, store: ProposalStore, proposal: WriteProposal, write: asyncio.Future[str]
    ) -> None:
        """A claimed execution's worker was cancelled mid-write. Use the
        write's real outcome when it already settled; otherwise abandon the
        in-flight call and record the uncertainty — the API server may or
        may not have committed the mutation by the time cancellation lands.
        """
        if write.done() and not write.cancelled() and write.exception() is None:
            # _run_write already audited this outcome itself.
            outcome = write.result()
            store.finish_execution(
                proposal.id, executed=outcome == "done", reason="" if outcome == "done" else outcome
            )
            return
        write.cancel()
        reason = "interrupted before completion — the cluster outcome is uncertain"
        store.finish_execution(proposal.id, executed=False, reason=reason)
        # _run_write only got as far as its intent record: the terminal
        # outcome must reach the audit trail even while cancellation is
        # unwinding — shield the append so a second cancel cannot skip it
        # (the offloaded thread completes regardless).
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(self._audit_proposal_outcome(proposal, "failed", reason))

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
        managed_note: str | None = None,
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

        screen = self._confirm_screen(
            title,
            operation,
            require_name=require_name,
            preview=preview,
            managed_note=managed_note,
        )
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
        footer = self._provider_footer(manifest)
        if share:
            self._describe_pane.show(title, manifest, events, footer_note=footer)
        else:
            await self.push_screen(DescribeScreen(title, manifest, events, footer_note=footer))

    def _provider_footer(self, manifest: dict[str, Any]) -> str | None:
        """One-line describe footer for Service/Ingress on a detected provider.

        A pointer, not a catalog (issue #30): the CSP annotation knowledge
        lives in the agent, so the footer just says where to ask.
        """
        if self._provider_hint is None:
            return None
        if manifest.get("kind") not in ("Service", "Ingress"):
            return None
        return (
            f"provider: {self._provider_hint} — ask the agent about "
            "load balancer annotations (ctrl+a)"
        )

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

    async def _teardown_forwards(self, registry: ForwardRegistry) -> list[ForwardRecord]:
        """Stop every forward (app exit / `:ctx` switch), auditing in order.

        Session-scoped by design (issue #38): forwards never outlive the
        app that started them. stop_all() polls synchronously up to the
        grace deadline — kept off the closing event loop. It also releases
        any handshake waiters, so in-flight readiness confirmations resolve
        promptly; they are awaited before the stops are enqueued so an exit
        during startup still logs the start entry first (never a stop-only
        or reversed trail).

        Returns:
            The stopped records, so a context switch can report the count.
        """
        self._forwards_closing = True
        # Launches and re-attaches whose spawn is still off-loop must land
        # (and enqueue their start entries) before any stop below is recorded.
        for launch in list(self._launching_forwards):
            with contextlib.suppress(Exception):
                await launch.wait()
        for done in list(self._reattaching_forwards):
            with contextlib.suppress(Exception):
                await done.wait()
        records = await asyncio.to_thread(registry.stop_all)
        for confirm in [w for workers in self._confirming_forwards.values() for w in workers]:
            with contextlib.suppress(Exception):
                await confirm.wait()
        # User stops whose deferred audit worker never got to run (shutdown
        # cancels workers): their start entries are enqueued by now, so
        # flushing here keeps the order — user stops, then teardown stops.
        for spec in self._deferred_stop_audits.values():
            if self._audit is not None:
                self._enqueue_forward_audit("port-forward-stop", spec)
        self._deferred_stop_audits.clear()
        for record in records:
            if self._audit is not None:
                self._enqueue_forward_audit("port-forward-stop", record.spec, teardown=True)
        return records

    async def _reap_dispatches(self) -> None:
        """Refuse new foreign UI work and reap in-flight bridge dispatches
        (issue #165): the MCP server stays live until after run_async()
        returns, so a request racing teardown could otherwise spawn work
        (log streams) after the unmount sweeps and leave it alive against
        an unmounted app."""
        self._app_context = None
        for pending in [t for t in self._dispatch_tasks if not t.done()]:
            pending.cancel()
        if self._dispatch_tasks:
            await asyncio.gather(*self._dispatch_tasks, return_exceptions=True)
        self._dispatch_tasks.clear()

    async def on_unmount(self) -> None:
        await self._reap_dispatches()
        # Cancel any active log stream tasks before the event loop shuts down.
        # A proposal must never outlive the session that previewed it; close
        # the store first so an in-flight submission cannot land after the
        # final sweep.
        if self._proposal_store is not None:
            self._proposal_store.close()
        await self._expire_proposals_audited("the TUI session ended")
        if self._ns_prefetch_task is not None:
            self._ns_prefetch_task.cancel()
        if self._ctx_prefetch_task is not None:
            self._ctx_prefetch_task.cancel()
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
        if self._forwards is not None:
            await self._teardown_forwards(self._forwards)
        # Flush pending forward audits (e.g. a Ctrl-D pressed right before
        # quit) so no queued entry is lost: let a mid-drain worker finish,
        # then drain the remainder directly — workers won't run past here.
        worker = self._forward_audit_worker
        if worker is not None and not worker.is_finished:
            with contextlib.suppress(Exception):
                await worker.wait()
        await self._drain_forward_audits()
        await self.watch_manager.stop_all()


class AppUIBridge(UIBridge):
    """Nominal `UIBridge` adapter over `KorvidApp`.

    The layer-boundary interface must be an `abc.ABC` (AGENTS.md), but
    Textual's `App` metaclass conflicts with `ABCMeta`, so the app cannot
    inherit `UIBridge` directly — this thin adapter conforms nominally and
    delegates to the app's bridge methods.

    Every call is marshaled onto the app-owned execution context (issue
    #165): MCP requests (and the follow mirrors they spawn) arrive in
    tasks whose context lacks Textual's `active_app` ContextVar, and
    composing a widget tree there (`DescribeScreen`'s `VerticalScroll`)
    raised `NoActiveAppError` and terminated the app. Marshaling at this
    single boundary also fixes the downstream-task hazard: log-stream
    tasks spawned inside a dispatched call inherit the app context instead
    of carrying the MCP request context for the stream's lifetime.
    """

    def __init__(self, app: KorvidApp) -> None:
        self._app = app

    async def _dispatch(self, coro: Coroutine[Any, Any, str]) -> str:
        """Run one bridge coroutine inside a copy of the app context.

        A fresh copy per call: a `contextvars.Context` cannot be entered
        concurrently, and the serialized proxy is not the only caller
        (the in-app agent path may overlap a queued MCP call's dispatch).
        Cancellation propagates into the inner task so shutdown never
        strands UI work.
        """
        snapshot = self._app._app_context
        if snapshot is None:
            # Reachable in production on both edges: the MCP endpoint goes
            # live before app.run_async() (pre-mount), and on_unmount
            # invalidates the snapshot so a request racing teardown cannot
            # spawn work against an unmounted app. Refuse instead (and
            # close the coroutine so it never warns as un-awaited).
            coro.close()
            return "ERROR: UI not ready — the app is starting or shutting down; retry shortly"
        task = asyncio.get_running_loop().create_task(
            coro, context=snapshot.run(contextvars.copy_context)
        )
        self._app._dispatch_tasks.add(task)
        task.add_done_callback(self._app._dispatch_tasks.discard)
        try:
            return await task
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        return await self._dispatch(self._app.agent_navigate(view, namespace))

    async def agent_set_filter(self, pattern: str) -> str:
        return await self._dispatch(self._app.agent_set_filter(pattern))

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        return await self._dispatch(self._app.agent_open_logs(pod, namespace, container))

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        return await self._dispatch(self._app.agent_open_describe(kind, name, namespace))

    async def agent_drill_down(self, name: str) -> str:
        return await self._dispatch(self._app.agent_drill_down(name))

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        return await self._dispatch(
            self._app.agent_request_write(action, kind, name, namespace, replicas, resources)
        )

    async def agent_submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        return await self._dispatch(
            self._app.agent_submit_write_proposal(
                action,
                kind,
                name,
                namespace,
                replicas,
                resources,
                session_id=session_id,
                client_name=client_name,
                client_version=client_version,
            )
        )

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        return await self._dispatch(self._app.agent_get_write_proposal(proposal_id))

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        return await self._dispatch(
            self._app.agent_cancel_write_proposal(proposal_id, session_id=session_id)
        )
