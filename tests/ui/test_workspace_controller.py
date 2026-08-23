"""Direct unit tests for `WorkspaceController` (issue #187 / Deep Task 3).

The controller owns resource-view navigation, filter/sort transitions, the
drill down/pop/prewarm/watch-release flow, the hierarchy lookup/open/return/
goto flow, the relationship snapshot load/open flow, and the split-pane
lifecycle — together with the workspace-only mutable state (`_nav_lock`,
prewarm leases, hierarchy context, jump-poll budget, render-coalescing set,
metrics target). It reaches Textual only through the narrow `UiSurface`,
`WorkspaceSurface`, and `ContextGuard` boundaries plus a handful of typed
collaborator ports, so every behaviour here is exercised without a running
Textual app.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.k8s.components import ComponentRef
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary
from korvid.ui.ui_surface import Severity, UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.workspace_controller import (
    RELATIONSHIP_GROUP,
    ContextGuard,
    PermissionCheck,
    WorkspaceController,
    WorkspaceSurface,
)
from korvid.ui.workspace_state import PaneState, WorkspaceState

# ---------------------------------------------------------------------------
# Fixtures / metas
# ---------------------------------------------------------------------------

_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
_RS_META = ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True)
_SYNTHETIC_META = ResourceMeta("HelmRelease", "helmreleases", "", "v1", True, synthetic=True)
_ALIASES = build_alias_map([PODS_META, _DEPLOY_META, _RS_META])


def _summary(
    name: str,
    *,
    namespace: str = "default",
    kind: str = "Pod",
    uid: str = "",
    owner_uids: tuple[str, ...] = (),
) -> GenericSummary:
    return GenericSummary(
        name=name, namespace=namespace, kind=kind, created="", uid=uid, owner_uids=owner_uids
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Note:
    message: str
    severity: str


class FakeUi(UiSurface):
    """Records notifications, workers, screens and cancellations."""

    def __init__(self) -> None:
        self.notes: list[_Note] = []
        self.workers: list[tuple[Any, str, bool, bool]] = []  # (work, group, exclusive, exit)
        self.screens: list[Any] = []
        self.screen_callbacks: list[Any] = []
        self.cancelled: list[str] = []
        self._run_workers = False

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: Severity = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notes.append(_Note(message, severity))

    def push_screen(self, screen: Any, callback: Any = None) -> Any:
        self.screens.append(screen)
        self.screen_callbacks.append(callback)
        return None

    def run_worker(
        self,
        work: Any,
        *,
        exclusive: bool = False,
        group: str = "default",
        name: str = "",
        exit_on_error: bool = True,
        thread: bool = False,
    ) -> Any:
        self.workers.append((work, group, exclusive, exit_on_error))
        if asyncio.iscoroutine(work) and not self._run_workers:
            work.close()
        return None

    async def cancel_workers(self, group: str) -> None:
        self.cancelled.append(group)

    def suspend(self) -> contextlib.AbstractContextManager[None]:
        raise NotImplementedError  # pragma: no cover

    def refresh(self) -> None:  # pragma: no cover
        pass

    def call_from_thread(
        self, callback: Callable[..., Any], *args: Any
    ) -> None:  # pragma: no cover
        callback(*args)

    def call_later(self, callback: Callable[..., None], *args: Any) -> None:  # pragma: no cover
        callback(*args)

    def progress(self, label: str) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def is_current_screen(self, screen: Any) -> bool:  # pragma: no cover
        return True

    def screen_depth(self) -> int:
        return self._depth if hasattr(self, "_depth") else 1

    def set_depth(self, depth: int) -> None:
        self._depth = depth


class FakeView(ViewState):
    def __init__(self, state: WorkspaceState, store: ResourceStore) -> None:
        self._state = state
        self._store = store
        self.aliases_map: dict[str, ResourceMeta] = dict(_ALIASES)
        self.selection: tuple[str | None, str | None] = ("default", "sel")
        self.uid: str | None = None

    def current_kind(self) -> str:
        return self._state.current_kind

    def current_scope(self) -> str:
        return self._state.current_scope

    def current_namespace(self) -> str:
        return self._state.current_scope

    def canonical_kind(self, kind: str) -> str:
        meta = self.aliases_map.get(kind)
        return meta.plural if meta is not None else kind

    def aliases(self) -> Mapping[str, ResourceMeta]:
        return self.aliases_map

    def resources(self, kind: str, scope: str) -> list[Summary]:
        return self._store.get(kind, scope)

    def readonly(self) -> bool:  # pragma: no cover
        return False

    def default_namespace(self) -> str | None:
        return "default"

    def selected_ns_name(self) -> tuple[str | None, str | None]:
        return self.selection

    def selected_uid(self, namespace: str | None, name: str) -> str | None:
        return self.uid

    def gvr_label(self, meta: ResourceMeta) -> str:
        return f"{meta.plural}.{meta.group}" if meta.group else meta.plural

    def write_locus(self, namespace: str | None) -> str:  # pragma: no cover
        return "cluster-wide" if namespace is None else f"in ns/{namespace}"


class FakeSurface(WorkspaceSurface):
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.mounted: list[str] = []
        self.removed: list[str] = []
        self.focused: list[str] = []
        self.rendered: list[tuple[str, str | None]] = []
        self._focused_is_table = True
        self._has_tables = True
        self._has_focus = False
        self.focus_row_result = True
        self._hierarchy_open = False
        self.hierarchy_trees: list[Any] = []
        self.namespace_words: list[list[str]] = []
        self.opened_pickers: list[list[str]] = []
        self.row_key: str | None = None

    def set_namespace_words(self, names: list[str]) -> None:
        self.namespace_words.append(list(names))

    def open_namespace_picker(self, names: list[str]) -> None:
        self.opened_pickers.append(list(names))

    def focused_row_key(self) -> str | None:
        return self.row_key

    def render_table(self, kind: str, *, only: PaneState | None = None) -> None:
        self.calls.append(("render", kind))
        self.rendered.append((kind, only.table_id if only is not None else None))

    def refresh_empty_state(self, kind: str) -> None:
        self.calls.append(("empty_state", kind))

    def hide_empty_state(self) -> None:
        self.calls.append(("hide_empty_state", None))

    async def mount_pane_table(self, pane: PaneState) -> None:
        self.calls.append(("mount", pane.table_id))
        self.mounted.append(pane.table_id)

    async def remove_pane_table(self, table_id: str) -> None:
        self.calls.append(("remove", table_id))
        self.removed.append(table_id)

    def unsplit_survivor(self, table_id: str) -> None:
        self.calls.append(("unsplit", table_id))

    def focus_table(self, table_id: str) -> None:
        self.calls.append(("focus_table", table_id))
        self.focused.append(table_id)

    def focused_is_table(self) -> bool:
        return self._focused_is_table

    def has_tables(self) -> bool:
        return self._has_tables

    def has_focus(self) -> bool:
        return self._has_focus

    def update_pane_focus_classes(self) -> None:
        self.calls.append(("focus_classes", None))

    def focus_row(self, row_key: str) -> bool:
        self.calls.append(("focus_row", row_key))
        return self.focus_row_result

    def hide_describe(self) -> None:
        self.calls.append(("hide_describe", None))

    def refresh_status(self) -> None:
        self.calls.append(("status", None))

    def refresh_bindings(self) -> None:
        self.calls.append(("bindings", None))

    def update_hierarchy_tree(self, root: Any) -> None:
        self.calls.append(("update_tree", root))
        self.hierarchy_trees.append(root)

    def hierarchy_open(self) -> bool:
        return self._hierarchy_open

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@dataclasses.dataclass
class FakeKeyEvent:
    """Minimal `KeyEvent` stand-in for `handle_pane_chord`: records the
    stop/prevent-default calls the swallow behavior depends on."""

    key: str
    stopped: bool = False
    prevented: bool = False

    def stop(self, stop: bool = True) -> None:
        self.stopped = stop

    def prevent_default(self, prevent: bool = True) -> None:
        self.prevented = prevent


class FakeContext(ContextGuard):
    def __init__(self) -> None:
        self._epoch = 0
        self._switching = False
        self.reads = True

    def epoch(self) -> int:
        return self._epoch

    def switching(self) -> bool:
        return self._switching

    def reads_allowed(self) -> bool:
        return self.reads


class FakeWatch:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.stopped: list[tuple[str, str]] = []
        self._active: set[tuple[str, str]] = set()

    @property
    def active(self) -> set[tuple[str, str]]:
        return set(self._active)

    async def start(self, kind: str, scope: str) -> None:
        self.started.append((kind, scope))
        self._active.add((kind, scope))

    async def stop(self, kind: str, scope: str) -> None:
        self.stopped.append((kind, scope))
        self._active.discard((kind, scope))

    def seed_active(self, kind: str, scope: str) -> None:
        self._active.add((kind, scope))


class FakeMetrics:
    def __init__(self) -> None:
        self.started: list[str | None] = []
        self.stopped = 0

    async def start(self, namespace: str | None) -> None:
        self.started.append(namespace)

    async def stop(self) -> None:
        self.stopped += 1


class FakeLogs:
    def __init__(self) -> None:
        self.closed_for: list[object] = []

    async def close_if_owned_by(self, pane: object) -> None:
        self.closed_for.append(pane)


class FakeHints:
    def __init__(self) -> None:
        self.refreshed = 0

    def refresh_for_focus(self) -> None:
        self.refreshed += 1


class FakeLoader:
    def __init__(self, graph: Any = None) -> None:
        self.graph = graph or object()
        self.calls: list[Any] = []

    async def load(
        self, root: Any, namespace: str | None, aliases: Mapping[str, ResourceMeta]
    ) -> Any:
        self.calls.append((root, namespace))
        return self.graph


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Bundle:
    ctl: WorkspaceController
    state: WorkspaceState
    store: ResourceStore
    ui: FakeUi
    surface: FakeSurface
    view: FakeView
    context: FakeContext
    watch: FakeWatch
    metrics: FakeMetrics
    logs: FakeLogs
    hints: FakeHints
    loader: FakeLoader


def _make(
    *,
    kind: str = "pods",
    scope: str = "default",
    config: KorvidConfig | None = None,
    get_manifest: Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None = None,
    get_helm_components: Callable[[str, str], Awaitable[list[ComponentRef]]] | None = None,
    describe_named: Callable[[str, str, str], Coroutine[Any, Any, None]] | None = None,
    check_permission: PermissionCheck | None = None,
    list_namespaces: Callable[[], Awaitable[list[str]]] | None = None,
    loader: FakeLoader | None = None,
) -> _Bundle:
    state = WorkspaceState(kind, scope)
    store = ResourceStore()
    ui = FakeUi()
    surface = FakeSurface()
    view = FakeView(state, store)
    context = FakeContext()
    watch = FakeWatch()
    metrics = FakeMetrics()
    logs = FakeLogs()
    hints = FakeHints()
    loader = loader if loader is not None else FakeLoader()
    cfg = config if config is not None else KorvidConfig()

    async def _describe(kind_: str, ns: str, name: str) -> None:  # pragma: no cover
        return None

    ctl = WorkspaceController(
        state=state,
        store=store,
        watch_manager=watch,
        metrics=metrics,
        relationship_loader=loader,
        ui=ui,
        surface=surface,
        view=view,
        context=context,
        logs=logs,
        hints=hints,
        config=lambda: cfg,
        get_manifest=lambda: get_manifest,
        get_helm_components=lambda: get_helm_components,
        olm_alias_key=lambda plural: None,
        describe_named=describe_named or _describe,
        check_permission=lambda: check_permission,
        list_namespaces=lambda: list_namespaces,
    )
    return _Bundle(
        ctl, state, store, ui, surface, view, context, watch, metrics, logs, hints, loader
    )


# ---------------------------------------------------------------------------
# 1. Navigation serialization + view transition
# ---------------------------------------------------------------------------


async def test_navigate_transitions_kind_and_swaps_watch() -> None:
    b = _make(kind="pods", scope="default")
    b.watch.seed_active("pods", "default")
    await b.ctl.navigate("deployments", None)
    assert b.state.current_kind == "deployments"
    assert ("pods", "default") in b.watch.stopped
    assert ("deployments", "default") in b.watch.started
    assert ("render", "deployments") in b.surface.calls
    assert ("status", None) in b.surface.calls


async def test_navigate_serializes_under_nav_lock() -> None:
    b = _make(kind="pods", scope="default")
    await b.ctl.nav_lock.acquire()
    task = asyncio.create_task(b.ctl.navigate("deployments", None))
    await asyncio.sleep(0)
    assert b.state.current_kind == "pods"  # blocked on the lock
    b.ctl.nav_lock.release()
    await task
    assert b.state.current_kind == "deployments"


async def test_navigate_bails_when_initiating_pane_closed() -> None:
    b = _make(kind="pods", scope="default")
    b.state.split()  # focus pane-1
    pane1 = b.state.focused
    await b.ctl.nav_lock.acquire()
    task = asyncio.create_task(b.ctl.navigate("deployments", None))
    await asyncio.sleep(0)
    b.state.close_focused()  # closes pane1 while navigate waits on the lock
    b.ctl.nav_lock.release()
    await task
    assert pane1.kind == "pods"  # transition never landed on the closed pane


async def test_navigate_command_abandons_drill_and_hierarchy_return() -> None:
    b = _make(kind="pods", scope="default")
    from korvid.ui.navigation import DrillLevel

    b.state.focused.drill.push(
        DrillLevel(
            parent_kind="deployments",
            parent_name="web",
            parent_namespace="default",
            parent_uid="u1",
            child_kind="replicasets",
        )
    )
    b.state.focused.hierarchy_return = object()  # type: ignore[assignment]  # sentinel
    await b.ctl.navigate_command("pods", None)
    assert b.state.focused.drill.peek() is None
    assert b.state.focused.hierarchy_return is None


# ---------------------------------------------------------------------------
# 2. Split / focus / close / collapse with side-effect ordering
# ---------------------------------------------------------------------------


async def test_split_mounts_pane_and_starts_watch() -> None:
    b = _make(kind="pods", scope="default")
    await b.ctl.split_pane()
    assert b.state.is_split
    assert b.surface.mounted == ["pane-1"]
    assert ("pods", "default") in b.watch.started
    assert ("hide_empty_state", None) in b.surface.calls
    assert "pane-1" in b.surface.focused


async def test_split_rejected_when_already_split() -> None:
    b = _make(kind="pods", scope="default")
    await b.ctl.split_pane()
    b.surface.mounted.clear()
    await b.ctl.split_pane()
    assert b.surface.mounted == []
    assert any(n.severity == "warning" for n in b.ui.notes)


async def test_close_focused_pane_orders_logs_watch_remove() -> None:
    b = _make(kind="pods", scope="default")
    await b.ctl.split_pane()
    await b.ctl.navigate("deployments", None)  # pane-1 now on deployments
    b.surface.calls.clear()
    b.logs.closed_for.clear()
    await b.ctl.close_focused_pane()
    assert not b.state.is_split
    # the closing pane's logs are dropped, and its unique watch stopped
    assert b.logs.closed_for  # close_if_owned_by(closing)
    assert ("deployments", "default") in b.watch.stopped
    order = b.surface.names()
    assert order.index("remove") < order.index("unsplit")


async def test_focus_other_pane_moves_focus_and_refreshes() -> None:
    b = _make(kind="pods", scope="default")
    await b.ctl.split_pane()
    assert b.state.focused_index == 1
    b.hints.refreshed = 0
    b.ctl.focus_other_pane()
    assert b.state.focused_index == 0
    assert b.hints.refreshed == 1
    assert ("status", None) in b.surface.calls


async def test_collapse_split_removes_second_pane_widget() -> None:
    b = _make(kind="pods", scope="default")
    await b.ctl.split_pane()
    b.surface.removed.clear()
    await b.ctl.collapse_split()
    assert not b.state.is_split
    assert b.surface.removed == ["pane-1"]


async def test_handle_pane_chord_valid_key_runs_action_and_resets() -> None:
    """The second chord key `v` splits the pane and disarms the chord."""
    b = _make(kind="pods", scope="default")
    arm = FakeKeyEvent("ctrl+w")
    await b.ctl.handle_pane_chord(arm)
    assert b.ctl.chord_pending is True
    assert arm.stopped
    assert arm.prevented

    action = FakeKeyEvent("v")
    await b.ctl.handle_pane_chord(action)
    assert b.state.is_split  # the mapped action ran
    assert b.ctl.chord_pending is False  # the chord resets after handling
    assert action.stopped
    assert action.prevented


async def test_handle_pane_chord_unmapped_key_resets_without_action() -> None:
    """An unmapped second key still swallows the keypress and disarms - it
    must never fall through to the table's normal binding (issue #48)."""
    b = _make(kind="pods", scope="default")
    await b.ctl.handle_pane_chord(FakeKeyEvent("ctrl+w"))
    assert b.ctl.chord_pending is True

    stray = FakeKeyEvent("x")
    await b.ctl.handle_pane_chord(stray)
    assert not b.state.is_split  # no action ran for an unmapped key
    assert b.ctl.chord_pending is False  # still swallowed and reset
    assert stray.stopped
    assert stray.prevented


