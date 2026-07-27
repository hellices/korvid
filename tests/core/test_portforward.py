"""Tests for the session-scoped port-forward registry (issue #38)."""

from __future__ import annotations

import queue
import subprocess
import threading
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


def test_retarget_reopens_registry_for_new_context() -> None:
    """`:ctx` switching (issue #36): after teardown stop_all() + retarget(),
    new forwards must start (registry reopened) against the new cluster."""
    procs: list[_FakeProc] = []
    registry = _registry(procs, context="ctx-a")
    registry.start(_spec())
    registry.stop_all()
    registry.retarget("ctx-b")
    registry.start(_spec(local_port=9090))
    idx = procs[-1].argv.index("--context")
    assert procs[-1].argv[idx + 1] == "ctx-b"


def test_retarget_discards_spawn_from_before_the_switch() -> None:
    """A spawn in flight when stop_all() ran targets the old cluster: even
    if it lands after retarget() reopened the registry, it is discarded."""
    procs: list[_FakeProc] = []
    registry = _registry(procs, context="ctx-a")
    original_spawn = registry._spawn

    def racing_spawn(spec: ForwardSpec) -> Any:
        proc = original_spawn(spec)
        registry.stop_all()  # the context switch wins the race mid-spawn
        registry.retarget("ctx-b")
        return proc

    registry._spawn = racing_spawn  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="shut down"):
        registry.start(_spec())
    assert procs[0].terminated or procs[0].killed
    assert registry.forwards() == []


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


def test_reattach_can_retarget_at_the_owning_workload() -> None:
    """A vanished pod's forward follows its workload — kubectl resolves the
    replacement pod when the target is a workload (issue #38)."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    record = registry.start(_spec(workload="deployments/api"))
    procs[0].exit(1)
    registry.refresh()
    revived = registry.reattach(record.id, retarget=True)
    assert revived is record
    assert "deployment/api" in procs[1].argv
    # The record now describes the workload target the forward actually runs.
    assert record.spec.kind == "deployments"
    assert record.spec.name == "api"
    assert record.spec.local_port == 8080  # port mapping is untouched
    assert record.status == "alive"


def test_reattach_retarget_without_a_workload_is_refused() -> None:
    """No recorded owner means nothing to follow — no blind respawn."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    record = registry.start(_spec())
    procs[0].exit(1)
    registry.refresh()
    assert registry.reattach(record.id, retarget=True) is None
    assert len(procs) == 1
    assert record.status == "broken"  # still listed, still re-attachable


def test_controller_owner_reads_the_controlling_reference() -> None:
    from korvid.core.portforward import controller_owner

    manifest = {
        "metadata": {
            "ownerReferences": [
                {"kind": "Node", "name": "n1"},  # not a controller
                {"kind": "ReplicaSet", "name": "api-6d5f", "controller": True},
            ]
        }
    }
    assert controller_owner(manifest) == ("ReplicaSet", "api-6d5f")
    assert controller_owner({}) is None
    assert controller_owner({"metadata": {"ownerReferences": "bogus"}}) is None
    assert controller_owner({"metadata": {"ownerReferences": [{"controller": True}]}}) is None


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


