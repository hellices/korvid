"""Tests for the session-scoped port-forward registry (issue #38)."""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from time import monotonic
from typing import Any
from unittest.mock import patch

import pytest

from korvid.core.portforward import (
    ForwardRecord,
    ForwardRegistry,
    ForwardSpec,
    candidate_remote_ports,
)


class _FakeProc:
    """Stand-in for a kubectl port-forward subprocess."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.waited = False
        # None = no readiness channel; _piped_registry swaps in _GatedStream.
        self.stdout: Any = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise RuntimeError("wait() on a live fake proc")
        self.waited = True
        return self.returncode

    def exit(self, code: int) -> None:
        """Test hook: simulate the subprocess dying on its own."""
        self.returncode = code


def _registry(procs: list[_FakeProc], context: str | None = None) -> ForwardRegistry:
    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        procs.append(proc)
        return proc

    return ForwardRegistry(context=context, popen=_popen)


def _spec(**overrides: Any) -> ForwardSpec:
    base: dict[str, Any] = {
        "kind": "pods",
        "namespace": "default",
        "name": "api-1",
        "local_port": 8080,
        "remote_port": 80,
    }
    base.update(overrides)
    return ForwardSpec(**base)


def test_start_spawns_kubectl_and_registers_alive_forward() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    record = registry.start(_spec())
    assert len(procs) == 1
    assert procs[0].argv[:2] == ["kubectl", "port-forward"]
    assert "8080:80" in procs[0].argv
    assert record.status == "alive"
    assert record.spec.name == "api-1"
    assert registry.forwards() == [record]


def test_start_pins_kube_context() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs, context="staging")
    registry.start(_spec())
    assert "--context" in procs[0].argv


def test_ids_are_unique_and_stable() -> None:
    registry = _registry([])
    first = registry.start(_spec())
    second = registry.start(_spec(local_port=9090))
    assert first.id != second.id
    assert [r.id for r in registry.forwards()] == [first.id, second.id]


def test_refresh_marks_dead_process_broken() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    record = registry.start(_spec())
    procs[0].exit(1)
    registry.refresh()
    assert registry.forwards()[0].status == "broken"
    assert record.id == registry.forwards()[0].id


def test_refresh_keeps_live_forward_alive() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    registry.start(_spec())
    registry.refresh()
    assert registry.forwards()[0].status == "alive"


def test_stop_terminates_and_removes_forward() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    record = registry.start(_spec())
    stopped = registry.stop(record.id)
    assert stopped is not None
    assert stopped.id == record.id
    assert procs[0].terminated
    assert registry.forwards() == []


def test_stop_unknown_id_returns_none() -> None:
    registry = _registry([])
    assert registry.stop(99) is None


def test_stop_broken_forward_does_not_terminate_again() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    record = registry.start(_spec())
    procs[0].exit(1)
    registry.refresh()
    stopped = registry.stop(record.id)
    assert stopped is not None
    assert not procs[0].terminated
    assert registry.forwards() == []


def test_reattach_restarts_broken_forward_in_place() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    record = registry.start(_spec())
    procs[0].exit(1)
    registry.refresh()
    revived = registry.reattach(record.id)
    assert revived is not None
    assert revived.id == record.id
    assert revived.status == "alive"
    assert len(procs) == 2
    assert registry.forwards() == [revived]


def test_reattach_alive_forward_is_a_noop() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    record = registry.start(_spec())
    assert registry.reattach(record.id) is None
    assert len(procs) == 1


def test_reattach_unknown_id_returns_none() -> None:
    registry = _registry([])
    assert registry.reattach(42) is None


def test_stop_all_terminates_every_live_forward() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    registry.start(_spec())
    registry.start(_spec(local_port=9090))
    registry.stop_all()
    assert all(p.terminated for p in procs)
    assert registry.forwards() == []


def test_start_failure_propagates_oserror() -> None:
    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        raise OSError("kubectl not found")

    registry = ForwardRegistry(context=None, popen=_popen)
    with pytest.raises(OSError, match="kubectl not found"):
        registry.start(_spec())
    assert registry.forwards() == []


def test_candidate_ports_from_pod_manifest() -> None:
    from korvid.core.portforward import candidate_remote_ports

    manifest = {
        "spec": {
            "containers": [
                {"name": "app", "ports": [{"containerPort": 8080}, {"containerPort": 9090}]},
                {"name": "sidecar", "ports": [{"containerPort": 8080}]},
            ]
        }
    }
    assert candidate_remote_ports("pods", manifest) == [8080, 9090]


def test_candidate_ports_from_service_manifest() -> None:
    from korvid.core.portforward import candidate_remote_ports

    manifest = {"spec": {"ports": [{"port": 80}, {"port": 443}]}}
    assert candidate_remote_ports("services", manifest) == [80, 443]


def test_candidate_ports_tolerate_malformed_manifest() -> None:
    from korvid.core.portforward import candidate_remote_ports

    assert candidate_remote_ports("pods", {}) == []
    assert candidate_remote_ports("services", {"spec": {"ports": "nope"}}) == []
    assert candidate_remote_ports("pods", {"spec": {"containers": [{"ports": [{}]}]}}) == []


class _StubbornProc(_FakeProc):
    """Ignores terminate(); only kill() ends it."""

    def terminate(self) -> None:
        self.terminated = True  # signal recorded, process survives


def test_stop_does_not_block_on_slow_process() -> None:
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _StubbornProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    record = registry.start(_spec())
    stopped = registry.stop(record.id)  # must return immediately, no wait()
    assert stopped is not None
    assert procs[0].terminated
    assert not procs[0].killed


def test_refresh_kills_stopped_process_after_grace() -> None:
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _StubbornProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    record = registry.start(_spec())
    registry.stop(record.id)
    registry.refresh()
    assert not procs[0].killed  # grace not yet elapsed
    with patch("korvid.core.portforward.monotonic", return_value=monotonic() + 60.0):
        registry.refresh()
    assert procs[0].killed


def test_stop_all_kills_stragglers_after_shared_deadline() -> None:
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _StubbornProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    registry.start(_spec())
    registry.start(_spec(local_port=9090))
    clock = monotonic()
    first = iter([clock])

    def _mono() -> float:
        # First call computes the deadline; every later call is past it.
        return next(first, clock + 60.0)

    with (
        patch("korvid.core.portforward.monotonic", side_effect=_mono),
        patch("korvid.core.portforward.time.sleep"),
    ):
        records = registry.stop_all()
    assert len(records) == 2
    assert all(p.terminated for p in procs)
    assert all(p.killed for p in procs)
    assert all(p.waited for p in procs)  # killed children are reaped, no zombies


def test_stop_all_returns_stopped_records_for_auditing() -> None:
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    registry.start(_spec())
    registry.start(_spec(local_port=9090))
    records = registry.stop_all()
    assert [r.spec.local_port for r in records] == [8080, 9090]


def test_stop_all_covers_previously_stopped_stragglers() -> None:
    """A stubborn proc stopped with ctrl+d must not outlive session teardown."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _StubbornProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    record = registry.start(_spec())
    registry.stop(record.id)  # moved to the reap list, still alive
    clock = monotonic()
    first = iter([clock])

    def _mono() -> float:
        return next(first, clock + 60.0)

    with (
        patch("korvid.core.portforward.monotonic", side_effect=_mono),
        patch("korvid.core.portforward.time.sleep"),
    ):
        registry.stop_all()
    assert procs[0].killed