# ---------------------------------------------------------------------------
# 3. Drill prewarm lease acquire/release + watch cleanup
# ---------------------------------------------------------------------------


async def test_prewarm_acquires_lease_and_starts_watch() -> None:
    b = _make(kind="deployments", scope="default")
    await b.ctl.prewarm_view("replicasets", "default", lambda rows: True)
    assert b.ctl._prewarm_leases[("replicasets", "default")] == 1
    assert ("replicasets", "default") in b.watch.started


async def test_stop_watch_if_unused_reaps_when_last_lease() -> None:
    b = _make(kind="deployments", scope="default")
    await b.ctl.prewarm_view("replicasets", "default", lambda rows: True)
    await b.ctl.stop_watch_if_unused("replicasets", "default")
    assert ("replicasets", "default") not in b.ctl._prewarm_leases
    assert ("replicasets", "default") in b.watch.stopped


async def test_stop_watch_keeps_stream_for_displayed_pane() -> None:
    b = _make(kind="replicasets", scope="default")
    # Acquire a real lease (an always-true `ready` returns on the first
    # check, so this completes without waiting on the timeout).
    await b.ctl.prewarm_view("replicasets", "default", lambda rows: True)
    await b.ctl.stop_watch_if_unused("replicasets", "default")
    # a pane displays replicasets/default, so the stream is not reaped
    assert ("replicasets", "default") not in b.watch.stopped


