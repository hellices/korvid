#!/usr/bin/env python3
"""Validate that a release tag is annotated and comes from a trusted ref."""

from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("trusted_ref")
    parser.add_argument("repository", nargs="?", default=".")
    parser.add_argument(
        "--expected-commit",
        help="fail if the dereferenced tag moved since initial verification",
    )
    args = parser.parse_args(argv)
    tag = args.tag
    trusted_ref = args.trusted_ref
    repo = Path(args.repository)
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
    if args.expected_commit is not None and commit != args.expected_commit:
        print(
            f"release tag {tag!r} changed from verified commit; refusing publication",
            file=sys.stderr,
        )
        return 1
    reachable = _git(repo, "merge-base", "--is-ancestor", commit, trusted_ref)
    if reachable.returncode != 0:
        print(
            f"release tag {tag!r} is not reachable from trusted ref {trusted_ref!r}",
            file=sys.stderr,
        )
        return 1
    print(commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
