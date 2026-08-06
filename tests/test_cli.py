"""Tests for the lightweight console entrypoint."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

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
    assert result.stdout == "korvid 0.1.1\n"


def test_console_entrypoint_delegates_to_the_app_composition_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anything that is not the exact version-only invocation must reach
    `korvid.__main__.main` unchanged."""
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

    assert capsys.readouterr().out.strip() == "korvid 0.1.1"
