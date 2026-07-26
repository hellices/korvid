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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from korvid.k8s.portforward import build_port_forward_argv

#: How long to wait for kubectl to exit after terminate() before kill().
_STOP_GRACE_SECONDS = 2.0


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
        """Poll every tracked process; exited ones become ``broken``."""
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
        """Terminate and forget one forward; None when the id is unknown."""
        record = self._records.pop(forward_id, None)
        if record is None:
            return None
        self._terminate(record)
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
        """Terminate every tracked forward (app exit / context teardown)."""
        records = list(self._records.values())
        self._records.clear()
        for record in records:
            self._terminate(record)
        return records

    @staticmethod
    def _terminate(record: ForwardRecord) -> None:
        proc = record._proc
        if proc is None or proc.poll() is not None:
            return  # already exited (broken) — nothing to signal
        proc.terminate()
        try:
            proc.wait(timeout=_STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


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
