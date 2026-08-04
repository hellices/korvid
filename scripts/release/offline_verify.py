#!/usr/bin/env python3
"""Verify an offline bundle without any package index (issue #169).

Steps, all fail-closed:

1. `SHA256SUMS` covers and matches every listed file.
2. A clean venv installs `korvid[all,entra]==X.Y.Z` with `--no-index
   --find-links wheels`, then imports korvid and runs `korvid --help`.
3. The negative check: with one dependency wheel removed, a fresh venv
   install must FAIL — proving the positive install did not silently
   reach the network or a runner cache.

Stdlib only — this runs on bare GitHub runners.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path


def extract_bundle(archive: Path, destination: Path) -> Path:
    """Unpack the published archive and return its single bundle root."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    if archive.suffix == ".zip":
        base = destination.resolve()
        with zipfile.ZipFile(archive) as zipped:
            for name in zipped.namelist():
                target = (destination / name).resolve()
                if target != base and base not in target.parents:
                    raise ValueError(f"{archive.name}: unsafe archive member {name!r}")
            zipped.extractall(destination)
    else:
        # Python 3.12+ warns when no extraction policy is explicit; the
        # data filter also rejects traversal, devices, and unsafe links.
        with tarfile.open(archive) as tar:
            tar.extractall(destination, filter="data")
    roots = [path for path in destination.iterdir() if path.is_dir()]
    loose_files = [path for path in destination.iterdir() if path.is_file()]
    if len(roots) != 1 or loose_files:
        raise ValueError(f"{archive.name}: expected exactly one top-level bundle directory")
    return roots[0]


def verify_sha256sums(root: Path) -> None:
    """Validate every entry of root/SHA256SUMS; raise ValueError on any
    missing or tampered file."""
    for line in (root / "SHA256SUMS").read_text().splitlines():
        if not line.strip():
            continue
        expected, _, name = line.partition("  ")
        path = root / name
        if not path.is_file():
            raise ValueError(f"SHA256SUMS entry missing from bundle: {name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"checksum mismatch for {name}")


def pick_removable_wheel(wheels: Path) -> Path:
    """A dependency wheel whose removal must break the offline install."""
    for wheel in sorted(wheels.glob("*.whl")):
        if not wheel.name.startswith("korvid-"):
            return wheel
    raise ValueError(f"no dependency wheel found in {wheels} to remove")


def _venv_python(env_dir: Path) -> Path:
    exe = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
    return exe / ("python.exe" if sys.platform == "win32" else "python")


def _venv_launcher(env_dir: Path, *, platform_name: str | None = None) -> Path:
    """The installed `korvid` console launcher for the target platform."""
    platform_name = platform_name or sys.platform
    exe = env_dir / ("Scripts" if platform_name == "win32" else "bin")
    return exe / ("korvid.exe" if platform_name == "win32" else "korvid")


def _pip_install(python: Path, wheels: Path, spec: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-cache-dir",
            "--find-links",
            str(wheels),
            spec,
        ],
        capture_output=True,
        text=True,
    )


def _run(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"command failed: {args}\n{result.stdout}\n{result.stderr}")


def _verify_bundle(root: Path, version: str | None) -> None:
    wheels = root / "wheels"
    spec = f"korvid[all,entra]=={version}" if version else "korvid[all,entra]"

    print("1/3 verifying SHA256SUMS…")
    verify_sha256sums(root)

    print("2/3 offline install into a clean venv…")
    with tempfile.TemporaryDirectory() as tmp:
        env_dir = Path(tmp) / "venv"
        venv.create(env_dir, with_pip=True)
        python = _venv_python(env_dir)
        result = _pip_install(python, wheels, spec)
        if result.returncode != 0:
            raise SystemExit(f"offline install failed:\n{result.stdout}\n{result.stderr}")
        _run([str(python), "-c", "import korvid"])
        # Exercise the wheel's entry-point metadata and platform launcher,
        # not merely the importable module (issue #169 review on #182).
        _run([str(_venv_launcher(env_dir)), "--help"])

    print("3/3 negative check: one dependency wheel removed must fail…")
    victim = pick_removable_wheel(wheels)
    with tempfile.TemporaryDirectory() as tmp:
        crippled = Path(tmp) / "wheels"
        shutil.copytree(wheels, crippled)
        (crippled / victim.name).unlink()
        env_dir = Path(tmp) / "venv"
        venv.create(env_dir, with_pip=True)
        result = _pip_install(_venv_python(env_dir), crippled, spec)
        if result.returncode == 0:
            raise SystemExit(
                f"install succeeded without {victim.name} — the offline check is"
                " reaching a network index or cache"
            )
    print("offline bundle verified")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle", help="unpacked bundle directory")
    source.add_argument("--archive", help="published .tar.gz or .zip archive")
    parser.add_argument("--version", help="expected korvid version (from the tag)")
    args = parser.parse_args(argv)

    if args.bundle:
        root = Path(args.bundle)
        if not root.is_dir():
            print(f"bundle directory not found: {root}", file=sys.stderr)
            return 1
        _verify_bundle(root, args.version)
        return 0

    archive = Path(args.archive)
    if not archive.is_file():
        print(f"bundle archive not found: {archive}", file=sys.stderr)
        return 1
    # Verify the exact published archive after fresh extraction — never the
    # pre-archive staging directory (issue #169 review on #182).
    with tempfile.TemporaryDirectory() as tmp:
        root = extract_bundle(archive, Path(tmp) / "extracted")
        _verify_bundle(root, args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
