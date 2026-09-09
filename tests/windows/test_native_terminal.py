"""Native Windows ConPTY smoke coverage for the real Textual driver."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import site
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from tests.windows import conpty, native_app
from tests.windows.conpty import (
    BoundedTranscript,
    ConPtyProcess,
    WitnessDirectory,
    isolated_environment,
    process_handle_count,
    wait_for_process_exit,
)

_REPO_ROOT = Path(__file__).parents[2]
_PHASE_TIMEOUT = 15.0
_SMOKE_BUDGET_SECONDS = 150.0


class _AttributeKernel:
    def __init__(self) -> None:
        self.pseudoconsole_value: int | None = None
        self.pseudoconsole_size: int | None = None
        self.deleted = 0

    def InitializeProcThreadAttributeList(
        self,
        attribute_list: object | None,
        count: int,
        flags: int,
        size: Any,
    ) -> bool:
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t)).contents.value = 64
        return attribute_list is not None

    def UpdateProcThreadAttribute(
        self,
        attribute_list: object,
        flags: int,
        attribute: int,
        value: object,
        size: int,
        previous: object | None,
        return_size: object | None,
    ) -> bool:
        raw_value = getattr(value, "value", None)
        self.pseudoconsole_value = raw_value if isinstance(raw_value, int) else None
        self.pseudoconsole_size = size
        return True

    def DeleteProcThreadAttributeList(self, attribute_list: object) -> None:
        self.deleted += 1


class _FakeApi:
    def __init__(self, kernel32: object) -> None:
        self.kernel32 = kernel32
        self.closed_handles: list[int | None] = []

    def close_handle(self, handle: int | None) -> None:
        self.closed_handles.append(handle)


class _WaitKernel:
    def __init__(self, wait_result: int) -> None:
        self.wait_result = wait_result
        self.open_handle = 91

    def WaitForSingleObject(self, handle: int, timeout: int) -> int:
        return self.wait_result

    def GetExitCodeProcess(self, handle: int, exit_code: Any) -> bool:
        ctypes.cast(exit_code, ctypes.POINTER(ctypes.c_ulong)).contents.value = 0
        return True

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
        return self.open_handle


class _CleanupReader:
    def __init__(self, *, alive: bool = True) -> None:
        self.alive = alive
        self.join_calls: list[float] = []

    def join(self, timeout: float) -> None:
        self.join_calls.append(timeout)

    def is_alive(self) -> bool:
        return self.alive


class _CleanupStop:
    def __init__(self) -> None:
        self.set_called = False

    def set(self) -> None:
        self.set_called = True


class _CleanupKernel:
    def __init__(
        self,
        events: list[tuple[str, int]],
        *,
        wait_results: list[int] | None = None,
    ) -> None:
        self.events = events
        self.wait_results = wait_results or []
        self.wait_calls: list[tuple[int, int]] = []
        self.terminated_jobs: list[int] = []

    def ClosePseudoConsole(self, hpc: int) -> None:
        self.events.append(("pseudo", hpc))

    def WaitForSingleObject(self, handle: int, timeout: int) -> int:
        self.wait_calls.append((handle, timeout))
        return self.wait_results.pop(0)

    def TerminateJobObject(self, job_handle: int, exit_code: int) -> bool:
        self.terminated_jobs.append(job_handle)
        return True


class _CleanupApi:
    def __init__(
        self,
        kernel32: _CleanupKernel,
        reader: _CleanupReader,
        events: list[tuple[str, int]],
        *,
        failing_handles: set[int] | None = None,
        unblock_reader: bool = True,
    ) -> None:
        self.kernel32 = kernel32
        self._reader = reader
        self._events = events
        self._failing_handles = failing_handles or set()
        self._unblock_reader = unblock_reader

    def close_handle(self, handle: int | None) -> None:
        assert handle is not None
        self._events.append(("handle", handle))
        if handle == 30 and self._unblock_reader:
            self._reader.alive = False
        if handle in self._failing_handles:
            raise OSError(f"close {handle} failed")


def _bare_process(api: object) -> ConPtyProcess:
    process = object.__new__(ConPtyProcess)
    process._api = cast(Any, api)
    process._process_handle = 40
    process._pid = 1234
    return process


def _cleanup_process(
    tmp_path: Path,
    *,
    failing_handles: set[int] | None = None,
    reader_alive: bool = True,
    unblock_reader: bool = True,
    termination_timeout: bool = False,
) -> tuple[ConPtyProcess, _CleanupReader, _CleanupStop, list[tuple[str, int]], Path]:
    events: list[tuple[str, int]] = []
    reader = _CleanupReader(alive=reader_alive)
    stop = _CleanupStop()
    kernel32 = _CleanupKernel(
        events,
        wait_results=[0x00000102, 0x00000102] if termination_timeout else None,
    )
    api = _CleanupApi(
        kernel32,
        reader,
        events,
        failing_handles=failing_handles,
        unblock_reader=unblock_reader,
    )
    process = _bare_process(api)
    process._pseudo = conpty._PseudoConsole(hpc=20, input_handle=10, output_handle=30)
    process._job_handle = 50
    process._reader = cast(Any, reader)
    process._reader_stop = cast(Any, stop)
    process._reader_error = None
    process.transcript = BoundedTranscript(limit=32)
    process.transcript.append(b"terminal tail")
    artifact = tmp_path / "conpty-output.bin"
    process._artifact_path = artifact
    return process, reader, stop, events, artifact


def _send_filter_pattern(
    send: Callable[[bytes], None],
    wait_for_focus: Callable[[], dict[str, Any]],
    pattern: str,
) -> dict[str, Any]:
    send(b"/")
    focused = wait_for_focus()
    if focused.get("filter_focused") is not True:
        raise AssertionError("filter witness did not confirm input focus")
    send(pattern.encode("ascii") + b"\r")
    return focused


class _RunResultApp:
    def __init__(self, return_code: int) -> None:
        self.return_code = return_code
        self.run_calls: list[tuple[bool, bool]] = []

    def run(self, *, headless: bool, mouse: bool) -> None:
        self.run_calls.append((headless, mouse))


def test_filter_input_waits_for_focus_before_sending_text() -> None:
    events: list[tuple[str, object]] = []
    focused = {"filter_focused": True}

    def send(data: bytes) -> None:
        events.append(("send", data))

    def wait_for_focus() -> dict[str, Any]:
        events.append(("wait", None))
        return focused

    observed = _send_filter_pattern(send, wait_for_focus, "api")

    assert observed is focused
    assert events == [
        ("send", b"/"),
        ("wait", None),
        ("send", b"api\r"),
    ]


def test_run_app_instance_returns_clean_textual_exit_code() -> None:
    app = _RunResultApp(return_code=0)
    witnessed: list[int] = []

    result = native_app._run_app_instance(cast(Any, app), witnessed.append)

    assert result == 0
    assert app.run_calls == [(False, False)]
    assert witnessed == [0]


def test_run_app_instance_propagates_handled_textual_error_code() -> None:
    app = _RunResultApp(return_code=1)
    witnessed: list[int] = []

    result = native_app._run_app_instance(cast(Any, app), witnessed.append)

    assert result == 1
    assert app.run_calls == [(False, False)]
    assert witnessed == [1]


def test_native_snapshot_records_process_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    app = cast(Any, type("SnapshotApp", (), {"_driver": None})())
    monkeypatch.setattr(os, "getpid", lambda: 5152)
    monkeypatch.setattr(os, "getppid", lambda: 7692)
    monkeypatch.setattr(native_app, "_process_handle_count", lambda: 7)
    monkeypatch.setattr(native_app, "_textual_threads", dict)

    snapshot = native_app._snapshot(app)

    assert snapshot["pid"] == 5152
    assert snapshot["parent_pid"] == 7692


@pytest.mark.parametrize(
    ("rows", "table_visible", "workspace_visible", "expected"),
    [
        (2, False, True, False),
        (2, True, False, False),
        (2, True, True, True),
        (0, True, True, False),
        (1, True, True, False),
    ],
)
def test_resources_ready_requires_rendered_visible_table(
    rows: int,
    table_visible: bool,
    workspace_visible: bool,
    expected: bool,
) -> None:
    assert native_app._resources_are_visible(rows, table_visible, workspace_visible) is expected


@pytest.mark.parametrize(
    ("mounted", "launcher_pid", "expected"),
    [
        ({"pid": 7692, "parent_pid": 100}, 7692, 7692),
        ({"pid": 5152, "parent_pid": 7692}, 7692, 5152),
    ],
)
def test_app_process_id_accepts_direct_or_venv_launcher_lineage(
    mounted: dict[str, Any],
    launcher_pid: int,
    expected: int,
) -> None:
    assert _app_process_id(mounted, launcher_pid) == expected


def test_app_process_id_rejects_unrelated_process() -> None:
    with pytest.raises(AssertionError, match="not the ConPTY process or its child"):
        _app_process_id({"pid": 5152, "parent_pid": 4000}, launcher_pid=7692)


def test_attribute_list_passes_pseudoconsole_handle_value() -> None:
    kernel32 = _AttributeKernel()
    api = _FakeApi(kernel32)
    hpc = 0x1234ABCD

    storage, pointer = conpty._attribute_list(cast(Any, api), hpc)

    assert len(storage) == 64
    assert kernel32.pseudoconsole_value == hpc
    assert kernel32.pseudoconsole_size == ctypes.sizeof(ctypes.c_void_p)
    kernel32.DeleteProcThreadAttributeList(pointer)
    assert kernel32.deleted == 1


def test_spawn_deletes_attribute_list_when_job_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    kernel32 = _AttributeKernel()
    api = _FakeApi(kernel32)
    storage = ctypes.create_string_buffer(64)
    pointer = ctypes.cast(storage, ctypes.c_void_p)

    def attribute_list(
        api: object,
        hpc: int,
    ) -> tuple[ctypes.Array[Any], ctypes.c_void_p]:
        return storage, pointer

    def fail_job(api: object) -> int:
        raise OSError("job creation failed")

    monkeypatch.setattr(conpty, "_attribute_list", attribute_list)
    monkeypatch.setattr(conpty, "_create_kill_job", fail_job)

    with pytest.raises(OSError, match="job creation failed"):
        conpty._spawn_process(
            cast(Any, api),
            conpty._PseudoConsole(hpc=1, input_handle=2, output_handle=3),
            ["child.exe"],
            tmp_path,
            {},
        )

    assert kernel32.deleted == 1


def test_spawn_replaces_redirected_parent_standard_handles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Kernel(_AttributeKernel):
        def __init__(self) -> None:
            super().__init__()
            self.flags = 0
            self.standard_handles: tuple[int | None, ...] = ()

        def CreateProcessW(self, *args: Any) -> bool:
            startup = ctypes.cast(args[8], ctypes.POINTER(conpty._StartupInfoEx)).contents
            self.flags = startup.StartupInfo.dwFlags
            self.standard_handles = (
                startup.StartupInfo.hStdInput,
                startup.StartupInfo.hStdOutput,
                startup.StartupInfo.hStdError,
            )
            process = ctypes.cast(args[9], ctypes.POINTER(conpty._ProcessInformation)).contents
            process.hProcess = 10
            process.hThread = 11
            process.dwProcessId = 12
            return True

        def AssignProcessToJobObject(self, job: int, process: int) -> bool:
            return True

        def ResumeThread(self, thread: object) -> int:
            return 1

    kernel = Kernel()
    monkeypatch.setattr(conpty, "_create_kill_job", lambda api: 20)

    spawned = conpty._spawn_process(
        cast(Any, _FakeApi(kernel)),
        conpty._PseudoConsole(hpc=1, input_handle=2, output_handle=3),
        ["child.exe"],
        tmp_path,
        {},
    )

    assert spawned.pid == 12
    assert kernel.flags & 0x00000100  # STARTF_USESTDHANDLES
    assert kernel.standard_handles == (None, None, None)


def test_poll_reports_wait_failed_last_error(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _bare_process(_FakeApi(_WaitKernel(0xFFFFFFFF)))
    monkeypatch.setattr(conpty, "_last_error", lambda: 1234)

    with pytest.raises(OSError, match="WaitForSingleObject") as error:
        process.poll()

    assert error.value.errno == 1234


def test_wait_reports_wait_failed_last_error(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _bare_process(_FakeApi(_WaitKernel(0xFFFFFFFF)))
    monkeypatch.setattr(conpty, "_last_error", lambda: 2345)

    with pytest.raises(OSError, match="WaitForSingleObject") as error:
        process.wait(timeout=1.0)

    assert error.value.errno == 2345


def test_wait_for_process_exit_reports_wait_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeApi(_WaitKernel(0xFFFFFFFF))
    monkeypatch.setattr(conpty, "_WindowsApi", lambda: cast(Any, api))
    monkeypatch.setattr(conpty, "_last_error", lambda: 3456)

    with pytest.raises(OSError, match="WaitForSingleObject") as error:
        wait_for_process_exit(99, timeout=1.0)

    assert error.value.errno == 3456
    assert api.closed_handles == [91]


def test_wait_for_process_exit_returns_false_only_for_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeApi(_WaitKernel(0x00000102))
    monkeypatch.setattr(conpty, "_WindowsApi", lambda: cast(Any, api))

    assert wait_for_process_exit(99, timeout=1.0) is False
    assert api.closed_handles == [91]


def test_close_preserves_termination_error_and_attempts_all_cleanup(
    tmp_path: Path,
) -> None:
    process, reader, stop, events, artifact = _cleanup_process(
        tmp_path,
        failing_handles={40},
        termination_timeout=True,
    )
    kernel32 = cast(_CleanupKernel, cast(Any, process._api).kernel32)

    with pytest.raises(TimeoutError, match=r"did not exit within 5\.0s") as error:
        process.close()

    assert events == [
        ("handle", 10),
        ("pseudo", 20),
        ("handle", 30),
        ("handle", 40),
        ("handle", 50),
    ]
    assert stop.set_called
    assert reader.join_calls == [5.0, 1.0]
    assert kernel32.wait_calls == [(40, 0), (40, 5_000)]
    assert kernel32.terminated_jobs == [50]
    assert artifact.read_bytes() == b"terminal tail"
    assert any("close 40 failed" in note for note in getattr(error.value, "__notes__", []))


def test_close_surfaces_handle_error_after_remaining_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process, _, _, events, artifact = _cleanup_process(
        tmp_path,
        failing_handles={10},
        reader_alive=False,
    )
    monkeypatch.setattr(process, "_terminate_if_running", lambda: None)

    with pytest.raises(OSError, match="close 10 failed"):
        process.close()

    assert events == [
        ("handle", 10),
        ("pseudo", 20),
        ("handle", 30),
        ("handle", 40),
        ("handle", 50),
    ]
    assert artifact.read_bytes() == b"terminal tail"


def test_close_surfaces_stuck_reader_after_handles_and_artifact_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process, reader, stop, events, artifact = _cleanup_process(
        tmp_path,
        unblock_reader=False,
    )
    monkeypatch.setattr(process, "_terminate_if_running", lambda: None)

    with pytest.raises(RuntimeError, match="output reader did not stop"):
        process.close()

    assert events == [
        ("handle", 10),
        ("pseudo", 20),
        ("handle", 30),
        ("handle", 40),
        ("handle", 50),
    ]
    assert stop.set_called
    assert reader.join_calls == [5.0, 1.0, 1.0]
    assert artifact.read_bytes() == b"terminal tail"


def test_bounded_transcript_keeps_only_the_latest_complete_tail() -> None:
    transcript = BoundedTranscript(limit=8)

    transcript.append(b"12345")
    checkpoint = transcript.position()
    transcript.append(b"67890")

    assert transcript.tail() == b"34567890"
    assert transcript.contains(b"678")
    assert transcript.contains_after(b"678", checkpoint)
    assert not transcript.contains_after(b"345", checkpoint)
    assert transcript.count(b"34") == 1


def test_initial_render_may_arrive_before_mount_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session, _, _, _, _ = _cleanup_process(tmp_path, reader_alive=False)
    session.transcript.append(b"api-1 worker-2")
    mounted = {
        "pid": session.pid,
        "parent_pid": 100,
        "driver": "textual.drivers.windows_driver.WindowsDriver",
        "stdin_tty": True,
        "stdout_tty": True,
        "headless": False,
        "can_suspend": True,
    }
    resources = {
        "rows": 2,
        "table_visible": True,
        "workspace_visible": True,
        "textual_threads": {"textual-input": 1, "textual-output": 1},
    }

    def phase(
        witnesses: WitnessDirectory, name: str, process: ConPtyProcess, deadline: float
    ) -> dict[str, Any]:
        return mounted if name == "mounted" else resources

    def stop_at_help(data: bytes) -> None:
        assert data == b"?"
        raise RuntimeError("reached help input after rendering")

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_prepare_native_fixture", lambda root: (None, {}))
    monkeypatch.setattr(module, "process_handle_count", lambda: 1)
    monkeypatch.setattr(module, "_phase", phase)
    monkeypatch.setattr(module, "_remaining", lambda deadline, cap: 0.0)
    monkeypatch.setattr(ConPtyProcess, "start", lambda *args, **kwargs: session)
    monkeypatch.setattr(session, "diagnostics", lambda: "pre-observed startup output")
    monkeypatch.setattr(session, "send", stop_at_help)
    monkeypatch.setattr(session, "close", lambda: None)

    with pytest.raises(RuntimeError, match="reached help input after rendering"):
        test_korvid_operates_through_native_windows_conpty(tmp_path)


@pytest.mark.parametrize("leak_owned_handle", [False, True])
def test_native_cleanup_checks_ownership_not_process_wide_totals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, leak_owned_handle: bool
) -> None:
    session, _, _, _, _ = _cleanup_process(tmp_path, reader_alive=False)
    witness_root = tmp_path / "witness"
    witness_root.mkdir()
    (witness_root / "app-exited.json").write_text(
        json.dumps({"return_code": 0, "textual_threads": {}}), encoding="utf-8"
    )
    common: dict[str, Any] = {
        "pid": session.pid,
        "parent_pid": 100,
        "driver": "textual.drivers.windows_driver.WindowsDriver",
        "stdin_tty": True,
        "stdout_tty": True,
        "headless": False,
        "can_suspend": True,
        "rows": 2,
        "table_visible": True,
        "workspace_visible": True,
        "textual_threads": {"textual-input": 1, "textual-output": 1},
        "filter": "",
        "filter_open": False,
        "filter_focused": True,
        "process_handles": 212,
    }
    overrides: dict[str, dict[str, Any]] = {
        "help-open": {"body": "korvid"},
        "filter-applied": {"filter": "api", "rows": 1, "process_handles": 208},
        "shell-child-started": {
            "pid": 2456,
            "parent_pid": session.pid,
            "executable": "kubectl.exe",
            "argv": ["exec", "api-1"],
        },
        "shell-input": {"text": "native-shell-input"},
        "post-resume-filter-applied": {"filter": "worker", "rows": 1},
    }

    def phase(
        witnesses: WitnessDirectory, name: str, process: ConPtyProcess, deadline: float
    ) -> dict[str, Any]:
        return common | overrides.get(name, {})

    close = session.close

    def close_with_optional_leak() -> None:
        close()
        if leak_owned_handle:
            session._process_handle = 99

    module = sys.modules[__name__]
    host_counts = iter((90, 100))
    monkeypatch.setattr(module, "_smoke_root", lambda root: (root, False))
    monkeypatch.setattr(
        module, "_prepare_native_fixture", lambda root: (WitnessDirectory(witness_root), {})
    )
    monkeypatch.setattr(module, "process_handle_count", lambda: next(host_counts))
    monkeypatch.setattr(module, "wait_for_process_exit", lambda pid, timeout: True)
    monkeypatch.setattr(module, "_phase", phase)
    monkeypatch.setattr(ConPtyProcess, "start", lambda *args, **kwargs: session)
    monkeypatch.setattr(session, "diagnostics", lambda: "native cleanup evidence")
    monkeypatch.setattr(session, "send", lambda data: None)
    monkeypatch.setattr(session, "wait_for_output", lambda *args, **kwargs: True)
    monkeypatch.setattr(session, "wait", lambda timeout: 0)
    monkeypatch.setattr(session, "_terminate_if_running", lambda: None)
    monkeypatch.setattr(session, "close", close_with_optional_leak)

    if leak_owned_handle:
        with pytest.raises(AssertionError, match="owns native resources"):
            test_korvid_operates_through_native_windows_conpty(tmp_path)
    else:
        test_korvid_operates_through_native_windows_conpty(tmp_path)
        assert session.owned_resource_count == 0
        evidence = json.loads((witness_root / "host-cleanup.json").read_text(encoding="utf-8"))
        assert evidence["app_handles"] == {"filtered": 208, "after_input": 212}
        assert evidence["host_handles"] == {"before": 90, "after": 100}


def test_isolated_environment_does_not_inherit_user_kubernetes_paths(tmp_path: Path) -> None:
    python = tmp_path / "venv" / "Scripts" / "python.exe"

    environment = isolated_environment(tmp_path, repo_root=_REPO_ROOT, python=python)

    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["USERPROFILE"] == str(tmp_path / "home")
    assert environment["APPDATA"] == str(tmp_path / "home" / "AppData" / "Roaming")
    assert environment["KUBECONFIG"] == str(tmp_path / "isolated-kubeconfig")
    assert environment["KORVID_SMOKE_PYTHON"] == str(python)
    assert environment["PATH"].split(os.pathsep)[0] == str(tmp_path / "bin")
    assert "KUBECONFIG" not in {
        key for key, value in os.environ.items() if environment.get(key) == value
    }


def test_witness_directory_reads_a_complete_phase_payload(tmp_path: Path) -> None:
    (tmp_path / "mounted.json").write_text(
        json.dumps({"driver": "WindowsDriver", "ready": True}),
        encoding="utf-8",
    )

    payload = WitnessDirectory(tmp_path).wait("mounted", timeout=0.0)

    assert payload == {"driver": "WindowsDriver", "ready": True}


def _smoke_root(tmp_path: Path) -> tuple[Path, bool]:
    configured = os.environ.get("KORVID_WINDOWS_SMOKE_ARTIFACT_DIR")
    if configured is None:
        return tmp_path, False
    root = Path(configured) / f"native-terminal-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    return root, True


def _prepare_native_fixture(root: Path) -> tuple[WitnessDirectory, dict[str, str]]:
    """Create the sole external edge: a copied Python PE acting as fake kubectl.

    `sitecustomize` diverts only the renamed `kubectl.exe`; the app process
    remains the normal venv Python and executes the production shell controller.
    """
    witness_root = root / "witness"
    bin_root = root / "bin"
    bootstrap_root = root / "bootstrap"
    witness_root.mkdir(parents=True)
    bin_root.mkdir(parents=True)
    bootstrap_root.mkdir(parents=True)
    base_executable = Path(getattr(sys, "_base_executable", sys.executable))
    shutil.copy2(base_executable, bin_root / "kubectl.exe")
    (root / "pyvenv.cfg").write_text(
        f"home = {sys.base_prefix}\n"
        "include-system-site-packages = false\n"
        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n"
        f"executable = {base_executable}\n",
        encoding="utf-8",
    )
    (bootstrap_root / "sitecustomize.py").write_text(
        "import os\n"
        "import pathlib\n"
        "import sys\n"
        "\n"
        'if pathlib.Path(sys.executable).name.casefold() == "kubectl.exe":\n'
        "    from tests.windows.native_app import main\n"
        "\n"
        '    code = main(["--fake-kubectl", *sys.argv])\n'
        "    sys.stdout.flush()\n"
        "    sys.stderr.flush()\n"
        "    os._exit(code)\n",
        encoding="utf-8",
    )
    environment = isolated_environment(root, repo_root=_REPO_ROOT, python=Path(sys.executable))
    site_packages = [path for path in site.getsitepackages() if Path(path).is_dir()]
    environment["PATH"] = os.pathsep.join(
        (str(bin_root), str(base_executable.parent), environment["PATH"])
    )
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(bootstrap_root),
            str(_REPO_ROOT / "src"),
            str(_REPO_ROOT),
            *site_packages,
        )
    )
    environment["KORVID_SMOKE_WITNESS_DIR"] = str(witness_root)
    return WitnessDirectory(witness_root), environment


def _phase(
    witnesses: WitnessDirectory,
    name: str,
    session: ConPtyProcess,
    deadline: float,
) -> dict[str, Any]:
    try:
        return witnesses.wait(name, timeout=_remaining(deadline, _PHASE_TIMEOUT))
    except (AssertionError, TimeoutError) as exc:
        raise AssertionError(f"{exc}\n{session.diagnostics()}") from exc


def _remaining(deadline: float, cap: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"native Windows smoke exceeded {_SMOKE_BUDGET_SECONDS:.0f}s budget")
    return min(remaining, cap)


def _thread_count(payload: dict[str, Any], name: str) -> int:
    threads = payload["textual_threads"]
    assert isinstance(threads, dict)
    value = threads.get(name, 0)
    assert isinstance(value, int)
    return value


def _app_process_id(mounted: dict[str, Any], launcher_pid: int) -> int:
    app_pid = int(mounted["pid"])
    app_parent_pid = int(mounted["parent_pid"])
    if app_pid != launcher_pid and app_parent_pid != launcher_pid:
        raise AssertionError(
            f"mounted app PID {app_pid} is not the ConPTY process or its child "
            f"(launcher PID {launcher_pid}, app parent PID {app_parent_pid})"
        )
    return app_pid


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ConPTY")
def test_korvid_operates_through_native_windows_conpty(tmp_path: Path) -> None:
    """Exercise production app, WindowsDriver, keyboard input, and shell suspension."""
    deadline = time.monotonic() + _SMOKE_BUDGET_SECONDS
    root, keep_artifact = _smoke_root(tmp_path)
    witnesses, environment = _prepare_native_fixture(root)
    artifact = root / "conpty-output.bin" if keep_artifact else None
    handles_before = process_handle_count()

    session = ConPtyProcess.start(
        [sys.executable, "-u", "-m", "tests.windows.native_app", "--app"],
        cwd=_REPO_ROOT,
        env=environment,
        columns=100,
        rows=35,
        capture_limit=128 * 1024,
        artifact_path=artifact,
    )
    launcher_pid = session.pid
    app_pid = 0
    shell_pid = 0
    with session:
        mounted = _phase(witnesses, "mounted", session, deadline)
        app_pid = _app_process_id(mounted, launcher_pid)
        resources = _phase(witnesses, "resources-ready", session, deadline)
        context = session.diagnostics()
        assert mounted["driver"] == "textual.drivers.windows_driver.WindowsDriver", context
        assert mounted["stdin_tty"] is True, context
        assert mounted["stdout_tty"] is True, context
        assert mounted["headless"] is False, context
        assert mounted["can_suspend"] is True, context
        assert resources["rows"] == 2, context
        assert resources["table_visible"] is True, context
        assert resources["workspace_visible"] is True, context
        assert _thread_count(resources, "textual-input") == 1, context
        assert _thread_count(resources, "textual-output") == 1, context
        assert session.wait_for_output(
            b"api-1",
            timeout=_remaining(deadline, 5.0),
        ), context
        assert session.wait_for_output(
            b"worker-2",
            timeout=_remaining(deadline, 5.0),
        ), context

        session.send(b"?")
        help_open = _phase(witnesses, "help-open", session, deadline)
        assert "korvid" in str(help_open["body"]).casefold(), session.diagnostics()
        session.send(b"?")
        _phase(witnesses, "help-closed", session, deadline)

        _send_filter_pattern(
            session.send,
            lambda: _phase(witnesses, "filter-focused", session, deadline),
            "api",
        )
        filtered = _phase(witnesses, "filter-applied", session, deadline)
        assert filtered["filter"] == "api", session.diagnostics()
        assert filtered["rows"] == 1, session.diagnostics()
        assert filtered["filter_open"] is False, session.diagnostics()

        suspend_output_start = session.transcript.position()
        session.send(b"s")
        _phase(witnesses, "suspended", session, deadline)
        shell_started = _phase(witnesses, "shell-child-started", session, deadline)
        shell_pid = int(shell_started["pid"])
        assert shell_pid != app_pid, session.diagnostics()
        assert int(shell_started["parent_pid"]) == app_pid, session.diagnostics()
        assert Path(str(shell_started["executable"])).name.casefold() == "kubectl.exe", (
            session.diagnostics()
        )
        assert shell_started["stdin_tty"] is True, session.diagnostics()
        assert shell_started["stdout_tty"] is True, session.diagnostics()
        assert "exec" in shell_started["argv"], session.diagnostics()
        assert "api-1" in shell_started["argv"], session.diagnostics()
        assert session.wait_for_output(
            b"korvid shell -> api-1", timeout=_remaining(deadline, 5.0)
        ), session.diagnostics()
        assert session.wait_for_output(
            b"KORVID_SMOKE_SHELL_READY", timeout=_remaining(deadline, 5.0)
        ), session.diagnostics()
        assert session.wait_for_output(
            b"\x1b[?1049l",
            after=suspend_output_start,
            timeout=_remaining(deadline, 5.0),
        ), session.diagnostics()

        shell_input_output_start = session.transcript.position()
        session.send(b"native-shell-input\r")
        shell_input = _phase(witnesses, "shell-input", session, deadline)
        assert shell_input["text"] == "native-shell-input", session.diagnostics()
        assert session.wait_for_output(
            b"KORVID_SMOKE_SHELL_INPUT:native-shell-input",
            after=shell_input_output_start,
            timeout=_remaining(deadline, 5.0),
        ), session.diagnostics()
        resume_output_start = session.transcript.position()
        session.send(b"exit\r")
        _phase(witnesses, "shell-child-exited", session, deadline)
        resumed = _phase(witnesses, "resumed", session, deadline)
        assert _thread_count(resumed, "textual-input") == 1, session.diagnostics()
        assert _thread_count(resumed, "textual-output") == 1, session.diagnostics()
        assert session.wait_for_output(
            b"\x1b[?1049h",
            after=resume_output_start,
            timeout=_remaining(deadline, 5.0),
        ), session.diagnostics()

        _send_filter_pattern(
            session.send,
            lambda: _phase(witnesses, "post-resume-filter-focused", session, deadline),
            "worker",
        )
        post_filter = _phase(witnesses, "post-resume-filter-applied", session, deadline)
        assert post_filter["filter"] == "worker", session.diagnostics()
        assert post_filter["rows"] == 1, session.diagnostics()
        assert post_filter["filter_open"] is False, session.diagnostics()
        session.send(b"/")
        clear_focus = _phase(witnesses, "post-resume-filter-open", session, deadline)
        assert clear_focus["filter_focused"] is True, session.diagnostics()
        session.send(b"\x1b")
        post_resume = _phase(witnesses, "post-resume-input", session, deadline)
        assert post_resume["filter"] == "", session.diagnostics()
        assert post_resume["rows"] == 2, session.diagnostics()

        session.send(b"q")
        assert session.wait(timeout=_remaining(deadline, _PHASE_TIMEOUT)) == 0, (
            session.diagnostics()
        )

    exited = witnesses.wait("app-exited", timeout=_remaining(deadline, 2.0))
    assert exited["return_code"] == 0
    assert _thread_count(exited, "textual-input") == 0
    assert _thread_count(exited, "textual-output") == 0
    assert wait_for_process_exit(launcher_pid, timeout=_remaining(deadline, 2.0))
    assert wait_for_process_exit(app_pid, timeout=_remaining(deadline, 2.0))
    assert wait_for_process_exit(shell_pid, timeout=_remaining(deadline, 2.0))
    assert session.owned_resource_count == 0, "ConPTY still owns native resources"
    assert session.reader_alive is False
    # Process-wide totals vary with UI state; exact ownership checks above are the gate.
    (root / "witness" / "host-cleanup.json").write_text(
        json.dumps(
            {
                "owned_resources": session.owned_resource_count,
                "reader_alive": session.reader_alive,
                "host_handles": {"before": handles_before, "after": process_handle_count()},
                "app_handles": {
                    "filtered": filtered["process_handles"],
                    "after_input": post_resume["process_handles"],
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
