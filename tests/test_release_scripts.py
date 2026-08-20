"""Release tooling (issue #169): version gate, bundle assembly, checksums,
offline verification helpers, and release metadata — the logic-bearing parts
of the release workflow, unit-tested so the YAML stays thin."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml

from tests.release_contracts import markdown_section, run_scripts, workflow_jobs

SCRIPTS = Path(__file__).parents[1] / "scripts" / "release"
sys.path.insert(0, str(SCRIPTS))

import bundle  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import check_artifacts  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import check_dry_run  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import check_sbom  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import check_source  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import check_version  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import compare_assets  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import metadata  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import offline_verify  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import release_manifest  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import smoke_install  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import version_format  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path


def _pyproject(tmp_path: Path, version: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nname = "korvid"\nversion = "{version}"\n')
    return path


# --- check_version ----------------------------------------------------------


def test_matching_tag_passes_and_prints_the_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert check_version.main(["v1.2.3", str(_pyproject(tmp_path, "1.2.3"))]) == 0
    assert capsys.readouterr().out.strip() == "1.2.3"


def test_mismatched_tag_fails_naming_both_versions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert check_version.main(["v1.2.3", str(_pyproject(tmp_path, "1.2.4"))]) == 1
    err = capsys.readouterr().err
    assert "v1.2.3" in err
    assert "1.2.4" in err


def test_tag_without_v_prefix_fails(tmp_path: Path) -> None:
    assert check_version.main(["1.2.3", str(_pyproject(tmp_path, "1.2.3"))]) == 1


@pytest.mark.parametrize("version", ["0.1.0.dev1", "1.0", "0.1.0rc1", "1.2.3-x", "$(id)"])
def test_release_tag_outside_the_supported_version_format_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    """The same X.Y.Z guard the dry run applies, on the publication path."""
    assert check_version.main([f"v{version}", str(_pyproject(tmp_path, version))]) == 1
    captured = capsys.readouterr()
    assert "supported release version" in captured.err
    assert captured.out.strip() == ""


def test_release_version_format_helper_is_shared_by_both_gates() -> None:
    assert version_format.is_supported_release_version("0.1.0")
    assert version_format.is_supported_release_version("10.20.30")
    assert not version_format.is_supported_release_version("0.1.0.dev1")
    assert not version_format.is_supported_release_version("1.0")
    assert not version_format.is_supported_release_version("\u0660.\u0661.\u0660")


# --- check_source -----------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _release_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "user.email", "release@example.invalid")
    (repo / "tracked.txt").write_text("reviewed\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "reviewed")
    return repo


def test_annotated_tag_reachable_from_trusted_branch_passes_without_logging_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _release_repo(tmp_path)
    _git(repo, "tag", "-a", "v1.2.3", "-m", "release 1.2.3")
    commit = _git(repo, "rev-parse", "v1.2.3^{}")
    assert check_source.main(["v1.2.3", "main", str(repo)]) == 0
    output = capsys.readouterr().out
    assert "release source verified" in output
    assert commit not in output


def test_tagged_commit_not_reachable_from_trusted_branch_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _release_repo(tmp_path)
    _git(repo, "checkout", "-b", "unreviewed")
    (repo / "tracked.txt").write_text("unreviewed\n")
    _git(repo, "commit", "-am", "unreviewed")
    _git(repo, "tag", "-a", "v1.2.3", "-m", "release 1.2.3")
    assert check_source.main(["v1.2.3", "main", str(repo)]) == 1
    assert "not reachable from trusted ref" in capsys.readouterr().err


def test_lightweight_release_tag_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _release_repo(tmp_path)
    _git(repo, "tag", "v1.2.3")
    assert check_source.main(["v1.2.3", "main", str(repo)]) == 1
    assert "annotated tag" in capsys.readouterr().err


def test_release_tag_must_still_match_the_originally_verified_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _release_repo(tmp_path)
    _git(repo, "tag", "-a", "v1.2.3", "-m", "release 1.2.3")
    verified_commit = _git(repo, "rev-parse", "v1.2.3^{}")
    assert (
        check_source.main(
            [
                "v1.2.3",
                "main",
                str(repo),
                "--expected-commit",
                verified_commit,
            ]
        )
        == 0
    )

    # Simulate a force-moved release tag after protected-environment
    # approval began: the remote re-fetch now resolves a different commit.
    (repo / "tracked.txt").write_text("moved\n")
    _git(repo, "commit", "-am", "move tag")
    _git(repo, "tag", "-d", "v1.2.3")
    _git(repo, "tag", "-a", "v1.2.3", "-m", "moved release")
    assert (
        check_source.main(
            [
                "v1.2.3",
                "main",
                str(repo),
                "--expected-commit",
                verified_commit,
            ]
        )
        == 1
    )
    assert "changed from verified commit" in capsys.readouterr().err


def test_source_policy_errors_do_not_log_commit_hashes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _release_repo(tmp_path)
    _git(repo, "checkout", "-b", "unreviewed")
    (repo / "tracked.txt").write_text("unreviewed\n")
    _git(repo, "commit", "-am", "unreviewed")
    _git(repo, "tag", "-a", "v1.2.3", "-m", "release 1.2.3")
    commit = _git(repo, "rev-parse", "v1.2.3^{}")
    assert check_source.main(["v1.2.3", "main", str(repo)]) == 1
    error = capsys.readouterr().err
    assert commit not in error
    assert "v1.2.3" not in error
    assert "main" not in error


# --- check_dry_run ----------------------------------------------------------


def test_dry_run_at_trusted_head_prints_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _release_repo(tmp_path)
    assert check_dry_run.main(["main", str(repo), str(_pyproject(repo, "0.1.0"))]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"


def test_dry_run_refuses_a_commit_behind_trusted_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _release_repo(tmp_path)
    reviewed_commit = _git(repo, "rev-parse", "HEAD")
    (repo / "tracked.txt").write_text("trusted update\n")
    _git(repo, "commit", "-am", "trusted update")
    trusted_head = _git(repo, "rev-parse", "main")
    _git(repo, "checkout", "--detach", reviewed_commit)
    assert check_dry_run.main(["main", str(repo), str(_pyproject(repo, "0.1.0"))]) == 1
    error = capsys.readouterr().err
    assert "trusted branch head" in error
    assert reviewed_commit not in error
    assert trusted_head not in error


def test_dry_run_refuses_an_unreviewed_commit_ahead_of_trusted_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _release_repo(tmp_path)
    trusted_head = _git(repo, "rev-parse", "main")
    _git(repo, "checkout", "--detach")
    (repo / "tracked.txt").write_text("unreviewed ahead\n")
    _git(repo, "commit", "-am", "unreviewed ahead")
    ahead_commit = _git(repo, "rev-parse", "HEAD")
    assert check_dry_run.main(["main", str(repo), str(_pyproject(repo, "0.1.0"))]) == 1
    error = capsys.readouterr().err
    assert "trusted branch head" in error
    assert ahead_commit not in error
    assert trusted_head not in error


def test_dry_run_refuses_invalid_project_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _release_repo(tmp_path)
    pyproject = repo / "pyproject.toml"
    pyproject.write_text('[project]\nname = "korvid"\n')
    assert check_dry_run.main(["main", str(repo), str(pyproject)]) == 1
    assert "project metadata is invalid" in capsys.readouterr().err


def test_dry_run_refuses_a_stale_dispatch_sha_against_the_live_remote_ref(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dispatch can check out an older SHA while `origin/main` has moved on;
    comparing against the live remote-tracking ref must reject it."""
    repo = _release_repo(tmp_path)
    stale_commit = _git(repo, "rev-parse", "HEAD")
    (repo / "tracked.txt").write_text("live main\n")
    _git(repo, "commit", "-am", "live main")
    live_commit = _git(repo, "rev-parse", "main")
    _git(repo, "update-ref", "refs/remotes/origin/main", live_commit)
    _git(repo, "checkout", "--detach", stale_commit)

    assert check_dry_run.main(["origin/main", str(repo), str(_pyproject(repo, "0.1.0"))]) == 1
    error = capsys.readouterr().err
    assert "trusted branch head" in error
    assert stale_commit not in error
    assert live_commit not in error


def test_dry_run_accepts_the_live_remote_tracking_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _release_repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "main"))
    assert check_dry_run.main(["origin/main", str(repo), str(_pyproject(repo, "0.1.0"))]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"


@pytest.mark.parametrize("version", ["0.1.0.dev1", "1.0", "0.1.0rc1", "1.2.3-x", "$(id)", ""])
def test_dry_run_rejects_versions_outside_the_supported_release_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    """The version reaches shell interpolation, `$GITHUB_OUTPUT`, and artifact
    file names, so validate its shape before printing it."""
    repo = _release_repo(tmp_path)
    assert check_dry_run.main(["main", str(repo), str(_pyproject(repo, version))]) == 1
    captured = capsys.readouterr()
    assert "supported release version" in captured.err
    assert captured.out.strip() == ""


def test_dry_run_accepts_the_supported_x_y_z_release_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _release_repo(tmp_path)
    assert check_dry_run.main(["main", str(repo), str(_pyproject(repo, "10.20.30"))]) == 0
    assert capsys.readouterr().out.strip() == "10.20.30"


# --- checksums --------------------------------------------------------------


def test_sha256sums_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "a.whl").write_bytes(b"wheel-a")
    (tmp_path / "b.json").write_bytes(b"{}")
    sums = bundle.write_sha256sums(tmp_path, ["a.whl", "b.json"])
    assert sums.name == "SHA256SUMS"
    offline_verify.verify_sha256sums(tmp_path)  # must not raise


