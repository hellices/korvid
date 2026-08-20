"""The shipped operation-eval modules must never reach `ui` or `core`.

Textual stays confined to `ui/` (AGENTS.md) and `tach.toml` gives
`korvid.evals` no `korvid.core` dependency. The check runs in a subprocess
so nothing already cached in this test process can mask a regression.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_SHIPPED_OPERATION_MODULES = (
    "korvid.evals.operation",
    "korvid.evals.operation_journal",
    "korvid.evals.operation_outcome",
    "korvid.evals.operation_state",
    "korvid.evals.operation_grader",
)

_PROBE = """
import sys

import {module}  # noqa: F401

forbidden = [
    name
    for name in sys.modules
    if name == "textual"
    or name.startswith("textual.")
    or name == "korvid.ui"
    or name.startswith("korvid.ui.")
    or name == "korvid.core"
    or name.startswith("korvid.core.")
]
if forbidden:
    raise SystemExit(f"shipped operation eval module reached forbidden layers: {{forbidden}}")
"""


@pytest.mark.parametrize("module", _SHIPPED_OPERATION_MODULES)
def test_shipped_operation_modules_do_not_import_ui_or_core(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