def test_reattach_stops_a_lingering_child_before_respawning() -> None:
    """An EOF-broken record may still have a running child holding the port."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].stdout.feed("error: unable to listen\n")
    # Stream closes while poll() still reports the child as running.
    procs[0].stdout.feed(None)
    assert registry.wait_ready(record.id, timeout=2.0) == "broken"
    assert registry.reattach(record.id) is record
    assert procs[0].terminated, "the lingering child was orphaned by the re-attach"
    assert len(procs) == 2
    assert record.status == "starting"
    procs[1].stdout.feed(None)


def test_stop_all_does_not_hang_on_an_unreapable_straggler() -> None:
    """A kill-immune child must not block app exit forever."""
    procs: list[_FakeProc] = []

    class _UnreapableProc(_StubbornProc):
        def kill(self) -> None:
            self.killed = True  # SIGKILL sent; the child is stuck in the kernel

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout or 0.0)

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _UnreapableProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    registry.start(_spec())
    clock = monotonic()
    first = iter([clock])

    def _mono() -> float:
        return next(first, clock + 60.0)

    with (
        patch("korvid.core.portforward.monotonic", side_effect=_mono),
        patch("korvid.core.portforward.time.sleep"),
    ):
        records = registry.stop_all()  # must return despite the wedged child
    assert len(records) == 1
    assert procs[0].killed


def test_candidate_ports_excludes_booleans() -> None:
    """bool is an int subclass — `port: true` must not become prefill 'True'."""
    manifest = {"spec": {"containers": [{"ports": [{"containerPort": True}]}]}}
    assert candidate_remote_ports("pods", manifest) == []


def test_start_rejects_duplicate_local_port() -> None:
    """Reusing a live forward's local port must fail fast, not spawn kubectl."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    first = registry.start(_spec())
    with pytest.raises(ValueError, match=f"local port 8080 already forwarded by #{first.id}"):
        registry.start(_spec(name="api-2"))
    assert len(procs) == 1


