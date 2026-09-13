#!/usr/bin/env python3
"""Fail closed when tracked korvid Python modules exceed reviewed size limits."""

from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAX_LINES = 1_200


@dataclasses.dataclass(frozen=True)
class ModuleLimit:
    """Reviewed line cap for one exceptional module."""

    path: str
    max_lines: int
    rationale: str


@dataclasses.dataclass(frozen=True)
class ConstructorLimit:
    """Reviewed line cap for one class method."""

    path: str
    class_name: str
    method_name: str
    max_lines: int


MODULE_LIMITS: tuple[ModuleLimit, ...] = (
    ModuleLimit(
        "src/korvid/ui/app.py",
        1_500,
        "Textual application shell after controller-runtime extraction",
    ),
    ModuleLimit(
        "src/korvid/__main__.py",
        1_773,
        "composition root baseline before repository stabilization",
    ),
    ModuleLimit(
        "src/korvid/core/config.py",
        1_684,
        "configuration parsing and migration baseline before repository stabilization",
    ),
    ModuleLimit(
        "src/korvid/k8s/client.py",
        1_944,
        "Kubernetes reliability hardening baseline after PR #381",
    ),
    ModuleLimit(
        "src/korvid/tools/executor.py",
        2_069,
        "bounded LIST execution baseline after PR #381",
    ),
    ModuleLimit(
        "src/korvid/tools/registry.py",
        1_420,
        "tool registry baseline before repository stabilization",
    ),
    ModuleLimit(
        "src/korvid/ui/agent_ui_controller.py",
        2_450,
        "agent UI controller baseline before repository stabilization",
    ),
    ModuleLimit(
        "src/korvid/ui/workspace_controller.py",
        1_640,
        "workspace controller baseline before repository stabilization",
    ),
    ModuleLimit(
        "src/korvid/ui/widgets/resource_table.py",
        1_446,
        "resource table baseline before repository stabilization",
    ),
)

CONSTRUCTOR_LIMITS: tuple[ConstructorLimit, ...] = (
    ConstructorLimit("src/korvid/ui/app.py", "KorvidApp", "__init__", 160),
)


def _tracked_python_modules(root: Path) -> tuple[list[str], list[str]]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", "src/korvid"],
            check=False,
            capture_output=True,
        )
    except OSError as error:
        return [], [f"cannot list tracked modules: {type(error).__name__}: {error}"]
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip() or "no stderr"
        return [], [f"git ls-files failed with exit {result.returncode}: {detail}"]
    try:
        names = result.stdout.decode("utf-8").split("\0")
    except UnicodeDecodeError as error:
        return [], [f"tracked path cannot decode as UTF-8: {error}"]
    modules = sorted(
        name for name in names if name and name.startswith("src/korvid/") and name.endswith(".py")
    )
    if not modules:
        return [], ["git ls-files returned no Python modules under src/korvid"]
    return modules, []


def _valid_module_path(path: object) -> bool:
    if not isinstance(path, str) or not path:
        return False
    candidate = PurePosixPath(path)
    return (
        not candidate.is_absolute()
        and candidate.parts[:2] == ("src", "korvid")
        and candidate.suffix == ".py"
        and all(part not in {"", ".", ".."} for part in candidate.parts)
        and candidate.as_posix() == path
    )


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _module_policy(
    entries: Sequence[ModuleLimit], tracked: set[str]
) -> tuple[dict[str, int], list[str]]:
    limits: dict[str, int] = {}
    errors: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, ModuleLimit):
            errors.append(f"module policy entry {index} has type {type(entry).__name__}")
            continue
        if not _valid_module_path(entry.path):
            errors.append(f"invalid module policy path: {entry.path!r}")
        elif entry.path not in tracked:
            errors.append(f"{entry.path}: policy path is not a tracked Python module")
        if entry.path in seen:
            errors.append(f"{entry.path}: duplicate module policy path")
        seen.add(entry.path)
        if not _positive_integer(entry.max_lines):
            errors.append(f"{entry.path}: max_lines must be a positive integer")
        if not isinstance(entry.rationale, str) or not entry.rationale.strip():
            errors.append(f"{entry.path}: rationale must not be empty")
        if (
            _valid_module_path(entry.path)
            and entry.path in tracked
            and entry.path not in limits
            and _positive_integer(entry.max_lines)
        ):
            limits[entry.path] = entry.max_lines
    return limits, errors


