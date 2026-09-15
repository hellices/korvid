"""Log subsystem ownership: streams, buffer, pane lifecycle (issue #187).

`LogController` owns the mutable state of the log subsystem — the live stream
tasks, the shared display buffer, the reconnect/error flags, the selected
stream triples, the log-pane generation counter, the pane display mode, and
which workspace pane owns the pane — together with the workflows that drive
them: opening/toggling streams (`l`/`L`), the live and previous-log stream
lifecycles with their reconnect policy, and the display actions (format, wrap,
timestamps, save, previous, search).

`KorvidApp` keeps only the Textual action/message entry points as one-line
delegates; the moved implementation lives here. The controller never imports
`app.py`: it reaches the UI through the narrow `UiSurface` (for notifications)
and a `LogPaneView` accessor for the concrete pane widget, which Textual keeps
mounted, and everything else — the selected pod, the visible rows, the
container list, the focused pane token, the context epoch and its guards, and
the footer refresh — arrives as constructor-injected callables read at call
time (late binding, so a `:ctx` retarget of `stream_logs` is observed).

Unlike the other controllers, the log streams are intentionally raw
`asyncio.Task`s rather than `UiSurface.run_worker` workers: the controller owns
their whole lifecycle (one task per panel, cancelled and reaped on reopen,
close, and shutdown), and the fan-out/reconnect bookkeeping is simpler when the
task set is managed directly. `KorvidApp.on_unmount` calls `shutdown()` so no
stream outlives the session.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Protocol

from textual.css.query import NoMatches

from korvid.core.errors import explain_api_error
from korvid.core.logbuffer import LogBuffer
from korvid.core.logexport import default_log_export_dir, export_log_lines
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.ui.action_availability import (
    CONTEXT_SWITCH_IN_PROGRESS,
    AvailabilityCode,
    UnavailableReason,
)
from korvid.ui.read_availability import PaneSearch
from korvid.ui.ui_surface import UiSurface
from korvid.ui.widgets.log_pane import MAX_PANELS

logger = logging.getLogger(__name__)

#: Live streams: cap on distinct pods accumulated side-by-side via `l`.
_MAX_LOG_PODS = 4

#: Multi-stream `L`: cap on pods whose containers are streamed at once.
_MAX_MULTI_STREAM_PODS = 8

#: Live-stream reconnect budget before the pane gives up visibly.
_MAX_RECONNECT_ATTEMPTS = 5

#: The (pod, container) source of one panel, and the (ns, pod, container)
#: triple that also carries the namespace for reopen/toggle bookkeeping.
Source = tuple[str, str]
Triple = tuple[str, str, str]

#: Actions that drive the *visible* log pane rather than the focused view.
#: `ActionPolicy` gates their bindings on the same fact; this is the
#: controller's own half of that agreement (issue #388 task 4).
_PANE_LOCAL_ACTIONS: frozenset[str] = frozenset(
    {"log_format", "log_wrap", "log_timestamps", "log_save", "log_previous"}
)

#: `Ctrl-S` on a visible pane whose buffer holds nothing. The one wording
#: for it: `action_log_save` notifies this message and the palette greys
#: the row with the same sentence (issue #388, round 6). No selection in
#: the sense the code names - the action has no subject to act on - and
#: the pane's visibility, which `_PANE_LOCAL_ACTIONS` already answers for,
#: is a different question with a different code.
_EMPTY_LOG_BUFFER = UnavailableReason(
    AvailabilityCode.NO_SELECTION, "Log buffer is empty — nothing to save"
)


class SelectedNsName(Protocol):
    """The selection read, with the silent `notify=False` probe path.

    `ViewState.selected_ns_name` satisfies it structurally; naming it here
    keeps the keyword checked at the injection site instead of erasing it
    behind `Callable[..., ...]`.
    """

    def __call__(self, *, notify: bool = True) -> tuple[str | None, str | None]: ...


#: The stream producer the app injects: `stream_logs(namespace, pod, container,
#: *, previous=..., follow=...)` yielding decoded lines. `None` disables logs.
StreamLogsFn = Callable[..., AsyncIterator[LogLine]]


class LogPaneView(Protocol):
    """The narrow slice of `LogPane` the controller drives.

    A structural port so the controller neither imports the widget module for
    typing nor needs a running app to be tested: a fake pane satisfies it. The
    real `LogPane` (kept mounted by Textual composition) is handed in through a
    `get_log_pane` accessor and matches structurally.
    """

    display: bool

    def open(
        self,
        sources: list[Source],
        *,
        force_prefix: bool = ...,
        log_buffer: LogBuffer | None = ...,
    ) -> None: ...

    def close(self) -> None: ...

    def feed(self, line: LogLine) -> None: ...

    def replay(self, lines: list[LogLine]) -> None: ...

    def set_state(self, state: str) -> None: ...

    def write_banner(self, text: str) -> None: ...

    def show_overflow_banner(self) -> None: ...

    def search_next(self) -> None: ...

    def search_prev(self) -> None: ...

    @property
    def has_search_hits(self) -> bool: ...

    def toggle_format(self) -> None: ...

    def toggle_wrap(self) -> None: ...

    def toggle_timestamps(self) -> None: ...


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


class LogController:
    """Owns the log subsystem's state and its open/stream/display workflows."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        get_log_pane: Callable[[], LogPaneView],
        get_stream_logs: Callable[[], StreamLogsFn | None],
        pod_containers: Callable[[str, str], tuple[str, ...]],
        selected_ns_name: SelectedNsName,
        visible_pod_keys: Callable[[], list[str]],
        current_kind: Callable[[], str],
        focused_pane: Callable[[], object],
        ctx_epoch: Callable[[], int],
        ctx_switch_crossed: Callable[[int], bool],
        ctx_reads_allowed: Callable[[], bool],
        #: The silent twin of `ctx_reads_allowed`, for the availability
        #: probe: whether a `:ctx` switch is in flight, without notifying.
        ctx_switching: Callable[[], bool],
        refresh_bindings: Callable[[], None],
        buffer_max_lines: int,
    ) -> None:
        self._ui = ui
        self._get_log_pane = get_log_pane
        self._get_stream_logs = get_stream_logs
        self._pod_containers = pod_containers
        self._selected_ns_name = selected_ns_name
        self._visible_pod_keys = visible_pod_keys
        self._current_kind = current_kind
        self._focused_pane = focused_pane
        self._ctx_epoch = ctx_epoch
        self._ctx_switch_crossed = ctx_switch_crossed
        self._ctx_reads_allowed = ctx_reads_allowed
        self._ctx_switching = ctx_switching
        self._refresh_bindings = refresh_bindings

        #: One task per streaming panel; owned and reaped by this controller.
        self._tasks: set[asyncio.Task[None]] = set()
        #: Shared display buffer for the open pane (None while closed).
        self._buffer: LogBuffer | None = None
        #: True once a stream has surfaced a terminal error to the user.
        self._error: bool = False
        #: (ns, pod, container) triples currently shown; drives toggle/reopen.
        self._current_triples: list[Triple] = []
        #: Monotonic pane generation: bumped on every open and close so a
        #: slow agent open can detect a user pane change and stand down.
        self._pane_gen: int = 0
        #: Whether the open pane forces per-panel titles (multi-stream `L`).
        self._force_prefix: bool = False
        #: Pane display mode: "" closed, "l" live, "L" multi, "p" previous.
        self._mode: str = ""
        #: Workspace pane whose selection opened the pane; only its navigation
        #: (or close) tears the stream down — the split workflow watches one
        #: pane while tailing logs from the other. An opaque identity token.
        self._owner: object | None = None
        #: Reconnect backoff for live streams; a public knob tests shrink.
        self.reconnect_sleep: float = 1.0
        #: Display-buffer capacity; a public knob tests shrink to force overflow.
        self.buffer_max_lines: int = buffer_max_lines

    # ------------------------------------------------------------------
    # Read-only inspection (used by the app and by unit tests)
    # ------------------------------------------------------------------

    @property
    def pane_gen(self) -> int:
        """Current pane generation; the agent open path guards on this."""
        return self._pane_gen

    @property
    def tasks(self) -> frozenset[asyncio.Task[None]]:
        """Snapshot of the live stream tasks."""
        return frozenset(self._tasks)

    @property
    def buffer(self) -> LogBuffer | None:
        """The shared display buffer, or None while the pane is closed."""
        return self._buffer

    @property
    def mode(self) -> str:
        """Pane display mode: "" closed, "l" live, "L" multi, "p" previous."""
        return self._mode

    @property
    def current_triples(self) -> list[Triple]:
        """(ns, pod, container) triples currently shown (a copy)."""
        return list(self._current_triples)

    @property
    def owner(self) -> object | None:
        """Workspace pane that opened the current log view."""
        return self._owner

    # ------------------------------------------------------------------
    # Open / toggle entry points (`l` and `L`)
    # ------------------------------------------------------------------

    def _pane_displayed(self) -> bool:
        """`get_log_pane().display`, guarded against the pre-compose window:
        the app's own accessor (`app._log_pane`) does a `query_one(LogPane)`
        that raises `NoMatches` before Textual has mounted the widget, and
        `unavailable_reason` is a probe that must never raise where a real
        keypress never could (issue #388 task 4 review) - `KorvidApp` guards
        the same fact the same way for `ActionPolicy.binding_enabled`
        (`_log_pane_open`); no pane yet reads as not displayed."""
        try:
            return self._get_log_pane().display
        except NoMatches:
            return False

    def unavailable_reason(self, action: str) -> UnavailableReason | None:
        """Why `action` can't run right now, or None - a side-effect-free
        probe for the palette (issue #388 task 4). Synchronous and silent.

        `logs` and `logs_multi` answer with the same guards their handlers
        apply, in the same order: a `:ctx` switch, the stream source, and
        then the selected pod (`l`) or the listed pods (`L`). `l` on an
        already-open pane in another mode *closes* it, so that case is
        invocable, exactly as the handler behaves. The pane-local display
        actions answer with the pane's visibility, matching
        `ActionPolicy`'s own binding gate on the same actions - and
        `log_save` answers with one fact more, because a visible pane with
        an empty (or not yet built) buffer is a `Ctrl-S` that can only say
        "nothing to save" (issue #388, round 6).

        The deeper refusals stay out on purpose: the cap notices
        (`Log pane caps at N pods`, `Streaming first N of M`) are emitted by
        helpers that notify while they compute, which a probe must never
        do."""
        if action in _PANE_LOCAL_ACTIONS:
            return self._pane_local_reason(action)
        if action not in ("logs", "logs_multi"):
            return None
        if self._ctx_switching():
            return CONTEXT_SWITCH_IN_PROGRESS
        if action == "logs" and self._pane_displayed() and self._mode != "l":
            # `l` closes a pane opened in multi/previous mode; nothing else
            # is read, so nothing else can refuse.
            return None
        if self._get_stream_logs() is None:
            return UnavailableReason(
                AvailabilityCode.MISSING_CAPABILITY, "Log streaming unavailable"
            )
        if action == "logs_multi":
            if not self._visible_pod_keys():
                return UnavailableReason(AvailabilityCode.NO_SELECTION, "No resource selected")
            return None
        ns, name = self._selected_ns_name(notify=False)
        if ns is None or name is None:
            return UnavailableReason(AvailabilityCode.NO_SELECTION, "No resource selected")
        return None

    def _pane_local_reason(self, action: str) -> UnavailableReason | None:
        """Why a pane-local display action can't run, or None.

        The pane has to be on screen - the fact `ActionPolicy` gates their
        bindings on - and `Ctrl-S` needs one more: something in the buffer
        to write out.
        """
        if not self._pane_displayed():
            return UnavailableReason(AvailabilityCode.PANE_CLOSED, "Open the log pane first")
        if action == "log_save" and not self._saveable_lines():
            return _EMPTY_LOG_BUFFER
        return None

    def _saveable_lines(self) -> bool:
        """Whether the buffer holds anything `action_log_save` would write.

        The same read that handler makes (`self._buffer`, then its lines),
        so the probe cannot report a save the keypress would refuse. A pane
        can be displayed before any buffer exists, which `Ctrl-S` ignores
        silently; for the palette that is the same empty answer.
        """
        buffer = self._buffer
        return buffer is not None and bool(buffer.lines())

    async def action_logs(self) -> None:
        """Open logs for the selected pod, or toggle it in/out of the pane (``l``).

        With the pane already open in live mode, ``l`` on another pod adds its
        containers side-by-side (max ``_MAX_LOG_PODS`` pods); ``l`` on a pod
        already shown removes it (closing the pane when it was the last one).
        Adding/removing reopens the streams, so panels restart at the last
        ~200 tailed lines.
        """
        if self._current_kind() != "pods":
            self._ui.notify("Logs are only available for pods", severity="warning")
            return
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch()

        log_pane = self._get_log_pane()
        if log_pane.display and self._mode != "l":
            # L (multi-stream) and p (previous) modes don't accumulate.
            await self.close()
            return

        if self._get_stream_logs() is None:
            self._ui.notify("Log streaming unavailable", severity="warning")
            return

        ns, name = self._selected_ns_name()
        if ns is None or name is None:
            return

        if log_pane.display:
            await self._toggle_log_pod(ns, name, epoch)
            return

        self._mode = "l"
        triples = self._pod_triples(ns, name)
        await self.open_pane(
            ns, [(pod, ctr) for _, pod, ctr in triples], triples=triples, epoch=epoch
        )

    def _pod_triples(self, namespace: str, name: str) -> list[Triple]:
        """Return (ns, pod, container) triples for one pod (one per container)."""
        containers = self._pod_containers(namespace, name)
        if containers:
            return [(namespace, name, ctr) for ctr in containers]
        return [(namespace, name, "")]

    async def _toggle_log_pod(self, namespace: str, name: str, epoch: int) -> None:
        """Add or remove *namespace/name* from the accumulated live-log panels."""
        existing = list(self._current_triples)
        pods: list[Source] = []
        for t_ns, t_pod, _ in existing:
            if (t_ns, t_pod) not in pods:
                pods.append((t_ns, t_pod))

        if (namespace, name) in pods:
            triples = [t for t in existing if (t[0], t[1]) != (namespace, name)]
            if not triples:
                await self.close()
                return
        else:
            if len(pods) >= _MAX_LOG_PODS:
                self._ui.notify(
                    f"Log pane caps at {_MAX_LOG_PODS} pods — Esc closes all",
                    severity="warning",
                )
                return
            triples = existing + self._pod_triples(namespace, name)
            if len(triples) > MAX_PANELS:
                self._ui.notify(
                    f"Panel cap is {MAX_PANELS} containers — cannot add {name}",
                    severity="warning",
                )
                return

        await self.cancel_tasks()
        sources = [(pod, ctr) for _, pod, ctr in triples]
        await self.open_pane(triples[0][0], sources, triples=triples, epoch=epoch)

    async def action_logs_multi(self) -> None:
        """Stream all filtered pods' containers (``L`` binding); cap at 8."""
        if self._current_kind() != "pods":
            self._ui.notify("Logs are only available for pods", severity="warning")
            return
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch()

        if self._get_stream_logs() is None:
            self._ui.notify("Log streaming unavailable", severity="warning")
            return

        pod_keys = self._visible_pod_keys()
        if not pod_keys:
            self._ui.notify("No resource selected", severity="warning")
            return

        triples = self._build_multi_stream_triples(pod_keys)
        if not triples:
            self._ui.notify("No pods to stream", severity="warning")
            return

        if self._get_log_pane().display:
            await self.close()

        self._mode = "L"
        ns0 = triples[0][0]
        await self.open_pane(
            ns0,
            [(pod, ctr) for _, pod, ctr in triples],
            triples=triples,
            force_prefix=True,
            epoch=epoch,
        )

    def _build_multi_stream_triples(self, pod_keys: list[str]) -> list[Triple]:
        """Collect (namespace, pod, container) triples for visible pods; cap at 8."""
        total = len(pod_keys)
        if total > _MAX_MULTI_STREAM_PODS:
            pod_keys = pod_keys[:_MAX_MULTI_STREAM_PODS]
            self._ui.notify(f"Streaming first {_MAX_MULTI_STREAM_PODS} of {total} matching pods")

        triples: list[Triple] = []
        for pod_key in pod_keys:
            parts = pod_key.split("/", 1)
            if len(parts) != 2:
                continue
            ns, name = parts[0], parts[1]
            containers = self._pod_containers(ns, name)
            if containers:
                for ctr in containers:
                    triples.append((ns, name, ctr))
            else:
                triples.append((ns, name, ""))
        return triples

    async def open_agent_logs(self, namespace: str, triples: list[Triple]) -> None:
        """Open live logs on the agent's behalf for already-resolved *triples*.

        The app's `agent_open_logs` owns the agent-priority checks (screen
        stack, approval dialog) and the stale-generation guards around the
        `pane_gen`/`cancel_tasks` seams; this only performs the destructive
        open once the app has cleared them.
        """
        self._mode = "l"
        await self.open_pane(namespace, [(pod, ctr) for _, pod, ctr in triples], triples=triples)

    # ------------------------------------------------------------------
    # Pane / stream lifecycle
    # ------------------------------------------------------------------

    async def open_pane(
        self,
        namespace: str,
        sources: list[Source],
        *,
        triples: list[Triple] | None = None,
        force_prefix: bool = False,
        previous: bool = False,
        epoch: int | None = None,
    ) -> None:
        """Show the log pane and spawn one streaming task per (pod, container).

        *epoch* is the `_ctx_epoch` captured when the user triggered the
        action; when a :ctx switch started or completed since (issue #84),
        the open is dropped — the streams would attach to the new cluster
        while labeled with the old selection. Callers without an awaited
        gap (epoch=None) are still refused while a switch is in flight.
        """
        if self._ctx_switch_crossed(self._ctx_epoch() if epoch is None else epoch):
            self._ui.notify(
                "Log streaming cancelled - the kube context changed",
                severity="warning",
            )
            return
        self._pane_gen += 1
        # Resolve triples before saving so current_triples is always complete.
        if triples is None:
            triples = [(namespace, pod, ctr) for pod, ctr in sources]

        # LogPane silently ignores sources beyond MAX_PANELS; enforce the same
        # cap here so no stream task is ever spawned without a panel to feed.
        if len(triples) > MAX_PANELS:
            self._ui.notify(
                f"Showing first {MAX_PANELS} of {len(triples)} containers",
                severity="warning",
            )
            triples = triples[:MAX_PANELS]
            sources = sources[:MAX_PANELS]

        self._current_triples = list(triples)
        self._force_prefix = force_prefix
        self._owner = self._focused_pane()

        log_pane = self._get_log_pane()
        self._buffer = LogBuffer(self.buffer_max_lines)
        log_pane.open(sources, force_prefix=force_prefix, log_buffer=self._buffer)
        # The pane controls (f/w/t/Ctrl-S/p) gate on pane visibility: tell
        # the footer legend the pane just appeared (issue #114).
        self._refresh_bindings()

        if previous:
            log_pane.write_banner("\u2500\u2500 previous container logs \u2500\u2500")

        log_pane.set_state("streaming")

        # Defensive: callers cancel+gather before re-opening, but never let a
        # stale task survive the set replacement below.
        for stale in self._tasks:
            stale.cancel()
        self._tasks = set()
        self._error = False

        for ns, pod, container in triples:
            task: asyncio.Task[None] = asyncio.create_task(
                self._spawn_log_stream(ns, pod, container, previous=previous)
            )
            self._tasks.add(task)

    async def _spawn_log_stream(
        self, namespace: str, pod: str, container: str, *, previous: bool = False
    ) -> None:
        """Delegate to the appropriate streaming coroutine based on follow flag."""
        stream_logs = self._get_stream_logs()
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
        stream_logs: StreamLogsFn,
    ) -> None:
        """Retry loop for live (follow=True) streams.

        Retries up to ``_MAX_RECONNECT_ATTEMPTS`` times on transient errors or
        unexpected EOF.  ApiStatusError and CancelledError are never retried.
        Each (re)connection replays the last ~tail_lines existing lines;
        ``_ReplayFilter`` drops the ones already displayed so reconnects
        don't duplicate output.
        """
        log_pane = self._get_log_pane()
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

    def _mark_stream_healthy(self, log_pane: LogPaneView, consecutive_failures: int) -> None:
        """Restore the streaming indicator after a successful reconnect."""
        if consecutive_failures > 0 and not self._error:
            log_pane.set_state("streaming")

    async def _pause_before_reconnect(
        self,
        log_pane: LogPaneView,
        current: asyncio.Task[None] | None,
        consecutive_failures: int,
    ) -> bool:
        """Sleep before the next attempt; False when retries are exhausted."""
        if consecutive_failures > _MAX_RECONNECT_ATTEMPTS or self._error:
            if not self._error:
                self._ui.notify(
                    f"log stream lost after {_MAX_RECONNECT_ATTEMPTS} reconnect attempts",
                    title="Log stream error",
                    severity="error",
                )
                self._error = True
                log_pane.set_state("error")
            self._discard_task(current)
            return False
        log_pane.set_state("reconnecting")
        await asyncio.sleep(self.reconnect_sleep)
        return True

    async def _previous_log_stream(
        self,
        namespace: str,
        pod: str,
        container: str,
        stream_logs: StreamLogsFn,
    ) -> None:
        """One-shot previous-container-log stream (follow=False, no reconnect)."""
        log_pane = self._get_log_pane()
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
            if log_pane.display and not self._error:
                self._error = True
                self._ui.notify(
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
        log_pane: LogPaneView,
        current: asyncio.Task[None] | None,
        namespace: str,
        exc: ApiStatusError,
    ) -> None:
        """Notify the user of an API error and put the stream into error state."""
        if not log_pane.display:
            self._discard_task(current)
            return
        msg = explain_api_error(exc.status, exc.reason, "pods", namespace)
        self._ui.notify(msg, title="Log stream error", severity="error")
        self._error = True
        log_pane.set_state("error")
        self._discard_task(current)

    def _all_streams_ended(self) -> bool:
        """True when every spawned stream task has finished without an error."""
        return not self._tasks and not self._error

    def _discard_task(self, current: asyncio.Task[None] | None) -> None:
        """Remove *current* from the live task set (no-op if None or absent)."""
        if current is not None:
            self._tasks.discard(current)

    def _buffer_line(self, log_pane: LogPaneView, line: LogLine) -> None:
        """Append *line* to the shared buffer; show overflow banner on first overflow."""
        if self._buffer is None:
            return
        was_full = self._buffer.overflowed
        self._buffer.append(line)
        if not was_full and self._buffer.overflowed:
            log_pane.show_overflow_banner()

    async def cancel_tasks(self) -> None:
        """Cancel and await stream tasks without hiding the pane (reopen path)."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._buffer = None
        self._error = False

    async def close(self) -> None:
        """Cancel all stream tasks and hide the log pane."""
        self._pane_gen += 1
        await self.cancel_tasks()
        self._current_triples = []
        self._force_prefix = False
        self._mode = ""
        self._owner = None
        with contextlib.suppress(Exception):
            self._get_log_pane().close()
        # The pane controls gate on pane visibility (issue #114).
        self._refresh_bindings()

    async def close_if_owned_by(self, pane: object) -> None:
        """Close the pane only when *pane* is the one that opened it.

        The split workflow watches one pane while tailing logs from the other,
        so only the owning pane's navigation (or its close) tears the stream
        down; another pane's `:view`/`:ns` leaves it streaming.
        """
        if self._owner is pane:
            await self.close()

    async def shutdown(self) -> None:
        """Cancel any active stream tasks at app teardown.

        Mirrors `cancel_tasks` minus the buffer reset — `on_unmount` only needs
        the tasks reaped before the event loop shuts down, and swallows any
        residual error the gather surfaces.
        """
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------
    # Display actions (f / w / t / Ctrl-S / p / n / N)
    # ------------------------------------------------------------------

    async def action_log_format(self) -> None:
        """Toggle JSON/raw formatting and re-render the buffer (``f`` key)."""
        self._toggle_display(lambda pane: pane.toggle_format())

    async def action_log_wrap(self) -> None:
        """Toggle line wrapping and re-render the buffer (``w`` key)."""
        self._toggle_display(lambda pane: pane.toggle_wrap())

    async def action_log_timestamps(self) -> None:
        """Toggle the timestamp prefix and re-render the buffer (``t`` key)."""
        self._toggle_display(lambda pane: pane.toggle_timestamps())

    def _toggle_display(self, toggle: Callable[[LogPaneView], None]) -> None:
        """Shared path for display toggles: flip the setting, replay the buffer.

        ``LogPane.replay`` restores contextual banners (previous-logs,
        overflow), so every toggle must funnel through here instead of
        clearing panels ad hoc.
        """
        log_pane = self._get_log_pane()
        if not log_pane.display:
            return
        toggle(log_pane)
        if self._buffer is not None:
            log_pane.replay(self._buffer.lines())

    def action_log_save(self) -> None:
        """Save the current log buffer to a generated file (``ctrl+s``)."""
        log_pane = self._get_log_pane()
        if not log_pane.display or self._buffer is None:
            return
        lines = self._buffer.lines()
        if not lines:
            # markup=False: the palette greys the `Ctrl-S` row with this
            # same reason and renders it literally (#388).
            self._ui.notify(
                _EMPTY_LOG_BUFFER.message, severity=_EMPTY_LOG_BUFFER.severity, markup=False
            )
            return
        try:
            path = export_log_lines(lines, default_log_export_dir())
        except OSError as exc:
            self._ui.notify(f"Failed to save logs: {exc}", severity="error")
            return
        self._ui.notify(f"Logs saved to {path}")

    async def action_log_previous(self) -> None:
        """Re-open the same streams in previous-container-log mode (``p`` key)."""
        log_pane = self._get_log_pane()
        if not log_pane.display:
            return
        if not self._current_triples:
            return
        if not self._ctx_reads_allowed():
            return
        epoch = self._ctx_epoch()
        triples = list(self._current_triples)
        force_prefix = self._force_prefix
        sources = [(pod, ctr) for _, pod, ctr in triples]
        # Cancel live tasks without hiding the pane.
        await self.cancel_tasks()
        self._mode = "p"
        # Re-open with previous=True (clears RichLog, writes banner, spawns tasks).
        ns0 = triples[0][0]
        await self.open_pane(
            ns0, sources, triples=triples, force_prefix=force_prefix, previous=True, epoch=epoch
        )

    def search_next(self) -> None:
        """Advance to the next search hit in an open log pane (``n`` key)."""
        log_pane = self._get_log_pane()
        if log_pane.display:
            log_pane.search_next()

    def search_state(self) -> PaneSearch:
        """Whether the log pane is on screen and has hits (issue #388).

        The two facts `search_next`/`search_prev` act on, answered without
        acting on them - the palette asks this so `n`/`N` are not offered
        when they would step nowhere. Guarded like `_pane_displayed`: a
        probe can be asked before the widget is mounted, where a real
        keypress could never arrive.
        """
        try:
            log_pane = self._get_log_pane()
        except NoMatches:
            return PaneSearch(displayed=False, hits=False)
        return PaneSearch(displayed=log_pane.display, hits=log_pane.has_search_hits)

    def search_prev(self) -> bool:
        """Previous search hit in an open log pane (``N`` key).

        Returns True when a visible pane handled the key; the app falls back to
        sorting by name only when it returns False.
        """
        log_pane = self._get_log_pane()
        if log_pane.display:
            log_pane.search_prev()
            return True
        return False