def test_start_reuses_local_port_of_exited_forward() -> None:
    """A dead forward no longer holds its local port — restarts must work."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    registry.start(_spec())
    procs[0].exit(1)
    record = registry.start(_spec(name="api-2"))
    assert record.status == "alive"
    assert len(procs) == 2


def test_start_forces_down_stopping_process_holding_port() -> None:
    """A stopped-but-stubborn kubectl must not win the bind race on restart."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _StubbornProc(argv) if not procs else _FakeProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    first = registry.start(_spec())
    registry.stop(first.id)
    assert procs[0].poll() is None  # SIGTERM ignored — still holds the port
    record = registry.start(_spec(name="api-2"))
    assert procs[0].killed
    assert procs[0].waited
    assert record.status == "alive"
    assert len(procs) == 2


def test_reattach_rejects_port_claimed_by_live_forward() -> None:
    """Re-attach must not report success when it would just lose the bind race."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    broken = registry.start(_spec())
    procs[0].exit(1)
    registry.refresh()
    claimer = registry.start(_spec(name="api-2"))
    with pytest.raises(ValueError, match=f"local port 8080 already forwarded by #{claimer.id}"):
        registry.reattach(broken.id)
    assert broken.status == "broken"
    assert len(procs) == 2


class _GatedStream:
    """File-like stdout whose lines are fed by the test (None ends the stream)."""

    def __init__(self) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()

    def __iter__(self) -> _GatedStream:
        return self

    def __next__(self) -> str:
        line = self._lines.get()
        if line is None:
            raise StopIteration
        return line

    def feed(self, line: str | None) -> None:
        self._lines.put(line)


def _piped_registry(procs: list[_FakeProc]) -> ForwardRegistry:
    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        proc.stdout = _GatedStream()
        procs.append(proc)
        return proc

    return ForwardRegistry(popen=_popen)


def test_start_is_not_alive_until_kubectl_reports_ready() -> None:
    """Popen returning only proves the child exists — not a working forward."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    assert record.status == "starting"
    registry.refresh()
    assert record.status == "starting"
    procs[0].stdout.feed("Forwarding from 127.0.0.1:8080 -> 80\n")
    assert registry.wait_ready(record.id, timeout=2.0) == "alive"


