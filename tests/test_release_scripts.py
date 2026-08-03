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
import check_version  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import metadata  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path
import offline_verify  # type: ignore[import-not-found]  # noqa: E402  # scripts/release via sys.path


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
