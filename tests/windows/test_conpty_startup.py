"""Portable regressions for ConPTY startup cleanup."""

from __future__ import annotations

import ctypes
import shutil
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from tests.windows import conpty


class _FailingProcess(conpty.ConPtyProcess):
    def __init__(self, **kwargs: Any) -> None:
        raise RuntimeError("constructor failed")


class _DiscardKernel:
    def WaitForSingleObject(self, handle: int, timeout: int) -> int:
        return conpty._WAIT_OBJECT_0


class _DiscardApi:
    def __init__(self) -> None:
        self.kernel32 = _DiscardKernel()
        self.closed: list[int | None] = []

    def close_handle(self, handle: int | None) -> None:
        self.closed.append(handle)
        if handle == 40:
            raise OSError("process close failed")
        if handle == 50:
            raise OSError("job close failed")


class _PseudoKernel:
    def __init__(self, events: list[tuple[str, int]]) -> None:
        self._events = events

    def ClosePseudoConsole(self, hpc: int) -> None:
        self._events.append(("pseudo", hpc))


class _PseudoApi:
    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []
        self.kernel32 = _PseudoKernel(self.events)

    def close_handle(self, handle: int | None) -> None:
        assert handle is not None
        self.events.append(("handle", handle))
        if handle == 10:
            raise OSError("input close failed")
        if handle == 30:
            raise OSError("output close failed")


class _SpawnKernel:
    def __init__(self) -> None:
        self.terminated_jobs: list[int] = []

    def CreateProcessW(self, *_args: object) -> bool:
        process_pointer = cast(Any, _args[-1])
        process = ctypes.cast(process_pointer, ctypes.POINTER(conpty._ProcessInformation)).contents
        process.hProcess = 40
        process.hThread = 41
        process.dwProcessId = 1234
        return True

    def AssignProcessToJobObject(self, job_handle: int, process_handle: int) -> bool:
        return True

    def ResumeThread(self, thread_handle: object) -> int:
        return 1

    def DeleteProcThreadAttributeList(self, attribute_list: object) -> None:
        return None

    def WaitForSingleObject(self, handle: int, timeout: int) -> int:
        if timeout == 0:
            return conpty._WAIT_TIMEOUT
        return conpty._WAIT_OBJECT_0

    def TerminateJobObject(self, job_handle: int, exit_code: int) -> bool:
        self.terminated_jobs.append(job_handle)
        return True


class _SpawnApi:
    def __init__(self) -> None:
        self.kernel32 = _SpawnKernel()
        self.closed: list[int | None] = []

    def close_handle(self, handle: int | None) -> None:
        self.closed.append(handle)
        if handle == 41:
            raise OSError("thread close failed")
        if handle == 40:
            raise OSError("process close failed")


class _PseudoCreateKernel:
    def CreatePseudoConsole(self, *_args: object) -> int:
        return 0x80004005


class _PseudoCreateApi:
    def __init__(self) -> None:
        self.kernel32 = _PseudoCreateKernel()
        self.closed: list[int | None] = []

    def close_handle(self, handle: int | None) -> None:
        self.closed.append(handle)
        if handle == 11:
            raise OSError("host input close failed")


