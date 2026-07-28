"""Session-scoped port-forward registry (issue #38).

Tracks `kubectl port-forward` subprocesses started from the UI. The registry
owns process lifecycle only — no Textual imports — so the `:pf` screen and
the app's teardown paths can share one source of truth.

Liveness is the design goal (issue #38): a hand-managed forward dies silently
when its target pod restarts. `refresh()` polls every child process and marks
exited ones ``broken`` instead of dropping them, so the UI can surface the
breakage and offer a one-key re-attach.

Startup is a handshake, not a guess: Popen returning only proves the child
exists — kubectl validates the target, RBAC, and the local bind after that.
A spawned forward is ``starting`` until kubectl prints its "Forwarding from"
line (watched by a daemon reader thread); `wait_ready()` lets the caller
block for that transition and report an early exit as a failed start.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import Any, Protocol

from korvid.k8s.portforward import build_port_forward_argv

#: How long a terminated kubectl gets to exit before it is killed.
_STOP_GRACE_SECONDS = 2.0

#: Poll step while stop_all() waits out the shared grace deadline.
_STOP_POLL_SECONDS = 0.05

#: kubectl's readiness line — printed once per listener when the bind worked.
_READY_PREFIX = "Forwarding from"


class _ForwardProcess(Protocol):
    """The slice of subprocess.Popen the registry needs (test seam)."""

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


# One generation's watcher inputs, captured by _prepare_handshake() while the
# record cannot change: (process, its stdout stream, its readiness event).
_WatcherBinding = tuple[_ForwardProcess, Iterable[str], threading.Event]


@dataclass(frozen=True)
class ForwardSpec:
    """What to forward: everything needed to (re)build the kubectl argv."""

    kind: str  # "pods" | "services" | a workload plural after a retarget
    namespace: str
    name: str
    local_port: int
    remote_port: int
    #: The pod's owning workload as ``"<kind-plural>/<name>"`` (captured at
    #: start). Lets a re-attach follow a replaced pod to its replacement.
    workload: str | None = None

    def retargeted(self) -> ForwardSpec | None:
        """The same forward aimed at the owning workload, or None without one.

        kubectl resolves a live pod itself for workload targets, so the
        revived forward reaches the vanished pod's replacement (issue #38).
        """
        if self.workload is None:
            return None
        kind, _, name = self.workload.partition("/")
        if not kind or not name:
            return None
        return replace(self, kind=kind, name=name, workload=None)


@dataclass
class ForwardRecord:
    """A tracked forward. ``status``: ``starting`` / ``alive`` / ``broken``."""

    id: int
    spec: ForwardSpec
    status: str = "alive"
    #: Last non-empty line kubectl printed — the failure detail on early exit.
    last_output: str = ""
    _proc: _ForwardProcess | None = field(default=None, repr=False)
    #: Set once the handshake resolved (ready line seen, or stdout hit EOF).
    _ready: threading.Event | None = field(default=None, repr=False)
    #: Serializes reader-thread mutations against re-attach process swaps.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    #: Bumped by every re-attach process swap: lets a stale wait_ready()
    #: waiter detect that the record now describes a replacement process.
    _generation: int = field(default=0, repr=False)


class ForwardRegistry:
    """Start, monitor, and stop session-scoped port-forwards.

    Args:
        context: kubeconfig context name to pin subprocesses to (or None).
        popen: subprocess factory, injectable for tests. Must accept the
            argv list plus the stdio keyword arguments `subprocess.Popen`
            takes.
    """

    def __init__(
        self,
        *,
        context: str | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self._context = context
        self._popen = popen
        #: Registered forwards. Single-key lookups (get(), and the entry
        #: reads in wait_ready()/fail_start()/reattach()) are deliberately
        #: lock-free: CPython dict reads are atomic and a record object is
        #: never replaced under its id — a re-attach swaps the *process*
        #: inside the record under the record's own lock. Every compound
        #: read (iteration) and every mutation holds _ops.
        self._records: dict[int, ForwardRecord] = {}
        self._next_id = 1
        #: Terminated processes awaiting exit: (proc, kill deadline, local port).
        self._reaping: list[tuple[_ForwardProcess, float, int]] = []
        #: Serializes registration state across threads: start()/reattach()
        #: run off the UI event loop while refresh()/stop()/stop_all() run on
        #: it. Held only for fast mutations — never across a blocking wait.
        self._ops = threading.RLock()
        #: Set by stop_all(): a spawn that lands afterwards is discarded
        #: instead of registered, so no child outlives teardown. retarget()
        #: reopens the latch for runtime context switches; _generation
        #: guards the gap — a spawn from before the teardown that lands
        #: after the reopen is still discarded (it targets the old cluster).
        self._closed = False
        self._generation = 0
        #: Ports with a spawn in flight: claimed atomically with the free
        #: check in _ensure_port_free(), released once the forward is
        #: registered (or the start failed) — two concurrent starts can
        #: never both pass the check and race for the same bind.
        self._claimed_ports: set[int] = set()

    def retarget(self, context: str | None) -> None:
        """Reopen the registry against *context* (issue #36, `:ctx`).

        Call only after ``stop_all()`` returned — the old cluster's children
        are reaped by then. Forwards started from now on run against the new
        context; a spawn that was already in flight when the switch began is
        still discarded at registration (generation mismatch), because its
        kubectl was launched with the old context's arguments.
        """
        with self._ops:
            self._context = context
            self._closed = False

    def start(self, spec: ForwardSpec) -> ForwardRecord:
        """Spawn `kubectl port-forward` for ``spec`` and track it.

        Raises:
            OSError: when the subprocess cannot be spawned (kubectl missing).
            ValueError: when the spec's kind is not forwardable, the local
                port is already used by a live forward — kubectl would only
                fail the bind asynchronously and masquerade as "broken" —
                or the registry has been shut down by stop_all().
        """
        self.refresh()  # a just-died forward must not hold its local port
        generation = self._generation
        self._ensure_port_free(spec.local_port)  # claims the port on success
        # try/finally, not an OSError catch: _spawn also raises ValueError
        # for an unforwardable kind, and *any* failure escaping with the
        # claim held would block the port for the rest of the session.
        spawned = False
        try:
            proc = self._spawn(spec)
            spawned = True
        finally:
            if not spawned:
                self._release_claim(spec.local_port)
        with self._ops:
            self._claimed_ports.discard(spec.local_port)
            closed = self._closed or self._generation != generation
            if not closed:
                record = ForwardRecord(id=self._next_id, spec=spec, _proc=proc)
                # Prepared before publication: nothing may observe the
                # dataclass default ``alive`` on an unconfirmed process.
                binding = self._prepare_handshake(record)
                self._next_id += 1
                self._records[record.id] = record
        if closed:
            # stop_all() won the race while the spawn was in flight — adopt
            # nothing: an untracked child would outlive the session.
            self._discard_spawn(proc)
            msg = "port-forward registry is shut down"
            raise ValueError(msg)
        self._start_watcher(record, binding)
        return record

    def _spawn(self, spec: ForwardSpec) -> _ForwardProcess:
        argv = build_port_forward_argv(
            spec.kind,
            spec.namespace,
            spec.name,
            local_port=spec.local_port,
            remote_port=spec.remote_port,
            context=self._context,
        )
        # stdout is read (readiness handshake + failure detail) by a daemon
        # thread, so the child can never block on an unread PIPE. stderr is
        # merged in: kubectl reports bind/RBAC errors there.
        proc: _ForwardProcess = self._popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return proc

    def _prepare_handshake(self, record: ForwardRecord) -> _WatcherBinding | None:
        """Install the readiness state before the record becomes observable.

        Publishing first and downgrading to ``starting`` afterwards would
        let a concurrent poll read an unconfirmed process as ``alive`` —
        and, on a re-attach, a record still reading ``broken`` after the
        swap would be a reclaim target for a concurrent same-port start.

        Returns:
            The exact process/stream/event binding to hand to
            `_start_watcher()`, or None when there is no readiness channel.
            The caller must pass this binding on rather than re-reading the
            record: by launch time a concurrent re-attach may already have
            swapped a replacement in.
        """
        proc = record._proc
        stream = getattr(proc, "stdout", None)
        if proc is None or stream is None:
            # No readiness channel (injected test doubles) — trust the spawn.
            record.status = "alive"
            record._ready = None
            return None
        record.status = "starting"
        ready = threading.Event()
        record._ready = ready
        return (proc, stream, ready)

    def _start_watcher(self, record: ForwardRecord, binding: _WatcherBinding | None) -> None:
        """Start the reader thread for a prepared generation (outside the locks).

        ``binding`` is what `_prepare_handshake()` captured for this
        generation. The record's mutable fields are deliberately not re-read
        here: a re-attach may have swapped in a replacement since, and a
        second reader on the replacement's stream could split its readiness
        line and EOF — marking a valid forward ``broken``.
        """
        if binding is None:
            return
        proc, stream, ready = binding
        threading.Thread(
            target=self._watch_output, args=(record, proc, stream, ready), daemon=True
        ).start()

    @staticmethod
    def _watch_output(
        record: ForwardRecord,
        proc: _ForwardProcess,
        stream: Iterable[str],
        ready: threading.Event,
    ) -> None:
        """Reader thread: drain the output of one specific child process.

        The watcher is bound to the process generation it was spawned for —
        after a re-attach swaps in a fresh process, buffered output flushed
        by the dead one must not mutate the reused record (a late
        "Forwarding from" line would wrongly mark the replacement alive).
        The generation check and the mutations it guards happen atomically
        under the record lock, which reattach() also holds while swapping
        the process in — a check passed against the old generation cannot
        interleave with the swap and leak stale writes onto the new one.
        """
        try:
            for raw in stream:
                line = raw.strip()
                with record._lock:
                    if record._proc is not proc:
                        ready.set()  # our generation is over — wake its waiter
                        return  # superseded by a re-attach — stale output
                    if line:
                        record.last_output = line
                    if record.status == "starting" and line.startswith(_READY_PREFIX):
                        record.status = "alive"
                        ready.set()
        except (OSError, ValueError):  # pipe closed under us mid-read
            pass
        # EOF: the child exited or closed its stdout — either way this
        # generation can never confirm its listener. Mark the failed start
        # now: poll() may not observe the exit yet, and reporting "starting"
        # would cost the caller its full timeout and hide kubectl's error.
        with record._lock:
            if record._proc is proc and record.status == "starting":
                record.status = "broken"
        ready.set()

    def wait_ready(self, forward_id: int, *, timeout: float) -> str:
        """Block until the forward's handshake resolves (call off the loop).

        Returns:
            The resulting status: ``alive`` when kubectl confirmed the
            listener, ``broken`` when the child exited before that (the
            record keeps kubectl's last words in ``last_output``),
            ``superseded`` when a re-attach swapped the watched process out
            from under this waiter — the record's state now describes the
            replacement and must not be applied to this generation — or
            ``starting`` when the child is silent but still running after
            ``timeout`` — the caller decides whether to keep waiting or to
            abort the unconfirmed forward via `fail_start()` / `stop()`.
        """
        record = self._records.get(forward_id)
        if record is None:
            return "broken"
        # Snapshot the event and generation for this invocation: a concurrent
        # re-attach swaps both, and re-reading them would strand a superseded
        # waiter on the replacement generation's event for the full timeout.
        with record._lock:
            ready = record._ready
            generation = record._generation
            status = record.status
        if ready is None or status == "alive":
            return status
        ready.wait(timeout)
        with record._lock:
            if record._generation != generation:
                return "superseded"
            if record.status in ("alive", "broken"):
                return record.status
            proc = record._proc
            if proc is not None and proc.poll() is not None:
                record.status = "broken"
                return "broken"
            return "starting"

    def refresh(self) -> None:
        """Poll every tracked process; exited ones become ``broken``.

        Also escalates previously stopped processes that outlived their
        grace deadline to SIGKILL — stop() itself never blocks.
        """
        self._reap()
        # Snapshot: a start() on another thread may register concurrently,
        # and iterating the live dict would raise on the size change.
        for record in self.forwards():
            # Same lock discipline as the reader thread and fail_start():
            # status transitions stay serialized against the handshake.
            with record._lock:
                proc = record._proc
                if record.status == "broken" or proc is None or proc.poll() is None:
                    continue
                record.status = "broken"
                # Snapshot with the transition: a concurrent re-attach may
                # install the replacement's fresh event the instant this
                # lock is released, and waking that one would fail a valid
                # replacement's handshake early.
                ready = record._ready
            # A handshake blocked on this now-dead child must fail now,
            # not after its full timeout — a wedged pipe never EOFs.
            self._release_waiters(ready)

    def forwards(self) -> list[ForwardRecord]:
        """Tracked forwards in start order."""
        with self._ops:
            return list(self._records.values())

    def get(self, forward_id: int) -> ForwardRecord | None:
        """The tracked forward with ``forward_id``, or None."""
        return self._records.get(forward_id)

    def generation(self, forward_id: int) -> int | None:
        """The forward's current process generation (each re-attach bumps it).

        Snapshot this before `wait_ready()` and hand it to `fail_start()`:
        a timed-out handshake then can never tear down a replacement
        process that was re-attached while the caller was waiting.
        """
        record = self._records.get(forward_id)
        if record is None:
            return None
        with record._lock:
            return record._generation

    def stop(self, forward_id: int) -> ForwardRecord | None:
        """Signal one forward to exit and forget it; None when the id is unknown.

        Non-blocking: the process gets SIGTERM immediately and a later
        refresh() escalates to SIGKILL if it ignores the grace period, so a
        wedged kubectl can never freeze the caller (the UI event loop).
        """
        with self._ops:
            record = self._records.pop(forward_id, None)
        if record is None:
            return None
        with record._lock:
            ready = record._ready
        self._signal_stop(record)
        self._release_waiters(ready)
        return record

    def reattach(self, forward_id: int, *, retarget: bool = False) -> ForwardRecord | None:
        """Restart a ``broken`` forward in place (same id, same ports).

        With ``retarget`` the replacement aims at the spec's recorded owning
        workload instead of the original pod — the way a forward follows a
        vanished pod to its replacement (kubectl resolves a live pod itself
        for workload targets). The record's spec is updated on adoption so
        the forward list shows what actually runs.

        Returns None (and changes nothing) when the id is unknown, the
        forward is still alive — re-running a live forward would just fail
        on the occupied local port — a retarget was requested without a
        recorded workload, or the record was stopped or torn down while the
        replacement was being spawned (the replacement is put down, never
        adopted).

        Raises:
            OSError: when the replacement subprocess cannot be spawned.
            ValueError: when another live forward has since claimed the
                broken forward's local port.
        """
        self.refresh()  # a stale 'alive' peer must not block the port check
        record = self._records.get(forward_id)
        if record is None or record.status != "broken":
            return None
        spec = record.spec.retargeted() if retarget else record.spec
        if spec is None:
            return None
        # _ensure_port_free also covers this record itself: a record broken
        # by EOF may still have its child running and holding the local port
        # (poll() can lag the stream closing) — it is signalled and reaped
        # there instead of being orphaned by the process swap below.
        self._ensure_port_free(spec.local_port)  # claims the port on success
        # Same failure-shape as start(): any exception escaping the spawn
        # must release the claim, not just the documented OSError.
        spawned = False
        try:
            replacement = self._spawn(spec)
            spawned = True
        finally:
            if not spawned:
                self._release_claim(spec.local_port)
        superseded: threading.Event | None = None
        binding: _WatcherBinding | None = None
        # Adopt under the ops lock: a stop or teardown that won the race
        # while the spawn was in flight must not have its outcome undone by
        # this thread publishing a fresh process afterwards.
        with self._ops:
            self._claimed_ports.discard(spec.local_port)
            adopted = not self._closed and self._records.get(forward_id) is record
            if adopted:
                # Swap under the record lock: a stale watcher that already
                # passed its generation check must finish its guarded writes
                # before the new process (and the reset output) are published.
                with record._lock:
                    record._proc = replacement
                    record.spec = spec
                    record.last_output = ""
                    record._generation += 1
                    superseded = record._ready
                    # The starting state swaps in atomically with the
                    # process: a record left ``broken`` here would be a
                    # reclaim target for a concurrent same-port start, which
                    # would signal the fresh replacement down.
                    binding = self._prepare_handshake(record)
        if not adopted:
            self._discard_spawn(replacement)
            return None
        if superseded is not None:
            # Resolve the previous generation's waiter right away — its
            # reader may never observe the swap (blocked on a silent
            # stream), and a superseded confirmation must not sit out the
            # full readiness timeout before it can get out of the way.
            # Invariant: wait_ready() captures record._ready once on entry,
            # so setting the old event here can never spuriously wake a
            # waiter of the replacement generation — those only ever block
            # on the fresh event _prepare_handshake() installed above.
            superseded.set()
        self._start_watcher(record, binding)
        return record

    def fail_start(
        self, forward_id: int, *, keep: bool = True, generation: int | None = None
    ) -> str:
        """Abort a forward whose readiness handshake never resolved.

        Covers both unconfirmed outcomes: a child silent-but-running after
        the caller's wait window, and one already marked ``broken`` by its
        stream closing while the process may still run and hold the local
        port. Either way the child is signalled to exit (grace-escalated
        like stop()) and the record is ``broken``. With ``keep`` it stays
        listed so `:pf` shows the failure and offers a re-attach;
        ``keep=False`` also unlists a start that never worked.

        Returns the outcome so the caller never has to infer it from the
        (possibly reused) record:

        - ``"aborted"``: this call failed the forward and signalled its child.
        - ``"alive"``: the forward confirmed its listener in the meantime —
          the caller's timeout snapshot lost that race; the forward is ready.
        - ``"superseded"``: ``generation`` (snapshot from `generation()`
          before waiting) no longer matches — the timed-out handshake belongs
          to a superseded process and the re-attached replacement was left
          untouched, whatever its state.
        - ``"gone"``: the record was already unlisted (stopped, torn down, or
          never known) — that deliberate outcome stands.
        """
        record = self._records.get(forward_id)
        if record is None:
            return "gone"
        # Validation and teardown form one _ops critical section: a re-attach
        # that would adopt a replacement (it needs _ops too) can only run
        # before the generation check or after the abort — never between the
        # validation and the pop, where it used to get its fresh process
        # signalled down as the failed generation.
        with self._ops:
            if self._records.get(forward_id) is not record:
                # A stop (or teardown) unlisted the record between the
                # lock-free lookup and this lock — that deliberate outcome
                # stands; reporting a failed start would misdescribe it.
                return "gone"
            with record._lock:
                if generation is not None and record._generation != generation:
                    return "superseded"
                # Check and transition atomically: the reader thread flips
                # ``starting`` to ``alive`` under the same lock, so a forward
                # confirmed at the last instant cannot be torn down here.
                if record.status == "alive":
                    return "alive"
                record.status = "broken"
                # Snapshot the validated generation's process and waiter:
                # only these may be torn down below, whatever the record
                # describes by then.
                proc = record._proc
                ready = record._ready
            if not keep:
                self._records.pop(forward_id, None)
        # Signal outside _ops: the snapshot pins the validated generation's
        # process, so other registry operations need not wait on the syscall.
        self._signal_proc(proc, record.spec.local_port)
        self._release_waiters(ready)
        return "aborted"

    def _ensure_port_free(self, local_port: int) -> None:
        """Guarantee ``local_port`` is bindable before spawning kubectl.

        A live forward on the port raises immediately — the new child would
        only lose the bind race and masquerade as a broken target. A stopped
        child still exiting (SIGTERM grace) may also hold the socket; that
        one is forced down with SIGKILL (immediate, cannot be ignored) so a
        stop-then-restart on the same port works without a delayed failure.
        A ``broken`` record's child can also still be running (EOF marks the
        status before poll() observes the exit) — it is signalled down and
        handed to the same reap pass instead of keeping the port.

        On success the port is left claimed (atomically with the checks) so
        a concurrent start cannot pass the same check before this spawn
        registers; the caller releases the claim once the forward is
        registered or the start failed.

        Raises:
            ValueError: when the registry has been shut down, a live forward
                already uses ``local_port``, another start is already
                claiming it, or a previously stopped child holding it cannot
                be reaped in time — the spawn fails cleanly instead of
                blocking the caller indefinitely.
        """
        holders = self._claim_port(local_port)
        # Blocking waits happen outside the ops lock so refresh()/stop() on
        # the event loop are never stalled behind a stuck port reclaim.
        for index, (proc, _deadline, _port) in enumerate(holders):
            if proc.poll() is not None:
                continue  # exited — no longer holds the port, drop the entry
            proc.kill()
            try:
                proc.wait(timeout=_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired as exc:
                # Even SIGKILL cannot reap a child stuck in the kernel. Keep
                # tracking every unreaped holder and fail this spawn bounded
                # rather than freezing the caller.
                with self._ops:
                    self._reaping.extend(holders[index:])
                    self._claimed_ports.discard(local_port)
                msg = f"local port {local_port} is still being released — try again"
                raise ValueError(msg) from exc

    def _claim_port(self, local_port: int) -> list[tuple[_ForwardProcess, float, int]]:
        """Atomically validate and claim ``local_port`` under the ops lock.

        Returns the unreaped previous holders of the port for the caller to
        wait on outside the lock.

        Raises:
            ValueError: when the registry is shut down, the port is used by
                a live forward, or another start is already claiming it.
        """
        holders: list[tuple[_ForwardProcess, float, int]] = []
        with self._ops:
            if self._closed:
                # Rejecting here (atomically with the claim) keeps shutdown
                # side-effect free: a late caller must never spawn a real
                # kubectl just for the post-spawn check to put it down. The
                # post-spawn check still covers a shutdown that begins after
                # this point.
                msg = "port-forward registry is shut down"
                raise ValueError(msg)
            if local_port in self._claimed_ports:
                msg = f"local port {local_port} is already being claimed by another start"
                raise ValueError(msg)
            for existing in list(self._records.values()):
                if existing.spec.local_port != local_port:
                    continue
                if existing.status != "broken":
                    msg = f"local port {local_port} already forwarded by #{existing.id}"
                    raise ValueError(msg)
                proc = existing._proc
                if (
                    proc is not None
                    and proc.poll() is None
                    and all(reaping is not proc for reaping, _, _ in self._reaping)
                ):
                    self._signal_stop(existing)
            remaining: list[tuple[_ForwardProcess, float, int]] = []
            for entry in self._reaping:
                (holders if entry[2] == local_port else remaining).append(entry)
            self._reaping = remaining
            self._claimed_ports.add(local_port)
        return holders

    def _release_claim(self, local_port: int) -> None:
        """Release a port claim after a failed spawn (never registered)."""
        with self._ops:
            self._claimed_ports.discard(local_port)

    def stop_all(self) -> list[ForwardRecord]:
        """Terminate every tracked forward (app exit / context teardown).

        All children are signalled first, then waited on under one shared
        grace deadline; stragglers are killed. Shutdown latency is therefore
        bounded by a single grace period, not one per forward.

        Returns:
            The stopped records, so the caller can audit each stop.
        """
        with self._ops:
            self._closed = True
            self._generation += 1
            records = list(self._records.values())
            self._records.clear()
            reaping = self._reaping
            self._reaping = []
        for record in records:
            self._release_waiters(record._ready)
        live = [
            record._proc
            for record in records
            if record._proc is not None and record._proc.poll() is None
        ]
        for proc in live:
            proc.terminate()
        # Include earlier ctrl+d stops still awaiting their grace kill — a
        # stubborn forward must not outlive the session just because the
        # user exited before the next refresh() tick.
        live.extend(proc for proc, _, _ in reaping if proc.poll() is None)
        deadline = monotonic() + _STOP_GRACE_SECONDS
        while any(proc.poll() is None for proc in live):
            if monotonic() > deadline:
                stragglers = [proc for proc in live if proc.poll() is None]
                for proc in stragglers:
                    proc.kill()
                # SIGKILL cannot be ignored, but reaping a child stuck in
                # the kernel can still stall — all stragglers share one
                # bounded reap budget so teardown latency stays a single
                # grace period, not one per wedged child.
                reap_deadline = monotonic() + _STOP_GRACE_SECONDS
                for proc in stragglers:
                    budget = reap_deadline - monotonic()
                    if budget <= 0:
                        break
                    with suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=budget)
                break
            time.sleep(_STOP_POLL_SECONDS)
        return records

    def _signal_stop(self, record: ForwardRecord) -> None:
        self._signal_proc(record._proc, record.spec.local_port)

    def _signal_proc(self, proc: _ForwardProcess | None, local_port: int) -> None:
        if proc is None or proc.poll() is not None:
            return  # already exited (broken) — nothing to signal
        proc.terminate()
        with self._ops:
            self._reaping.append((proc, monotonic() + _STOP_GRACE_SECONDS, local_port))

    def _discard_spawn(self, proc: _ForwardProcess) -> None:
        """Put down a child spawned after the registry shut down.

        It was never registered, so nothing else will ever reap it — kill
        outright (no grace: teardown already spent the grace budget) and
        wait bounded off the event loop.
        """
        proc.kill()
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_STOP_GRACE_SECONDS)

    @staticmethod
    def _release_waiters(ready: threading.Event | None) -> None:
        """Wake anyone blocked in wait_ready() on this generation's event.

        Without this, a stop during the handshake would leave the caller
        waiting out the full readiness timeout on a forward that no longer
        exists (a fake child never delivers the EOF a real kubectl would).
        Takes the snapshotted event, never the record: re-reading the
        record's mutable ``_ready`` could pick up (and spuriously wake) a
        replacement generation swapped in by a concurrent re-attach.
        """
        if ready is not None:
            ready.set()

    def _reap(self) -> None:
        """Advance stopped processes: drop exited ones, kill deadline-breakers."""
        with self._ops:
            remaining: list[tuple[_ForwardProcess, float, int]] = []
            for proc, deadline, port in self._reaping:
                if proc.poll() is not None:
                    continue  # exited; poll() reaped it
                if monotonic() > deadline:
                    proc.kill()
                # Keep until poll() confirms the exit so the child is reaped.
                remaining.append((proc, deadline, port))
            self._reaping = remaining


def controller_owner(manifest: dict[str, Any]) -> tuple[str, str] | None:
    """The (kind, name) of the reference that controls this object, if any.

    Reads ``metadata.ownerReferences`` and picks the entry marked
    ``controller: true`` — the workload a re-attach can follow when the pod
    itself is gone. Malformed or missing data yields None, never an error.
    """
    refs = manifest.get("metadata", {}).get("ownerReferences")
    if not isinstance(refs, list):
        return None
    for ref in refs:
        if not isinstance(ref, dict) or not ref.get("controller"):
            continue
        kind, name = ref.get("kind"), ref.get("name")
        if isinstance(kind, str) and isinstance(name, str) and kind and name:
            return kind, name
    return None


def candidate_remote_ports(kind: str, manifest: dict[str, Any]) -> list[int]:
    """Declared TCP ports of a pod or service manifest, for dialog prefill.

    `kubectl port-forward` speaks TCP only, so UDP/SCTP declarations are
    skipped — offering one would prefill a port that is guaranteed to fail.
    An absent ``protocol`` defaults to TCP, matching the Kubernetes API.

    Best-effort over a cluster-supplied document: anything malformed is
    skipped, never raised — an empty result just means the user types the
    port themselves.
    """
    spec = manifest.get("spec")
    if not isinstance(spec, dict):
        return []
    if kind == "services":
        entries = spec.get("ports")
        port_key = "port"
    else:
        containers = spec.get("containers")
        entries = []
        for container in containers if isinstance(containers, list) else []:
            if isinstance(container, dict) and isinstance(container.get("ports"), list):
                entries.extend(container["ports"])
        port_key = "containerPort"
    ports: list[int] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("protocol", "TCP") != "TCP":
            continue
        port = entry.get(port_key)
        if (
            isinstance(port, int)
            and not isinstance(port, bool)  # bool is an int subclass
            and 0 < port < 65536
            and port not in ports
        ):
            ports.append(port)
    return ports
