#!/usr/bin/env python3
"""Validate wheel and sdist release metadata before publication."""

from __future__ import annotations

import argparse
import email
import sys
import tarfile
import tomllib
import zipfile
from email.message import Message
from pathlib import Path


def _wheel_metadata(path: Path) -> Message:
    with zipfile.ZipFile(path) as wheel:
        names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError(f"{path.name}: expected exactly one .dist-info/METADATA")
        return email.message_from_bytes(wheel.read(names[0]))


def _sdist_metadata(path: Path) -> Message:
    with tarfile.open(path) as sdist:
        members = [member for member in sdist.getmembers() if member.name.endswith("/PKG-INFO")]
        if len(members) != 1:
            raise ValueError(f"{path.name}: expected exactly one PKG-INFO")
        extracted = sdist.extractfile(members[0])
        if extracted is None:
            raise ValueError(f"{path.name}: could not read PKG-INFO")
        return email.message_from_bytes(extracted.read())


def _validate_metadata(
    artifact: Path,
    metadata: Message,
    *,
    version: str,
    expected_extras: set[str],
) -> None:
    actual_version = metadata.get("Version")
    if actual_version != version:
        raise ValueError(
            f"{artifact.name}: metadata version {actual_version!r} does not match {version!r}"
        )
    provided = set(metadata.get_all("Provides-Extra", []))
    missing = expected_extras - provided
    if missing:
        raise ValueError(f"{artifact.name}: missing Provides-Extra entries: {sorted(missing)}")
    requirements = metadata.get_all("Requires-Dist", [])
    for extra in expected_extras:
        markers = (f'extra == "{extra}"', f"extra == '{extra}'")
        if not any(marker in requirement for requirement in requirements for marker in markers):
            raise ValueError(
                f"{artifact.name}: extra {extra!r} has no corresponding Requires-Dist metadata"
            )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--pyproject", default="pyproject.toml")
    args = parser.parse_args(argv)

    dist = Path(args.dist)
    wheels = sorted(dist.glob("korvid-*.whl"))
    sdists = sorted(dist.glob("korvid-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("dist must contain exactly one korvid wheel and one sdist")
    project = tomllib.loads(Path(args.pyproject).read_text())
    expected_extras = set(project["project"].get("optional-dependencies", {}))
    if not expected_extras:
        raise ValueError("pyproject.toml declares no optional extras to validate")

    _validate_metadata(
        wheels[0],
        _wheel_metadata(wheels[0]),
        version=args.version,
        expected_extras=expected_extras,
    )
    _validate_metadata(
        sdists[0],
        _sdist_metadata(sdists[0]),
        version=args.version,
        expected_extras=expected_extras,
    )
    print("wheel and sdist metadata verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
