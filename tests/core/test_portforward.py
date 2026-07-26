"""Tests for the session-scoped port-forward registry (issue #38)."""

from __future__ import annotations

from typing import Any

import pytest

from korvid.core.portforward import ForwardRegistry, ForwardSpec


class _FakeProc:
    """Stand-in for a kubectl port-forward subprocess."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

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
