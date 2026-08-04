"""Release tooling (issue #169): version gate, bundle assembly, checksums,
offline verification helpers, and release metadata — the logic-bearing parts
of the release workflow, unit-tested so the YAML stays thin."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts" / "release"
sys.path.insert(0, str(SCRIPTS))

import bundle  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import check_artifacts  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import check_sbom  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import check_source  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import check_version  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import metadata  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import offline_verify  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import release_manifest  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path


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


def test_annotated_tag_reachable_from_trusted_branch_passes(tmp_path: Path) -> None:
    repo = _release_repo(tmp_path)
    _git(repo, "tag", "-a", "v1.2.3", "-m", "release 1.2.3")
    assert check_source.main(["v1.2.3", "main", str(repo)]) == 0


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


def _metadata_text(*, version: str = "1.2.3", include_entra: bool = True) -> str:
    entra = (
        'Provides-Extra: entra\nRequires-Dist: azure-identity>=1.19; extra == "entra"\n'
        if include_entra
        else ""
    )
    return (
        "Metadata-Version: 2.4\n"
        "Name: korvid\n"
        f"Version: {version}\n"
        "Provides-Extra: agent\n"
        "Provides-Extra: mcp\n"
        f"{entra}"
        "Provides-Extra: all\n"
        'Requires-Dist: httpx>=0.27; extra == "agent"\n'
        'Requires-Dist: mcp<2,>=1.10; extra == "mcp"\n'
        'Requires-Dist: korvid[agent,mcp]; extra == "all"\n'
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


# --- SBOM completeness ------------------------------------------------------


def _sbom(path: Path, *, include_korvid: bool = True) -> Path:
    payload = {
        "metadata": {"component": {"name": "korvid" if include_korvid else "dependencies"}},
        "components": [{"name": "httpx"}, {"name": "textual"}],
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
