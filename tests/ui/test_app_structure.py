"""Structural contracts for the Textual application shell."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

ROOT = Path(__file__).parents[2]
KORVID = ROOT / "src" / "korvid"
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
    "AppUIBridge",
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
    "_RelationshipLister",
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


class _CallTargetVisitor(ast.NodeVisitor):
    """Resolve call targets without leaking aliases across runtime scopes."""

    def __init__(
        self,
        runtime_module_targets: dict[str, frozenset[str]] | None = None,
        runtime_scope_targets: dict[ast.AST, dict[str, frozenset[str]]] | None = None,
    ) -> None:
        self.targets: list[tuple[int, str]] = []
        self._scopes: list[dict[str, frozenset[str] | None]] = [{}]
        self._scope_kinds = ["module"]
        self._scope_nodes: list[ast.AST | None] = [None]
        self._global_names: list[set[str]] = [set()]
        self._nonlocal_names: list[set[str]] = [set()]
        self._runtime_module_targets = runtime_module_targets or {}
        self._runtime_scope_targets = runtime_scope_targets or {}
        self._module_target_history: dict[str, set[str]] = {}
        self._scope_target_history: dict[ast.AST, dict[str, set[str]]] = {}

    def _resolve_module_name(self, name: str) -> frozenset[str]:
        has_binding = name in self._scopes[0]
        current = self._scopes[0].get(name)
        resolved = frozenset() if current is None else current
        resolved |= self._runtime_module_targets.get(name, frozenset())
        if resolved or has_binding:
            return resolved
        return frozenset((name,))

    def _resolve_name(self, name: str) -> frozenset[str]:
        if name in self._global_names[-1]:
            return self._resolve_module_name(name)
        runtime_global_lookup = self._scope_kinds[-1] in {
            "function",
            "lambda",
            "comprehension",
        }
        start = len(self._scopes) - (2 if name in self._nonlocal_names[-1] else 1)
        for index in range(start, -1, -1):
            kind = self._scope_kinds[index]
            scope = self._scopes[index]
            if runtime_global_lookup and kind == "class":
                continue
            closure_targets = frozenset[str]()
            has_closure_binding = False
            if (
                runtime_global_lookup
                and index < len(self._scopes) - 1
                and kind in {"function", "lambda", "comprehension"}
            ):
                scope_node = self._scope_nodes[index]
                if scope_node is not None:
                    history = self._runtime_scope_targets.get(scope_node, {})
                    has_closure_binding = name in history
                    closure_targets = history.get(name, frozenset())
            if name in scope:
                targets = scope[name]
                resolved = frozenset() if targets is None else targets
                if runtime_global_lookup and kind == "module":
                    return self._resolve_module_name(name)
                return resolved | closure_targets
            if has_closure_binding:
                return closure_targets
        if runtime_global_lookup and name in self._runtime_module_targets:
            return self._runtime_module_targets[name]
        return frozenset((name,))

    def _binding_scope_index(self, name: str) -> int:
        if name in self._global_names[-1]:
            return 0
        if name in self._nonlocal_names[-1]:
            for index in range(len(self._scopes) - 2, -1, -1):
                if (
                    self._scope_kinds[index] in {"function", "lambda"}
                    and name in self._scopes[index]
                ):
                    return index
            for index in range(len(self._scopes) - 2, -1, -1):
                if self._scope_kinds[index] in {"function", "lambda"}:
                    return index
        return len(self._scopes) - 1

    def _record_scope_binding(self, index: int, name: str, aliases: frozenset[str]) -> None:
        if index == 0 or self._scope_kinds[index] not in {
            "function",
            "lambda",
            "comprehension",
        }:
            return
        scope_node = self._scope_nodes[index]
        if scope_node is not None:
            history = self._scope_target_history.setdefault(scope_node, {})
            history.setdefault(name, set()).update(aliases)

    def _bind_aliases(self, name: str, aliases: frozenset[str]) -> None:
        index = self._binding_scope_index(name)
        self._scopes[index][name] = aliases or None
        self._record_scope_binding(index, name, aliases)
        if aliases and index == 0:
            self._module_target_history.setdefault(name, set()).update(aliases)

    def _bind_unknown(self, name: str) -> None:
        index = self._binding_scope_index(name)
        self._scopes[index][name] = None
        self._record_scope_binding(index, name, frozenset())

    def _reference_targets(self, value: ast.expr) -> frozenset[str]:
        if isinstance(value, ast.Name):
            return self._resolve_name(value.id)
        if isinstance(value, ast.Attribute):
            return frozenset((value.attr,))
        if isinstance(value, ast.IfExp):
            return self._reference_targets(value.body) | self._reference_targets(value.orelse)
        if isinstance(value, ast.BoolOp):
            return frozenset(
                target for operand in value.values for target in self._reference_targets(operand)
            )
        if isinstance(value, (ast.List, ast.Tuple)):
            return frozenset(
                target for element in value.elts for target in self._reference_targets(element)
            )
        return frozenset()

    def _default_argument_targets(self, arguments: ast.arguments) -> dict[str, frozenset[str]]:
        targets: dict[str, frozenset[str]] = {}
        positional = (*arguments.posonlyargs, *arguments.args)
        first_default = len(positional) - len(arguments.defaults)
        for argument, positional_default in zip(
            positional[first_default:], arguments.defaults, strict=True
        ):
            self.visit(positional_default)
            targets[argument.arg] = self._reference_targets(positional_default)
        for argument, keyword_default in zip(
            arguments.kwonlyargs, arguments.kw_defaults, strict=True
        ):
            if keyword_default is not None:
                self.visit(keyword_default)
                targets[argument.arg] = self._reference_targets(keyword_default)
        return targets

    def _bind_arguments(
        self,
        arguments: ast.arguments,
        default_targets: dict[str, frozenset[str]],
    ) -> None:
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ):
            self._bind_aliases(argument.arg, default_targets.get(argument.arg, frozenset()))
        if arguments.vararg is not None:
            self._bind_unknown(arguments.vararg.arg)
        if arguments.kwarg is not None:
            self._bind_unknown(arguments.kwarg.arg)

    def _bind_target(self, target: ast.expr) -> None:
        if isinstance(target, ast.Name):
            self._bind_unknown(target.id)
        elif isinstance(target, (ast.List, ast.Tuple)):
            for element in target.elts:
                self._bind_target(element)

    def _bind_assignment(self, target: ast.expr, aliases: frozenset[str]) -> None:
        if isinstance(target, ast.Name):
            self._bind_aliases(target.id, aliases)
        elif isinstance(target, (ast.List, ast.Tuple)):
            for element in target.elts:
                self._bind_assignment(element, aliases)
        else:
            self._bind_target(target)

    def _visit_branch(
        self,
        statements: list[ast.stmt],
        initial: dict[str, frozenset[str] | None],
    ) -> dict[str, frozenset[str] | None]:
        self._scopes[-1] = initial.copy()
        for statement in statements:
            self.visit(statement)
        return self._scopes[-1].copy()

    def _merge_branches(
        self,
        original: dict[str, frozenset[str] | None],
        branches: list[dict[str, frozenset[str] | None]],
    ) -> None:
        merged = original.copy()
        for name in set().union(*(scope.keys() for scope in branches)):
            known = frozenset(
                target for scope in branches for target in (scope.get(name) or frozenset())
            )
            merged[name] = known or None
        self._scopes[-1] = merged

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        default_targets = self._default_argument_targets(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        self._bind_unknown(node.name)
        self._scopes.append({})
        self._scope_kinds.append("function")
        self._scope_nodes.append(node)
        self._global_names.append(set())
        self._nonlocal_names.append(set())
        self._bind_arguments(node.args, default_targets)
        for statement in node.body:
            self.visit(statement)
        self._nonlocal_names.pop()
        self._global_names.pop()
        self._scope_nodes.pop()
        self._scope_kinds.pop()
        self._scopes.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.targets.extend((node.lineno, name) for name in self._resolve_name(node.func.id))
        elif isinstance(node.func, ast.Attribute):
            self.targets.append((node.lineno, node.func.attr))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._bind_unknown(alias.asname or alias.name.partition(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self._bind_aliases(alias.asname or alias.name, frozenset((alias.name,)))

    def visit_Global(self, node: ast.Global) -> None:
        self._global_names[-1].update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self._nonlocal_names[-1].update(node.names)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        aliases = self._reference_targets(node.value)
        for target in node.targets:
            self._bind_assignment(target, aliases)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
            self._bind_assignment(node.target, self._reference_targets(node.value))
        else:
            self._bind_target(node.target)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind_assignment(node.target, self._reference_targets(node.value))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    ) -> None:
        first_generator = node.generators[0]
        self.visit(first_generator.iter)
        first_targets = self._reference_targets(first_generator.iter)
        self._scopes.append({})
        self._scope_kinds.append("comprehension")
        self._scope_nodes.append(node)
        self._global_names.append(set())
        self._nonlocal_names.append(set())
        self._bind_assignment(first_generator.target, first_targets)
        for condition in first_generator.ifs:
            self.visit(condition)
        for generator in node.generators[1:]:
            self.visit(generator.iter)
            self._bind_assignment(generator.target, self._reference_targets(generator.iter))
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self._nonlocal_names.pop()
        self._global_names.pop()
        self._scope_nodes.pop()
        self._scope_kinds.pop()
        self._scopes.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        default_targets = self._default_argument_targets(node.args)
        self._scopes.append({})
        self._scope_kinds.append("lambda")
        self._scope_nodes.append(node)
        self._global_names.append(set())
        self._nonlocal_names.append(set())
        self._bind_arguments(node.args, default_targets)
        self.visit(node.body)
        self._nonlocal_names.pop()
        self._global_names.pop()
        self._scope_nodes.pop()
        self._scope_kinds.pop()
        self._scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        base_targets = frozenset[str]()
        for base in node.bases:
            self.visit(base)
            base_targets |= self._reference_targets(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._bind_aliases(node.name, base_targets)
        self._scopes.append({})
        self._scope_kinds.append("class")
        self._scope_nodes.append(node)
        self._global_names.append(set())
        self._nonlocal_names.append(set())
        for statement in node.body:
            self.visit(statement)
        self._nonlocal_names.pop()
        self._global_names.pop()
        self._scope_nodes.pop()
        self._scope_kinds.pop()
        self._scopes.pop()

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        if _is_type_checking_guard(node.test):
            for statement in node.orelse:
                self.visit(statement)
            return
        original = self._scopes[-1].copy()
        branches = [self._visit_branch(branch, original) for branch in (node.body, node.orelse)]
        self._merge_branches(original, branches)

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        original = self._scopes[-1].copy()
        self._scopes[-1] = original.copy()
        handler_entries = [original]
        for statement in node.body:
            self.visit(statement)
            handler_entries.append(self._scopes[-1].copy())
        for statement in node.orelse:
            self.visit(statement)
        branches = [self._scopes[-1].copy()]
        if node.handlers:
            branches.append(original)
        for handler in node.handlers:
            for entry in handler_entries:
                self._scopes[-1] = entry.copy()
                if handler.type is not None:
                    self.visit(handler.type)
                if handler.name is not None:
                    self._bind_unknown(handler.name)
                for statement in handler.body:
                    self.visit(statement)
                branches.append(self._scopes[-1].copy())
        self._merge_branches(original, branches)
        for statement in node.finalbody:
            self.visit(statement)

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._visit_try(node)

    def _merge_loop_paths(
        self,
        original: dict[str, frozenset[str] | None],
        iteration: dict[str, frozenset[str] | None],
        orelse: list[ast.stmt],
    ) -> None:
        self._merge_branches(original, [original, iteration])
        completed = self._visit_branch(orelse, self._scopes[-1])
        self._merge_branches(original, [original, iteration, completed])

    def _visit_for(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        iterable_targets = self._reference_targets(node.iter)
        original = self._scopes[-1].copy()
        self._scopes[-1] = original.copy()
        self._bind_assignment(node.target, iterable_targets)
        for statement in node.body:
            self.visit(statement)
        self._merge_loop_paths(original, self._scopes[-1].copy(), node.orelse)

    def visit_For(self, node: ast.For) -> None:
        self._visit_for(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_for(node)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        original = self._scopes[-1].copy()
        iteration = self._visit_branch(node.body, original)
        self._merge_loop_paths(original, iteration, node.orelse)

    def _bind_match_pattern(self, pattern: ast.pattern) -> None:
        for node in ast.walk(pattern):
            if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
                self._bind_unknown(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest is not None:
                self._bind_unknown(node.rest)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        original = self._scopes[-1].copy()
        branches = [original]
        for case in node.cases:
            self._scopes[-1] = original.copy()
            self._bind_match_pattern(case.pattern)
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)
            branches.append(self._scopes[-1].copy())
        self._merge_branches(original, branches)


def _call_targets(tree: ast.AST) -> list[tuple[int, str]]:
    collector = _CallTargetVisitor()
    collector.visit(tree)
    runtime_module_targets = {
        name: frozenset(targets) for name, targets in collector._module_target_history.items()
    }
    runtime_scope_targets = {
        node: {name: frozenset(targets) for name, targets in scope.items()}
        for node, scope in collector._scope_target_history.items()
    }
    visitor = _CallTargetVisitor(runtime_module_targets, runtime_scope_targets)
    visitor.visit(tree)
    return visitor.targets


def _called_names(tree: ast.AST) -> set[str]:
    return {name for _, name in _call_targets(tree)}


def _runtime_component_constructions() -> set[str]:
    """Return runtime-component constructions outside the composition root."""
    return {
        f"{path.relative_to(KORVID)}:{line}: {name}"
        for path in KORVID.rglob("*.py")
        if path != KORVID / "__main__.py"
        for line, name in _call_targets(ast.parse(path.read_text(encoding="utf-8")))
        if name in RUNTIME_COMPONENTS
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
    offenders = _runtime_component_constructions()
    assert not offenders, "\n".join(sorted(offenders))
    assert _called_names(ast.parse(MAIN.read_text(encoding="utf-8"))) >= RUNTIME_COMPONENTS


def test_composition_root_contract_rejects_runtime_component_in_support_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-UI module must not construct composition-root components."""
    support_module = tmp_path / "composition_support.py"
    support_module.write_text("WriteCoordinator()\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "KORVID", tmp_path)

    with pytest.raises(
        AssertionError,
        match=r"composition_support\.py:1: WriteCoordinator",
    ):
        test_only_the_composition_root_constructs_the_app_runtime()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import korvid.ui.workspace_controller as controllers\n"
            "controllers.WriteCoordinator()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import "
            "WorkspaceController as Coordinator\nCoordinator()\n",
            "WorkspaceController",
        ),
        (
            "from korvid.ui.app_surfaces import AppUIBridge as Bridge\nBridge(None)\n",
            "AppUIBridge",
        ),
        (
            "from korvid.ui.app_surfaces import _RelationshipLister as Lister\nLister(None)\n",
            "_RelationshipLister",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "if TYPE_CHECKING:\n"
            "    from decoy import Other as WriteCoordinator\n"
            "WriteCoordinator()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator as Coordinator\n"
            "def shadow():\n"
            "    from decoy import Other as Coordinator\n"
            "Coordinator()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator as Coordinator\n"
            "Coordinator()\n"
            "from decoy import Other as Coordinator\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "Factory = WriteCoordinator\n"
            "Factory()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "Factory, _ = WriteCoordinator, object\n"
            "Factory()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "[factory() for factory in (WriteCoordinator,)]\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "{factory() for factory in (WriteCoordinator,)}\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            '{key: factory() for key, factory in (("key", WriteCoordinator),)}\n',
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "tuple(factory() for factory in (WriteCoordinator,))\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "for factory in (WriteCoordinator,):\n"
            "    factory()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "class Shadow:\n"
            "    WriteCoordinator = object()\n"
            "    def build(self):\n"
            "        WriteCoordinator()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "try:\n"
            "    pass\n"
            "except Exception:\n"
            "    WriteCoordinator = object()\n"
            "WriteCoordinator()\n",
            "WriteCoordinator",
        ),
        (
            "from decoy import Other\n"
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "Factory = Other\n"
            "try:\n"
            "    Factory = WriteCoordinator\n"
            "    raise RuntimeError\n"
            "    Factory = Other\n"
            "except RuntimeError:\n"
            "    Factory()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "try:\n"
            "    pass\n"
            "except* Exception:\n"
            "    WriteCoordinator = object()\n"
            "WriteCoordinator()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "from decoy import Other\n"
            "enabled = True\n"
            "Factory = WriteCoordinator if enabled else Other\n"
            "Factory()\n",
            "WriteCoordinator",
        ),
        (
            "from decoy import Other\n"
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "Factory = Other and WriteCoordinator\n"
            "Factory()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "from decoy import Other\n"
            "Factory = WriteCoordinator\n"
            "while False:\n"
            "    Factory = Other\n"
            "Factory()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "from decoy import Other\n"
            "Factory = WriteCoordinator\n"
            "for _ in ():\n"
            "    Factory = Other\n"
            "Factory()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "from decoy import Other\n"
            "Factory = WriteCoordinator\n"
            "match 0:\n"
            "    case 1:\n"
            "        Factory = Other\n"
            "Factory()\n",
            "WriteCoordinator",
        ),
        (
            "from decoy import Other as Factory\n"
            "def build():\n"
            "    Factory()\n"
            "from korvid.ui.workspace_controller import WriteCoordinator as Factory\n"
            "build()\n",
            "WriteCoordinator",
        ),
        (
            "from decoy import Other\n"
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "def outer():\n"
            "    Factory = Other\n"
            "    def build():\n"
            "        Factory()\n"
            "    Factory = WriteCoordinator\n"
            "    build()\n"
            "outer()\n",
            "WriteCoordinator",
        ),
        (
            "from decoy import Other as Factory\n"
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "def rebind():\n"
            "    global Factory\n"
            "    Factory = WriteCoordinator\n"
            "rebind()\n"
            "Factory()\n",
            "WriteCoordinator",
        ),
        (
            "from decoy import Other\n"
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "def build():\n"
            "    Factory = Other\n"
            "    def rebind():\n"
            "        nonlocal Factory\n"
            "        Factory = WriteCoordinator\n"
            "    rebind()\n"
            "    Factory()\n"
            "build()\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "def build(required, factory=WriteCoordinator):\n"
            "    factory()\n"
            "build(None)\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "def build(*, required, factory=WriteCoordinator):\n"
            "    factory()\n"
            "build(required=None)\n",
            "WriteCoordinator",
        ),
        (
            "from korvid.ui.workspace_controller import WriteCoordinator\n"
            "build = lambda required, factory=WriteCoordinator: factory()\n"
            "build(None)\n",
            "WriteCoordinator",
        ),
    ],
    ids=[
        "qualified",
        "imported-alias",
        "app-ui-bridge",
        "relationship-lister",
        "type-checking-shadow",
        "nested-shadow",
        "later-shadow",
        "assigned-alias",
        "destructured-alias",
        "list-comprehension-target",
        "set-comprehension-target",
        "dict-comprehension-target",
        "generator-comprehension-target",
        "for-target-alias",
        "class-scope-shadow",
        "except-handler-shadow",
        "try-prefix-handler",
        "except-star-handler-shadow",
        "conditional-alias",
        "boolean-alias",
        "zero-iteration-while",
        "zero-iteration-for",
        "no-match-case",
        "late-global-rebind",
        "late-closure-rebind",
        "global-rebind",
        "nonlocal-rebind",
        "positional-default",
        "keyword-default",
        "lambda-default",
    ],
)
def test_composition_root_contract_rejects_indirect_runtime_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
    expected: str,
) -> None:
    support_module = tmp_path / "composition_support.py"
    support_module.write_text(source, encoding="utf-8")
    monkeypatch.setitem(globals(), "KORVID", tmp_path)

    with pytest.raises(
        AssertionError,
        match=rf"composition_support\.py:\d+: {expected}",
    ):
        test_only_the_composition_root_constructs_the_app_runtime()


