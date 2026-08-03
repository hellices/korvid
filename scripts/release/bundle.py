#!/usr/bin/env python3
"""Assemble a verifiable offline installation bundle (issue #169).

Layout:

    korvid-X.Y.Z-offline-<platform>-py<version>/
      wheels/           # korvid + every locked dependency for [all,entra]
      install.sh
      install.ps1
      SHA256SUMS        # covers wheels, sbom, installers, README
      sbom.cdx.json
      README.txt

Linux bundles archive as .tar.gz, Windows bundles as .zip. Stdlib only —
this runs on bare GitHub runners before any project install.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

_INSTALL_SH = """\
#!/bin/sh
# Offline install: no index, no network — wheels/ is the whole universe.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
version={version}
python3 -m pip install --no-index --find-links "$here/wheels" "korvid[all,entra]==$version"
"""

_INSTALL_PS1 = """\
# Offline install: no index, no network — wheels/ is the whole universe.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$version = "{version}"
python -m pip install --no-index --find-links "$here\\wheels" "korvid[all,entra]==$version"
"""

_README = """\
korvid {version} — offline installation bundle ({platform_tag}, Python {python_tag})

1. Verify integrity:      sha256sum -c SHA256SUMS   (certutil/Get-FileHash on Windows)
2. Install (no network):  ./install.sh              (install.ps1 on Windows)
   or directly:
   python -m pip install --no-index --find-links ./wheels "korvid[all,entra]=={version}"

This bundle contains korvid and its locked Python dependencies only.
Helm, Telepresence, debug images, model artifacts, and Kubernetes
credentials are separate operator-supplied dependencies — see
docs/airgap.md in the repository.
"""


def write_sha256sums(root: Path, names: list[str]) -> Path:
    """`sha256sum -c`-compatible sums over *names* (relative to *root*)."""
    lines = []
    for name in sorted(names):
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    sums = root / "SHA256SUMS"
    sums.write_text("\n".join(lines) + "\n")
    return sums


def main(argv: list[str]) -> str | None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform-tag", required=True, help="e.g. linux-x86_64")
    parser.add_argument("--python-tag", required=True, help="e.g. 3.12")
    parser.add_argument("--wheels", required=True, help="wheelhouse directory")
    parser.add_argument("--sbom", required=True, help="CycloneDX JSON path")
    parser.add_argument("--output", required=True, help="output directory")
    args = parser.parse_args(argv)

    name = f"korvid-{args.version}-offline-{args.platform_tag}-py{args.python_tag}"
    out = Path(args.output)
    root = out / name
    if root.exists():
        shutil.rmtree(root)
    wheels_dst = root / "wheels"
    wheels_dst.mkdir(parents=True)
    for wheel in sorted(Path(args.wheels).glob("*.whl")):
        shutil.copy2(wheel, wheels_dst / wheel.name)
    (root / "install.sh").write_text(_INSTALL_SH.format(version=args.version))
    (root / "install.sh").chmod(0o755)
    (root / "install.ps1").write_text(_INSTALL_PS1.format(version=args.version))
    shutil.copy2(args.sbom, root / "sbom.cdx.json")
    (root / "README.txt").write_text(
        _README.format(
            version=args.version,
            platform_tag=args.platform_tag,
            python_tag=args.python_tag,
        )
    )
    members = [f"wheels/{w.name}" for w in sorted(wheels_dst.glob("*.whl"))]
    members += ["install.sh", "install.ps1", "sbom.cdx.json", "README.txt"]
    write_sha256sums(root, members)

    if args.platform_tag.startswith("windows"):
        archive = out / f"{name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(root.rglob("*")):
                zf.write(path, path.relative_to(out))
    else:
        archive = out / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(root, arcname=name)
    print(archive)
    return str(archive)


if __name__ == "__main__":
    main(sys.argv[1:])