async def test_prewarm_skips_restart_for_live_pane_backed_watch() -> None:
    b = _make(kind="replicasets", scope="default")
    b.watch.seed_active("replicasets", "default")
    await b.ctl.prewarm_view("replicasets", "default", lambda rows: True)
    assert ("replicasets", "default") not in b.watch.started  # already warm


async def test_drill_into_pushes_level_and_navigates() -> None:
    b = _make(kind="deployments", scope="default")
    b.store.apply_event(
        "deployments", "default", "ADDED", _summary("web", kind="Deployment", uid="dep-uid")
    )
    b.store.apply_event(
        "replicasets",
        "default",
        "ADDED",
        _summary("web-rs", kind="ReplicaSet", uid="rs", owner_uids=("dep-uid",)),
    )
    err = await b.ctl.drill_into("default", "web")
    assert err is None
    assert b.state.current_kind == "replicasets"
    assert b.state.focused.drill.parent_uid == "dep-uid"


async def test_drill_into_reports_missing_object() -> None:
    b = _make(kind="deployments", scope="default")
    err = await b.ctl.drill_into("default", "ghost")
    assert err is not None
    assert "ghost" in err
    assert b.state.current_kind == "deployments"


# ---------------------------------------------------------------------------
# 4. Hierarchy stale-result protection + goto
# ---------------------------------------------------------------------------


