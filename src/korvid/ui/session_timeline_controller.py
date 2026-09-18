"""Session timeline producers and modal lifecycle (issue #282).

`SessionTimelineController` owns:

- attaching the watch-delta sink to `WatchManager` on startup,
- driving the bounded Warning-event feed (reconnect loop with backoff and a
  failure cap),
- recording context-switch and write entries from call sites that know the
  relevant metadata already, and
- opening `SessionTimelineScreen` and translating its goto result into a
  navigation worker, with an epoch guard that discards stale results.

It calls only `UiSurface` and `ViewState`, and constructs `SessionTimelineScreen`
to hand to `push_screen` — no direct Textual API use; the screen is only ever
constructed and pushed, never driven (no widget queries, no reading its
Textual state) — so it is fully testable without a running app.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, Literal, Protocol

from korvid.core.errors import explain_api_error
from korvid.core.pulse import PulseCoverageState
from korvid.core.session_timeline import AppendResult, SessionTimeline, TimelineResourceRef
from korvid.core.store import Summary
from korvid.k8s.errors import ApiStatusError
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.session_timeline_screen import SessionTimelineScreen, TimelineGotoResult

logger = logging.getLogger(__name__)

#: Worker group for the Warning-event feed (a `:ctx` switch cancels it so a
#: stale connection's events never land under the new epoch).
TIMELINE_EVENT_GROUP = "timeline-warning-events"

#: Worker group for the timeline goto navigation (mirrors the relationship
#: controller's exclusive/exit_on_error=False shape).
TIMELINE_NAVIGATION_GROUP = "timeline"

#: The one wording for "this session was composed without a timeline",
#: shared by the notification `open()` emits for a real `T` press and the
#: silent reason the palette probe returns, so the two cannot drift
#: (issue #388 round 13).
TIMELINE_UNAVAILABLE = UnavailableReason(
    AvailabilityCode.MISSING_CAPABILITY, "Timeline unavailable in this session"
)

#: API status codes that answer the Warning feed permanently.
_DENIED = frozenset({401, 403, 405})

#: Ceiling on the reconnect backoff so a down cluster is polled at a fixed
#: low rate rather than exponentially rarely.
_MAX_BACKOFF = 30.0

#: The only watch verbs that carry object state; BOOKMARK and ERROR are
#: protocol bookkeeping and are silently dropped.
_WATCH_VERBS: dict[str, Literal["ADDED", "MODIFIED", "DELETED"]] = {
    "ADDED": "ADDED",
    "MODIFIED": "MODIFIED",
    "DELETED": "DELETED",
}


class _WatchSink(Protocol):
    """Minimum interface the controller needs from `WatchManager`.

    Structural protocol so tests can inject a lightweight fake.
    """

    on_event: Callable[[str, str, str, Summary], None] | None


async def _aclose_quietly(stream: AsyncIterator[Any] | None) -> None:
    """Best-effort aclose — suppresses transport errors, lets Cancel propagate."""
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    with contextlib.suppress(Exception):
        await aclose()


class SessionTimelineController:
    """Produces timeline entries and owns the modal open/navigate lifecycle."""

    #: Base reconnect delay for the Warning feed; doubled per failure up to `_MAX_BACKOFF`.
    TIMELINE_EVENT_RETRY_SECONDS: float = 1.0
    #: Consecutive stream failures after which the feed gives up visibly.
    TIMELINE_EVENT_MAX_FAILURES: int = 5

    def __init__(
        self,
        *,
        ui: UiSurface,
        view: ViewState,
        watch_manager: _WatchSink,
        timeline: SessionTimeline | None,
        get_epoch: Callable[[], int],
        epoch_crossed: Callable[[int], bool],
        watch_warning_events: (Callable[[str | None], AsyncIterator[dict[str, Any]]] | None) = None,
        selected_resource: Callable[[], TimelineResourceRef | None] | None = None,
        navigate: Callable[[str, str, str, int], Coroutine[Any, Any, None]],
        warning_observer: Callable[[dict[str, Any], int], None] | None = None,
        warning_coverage: Callable[[int, PulseCoverageState, str], None] | None = None,
    ) -> None:
        self._ui = ui
        self._view = view
        self._watch_manager = watch_manager
        self._timeline = timeline
        self._get_epoch = get_epoch
        self._epoch_crossed = epoch_crossed
        self._watch_warning_events = watch_warning_events
        self._selected_resource = selected_resource
        self._navigate = navigate
        self._warning_observer = warning_observer
        self._warning_coverage = warning_coverage

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Attach producers, or leave the feature inert when timeline is None.

        Without a timeline nothing is wired: the watch sink stays None so
        the manager skips it per event, and no Warning-feed worker starts.
        """
        if self._timeline is None:
            self._report_warning_coverage(
                self._get_epoch(), "unavailable", "No existing timeline Warning feed"
            )
            return
        self._watch_manager.on_event = self.record_watch_event
        self.start_warning_watch()

    async def stop(self) -> None:
        """Cancel the Warning-event feed and wait for it to settle."""
        # Only the Warning-event group is cancelled here: it holds a watch
        # connection scoped to the old cluster, which must not outlive the
        # switch. The navigation group (TIMELINE_NAVIGATION_GROUP) is left
        # alone because `_on_timeline_result`'s navigate callback
        # (`_jump_to_object`) re-checks its own epoch before touching
        # anything, so an in-flight goto is already safe to let finish or
        # self-abort rather than being torn down here too.
        await self._ui.cancel_workers(TIMELINE_EVENT_GROUP)

    def start_warning_watch(self) -> None:
        """Start the epoch-bound Warning-event feed worker, if available.

        `exit_on_error=False`: the timeline is a side channel, so an
        unexpected failure becomes a notification instead of tearing the
        TUI down over a record-keeping stream.
        """
        if self._timeline is None or self._watch_warning_events is None:
            self._report_warning_coverage(
                self._get_epoch(), "unavailable", "Warning feed unavailable"
            )
            return
        self._ui.run_worker(
            self._run_warning_watch(),
            exclusive=False,
            group=TIMELINE_EVENT_GROUP,
            exit_on_error=False,
        )

    # ------------------------------------------------------------------
    # Producers
    # ------------------------------------------------------------------

    def record_watch_event(self, kind: str, scope: str, event_type: str, obj: Summary) -> None:
        """Record one watch delta the store has already applied.

        Runs inside the watch task (the manager guards the call), so it does
        no I/O and never raises: a timeline problem must not break a watch.
        """
        timeline = self._timeline
        if timeline is None:
            return
        verb = _WATCH_VERBS.get(event_type)
        if verb is None:
            return
        aliases = self._view.aliases()
        meta = aliases.get(kind)
        display_kind = meta.kind if meta is not None else str(getattr(obj, "kind", "") or kind)
        self._append_timeline(
            "watch delta",
            lambda: timeline.append_watch(
                epoch=self._get_epoch(),
                kind_alias=self._view.canonical_kind(kind),
                display_kind=display_kind,
                namespace=str(getattr(obj, "namespace", "") or ""),
                name=str(getattr(obj, "name", "") or ""),
                uid=str(getattr(obj, "uid", "") or "") or None,
                verb=verb,
            ),
        )

    def record_context_switch(
        self,
        *,
        epoch: int,
        phase: Literal["started", "completed", "failed"],
        from_context: str | None,
        to_context: str | None,
        note: str = "",
    ) -> None:
        """Record one context-switch phase against the epoch that owns it."""
        timeline = self._timeline
        if timeline is None:
            return
        self._append_timeline(
            "context switch",
            lambda: timeline.append_context_switch(
                epoch=epoch,
                phase=phase,
                from_context=from_context,
                to_context=to_context,
                note=note,
            ),
        )

    def record_write(
        self,
        *,
        epoch: int,
        action: str,
        kind_alias: str,
        display_kind: str,
        namespace: str | None,
        name: str,
        outcome: str,
    ) -> None:
        """Record an approved write using already-resolved metadata."""
        timeline = self._timeline
        if timeline is None:
            return
        self._append_timeline(
            "write entry",
            lambda: timeline.append_write(
                epoch=epoch,
                action=action,
                kind_alias=kind_alias,
                display_kind=display_kind,
                namespace=namespace,
                name=name,
                uid=None,
                outcome=outcome,
            ),
        )

    # ------------------------------------------------------------------
    # Modal lifecycle
    # ------------------------------------------------------------------

    def unavailable_reason(self) -> UnavailableReason | None:
        """Why `T` can't open the timeline right now, or None - a
        side-effect-free probe for the palette (issue #388 round 13).

        Synchronous and silent, and it asks the one question `open()`
        asks: was a `SessionTimeline` composed for this session? Nothing
        else gates the modal - `snapshot` is an in-memory read, so an
        empty feed and an unselected table both still open - and the
        notification stays the keypress's to emit, from this same
        sentence.
        """
        return None if self._timeline is not None else TIMELINE_UNAVAILABLE

    def open(self) -> None:
        """Open the session timeline modal, or warn when unavailable.

        Unlike the relationship graph, `SessionTimeline.snapshot` is an
        in-memory read, so the modal opens even with nothing selected.
        """
        timeline = self._timeline
        if timeline is None:
            self._ui.notify(
                TIMELINE_UNAVAILABLE.message,
                severity=TIMELINE_UNAVAILABLE.severity,
                markup=False,
            )
            return
        epoch = self._get_epoch()
        selected = self._selected_resource() if self._selected_resource is not None else None
        self._ui.push_screen(
            SessionTimelineScreen(
                timeline,
                current_epoch=epoch,
                resource_toggle=selected,
            ),
            functools.partial(self._on_timeline_result, epoch),
        )

    def _on_timeline_result(self, epoch: int, result: TimelineGotoResult | None) -> None:
        """Translate a dismissed goto result into a navigation worker.

        A `:ctx` switch that crossed *epoch* while the modal was open
        discards the navigation instead of jumping into a view built for
        the wrong cluster.
        """
        if result is None:
            return
        if self._epoch_crossed(epoch):
            self._ui.notify(
                "timeline navigation cancelled - the kube context changed"
                " while the timeline was open",
                severity="warning",
            )
            return
        _, kind_alias, namespace, name = result
        self._ui.run_worker(
            self._navigate(kind_alias, namespace, name, epoch),
            exclusive=True,
            group=TIMELINE_NAVIGATION_GROUP,
            exit_on_error=False,
        )

    # ------------------------------------------------------------------
    # Warning-event feed internals
    # ------------------------------------------------------------------

    async def _run_warning_watch(self) -> None:
        """Feed live Warning Events into the timeline for one context epoch."""
        watch = self._watch_warning_events
        timeline = self._timeline
        if watch is None or timeline is None:
            return
        await self._warning_loop(watch, timeline, self._get_epoch())

    async def _warning_loop(
        self,
        watch: Callable[[str | None], AsyncIterator[dict[str, Any]]],
        timeline: SessionTimeline,
        epoch: int,
    ) -> None:
        """Reconnect loop for the Warning feed, bound to *epoch*.

        A clean stream end resets the failure budget; cancellation propagates;
        a permanent denial (401/403/405) stops the loop visibly; anything else
        backs off with a capped exponential delay up to `TIMELINE_EVENT_MAX_FAILURES`.
        """
        failures = 0
        while epoch == self._get_epoch():
            stream: AsyncIterator[dict[str, Any]] | None = None
            self._report_warning_coverage(
                epoch, "partial", "Context-wide, best-effort Warning stream"
            )
            try:
                stream = watch(None)
                async for event in stream:
                    if epoch != self._get_epoch():
                        return
                    failures = 0
                    self._append_timeline(
                        "Warning event",
                        functools.partial(
                            timeline.append_warning_event,
                            epoch=epoch,
                            event=event,
                            kind_alias=self._event_kind_alias(event),
                        ),
                    )
                    self._share_warning(event, epoch)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._report_warning_coverage(
                    epoch,
                    "forbidden"
                    if isinstance(exc, ApiStatusError) and exc.status in _DENIED
                    else "failed",
                    "Warning stream unavailable; periodic snapshots remain independent",
                )
                if self._watch_denied(exc):
                    return
                failures += 1
            finally:
                await _aclose_quietly(stream)
            if failures >= self.TIMELINE_EVENT_MAX_FAILURES:
                self._ui.notify(
                    f"Warning-event timeline feed stopped after"
                    f" {self.TIMELINE_EVENT_MAX_FAILURES} failures",
                    severity="error",
                    markup=False,
                )
                return
            await asyncio.sleep(min(self.TIMELINE_EVENT_RETRY_SECONDS * 2**failures, _MAX_BACKOFF))

    def _report_warning_coverage(self, epoch: int, state: PulseCoverageState, detail: str) -> None:
        if self._warning_coverage is not None:
            try:
                self._warning_coverage(epoch, state, detail)
            except Exception:
                logger.warning("Warning observation coverage callback failed")

    def _share_warning(self, event: dict[str, Any], epoch: int) -> None:
        if self._warning_observer is not None:
            try:
                self._warning_observer(event, epoch)
            except Exception:
                self._report_warning_coverage(
                    epoch, "failed", "Warning observation callback failed"
                )
                return
        self._report_warning_coverage(epoch, "partial", "Context-wide, best-effort Warning stream")

    def _watch_denied(self, exc: Exception) -> bool:
        """True when *exc* is a permanent answer the feed must stop on."""
        if isinstance(exc, ApiStatusError) and exc.status in _DENIED:
            self._ui.notify(
                explain_api_error(exc.status, exc.reason, "events", None),
                severity="warning",
                markup=False,
            )
            return True
        logger.warning("Warning-event timeline feed failed", exc_info=exc)
        return False

    def _event_kind_alias(self, event: dict[str, Any]) -> str | None:
        """Resolve an Event's involvedObject to a discovered view alias.

        None when nothing discovered matches: the entry still records the
        Event's own kind/name, it just cannot be filtered by view.
        """
        involved = event.get("involvedObject")
        if not isinstance(involved, dict):
            return None
        api_version = str(involved.get("apiVersion") or "")
        kind = str(involved.get("kind") or "")
        group = api_version.rpartition("/")[0]
        for alias, meta in self._view.aliases().items():
            if self._view.canonical_kind(alias) != alias or meta.synthetic:
                continue
            if meta.kind == kind and meta.group == group:
                return alias
        return None

    # ------------------------------------------------------------------
    # Append helpers
    # ------------------------------------------------------------------

    def _append_timeline(self, label: str, append: Callable[[], AppendResult]) -> None:
        try:
            result = append()
        except Exception as exc:
            logger.warning("Timeline append failed for %s", label, exc_info=exc)
            self._ui.notify(
                f"Timeline skipped {label}: internal timeline error",
                severity="warning",
                markup=False,
            )
            return
        self._record_append_result(label, result)

    def _record_append_result(self, label: str, result: AppendResult | None) -> None:
        """Surface a refused append so the user knows data was not kept."""
        if result is None or result.accepted or result.diagnostic is None:
            return
        self._ui.notify(
            f"Timeline skipped {label}: {result.diagnostic}",
            severity="warning",
            markup=False,
        )
