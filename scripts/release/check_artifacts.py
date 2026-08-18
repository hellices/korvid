#!/usr/bin/env python3
"""Validate wheel and sdist release metadata before publication."""

from __future__ import annotations

import argparse
import email
import re
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


def _validate_install_guidance(artifact: Path, description: str) -> None:
    section_match = re.search(
        r"(?ms)^## Installation[ \t]*\n(?P<body>.*?)(?=^##[ \t]|\Z)",
        description,
    )
    if section_match is None:
        raise ValueError(
            f"{artifact.name}: the PyPI long description is missing ## Installation section"
        )
    section = section_match.group("body")
    pip_position = section.find("python -m pip install")
    isolated_positions = [
        position
        for installer in ("uv tool install", "pipx install")
        if (position := section.find(installer)) != -1
    ]
    if not isolated_positions or (pip_position != -1 and pip_position < min(isolated_positions)):
        raise ValueError(
            f"{artifact.name}: the PyPI Installation section must recommend"
            " an isolated application installer before pip"
        )


def _validate_project_page(artifact: Path, metadata: Message) -> None:
    """The fields that decide whether PyPI shows a page or a blank slab.

    None of this is recoverable after the fact: a released version number
    cannot be reuploaded, so a package that lands with no description keeps
    that page until the next release.
    """
    content_type = metadata.get("Description-Content-Type")
    # Compare the media type exactly. `startswith` also accepts
    # `text/markdown-broken`, which PyPI renders as plain text - a
    # fail-closed check that approves it is worse than no check, because it
    # reports success on the one property that cannot be fixed after upload.
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "text/markdown":
        raise ValueError(
            f"{artifact.name}: Description-Content-Type is {content_type!r};"
            " PyPI renders anything but text/markdown as plain text"
        )
    body = metadata.get_payload()
    description = body if isinstance(body, str) else ""
    if not description.strip():
        description = metadata.get("Description", "") or ""
    if len(description.strip()) < 200:
        raise ValueError(
            f"{artifact.name}: the long description is empty or truncated;"
            " it is the PyPI project page"
        )
    _validate_install_guidance(artifact, description)
    # A label alone is not a link: `Project-URL: Homepage` and
    # `Project-URL: Homepage,` both name the entry while pointing nowhere,
    # and either would render an empty sidebar.
    urls = set()
    for entry in metadata.get_all("Project-URL", []):
        label, _, target = entry.partition(",")
        if target.strip():
            urls.add(label.strip())
    required = {"Homepage", "Source", "Issues"}
    missing_urls = required - urls
    if missing_urls:
        raise ValueError(
            f"{artifact.name}: missing Project-URL entries: {sorted(missing_urls)};"
            " PyPI builds the project sidebar from them"
        )


def _validate_metadata(
    artifact: Path,
    metadata: Message,
    *,
    version: str,
    expected_dependencies: dict[str, set[str]],
) -> None:
    actual_version = metadata.get("Version")
    if actual_version != version:
        raise ValueError(
            f"{artifact.name}: metadata version {actual_version!r} does not match {version!r}"
        )
    _validate_project_page(artifact, metadata)
    provided = set(metadata.get_all("Provides-Extra", []))
    missing = set(expected_dependencies) - provided
    if missing:
        raise ValueError(f"{artifact.name}: missing Provides-Extra entries: {sorted(missing)}")
    requirements = metadata.get_all("Requires-Dist", [])
    actual_dependencies: dict[str, set[str]] = {extra: set() for extra in expected_dependencies}
    for requirement in requirements:
        marker = re.search(r"""extra\s*==\s*["']([^"']+)["']""", requirement)
        name = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
        if marker and name and marker.group(1) in actual_dependencies:
            actual_dependencies[marker.group(1)].add(re.sub(r"[-_.]+", "-", name.group(1)).lower())
    for extra, expected in expected_dependencies.items():
        actual = actual_dependencies[extra]
        if actual != expected:
            missing_dependencies = sorted(expected - actual)
            unexpected_dependencies = sorted(actual - expected)
            raise ValueError(
                f"{artifact.name}: extra {extra!r} dependency metadata differs;"
                f" missing={missing_dependencies}, unexpected={unexpected_dependencies}"
            )


def _expected_extra_dependencies(project: dict[str, object]) -> dict[str, set[str]]:
    """Expand pyproject extras into the dependency names Hatch emits.

    Hatch expands a self-reference such as `all = ["korvid[agent,mcp]"]`
    into the dependency union of those extras in wheel/sdist metadata.
    """
    raw_extras = project.get("optional-dependencies", {})
    if not isinstance(raw_extras, dict):
        return {}
    project_name = re.sub(r"[-_.]+", "-", str(project.get("name", ""))).lower()
    cache: dict[str, set[str]] = {}

    def expand(extra: str, active: frozenset[str] = frozenset()) -> set[str]:
        if extra in cache:
            return set(cache[extra])
        if extra in active:
            raise ValueError(f"cyclic optional-extra reference involving {extra!r}")
        requirements = raw_extras.get(extra, [])
        if not isinstance(requirements, list):
            raise ValueError(f"optional extra {extra!r} is not a requirement list")
        dependencies: set[str] = set()
        for requirement in requirements:
            match = re.match(
                r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[([A-Za-z0-9_,.-]+)\])?",
                str(requirement),
            )
            if match is None:
                continue
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            referenced = match.group(2)
            if name == project_name and referenced:
                for referenced_extra in referenced.split(","):
                    dependencies.update(expand(referenced_extra, active | {extra}))
            else:
                dependencies.add(name)
        cache[extra] = dependencies
        return set(dependencies)

    return {extra: expand(extra) for extra in raw_extras}


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
    expected_dependencies = _expected_extra_dependencies(project["project"])
    if not expected_dependencies:
        raise ValueError("pyproject.toml declares no optional extras to validate")

    _validate_metadata(
        wheels[0],
        _wheel_metadata(wheels[0]),
        version=args.version,
        expected_dependencies=expected_dependencies,
    )
    _validate_metadata(
        sdists[0],
        _sdist_metadata(sdists[0]),
        version=args.version,
        expected_dependencies=expected_dependencies,
    )
    print("wheel and sdist metadata verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