async def test_hierarchy_pick_discarded_after_context_switch() -> None:
    b = _make(kind="pods", scope="default")
    origin = (b.state.focused, "pods", "default")
    b.context._epoch = 5  # a switch crossed since epoch 0 was captured
    b.ctl._on_hierarchy_pick(0, origin, ("goto", "pods", "default", "web"))
    assert b.ui.workers == []  # no navigation worker launched
    assert any(n.severity == "warning" for n in b.ui.notes)


async def test_hierarchy_pick_goto_launches_jump_worker() -> None:
    b = _make(kind="deployments", scope="default")
    origin = (b.state.focused, "deployments", "default")
    b.ctl._on_hierarchy_pick(0, origin, ("goto", "pods", "default", "web"))
    assert len(b.ui.workers) == 1
    _work, group, exclusive, _exit = b.ui.workers[0]
    assert group == "hierarchy"
    assert exclusive is True


async def test_jump_to_object_aborts_on_context_switch() -> None:
    b = _make(kind="pods", scope="default")
    await b.ctl.jump_to_object("pods", "default", "web", epoch=b.context._epoch - 1)
    # crossed epoch: no navigation, no focus attempt
    assert b.state.current_kind == "pods"
    assert ("focus_row", "default/web") not in b.surface.calls


async def test_jump_to_object_navigates_and_focuses_row() -> None:
    b = _make(kind="pods", scope="default")
    b.surface.focus_row_result = True
    await b.ctl.jump_to_object("deployments", "default", "web")
    assert b.state.current_kind == "deployments"
    assert ("focus_row", "default/web") in b.surface.calls


