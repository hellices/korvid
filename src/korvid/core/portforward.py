"""Session-scoped port-forward registry (issue #38).

Tracks `kubectl port-forward` subprocesses started from the UI. The registry
owns process lifecycle only — no Textual imports — so the `:pf` screen and
the app's teardown paths can share one source of truth.

Liveness is the design goal (issue #38): a hand-managed forward dies silently
when its target pod restarts. `refresh()` polls every child process and marks
exited ones ``broken`` instead of dropping them, so the UI can surface the
breakage and offer a one-key re-attach.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Protocol

from korvid.k8s.portforward import build_port_forward_argv

#: How long a terminated kubectl gets to exit before it is killed.
_STOP_GRACE_SECONDS = 2.0

#: Poll step while stop_all() waits out the shared grace deadline.
_STOP_POLL_SECONDS = 0.05


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
    """A tracked forward. ``status`` is one of ``alive`` / ``broken``."""

    id: int
    spec: ForwardSpec
    status: str = "alive"
    _proc: _ForwardProcess | None = field(default=None, repr=False)


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
        #: Terminated processes awaiting exit, with their kill deadlines.
        self._reaping: list[tuple[_ForwardProcess, float]] = []

    def start(self, spec: ForwardSpec) -> ForwardRecord:
        """Spawn `kubectl port-forward` for ``spec`` and track it.

        Raises:
            OSError: when the subprocess cannot be spawned (kubectl missing).
            ValueError: when the spec's kind is not forwardable.
        """
        record = ForwardRecord(id=self._next_id, spec=spec, _proc=self._spawn(spec))
        self._next_id += 1
        self._records[record.id] = record
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
        # DEVNULL on purpose: kubectl chats on stdout for every connection
        # and an unread PIPE would eventually block the child. Failure detail
        # is conveyed by the exit itself (refresh() -> broken).
        proc: _ForwardProcess = self._popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc

    def refresh(self) -> None:
        """Poll every tracked process; exited ones become ``broken``.

        Also escalates previously stopped processes that outlived their
        grace deadline to SIGKILL — stop() itself never blocks.
        """
        self._reap()
        for record in self._records.values():
            proc = record._proc
            if record.status == "alive" and proc is not None and proc.poll() is not None:
                record.status = "broken"

    def forwards(self) -> list[ForwardRecord]:
        """Tracked forwards in start order."""
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
        record = self._records.pop(forward_id, None)
        if record is None:
            return None
        self._signal_stop(record)
        return record

    def reattach(self, forward_id: int) -> ForwardRecord | None:
        """Restart a ``broken`` forward in place (same id, same spec).

        Returns None (and changes nothing) when the id is unknown or the
        forward is still alive — re-running a live forward would just fail
        on the occupied local port.

        Raises:
            OSError: when the replacement subprocess cannot be spawned.
        """
        record = self._records.get(forward_id)
        if record is None or record.status != "broken":
            return None
        record._proc = self._spawn(record.spec)
        record.status = "alive"
        return record

    def stop_all(self) -> list[ForwardRecord]:
        """Terminate every tracked forward (app exit / context teardown).

        All children are signalled first, then waited on under one shared
        grace deadline; stragglers are killed. Shutdown latency is therefore
        bounded by a single grace period, not one per forward.

        Returns:
            The stopped records, so the caller can audit each stop.
        """
        records = list(self._records.values())
        self._records.clear()
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
        live.extend(proc for proc, _ in self._reaping if proc.poll() is None)
        self._reaping.clear()
        deadline = monotonic() + _STOP_GRACE_SECONDS
        while any(proc.poll() is None for proc in live):
            if monotonic() > deadline:
                for proc in live:
                    if proc.poll() is None:
                        proc.kill()
                break
            time.sleep(_STOP_POLL_SECONDS)
        return records

    def _signal_stop(self, record: ForwardRecord) -> None:
        proc = record._proc
        if proc is None or proc.poll() is not None:
            return  # already exited (broken) — nothing to signal
        proc.terminate()
        self._reaping.append((proc, monotonic() + _STOP_GRACE_SECONDS))

    def _reap(self) -> None:
        """Advance stopped processes: drop exited ones, kill deadline-breakers."""
        remaining: list[tuple[_ForwardProcess, float]] = []
        for proc, deadline in self._reaping:
            if proc.poll() is not None:
                continue  # exited; poll() reaped it
            if monotonic() > deadline:
                proc.kill()
            # Keep until poll() confirms the exit so the child is reaped.
            remaining.append((proc, deadline))
        self._reaping = remaining


def candidate_remote_ports(kind: str, manifest: dict[str, Any]) -> list[int]:
    """Declared ports of a pod or service manifest, for dialog prefill.

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
        port = entry.get(port_key) if isinstance(entry, dict) else None
        if isinstance(port, int) and 0 < port < 65536 and port not in ports:
            ports.append(port)
    return ports
