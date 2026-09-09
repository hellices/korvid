"""Small stdlib-only Windows ConPTY host used by the native smoke test."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self


class BoundedTranscript:
    """Bounded byte capture for ConPTY diagnostics."""

    def __init__(self, *, limit: int) -> None:
        if limit <= 0:
            raise ValueError("transcript limit must be positive")
        self._limit = limit
        self._data = bytearray()
        self._position = 0
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            self._data.extend(data)
            self._position += len(data)
            excess = len(self._data) - self._limit
            if excess > 0:
                del self._data[:excess]

    def tail(self) -> bytes:
        with self._lock:
            return bytes(self._data)

    def contains(self, value: bytes) -> bool:
        return value in self.tail()

    def position(self) -> int:
        """Return a checkpoint after all bytes appended so far."""
        with self._lock:
            return self._position

    def contains_after(self, value: bytes, position: int) -> bool:
        """Check only output appended after `position`, rejecting stale checkpoints."""
        with self._lock:
            retained_start = self._position - len(self._data)
            if position < retained_start:
                raise ValueError("transcript checkpoint is no longer retained")
            if position > self._position:
                raise ValueError("transcript checkpoint is in the future")
            offset = position - retained_start
            return value in self._data[offset:]

    def count(self, value: bytes) -> int:
        return self.tail().count(value)


class WitnessDirectory:
    """Read phase witnesses emitted by the native child process."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def wait(self, name: str, *, timeout: float) -> dict[str, Any]:
        path = self._root / f"{name}.json"
        deadline = time.monotonic() + timeout
        while True:
            if path.is_file():
                payload: object = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise AssertionError(f"witness {name!r} must contain a JSON object")
                return {str(key): value for key, value in payload.items()}
            if time.monotonic() >= deadline:
                available = sorted(item.stem for item in self._root.glob("*.json"))
                raise AssertionError(
                    f"phase {name!r} not observed within {timeout:.1f}s; available={available}"
                )
            time.sleep(0.02)


class ConPtyCapabilityError(RuntimeError):
    """The Windows host does not expose the required ConPTY APIs."""


class _Coord(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _StartupInfo),
        ("lpAttributeList", wintypes.LPVOID),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESTDHANDLES = 0x00000100
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_WAIT_FAILED = 0xFFFFFFFF
_WAIT_TIMEOUT = 0x00000102
_ERROR_BROKEN_PIPE = 109
_ERROR_INVALID_PARAMETER = 87
_ARTIFACT_LIMIT = 128 * 1024
_ARTIFACT_FLUSH_SECONDS = 0.25


def _handle_value(handle: object) -> int:
    if isinstance(handle, int):
        return handle
    value = getattr(handle, "value", None)
    return 0 if value is None else int(value)


def _platform_attribute(module: object, name: str) -> Any:
    return getattr(module, name)


def _last_error() -> int:
    return int(_platform_attribute(ctypes, "get_last_error")())


def _raise_api_error(operation: str) -> None:
    raise _api_error(operation)


def _api_error(operation: str) -> OSError:
    code = _last_error()
    return OSError(code, f"{operation} failed with Windows error {code}")


def _raise_unexpected_wait_result(operation: str, result: int) -> None:
    if result == _WAIT_FAILED:
        _raise_api_error(operation)
    raise OSError(result, f"{operation} returned unexpected wait result {result}")