# ---------------------------------------------------------------------------
# 5. Relationship worker group / result guard / error visibility
# ---------------------------------------------------------------------------


async def test_show_relationships_without_loader_warns() -> None:
    b = _make(kind="pods", scope="default", loader=FakeLoader())
    b.ctl._relationship_loader = None  # composition without the graph
    b.ctl.show_relationships()
    assert any("Relationships unavailable" in n.message for n in b.ui.notes)
    assert b.ui.workers == []


async def test_show_relationships_launches_exit_on_error_false_worker() -> None:
    b = _make(kind="pods", scope="default")
    b.store.apply_event("pods", "default", "ADDED", _summary("sel", uid="p1"))
    b.view.selection = ("default", "sel")
    b.ctl.show_relationships()
    assert len(b.ui.workers) == 1
    _work, group, exclusive, exit_on_error = b.ui.workers[0]
    assert group == RELATIONSHIP_GROUP
    assert exclusive is True
    assert exit_on_error is False


async def test_relationship_result_discarded_after_context_switch() -> None:
    b = _make(kind="pods", scope="default")
    b.context._epoch = 3
    b.ctl.on_relationship_result(0, ("goto", "apps", "Deployment", "default", "web"))
    assert b.ui.workers == []
    assert any(n.severity == "warning" for n in b.ui.notes)


