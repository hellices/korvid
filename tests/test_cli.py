"""Tests for the lightweight console entrypoint."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import korvid

ROOT = Path(__file__).resolve().parents[1]

_BLOCKED_IMPORTS = (
    "korvid.__main__",
    "korvid.agent",
    "korvid.core",
    "korvid.k8s",
    "korvid.mcp",
    "korvid.tools",
    "korvid.ui",
    "textual",
)


def test_console_version_entrypoint_avoids_startup_imports() -> None:
    """`korvid --version` must resolve via a lightweight entrypoint only."""
    entrypoint = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"][
        "korvid"
    ]
    module_name, function_name = entrypoint.split(":")
    probe = f"""
import importlib
import importlib.abc
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "src"))

BLOCKED = {_BLOCKED_IMPORTS!r}

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        for blocked in BLOCKED:
            if fullname == blocked or fullname.startswith(blocked + "."):
                raise SystemExit(f"blocked import: {{fullname}}")
        return None

sys.meta_path.insert(0, Blocker())
entrypoint = importlib.import_module({module_name!r})
sys.argv = ["korvid", "--version"]
try:
    getattr(entrypoint, {function_name!r})()
except SystemExit as exc:
    if exc.code != 0:
        raise SystemExit(f"unexpected exit code: {{exc.code!r}}")
else:
    raise SystemExit("expected SystemExit(0)")
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"korvid {korvid.__version__}\n"


def test_console_entrypoint_delegates_to_the_app_composition_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal TUI arguments must reach the composition root unchanged."""
    import korvid.__main__ as app_main
    import korvid.cli as cli

    calls: list[list[str]] = []
    monkeypatch.setattr(app_main, "main", lambda: calls.append(list(sys.argv)))
    monkeypatch.setattr(sys, "argv", ["korvid", "--readonly", "-n", "team-a"])

    cli.main()

    assert calls == [["korvid", "--readonly", "-n", "team-a"]]


def test_console_entrypoint_does_not_shortcut_version_used_as_a_flag_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`korvid -n --version` sets the namespace to `--version` in the real
    parser; the fast path must not diverge and print a version instead."""
    import korvid.__main__ as app_main
    import korvid.cli as cli

    calls: list[list[str]] = []
    monkeypatch.setattr(app_main, "main", lambda: calls.append(list(sys.argv)))
    monkeypatch.setattr(sys, "argv", ["korvid", "-n", "--version"])

    cli.main()

    assert calls == [["korvid", "-n", "--version"]]


def test_console_entrypoint_takes_the_fast_path_only_for_the_exact_version_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import korvid.__main__ as app_main
    import korvid.cli as cli

    monkeypatch.setattr(app_main, "main", lambda: pytest.fail("startup must not run"))
    monkeypatch.setattr(sys, "argv", ["korvid", "--version"])

    with pytest.raises(SystemExit, match="0"):
        cli.main()

    assert capsys.readouterr().out.strip() == f"korvid {korvid.__version__}"


def test_stdio_help_does_not_import_the_app() -> None:
    probe = """
import importlib.abc
import sys

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"korvid.__main__", "korvid.ui", "korvid.providers", "korvid.k8s", "textual"}:
            raise RuntimeError(f"stdio imported application code: {fullname}")

sys.meta_path.insert(0, Blocker())
from korvid.cli import main
sys.argv = ["korvid", "mcp", "stdio", "--help"]
main()
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "--instance" in result.stdout


@pytest.mark.parametrize("instance", ["0", "-1", "not-a-pid"])
def test_stdio_rejects_invalid_instance_before_startup(instance: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from korvid.cli import main; main()",
            "mcp",
            "stdio",
            "--instance",
            instance,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "positive" in result.stderr
    assert result.stdout == ""


def test_module_entrypoint_supports_stdio_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "korvid", "mcp", "stdio", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--instance" in result.stdout


def test_stdio_missing_extra_has_an_install_hint_without_app_startup() -> None:
    probe = """
import importlib.util
import sys
real_find_spec = importlib.util.find_spec
importlib.util.find_spec = lambda name: None if name == "httpx2" else real_find_spec(name)
from korvid.cli import main
sys.argv = ["korvid", "mcp", "stdio"]
try:
    main()
finally:
    assert "korvid.__main__" not in sys.modules
    assert "korvid.mcp.stdio" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "including mcp" in result.stderr
    assert "uv tool install" in result.stderr
    assert "Traceback" not in result.stderr