def test_wait_ready_reports_failed_start_with_kubectl_detail() -> None:
    """An exit before the ready line is a failed start, with kubectl's words."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].stdout.feed("error: address already in use\n")
    procs[0].stdout.feed(None)
    procs[0].exit(1)
    assert registry.wait_ready(record.id, timeout=2.0) == "broken"
    deadline = monotonic() + 2.0
    while not record.last_output and monotonic() < deadline:
        time.sleep(0.01)
    assert "address already in use" in record.last_output


def test_wait_ready_times_out_while_kubectl_stays_silent() -> None:
    """A silent child that has not exited is still starting, not broken."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    assert registry.wait_ready(record.id, timeout=0.1) == "starting"
    assert record.status == "starting"


def test_reattach_goes_through_readiness_again() -> None:
    """The replacement kubectl gets the same handshake as a fresh start."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].stdout.feed("Forwarding from 127.0.0.1:8080 -> 80\n")
    assert registry.wait_ready(record.id, timeout=2.0) == "alive"
    procs[0].exit(1)
    registry.refresh()
    assert record.status == "broken"
    revived = registry.reattach(record.id)
    assert revived is not None
    assert revived.status == "starting"
    procs[1].stdout.feed("Forwarding from 127.0.0.1:8080 -> 80\n")
    assert registry.wait_ready(record.id, timeout=2.0) == "alive"


def test_reattach_port_check_ignores_peer_that_just_died() -> None:
    """A stale 'alive' peer that already exited must not block a re-attach."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    broken = registry.start(_spec())
    procs[0].exit(1)
    registry.refresh()
    registry.start(_spec(name="api-2"))  # claims 8080
    procs[1].exit(1)  # dies, but no refresh runs before the re-attach
    revived = registry.reattach(broken.id)
    assert revived is not None


def test_reattach_ignores_late_output_from_the_dead_process() -> None:
    """A dead process's buffered chatter must not mark its replacement alive."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].stdout.feed("Forwarding from 127.0.0.1:8080 -> 80\n")
    assert registry.wait_ready(record.id, timeout=2.0) == "alive"
    procs[0].exit(1)
    registry.refresh()
    assert record.status == "broken"
    registry.reattach(record.id)
    assert record.status == "starting"
    # The dead process's pipe flushes buffered output after the re-attach.
    procs[0].stdout.feed("Forwarding from 127.0.0.1:8080 -> 80\n")
    procs[0].stdout.feed(None)
    assert registry.wait_ready(record.id, timeout=0.3) == "starting"
    assert record.last_output == ""
    # The replacement's own handshake still resolves normally.
    procs[1].stdout.feed("Forwarding from 127.0.0.1:8080 -> 80\n")
    assert registry.wait_ready(record.id, timeout=2.0) == "alive"


def test_candidate_ports_skip_non_tcp_protocols() -> None:
    """kubectl port-forward is TCP-only — UDP/SCTP declarations are unusable."""
    svc = {
        "spec": {
            "ports": [
                {"port": 53, "protocol": "UDP"},
                {"port": 80, "protocol": "TCP"},
                {"port": 132, "protocol": "SCTP"},
                {"port": 443},  # no protocol declared — defaults to TCP
            ]
        }
    }
    assert candidate_remote_ports("services", svc) == [80, 443]
    pod = {
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "ports": [
                        {"containerPort": 53, "protocol": "UDP"},
                        {"containerPort": 8080, "protocol": "TCP"},
                        {"containerPort": 9090},
                    ],
                }
            ]
        }
    }
    assert candidate_remote_ports("pods", pod) == [8080, 9090]


def test_fail_start_aborts_silent_child_but_keeps_it_listed() -> None:
    """A handshake that never resolves is failed explicitly, not guessed ready."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    assert registry.wait_ready(record.id, timeout=0.05) == "starting"
    failed = registry.fail_start(record.id)
    assert failed is record
    assert record.status == "broken"
    assert procs[0].terminated
    # Stays listed so :pf can show the failure and offer a re-attach.
    assert registry.get(record.id) is record
    procs[0].stdout.feed(None)  # release the reader thread
    revived = registry.reattach(record.id)
    assert revived is record
    assert revived.status == "starting"
    procs[1].stdout.feed(None)


