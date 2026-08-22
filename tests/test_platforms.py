"""Tests for shared platform helpers and CI workflow invariants."""

import ast
import errno
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

import tests.platforms as platform_helpers
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


def _workflow_files() -> tuple[Path, ...]:
    workflows = Path(__file__).parents[1] / ".github" / "workflows"
    return tuple(sorted((*workflows.glob("*.yml"), *workflows.glob("*.yaml"))))


def _workflow_job(workflow: str, name: str) -> dict[str, Any]:
    parsed = yaml.safe_load(workflow)
    assert isinstance(parsed, dict), "expected workflow YAML to parse to a mapping"
    jobs = parsed.get("jobs")
    assert isinstance(jobs, dict), "expected workflow to define a jobs mapping"
    job = jobs.get(name)
    assert isinstance(job, dict), f"expected workflow to define jobs[{name!r}]"
    return job


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


def test_assert_pinned_action_refs_accepts_revision_changes() -> None:
    workflow = """
    - uses: astral-sh/setup-uv@1111111111111111111111111111111111111111
    - uses: astral-sh/setup-uv@2222222222222222222222222222222222222222
    """

    assert platform_helpers.assert_pinned_action_refs(workflow, "astral-sh/setup-uv") == (
        "1111111111111111111111111111111111111111",
        "2222222222222222222222222222222222222222",
    )


def test_assert_pinned_action_refs_rejects_any_unpinned_use_site() -> None:
    workflow = """
    - uses: astral-sh/setup-uv@1111111111111111111111111111111111111111
    - name: Named setup step
      uses: Astral-SH/setup-UV@v10
    """

    with pytest.raises(
        AssertionError, match="expected astral-sh/setup-uv@<40 lowercase hex characters>"
    ):
        platform_helpers.assert_pinned_action_refs(workflow, "astral-sh/setup-uv")


def test_assert_pinned_action_version_rejects_unrelated_matching_text() -> None:
    workflow = """
    jobs:
      build:
        steps:
          - uses: astral-sh/setup-uv@1111111111111111111111111111111111111111
            with:
              version: "0.10.8"
          - run: 'echo version: "0.10.9"'
    """

    with pytest.raises(
        AssertionError, match=r"expected every astral-sh/setup-uv step to use version 0.10.9"
    ):
        platform_helpers.assert_pinned_action_version(workflow, "astral-sh/setup-uv", "0.10.9")


def test_all_setup_uv_workflow_steps_are_pinned_to_one_revision() -> None:
    action = "astral-sh/setup-uv"
    refs: list[str] = []
    matched_workflows: list[Path] = []
    for path in _workflow_files():
        workflow = read_text_utf8(path)
        if action.casefold() not in workflow.casefold():
            continue
        matched_workflows.append(path)
        refs.extend(platform_helpers.assert_pinned_action_refs(workflow, action))

    assert matched_workflows
    assert len(set(refs)) == 1


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
    windows_job = _workflow_job(_ci_workflow(), "windows-test")
    segment = yaml.safe_dump(windows_job, sort_keys=False)
    steps = windows_job["steps"]
    assert isinstance(steps, list)
    runs = [step["run"] for step in steps if isinstance(step, dict) and "run" in step]
    setup_uv = next(
        step
        for step in steps
        if isinstance(step, dict)
        and str(step.get("uses", "")).partition("@")[0].casefold() == "astral-sh/setup-uv"
    )
    assert windows_job["runs-on"] == "windows-latest"
    assert_pinned_action_ref(segment, "actions/checkout")
    assert_pinned_action_ref(segment, "astral-sh/setup-uv")
    assert isinstance(setup_uv.get("with"), dict)
    assert setup_uv["with"]["python-version"] == "3.12"
    assert "uv sync --locked --dev --all-extras" in runs
    assert "uv run pytest -q" in runs


def test_workflow_job_lookup_is_order_independent() -> None:
    workflow = """
name: CI
jobs:
  pre-commit:
    runs-on: ubuntu-latest
  windows-test:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
      - uses: astral-sh/setup-uv@1111111111111111111111111111111111111111
      - run: uv sync --locked --dev --all-extras
      - run: uv run pytest -q
  test:
    runs-on: ubuntu-latest
"""

    job = _workflow_job(workflow, "windows-test")

    assert job["runs-on"] == "windows-latest"
    assert "uv run pytest -q" in yaml.safe_dump(job, sort_keys=False)


def test_symlink_or_skip_calls_path_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "target.txt"
    link = tmp_path / "link.txt"
    called: dict[str, Path] = {}

    def fake(self: Path, dest: Path, target_is_directory: bool = False) -> None:
        assert target_is_directory is False
        called["link"] = self
        called["target"] = dest

    monkeypatch.setattr(Path, "symlink_to", fake)

    platform_helpers.symlink_or_skip(link, target)

    assert called == {"link": link, "target": target}


@pytest.mark.parametrize("winerror", [1314, None])
def test_symlink_or_skip_skips_windows_privilege_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, winerror: int | None
) -> None:
    link = tmp_path / "link.txt"
    target = tmp_path / "target.txt"

    class FakePrivilegeError(PermissionError):
        def __init__(self, value: int | None) -> None:
            super().__init__(errno.EPERM, "privilege unavailable")
            self.winerror = value

    def fail(self: Path, dest: Path, target_is_directory: bool = False) -> None:
        raise FakePrivilegeError(winerror)

    monkeypatch.setattr(platform_helpers, "WINDOWS", True)
    monkeypatch.setattr(Path, "symlink_to", fail)

    with pytest.raises(pytest.skip.Exception, match=r"Developer Mode|administrator"):
        platform_helpers.symlink_or_skip(link, target)


def test_symlink_or_skip_propagates_unrelated_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    link = tmp_path / "link.txt"
    target = tmp_path / "target.txt"

    def fail(self: Path, dest: Path, target_is_directory: bool = False) -> None:
        raise FileNotFoundError(errno.ENOENT, "missing target")

    monkeypatch.setattr(platform_helpers, "WINDOWS", True)
    monkeypatch.setattr(Path, "symlink_to", fail)

    with pytest.raises(FileNotFoundError, match="missing target"):
        platform_helpers.symlink_or_skip(link, target)


def test_readme_links_windows_contributor_notes() -> None:
    readme = _readme()

    assert (
        "[Windows contributor notes](https://github.com/hellices/korvid/blob/main/docs/windows.md)"
    ) in readme


def test_windows_doc_records_verified_support_contract() -> None:
    doc = _windows_doc()

    for snippet in (
        "uv sync --dev --all-extras",
        "uv run pytest -q",
        "windows-test",
        "30936032385",
        "3376 passed / 37 skipped / 0 failures",
        "21 opt-in contract-suite skips",
        "16 capability skips",
        "3 newly classified capability skips",
        "13 pre-existing platform skips",
        "`~user`",
        "Developer Mode",
        "symlink",
        "capability skip",
        "hosted runner had privilege",
        "count remained 37",
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
