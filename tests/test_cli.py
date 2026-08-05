"""Tests for the lightweight console entrypoint."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

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
    assert result.stdout == "korvid 0.1.0\n"
