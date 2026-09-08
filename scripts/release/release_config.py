#!/usr/bin/env python3
"""Read validated release metadata from ``pyproject.toml``.

Usage: release_config.py {version|upgrade-source} [--pyproject PYPROJECT]
Prints the selected value on success.
"""

from __future__ import annotations

import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from version_format import is_supported_release_version

USAGE = "usage: release_config.py {version|upgrade-source} [--pyproject PYPROJECT]"


@dataclass(frozen=True)
class ReleaseConfig:
    """Validated release metadata from ``pyproject.toml``."""

    version: str
    upgrade_source: str


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        path_stat = path.stat()
    except FileNotFoundError as exc:
        raise ValueError(f"{path} does not exist") from exc
    except PermissionError as exc:
        raise ValueError(f"permission denied reading {path}") from exc
    except OSError as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc

    if stat.S_ISDIR(path_stat.st_mode):
        raise ValueError(f"{path} is a directory, expected a pyproject.toml file")

    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except IsADirectoryError as exc:
        raise ValueError(f"{path} is a directory, expected a pyproject.toml file") from exc
    except PermissionError as exc:
        raise ValueError(f"permission denied reading {path}") from exc
    except OSError as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is not valid TOML: {exc}") from exc


def _release_table(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    project = document.get("project")
    if not isinstance(project, dict):
        raise ValueError("missing [project] table")

    tool = document.get("tool")
    korvid = tool.get("korvid") if isinstance(tool, dict) else None
    release = korvid.get("release") if isinstance(korvid, dict) else None
    if not isinstance(release, dict):
        raise ValueError("missing [tool.korvid.release] table")
    return project, release


def _validated_release_value(table: dict[str, Any], key: str, label: str) -> str:
    if key not in table:
        raise ValueError(f"missing {label}")
    value = table[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if not is_supported_release_version(value):
        raise ValueError(f"{label} must be a supported release version")
    return value


def _version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def read_release_config(pyproject: Path) -> ReleaseConfig:
    """Return validated release metadata from *pyproject*."""

    document = _read_toml(pyproject)
    project, release = _release_table(document)
    version = _validated_release_value(project, "version", "project.version")
    upgrade_source = _validated_release_value(
        release,
        "upgrade-from",
        "tool.korvid.release.upgrade-from",
    )
    if _version_tuple(upgrade_source) >= _version_tuple(version):
        raise ValueError("tool.korvid.release.upgrade-from must be older than project.version")
    return ReleaseConfig(version=version, upgrade_source=upgrade_source)


def _parse_args(argv: list[str]) -> tuple[str, Path]:
    if not argv:
        raise ValueError(USAGE)
    selector = argv[0]
    if selector not in {"version", "upgrade-source"}:
        raise ValueError(USAGE)
    if len(argv) == 1:
        return selector, Path("pyproject.toml")
    if len(argv) == 3 and argv[1] == "--pyproject":
        return selector, Path(argv[2])
    raise ValueError(USAGE)


def main(argv: list[str]) -> int:
    try:
        selector, pyproject = _parse_args(argv)
        config = read_release_config(pyproject)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if selector == "version":
        print(config.version)
    else:
        print(config.upgrade_source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
