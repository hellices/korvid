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
    app_pid = session.pid
    shell_pid = 0
    with session:
        mounted = _phase(witnesses, "mounted", session, deadline)
        resources = _phase(witnesses, "resources-ready", session, deadline)
        context = session.diagnostics()
        assert mounted["driver"] == "textual.drivers.windows_driver.WindowsDriver", context
        assert mounted["stdin_tty"] is True, context
        assert mounted["stdout_tty"] is True, context
        assert mounted["headless"] is False, context
        assert mounted["can_suspend"] is True, context
        assert resources["rows"] == 2, context
        assert _thread_count(resources, "textual-input") == 1, context
        assert _thread_count(resources, "textual-output") == 1, context

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
        assert int(post_resume["process_handles"]) <= int(filtered["process_handles"]) + 2, (
            session.diagnostics()
        )

        session.send(b"q")
        assert session.wait(timeout=_remaining(deadline, _PHASE_TIMEOUT)) == 0, (
            session.diagnostics()
        )

    exited = witnesses.wait("app-exited", timeout=_remaining(deadline, 2.0))
    assert exited["return_code"] == 0
    assert _thread_count(exited, "textual-input") == 0
    assert _thread_count(exited, "textual-output") == 0
    assert wait_for_process_exit(app_pid, timeout=_remaining(deadline, 2.0))
    assert wait_for_process_exit(shell_pid, timeout=_remaining(deadline, 2.0))
    assert session.owned_resource_count == 0
    assert session.reader_alive is False
    assert process_handle_count() <= handles_before + 1