def test_start_reaps_a_broken_but_running_child_on_the_same_port() -> None:
    """A broken record's still-running child must not win the new bind race."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].stdout.feed("error: unable to listen\n")
    # Stream closes while poll() still reports the child as running.
    procs[0].stdout.feed(None)
    assert registry.wait_ready(record.id, timeout=2.0) == "broken"
    fresh = registry.start(_spec(name="api-2"))  # same local port
    assert procs[0].terminated, "the broken record's child still held the port"
    assert fresh.status == "starting"
    procs[1].stdout.feed(None)


def test_stop_all_reaps_stragglers_under_one_shared_deadline() -> None:
    """Multiple unreapable children must not stack their kill timeouts."""
    now = {"t": 0.0}

    class _SlowUnreapableProc(_StubbornProc):
        def __init__(self, argv: list[str]) -> None:
            super().__init__(argv)
            self.wait_calls: list[float | None] = []

        def kill(self) -> None:
            self.killed = True  # SIGKILL sent; the child is stuck in the kernel

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls.append(timeout)
            now["t"] += timeout or 0.0  # reaping this child burns wall time
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout or 0.0)

    procs: list[_SlowUnreapableProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _SlowUnreapableProc:
        proc = _SlowUnreapableProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    registry.start(_spec())
    registry.start(_spec(local_port=9090))

    def _sleep(seconds: float) -> None:
        now["t"] += seconds

    with (
        patch("korvid.core.portforward.monotonic", side_effect=lambda: now["t"]),
        patch("korvid.core.portforward.time.sleep", side_effect=_sleep),
    ):
        records = registry.stop_all()
    assert len(records) == 2
    assert all(p.killed for p in procs)
    # The first wedged child consumed the shared reap budget — the second
    # must not be granted a fresh timeout of its own.
    waits = [w for p in procs for w in p.wait_calls]
    assert len(waits) == 1


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


def test_record_is_never_published_as_alive_before_its_handshake() -> None:
    """The status must be ``starting`` at registration, not downgraded after.

    A record inserted with the dataclass default ``alive`` and flipped to
    ``starting`` afterwards would let a concurrent `:pf` poll observe an
    unconfirmed kubectl as a success.
    """
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    published: list[str] = []

    class _SpyRecords(dict[int, ForwardRecord]):
        def __setitem__(self, key: int, value: ForwardRecord) -> None:
            published.append(value.status)
            super().__setitem__(key, value)

    registry._records = _SpyRecords()
    registry.start(_spec())
    assert published == ["starting"]
    procs[0].stdout.feed(None)  # release the reader thread


def test_reattach_publishes_the_replacement_already_starting() -> None:
    """The swap must install the ``starting`` state atomically.

    A replacement published while the record still reads ``broken`` is a
    reclaim target for a concurrent same-port start — which would signal
    the fresh process down.
    """
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].stdout.feed(None)
    procs[0].exit(1)
    registry.refresh()
    assert record.status == "broken"
    observed: list[str] = []
    original = registry._start_watcher

    def _spy(rec: ForwardRecord, *args: Any) -> None:
        observed.append(rec.status)
        original(rec, *args)

    registry._start_watcher = _spy  # type: ignore[assignment]  # test spy
    assert registry.reattach(record.id) is record
    assert observed == ["starting"]
    procs[1].stdout.feed(None)  # release the reader thread


def test_refresh_wakes_only_the_generation_it_marked_broken() -> None:
    """The waiter release must target the event snapshotted with the broken
    transition — a re-attach landing in between installs the replacement's
    fresh event, and waking that one fails a valid replacement early."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].returncode = 1  # died silently — refresh() will mark it broken
    original = registry._release_waiters

    def _ambush(arg: Any) -> None:
        if len(procs) == 1:
            # A re-attach lands between the broken transition and the wake.
            registry.reattach(record.id)
        original(arg)

    registry._release_waiters = _ambush  # type: ignore[assignment]  # test spy
    registry.refresh()
    assert record._proc is procs[1]  # the re-attach adopted its replacement
    assert record._ready is not None
    assert not record._ready.is_set()  # the replacement's handshake still pends
    procs[0].stdout.feed(None)
    procs[1].stdout.feed(None)


def test_delayed_initial_watcher_stays_bound_to_its_own_generation() -> None:
    """A start whose watcher launch is delayed past a re-attach must read its
    own (dead) child's stream — a second reader on the replacement's stream
    could split its readiness line and EOF, breaking a valid forward."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    watched: list[Any] = []
    both_watching = threading.Event()
    original_watch = registry._watch_output

    def _spy_watch(record: ForwardRecord, proc: Any, stream: Any, ready: Any) -> None:
        watched.append(stream)
        if len(watched) == 2:
            both_watching.set()
        original_watch(record, proc, stream, ready)

    registry._watch_output = _spy_watch  # type: ignore[method-assign]  # test spy
    original_start = registry._start_watcher

    def _delayed(rec: ForwardRecord, *args: Any) -> None:
        if len(procs) == 1:
            # The initial child dies and a re-attach adopts a replacement
            # before the initial start's thread resumes launching here.
            procs[0].returncode = 1
            registry.reattach(rec.id)
        original_start(rec, *args)

    registry._start_watcher = _delayed  # type: ignore[assignment]  # test spy
    record = registry.start(_spec())
    # One watcher per generation — signalled deterministically by the spy.
    assert both_watching.wait(2.0)
    # One reader per generation, each on its own stream — never two on the
    # replacement's.
    assert {id(stream) for stream in watched} == {
        id(procs[0].stdout),
        id(procs[1].stdout),
    }
    assert registry.get(record.id) is record
    procs[0].stdout.feed(None)
    procs[1].stdout.feed(None)


def test_wait_ready_reports_failed_start_with_kubectl_detail() -> None:
    """An exit before the ready line is a failed start, with kubectl's words."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].stdout.feed("error: address already in use\n")
    procs[0].stdout.feed(None)
    procs[0].exit(1)
    # The watcher records the error line before it resolves readiness, so a
    # returned "broken" already implies last_output is written.
    assert registry.wait_ready(record.id, timeout=2.0) == "broken"
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
    assert failed == "aborted"
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
    assert registry.fail_start(record.id) == "alive"
    assert record.status == "alive"
    assert not procs[0].terminated
    assert registry.fail_start(999) == "gone"
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
    assert failed == "aborted"
    assert procs[0].terminated, "the lingering child was never signalled down"
    # Stays listed so :pf can show the failure and offer a re-attach.
    assert registry.get(record.id) is record


