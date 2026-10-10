"""UID-safe Pod actions launched from Deployment outcome evidence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from korvid.k8s.errors import ApiStatusError
from korvid.ui.hints import EventsFetcher
from korvid.ui.ui_surface import UiSurface
from korvid.ui.widgets.hint_detail import HintDetailScreen
from korvid.ui.workspace_controller import ContextGuard, WorkspaceController


class DeploymentOutcomePodActions:
    """Navigate to one exact Pod incarnation, then run the requested read."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        target_uid: Callable[[str, str | None, str], Awaitable[str | None]],
        context: ContextGuard,
        workspace: Callable[[], WorkspaceController],
        describe: Callable[[str, str, str], Awaitable[None]],
        logs: Callable[[str, str, str], Awaitable[None]],
        events: Callable[[], EventsFetcher | None],
    ) -> None:
        self._ui = ui
        self._target_uid = target_uid
        self._context = context
        self._workspace = workspace
        self._describe = describe
        self._logs = logs
        self._events = events

    async def __call__(
        self,
        verb: str,
        epoch: int,
        namespace: str,
        name: str,
        uid: str,
    ) -> None:
        if not await self._identity_intact(epoch, namespace, name, uid):
            return
        workspace = self._workspace()
        await workspace.jump_to_object("pods", namespace, name, epoch=epoch, expected_uid=uid)
        if verb == "goto":
            return
        async with workspace.nav_lock:
            if not await self._identity_intact(epoch, namespace, name, uid):
                return
            if verb == "describe":
                await self._describe(namespace, name, uid)
            elif verb == "logs":
                await self._logs(namespace, name, uid)
            elif verb == "events":
                await self._show_events(epoch, namespace, name, uid)

    async def _identity_intact(self, epoch: int, namespace: str, name: str, uid: str) -> bool:
        intact = not self._context.crossed(epoch)
        try:
            live_uid = await self._target_uid("pods", namespace, name) if intact else None
        except ApiStatusError as exc:
            if exc.status != 404:
                raise
            live_uid = None
        intact = intact and not self._context.crossed(epoch) and live_uid == uid
        if not intact:
            self._ui.notify(
                "Pod identity changed; refresh the Deployment outcome",
                severity="warning",
                markup=False,
            )
        return intact

    async def _show_events(self, epoch: int, namespace: str, name: str, uid: str) -> None:
        events = self._events()
        if events is None:
            self._ui.notify("Events unavailable in this session", severity="warning")
            return
        rows = await events.fetch(namespace, name, uid=uid)
        if self._context.crossed(epoch):
            return
        await self._ui.push_screen(HintDetailScreen(f"Events for pod/{namespace}/{name}", (), rows))