class _PseudoTransferKernel:
    def __init__(self, events: list[tuple[str, int]]) -> None:
        self._events = events

    def CreatePseudoConsole(self, *_args: object) -> int:
        hpc_pointer = cast(Any, _args[-1])
        ctypes.cast(hpc_pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = 20
        return 0

    def ClosePseudoConsole(self, hpc: int) -> None:
        self._events.append(("pseudo", hpc))


class _PseudoTransferApi:
    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []
        self.kernel32 = _PseudoTransferKernel(self.events)

    def close_handle(self, handle: int | None) -> None:
        assert handle is not None
        self.events.append(("handle", handle))
        if handle == 10:
            raise OSError("internal input close failed")


class _JobCreateKernel:
    def CreateJobObjectW(self, security: object | None, name: object | None) -> int:
        return 50

    def SetInformationJobObject(self, *_args: object) -> bool:
        return False


class _JobCreateApi:
    def __init__(self) -> None:
        self.kernel32 = _JobCreateKernel()
        self.closed: list[int | None] = []

    def close_handle(self, handle: int | None) -> None:
        self.closed.append(handle)
        raise OSError("job close failed")


@pytest.fixture
def artifact_path() -> Iterator[Path]:
    root = Path("tests/windows") / f".conpty-artifact-{uuid.uuid4().hex}"
    root.mkdir()
    try:
        yield root / "conpty-output.bin"
    finally:
        shutil.rmtree(root)


def _artifact_process(path: Path) -> conpty.ConPtyProcess:
    process = object.__new__(conpty.ConPtyProcess)
    process._pseudo = conpty._PseudoConsole(hpc=20, input_handle=10, output_handle=30)
    process._pid = 1234
    process._artifact_path = path
    process._artifact_error = None
    process._artifact_dirty = False
    process._artifact_next_flush = 0.0
    process._artifact_lock = threading.Lock()
    process._reader_error = None
    process._reader_stop = threading.Event()
    process.transcript = conpty.BoundedTranscript(limit=256 * 1024)
    return process


def test_constructor_failure_preserves_error_and_attempts_all_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = object()
    pseudo = conpty._PseudoConsole(hpc=20, input_handle=10, output_handle=30)
    spawned = conpty._SpawnedProcess(process_handle=40, job_handle=50, pid=1234)
    events: list[str] = []

    monkeypatch.setattr(conpty, "_WindowsApi", lambda: api)
    monkeypatch.setattr(conpty, "_create_pseudoconsole", lambda *_args: pseudo)
    monkeypatch.setattr(conpty, "_spawn_process", lambda *_args: spawned)

    def discard(*_args: object) -> None:
        events.append("discard")
        raise OSError("discard failed")

    def close_pseudo(*_args: object) -> None:
        events.append("pseudo")
        raise OSError("pseudo failed")

    monkeypatch.setattr(conpty, "_discard_spawned_process", discard)
    monkeypatch.setattr(conpty, "_close_pseudoconsole", close_pseudo)

    with pytest.raises(RuntimeError, match="constructor failed") as exc_info:
        _FailingProcess.start(
            ["python"],
            cwd=Path("."),
            env={},
            columns=80,
            rows=24,
            capture_limit=32,
            artifact_path=None,
        )

    assert events == ["discard", "pseudo"]
    assert getattr(exc_info.value, "__notes__", []) == [
        "Additional cleanup error: discard failed",
        "Additional cleanup error: pseudo failed",
    ]


def test_discard_closes_job_when_process_handle_close_fails() -> None:
    api = _DiscardApi()
    spawned = conpty._SpawnedProcess(process_handle=40, job_handle=50, pid=1234)

    with pytest.raises(OSError, match="process close failed") as exc_info:
        conpty._discard_spawned_process(cast(Any, api), spawned)

    assert api.closed == [40, 50]
    assert getattr(exc_info.value, "__notes__", []) == [
        "Additional cleanup error: job close failed"
    ]


def test_pseudoconsole_cleanup_continues_after_input_close_failure() -> None:
    api = _PseudoApi()
    pseudo = conpty._PseudoConsole(hpc=20, input_handle=10, output_handle=30)

    with pytest.raises(OSError, match="input close failed") as exc_info:
        conpty._close_pseudoconsole(cast(Any, api), pseudo)

    assert api.events == [("handle", 10), ("pseudo", 20), ("handle", 30)]
    assert getattr(exc_info.value, "__notes__", []) == [
        "Additional cleanup error: output close failed"
    ]


def test_spawn_thread_close_failure_discards_unreturned_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _SpawnApi()
    storage = ctypes.create_string_buffer(1)
    pointer = ctypes.cast(storage, ctypes.c_void_p)
    monkeypatch.setattr(conpty, "_attribute_list", lambda *_args: (storage, pointer))
    monkeypatch.setattr(conpty, "_create_kill_job", lambda *_args: 50)

    with pytest.raises(OSError, match="thread close failed") as exc_info:
        conpty._spawn_process(
            cast(Any, api),
            conpty._PseudoConsole(hpc=20, input_handle=10, output_handle=30),
            ["python"],
            Path("."),
            {},
        )

    assert api.closed == [41, 40, 50]
    assert api.kernel32.terminated_jobs == [50]
    assert getattr(exc_info.value, "__notes__", []) == [
        "Additional cleanup error: process close failed"
    ]


@pytest.mark.parametrize(
    "spawn_error",
    [OSError("spawn failed"), ValueError("spawn failed")],
)
def test_spawn_failure_preserves_primary_error_when_pseudoconsole_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    spawn_error: OSError | ValueError,
) -> None:
    api = object()
    pseudo = conpty._PseudoConsole(hpc=20, input_handle=10, output_handle=30)
    monkeypatch.setattr(conpty, "_WindowsApi", lambda: api)
    monkeypatch.setattr(conpty, "_create_pseudoconsole", lambda *_args: pseudo)

    def fail_spawn(*_args: object) -> None:
        raise spawn_error

    def fail_cleanup(*_args: object) -> None:
        raise OSError("pseudo cleanup failed")

    monkeypatch.setattr(conpty, "_spawn_process", fail_spawn)
    monkeypatch.setattr(conpty, "_close_pseudoconsole", fail_cleanup)

    with pytest.raises(type(spawn_error), match="spawn failed") as exc_info:
        conpty.ConPtyProcess.start(
            ["python"],
            cwd=Path("."),
            env={},
            columns=80,
            rows=24,
            capture_limit=32,
            artifact_path=None,
        )

    assert getattr(exc_info.value, "__notes__", []) == [
        "Additional cleanup error: pseudo cleanup failed"
    ]


