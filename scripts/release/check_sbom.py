#!/usr/bin/env python3
"""Validate that an SBOM covers korvid and the exported runtime graph."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", line)
        if match:
            names.add(_normalize(match.group(1)))
    return names


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sbom", required=True)
    parser.add_argument("--requirements", required=True)
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.sbom).read_text())
    root = payload.get("metadata", {}).get("component", {})
    if _normalize(str(root.get("name", ""))) != "korvid":
        raise ValueError("SBOM root component is not korvid")
    components = {
        _normalize(str(component.get("name", "")))
        for component in payload.get("components", [])
        if isinstance(component, dict)
    }
    missing = _requirements(Path(args.requirements)) - components
    if missing:
        raise ValueError(f"SBOM is missing locked dependencies: {sorted(missing)}")
    print(f"SBOM verified: korvid + {len(components)} dependency components")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
