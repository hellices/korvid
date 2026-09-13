"""Own the bounded observation lifecycle without moving the user's workspace."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from korvid.core.pulse import (
    PulseCoverage,
    PulseCoverageState,
    PulseModel,
    PulseSnapshot,
    PulseTarget,
)
from korvid.core.pulse_collector import PulseCollector
from korvid.core.store import ALL_NAMESPACES
from korvid.k8s.errors import ApiStatusError, KubeClientError
from korvid.ui.object_navigation import NavigationOrigin
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.pulse import PulseGoto, PulseScreen
from korvid.ui.workspace_controller import ContextGuard

REFRESH_SECONDS = 15.0
RENDER_SECONDS = 0.25
REFRESH_GROUP = "pulse-refresh"
TIMER_GROUP = "pulse-render"
NAVIGATION_GROUP = "pulse-navigation"


def _now() -> datetime:
    return datetime.now(UTC)


class PulseController:
    """Coalesce background reads and presentation; only user actions navigate."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        view: ViewState,
        context: ContextGuard,
        model: PulseModel,
        collector: PulseCollector | None,
        present: Callable[[PulseSnapshot], None],
        capture_navigation_origin: Callable[[], NavigationOrigin],
        navigate: Callable[[str, str, str, int, str, NavigationOrigin], Awaitable[None]],
        get_manifest: Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None = None,
        clock: Callable[[], datetime] = _now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ui = ui
        self._view = view
        self._context = context
        self._model = model
        self._collector = collector
        self._present = present
        self._capture_navigation_origin = capture_navigation_origin
        self._navigate = navigate
        self._get_manifest = get_manifest
        self._clock = clock
        self._monotonic = monotonic
        self._started = False
        self._suspended = False
        self._refreshing = False
        self._pending = False
        self._identity: tuple[int, str | None] | None = None
        self._generation = 0
        self._navigation_identity: tuple[int, str | None, str] | None = None
        self._navigation_generation = 0
        self._next_refresh = 0.0
        self._last_render = float("-inf")
        self._last_snapshot: PulseSnapshot | None = None
        self._screen: PulseScreen | None = None
        self._watch_coverage: tuple[int, PulseCoverage] | None = None

    def _scope(self) -> str | None:
        scope = self._view.current_scope()
        return None if scope == ALL_NAMESPACES else scope

    def snapshot(self) -> PulseSnapshot:
        """Return a fresh immutable view of retained, already-sanitized facts."""
        return self._model.snapshot(self._clock())

    def start(self) -> None:
        """Start after mount; neither initial reads nor the timer block startup."""
        if self._started:
            return
        self._started = True
        self.resume()
        self.sync_scope()
        self.tick()
        self._ui.run_worker(self._timer(), group=TIMER_GROUP, exit_on_error=False)

    async def _timer(self) -> None:
        while self._started:
            await asyncio.sleep(RENDER_SECONDS)
            self.tick()

    def tick(self) -> None:
        """Reconcile scope/cadence and render at most four times per second."""
        if not self._started or self._suspended or self._context.switching():
            return
        self.sync_scope()
        now = self._monotonic()
        if now >= self._next_refresh:
            self.request_refresh()
        if now - self._last_render < RENDER_SECONDS:
            return
        self._last_render = now
        snapshot = self.snapshot()
        if snapshot != self._last_snapshot:
            self._last_snapshot = snapshot
            self._present(snapshot)
            if self._screen is not None:
                self._screen.note_update(snapshot)

    def sync_scope(self) -> None:
        """Invalidate the old frame immediately; never publish it in a new scope."""
        if not self._started or self._suspended or self._context.switching():
            return
        identity = self._context.epoch(), self._scope()
        navigation_identity = (*identity, self._view.current_kind())
        if navigation_identity != self._navigation_identity:
            self._navigation_identity = navigation_identity
            self._navigation_generation += 1
        if identity == self._identity:
            return
        self._identity = identity
        self._generation += 1
        self._model.reset(*identity)
        if self._watch_coverage is not None and self._watch_coverage[0] == identity[0]:
            self._model.set_coverage(self._watch_coverage[1])
        self.request_refresh()

    def request_refresh(self) -> None:
        """Queue one refresh, coalescing repeated triggers behind the active read."""
        if not self._started or self._suspended or self._context.switching():
            return
        self._next_refresh = self._monotonic() + REFRESH_SECONDS
        if self._collector is None:
            for coverage in self.snapshot().coverage:
                self._model.set_coverage(
                    PulseCoverage(
                        coverage.source, "unavailable", None, "Snapshot reader unavailable"
                    )
                )
            return
        self._pending = True
        if self._refreshing:
            return
        self._refreshing = True
        self._ui.run_worker(self._refresh(), group=REFRESH_GROUP, exit_on_error=False)

    async def _refresh(self) -> None:
        try:
            while self._pending and self._started and not self._suspended:
                self._pending = False
                identity, generation = self._identity, self._generation
                if identity is None or self._collector is None:
                    return
                await self._collect_frame(identity, generation)
        finally:
            self._refreshing = False

    def _frame_current(self, identity: tuple[int, str | None], generation: int) -> bool:
        return (
            self._started
            and not self._suspended
            and not self._context.switching()
            and generation == self._generation
            and identity == self._identity
            and identity == (self._context.epoch(), self._scope())
        )

    async def _collect_frame(self, identity: tuple[int, str | None], generation: int) -> None:
        if self._collector is None or not self._frame_current(identity, generation):
            return
        try:
            results = await self._collector.collect(identity[1])
            if not self._frame_current(identity, generation):
                return
            for result in results:
                self._model.replace_source(result.source, result.objects, result.coverage)
            if any(coverage.source == "collector" for coverage in self.snapshot().coverage):
                self._model.set_coverage(
                    PulseCoverage("collector", "complete", self._clock(), "Refresh recovered")
                )
        except Exception:
            if self._frame_current(identity, generation):
                self._model.set_coverage(
                    PulseCoverage(
                        "collector",
                        "failed",
                        self._clock(),
                        "Pulse refresh failed; retry with :pulse then r",
                    )
                )

    async def suspend(self) -> None:
        """Quiesce reads before the shared Kubernetes connection is retargeted."""
        self._suspended = True
        self._pending = False
        self._generation += 1
        self._navigation_generation += 1
        await self._ui.cancel_workers(REFRESH_GROUP)
        await self._ui.cancel_workers(NAVIGATION_GROUP)
        self._refreshing = False

    def resume(self) -> None:
        """Allow the next tick to bind to the context that actually took effect."""
        self._suspended = False
        self._identity = None
        self._navigation_identity = None
        self._next_refresh = 0.0

    async def stop(self) -> None:
        """Stop every owned worker before the app closes its shared connection."""
        self._started = False
        await self.suspend()
        await self._ui.cancel_workers(TIMER_GROUP)

    def record_warning(self, event: dict[str, Any], epoch: int) -> None:
        """Accept a shared event with no I/O, immediate render, or popup."""
        if (
            self._started
            and not self._suspended
            and not self._context.switching()
            and epoch == self._context.epoch()
        ):
            self._model.record_warning(event, epoch, self._clock())

    def warning_status(self, epoch: int, state: PulseCoverageState, detail: str) -> None:
        """Expose the inherited watch's limitations separately from snapshot reads."""
        if epoch == self._context.epoch() and not self._suspended:
            coverage = PulseCoverage("warning-watch", state, self._clock(), detail)
            self._watch_coverage = epoch, coverage
            self._model.set_coverage(coverage)

    def _alias(self, target: PulseTarget) -> str | None:
        return next(
            (
                alias
                for alias, meta in self._view.aliases().items()
                if meta.kind == target.kind
                and meta.group == target.group
                and not meta.synthetic
                and self._view.canonical_kind(alias) == alias
            ),
            None,
        )

    def target_problem(self, target: PulseTarget) -> str | None:
        """Explain why an observed identity cannot enter an existing view."""
        if not target.uid:
            return "This observation has no target UID; navigation is disabled"
        alias = self._alias(target)
        if alias is None:
            return "This resource kind is not a discovered view; observation only"
        if self._view.aliases()[alias].namespaced != bool(target.namespace):
            return "The observed namespace does not match the discovered resource scope"
        return None

    def open_detail(self) -> None:
        """Open only from the ordinary workspace, never over an approval dialog."""
        if self._ui.screen_depth() != 1 or self._context.switching():
            return
        self.sync_scope()
        self._screen = PulseScreen(
            self.snapshot(),
            current=self.snapshot,
            refresh=self.request_refresh,
            target_problem=self.target_problem,
        )
        self._ui.push_screen(self._screen, self._on_result)

    def _on_result(self, result: PulseGoto | None) -> None:
        self._screen = None
        if result is not None:
            self._ui.run_worker(
                self.navigate_to(result), group=NAVIGATION_GROUP, exit_on_error=False
            )

    def _selection_current(
        self, selection: PulseGoto, generation: int, origin: NavigationOrigin
    ) -> bool:
        return (
            not self._suspended
            and not self._context.switching()
            and generation == self._navigation_generation
            and origin == self._capture_navigation_origin()
            and selection.epoch == self._context.epoch()
            and selection.scope == self._scope()
            and self._ui.screen_depth() == 1
        )

    async def navigate_to(self, selection: PulseGoto) -> None:
        """Revalidate the live UID and observation frame before normal navigation."""
        generation = self._navigation_generation
        origin = self._capture_navigation_origin()
        problem = self.target_problem(selection.target)
        if problem or not self._selection_current(selection, generation, origin):
            self._ui.notify(
                problem or "Pulse context or scope changed; refresh the observation",
                severity="warning",
                markup=False,
            )
            return
        alias = self._alias(selection.target)
        if alias is None or self._get_manifest is None:
            self._ui.notify(
                "Cannot verify the observed resource identity", severity="warning", markup=False
            )
            return
        target = selection.target
        origin_kind = self._view.current_kind()
        try:
            manifest = await self._get_manifest(alias, target.namespace or None, target.name)
        except (ApiStatusError, KubeClientError, ValueError):
            if (
                self._selection_current(selection, generation, origin)
                and origin_kind == self._view.current_kind()
            ):
                self._ui.notify(
                    "Observed resource is missing or cannot be verified",
                    severity="warning",
                    markup=False,
                )
            return
        if (
            not self._selection_current(selection, generation, origin)
            or origin_kind != self._view.current_kind()
        ):
            return
        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("uid") != target.uid:
            self._ui.notify(
                "Resource identity changed; refresh Pulse before navigating",
                severity="warning",
                markup=False,
            )
            return
        if target.uid is not None:
            await self._navigate(
                alias, target.namespace, target.name, selection.epoch, target.uid, origin
            )
