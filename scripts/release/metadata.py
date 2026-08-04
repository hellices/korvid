#!/usr/bin/env python3
"""Release metadata (issue #169): records exactly what was built from what.

Writes JSON with the version, source commit, Python version, runner
platform, and the sha256 of the committed lockfile the artifacts were
resolved from.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import tomllib
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", default="pyproject.toml")
    parser.add_argument("--lockfile", default="uv.lock")
    parser.add_argument("--output", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    version = str(tomllib.loads(Path(args.pyproject).read_text())["project"]["version"])
    lock_digest = hashlib.sha256(Path(args.lockfile).read_bytes()).hexdigest()
    payload = {
        "version": version,
        "commit": args.commit,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "lockfile_sha256": lock_digest,
    }
    Path(args.output).write_text(json.dumps(payload, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
