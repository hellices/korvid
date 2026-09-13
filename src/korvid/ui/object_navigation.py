"""Workspace navigation defaults and optional exact-incarnation focus guards."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from korvid.core.store import ALL_NAMESPACES
from korvid.k8s.olm import PACKAGES_GROUP
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState

if TYPE_CHECKING:
    from korvid.ui.workspace_controller import ContextGuard, WorkspaceSurface
    from korvid.ui.workspace_state import WorkspaceState

NavigationOrigin = tuple[str, int]


def capture_navigation_origin(state: WorkspaceState) -> NavigationOrigin:
    """Capture a non-reused pane identifier and its requested-navigation counter."""
    pane = state.focused
    return pane.table_id, pane.nav_gen


def default_scope_for(view: ViewState, kind: str | None, namespace: str | None) -> str | None:
    """Default catalog navigation to all namespaces unless scope was explicit."""
    if namespace is not None or kind is None:
        return namespace
    meta = view.aliases().get(kind)
    if meta is not None and (meta.group, meta.plural) == (PACKAGES_GROUP, "packagemanifests"):
        return ALL_NAMESPACES
    return None


def _row_uid(view: ViewState, kind: str, scope: str, namespace: str, name: str) -> str | None:
    return next(
        (
            str(getattr(row, "uid", "") or "")
            for row in view.resources(kind, scope)
            if getattr(row, "namespace", "") == namespace and row.name == name
        ),
        None,
    )


async def jump_to_object(
    kind: str,
    namespace: str,
    name: str,
    *,
    epoch: int | None,
    expected_uid: str | None,
    origin: NavigationOrigin | None,
    ui: UiSurface,
    view: ViewState,
    context: ContextGuard,
    state: WorkspaceState,
    surface: WorkspaceSurface,
    navigate: Callable[[str, str | None, Callable[[], bool]], Awaitable[None]],
    poll_attempts: int,
) -> None:
    """Navigate through the workspace, then focus only a still-matching target."""
    if (epoch is not None and context.crossed(epoch)) or (
        origin is not None and origin != capture_navigation_origin(state)
    ):
        return
    meta = view.aliases().get(kind)
    if meta is None:
        ui.notify(f"{kind} is not a discovered view", severity="warning", markup=False)
        return
    pane = state.focused
    table_id, generation = origin if origin is not None else capture_navigation_origin(state)
    navigation_started = False

    def navigation_current() -> bool:
        nonlocal navigation_started
        navigation_started = (
            state.focused is pane
            and pane.table_id == table_id
            and pane.nav_gen == generation
            and (epoch is None or not context.crossed(epoch))
        )
        return navigation_started

    await navigate(kind, namespace if meta.namespaced and namespace else None, navigation_current)
    if not navigation_started:
        return
    generation += 1
    for _attempt in range(poll_attempts):
        if (
            not navigation_current()
            or state.current_kind != kind
            or (meta.namespaced and namespace and state.current_scope != namespace)
        ):
            return
        uid = _row_uid(view, kind, state.current_scope, namespace, name)
        if expected_uid is not None and uid is not None and uid != expected_uid:
            ui.notify(
                "Resource identity changed; refresh the observation",
                severity="warning",
                markup=False,
            )
            return
        if (expected_uid is None or uid == expected_uid) and surface.focus_row(
            f"{namespace}/{name}"
        ):
            return
        await asyncio.sleep(0.05)
    if navigation_current():
        ui.notify(
            f"{name} is not visible in {kind} - it may be gone or outside the current scope",
            severity="warning",
            markup=False,
        )
