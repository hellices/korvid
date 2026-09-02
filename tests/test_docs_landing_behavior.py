"""Behavior checks for the landing page's JavaScript controllers."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
JS_TESTS = ROOT / "tests" / "js"


def _run_harness(name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(JS_TESTS / name)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scene_switcher_behavior() -> None:
    result = _run_harness("scene_switcher_harness.mjs")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "not ok" not in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scene_fallback_behavior() -> None:
    result = _run_harness("scene_fallback_harness.mjs")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "not ok" not in result.stdout
