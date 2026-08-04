#!/usr/bin/env python3
"""Validate that a release tag is annotated and comes from a trusted ref."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(
            "usage: check_source.py TAG TRUSTED_REF [repository]",
            file=sys.stderr,
        )
        return 2
    tag, trusted_ref = argv[:2]
    repo = Path(argv[2]) if len(argv) == 3 else Path.cwd()
    tag_ref = f"refs/tags/{tag}"

    tag_type = _git(repo, "cat-file", "-t", tag_ref)
    if tag_type.returncode != 0:
        print(f"release tag {tag!r} does not exist", file=sys.stderr)
        return 1
    if tag_type.stdout.strip() != "tag":
        print(f"release tag {tag!r} must be an annotated tag", file=sys.stderr)
        return 1

    tagged_commit = _git(repo, "rev-list", "-n", "1", tag_ref)
    if tagged_commit.returncode != 0:
        print(f"could not resolve release tag {tag!r}", file=sys.stderr)
        return 1
    commit = tagged_commit.stdout.strip()
    reachable = _git(repo, "merge-base", "--is-ancestor", commit, trusted_ref)
    if reachable.returncode != 0:
        print(
            f"tagged commit {commit} is not reachable from trusted ref {trusted_ref!r}",
            file=sys.stderr,
        )
        return 1
    print(commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
