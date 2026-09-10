from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]


class _LoadedUpdateResult:
    def __init__(self, result: object) -> None:
        status = getattr(result, "status", None)
        branch = getattr(result, "branch", None)
        pr_number = getattr(result, "pr_number", None)
        assert isinstance(status, str)
        assert isinstance(branch, str)
        assert pr_number is None or isinstance(pr_number, int)
        self.status = status
        self.branch = branch
        self.pr_number = pr_number


class _LoadedHandoffModule:
    def __init__(self, module: ModuleType) -> None:
        self._module = module
        error_type = getattr(module, "HandoffError", None)
        assert isinstance(error_type, type)
        assert issubclass(error_type, Exception)
        self.HandoffError = error_type

    def _run_command(self, argv: list[str], *, cwd: Path | None = None) -> str:
        run_command = getattr(self._module, "_run_command", None)
        assert callable(run_command)
        result = run_command(argv, cwd=cwd)
        assert isinstance(result, str)
        return result

    def main(self, argv: list[str] | None = None, *, command_runner: object = None) -> int:
        main = getattr(self._module, "main", None)
        assert callable(main)
        result = main(argv, command_runner=command_runner)
        assert isinstance(result, int)
        return result

    def update_homebrew_tap(self, **kwargs: object) -> _LoadedUpdateResult:
        update_homebrew_tap = getattr(self._module, "update_homebrew_tap", None)
        assert callable(update_homebrew_tap)
        result = update_homebrew_tap(**kwargs)
        return _LoadedUpdateResult(result)


