"""Pods hint-strip lifecycle, extracted from the app (issue #97 U3b).

`HintController` owns the warning-event cache, the parked-cursor refresh
timer, and the background event fetch that decorates the status-derived
hint. It depends only on narrow callables injected at construction — widget
access (the strip adapters), worker scheduling (`start_fetch`), and timer
creation stay with the app, per the U3 constraints in issue #97.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol

from korvid.k8s.models import ContainerTrouble, PodSummary
from korvid.ui.widgets.hint_strip import parse_rfc3339


class EventsFetcher(ABC):
    """Events for one object — layer-boundary interface (AGENTS.md: `abc.ABC`).

    The concrete adapter wraps the k8s client and is wired in `__main__.py`.
    `uid` narrows the query so earlier same-named incarnations are excluded.
    """

    @abstractmethod
    async def fetch(
        self, namespace: str, name: str, *, uid: str | None = None
    ) -> list[dict[str, Any]]: ...


class TimerHandle(Protocol):
    """The slice of Textual's `Timer` the controller needs."""

    def stop(self) -> None: ...


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


def pod_needs_hint(summary: PodSummary) -> bool:
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


def event_line_fresh(event_ts: datetime | None, summary: PodSummary) -> bool:
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


def event_timestamp(event: dict[str, Any]) -> datetime | None:
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


def newest_warning(events: list[dict[str, Any]]) -> tuple[str, datetime | None] | None:
    """(line, timestamp) of the most recent Warning event, or None.

    Timestamps are parsed before comparing: RFC 3339 strings do not sort
    chronologically once fractional seconds or offsets differ.
    """
    warnings = [e for e in events if e.get("type") == "Warning"]
    if not warnings:
        return None
    epoch = datetime.min.replace(tzinfo=UTC)
    newest = max(warnings, key=lambda e: event_timestamp(e) or epoch)
    reason = str(newest.get("reason") or "Warning")
    message = str(newest.get("message") or "").strip()
    line = f"{reason}: {message}" if message else reason
    return line, event_timestamp(newest)


#: Seconds a fetched warning-event line stays cached per pod. Short enough
#: that a cursor parked on a crashing pod eventually sees fresh events.
DEFAULT_HINT_EVENT_TTL = 15.0


