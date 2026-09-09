"""Behavior checks for the landing page's JavaScript controllers."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import BinaryIO, cast

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


def _run_harness(name: str) -> subprocess.CompletedProcess[str]:
    found = shutil.which("node")
    if found is None:
        raise RuntimeError("node is not installed")
    node = str(Path(found).resolve())
    command = [node, str(JS_TESTS / name)]
    with (
        tempfile.TemporaryFile(mode="w+b", dir=ROOT) as stdout_capture,
        tempfile.TemporaryFile(mode="w+b", dir=ROOT) as stderr_capture,
    ):
        try:
            result = subprocess.run(
                command,
                stdout=stdout_capture,
                stderr=stderr_capture,
                check=False,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                timeout=_HARNESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired as error:
            stdout_text = _read_capture(stdout_capture, limit=_DIAGNOSTIC_LIMIT)
            stderr_text = _read_capture(stderr_capture, limit=_DIAGNOSTIC_LIMIT)
            error.stdout = stdout_text.encode("utf-8")
            error.stderr = stderr_text.encode("utf-8")
            harness = str(JS_TESTS / name)
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
            result.args,
            result.returncode,
            _read_capture(stdout_capture),
            _read_capture(stderr_capture),
        )


def test_harness_timeout_preserves_bounded_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = str(ROOT / "node-diagnostic.exe")
    python = str(Path(sys.executable).resolve())
    process_control = [
        python,
        "-I",
        "-S",
        "-c",
        'import os; os.write(2, b"probe:python-child-started\\n")',
    ]
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    stdout_output = (
        b"stdout-start\n" + b"x" * 10_000 + b"stdout-middle" + b"x" * 10_000 + b"stdout-tail"
    )
    stderr_output = (
        b"stderr-start\n" + b"y" * 10_000 + b"stderr-middle" + b"y" * 10_000 + b"stderr-tail"
    )
    original_timeout = subprocess.TimeoutExpired(
        [resolved, str(JS_TESTS / "scene_fallback_harness.mjs")],
        10,
    )

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        command = args[0]
        assert isinstance(command, list)
        if len(calls) == 1:
            stdout = cast(BinaryIO, kwargs["stdout"])
            stderr = cast(BinaryIO, kwargs["stderr"])
            stdout.write(stdout_output)
            stderr.write(stderr_output)
            stdout.flush()
            stderr.flush()
            raise original_timeout
        if command == process_control:
            return subprocess.CompletedProcess(
                command,
                0,
                "",
                "probe:python-child-started\n",
            )
        if command == [resolved, "--version"]:
            return subprocess.CompletedProcess(command, 0, "v22.23.2\n", "")
        raise subprocess.TimeoutExpired(
            command,
            5,
            output=b"probe stdout",
            stderr=b"probe:cjs-boot\nprobe:file-read\n",
        )

    monkeypatch.setattr(shutil, "which", lambda executable: resolved)
    monkeypatch.setattr(subprocess, "run", run)
    for name in ("NODE_OPTIONS", "NODE_PATH", "NODE_EXTRA_CA_CERTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NODE_OPTIONS", "must-not-appear")

    with pytest.raises(subprocess.TimeoutExpired, match="timed out") as raised:
        _run_harness("scene_fallback_harness.mjs")

    assert raised.value is original_timeout
    assert len(calls) == 4
    command = calls[0][0][0]
    options = calls[0][1]
    assert command == [resolved, str(JS_TESTS / "scene_fallback_harness.mjs")]
    assert options["timeout"] == 10
    assert options["stdin"] is subprocess.DEVNULL
    assert "capture_output" not in options
    assert options["stdout"] is not subprocess.PIPE
    assert options["stderr"] is not subprocess.PIPE
    assert calls[1][0][0] == process_control
    assert calls[1][1]["timeout"] == 5
    assert calls[1][1]["stdin"] is subprocess.DEVNULL
    assert calls[2][0][0] == [resolved, "--version"]
    assert calls[2][1]["timeout"] == 5
    assert calls[2][1]["stdin"] is subprocess.DEVNULL
    cjs_command = calls[3][0][0]
    assert isinstance(cjs_command, list)
    assert cjs_command[:2] == [resolved, "--eval"]
    assert cjs_command[-1] == str(JS_TESTS / "scene_fallback_harness.mjs")
    assert calls[3][1]["timeout"] == 5
    assert calls[3][1]["stdin"] is subprocess.DEVNULL
    note = "\n".join(raised.value.__notes__)
    assert f"Node executable: {resolved}" in note
    assert "Node stdin: subprocess.DEVNULL" in note
    assert "Node environment present: NODE_OPTIONS" in note
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_harness_nonzero_exit_preserves_captured_output() -> None:
    result = _run_harness("harness_nonzero_exit.mjs")

    assert result.returncode == 7
    assert result.stdout == "contract stdout\n"
    assert result.stderr == "contract stderr\n"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scene_switcher_behavior() -> None:
    result = _run_harness("scene_switcher_harness.mjs")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "not ok" not in result.stdout
    assert "scene-switcher stage=complete" in result.stderr


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