def test_tests_construct_apps_only_through_the_factory() -> None:
    direct_constructions = {
        f"{path.relative_to(ROOT)}:{line}: {name}"
        for path in TESTS.rglob("*.py")
        if path != TEST_APP_FACTORY
        for line, name in _call_targets(ast.parse(path.read_text(encoding="utf-8")))
        if name.endswith("KorvidApp")
    }
    assert not direct_constructions, "\n".join(sorted(direct_constructions))


@pytest.mark.parametrize(
    "source",
    [
        "import korvid.ui.app as app\napp.KorvidApp()\n",
        "from korvid.ui.app import KorvidApp as App\nApp()\n",
        "from korvid.ui.app import KorvidApp\nFactory = KorvidApp\nFactory()\n",
        "from korvid.ui.app import KorvidApp\nFactory, _ = KorvidApp, object\nFactory()\n",
        "from korvid.ui.app import KorvidApp\n[factory() for factory in (KorvidApp,)]\n",
        "from korvid.ui.app import KorvidApp\n{factory() for factory in (KorvidApp,)}\n",
        "from korvid.ui.app import KorvidApp\n"
        '{key: factory() for key, factory in (("key", KorvidApp),)}\n',
        "from korvid.ui.app import KorvidApp\ntuple(factory() for factory in (KorvidApp,))\n",
        "from korvid.ui.app import KorvidApp\nfor factory in (KorvidApp,):\n    factory()\n",
        "from korvid.ui.app import KorvidApp\n"
        "class Shadow:\n"
        "    KorvidApp = object()\n"
        "    def build(self):\n"
        "        KorvidApp()\n",
        "from korvid.ui.app import KorvidApp\n"
        "try:\n"
        "    pass\n"
        "except Exception:\n"
        "    KorvidApp = object()\n"
        "KorvidApp()\n",
        "from decoy import Other\n"
        "from korvid.ui.app import KorvidApp\n"
        "Factory = Other\n"
        "try:\n"
        "    Factory = KorvidApp\n"
        "    raise RuntimeError\n"
        "    Factory = Other\n"
        "except RuntimeError:\n"
        "    Factory()\n",
        "from korvid.ui.app import KorvidApp\n"
        "try:\n"
        "    pass\n"
        "except* Exception:\n"
        "    KorvidApp = object()\n"
        "KorvidApp()\n",
        "from korvid.ui.app import KorvidApp\n"
        "from decoy import Other\n"
        "enabled = True\n"
        "Factory = KorvidApp if enabled else Other\n"
        "Factory()\n",
        "from korvid.ui.app import KorvidApp\n"
        "from decoy import Other\n"
        "Factory = KorvidApp or Other\n"
        "Factory()\n",
        "from korvid.ui.app import KorvidApp\n"
        "from decoy import Other\n"
        "Factory = KorvidApp\n"
        "while False:\n"
        "    Factory = Other\n"
        "Factory()\n",
        "from korvid.ui.app import KorvidApp\n"
        "from decoy import Other\n"
        "Factory = KorvidApp\n"
        "for _ in ():\n"
        "    Factory = Other\n"
        "Factory()\n",
        "from korvid.ui.app import KorvidApp\n"
        "from decoy import Other\n"
        "Factory = KorvidApp\n"
        "match 0:\n"
        "    case 1:\n"
        "        Factory = Other\n"
        "Factory()\n",
        "from decoy import Other as Factory\n"
        "def build():\n"
        "    Factory()\n"
        "from korvid.ui.app import KorvidApp as Factory\n"
        "build()\n",
        "from korvid.ui.app import KorvidApp\n"
        "def outer():\n"
        "    def build():\n"
        "        Factory()\n"
        "    Factory = KorvidApp\n"
        "    build()\n"
        "outer()\n",
        "from decoy import Other as Factory\n"
        "from korvid.ui.app import KorvidApp\n"
        "def rebind():\n"
        "    global Factory\n"
        "    Factory = KorvidApp\n"
        "rebind()\n"
        "Factory()\n",
        "from decoy import Other\n"
        "from korvid.ui.app import KorvidApp\n"
        "def build():\n"
        "    Factory = Other\n"
        "    def rebind():\n"
        "        nonlocal Factory\n"
        "        Factory = KorvidApp\n"
        "    rebind()\n"
        "    Factory()\n"
        "build()\n",
        "from korvid.ui.app import KorvidApp\n"
        "def build(required, factory=KorvidApp):\n"
        "    factory()\n"
        "build(None)\n",
        "from korvid.ui.app import KorvidApp\n"
        "def build(*, required, factory=KorvidApp):\n"
        "    factory()\n"
        "build(required=None)\n",
        "from korvid.ui.app import KorvidApp\n"
        "build = lambda required, factory=KorvidApp: factory()\n"
        "build(None)\n",
        "from korvid.ui.app import KorvidApp\nclass _Host(KorvidApp):\n    pass\n_Host()\n",
        "from korvid.ui.app import KorvidApp as BaseApp\n"
        "class _Host(BaseApp):\n"
        "    pass\n"
        "App = _Host\n"
        "App()\n",
    ],
    ids=[
        "qualified",
        "imported-alias",
        "assigned-alias",
        "destructured-alias",
        "list-comprehension-target",
        "set-comprehension-target",
        "dict-comprehension-target",
        "generator-comprehension-target",
        "for-target-alias",
        "class-scope-shadow",
        "except-handler-shadow",
        "try-prefix-handler",
        "except-star-handler-shadow",
        "conditional-alias",
        "boolean-alias",
        "zero-iteration-while",
        "zero-iteration-for",
        "no-match-case",
        "late-global-rebind",
        "late-closure-rebind",
        "global-rebind",
        "nonlocal-rebind",
        "positional-default",
        "keyword-default",
        "lambda-default",
        "subclass",
        "subclass-alias",
    ],
)
def test_factory_contract_rejects_indirect_app_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    factory = tmp_path / "app_factory.py"
    factory.write_text("# approved factory\n", encoding="utf-8")
    (tmp_path / "test_direct.py").write_text(source, encoding="utf-8")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    monkeypatch.setitem(globals(), "TESTS", tmp_path)
    monkeypatch.setitem(globals(), "TEST_APP_FACTORY", factory)

    with pytest.raises(
        AssertionError,
        match=r"test_direct\.py:\d+: KorvidApp",
    ):
        test_tests_construct_apps_only_through_the_factory()


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