async def test_cancel_relationship_workers_cancels_group() -> None:
    b = _make(kind="pods", scope="default")
    await b.ctl.cancel_relationship_workers()
    assert RELATIONSHIP_GROUP in b.ui.cancelled


# ---------------------------------------------------------------------------
# 6. Filter and sort state / render refresh
# ---------------------------------------------------------------------------


async def test_set_filter_updates_state_and_renders_focused_pane() -> None:
    b = _make(kind="pods", scope="default")
    b.ctl.set_filter("api")
    assert b.state.filter_pattern == "api"
    assert b.state.resource_filter.active
    assert b.surface.rendered[-1] == ("pods", b.state.focused.table_id)
    assert ("status", None) in b.surface.calls


async def test_clear_filter_resets_state() -> None:
    b = _make(kind="pods", scope="default")
    b.ctl.set_filter("api")
    b.ctl.clear_filter()
    assert b.state.filter_pattern == ""
    assert not b.state.resource_filter.active


async def test_sort_by_toggles_and_renders() -> None:
    b = _make(kind="pods", scope="default")
    b.ctl.sort_by("age")
    assert b.state.sorts.get("pods") is not None
    assert b.state.sorts["pods"].column == "age"
    assert ("render", "pods") in b.surface.calls


async def test_sort_command_clears_on_bare_sort() -> None:
    b = _make(kind="pods", scope="default")
    b.ctl.sort_by("age")
    b.ctl.sort_command(None)
    assert b.state.sorts.get("pods") is None


async def test_sort_by_cpu_ignored_off_pods_view() -> None:
    b = _make(kind="deployments", scope="default")
    b.ctl.sort_by("cpu")
    assert b.state.sorts.get("deployments") is None


# ---------------------------------------------------------------------------
# 7. Context reset / quiesce contract
# ---------------------------------------------------------------------------


async def test_quiesce_for_context_switch_resets_workspace() -> None:
    b = _make(kind="deployments", scope="team")
    await b.ctl.split_pane()
    from korvid.ui.navigation import DrillLevel

    b.state.focused.drill.push(
        DrillLevel(
            parent_kind="deployments",
            parent_name="web",
            parent_namespace="team",
            parent_uid="u",
            child_kind="replicasets",
        )
    )
    b.ctl.set_filter("api")
    await b.ctl.quiesce_for_context_switch()
    assert not b.state.is_split  # split collapsed
    assert b.state.focused.drill.peek() is None  # drill cleared
    assert b.state.filter_pattern == ""  # filter reset
    assert RELATIONSHIP_GROUP in b.ui.cancelled  # relationship workers cancelled


async def test_reset_view_after_switch_returns_to_pods() -> None:
    cfg = KorvidConfig(namespace="prod")
    b = _make(kind="deployments", scope="team", config=cfg)
    b.ctl.reset_view_after_switch()
    assert b.state.current_kind == "pods"
    assert b.state.current_scope == "prod"


# ---------------------------------------------------------------------------
# Render coalescing
# ---------------------------------------------------------------------------


async def test_mark_render_pending_coalesces() -> None:
    b = _make(kind="pods", scope="default")
    assert b.ctl.mark_render_pending("pods") is True
    assert b.ctl.mark_render_pending("pods") is False  # already queued
    b.ctl.on_resources_updated("pods")
    assert b.ctl.mark_render_pending("pods") is True  # consumed, can queue again


async def test_metrics_poller_targets_pods_scope() -> None:
    b = _make(kind="pods", scope="default")
    await b.ctl.sync_metrics_poller()
    assert b.metrics.started == [None] or b.metrics.started == ["default"]


