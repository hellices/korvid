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
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ForwardSpec:
    """What to forward: everything needed to (re)build the kubectl argv."""

    kind: str  # "pods" | "services"
    namespace: str
    name: str
    local_port: int
    remote_port: int


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
        self._records: dict[int, ForwardRecord] = {}
        self._next_id = 1
        #: Terminated processes awaiting exit: (proc, kill deadline, local port).
        self._reaping: list[tuple[_ForwardProcess, float, int]] = []
        #: Serializes registration state across threads: start()/reattach()
        #: run off the UI event loop while refresh()/stop()/stop_all() run on
        #: it. Held only for fast mutations — never across a blocking wait.
        self._ops = threading.RLock()
        #: Set (permanently) by stop_all(): a spawn that lands afterwards is
        #: discarded instead of registered, so no child outlives teardown.
        self._closed = False
        #: Ports with a spawn in flight: claimed atomically with the free
        #: check in _ensure_port_free(), released once the forward is
        #: registered (or the start failed) — two concurrent starts can
        #: never both pass the check and race for the same bind.
        self._claimed_ports: set[int] = set()

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
        self._ensure_port_free(spec.local_port)  # claims the port on success
        try:
            proc = self._spawn(spec)
        except OSError:
            self._release_claim(spec.local_port)
            raise
        with self._ops:
            self._claimed_ports.discard(spec.local_port)
            closed = self._closed
            if not closed:
                record = ForwardRecord(id=self._next_id, spec=spec, _proc=proc)
                self._next_id += 1
                self._records[record.id] = record
        if closed:
            # stop_all() won the race while the spawn was in flight — adopt
            # nothing: an untracked child would outlive the session.
            self._discard_spawn(proc)
            msg = "port-forward registry is shut down"
            raise ValueError(msg)
        self._begin_handshake(record)
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

    def _begin_handshake(self, record: ForwardRecord) -> None:
        """Watch the child's output until kubectl confirms the listener."""
        proc = record._proc
        stream = getattr(proc, "stdout", None)
        if proc is None or stream is None:
            # No readiness channel (injected test doubles) — trust the spawn.
            record.status = "alive"
            return
        record.status = "starting"
        ready = threading.Event()
        record._ready = ready
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
            # A handshake blocked on this now-dead child must fail now,
            # not after its full timeout — a wedged pipe never EOFs.
            self._release_waiters(record)

    def forwards(self) -> list[ForwardRecord]:
        """Tracked forwards in start order."""
        with self._ops:
            return list(self._records.values())

    def get(self, forward_id: int) -> ForwardRecord | None:
        """The tracked forward with ``forward_id``, or None."""
        return self._records.get(forward_id)

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
        self._signal_stop(record)
        self._release_waiters(record)
        return record

    def reattach(self, forward_id: int) -> ForwardRecord | None:
        """Restart a ``broken`` forward in place (same id, same spec).

        Returns None (and changes nothing) when the id is unknown, the
        forward is still alive — re-running a live forward would just fail
        on the occupied local port — or the record was stopped or torn down
        while the replacement was being spawned (the replacement is put
        down, never adopted).

        Raises:
            OSError: when the replacement subprocess cannot be spawned.
            ValueError: when another live forward has since claimed the
                broken forward's local port.
        """
        self.refresh()  # a stale 'alive' peer must not block the port check
        record = self._records.get(forward_id)
        if record is None or record.status != "broken":
            return None
        # _ensure_port_free also covers this record itself: a record broken
        # by EOF may still have its child running and holding the local port
        # (poll() can lag the stream closing) — it is signalled and reaped
        # there instead of being orphaned by the process swap below.
        self._ensure_port_free(record.spec.local_port)  # claims the port on success
        try:
            replacement = self._spawn(record.spec)
        except OSError:
            self._release_claim(record.spec.local_port)
            raise
        superseded: threading.Event | None = None
        # Adopt under the ops lock: a stop or teardown that won the race
        # while the spawn was in flight must not have its outcome undone by
        # this thread publishing a fresh process afterwards.
        with self._ops:
            self._claimed_ports.discard(record.spec.local_port)
            adopted = not self._closed and self._records.get(forward_id) is record
            if adopted:
                # Swap under the record lock: a stale watcher that already
                # passed its generation check must finish its guarded writes
                # before the new process (and the reset output) are published.
                with record._lock:
                    record._proc = replacement
                    record.last_output = ""
                    record._generation += 1
                    superseded = record._ready
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
            # on the fresh event _begin_handshake() installs below.
            superseded.set()
        self._begin_handshake(record)
        return record

    def fail_start(self, forward_id: int, *, keep: bool = True) -> ForwardRecord | None:
        """Abort a forward whose readiness handshake never resolved.

        Covers both unconfirmed outcomes: a child silent-but-running after
        the caller's wait window, and one already marked ``broken`` by its
        stream closing while the process may still run and hold the local
        port. Either way the child is signalled to exit (grace-escalated
        like stop()) and the record is ``broken``. With ``keep`` it stays
        listed so `:pf` shows the failure and offers a re-attach;
        ``keep=False`` also unlists a start that never worked.

        Returns None without touching anything when the forward confirmed
        its listener in the meantime (``alive``) — the caller's timeout
        snapshot lost that race and must treat the forward as ready.
        """
        record = self._records.get(forward_id)
        if record is None:
            return None
        with record._lock:
            # Check and transition atomically: the reader thread flips
            # ``starting`` to ``alive`` under the same lock, so a forward
            # confirmed at the last instant cannot be torn down here.
            if record.status == "alive":
                return None
            record.status = "broken"
        if not keep:
            with self._ops:
                self._records.pop(forward_id, None)
        self._signal_stop(record)
        self._release_waiters(record)
        return record

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
            ValueError: when a live forward already uses ``local_port``,
                another start is already claiming it, or a previously
                stopped child holding it cannot be reaped in time — the
                spawn fails cleanly instead of blocking the caller
                indefinitely.
        """
        holders: list[tuple[_ForwardProcess, float, int]] = []
        with self._ops:
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
            records = list(self._records.values())
            self._records.clear()
            reaping = self._reaping
            self._reaping = []
        for record in records:
            self._release_waiters(record)
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
        proc = record._proc
        if proc is None or proc.poll() is not None:
            return  # already exited (broken) — nothing to signal
        proc.terminate()
        with self._ops:
            self._reaping.append((proc, monotonic() + _STOP_GRACE_SECONDS, record.spec.local_port))

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
    def _release_waiters(record: ForwardRecord) -> None:
        """Wake anyone blocked in wait_ready() for a just-stopped forward.

        Without this, a stop during the handshake would leave the caller
        waiting out the full readiness timeout on a forward that no longer
        exists (a fake child never delivers the EOF a real kubectl would).
        """
        if record._ready is not None:
            record._ready.set()

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