class HintController:
    """Hint-strip lifecycle for the pods view (issue #26/#34, extracted per #97).

    Owns: the per-pod warning-event cache (uid-keyed so a recreated pod never
    inherits its predecessor's line), the parked-cursor refresh timer, the
    background event fetch, and the freshness rules that suppress stale lines.

    Does not own: widget lookups (the strip arrives as `show_trouble` /
    `clear_hint` adapters), worker scheduling (`start_fetch` wraps the app's
    `run_worker`), or timer creation (`set_timer` is the app's).
    """

    def __init__(
        self,
        *,
        find_pod_summary: Callable[[str], PodSummary | None],
        cursor_row_key: Callable[[], str | None],
        on_pods_view: Callable[[], bool],
        get_events: Callable[[], EventsFetcher | None],
        show_trouble: Callable[..., None],
        clear_hint: Callable[[], None],
        start_fetch: Callable[[Coroutine[Any, Any, None]], object],
        set_timer: Callable[[float, Callable[[], None]], TimerHandle],
        ctx_epoch: Callable[[], int],
        ctx_crossed: Callable[[int], bool],
        ttl: float = DEFAULT_HINT_EVENT_TTL,
    ) -> None:
        self._find_pod_summary = find_pod_summary
        self._cursor_row_key = cursor_row_key
        self._on_pods_view = on_pods_view
        self._get_events = get_events
        self._show_trouble = show_trouble
        self._clear_hint = clear_hint
        self._start_fetch = start_fetch
        self._set_timer = set_timer
        self._ctx_epoch = ctx_epoch
        self._ctx_crossed = ctx_crossed
        self.ttl = ttl
        self.cache: dict[str, tuple[float, str | None, datetime | None]] = {}
        self.timer: TimerHandle | None = None

    def show_for_row(self, row_key: str) -> None:
        """Render the hint for one pod row: cached event line when fresh,
        otherwise the status-derived hint plus a background event fetch."""
        summary = self._find_pod_summary(row_key)
        if summary is None or not pod_needs_hint(summary):
            self._clear_hint()
            return
        # uid in the cache key: a recreated pod must not inherit the cached
        # event line of its previous incarnation.
        cache_key = f"{row_key}#{summary.uid}"
        cached = self.cache.get(cache_key)
        if cached is not None and (age := monotonic() - cached[0]) < self.ttl:
            _at, line, event_ts = cached
            if line is not None and not event_line_fresh(event_ts, summary):
                # A newer termination arrived since the line was cached.
                line = None
            if summary.trouble or line:
                self._show_trouble(summary.trouble, event=line)
            else:
                self._clear_hint()
            # Keep the parked-cursor refresh armed for the entry's remaining
            # life — switching rows and back must not strand it timerless.
            self.schedule_refresh(row_key, delay=self.ttl - age)
            return
        if summary.trouble:
            self._show_trouble(summary.trouble)
        else:
            # Event-only hint (e.g. Running but not ready): nothing to show
            # until the warning event arrives.
            self._clear_hint()
        if self._get_events() is not None:
            self._start_fetch(self.fetch_event(row_key, cache_key, summary))

    def refresh_for_focus(self) -> None:
        """Re-evaluate the hint strip for the focused pane's selection.

        Focus changes re-target command routing without moving any cursor,
        so the highlight-driven handler never fires — without this, a
        warning from the previously focused pane would linger over a pane
        showing deployments or a healthy pod.
        """
        row_key = self._cursor_row_key() if self._on_pods_view() else None
        if row_key is None:
            self._clear_hint()
            return
        self.show_for_row(row_key)

    def schedule_refresh(self, row_key: str, *, delay: float | None = None) -> None:
        """Re-evaluate a parked cursor when the cache entry expires; without
        this a cursor that never moves would show the same event forever."""
        if self.timer is not None:
            self.timer.stop()

        def _refresh() -> None:
            self.timer = None
            if self._on_pods_view() and self._cursor_row_key() == row_key:
                self.show_for_row(row_key)

        self.timer = self._set_timer(max(0.05, delay if delay is not None else self.ttl), _refresh)

    async def fetch_event(self, row_key: str, cache_key: str, summary: PodSummary) -> None:
        """Best-effort: append the newest warning event to the visible strip."""
        fetcher = self._get_events()
        if fetcher is None:  # caller guards; satisfy the type checker
            return
        epoch = self._ctx_epoch()
        try:
            events = await fetcher.fetch(summary.namespace, summary.name, uid=summary.uid or None)
        except Exception:  # events are decoration; the status-derived hint already shows
            if self._ctx_crossed(epoch):
                # The fetch failed because the context switch closed the old
                # client — recaching / rescheduling would resurrect
                # old-cluster hints after teardown cleared them.
                return
            self.store_event(cache_key, None, None)
            # Retry once the TTL passes: a transient API failure must not
            # hide the hint forever while the cursor stays parked.
            if self._on_pods_view() and self._cursor_row_key() == row_key:
                self.schedule_refresh(row_key)
            return
        if self._ctx_crossed(epoch):
            # A late success from the old cluster must not be cached against
            # (or rendered over) a same-keyed row on the new one.
            return
        self._apply_events(row_key, cache_key, summary, events)

    def _apply_events(
        self, row_key: str, cache_key: str, summary: PodSummary, events: list[dict[str, Any]]
    ) -> None:
        """Cache the fetched events and render them if the cursor still fits."""
        # The snapshot taken at highlight time may be stale after the await:
        # re-read the store and filter/render against the *current* status.
        fresh = self._find_pod_summary(row_key)
        if fresh is None or fresh.uid != summary.uid:
            # Deleted or recreated mid-fetch: the results describe the old
            # incarnation. Re-evaluate the row so the new one gets its own pass.
            if self._on_pods_view() and self._cursor_row_key() == row_key:
                self.show_for_row(row_key)
            return
        found = newest_warning(events)
        line, event_ts = found if found is not None else (None, None)
        if line is not None and not event_line_fresh(event_ts, fresh):
            line, event_ts = None, None
        self.store_event(cache_key, line, event_ts)
        if not self._on_pods_view() or self._cursor_row_key() != row_key:
            return
        self.schedule_refresh(row_key)
        if not pod_needs_hint(fresh):
            self._clear_hint()
            return
        if fresh.trouble or line:
            self._show_trouble(fresh.trouble, event=line)
        else:
            self._clear_hint()

    def store_event(self, cache_key: str, line: str | None, event_ts: datetime | None) -> None:
        """Cache the fetched line (with its occurrence time, so cache hits can
        re-apply freshness); expired entries are swept on every write so the
        cache cannot grow without bound in a long-running session."""
        now = monotonic()
        expired = [k for k, (at, _line, _ts) in self.cache.items() if now - at >= self.ttl]
        for k in expired:
            del self.cache[k]
        self.cache[cache_key] = (now, line, event_ts)

    def teardown(self) -> None:
        """Drop all hint state on a context switch: the cache and any armed
        refresh describe the old cluster and must not resurface on the new one.
        (The in-flight fetch worker is cancelled by the app, which owns it.)
        """
        if self.timer is not None:
            self.timer.stop()
            self.timer = None
        self.cache.clear()


__all__ = [
    "DEFAULT_HINT_EVENT_TTL",
    "ContainerTrouble",
    "EventsFetcher",
    "HintController",
    "TimerHandle",
    "event_line_fresh",
    "event_timestamp",
    "newest_warning",
    "pod_needs_hint",
]
