"""Behavior checks for the landing page's JavaScript controllers."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any, BinaryIO, cast

import psutil  # type: ignore[import-untyped]  # dependency ships without inline stubs
import pytest

ROOT = Path(__file__).parent.parent
JS_TESTS = ROOT / "tests" / "js"
_DIAGNOSTIC_LIMIT = 4096
_HARNESS_TIMEOUT = 10
_STARTUP_PROBE_TIMEOUT = 5
_NODE_ENV_NAMES = ("NODE_OPTIONS", "NODE_PATH", "NODE_EXTRA_CA_CERTS")
_PYTHON_PROCESS_PROBE = 'import os; os.write(2, b"probe:python-child-started\\n")'
_CJS_STARTUP_PROBE = r"""
const fs = require("node:fs");
fs.writeSync(2, "probe:cjs-boot\n");
const source = fs.readFileSync(process.argv[1]);
fs.writeSync(2, `probe:file-read bytes=${source.length}\n`);
import("node:vm").then(
  () => fs.writeSync(2, "probe:esm-vm-import-complete\n"),
  (error) => {
    fs.writeSync(2, `probe:esm-vm-import-failed name=${error.name}\n`);
    process.exitCode = 1;
  },
);
"""


class _SceneMarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.switchers = 0
        self.tabs: dict[str, str] = {}
        self.panels: dict[str, str] = {}
        self.video_panels: set[str] = set()
        self.fallback_panels: set[str] = set()
        self._inside_switcher = False
        self._current_panel: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "section" and "data-scene-switcher" in attributes:
            self.switchers += 1
            self._inside_switcher = True
        if not self._inside_switcher:
            return
        if attributes.get("role") == "tab":
            panel_id = attributes.get("aria-controls")
            tab_id = attributes.get("id")
            if panel_id is not None and tab_id is not None:
                self.tabs[panel_id] = tab_id
        if attributes.get("role") == "tabpanel":
            self._current_panel = attributes.get("id")
            tab_id = attributes.get("aria-labelledby")
            if self._current_panel is not None and tab_id is not None:
                self.panels[self._current_panel] = tab_id
        if self._current_panel is None:
            return
        if tag == "video" and "controls" in attributes:
            self.video_panels.add(self._current_panel)
        if (
            tag == "img"
            and "scene-panel__fallback" in (attributes.get("class") or "").split()
            and (attributes.get("alt") or "").strip()
        ):
            self.fallback_panels.add(self._current_panel)

    def handle_endtag(self, tag: str) -> None:
        if tag == "article":
            self._current_panel = None
        if tag == "section" and self._inside_switcher:
            self._inside_switcher = False


def _bounded_timeout_output(output: bytes | str | None) -> str:
    if output is None:
        return "<none>"
    text = output.decode(errors="replace") if isinstance(output, bytes) else output
    if len(text) <= _DIAGNOSTIC_LIMIT:
        return text
    head = _DIAGNOSTIC_LIMIT // 2
    tail = _DIAGNOSTIC_LIMIT - head
    omitted = len(text) - _DIAGNOSTIC_LIMIT
    return f"{text[:head]}\n... <{omitted} characters truncated from middle> ...\n{text[-tail:]}"


def _read_capture(stream: BinaryIO, *, limit: int | None = None) -> str:
    stream.flush()
    size = stream.seek(0, os.SEEK_END)
    if limit is None or size <= limit:
        stream.seek(0)
        return stream.read().decode("utf-8", errors="replace")

    marker = ""
    while True:
        captured = limit - len(marker)
        omitted = size - captured
        updated = f"\n... <{omitted} bytes truncated from middle> ...\n"
        if len(updated) == len(marker):
            marker = updated
            break
        marker = updated
    head = captured // 2
    tail = captured - head
    stream.seek(0)
    start = stream.read(head).decode("utf-8", errors="replace")
    stream.seek(-tail, os.SEEK_END)
    end = stream.read(tail).decode("utf-8", errors="replace")
    return f"{start}{marker}{end}"


def _run_startup_probe(label: str, command: list[str]) -> str:
    stdout: bytes | str | None
    stderr: bytes | str | None
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            timeout=_STARTUP_PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        status = f"timed out after {_STARTUP_PROBE_TIMEOUT} seconds"
        stdout = error.stdout
        stderr = error.stderr
    except OSError as error:
        return f"Startup probe: {label}\nstatus: diagnostic error {type(error).__name__}"
    else:
        status = f"exit {result.returncode}"
        stdout = result.stdout
        stderr = result.stderr
    return "\n".join(
        (
            f"Startup probe: {label}",
            f"status: {status}",
            f"stdout:\n{_bounded_timeout_output(stdout)}",
            f"stderr:\n{_bounded_timeout_output(stderr)}",
        )
    )


def _process_snapshot(pid: int) -> str:
    try:
        snapshot = psutil.Process(pid).as_dict(
            attrs=["status", "cpu_times", "num_threads", "memory_info"]
        )
        cpu_times = snapshot["cpu_times"]
        memory_info = snapshot["memory_info"]
        status = str(snapshot["status"])[:32]
        cpu_user = float(cpu_times.user)
        cpu_system = float(cpu_times.system)
        threads = int(snapshot["num_threads"])
        rss = int(memory_info.rss)
        vms = int(memory_info.vms)
    except (
        psutil.Error,
        OSError,
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        return f"Process snapshot: diagnostic-error={type(error).__name__}"
    return (
        f"Process snapshot: status={status} cpu-user-seconds={cpu_user:g} "
        f"cpu-system-seconds={cpu_system:g} threads={threads} "
        f"rss-bytes={rss} vms-bytes={vms}"
    )


def _bounded_process_wait(process: subprocess.Popen[bytes]) -> str:
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return "timed-out"
    except OSError as error:
        return f"error type={type(error).__name__}"
    return "reaped"


def _terminate_and_reap(process: subprocess.Popen[bytes], poll_state: int | None) -> str:
    if poll_state is not None:
        return f"already-exited={poll_state}"
    diagnostics: list[str] = []
    try:
        process.terminate()
    except OSError as error:
        diagnostics.append(f"terminate=error type={type(error).__name__}")
    wait_status = _bounded_process_wait(process)
    if wait_status == "reaped":
        if diagnostics:
            diagnostics.append("wait=reaped")
            return "; ".join(diagnostics)
        return "terminate=reaped"
    if wait_status != "timed-out":
        diagnostics.append(f"wait={wait_status}")

    try:
        process.kill()
    except OSError as error:
        diagnostics.append(f"kill=error type={type(error).__name__}")
        diagnostics.append(f"reap={_bounded_process_wait(process)}")
        return "; ".join(diagnostics)
    try:
        process.wait()
    except OSError as error:
        diagnostics.append("kill=sent")
        diagnostics.append(f"reap=error type={type(error).__name__}")
        return "; ".join(diagnostics)
    diagnostics.append("kill=reaped")
    return "; ".join(diagnostics)


def _run_harness(name: str) -> subprocess.CompletedProcess[str]:
    found = shutil.which("node")
    if found is None:
        raise RuntimeError("node is not installed")
    node = str(Path(found).resolve())
    preload = str(JS_TESTS / "harness_preload.cjs")
    harness = str(JS_TESTS / name)
    command = [node, "--require", preload, harness]
    with (
        tempfile.TemporaryFile(mode="w+b", dir=ROOT) as stdout_capture,
        tempfile.TemporaryFile(mode="w+b", dir=ROOT) as stderr_capture,
    ):
        started = monotonic()
        process = subprocess.Popen(
            command,
            stdout=stdout_capture,
            stderr=stderr_capture,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
        )
        try:
            returncode = process.wait(timeout=_HARNESS_TIMEOUT)
        except OSError:
            _terminate_and_reap(process, None)
            raise
        except subprocess.TimeoutExpired as error:
            poll_state = process.poll()
            elapsed_ms = max(0, int((monotonic() - started) * 1000))
            snapshot = _process_snapshot(process.pid)
            termination = _terminate_and_reap(process, poll_state)
            stdout_text = _read_capture(stdout_capture, limit=_DIAGNOSTIC_LIMIT)
            stderr_text = _read_capture(stderr_capture, limit=_DIAGNOSTIC_LIMIT)
            error.stdout = stdout_text.encode("utf-8")
            error.stderr = stderr_text.encode("utf-8")
            present = ", ".join(name for name in _NODE_ENV_NAMES if name in os.environ) or "<none>"
            process_probe = _run_startup_probe(
                "Python child process control",
                [
                    str(Path(sys.executable).resolve()),
                    "-I",
                    "-S",
                    "-c",
                    _PYTHON_PROCESS_PROBE,
                ],
            )
            version_probe = _run_startup_probe("node --version", [node, "--version"])
            loader_probe = _run_startup_probe(
                "CJS file and ESM loader",
                [node, "--eval", _CJS_STARTUP_PROBE, harness],
            )
            error.add_note(
                "\n".join(
                    (
                        f"Node harness: {name}",
                        f"Node executable: {node}",
                        "Node stdin: subprocess.DEVNULL (noninteractive harness)",
                        f"Node environment present: {present}",
                        f"Node process: pid={process.pid} poll={poll_state} elapsed-ms={elapsed_ms}",
                        snapshot,
                        f"Node termination: {termination}",
                        f"Captured stdout:\n{stdout_text}",
                        f"Captured stderr:\n{stderr_text}",
                        process_probe,
                        version_probe,
                        loader_probe,
                    )
                )
            )
            raise
        return subprocess.CompletedProcess(
            process.args,
            returncode,
            _read_capture(stdout_capture),
            _read_capture(stderr_capture),
        )


class _HarnessTimeoutProcess:
    pid = 4242

    def __init__(
        self,
        command: list[str],
        timeout_error: subprocess.TimeoutExpired,
        events: list[str],
    ) -> None:
        self.args = command
        self._timeout_error = timeout_error
        self._events = events

    def wait(self, timeout: float | None = None) -> int:
        self._events.append(f"wait:{timeout}")
        if timeout == _HARNESS_TIMEOUT:
            raise self._timeout_error
        return -15

    def poll(self) -> int | None:
        self._events.append("poll")
        return None

    def terminate(self) -> None:
        self._events.append("terminate")

    def kill(self) -> None:
        raise AssertionError("a process reaped after terminate must not be killed")


class _HarnessPopen:
    def __init__(
        self,
        *,
        command: list[str],
        process: _HarnessTimeoutProcess,
        stdout_output: bytes,
        stderr_output: bytes,
        spawn_options: dict[str, object],
        events: list[str],
    ) -> None:
        self._command = command
        self._process = process
        self._stdout_output = stdout_output
        self._stderr_output = stderr_output
        self._spawn_options = spawn_options
        self._events = events

    def __call__(self, popen_command: list[str], **options: object) -> _HarnessTimeoutProcess:
        assert popen_command == self._command
        self._spawn_options.update(options)
        stdout = cast(BinaryIO, options["stdout"])
        stderr = cast(BinaryIO, options["stderr"])
        stdout.write(self._stdout_output)
        stderr.write(self._stderr_output)
        stdout.flush()
        stderr.flush()
        self._events.append("spawn")
        return self._process


class _HarnessProbeRunner:
    def __init__(
        self,
        *,
        process_control: list[str],
        node: str,
        calls: list[tuple[list[str], dict[str, object]]],
    ) -> None:
        self._process_control = process_control
        self._node = node
        self._calls = calls

    def __call__(
        self, probe_command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        self._calls.append((probe_command, options))
        if probe_command == self._process_control:
            return subprocess.CompletedProcess(
                probe_command,
                0,
                "",
                "probe:python-child-started\n",
            )
        if probe_command == [self._node, "--version"]:
            return subprocess.CompletedProcess(probe_command, 0, "v22.23.2\n", "")
        if probe_command[:2] == [self._node, "--eval"]:
            raise subprocess.TimeoutExpired(
                probe_command,
                _STARTUP_PROBE_TIMEOUT,
                output=b"probe stdout",
                stderr=b"probe:cjs-boot\nprobe:file-read\n",
            )
        raise AssertionError(f"unexpected startup probe: {probe_command!r}")


class _HarnessPsutilProcess:
    def __init__(self, pid: int, *, events: list[str]) -> None:
        assert pid == 4242
        self._events = events

    def as_dict(self, attrs: list[str]) -> dict[str, object]:
        assert attrs == ["status", "cpu_times", "num_threads", "memory_info"]
        self._events.append("snapshot")
        return {
            "status": "running",
            "cpu_times": SimpleNamespace(user=1.25, system=0.5),
            "num_threads": 3,
            "memory_info": SimpleNamespace(rss=4096, vms=8192),
            "cmdline": ["node", "--token=must-not-appear"],
            "environ": {"TOKEN": "must-not-appear"},
            "open_files": ["/secret/must-not-appear"],
            "connections": ["https://must-not-appear.invalid"],
        }


def test_harness_timeout_preserves_bounded_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = str(ROOT / "node-diagnostic.exe")
    preload = str(JS_TESTS / "harness_preload.cjs")
    harness = str(JS_TESTS / "scene_fallback_harness.mjs")
    command = [resolved, "--require", preload, harness]
    python = str(Path(sys.executable).resolve())
    process_control = [
        python,
        "-I",
        "-S",
        "-c",
        'import os; os.write(2, b"probe:python-child-started\\n")',
    ]
    probe_calls: list[tuple[list[str], dict[str, object]]] = []
    spawn_options: dict[str, object] = {}
    events: list[str] = []
    stdout_output = (
        b"stdout-start\n" + b"x" * 10_000 + b"stdout-middle" + b"x" * 10_000 + b"stdout-tail"
    )
    stderr_output = (
        b"stderr-start\n" + b"y" * 10_000 + b"stderr-middle" + b"y" * 10_000 + b"stderr-tail"
    )
    original_timeout = subprocess.TimeoutExpired(command, 10)
    process = _HarnessTimeoutProcess(command, original_timeout, events)
    popen = _HarnessPopen(
        command=command,
        process=process,
        stdout_output=stdout_output,
        stderr_output=stderr_output,
        spawn_options=spawn_options,
        events=events,
    )
    run = _HarnessProbeRunner(
        process_control=process_control,
        node=resolved,
        calls=probe_calls,
    )

    monkeypatch.setattr(shutil, "which", lambda executable: resolved)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda pid: _HarnessPsutilProcess(pid, events=events),
    )
    for name in _NODE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NODE_OPTIONS", "must-not-appear")

    with pytest.raises(subprocess.TimeoutExpired, match="timed out") as raised:
        _run_harness("scene_fallback_harness.mjs")

    assert raised.value is original_timeout
    assert events == ["spawn", "wait:10", "poll", "snapshot", "terminate", "wait:2"]
    assert spawn_options["stdin"] is subprocess.DEVNULL
    assert spawn_options["stdout"] is not subprocess.PIPE
    assert spawn_options["stderr"] is not subprocess.PIPE
    assert spawn_options["cwd"] == ROOT
    assert "timeout" not in spawn_options
    assert "check" not in spawn_options
    assert [call[0] for call in probe_calls] == [
        process_control,
        [resolved, "--version"],
        [resolved, "--eval", _CJS_STARTUP_PROBE, harness],
    ]
    assert all(call[1]["timeout"] == 5 for call in probe_calls)
    assert all(call[1]["stdin"] is subprocess.DEVNULL for call in probe_calls)
    note = "\n".join(raised.value.__notes__)
    assert f"Node executable: {resolved}" in note
    assert "Node stdin: subprocess.DEVNULL" in note
    assert "Node environment present: NODE_OPTIONS" in note
    assert re.search(r"Node process: pid=4242 poll=None elapsed-ms=\d+", note)
    assert (
        "Process snapshot: status=running cpu-user-seconds=1.25 "
        "cpu-system-seconds=0.5 threads=3 rss-bytes=4096 vms-bytes=8192"
    ) in note
    assert "Node termination: terminate=reaped" in note
    assert "must-not-appear" not in note
    assert "Startup probe: Python child process control" in note
    assert "probe:python-child-started" in note
    assert "Startup probe: node --version" in note
    assert "status: exit 0" in note
    assert "v22.23.2" in note
    assert "Startup probe: CJS file and ESM loader" in note
    assert "status: timed out after 5 seconds" in note
    assert "probe:cjs-boot" in note
    assert "probe:file-read" in note
    assert "stdout-start" in note
    assert "stderr-start" in note
    assert "stdout-tail" in note
    assert "stderr-tail" in note
    assert "stdout-middle" not in note
    assert "stderr-middle" not in note
    assert "truncated" in note
    assert _bounded_timeout_output(None) == "<none>"
    assert _bounded_timeout_output("complete string") == "complete string"


def test_harness_reaps_child_before_reraising_initial_wait_os_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    resolved = str(ROOT / "node-wait-error.exe")
    initial_error = PermissionError("initial wait failed")

    class FakeProcess:
        pid = 5151

        def wait(self, timeout: float | None = None) -> int:
            events.append(f"wait:{timeout}")
            if timeout == _HARNESS_TIMEOUT:
                raise initial_error
            return -15

        def poll(self) -> int | None:
            raise AssertionError("wait-error cleanup must not trust the failed process handle")

        def terminate(self) -> None:
            events.append("terminate")

        def kill(self) -> None:
            raise AssertionError("a process reaped after terminate must not be killed")

    def popen(command: list[str], **options: object) -> FakeProcess:
        assert command[-1] == str(JS_TESTS / "harness_wait_error.mjs")
        assert options["stdin"] is subprocess.DEVNULL
        events.append("spawn")
        return FakeProcess()

    monkeypatch.setattr(shutil, "which", lambda executable: resolved)
    monkeypatch.setattr(subprocess, "Popen", popen)

    with pytest.raises(PermissionError, match="initial wait failed") as raised:
        _run_harness("harness_wait_error.mjs")

    assert raised.value is initial_error
    assert events == ["spawn", "wait:10", "terminate", "wait:2"]


def test_harness_timeout_escalates_from_terminate_to_kill() -> None:
    events: list[str] = []

    class FakeProcess:
        def __init__(self) -> None:
            self.args = ["node", "harness.mjs"]
            self.pid = 5252

        def terminate(self) -> None:
            events.append("terminate")

        def kill(self) -> None:
            events.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            events.append(f"wait:{timeout}")
            if sum(event.startswith("wait:") for event in events) == 1:
                assert timeout is not None
                raise subprocess.TimeoutExpired(self.args, timeout)
            return -9

    result = _terminate_and_reap(cast(Any, FakeProcess()), poll_state=None)

    assert result == "kill=reaped"
    assert events == ["terminate", "wait:2", "kill", "wait:None"]


def test_harness_timeout_waits_without_another_deadline_after_kill() -> None:
    events: list[str] = []

    class FakeProcess:
        def __init__(self) -> None:
            self.args = ["node", "harness.mjs"]
            self.pid = 5253

        def terminate(self) -> None:
            events.append("terminate")

        def kill(self) -> None:
            events.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            events.append(f"wait:{timeout}")
            if timeout is not None:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return -9

    result = _terminate_and_reap(cast(Any, FakeProcess()), poll_state=None)

    assert result == "kill=reaped"
    assert events == ["terminate", "wait:2", "kill", "wait:None"]


def test_harness_timeout_bounds_reap_when_kill_itself_fails() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def terminate(self) -> None:
            self.calls.append("terminate")

        def kill(self) -> None:
            self.calls.append("kill")
            raise PermissionError("denied")

        def wait(self, timeout: float | None = None) -> int:
            self.calls.append(f"wait:{timeout}")
            raise subprocess.TimeoutExpired("node", timeout or 0)

    process = FakeProcess()
    result = _terminate_and_reap(cast(Any, process), poll_state=None)

    assert process.calls == ["terminate", "wait:2", "kill", "wait:2"]
    assert result == "kill=error type=PermissionError; reap=timed-out"


def test_harness_timeout_preserves_diagnostics_when_final_reap_wait_fails() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def terminate(self) -> None:
            self.calls.append("terminate")

        def kill(self) -> None:
            self.calls.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            self.calls.append(f"wait:{timeout}")
            if timeout is not None:
                raise subprocess.TimeoutExpired("node", timeout)
            raise ChildProcessError("wait failed")

    process = FakeProcess()
    result = _terminate_and_reap(cast(Any, process), poll_state=None)

    assert process.calls == ["terminate", "wait:2", "kill", "wait:None"]
    assert result == "kill=sent; reap=error type=ChildProcessError"


@pytest.mark.parametrize("failure_point", ["terminate", "wait"])
def test_harness_timeout_reaps_after_termination_os_error(failure_point: str) -> None:
    events: list[str] = []

    class FakeProcess:
        def __init__(self) -> None:
            self.args = ["node", "harness.mjs"]
            self.pid = 5353
            self.wait_calls = 0

        def terminate(self) -> None:
            events.append("terminate")
            if failure_point == "terminate":
                raise OSError("terminate failed")

        def kill(self) -> None:
            events.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            events.append(f"wait:{timeout}")
            self.wait_calls += 1
            if self.wait_calls == 1:
                if failure_point == "wait":
                    raise OSError("wait failed")
                assert timeout is not None
                raise subprocess.TimeoutExpired(self.args, timeout)
            return -9

    result = _terminate_and_reap(cast(Any, FakeProcess()), poll_state=None)

    assert result == f"{failure_point}=error type=OSError; kill=reaped"
    assert events == ["terminate", "wait:2", "kill", "wait:None"]


def _assert_elapsed_milestones(stderr: str) -> None:
    milestones = [line for line in stderr.splitlines() if " stage=" in line]
    assert milestones
    assert milestones[0] == "korvid-harness stage=node-started elapsed-ms=0"
    assert all(re.search(r" elapsed-ms=\d+$", line) for line in milestones)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_harness_waits_for_node_exit_not_inherited_output_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = ROOT / f".docs-harness-descendant-{os.getpid()}.pid"
    pid_file.unlink(missing_ok=True)
    monkeypatch.setenv("KORVID_HARNESS_DESCENDANT_PID_FILE", str(pid_file))
    try:
        result = _run_harness("harness_inherited_output_handle.mjs")

        assert result.returncode == 0
        assert "parent-complete" in result.stderr
    finally:
        if pid_file.exists():
            descendant_pid = int(pid_file.read_text())
            os.kill(descendant_pid, signal.SIGTERM)
            pid_file.unlink()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_harness_true_hang_preserves_bounded_file_diagnostics() -> None:
    with pytest.raises(subprocess.TimeoutExpired, match="timed out") as raised:
        _run_harness("harness_true_hang.mjs")

    assert 0 < raised.value.timeout <= 10
    assert raised.value.cmd[-1] == str(JS_TESTS / "harness_true_hang.mjs")
    assert isinstance(raised.value.stdout, bytes)
    assert isinstance(raised.value.stderr, bytes)
    assert len(raised.value.stdout) <= _DIAGNOSTIC_LIMIT
    assert len(raised.value.stderr) <= _DIAGNOSTIC_LIMIT
    note = "\n".join(raised.value.__notes__)
    assert "stdout-start" in note
    assert "stderr-start" in note
    assert "stdout-tail" in note
    assert "stderr-tail" in note
    assert "stdout-middle" not in note
    assert "stderr-middle" not in note
    assert "truncated" in note
    assert "harness-hang stage=complete exit-code=0" in note
    assert "active-resources=" in note
    assert "Timeout" in note
    assert "harness-hang stage=before-exit" not in note
    assert "harness-hang stage=exit " not in note
    _assert_elapsed_milestones(note)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_harness_nonzero_exit_preserves_captured_output() -> None:
    result = _run_harness("harness_nonzero_exit.mjs")

    assert result.returncode == 7
    assert result.stdout == "contract stdout\n"
    assert "contract stderr\n" in result.stderr
    assert "harness-contract stage=before-exit exit-code=7" in result.stderr
    assert "harness-contract stage=exit exit-code=7" in result.stderr
    _assert_elapsed_milestones(result.stderr)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scene_switcher_behavior() -> None:
    result = _run_harness("scene_switcher_harness.mjs")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "not ok" not in result.stdout
    assert "scene-switcher stage=complete" in result.stderr
    assert "scene-switcher stage=before-exit exit-code=0" in result.stderr
    assert "scene-switcher stage=exit exit-code=0" in result.stderr
    assert "active-resources=" in result.stderr
    assert "active-handles=" in result.stderr
    assert "active-requests=" in result.stderr
    _assert_elapsed_milestones(result.stderr)


def test_landing_markup_connects_scene_controls_to_fallback_content() -> None:
    parser = _SceneMarkupParser()
    parser.feed((ROOT / "docs" / "index.md").read_text())

    assert parser.switchers == 1
    assert parser.tabs
    assert parser.tabs == parser.panels
    assert set(parser.panels) == parser.video_panels == parser.fallback_panels


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scene_fallback_behavior() -> None:
    result = _run_harness("scene_fallback_harness.mjs")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "not ok" not in result.stdout
    assert "scene-fallback stage=complete" in result.stderr
    assert "scene-fallback stage=before-exit exit-code=0" in result.stderr
    assert "scene-fallback stage=exit exit-code=0" in result.stderr
    _assert_elapsed_milestones(result.stderr)