def test_fail_start_leaves_resolved_forwards_alone() -> None:
    """A confirmed (``alive``) forward can never be aborted by fail_start()."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].stdout.feed("Forwarding from 127.0.0.1:8080 -> 80\n")
    assert registry.wait_ready(record.id, timeout=2.0) == "alive"
    assert registry.fail_start(record.id) is None
    assert record.status == "alive"
    assert not procs[0].terminated
    assert registry.fail_start(999) is None
    procs[0].stdout.feed(None)


def test_fail_start_stops_a_child_that_eofed_but_still_runs() -> None:
    """EOF fails the start while the child may still run and hold the port."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].stdout.feed("error: unable to listen\n")
    # Stream closes while poll() still reports the child as running.
    procs[0].stdout.feed(None)
    assert registry.wait_ready(record.id, timeout=2.0) == "broken"
    failed = registry.fail_start(record.id)
    assert failed is record
    assert procs[0].terminated, "the lingering child was never signalled down"
    # Stays listed so :pf can show the failure and offer a re-attach.
    assert registry.get(record.id) is record


def test_start_gives_up_on_port_holder_that_cannot_be_reaped() -> None:
    """A kill-immune child must fail the new spawn, not block the caller."""
    procs: list[_FakeProc] = []

    class _WedgedProc(_StubbornProc):
        def kill(self) -> None:
            self.killed = True  # SIGKILL sent; the child is stuck in the kernel

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout or 0.0)

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _WedgedProc(argv) if not procs else _FakeProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    first = registry.start(_spec())
    registry.stop(first.id)
    with pytest.raises(ValueError, match="still being released"):
        registry.start(_spec(name="api-2"))
    assert procs[0].killed


class _SignallingEvent(threading.Event):
    """Readiness-event double that reports when wait() was entered."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()

    def wait(self, timeout: float | None = None) -> bool:
        self.entered.set()
        return super().wait(timeout)


def _blocked_waiter(
    registry: ForwardRegistry, record: ForwardRecord
) -> tuple[threading.Thread, list[str]]:
    """A thread provably blocked in wait_ready() on ``record``'s handshake."""
    gate = _SignallingEvent()
    record._ready = gate
    results: list[str] = []

    def _wait() -> None:
        results.append(registry.wait_ready(record.id, timeout=30.0))

    waiter = threading.Thread(target=_wait)
    waiter.start()
    assert gate.entered.wait(5.0), "waiter never blocked on the readiness event"
    return waiter, results


def test_eof_before_exit_is_a_failed_start() -> None:
    """kubectl closing stdout fails the handshake before poll() sees the exit."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].stdout.feed("error: pod not found\n")
    # EOF arrives while poll() still reports the child as running.
    procs[0].stdout.feed(None)
    assert registry.wait_ready(record.id, timeout=2.0) == "broken"
    assert "pod not found" in record.last_output


def test_refresh_releases_waiter_when_a_starting_child_dies_silently() -> None:
    """Liveness polling must fail a blocked handshake, not leave it to time out."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    waiter, results = _blocked_waiter(registry, record)
    # The child dies without flushing EOF — its reader thread stays blocked,
    # so only the poll can notice and must release the stranded waiter.
    procs[0].exit(1)
    registry.refresh()
    waiter.join(timeout=5.0)
    assert not waiter.is_alive(), "waiter still blocked after refresh marked it broken"
    assert results == ["broken"]
    procs[0].stdout.feed(None)


def test_reattach_releases_the_previous_generations_waiter() -> None:
    """A superseded readiness waiter must not sit out its full timeout."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    waiter, results = _blocked_waiter(registry, record)
    procs[0].exit(1)
    # Mark the record broken without waking the waiter — the swap itself must
    # be sufficient to release it, whatever path flipped the status.
    record.status = "broken"
    assert registry.reattach(record.id) is record
    waiter.join(timeout=5.0)
    assert not waiter.is_alive(), "superseded waiter still blocked after re-attach"
    assert results == ["starting"]  # the replacement generation, still unconfirmed
    procs[0].stdout.feed(None)
    procs[1].stdout.feed(None)