def test_pseudoconsole_creation_failure_preserves_error_and_closes_all_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _PseudoCreateApi()
    pipes = iter(((10, 11), (20, 21)))
    monkeypatch.setattr(conpty, "_create_pipe", lambda _api: next(pipes))

    with pytest.raises(OSError, match="CreatePseudoConsole") as exc_info:
        conpty._create_pseudoconsole(cast(Any, api), 80, 24)

    assert api.closed == [11, 20, 10, 21]
    assert getattr(exc_info.value, "__notes__", []) == [
        "Additional cleanup error: host input close failed"
    ]


def test_pseudoconsole_transfer_failure_closes_created_console_and_host_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _PseudoTransferApi()
    pipes = iter(((10, 11), (30, 31)))
    monkeypatch.setattr(conpty, "_create_pipe", lambda _api: next(pipes))

    with pytest.raises(OSError, match="internal input close failed"):
        conpty._create_pseudoconsole(cast(Any, api), 80, 24)

    assert api.events == [
        ("handle", 10),
        ("handle", 31),
        ("handle", 11),
        ("pseudo", 20),
        ("handle", 30),
    ]


def test_kill_job_creation_preserves_configuration_error_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _JobCreateApi()
    monkeypatch.setattr(conpty, "_last_error", lambda: 1234)

    with pytest.raises(OSError, match="SetInformationJobObject") as exc_info:
        conpty._create_kill_job(cast(Any, api))

    assert exc_info.value.errno == 1234
    assert api.closed == [50]
    assert getattr(exc_info.value, "__notes__", []) == [
        "Additional cleanup error: job close failed"
    ]


def test_reader_periodically_snapshots_dirty_output_before_close(
    monkeypatch: pytest.MonkeyPatch,
    artifact_path: Path,
) -> None:
    process = _artifact_process(artifact_path)
    output = [b"first", b"second", b"", b""]
    clock = iter((0.0, 0.1, 0.2, 0.3))

    def read_available(_handle: int) -> bytes:
        data = output.pop(0)
        if not output:
            process._reader_stop.set()
        return data

    monkeypatch.setattr(process, "_read_available", read_available)
    monkeypatch.setattr("tests.windows.conpty.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("tests.windows.conpty.time.sleep", lambda _seconds: None)

    process._read_output()

    assert artifact_path.read_bytes() == b"firstsecond"
    assert process.transcript.tail() == b"firstsecond"


def test_artifact_snapshot_atomically_replaces_with_bounded_tail(
    artifact_path: Path,
) -> None:
    process = _artifact_process(artifact_path)
    artifact_path.write_bytes(b"stale")
    process.transcript.append(b"a" * (128 * 1024) + b"new-tail")

    process._write_artifact()

    assert artifact_path.read_bytes() == process.transcript.tail()[-128 * 1024 :]
    assert artifact_path.stat().st_size == 128 * 1024
    assert list(artifact_path.parent.iterdir()) == [artifact_path]


def test_artifact_snapshot_uses_bounded_lock_acquisition(
    monkeypatch: pytest.MonkeyPatch, artifact_path: Path
) -> None:
    class HeldLock:
        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.released = False

        def __enter__(self) -> None:
            raise AssertionError("unbounded snapshot lock acquisition")

        def __exit__(self, *args: object) -> None:
            pass

        def acquire(self, *, timeout: float) -> bool:
            self.timeouts.append(timeout)
            return False

        def release(self) -> None:
            self.released = True

    process = _artifact_process(artifact_path)
    lock = HeldLock()
    monkeypatch.setattr(process, "_artifact_lock", lock)

    with pytest.raises(TimeoutError, match="snapshot lock"):
        process._write_artifact()

    assert lock.timeouts == [1.0]
    assert not lock.released


def test_artifact_write_failure_keeps_draining_and_surfaces_on_close(
    monkeypatch: pytest.MonkeyPatch,
    artifact_path: Path,
) -> None:
    missing_path = artifact_path.parent / "missing" / artifact_path.name
    process = _artifact_process(missing_path)
    output: list[bytes | None] = [b"first", b"second", None]

    monkeypatch.setattr(process, "_read_available", lambda _handle: output.pop(0))
    process._read_output()

    assert process.transcript.tail() == b"firstsecond"
    assert process._artifact_error is not None

    process._process_handle = None
    process._job_handle = None
    process._reader = cast(
        Any,
        type(
            "StoppedReader",
            (),
            {
                "is_alive": lambda self: False,
                "join": lambda self, timeout: None,
            },
        )(),
    )
    monkeypatch.setattr(process, "_terminate_if_running", lambda: None)
    monkeypatch.setattr(process, "_close_pseudo_input", lambda: None)
    monkeypatch.setattr(process, "_close_pseudoconsole", lambda: None)
    monkeypatch.setattr(process, "_close_pseudo_output", lambda: None)

    with pytest.raises(OSError, match="artifact snapshot failed"):
        process.close()
