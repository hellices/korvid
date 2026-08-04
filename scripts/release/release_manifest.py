#!/usr/bin/env python3
"""Collect release assets and generate their complete SHA256SUMS."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from bundle import write_sha256sums


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    artifacts = Path(args.artifacts)
    output = Path(args.output)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    copied: list[str] = []
    for source in sorted(path for path in artifacts.rglob("*") if path.is_file()):
        if source.name == "SHA256SUMS":
            continue
        target = output / source.name
        if target.exists():
            raise ValueError(f"release assets collide on filename {source.name!r}")
        shutil.copy2(source, target)
        copied.append(target.name)
    if not copied:
        raise ValueError(f"no release assets found under {artifacts}")
    write_sha256sums(output, copied)
    print(output / "SHA256SUMS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