# ---------------------------------------------------------------------------
# Cluster-wide list permission gate (issue #108)
# ---------------------------------------------------------------------------


async def test_cluster_list_permitted_without_a_checker_allows() -> None:
    b = _make(kind="pods", scope="default", check_permission=None)
    assert await b.ctl.cluster_list_permitted() is True


async def test_cluster_list_permitted_probes_the_current_kind() -> None:
    calls: list[tuple[str, ...]] = []

    async def _check(
        verb: str, plural: str, ns: str, name: str | None, group: str, sub: str
    ) -> bool:
        calls.append((verb, plural, group))
        return True

    b = _make(kind="deployments", scope="default", check_permission=_check)
    assert await b.ctl.cluster_list_permitted() is True
    assert calls == [("list", "deployments", "apps")]


async def test_cluster_list_permitted_probes_the_backing_kind_of_a_synthetic_view() -> None:
    calls: list[tuple[str, ...]] = []

    async def _check(
        verb: str, plural: str, ns: str, name: str | None, group: str, sub: str
    ) -> bool:
        calls.append((verb, plural, group))
        return True

    b = _make(kind="pods", scope="default", check_permission=_check)
    b.view.aliases_map["helmreleases"] = dataclasses.replace(
        _SYNTHETIC_META, backing=("secrets", "")
    )
    b.state.focused.kind = "helmreleases"
    assert await b.ctl.cluster_list_permitted() is True
    assert calls == [("list", "secrets", "")]


async def test_a_synthetic_view_without_a_backing_kind_is_never_probed() -> None:
    async def _check(*_args: Any) -> bool:  # pragma: no cover - must not run
        raise AssertionError("nothing to probe")

    b = _make(kind="pods", scope="default", check_permission=_check)
    b.view.aliases_map["helmrevisions"] = _SYNTHETIC_META
    b.state.focused.kind = "helmrevisions"
    assert await b.ctl.cluster_list_permitted() is True


async def test_an_unknown_kind_is_allowed_so_the_watch_reports_its_own_error() -> None:
    async def _check(*_args: Any) -> bool:  # pragma: no cover - must not run
        raise AssertionError("unknown kind must not be probed")

    b = _make(kind="widgets", scope="default", check_permission=_check)
    assert await b.ctl.cluster_list_permitted() is True


async def test_a_forbidden_cluster_list_notifies_and_stays_put() -> None:
    async def _check(*_args: Any) -> bool:
        return False

    b = _make(kind="pods", scope="default", check_permission=_check)
    assert await b.ctl.cluster_list_permitted() is False
    assert len(b.ui.notes) == 1
    assert b.ui.notes[-1].severity == "warning"
    assert "forbidden" in b.ui.notes[-1].message


async def test_a_failing_permission_check_fails_open() -> None:
    async def _check(*_args: Any) -> bool:
        raise RuntimeError("SSAR unavailable")

    b = _make(kind="pods", scope="default", check_permission=_check)
    assert await b.ctl.cluster_list_permitted() is True
    assert b.ui.notes == []


# ---------------------------------------------------------------------------
# Namespace picker + completion prefetch (issue #108)
# ---------------------------------------------------------------------------


async def test_the_namespace_picker_opens_with_the_listed_namespaces() -> None:
    async def _list() -> list[str]:
        return ["default", "kube-system"]

    b = _make(list_namespaces=_list)
    await b.ctl.show_namespace_picker()
    assert b.surface.namespace_words == [["default", "kube-system"]]
    assert b.surface.opened_pickers == [["default", "kube-system"]]


async def test_the_namespace_picker_reports_when_listing_is_unavailable() -> None:
    b = _make(list_namespaces=None)
    await b.ctl.show_namespace_picker()
    assert b.surface.opened_pickers == []
    assert b.ui.notes[-1].message == "Namespace listing unavailable"


async def test_the_namespace_picker_refuses_to_list_during_a_switch() -> None:
    async def _list() -> list[str]:  # pragma: no cover - must not run
        raise AssertionError("listing raced the client swap")

    b = _make(list_namespaces=_list)
    b.context.reads = False
    await b.ctl.show_namespace_picker()
    assert b.surface.opened_pickers == []


async def test_a_forbidden_namespace_list_explains_the_direct_switch() -> None:
    async def _list() -> list[str]:
        raise ApiStatusError(403, "Forbidden")

    b = _make(list_namespaces=_list)
    await b.ctl.show_namespace_picker()
    assert b.surface.opened_pickers == []
    assert "`:ns <name>`" in b.ui.notes[-1].message
    assert b.ui.notes[-1].severity == "error"