def _constructor_policy(
    entries: Sequence[ConstructorLimit], tracked: set[str]
) -> tuple[dict[str, list[ConstructorLimit]], list[str]]:
    limits: dict[str, list[ConstructorLimit]] = {}
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, ConstructorLimit):
            errors.append(f"constructor policy entry {index} has type {type(entry).__name__}")
            continue
        key = (entry.path, entry.class_name, entry.method_name)
        valid_path = _valid_module_path(entry.path)
        if not valid_path:
            errors.append(f"invalid constructor policy path: {entry.path!r}")
        elif entry.path not in tracked:
            errors.append(f"{entry.path}: constructor policy path is not tracked")
        if key in seen:
            errors.append(
                f"{entry.path}: duplicate constructor policy for "
                f"{entry.class_name}.{entry.method_name}"
            )
        seen.add(key)
        if not isinstance(entry.class_name, str) or not entry.class_name.isidentifier():
            errors.append(f"{entry.path}: invalid class name {entry.class_name!r}")
        if not isinstance(entry.method_name, str) or not entry.method_name.isidentifier():
            errors.append(f"{entry.path}: invalid method name {entry.method_name!r}")
        if not _positive_integer(entry.max_lines):
            errors.append(f"{entry.path}: constructor max_lines must be a positive integer")
        if (
            valid_path
            and entry.path in tracked
            and key
            not in {
                (item.path, item.class_name, item.method_name)
                for group in limits.values()
                for item in group
            }
            and isinstance(entry.class_name, str)
            and entry.class_name.isidentifier()
            and isinstance(entry.method_name, str)
            and entry.method_name.isidentifier()
            and _positive_integer(entry.max_lines)
        ):
            limits.setdefault(entry.path, []).append(entry)
    return limits, errors


def _read_source(root: Path, relative: str) -> tuple[str | None, str | None]:
    try:
        raw = (root / relative).read_bytes()
    except OSError as error:
        return None, f"{relative}: cannot read file: {type(error).__name__}: {error}"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as error:
        return None, f"{relative}: cannot decode UTF-8: {error}"


def _method_span(
    source: str, relative: str, limit: ConstructorLimit
) -> tuple[int | None, str | None]:
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError as error:
        return None, f"{relative}: cannot parse Python for constructor policy: {error.msg}"
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == limit.class_name
    ]
    if len(classes) != 1:
        return None, f"{relative}: expected exactly one class {limit.class_name}"
    methods = [
        node
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == limit.method_name
    ]
    if len(methods) != 1 or methods[0].end_lineno is None:
        return (
            None,
            f"{relative}: expected exactly one method {limit.class_name}.{limit.method_name}",
        )
    return methods[0].end_lineno - methods[0].lineno + 1, None


def _check_module_sizes(
    root: Path, tracked_modules: Sequence[str], module_policy: dict[str, int]
) -> tuple[dict[str, str], list[str]]:
    sources: dict[str, str] = {}
    errors: list[str] = []
    for relative in tracked_modules:
        source, read_error = _read_source(root, relative)
        if read_error is not None:
            errors.append(read_error)
            continue
        if source is None:
            errors.append(f"{relative}: source reader returned no content")
            continue
        sources[relative] = source
        line_count = len(source.splitlines())
        maximum = module_policy.get(relative, DEFAULT_MAX_LINES)
        if line_count > maximum:
            errors.append(f"{relative}: {line_count} physical lines exceeds limit {maximum}")
    return sources, errors


def check_repository(
    root: Path = REPOSITORY_ROOT,
    *,
    module_limits: Sequence[ModuleLimit] = MODULE_LIMITS,
    constructor_limits: Sequence[ConstructorLimit] = CONSTRUCTOR_LIMITS,
) -> list[str]:
    """Return every source-size policy violation under ``root``.

    Args:
        root: Git repository whose tracked korvid modules should be checked.
        module_limits: Explicit caps and rationales for reviewed exceptions.
        constructor_limits: Explicit caps for oversized class methods.

    Returns:
        Stable, human-readable violations. An empty list means the gate passes.
    """
    tracked_modules, errors = _tracked_python_modules(root)
    if errors:
        return errors
    tracked = set(tracked_modules)
    module_policy, module_errors = _module_policy(module_limits, tracked)
    constructor_policy, constructor_errors = _constructor_policy(constructor_limits, tracked)
    errors.extend(module_errors)
    errors.extend(constructor_errors)

    sources, source_errors = _check_module_sizes(root, tracked_modules, module_policy)
    errors.extend(source_errors)

    for relative, limits in constructor_policy.items():
        source = sources.get(relative)
        if source is None:
            continue
        for limit in limits:
            span, span_error = _method_span(source, relative, limit)
            if span_error is not None:
                errors.append(span_error)
            elif span is not None and span > limit.max_lines:
                errors.append(
                    f"{relative}: {limit.class_name}.{limit.method_name} spans "
                    f"{span} physical lines; limit is {limit.max_lines}"
                )
    return errors


def main() -> int:
    """Run the repository policy and return a process exit status."""
    errors = check_repository()
    for error in errors:
        print(f"source-size: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
