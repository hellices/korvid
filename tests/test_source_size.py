"""Tests for the fail-closed Python source-size ratchet."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.check_source_size import (
    ConstructorLimit,
    ModuleLimit,
    check_repository,
)

ROOT = Path(__file__).parent.parent
MODULE = "src/korvid/example.py"


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "korvid").mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    return root


def _track(root: Path, relative: str, content: str | bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "--", relative], cwd=root, check=True)


def _python_lines(count: int) -> str:
    return "value = 1\n" * count


def _constructor(span: int) -> str:
    return "class KorvidApp:\n    def __init__(self):\n" + "        pass\n" * (span - 1)


def test_default_limit_accepts_boundary_and_rejects_one_line_over(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _track(root, MODULE, _python_lines(1_200))
    assert check_repository(root, module_limits=(), constructor_limits=()) == []

    _track(root, MODULE, _python_lines(1_201))
    errors = check_repository(root, module_limits=(), constructor_limits=())
    assert errors == [f"{MODULE}: 1201 physical lines exceeds limit 1200"]


def test_grandfathered_limit_is_a_fixed_cap(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    policy = (ModuleLimit(MODULE, 1_201, "legacy orchestration baseline"),)
    _track(root, MODULE, _python_lines(1_201))
    assert check_repository(root, module_limits=policy, constructor_limits=()) == []

    _track(root, MODULE, _python_lines(1_202))
    errors = check_repository(root, module_limits=policy, constructor_limits=())
    assert errors == [f"{MODULE}: 1202 physical lines exceeds limit 1201"]


def test_policy_rejects_missing_duplicate_and_unjustified_entries(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _track(root, MODULE, "value = 1\n")
    policy = (
        ModuleLimit(MODULE, 1_201, "first baseline"),
        ModuleLimit(MODULE, 1_202, "duplicate baseline"),
        ModuleLimit("src/korvid/missing.py", 1_300, "missing baseline"),
        ModuleLimit("src/korvid/empty_reason.py", 1_300, "   "),
    )

    errors = check_repository(root, module_limits=policy, constructor_limits=())

    assert any("duplicate module policy path" in error for error in errors)
    assert any("policy path is not a tracked Python module" in error for error in errors)
    assert any("rationale must not be empty" in error for error in errors)


def test_policy_rejects_malformed_paths_and_caps(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _track(root, MODULE, "value = 1\n")
    policy = (
        ModuleLimit("/absolute.py", 1_300, "absolute"),
        ModuleLimit("src/korvid/../escape.py", 1_300, "traversal"),
        ModuleLimit("src/korvid/not_python.txt", 1_300, "wrong suffix"),
        ModuleLimit(MODULE, 0, "non-positive"),
    )

    errors = check_repository(root, module_limits=policy, constructor_limits=())

    assert sum("invalid module policy path" in error for error in errors) == 3
    assert any("max_lines must be a positive integer" in error for error in errors)


def test_unreadable_utf8_fails_closed(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _track(root, MODULE, b"value = '\xff'\n")

    errors = check_repository(root, module_limits=(), constructor_limits=())

    assert len(errors) == 1
    assert errors[0].startswith(f"{MODULE}: cannot decode UTF-8:")


def test_constructor_limit_accepts_boundary_and_rejects_one_line_over(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    relative = "src/korvid/ui/app.py"
    module_policy = (ModuleLimit(relative, 1_500, "Textual application shell"),)
    constructor_policy = (ConstructorLimit(relative, "KorvidApp", "__init__", 160),)
    _track(root, relative, _constructor(160))
    assert (
        check_repository(
            root,
            module_limits=module_policy,
            constructor_limits=constructor_policy,
        )
        == []
    )

    _track(root, relative, _constructor(161))
    errors = check_repository(
        root,
        module_limits=module_policy,
        constructor_limits=constructor_policy,
    )
    assert errors == [
        "src/korvid/ui/app.py: KorvidApp.__init__ spans 161 physical lines; limit is 160"
    ]


def test_real_repository_satisfies_the_committed_policy() -> None:
    assert check_repository(ROOT) == []