async def test_a_stale_api_error_from_the_old_cluster_is_not_surfaced() -> None:
    holder: dict[str, _Bundle] = {}

    async def _list() -> list[str]:
        holder["b"].context._epoch += 1
        raise ApiStatusError(403, "Forbidden")

    b = _make(list_namespaces=_list)
    holder["b"] = b
    await b.ctl.show_namespace_picker()
    assert b.ui.notes == []


async def test_any_other_listing_failure_is_surfaced() -> None:
    async def _list() -> list[str]:
        raise RuntimeError("boom")

    b = _make(list_namespaces=_list)
    await b.ctl.show_namespace_picker()
    assert b.ui.notes[-1].severity == "error"
    assert "boom" in b.ui.notes[-1].message


async def test_a_listing_that_awaited_through_a_switch_cancels_the_picker() -> None:
    holder: dict[str, _Bundle] = {}

    async def _list() -> list[str]:
        holder["b"].context._epoch += 1
        return ["default"]

    b = _make(list_namespaces=_list)
    holder["b"] = b
    await b.ctl.show_namespace_picker()
    assert b.surface.opened_pickers == []
    assert "kube context changed" in b.ui.notes[-1].message


async def test_an_empty_namespace_list_warns_about_rbac() -> None:
    async def _list() -> list[str]:
        return []

    b = _make(list_namespaces=_list)
    await b.ctl.show_namespace_picker()
    assert b.surface.opened_pickers == []
    assert "RBAC" in b.ui.notes[-1].message


async def test_the_prefetch_warms_the_completion_words() -> None:
    async def _list() -> list[str]:
        return ["alpha"]

    b = _make(list_namespaces=_list)
    b.ctl.start_namespace_prefetch()
    await asyncio.sleep(0)  # let the task run to completion
    await b.ctl.cancel_namespace_prefetch()
    assert b.surface.namespace_words == [["alpha"]]


async def test_the_prefetch_is_skipped_without_a_lister() -> None:
    b = _make(list_namespaces=None)
    b.ctl.start_namespace_prefetch()
    await b.ctl.cancel_namespace_prefetch()
    assert b.surface.namespace_words == []


async def test_a_cancelled_prefetch_never_publishes_old_cluster_words() -> None:
    started = asyncio.Event()

    async def _list() -> list[str]:
        started.set()
        await asyncio.sleep(60)
        return ["stale"]  # pragma: no cover - cancelled first

    b = _make(list_namespaces=_list)
    b.ctl.start_namespace_prefetch()
    await started.wait()
    await b.ctl.cancel_namespace_prefetch()
    assert b.surface.namespace_words == []


async def test_a_failing_prefetch_is_swallowed() -> None:
    async def _list() -> list[str]:
        raise RuntimeError("no cluster")

    b = _make(list_namespaces=_list)
    b.ctl.start_namespace_prefetch()
    await asyncio.sleep(0)  # let the task run to completion
    await b.ctl.cancel_namespace_prefetch()
    assert b.surface.namespace_words == []


# ---------------------------------------------------------------------------
# Selected timeline resource (issue #282)
# ---------------------------------------------------------------------------


async def test_the_selected_timeline_resource_describes_the_row_under_the_cursor() -> None:
    b = _make(kind="pods", scope="default")
    b.surface.row_key = "team/web-1"
    b.view.uid = "uid-1"
    ref = b.ctl.selected_timeline_resource()
    assert ref is not None
    assert (ref.kind_alias, ref.display_kind) == ("pods", "Pod")
    assert (ref.namespace, ref.name, ref.uid) == ("team", "web-1", "uid-1")


async def test_a_synthetic_view_has_no_timeline_resource() -> None:
    b = _make(kind="pods", scope="default")
    b.view.aliases_map["helmreleases"] = _SYNTHETIC_META
    b.state.focused.kind = "helmreleases"
    b.surface.row_key = "team/web-1"
    assert b.ctl.selected_timeline_resource() is None


async def test_an_unselected_table_has_no_timeline_resource_and_never_notifies() -> None:
    b = _make(kind="pods", scope="default")
    b.surface.row_key = None
    assert b.ctl.selected_timeline_resource() is None
    assert b.ui.notes == []


async def test_a_row_key_without_a_namespace_has_no_timeline_resource() -> None:
    b = _make(kind="pods", scope="default")
    b.surface.row_key = "web-1"
    assert b.ctl.selected_timeline_resource() is None
