"""Structural contracts for the Textual application shell."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

UI = Path(__file__).parents[2] / "src" / "korvid" / "ui"
UI_PACKAGE = ("korvid", "ui")


def _tree(name: str) -> ast.Module:
    return ast.parse((UI / name).read_text(encoding="utf-8"), filename=name)


def _is_type_checking_guard(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "typing"
        and node.attr == "TYPE_CHECKING"
    )


class _RuntimeImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.modules: set[str] = set()

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking_guard(node.test):
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.modules.update(alias.name for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            package = UI_PACKAGE[: len(UI_PACKAGE) - node.level + 1]
            base = ".".join((*package, *(node.module or "").split(".")))
        else:
            base = node.module or ""
        self.modules.add(base)
        separator = "" if base.endswith(".") else "."
        self.modules.update(f"{base}{separator}{alias.name}" for alias in node.names)


def _runtime_imports(tree: ast.Module) -> set[str]:
    visitor = _RuntimeImportVisitor()
    visitor.visit(tree)
    return visitor.modules


def _app_runtime_imports(name: str) -> list[str]:
    return sorted(
        module
        for module in _runtime_imports(_tree(name))
        if module == "korvid.ui.app"
        or module.startswith("korvid.ui.app.")
        or module == ".app"
        or module.startswith(".app.")
    )


def test_app_support_modules_do_not_import_the_app_at_runtime() -> None:
    for name in ("app_bindings.py", "app_surfaces.py"):
        assert not _app_runtime_imports(name), f"runtime import of korvid.ui.app in {name}"


def test_app_module_retains_only_the_textual_shell_responsibilities() -> None:
    source = (UI / "app.py").read_text(encoding="utf-8")
    assert "class AppUIBridge" not in source
    assert "class AppWorkspaceSurface" not in source
    assert 'Binding("q", "quit"' not in source
    assert "ContextSwitchCoordinator(" not in source
    assert "AgentUiController(" not in source


def test_app_runtime_does_not_import_the_app_at_runtime() -> None:
    assert not _app_runtime_imports("app_runtime.py"), (
        "runtime import of korvid.ui.app in app_runtime.py"
    )


@pytest.mark.parametrize(
    "source",
    [
        "def load_at_runtime():\n    import korvid.ui.app\n",
        "from korvid.ui import app\n",
        "from ..ui import app\n",
    ],
)
@pytest.mark.parametrize(
    "contract",
    [
        test_app_support_modules_do_not_import_the_app_at_runtime,
        test_app_runtime_does_not_import_the_app_at_runtime,
    ],
)
def test_app_structure_contracts_reject_all_runtime_app_import_forms(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    contract: Callable[[], None],
) -> None:
    monkeypatch.setitem(globals(), "_tree", lambda _name: ast.parse(source))

    with pytest.raises(AssertionError, match=r"runtime import of korvid\.ui\.app"):
        contract()


def test_late_runtime_reference_fails_before_binding_and_cannot_rebind() -> None:
    from korvid.ui.app_runtime import _LateReference

    reference = _LateReference[object]()
    with pytest.raises(RuntimeError, match="read before binding"):
        reference.get()

    value = object()
    reference.bind(value)
    assert reference.get() is value
    with pytest.raises(RuntimeError, match="already bound"):
        reference.bind(object())