def test_fail_start_can_drop_a_start_that_never_worked() -> None:
    """`keep=False` aborts the handshake and unlists the forward atomically."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    assert registry.wait_ready(record.id, timeout=0.05) == "starting"
    failed = registry.fail_start(record.id, keep=False)
    assert failed == "aborted"
    assert record.status == "broken"
    assert procs[0].terminated
    assert registry.get(record.id) is None  # never worked — not offered for re-attach
    procs[0].stdout.feed(None)  # release the reader thread


def test_fail_start_keep_false_yields_to_a_confirmed_forward() -> None:
    """Even with `keep=False` a last-instant confirmation wins: no teardown."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].stdout.feed("Forwarding from 127.0.0.1:8080 -> 80\n")
    assert registry.wait_ready(record.id, timeout=2.0) == "alive"
    assert registry.fail_start(record.id, keep=False) == "alive"
    assert registry.get(record.id) is record  # still listed, still alive
    assert not procs[0].terminated
    procs[0].stdout.feed(None)


def test_fail_start_from_a_superseded_generation_leaves_the_replacement_alone() -> None:
    """A stale confirmation's abort must not tear down the re-attached process."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    record = registry.start(_spec())
    stale_generation = registry.generation(record.id)
    procs[0].exit(1)
    registry.refresh()
    assert record.status == "broken"
    assert registry.reattach(record.id) is record  # bumps the generation
    assert record.status == "alive"
    assert registry.fail_start(record.id, generation=stale_generation) == "superseded"
    assert record.status == "alive"  # the replacement was left untouched
    assert not procs[1].terminated


def test_fail_start_with_the_current_generation_still_aborts() -> None:
    """The generation guard rejects only stale callers, not the real timeout."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    assert registry.wait_ready(record.id, timeout=0.05) == "starting"
    generation = registry.generation(record.id)
    assert registry.fail_start(record.id, generation=generation) == "aborted"
    assert record.status == "broken"
    assert procs[0].terminated
    procs[0].stdout.feed(None)  # release the reader thread


def test_fail_start_racing_a_reattach_spares_the_replacement() -> None:
    """The abort signals the generation it validated, never a just-adopted replacement."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    procs[0].stdout.feed(None)  # EOF: broken while the child still runs
    assert registry.wait_ready(record.id, timeout=2.0) == "broken"
    generation = registry.generation(record.id)
    assert generation is not None
    ambushes: list[int] = []

    class _AmbushDict(dict[int, ForwardRecord]):
        """Interleaves a re-attach right before fail_start unlists the record."""

        def pop(self, key: int, default: ForwardRecord | None = None) -> ForwardRecord | None:  # type: ignore[override]  # test seam
            if not ambushes:
                ambushes.append(key)
                registry.reattach(key)
            return super().pop(key, default)

    registry._records = _AmbushDict(registry._records)
    registry.fail_start(record.id, keep=False, generation=generation)
    assert ambushes == [record.id]
    assert len(procs) == 2
    assert not procs[1].terminated  # the replacement was never the abort's target
    assert not procs[1].killed
    procs[1].stdout.feed(None)  # release the replacement's reader thread


def test_fail_start_racing_a_stop_defers_to_the_stop() -> None:
    """An abort whose record was stopped between its lock-free lookup and its
    lock acquisition must report nothing — the deliberate stop's outcome
    stands, not a spurious failed-start report."""
    procs: list[_FakeProc] = []
    registry = _piped_registry(procs)
    record = registry.start(_spec())
    stops: list[int] = []

    class _StopAmbushDict(dict[int, ForwardRecord]):
        """Interleaves a stop right after fail_start's lock-free lookup."""

        def get(  # type: ignore[override]  # test seam
            self, key: int, default: ForwardRecord | None = None
        ) -> ForwardRecord | None:
            found = super().get(key, default)
            if not stops and found is not None:
                stops.append(key)
                registry.stop(key)
            return found

    registry._records = _StopAmbushDict(registry._records)
    assert registry.fail_start(record.id, keep=False) == "gone"
    assert stops == [record.id]
    assert record.status == "starting"  # the abort never mutated the stopped record
    procs[0].stdout.feed(None)  # release the reader thread


