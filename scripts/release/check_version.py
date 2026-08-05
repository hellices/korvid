#!/usr/bin/env python3
"""Release version gate (issue #169): the annotated tag must match
pyproject.toml exactly, or nothing is built or published.

Usage: check_version.py vX.Y.Z [pyproject.toml]
Prints the version on success (for later workflow steps).
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from version_format import UNSUPPORTED_VERSION, is_supported_release_version


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_version.py vX.Y.Z [pyproject.toml]", file=sys.stderr)
        return 2
    tag = argv[0]
    pyproject = Path(argv[1]) if len(argv) > 1 else Path("pyproject.toml")
    data = tomllib.loads(pyproject.read_text())
    version = str(data["project"]["version"])
    # Validate the shape before the value reaches the shell, $GITHUB_OUTPUT,
    # or artifact file names. Never echo the rejected value.
    if not is_supported_release_version(version):
        print(UNSUPPORTED_VERSION, file=sys.stderr)
        return 1
    if not tag.startswith("v"):
        print(f"release tag {tag!r} must look like v{version}", file=sys.stderr)
        return 1
    if tag[1:] != version:
        print(
            f"release tag {tag!r} does not match pyproject.toml version {version!r}",
            file=sys.stderr,
        )
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