def test_sha256sums_detect_tampering(tmp_path: Path) -> None:
    (tmp_path / "a.whl").write_bytes(b"wheel-a")
    bundle.write_sha256sums(tmp_path, ["a.whl"])
    (tmp_path / "a.whl").write_bytes(b"tampered")
    with pytest.raises(ValueError, match=r"a\.whl"):
        offline_verify.verify_sha256sums(tmp_path)


def test_sha256sums_detect_missing_files(tmp_path: Path) -> None:
    (tmp_path / "a.whl").write_bytes(b"wheel-a")
    bundle.write_sha256sums(tmp_path, ["a.whl"])
    (tmp_path / "a.whl").unlink()
    with pytest.raises(ValueError, match=r"a\.whl"):
        offline_verify.verify_sha256sums(tmp_path)


def test_sha256sums_reject_an_unlisted_extra_wheel(tmp_path: Path) -> None:
    (tmp_path / "korvid.whl").write_bytes(b"korvid")
    bundle.write_sha256sums(tmp_path, ["korvid.whl"])
    (tmp_path / "injected-newer.whl").write_bytes(b"injected")
    with pytest.raises(ValueError, match="unlisted"):
        offline_verify.verify_sha256sums(tmp_path)


def test_sha256sums_reject_a_path_outside_the_bundle(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    outside = tmp_path / "outside.whl"
    outside.write_bytes(b"outside")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    (root / "SHA256SUMS").write_text(f"{digest}  ../outside.whl\n")
    with pytest.raises(ValueError, match="unsafe"):
        offline_verify.verify_sha256sums(root)


# --- bundle assembly --------------------------------------------------------


def _fake_wheelhouse(tmp_path: Path) -> Path:
    wheels = tmp_path / "wheelhouse"
    wheels.mkdir()
    (wheels / "korvid-1.2.3-py3-none-any.whl").write_bytes(b"korvid")
    (wheels / "httpx-0.28.0-py3-none-any.whl").write_bytes(b"httpx")
    return wheels


def test_bundle_layout_and_archive(tmp_path: Path) -> None:
    wheels = _fake_wheelhouse(tmp_path)
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text("{}")
    archive = bundle.main(
        [
            "--version",
            "1.2.3",
            "--platform-tag",
            "linux-x86_64",
            "--python-tag",
            "3.12",
            "--wheels",
            str(wheels),
            "--sbom",
            str(sbom),
            "--output",
            str(tmp_path / "out"),
        ]
    )
    assert archive is not None
    path = Path(archive)
    assert path.name == "korvid-1.2.3-offline-linux-x86_64-py3.12.tar.gz"
    with tarfile.open(path) as tar:
        names = tar.getnames()
    root = "korvid-1.2.3-offline-linux-x86_64-py3.12"
    for expected in (
        f"{root}/wheels/korvid-1.2.3-py3-none-any.whl",
        f"{root}/wheels/httpx-0.28.0-py3-none-any.whl",
        f"{root}/install.sh",
        f"{root}/install.ps1",
        f"{root}/SHA256SUMS",
        f"{root}/sbom.cdx.json",
        f"{root}/README.txt",
    ):
        assert expected in names


def test_bundle_windows_archives_as_zip(tmp_path: Path) -> None:
    wheels = _fake_wheelhouse(tmp_path)
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text("{}")
    archive = bundle.main(
        [
            "--version",
            "1.2.3",
            "--platform-tag",
            "windows-x86_64",
            "--python-tag",
            "3.11",
            "--wheels",
            str(wheels),
            "--sbom",
            str(sbom),
            "--output",
            str(tmp_path / "out"),
        ]
    )
    assert archive is not None
    path = Path(archive)
    assert path.suffix == ".zip"
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    assert any(name.endswith("install.ps1") for name in names)


def test_bundle_checksums_cover_every_wheel(tmp_path: Path) -> None:
    wheels = _fake_wheelhouse(tmp_path)
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text("{}")
    archive = bundle.main(
        [
            "--version",
            "1.2.3",
            "--platform-tag",
            "linux-x86_64",
            "--python-tag",
            "3.12",
            "--wheels",
            str(wheels),
            "--sbom",
            str(sbom),
            "--output",
            str(tmp_path / "out"),
        ]
    )
    assert archive is not None
    root = tmp_path / "out" / "korvid-1.2.3-offline-linux-x86_64-py3.12"
    sums = (root / "SHA256SUMS").read_text()
    assert "wheels/korvid-1.2.3-py3-none-any.whl" in sums
    assert "wheels/httpx-0.28.0-py3-none-any.whl" in sums
    assert "sbom.cdx.json" in sums
    offline_verify.verify_sha256sums(root)  # bundle output verifies clean


# --- offline verification helpers -------------------------------------------


def test_smoke_install_requirement_for_base_uses_the_local_wheel_url(tmp_path: Path) -> None:
    wheel = tmp_path / "korvid-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    assert smoke_install.requirement_for(wheel, "base") == wheel.resolve().as_uri()


def test_smoke_install_requirement_for_agent_uses_a_pep_508_direct_reference(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "korvid-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    assert smoke_install.requirement_for(wheel, "agent") == (
        f"korvid[agent] @ {wheel.resolve().as_uri()}"
    )


def test_smoke_install_required_modules_follow_the_selected_variant() -> None:
    assert smoke_install.required_modules("base") == set()
    assert smoke_install.required_modules("agent") == {"httpx", "keyring"}
    assert smoke_install.required_modules("mcp") == {"mcp"}
    assert smoke_install.required_modules("all") == {"httpx", "keyring", "mcp"}


def test_smoke_install_required_korvid_modules_follow_the_selected_variant() -> None:
    base = {"korvid.__main__", "korvid.ui.app"}
    agent = {"korvid.providers.registry", "korvid.providers.token_store"}
    mcp = {"korvid.mcp.server"}
    assert smoke_install.required_korvid_modules("base") == base
    assert smoke_install.required_korvid_modules("agent") == base | agent
    assert smoke_install.required_korvid_modules("mcp") == base | mcp
    assert smoke_install.required_korvid_modules("all") == base | agent | mcp


def test_smoke_install_forbids_optional_feature_packages_outside_their_variant() -> None:
    """MCP 2 uses httpx2, so a plain MCP install must not leak agent/obs httpx."""
    assert smoke_install.forbidden_modules("base") == {"httpx", "keyring", "mcp"}
    assert smoke_install.forbidden_modules("agent") == {"mcp"}
    assert smoke_install.forbidden_modules("mcp") == {"httpx", "keyring"}
    assert smoke_install.forbidden_modules("all") == set()


def test_smoke_install_variant_matrix_excludes_entra() -> None:
    """`entra` is deliberately outside the agreed base/agent/mcp/all matrix."""
    assert set(smoke_install.variants()) == {"base", "agent", "mcp", "all"}


def test_smoke_install_plan_installs_the_variant_directly_in_a_fresh_environment(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "korvid-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    for variant in smoke_install.variants():
        plan = smoke_install.install_plan(wheel, variant)
        fresh = plan[0]
        assert fresh.name == "fresh"
        assert fresh.requirements == (smoke_install.requirement_for(wheel, variant),)


def test_smoke_install_plan_keeps_a_separate_base_to_extra_expansion_check(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "korvid-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    base_requirement = smoke_install.requirement_for(wheel, "base")
    for variant in ("agent", "mcp", "all"):
        plan = smoke_install.install_plan(wheel, variant)
        assert len(plan) == 2
        expansion = plan[1]
        assert expansion.name == "expansion"
        assert expansion.requirements == (
            base_requirement,
            smoke_install.requirement_for(wheel, variant),
        )
        assert expansion.env_dir_name != plan[0].env_dir_name


def test_smoke_install_plan_for_base_is_a_single_fresh_environment(tmp_path: Path) -> None:
    wheel = tmp_path / "korvid-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    plan = smoke_install.install_plan(wheel, "base")
    assert len(plan) == 1
    assert plan[0].requirements == (wheel.resolve().as_uri(),)


def test_smoke_install_rejects_unknown_variants(tmp_path: Path) -> None:
    wheel = tmp_path / "korvid-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    with pytest.raises(ValueError, match="unknown variant"):
        smoke_install.requirement_for(wheel, "nope")
    with pytest.raises(ValueError, match="unknown variant"):
        smoke_install.required_modules("nope")
    with pytest.raises(ValueError, match="unknown variant"):
        smoke_install.required_korvid_modules("nope")
    with pytest.raises(ValueError, match="unknown variant"):
        smoke_install.forbidden_modules("nope")
    with pytest.raises(ValueError, match="unknown variant"):
        smoke_install.install_plan(wheel, "nope")


def _create_fake_smoke_venv(env_dir: Path, *, with_pip: bool = False) -> None:
    launcher_dir = env_dir / ("Scripts" if smoke_install.os.name == "nt" else "bin")
    launcher_dir.mkdir(parents=True)
    for name in ("python", "korvid"):
        binary = launcher_dir / (f"{name}.exe" if smoke_install.os.name == "nt" else name)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)


def _complete_fake_smoke_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    if "uninstall" in args:
        launcher = smoke_install._resolve_launcher(Path(args[0]).parent.parent)
        if launcher is not None:
            launcher.unlink()
    stdout = "usage: korvid" if args[1:] == ["--help"] else "korvid 1.2.3"
    return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


def test_smoke_install_runs_a_fresh_install_then_a_separate_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the real phase runner offline: the fresh environment installs the
    variant directly, and the expansion environment is a *different* venv that
    starts from base."""
    wheel = tmp_path / "korvid-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    created: list[Path] = []
    commands: list[list[str]] = []

    def _fake_venv(env_dir: Path, *, with_pip: bool = False) -> None:
        created.append(env_dir)
        _create_fake_smoke_venv(env_dir, with_pip=with_pip)

    def _fake_run(
        args: list[str], *, env: dict[str, str], cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return _complete_fake_smoke_command(args)

    monkeypatch.setattr(smoke_install.venv, "create", _fake_venv)
    monkeypatch.setattr(smoke_install, "_run", _fake_run)

    smoke_install._smoke_install(wheel, "1.2.3", "agent", workspace)

    assert created == [workspace / "venv-fresh", workspace / "venv-expansion"]
    installs = [args for args in commands if "install" in args]
    agent_requirement = smoke_install.requirement_for(wheel, "agent")
    base_requirement = smoke_install.requirement_for(wheel, "base")
    assert installs[0][-1] == agent_requirement
    assert "--upgrade" not in installs[0]
    assert installs[1][-1] == base_requirement
    assert installs[2][-1] == agent_requirement
    assert "--upgrade" in installs[2]
    # The fresh install must never be reached through a base install first.
    assert base_requirement not in installs[0]
    assert any("keyring" in " ".join(args) for args in commands)
    assert any("korvid.providers.registry" in " ".join(args) for args in commands)
    assert any("korvid.__main__" in " ".join(args) for args in commands)
    assert any("find_spec('mcp')" in " ".join(args) for args in commands)


def test_smoke_install_resolves_a_relative_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every command runs with `cwd=workspace`, so a relative workspace would
    make the venv interpreter path unresolvable."""
    wheel = tmp_path / "korvid-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    seen: list[Path] = []
    monkeypatch.setattr(
        smoke_install,
        "_smoke_install",
        lambda _wheel, _version, _variant, workspace: seen.append(workspace),
    )
    monkeypatch.chdir(tmp_path)

    assert (
        smoke_install.main(
            [
                "--wheel",
                str(wheel),
                "--version",
                "1.2.3",
                "--variant",
                "base",
                "--workspace",
                "relative-workspace",
            ]
        )
        == 0
    )
    assert seen == [tmp_path.resolve() / "relative-workspace"]
    assert seen[0].is_absolute()


def test_smoke_install_reports_workspace_cleanup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cleanup stays fail-closed: an unremovable workspace fails the job."""
    wheel = tmp_path / "korvid-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    monkeypatch.setattr(smoke_install, "_smoke_install", lambda *args, **kwargs: None)

    def _boom(path: object) -> None:
        raise OSError("device busy")

    monkeypatch.setattr(smoke_install.shutil, "rmtree", _boom)
    exit_code = smoke_install.main(
        [
            "--wheel",
            str(wheel),
            "--version",
            "1.2.3",
            "--variant",
            "base",
            "--workspace",
            str(tmp_path / "workspace"),
        ]
    )
    assert exit_code == 1
    assert "failed to clean workspace" in capsys.readouterr().err


def test_smoke_install_rejects_a_wheel_with_the_wrong_version(tmp_path: Path) -> None:
    wheel = tmp_path / "korvid-1.2.4-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    with pytest.raises(ValueError, match=r"1\.2\.3"):
        smoke_install.validate_wheel_version(wheel, "1.2.3")


def test_pip_install_tooling_state_does_not_pollute_runtime_user_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for #201: pip side-effects (e.g. .rustup/settings.toml)
    must not land in the runtime user-state roots checked by _assert_no_user_state.
    RED before the fix (pip and runtime share one HOME → assertion fires),
    GREEN after env separation (pip gets its own disposable HOME)."""
    wheel = tmp_path / "korvid-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    commands: list[tuple[list[str], Path]] = []

    def _fake_run(
        args: list[str], *, env: dict[str, str], cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        commands.append((args, Path(env["HOME"])))
        if "pip" in args and "install" in args and "uninstall" not in args:
            # Simulate pip writing toolchain state (reproduces the real failure)
            rustup_dir = Path(env["HOME"]) / ".rustup"
            rustup_dir.mkdir(parents=True, exist_ok=True)
            (rustup_dir / "settings.toml").write_text("[toolchain]\n")
        return _complete_fake_smoke_command(args)

    monkeypatch.setattr(smoke_install.venv, "create", _create_fake_smoke_venv)
    monkeypatch.setattr(smoke_install, "_run", _fake_run)

    smoke_install._smoke_install(wheel, "1.2.3", "base", workspace)
    tool_roots = smoke_install._tooling_roots(workspace)
    runtime_roots = smoke_install._state_roots(workspace)
    assert (tool_roots.home / ".rustup" / "settings.toml").is_file()
    assert tool_roots.home.is_relative_to(workspace)
    assert smoke_install._unexpected_files(runtime_roots.home) == []
    tooling_commands = [(args, home) for args, home in commands if args[1:3] == ["-m", "pip"]]
    runtime_commands = [(args, home) for args, home in commands if args[1:3] != ["-m", "pip"]]
    assert any("install" in args for args, _home in tooling_commands)
    assert any("uninstall" in args for args, _home in tooling_commands)
    assert all(home == tool_roots.home for _args, home in tooling_commands)
    assert any(args[1:] == ["--help"] for args, _home in runtime_commands)
    assert any(args[1:] == ["--version"] for args, _home in runtime_commands)
    assert any("import korvid;" in " ".join(args) for args, _home in runtime_commands)
    assert any("import korvid.__main__" in " ".join(args) for args, _home in runtime_commands)
    assert any("find_spec('mcp')" in " ".join(args) for args, _home in runtime_commands)
    assert all(home == runtime_roots.home for _args, home in runtime_commands)


def test_smoke_install_catches_runtime_probe_user_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime probes must use the roots checked by the fail-closed assertion."""
    wheel = tmp_path / "korvid-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def _fake_run(
        args: list[str], *, env: dict[str, str], cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        if args[1:] == ["--help"]:
            home = Path(env["HOME"])
            home.mkdir(parents=True, exist_ok=True)
            (home / ".korvid_state").write_text("runtime-probe-artifact\n")
        return _complete_fake_smoke_command(args)

    monkeypatch.setattr(smoke_install.venv, "create", _create_fake_smoke_venv)
    monkeypatch.setattr(smoke_install, "_run", _fake_run)

    with pytest.raises(RuntimeError, match="noninteractive smoke created user-state files"):
        smoke_install._smoke_install(wheel, "1.2.3", "base", workspace)


def test_pick_removable_wheel_never_picks_korvid(tmp_path: Path) -> None:
    wheels = _fake_wheelhouse(tmp_path)
    victim = offline_verify.pick_removable_wheel(wheels)
    assert victim.name == "httpx-0.28.0-py3-none-any.whl"


def test_pick_removable_wheel_requires_a_dependency(tmp_path: Path) -> None:
    wheels = tmp_path / "only-korvid"
    wheels.mkdir()
    (wheels / "korvid-1.2.3-py3-none-any.whl").write_bytes(b"korvid")
    with pytest.raises(ValueError, match="dependency wheel"):
        offline_verify.pick_removable_wheel(wheels)


def test_offline_verifier_uses_posix_console_launcher(tmp_path: Path) -> None:
    assert offline_verify._venv_launcher(tmp_path, platform_name="linux") == (
        tmp_path / "bin" / "korvid"
    )


def test_offline_verifier_uses_windows_console_launcher(tmp_path: Path) -> None:
    assert offline_verify._venv_launcher(tmp_path, platform_name="win32") == (
        tmp_path / "Scripts" / "korvid.exe"
    )


# --- artifact metadata ------------------------------------------------------


def _metadata_text(
    *,
    version: str = "1.2.3",
    include_entra: bool = True,
    include_keyring: bool = True,
    include_urls: bool = True,
    urls_block: str | None = None,
    content_type: str | None = "text/markdown",
    body: str = (
        "# korvid\n\nAI-native Kubernetes TUI - a keyboard-first cockpit with an"
        " embedded agent that can read your cluster, explain what it found, and"
        " carry out changes only behind an explicit approval gate. Runs on"
        " Linux, macOS and Windows against any reachable kube context.\n"
    ),
) -> str:
    entra = (
        'Provides-Extra: entra\nRequires-Dist: azure-identity>=1.19; extra == "entra"\n'
        if include_entra
        else ""
    )
    keyring = 'Requires-Dist: keyring>=25.7.0; extra == "agent"\n' if include_keyring else ""
    urls = (
        "Project-URL: Homepage, https://github.com/hellices/korvid\n"
        "Project-URL: Source, https://github.com/hellices/korvid\n"
        "Project-URL: Issues, https://github.com/hellices/korvid/issues\n"
        if include_urls
        else ""
    )
    if urls_block is not None:
        urls = urls_block
    described = f"Description-Content-Type: {content_type}\n" if content_type else ""
    return (
        "Metadata-Version: 2.4\n"
        "Name: korvid\n"
        f"Version: {version}\n"
        f"{urls}"
        f"{described}"
        "Provides-Extra: agent\n"
        "Provides-Extra: mcp\n"
        f"{entra}"
        "Provides-Extra: observability\n"
        "Provides-Extra: all\n"
        'Requires-Dist: httpx>=0.27; extra == "agent"\n'
        f"{keyring}"
        'Requires-Dist: mcp<2,>=1.10; extra == "mcp"\n'
        'Requires-Dist: anyio>=4.5; extra == "mcp"\n'
        'Requires-Dist: starlette>=0.36; extra == "mcp"\n'
        'Requires-Dist: uvicorn>=0.30; extra == "mcp"\n'
        'Requires-Dist: httpx>=0.27; extra == "observability"\n'
        'Requires-Dist: httpx>=0.27; extra == "all"\n'
        'Requires-Dist: keyring>=25.7.0; extra == "all"\n'
        'Requires-Dist: mcp<2,>=1.10; extra == "all"\n'
        'Requires-Dist: anyio>=4.5; extra == "all"\n'
        'Requires-Dist: starlette>=0.36; extra == "all"\n'
        'Requires-Dist: uvicorn>=0.30; extra == "all"\n'
        "\n"
        f"{body}"
    )


def _fake_dist(tmp_path: Path, metadata_text: str) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "korvid-1.2.3-py3-none-any.whl", "w") as wheel:
        wheel.writestr("korvid-1.2.3.dist-info/METADATA", metadata_text)
    pkg_info = tmp_path / "PKG-INFO"
    pkg_info.write_text(metadata_text)
    with tarfile.open(dist / "korvid-1.2.3.tar.gz", "w:gz") as sdist:
        sdist.add(pkg_info, arcname="korvid-1.2.3/PKG-INFO")
    return dist


def test_wheel_and_sdist_metadata_match_version_and_extras(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path, _metadata_text())
    assert check_artifacts.main(["--dist", str(dist), "--version", "1.2.3"]) == 0


def test_artifact_metadata_missing_an_extra_fails(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path, _metadata_text(include_entra=False))
    with pytest.raises(ValueError, match="entra"):
        check_artifacts.main(["--dist", str(dist), "--version", "1.2.3"])


def test_artifact_metadata_version_mismatch_fails(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path, _metadata_text(version="1.2.4"))
    with pytest.raises(ValueError, match=r"1\.2\.4"):
        check_artifacts.main(["--dist", str(dist), "--version", "1.2.3"])


def test_artifact_metadata_rejects_a_partial_extra_dependency_set(
    tmp_path: Path,
) -> None:
    dist = _fake_dist(tmp_path, _metadata_text(include_keyring=False))
    with pytest.raises(ValueError, match="keyring"):
        check_artifacts.main(["--dist", str(dist), "--version", "1.2.3"])


def test_artifact_metadata_requires_the_pypi_project_page_fields(tmp_path: Path) -> None:
    """The long description is the PyPI project page.

    An artifact can carry a correct version and a correct dependency set and
    still land on PyPI as a blank page - `Description-Content-Type` missing
    makes PyPI fall back to plain text, and an absent body renders nothing
    at all. Neither is recoverable: a released version cannot be reuploaded.
    """
    dist = _fake_dist(tmp_path, _metadata_text(body=""))
    with pytest.raises(ValueError, match="long description"):
        check_artifacts.main(["--dist", str(dist), "--version", "1.2.3"])


def test_artifact_metadata_requires_a_markdown_content_type(tmp_path: Path) -> None:
    dist = _fake_dist(tmp_path, _metadata_text(content_type=None))
    with pytest.raises(ValueError, match="Description-Content-Type"):
        check_artifacts.main(["--dist", str(dist), "--version", "1.2.3"])


def test_artifact_metadata_requires_the_project_urls(tmp_path: Path) -> None:
    """`[project.urls]` is what builds PyPI's sidebar. Losing it silently is
    easy - a stray edit to pyproject drops the whole table - and the result
    is a project page with nowhere to click through to the source."""
    dist = _fake_dist(tmp_path, _metadata_text(include_urls=False))
    with pytest.raises(ValueError, match="Project-URL"):
        check_artifacts.main(["--dist", str(dist), "--version", "1.2.3"])


@pytest.mark.parametrize(
    "urls",
    [
        pytest.param("Project-URL: Homepage\n", id="no-delimiter"),
        pytest.param("Project-URL: Homepage,\n", id="empty-url"),
        pytest.param("Project-URL: Homepage,    \n", id="blank-url"),
    ],
)
def test_artifact_metadata_rejects_a_project_url_with_no_destination(
    tmp_path: Path, urls: str
) -> None:
    """A label alone is not a link.

    Splitting on the comma and keeping the left side meant `Project-URL:
    Homepage` satisfied the requirement while pointing nowhere - the check
    passed on metadata that renders an empty sidebar.
    """
    dist = _fake_dist(tmp_path, _metadata_text(urls_block=urls))
    with pytest.raises(ValueError, match="Project-URL"):
        check_artifacts.main(["--dist", str(dist), "--version", "1.2.3"])


@pytest.mark.parametrize(
    ("content_type", "accepted"),
    [
        pytest.param("text/markdown", True, id="bare"),
        pytest.param("text/markdown; charset=UTF-8", True, id="charset"),
        pytest.param("text/markdown; variant=GFM", True, id="variant"),
        pytest.param("TEXT/Markdown", True, id="case-insensitive"),
        pytest.param("text/markdown-broken", False, id="lookalike"),
        pytest.param("text/x-rst", False, id="rst"),
        pytest.param("text/plain", False, id="plain"),
    ],
)
def test_artifact_metadata_matches_the_media_type_exactly(
    tmp_path: Path, content_type: str, accepted: bool
) -> None:
    """`startswith("text/markdown")` also accepts `text/markdown-broken`.

    PyPI would render that as plain text, so a fail-closed check that
    approves it is worse than none - it reports success on the one property
    that cannot be fixed after upload.
    """
    dist = _fake_dist(tmp_path, _metadata_text(content_type=content_type))
    if accepted:
        assert check_artifacts.main(["--dist", str(dist), "--version", "1.2.3"]) == 0
    else:
        with pytest.raises(ValueError, match="Description-Content-Type"):
            check_artifacts.main(["--dist", str(dist), "--version", "1.2.3"])


# --- SBOM completeness ------------------------------------------------------


def _sbom(path: Path, *, include_korvid: bool = True) -> Path:
    payload = {
        "metadata": {"component": {"name": "korvid" if include_korvid else "dependencies"}},
        "components": [
            {"name": "httpx", "version": "0.28.0"},
            {"name": "textual", "version": "8.1.1"},
        ],
    }
    path.write_text(json.dumps(payload))
    return path


def test_sbom_contains_korvid_and_every_exported_dependency(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-e .\nhttpx==0.28.0 \\\n  --hash=sha256:abc\ntextual==8.1.1\n")
    assert (
        check_sbom.main(
            ["--sbom", str(_sbom(tmp_path / "sbom.json")), "--requirements", str(requirements)]
        )
        == 0
    )


def test_sbom_without_korvid_fails(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("httpx==0.28.0\n")
    with pytest.raises(ValueError, match="korvid"):
        check_sbom.main(
            [
                "--sbom",
                str(_sbom(tmp_path / "sbom.json", include_korvid=False)),
                "--requirements",
                str(requirements),
            ]
        )


def test_sbom_missing_a_locked_dependency_fails(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("httpx==0.28.0\nanyio==4.0.0\n")
    with pytest.raises(ValueError, match="anyio"):
        check_sbom.main(
            ["--sbom", str(_sbom(tmp_path / "sbom.json")), "--requirements", str(requirements)]
        )


def test_sbom_dependency_version_must_match_the_lock(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("httpx==0.27.0\n")
    with pytest.raises(ValueError, match=r"httpx==0\.27\.0"):
        check_sbom.main(
            ["--sbom", str(_sbom(tmp_path / "sbom.json")), "--requirements", str(requirements)]
        )


# --- release-level manifest -------------------------------------------------


def test_release_manifest_covers_online_and_offline_artifacts(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    (artifacts / "dist").mkdir(parents=True)
    (artifacts / "offline").mkdir()
    (artifacts / "dist" / "korvid-1.2.3.whl").write_bytes(b"wheel")
    (artifacts / "dist" / "korvid-1.2.3.tar.gz").write_bytes(b"sdist")
    (artifacts / "offline" / "korvid-offline-linux.tar.gz").write_bytes(b"linux")
    (artifacts / "offline" / "korvid-offline-windows.zip").write_bytes(b"windows")
    output = tmp_path / "release-files"
    assert release_manifest.main(["--artifacts", str(artifacts), "--output", str(output)]) == 0
    sums = (output / "SHA256SUMS").read_text()
    assert "korvid-1.2.3.whl" in sums
    assert "korvid-1.2.3.tar.gz" in sums
    assert "korvid-offline-linux.tar.gz" in sums
    assert "korvid-offline-windows.zip" in sums


def test_release_manifest_rejects_colliding_asset_names(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    (artifacts / "one").mkdir(parents=True)
    (artifacts / "two").mkdir()
    (artifacts / "one" / "same.whl").write_bytes(b"one")
    (artifacts / "two" / "same.whl").write_bytes(b"two")
    with pytest.raises(ValueError, match=r"same\.whl"):
        release_manifest.main(
            ["--artifacts", str(artifacts), "--output", str(tmp_path / "release-files")]
        )


def test_release_asset_comparison_accepts_identical_sets(tmp_path: Path) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    for root in (local, remote):
        (root / "korvid.whl").write_bytes(b"same wheel")
        (root / "SHA256SUMS").write_bytes(b"same sums")
    assert compare_assets.main([str(local), str(remote)]) == 0


def test_release_asset_comparison_rejects_different_bytes(tmp_path: Path) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    (local / "korvid.whl").write_bytes(b"reviewed")
    (remote / "korvid.whl").write_bytes(b"rebuilt")
    with pytest.raises(ValueError, match=r"korvid\.whl"):
        compare_assets.main([str(local), str(remote)])


def test_release_asset_comparison_rejects_missing_assets(tmp_path: Path) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    (local / "offline.zip").write_bytes(b"bundle")
    with pytest.raises(ValueError, match=r"offline\.zip"):
        compare_assets.main([str(local), str(remote)])


# --- archive verification ---------------------------------------------------


def test_offline_verifier_extracts_the_published_archive(tmp_path: Path) -> None:
    wheels = _fake_wheelhouse(tmp_path)
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text("{}")
    archive = bundle.main(
        [
            "--version",
            "1.2.3",
            "--platform-tag",
            "linux-x86_64",
            "--python-tag",
            "3.12",
            "--wheels",
            str(wheels),
            "--sbom",
            str(sbom),
            "--output",
            str(tmp_path / "out"),
        ]
    )
    assert archive is not None
    extracted = offline_verify.extract_bundle(Path(archive), tmp_path / "extracted")
    assert extracted.name == "korvid-1.2.3-offline-linux-x86_64-py3.12"
    assert (extracted / "SHA256SUMS").is_file()
    assert (extracted / "wheels" / "korvid-1.2.3-py3-none-any.whl").is_file()


# --- workflow invariants ----------------------------------------------------


_RELEASE_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "release.yml"


def _release_workflow() -> str:
    return _RELEASE_WORKFLOW.read_text(encoding="utf-8")


def test_markdown_section_stops_at_the_next_peer_heading() -> None:
    text = "# Title\n## Release\nkeep\n### Child\nkeep child\n## Cleanup\ndrop\n"
    assert markdown_section(text, "Release") == "keep\n### Child\nkeep child"


def test_markdown_section_ignores_level_two_like_lines_inside_fenced_code_blocks() -> None:
    text = (
        "# Title\n"
        "## Release\n"
        "keep\n"
        "```yaml\n"
        "## not a real heading\n"
        "```\n"
        "still keep\n"
        "## Cleanup\n"
        "drop\n"
    )
    assert markdown_section(text, "Release") == (
        "keep\n```yaml\n## not a real heading\n```\nstill keep"
    )


def test_markdown_section_accepts_a_longer_closing_fence() -> None:
    text = (
        "# Title\n## Release\n```yaml\n## not a real heading\n````\nstill keep\n## Cleanup\ndrop\n"
    )
    assert markdown_section(text, "Release") == ("```yaml\n## not a real heading\n````\nstill keep")


def test_run_scripts_returns_only_shell_steps() -> None:
    job = {"steps": [{"uses": "actions/checkout@sha"}, {"run": "uv build"}, {"run": "uv publish"}]}
    assert run_scripts(job) == ("uv build", "uv publish")


def _bash_executable() -> str:
    if sys.platform != "win32":
        bash = shutil.which("bash")
        assert bash is not None
        return bash

    git = shutil.which("git")
    assert git is not None
    git_bash = Path(git).parent.parent / "bin" / "bash.exe"
    assert git_bash.is_file()
    return str(git_bash)


def _verify_step_run(key: str, value: str) -> str:
    document = yaml.safe_load(_release_workflow())
    for step in document["jobs"]["verify"]["steps"]:
        if step.get(key) == value:
            run_script = step.get("run")
            assert isinstance(run_script, str)
            return run_script
    raise AssertionError(f"verify step not found: {key}={value}")


def _overwritten_annotated_tag_checkout(tmp_path: Path) -> tuple[Path, str]:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = _release_repo(source_root)
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    tag = "v1.2.3"

    _git(tmp_path, "init", "--bare", str(remote))
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "tag", "-a", tag, "-m", "release 1.2.3")
    _git(source, "push", "origin", "main", tag)
    _git(tmp_path, "clone", "--branch", "main", str(remote), str(checkout))

    event_commit = _git(checkout, "rev-parse", f"refs/tags/{tag}^{{commit}}")
    _git(checkout, "update-ref", f"refs/tags/{tag}", event_commit)
    assert _git(checkout, "cat-file", "-t", f"refs/tags/{tag}") == "commit"
    return checkout, tag


def _readme() -> str:
    return (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")


def _release_runbook() -> str:
    path = Path(__file__).parents[1] / "docs" / "release.md"
    assert path.is_file(), "docs/release.md is missing"
    return path.read_text(encoding="utf-8")


def _security_policy() -> str:
    return (Path(__file__).parents[1] / "SECURITY.md").read_text(encoding="utf-8")


def _offsets_of(text: str, needle: str) -> list[int]:
    """Return every start offset of `needle` in `text`.

    Args:
        text: The text to scan.
        needle: The substring to locate.

    Returns:
        The offsets, in order.
    """
    offsets: list[int] = []
    start = text.find(needle)
    while start != -1:
        offsets.append(start)
        start = text.find(needle, start + 1)
    return offsets


def _project_version() -> str:
    """The version the release workflow will demand the tag match.

    Read rather than hardcoded: the runbook and the README have to name the
    version actually being shipped, and pinning the expected string in the
    test only moves the drift one file further away.
    """
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert match is not None, "pyproject.toml has no project version"
    return match.group(1)


def test_linux_bundle_pins_and_names_the_manylinux_2_28_baseline() -> None:
    workflow = _release_workflow()
    assert "manylinux_2_28_x86_64@sha256:" in workflow
    assert 'platform_tag="linux-manylinux_2_28-x86_64"' in workflow


def test_release_metadata_is_generated_in_the_build_job() -> None:
    jobs = workflow_jobs(_RELEASE_WORKFLOW)
    assert any("scripts/release/metadata.py" in script for script in run_scripts(jobs["build"]))
    assert not any("scripts/release/metadata.py" in script for script in run_scripts(jobs["sbom"]))


def test_release_build_toolchain_is_fully_pinned() -> None:
    workflow = _release_workflow()
    setup_uv_count = workflow.count(
        "uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9"
    )
    assert setup_uv_count > 0
    assert workflow.count('version: "0.10.9"') == setup_uv_count
    assert (
        "uv build --build-constraints scripts/release/build-constraints.txt --require-hashes"
    ) in workflow
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    build_requirements = pyproject["build-system"]["requires"]
    assert build_requirements
    requirement_specs = [
        requirement.partition(";")[0].strip() for requirement in build_requirements
    ]
    assert all(
        spec.count("==") == 1
        and not any(operator in spec.replace("==", "") for operator in "<>!~")
        and all(part.strip() for part in spec.split("=="))
        and "*" not in spec.split("==", 1)[1]
        for spec in requirement_specs
    )
    constraints_input = (
        Path(__file__).parents[1] / "scripts" / "release" / "build-constraints.in"
    ).read_text(encoding="utf-8")
    input_requirements = {
        line.strip()
        for line in constraints_input.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert set(build_requirements) <= input_requirements
    constraints = (
        Path(__file__).parents[1] / "scripts" / "release" / "build-constraints.txt"
    ).read_text(encoding="utf-8")
    constraint_blocks: dict[str, list[str]] = {}
    current_requirement = ""
    for line in constraints.splitlines():
        if line and not line[0].isspace():
            current_requirement = line.removesuffix("\\").strip()
            constraint_blocks[current_requirement] = []
        elif current_requirement:
            constraint_blocks[current_requirement].append(line.strip())
    assert input_requirements <= constraint_blocks.keys()
    assert all(
        any(part.startswith("--hash=sha256:") for part in parts)
        for parts in constraint_blocks.values()
    )


def test_release_audit_covers_every_shipped_extra() -> None:
    workflow = _release_workflow()
    assert (
        "uv export --frozen --all-extras --no-emit-project --no-dev -o requirements.txt"
    ) in workflow


def test_draft_release_is_staged_before_irreversible_pypi_publication() -> None:
    jobs = workflow_jobs(_RELEASE_WORKFLOW)
    assert set(jobs["stage-github-release"]["needs"]) == {"verify", "smoke", "attest"}
    assert set(jobs["publish-pypi"]["needs"]) == {"verify", "stage-github-release"}
    assert set(jobs["finalize-github-release"]["needs"]) == {"verify", "publish-pypi"}
    stage_scripts = "\n".join(run_scripts(jobs["stage-github-release"]))
    assert "gh release create" in stage_scripts
    assert "--draft" in stage_scripts
    assert "scripts/release/compare_assets.py" in stage_scripts
    publish_step = next(
        step
        for step in jobs["publish-pypi"]["steps"]
        if str(step.get("uses", "")).startswith("pypa/gh-action-pypi-publish")
    )
    assert publish_step.get("with", {}).get("skip-existing") is True
    assert "--draft=false" in "\n".join(run_scripts(jobs["finalize-github-release"]))


def test_release_docs_require_immutable_protected_tags() -> None:
    readme = _readme()
    assert "immutable `v*` tag ruleset" in readme
    assert "restrict tag creation" in readme
    assert "update and deletion" in readme
    assert "protected tags only" in readme


def _release_notes() -> str:
    path = Path(__file__).parents[1] / "docs" / "release-notes" / f"v{_project_version()}.md"
    assert path.is_file(), f"{path.name} is missing; the release stages notes from this file"
    return path.read_text(encoding="utf-8")


def test_release_stages_written_notes_rather_than_a_generated_commit_list() -> None:
    """`--generate-notes` lists merged pull requests.

    For a first public release that produces a page of "bump the runtime
    group with 3 updates" - accurate, and useless to someone deciding
    whether to install. Notes are written per version and the workflow
    fails if the file for the tag is missing, so the release cannot quietly
    fall back to the generated list.
    """
    workflow = _release_workflow()
    assert "--generate-notes \\" not in workflow, "the release still falls back to generated notes"
    assert '--notes-file "$NOTES"' in workflow
    assert 'NOTES="docs/release-notes/${TAG}.md"' in workflow
    assert 'if [ ! -f "$NOTES" ]' in workflow


def test_release_rewrites_the_notes_of_a_draft_it_resumes() -> None:
    """The recovery path validated assets and trusted the body.

    A draft can exist from an earlier run, or be created by hand before the
    tag is pushed. Resuming one only compared its files, so a body nobody
    reviewed - or a stale one from a previous attempt - would be published
    verbatim while the reviewed notes file sat unused. The notes are part of
    the release, so the resumed draft is rewritten from the file.
    """
    workflow = _release_workflow()
    assert 'gh release edit "$TAG" --repo "$REPO" \\' in workflow
    assert workflow.count('--notes-file "$NOTES"') == 2, (
        "both the create and the resume path must take the body from the notes file"
    )


def test_release_notes_state_the_security_posture_the_project_claims() -> None:
    """Every write goes through an approval gate and a fail-closed audit log.

    That is the whole argument for running an agent against a live cluster,
    so a release note that omits it is selling a different product than the
    one being shipped.
    """
    notes = " ".join(_release_notes().split())
    assert "approval" in notes
    assert "audit" in notes
    assert "read-only" in notes or "read only" in notes


def test_pypi_metadata_gives_the_project_page_its_sidebar_links() -> None:
    """PyPI builds its sidebar from `[project.urls]`, and korvid had none.

    A project page with no Source or Issues link asks the reader to guess
    where the code lives, on the one page where a stranger decides whether
    to trust the package.
    """
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    urls = pyproject["project"]["urls"]
    repo = "https://github.com/hellices/korvid"
    assert urls["Homepage"] == repo
    assert urls["Source"] == repo
    assert urls["Issues"] == f"{repo}/issues"
    assert urls["Release notes"] == f"{repo}/releases"
    assert urls["Documentation"] == f"{repo}/blob/main/docs/tui.md"
    assert urls["Security"] == f"{repo}/blob/main/SECURITY.md"


def test_every_sidebar_link_to_a_repository_file_points_at_a_real_file() -> None:
    """A sidebar link is only useful if it resolves.

    `Documentation` pointed at `docs/README.md`, which does not exist - a
    404 on the PyPI project page, and nothing in the repository would have
    noticed. Checking the exact strings is not enough; the targets that name
    a file in this repository are resolved against the working tree.
    """
    root = Path(__file__).parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    prefix = "https://github.com/hellices/korvid/blob/main/"
    for label, url in pyproject["project"]["urls"].items():
        if not url.startswith(prefix):
            continue
        target = root / url[len(prefix) :]
        assert target.is_file(), f"[project.urls] {label} points at a missing file: {url}"


def test_pypi_metadata_declares_audience_and_supported_pythons() -> None:
    """Trove classifiers are how PyPI search and filtering find a package.

    The Python versions are stated one by one on purpose: `requires-python`
    is what installers enforce, but the classifier list is what a human
    reads, and the CI matrix is the thing that actually proves them.
    """
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    classifiers = pyproject["project"]["classifiers"]
    assert "Environment :: Console :: Curses" in classifiers
    assert "Intended Audience :: System Administrators" in classifiers
    assert "Topic :: System :: Systems Administration" in classifiers
    assert "License :: OSI Approved :: Apache Software License" not in classifiers, (
        "PEP 639 forbids a License classifier alongside the SPDX `license` field"
    )
    for minor in ("11", "12", "13"):
        assert f"Programming Language :: Python :: 3.{minor}" in classifiers
    assert pyproject["project"]["keywords"]


def test_every_absolute_repository_link_resolves_to_a_real_path() -> None:
    """Absolute links do not fail loudly - they 404 for the reader.

    Rewriting the README's relative links for PyPI moved every one of them
    out of reach of any tooling that checks paths, so the targets are
    resolved back against the working tree here. The release notes get the
    same treatment: they are published to a page nobody can edit in place.
    """
    root = Path(__file__).parents[1]
    prefixes = (
        "https://github.com/hellices/korvid/blob/main/",
        "https://github.com/hellices/korvid/tree/main/",
        "https://raw.githubusercontent.com/hellices/korvid/main/",
    )
    documents = {"README.md": _readme(), "release notes": _release_notes()}
    broken: list[str] = []
    for name, text in documents.items():
        for url in re.findall(r"\]\(([^)]+)\)", text):
            for prefix in prefixes:
                if url.startswith(prefix):
                    target = root / url[len(prefix) :].split("#")[0]
                    if not target.exists():
                        broken.append(f"{name}: {url}")
    assert not broken, f"links to paths that do not exist: {broken}"


def test_readme_has_no_relative_links_because_pypi_cannot_follow_them() -> None:
    """The README is the PyPI project page (`readme = "README.md"`).

    PyPI renders it outside the repository, so a relative target such as
    `docs/mcp.md` becomes a dead link and `docs/assets/demo.gif` becomes a
    broken image - on the page that has to sell the project. Anchors are
    fine: they resolve inside the rendered document.
    """
    readme = _readme()
    targets = re.findall(r"\]\(([^)]+)\)", readme)
    relative = [t for t in targets if not t.startswith(("http://", "https://", "#"))]
    assert not relative, f"README links PyPI cannot resolve: {sorted(set(relative))}"


def test_readme_recommends_an_isolated_install_for_an_application() -> None:
    """Every active public path isolates this CLI from system Python.

    PEP 668 protects the system interpreter, so `--break-system-packages` is
    prohibited and installs must stay in isolated `uv tool` or `pipx`
    environments.
    """
    version = _project_version()
    readme = _readme()
    quick_start = markdown_section(readme, "Quick start")
    normalized_quick_start = " ".join(quick_start.split())
    pip_fallback = f"python -m pip install 'korvid[all]=={version}'"
    assert f"uv tool install 'korvid[all]=={version}'" in quick_start
    assert pip_fallback in quick_start
    assert "inside an activated virtual environment" in normalized_quick_start
    assert "including one created inside a container" in normalized_quick_start
    assert quick_start.index("uv tool install") < quick_start.index(pip_fallback)
    assert f"pipx install 'korvid[all]=={version}'" in quick_start
    install = readme[readme.index("## Installation") : readme.index("### Development")]
    assert f"uv tool install 'korvid[all]=={version}'" in install
    assert f"python -m pip install 'korvid[all]=={version}'        # recommended" not in install
    assert "activated virtual environment, including one created inside a container" in " ".join(
        readme.split()
    )
    assert "virtual environment or a container image you control" not in readme
    assert "uv tool uninstall korvid" in install
    assert "pipx uninstall korvid" in install
    assert "3.11" in readme


def test_readme_describes_pep_668_without_an_inaccurate_fedora_claim() -> None:
    readme = _readme()
    quick_start = readme[readme.index("## Quick start") :]
    assert "PEP 668" in quick_start
    assert "externally-managed-environment" in quick_start
    assert "Fedora 38" not in quick_start


def test_runtime_install_hint_consumers_use_the_shared_helper() -> None:
    root = Path(__file__).parents[1] / "src" / "korvid"
    for relative in ("__main__.py", "ui/app.py", "providers/entra.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "from korvid.agent.install_hint import isolated_install_hint" in source
        assert "isolated_install_hint(" in source


def test_release_smoke_docs_describe_a_ci_venv_pip_check() -> None:
    root = Path(__file__).parents[1]
    runbook = markdown_section(_release_runbook(), "What the smoke matrix proves")
    smoke = (root / "scripts" / "release" / "smoke_install.py").read_text(encoding="utf-8")
    assert "disposable CI virtual environment" in runbook
    assert "disposable CI virtual environment" in smoke
    assert "the documented base-to-extra expansion command" not in smoke
    assert "run the documented" not in runbook


def test_release_docs_runbook_requires_protected_tags_and_maintainer_approval() -> None:
    runbook = " ".join(_release_runbook().split())
    assert "allow protected tags only" in runbook
    assert "require approval from a designated release maintainer" in runbook


def test_release_docs_runbook_lists_and_cleans_the_os_keyring_credential() -> None:
    runbook = " ".join(_release_runbook().split())
    assert "OS keyring" in runbook
    assert "service `korvid`, account `github-oauth`" in runbook
    assert 'keyring.get_password("korvid", "github-oauth")' in runbook
    assert 'keyring.delete_password("korvid", "github-oauth")' in runbook
    assert "except PasswordDeleteError" not in runbook
    assert "before uninstalling korvid" in runbook


def test_release_readme_discloses_the_retained_os_keyring_credential() -> None:
    readme = " ".join(_readme().split())
    assert "OS keyring credential (`korvid` / `github-oauth`)" in readme
    assert (
        "cleanup is explicit and opt-in in the [release runbook]"
        "(https://github.com/hellices/korvid/blob/main/docs/release.md)"
    ) in readme


def test_release_docs_preserve_failed_tags_as_unpublished_audit_history() -> None:
    """Neither earlier tag published, and each stopped somewhere different.

    Collapsing them into "the earlier attempts failed" would lose the only
    thing that matters for the next attempt: `v0.1.1` got all the way to
    `publish-pypi` and was rejected for a missing trusted publisher, so the
    build path is proven and the registration is not.
    """
    runbook = " ".join(_release_runbook().split())
    assert "`v0.1.0` remains immutable, unpublished audit history" in runbook
    assert "before build, attestation, staging, PyPI publication, or GitHub Release" in runbook
    assert "`v0.1.1` is unpublished audit history" in runbook
    assert "stopped at `publish-pypi`" in runbook
    assert "no PyPI Trusted Publisher had" in runbook


def test_release_docs_runbook_gives_the_five_trusted_publisher_claims() -> None:
    """The registration is the one release step that cannot be automated, and
    every field is matched exactly against the OIDC token. A runbook that says
    "register a trusted publisher" without the values is why `v0.1.1` stopped."""
    runbook = " ".join(_release_runbook().split())
    assert "https://pypi.org/manage/project/korvid/settings/publishing/" in runbook
    assert "| PyPI Project Name | `korvid` |" in runbook
    assert "| Owner | `hellices` |" in runbook
    assert "| Repository name | `korvid` |" in runbook
    assert "| Workflow name | `release.yml` |" in runbook
    assert "| Environment name | `release` |" in runbook
    assert "Verify the active GitHub publisher" in runbook
    assert "Do not create a second project" in runbook
    assert "Two-factor authentication must be enabled" in runbook
    assert "No API token is created" in runbook


def test_security_policy_supports_only_the_current_minor_line() -> None:
    version = _project_version()
    policy = " ".join(_security_policy().split())
    major, minor, _patch = version.split(".")
    assert f"Until `v{version}` is published, that remains `0.1.2`" in policy
    assert f"latest `{major}.{minor}.x` version" in policy
    assert "After publication" in policy


def test_workflow_exports_source_commit_without_logging_it_from_python() -> None:
    workflow = _release_workflow()
    assert 'source_commit=$(git rev-list -n 1 "refs/tags/$TAG")' in workflow
    assert "source_commit=$(uv run" not in workflow


def test_release_workflow_has_a_main_only_non_publishing_manual_dry_run() -> None:
    workflow = _release_workflow()
    jobs = workflow_jobs(_RELEASE_WORKFLOW)
    assert "workflow_dispatch:" in workflow
    version_script = _verify_step_run("id", "version")
    source_script = _verify_step_run("id", "source")
    assert 'if [ "$REF" != "refs/heads/main" ]' in version_script
    assert "check_dry_run.py origin/main" in version_script
    assert "check_source.py" in source_script
    manual_start = version_script.index('elif [ "$EVENT_NAME" = "workflow_dispatch" ]')
    manual_end = version_script.index("\nelse\n", manual_start)
    assert "check_source.py" not in version_script[manual_start:manual_end]
    for job_name in ("stage-github-release", "publish-pypi", "finalize-github-release"):
        assert jobs[job_name]["if"] == "github.event_name == 'push'"


def test_release_workflow_smoke_matrix_covers_every_supported_runner_and_variant() -> None:
    smoke_job = workflow_jobs(_RELEASE_WORKFLOW)["smoke"]
    matrix = smoke_job["strategy"]["matrix"]
    assert smoke_job["strategy"]["fail-fast"] is False
    assert matrix["os"] == ["ubuntu-latest", "macos-latest", "windows-latest"]
    assert matrix["python-version"] == ["3.11", "3.12", "3.13"]
    assert matrix["variant"] == ["base", "agent", "mcp", "all"]
    assert smoke_job["runs-on"] == "${{ matrix.os }}"


def test_release_workflow_smokes_the_downloaded_wheel_once_without_rebuilding() -> None:
    smoke_job = workflow_jobs(_RELEASE_WORKFLOW)["smoke"]
    scripts = run_scripts(smoke_job)
    download_step = next(
        step
        for step in smoke_job["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact")
    )
    setup_python_step = next(
        step
        for step in smoke_job["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-python")
    )
    smoke_step = next(
        step
        for step in smoke_job["steps"]
        if "scripts/release/smoke_install.py" in str(step.get("run", ""))
    )
    assert sum("scripts/release/smoke_install.py" in script for script in scripts) == 1
    assert download_step.get("with", {}).get("name") == "dist"
    assert download_step.get("with", {}).get("path") == "dist"
    assert setup_python_step.get("with", {}).get("python-version") == "${{ matrix.python-version }}"
    assert "${{ runner.temp }}" in smoke_step.get("env", {}).get("WORKSPACE", "")
    assert "uv build" not in "\n".join(scripts)


# --- the dry-run source policy compares against the live remote -------------


def test_release_workflow_refreshes_live_source_refs_before_source_policy() -> None:
    verify_job = workflow_jobs(_RELEASE_WORKFLOW)["verify"]
    steps = verify_job["steps"]
    fetch_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Fetch the live trusted source refs"
    )
    version_index = next(i for i, step in enumerate(steps) if step.get("id") == "version")
    source_index = next(i for i, step in enumerate(steps) if step.get("id") == "source")
    fetch_step = steps[fetch_index]
    checkout_step = next(
        step for step in steps if str(step.get("uses", "")).startswith("actions/checkout")
    )
    fetch_script = str(fetch_step["run"])

    assert fetch_index < version_index < source_index
    assert fetch_script.index('"refs/heads/main:refs/remotes/origin/main"') < fetch_script.index(
        'git fetch --force origin "+refs/tags/$TAG:refs/tags/$TAG"'
    )
    assert (
        'if [ "$EVENT_NAME" = "push" ]; then\n'
        '  git fetch --force origin "+refs/tags/$TAG:refs/tags/$TAG"\n'
        "fi"
    ) in fetch_script
    assert "check_dry_run.py origin/main" in _verify_step_run("id", "version")
    assert "check_source.py" in _verify_step_run("id", "source")
    assert fetch_step.get("env", {}).get("TAG") == "${{ github.ref_name }}"
    assert checkout_step.get("with", {}).get("fetch-depth") == 0


def test_release_workflow_restores_overwritten_annotated_tag_before_source_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout, tag = _overwritten_annotated_tag_checkout(tmp_path)
    restore = subprocess.run(
        [
            _bash_executable(),
            "-eu",
            "-o",
            "pipefail",
            "-c",
            _verify_step_run("name", "Fetch the live trusted source refs"),
        ],
        cwd=checkout,
        env={**os.environ, "EVENT_NAME": "push", "TAG": tag},
        capture_output=True,
        text=True,
    )

    assert restore.returncode == 0, restore.stderr
    assert _git(checkout, "cat-file", "-t", f"refs/tags/{tag}") == "tag"
    assert check_source.main([tag, "origin/main", str(checkout)]) == 0
    assert "release source verified" in capsys.readouterr().out


def test_release_workflow_rejects_restored_tag_that_differs_from_event_commit(
    tmp_path: Path,
) -> None:
    checkout, tag = _overwritten_annotated_tag_checkout(tmp_path)
    restore = subprocess.run(
        [
            _bash_executable(),
            "-eu",
            "-o",
            "pipefail",
            "-c",
            _verify_step_run("name", "Fetch the live trusted source refs"),
        ],
        cwd=checkout,
        env={**os.environ, "EVENT_NAME": "push", "TAG": tag},
        capture_output=True,
        text=True,
    )
    assert restore.returncode == 0, restore.stderr

    source_check = subprocess.run(
        [
            _bash_executable(),
            "-eu",
            "-o",
            "pipefail",
            "-c",
            _verify_step_run("id", "source"),
        ],
        cwd=checkout,
        env={
            **os.environ,
            "EVENT_NAME": "push",
            "GITHUB_SHA": "0" * 40,
            "TAG": tag,
        },
        capture_output=True,
        text=True,
    )

    assert source_check.returncode == 1
    assert source_check.stdout.strip() == "release tag does not match event commit"


def test_release_workflow_binds_restored_tag_to_event_commit_before_validation() -> None:
    source_script = _verify_step_run("id", "source")
    assert (
        'source_commit=$(git rev-list -n 1 "refs/tags/$TAG")\n'
        '  if [ "$source_commit" != "$GITHUB_SHA" ]; then\n'
        '    echo "release tag does not match event commit"\n'
        "    exit 1\n"
        "  fi\n"
        "  uv run --no-project python scripts/release/check_source.py"
    ) in source_script


# --- provenance attestation is irreversible, so tag pushes only -------------


def test_attestation_is_gated_to_tag_pushes_and_never_runs_on_a_dry_run() -> None:
    jobs = workflow_jobs(_RELEASE_WORKFLOW)
    assert "if" not in jobs["collect"]
    assert set(jobs["attest"]["needs"]) == {"verify", "collect", "smoke"}
    assert jobs["attest"]["if"] == "github.event_name == 'push'"
    assert "attest" in set(jobs["stage-github-release"]["needs"])
    attest_step = next(
        step
        for step in jobs["attest"]["steps"]
        if str(step.get("uses", "")).startswith("actions/attest-build-provenance")
    )
    assert attest_step.get("with", {}).get("subject-path") == "release-files/*"


def test_attestation_revalidates_live_tag_and_main_immediately_before_signing() -> None:
    attest_job = workflow_jobs(_RELEASE_WORKFLOW)["attest"]
    steps = attest_job["steps"]
    revalidate_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Revalidate the remote tag immediately before attestation"
    )
    sign_index = next(
        i
        for i, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/attest-build-provenance")
    )
    checkout_step = next(
        step for step in steps if str(step.get("uses", "")).startswith("actions/checkout")
    )
    revalidate_step = steps[revalidate_index]
    revalidate_script = str(revalidate_step["run"])
    fetch = revalidate_script.index("git fetch --force origin")
    check = revalidate_script.index('python scripts/release/check_source.py "$TAG" origin/main')

    assert fetch < check
    assert revalidate_index < sign_index
    assert checkout_step.get("with", {}).get("fetch-depth") == 0
    assert revalidate_step.get("env", {}).get("TAG") == "${{ github.ref_name }}"
    assert (
        revalidate_step.get("env", {}).get("EXPECTED_COMMIT")
        == "${{ needs.verify.outputs.source_commit }}"
    )


# --- no GitHub expressions inside shell bodies ------------------------------


def _workflow_run_bodies() -> list[tuple[str, str]]:
    return [
        (job_name, script)
        for job_name, job in workflow_jobs(_RELEASE_WORKFLOW).items()
        for script in run_scripts(job)
    ]


def test_release_workflow_keeps_github_expressions_out_of_shell_bodies() -> None:
    offenders = [job for job, body in _workflow_run_bodies() if "${{" in body]
    assert offenders == []


def test_release_workflow_smoke_step_passes_matrix_values_through_env() -> None:
    smoke_job = workflow_jobs(_RELEASE_WORKFLOW)["smoke"]
    smoke_step = next(
        step
        for step in smoke_job["steps"]
        if step.get("name") == "Smoke-test the downloaded wheel in a clean workspace"
    )
    assert smoke_step.get("env", {}).get("VERSION") == "${{ needs.verify.outputs.version }}"
    assert smoke_step.get("env", {}).get("VARIANT") == "${{ matrix.variant }}"
    assert smoke_step.get("shell") == "bash"


# --- the 36-cell smoke matrix must not hang a release -----------------------


def test_release_workflow_smoke_job_declares_a_timeout() -> None:
    document = yaml.safe_load(_release_workflow())
    timeout = document["jobs"]["smoke"]["timeout-minutes"]
    assert isinstance(timeout, int)
    assert 0 < timeout <= 60


# --- the packaged version is the version the package reports ----------------


def test_pyproject_version_matches_the_package_version() -> None:
    import korvid

    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["version"] == korvid.__version__


# --- release documentation invariants ---------------------------------------


def test_release_docs_call_provenance_attestation_irreversible() -> None:
    runbook = _release_runbook()
    assert "Sigstore" in runbook
    assert "Rekor" in runbook
    assert "attestation is irreversible" in runbook


def test_release_docs_state_the_dry_run_skips_attestation_and_publication() -> None:
    runbook = _release_runbook()
    assert "does not exercise attestation" in runbook
    assert "stage-github-release" in runbook
    assert "compare-assets recovery" in runbook
    assert "tag revalidation" in runbook
    assert "reduces but does not eliminate" in runbook


def test_release_docs_show_how_to_find_the_run_id_and_the_dispatch_precondition() -> None:
    runbook = _release_runbook()
    assert "gh run list --workflow Release --limit 1" in runbook
    assert "default branch" in runbook


def test_release_docs_correct_the_xdg_config_claim() -> None:
    runbook = _release_runbook()
    assert "`XDG_CONFIG_HOME` is not honored" in runbook
    assert "always under `~/.config/korvid`" in runbook


def test_release_docs_keep_a_source_install_fallback_for_unreleased_main() -> None:
    runbook = _release_runbook()
    readme = _readme()
    source_install = "uv tool install 'korvid[all] @ git+https://github.com/hellices/korvid'"
    pipx_source_install = "pipx install 'korvid[all] @ git+https://github.com/hellices/korvid'"
    assert source_install in runbook
    runbook_install = runbook[
        runbook.index("## Install, reinstall, and uninstall from PyPI") : runbook.index(
            "## What the smoke matrix proves"
        )
    ]
    assert pipx_source_install in runbook_install
    assert "python -m pip install" not in runbook_install
    assert source_install in readme
    assert "Tagged versions should be installed from PyPI" in readme
    assert "appearing on PyPI" not in runbook
    assert "For unreleased `main` development" in runbook
    quick_start = readme[readme.index("## Quick start") : readme.index("## Features")]
    assert "Until `0.2.0` is published on PyPI" not in quick_start
    assert "For unreleased `main` development" in quick_start
    assert "uv tool install 'korvid[all] @ git+https://github.com/hellices/korvid'" in quick_start


def test_release_docs_describe_fresh_installs_and_extra_expansion_separately() -> None:
    runbook = _release_runbook()
    assert "fresh install of each variant" in runbook
    assert "separate base-to-extra expansion check" in runbook


def test_release_docs_hand_the_tap_merge_to_the_maintainer() -> None:
    version = _project_version()
    runbook = _release_runbook()
    normalized = " ".join(runbook.split())
    assert "HOMEBREW_TAP_TOKEN" in runbook
    assert f"gh release download v{version} --pattern korvid.rb" in runbook
    assert 'formula_path="$PWD/dist/v0.2.0/korvid.rb"' in runbook
    assert 'if [ ! -f "$formula_path" ]' in runbook
    assert 'cp "$formula_path" Formula/korvid.rb' in runbook
    assert 'if cmp -s "$formula_path" Formula/korvid.rb' in runbook
    assert "formula is already present on tap main" in runbook
    assert (
        runbook.count('gh pr checks "$TAP_PR" --repo hellices/homebrew-korvid --watch || exit 1')
        == 2
    )
    # The formula every `brew install korvid` resolves is not merged by a
    # script. Both paths stop at green and hand the merge back by name.
    assert "gh pr merge" not in runbook
    assert runbook.count("now merge PR #$TAP_PR yourself") == 2
    # Claiming "reviewed" without showing the diff is the runbook lying to the
    # maintainer it is handing the merge to. Both paths must show it, and show
    # it *before* they make the claim - asserting it merely appears once let
    # the manual path go without.
    diff_cmd = 'gh pr diff "$TAP_PR" --repo hellices/homebrew-korvid'
    assert runbook.count(diff_cmd) == 2
    for claim in _offsets_of(runbook, "reviewed and green"):
        preceding = runbook.rfind(diff_cmd, 0, claim)
        assert preceding != -1, "a path claims review without showing the diff"
        assert "reviewed and green" not in runbook[preceding:claim]
    assert "--json number,title,baseRefName,headRefName,headRepositoryOwner" in runbook
    assert '.baseRefName == "main"' in runbook
    assert '.headRefName == "bump-korvid-0.2.0"' in runbook
    assert '.headRepositoryOwner.login == "hellices"' in runbook
    assert "trusted bump-korvid-0.2.0 tap PR not found" in runbook
    assert "branch=bump-korvid-" in runbook
    assert 'git show-ref --verify --quiet "refs/remotes/origin/$branch"' in runbook
    assert 'git switch --track -c "$branch" "origin/$branch"' in runbook
    assert "git diff --cached --quiet" in runbook
    assert "TAP_PR_URL=$(gh pr create" in runbook
    assert "could not identify created tap PR" in runbook
    assert f"korvid --version | grep -Fx 'korvid {version}'" in runbook
    assert "tag-revalidated `uv.lock`" in normalized
    assert "not separately attested or listed in `SHA256SUMS`" in normalized
    assert "attested release asset" not in runbook
    verify = runbook[runbook.index("Finally verify the tap") : runbook.index("## Install")]
    assert "```sh\nset -eu" in verify


# --- metadata ---------------------------------------------------------------


def test_metadata_records_source_and_lockfile_digest(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text("locked")
    out = tmp_path / "release-metadata.json"
    pyproject = _pyproject(tmp_path, "1.2.3")
    assert (
        metadata.main(
            [
                "--pyproject",
                str(pyproject),
                "--lockfile",
                str(lock),
                "--output",
                str(out),
                "--commit",
                "abc123",
            ]
        )
        == 0
    )
    data = json.loads(out.read_text())
    assert data["version"] == "1.2.3"
    assert data["commit"] == "abc123"
    assert data["lockfile_sha256"] == hashlib.sha256(b"locked").hexdigest()
    assert data["python"].startswith("3.")
    assert data["platform"]


# --- the negative offline check is honest ------------------------------------


def test_offline_verify_cli_rejects_a_missing_bundle(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "offline_verify.py"), "--bundle", str(tmp_path / "nope")],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "nope" in result.stderr
