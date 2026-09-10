from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _module() -> Any:
    path = _ROOT / "scripts" / "release" / "update_homebrew_tap.py"
    assert path.is_file(), "missing scripts/release/update_homebrew_tap.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("update_homebrew_tap", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_dir(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={repo}", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _formula(version: str, *, suffix: str = "") -> str:
    return (
        "# typed: false\n"
        "# frozen_string_literal: true\n\n"
        "class Korvid < Formula\n"
        '  url "https://files.pythonhosted.org/packages/source/k/korvid/korvid-'
        f'{version}.tar.gz"\n'
        f'  sha256 "{"a" * 64}"\n'
        "  test do\n"
        f'    assert_match "{version}", shell_output("#{{bin}}/korvid --version")\n'
        "  end\n"
        "end\n"
        f"{suffix}"
    )


def _tap_remote(tmp_path: Path, *, main_formula: str) -> tuple[Path, Path]:
    remote = tmp_path / "homebrew-korvid.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    _git(tmp_path, "clone", str(remote), str(seed))
    _git(seed, "config", "user.name", "Seed Test")
    _git(seed, "config", "user.email", "seed@example.invalid")
    (seed / "Formula").mkdir()
    (seed / "Formula" / "korvid.rb").write_text(main_formula, encoding="utf-8")
    (seed / "README.md").write_text("# tap\n", encoding="utf-8")
    _git(seed, "add", "Formula/korvid.rb", "README.md")
    _git(seed, "commit", "-m", "seed main")
    _git(seed, "push", "origin", "main")
    return remote, seed


def _push_branch(
    seed: Path,
    *,
    branch: str,
    formula: str,
    extra_files: dict[str, str] | None = None,
) -> str:
    _git(seed, "switch", "main")
    _git(seed, "pull", "--ff-only", "origin", "main")
    _git(seed, "switch", "-C", branch)
    (seed / "Formula" / "korvid.rb").write_text(formula, encoding="utf-8")
    for relative_path, contents in (extra_files or {}).items():
        path = seed / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", branch)
    _git(seed, "push", "origin", f"HEAD:refs/heads/{branch}")
    return _git(seed, "rev-parse", "HEAD")


def _remote_branch_commit(remote: Path, branch: str) -> str:
    return _git_dir(remote, "rev-parse", f"refs/heads/{branch}")


def _remote_branch_paths(remote: Path, branch: str) -> list[str]:
    commit = _remote_branch_commit(remote, branch)
    lines = _git_dir(remote, "diff-tree", "--no-commit-id", "--name-only", "-r", commit)
    return [line for line in lines.splitlines() if line]


