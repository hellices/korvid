#!/usr/bin/env python3
"""Compare two flat release-asset directories byte for byte."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


def _assets(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise ValueError(f"release asset directory not found: {root}")
    assets: dict[str, str] = {}
    for path in sorted(root.iterdir()):
        if not path.is_file():
            raise ValueError(f"release asset directory is not flat: {path.name}")
        assets[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return assets


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("expected")
    parser.add_argument("actual")
    args = parser.parse_args(argv)
    expected = _assets(Path(args.expected))
    actual = _assets(Path(args.actual))
    if expected.keys() != actual.keys():
        missing = sorted(expected.keys() - actual.keys())
        unexpected = sorted(actual.keys() - expected.keys())
        raise ValueError(f"release asset names differ; missing={missing}, unexpected={unexpected}")
    changed = sorted(name for name in expected if expected[name] != actual[name])
    if changed:
        raise ValueError(f"release asset bytes differ: {changed}")
    print(f"release assets verified: {len(expected)} identical files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
