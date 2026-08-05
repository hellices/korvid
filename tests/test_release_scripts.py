"""Release tooling (issue #169): version gate, bundle assembly, checksums,
offline verification helpers, and release metadata — the logic-bearing parts
of the release workflow, unit-tested so the YAML stays thin."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml

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
    """`mcp` pulls httpx transitively, so httpx is only forbidden where no
    selected extra provides it."""
    assert smoke_install.forbidden_modules("base") == {"httpx", "keyring", "mcp"}
    assert smoke_install.forbidden_modules("agent") == {"mcp"}
    assert smoke_install.forbidden_modules("mcp") == {"keyring"}
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
        launcher_dir = env_dir / ("Scripts" if smoke_install.os.name == "nt" else "bin")
        launcher_dir.mkdir(parents=True)
        for name in ("python", "korvid"):
            binary = launcher_dir / (f"{name}.exe" if smoke_install.os.name == "nt" else name)
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)

    def _fake_run(
        args: list[str], *, env: dict[str, str], cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if "uninstall" in args:
            launcher = smoke_install._resolve_launcher(Path(args[0]).parent.parent)
            if launcher is not None:
                launcher.unlink()
        stdout = "usage: korvid" if args[1:] == ["--help"] else "korvid 1.2.3"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

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
) -> str:
    entra = (
        'Provides-Extra: entra\nRequires-Dist: azure-identity>=1.19; extra == "entra"\n'
        if include_entra
        else ""
    )
    keyring = 'Requires-Dist: keyring>=25.7.0; extra == "agent"\n' if include_keyring else ""
    return (
        "Metadata-Version: 2.4\n"
        "Name: korvid\n"
        f"Version: {version}\n"
        "Provides-Extra: agent\n"
        "Provides-Extra: mcp\n"
        f"{entra}"
        "Provides-Extra: all\n"
        'Requires-Dist: httpx>=0.27; extra == "agent"\n'
        f"{keyring}"
        'Requires-Dist: mcp<2,>=1.10; extra == "mcp"\n'
        'Requires-Dist: anyio>=4.5; extra == "mcp"\n'
        'Requires-Dist: starlette>=0.36; extra == "mcp"\n'
        'Requires-Dist: uvicorn>=0.30; extra == "mcp"\n'
        'Requires-Dist: httpx>=0.27; extra == "all"\n'
        'Requires-Dist: keyring>=25.7.0; extra == "all"\n'
        'Requires-Dist: mcp<2,>=1.10; extra == "all"\n'
        'Requires-Dist: anyio>=4.5; extra == "all"\n'
        'Requires-Dist: starlette>=0.36; extra == "all"\n'
        'Requires-Dist: uvicorn>=0.30; extra == "all"\n'
        "\n"
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


def _release_workflow() -> str:
    return (Path(__file__).parents[1] / ".github" / "workflows" / "release.yml").read_text()


def _readme() -> str:
    return (Path(__file__).parents[1] / "README.md").read_text()


def _release_runbook() -> str:
    path = Path(__file__).parents[1] / "docs" / "release.md"
    assert path.is_file(), "docs/release.md is missing"
    return path.read_text()


def test_linux_bundle_pins_and_names_the_manylinux_2_28_baseline() -> None:
    workflow = _release_workflow()
    assert "manylinux_2_28_x86_64@sha256:" in workflow
    assert 'platform_tag="linux-manylinux_2_28-x86_64"' in workflow


def test_release_metadata_is_generated_in_the_build_job() -> None:
    workflow = _release_workflow()
    build = workflow.index("\n  build:")
    smoke = workflow.index("\n  smoke:")
    sbom = workflow.index("\n  sbom:")
    offline = workflow.index("\n  offline:")
    assert "scripts/release/metadata.py" in workflow[build:smoke]
    assert "scripts/release/metadata.py" not in workflow[sbom:offline]


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
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert '"hatchling==1.27.0"' in pyproject
    constraints = (
        Path(__file__).parents[1] / "scripts" / "release" / "build-constraints.txt"
    ).read_text()
    assert "hatchling==1.27.0" in constraints
    assert "--hash=sha256:" in constraints


def test_release_audit_covers_every_shipped_extra() -> None:
    workflow = _release_workflow()
    assert (
        "uv export --frozen --all-extras --no-emit-project --no-dev -o requirements.txt"
    ) in workflow


def test_draft_release_is_staged_before_irreversible_pypi_publication() -> None:
    workflow = _release_workflow()
    stage = workflow.index("\n  stage-github-release:")
    publish = workflow.index("\n  publish-pypi:")
    finalize = workflow.index("\n  finalize-github-release:")
    assert stage < publish < finalize
    assert "gh release create" in workflow[stage:publish]
    assert "--draft" in workflow[stage:publish]
    assert "scripts/release/compare_assets.py" in workflow[stage:publish]
    assert "skip-existing: true" in workflow[publish:finalize]
    assert "--draft=false" in workflow[finalize:]


def test_release_docs_require_immutable_protected_tags() -> None:
    readme = _readme()
    assert "immutable `v*` tag ruleset" in readme
    assert "restrict tag creation" in readme
    assert "update and deletion" in readme
    assert "protected tags only" in readme


def test_release_docs_readme_pins_first_release_install_and_links_the_runbook() -> None:
    readme = _readme()
    assert "python -m pip install 'korvid[all]==0.1.0'" in readme
    assert "docs/release.md" in readme


def test_release_docs_runbook_names_bindings_commands_and_irreversible_steps() -> None:
    runbook = _release_runbook()
    assert "refs/tags/v*" in runbook
    assert "`release`" in runbook
    assert "`.github/workflows/release.yml`" in runbook
    assert "`hellices/korvid`" in runbook
    assert "gh workflow run Release --ref main" in runbook
    assert 'gh run watch "$RUN_ID" --exit-status' in runbook
    assert 'git tag -a v0.1.0 COMMIT -m "korvid v0.1.0"' in runbook
    assert "git push origin refs/tags/v0.1.0" in runbook
    assert "gh release download v0.1.0 --dir dist/v0.1.0" in runbook
    assert (
        "gh attestation verify dist/v0.1.0/korvid-0.1.0-py3-none-any.whl --repo hellices/korvid"
    ) in runbook
    assert ("gh attestation verify dist/v0.1.0/SHA256SUMS --repo hellices/korvid") in runbook
    assert ("cd dist/v0.1.0 && shasum --algorithm 256 --check SHA256SUMS") in runbook
    assert "PyPI publication is irreversible" in runbook
    assert "annotated tag publication is irreversible" in runbook


def test_release_docs_runbook_requires_protected_tags_and_maintainer_approval() -> None:
    runbook = " ".join(_release_runbook().split())
    assert "allow protected tags only" in runbook
    assert "require approval from a designated release maintainer" in runbook


def test_release_docs_runbook_lists_retained_user_data_and_opt_in_cleanup() -> None:
    runbook = _release_runbook()
    stop_processes = runbook.index("Stop all korvid processes")
    remove_files = runbook.index("Then remove the retained files")
    assert "~/.config/korvid/config.yaml" in runbook
    assert "~/.config/korvid/credentials.json" in runbook
    assert "~/.local/state/korvid/audit.jsonl" in runbook
    assert "~/.local/state/korvid/audit.jsonl.lock" in runbook
    assert "~/.local/share/korvid/logs" in runbook
    assert "~/.local/share/korvid/agent-payloads" in runbook
    assert "python -m pip install 'korvid[all]==0.1.0'" in runbook
    assert "python -m pip uninstall -y korvid" in runbook
    assert "opt-in cleanup" in runbook
    assert "rerun your package manager with the full desired extra set" in runbook
    assert 'state_root="${XDG_STATE_HOME:-$HOME/.local/state}/korvid"' in runbook
    assert 'data_root="${XDG_DATA_HOME:-$HOME/.local/share}/korvid"' in runbook
    assert 'rm -f "$state_root/audit.jsonl"' in runbook
    assert 'rm -rf "$data_root/logs" "$data_root/agent-payloads"' in runbook
    assert stop_processes < remove_files


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
    assert "cleanup is explicit and opt-in in the [release runbook](docs/release.md)" in readme


def test_release_docs_runbook_marks_recovery_boundaries_and_first_release_upgrade_limit() -> None:
    runbook = _release_runbook()
    assert "Deleting or moving a published tag/version is not rollback" in runbook
    assert "resume the idempotent workflow only when the staged assets match" in runbook
    assert "stop and diagnose" in runbook
    assert "v0.1.0 cannot prove a cross-version PyPI upgrade" in runbook


def test_workflow_exports_source_commit_without_logging_it_from_python() -> None:
    workflow = _release_workflow()
    assert 'source_commit=$(git rev-list -n 1 "refs/tags/$TAG")' in workflow
    assert "source_commit=$(uv run" not in workflow


def test_release_workflow_has_a_main_only_non_publishing_manual_dry_run() -> None:
    workflow = _release_workflow()
    verify = workflow.index("\n  verify:")
    build = workflow.index("\n  build:")
    stage = workflow.index("\n  stage-github-release:")
    publish = workflow.index("\n  publish-pypi:")
    finalize = workflow.index("\n  finalize-github-release:")
    assert "workflow_dispatch:" in workflow
    assert "scripts/release/check_dry_run.py" in workflow
    assert "refs/heads/main" in workflow[verify:build]
    assert "check_dry_run.py origin/main" in workflow[verify:build]
    assert "check_source.py" in workflow[verify:build]
    manual_start = workflow.index('elif [ "$EVENT_NAME" = "workflow_dispatch" ]', verify, build)
    manual_end = workflow.index("\n          else", manual_start, build)
    assert "check_source.py" not in workflow[manual_start:manual_end]
    assert "github.event_name == 'push'" in workflow[stage:publish]
    assert "github.event_name == 'push'" in workflow[publish:finalize]
    assert "github.event_name == 'push'" in workflow[finalize:]


def test_release_workflow_smoke_matrix_covers_every_supported_runner_and_variant() -> None:
    workflow = _release_workflow()
    smoke = workflow.index("\n  smoke:")
    sbom = workflow.index("\n  sbom:")
    smoke_job = workflow[smoke:sbom]
    assert "fail-fast: false" in smoke_job
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in smoke_job
    assert 'python-version: ["3.11", "3.12", "3.13"]' in smoke_job
    assert "variant: [base, agent, mcp, all]" in smoke_job
    assert "runs-on: ${{ matrix.os }}" in smoke_job


def test_release_workflow_smokes_the_downloaded_wheel_once_without_rebuilding() -> None:
    workflow = _release_workflow()
    smoke = workflow.index("\n  smoke:")
    sbom = workflow.index("\n  sbom:")
    smoke_job = workflow[smoke:sbom]
    assert smoke_job.count("scripts/release/smoke_install.py") == 1
    assert "name: dist" in smoke_job
    assert "path: dist" in smoke_job
    assert "python-version: ${{ matrix.python-version }}" in smoke_job
    assert "${{ runner.temp }}" in smoke_job
    assert "uv build" not in smoke_job


# --- the dry-run source policy compares against the live remote -------------


def test_release_workflow_fetches_the_live_trusted_branch_before_the_source_policy() -> None:
    """`actions/checkout` can leave `origin/main` at the dispatched SHA, which
    would make the dry-run HEAD comparison vacuous."""
    workflow = _release_workflow()
    verify = workflow.index("\n  verify:")
    build = workflow.index("\n  build:")
    verify_job = workflow[verify:build]
    fetch = verify_job.index(
        'git fetch --force --no-tags origin "refs/heads/main:refs/remotes/origin/main"'
    )
    assert fetch < verify_job.index("check_dry_run.py origin/main")
    assert fetch < verify_job.index("check_source.py")
    assert "fetch-depth: 0" in verify_job


# --- provenance attestation is irreversible, so tag pushes only -------------


def test_attestation_is_gated_to_tag_pushes_and_never_runs_on_a_dry_run() -> None:
    workflow = _release_workflow()
    document = yaml.safe_load(workflow)
    collect = workflow.index("\n  collect:")
    attest = workflow.index("\n  attest:")
    stage = workflow.index("\n  stage-github-release:")
    assert collect < attest < stage
    assert "github.event_name == 'push'" not in workflow[collect:attest]
    assert document["jobs"]["attest"]["needs"] == ["verify", "collect", "smoke"]
    assert "if: github.event_name == 'push'" in workflow[attest:stage]
    assert "actions/attest-build-provenance" in workflow[attest:stage]


def test_attestation_revalidates_live_tag_and_main_immediately_before_signing() -> None:
    workflow = _release_workflow()
    attest = workflow.index("\n  attest:")
    stage = workflow.index("\n  stage-github-release:")
    attest_job = workflow[attest:stage]
    fetch = attest_job.index(
        "git fetch --force origin \\\n"
        '            "+refs/tags/$TAG:refs/tags/$TAG" \\\n'
        '            "refs/heads/main:refs/remotes/origin/main"'
    )
    check = attest_job.index(
        'python scripts/release/check_source.py "$TAG" origin/main \\\n'
        '            --expected-commit "$EXPECTED_COMMIT"'
    )
    sign = attest_job.index("actions/attest-build-provenance")
    assert fetch < check < sign
    assert "fetch-depth: 0" in attest_job
    assert "TAG: ${{ github.ref_name }}" in attest_job
    assert "EXPECTED_COMMIT: ${{ needs.verify.outputs.source_commit }}" in attest_job


# --- no GitHub expressions inside shell bodies ------------------------------


def _workflow_run_bodies() -> list[tuple[str, str]]:
    document = yaml.safe_load(_release_workflow())
    bodies: list[tuple[str, str]] = []
    for job_name, job in document["jobs"].items():
        for step in job.get("steps", []):
            if "run" in step:
                bodies.append((job_name, step["run"]))
    return bodies


def test_release_workflow_keeps_github_expressions_out_of_shell_bodies() -> None:
    offenders = [job for job, body in _workflow_run_bodies() if "${{" in body]
    assert offenders == []


def test_release_workflow_smoke_step_passes_matrix_values_through_env() -> None:
    workflow = _release_workflow()
    smoke = workflow.index("\n  smoke:")
    sbom = workflow.index("\n  sbom:")
    smoke_job = workflow[smoke:sbom]
    assert "VERSION: ${{ needs.verify.outputs.version }}" in smoke_job
    assert "VARIANT: ${{ matrix.variant }}" in smoke_job
    assert "shell: bash" in smoke_job


# --- the 36-cell smoke matrix must not hang a release -----------------------


def test_release_workflow_smoke_job_declares_a_timeout() -> None:
    document = yaml.safe_load(_release_workflow())
    timeout = document["jobs"]["smoke"]["timeout-minutes"]
    assert isinstance(timeout, int)
    assert 0 < timeout <= 60


# --- the packaged version is the version the package reports ----------------


def test_pyproject_version_matches_the_package_version() -> None:
    import korvid

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
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


def test_release_docs_list_and_clean_the_mcp_endpoint_state() -> None:
    runbook = _release_runbook()
    assert "~/.local/state/korvid/mcp-endpoint.json" in runbook
    assert "~/.local/state/korvid/mcp-endpoint.json.lock" in runbook
    cleanup = runbook.index("## opt-in cleanup")
    assert "mcp-endpoint.json" in runbook[cleanup:]
    assert "~/.config/korvid/credentials.json" in runbook[cleanup:]


def test_release_docs_keep_a_source_install_fallback_before_publication() -> None:
    runbook = _release_runbook()
    readme = _readme()
    source_install = "pip install 'korvid[all] @ git+https://github.com/hellices/korvid'"
    assert source_install in runbook
    assert source_install in readme
    assert "PyPI is the release path" in readme


def test_release_docs_describe_fresh_installs_and_extra_expansion_separately() -> None:
    runbook = _release_runbook()
    assert "fresh install of each variant" in runbook
    assert "separate base-to-extra expansion check" in runbook


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
