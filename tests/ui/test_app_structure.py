"""Structural contracts for the Textual application shell."""

from __future__ import annotations

import ast
from pathlib import Path

UI = Path(__file__).parents[2] / "src" / "korvid" / "ui"


def _tree(name: str) -> ast.Module:
    return ast.parse((UI / name).read_text(encoding="utf-8"), filename=name)


def test_app_support_modules_do_not_import_the_app_at_runtime() -> None:
    for name in ("app_bindings.py", "app_surfaces.py"):
        imports = [node.module for node in _tree(name).body if isinstance(node, ast.ImportFrom)]
        assert "korvid.ui.app" not in imports


def test_app_module_retains_only_the_textual_shell_responsibilities() -> None:
    source = (UI / "app.py").read_text(encoding="utf-8")
    assert "class AppUIBridge" not in source
    assert "class AppWorkspaceSurface" not in source
    assert 'Binding("q", "quit"' not in source
    assert "ContextSwitchCoordinator(" in source  # Removed in the next extraction.
