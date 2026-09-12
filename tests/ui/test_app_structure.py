"""Structural contracts for the Textual application shell."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

ROOT = Path(__file__).parents[2]
UI = ROOT / "src" / "korvid" / "ui"
MAIN = ROOT / "src" / "korvid" / "__main__.py"
TESTS = ROOT / "tests"
TEST_APP_FACTORY = TESTS / "app_factory.py"
UI_PACKAGE = ("korvid", "ui")

RUNTIME_COMPONENTS = {
    "AgentUiController",
    "AppAgentPanel",
    "AppAgentScreens",
    "AppContextSurface",
    "AppContextDispatch",
    "AppInspectSurface",
    "AppProposalEvents",
    "AppProposalScreens",
    "AppReviewTasks",
    "AppRuntime",
    "AppSessionConfiguration",
    "AppTransferScreens",
    "AppUiSurface",
    "AppViewState",
    "AppWorkspaceSurface",
    "CommandRouter",
    "ContextSwitchCoordinator",
    "DebugController",
    "DrainController",
    "ForwardController",
    "HelmController",
    "HintController",
    "IntegrationController",
    "LogController",
    "OperatorController",
    "ProposalController",
    "RelationshipSnapshotLoader",
    "ResourceInspectController",
    "ResourceWriteController",
    "SessionTimelineController",
    "ShellController",
    "TransferController",
    "WorkspaceController",
    "WorkspaceState",
    "WriteCoordinator",
}


def _tree(name: str) -> ast.Module:
    return ast.parse((UI / name).read_text(encoding="utf-8"), filename=name)


def _called_names(tree: ast.AST) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


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


def test_only_the_composition_root_constructs_the_app_runtime() -> None:
    ui_calls = set().union(
        *(_called_names(ast.parse(path.read_text(encoding="utf-8"))) for path in UI.rglob("*.py"))
    )
    assert not ui_calls.intersection(RUNTIME_COMPONENTS)
    assert _called_names(ast.parse(MAIN.read_text(encoding="utf-8"))) >= RUNTIME_COMPONENTS


def test_tests_construct_apps_only_through_the_factory() -> None:
    direct_constructions = {
        f"{path.relative_to(ROOT)}:{node.lineno}: {node.func.id}"
        for path in TESTS.rglob("*.py")
        if path != TEST_APP_FACTORY
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.endswith("KorvidApp")
    }
    assert not direct_constructions, "\n".join(sorted(direct_constructions))


def test_app_runtime_can_be_bound_only_once() -> None:
    from korvid.ui.app import KorvidApp
    from korvid.ui.app_runtime import AppRuntime

    app = object.__new__(KorvidApp)
    app._runtime_bound = False
    value = object()
    runtime = cast(
        "AppRuntime",
        SimpleNamespace(
            view=value,
            relationship_loader=value,
            context=value,
            timeline=value,
            writes=value,
            bridge_dispatch=value,
            inspect_surface=value,
            inspect=value,
            shell=value,
            forward=value,
            transfer=value,
            operators=value,
            helm=value,
            debug=value,
            drain=value,
            resource_writes=value,
            workspace=value,
            hints=value,
            logs=value,
            workspace_controller=value,
            proposals=value,
            integrations=value,
            agent_ui=value,
            commands=value,
        ),
    )

    app.bind_runtime(runtime)

    with pytest.raises(RuntimeError, match="app runtime already bound"):
        app.bind_runtime(runtime)


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