def _module() -> _LoadedHandoffModule:
    path = _ROOT / "scripts" / "release" / "update_homebrew_tap.py"
    assert path.is_file(), "missing scripts/release/update_homebrew_tap.py"
    original_path = list(sys.path)
    handoff_name = "update_homebrew_tap"
    prior_handoff = sys.modules.get(handoff_name)
    prior_version_format = sys.modules.get("version_format")
    try:
        sys.path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location(handoff_name, path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return _LoadedHandoffModule(module)
    finally:
        sys.path[:] = original_path
        if prior_handoff is None:
            sys.modules.pop(handoff_name, None)
        else:
            sys.modules[handoff_name] = prior_handoff
        if prior_version_format is None:
            sys.modules.pop("version_format", None)
        else:
            sys.modules["version_format"] = prior_version_format


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
        module: _LoadedHandoffModule,
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
            return json.dumps(self._open_prs)
        if argv[:4] == [
            "gh",
            "pr",
            "create",
            "--repo",
        ]:
            return self._created_pr_url
        return self._module._run_command(argv, cwd=cwd)


def test_module_loader_restores_sys_path_and_imported_modules() -> None:
    original_path = list(sys.path)
    prior_handoff = sys.modules.pop("update_homebrew_tap", None)
    prior_version_format = sys.modules.pop("version_format", None)

    try:
        handoff = _module()

        assert handoff.HandoffError.__name__ == "HandoffError"
        assert sys.path == original_path
        assert "update_homebrew_tap" not in sys.modules
        assert "version_format" not in sys.modules
    finally:
        sys.path[:] = original_path
        sys.modules.pop("update_homebrew_tap", None)
        sys.modules.pop("version_format", None)
        if prior_handoff is not None:
            sys.modules["update_homebrew_tap"] = prior_handoff
        if prior_version_format is not None:
            sys.modules["version_format"] = prior_version_format


@pytest.mark.parametrize(
    "userinfo",
    ["release-bot:synthetic-secret", "synthetic-secret:x-oauth-basic", "synthetic-secret"],
)
def test_main_rejects_credentialed_tap_clone_sources_without_running_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], userinfo: str
) -> None:
    handoff = _module()
    formula = tmp_path / "korvid.rb"
    formula.write_text(_formula("1.2.3"), encoding="utf-8")
    runner = _FakeRunner(handoff)
    credentialed = f"https://{userinfo}@example.invalid/homebrew-korvid.git"

    assert (
        handoff.main(
            [
                "--version",
                "1.2.3",
                "--formula",
                str(formula),
                "--tap-clone-source",
                credentialed,
                "--bot-login",
                "homebrew-release[bot]",
            ],
            command_runner=runner,
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "tap clone source must not embed credentials" in captured.err
    assert "synthetic-secret" not in captured.err
    assert runner.calls == []


@pytest.mark.parametrize(
    "userinfo",
    ["release-bot:synthetic-secret", "synthetic-secret:x-oauth-basic", "synthetic-secret"],
)
@pytest.mark.parametrize("host", ["example.invalid", "[::1]:invalid-port"])
def test_run_command_redacts_credentials_from_failing_command_diagnostics(
    monkeypatch: pytest.MonkeyPatch, userinfo: str, host: str
) -> None:
    handoff = _module()
    credentialed = f"https://{userinfo}@{host}/homebrew-korvid.git"
    seen_kwargs: dict[str, object] = {}

    def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args
        seen_kwargs.update(kwargs)
        return subprocess.CompletedProcess(
            args=["git", "clone", credentialed],
            returncode=1,
            stdout=f"attempted clone {credentialed}\n",
            stderr=f"fatal: could not read from {credentialed}\n",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(handoff.HandoffError, match=r"git clone .* failed") as exc_info:
        handoff._run_command(["git", "clone", credentialed])

    message = str(exc_info.value)
    assert "synthetic-secret" not in message
    assert f"***@{host}" in message
    assert "attempted clone" in message
    assert seen_kwargs == {"cwd": None, "capture_output": True, "text": True, "check": False}


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        ("users/", ""),
        ("users/", "{"),
        ("users/", "null"),
        ("users/", "[]"),
        ("repos/", ""),
        ("repos/", "{"),
        ("repos/", "null"),
        ("repos/", "[null]"),
        ("repos/", "[1]"),
    ],
)
def test_main_reports_invalid_api_json_without_publishing_a_branch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], endpoint: str, payload: str
) -> None:
    handoff = _module()
    remote, _ = _tap_remote(tmp_path, main_formula=_formula("1.0.0"))
    formula = tmp_path / "korvid.rb"
    formula.write_text(_formula("1.2.3"), encoding="utf-8")
    runner = _FakeRunner(handoff)

    def run(argv: list[str], *, cwd: Path | None = None) -> str:
        if len(argv) >= 3 and argv[:2] == ["gh", "api"] and argv[2].startswith(endpoint):
            return payload
        return runner(argv, cwd=cwd)

    assert (
        handoff.main(
            [
                "--version",
                "1.2.3",
                "--formula",
                str(formula),
                "--tap-clone-source",
                str(remote),
                "--clone-dir",
                str(tmp_path / "clone"),
                "--bot-login",
                "homebrew-release[bot]",
            ],
            command_runner=run,
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert "Traceback" not in captured.err
    assert not _has_remote_branch(remote, "bump-korvid-1.2.3")


@pytest.mark.parametrize("probe", ["existing", "absent", "transport-error"])
def test_raw_remote_probe_distinguishes_absence_from_transport_failure(
    tmp_path: Path, probe: str
) -> None:
    handoff = _module()
    _, seed = _tap_remote(tmp_path, main_formula=_formula("1.0.0"))
    remote_branch_exists = handoff._module._remote_branch_exists
    assert callable(remote_branch_exists)
    if probe == "transport-error":
        _git(seed, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
        with pytest.raises(handoff.HandoffError, match="git ls-remote failed"):
            remote_branch_exists(seed, "main")
    else:
        branch = "main" if probe == "existing" else "missing"
        assert remote_branch_exists(seed, branch) is (probe == "existing")


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


@pytest.mark.parametrize(
    ("formula_text", "message"),
    [
        (
            _formula("1.2.3").replace(
                '  url "https://files.pythonhosted.org/packages/source/k/korvid/korvid-1.2.3.tar.gz"\n',
                "",
            ),
            "url",
        ),
        (
            _formula("1.2.3").replace(
                '    assert_match "1.2.3", shell_output("#{bin}/korvid --version")\n', ""
            ),
            "assert_match",
        ),
        (
            _formula("1.2.3").replace(
                '    assert_match "1.2.3", shell_output("#{bin}/korvid --version")\n',
                '    assert_match "9.9.9", shell_output("#{bin}/korvid --version")\n',
            ),
            "disagree",
        ),
        (
            _formula("1.2.3").replace(
                "  sha256 ",
                '  url "https://files.pythonhosted.org/packages/source/k/korvid/korvid-1.2.3.tar.gz"\n'
                "  sha256 ",
            ),
            "single url version anchor",
        ),
        (
            _formula("1.2.3").replace(
                "  end\n",
                '    assert_match "1.2.3", shell_output("#{bin}/korvid --version")\n  end\n',
            ),
            "single assert_match version anchor",
        ),
    ],
)
def test_formula_version_requires_both_version_anchors_to_match(
    formula_text: str, message: str
) -> None:
    handoff = _module()

    with pytest.raises(handoff.HandoffError, match=message):
        handoff._module._formula_version(formula_text)


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
    branch_head = _push_branch(seed, branch=branch, formula=_formula("0.4.1"))
    formula = tmp_path / "generated.rb"
    formula.write_text(_formula("0.4.1"), encoding="utf-8")
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


def test_a_safe_retry_refreshes_a_same_version_branch_when_the_formula_changed(
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
        handoff, open_prs=[], created_pr_url="https://github.com/hellices/homebrew-korvid/pull/44"
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
    assert result.pr_number == 44
    assert _remote_branch_commit(remote, branch) != branch_head
    assert _git_dir(remote, "show", f"refs/heads/{branch}:Formula/korvid.rb").rstrip(
        "\n"
    ) == _formula("0.4.1", suffix="# regenerated later\n").rstrip("\n")


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
    assert _remote_branch_commit(remote, branch) != branch_head
    assert _git_dir(remote, "show", f"refs/heads/{branch}:Formula/korvid.rb").rstrip(
        "\n"
    ) == _formula("0.4.1", suffix="# regenerated later\n").rstrip("\n")
    assert any(call[0][:3] == ("gh", "pr", "create") for call in runner.calls)


def test_an_existing_same_version_branch_with_a_matching_bot_owned_pull_request_is_reused(
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
    assert _remote_branch_commit(remote, branch) != branch_head
    assert _git_dir(remote, "show", f"refs/heads/{branch}:Formula/korvid.rb").rstrip(
        "\n"
    ) == _formula("0.4.1", suffix="# regenerated later\n").rstrip("\n")
    assert not any(call[0][:3] == ("gh", "pr", "create") for call in runner.calls)
    assert any(call[0][:3] == ("gh", "api", "users/homebrew-release[bot]") for call in runner.calls)


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