def _has_remote_branch(remote: Path, branch: str) -> bool:
    probe = subprocess.run(
        ["git", f"--git-dir={remote}", "show-ref", "--verify", f"refs/heads/{branch}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


class _FakeRunner:
    def __init__(
        self,
        module: Any,
        *,
        open_prs: list[dict[str, Any]] | None = None,
        created_pr_url: str = "https://github.com/hellices/homebrew-korvid/pull/17",
        bot_user_id: int = 123456,
    ) -> None:
        self._module = module
        self._open_prs = open_prs or []
        self._created_pr_url = created_pr_url
        self._bot_user_id = bot_user_id
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []

    def __call__(self, argv: list[str], *, cwd: Path | None = None) -> str:
        self.calls.append((tuple(argv), cwd))
        if argv[:3] == ["gh", "auth", "setup-git"]:
            return ""
        if argv[:3] == ["gh", "api", "users/homebrew-release[bot]"]:
            return f'{{"id": {self._bot_user_id}}}'
        if (
            len(argv) >= 3
            and argv[0] == "gh"
            and argv[1] == "api"
            and argv[2].startswith("repos/hellices/homebrew-korvid/pulls?")
        ):
            return self._module.json.dumps(self._open_prs)
        if argv[:4] == [
            "gh",
            "pr",
            "create",
            "--repo",
        ]:
            return self._created_pr_url
        return self._module._run_command(argv, cwd=cwd)


def test_main_rejects_versions_outside_the_stable_release_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    handoff = _module()
    formula = tmp_path / "korvid.rb"
    formula.write_text(_formula("1.2.3"), encoding="utf-8")

    assert (
        handoff.main(
            [
                "--version",
                "1.2",
                "--formula",
                str(formula),
                "--tap-clone-source",
                "file:///does-not-matter",
                "--bot-login",
                "homebrew-release[bot]",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "supported release version" in captured.err


def test_equal_version_on_tap_main_is_a_no_op_even_when_the_formula_differs(
    tmp_path: Path,
) -> None:
    handoff = _module()
    remote, _ = _tap_remote(
        tmp_path,
        main_formula=_formula(
            "0.4.1", suffix='\n  bottle do\n    root_url "https://example.invalid"\n  end\n'
        ),
    )
    formula = tmp_path / "generated.rb"
    formula.write_text(_formula("0.4.1", suffix="# regenerated from source\n"), encoding="utf-8")
    runner = _FakeRunner(handoff)

    result = handoff.update_homebrew_tap(
        version="0.4.1",
        formula=formula,
        tap_clone_source=remote.as_uri(),
        tap_repository="hellices/homebrew-korvid",
        clone_dir=tmp_path / "tap-clone",
        bot_login="homebrew-release[bot]",
        command_runner=runner,
    )

    assert result.status == "noop"
    assert result.pr_number is None
    assert not _has_remote_branch(remote, "bump-korvid-0.4.1")
    assert not any(
        call[0][:3] == ("gh", "api", "users/homebrew-release[bot]") for call in runner.calls
    )
    assert not any(call[0][:3] == ("gh", "pr", "create") for call in runner.calls)


def test_a_newer_tap_main_version_rejects_the_downgrade_before_branch_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    handoff = _module()
    remote, _ = _tap_remote(tmp_path, main_formula=_formula("0.4.2"))
    formula = tmp_path / "generated.rb"
    formula.write_text(_formula("0.4.1"), encoding="utf-8")
    runner = _FakeRunner(handoff)

    assert (
        handoff.main(
            [
                "--version",
                "0.4.1",
                "--formula",
                str(formula),
                "--tap-clone-source",
                remote.as_uri(),
                "--tap-repository",
                "hellices/homebrew-korvid",
                "--clone-dir",
                str(tmp_path / "tap-clone"),
                "--bot-login",
                "homebrew-release[bot]",
            ],
            command_runner=runner,
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "downgrade" in captured.err
    assert not _has_remote_branch(remote, "bump-korvid-0.4.1")


def test_an_existing_same_version_branch_without_a_pull_request_is_a_safe_retry(
    tmp_path: Path,
) -> None:
    handoff = _module()
    remote, seed = _tap_remote(tmp_path, main_formula=_formula("0.4.0"))
    branch = "bump-korvid-0.4.1"
    branch_head = _push_branch(
        seed, branch=branch, formula=_formula("0.4.1", suffix="# prior run\n")
    )
    formula = tmp_path / "generated.rb"
    formula.write_text(_formula("0.4.1", suffix="# regenerated later\n"), encoding="utf-8")
    runner = _FakeRunner(
        handoff, open_prs=[], created_pr_url="https://github.com/hellices/homebrew-korvid/pull/41"
    )

    result = handoff.update_homebrew_tap(
        version="0.4.1",
        formula=formula,
        tap_clone_source=remote.as_uri(),
        tap_repository="hellices/homebrew-korvid",
        clone_dir=tmp_path / "tap-clone",
        bot_login="homebrew-release[bot]",
        command_runner=runner,
    )

    assert result.status == "opened"
    assert result.pr_number == 41
    assert _remote_branch_commit(remote, branch) == branch_head
    assert not any(
        call[0][:3] == ("gh", "api", "users/homebrew-release[bot]") for call in runner.calls
    )
    assert any(call[0][:3] == ("gh", "pr", "create") for call in runner.calls)


def test_a_safe_retry_still_works_after_main_advances_past_the_branch_fork_point(
    tmp_path: Path,
) -> None:
    handoff = _module()
    remote, seed = _tap_remote(tmp_path, main_formula=_formula("0.4.0"))
    branch = "bump-korvid-0.4.1"
    branch_head = _push_branch(
        seed, branch=branch, formula=_formula("0.4.1", suffix="# prior run\n")
    )
    _git(seed, "switch", "main")
    (seed / "README.md").write_text("# tap\nmain advanced\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "advance main")
    _git(seed, "push", "origin", "main")
    formula = tmp_path / "generated.rb"
    formula.write_text(_formula("0.4.1", suffix="# regenerated later\n"), encoding="utf-8")
    runner = _FakeRunner(
        handoff, open_prs=[], created_pr_url="https://github.com/hellices/homebrew-korvid/pull/43"
    )

    result = handoff.update_homebrew_tap(
        version="0.4.1",
        formula=formula,
        tap_clone_source=remote.as_uri(),
        tap_repository="hellices/homebrew-korvid",
        clone_dir=tmp_path / "tap-clone",
        bot_login="homebrew-release[bot]",
        command_runner=runner,
    )

    assert result.status == "opened"
    assert result.pr_number == 43
    assert _remote_branch_commit(remote, branch) == branch_head
    assert any(call[0][:3] == ("gh", "pr", "create") for call in runner.calls)


def test_an_existing_same_version_branch_with_a_matching_bot_owned_pull_request_is_reused(
    tmp_path: Path,
) -> None:
    handoff = _module()
    remote, seed = _tap_remote(tmp_path, main_formula=_formula("0.4.0"))
    branch = "bump-korvid-0.4.1"
    branch_head = _push_branch(seed, branch=branch, formula=_formula("0.4.1"))
    formula = tmp_path / "generated.rb"
    formula.write_text(_formula("0.4.1", suffix="# regenerated later\n"), encoding="utf-8")
    runner = _FakeRunner(
        handoff,
        open_prs=[
            {
                "number": 58,
                "title": "korvid 0.4.1",
                "user": {"login": "homebrew-release[bot]"},
                "head": {"ref": branch, "repo": {"owner": {"login": "hellices"}}},
                "base": {"ref": "main"},
            }
        ],
    )

    result = handoff.update_homebrew_tap(
        version="0.4.1",
        formula=formula,
        tap_clone_source=remote.as_uri(),
        tap_repository="hellices/homebrew-korvid",
        clone_dir=tmp_path / "tap-clone",
        bot_login="homebrew-release[bot]",
        command_runner=runner,
    )

    assert result.status == "reused"
    assert result.pr_number == 58
    assert _remote_branch_commit(remote, branch) == branch_head
    assert not any(call[0][:3] == ("gh", "pr", "create") for call in runner.calls)
    assert not any(
        call[0][:3] == ("gh", "api", "users/homebrew-release[bot]") for call in runner.calls
    )


def test_an_existing_pull_request_owned_by_another_login_is_rejected(
    tmp_path: Path,
) -> None:
    handoff = _module()
    remote, seed = _tap_remote(tmp_path, main_formula=_formula("0.4.0"))
    branch = "bump-korvid-0.4.1"
    _push_branch(seed, branch=branch, formula=_formula("0.4.1"))
    formula = tmp_path / "generated.rb"
    formula.write_text(_formula("0.4.1", suffix="# regenerated later\n"), encoding="utf-8")
    runner = _FakeRunner(
        handoff,
        open_prs=[
            {
                "number": 58,
                "title": "korvid 0.4.1",
                "user": {"login": "some-human"},
                "head": {"ref": branch, "repo": {"owner": {"login": "hellices"}}},
                "base": {"ref": "main"},
            }
        ],
    )

    with pytest.raises(
        handoff.HandoffError, match=r"opened by some-human, expected homebrew-release\[bot\]"
    ):
        handoff.update_homebrew_tap(
            version="0.4.1",
            formula=formula,
            tap_clone_source=remote.as_uri(),
            tap_repository="hellices/homebrew-korvid",
            clone_dir=tmp_path / "tap-clone",
            bot_login="homebrew-release[bot]",
            command_runner=runner,
        )


def test_an_existing_branch_with_unrelated_changes_is_rejected_instead_of_overwritten(
    tmp_path: Path,
) -> None:
    handoff = _module()
    remote, seed = _tap_remote(tmp_path, main_formula=_formula("0.4.0"))
    branch = "bump-korvid-0.4.1"
    _push_branch(
        seed,
        branch=branch,
        formula=_formula("0.4.1"),
        extra_files={"docs/notes.txt": "not a formula-only change\n"},
    )
    formula = tmp_path / "generated.rb"
    formula.write_text(_formula("0.4.1", suffix="# generated\n"), encoding="utf-8")
    runner = _FakeRunner(handoff)

    with pytest.raises(handoff.HandoffError, match=r"only Formula/korvid\.rb"):
        handoff.update_homebrew_tap(
            version="0.4.1",
            formula=formula,
            tap_clone_source=remote.as_uri(),
            tap_repository="hellices/homebrew-korvid",
            clone_dir=tmp_path / "tap-clone",
            bot_login="homebrew-release[bot]",
            command_runner=runner,
        )


def test_a_new_branch_commit_uses_the_app_bot_identity_and_touches_only_the_formula(
    tmp_path: Path,
) -> None:
    handoff = _module()
    remote, _ = _tap_remote(tmp_path, main_formula=_formula("0.4.0"))
    formula = tmp_path / "generated.rb"
    formula.write_text(_formula("0.4.1", suffix="# generated\n"), encoding="utf-8")
    runner = _FakeRunner(
        handoff, created_pr_url="https://github.com/hellices/homebrew-korvid/pull/72"
    )

    result = handoff.update_homebrew_tap(
        version="0.4.1",
        formula=formula,
        tap_clone_source=remote.as_uri(),
        tap_repository="hellices/homebrew-korvid",
        clone_dir=tmp_path / "tap-clone",
        bot_login="homebrew-release[bot]",
        command_runner=runner,
    )

    branch = "bump-korvid-0.4.1"
    commit = _remote_branch_commit(remote, branch)

    assert result.status == "opened"
    assert result.pr_number == 72
    assert _remote_branch_paths(remote, branch) == ["Formula/korvid.rb"]
    assert _git_dir(remote, "show", "-s", "--format=%an", commit) == "homebrew-release[bot]"
    assert (
        _git_dir(remote, "show", "-s", "--format=%ae", commit)
        == "123456+homebrew-release[bot]@users.noreply.github.com"
    )
    create_call = next(call[0] for call in runner.calls if call[0][:3] == ("gh", "pr", "create"))
    assert "--base" in create_call
    assert "main" in create_call
    assert "--head" in create_call
    assert branch in create_call
