"""Tests for the session-scoped port-forward registry (issue #38)."""

from __future__ import annotations

from time import monotonic
from typing import Any
from unittest.mock import patch

import pytest

from korvid.core.portforward import ForwardRegistry, ForwardSpec, candidate_remote_ports


class _FakeProc:
    """Stand-in for a kubectl port-forward subprocess."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.waited = False

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
