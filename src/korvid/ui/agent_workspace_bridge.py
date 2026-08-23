"""Live workspace bridge: typed `AgentUiBridge` over the running TUI.

`AgentWorkspaceBridge` is the ui-layer implementation of `AgentUiBridge`.  It
translates each typed action in the `UiAction` union to the exact controller
method the equivalent human keystroke calls, so the agent path and the user
path share the same guards (approval dialogs, describe-screen priority, stale
context detection).

The tools-layer write surface (`AgentToolUIBridge(UIBridge)`) is deliberately
kept separate: it carries the write-approval authority and MCP ownership that
this typed port must never acquire.  Both surfaces are wired in the composition
root without merging.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from korvid.agent.interaction import (
    AgentUiBridge,
    DrillDown,
    FocusPane,
    InteractionContext,
    Navigate,
    OpenDescribe,
    OpenEvidence,
    OpenLogs,
    PaneContext,
    SelectResource,
    SetFilter,
    UiAction,
    UiActionResult,
)
from korvid.core.config import KorvidConfig
from korvid.ui.agent_ui_controller import AgentScreens, WorkspaceOps
from korvid.ui.workspace_controller import ContextGuard
from korvid.ui.workspace_state import WorkspaceState


class _ControllerOps(Protocol):
    """The subset of `AgentUiController` the bridge dispatches to.

    Structural — satisfied by `AgentUiController` without importing it, so the
    bridge module avoids a circular dependency on `agent_ui_controller`.
    """

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str: ...

    async def agent_set_filter(self, pattern: str) -> str: ...

    async def agent_open_logs(
        self, pod: str, namespace: str, container: str | None = None
    ) -> str: ...

    async def agent_open_describe(
        self, kind: str, name: str, namespace: str | None = None
    ) -> str: ...

    async def agent_drill_down(self, name: str) -> str: ...

    async def open_evidence(self, ref: str) -> str: ...


class AgentWorkspaceBridge(AgentUiBridge):
    """Live implementation of `AgentUiBridge` over a running workspace.

    Reads state through `WorkspaceState`, `ContextGuard`, and `AgentScreens`,
    then dispatches typed actions to the controller's existing guarded methods
    and `WorkspaceOps` transitions — the same paths a user keystroke follows.

    `AgentToolUIBridge` (the `UIBridge` write surface) is **not** composed
    here; they share no authority and must remain separate.
    """

    def __init__(
        self,
        *,
        config: Callable[[], KorvidConfig],
        context: ContextGuard,
        workspace: WorkspaceState,
        screens: AgentScreens,
        navigation: WorkspaceOps,
        controller: _ControllerOps,
        timeline_cursor: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._config = config
        self._context = context
        self._workspace = workspace
        self._screens = screens
        self._navigation = navigation
        self._controller = controller
        self._timeline_cursor = timeline_cursor

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def _build_pane_context(self, pane_index: int) -> PaneContext:
        """Build a `PaneContext` for the pane at *pane_index* (no focus change)."""
        pane = self._workspace.panes[pane_index]
        identity = self._screens.selected_identity(pane.table_id, pane.kind)
        return PaneContext(
            kind=pane.kind,
            scope=pane.scope,
            filter_pattern=pane.filter_pattern or None,
            selected=identity,
        )

    def snapshot(self) -> InteractionContext:
        """Return the current human-visible workspace state without side effects."""
        focused_idx = self._workspace.focused_index
        pane_count = self._workspace.pane_count

        focused_pane = self._build_pane_context(focused_idx)
        secondary_pane: PaneContext | None = None
        if pane_count > 1:
            other_idx = 1 - focused_idx
            secondary_pane = self._build_pane_context(other_idx)

        return InteractionContext(
            kube_context=self._config().kube_context,
            context_epoch=self._context.epoch(),
            focused_pane=focused_pane,
            secondary_pane=secondary_pane,
            timeline_cursor=self._timeline_cursor(),
        )

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    async def apply(self, action: UiAction) -> UiActionResult:
        """Apply one typed action; catches only expected domain errors."""
        try:
            message = await self._apply(action)
        except (KeyError, ValueError) as exc:
            return UiActionResult(False, f"ERROR: {exc}", self.snapshot())
        return UiActionResult(
            ok=not message.startswith("ERROR:"),
            message=message,
            context=self.snapshot(),
        )

    async def _apply(self, action: UiAction) -> str:
        """Dispatch *action* to the appropriate controller method."""
        if isinstance(action, Navigate):
            return await self._controller.agent_navigate(action.view, action.namespace)

        if isinstance(action, SetFilter):
            return await self._controller.agent_set_filter(action.filter_pattern or "")

        if isinstance(action, SelectResource):
            return self._apply_select(action)

        if isinstance(action, FocusPane):
            self._navigation.focus_pane(action.index)
            return f"focused pane {action.index}"

        if isinstance(action, OpenLogs):
            return await self._controller.agent_open_logs(
                action.pod, action.namespace, action.container
            )

        if isinstance(action, OpenDescribe):
            return await self._controller.agent_open_describe(
                action.kind, action.name, action.namespace
            )

        if isinstance(action, DrillDown):
            return await self._controller.agent_drill_down(action.name)

        if isinstance(action, OpenEvidence):
            return await self._controller.open_evidence(action.ref)

        # The UiAction TypeAlias is exhaustive; this branch guards new members.
        return f"ERROR: unhandled action type {type(action).__name__}"

    def _apply_select(self, action: SelectResource) -> str:
        """Validate and select *action*; raises ValueError on staleness."""
        row = self._navigation.focused_row_data(action.name, action.namespace)
        if row is None:
            raise KeyError(
                f"resource {action.name!r} not in current view (check kind, namespace, and filter)"
            )
        row_key, current_uid = row
        if action.uid is not None and current_uid != action.uid:
            raise ValueError("stale resource identity")
        if not self._navigation.select_row(row_key):
            raise ValueError("resource is hidden by the active filter")
        return f"selected {action.kind}/{action.name}"