def test_start_racing_teardown_never_leaks_a_child() -> None:
    """A spawn that lands after stop_all() is put down, not silently orphaned."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        registry.stop_all()  # teardown wins the race while the spawn is in flight
        proc = _FakeProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    with pytest.raises(ValueError, match="shut down"):
        registry.start(_spec())
    assert procs[0].killed, "the orphaned child was left running past teardown"
    assert registry.forwards() == []


def test_start_after_shutdown_never_spawns_kubectl() -> None:
    """A closed registry must reject a start before Popen — spawning a real
    kubectl just to kill it is an external side effect of a shutdown guard."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    registry.stop_all()
    with pytest.raises(ValueError, match="shut down"):
        registry.start(_spec())
    assert procs == []


def test_reattach_racing_teardown_never_leaks_a_child() -> None:
    """A replacement spawned after stop_all() is discarded, not adopted."""
    procs: list[_FakeProc] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        if procs:  # the replacement spawn — teardown wins the race first
            registry.stop_all()
        proc = _FakeProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    record = registry.start(_spec())
    procs[0].exit(1)
    registry.refresh()
    assert record.status == "broken"
    assert registry.reattach(record.id) is None
    assert procs[1].killed, "the orphaned replacement was left running past teardown"
    assert registry.forwards() == []


def test_concurrent_starts_cannot_claim_the_same_port() -> None:
    """The port check and registration are atomic — the losing start fails fast."""
    procs: list[_FakeProc] = []
    loser: list[ValueError] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        procs.append(proc)
        if len(procs) == 1:
            # A second start on the same port interleaves while this spawn
            # is still in flight — it must fail fast on the claimed port,
            # not spawn a doomed child that loses the bind race later.
            try:
                registry.start(_spec(name="api-2"))
            except ValueError as exc:
                loser.append(exc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    record = registry.start(_spec())
    assert record.status == "alive"
    assert len(procs) == 1, "the losing start spawned a child anyway"
    assert loser, "the losing start did not fail fast"
    assert "8080" in str(loser[0])


def test_failed_spawn_releases_the_port_claim() -> None:
    """A spawn that raises must not leave its port permanently claimed."""

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        raise OSError("kubectl vanished")

    registry = ForwardRegistry(popen=_popen)
    with pytest.raises(OSError, match="kubectl vanished"):
        registry.start(_spec())
    procs: list[_FakeProc] = []

    def _popen_ok(argv: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(argv)
        procs.append(proc)
        return proc

    registry._popen = _popen_ok
    assert registry.start(_spec()).status == "alive"  # port claim was released


def test_non_oserror_spawn_failure_releases_the_port_claim() -> None:
    """An unforwardable kind fails before Popen — the claim must still be released."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    with pytest.raises(ValueError, match="pods, services, and workloads only"):
        registry.start(_spec(kind="configmaps"))
    assert registry.start(_spec()).status == "alive"  # port claim was released


def test_failed_reattach_spawn_releases_the_port_claim() -> None:
    """A replacement spawn raising anything must not leave the port claimed."""
    procs: list[_FakeProc] = []
    calls: list[int] = []

    def _popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("spawn exploded")
        proc = _FakeProc(argv)
        procs.append(proc)
        return proc

    registry = ForwardRegistry(popen=_popen)
    record = registry.start(_spec())
    procs[0].exit(1)
    registry.refresh()
    assert record.status == "broken"
    with pytest.raises(RuntimeError, match="spawn exploded"):
        registry.reattach(record.id)
    assert registry.reattach(record.id) is record  # port claim was released


def test_wait_ready_tells_a_stale_waiter_it_was_superseded() -> None:
    """A waiter woken by a re-attach must not read the replacement's fate as its own."""
    procs: list[_FakeProc] = []
    registry = _registry(procs)
    record = registry.start(_spec())
    procs[0].exit(1)
    registry.refresh()
    assert record.status == "broken"
    waiter, results = _blocked_waiter(registry, record)
    assert registry.reattach(record.id) is record
    waiter.join(timeout=5.0)
    # The replacement is already "alive" here — but that is *its* status,
    # not the superseded generation's, and must not be reported as such.
    assert results == ["superseded"]


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
    # And it learns it was superseded — the replacement's own unconfirmed
    # "starting" state is not this generation's to report.
    assert results == ["superseded"]
    procs[0].stdout.feed(None)
    procs[1].stdout.feed(None)
