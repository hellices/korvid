"""Tests for shared platform helpers and CI workflow invariants."""

import ast
import os
from pathlib import Path

import pytest

from tests.platforms import (
    POSIX,
    WINDOWS,
    assert_pinned_action_ref,
    posix_only,
    read_text_utf8,
)


def test_windows_and_posix_flags_follow_os_name() -> None:
    assert WINDOWS is (os.name == "nt")
    assert POSIX is (os.name == "posix")


def test_posix_only_returns_a_skipif_mark_with_the_given_reason() -> None:
    mark = posix_only("POSIX permissions required")

    assert isinstance(mark, pytest.MarkDecorator)
    assert mark.mark.name == "skipif"
    assert mark.mark.args == (not POSIX,)
    assert mark.mark.kwargs == {"reason": "POSIX permissions required"}


def _find_test_function(module: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"expected to find test function {name}")


def _decorates_with_posix_only(node: ast.FunctionDef | ast.AsyncFunctionDef, reason: str) -> bool:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if not isinstance(decorator.func, ast.Name) or decorator.func.id != "posix_only":
            continue
        if len(decorator.args) != 1 or decorator.keywords:
            continue
        if isinstance(decorator.args[0], ast.Constant) and decorator.args[0].value == reason:
            return True
    return False


@pytest.mark.parametrize(
    ("relative_path", "test_name"),
    [
        ("tests/core/test_transfer.py", "test_unknown_user_tilde_is_a_validation_error"),
        ("tests/ui/test_transfer_picker.py", "test_unexpandable_tilde_falls_back_to_home"),
    ],
)
def test_posix_user_tilde_cases_use_the_shared_marker(relative_path: str, test_name: str) -> None:
    module = ast.parse(read_text_utf8(Path(__file__).parents[1] / relative_path))

    assert _decorates_with_posix_only(
        _find_test_function(module, test_name),
        "requires POSIX ~user account expansion behavior",
    )


def _ci_workflow() -> str:
    return read_text_utf8(Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml")


def _readme() -> str:
    return read_text_utf8(Path(__file__).parents[1] / "README.md")


def _windows_doc() -> str:
    return read_text_utf8(Path(__file__).parents[1] / "docs" / "windows.md")


def test_assert_pinned_action_ref_accepts_full_lowercase_commit_shas() -> None:
    workflow = "- uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1"

    assert (
        assert_pinned_action_ref(workflow, "actions/checkout")
        == "34e114876b0b11c390a56381ad16ebd13914f8d5"
    )


@pytest.mark.parametrize("bad_ref", ["v4", "v4.3.1", "34E114876B0B11C390A56381AD16EBD13914F8D5"])
def test_assert_pinned_action_ref_rejects_tags_and_non_lowercase_refs(bad_ref: str) -> None:
    workflow = f"- uses: astral-sh/setup-uv@{bad_ref}"

    with pytest.raises(
        AssertionError, match="expected astral-sh/setup-uv@<40 lowercase hex characters>"
    ):
        assert_pinned_action_ref(workflow, "astral-sh/setup-uv")


def test_read_text_utf8_uses_utf8_encoding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    captured: dict[str, object] = {}

    def fake_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        captured["path"] = self
        captured["encoding"] = encoding
        captured["errors"] = errors
        return "name: CI"

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert read_text_utf8(path) == "name: CI"
    assert captured == {"path": path, "encoding": "utf-8", "errors": None}


def test_ci_workflow_defines_the_required_windows_test_job() -> None:
    workflow = _ci_workflow()
    test_job = workflow.index("\n  test:")
    windows_job = workflow.index("\n  windows-test:")
    pre_commit = workflow.index("\n  pre-commit:")
    assert test_job < windows_job < pre_commit
    segment = workflow[windows_job:pre_commit]
    assert "runs-on: windows-latest" in segment
    assert_pinned_action_ref(segment, "actions/checkout")
    assert_pinned_action_ref(segment, "astral-sh/setup-uv")
    assert 'python-version: "3.12"' in segment
    assert "uv sync --locked --dev --all-extras" in segment
    assert "uv run pytest -q" in segment


def test_readme_links_windows_contributor_notes() -> None:
    readme = _readme()

    assert "[Windows contributor notes](docs/windows.md)" in readme


def test_windows_doc_records_verified_support_contract() -> None:
    doc = _windows_doc()

    for snippet in (
        "uv sync --dev --all-extras",
        "uv run pytest -q",
        "windows-test",
        "30930727214",
        "3373 passed / 37 skipped / 0 failures",
        "21 opt-in contract-suite skips",
        "16 capability skips",
        "`~user`",
        "Developer Mode",
        "symlink",
        "SuspendNotSupported",
        "legacy_windows=False",
        'newline=""',
        "`->`",
        "`--`",
        "0o600",
        "ACL",
        "do not claim ACL confidentiality",
        "op_factory",
        "cancelled writes",
    ):
        assert snippet in doc
