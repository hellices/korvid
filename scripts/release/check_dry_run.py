#!/usr/bin/env python3
"""Validate a main-only release dry run against the trusted branch head.

Usage: check_dry_run.py TRUSTED_REF [REPOSITORY] [PYPROJECT]
Prints the version on success (for later workflow steps).

The caller must refresh TRUSTED_REF from the remote before running this check.
`actions/checkout` can leave `refs/remotes/origin/main` pointing at the
dispatched SHA, which would make the HEAD comparison below vacuous.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from subprocess import CompletedProcess, run

from version_format import UNSUPPORTED_VERSION, is_supported_release_version


def _git(repo: Path, *args: str) -> CompletedProcess[str]:
    return run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def _read_version(pyproject: Path) -> str:
    try:
        data = tomllib.loads(pyproject.read_text())
        return str(data["project"]["version"])
    except (FileNotFoundError, OSError, KeyError, tomllib.TOMLDecodeError, TypeError) as exc:
        raise ValueError("project metadata is invalid") from exc


def main(argv: list[str]) -> int:
    if not 1 <= len(argv) <= 3:
        print(
            "usage: check_dry_run.py TRUSTED_REF [REPOSITORY] [PYPROJECT]",
            file=sys.stderr,
        )
        return 2

    trusted_ref = argv[0]
    repo = Path(argv[1]) if len(argv) > 1 else Path(".")
    pyproject = Path(argv[2]) if len(argv) > 2 else repo / "pyproject.toml"

    try:
        version = _read_version(pyproject)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # Never echo the rejected value: it is untrusted with respect to the shell
    # and file names this version later feeds.
    if not is_supported_release_version(version):
        print(UNSUPPORTED_VERSION, file=sys.stderr)
        return 1

    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        print("could not resolve checked-out HEAD", file=sys.stderr)
        return 1

    trusted_head = _git(repo, "rev-parse", trusted_ref)
    if trusted_head.returncode != 0:
        print("could not resolve trusted branch head", file=sys.stderr)
        return 1

    if head.stdout.strip() != trusted_head.stdout.strip():
        print("checked-out HEAD must match the trusted branch head", file=sys.stderr)
        return 1

    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