class _CleanupErrors:
    def __init__(self) -> None:
        self._first: OSError | RuntimeError | ValueError | None = None

    def attempt(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        except (OSError, RuntimeError) as exc:
            self.add(exc)

    def add(self, error: OSError | RuntimeError | ValueError) -> None:
        if self._first is None:
            self._first = error
        else:
            self._first.add_note(f"Additional cleanup error: {error}")

    @property
    def failed(self) -> bool:
        return self._first is not None

    def raise_first(self) -> None:
        if self._first is not None:
            raise self._first


def _configure_api(kernel32: Any) -> None:
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.CreatePseudoConsole.argtypes = [
        _Coord,
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    kernel32.CreatePseudoConsole.restype = ctypes.c_long
    kernel32.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
    kernel32.ClosePseudoConsole.restype = None
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_size_t,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.LPVOID,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(_StartupInfoEx),
        ctypes.POINTER(_ProcessInformation),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE


class _WindowsApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise ConPtyCapabilityError("ConPTY is available only on Windows")
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise ConPtyCapabilityError("ctypes.WinDLL is unavailable on Windows")
        kernel32 = win_dll("kernel32", use_last_error=True)
        for name in (
            "CreatePseudoConsole",
            "ClosePseudoConsole",
            "InitializeProcThreadAttributeList",
            "UpdateProcThreadAttribute",
        ):
            if not hasattr(kernel32, name):
                raise ConPtyCapabilityError(f"required Windows ConPTY API {name} is unavailable")
        _configure_api(kernel32)
        self.kernel32: Any = kernel32

    def close_handle(self, handle: int | None) -> None:
        if handle not in (None, 0) and not self.kernel32.CloseHandle(handle):
            _raise_api_error("CloseHandle")


@dataclass
class _PseudoConsole:
    hpc: int
    input_handle: int
    output_handle: int


@dataclass
class _SpawnedProcess:
    process_handle: int
    job_handle: int
    pid: int


def _create_pipe(api: _WindowsApi) -> tuple[int, int]:
    read_handle = wintypes.HANDLE()
    write_handle = wintypes.HANDLE()
    if not api.kernel32.CreatePipe(
        ctypes.byref(read_handle),
        ctypes.byref(write_handle),
        None,
        0,
    ):
        _raise_api_error("CreatePipe")
    return _handle_value(read_handle), _handle_value(write_handle)


def _create_pseudoconsole(api: _WindowsApi, columns: int, rows: int) -> _PseudoConsole:
    input_read, input_write = _create_pipe(api)
    output_read: int | None = None
    output_write: int | None = None
    hpc = wintypes.HANDLE()
    pseudo: _PseudoConsole | None = None
    errors = _CleanupErrors()
    try:
        output_read, output_write = _create_pipe(api)
        result = int(
            api.kernel32.CreatePseudoConsole(
                _Coord(columns, rows),
                input_read,
                output_write,
                0,
                ctypes.byref(hpc),
            )
        )
        if result != 0:
            code = result & 0xFFFFFFFF
            raise OSError(code, f"CreatePseudoConsole failed with HRESULT 0x{code:08x}")
        pseudo = _PseudoConsole(_handle_value(hpc), input_write, output_read)
    except (OSError, RuntimeError, ValueError) as error:
        errors.add(error)
        errors.attempt(lambda: api.close_handle(input_write))
        errors.attempt(lambda: api.close_handle(output_read))
    finally:
        errors.attempt(lambda: api.close_handle(input_read))
        errors.attempt(lambda: api.close_handle(output_write))
    if errors.failed:
        if pseudo is not None:
            errors.attempt(lambda: _close_pseudoconsole(api, pseudo))
        errors.raise_first()
    assert pseudo is not None
    return pseudo


def _environment_block(env: Mapping[str, str]) -> ctypes.Array[Any]:
    entries = [
        f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper())
    ]
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0")


def _attribute_list(api: _WindowsApi, hpc: int) -> tuple[ctypes.Array[Any], wintypes.LPVOID]:
    size = ctypes.c_size_t()
    api.kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if size.value == 0:
        _raise_api_error("InitializeProcThreadAttributeList(size)")
    storage = ctypes.create_string_buffer(size.value)
    pointer = ctypes.cast(storage, wintypes.LPVOID)
    if not api.kernel32.InitializeProcThreadAttributeList(pointer, 1, 0, ctypes.byref(size)):
        _raise_api_error("InitializeProcThreadAttributeList")
    hpc_value = wintypes.HANDLE(hpc)
    if not api.kernel32.UpdateProcThreadAttribute(
        pointer,
        0,
        _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        hpc_value,
        ctypes.sizeof(hpc_value),
        None,
        None,
    ):
        errors = _CleanupErrors()
        errors.add(_api_error("UpdateProcThreadAttribute"))
        errors.attempt(lambda: api.kernel32.DeleteProcThreadAttributeList(pointer))
        errors.raise_first()
    return storage, pointer


def _create_kill_job(api: _WindowsApi) -> int:
    handle = _handle_value(api.kernel32.CreateJobObjectW(None, None))
    if handle == 0:
        _raise_api_error("CreateJobObjectW")
    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not api.kernel32.SetInformationJobObject(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        errors = _CleanupErrors()
        errors.add(_api_error("SetInformationJobObject"))
        errors.attempt(lambda: api.close_handle(handle))
        errors.raise_first()
    return handle


def _terminate_created_process(api: _WindowsApi, process_handle: int) -> None:
    if not api.kernel32.TerminateProcess(process_handle, 1):
        _raise_api_error("TerminateProcess")
    _wait_for_terminated_process(api, process_handle)


def _wait_for_terminated_process(api: _WindowsApi, process_handle: int) -> None:
    result = int(api.kernel32.WaitForSingleObject(process_handle, 5_000))
    if result == _WAIT_TIMEOUT:
        raise TimeoutError("Windows process did not exit within 5.0s during cleanup")
    if result != _WAIT_OBJECT_0:
        _raise_unexpected_wait_result("WaitForSingleObject", result)


def _cleanup_unreturned_process(
    api: _WindowsApi,
    process_handle: int | None,
    job_handle: int | None,
    *,
    assigned: bool,
    errors: _CleanupErrors,
) -> None:
    if process_handle is None:
        errors.attempt(lambda: api.close_handle(job_handle))
        return
    if assigned and job_handle is not None:
        spawned = _SpawnedProcess(process_handle, job_handle, 0)
        errors.attempt(lambda: _discard_spawned_process(api, spawned))
        return
    errors.attempt(lambda: _terminate_created_process(api, process_handle))
    errors.attempt(lambda: api.close_handle(process_handle))
    errors.attempt(lambda: api.close_handle(job_handle))


def _spawn_process(
    api: _WindowsApi,
    pseudo: _PseudoConsole,
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
) -> _SpawnedProcess:
    if not argv:
        raise ValueError("ConPTY process argv must not be empty")
    attribute_storage, attribute_pointer = _attribute_list(api, pseudo.hpc)
    process = _ProcessInformation()
    job_handle: int | None = None
    created = False
    assigned = False
    spawned: _SpawnedProcess | None = None
    errors = _CleanupErrors()
    try:
        startup = _StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        # Null standard handles let ConPTY replace the parent's redirected streams.
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.lpAttributeList = attribute_pointer
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
        environment = _environment_block(env)
        job_handle = _create_kill_job(api)
        created = bool(
            api.kernel32.CreateProcessW(
                None,
                command_line,
                None,
                None,
                False,
                _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT | _EXTENDED_STARTUPINFO_PRESENT,
                environment,
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(process),
            )
        )
        if not created:
            _raise_api_error("CreateProcessW")
        process_handle = _handle_value(process.hProcess)
        assert job_handle is not None
        if not api.kernel32.AssignProcessToJobObject(job_handle, process_handle):
            raise _api_error("AssignProcessToJobObject")
        assigned = True
        if api.kernel32.ResumeThread(process.hThread) == 0xFFFFFFFF:
            raise _api_error("ResumeThread")
        spawned = _SpawnedProcess(process_handle, job_handle, int(process.dwProcessId))
    except (OSError, RuntimeError, ValueError) as error:
        errors.add(error)
    finally:
        errors.attempt(lambda: api.kernel32.DeleteProcThreadAttributeList(attribute_pointer))
        del attribute_storage
        if created:
            errors.attempt(lambda: api.close_handle(_handle_value(process.hThread)))
    if errors.failed:
        cleanup_process_handle = _handle_value(process.hProcess) if created else None
        _cleanup_unreturned_process(
            api,
            cleanup_process_handle,
            job_handle,
            assigned=assigned,
            errors=errors,
        )
        errors.raise_first()
    assert spawned is not None
    return spawned


def _close_pseudoconsole(api: _WindowsApi, pseudo: _PseudoConsole) -> None:
    errors = _CleanupErrors()
    errors.attempt(lambda: api.close_handle(pseudo.input_handle))
    errors.attempt(lambda: api.kernel32.ClosePseudoConsole(pseudo.hpc))
    errors.attempt(lambda: api.close_handle(pseudo.output_handle))
    errors.raise_first()


def _discard_spawned_process(api: _WindowsApi, spawned: _SpawnedProcess) -> None:
    errors = _CleanupErrors()
    result = int(api.kernel32.WaitForSingleObject(spawned.process_handle, 0))
    if result == _WAIT_TIMEOUT:
        if not api.kernel32.TerminateJobObject(spawned.job_handle, 1):
            errors.add(_api_error("TerminateJobObject"))
    elif result != _WAIT_OBJECT_0:
        errors.attempt(lambda: _raise_unexpected_wait_result("WaitForSingleObject", result))
    errors.attempt(lambda: _wait_for_terminated_process(api, spawned.process_handle))
    errors.attempt(lambda: api.close_handle(spawned.process_handle))
    errors.attempt(lambda: api.close_handle(spawned.job_handle))
    errors.raise_first()


class ConPtyProcess:
    """One process tree attached to a Windows pseudo console."""

    def __init__(
        self,
        *,
        api: _WindowsApi,
        pseudo: _PseudoConsole,
        spawned: _SpawnedProcess,
        capture_limit: int,
        artifact_path: Path | None,
    ) -> None:
        self._api = api
        self._pseudo: _PseudoConsole | None = pseudo
        self._process_handle: int | None = spawned.process_handle
        self._job_handle: int | None = spawned.job_handle
        self._pid = spawned.pid
        self._artifact_path = artifact_path
        self._artifact_error: OSError | None = None
        self._artifact_dirty = False
        self._artifact_next_flush = 0.0
        self._artifact_lock = threading.Lock()
        self._reader_error: str | None = None
        self._reader_stop = threading.Event()
        self.transcript = BoundedTranscript(limit=capture_limit)
        self._reader = threading.Thread(
            target=self._read_output,
            name=f"conpty-output-{self._pid}",
            daemon=True,
        )
        self._reader.start()

    @classmethod
    def start(
        cls,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        columns: int,
        rows: int,
        capture_limit: int,
        artifact_path: Path | None,
    ) -> Self:
        if not 1 <= columns <= 32_767 or not 1 <= rows <= 32_767:
            raise ValueError("ConPTY dimensions must be between 1 and 32767")
        if capture_limit <= 0:
            raise ValueError("ConPTY capture limit must be positive")
        api = _WindowsApi()
        pseudo = _create_pseudoconsole(api, columns, rows)
        try:
            spawned = _spawn_process(api, pseudo, argv, cwd, env)
        except (OSError, ValueError) as error:
            errors = _CleanupErrors()
            errors.add(error)
            errors.attempt(lambda: _close_pseudoconsole(api, pseudo))
            raise
        try:
            return cls(
                api=api,
                pseudo=pseudo,
                spawned=spawned,
                capture_limit=capture_limit,
                artifact_path=artifact_path,
            )
        except RuntimeError as error:
            errors = _CleanupErrors()
            errors.add(error)
            errors.attempt(lambda: _discard_spawned_process(api, spawned))
            errors.attempt(lambda: _close_pseudoconsole(api, pseudo))
            raise

    @property
    def pid(self) -> int:
        """Return the attached process ID."""
        return self._pid

    @property
    def reader_alive(self) -> bool:
        """Whether the output-drain thread is still running."""
        return self._reader.is_alive()

    @property
    def owned_resource_count(self) -> int:
        """Count raw Windows resources that have not been closed."""
        pseudo_count = 0
        if self._pseudo is not None:
            pseudo_count = sum(
                int(handle != 0)
                for handle in (
                    self._pseudo.hpc,
                    self._pseudo.input_handle,
                    self._pseudo.output_handle,
                )
            )
        return (
            pseudo_count + int(self._process_handle is not None) + int(self._job_handle is not None)
        )

    def _read_available(self, output_handle: int) -> bytes | None:
        available = wintypes.DWORD()
        if not self._api.kernel32.PeekNamedPipe(
            output_handle,
            None,
            0,
            None,
            ctypes.byref(available),
            None,
        ):
            if _last_error() == _ERROR_BROKEN_PIPE:
                return None
            _raise_api_error("PeekNamedPipe")
        if available.value == 0:
            return b""
        size = min(int(available.value), 4096)
        buffer = ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        if not self._api.kernel32.ReadFile(
            output_handle,
            buffer,
            size,
            ctypes.byref(read),
            None,
        ):
            if _last_error() == _ERROR_BROKEN_PIPE:
                return None
            _raise_api_error("ReadFile")
        return bytes(buffer.raw[: read.value])

    def _read_output(self) -> None:
        pseudo = self._pseudo
        if pseudo is None:
            return
        try:
            while not self._reader_stop.is_set():
                data = self._read_available(pseudo.output_handle)
                if data is None:
                    self._snapshot_artifact_if_due(force=True)
                    return
                if data:
                    self.transcript.append(data)
                    self._artifact_dirty = True
                    self._snapshot_artifact_if_due()
                else:
                    self._snapshot_artifact_if_due()
                    time.sleep(0.01)
        except OSError as exc:
            self._reader_error = str(exc)

    def _snapshot_artifact_if_due(self, *, force: bool = False) -> None:
        if self._artifact_path is None or not self._artifact_dirty:
            return
        now = time.monotonic()
        if not force and now < self._artifact_next_flush:
            return
        self._artifact_next_flush = now + _ARTIFACT_FLUSH_SECONDS
        try:
            self._write_artifact()
        except OSError as exc:
            if self._artifact_error is None:
                self._artifact_error = exc
        else:
            self._artifact_dirty = False

    def send(self, data: bytes) -> None:
        """Write actual terminal bytes to the pseudo console."""
        pseudo = self._pseudo
        if pseudo is None:
            raise RuntimeError("ConPTY input is closed")
        offset = 0
        while offset < len(data):
            chunk = data[offset:]
            written = wintypes.DWORD()
            buffer = ctypes.create_string_buffer(chunk)
            if not self._api.kernel32.WriteFile(
                pseudo.input_handle,
                buffer,
                len(chunk),
                ctypes.byref(written),
                None,
            ):
                _raise_api_error("WriteFile")
            if written.value == 0:
                raise OSError("WriteFile wrote zero bytes to ConPTY")
            offset += int(written.value)

    def wait_for_output(
        self,
        value: bytes,
        *,
        timeout: float,
        after: int | None = None,
    ) -> bool:
        """Wait until the bounded transcript contains `value` after a checkpoint."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._contains_output(value, after):
                return True
            time.sleep(0.02)
        return self._contains_output(value, after)

    def _contains_output(self, value: bytes, after: int | None) -> bool:
        if after is None:
            return self.transcript.contains(value)
        return self.transcript.contains_after(value, after)

    def poll(self) -> int | None:
        """Return the exit code, or `None` while the child is active."""
        process_handle = self._process_handle
        if process_handle is None:
            raise RuntimeError("ConPTY process handle is closed")
        result = int(self._api.kernel32.WaitForSingleObject(process_handle, 0))
        if result == _WAIT_TIMEOUT:
            return None
        if result != _WAIT_OBJECT_0:
            _raise_unexpected_wait_result("WaitForSingleObject", result)
        exit_code = wintypes.DWORD()
        if not self._api.kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
            _raise_api_error("GetExitCodeProcess")
        return int(exit_code.value)

    def wait(self, *, timeout: float) -> int:
        """Wait for the attached process to exit within `timeout` seconds."""
        process_handle = self._process_handle
        if process_handle is None:
            raise RuntimeError("ConPTY process handle is closed")
        milliseconds = max(0, min(round(timeout * 1000), 0xFFFFFFFE))
        result = int(self._api.kernel32.WaitForSingleObject(process_handle, milliseconds))
        if result == _WAIT_TIMEOUT:
            raise TimeoutError(f"ConPTY process {self._pid} did not exit within {timeout:.1f}s")
        if result != _WAIT_OBJECT_0:
            _raise_unexpected_wait_result("WaitForSingleObject", result)
        exit_code = self.poll()
        assert exit_code is not None
        return exit_code

    def diagnostics(self) -> str:
        """Return bounded state and transcript output for assertion failures."""
        return_code: object
        try:
            return_code = self.poll()
        except RuntimeError:
            return_code = "handle-closed"
        tail = self.transcript.tail()[-8_192:].decode("utf-8", errors="replace")
        return (
            f"ConPTY pid={self._pid} returncode={return_code!r} "
            f"owned_resources={self.owned_resource_count} "
            f"reader_alive={self.reader_alive} reader_error={self._reader_error!r}\n"
            f"--- bounded terminal tail ---\n{tail}\n--- end terminal tail ---"
        )

    def _terminate_if_running(self) -> None:
        if self._process_handle is None or self.poll() is not None:
            return
        job_handle = self._job_handle
        if job_handle is None:
            raise RuntimeError("active ConPTY process has no owning job")
        if not self._api.kernel32.TerminateJobObject(job_handle, 1):
            _raise_api_error("TerminateJobObject")
        self.wait(timeout=5.0)

    def _close_pseudo_input(self) -> None:
        pseudo = self._pseudo
        if pseudo is None or pseudo.input_handle == 0:
            return
        self._api.close_handle(pseudo.input_handle)
        pseudo.input_handle = 0

    def _close_pseudoconsole(self) -> None:
        pseudo = self._pseudo
        if pseudo is None or pseudo.hpc == 0:
            return
        self._api.kernel32.ClosePseudoConsole(pseudo.hpc)
        pseudo.hpc = 0

    def _close_pseudo_output(self) -> None:
        pseudo = self._pseudo
        if pseudo is None or pseudo.output_handle == 0:
            return
        self._api.close_handle(pseudo.output_handle)
        pseudo.output_handle = 0
        if pseudo.input_handle == 0 and pseudo.hpc == 0:
            self._pseudo = None

    def _close_process_handle(self) -> None:
        if self._process_handle is None:
            return
        self._api.close_handle(self._process_handle)
        self._process_handle = None

    def _close_job_handle(self) -> None:
        if self._job_handle is None:
            return
        self._api.close_handle(self._job_handle)
        self._job_handle = None

    def _write_artifact(self) -> None:
        path = self._artifact_path
        if path is None:
            return
        temporary = path.with_name(f".{path.name}.{self._pid}.snapshot")
        artifact_lock = getattr(self, "_artifact_lock", None)
        if artifact_lock is None:
            artifact_lock = self._artifact_lock = threading.Lock()
        if not artifact_lock.acquire(timeout=1.0):
            raise TimeoutError("ConPTY artifact writer did not release snapshot lock")
        try:
            snapshot = self.transcript.tail()[-_ARTIFACT_LIMIT:]
            try:
                temporary.write_bytes(snapshot)
                os.replace(temporary, path)
            except OSError as exc:
                error = OSError(exc.errno, f"ConPTY artifact snapshot failed: {exc}")
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    error.add_note(f"Additional cleanup error: {cleanup_error}")
                raise error from exc
        finally:
            artifact_lock.release()

    def close(self) -> None:
        """Close the process, its job, the pseudo console, and all pipe handles."""
        if self.owned_resource_count == 0:
            return
        errors = _CleanupErrors()
        errors.attempt(self._terminate_if_running)
        errors.attempt(self._close_pseudo_input)
        errors.attempt(self._close_pseudoconsole)
        errors.attempt(lambda: self._reader.join(timeout=5.0))
        if self._reader.is_alive():
            errors.attempt(self._reader_stop.set)
            errors.attempt(lambda: self._reader.join(timeout=1.0))
        errors.attempt(self._close_pseudo_output)
        if self._reader.is_alive():
            errors.attempt(lambda: self._reader.join(timeout=1.0))
        if self._reader.is_alive():
            errors.add(RuntimeError("ConPTY output reader did not stop"))
        if self._reader_error is not None:
            errors.add(OSError(f"ConPTY output reader failed: {self._reader_error}"))
        errors.attempt(self._close_process_handle)
        errors.attempt(self._close_job_handle)
        artifact_error = getattr(self, "_artifact_error", None)
        if artifact_error is not None:
            errors.add(artifact_error)
        errors.attempt(self._write_artifact)
        errors.raise_first()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.close()


def isolated_environment(root: Path, *, repo_root: Path, python: Path) -> dict[str, str]:
    """Build an environment isolated from the invoking user's Kubernetes state."""
    home = root / "home"
    appdata = home / "AppData" / "Roaming"
    local_appdata = home / "AppData" / "Local"
    temp = root / "temp"
    bin_root = root / "bin"
    for directory in (home, appdata, local_appdata, temp, bin_root):
        directory.mkdir(parents=True, exist_ok=True)

    system_root = os.environ.get("SYSTEMROOT", os.environ.get("WINDIR", str(root / "Windows")))
    system32 = Path(system_root) / "System32"
    return {
        "APPDATA": str(appdata),
        "COLORTERM": "truecolor",
        "COMSPEC": os.environ.get("COMSPEC", str(system32 / "cmd.exe")),
        "HOME": str(home),
        "KUBECONFIG": str(root / "isolated-kubeconfig"),
        "KORVID_SMOKE_PYTHON": str(python),
        "LOCALAPPDATA": str(local_appdata),
        "PATH": os.pathsep.join((str(bin_root), str(system32))),
        "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.pathsep.join((str(repo_root / "src"), str(repo_root))),
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
        "SYSTEMROOT": system_root,
        "TEMP": str(temp),
        "TERM": "xterm-256color",
        "TMP": str(temp),
        "USERPROFILE": str(home),
        "WINDIR": system_root,
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
    }


def process_handle_count() -> int:
    """Return the current Windows process's kernel handle count."""
    api = _WindowsApi()
    count = wintypes.DWORD()
    if not api.kernel32.GetProcessHandleCount(
        api.kernel32.GetCurrentProcess(),
        ctypes.byref(count),
    ):
        _raise_api_error("GetProcessHandleCount")
    return int(count.value)


def wait_for_process_exit(pid: int, *, timeout: float) -> bool:
    """Wait only for the specified PID to exit."""
    api = _WindowsApi()
    handle = _handle_value(
        api.kernel32.OpenProcess(
            _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
    )
    if handle == 0:
        if _last_error() == _ERROR_INVALID_PARAMETER:
            return True
        _raise_api_error("OpenProcess")
    try:
        milliseconds = max(0, min(round(timeout * 1000), 0xFFFFFFFE))
        result = int(api.kernel32.WaitForSingleObject(handle, milliseconds))
        if result == _WAIT_TIMEOUT:
            return False
        if result != _WAIT_OBJECT_0:
            _raise_unexpected_wait_result("WaitForSingleObject", result)
        return True
    finally:
        api.close_handle(handle)
